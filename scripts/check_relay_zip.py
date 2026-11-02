#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prove the built relay zip is the one the current `src/relay/` tree produces.

Usage:
    python3 scripts/check_relay_zip.py [--zip PATH | --build-dir DIR] [--require-zip] [--json]

`infra/main.tf` hashes the relay **zip** (`filebase64sha256`), never its source, so this gate compares
the build manifest `scripts/package_relay.sh` embeds at the archive root, `.relay-build.json`, against
the working tree:

    {"schema": 1, "input_roots": [...], "sources": {"<relay-relative path>": "<sha256>", ...}}

It needs no Node, install or network. `MANIFEST_NAME` and `INPUT_ROOTS` mirror `package_relay.sh`'s
`BUILD_MANIFEST` and `MANIFEST_INPUT_PATHS`; `tests/unit/test_check_relay_zip.py` asserts they match.

Failure modes, all of which mean "do not apply":

* no zip (with `--require-zip`);
* no manifest in the zip (fail closed);
* any input whose digest differs, is missing from the manifest, or is recorded but no longer on disk,
  each reported by path.

Exit codes: 0 = the zip matches the source tree; 1 = at least one failure above; 3 = **nothing was
verified** (`[SKIP]`): no `src/relay/` at all, or no zip while `--require-zip` was not passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RELAY_SOURCE_DIR = REPO_ROOT / "src" / "relay"
DEFAULT_BUILD_DIR = REPO_ROOT / "infra" / "build" / "lambda"
RELAY_ZIP_NAME = "relay.zip"

#: Mirrors `scripts/package_relay.sh`'s `BUILD_MANIFEST`.
MANIFEST_NAME = ".relay-build.json"

#: Mirrors `scripts/package_relay.sh`'s `MANIFEST_INPUT_PATHS`: the compile scope, the lockfile, and
#: the tsconfigs.
INPUT_ROOTS: tuple[str, ...] = (
    "src",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "tsconfig.build.json",
)

GATE_SKIP_EXIT_STATUS = 3


@dataclass(frozen=True)
class Result:
    """One gate outcome, renderable as text or JSON."""

    status: str  # "pass" | "fail" | "skip"
    detail: str

    @property
    def exit_code(self) -> int:
        return {"pass": 0, "fail": 1, "skip": GATE_SKIP_EXIT_STATUS}[self.status]


def current_source_digests(source_dir: Path = RELAY_SOURCE_DIR) -> dict[str, str]:
    """SHA-256 of every compile input, keyed by path relative to `src/relay`.

    `node_modules` is excluded; the lockfile it derives from is an input.
    """
    digests: dict[str, str] = {}
    for root in INPUT_ROOTS:
        candidate = source_dir / root
        paths: list[Path]
        if candidate.is_file():
            paths = [candidate]
        elif candidate.is_dir():
            paths = [
                path
                for path in candidate.rglob("*")
                if path.is_file() and "node_modules" not in path.parts
            ]
        else:
            continue
        for path in paths:
            digests[path.relative_to(source_dir).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digests


def compare(recorded: dict[str, str], current: dict[str, str]) -> list[str]:
    """Every disagreement between a manifest and the working tree, as reader-facing lines."""
    differences: list[str] = []
    for path in sorted(set(recorded) | set(current)):
        was, now = recorded.get(path), current.get(path)
        if was == now:
            continue
        if was is None:
            differences.append(
                f"{path}: present in src/relay but not in the zip's manifest (added)"
            )
        elif now is None:
            differences.append(f"{path}: in the zip's manifest but absent from src/relay (removed)")
        else:
            differences.append(
                f"{path}: changed since the zip was built ({was[:12]}… → {now[:12]}…)"
            )
    return differences


def check(
    zip_path: Path,
    *,
    require_zip: bool = False,
    source_dir: Path = RELAY_SOURCE_DIR,
) -> Result:
    if not source_dir.is_dir():
        return Result(
            "skip",
            f"no relay source at {source_dir}: nothing was verified",
        )

    if not zip_path.is_file():
        if require_zip:
            return Result(
                "fail",
                f"{zip_path} is missing; terraform cannot evaluate filebase64sha256 on it. "
                "Run scripts/package_relay.sh",
            )
        return Result(
            "skip",
            f"no {zip_path} exists, so the relay package's currency was NOT checked. A pass here "
            "would be a green verdict over an unread artefact -- exactly what this gate exists to "
            "refuse -- so it is a skip. Run scripts/package_relay.sh, or pass --require-zip to make "
            "the absence a failure (which is what scripts/deploy.sh does).",
        )

    try:
        with zipfile.ZipFile(zip_path) as archive:
            if MANIFEST_NAME not in set(archive.namelist()):
                return Result(
                    "fail",
                    f"{zip_path} carries no {MANIFEST_NAME}; it predates the staleness guard and "
                    "cannot be shown to match src/relay. Rebuild with scripts/package_relay.sh",
                )
            manifest_bytes = archive.read(MANIFEST_NAME)
    except (OSError, zipfile.BadZipFile) as exc:
        return Result("fail", f"{zip_path} could not be read as a zip: {exc}")

    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as exc:
        return Result("fail", f"{MANIFEST_NAME} in {zip_path} is not valid JSON: {exc}")
    if not isinstance(manifest, dict):
        return Result("fail", f"{MANIFEST_NAME} in {zip_path} is not a JSON object")

    recorded = manifest.get("sources")
    if not isinstance(recorded, dict) or not recorded:
        return Result(
            "fail",
            f"{MANIFEST_NAME} in {zip_path} records no 'sources' map, so nothing can be compared",
        )
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in recorded.items()):
        return Result("fail", f"{MANIFEST_NAME} in {zip_path} has a non-string entry in 'sources'")

    differences = compare(dict(recorded), current_source_digests(source_dir))
    if differences:
        listed = "\n  - ".join(differences)
        return Result(
            "fail",
            f"{zip_path} is stale relative to src/relay ({len(differences)} differing input(s)):"
            f"\n  - {listed}\nRun scripts/package_relay.sh and re-run",
        )

    return Result("pass", f"{zip_path} matches src/relay ({len(recorded)} compile inputs)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--zip",
        dest="zip_path",
        type=Path,
        default=None,
        help="Path to the relay zip. Overrides --build-dir; this is what scripts/deploy.sh passes, "
        "so the file checked is exactly the file terraform reads.",
    )
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIR)
    parser.add_argument(
        "--require-zip",
        action="store_true",
        help="Treat an absent relay zip as a failure (the deploy path passes this).",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    zip_path = args.zip_path if args.zip_path is not None else args.build_dir / RELAY_ZIP_NAME
    result = check(zip_path, require_zip=args.require_zip)
    if args.json:
        print(json.dumps({"status": result.status, "detail": result.detail}, indent=2))
    else:
        prefix = {
            "pass": "check_relay_zip: OK",
            "fail": "check_relay_zip: FAIL",
            "skip": "check_relay_zip: SKIP",
        }[result.status]
        stream = sys.stdout if result.status == "pass" else sys.stderr
        print(f"{prefix} -- {result.detail}", file=stream)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
