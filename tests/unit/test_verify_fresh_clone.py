# SPDX-License-Identifier: MIT
"""Proves `scripts/verify_fresh_clone.sh` materialises tracked content only.

An untracked sentinel file is absent from the scratch tree, and a known tracked file is present.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from scripts import proc_run

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "verify_fresh_clone.sh"

#: A tracked file known to exist at HEAD, used to prove the scratch tree is not empty.
_KNOWN_TRACKED_FILE = "Makefile"


def _run_materialise_only() -> str:
    """Run the script in `--materialise-only` mode and return the scratch directory path.

    The path is the last non-blank stdout line; diagnostics go to stderr.
    """
    result = proc_run.run(
        ["bash", str(_SCRIPT_PATH), "--materialise-only"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"script produced no stdout; stderr was:\n{result.stderr}"
    return str(lines[-1]).strip()


#: Prefix of the throwaway files `_untracked_sentinel` creates.
_SENTINEL_PREFIX = "UNTRACKED_SENTINEL_"

#: The pytest-xdist worker id embedded in sentinel names, so each worker's leak guard sees only its
#: own sentinels.
_WORKER_ID = os.environ.get("PYTEST_XDIST_WORKER", "serial")

#: Directory the sentinel is created in, relative to the repository root. It is gitignored and
#: excluded from `test_gates.py`'s `_copy_repo`, so whole-tree copies in other tests never see it.
_SENTINEL_DIR = "build"


def _sentinel_name() -> str:
    """A sentinel filename owned by THIS worker and unique within it."""
    return f"{_SENTINEL_PREFIX}{_WORKER_ID}_{uuid.uuid4().hex}.txt"


def _sentinels_present() -> set[str]:
    """Repo-relative POSIX paths of this worker's sentinel files currently on disk."""
    directory = _REPO_ROOT / _SENTINEL_DIR
    if not directory.is_dir():
        return set()
    return {
        p.relative_to(_REPO_ROOT).as_posix()
        for p in directory.glob(f"{_SENTINEL_PREFIX}{_WORKER_ID}_*")
    }


@pytest.fixture(autouse=True)
def _no_sentinel_is_left_behind() -> Iterator[None]:
    """Fail a test that leaves a sentinel behind.

    Compares this worker's sentinel set before and after the test, so residue from an earlier run or
    another worker is not attributed to it.
    """
    before = _sentinels_present()
    yield
    leaked = sorted(_sentinels_present() - before)
    assert not leaked, (
        f"this test left sentinel file(s) behind: {leaked}. The fixture removes them in a `finally`; "
        "if that was bypassed, delete them and check what interrupted the run."
    )


@pytest.fixture
def _untracked_sentinel() -> Iterator[str]:
    """Create a uniquely-named untracked file under `_SENTINEL_DIR`; remove it afterwards.

    Yields the path relative to the repository root.
    """
    directory = _REPO_ROOT / _SENTINEL_DIR
    directory.mkdir(parents=True, exist_ok=True)
    sentinel_path = directory / _sentinel_name()
    sentinel_path.write_text("must never appear in a tracked-only scratch tree\n")
    try:
        yield sentinel_path.relative_to(_REPO_ROOT).as_posix()
    finally:
        sentinel_path.unlink(missing_ok=True)
        # Removes the directory only if empty; another process may still hold a sentinel in it.
        with contextlib.suppress(OSError):
            directory.rmdir()


def test_the_leak_guard_is_scoped_to_this_worker() -> None:
    """`_sentinels_present()` sees this worker's sentinel and not another worker's."""
    directory = _REPO_ROOT / _SENTINEL_DIR
    directory.mkdir(parents=True, exist_ok=True)
    mine = directory / _sentinel_name()
    # `not<id>` cannot collide with the `<id>_` glob whatever the worker is called, including "serial".
    foreign = directory / f"{_SENTINEL_PREFIX}not{_WORKER_ID}_{uuid.uuid4().hex}.txt"
    try:
        mine.write_text("this worker's\n")
        foreign.write_text("another worker's\n")
        visible = _sentinels_present()
        assert mine.relative_to(_REPO_ROOT).as_posix() in visible, (
            "the guard cannot see this worker's own sentinel, which would make the leak check pass "
            f"while verifying nothing: {sorted(visible)}"
        )
        assert foreign.relative_to(_REPO_ROOT).as_posix() not in visible, (
            "another worker's sentinel is visible to this worker's guard, so a concurrent sibling "
            f"test would again be reported as leaking a file it never created: {sorted(visible)}"
        )
    finally:
        mine.unlink(missing_ok=True)
        foreign.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            directory.rmdir()


def test_untracked_sentinel_is_absent_from_scratch_tree(_untracked_sentinel: str) -> None:
    """An untracked file present in the working tree does not appear in the scratch tree."""
    assert (_REPO_ROOT / _untracked_sentinel).is_file(), (
        f"the sentinel {_untracked_sentinel} was not created, so its absence below would prove nothing"
    )
    scratch_dir = Path(_run_materialise_only())
    try:
        assert not (scratch_dir / _untracked_sentinel).exists(), (
            f"untracked sentinel '{_untracked_sentinel}' leaked into the scratch tree at "
            f"{scratch_dir} — materialisation is not scoped to tracked content"
        )
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def test_known_tracked_file_is_present_in_scratch_tree() -> None:
    """A file tracked at HEAD (the `Makefile`) is present in the scratch tree."""
    scratch_dir = Path(_run_materialise_only())
    try:
        tracked_file = scratch_dir / _KNOWN_TRACKED_FILE
        assert tracked_file.is_file(), (
            f"expected tracked file '{_KNOWN_TRACKED_FILE}' missing from scratch tree "
            f"{scratch_dir} — materialisation did not populate tracked content"
        )
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
