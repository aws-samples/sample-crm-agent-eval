# SPDX-License-Identifier: MIT
"""Tests for `scripts/check_testid_preservation.py`.

Fixtures live in `tmp_path`; the end-to-end cases drive the real script over a scratch copy of the
real frontend tree.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_testid_preservation.py"
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
BASELINE = REPO_ROOT / "scripts" / "aria_landmark_baseline.json"


def _load_module(name: str, path: Path) -> ModuleType:
    """Import a script from `scripts/` without requiring it to be importable as a package."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module("check_testid_preservation", GATE)
run_gates = _load_module("run_gates_for_test", REPO_ROOT / "scripts" / "run_gates.py")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        [sys.executable, str(GATE), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "src"
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def _run_isolated(root: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the gate over a synthetic tree with an empty baseline and no reference-only roots."""
    baseline = tmp_path / "empty-baseline.json"
    baseline.write_text("{}\n", encoding="utf-8")
    return _run(
        "--root",
        str(root),
        "--baseline",
        str(baseline),
        "--extra-test-root",
        str(tmp_path / "no-extra-roots"),
        *args,
    )


# ---------------------------------------------------------------------------
# The real tree, and the skip disposition.
# ---------------------------------------------------------------------------


def test_passes_on_the_real_frontend_tree() -> None:
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_skips_with_exit_three_when_the_root_is_absent(tmp_path: Path) -> None:
    result = _run("--root", str(tmp_path / "absent"))
    assert result.returncode == gate.SKIP_EXIT_STATUS == 3
    assert "SKIP" in result.stdout
    assert "Nothing was verified" in result.stdout


def test_skips_when_there_are_tests_but_no_components(tmp_path: Path) -> None:
    """A tree with tests but no components exits 3 (verified nothing)."""
    root = _tree(tmp_path, {"a.test.tsx": 'getByTestId("x");\n'})
    result = _run_isolated(root, tmp_path)
    assert result.returncode == 3
    assert "SKIP" in result.stdout


def test_skips_when_there_are_components_but_no_tests(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"A.tsx": 'export const A = () => <p data-testid="x" />;\n'})
    result = _run_isolated(root, tmp_path)
    assert result.returncode == 3


def test_skip_status_matches_run_gates_contract() -> None:
    assert gate.SKIP_EXIT_STATUS == run_gates.GATE_SKIP_EXIT_STATUS


# ---------------------------------------------------------------------------
# data-testid resolution.
# ---------------------------------------------------------------------------


class TestTestIdResolution:
    def test_a_removed_testid_fails(self, tmp_path: Path) -> None:
        """A testid referenced by a test but absent from the markup fails."""
        root = _tree(
            tmp_path,
            {
                "Board.tsx": "export const B = () => <p />;\n",
                "Board.test.tsx": 'it("loads", () => screen.getByTestId("pipeline-loading"));\n',
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 1
        assert "pipeline-loading" in result.stderr
        assert "a retained test is not modified" in result.stderr

    def test_the_advice_names_both_valid_resolutions(self, tmp_path: Path) -> None:
        """The failure message names both restoring the attribute and removing the reference."""
        root = _tree(
            tmp_path,
            {
                "Board.tsx": "export const B = () => <p />;\n",
                "Board.test.tsx": 'it("loads", () => screen.getByTestId("retired-thing"));\n',
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 1
        assert "restore the attribute in the markup" in result.stderr
        assert "retired" in result.stderr
        assert "remove the now-orphaned reference" in result.stderr

    def test_a_present_testid_passes(self, tmp_path: Path) -> None:
        root = _tree(
            tmp_path,
            {
                "Board.tsx": 'export const B = () => <p data-testid="pipeline-loading" />;\n',
                "Board.test.tsx": 'it("loads", () => screen.getByTestId("pipeline-loading"));\n',
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "call",
        [
            'getByTestId("x")',
            'queryByTestId("x")',
            'findByTestId("x")',
            'getAllByTestId("x")',
            'queryAllByTestId("x")',
            "getByTestId(`x`)",
            "getByTestId('x')",
            "querySelector('[data-testid=\"x\"]')",
            'querySelector("[data-testid=\\"x\\"]")',
        ],
    )
    def test_every_reference_form_is_recognised(self, tmp_path: Path, call: str) -> None:
        """Every supported reference form is recognised."""
        root = _tree(
            tmp_path,
            {"A.tsx": "export const A = () => <p />;\n", "A.test.tsx": f"it('r', () => {call});\n"},
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 1, f"{call} was not recognised as a reference"

    def test_a_definition_in_a_conditional_resolves_both_branches(self, tmp_path: Path) -> None:
        root = _tree(
            tmp_path,
            {
                "A.tsx": (
                    "export const A = () => "
                    '<p data-testid={denied ? "scope-denied-banner" : "results"} />;\n'
                ),
                "A.test.tsx": ('getByTestId("scope-denied-banner");\ngetByTestId("results");\n'),
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_template_literal_definition_resolves_by_prefix(self, tmp_path: Path) -> None:
        """A template-literal definition resolves a reference carrying its prefix."""
        root = _tree(
            tmp_path,
            {
                "Board.tsx": (
                    "export const B = () => <div data-testid={`pipeline-column-${stage}`} />;\n"
                ),
                "Board.test.tsx": 'getByTestId("pipeline-column-Qualification");\n',
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_template_prefix_does_not_resolve_an_unrelated_id(self, tmp_path: Path) -> None:
        """A template prefix does not resolve an id that lacks the prefix."""
        root = _tree(
            tmp_path,
            {
                "Board.tsx": "export const B = () => <div data-testid={`col-${stage}`} />;\n",
                "Board.test.tsx": 'getByTestId("something-else");\n',
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 1
        assert "something-else" in result.stderr

    def test_a_stub_defined_inside_the_referencing_test_resolves(self, tmp_path: Path) -> None:
        """A testid defined by a stub inside the referencing test file resolves."""
        root = _tree(
            tmp_path,
            {
                "A.tsx": "export const A = () => <p />;\n",
                "App.test.tsx": (
                    'const Stub = () => <p data-testid="params">{x}</p>;\n'
                    'it("routes", () => getByTestId("params"));\n'
                ),
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_constant_mediated_pair_is_visible_to_the_gate(self, tmp_path: Path) -> None:
        """A testid shared through an exported constant on both sides resolves."""
        root = _tree(
            tmp_path,
            {
                "Widget.tsx": (
                    'export const WIDGET_TESTID = "widget-root";\n'
                    "export const W = () => <div data-testid={WIDGET_TESTID} />;\n"
                ),
                "Widget.test.tsx": (
                    'import { WIDGET_TESTID } from "./Widget";\n'
                    'it("r", () => getByTestId(WIDGET_TESTID));\n'
                ),
            },
        )
        assert _run_isolated(root, tmp_path).returncode == 0

    def test_deleting_a_constant_mediated_attribute_now_fails(self, tmp_path: Path) -> None:
        """Removing a constant-mediated attribute reports the reference unresolved."""
        root = _tree(
            tmp_path,
            {
                "Widget.tsx": (
                    'export const WIDGET_TESTID = "widget-root";\nexport const W = () => <div />;\n'
                ),
                "Widget.test.tsx": (
                    'import { WIDGET_TESTID } from "./Widget";\n'
                    'it("r", () => getByTestId(WIDGET_TESTID));\n'
                ),
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "widget-root" in result.stderr

    def test_a_cross_file_constant_resolves_and_its_removal_names_both_test_files(
        self, tmp_path: Path
    ) -> None:
        """Removing a cross-file constant's attribute names every referencing test file."""
        root = _tree(
            tmp_path,
            {
                "Card.tsx": 'export const CARD_TESTID = "card";\nexport const C = () => <p />;\n',
                "Card.test.tsx": (
                    'import { CARD_TESTID } from "./Card";\n'
                    'it("a", () => getByTestId(CARD_TESTID));\n'
                ),
                "Trace.test.tsx": (
                    'import { CARD_TESTID } from "./Card";\n'
                    'it("b", () => getByTestId(CARD_TESTID));\n'
                ),
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 1
        assert "Card.test.tsx" in result.stderr and "Trace.test.tsx" in result.stderr

    def test_a_conflicting_constant_is_dropped_rather_than_guessed(self, tmp_path: Path) -> None:
        """A constant exported with conflicting values resolves to neither value."""
        sources = {
            "A.tsx": 'export const T = "alpha";\nexport const A = () => <p />;\n',
            "B.tsx": 'export const T = "beta";\nexport const B = () => <p />;\n',
            "A.test.tsx": 'it("r", () => getByTestId(T));\n',
        }
        root = _tree(tmp_path, sources)
        result = _run_isolated(root, tmp_path)
        assert "alpha" not in result.stderr and "beta" not in result.stderr

    def test_an_unresolvable_identifier_contributes_no_false_definition(
        self, tmp_path: Path
    ) -> None:
        """A prop-mediated definition does not resolve a literal reference."""
        root = _tree(
            tmp_path,
            {
                "P.tsx": "export const P = ({testid}) => <p data-testid={testid} />;\n",
                "P.test.tsx": 'it("r", () => getByTestId("passed-in"));\n',
                "Pad.tsx": "".join(
                    f'export const Q{i} = () => <p data-testid="pad-{i}" />;\n' for i in range(6)
                ),
                "Pad.test.tsx": "".join(
                    f'it("p{i}", () => getByTestId("pad-{i}"));\n' for i in range(6)
                ),
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 1
        assert "passed-in" in result.stderr


class TestAttributeOperatorSelectors:
    """Attribute-operator selectors (`^=`, `$=`, `*=`) on `data-testid` are checked."""

    def test_a_prefix_selector_reference_with_no_matching_definition_fails(
        self, tmp_path: Path
    ) -> None:
        """A prefix selector with no matching definition fails."""
        root = _tree(
            tmp_path,
            {
                "A.tsx": 'export const A = () => <p data-testid="unrelated" />;\n',
                "a.test.tsx": "document.querySelectorAll('[data-testid^=\"mode-label-\"]');\n",
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 1
        assert 'data-testid^="mode-label-"' in result.stderr

    def test_a_prefix_selector_resolves_against_a_literal_carrying_the_prefix(
        self, tmp_path: Path
    ) -> None:
        root = _tree(
            tmp_path,
            {
                "A.tsx": 'export const A = () => <p data-testid="mode-label-anthropic" />;\n',
                "a.test.tsx": "document.querySelectorAll('[data-testid^=\"mode-label-\"]');\n",
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 0, result.stderr

    def test_a_prefix_selector_resolves_against_a_template_definition(self, tmp_path: Path) -> None:
        """A prefix selector resolves against a template definition with that prefix."""
        root = _tree(
            tmp_path,
            {
                "A.tsx": (
                    "export const A = ({ id }: { id: string }) => (\n"
                    "  <p data-testid={`mode-label-${id}`} />\n"
                    ");\n"
                ),
                "a.test.tsx": "document.querySelectorAll('[data-testid^=\"mode-label-\"]');\n",
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 0, result.stderr

    def test_a_prefix_selector_narrower_than_the_template_prefix_still_resolves(
        self, tmp_path: Path
    ) -> None:
        """A prefix selector extending a template's prefix resolves against it."""
        root = _tree(
            tmp_path,
            {
                "A.tsx": (
                    "export const A = ({ id }: { id: string }) => (\n"
                    "  <p data-testid={`mode-label-${id}`} />\n"
                    ");\n"
                ),
                "a.test.tsx": "document.querySelectorAll('[data-testid^=\"mode-label-open\"]');\n",
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 0, result.stderr

    def test_an_unrelated_prefix_does_not_resolve_against_a_template_definition(
        self, tmp_path: Path
    ) -> None:
        """A prefix selector unrelated to a template's prefix does not resolve against it."""
        root = _tree(
            tmp_path,
            {
                "A.tsx": (
                    "export const A = ({ id }: { id: string }) => (\n"
                    "  <p data-testid={`mode-label-${id}`} />\n"
                    ");\n"
                ),
                "a.test.tsx": "document.querySelectorAll('[data-testid^=\"arm-row-\"]');\n",
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 1
        assert 'data-testid^="arm-row-"' in result.stderr

    def test_a_suffix_selector_is_checked_and_not_accepted_by_a_template(
        self, tmp_path: Path
    ) -> None:
        """A `$=` suffix selector is checked and is not resolved by a template definition."""
        root = _tree(
            tmp_path,
            {
                "A.tsx": (
                    "export const A = ({ id }: { id: string }) => (\n"
                    "  <p data-testid={`row-${id}`} />\n"
                    ");\n"
                ),
                "a.test.tsx": "document.querySelectorAll('[data-testid$=\"-total\"]');\n",
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 1
        assert 'data-testid$="-total"' in result.stderr

    def test_a_substring_selector_resolves_against_a_containing_literal(
        self, tmp_path: Path
    ) -> None:
        root = _tree(
            tmp_path,
            {
                "A.tsx": 'export const A = () => <p data-testid="arm-row-total" />;\n',
                "a.test.tsx": "document.querySelectorAll('[data-testid*=\"row\"]');\n",
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 0, result.stderr

    def test_an_interpolated_operator_selector_is_skipped_rather_than_misread(
        self, tmp_path: Path
    ) -> None:
        """An interpolated operator selector is skipped."""
        root = _tree(
            tmp_path,
            {
                "A.tsx": 'export const A = () => <p data-testid="x" />;\n',
                "a.test.tsx": (
                    "const p = 'mode';\ndocument.querySelectorAll(`[data-testid^=\"${p}-\"]`);\n"
                ),
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 0, result.stderr

    def test_the_exact_selector_path_is_unchanged(self, tmp_path: Path) -> None:
        """An exact selector still resolves only by exact match."""
        root = _tree(
            tmp_path,
            {
                "A.tsx": 'export const A = () => <p data-testid="unrelated" />;\n',
                "a.test.tsx": "document.querySelector('[data-testid=\"gone\"]');\n",
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 1
        assert '`data-testid="gone"`' in result.stderr


class TestReferenceOnlyRoots:
    """The browser journeys are a reference-only root checked against `frontend/src` markup."""

    def test_the_default_reference_only_root_exists(self) -> None:
        """Every default reference-only root exists and contains journey specs."""
        assert gate.DEFAULT_EXTRA_TEST_ROOTS, "no reference-only root is configured"
        for root in gate.DEFAULT_EXTRA_TEST_ROOTS:
            assert root.is_dir(), f"configured reference-only root {root} does not exist"
            assert list(root.rglob("*.spec.ts")), f"{root} contains no journey specs to check"

    def test_a_reference_in_an_extra_root_is_checked(self, tmp_path: Path) -> None:
        """An unresolved reference in an extra root fails and names the journey file."""
        root = _tree(tmp_path, {"A.tsx": 'export const A = () => <p data-testid="present" />;\n'})
        journeys = tmp_path / "journeys"
        journeys.mkdir()
        (journeys / "01-x.spec.ts").write_text(
            'page.getByTestId("absent-from-markup");\n', encoding="utf-8"
        )
        baseline = tmp_path / "empty-baseline.json"
        baseline.write_text("{}\n", encoding="utf-8")
        result = _run(
            "--root",
            str(root),
            "--baseline",
            str(baseline),
            "--extra-test-root",
            str(journeys),
        )
        assert result.returncode == 1
        assert "absent-from-markup" in result.stderr
        assert "01-x.spec.ts" in result.stderr

    def test_an_extra_root_does_not_supply_definitions(self, tmp_path: Path) -> None:
        """A `data-testid` string inside an extra root supplies no definition."""
        root = _tree(tmp_path, {"A.tsx": 'export const A = () => <p data-testid="other" />;\n'})
        journeys = tmp_path / "journeys"
        journeys.mkdir()
        (journeys / "01-x.spec.ts").write_text(
            'const selector = \'data-testid="ghost"\';\npage.getByTestId("ghost");\n',
            encoding="utf-8",
        )
        baseline = tmp_path / "empty-baseline.json"
        baseline.write_text("{}\n", encoding="utf-8")
        result = _run(
            "--root",
            str(root),
            "--baseline",
            str(baseline),
            "--extra-test-root",
            str(journeys),
        )
        assert result.returncode == 1
        assert "ghost" in result.stderr

    def test_an_absent_extra_root_contributes_nothing_and_does_not_fail(
        self, tmp_path: Path
    ) -> None:
        """An absent extra root contributes nothing and the run passes."""
        root = _tree(
            tmp_path,
            {
                "A.tsx": 'export const A = () => <p data-testid="x" />;\n',
                "a.test.tsx": 'getByTestId("x");\n',
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 0, result.stderr

    def test_a_synthetic_run_does_not_pull_in_the_real_journeys(self, tmp_path: Path) -> None:
        """An isolated synthetic run mentions no real journey spec."""
        root = _tree(
            tmp_path,
            {
                "A.tsx": 'export const A = () => <p data-testid="x" />;\n',
                "a.test.tsx": 'getByTestId("x");\n',
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 0, result.stderr
        assert "spec.ts" not in result.stdout + result.stderr, (
            "a synthetic run mentioned a journey spec, so it read outside its fixture tree"
        )


class TestReferenceFloor:
    def test_a_collapsed_reference_set_skips_rather_than_passing(self, tmp_path: Path) -> None:
        """A large suite whose references all fail to resolve exits 3 rather than passing."""
        files = {"A.tsx": "export const A = () => <p />;\n"}
        # More than FLOOR_APPLIES_ABOVE_TEST_FILES test files, none of whose references resolve.
        for i in range(gate.FLOOR_APPLIES_ABOVE_TEST_FILES + 2):
            files[f"T{i}.test.tsx"] = f"it('r{i}', () => getByTestId(UNKNOWN_CONST_{i}));\n"
        root = _tree(tmp_path, files)
        result = _run_isolated(root, tmp_path)
        assert result.returncode == gate.SKIP_EXIT_STATUS == 3
        assert "[SKIP]" in result.stdout
        assert "nothing was verified" in result.stdout.lower()

    def test_the_advice_does_not_tell_anyone_to_use_string_literals(self, tmp_path: Path) -> None:
        """The floor message advises against switching call sites to string literals."""
        files = {"A.tsx": "export const A = () => <p />;\n"}
        for i in range(gate.FLOOR_APPLIES_ABOVE_TEST_FILES + 2):
            files[f"T{i}.test.tsx"] = f"it('r{i}', () => nothing{i});\n"
        root = _tree(tmp_path, files)
        result = _run_isolated(root, tmp_path)
        assert "do NOT switch call sites back to string literals" in result.stdout

    def test_a_real_violation_outranks_the_floor(self, tmp_path: Path) -> None:
        """An unresolved reference exits 1 even when the resolved count is below the floor."""
        root = _tree(
            tmp_path,
            {
                "A.tsx": "export const A = () => <p />;\n",
                "A.test.tsx": 'it("r", () => getByTestId("gone"));\n',
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "gone" in result.stderr

    def test_the_real_tree_is_far_above_the_floor(self) -> None:
        """The real tree resolves more than three times the reference floor."""
        root = REPO_ROOT / "frontend" / "src"
        _tests, sources = gate.collect_files(root)
        tests = gate.collect_files(root)[0]
        constants = gate.exported_string_constants(sources + tests)
        references = gate.referenced_test_ids(tests, constants)
        assert len(references) > gate.MINIMUM_REFERENCE_FLOOR * 3, (
            f"only {len(references)} references resolve, uncomfortably close to the floor of "
            f"{gate.MINIMUM_REFERENCE_FLOOR}"
        )


class TestConstantResolution:
    def test_the_constant_table_finds_the_three_live_testid_constants(self) -> None:
        """The real tree's three constant-mediated testids resolve from exported constants."""
        root = REPO_ROOT / "frontend" / "src"
        tests, sources = gate.collect_files(root)
        constants = gate.exported_string_constants(sources + tests)
        values = set(constants.values())
        for expected in ("proposal-card", "usage-meter", "event-trace"):
            assert expected in values, f"{expected} is not resolvable from an exported constant"

    def test_a_ts_module_is_not_read_as_a_testid_definition(self, tmp_path: Path) -> None:
        """A `.ts` selector string does not count as a testid definition."""
        root = _tree(
            tmp_path,
            {
                "sel.ts": "export const S = '[data-testid=\"from-ts\"]';\n",
                "A.tsx": "export const A = () => <p />;\n",
                "A.test.tsx": 'it("r", () => getByTestId("from-ts"));\n',
            },
        )
        assert _run_isolated(root, tmp_path).returncode == 1

    def test_source_suffixes_exclude_ts(self) -> None:
        assert ".ts" not in gate._SOURCE_SUFFIXES

    def test_a_stub_in_a_different_test_file_does_not_resolve(self, tmp_path: Path) -> None:
        """A stub defined in a different test file does not resolve the reference."""
        root = _tree(
            tmp_path,
            {
                "A.tsx": "export const A = () => <p />;\n",
                "One.test.tsx": 'const S = () => <p data-testid="shared" />;\n',
                "Two.test.tsx": 'it("r", () => getByTestId("shared"));\n',
            },
        )
        result = _run_isolated(root, tmp_path)
        assert result.returncode == 1
        assert "Two.test.tsx" in result.stderr

    def test_generated_directories_are_not_scanned(self, tmp_path: Path) -> None:
        root = _tree(
            tmp_path,
            {
                "A.tsx": 'export const A = () => <p data-testid="real" />;\n',
                "A.test.tsx": 'getByTestId("real");\n',
                "types/generated/g.tsx": 'export const G = () => <p data-testid="gen" />;\n',
            },
        )
        assert not any("generated" in p.parts for p in gate.collect_files(root)[1])


# ---------------------------------------------------------------------------
# ARIA landmark floor.
# ---------------------------------------------------------------------------


class TestAriaLandmarkFloor:
    def test_a_dropped_aria_live_fails(self) -> None:
        current = {"pages/pipeline/Board.tsx": {'role="status"': 1}}
        baseline = {"pages/pipeline/Board.tsx": {'role="status"': 1, "aria-live": 1}}
        problems = gate.landmark_regressions(baseline, current)
        assert len(problems) == 1
        assert "aria-live" in problems[0]
        assert "dropped from 1 to 0" in problems[0]

    def test_a_reduced_count_fails(self) -> None:
        problems = gate.landmark_regressions(
            {"A.tsx": {'role="alert"': 3}}, {"A.tsx": {'role="alert"': 2}}
        )
        assert len(problems) == 1
        assert "dropped from 3 to 2" in problems[0]

    def test_a_vanished_file_fails(self) -> None:
        problems = gate.landmark_regressions({"A.tsx": {'role="alert"': 1}}, {})
        assert len(problems) == 1
        assert "gone" in problems[0]

    def test_an_increased_count_passes(self) -> None:
        """An increased landmark count passes."""
        problems = gate.landmark_regressions(
            {"A.tsx": {'role="status"': 1}}, {"A.tsx": {'role="status"': 4}}
        )
        assert problems == []

    def test_counts_only_explicitly_authored_attributes(self, tmp_path: Path) -> None:
        """Only explicitly authored ARIA attributes are counted, not implicit roles."""
        root = _tree(
            tmp_path,
            {
                "A.tsx": (
                    "export const A = () => <><button>Go</button>"
                    '<p role="status" /><p aria-live="polite" /></>;\n'
                )
            },
        )
        counts = gate.count_aria_landmarks(gate.collect_files(root)[1], root)
        assert counts == {"A.tsx": {'role="status"': 1, "aria-live": 1}}

    def test_a_prose_mention_of_aria_live_is_not_counted(self, tmp_path: Path) -> None:
        """A prose mention of `aria-live` in a comment is not counted."""
        root = _tree(
            tmp_path,
            {"A.tsx": "/* announced via the `aria-live` region below */\nexport const A = 1;\n"},
        )
        assert gate.count_aria_landmarks(gate.collect_files(root)[1], root) == {}


# ---------------------------------------------------------------------------
# End-to-end non-vacuity against a scratch copy of the real tree.
# ---------------------------------------------------------------------------


class TestEndToEndAgainstRealTree:
    def _scratch(self, tmp_path: Path) -> Path:
        dest = tmp_path / "src"
        shutil.copytree(FRONTEND_SRC, dest)
        return dest

    def test_renaming_a_real_testid_fails(self, tmp_path: Path) -> None:
        root = self._scratch(tmp_path)
        board = root / "pages" / "pipeline" / "PipelineBoard.tsx"
        text = board.read_text(encoding="utf-8")
        mutated = text.replace('data-testid="pipeline-loading"', 'data-testid="renamed"', 1)
        assert mutated != text, "fixture setup did not find the attribute"
        board.write_text(mutated, encoding="utf-8")

        result = _run("--root", str(root), "--baseline", str(BASELINE))

        assert result.returncode == 1, result.stdout + result.stderr
        assert "pipeline-loading" in result.stderr

    def test_removing_a_real_aria_landmark_fails(self, tmp_path: Path) -> None:
        """Removing a landmark from a file at its baseline floor exactly fails the gate."""
        root = self._scratch(tmp_path)
        _test_files, source_files = gate.collect_files(root)
        counts = gate.count_aria_landmarks(source_files, root)
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

        target: tuple[str, str] | None = None
        for relpath, floors in sorted(baseline.items()):
            current = counts.get(relpath, {})
            for attribute, floor in sorted(floors.items()):
                if floor > 0 and current.get(attribute, 0) == floor:
                    target = (relpath, attribute)
                    break
            if target is not None:
                break
        assert target is not None, (
            "no file in the ARIA baseline is at its floor exactly, so removing a landmark anywhere "
            "would leave every file still at-or-above and this check could not fire. The baseline "
            "has drifted below the tree everywhere and needs refreshing — this is a real finding, "
            "not a fixture problem."
        )
        relpath, attribute = target

        path = root / relpath
        text = path.read_text(encoding="utf-8")
        mutated = re.sub(rf'\s*{re.escape(attribute)}="[^"]*"', "", text, count=1)
        assert mutated != text, f"could not find a {attribute} attribute to remove in {relpath}"
        path.write_text(mutated, encoding="utf-8")

        result = _run("--root", str(root), "--baseline", str(BASELINE))

        assert result.returncode == 1, result.stdout + result.stderr
        assert attribute in result.stderr
        assert Path(relpath).name in result.stderr

    def test_the_unmodified_scratch_copy_passes(self, tmp_path: Path) -> None:
        """The unmodified scratch copy passes."""
        root = self._scratch(tmp_path)
        result = _run("--root", str(root), "--baseline", str(BASELINE))
        assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# The committed baseline.
# ---------------------------------------------------------------------------


def test_the_committed_baseline_matches_the_current_tree() -> None:
    """The committed ARIA baseline reports no regression against the current tree."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = gate.count_aria_landmarks(gate.collect_files(FRONTEND_SRC)[1], FRONTEND_SRC)
    assert gate.landmark_regressions(baseline, current) == []


def test_the_committed_baseline_is_not_empty() -> None:
    """The committed ARIA baseline covers more than 10 files and 20 landmarks."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert len(baseline) > 10
    assert sum(sum(v.values()) for v in baseline.values()) > 20


def test_the_baseline_covers_every_page_the_reskin_touches() -> None:
    """The ARIA baseline covers the pipeline, accounts, contacts, opportunities, activities and
    settings pages."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    covered = {key.split("/")[1] for key in baseline if key.startswith("pages/")}
    for page in ("pipeline", "accounts", "contacts", "opportunities", "activities", "settings"):
        assert page in covered, f"pages/{page} has no ARIA landmark floor"


# ---------------------------------------------------------------------------------------------
# Definition scope: `.ts` is not a definition suffix
# ---------------------------------------------------------------------------------------------


def test_a_ts_module_is_not_read_as_a_testid_definition(tmp_path: Path) -> None:
    """A testid appearing only as a selector string in a `.ts` module does not resolve."""
    root = _tree(
        tmp_path,
        {
            "selectors.ts": "export const CARD = '[data-testid=\"ghost-card\"]';\n",
            "Thing.tsx": "export const Thing = () => null;\n",
            "Thing.test.tsx": 'getByTestId("ghost-card");\n',
        },
    )

    result = _run_isolated(root, tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "ghost-card" in result.stderr


def test_source_suffixes_exclude_ts() -> None:
    """`_SOURCE_SUFFIXES` is exactly `.tsx` and `.jsx`."""
    assert frozenset({".tsx", ".jsx"}) == gate._SOURCE_SUFFIXES
