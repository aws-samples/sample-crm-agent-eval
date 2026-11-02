#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: every tracked Markdown link resolves, and every tracked `.md` is reachable from README.md.

Scope is `git ls-files '*.md'` under `--repo-root` (tracked files only; no history is read). For each
file, outside fenced code, inline code and HTML comments:

- `missing-target`: an inline link or image `[t](target)` / `![a](target)`, or a reference
  definition `[r]: target`, names a relative path that is not a tracked file or a directory holding
  one. The comparison is case-sensitive. A leading `/` resolves from the repository root; external
  schemes (`http:`, `https:`, `mailto:` and any other `scheme:`) and `//host` are skipped.
- `missing-anchor`: a `#fragment` on a link to a tracked `.md` (or on a bare `#fragment`, meaning
  the same file) matches neither a heading slug of that file nor an HTML `<a id>`/`<a name>` in it.
  Slugs follow GitHub's rule: rendered heading text, lowercased, with every character that is not a
  letter, digit, space, `-` or `_` dropped, each space replaced by `-`, and `-1`, `-2` … appended to
  repeats. Fragments on any other target are not checked.
- `unreachable`: a tracked `.md` that no chain of `.md` links leads to from `README.md`. A link to a
  directory reaches that directory's `README.md`. :data:`EXEMPT_UNREACHABLE` lists exceptions.

Usage: `python3 scripts/check_doc_links.py [--repo-root DIR]`

Exit codes: 0 pass; 1 one `path:line: <rule>: <detail>` line per finding; 2 usage or environment
error (not a git work tree, unreadable file); 3 SKIP: no tracked `.md`, so nothing was verified.
`scripts/run_gates.py` renders 3 as `[SKIP]`.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import sys
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import proc_run  # noqa: E402

#: Exit status for "ran, verified nothing". Must equal `scripts/run_gates.py`'s
#: `GATE_SKIP_EXIT_STATUS`.
SKIP_EXIT_STATUS: Final = 3

#: The document every other tracked `.md` must be reachable from.
ROOT_DOCUMENT: Final = "README.md"

#: Tracked `.md` paths allowed to be unreachable from :data:`ROOT_DOCUMENT`: path -> reason.
EXEMPT_UNREACHABLE: Final[dict[str, str]] = {}

_FENCE_RE: Final = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_ATX_RE: Final = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_ATX_CLOSING_RE: Final = re.compile(r"(?:^|[ \t]+)#+[ \t]*$")
_SETEXT_RE: Final = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_INLINE_CODE_RE: Final = re.compile(r"(`+)(.+?)\1")
_COMMENT_RE: Final = re.compile(r"<!--.*?-->", re.DOTALL)
_INLINE_LINK_RE: Final = re.compile(
    r"\]\(\s*(<[^>\n]*>|[^\s()<>]*(?:\([^\s()]*\)[^\s()<>]*)*)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?\s*\)"
)
_REFERENCE_DEF_RE: Final = re.compile(r"^ {0,3}\[([^\]^\n][^\]\n]*)\]:[ \t]*(<[^>\n]*>|\S+)")
_HTML_ANCHOR_RE: Final = re.compile(
    r"<a\s[^>]*?\b(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)
_HTML_TAG_RE: Final = re.compile(r"<[^>\n]+>")
_LINK_TEXT_RE: Final = re.compile(r"!?\[([^\]]*)\](?:\([^)]*\)|\[[^\]]*\])")
_EMPHASIS_UNDERSCORE_RE: Final = re.compile(r"(?<![0-9A-Za-z])_+|_+(?![0-9A-Za-z])")
_SCHEME_RE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


class GateError(Exception):
    """The gate could not establish its scope or read a file in it."""


@dataclass(frozen=True, slots=True)
class Finding:
    """One broken link or unreachable document."""

    path: str
    line: int
    rule: str
    detail: str

    def render(self) -> str:
        """Render as `path:line: <rule>: <detail>`."""
        return f"{self.path}:{self.line}: {self.rule}: {self.detail}"


@dataclass(frozen=True, slots=True)
class Link:
    """One link target as written, with the line it is on."""

    line: int
    target: str


@dataclass(slots=True)
class Document:
    """The links and anchors one Markdown file defines."""

    links: list[Link] = field(default_factory=list)
    anchors: set[str] = field(default_factory=set)


def slugify(heading: str) -> str:
    """Return GitHub's anchor slug for a heading's raw Markdown text (before de-duplication)."""
    text = _INLINE_CODE_RE.sub(lambda m: m.group(2), heading)
    text = _LINK_TEXT_RE.sub(lambda m: m.group(1), text)
    text = _HTML_TAG_RE.sub("", text)
    text = text.replace("*", "")
    text = _EMPHASIS_UNDERSCORE_RE.sub("", text).lower()
    kept = "".join(ch for ch in text if ch.isalnum() or ch in " -_")
    return kept.replace(" ", "-")


def _closes(opener: re.Match[str] | None, line: str, fence: str) -> bool:
    """Whether `line` closes the open `fence`: same character, at least as long, nothing after."""
    if opener is None or opener.group(1)[0] != fence[0]:
        return False
    marker = opener.group(1)
    return len(marker) >= len(fence) and not line.strip()[len(marker) :].strip()


def _code_free_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield `(line number, line)` outside fenced code, with inline code and comments blanked."""
    text = _COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        opener = _FENCE_RE.match(line)
        if fence is not None:
            if _closes(opener, line, fence):
                fence = None
            continue
        if opener is not None:
            fence = opener.group(1)
            continue
        yield number, _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), line)


def parse_document(text: str) -> Document:
    """Collect a Markdown file's link targets and the anchors its headings and HTML define."""
    document = Document()
    occurrences: dict[str, int] = {}

    def add_heading(raw: str) -> None:
        base = slugify(raw)
        slug = base
        while slug in occurrences:
            occurrences[base] += 1
            slug = f"{base}-{occurrences[base]}"
        occurrences[slug] = 0
        document.anchors.add(slug)

    previous: tuple[int, str] | None = None
    raw_lines = text.splitlines()
    for number, line in _code_free_lines(text):
        atx = _ATX_RE.match(line)
        if atx is not None:
            add_heading(_ATX_CLOSING_RE.sub("", raw_lines[number - 1].strip().lstrip("#").strip()))
        elif (
            _SETEXT_RE.match(line)
            and previous is not None
            and previous[0] == number - 1
            and previous[1].strip()
            and not _ATX_RE.match(previous[1])
        ):
            add_heading(raw_lines[number - 2].strip())
        document.anchors.update(_HTML_ANCHOR_RE.findall(line))
        for match in _INLINE_LINK_RE.finditer(line):
            document.links.append(Link(number, match.group(1)))
        definition = _REFERENCE_DEF_RE.match(line)
        if definition is not None:
            document.links.append(Link(number, definition.group(2)))
        previous = (number, line)
    return document


def _resolve(source: str, target: str) -> tuple[str, str | None] | None:
    """Resolve `target` from `source` to `(repo path, fragment)`; `None` when it is external."""
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    if not target or _SCHEME_RE.match(target) or target.startswith("//"):
        return None
    path_part, _, fragment = target.partition("#")
    path_part = unquote(path_part.split("?", 1)[0])
    if not path_part:
        return source, fragment or None
    base = "" if path_part.startswith("/") else posixpath.dirname(source)
    resolved = posixpath.normpath(posixpath.join(base, path_part.lstrip("/")))
    return ("" if resolved == "." else resolved), fragment or None


def tracked_paths(repo_root: Path) -> list[str]:
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


def run_check(repo_root: Path) -> tuple[int, list[Finding]]:
    """Check every tracked `.md`; return `(documents scanned, findings)`."""
    tracked = {path for path in tracked_paths(repo_root) if (repo_root / path).is_file()}
    directories = {posixpath.dirname(path) for path in tracked}
    directories |= {
        "/".join(d.split("/")[:i]) for d in directories for i in range(len(d.split("/")))
    }
    markdown = sorted(path for path in tracked if path.endswith(".md"))
    documents: dict[str, Document] = {}
    for path in markdown:
        try:
            text = (repo_root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise GateError(f"could not read {path}: {exc}") from exc
        documents[path] = parse_document(text)

    findings: list[Finding] = []
    edges: dict[str, set[str]] = {path: set() for path in markdown}
    for path, document in documents.items():
        for link in document.links:
            resolved = _resolve(path, link.target)
            if resolved is None:
                continue
            target, fragment = resolved
            if target.startswith(".."):
                findings.append(
                    Finding(
                        path, link.line, "missing-target", f"{link.target} (outside the repository)"
                    )
                )
                continue
            if target not in tracked and target not in directories:
                findings.append(Finding(path, link.line, "missing-target", link.target))
                continue
            reached = target if target in documents else posixpath.join(target, ROOT_DOCUMENT)
            if reached in documents:
                edges[path].add(reached)
            if fragment is not None and target in documents:
                anchors = documents[target].anchors
                decoded = unquote(fragment)
                if decoded not in anchors and decoded.lower() not in anchors:
                    findings.append(Finding(path, link.line, "missing-anchor", link.target))

    if markdown:
        reachable: set[str] = set()
        queue = deque([ROOT_DOCUMENT] if ROOT_DOCUMENT in documents else [])
        while queue:
            current = queue.popleft()
            if current in reachable:
                continue
            reachable.add(current)
            queue.extend(edges[current] - reachable)
        if ROOT_DOCUMENT not in documents:
            findings.append(
                Finding(ROOT_DOCUMENT, 1, "unreachable", "the root document is not tracked")
            )
        for path in markdown:
            if path not in reachable and path not in EXEMPT_UNREACHABLE:
                findings.append(
                    Finding(path, 1, "unreachable", f"no link chain from {ROOT_DOCUMENT}")
                )
    return len(markdown), findings


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
        print(f"check_doc_links: ERROR — {exc}", file=sys.stderr)
        return 2
    if scanned == 0:
        print(f"[SKIP] check_doc_links: no tracked .md in {args.repo_root}. Nothing was verified.")
        return SKIP_EXIT_STATUS
    for finding in sorted(findings, key=lambda f: (f.path, f.line, f.rule, f.detail)):
        print(finding.render())
    if findings:
        print(f"check_doc_links: FAIL — {len(findings)} finding(s) in {scanned} Markdown files")
        return 1
    print(f"check_doc_links: OK — {scanned} Markdown files, every link resolves and is reachable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
