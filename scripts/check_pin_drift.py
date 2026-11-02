#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verify that the pinned toolchain has not drifted.

Checks four properties:

1. The interpreter version is pinned in ``.python-version`` and agrees with
   ``requires-python`` in ``pyproject.toml``.
2. Every direct dependency in ``pyproject.toml`` uses an exact ``==`` constraint.
3. Every direct dependency appears in the frozen lock file at the same version.
4. The lock file is fully pinned — no bare, range, or VCS requirements.

Run as ``python3 scripts/check_pin_drift.py``. Exits 0 when clean, 1 with one
line per problem otherwise.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOCKFILE = REPO_ROOT / "requirements.txt"
PYTHON_VERSION_FILE = REPO_ROOT / ".python-version"

_PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*==\s*(?P<version>[A-Za-z0-9._*+!-]+)$")


def _normalise(name: str) -> str:
    """Normalise a distribution name per PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _read_lockfile(path: Path) -> tuple[dict[str, str], list[str]]:
    """Return (pinned name -> version, list of problems) for a frozen lock file."""
    pinned: dict[str, str] = {}
    problems: list[str] = []
    if not path.exists():
        return pinned, [f"{path.name}: missing frozen lock file"]

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN_RE.match(line)
        if match is None:
            problems.append(f"{path.name}:{lineno}: not an exact pin: {line!r}")
            continue
        pinned[_normalise(match.group("name"))] = match.group("version")
    return pinned, problems


def _read_direct_deps(data: dict[str, object]) -> tuple[dict[str, str], list[str]]:
    """Return (direct dep name -> version, problems) from parsed pyproject data."""
    direct: dict[str, str] = {}
    problems: list[str] = []
    project = data.get("project")
    if not isinstance(project, dict):
        return direct, ["pyproject.toml: missing [project] table"]

    groups: list[tuple[str, object]] = [("dependencies", project.get("dependencies", []))]
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        groups.extend((f"optional-dependencies.{key}", value) for key, value in optional.items())

    for label, entries in groups:
        if not isinstance(entries, list):
            problems.append(f"pyproject.toml: {label} is not a list")
            continue
        for entry in entries:
            if not isinstance(entry, str):
                problems.append(f"pyproject.toml: {label} contains a non-string entry")
                continue
            match = _PIN_RE.match(entry.strip())
            if match is None:
                problems.append(f"pyproject.toml: {label} entry {entry!r} is not an exact '==' pin")
                continue
            direct[_normalise(match.group("name"))] = match.group("version")
    return direct, problems


def _check_interpreter(data: dict[str, object]) -> list[str]:
    """Verify the interpreter pin exists and agrees with requires-python."""
    problems: list[str] = []
    if not PYTHON_VERSION_FILE.exists():
        return [".python-version: missing interpreter pin"]

    pinned = PYTHON_VERSION_FILE.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", pinned):
        problems.append(f".python-version: {pinned!r} is not a full X.Y.Z pin")
        return problems

    project = data.get("project")
    requires = project.get("requires-python") if isinstance(project, dict) else None
    if not isinstance(requires, str):
        problems.append("pyproject.toml: missing requires-python")
        return problems

    major_minor = ".".join(pinned.split(".")[:2])
    if major_minor not in requires:
        problems.append(
            f"pyproject.toml: requires-python {requires!r} does not cover "
            f".python-version {pinned!r}"
        )
    return problems


def main() -> int:
    """Run every drift check and report all problems together."""
    problems: list[str] = []

    if not PYPROJECT.exists():
        print("FAIL pyproject.toml: missing", file=sys.stderr)
        return 1

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

    problems.extend(_check_interpreter(data))

    direct, direct_problems = _read_direct_deps(data)
    problems.extend(direct_problems)

    pinned, lock_problems = _read_lockfile(LOCKFILE)
    problems.extend(lock_problems)

    for name, version in sorted(direct.items()):
        locked = pinned.get(name)
        if locked is None:
            problems.append(f"{LOCKFILE.name}: direct dependency {name!r} is absent from the lock")
        elif locked != version:
            problems.append(
                f"{LOCKFILE.name}: {name!r} pinned to {version!r} in pyproject.toml "
                f"but {locked!r} in the lock"
            )

    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        print(f"\n{len(problems)} pin-drift problem(s)", file=sys.stderr)
        return 1

    print(
        f"OK pin drift: interpreter pinned, {len(direct)} direct deps exact-pinned, "
        f"{len(pinned)} locked packages fully pinned"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
