#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run `gitleaks` as a counted gate over the files git does not ignore, with every match redacted.

The scanned set is exactly `git -C SOURCE ls-files -z --cached --others --exclude-standard`: tracked
files plus untracked files that `.gitignore` and `.git/info/exclude` do not exclude. The listing
overrides `core.excludesFile` on the command line, so neither a user-global excludes file nor one
injected through `GIT_CONFIG*` variables narrows it. Entries
absent from disk are skipped; symlinks are kept as symlinks and not followed; `.git/` is never read.
An entry that is unreadable, is neither a regular file nor a symlink (an untracked nested repository,
a gitlink, a special file), or sits below a symlinked directory is an error, never a skip. The
set is materialised into a temporary scratch tree that gitleaks scans with `detect --no-git`, and
each finding's `File` is reported relative to SOURCE. Gitignored files are not read by this gate;
`check_untracked_artifact_secrets.py` covers them with the patterns of `check_no_account_id.sh` and
`check_no_vendor_keys.sh`.

Rule set: an explicit `--config`; else `SOURCE/.gitleaks.toml` when it is a file on disk (untracked
and optional); else gitleaks' built-in rules.

Exit codes: `0` no findings; `1` at least one finding (rule id, file and line reported, match always
**redacted**); `2` usage or environment error, including an explicit `--config` that does not exist
and a SOURCE that is not a git work tree; `3` **VERIFIED NOTHING** — the `gitleaks` binary is absent,
or the scan set placed no file, so this run is not evidence and `run_gates.py` renders it `[SKIP]`.
A gitleaks `skipping file` log line is an error (exit 2), not a pass. CI installs a pinned `gitleaks`
binary so the gate runs there. Stdlib only, so the pre-commit hook can run it with system `python3`.

Usage: python3 scripts/check_secret_scan.py [--source PATH] [--config PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import proc_run  # noqa: E402

#: Exit status meaning "this gate ran and verified nothing", read by `run_gates.py` as `[SKIP]`.
EXIT_VERIFIED_NOTHING: Final[int] = 3

BINARY: Final[str] = "gitleaks"

#: The scanner's flags: scan the filesystem rather than history, and redact every matched value.
SCAN_FLAGS: Final[tuple[str, ...]] = ("detect", "--no-git", "--redact")

#: The `git ls-files` arguments selecting the scanned set: tracked plus untracked-not-ignored.
SCAN_SET_ARGS: Final[tuple[str, ...]] = (
    "ls-files",
    "-z",
    "--cached",
    "--others",
    "--exclude-standard",
)

#: Inherited git location variables, cleared so `git -C SOURCE` reads SOURCE's own repository.
GIT_LOCATION_ENV: Final[tuple[str, ...]] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_INDEX_FILE",
)

#: Command-line config for the listing, which outranks every config file and `GIT_CONFIG_*`
#: variable, so only `.gitignore` and `.git/info/exclude` decide the set.
GIT_LISTING_CONFIG: Final[tuple[str, ...]] = ("-c", f"core.excludesFile={os.devnull}")

#: The gitleaks log text for a file it did not read.
SKIPPED_FILE_MARKER: Final[str] = "skipping file"

#: The optional rule-set file gitleaks itself looks for at the root of the scanned tree.
LOCAL_RULE_SET_NAME: Final[str] = ".gitleaks.toml"


class SecretScanError(RuntimeError):
    """The scan could not be performed for a reason that is not a finding."""


class NothingToScanError(RuntimeError):
    """The scan set placed no file, so a clean result would verify nothing."""


def binary_unavailable_reason(binary: str = BINARY) -> str | None:
    """`None` when the scanner can run; else why it cannot, phrased for a skip message."""
    if shutil.which(binary) is None:
        return f"{binary!r} is not on PATH"
    return None


def resolve_config(source: Path, config: Path | None) -> Path | None:
    """The rule set to pass: `config` if given, else `source`'s local rule set, else `None`.

    `None` means gitleaks' built-in rules.
    """
    if config is not None:
        return config
    local = source / LOCAL_RULE_SET_NAME
    return local if local.is_file() else None


def scan_set(source: Path) -> list[str]:
    """The source-relative paths git does not ignore under `source`, deduplicated, in git's order.

    Raises :class:`SecretScanError` when `source` is not a git work tree or `git` fails.
    """
    env = {k: v for k, v in os.environ.items() if k not in GIT_LOCATION_ENV}
    try:
        inside = proc_run.run(
            ["git", *GIT_LISTING_CONFIG, "-C", str(source), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            env=env,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            raise SecretScanError(
                f"{source} is not a git work tree, so the non-ignored file set cannot be "
                f"determined: {inside.stderr.strip()[:400]}"
            )
        listed = proc_run.run(
            ["git", *GIT_LISTING_CONFIG, "-C", str(source), *SCAN_SET_ARGS],
            capture_output=True,
            check=False,
            env=env,
        )
    except OSError as exc:
        raise SecretScanError(f"could not run git: {exc}") from exc
    if listed.returncode != 0:
        stderr = listed.stderr.decode("utf-8", errors="replace").strip()
        raise SecretScanError(f"git ls-files exited {listed.returncode}: {stderr[:400]}")
    entries = (os.fsdecode(raw) for raw in listed.stdout.split(b"\0") if raw)
    return list(dict.fromkeys(entries))


def _parents_are_present_directories(source: Path, rel: str) -> bool:
    """`False` when a parent of `rel` under `source` is absent; raise if one is a symlink."""
    current = source
    for part in Path(rel).parts[:-1]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            return False
        if stat.S_ISLNK(mode):
            raise SecretScanError(
                f"scan-set entry {rel!r} sits below the symlinked directory {part!r}, so it "
                "cannot be placed without writing through a symlink"
            )
    return True


def _scratch_target(root: Path, rel: str) -> Path:
    """Create `rel`'s parents under `root` without traversing a symlink; return its target path."""
    parts = Path(rel).parts
    current = root
    for part in parts[:-1]:
        current = current / part
        try:
            current.mkdir()
        except FileExistsError:
            if not stat.S_ISDIR(current.lstat().st_mode):
                raise SecretScanError(
                    f"scan-set entry {rel!r} needs {part!r} as a directory, but it is already "
                    "placed as a file or symlink"
                ) from None
    if not current.resolve().is_relative_to(root):
        raise SecretScanError(f"scan-set entry {rel!r} would be placed outside the scratch tree")
    return current / parts[-1]


def _place_file(origin: Path, target: Path) -> None:
    """Hard-link `origin` at `target`, else copy it into a newly created file; never overwrite."""
    try:
        os.link(origin, target)
    except FileExistsError:
        raise
    except OSError:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(target, flags, 0o600), "wb") as out, origin.open("rb") as src:
            shutil.copyfileobj(src, out)


def materialise(source: Path, paths: list[str], dest: Path) -> int:
    """Place each of `paths` from `source` into `dest`; return how many entries were placed.

    Regular files are hard-linked, falling back to a copy; symlinks are recreated, not followed.
    Nothing is written through a symlink or outside `dest`, and no placed entry is overwritten.
    Only entries absent from disk are skipped. Raises :class:`SecretScanError` for an entry that is
    unreadable, is neither a regular file nor a symlink, sits below a symlinked directory, or is
    not a relative path outside `.git/`. Other `OSError`s propagate.
    """
    root = dest.resolve()
    placed = 0
    for rel in paths:
        parts = Path(rel).parts
        if not parts or Path(rel).is_absolute() or ".." in parts or ".git" in parts:
            raise SecretScanError(f"scan-set entry {rel!r} is not a placeable relative path")
        if not _parents_are_present_directories(source, rel):
            continue
        origin = source / rel
        try:
            mode = origin.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise SecretScanError(
                f"scan-set entry {rel!r} is neither a regular file nor a symlink (an untracked "
                "nested repository, a gitlink or a special file), so its content cannot be scanned"
            )
        if stat.S_ISREG(mode) and not os.access(origin, os.R_OK):
            raise SecretScanError(
                f"scan-set entry {rel!r} is not readable, so it cannot be scanned"
            )
        target = _scratch_target(root, rel)
        if stat.S_ISLNK(mode):
            target.symlink_to(origin.readlink())
        else:
            _place_file(origin, target)
        placed += 1
    return placed


def _relativise(finding: dict[str, Any], tree: Path) -> dict[str, Any]:
    """`finding` with every scratch-tree path prefix in `File` and `Fingerprint` removed."""
    prefix = f"{tree}{os.sep}"
    for key in ("File", "Fingerprint"):
        value = finding.get(key)
        if isinstance(value, str) and value.startswith(prefix):
            finding[key] = value[len(prefix) :]
    return finding


def _run_gitleaks(source: Path, config: Path | None, report: Path) -> tuple[int, str]:
    """Invoke the scanner over `source` and return `(exit status, combined output)`.

    `config` `None` runs gitleaks' built-in rules.

    Output is decoded with `errors="replace"`, since the scanner echoes paths and context that may
    not be valid UTF-8. The child runs under `LC_ALL=C`.
    """
    completed = proc_run.run(
        [
            BINARY,
            *SCAN_FLAGS,
            *(("--config", str(config)) if config is not None else ()),
            "--source",
            str(source),
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    return completed.returncode, f"{completed.stdout}\n{completed.stderr}".strip()


def findings_for(source: Path, config: Path | None) -> list[dict[str, Any]]:
    """Return the scanner's findings over `source`'s non-ignored files, as report entries.

    `config` `None` runs gitleaks' built-in rules; a given `config` must exist. Each entry's `File`
    is relative to `source`.

    The report file is the success signal, not the exit code: `gitleaks` exits non-zero both on
    findings and when it cannot run. A non-zero status with no report raises
    :class:`SecretScanError`, as do a `source` that is not a git work tree and a gitleaks
    `skipping file` log line. A scan set that places no file raises :class:`NothingToScanError`.
    The scratch tree and report live in a temporary directory, never the repository.
    """
    if config is not None and not config.is_file():
        raise SecretScanError(f"config {config} does not exist, so no rule set could be applied")
    paths = scan_set(source)
    with tempfile.TemporaryDirectory(prefix="crm-secret-scan-") as scratch:
        tree = Path(scratch) / "tree"
        tree.mkdir()
        report = Path(scratch) / "report.json"
        try:
            placed = materialise(source, paths, tree)
        except OSError as exc:
            raise SecretScanError(f"could not materialise the scan set: {exc}") from exc
        if placed == 0:
            raise NothingToScanError(
                f"no file under {source} is in the scan set, so a clean result would verify nothing"
            )
        status, output = _run_gitleaks(tree, config, report)
        if SKIPPED_FILE_MARKER in output.lower():
            raise SecretScanError(f"{BINARY} did not read every file: {output[:400]}")
        if report.is_file():
            try:
                # Strict decode: with `--redact` the report carries no source bytes. A
                # `UnicodeDecodeError` is not caught by the `JSONDecodeError` clause below.
                parsed = json.loads(report.read_text(encoding="utf-8") or "[]")
            except json.JSONDecodeError as exc:
                raise SecretScanError(f"{BINARY} wrote an unparseable report: {exc}") from exc
            if isinstance(parsed, list):
                return [_relativise(dict(entry), tree) for entry in parsed]
            raise SecretScanError(f"{BINARY} report was {type(parsed).__name__}, expected a list")
        if status != 0:
            raise SecretScanError(f"{BINARY} exited {status} and wrote no report: {output[:400]}")
    return []


def describe(finding: dict[str, Any]) -> str:
    """One finding as a single actionable line, carrying no secret value."""
    return (
        f"{finding.get('RuleID', '<no rule>')}: {finding.get('File', '<no file>')}:"
        f"{finding.get('StartLine', '?')} — {finding.get('Match', '')!r}"
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point: scan, report, and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=REPO_ROOT,
        help="Git work tree whose non-ignored files are scanned (default: the repository root).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Rule set (default: SOURCE/.gitleaks.toml if present, else gitleaks' built-in rules).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    reason = binary_unavailable_reason()
    if reason is not None:
        message = (
            f"check_secret_scan: SKIP — {reason}, so NOTHING was scanned and this run is not "
            "evidence that the tree is free of secrets. Exit 3 rather than 0: a secret scanner that "
            "silently skips is worse than none. Install gitleaks, or read this as an "
            "unverified control."
        )
        if args.json:
            print(json.dumps({"status": "skip", "reason": reason, "findings": []}, indent=2))
        else:
            print(message, file=sys.stderr)
        return EXIT_VERIFIED_NOTHING

    try:
        config = resolve_config(args.source, args.config)
        findings = findings_for(args.source, config)
    except NothingToScanError as exc:
        if args.json:
            print(json.dumps({"status": "skip", "reason": str(exc), "findings": []}, indent=2))
        else:
            print(f"check_secret_scan: SKIP — {exc}. NOTHING was scanned.", file=sys.stderr)
        return EXIT_VERIFIED_NOTHING
    except SecretScanError as exc:
        if args.json:
            print(json.dumps({"status": "error", "reason": str(exc), "findings": []}, indent=2))
        else:
            print(f"check_secret_scan: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "status": "fail" if findings else "pass",
                    "source": str(args.source),
                    "rule_set": str(config) if config is not None else "built-in",
                    "findings": [describe(f) for f in findings],
                },
                indent=2,
            )
        )
    if findings:
        if not args.json:
            print(f"check_secret_scan: FAIL — {len(findings)} finding(s)", file=sys.stderr)
            for finding in findings:
                print(f"  {describe(finding)}", file=sys.stderr)
        return 1
    if not args.json:
        rule_set = config.name if config is not None else "gitleaks' built-in rules"
        print(f"check_secret_scan: OK — no findings under {args.source} using {rule_set}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
