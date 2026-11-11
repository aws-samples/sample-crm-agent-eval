# SPDX-License-Identifier: MIT
"""Integrity tests for `scripts/check_cost_withheld`: installed behaviour, the provisional
interlock, the frontend consumer, artifact scope, and verdict/winner/ranking absence.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PY = REPO_ROOT / "scripts" / "check_cost_withheld.py"
GATE_SH = REPO_ROOT / "scripts" / "check_cost_withheld.sh"
PRICING = REPO_ROOT / "config" / "pricing.yaml"
RESULTS = REPO_ROOT / "results"


def _load_module(name: str, path: Path) -> ModuleType:
    """Import a script from `scripts/` without requiring it to be importable as a package."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module("check_cost_withheld_integrity", GATE_PY)


PROVISIONAL_PRICING = """\
price_table_version: "0.1.0-provisional"
provisional: true
unit: per_1m_tokens
rates:
  anthropic.claude-opus-5-5:
    input: 0.0
    output: 0.0
"""

SETTLED_PRICING = PROVISIONAL_PRICING.replace("provisional: true", "provisional: false")


def _tree(tmp_path: Path, *, pricing: str, artifacts: dict[str, str]) -> tuple[Path, Path]:
    """Write a synthetic pricing file and results tree; return `(pricing_path, results_dir)`."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    pricing_path = tmp_path / "pricing.yaml"
    pricing_path.write_text(pricing, encoding="utf-8")
    results_dir = tmp_path / "results"
    for name, body in artifacts.items():
        path = results_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return pricing_path, results_dir


# ---------------------------------------------------------------------------
# 1. The gate is non-vacuous as installed.
# ---------------------------------------------------------------------------


class TestTheVerifiedNothingDisposition:
    """An empty scan exits 3 (verified nothing), never the pass message."""

    def test_an_empty_scan_exits_three_rather_than_zero(self, tmp_path: Path) -> None:
        pricing_path, results_dir = _tree(tmp_path, pricing=PROVISIONAL_PRICING, artifacts={})
        assert (
            gate.main(["--pricing-file", str(pricing_path), "--results-dir", str(results_dir)])
            == gate.EXIT_VERIFIED_NOTHING
        )

    def test_an_absent_results_dir_also_exits_three(self, tmp_path: Path) -> None:
        """An absent results directory exits 3 like an empty one."""
        pricing_path, _ = _tree(tmp_path, pricing=PROVISIONAL_PRICING, artifacts={})
        assert (
            gate.main(
                ["--pricing-file", str(pricing_path), "--results-dir", str(tmp_path / "nope")]
            )
            == gate.EXIT_VERIFIED_NOTHING
        )

    def test_a_violation_outranks_an_empty_scan(self, tmp_path: Path) -> None:
        """A violation exits 1 even when the rest of the scan is empty."""
        pricing_path, results_dir = _tree(
            tmp_path, pricing=PROVISIONAL_PRICING, artifacts={"matrix.md": "cost: $47.00\n"}
        )
        assert (
            gate.main(["--pricing-file", str(pricing_path), "--results-dir", str(results_dir)]) == 1
        )

    def test_supplying_any_consumer_makes_the_run_evidence_again(self, tmp_path: Path) -> None:
        """Supplying any consumer turns an otherwise empty scan into exit 0."""
        pricing_path, results_dir = _tree(tmp_path, pricing=PROVISIONAL_PRICING, artifacts={})
        readme = tmp_path / "README.md"
        readme.write_text("no figures here\n", encoding="utf-8")
        assert (
            gate.main(
                [
                    "--pricing-file",
                    str(pricing_path),
                    "--results-dir",
                    str(results_dir),
                    "--readme-file",
                    str(readme),
                ]
            )
            == 0
        )

    def test_a_settled_price_table_is_zero_not_three(self, tmp_path: Path) -> None:
        """A settled (`provisional: false`) price table exits 0, not 3."""
        pricing_path, results_dir = _tree(tmp_path, pricing=SETTLED_PRICING, artifacts={})
        assert (
            gate.main(["--pricing-file", str(pricing_path), "--results-dir", str(results_dir)]) == 0
        )

    def test_the_coverage_figure_describes_the_scan_that_produced_the_verdict(
        self, tmp_path: Path
    ) -> None:
        """`run_gate` returns exactly the violations of the `run_gate_with_coverage` scan."""
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"matrix.md": "clean\n", "raw/one.md": "clean\n"},
        )
        result = gate.run_gate_with_coverage(pricing_path=pricing_path, results_dir=results_dir)
        assert result.artifacts_scanned == 2
        assert result.consumers_scanned == ("published_artifacts",)
        assert not result.verified_nothing
        assert list(result.violations) == gate.run_gate(
            pricing_path=pricing_path, results_dir=results_dir
        )


class TestTheWrapperFailsOnAnAbsentTrackedConsumer:
    """The shell wrapper exits 2 when a named README or frontend consumer is absent."""

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return proc_run.run(
            ["bash", str(GATE_SH), *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_an_absent_readme_is_an_error_not_a_dropped_consumer(self, tmp_path: Path) -> None:
        result = self._run("--readme-file", str(tmp_path / "gone.md"))
        assert result.returncode == 2, result.stdout + result.stderr
        assert "readme file not found" in result.stderr

    def test_an_absent_frontend_dir_is_an_error_not_a_dropped_consumer(
        self, tmp_path: Path
    ) -> None:
        result = self._run("--frontend-dir", str(tmp_path / "gone"))
        assert result.returncode == 2, result.stdout + result.stderr
        assert "frontend dir not found" in result.stderr

    def test_the_installed_invocation_still_passes_with_both_consumers(
        self, tmp_path: Path
    ) -> None:
        """With both consumers present the wrapper passes and names the consumers it scanned."""
        pricing_path = tmp_path / "pricing.yaml"
        pricing_path.write_text(PROVISIONAL_PRICING, encoding="utf-8")
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        frontend_dir = tmp_path / "frontend"
        frontend_dir.mkdir()
        readme_path = tmp_path / "README.md"
        readme_path.write_text("# clean\n\nNo cost figures.\n", encoding="utf-8")
        result = self._run(
            "--pricing-file",
            str(pricing_path),
            "--results-dir",
            str(results_dir),
            "--frontend-dir",
            str(frontend_dir),
            "--readme-file",
            str(readme_path),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "consumers=['published_artifacts', 'frontend']" in result.stdout


class TestGateIsNonVacuousAsInstalled:
    def test_a_planted_currency_figure_fails(self, tmp_path: Path) -> None:
        """A published currency figure is a violation."""
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"matrix.md": "| model | cost |\n| opus | $12.34 |\n"},
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert violations, "the gate reported no violation for a published `$12.34`"
        assert any("12.34" in v for v in violations)

    def test_a_clean_artifact_passes(self, tmp_path: Path) -> None:
        """An artifact with no currency figure passes."""
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"matrix.md": "| model | cost |\n| opus | withheld |\n"},
        )
        assert gate.run_gate(pricing_path=pricing_path, results_dir=results_dir) == []

    def test_the_real_published_artifacts_pass(self) -> None:
        violations = gate.run_gate(
            pricing_path=PRICING, results_dir=RESULTS, readme_path=REPO_ROOT / "README.md"
        )
        assert violations == [], violations

    def test_the_shell_entry_point_exits_zero_on_the_real_tree(self) -> None:
        """The shell wrapper exits 0 on the real tree."""
        result = proc_run.run(
            ["bash", str(GATE_SH)], cwd=REPO_ROOT, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_shell_entry_point_fails_on_a_planted_figure(self, tmp_path: Path) -> None:
        """The shell wrapper exits non-zero on a planted currency figure."""
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"matrix.md": "total spend: $99.99\n"},
        )
        result = proc_run.run(
            [
                "bash",
                str(GATE_SH),
                "--pricing-file",
                str(pricing_path),
                "--results-dir",
                str(results_dir),
                "--readme-file",
                str(tmp_path / "absent-README.md"),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# 2. The interlock every withholding rule depends on.
# ---------------------------------------------------------------------------


class TestProvisionalInterlock:
    def test_pricing_yaml_is_no_longer_provisional_per_d3(self) -> None:
        """`config/pricing.yaml` is not provisional."""
        table = gate.load_price_table(PRICING)
        assert gate.is_provisional(table) is False, (
            "config/pricing.yaml is provisional again; published cost figures must be withheld and "
            "the clearance to publish cost re-examined."
        )

    def test_the_real_rates_are_populated_not_zero_placeholders(self) -> None:
        """`config/pricing.yaml` carries non-zero rates."""
        assert gate.all_rates_are_zero(gate.load_price_table(PRICING)) is False

    def test_the_gate_no_ops_once_pricing_is_settled(self, tmp_path: Path) -> None:
        """The same artifact fails under a provisional table and passes under a settled one."""
        artifacts = {"matrix.md": "| opus | $12.34 |\n"}
        provisional_path, results_dir = _tree(
            tmp_path / "prov", pricing=PROVISIONAL_PRICING, artifacts=artifacts
        )
        settled_path, settled_results = _tree(
            tmp_path / "settled", pricing=SETTLED_PRICING, artifacts=artifacts
        )
        assert gate.run_gate(pricing_path=provisional_path, results_dir=results_dir)
        assert gate.run_gate(pricing_path=settled_path, results_dir=settled_results) == []


# ---------------------------------------------------------------------------
# 3. Verdict, winner, ranking — the gap in the gate, covered here.
# ---------------------------------------------------------------------------

#: Word-bounded language that asserts a comparison between the two model families.
VERDICT_PATTERN = re.compile(
    r"\b(?:verdict|winner|wins|won|loser|outperform\w*|beats?|superior|inferior|"
    r"runner-up|best|worst|better|worse|recommend\w*|prefer\w*|ranked|ranking|"
    r"first place|second place)\b",
    re.IGNORECASE,
)

#: Published prose artefacts exempt from the verdict scan.
_PROSE_EXEMPT_NAMES = frozenset({"README.md"})


class TestVerdictAbsence:
    """No published artefact declares a verdict, winner, or ranking (the gate checks currency)."""

    def test_the_gate_itself_checks_no_verdict_language(self) -> None:
        """The gate source mentions no verdict, winner, or ranking language."""
        source = GATE_PY.read_text(encoding="utf-8").lower()
        for term in ("verdict", "winner", "ranking"):
            assert term not in source, (
                f"the gate now mentions {term!r}. If it gained a verdict check, move "
                "TestVerdictAbsence's coverage into the gate and drop it from here."
            )

    def test_no_data_artifact_declares_a_verdict(self) -> None:
        """No `matrix.{md,csv,json}` or `raw/**` artefact contains verdict language."""
        offenders: list[str] = []
        for artifact in gate.iter_published_artifacts(RESULTS, readme_path=None):
            if artifact.name in _PROSE_EXEMPT_NAMES:
                continue
            if not gate.has_enumerable_schema(artifact, RESULTS) and artifact.name != "matrix.md":
                continue
            for match in VERDICT_PATTERN.finditer(artifact.read_text(encoding="utf-8")):
                offenders.append(f"{artifact.relative_to(REPO_ROOT)}: {match.group()!r}")
        assert offenders == [], (
            "a published artefact declares a comparison between model families, which this project "
            "forbids outright — this is a measurement instrument:\n" + "\n".join(offenders)
        )

    def test_the_verdict_pattern_is_not_vacuous(self) -> None:
        """The verdict pattern matches sample comparison claims."""
        for sample in [
            "Anthropic wins on correctness",
            "the winner is clear",
            "opus outperforms gpt",
            "we recommend the Anthropic family",
            "ranked by mean correctness",
            "best overall model",
        ]:
            assert VERDICT_PATTERN.search(sample), f"pattern missed {sample!r}"

    def test_the_verdict_pattern_does_not_flag_neutral_measurement_prose(self) -> None:
        """The verdict pattern does not match neutral measurement prose."""
        for sample in [
            "mean_correctness: 0.7500",
            "20 tasks x 1 repetition = 40 agent runs",
            "cost withheld — price table is provisional",
            "stub-mode measurements, not a validated comparison",
            "anthropic.claude-opus-5-5",
            "unscored (prose-only)",
        ]:
            assert not VERDICT_PATTERN.search(sample), f"pattern falsely flagged {sample!r}"

    @pytest.mark.parametrize(
        "artifact_name", ["matrix.md", "matrix.csv", "matrix.json", "raw/run-1.json"]
    )
    def test_a_planted_verdict_is_caught_in_every_artifact_shape(
        self, tmp_path: Path, artifact_name: str
    ) -> None:
        """A planted verdict is reachable by the scan in every artifact shape."""
        _, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={artifact_name: "the winner is anthropic\n"},
        )
        found = [
            artifact
            for artifact in gate.iter_published_artifacts(results_dir, readme_path=None)
            if VERDICT_PATTERN.search(artifact.read_text(encoding="utf-8"))
        ]
        assert found, f"a planted verdict in {artifact_name} was not reachable by the scan"


# ---------------------------------------------------------------------------
# The browser surface.
# ---------------------------------------------------------------------------

EVALS_DIR = REPO_ROOT / "frontend" / "src" / "pages" / "evals"


def test_the_gate_scans_the_browser_surface() -> None:
    """The gate reaches the rendered dashboard, and the vitest suite still exists alongside it."""
    source = GATE_PY.read_text(encoding="utf-8")
    assert "find_currency_rendering_in_frontend" in source
    assert "--frontend-dir" in GATE_SH.read_text(encoding="utf-8")
    assert (EVALS_DIR / "evals.integrity.test.tsx").is_file()


def test_the_shell_entry_point_passes_the_frontend_dir_by_default() -> None:
    """The shell wrapper passes `--frontend-dir` by default, as read off a `bash -x` trace."""
    traced = proc_run.run(
        ["bash", "-x", str(GATE_SH)], capture_output=True, text=True, cwd=REPO_ROOT, check=False
    )
    assert traced.returncode == 0, traced.stderr
    assert "--frontend-dir" in traced.stderr, (
        "the shell entry point did not pass --frontend-dir, so `run_gates.py` would invoke this "
        "gate without its frontend consumer:\n" + traced.stderr[-2000:]
    )
    assert str(EVALS_DIR) in traced.stderr


def _run_wrapper_on_frontend(
    frontend_dir: Path, pricing_path: Path
) -> subprocess.CompletedProcess[str]:
    """Run the shell wrapper over `frontend_dir` alone under `pricing_path`."""
    return proc_run.run(
        [
            "bash",
            str(GATE_SH),
            "--pricing-file",
            str(pricing_path),
            "--frontend-dir",
            str(frontend_dir),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


_PLANT_MARKER = "planted cost read"


def test_the_shell_entry_point_fails_on_a_planted_render(tmp_path: Path) -> None:
    """A `cost_total` read planted into a copy of the real page fails the shell wrapper on the plant.

    The real page may itself read cost fields behind its withholding branch, so the plant is judged
    against the unplanted copy: only the planted run reports the planted line.
    """
    original = (EVALS_DIR / "EvalArmResults.tsx").read_text(encoding="utf-8")

    # The plant goes after the first line that reads `cost_withheld_provisional`.
    lines = original.splitlines(keepends=True)
    anchor_indexes = [i for i, line in enumerate(lines) if "cost_withheld_provisional" in line]
    assert anchor_indexes, (
        "no line of EvalArmResults.tsx reads `cost_withheld_provisional`, so this plant has nothing "
        "to anchor on. Either the withholding branch is gone (in which case cost withholding is unguarded on "
        "the browser surface and that is the real finding) or it moved and this anchor needs "
        "re-deriving."
    )
    assert _PLANT_MARKER not in original
    insert_at = anchor_indexes[0] + 1
    planted_line = f"  const planted = modelMetrics.cost_total;  // {_PLANT_MARKER}\n"
    planted = "".join(lines[:insert_at] + [planted_line] + lines[insert_at:])

    pricing_path = tmp_path / "pricing.yaml"
    pricing_path.write_text(PROVISIONAL_PRICING, encoding="utf-8")
    unplanted_copy = tmp_path / "unplanted" / "evals"
    planted_copy = tmp_path / "planted" / "evals"
    for directory, text in ((unplanted_copy, original), (planted_copy, planted)):
        directory.mkdir(parents=True)
        (directory / "EvalArmResults.tsx").write_text(text, encoding="utf-8")

    baseline = _run_wrapper_on_frontend(unplanted_copy, pricing_path)
    assert baseline.returncode in (0, 1), baseline.stderr
    assert _PLANT_MARKER not in baseline.stderr

    completed = _run_wrapper_on_frontend(planted_copy, pricing_path)
    assert completed.returncode == 1, (
        "the historical `Cost7.5` defect did not fail the gate through its shell entry point, "
        "which is the only path `run_gates.py` uses"
    )
    planted_findings = [line for line in completed.stderr.splitlines() if _PLANT_MARKER in line]
    assert len(planted_findings) == 1, completed.stderr
    assert "reads currency field '.cost_total'" in planted_findings[0]


class TestFrontendConsumer:
    def test_a_currency_field_read_is_a_violation(self, tmp_path: Path) -> None:
        (tmp_path / "Page.tsx").write_text(
            "export const P = () => <dd>{metrics.cost_total}</dd>;\n", encoding="utf-8"
        )
        violations = gate.find_currency_rendering_in_frontend(tmp_path)
        assert len(violations) == 1
        assert "cost_total" in violations[0]

    def test_a_string_key_index_read_is_a_violation(self, tmp_path: Path) -> None:
        (tmp_path / "Page.tsx").write_text(
            'export const P = () => <dd>{metrics["cost_cache_total"]}</dd>;\n', encoding="utf-8"
        )
        assert gate.find_currency_rendering_in_frontend(tmp_path)

    def test_a_cost_excluding_cache_read_is_a_violation(self, tmp_path: Path) -> None:
        """A frontend read of `cost_excluding_cache` is a violation."""
        (tmp_path / "Page.tsx").write_text(
            "export const P = () => <dd>{metrics.cost_excluding_cache}</dd>;\n", encoding="utf-8"
        )
        violations = gate.find_currency_rendering_in_frontend(tmp_path)
        assert any("cost_excluding_cache" in v for v in violations)

    def test_a_hardcoded_currency_literal_is_a_violation(self, tmp_path: Path) -> None:
        (tmp_path / "Page.tsx").write_text(
            "export const P = () => <dd>$0.47 per run</dd>;\n", encoding="utf-8"
        )
        assert gate.find_currency_rendering_in_frontend(tmp_path)

    def test_a_type_declaration_of_the_field_is_not_a_violation(self, tmp_path: Path) -> None:
        """A type declaration of a currency field is not a violation."""
        (tmp_path / "types.ts").write_text(
            "export interface M {\n  readonly cost_withheld_provisional?: boolean;\n"
            "  readonly cost_total?: number;\n}\n",
            encoding="utf-8",
        )
        assert gate.find_currency_rendering_in_frontend(tmp_path) == []

    def test_reading_the_withheld_flag_is_not_a_violation(self, tmp_path: Path) -> None:
        (tmp_path / "Page.tsx").write_text(
            "export const P = () => (metrics.cost_withheld_provisional === true ? <dd>x</dd> "
            ": null);\n",
            encoding="utf-8",
        )
        assert gate.find_currency_rendering_in_frontend(tmp_path) == []

    def test_test_files_are_skipped(self, tmp_path: Path) -> None:
        """Test files are excluded from the frontend scan."""
        (tmp_path / "evals.integrity.test.tsx").write_text(
            'expect(page).not.toMatch("$0.47");\nconst arm = { cost_total: 7.5 };\n',
            encoding="utf-8",
        )
        assert gate.find_currency_rendering_in_frontend(tmp_path) == []

    def test_the_real_dashboard_renders_cost_now_that_the_flag_is_cleared(self) -> None:
        """The scanner finds the real dashboard's cost read and the shipped wrapper still passes."""
        findings = gate.find_currency_rendering_in_frontend(EVALS_DIR)
        assert any("cost_" in f for f in findings), (
            "the real dashboard reads no cost field, yet it is expected to render cost — either the scanner "
            "broke or the dashboard's cost rendering was removed"
        )
        result = proc_run.run(
            ["bash", str(GATE_SH)], cwd=REPO_ROOT, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_missing_directory_reports_nothing_rather_than_erroring(self, tmp_path: Path) -> None:
        assert gate.find_currency_rendering_in_frontend(tmp_path / "absent") == []


# ---------------------------------------------------------------------------
# Artifact scope.
# ---------------------------------------------------------------------------


def test_a_cost_excluding_cache_figure_in_matrix_md_fails_while_provisional(tmp_path: Path) -> None:
    """A `cost_excluding_cache` figure without `$` in matrix.md is a violation while provisional."""
    pricing_path, results_dir = _tree(
        tmp_path,
        pricing=PROVISIONAL_PRICING,
        artifacts={"matrix.md": "opus cost_excluding_cache: 12.34\n"},
    )
    violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
    assert any("cost_excluding_cache" in v or "12.34" in v for v in violations), violations


class TestArtifactScope:
    def test_an_artifact_outside_the_old_hand_listed_set_is_scanned(self, tmp_path: Path) -> None:
        """A prose currency figure in `verification-inventory.md` is a violation."""
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={
                "verification-inventory.md": (
                    "The sweep costs approximately $0.47 in model charges.\n"
                ),
            },
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert violations, "a prose currency figure outside the old hand-listed set was not caught"
        assert "verification-inventory.md" in violations[0]

    def test_a_nested_artifact_in_an_arbitrary_subdirectory_is_scanned(
        self, tmp_path: Path
    ) -> None:
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"some-evidence-dir/EVIDENCE.md": "Total spend was USD 12.00.\n"},
        )
        assert gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)

    def test_a_known_cost_key_is_caught_in_a_non_enumerable_json_artifact(
        self, tmp_path: Path
    ) -> None:
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"tf-outputs.json": '{"cost_total": 0.47}'},
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert violations
        assert "cost_total" in violations[0]

    def test_an_innocent_numeric_field_outside_an_enumerable_schema_is_not_a_violation(
        self, tmp_path: Path
    ) -> None:
        """An unrecognised numeric field in a non-enumerable JSON artifact is not a violation."""
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"e2e-browser.json": '{"config": {"maxFailures": 0, "globalTimeout": 300}}'},
        )
        assert gate.run_gate(pricing_path=pricing_path, results_dir=results_dir) == []

    def test_the_inverted_burden_still_applies_where_the_schema_is_enumerable(
        self, tmp_path: Path
    ) -> None:
        """An unrecognised numeric key in matrix.json is a violation."""
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"matrix.json": '{"arms": [], "run_expenditure_units": 0.47}'},
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert violations, "the renamed-key inverted burden stopped applying to matrix.json"

    def test_the_enumerable_schema_set_is_exactly_the_repo_generated_artifacts(self) -> None:
        """The enumerable-schema set is `matrix.json`, `matrix.csv`, and `raw/`."""
        assert gate._ENUMERABLE_SCHEMA_RELPATHS == ("matrix.json", "matrix.csv")
        assert gate._ENUMERABLE_SCHEMA_PARENT_DIRS == ("raw",)

    @pytest.mark.parametrize(
        ("relpath", "expected"),
        [
            ("matrix.json", True),
            ("matrix.csv", True),
            ("raw/anthropic.json", True),
            ("raw/nested/deeper.json", True),
            ("verification-inventory.json", False),
            ("tf-outputs.json", False),
            ("plan/post_apply_plan.json", False),
        ],
    )
    def test_schema_enumerability_is_decided_per_artifact(
        self, tmp_path: Path, relpath: str, expected: bool
    ) -> None:
        results_dir = tmp_path / "results"
        artifact = results_dir / relpath
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("{}", encoding="utf-8")
        assert gate.has_enumerable_schema(artifact, results_dir) is expected


class TestBinaryExclusions:
    def test_every_binary_exclusion_is_still_load_bearing(self, tmp_path: Path) -> None:
        """Removing any binary-suffix exclusion makes the gate report the matching file."""
        for suffix, reason in list(gate._BINARY_SUFFIX_EXCLUSIONS.items()):
            assert suffix.startswith("."), suffix
            assert reason.strip(), f"exclusion {suffix} carries no reason"
            pricing_path, results_dir = _tree(
                tmp_path / suffix.lstrip("."), pricing=PROVISIONAL_PRICING, artifacts={}
            )
            binary = results_dir / f"artifact{suffix}"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(b"\x00\x01\x02not-utf8\xff\xfe")
            assert gate.run_gate(pricing_path=pricing_path, results_dir=results_dir) == [], (
                f"{suffix} is excluded but the gate passes it anyway, so the exclusion does "
                "nothing and must be deleted"
            )
            gate._BINARY_SUFFIX_EXCLUSIONS.pop(suffix)
            try:
                without = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
            finally:
                gate._BINARY_SUFFIX_EXCLUSIONS[suffix] = reason
            assert without, (
                f"removing the {suffix} exclusion changes nothing, so it was never load-bearing"
            )

    def test_an_unreadable_artifact_with_no_exclusion_is_reported_not_skipped(
        self, tmp_path: Path
    ) -> None:
        """A non-UTF-8 artifact with no exclusion is reported as a violation."""
        pricing_path, results_dir = _tree(tmp_path, pricing=PROVISIONAL_PRICING, artifacts={})
        results_dir.mkdir(parents=True, exist_ok=True)
        (results_dir / "opaque.bin").write_bytes(b"\xff\xfe\x00binary")
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert violations
        assert "not valid UTF-8" in violations[0]


class TestExcludedResultsDirectories:
    """`results/plan/` is out of scope; every other published artifact is still scanned."""

    _FIGURE = "total cost was $12.34 for the sweep"

    def test_a_currency_figure_under_results_plan_is_out_of_scope(self, tmp_path: Path) -> None:
        """A figure under `results/plan/` is ignored while a sibling artifact is still scanned."""
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={
                "plan/drift_plan.show.json": f'{{"note":"{self._FIGURE}"}}',
                "matrix.md": "| arm | mean_correctness |\n| anthropic | 0.85 |\n",
            },
        )
        result = gate.run_gate_with_coverage(pricing_path=pricing_path, results_dir=results_dir)
        assert result.violations == ()
        assert result.artifacts_scanned == 1, "the sibling artifact must still be scanned"
        assert not result.verified_nothing

    def test_every_directory_exclusion_is_still_load_bearing(self, tmp_path: Path) -> None:
        """Removing any directory exclusion makes the gate report the planted file."""
        for index, (name, reason) in enumerate(list(gate._EXCLUDED_RESULTS_DIRS.items())):
            assert "/" not in name and name, name
            assert reason.strip(), f"exclusion {name} carries no reason"
            pricing_path, results_dir = _tree(
                tmp_path / f"dir{index}",
                pricing=PROVISIONAL_PRICING,
                artifacts={
                    f"{name}/planted.json": f'{{"note":"{self._FIGURE}"}}',
                    "matrix.md": "| arm | mean_correctness |\n| anthropic | 0.85 |\n",
                },
            )
            assert gate.run_gate(pricing_path=pricing_path, results_dir=results_dir) == [], (
                f"{name}/ is excluded but the gate reports it anyway, so the exclusion does "
                "nothing and must be deleted"
            )
            gate._EXCLUDED_RESULTS_DIRS.pop(name)
            try:
                without = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
            finally:
                gate._EXCLUDED_RESULTS_DIRS[name] = reason
            assert without, (
                f"removing the {name}/ exclusion changes nothing, so it was never load-bearing"
            )

    def test_an_all_excluded_tree_reports_verified_nothing_rather_than_passing(
        self, tmp_path: Path
    ) -> None:
        """A tree whose only artifacts are excluded exits 3 (verified nothing)."""
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"plan/only.show.json": f'{{"note":"{self._FIGURE}"}}'},
        )
        result = gate.run_gate_with_coverage(pricing_path=pricing_path, results_dir=results_dir)
        assert result.verified_nothing
        assert result.artifacts_scanned == 0
        assert (
            gate.main(["--pricing-file", str(pricing_path), "--results-dir", str(results_dir)])
            == gate.EXIT_VERIFIED_NOTHING
        )

    @pytest.mark.parametrize(
        ("relpath", "still_reported"),
        [
            # Excluded: the directory and anything nested under it.
            ("plan/drift_plan.show.json", False),
            ("plan/post_apply_plan.json", False),
            ("plan/nested/deeper.json", False),
            # NOT excluded: a FILE named like the directory, a sibling whose name merely starts the
            # same way, and every genuine published artifact.
            ("plan", True),
            ("plans/x.json", True),
            ("planning-notes.md", True),
            ("matrix.md", True),
            ("matrix.csv", True),
            ("verification-inventory.md", True),
            ("tf-outputs.json", True),
            ("raw/anthropic.json", True),
        ],
    )
    def test_the_exclusion_is_scoped_to_the_directory_and_nothing_that_merely_resembles_it(
        self, tmp_path: Path, relpath: str, still_reported: bool
    ) -> None:
        body = (
            f'{{"note":"{self._FIGURE}"}}'
            if relpath.endswith(".json")
            else f"note: {self._FIGURE}\n"
        )
        pricing_path, results_dir = _tree(
            tmp_path, pricing=PROVISIONAL_PRICING, artifacts={relpath: body}
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert bool(violations) is still_reported, (relpath, violations)

    @pytest.mark.parametrize(
        ("name", "body"),
        [
            ("matrix.md", "| arm | cost |\n| anthropic | $12.34 |\n"),
            ("matrix.csv", "arm,cost\nanthropic,$12.34\n"),
            ("matrix.json", '{"arms":[{"name":"anthropic","cost_total_usd":12.34}]}'),
            ("verification-inventory.md", "Residue clean. Spend was $12.34.\n"),
            ("raw/anthropic.json", '{"cost_total_usd":12.34}'),
        ],
    )
    def test_the_general_rule_is_untouched_for_genuine_published_artifacts(
        self, tmp_path: Path, name: str, body: str
    ) -> None:
        """A currency figure in each genuine published artifact is a violation."""
        pricing_path, results_dir = _tree(
            tmp_path, pricing=PROVISIONAL_PRICING, artifacts={name: body}
        )
        assert gate.run_gate(pricing_path=pricing_path, results_dir=results_dir), name

    def test_the_shell_entry_point_still_fails_on_a_planted_published_figure(
        self, tmp_path: Path
    ) -> None:
        """The CLI exits 1 on a published figure and does not report the excluded plan file."""
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={
                "plan/drift_plan.show.json": f'{{"note":"{self._FIGURE}"}}',
                "matrix.md": "| arm | cost |\n| anthropic | $12.34 |\n",
            },
        )
        result = proc_run.run(
            [
                sys.executable,
                str(GATE_PY),
                "--pricing-file",
                str(pricing_path),
                "--results-dir",
                str(results_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "matrix.md" in result.stderr
        assert "drift_plan.show.json" not in result.stderr


class TestTierSummaryFields:
    """The per-tier summary's non-currency figures pass while provisional; its cost does not."""

    def test_every_non_cost_tier_summary_csv_column_is_allowlisted(self) -> None:
        from eval import matrix

        columns = matrix._tier_summary_csv_columns()
        cost_columns = {c for c in columns if gate._COST_BEARING_NAME_PATTERN.search(c)}
        assert cost_columns == {f"tier_{t}_cost" for t in ("supervisor", "subagent", "tool")}
        assert set(columns) - cost_columns <= gate._NON_CURRENCY_NUMERIC_FIELDS

    def test_every_numeric_tier_summary_json_key_is_allowlisted_except_cost(self) -> None:
        from eval.metrics import RepeatCompletion, TierSuccess, TierSummaryEntry

        fields = TierSummaryEntry(
            tier="tool",
            input_tokens=1,
            output_tokens=1,
            cost_total=0.5,
            latency_p50_ms=1.0,
            latency_p95_ms=2.0,
            success=TierSuccess(passed=1, applicable=2),
            completion_over_repeats=RepeatCompletion(completed=1, applicable=1, repeats=3),
        ).to_report_fields()
        numeric = {
            key
            for key, value in {
                **fields,
                **fields["success"],  # type: ignore[dict-item]
                **fields["completion_over_repeats"],  # type: ignore[dict-item]
            }.items()
            if isinstance(value, int | float) and not isinstance(value, bool)
        }
        assert numeric - {"cost_total"} <= gate._NON_CURRENCY_NUMERIC_FIELDS
        assert gate._COST_BEARING_NAME_PATTERN.search("cost_total")

    def test_a_real_stub_sweeps_tier_summary_raises_no_violation_while_provisional(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import boto3
        from moto import mock_aws

        from crm.store.provisioning import create_all_tables
        from crm.store.store import Store
        from eval.harness import run_sweep

        def _noop_seeder(store: Store, *, tenant: str) -> None:
            del store, tenant

        monkeypatch.setattr("eval.tenancy._default_seeder", lambda: _noop_seeder)
        monkeypatch.setattr("eval.harness.is_cost_provisional", lambda: True)
        for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            monkeypatch.setenv(var, "testing")
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        results_dir = tmp_path / "results"
        with mock_aws():
            client = boto3.client("dynamodb", region_name="us-east-1")
            create_all_tables(client)
            run_sweep(store=Store(client=client), output_dir=results_dir)
        arms = __import__("json").loads((results_dir / "matrix.json").read_text())["arms"]
        assert all(arm["tier_summary"]["supervisor"]["input_tokens"] > 0 for arm in arms)
        pricing_path = tmp_path / "pricing.yaml"
        pricing_path.write_text(PROVISIONAL_PRICING, encoding="utf-8")
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert not [v for v in violations if "tier_summary" in v or "'tier_" in v]

    def test_a_tier_cost_figure_fails_while_provisional(self, tmp_path: Path) -> None:
        planted = '{"arms": [{"tier_summary": {"tool": {"cost_total": 0.42}}}]}'
        pricing_path, results_dir = _tree(
            tmp_path, pricing=PROVISIONAL_PRICING, artifacts={"matrix.json": planted}
        )
        assert gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)


#: The numeric keys a stub sweep publishes in its per-task spread, per-tool aggregation and per-tier
#: CSV latency columns, each classified non-currency.
_SWEEP_CLASSIFIED_KEYS: tuple[str, ...] = (
    "repeats",
    "correctness_min",
    "correctness_mean",
    "correctness_max",
    "success_fraction",
    "truncated_count",
    "data",
    "no_match",
    "denied",
    "tool_failure",
    "call_latency_ms_supervisor_count",
    "call_latency_ms_subagent_count",
    "call_latency_ms_tool_count",
)


@pytest.fixture(scope="class")
def provisional_stub_sweep(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """One real stub sweep plus a scratch copy of `config/pricing.yaml` forced provisional.

    Returns `(pricing_path, results_dir)`.
    """
    import boto3
    from moto import mock_aws

    from crm.store.provisioning import create_all_tables
    from crm.store.store import Store
    from eval.harness import run_sweep

    def _noop_seeder(store: Store, *, tenant: str) -> None:
        del store, tenant

    root = tmp_path_factory.mktemp("provisional_stub_sweep")
    pricing_path = root / "pricing.yaml"
    config_text = PRICING.read_text(encoding="utf-8")
    forced = re.sub(r"(?m)^provisional:\s*false\s*$", "provisional: true", config_text)
    pricing_path.write_text(forced, encoding="utf-8")
    assert gate.is_provisional(gate.load_price_table(pricing_path))
    results_dir = root / "results"
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("eval.tenancy._default_seeder", lambda: _noop_seeder)
        monkeypatch.setattr("eval.harness.is_cost_provisional", lambda: True)
        for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            monkeypatch.setenv(var, "testing")
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        with mock_aws():
            client = boto3.client("dynamodb", region_name="us-east-1")
            create_all_tables(client)
            run_sweep(store=Store(client=client), output_dir=results_dir)
    return pricing_path, results_dir


def _copy_results(source: Path, tmp_path: Path) -> Path:
    """Copy the sweep's results tree under `tmp_path` so a test can plant into it."""
    import shutil

    destination = tmp_path / "results"
    shutil.copytree(source, destination)
    return destination


class TestProvisionalStubSweep:
    """A real stub sweep's artifacts pass the gate under provisional pricing; a plant fails."""

    def test_the_sweep_publishes_every_classified_key_as_a_number(
        self, provisional_stub_sweep: tuple[Path, Path]
    ) -> None:
        import csv
        import json

        _, results_dir = provisional_stub_sweep
        numeric_json_keys: set[str] = set()

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if gate._is_published_currency_value(value):
                        numeric_json_keys.add(key)
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(json.loads((results_dir / "matrix.json").read_text(encoding="utf-8")))
        with (results_dir / "matrix.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        numeric_columns = set()
        for row in rows:
            for column, value in row.items():
                try:
                    float(value)
                except (TypeError, ValueError):
                    continue
                numeric_columns.add(column)
        assert set(_SWEEP_CLASSIFIED_KEYS) <= numeric_json_keys | numeric_columns

    def test_the_gate_passes_the_sweep_under_a_provisional_copy_of_the_config(
        self, provisional_stub_sweep: tuple[Path, Path]
    ) -> None:
        pricing_path, results_dir = provisional_stub_sweep
        assert gate.run_gate(pricing_path=pricing_path, results_dir=results_dir) == []
        result = proc_run.run(
            [
                sys.executable,
                str(GATE_PY),
                "--pricing-file",
                str(pricing_path),
                "--results-dir",
                str(results_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "no absolute currency figures published while provisional" in result.stdout

    @pytest.mark.parametrize("key", _SWEEP_CLASSIFIED_KEYS)
    def test_each_classification_is_load_bearing(
        self, provisional_stub_sweep: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, key: str
    ) -> None:
        pricing_path, results_dir = provisional_stub_sweep
        monkeypatch.setattr(
            gate, "_NON_CURRENCY_NUMERIC_FIELDS", gate._NON_CURRENCY_NUMERIC_FIELDS - {key}
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert violations
        assert all(key in violation for violation in violations), violations

    @pytest.mark.parametrize(
        ("container", "planted_key"),
        [
            ("task_spread", "cost_total"),
            ("task_spread", "settlement"),
            ("tool_metrics", "spend"),
        ],
    )
    def test_a_currency_value_planted_in_the_sweeps_matrix_json_fails(
        self,
        provisional_stub_sweep: tuple[Path, Path],
        tmp_path: Path,
        container: str,
        planted_key: str,
    ) -> None:
        import json

        pricing_path, source = provisional_stub_sweep
        results_dir = _copy_results(source, tmp_path)
        matrix_path = results_dir / "matrix.json"
        document = json.loads(matrix_path.read_text(encoding="utf-8"))
        document["arms"][0][container][0][planted_key] = 0.47
        matrix_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert [v for v in violations if planted_key in v and "0.47" in v], violations

    def test_a_currency_column_planted_in_the_sweeps_matrix_csv_fails(
        self, provisional_stub_sweep: tuple[Path, Path], tmp_path: Path
    ) -> None:
        pricing_path, source = provisional_stub_sweep
        results_dir = _copy_results(source, tmp_path)
        csv_path = results_dir / "matrix.csv"
        header, *rows = csv_path.read_text(encoding="utf-8").splitlines()
        planted = [f"{header},tool_charge"] + [f"{row},0.47" for row in rows]
        csv_path.write_text("\n".join(planted) + "\n", encoding="utf-8")
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert len([v for v in violations if "'tool_charge'" in v]) == len(rows), violations

    def test_every_latency_field_unmeasured_in_a_stub_is_classified(self) -> None:
        """Latency figures a stub sweep leaves null are numeric live, so each is classified."""
        from eval import matrix
        from eval.metrics import TierRoleModelMetrics, ToolAggregate, UsageTotals

        tool = ToolAggregate(
            tool_name="search_accounts",
            role="supervisor",
            count=2,
            call_latency_ms_p50=12.0,
            call_latency_ms_p95=30.0,
            truncated_count=1,
            outcome_distribution={"data": 1, "no_match": 1, "denied": 0, "tool_failure": 0},
        ).to_report_fields()
        tier_role_model = TierRoleModelMetrics(
            tier="tool",
            role="supervisor",
            model_id="anthropic.claude-opus-5-5",
            included_invocation_count=2,
            usage=UsageTotals(
                input_tokens=1, output_tokens=1, cache_write_tokens=0, cache_read_tokens=0
            ),
            cost=None,
            call_latency_measured=True,
            call_latency_ms_p50=12.0,
            call_latency_ms_p95=30.0,
        ).to_report_fields()
        distribution = tool["outcome_distribution"]
        assert isinstance(distribution, dict)
        numeric = {
            key
            for key, value in {**tool, **distribution, **tier_role_model}.items()
            if gate._is_published_currency_value(value)
        }
        csv_columns = set(matrix._latency_csv_columns()) | set(matrix._TASK_SPREAD_CSV_FIELDS)
        assert (numeric | csv_columns) - gate._NON_CURRENCY_NUMERIC_FIELDS == set()


# ---------------------------------------------------------------------------
# Non-currency exemptions apply only outside a cost-bearing ancestor.
# ---------------------------------------------------------------------------

#: Figures the generic non-currency leaf names (`rate`, `data`, `passed`, `p50`/`p95`/`count`)
#: once exempted, because only the leaf key was checked.
_PLANTED_UNDER_COST_ANCESTOR: tuple[tuple[str, str], ...] = (
    ("price_table", '{"price_table": {"anthropic": {"rate": 3.0}}}'),
    ("cost_by_outcome", '{"cost_by_outcome": {"data": 0.12}}'),
    ("usd", '{"usd": {"passed": 4.2}}'),
    ("spend_by_tier", '{"spend_by_tier": {"tool": {"p50": 1.5, "p95": 2.5, "count": 3}}}'),
    ("cost_samples", '{"cost_samples": {"tool": {"p50": [0.1, 0.2]}}}'),
    ("billing", '{"billing": [{"rate": 0.4}]}'),
)


class TestCostBearingAncestors:
    @pytest.mark.parametrize("artifact_name", ["matrix.json", "raw/run.json", "notes.json"])
    @pytest.mark.parametrize(
        ("ancestor", "body"),
        _PLANTED_UNDER_COST_ANCESTOR,
        ids=[a for a, _ in _PLANTED_UNDER_COST_ANCESTOR],
    )
    def test_an_allowlisted_leaf_under_a_cost_bearing_key_fails_while_provisional(
        self, tmp_path: Path, artifact_name: str, ancestor: str, body: str
    ) -> None:
        pricing_path, results_dir = _tree(
            tmp_path, pricing=PROVISIONAL_PRICING, artifacts={artifact_name: body}
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert violations, f"a figure under {ancestor!r} was exempted by its leaf name"
        assert any(ancestor in violation for violation in violations)

    @pytest.mark.parametrize(
        "body",
        [
            '{"tier_summary": {"tool": {"passed": 4, "applicable": 5, "rate": 0.8}}}',
            '{"outcome_distribution": {"data": 3, "denied": 1}}',
            '{"call_latency_ms_by_tier": {"tool": {"p50": 12, "p95": 30, "count": 4}}}',
        ],
        ids=["tier-summary", "outcome-distribution", "latency-by-tier"],
    )
    def test_the_same_leaves_outside_a_cost_bearing_key_still_pass(
        self, tmp_path: Path, body: str
    ) -> None:
        """Control: the ancestor rule narrows the exemption, it does not remove it."""
        pricing_path, results_dir = _tree(
            tmp_path, pricing=PROVISIONAL_PRICING, artifacts={"matrix.json": body}
        )
        assert gate.run_gate(pricing_path=pricing_path, results_dir=results_dir) == []

    def test_the_ancestor_rule_does_not_apply_once_pricing_is_settled(self, tmp_path: Path) -> None:
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=SETTLED_PRICING,
            artifacts={"matrix.json": _PLANTED_UNDER_COST_ANCESTOR[0][1]},
        )
        assert gate.run_gate(pricing_path=pricing_path, results_dir=results_dir) == []

    def test_the_violation_names_the_full_path(self) -> None:
        violations = gate.find_currency_fields_in_json(
            '{"price_table": {"anthropic": {"rate": 3.0}}}', source="matrix.json"
        )
        assert violations == [
            "matrix.json: numeric field $.price_table.anthropic.rate (3.0) sits under "
            "cost-bearing key 'price_table' while the price table is provisional"
        ]


# ---------------------------------------------------------------------------
# Tier comparison summary.
# ---------------------------------------------------------------------------

_SUMMARY_TIERS: tuple[str, ...] = ("supervisor", "subagent", "tool")
_SUMMARY_CSV_SUFFIXES: tuple[str, ...] = (
    "model_ids",
    "efforts",
    "completion_passed",
    "completion_applicable",
    "completion_rate",
    "accuracy",
    "cost_per_completed_task",
    "cost_per_task",
    "completed_task_latency_ms_p50",
    "completed_task_latency_ms_p95",
    "latency_sample_count",
    "invocation_count",
    "input_tokens",
    "output_tokens",
)
_DELEGATION_KINDS: tuple[str, ...] = ("data", "no_match", "denied", "tool_failure", "none")


def _summary_csv_columns() -> list[str]:
    """The `summary_*` columns, in order, written out independently of the renderer."""
    return [
        "summary_column",
        *(
            f"summary_{tier}_{suffix}"
            for tier in _SUMMARY_TIERS
            for suffix in _SUMMARY_CSV_SUFFIXES
        ),
        "summary_supervisor_budget_exhausted_task_count",
        *(f"summary_subagent_delegation_{kind}" for kind in _DELEGATION_KINDS),
        "summary_tool_truncated_task_count",
        *(f"summary_{tier}_retry_count" for tier in _SUMMARY_TIERS),
    ]


def _summary_entry(tier: str, *, cost: float | None) -> dict[str, object]:
    entry: dict[str, object] = {
        "model_ids": ["anthropic.claude-opus-5-5"],
        "efforts": ["medium"],
        "completion": {"passed": 3, "applicable": 5, "rate": 0.6},
        "accuracy": 0.6,
        "cost_withheld_provisional": cost is None,
        "cost_per_completed_task": cost,
        "cost_per_task": cost,
        "completed_task_latency_ms_p50": 1200.0,
        "completed_task_latency_ms_p95": 2500.0,
        "latency_sample_count": 3,
        "invocation_count": 9,
        "input_tokens": 1000,
        "output_tokens": 200,
        "retry_count": 1,
    }
    if tier == "supervisor":
        entry["budget_exhausted_task_count"] = 1
    elif tier == "subagent":
        entry["delegation_outcomes"] = {
            "data": 2,
            "no_match": 1,
            "denied": 1,
            "tool_failure": 0,
            "none": 1,
        }
    else:
        entry["truncated_task_count"] = 2
    return entry


def _summary_matrix_json(*, cost: float | None = None) -> dict[str, object]:
    """A `matrix.json` carrying the tier comparison summary and the arm block, cost withheld by default."""
    arm_block = {
        "family": "anthropic",
        "by_tier": {
            tier: {
                "configured_model_id": "anthropic.claude-opus-5-5",
                "efforts": ["medium"],
                "latency_sample_count": 3,
                "completed_task_latency_ms_p50": 1200.0,
                "completed_task_latency_ms_p95": 2500.0,
            }
            for tier in _SUMMARY_TIERS
        },
        "delegation_outcomes": {
            "data": 2,
            "no_match": 1,
            "denied": 1,
            "tool_failure": 0,
            "none": 1,
        },
    }
    return {
        "tier_comparison": {
            "columns": [
                {
                    "label": "anthropic",
                    "family": "anthropic",
                    "model_id": "anthropic.claude-opus-5-5",
                    "mode": "live",
                    "effort": "medium",
                    "task_count": 5,
                }
            ],
            "tiers": {tier: [_summary_entry(tier, cost=cost)] for tier in _SUMMARY_TIERS},
            "notes": ["Columns are arms in run order."],
        },
        "arms": [{"tier_task_metrics": arm_block}],
        "figure_semantics": {},
    }


def _summary_csv_value(column: str) -> str:
    if column == "summary_column":
        return "anthropic"
    if column.endswith("_model_ids"):
        return "anthropic.claude-opus-5-5"
    if column.endswith("_efforts"):
        return "medium"
    if "_cost_" in column:
        return ""
    return "0.6" if column.endswith(("_rate", "_accuracy")) else "3"


def _summary_matrix_csv(overrides: dict[str, str] | None = None) -> str:
    import csv
    import io

    columns = _summary_csv_columns()
    row = {column: _summary_csv_value(column) for column in columns}
    row.update(overrides or {})
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["task_id", *columns], lineterminator="\n")
    writer.writeheader()
    writer.writerow({"task_id": "t-01", **row})
    writer.writerow({"task_id": "t-02", **row})
    return buffer.getvalue()


class TestTierComparisonSummaryFields:
    """The summary's non-currency figures pass while provisional; its cost keys still fail."""

    def test_a_withheld_summary_raises_no_violation_while_provisional(self, tmp_path: Path) -> None:
        import json

        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={
                "matrix.json": json.dumps(_summary_matrix_json(), indent=2),
                "matrix.csv": _summary_matrix_csv(),
                "raw/run.json": json.dumps(_summary_matrix_json()["arms"]),
            },
        )
        assert gate.run_gate(pricing_path=pricing_path, results_dir=results_dir) == []

    @pytest.mark.parametrize("cost_key", ["cost_per_completed_task", "cost_per_task"])
    def test_a_numeric_summary_cost_key_is_one_violation(
        self, tmp_path: Path, cost_key: str
    ) -> None:
        import json

        document = _summary_matrix_json()
        tiers = document["tier_comparison"]["tiers"]  # type: ignore[index]
        tiers["supervisor"][0][cost_key] = 0.12
        pricing_path, results_dir = _tree(
            tmp_path, pricing=PROVISIONAL_PRICING, artifacts={"matrix.json": json.dumps(document)}
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert len(violations) == 1, violations
        assert cost_key in violations[0] and "0.12" in violations[0]

    def test_a_non_empty_summary_cost_column_is_one_violation_per_row(self, tmp_path: Path) -> None:
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={
                "matrix.csv": _summary_matrix_csv({"summary_supervisor_cost_per_task": "0.03"})
            },
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert len(violations) == 2, violations
        assert all("summary_supervisor_cost_per_task" in v for v in violations)

    def test_an_unknown_numeric_key_beside_the_summary_is_still_reported(
        self, tmp_path: Path
    ) -> None:
        import json

        document = _summary_matrix_json()
        tiers = document["tier_comparison"]["tiers"]  # type: ignore[index]
        tiers["tool"][0]["settlement_units"] = 4
        pricing_path, results_dir = _tree(
            tmp_path, pricing=PROVISIONAL_PRICING, artifacts={"matrix.json": json.dumps(document)}
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert len(violations) == 1, violations
        assert "settlement_units" in violations[0]
        assert "not a known non-currency field" in violations[0]

    def test_no_summary_cost_key_or_column_is_allowlisted(self) -> None:
        cost_columns = [c for c in _summary_csv_columns() if "_cost_" in c]
        assert len(cost_columns) == 2 * len(_SUMMARY_TIERS)
        for name in ("cost_per_completed_task", "cost_per_task", *cost_columns):
            assert name not in gate._NON_CURRENCY_NUMERIC_FIELDS
            assert gate._COST_BEARING_NAME_PATTERN.search(name)

    def test_every_numeric_summary_csv_column_is_allowlisted(self) -> None:
        string_suffixes = ("_model_ids", "_efforts")
        numeric = {
            c
            for c in _summary_csv_columns()
            if c != "summary_column" and "_cost_" not in c and not c.endswith(string_suffixes)
        }
        assert numeric - gate._NON_CURRENCY_NUMERIC_FIELDS == set()

    @pytest.mark.parametrize(
        "key",
        [
            "accuracy",
            "completed_task_latency_ms_p50",
            "completed_task_latency_ms_p95",
            "latency_sample_count",
            "invocation_count",
            "task_count",
            "budget_exhausted_task_count",
            "truncated_task_count",
            "none",
            "retry_count",
        ],
    )
    def test_each_new_json_key_is_load_bearing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
    ) -> None:
        import json

        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"matrix.json": json.dumps(_summary_matrix_json())},
        )
        monkeypatch.setattr(
            gate, "_NON_CURRENCY_NUMERIC_FIELDS", gate._NON_CURRENCY_NUMERIC_FIELDS - {key}
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert violations
        assert all(f".{key} (" in violation for violation in violations), violations

    @pytest.mark.parametrize(
        "column", ["summary_subagent_delegation_none", "summary_supervisor_retry_count"]
    )
    def test_each_new_csv_column_is_load_bearing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column: str
    ) -> None:
        pricing_path, results_dir = _tree(
            tmp_path, pricing=PROVISIONAL_PRICING, artifacts={"matrix.csv": _summary_matrix_csv()}
        )
        monkeypatch.setattr(
            gate, "_NON_CURRENCY_NUMERIC_FIELDS", gate._NON_CURRENCY_NUMERIC_FIELDS - {column}
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert len(violations) == 2, violations
        assert all(repr(column) in violation for violation in violations)


# ---------------------------------------------------------------------------
# Long-context invocation counts.
# ---------------------------------------------------------------------------

_LONG_CONTEXT_COUNT_KEYS: tuple[str, ...] = (
    "long_context_invocation_count",
    *(
        f"{prefix}_{tier}_long_context_invocation_count"
        for prefix in ("tier", "summary")
        for tier in _SUMMARY_TIERS
    ),
)


class TestLongContextInvocationCount:
    """The long-context call count is a non-currency count wherever a matrix artifact carries it."""

    @staticmethod
    def _matrix_json() -> str:
        import json

        return json.dumps(
            {
                "arms": [
                    {
                        "model_metrics": {"long_context_invocation_count": 1},
                        "tier_summary": {"tool": {"long_context_invocation_count": 2}},
                        "tier_role_model": [{"long_context_invocation_count": 3}],
                    }
                ]
            }
        )

    @staticmethod
    def _matrix_csv() -> str:
        columns = [
            key for key in _LONG_CONTEXT_COUNT_KEYS if key != "long_context_invocation_count"
        ]
        return (
            ",".join(["model_id", *columns])
            + "\n"
            + ",".join(["m", *("4" for _ in columns)])
            + "\n"
        )

    def test_the_count_passes_in_json_and_csv_while_provisional(self, tmp_path: Path) -> None:
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"matrix.json": self._matrix_json(), "matrix.csv": self._matrix_csv()},
        )
        assert gate.run_gate(pricing_path=pricing_path, results_dir=results_dir) == []

    def test_the_json_key_is_load_bearing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = "long_context_invocation_count"
        pricing_path, results_dir = _tree(
            tmp_path, pricing=PROVISIONAL_PRICING, artifacts={"matrix.json": self._matrix_json()}
        )
        monkeypatch.setattr(
            gate, "_NON_CURRENCY_NUMERIC_FIELDS", gate._NON_CURRENCY_NUMERIC_FIELDS - {key}
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert len(violations) == 3, violations
        assert all(f".{key} (" in violation for violation in violations), violations

    @pytest.mark.parametrize(
        "column", [k for k in _LONG_CONTEXT_COUNT_KEYS if k != "long_context_invocation_count"]
    )
    def test_each_csv_column_is_load_bearing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column: str
    ) -> None:
        pricing_path, results_dir = _tree(
            tmp_path, pricing=PROVISIONAL_PRICING, artifacts={"matrix.csv": self._matrix_csv()}
        )
        monkeypatch.setattr(
            gate, "_NON_CURRENCY_NUMERIC_FIELDS", gate._NON_CURRENCY_NUMERIC_FIELDS - {column}
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert len(violations) == 1, violations
        assert repr(column) in violations[0]

    def test_the_count_under_a_cost_bearing_key_still_fails(self, tmp_path: Path) -> None:
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"matrix.json": '{"cost_lines": [{"long_context_invocation_count": 2}]}'},
        )
        assert gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)


class TestArmTotals:
    """The all-tier arm count is a non-currency count; the all-tier arm cost is currency."""

    _KEY = "arm_long_context_invocation_count"

    @staticmethod
    def _matrix_json(arm_cost_total: float | None) -> str:
        import json

        return json.dumps(
            {
                "arms": [
                    {
                        "model_metrics": {"cost_withheld_provisional": True},
                        "arm_cost_total": arm_cost_total,
                        "arm_long_context_invocation_count": 2,
                    }
                ]
            }
        )

    _MATRIX_CSV = "model_id,arm_cost,arm_long_context_invocation_count\nm,,2\n"

    def test_the_arm_count_and_a_null_arm_cost_pass_while_provisional(self, tmp_path: Path) -> None:
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"matrix.json": self._matrix_json(None), "matrix.csv": self._MATRIX_CSV},
        )
        assert gate.run_gate(pricing_path=pricing_path, results_dir=results_dir) == []

    def test_the_arm_count_exemption_is_load_bearing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"matrix.json": self._matrix_json(None), "matrix.csv": self._MATRIX_CSV},
        )
        monkeypatch.setattr(
            gate, "_NON_CURRENCY_NUMERIC_FIELDS", gate._NON_CURRENCY_NUMERIC_FIELDS - {self._KEY}
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert len(violations) == 2, violations
        assert all(self._KEY in violation for violation in violations), violations

    def test_a_published_arm_cost_total_is_refused_while_provisional(self, tmp_path: Path) -> None:
        pricing_path, results_dir = _tree(
            tmp_path,
            pricing=PROVISIONAL_PRICING,
            artifacts={"matrix.json": self._matrix_json(0.25)},
        )
        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)
        assert violations and all("arm_cost_total" in v for v in violations), violations
