#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Secret-hygiene gate for **untracked** generated artifacts (`results/`, `infra/build/`).

`check_no_account_id.sh` and `check_no_vendor_keys.sh` scan tracked content only, via `git grep`;
this gate scans the gitignored artifact directories a deploy or verification run writes. It asserts:

1. **Containment.** Any artifact carrying a real account id, VPC/subnet/security-group id, or private
   key must be gitignored.
2. **No vendor keys.** No artifact may match a vendor-key pattern, tracked or not.
3. **Nothing matching is staged.** No artifact carrying such material may be tracked or staged.

Patterns are parsed from the `PATTERNS=(...)` arrays of those two shell gates by
:func:`shell_patterns` and matched with `grep -E`, the same POSIX ERE engine. Matched values are
never printed.

Exit codes: 0 pass; 1 violation; 2 environment error; 3 nothing verified (`[SKIP]`).
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import proc_run  # noqa: E402

#: The environment every matcher subprocess is given, pinning the locale to `C`. Under a UTF-8
#: locale (including `C.UTF-8`) `grep -E` does not match a line carrying a byte sequence the locale
#: rejects.
_MATCHER_ENV = {**os.environ, "LC_ALL": "C"}

#: Exit status meaning "verified nothing". Must equal `run_gates.GATE_SKIP_EXIT_STATUS`.
SKIP_EXIT_STATUS = 3

#: Directories of generated, untracked artifacts this gate scans.
DEFAULT_ARTIFACT_DIRS: tuple[Path, ...] = (REPO_ROOT / "results", REPO_ROOT / "infra" / "build")

#: The gates whose patterns this one reuses, by concern.
ACCOUNT_ID_GATE = REPO_ROOT / "scripts" / "check_no_account_id.sh"
VENDOR_KEY_GATE = REPO_ROOT / "scripts" / "check_no_vendor_keys.sh"

#: Placeholder account ids `check_no_account_id.sh` exempts, parsed from that script.
_PLACEHOLDER_ARRAY = re.compile(r"^_PLACEHOLDER_ACCOUNT_IDS='\((?P<ids>[0-9|]+)\)'", re.MULTILINE)

#: Concern labels named in violation messages: material requiring containment, and a value that is
#: never legitimate.
_CONTAINMENT_CONCERN = "account-id-or-key-material"
_NEVER_LEGITIMATE_CONCERN = "vendor-api-key"


class GateError(Exception):
    """Raised to short-circuit to a reported environment failure."""


def shell_patterns(gate_path: Path) -> list[str]:
    """Return the `PATTERNS=(...)` entries from a shell gate, shell-unquoted.

    Raises:
        GateError: If the array is absent or empty, or an entry does not unquote to exactly one word.
    """
    source = gate_path.read_text(encoding="utf-8")
    match = re.search(r"^PATTERNS=\(\n(.*?)^\)$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise GateError(f"{gate_path}: no PATTERNS=(...) array found; this gate cannot read it")
    patterns: list[str] = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        words = shlex.split(stripped)
        if len(words) != 1:
            raise GateError(f"{gate_path}: pattern line did not unquote to one word: {stripped!r}")
        patterns.append(words[0])
    if not patterns:
        raise GateError(f"{gate_path}: PATTERNS array is empty")
    return patterns


def placeholder_account_ids(gate_path: Path = ACCOUNT_ID_GATE) -> frozenset[str]:
    """Return the placeholder ids the account-id gate exempts, parsed from its own source."""
    match = _PLACEHOLDER_ARRAY.search(gate_path.read_text(encoding="utf-8"))
    if match is None:
        raise GateError(f"{gate_path}: no _PLACEHOLDER_ACCOUNT_IDS assignment found")
    return frozenset(match.group("ids").split("|"))


def _display(path: Path, repo_root: Path = REPO_ROOT) -> str:
    """Path relative to the repository when it is inside it, absolute otherwise."""
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def artifact_files(directories: tuple[Path, ...]) -> list[Path]:
    """Return every file under `directories` that exists, sorted."""
    found: list[Path] = []
    for directory in directories:
        if directory.is_dir():
            found.extend(sorted(p for p in directory.rglob("*") if p.is_file()))
    return found


def _grep_matching_files(pattern: str, files: list[Path]) -> set[Path]:
    """Return the files matching `pattern`, using `grep -E` so the engine matches the shell gates.

    `-I` skips binary files, matching the shell gates' own `git grep -I`.
    """
    if not files:
        return set()
    completed = proc_run.run(
        ["grep", "-I", "-l", "-E", "-e", pattern, *(str(p) for p in files)],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        env=_MATCHER_ENV,
    )
    if completed.returncode not in (0, 1):
        raise GateError(f"grep failed for pattern {pattern!r}: {completed.stderr.strip()}")
    return {Path(line) for line in completed.stdout.splitlines() if line}


def _tracked_or_staged(files: set[Path], repo_root: Path) -> set[Path]:
    """Return which of `files` git tracks or has staged."""
    if not files:
        return set()
    completed = proc_run.run(
        ["git", "ls-files", "--cached", "--", *(str(p) for p in files)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        env=_MATCHER_ENV,
    )
    if completed.returncode != 0:
        raise GateError(f"git ls-files failed: {completed.stderr.strip()}")
    return {repo_root / line for line in completed.stdout.splitlines() if line}


def unignored(paths: set[Path], repo_root: Path) -> set[Path]:
    """Return which of `paths` git does **not** ignore.

    Asked per file, not per directory: `.gitignore` carries `results/*`, so the directory itself is
    not ignored while every file inside is.
    """
    if not paths:
        return set()
    ordered = sorted(paths)
    completed = proc_run.run(
        ["git", "check-ignore", "--stdin"],
        cwd=repo_root,
        input="\n".join(str(p) for p in ordered),
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        env=_MATCHER_ENV,
    )
    if completed.returncode not in (0, 1):
        raise GateError(f"git check-ignore failed: {completed.stderr.strip()}")
    ignored = {Path(line) for line in completed.stdout.splitlines() if line}
    return {path for path in ordered if path not in ignored}


#: A 12-digit run bounded the way `check_no_account_id.sh`'s own pattern bounds one.
_TWELVE_DIGIT_RUN = re.compile(r"(?:^|[^0-9a-fA-F])([0-9]{12})(?:[^0-9a-fA-F]|$)")

#: Material that is sensitive regardless of any account-number allowlist.
_UNCONDITIONAL_MATERIAL = re.compile(
    r"-----BEGIN(?: RSA| EC| OPENSSH| DSA)? PRIVATE KEY-----"
    r"|vpc-[0-9a-f]{8}(?:[0-9a-f]{9})?"
    r"|subnet-[0-9a-f]{8}(?:[0-9a-f]{9})?"
    r"|sg-[0-9a-f]{8}(?:[0-9a-f]{9})?"
)


def carries_real_material(path: Path, gate_path: Path = ACCOUNT_ID_GATE) -> bool:
    """Whether `path` holds material a containment rule must actually protect.

    Honours the placeholder allowlist `check_no_account_id.sh` exempts (`123456789012` and others).

    Returns:
        `True` when the file carries unconditional material (private keys or VPC, subnet or SG ids) or a
        12-digit run that is not a known placeholder.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        # An unreadable file is treated as sensitive.
        return True
    if _UNCONDITIONAL_MATERIAL.search(text) is not None:
        return True
    placeholders = placeholder_account_ids(gate_path)
    return any(match.group(1) not in placeholders for match in _TWELVE_DIGIT_RUN.finditer(text))


def run_gate(
    directories: tuple[Path, ...] = DEFAULT_ARTIFACT_DIRS, repo_root: Path = REPO_ROOT
) -> tuple[list[str], int]:
    """Run the three checks.

    Args:
        directories: Artifact directories to scan.
        repo_root: The repository the `git` queries run against.

    Returns:
        `(violations, files_scanned)`. `files_scanned == 0` means nothing was verified (`[SKIP]`).
    """
    files = artifact_files(directories)
    if not files:
        return [], 0

    account_patterns = shell_patterns(ACCOUNT_ID_GATE)
    vendor_patterns = shell_patterns(VENDOR_KEY_GATE)
    violations: list[str] = []

    # (2) A vendor key is never legitimate in any artifact, tracked or not.
    for pattern in vendor_patterns:
        for path in sorted(_grep_matching_files(pattern, files)):
            violations.append(
                f"{_display(path, repo_root)}: matches a {_NEVER_LEGITIMATE_CONCERN} pattern. "
                "No generated artifact may contain a vendor API key. Value withheld from this "
                "message deliberately; rotate the key and delete the artifact."
            )

    # (1) and (3): anything carrying real key material must be contained, and must not be staged.
    candidates: set[Path] = set()
    for pattern in account_patterns:
        candidates |= _grep_matching_files(pattern, files)
    sensitive = {path for path in candidates if carries_real_material(path)}

    for path in sorted(_tracked_or_staged(sensitive, repo_root)):
        violations.append(
            f"{_display(path, repo_root)}: matches a {_CONTAINMENT_CONCERN} pattern and is "
            "tracked or staged. A generated artifact carrying real account or key material must "
            "never enter the repository — unstage it and keep it gitignored."
        )

    for path in sorted(unignored(sensitive, repo_root)):
        violations.append(
            f"{_display(path, repo_root)}: matches a {_CONTAINMENT_CONCERN} pattern and is NOT "
            "gitignored. Containment is the only thing standing between real deploy output and the "
            "repository."
        )

    return violations, len(files)


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the gate, print findings with values withheld, and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        action="append",
        default=None,
        help="Artifact directory to scan; repeatable. Defaults to results/ and infra/build/.",
    )
    args = parser.parse_args(argv)
    directories = tuple(args.artifact_dir) if args.artifact_dir else DEFAULT_ARTIFACT_DIRS

    try:
        violations, scanned = run_gate(directories)
    except GateError as exc:
        print(f"check_untracked_artifact_secrets: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"check_untracked_artifact_secrets: {exc}", file=sys.stderr)
        return 2

    if scanned == 0:
        named = ", ".join(_display(d) for d in directories)
        print(
            f"[SKIP] untracked_artifact_secrets: no files under {named}; these directories are "
            "gitignored and are only populated by a deploy or a verification run. Nothing was "
            "verified."
        )
        return SKIP_EXIT_STATUS

    if violations:
        print("check_untracked_artifact_secrets: VIOLATION", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        print("check_untracked_artifact_secrets: FAILED", file=sys.stderr)
        return 1

    print(
        f"check_untracked_artifact_secrets: OK — {scanned} artifact(s) scanned; no vendor key "
        "anywhere, nothing matching is tracked or staged, and every file holding real account or "
        "key material is gitignored."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
