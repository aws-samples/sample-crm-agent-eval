#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: no pipeline under `pipefail` feeds a consumer that can exit before reading all its input.

Under `set -o pipefail` a consumer that stops reading early (`grep -q`, `grep -m N`, `head`,
`sed Nq`) makes the producer die of SIGPIPE (status 141), and pipefail then reports the pipeline as
failed even when the consumer matched. A finding is any such command in a non-first position of a
pipeline, in code that runs with pipefail enabled.

Scope, derived from `git ls-files`:

- shell files: every tracked file whose first line is a shell shebang (`^#!.*sh`, the
  `verify-shellcheck` predicate) plus every tracked `*.sh`. Only text after the first
  `set … -o pipefail` / `set -…o pipefail` line is checked;
- `Makefile` / `*.mk` recipes: each logical recipe line runs in its own shell, so a line is checked
  when it enables pipefail itself or when `SHELL` / `.SHELLFLAGS` carries `pipefail`;
- CI scripts: GitHub workflow `run:` blocks (pipefail when the block enables it or the effective
  `shell` is `bash`, which GitHub runs with `-eo pipefail`) and GitLab job
  `before_script` + `script` (one shell) and `after_script` blocks.

Allowed: a pipeline inside a command substitution whose status is masked by a trailing `|| true` or
`|| :`, because the captured stdout is the value (`x="$(a | head -1 || true)"`). A masked pipeline
used as a condition is still a finding. Here-strings, here-document bodies, quoted text and
comments are not pipelines. Not covered: `awk … exit`, `grep -l`, `set +o pipefail`, pipelines in
`eval` strings, and `case` patterns inside a command substitution.

Usage: `python3 scripts/check_pipefail_sigpipe.py [--repo-root DIR]`

Exit codes: 0 pass; 1 one `path:line` line per finding; 2 usage or environment error (not a git
work tree, unreadable file, unparsable CI YAML); 3 SKIP: no tracked shell file, so nothing was
verified. `scripts/run_gates.py` renders 3 as `[SKIP]`.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import string
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import proc_run  # noqa: E402

#: Exit status for "ran, verified nothing". Must equal `scripts/run_gates.py`'s
#: `GATE_SKIP_EXIT_STATUS`.
SKIP_EXIT_STATUS = 3

_SHEBANG_RE = re.compile(r"^#!.*sh")
_MAKE_SHELL_PIPEFAIL_RE = re.compile(
    r"^(?:SHELL|\.SHELLFLAGS)\s*[:+?]?=.*\bpipefail\b", re.MULTILINE
)
_MASK_RE = re.compile(r"[ \t]*(?:true|:)(?=[ \t]*(?:$|[)`;&|\n#]))")
_LEADING_BLANK_RE = re.compile(r"(?:\s|\\\n)*")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SED_QUIT_RE = re.compile(r"(?:^|[;{}\s/])(?:\d+|\$)?[qQ](?:\s*\d+)?\s*(?:;|}|$)")
_GREP_NAMES = frozenset({"grep", "egrep", "fgrep"})
_GREP_ARG_SHORT = frozenset("efABCDd")
_GREP_ARG_LONG = frozenset(
    {"regexp", "file", "after-context", "before-context", "context", "devices", "directories"}
)
_PREFIX_WORDS = frozenset({"!", "command", "exec", "env", "time"})
_GITLAB_RESERVED = frozenset(
    {"image", "stages", "variables", "default", "include", "workflow", "services", "cache"}
)


class GateError(Exception):
    """The gate could not establish its scope or read a file in it."""


@dataclass(frozen=True)
class Pipeline:
    """One pipeline found in shell text: its commands, position, and capture/mask context."""

    offset: int
    commands: tuple[str, ...]
    in_substitution: bool
    status_masked: bool


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    consumer: str
    statement: str

    def render(self) -> str:
        return (
            f"{self.path}:{self.line}: `{self.consumer}` reads part of a pipe under pipefail "
            f"(producer can die of SIGPIPE): {self.statement}"
        )


class _Scanner:
    """Quote-, comment-, substitution- and here-document-aware pipeline finder for shell text."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.n = len(text)
        self.found: list[Pipeline] = []
        self.heredocs: list[tuple[str, bool]] = []

    def run(self) -> list[Pipeline]:
        self._scan(0, in_subst=False, closer=None)
        return sorted(self.found, key=lambda p: p.offset)

    def _skip_single(self, i: int) -> int:
        end = self.text.find("'", i)
        return self.n if end < 0 else end + 1

    def _skip_braces(self, i: int) -> int:
        depth = 1
        while i < self.n and depth:
            c = self.text[i]
            if c == "\\":
                i += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        return i

    def _skip_double(self, i: int) -> int:
        while i < self.n:
            c = self.text[i]
            if c == "\\":
                i += 2
            elif c == '"':
                return i + 1
            elif self.text.startswith("$(", i):
                i = self._scan(i + 2, in_subst=True, closer=")")
            elif self.text.startswith("${", i):
                i = self._skip_braces(i + 2)
            elif c == "`":
                i = self._scan(i + 1, in_subst=True, closer="`")
            else:
                i += 1
        return i

    def _read_heredoc_word(self, i: int) -> int:
        strip_tabs = self.text.startswith("-", i)
        if strip_tabs:
            i += 1
        while i < self.n and self.text[i] in " \t":
            i += 1
        match = re.match(r"""(['"]?)([A-Za-z0-9_.-]+)\1""", self.text[i:])
        if match is None:
            return i
        self.heredocs.append((match.group(2), strip_tabs))
        return i + match.end()

    def _skip_heredoc_bodies(self, i: int) -> int:
        for word, strip_tabs in self.heredocs:
            while i < self.n:
                end = self.text.find("\n", i)
                line = self.text[i : self.n if end < 0 else end]
                i = self.n if end < 0 else end + 1
                if (line.lstrip("\t") if strip_tabs else line) == word:
                    break
        self.heredocs.clear()
        return i

    def _scan(self, i: int, *, in_subst: bool, closer: str | None) -> int:
        text = self.text
        commands: list[str] = []
        cmd_start = pipe_start = i

        def end_pipeline(j: int, masked: bool = False) -> None:
            all_commands = [*commands, text[cmd_start:j]]
            if len(all_commands) > 1:
                self.found.append(Pipeline(pipe_start, tuple(all_commands), in_subst, masked))
            commands.clear()

        while i < self.n:
            c = text[i]
            if c == "\\":
                i += 2
            elif c == "'":
                i = self._skip_single(i + 1)
            elif c == '"':
                i = self._skip_double(i + 1)
            elif text.startswith("$(", i):
                i = self._scan(i + 2, in_subst=True, closer=")")
            elif text.startswith("${", i):
                i = self._skip_braces(i + 2)
            elif c == "`" and closer != "`":
                i = self._scan(i + 1, in_subst=True, closer="`")
            elif c == "#" and (i == 0 or text[i - 1] in " \t\n;(|&"):
                end = text.find("\n", i)
                i = self.n if end < 0 else end
            elif text.startswith("<<<", i):
                i += 3
            elif text.startswith("<<", i):
                i = self._read_heredoc_word(i + 2)
            elif closer is not None and c == closer:
                end_pipeline(i)
                return i + 1
            elif text.startswith("||", i):
                end_pipeline(i, masked=_MASK_RE.match(text, i + 2) is not None)
                i += 2
                cmd_start = pipe_start = i
            elif c == "|":
                commands.append(text[cmd_start:i])
                i += 2 if text.startswith("|&", i) else 1
                cmd_start = i
            elif c == "&" and (text[i - 1 : i] in (">", "<") or text.startswith("&>", i)):
                i += 1
            elif c in ";&\n(){}":
                end_pipeline(i)
                i += 1
                if c == "\n" and self.heredocs:
                    i = self._skip_heredoc_bodies(i)
                cmd_start = pipe_start = i
            else:
                i += 1
        end_pipeline(self.n)
        return self.n


def find_pipelines(text: str) -> list[Pipeline]:
    """Every pipeline of two or more commands in `text`, in source order."""
    return _Scanner(text).run()


def _words(command: str) -> list[str]:
    try:
        words = shlex.split(command, comments=False, posix=True)
    except ValueError:
        words = command.split()
    while words and (_ASSIGNMENT_RE.match(words[0]) or words[0] in _PREFIX_WORDS):
        words.pop(0)
    return words


def _grep_reads_partially(args: list[str]) -> bool:
    skip_next = False
    for word in args:
        if skip_next:
            skip_next = False
            continue
        if word == "--":
            return False
        if word.startswith("--"):
            name, has_value, _ = word[2:].partition("=")
            if name in ("quiet", "silent", "max-count"):
                return True
            skip_next = name in _GREP_ARG_LONG and not has_value
        elif word.startswith("-") and len(word) > 1:
            for k, ch in enumerate(word[1:], start=1):
                if ch in "qm":
                    return True
                if ch in _GREP_ARG_SHORT:
                    skip_next = k == len(word) - 1
                    break
    return False


def early_exit_consumer(command: str) -> str | None:
    """The command's name when it can stop reading stdin before EOF, else ``None``."""
    words = _words(command)
    if not words:
        return None
    name = PurePosixPath(words[0]).name
    if name in _GREP_NAMES and _grep_reads_partially(words[1:]):
        return name
    if name == "head":
        return name
    if name == "sed" and any(
        _SED_QUIT_RE.search(w) for w in words[1:] if not w.startswith("-") or w == "-e"
    ):
        return name
    return None


def findings_in(
    text: str,
    *,
    path: str,
    pipefail_from: int,
    line_of: Callable[[int], int] | None = None,
) -> list[Finding]:
    """Findings for pipelines in `text` starting at or after offset `pipefail_from`.

    `line_of(offset)` maps an offset to a 1-based line in `path`; default: the line within `text`.
    """
    results: list[Finding] = []
    for pipeline in find_pipelines(text):
        if pipeline.offset < pipefail_from:
            continue
        if pipeline.in_substitution and pipeline.status_masked:
            continue
        for command in pipeline.commands[1:]:
            consumer = early_exit_consumer(command)
            if consumer is None:
                continue
            lead = _LEADING_BLANK_RE.match(pipeline.commands[0])
            offset = pipeline.offset + (lead.end() if lead else 0)
            line = line_of(offset) if line_of else text.count("\n", 0, offset) + 1
            statement = " | ".join(c.strip() for c in pipeline.commands)
            results.append(Finding(path, line, consumer, " ".join(statement.split())[:200]))
            break
    return results


def _is_flag(word: str) -> bool:
    return len(word) > 1 and word[0] in "-+" and all(c in string.ascii_letters for c in word[1:])


def _sets_pipefail(line: str) -> bool:
    """True when `line`, split on blanks, is `[-] set [flags…] -…o pipefail…`."""
    words = [w for w in line.replace("\t", " ").split(" ") if w]
    if words[:1] == ["-"]:
        words = words[1:]
    if words[:1] != ["set"]:
        return False
    for i, word in enumerate(words[1:], start=1):
        if not _is_flag(word):
            return False
        if word[0] == "-" and word.endswith("o") and i + 1 < len(words):
            after = words[i + 1]
            if after.startswith("pipefail") and not (after[8:9].isalnum() or after[8:9] == "_"):
                return True
    return False


def _pipefail_offset(text: str) -> int | None:
    offset = 0
    for line in text.split("\n"):
        if _sets_pipefail(line):
            return offset
        offset += len(line) + 1
    return None


def check_shell_file(path: str, text: str) -> list[Finding]:
    start = _pipefail_offset(text)
    return [] if start is None else findings_in(text, path=path, pipefail_from=start)


def _make_recipe_units(text: str) -> Iterator[tuple[int, str]]:
    """`(first line number, shell text)` for each logical recipe line of a Makefile."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not lines[i].startswith("\t"):
            i += 1
            continue
        start = i
        unit = [lines[i][1:].lstrip("@+-")]
        while lines[i].endswith("\\") and i + 1 < len(lines):
            i += 1
            unit.append(lines[i][1:] if lines[i].startswith("\t") else lines[i])
        yield start + 1, "\n".join(unit).replace("$$", "$")
        i += 1


def check_makefile(path: str, text: str) -> list[Finding]:
    forced = _MAKE_SHELL_PIPEFAIL_RE.search(text) is not None
    results: list[Finding] = []
    for first_line, unit in _make_recipe_units(text):
        start = 0 if forced else _pipefail_offset(unit)
        if start is None:
            continue
        for f in findings_in(unit, path=path, pipefail_from=start):
            results.append(Finding(path, first_line + f.line - 1, f.consumer, f.statement))
    return results


def _ci_line_mapper(file_lines: list[str], block: str) -> Callable[[int], int]:
    def line_of(offset: int) -> int:
        target = block.splitlines()[block.count("\n", 0, offset)].strip()
        for number, line in enumerate(file_lines, start=1):
            if target and target in line:
                return number
        return 1

    return line_of


def _flatten(items: Any) -> list[str]:
    if isinstance(items, str):
        return [items]
    if isinstance(items, list):
        return [s for item in items for s in _flatten(item)]
    return []


def _github_blocks(data: dict[str, Any]) -> Iterator[tuple[str, bool]]:
    workflow_shell = ((data.get("defaults") or {}).get("run") or {}).get("shell")
    for job in (data.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        job_shell = ((job.get("defaults") or {}).get("run") or {}).get("shell", workflow_shell)
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                shell = step.get("shell", job_shell)
                if shell is None or str(shell).split()[0] in ("bash", "sh"):
                    yield step["run"], shell == "bash"


def _gitlab_blocks(data: dict[str, Any]) -> Iterator[tuple[str, bool]]:
    default_value = data.get("default")
    default: dict[str, Any] = default_value if isinstance(default_value, dict) else {}
    global_before = default.get("before_script", data.get("before_script", []))
    for key, job in data.items():
        if key in _GITLAB_RESERVED or not isinstance(job, dict) or "script" not in job:
            continue
        before = job.get("before_script", global_before)
        yield "\n".join(_flatten(before) + _flatten(job["script"])), False
        after = job.get("after_script", default.get("after_script"))
        if after:
            yield "\n".join(_flatten(after)), False


def check_ci_file(path: str, text: str) -> list[Finding]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GateError(f"{path}: not parsable YAML: {exc}") from exc
    if not isinstance(data, dict):
        return []
    blocks = _gitlab_blocks(data) if path.endswith(".gitlab-ci.yml") else _github_blocks(data)
    file_lines = text.splitlines()
    results: list[Finding] = []
    for block, forced in blocks:
        start = 0 if forced else _pipefail_offset(block)
        if start is not None:
            mapper = _ci_line_mapper(file_lines, block)
            results.extend(findings_in(block, path=path, pipefail_from=start, line_of=mapper))
    return results


@dataclass(frozen=True)
class Scope:
    shell: list[str]
    make: list[str]
    ci: list[str]


def _is_ci_file(path: str) -> bool:
    return path == ".gitlab-ci.yml" or (
        path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))
    )


def derive_scope(repo_root: Path) -> Scope:
    """Tracked shell files, Makefiles and CI definitions under `repo_root`.

    Raises :class:`GateError` when `repo_root` is not a git work tree or git cannot run.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_INDEX_FILE")}
    try:
        listed = proc_run.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"], capture_output=True, env=env
        )
    except OSError as exc:
        raise GateError(f"could not run git: {exc}") from exc
    if listed.returncode != 0:
        stderr = listed.stderr.decode("utf-8", errors="replace").strip()
        raise GateError(f"git ls-files exited {listed.returncode}: {stderr[:400]}")
    scope = Scope([], [], [])
    for path in (os.fsdecode(raw) for raw in listed.stdout.split(b"\0") if raw):
        full = repo_root / path
        if not full.is_file():
            continue
        name = PurePosixPath(path).name
        if name == "Makefile" or name.endswith(".mk"):
            scope.make.append(path)
        elif _is_ci_file(path):
            scope.ci.append(path)
        elif path.endswith(".sh") or _has_shell_shebang(full):
            scope.shell.append(path)
    return scope


def _has_shell_shebang(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            first = handle.readline(256)
    except OSError as exc:
        raise GateError(f"{path}: unreadable: {exc}") from exc
    return _SHEBANG_RE.match(first.decode("utf-8", errors="replace")) is not None


def _read(repo_root: Path, path: str) -> str:
    try:
        return (repo_root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GateError(f"{path}: unreadable: {exc}") from exc


def run_check(repo_root: Path) -> tuple[Scope, list[Finding]]:
    """The derived scope and every finding in it. Raises :class:`GateError`."""
    scope = derive_scope(repo_root)
    findings: list[Finding] = []
    for path in scope.shell:
        findings.extend(check_shell_file(path, _read(repo_root, path)))
    for path in scope.make:
        findings.extend(check_makefile(path, _read(repo_root, path)))
    for path in scope.ci:
        findings.extend(check_ci_file(path, _read(repo_root, path)))
    return scope, findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="The git work tree to check (default: this repository).",
    )
    args = parser.parse_args(argv)
    try:
        scope, findings = run_check(args.repo_root)
    except GateError as exc:
        print(f"check_pipefail_sigpipe: ERROR — {exc}", file=sys.stderr)
        return 2
    if not scope.shell:
        print(
            f"[SKIP] check_pipefail_sigpipe: no tracked shell file in {args.repo_root}. "
            "Nothing was verified."
        )
        return SKIP_EXIT_STATUS
    for finding in findings:
        print(finding.render())
    summary = (
        f"{len(scope.shell)} shell files, {len(scope.make)} Makefiles, {len(scope.ci)} CI files"
    )
    if findings:
        print(f"check_pipefail_sigpipe: FAIL — {len(findings)} finding(s) in {summary}")
        return 1
    print(f"check_pipefail_sigpipe: OK — {summary}, no early-exit consumer under pipefail")
    return 0


if __name__ == "__main__":
    sys.exit(main())
