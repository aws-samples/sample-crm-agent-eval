# SPDX-License-Identifier: MIT
"""Tests for `scripts/check_readme_gate_phases.py`.

Every case drives a fixture tree through `--repo-root`, except one test that runs the gate against
the real repository.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import scripts.check_readme_gate_phases as gate
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_readme_gate_phases.py"

#: Every fixture expecting a verdict other than 3 must contain the gate's sentinel module.
SENTINEL = gate.SENTINEL_SUBJECT

_TABLE = "| Gate | Enforces |\n|---|---|\n"


def _write_tree(
    root: Path,
    *,
    phases: dict[str, list[str]],
    rows: dict[str, str],
    table: bool = True,
) -> Path:
    """A fixture repository: `scripts/<gate>.py` docstrings with phase lists, plus the gate table.

    `phases` maps a module stem to the short names its docstring declares; `rows` maps a gate name to
    the body of its row in `docs/development.md`.
    """
    (root / "scripts").mkdir(parents=True)
    for stem, names in phases.items():
        items = "\n".join(
            f"{n}. ``{name}`` -- what this phase does." for n, name in enumerate(names, 1)
        )
        (root / "scripts" / f"{stem}.py").write_text(
            f'#!/usr/bin/env python3\n"""A fixture gate.\n\nIt runs in phases:\n\n{items}\n"""\n',
            encoding="utf-8",
        )
    body = "".join(f"| `{name}` | {text} |\n" for name, text in rows.items())
    (root / "docs").mkdir()
    (root / "docs" / "development.md").write_text(
        f"# Fixture\n\n### Offline gates\n\n{_TABLE if table else ''}{body}\n", encoding="utf-8"
    )
    return root


def _run(root: Path, *, sentinel_min_phases: int | None = 1) -> subprocess.CompletedProcess[str]:
    """Drive the gate against `root`.

    `sentinel_min_phases=None` omits the flag and runs at the shipped default floor.
    """
    floor = (
        [] if sentinel_min_phases is None else ["--sentinel-min-phases", str(sentinel_min_phases)]
    )
    return proc_run.run(
        [sys.executable, str(GATE), "--repo-root", str(root), *floor],
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_row_naming_every_documented_phase_passes(tmp_path: Path) -> None:
    """A row naming every documented phase passes."""
    root = _write_tree(
        tmp_path / "control",
        phases={SENTINEL: ["alpha", "beta"]},
        rows={SENTINEL: "does `alpha` and also `beta`"},
    )
    result = _run(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_a_phase_missing_from_the_row_fails(tmp_path: Path) -> None:
    """A phase declared by the module and absent from its row fails."""
    root = _write_tree(
        tmp_path / "missing",
        phases={SENTINEL: ["alpha", "beta"]},
        rows={SENTINEL: "does `alpha` only"},
    )
    result = _run(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "beta" in result.stderr
    assert "alpha" not in result.stderr.split("phase(s)")[1].split("—")[0]


def test_a_name_deleted_from_the_row_fails(tmp_path: Path) -> None:
    """Deleting a phase name from a passing row fails."""
    intact = _write_tree(
        tmp_path / "intact",
        phases={SENTINEL: ["alpha", "beta"]},
        rows={SENTINEL: "does `alpha` and also `beta`"},
    )
    assert _run(intact).returncode == 0

    mutated = _write_tree(
        tmp_path / "deleted",
        phases={SENTINEL: ["alpha", "beta"]},
        rows={SENTINEL: "does `alpha` and also that other thing"},
    )
    result = _run(mutated)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "beta" in result.stderr


def test_one_mention_of_several_is_enough_and_that_is_the_stated_bound(tmp_path: Path) -> None:
    """A single mention of a phase name in a row is enough to pass."""
    root = _write_tree(
        tmp_path / "mentions",
        phases={SENTINEL: ["alpha"]},
        rows={SENTINEL: "mentions `alpha` in passing and describes nothing"},
    )
    assert _run(root).returncode == 0


def test_a_documented_phase_list_with_no_table_row_fails(tmp_path: Path) -> None:
    """A module declaring a phase list with no table row fails."""
    root = _write_tree(
        tmp_path / "orphan",
        phases={SENTINEL: ["alpha"], "check_undocumented_fixture": ["gamma"]},
        rows={SENTINEL: "does `alpha`"},
    )
    result = _run(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "check_undocumented_fixture" in result.stderr
    assert "no row" in result.stderr


def test_a_prose_phase_description_is_not_a_subject(tmp_path: Path) -> None:
    """A module describing its phases in prose is reported as not covered and does not fail."""
    root = _write_tree(
        tmp_path / "prose",
        phases={SENTINEL: ["alpha"]},
        rows={SENTINEL: "does `alpha`", "check_prose_fixture": "checks several things, in words"},
    )
    (root / "scripts" / "check_prose_fixture.py").write_text(
        '"""A fixture gate that describes phases as prose: first the alpha step, then the beta step."""\n',
        encoding="utf-8",
    )
    result = _run(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "check_prose_fixture" in result.stdout, (
        "a gate outside the parse's reach must still be reported as NOT covered, or the run reads as "
        "though it covered the whole table"
    )


class TestItRefusesToPassWhenItVerifiedNothing:
    """States in which nothing was compared exit 3."""

    def test_no_module_documents_phases(self, tmp_path: Path) -> None:
        root = _write_tree(tmp_path / "empty", phases={}, rows={SENTINEL: "does things"})
        result = _run(root)
        assert result.returncode == 3, result.stdout + result.stderr
        assert "SKIP" in result.stderr

    def test_the_gate_table_is_absent(self, tmp_path: Path) -> None:
        root = _write_tree(
            tmp_path / "notable",
            phases={SENTINEL: ["alpha"]},
            rows={SENTINEL: "does `alpha`"},
            table=False,
        )
        result = _run(root)
        assert result.returncode == 3, result.stdout + result.stderr
        assert "SKIP" in result.stderr

    def test_discovery_no_longer_finds_the_sentinel(self, tmp_path: Path) -> None:
        """A tree whose discovered modules exclude the sentinel exits 3."""
        root = _write_tree(
            tmp_path / "nosentinel",
            phases={"check_other_fixture": ["alpha"]},
            rows={"check_other_fixture": "does `alpha`"},
        )
        result = _run(root)
        assert result.returncode == 3, result.stdout + result.stderr
        assert SENTINEL in result.stderr


class TestAPartialLossOfThePhaseListIsNotSilent:
    """The sentinel's minimum-phase floor turns a partial loss of its phase list into a failure.

    The mutation cases run at the shipped floor and mutate the same fixture the control just passed.
    """

    #: Exactly as many phase names as the shipped floor demands.
    FLOOR_PHASES = tuple(f"phase_{n}" for n in range(1, gate.SENTINEL_MIN_PHASES + 1))

    def _tree_at_the_floor(self, root: Path) -> Path:
        """A sentinel declaring exactly `SENTINEL_MIN_PHASES` phases, all named in its table row."""
        return _write_tree(
            root,
            phases={SENTINEL: list(self.FLOOR_PHASES)},
            rows={SENTINEL: "does " + ", ".join(f"`{name}`" for name in self.FLOOR_PHASES)},
        )

    @staticmethod
    def _mutate_sentinel_docstring(root: Path, pattern: str, replacement: str) -> None:
        """Rewrite the fixture sentinel's source in place, and refuse to be a no-op."""
        path = root / "scripts" / f"{SENTINEL}.py"
        source = path.read_text(encoding="utf-8")
        mutated = re.sub(pattern, replacement, source, count=1, flags=re.MULTILINE)
        assert mutated != source, f"the mutation {pattern!r} matched nothing, so it changed nothing"
        path.write_text(mutated, encoding="utf-8")

    def test_a_full_phase_list_is_green_at_the_shipped_floor(self, tmp_path: Path) -> None:
        """A sentinel declaring exactly the floor's phase count passes at the shipped floor."""
        root = self._tree_at_the_floor(tmp_path / "control")
        result = _run(root, sentinel_min_phases=None)
        assert result.returncode == 0, result.stdout + result.stderr
        assert f">= {gate.SENTINEL_MIN_PHASES} phase(s)" in result.stdout, result.stdout

    def test_removing_one_items_number_reds(self, tmp_path: Path) -> None:
        """Removing one item's `N.` number fails on the floor, not as a coverage violation."""
        root = self._tree_at_the_floor(tmp_path / "unnumbered")
        assert _run(root, sentinel_min_phases=None).returncode == 0, (
            "the control fixture is not green"
        )

        dropped = self.FLOOR_PHASES[-1]
        self._mutate_sentinel_docstring(root, rf"^\s*\d+\.\s+(``{dropped}``)", r"\1")

        result = _run(root, sentinel_min_phases=None)
        assert result.returncode == 1, result.stdout + result.stderr
        assert f"found {gate.SENTINEL_MIN_PHASES - 1} phase(s)" in result.stderr, result.stderr
        assert "does not name phase(s)" not in result.stderr, (
            "the red is a coverage violation, not the completeness floor"
        )
        assert dropped not in result.stderr.split("where at least")[0], (
            "the dropped phase is still being reported as discovered"
        )

    def test_deleting_an_item_outright_also_reds(self, tmp_path: Path) -> None:
        """Deleting a phase item outright fails on the floor."""
        root = self._tree_at_the_floor(tmp_path / "deleted")
        assert _run(root, sentinel_min_phases=None).returncode == 0, (
            "the control fixture is not green"
        )

        self._mutate_sentinel_docstring(root, rf"^\s*\d+\.\s+``{self.FLOOR_PHASES[-1]}``.*\n", "")

        result = _run(root, sentinel_min_phases=None)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "silently stopped comparing" in result.stderr, result.stderr

    def test_the_floor_is_a_floor_and_not_an_equality(self, tmp_path: Path) -> None:
        """More phases than the floor pass when named in the row and fail when one is unnamed."""
        root = _write_tree(
            tmp_path / "grown",
            phases={SENTINEL: ["alpha", "beta", "gamma"]},
            rows={SENTINEL: "does `alpha`, `beta` and `gamma`"},
        )
        assert _run(root, sentinel_min_phases=2).returncode == 0

        ungated = _write_tree(
            tmp_path / "grown-unnamed",
            phases={SENTINEL: ["alpha", "beta", "gamma"]},
            rows={SENTINEL: "does `alpha` and `beta`"},
        )
        result = _run(ungated, sentinel_min_phases=2)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "gamma" in result.stderr

    def test_the_floor_is_reported_on_a_passing_run(self, tmp_path: Path) -> None:
        """A passing run prints the floor and the discovered phase count."""
        root = _write_tree(
            tmp_path / "visible",
            phases={SENTINEL: ["alpha", "beta"]},
            rows={SENTINEL: "does `alpha` and `beta`"},
        )
        result = _run(root, sentinel_min_phases=2)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "floor:" in result.stdout, result.stdout
        assert ">= 2 phase(s) and yields 2" in result.stdout, result.stdout

    def test_no_real_invocation_weakens_the_floor(self) -> None:
        """Neither `run_gates.py` nor the Makefile passes `--sentinel-min-phases`."""
        for name in ("run_gates.py",):
            source = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
            assert "--sentinel-min-phases" not in source, (
                f"scripts/{name} overrides the sentinel floor; the shipped default must stand"
            )
        assert "--sentinel-min-phases" not in (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


class TestTheParserItself:
    """Unit tests for the docstring and table parsers."""

    def test_the_docstring_reader_takes_the_leading_docstring_only(self) -> None:
        source = '#!/usr/bin/env python3\n"""First.\n\n1. ``alpha`` -- x.\n"""\n\nX = """1. ``beta``"""\n'
        assert gate._PHASE_ITEM_RE.findall(gate.module_docstring(source)) == ["alpha"]

    def test_a_module_with_no_docstring_yields_nothing(self) -> None:
        assert gate.module_docstring("X = 1\n") == ""

    def test_the_numbering_is_not_required_to_match_execution_order(self) -> None:
        """The parse takes phase names in docstring order and ignores their numbers."""
        docstring = (
            "5. ``isolated_import`` -- runs last despite its number.\n6. ``source_parity`` -- x.\n"
        )
        assert gate._PHASE_ITEM_RE.findall(docstring) == ["isolated_import", "source_parity"]

    def test_a_blank_line_ends_the_gate_table(self) -> None:
        text = f"{_TABLE}| `a` | x |\n\n| `b` | y |\n"
        assert sorted(gate.parse_gate_rows(text)) == ["a"]


def test_the_real_repository_passes_its_own_gate() -> None:
    """The gate passes against the real repository at the shipped floor."""
    result = _run(REPO_ROOT, sentinel_min_phases=None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert gate.SENTINEL_SUBJECT in result.stdout


def test_this_gate_is_registered_in_run_gates() -> None:
    """The gate is registered in `run_gates.py`."""
    registry = (REPO_ROOT / "scripts" / "run_gates.py").read_text(encoding="utf-8")
    assert "check_readme_gate_phases" in registry
    assert "scripts/check_readme_gate_phases.py" in registry


@pytest.mark.parametrize("stem", ["check_readme_gate_phases"])
def test_the_gate_script_is_tracked_in_git(stem: str) -> None:
    """The gate script is tracked by git."""
    listed = proc_run.run(
        ["git", "ls-files", f"scripts/{stem}.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert listed.strip(), f"scripts/{stem}.py is registered but not tracked; git add it"
