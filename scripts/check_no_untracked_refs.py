#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: no comment, docstring or tracked document references content that is not tracked.

Spec trees, decision logs and review files live only in untracked local directories, so a
reference into them resolves in no clone. Scope is `git ls-files` under `--repo-root`; files with a
NUL byte or invalid UTF-8 are skipped. The scanned text is:

- Python: `#` comments, docstrings and string literals, f-strings included (:mod:`tokenize` and
  :mod:`ast`);
- TypeScript / JavaScript / CSS: `//` and `/* */` comments and string literals;
- Terraform / HCL: `#`, `//` and `/* */` comments and string literals;
- YAML: `#` comments and every value;
- JSON and NDJSON: every string, keys included;
- shell, TOML, Makefile, Dockerfile and other `#`-comment files: `#` comments;
- HTML, SVG and draw.io: `<!-- -->` comments;
- Markdown: the whole file.

String literals are skipped when :data:`SCAN_STRING_LITERALS` is off, and in this gate's own file,
whose pattern lists necessarily spell the references out. A reference is a match
of :data:`REFERENCE_RE` (requirement, decision and acceptance ids, section signs, review-finding
ids, untracked file names, and the spec slugs in :data:`SPEC_SLUGS`). :data:`ALLOWED_IDS` lists ids
that tracked content defines, and :data:`EXEMPTIONS` lists reference text a test reads; each entry
names the tracked file it depends on, and an entry whose file no longer carries it is reported.

Usage: `python3 scripts/check_no_untracked_refs.py [--repo-root DIR]`

Exit codes: 0 pass; 1 one `path:line: <token> (<kind>)` line per finding; 2 usage or environment
error (not a git work tree, unreadable file); 3 SKIP: no scannable tracked file, so nothing was
verified. `scripts/run_gates.py` renders 3 as `[SKIP]`.
"""

from __future__ import annotations

import argparse
import ast
import io
import os
import re
import sys
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import proc_run  # noqa: E402

#: Exit status for "ran, verified nothing". Must equal `scripts/run_gates.py`'s
#: `GATE_SKIP_EXIT_STATUS`.
SKIP_EXIT_STATUS: Final = 3

#: Whether string literals are scanned as well as comments and docstrings (Python, TypeScript /
#: JavaScript and Terraform).
SCAN_STRING_LITERALS: Final = True

#: This gate's own path; its string literals are the pattern lists and are not scanned.
SELF_PATH: Final = "scripts/check_no_untracked_refs.py"

#: Slugs of the local, untracked spec trees; a slug used as a reference points into one of them.
SPEC_SLUGS: Final[tuple[str, ...]] = (
    "app-tag-arm64-ci",
    "arch-dg1",
    "ci-secret-scan-scope",
    "cognito-admin-only-signup",
    "crm-eval-v2",
    "deps-bump-1",
    "eval-fidelity",
    "fixes-feats-1",
    "fixes-feats-2",
    "fixes-feats-3",
    "fixes-feats-4",
    "fixes-feats-5",
    "live-hardening",
    "matrix-tier-summary",
    "model-update-1",
    "model-upgrade",
    "region-agnostic",
    "repo-cleanup",
    "security-scan-remediation",
    "security-scan-remediation-2",
    "security-scan-remediation-3",
    "test1-followups",
    "test1-phase1",
    "test1-phase2",
    "vendor-sdk-transport",
)

_ID_PATTERNS: Final[tuple[str, ...]] = (
    r"FR-\d+(?:\.\d+)*",
    r"NFR-\d+",
    r"D-\d{1,3}[a-z]?",
    r"AC-\d+",
    r"FU-\d+",
    r"R-\d+",
    r"R\d+-[A-Z]+\d+",
    r"FX[A-Z]*-?[A-Z]?\d+",
    r"E2E\d*-[A-Z]+\d*",
    r"L\d+-[A-Z]",
    r"SA-\d+",
    r"SE-\d+",
    r"SFH-\d+",
    r"AT-\d+",
    r"AN-\d+",
    r"AP-\d+",
    r"P-C?\d+",
    r"G\d+[A-Z]?-[A-Z]?\d+",
    r"G\d+[A-Z]?-[A-Z]+-\d+",
    r"G\d+C\d+-[A-Z]\d+",
    r"[HW]\d{1,2}",
    r"[CHM]-\d+",
    r"CIL-\d+",
    r"S-\d+",
    r"F-\d+",
    r"R\d+-\d+",
)

_FILE_PATTERNS: Final[tuple[str, ...]] = (
    r"\.claude/",
    r"decisions\.md",
    r"design\.md",
    r"(?<![\w-])spec\.md",
    r"(?<![\w-])review(?:-e2e)?\.md",
    r"test-evidence",
)

#: Every reference shape. Ids and slugs are matched as whole tokens.
REFERENCE_RE: Final = re.compile(
    r"(?<![\w-])(?:"
    + "|".join(_ID_PATTERNS)
    + "|"
    + "|".join(re.escape(slug) for slug in sorted(SPEC_SLUGS, key=len, reverse=True))
    + r")(?![\w-])"
    + "|"
    + "|".join(_FILE_PATTERNS)
    + "|§"
)


@dataclass(frozen=True, slots=True)
class AllowedId:
    """An id shape that tracked content defines, so a reference to it resolves in any clone.

    Attributes:
        pattern: Regular expression a reported token must fully match to be allowed.
        defined_in: The tracked file that defines the ids; it must carry a match.
    """

    pattern: str
    defined_in: str


#: Ids defined in tracked files. A token matching one of these is not a reference.
ALLOWED_IDS: Final[tuple[AllowedId, ...]] = (
    AllowedId(r"PD-\d{2}", "docs/planted-defects.md"),
    AllowedId(r"gt-\d{2}", "src/eval/golden_tasks.yaml"),
    AllowedId(r"SFN-LOGS-1|SFN-XRAY-1|SWEEP-CONTROL-1", "docs/security-baseline.md"),
    AllowedId(r"R(?:[1-9]|10)(?:-R(?:[1-9]|10))?", "scripts/check_doc_figures.py"),
    AllowedId(r"A[1-5]|D(?:[1-9]|1\d|2[0-3])", "scripts/smoke/relay_iam.py"),
    AllowedId(r"H-(?:[2-9]|10)", "docs/security-baseline.md"),
)


@dataclass(frozen=True, slots=True)
class Exemption:
    """Reference text a test reads, so it stays in a tracked file; the reader may spell it too.

    Attributes:
        path: The tracked file carrying the reference.
        token: The exact reference token exempted in ``path``.
        reader: The tracked file that reads it; it must still contain ``token``.
        reason: What the reader asserts.
    """

    path: str
    token: str
    reader: str
    reason: str


#: Path-scoped exemptions for references a test reads.
EXEMPTIONS: Final[tuple[Exemption, ...]] = (
    Exemption(
        "tests/unit/test_security_baseline_doc.py",
        "H-90",
        "tests/unit/test_security_baseline_doc.py",
        "a mutation renaming a disposition heading outside the H-4..H-10 set the doc defines",
    ),
)

_PYTHON_SUFFIXES: Final = frozenset({".py"})
_C_STYLE_SUFFIXES: Final = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css"})
_TERRAFORM_SUFFIXES: Final = frozenset({".tf", ".tfvars", ".hcl"})
_YAML_SUFFIXES: Final = frozenset({".yml", ".yaml"})
_JSON_SUFFIXES: Final = frozenset({".json", ".ndjson"})
_HASH_SUFFIXES: Final = frozenset({".sh", ".bash", ".toml", ".cfg", ".ini", ".txt", ".example"})
_HASH_NAMES: Final = frozenset(
    {"Makefile", "Dockerfile", ".gitignore", ".dockerignore", ".gitattributes", "pre-commit"}
)
_MARKUP_SUFFIXES: Final = frozenset({".html", ".svg", ".drawio", ".xml"})
_MARKDOWN_SUFFIXES: Final = frozenset({".md"})

_HTML_COMMENT_RE: Final = re.compile(r"<!--(.*?)-->", re.DOTALL)
_JSON_STRING_RE: Final = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_FSTRING_MIDDLE: Final = getattr(tokenize, "FSTRING_MIDDLE", None)


class GateError(Exception):
    """The gate could not establish its scope or read a file in it."""


@dataclass(frozen=True, slots=True)
class Finding:
    """One reference to untracked content, or one stale allowlist or exemption entry."""

    path: str
    line: int
    token: str
    kind: str

    def render(self) -> str:
        """Render as `path:line: <token> (<kind>)`."""
        return f"{self.path}:{self.line}: {self.token} ({self.kind})"


def _lines_of(text: str, start_line: int) -> Iterator[tuple[int, str]]:
    for offset, line in enumerate(text.splitlines()):
        yield start_line + offset, line


def python_segments(text: str) -> list[tuple[int, str, str]]:
    """Return `(line, text, kind)` for each comment and docstring line of Python source.

    String literals are included as kind ``string`` when :data:`SCAN_STRING_LITERALS` is on. A file
    that does not tokenize is scanned with :func:`hash_segments` instead.
    """
    try:
        tree = ast.parse(text)
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (SyntaxError, tokenize.TokenError, ValueError):
        return hash_segments(text)
    docstrings: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add((first.value.lineno, first.value.col_offset))
    segments: list[tuple[int, str, str]] = []
    for token in tokens:
        if token.type == tokenize.COMMENT:
            segments.append((token.start[0], token.string, "comment"))
        elif token.type == _FSTRING_MIDDLE and SCAN_STRING_LITERALS:
            segments.extend(
                (line, chunk, "string") for line, chunk in _lines_of(token.string, token.start[0])
            )
        elif token.type == tokenize.STRING:
            kind = "docstring" if token.start in docstrings else "string"
            if kind == "docstring" or SCAN_STRING_LITERALS:
                segments.extend(
                    (line, chunk, kind) for line, chunk in _lines_of(token.string, token.start[0])
                )
    return segments


def _string_end(text: str, start: int) -> int | None:
    """Return the index just past the string literal opening at ``start``, or ``None``.

    Single- and double-quoted strings end at their line; one left open there is not a string, so
    ``None`` is returned and scanning resumes after the quote. Template literals may span lines.
    """
    quote = text[start]
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == quote:
            return index + 1
        if char == "\\":
            index += 1
        elif char == "\n" and quote != "`":
            return None
        index += 1
    return None


def c_style_segments(text: str, *, hash_comments: bool = False) -> list[tuple[int, str, str]]:
    """Return `(line, text, kind)` for `//` and `/* */` comments (and `#` ones when asked).

    Single-, double- and backtick-quoted strings are skipped (``string`` segments when
    :data:`SCAN_STRING_LITERALS` is on). Regular-expression literals are not recognised.
    """
    segments: list[tuple[int, str, str]] = []
    index, line, length = 0, 1, len(text)

    def emit(chunk: str, start_line: int, kind: str) -> None:
        segments.extend((ln, part, kind) for ln, part in _lines_of(chunk, start_line))

    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if char == "\n":
            line += 1
            index += 1
        elif (char == "/" and nxt == "/") or (hash_comments and char == "#"):
            end = text.find("\n", index)
            end = length if end < 0 else end
            emit(text[index:end], line, "comment")
            index = end
        elif char == "/" and nxt == "*":
            end = text.find("*/", index + 2)
            end = length if end < 0 else end + 2
            emit(text[index:end], line, "comment")
            line += text.count("\n", index, end)
            index = end
        elif char in "'\"`":
            string_end = _string_end(text, index)
            if string_end is None:
                index += 1
                continue
            if SCAN_STRING_LITERALS:
                emit(text[index:string_end], line, "string")
            line += text.count("\n", index, string_end)
            index = string_end
        else:
            index += 1
    return segments


def _hash_comment_start(raw: str) -> int | None:
    """Return the index of the `#` opening a comment on ``raw``, or ``None``.

    A `#` opens a comment at the start of a line or after whitespace, outside a quoted run.
    """
    quote = ""
    for position, char in enumerate(raw):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (position == 0 or raw[position - 1].isspace()):
            return position
    return None


def hash_segments(text: str) -> list[tuple[int, str, str]]:
    """Return `(line, text, kind)` for each `#` comment outside a quoted run on its line."""
    segments: list[tuple[int, str, str]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        start = _hash_comment_start(raw)
        if start is not None:
            segments.append((number, raw[start:], "comment"))
    return segments


def yaml_segments(text: str) -> list[tuple[int, str, str]]:
    """Return `(line, text, kind)` for each YAML line: its `#` comment and the data before it."""
    segments: list[tuple[int, str, str]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        start = _hash_comment_start(raw)
        data = raw if start is None else raw[:start]
        if data.strip():
            segments.append((number, data, "value"))
        if start is not None:
            segments.append((number, raw[start:], "comment"))
    return segments


def json_segments(text: str) -> list[tuple[int, str, str]]:
    """Return `(line, text, kind)` for every double-quoted string of a JSON or NDJSON file."""
    return [
        (number, match.group(0), "value")
        for number, raw in enumerate(text.splitlines(), 1)
        for match in _JSON_STRING_RE.finditer(raw)
    ]


def markup_segments(text: str) -> list[tuple[int, str, str]]:
    """Return `(line, text, kind)` for each line of every `<!-- -->` comment."""
    segments: list[tuple[int, str, str]] = []
    for match in _HTML_COMMENT_RE.finditer(text):
        start_line = text.count("\n", 0, match.start()) + 1
        segments.extend((ln, part, "comment") for ln, part in _lines_of(match.group(0), start_line))
    return segments


def markdown_segments(text: str) -> list[tuple[int, str, str]]:
    """Return every line of a Markdown document as kind ``doc``."""
    return [(number, line, "doc") for number, line in enumerate(text.splitlines(), 1)]


def segments_for(path: str, text: str) -> list[tuple[int, str, str]] | None:
    """Return the scannable segments of ``path``, or ``None`` when its type is out of scope."""
    pure = PurePosixPath(path)
    suffix = pure.suffix
    if suffix in _PYTHON_SUFFIXES:
        return python_segments(text)
    if suffix in _C_STYLE_SUFFIXES:
        return c_style_segments(text)
    if suffix in _TERRAFORM_SUFFIXES:
        return c_style_segments(text, hash_comments=True)
    if suffix in _MARKDOWN_SUFFIXES:
        return markdown_segments(text)
    if suffix in _YAML_SUFFIXES:
        return yaml_segments(text)
    if suffix in _JSON_SUFFIXES:
        return json_segments(text)
    if suffix in _MARKUP_SUFFIXES:
        return markup_segments(text)
    if suffix in _HASH_SUFFIXES or pure.name in _HASH_NAMES or pure.name.startswith("Dockerfile"):
        return hash_segments(text)
    return None


def _allowed(token: str) -> bool:
    return any(re.fullmatch(entry.pattern, token) for entry in ALLOWED_IDS)


def _exempt(path: str, token: str) -> bool:
    return any(path in (entry.path, entry.reader) and entry.token == token for entry in EXEMPTIONS)


def scan_file(path: str, text: str) -> list[Finding] | None:
    """Return the references in one file, or ``None`` when its type is out of scope."""
    segments = segments_for(path, text)
    if segments is None:
        return None
    findings: list[Finding] = []
    for line, chunk, kind in segments:
        if kind == "string" and path == SELF_PATH:
            continue
        for match in REFERENCE_RE.finditer(chunk):
            token = match.group(0)
            if _allowed(token) or _exempt(path, token):
                continue
            findings.append(Finding(path, line, token, kind))
    return findings


def tracked_files(repo_root: Path) -> list[str]:
    """Return every tracked path under `repo_root`, from `git ls-files`.

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
    return [os.fsdecode(raw) for raw in listed.stdout.split(b"\0") if raw]


def _read_text(path: Path) -> str | None:
    """Return the file's text, or `None` when it is binary."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GateError(f"could not read {path}: {exc}") from exc
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def stale_entries(repo_root: Path, tracked: set[str]) -> list[Finding]:
    """Return a finding for each allowlist or exemption entry whose tracked file no longer backs it."""
    findings: list[Finding] = []
    for allowed in ALLOWED_IDS:
        source = (
            _read_text(repo_root / allowed.defined_in) if allowed.defined_in in tracked else None
        )
        if (
            source is None
            or re.search(rf"(?<![\w-])(?:{allowed.pattern})(?![\w-])", source) is None
        ):
            findings.append(
                Finding(allowed.defined_in, 0, allowed.pattern, "stale allowlist entry")
            )
    for exemption in EXEMPTIONS:
        reader = _read_text(repo_root / exemption.reader) if exemption.reader in tracked else None
        if reader is None or exemption.token not in reader:
            findings.append(Finding(exemption.reader, 0, exemption.token, "stale exemption reader"))
    return findings


def run_check(repo_root: Path) -> tuple[int, list[Finding]]:
    """Scan every in-scope tracked file; return `(files scanned, findings)`."""
    tracked = tracked_files(repo_root)
    scanned = 0
    findings: list[Finding] = []
    for relative in tracked:
        full = repo_root / relative
        if not full.is_file():
            continue
        text = _read_text(full)
        if text is None:
            continue
        found = scan_file(relative, text)
        if found is None:
            continue
        scanned += 1
        findings.extend(found)
    findings.extend(stale_entries(repo_root, set(tracked)))
    return scanned, findings


def main(argv: list[str] | None = None) -> int:
    """Entry point: scan, print findings, and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="The git work tree to check (default: this repository).",
    )
    args = parser.parse_args(argv)
    try:
        scanned, findings = run_check(args.repo_root)
    except GateError as exc:
        print(f"check_no_untracked_refs: ERROR — {exc}", file=sys.stderr)
        return 2
    if scanned == 0:
        print(
            f"[SKIP] check_no_untracked_refs: no scannable tracked file in {args.repo_root}. "
            "Nothing was verified."
        )
        return SKIP_EXIT_STATUS
    for finding in findings:
        print(finding.render())
    if findings:
        print(f"check_no_untracked_refs: FAIL — {len(findings)} finding(s) in {scanned} files")
        return 1
    print(f"check_no_untracked_refs: OK — {scanned} files, no reference to untracked content")
    return 0


if __name__ == "__main__":
    sys.exit(main())
