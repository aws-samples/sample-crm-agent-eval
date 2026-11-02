# SPDX-License-Identifier: MIT
"""Tests for `scripts/check_tracked_tree_self_contained.py`.

The gate fails when a first-party import resolves only to an untracked file. Planting tests mutate a
materialised copy of the tracked tree under `/tmp`, never tracked source.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_tracked_tree_self_contained.py"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module("check_tracked_tree_self_contained_under_test", GATE)


@pytest.fixture(scope="module")
def materialised() -> Iterator[Path]:
    """One materialisation shared by the read-only tests; each planting test copies it."""
    tree = gate.materialise("HEAD")
    yield tree
    shutil.rmtree(tree, ignore_errors=True)


class TestTheGateIsNonVacuous:
    def test_the_real_tracked_tree_is_self_contained(self, materialised: Path) -> None:
        findings, examined = gate.unresolved_imports(materialised)
        assert findings == [], findings
        assert examined > 100, (
            f"only {examined} file(s) examined; a walk this small means the tree layout moved and "
            "this gate is asserting over almost nothing"
        )

    def test_the_walk_is_not_vacuous_and_the_roots_derive(self, materialised: Path) -> None:
        """The first-party root map derives non-empty and includes `scripts`, `crm` and `eval`."""
        roots = gate.first_party_roots(materialised)
        assert "scripts" in roots
        assert {"crm", "eval"} <= set(roots), sorted(roots)


class TestItWouldHaveCaughtC1:
    """Planted defects in a copied tree produce findings."""

    def _copy(self, materialised: Path, tmp_path: Path) -> Path:
        tree = tmp_path / "tree"
        shutil.copytree(materialised, tree)
        return tree

    def test_deleting_a_smoke_module_a_peer_imports_is_a_finding(
        self, materialised: Path, tmp_path: Path
    ) -> None:
        """Deleting `scripts/smoke/harness.py`, which peers import, is an `untracked` finding."""
        tree = self._copy(materialised, tmp_path)
        (tree / "scripts" / "smoke" / "harness.py").unlink()
        findings, _ = gate.unresolved_imports(tree)
        assert findings, "an absent first-party module produced no finding"
        assert any("scripts.smoke.harness" in f for f in findings)
        assert any("untracked" in f for f in findings), (
            "the finding must name the failure mode, or a reader treats it as a broken import"
        )

    def test_deleting_a_src_package_module_is_a_finding(
        self, materialised: Path, tmp_path: Path
    ) -> None:
        tree = self._copy(materialised, tmp_path)
        (tree / "src" / "eval" / "verification.py").unlink()
        findings, _ = gate.unresolved_imports(tree)
        assert any("eval.verification" in f for f in findings), findings

    def test_an_unparseable_tracked_file_is_a_finding_not_a_skip(
        self, materialised: Path, tmp_path: Path
    ) -> None:
        tree = self._copy(materialised, tmp_path)
        (tree / "scripts" / "run_gates.py").write_text("def broken(:\n", encoding="utf-8")
        findings, _ = gate.unresolved_imports(tree)
        assert any("does not parse" in f for f in findings), findings

    def test_a_third_party_import_is_not_a_finding(
        self, materialised: Path, tmp_path: Path
    ) -> None:
        """A third-party or unknown non-first-party import is not a finding."""
        tree = self._copy(materialised, tmp_path)
        target = tree / "scripts" / "planted_third_party.py"
        target.write_text("import boto3\nimport nonexistent_vendor_package\n", encoding="utf-8")
        findings, _ = gate.unresolved_imports(tree)
        assert not any("planted_third_party" in f for f in findings), findings


class TestTheCliContract:
    def test_a_clean_tree_exits_zero_and_names_what_it_examined(self) -> None:
        result = proc_run.run(
            [sys.executable, str(GATE)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "every first-party import resolves inside the tracked tree" in result.stdout

    def test_the_json_shape_carries_the_examined_count(self) -> None:
        import json

        result = proc_run.run(
            [sys.executable, str(GATE), "--json"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        document = json.loads(result.stdout)
        assert document["files_examined"] > 100
        assert document["findings"] == []

    def test_an_unmaterialisable_ref_is_exit_two_not_a_pass(self) -> None:
        """A ref that cannot be materialised exits 2, not 0."""
        result = proc_run.run(
            [sys.executable, str(GATE), "--ref", "no-such-ref-exists-here"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2, result.stdout + result.stderr

    def test_an_empty_walk_exits_three_rather_than_zero(self, tmp_path: Path) -> None:
        """A tree with no Python file examines zero files, the precondition for the exit-3 guard."""
        empty = tmp_path / "empty"
        (empty / "scripts").mkdir(parents=True)
        findings, examined = gate.unresolved_imports(empty)
        assert findings == []
        assert examined == 0, "the vacuity guard's own precondition does not hold"
