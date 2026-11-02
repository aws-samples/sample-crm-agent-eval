# SPDX-License-Identifier: MIT
"""Proves `make verify`'s target list covers every offline test suite.

The ``verify`` recipe is parsed for sub-targets running pytest on ``tests/unit``, ``tests/stub`` and
``tests/integration``; the real ``make`` binary is run against a scratch copy of the ``Makefile`` to
prove keep-going aggregation and that a failing stub test fails ``test-stub``.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

import pytest
from scripts import proc_run

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE_PATH = _REPO_ROOT / "Makefile"

#: Every offline suite directory `make verify`'s target list must reach, by pytest invocation.
_REQUIRED_SUITE_DIRS: tuple[str, ...] = ("tests/unit", "tests/stub", "tests/integration")


def _parse_make_targets(makefile_text: str) -> dict[str, list[str]]:
    """Return a mapping of target name to its recipe lines, parsed from a GNU Makefile.

    A target header is an unindented ``name: ...`` line; following tab-indented lines are its
    recipe until a blank line or the next header. Line continuations are not handled.
    """
    targets: dict[str, list[str]] = {}
    current: str | None = None
    header_pattern = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:(?!=)")
    for line in makefile_text.splitlines():
        if line.startswith("\t") and current is not None:
            targets[current].append(line[1:])
            continue
        header_match = header_pattern.match(line)
        if header_match:
            current = header_match.group(1)
            targets.setdefault(current, [])
            continue
        if line.strip() == "":
            current = None
    return targets


def _recipe_text_for(targets: dict[str, list[str]], name: str) -> str:
    """Flatten one target's recipe lines into a single string for substring/regex checks."""
    return "\n".join(targets.get(name, []))


def _referenced_sub_target_names(verify_recipe: str) -> list[str]:
    """Extract the sub-target names a `$(MAKE) ... a b c` recursive invocation lists."""
    match = re.search(r"\$\(MAKE\)\s+(?:-\S+\s+)*(.+)$", verify_recipe.strip())
    if not match:
        return []
    return match.group(1).split()


def test_verify_target_list_covers_unit_stub_and_integration() -> None:
    """`verify`'s recipe reaches a target invoking pytest against each required suite directory."""
    makefile_text = _MAKEFILE_PATH.read_text()
    targets = _parse_make_targets(makefile_text)
    assert "verify" in targets, "Makefile has no `verify` target"

    verify_recipe = _recipe_text_for(targets, "verify")
    sub_target_names = _referenced_sub_target_names(verify_recipe)
    assert sub_target_names, "`verify`'s recipe does not invoke $(MAKE) with any sub-target names"

    covered_dirs: set[str] = set()
    for sub_target_name in sub_target_names:
        sub_recipe = _recipe_text_for(targets, sub_target_name)
        for required_dir in _REQUIRED_SUITE_DIRS:
            if re.search(rf"pytest\b[^\n]*\b{re.escape(required_dir)}\b", sub_recipe):
                covered_dirs.add(required_dir)

    missing = set(_REQUIRED_SUITE_DIRS) - covered_dirs
    assert not missing, (
        f"`make verify` does not reach a pytest invocation against: {sorted(missing)} — a test "
        "suite under one of these directories can be silently skipped by the aggregate gate"
    )


@pytest.fixture
def _scratch_makefile_dir(tmp_path: Path) -> Path:
    """A scratch directory holding a copy of the real `Makefile`; tests run `make -C` against it."""
    scratch = tmp_path / "make_scratch"
    scratch.mkdir()
    shutil.copy(_MAKEFILE_PATH, scratch / "Makefile")
    return scratch


def _write_probe_targets(scratch_dir: Path, *, first_fails: bool, second_fails: bool) -> None:
    """Append phony targets `probe-a`, `probe-b` and `probe-verify` to the scratch `Makefile`.

    Each probe prints a marker and exits non-zero when its flag is set; `probe-verify` runs both
    with `$(MAKE) -k`.
    """
    makefile_path = scratch_dir / "Makefile"
    first_cmd = "echo PROBE_A_RAN && exit 1" if first_fails else "echo PROBE_A_RAN"
    second_cmd = "echo PROBE_B_RAN && exit 1" if second_fails else "echo PROBE_B_RAN"
    with makefile_path.open("a") as handle:
        handle.write("\n.PHONY: probe-a probe-b probe-verify\n")
        handle.write(f"probe-a:\n\t{first_cmd}\n")
        handle.write(f"probe-b:\n\t{second_cmd}\n")
        handle.write("probe-verify:\n\t$(MAKE) -k probe-a probe-b\n")


def test_make_verify_collects_every_failure_rather_than_stopping_at_the_first(
    _scratch_makefile_dir: Path,
) -> None:
    """`-k` aggregation runs both targets despite the first failing, and still exits non-zero."""
    _write_probe_targets(_scratch_makefile_dir, first_fails=True, second_fails=False)
    result = proc_run.run(
        ["make", "-C", str(_scratch_makefile_dir), "probe-verify"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "PROBE_A_RAN" in result.stdout
    assert "PROBE_B_RAN" in result.stdout
    assert result.returncode != 0


def test_make_verify_reports_success_only_when_every_target_passes(
    _scratch_makefile_dir: Path,
) -> None:
    """A clean run of every target yields a zero exit status."""
    _write_probe_targets(_scratch_makefile_dir, first_fails=False, second_fails=False)
    result = proc_run.run(
        ["make", "-C", str(_scratch_makefile_dir), "probe-verify"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "PROBE_A_RAN" in result.stdout
    assert "PROBE_B_RAN" in result.stdout
    assert result.returncode == 0


def test_a_failing_stub_test_actually_fails_make_verify(_scratch_makefile_dir: Path) -> None:
    """A failing test under the scratch `tests/stub/` fails the real `test-stub` target.

    The scratch `Makefile` invokes `sys.executable` in place of `.venv/bin/python`.
    """
    makefile_path = _scratch_makefile_dir / "Makefile"
    original_text = makefile_path.read_text()
    makefile_path.write_text(original_text.replace(".venv/bin/python", sys.executable))

    probe_dir = _scratch_makefile_dir / "tests" / "stub"
    probe_dir.mkdir(parents=True)
    (probe_dir / "__init__.py").write_text("")
    (probe_dir / "test_deliberately_failing_probe.py").write_text(
        "def test_this_always_fails() -> None:\n    assert False, 'deliberate probe failure'\n"
    )
    result = proc_run.run(
        [
            "make",
            "-C",
            str(_scratch_makefile_dir),
            "test-stub",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT / "src")},
    )
    assert result.returncode != 0
    assert "deliberate probe failure" in result.stdout + result.stderr


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
