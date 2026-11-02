#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: no git commit id appears in any tracked text file.

Scope is `git ls-files` under `--repo-root`; files containing a NUL byte or not valid UTF-8 are
binary and skipped. The gate reads file contents only, never history, so it gives the same result in
a clone of any history. A hex token (bounded by non-alphanumerics) is reported when:

- `full-length`: it is exactly 40 hex characters and not all digits; or
- `commit-context`: it is 7-39 hex characters with at least one letter a-f and one digit, and a
  commit keyword (:data:`COMMIT_KEYWORDS`) sits within :data:`CONTEXT_BEFORE` characters before it
  or :data:`CONTEXT_AFTER` after it (line breaks included), or the token is the value of a key
  naming a commit (:data:`COMMIT_KEY_RE`).

Never reported: GitHub Action pins (`uses: <owner>/<repo>[/<path>]@<40-hex>`, a third-party
repository's commit, matched by pattern wherever it appears), UUIDs, `sha256:` / `sha256-` digests,
hex runs longer than 40 (sha256 and longer), `#`-prefixed CSS colours, the members of
:data:`FAKE_COMMIT_IDS`, and a `sha-<12 hex>` container image tag (the content-digest tag
`infra/main.tf` builds as `agent_image_tag`) with no other commit keyword in its context window. A
`sha-<12 hex>` tag beside another commit keyword (`commit sha-...`) is still reported.
:data:`EXEMPT` holds any path-scoped exception with its reason.

Usage: `python3 scripts/check_no_commit_ids.py [--repo-root DIR]`

Exit codes: 0 pass; 1 one `path:line: <token> (<rule>)` line per finding; 2 usage or environment
error (not a git work tree, unreadable file); 3 SKIP: no tracked text file, so nothing was verified.
`scripts/run_gates.py` renders 3 as `[SKIP]`.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import proc_run  # noqa: E402

#: Exit status for "ran, verified nothing". Must equal `scripts/run_gates.py`'s
#: `GATE_SKIP_EXIT_STATUS`.
SKIP_EXIT_STATUS: Final = 3

#: Placeholder ids tests may use; obviously synthetic, never a real commit.
FAKE_COMMIT_IDS: Final[frozenset[str]] = frozenset({"abc1234", "abc12345", "0a1b2c3d4e5f6a7b8"})

#: Path-scoped exceptions: tracked path -> reason. Empty by design.
EXEMPT: Final[dict[str, str]] = {}

#: Words that put a short hex token on the same line in commit context.
COMMIT_KEYWORDS: Final[tuple[str, ...]] = (
    "commit",
    "commits",
    "committed",
    "sha",
    "revision",
    "rev",
    "merge",
    "merged",
    "rebase",
    "cherry-pick",
    "git",
    "main",
    "branch",
    "head",
    "pinned",
    "at",
)

#: Characters before / after a token searched for a commit keyword.
CONTEXT_BEFORE: Final = 60
CONTEXT_AFTER: Final = 30

#: A key whose value is a commit id: `base`, or any identifier containing `commit`, `sha` or
#: `revision` (`git_revision`, `captured_from_commit`, `git_commit`, ...), then `:` or `=`.
COMMIT_KEY_RE: Final = re.compile(
    r"(?:(?<![A-Za-z_])base|[A-Za-z_]*(?:commit|sha|revision)[A-Za-z_]*)[\"']?\s*[:=]\s*[\"']?$",
    re.IGNORECASE,
)

_HEX_TOKEN_RE: Final = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{7,}(?![0-9A-Za-z])")
_UUID_RE: Final = re.compile(
    r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"(?![0-9A-Za-z])"
)
_DIGEST_RE: Final = re.compile(r"sha256[:-][0-9a-fA-F]+")
_ACTION_PIN_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_-])uses:\s*[\"']?[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_./-]+@[0-9a-fA-F]{40}(?![0-9A-Za-z])"
)
_IMAGE_TAG_RE: Final = re.compile(r"(?<![0-9A-Za-z_-])sha-[0-9a-f]{12}(?![0-9A-Za-z])")
_KEYWORD_RE: Final = re.compile(
    r"(?<![A-Za-z_])(?:"
    + "|".join(re.escape(word) for word in COMMIT_KEYWORDS)
    + r")(?![A-Za-z_])",
    re.IGNORECASE,
)


class GateError(Exception):
    """The gate could not establish its scope or read a file in it."""


@dataclass(frozen=True, slots=True)
class Finding:
    """One commit id found in a tracked file."""

    path: str
    line: int
    token: str
    rule: str

    def render(self) -> str:
        """Render as `path:line: <token> (<rule>)`."""
        return f"{self.path}:{self.line}: {self.token} ({self.rule})"


def _blank(match: re.Match[str]) -> str:
    return " " * len(match.group(0))


def _mask_image_tags(text: str) -> str:
    """Blank each `sha-<12 hex>` image tag that has no commit keyword in its context window."""

    def _tag_or_blank(match: re.Match[str]) -> str:
        start, end = match.span()
        window = (
            text[max(0, start - CONTEXT_BEFORE) : start] + " " + text[end : end + CONTEXT_AFTER]
        )
        return match.group(0) if _KEYWORD_RE.search(window) else _blank(match)

    return _IMAGE_TAG_RE.sub(_tag_or_blank, text)


def scan_text(text: str) -> list[tuple[int, str, str]]:
    """Return `(line number, token, rule)` for every commit id in one file's text."""
    masked = _mask_image_tags(
        _ACTION_PIN_RE.sub(_blank, _DIGEST_RE.sub(_blank, _UUID_RE.sub(_blank, text)))
    )
    found: list[tuple[int, str, str]] = []
    for match in _HEX_TOKEN_RE.finditer(masked):
        token = match.group(0)
        lowered = token.lower()
        start, end = match.span()
        if lowered in FAKE_COMMIT_IDS or token.isdigit() or len(token) > 40:
            continue
        if start > 0 and masked[start - 1] == "#" and len(token) == 8:
            continue
        line = masked.count("\n", 0, start) + 1
        if len(token) == 40:
            found.append((line, token, "full-length"))
            continue
        if not (any(char in "abcdef" for char in lowered) and any(c.isdigit() for c in token)):
            continue
        window = (
            masked[max(0, start - CONTEXT_BEFORE) : start] + " " + masked[end : end + CONTEXT_AFTER]
        )
        line_start = masked.rfind("\n", 0, start) + 1
        keyed = COMMIT_KEY_RE.search(masked[line_start:start]) is not None
        if keyed or _KEYWORD_RE.search(window) is not None:
            found.append((line, token, "commit-context"))
    return found


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


def run_check(repo_root: Path) -> tuple[int, list[Finding]]:
    """Scan every tracked text file; return `(text files scanned, findings)`."""
    scanned = 0
    findings: list[Finding] = []
    for relative in tracked_files(repo_root):
        full = repo_root / relative
        if relative in EXEMPT or not full.is_file():
            continue
        text = _read_text(full)
        if text is None:
            continue
        scanned += 1
        findings.extend(
            Finding(relative, line, token, rule) for line, token, rule in scan_text(text)
        )
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
        print(f"check_no_commit_ids: ERROR — {exc}", file=sys.stderr)
        return 2
    if scanned == 0:
        print(
            f"[SKIP] check_no_commit_ids: no tracked text file in {args.repo_root}. "
            "Nothing was verified."
        )
        return SKIP_EXIT_STATUS
    for finding in findings:
        print(finding.render())
    if findings:
        print(f"check_no_commit_ids: FAIL — {len(findings)} commit id(s) in {scanned} text files")
        return 1
    print(f"check_no_commit_ids: OK — {scanned} text files, no commit id")
    return 0


if __name__ == "__main__":
    sys.exit(main())
