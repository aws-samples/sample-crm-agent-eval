# SPDX-License-Identifier: MIT
"""Tests for scripts/check_cost_withheld.{sh,py}.

A violating fixture is constructed for each check and shown to fail the gate, including ``cost_withheld
= False`` while the price table is provisional and all-zero.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts import proc_run

_REPO_ROOT_FOR_IMPORT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT_FOR_IMPORT / "scripts" / "check_cost_withheld.py"
_SPEC = importlib.util.spec_from_file_location("check_cost_withheld", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
sys.modules["check_cost_withheld"] = gate
_SPEC.loader.exec_module(gate)

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROVISIONAL_ALL_ZERO_TABLE: dict[str, object] = {
    "schema_version": 1,
    "price_table_version": "0.1.0-provisional",
    "provisional": True,
    "currency": "USD",
    "models": {
        "anthropic.claude-opus-5-5": {
            "input": 0.0,
            "output": 0.0,
            "cache_write": 0.0,
            "cache_read": 0.0,
        },
        "openai.gpt-6-astra": {
            "input": 0.0,
            "output": 0.0,
            "cache_write": 0.0,
            "cache_read": 0.0,
        },
    },
}

_FINAL_NONZERO_TABLE: dict[str, object] = {
    "schema_version": 1,
    "price_table_version": "1.0.0",
    "provisional": False,
    "currency": "USD",
    "models": {
        "anthropic.claude-opus-5-5": {
            "input": 15.0,
            "output": 75.0,
            "cache_write": 18.75,
            "cache_read": 1.5,
        },
    },
}


# ---------------------------------------------------------------------------
# A provisional copy of the committed price table and freshly rendered artifacts, so the tests
# that read "the real" inputs examine something.
# ---------------------------------------------------------------------------


def _provisional_pricing_copy(tmp_path: Path) -> Path:
    """Copy `config/pricing.yaml` with `provisional: true`, so the prohibition applies."""
    import re

    text = (REPO_ROOT / "config" / "pricing.yaml").read_text(encoding="utf-8")
    forced, count = re.subn(r"(?m)^provisional:\s*\S+\s*$", "provisional: true", text)
    assert count == 1, "config/pricing.yaml has no top-level provisional key to force"
    path = tmp_path / "pricing.yaml"
    path.write_text(forced, encoding="utf-8")
    assert gate.is_provisional(gate.load_price_table(path))
    return path


def _fixture_arm(model_id: str, family: str, *, index: int) -> Any:
    """A cost-withheld arm whose every block comes from its `eval.metrics` report builder."""
    from agents.tier_attribution import CacheCounts, InvocationAttribution, TokenCounts
    from eval.matrix import ArmResult, RunProvenance
    from eval.metrics import (
        ALL_TIERS,
        CallLatencySample,
        ModelMetrics,
        TaskSpread,
        TaskTierObservation,
        TierRoleModelMetrics,
        ToolAggregate,
        UsageTotals,
        aggregate_latency_metrics,
        aggregate_tier_summary,
        aggregate_tier_task_metrics,
        tally_exclusions,
    )
    from eval.scoring import TierScore, TierScores

    usage = UsageTotals(
        input_tokens=1200, output_tokens=300, cache_write_tokens=0, cache_read_tokens=0
    )
    latency = aggregate_latency_metrics(
        measured=True,
        task_latencies_ms=[1500.0, 2500.0],
        call_samples=[CallLatencySample(tier=tier, latency_ms=400.0) for tier in ALL_TIERS],
    )
    tier_rows = [
        TierRoleModelMetrics(
            tier=tier,
            role="supervisor",
            model_id=model_id,
            included_invocation_count=2,
            usage=usage,
            cost=None,
            call_latency_measured=True,
            call_latency_ms_p50=400.0,
            call_latency_ms_p95=400.0,
        )
        for tier in ALL_TIERS
    ]
    scores = TierScores(
        task_id="gt-01",
        supervisor=TierScore(tier="supervisor", applicable=True, passed=True),
        subagent=TierScore(tier="subagent", applicable=True, passed=True),
        tool=TierScore(tier="tool", applicable=True, passed=True),
    )
    invocations = tuple(
        InvocationAttribution(
            tier=tier,
            role="supervisor",
            model_id=model_id,
            tokens=TokenCounts(input_tokens=10, output_tokens=5),
            cache=CacheCounts(),
            latency_ms=400.0,
        )
        for tier in ALL_TIERS
    )
    tier_task = aggregate_tier_task_metrics(
        [
            TaskTierObservation(
                "gt-01", scores, invocations, "data", dict.fromkeys(ALL_TIERS, "medium")
            )
        ],
        measured=True,
        family=family,
        configured_model_ids=dict.fromkeys(ALL_TIERS, model_id),
    )
    metrics = ModelMetrics(
        model_id=model_id,
        included_invocation_count=6,
        usage=usage,
        cost=None,
        cache_min_checkpoint_tokens=1024,
        exclusions=tally_exclusions([]),
        latency=latency,
    )
    return ArmResult(
        provenance=RunProvenance(
            mode="live",
            timestamp=f"2026-01-01T00:00:0{index}+00:00",
            model_id=model_id,
            effort="medium",
            dataset_seed=20260101,
            git_revision="not-a-commit",
            runtime_identifier="local-runtime",
            price_table_version="provisional-copy",
            answer_style_sha256="0" * 64,
        ),
        model_metrics_fields=metrics.to_report_fields(),
        aggregate_score_fields={
            "scored_task_count": 1,
            "unscored_task_count": 0,
            "mean_correctness": 1.0,
            "bad_write_rate": 0.0,
            "tool_call_correct_rate": 1.0,
        },
        task_score_fields=(
            {
                "task_id": "gt-01",
                "scoring_mode": "machine",
                "correctness": 1.0,
                "unscored_reason": None,
                "bad_write": False,
                "tool_call_correct": True,
                "assertion_results": [],
                **scores.to_report_fields(),
            },
        ),
        tier_role_model_fields=tuple(row.to_report_fields() for row in tier_rows),
        tool_fields=(
            ToolAggregate(
                tool_name="get_account",
                role="supervisor",
                count=2,
                call_latency_ms_p50=40.0,
                call_latency_ms_p95=90.0,
                truncated_count=0,
                outcome_distribution={"data": 2, "no_match": 0, "denied": 0, "tool_failure": 0},
            ).to_report_fields(),
        ),
        task_spread_fields=(
            TaskSpread(
                task_id="gt-01",
                repeats=1,
                correctness_min=1.0,
                correctness_mean=1.0,
                correctness_max=1.0,
                success_fraction=1.0,
            ).to_report_fields(),
        ),
        tier_summary_fields=aggregate_tier_summary(
            tier_rows, latency, [scores], withhold_cost=True
        ).to_report_fields(),
        tier_task_metrics_fields=tier_task.to_report_fields(),
    )


def _rendered_results(tmp_path: Path) -> Path:
    """`matrix.{json,md,csv}` and one raw archive per arm, rendered by `eval.matrix`."""
    from eval.matrix import archive_raw_run, build_matrix_result, write_matrix_artifacts

    arms = [
        _fixture_arm("anthropic.claude-opus-5-5", "anthropic", index=0),
        _fixture_arm("openai.gpt-6-astra", "openai", index=1),
    ]
    results = tmp_path / "results"
    write_matrix_artifacts(build_matrix_result(arms), results)
    for arm in arms:
        archive_raw_run(arm, results / "raw")
    return results


def _plant_currency_figure(results: Path) -> None:
    with (results / "matrix.md").open("a", encoding="utf-8") as handle:
        handle.write("\nThe sweep cost $0.47 in model charges.\n")


def _write_yaml(path: Path, data: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _withheld_arm(model_id: str = "anthropic.claude-opus-5-5") -> dict[str, object]:
    """An arm with cost withheld and no currency field present."""
    return {
        "provenance": {"model_id": model_id, "price_table_version": "0.1.0-provisional"},
        "model_metrics": {
            "model_id": model_id,
            "included_invocation_count": 20,
            "input_tokens": 2560,
            "output_tokens": 640,
            "cost_withheld_provisional": True,
        },
        "aggregate_score": {},
        "task_scores": [],
    }


def _forced_unwithheld_arm(model_id: str = "anthropic.claude-opus-5-5") -> dict[str, object]:
    """An arm with cost_withheld forced False against zero rates."""
    return {
        "provenance": {"model_id": model_id, "price_table_version": "0.1.0-provisional"},
        "model_metrics": {
            "model_id": model_id,
            "included_invocation_count": 20,
            "input_tokens": 2560,
            "output_tokens": 640,
            "cost_withheld_provisional": False,
            "cost_input": 0.0,
            "cost_output": 0.0,
            "cost_cache_write": 0.0,
            "cost_cache_read": 0.0,
            "cost_cache_total": 0.0,
            "cost_total": 0.0,
            "price_table_version": "0.1.0-provisional",
        },
        "aggregate_score": {},
        "task_scores": [],
    }


class TestPriceTableHelpers:
    def test_is_provisional_reads_the_flag(self) -> None:
        assert gate.is_provisional(_PROVISIONAL_ALL_ZERO_TABLE) is True
        assert gate.is_provisional(_FINAL_NONZERO_TABLE) is False

    def test_all_rates_are_zero_true_for_the_provisional_table(self) -> None:
        assert gate.all_rates_are_zero(_PROVISIONAL_ALL_ZERO_TABLE) is True

    def test_all_rates_are_zero_false_once_any_rate_is_nonzero(self) -> None:
        assert gate.all_rates_are_zero(_FINAL_NONZERO_TABLE) is False

    def test_all_rates_are_zero_raises_on_non_numeric_rate(self) -> None:
        bad_table = {"models": {"m": {"input": "not-a-number"}}}
        with pytest.raises(gate.CostWithholdingGateError):
            gate.all_rates_are_zero(bad_table)


class TestCurrencyShapeDetection:
    """The gate detects a currency *figure* in prose, not only a currency *field name*."""

    @pytest.mark.parametrize(
        "line",
        [
            "Each live sweep run costs approximately $0.47 in model charges (USD 0.47 total).",
            "The sweep cost $0.47.",
            "Total: USD 0.47",
            "Total: 0.47 USD",
            "About 47 cents per run.",
            "Roughly £12 per sweep.",
            "Roughly €1,234.00 per sweep.",
            "Each run is 3 dollars.",
        ],
    )
    def test_a_prose_currency_figure_is_flagged(self, line: str) -> None:
        violations = gate.find_currency_shapes_in_text(line + "\n", source="README.md")
        assert violations, line

    def test_the_analysts_evasion_now_fails_the_gate_end_to_end(self, tmp_path: Path) -> None:
        """A currency sentence appended to a README, run through `run_gate` with a provisional
        price table, produces a violation."""
        pricing = tmp_path / "pricing.yaml"
        _write_yaml(pricing, _PROVISIONAL_ALL_ZERO_TABLE)
        results = tmp_path / "results"
        results.mkdir()
        (results / "matrix.json").write_text(
            json.dumps({"arms": [_withheld_arm()]}), encoding="utf-8"
        )
        readme = tmp_path / "README.md"
        readme.write_text(
            "# Results\n\nNo figure here.\n"
            "Each live sweep run costs approximately $0.47 in model charges (USD 0.47 total).\n",
            encoding="utf-8",
        )

        violations = gate.run_gate(pricing_path=pricing, results_dir=results, readme_path=readme)

        assert any("currency-shaped figure" in v for v in violations), violations
        assert any("$0" in v for v in violations), violations

    def test_the_rendered_artifacts_and_readme_carry_no_currency_shape(
        self, tmp_path: Path
    ) -> None:
        """Under a provisional table, freshly rendered artifacts and the README are clean."""
        pricing = _provisional_pricing_copy(tmp_path)
        results = _rendered_results(tmp_path)
        result = gate.run_gate_with_coverage(
            pricing_path=pricing, results_dir=results, readme_path=REPO_ROOT / "README.md"
        )
        assert result.artifacts_scanned > 0
        assert result.violations == ()

        _plant_currency_figure(results)
        planted = gate.run_gate(
            pricing_path=pricing, results_dir=results, readme_path=REPO_ROOT / "README.md"
        )
        assert any("$0.47" in v for v in planted), planted


class TestCurrencyShapesThatPreviouslySlipped:
    """Currency shapes that a field-name-only or symbol-only detector misses are detected."""

    @pytest.mark.parametrize(
        "line",
        [
            "Each run costs 0.47 US dollars.",
            "Each run costs 0.47$ per sweep.",
            "Total: 0.47    USD",
            "Each run costs forty-seven cents.",
            "Total \uff040.47",  # U+FF04 fullwidth dollar
            "Total \u20a9620",  # U+20A9 won
            "Total 43\u20bd",  # U+20BD rouble
            "Total &#36;0.47",  # numeric HTML character reference
            "Total &dollar;0.47",  # named HTML character reference
            "Total 0.47\u00a0USD",  # non-breaking space
            "Total $\u200b0.47",  # zero-width space between symbol and numeral
            "Total 0,47 \u20ac",  # European decimal comma, trailing symbol
            "Each run costs one thousand dollars.",
        ],
    )
    def test_a_previously_slipping_shape_is_flagged(self, line: str) -> None:
        assert gate.find_currency_shapes_in_text(line + "\n", source="README.md"), line

    def test_a_figure_split_across_one_line_break_is_flagged(self) -> None:
        text = "The sweep total is $\n0.47 across both arms.\n"
        violations = gate.find_currency_shapes_in_text(text, source="README.md")
        assert any("split across a line break" in v for v in violations), violations

    def test_a_split_figure_is_reported_once_not_per_line(self) -> None:
        text = "cost is USD\n0.47\n"
        violations = gate.find_currency_shapes_in_text(text, source="README.md")
        assert len([v for v in violations if "split across a line break" in v]) == 1, violations

    @pytest.mark.parametrize(
        "line",
        [
            "one of two arms scored above 0.8",
            "20 tasks x 1 repetition = 40 agent runs",
            "3,536 checks, 3,429 pass, 105 skip, 2 DEFER",
            "price_table_version 0.1.0-provisional",
            "cost_withheld_provisional true",
            "No absolute currency figure is published while provisional.",
            "Layer 5 has four inventories",
            "| 5 — Component integration | Adjacent resources work together | yes |",
            'RUN_ARGS+=(--readme-file "${README_FILE}")',
        ],
    )
    def test_ordinary_prose_is_not_flagged(self, line: str) -> None:
        """Ordinary prose without a currency figure is not flagged."""
        assert gate.find_currency_shapes_in_text(line + "\n", source="README.md") == []


class TestCurrencyDetectionStatedBoundaries:
    """The shapes this gate does not detect, each asserted as a stated boundary."""

    def test_a_bare_amount_with_no_currency_indication_is_not_detected(self) -> None:
        assert gate.find_currency_shapes_in_text("the run cost 0.47\n", source="r") == []

    def test_a_figure_split_across_two_line_breaks_is_not_detected(self) -> None:
        assert gate.find_currency_shapes_in_text("total is $\n\n0.47\n", source="r") == []

    def test_a_spelled_out_amount_with_no_adjacent_unit_word_is_not_detected(self) -> None:
        assert (
            gate.find_currency_shapes_in_text(
                "forty-seven hundredths of the unit price of a run\n", source="r"
            )
            == []
        )


class TestMatrixJsonIsNotExemptFromTheFieldNameCheck:
    """A cost-bearing key anywhere in `matrix.json`, at any depth, is a violation."""

    @staticmethod
    def _run(tmp_path: Path, document: dict[str, Any]) -> list[str]:
        pricing = tmp_path / "pricing.yaml"
        _write_yaml(pricing, _PROVISIONAL_ALL_ZERO_TABLE)
        results = tmp_path / "results"
        results.mkdir()
        (results / "matrix.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
        violations: list[str] = gate.run_gate(pricing_path=pricing, results_dir=results)
        return violations

    def test_a_clean_matrix_json_passes(self, tmp_path: Path) -> None:
        assert self._run(tmp_path, {"arms": [_withheld_arm()]}) == []

    def test_cost_total_inside_figure_semantics_fails(self, tmp_path: Path) -> None:
        document = {"arms": [_withheld_arm()], "figure_semantics": {"cost_total": 0.47}}
        violations = self._run(tmp_path, document)
        assert violations, "cost_total inside figure_semantics exited 0"
        assert any("figure_semantics.cost_total" in v for v in violations), violations

    def test_cost_total_at_the_arm_top_level_fails(self, tmp_path: Path) -> None:
        arm = _withheld_arm()
        arm["cost_total"] = 0.47
        violations = self._run(tmp_path, {"arms": [arm]})
        assert violations, "cost_total one level above model_metrics exited 0"

    def test_cost_total_nested_arbitrarily_deep_fails(self, tmp_path: Path) -> None:
        document = {
            "arms": [_withheld_arm()],
            "provenance": {"notes": [{"detail": {"cost_total": 0.47}}]},
        }
        assert self._run(tmp_path, document), "a deeply nested cost_total exited 0"

    def test_a_renamed_cost_bearing_key_with_a_numeric_value_fails(self, tmp_path: Path) -> None:
        """A numeric value under a renamed cost-bearing key is a violation."""
        arm = _withheld_arm()
        arm["usd_total"] = 0.47
        violations = self._run(tmp_path, {"arms": [arm]})
        assert violations, "a figure republished under 'usd_total' exited 0"
        assert any("cost-bearing field" in v for v in violations), violations

    def test_a_currency_shape_inside_a_json_string_value_fails(self, tmp_path: Path) -> None:
        document = {"arms": [_withheld_arm()], "notes": "each run costs about 0.47 US dollars"}
        assert self._run(tmp_path, document), "a currency shape in a JSON string value exited 0"

    def test_a_currency_shape_in_matrix_json_prose_is_flagged(self, tmp_path: Path) -> None:
        """A currency figure inside a `matrix.json` string value is flagged as prose."""
        pricing = tmp_path / "pricing.yaml"
        _write_yaml(pricing, _PROVISIONAL_ALL_ZERO_TABLE)
        results = tmp_path / "results"
        results.mkdir()
        (results / "matrix.json").write_text(
            json.dumps({"arms": [_withheld_arm()], "note": "sweep cost $1.20"}, indent=2),
            encoding="utf-8",
        )

        violations = gate.run_gate(pricing_path=pricing, results_dir=results)

        assert any("currency-shaped figure" in v for v in violations), violations


class TestCurrencyFigureDetectionInText:
    """matrix.md / matrix.csv / results/raw/*.json textual detection."""

    def test_a_markdown_row_naming_a_cost_field_is_flagged(self) -> None:
        text = "| cost_total | 12.34 |\n"
        violations = gate.find_currency_figures_in_text(text, source="matrix.md")
        assert violations

    def test_a_csv_data_row_under_a_cost_column_is_flagged(self) -> None:
        text = "task_id,cost_input\ngt-01,0.42\n"
        violations = gate.find_currency_figures_in_csv(text, source="matrix.csv")
        assert violations

    def test_a_csv_with_an_empty_cost_column_value_is_not_flagged(self) -> None:
        text = "task_id,cost_input\ngt-01,\n"
        violations = gate.find_currency_figures_in_csv(text, source="matrix.csv")
        assert violations == []

    def test_a_csv_with_no_cost_column_is_not_flagged(self) -> None:
        text = "task_id,scoring_mode,correctness\ngt-01,machine,0.8\n"
        violations = gate.find_currency_figures_in_csv(text, source="matrix.csv")
        assert violations == []

    def test_prose_mentioning_cost_withholding_is_not_flagged(self) -> None:
        text = "Cost figures are withheld: the price table is marked provisional.\n"
        violations = gate.find_currency_figures_in_text(text, source="matrix.md")
        assert violations == []

    def test_a_field_name_with_no_following_digit_is_not_flagged(self) -> None:
        text = "cost_withheld_provisional: true\n"
        violations = gate.find_currency_figures_in_text(text, source="matrix.md")
        assert violations == []


class TestCurrencyFigureDetectionInMatrixJson:
    def test_a_withheld_arm_is_not_flagged(self) -> None:
        payload = json.dumps({"arms": [_withheld_arm()]})
        violations = gate.find_currency_figures_in_matrix_json(payload, source="matrix.json")
        assert violations == []

    def test_an_arm_rendering_a_cost_field_is_flagged(self) -> None:
        payload = json.dumps({"arms": [_forced_unwithheld_arm()]})
        violations = gate.find_currency_figures_in_matrix_json(payload, source="matrix.json")
        assert violations
        assert "anthropic.claude-opus-5-5" in violations[0]

    def test_malformed_json_raises_a_gate_violation(self) -> None:
        with pytest.raises(gate.CostWithholdingGateError):
            gate.find_currency_figures_in_matrix_json("{not json", source="matrix.json")


class TestForcedUnwithheldDetection:
    """Detection of an arm whose cost_withheld is forced False."""

    def test_an_honestly_withheld_arm_is_not_flagged(self) -> None:
        payload = json.dumps({"arms": [_withheld_arm()]})
        violations = gate.find_forced_unwithheld_arms(payload, source="matrix.json")
        assert violations == []

    def test_cost_withheld_forced_false_is_caught(self) -> None:
        """`cost_withheld = False` against a provisional, all-zero table is a violation."""
        payload = json.dumps({"arms": [_forced_unwithheld_arm()]})
        violations = gate.find_forced_unwithheld_arms(payload, source="matrix.json")
        assert violations
        assert "anthropic.claude-opus-5-5" in violations[0]


class TestRunGateEndToEnd:
    def test_final_non_provisional_table_skips_every_check(self, tmp_path: Path) -> None:
        pricing_path = tmp_path / "pricing.yaml"
        _write_yaml(pricing_path, _FINAL_NONZERO_TABLE)
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        (results_dir / "matrix.md").write_text("| cost_total | 12.34 |\n", encoding="utf-8")

        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)

        assert violations == []

    def test_deliberately_violating_matrix_md_fixture_is_rejected(self, tmp_path: Path) -> None:
        """A hand-authored violating `matrix.md` fails the gate."""
        pricing_path = tmp_path / "pricing.yaml"
        _write_yaml(pricing_path, _PROVISIONAL_ALL_ZERO_TABLE)
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        (results_dir / "matrix.md").write_text(
            "## Usage and Cost\n\n| field | value |\n| --- | --- |\n| cost_total | 0.000000 |\n",
            encoding="utf-8",
        )

        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)

        assert violations
        assert any("matrix.md" in v for v in violations)

    def test_deliberately_violating_matrix_json_fixture_is_rejected(self, tmp_path: Path) -> None:
        """A `matrix.json` with cost_withheld forced False fails the gate end to end."""
        pricing_path = tmp_path / "pricing.yaml"
        _write_yaml(pricing_path, _PROVISIONAL_ALL_ZERO_TABLE)
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        (results_dir / "matrix.json").write_text(
            json.dumps({"arms": [_forced_unwithheld_arm(), _withheld_arm("openai.gpt-6-astra")]}),
            encoding="utf-8",
        )

        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)

        assert violations
        joined = "\n".join(violations)
        assert "anthropic.claude-opus-5-5" in joined
        assert "openai.gpt-6-astra" not in joined

    def test_honestly_withheld_artifacts_pass(self, tmp_path: Path) -> None:
        pricing_path = tmp_path / "pricing.yaml"
        _write_yaml(pricing_path, _PROVISIONAL_ALL_ZERO_TABLE)
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        (results_dir / "matrix.md").write_text(
            "Cost figures are withheld: the price table is marked provisional.\n",
            encoding="utf-8",
        )
        (results_dir / "matrix.json").write_text(
            json.dumps({"arms": [_withheld_arm()]}), encoding="utf-8"
        )

        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)

        assert violations == []

    def test_raw_archive_currency_figure_is_rejected(self, tmp_path: Path) -> None:
        pricing_path = tmp_path / "pricing.yaml"
        _write_yaml(pricing_path, _PROVISIONAL_ALL_ZERO_TABLE)
        results_dir = tmp_path / "results"
        raw_dir = results_dir / "raw"
        raw_dir.mkdir(parents=True)
        (raw_dir / "anthropic.claude-opus-5-5_2026-09-01T00-00-00Z.json").write_text(
            '{"cost_total": 0.5}\n', encoding="utf-8"
        )

        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)

        assert violations
        assert any("raw" in v for v in violations)

    def test_no_results_dir_is_not_an_error(self, tmp_path: Path) -> None:
        pricing_path = tmp_path / "pricing.yaml"
        _write_yaml(pricing_path, _PROVISIONAL_ALL_ZERO_TABLE)
        results_dir = tmp_path / "results-does-not-exist"

        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)

        assert violations == []

    def test_readme_file_is_scanned_when_supplied(self, tmp_path: Path) -> None:
        """A supplied README file is scanned for cost figures."""
        pricing_path = tmp_path / "pricing.yaml"
        _write_yaml(pricing_path, _PROVISIONAL_ALL_ZERO_TABLE)
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        readme_path = tmp_path / "README.md"
        readme_path.write_text("# FirstCRM Platform\n\ncost_total: 12.34\n", encoding="utf-8")

        violations = gate.run_gate(
            pricing_path=pricing_path, results_dir=results_dir, readme_path=readme_path
        )

        assert violations
        assert any("README.md" in v for v in violations)

    def test_readme_file_omitted_is_not_scanned(self, tmp_path: Path) -> None:
        """The gate does not scan README.md when no path is supplied."""
        pricing_path = tmp_path / "pricing.yaml"
        _write_yaml(pricing_path, _PROVISIONAL_ALL_ZERO_TABLE)
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        readme_path = tmp_path / "README.md"
        readme_path.write_text("cost_total: 12.34\n", encoding="utf-8")

        violations = gate.run_gate(pricing_path=pricing_path, results_dir=results_dir)

        assert violations == []


class TestMainExitCodes:
    def test_main_returns_zero_on_pass(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pricing_path = tmp_path / "pricing.yaml"
        _write_yaml(pricing_path, _FINAL_NONZERO_TABLE)
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        exit_code = gate.main(
            ["--pricing-file", str(pricing_path), "--results-dir", str(results_dir)]
        )

        assert exit_code == 0
        assert "OK" in capsys.readouterr().out

    def test_main_returns_one_on_violation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pricing_path = tmp_path / "pricing.yaml"
        _write_yaml(pricing_path, _PROVISIONAL_ALL_ZERO_TABLE)
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        (results_dir / "matrix.md").write_text("| cost_total | 1.0 |\n", encoding="utf-8")

        exit_code = gate.main(
            ["--pricing-file", str(pricing_path), "--results-dir", str(results_dir)]
        )

        assert exit_code == 1
        assert "FAIL" in capsys.readouterr().err

    def test_main_returns_two_on_missing_pricing_file(self, tmp_path: Path) -> None:
        exit_code = gate.main(
            [
                "--pricing-file",
                str(tmp_path / "does-not-exist.yaml"),
                "--results-dir",
                str(tmp_path),
            ]
        )
        assert exit_code == 2


class TestRealPricingFileIsCleanToday:
    """The committed price table, forced provisional, passes freshly rendered artifacts."""

    def test_gate_passes_against_the_committed_price_table(self, tmp_path: Path) -> None:
        pricing = _provisional_pricing_copy(tmp_path)
        results = _rendered_results(tmp_path)

        result = gate.run_gate_with_coverage(pricing_path=pricing, results_dir=results)
        assert result.artifacts_scanned > 0
        assert result.violations == ()

        (results / "matrix.md").write_text("| cost_total | 1.0 |\n", encoding="utf-8")
        assert gate.run_gate(pricing_path=pricing, results_dir=results)


class TestShellWrapper:
    """The shell entry point Makefile's check-cost-withheld target invokes."""

    def test_shell_wrapper_passes_rendered_artifacts_under_a_provisional_table(
        self, tmp_path: Path
    ) -> None:
        import re

        pricing = _provisional_pricing_copy(tmp_path)
        results = _rendered_results(tmp_path)
        no_frontend = tmp_path / "frontend"
        no_frontend.mkdir()
        argv = [
            "bash",
            str(REPO_ROOT / "scripts" / "check_cost_withheld.sh"),
            "--pricing-file",
            str(pricing),
            "--results-dir",
            str(results),
            "--frontend-dir",
            str(no_frontend),
        ]
        clean = proc_run.run(argv, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
        assert clean.returncode == 0, clean.stdout + clean.stderr
        scanned = re.search(r"(\d+) artifact\(s\) scanned", clean.stdout + clean.stderr)
        assert scanned is not None and int(scanned.group(1)) > 0, clean.stdout + clean.stderr

        _plant_currency_figure(results)
        planted = proc_run.run(argv, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
        assert planted.returncode == 1, planted.stdout + planted.stderr

    def test_shell_wrapper_fails_against_a_deliberately_violating_results_dir(
        self, tmp_path: Path
    ) -> None:
        pricing_path = tmp_path / "pricing.yaml"
        _write_yaml(pricing_path, _PROVISIONAL_ALL_ZERO_TABLE)
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        (results_dir / "matrix.md").write_text("| cost_total | 1.0 |\n", encoding="utf-8")

        result = proc_run.run(
            [
                "bash",
                str(REPO_ROOT / "scripts" / "check_cost_withheld.sh"),
                "--pricing-file",
                str(pricing_path),
                "--results-dir",
                str(results_dir),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "FAIL" in result.stderr

    def test_shell_wrapper_exits_two_on_bad_usage(self) -> None:
        result = proc_run.run(
            ["bash", str(REPO_ROOT / "scripts" / "check_cost_withheld.sh"), "--unknown-flag"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2

    def test_shell_wrapper_fails_when_the_readme_it_is_pointed_at_carries_a_cost_figure(
        self, tmp_path: Path
    ) -> None:
        """The wrapper fails on a `--readme-file` carrying a currency figure, with `SCAN_README`
        empty."""
        pricing_path = tmp_path / "pricing.yaml"
        _write_yaml(pricing_path, _PROVISIONAL_ALL_ZERO_TABLE)
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        readme_path = tmp_path / "README.md"
        readme_path.write_text("# Eval Summary\n\n| cost_total | 12.480000 |\n", encoding="utf-8")

        result = proc_run.run(
            [
                "bash",
                str(REPO_ROOT / "scripts" / "check_cost_withheld.sh"),
                "--pricing-file",
                str(pricing_path),
                "--results-dir",
                str(results_dir),
                "--readme-file",
                str(readme_path),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "SCAN_README": ""},
        )
        assert result.returncode == 1, result.stderr
        assert "FAIL" in result.stderr
        assert str(readme_path) in result.stderr

    def test_shell_wrapper_passes_when_the_readme_it_is_pointed_at_carries_no_cost_figure(
        self, tmp_path: Path
    ) -> None:
        """The wrapper passes a `--readme-file` that carries no cost figure."""
        pricing_path = tmp_path / "pricing.yaml"
        _write_yaml(pricing_path, _PROVISIONAL_ALL_ZERO_TABLE)
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        readme_path = tmp_path / "README.md"
        readme_path.write_text(
            "# Eval Summary\n\nNo cost figures published yet.\n", encoding="utf-8"
        )
        # The wrapper also scans the frontend, and the real dashboard renders cost, so an empty
        # synthetic frontend dir is supplied.
        frontend_dir = tmp_path / "frontend"
        frontend_dir.mkdir()

        result = proc_run.run(
            [
                "bash",
                str(REPO_ROOT / "scripts" / "check_cost_withheld.sh"),
                "--pricing-file",
                str(pricing_path),
                "--results-dir",
                str(results_dir),
                "--readme-file",
                str(readme_path),
                "--frontend-dir",
                str(frontend_dir),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# A numeric field in a structured artifact is a violation unless its key is a known non-currency
# field.
# ---------------------------------------------------------------------------


def _run_over_matrix_json(tmp_path: Path, document: dict[str, Any]) -> list[str]:
    pricing = tmp_path / "pricing.yaml"
    _write_yaml(pricing, _PROVISIONAL_ALL_ZERO_TABLE)
    results = tmp_path / "results"
    results.mkdir()
    (results / "matrix.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    violations: list[str] = gate.run_gate(pricing_path=pricing, results_dir=results)
    return violations


class TestRenamedCostKeysAreClosedByInvertingTheBurden:
    @pytest.mark.parametrize(
        "key",
        [
            # Cost-suggestive names a keyword denylist would plausibly carry.
            "fee_total",
            "total_outlay",
            "run_expenditure",
            # Innocuous names no denylist would carry.
            "burn_rate_total",
            "tally",
            "q3",
            "settlement",
            "consideration",
            "remuneration",
        ],
    )
    def test_a_numeric_field_under_an_unlisted_key_is_a_violation(
        self, tmp_path: Path, key: str
    ) -> None:
        arm = _withheld_arm()
        assert isinstance(arm, dict)
        arm[key] = 0.47
        violations = _run_over_matrix_json(tmp_path, {"arms": [arm]})
        assert violations, f"a numeric field named {key!r} was published with no finding"

    def test_an_integer_valued_unlisted_key_is_a_violation(self, tmp_path: Path) -> None:
        arm = _withheld_arm()
        arm["headcount_charge_units"] = 47
        assert _run_over_matrix_json(tmp_path, {"arms": [arm]})

    def test_a_number_nested_in_a_list_is_judged_by_its_enclosing_key(self, tmp_path: Path) -> None:
        """A number inside a list, such as `{"amounts": [0.47]}`, is judged by its enclosing key."""
        document = {"arms": [_withheld_arm()], "figure_semantics": {"amounts": [0.47]}}
        assert _run_over_matrix_json(tmp_path, document)

    def test_a_deeply_nested_unlisted_key_is_a_violation(self, tmp_path: Path) -> None:
        document = {
            "arms": [_withheld_arm()],
            "figure_semantics": {"detail": {"per_arm": {"opus": 0.47}}},
        }
        assert _run_over_matrix_json(tmp_path, document)

    def test_a_boolean_is_not_a_currency_value(self, tmp_path: Path) -> None:
        """A boolean value is not treated as a figure, although `True` is an `int` in Python."""
        document = {"arms": [_withheld_arm()], "figure_semantics": {"narrowed": True}}
        assert _run_over_matrix_json(tmp_path, document) == []

    def test_allowlisted_metric_fields_do_not_false_fail(self, tmp_path: Path) -> None:
        arm = _withheld_arm()
        metrics = arm["model_metrics"]
        assert isinstance(metrics, dict)
        metrics.update(
            {
                "cache_read_tokens": 3366,
                "cache_write_tokens": 0,
                "cache_min_checkpoint_tokens": 512,
                "cold_start": 0,
                "retry": 0,
                "throttle": 0,
            }
        )
        document = {
            "arms": [arm],
            "aggregate": {
                "mean_correctness": 0.85,
                "bad_write_rate": 0.0,
                "tool_call_correct_rate": 1.0,
                "scored_task_count": 20,
                "unscored_task_count": 0,
                "total_excluded": 0,
                "dataset_seed": 20260101,
            },
        }
        assert _run_over_matrix_json(tmp_path, document) == []

    def test_r4_latency_fields_do_not_false_fail(self, tmp_path: Path) -> None:
        """The latency fields, including nested `p50`/`p95`/`count` inside
        `call_latency_ms_by_tier`, are allowlisted and do not fail the rule."""
        arm = _withheld_arm()
        metrics = arm["model_metrics"]
        assert isinstance(metrics, dict)
        metrics.update(
            {
                "latency_measured": True,
                "latency_percentile_method": "nearest-rank",
                "task_latency_ms_p50": 1234,
                "task_latency_ms_p95": 5678,
                "call_latency_ms_by_tier": {
                    "supervisor": {"p50": 900, "p95": 1500, "count": 20},
                    "subagent": {"p50": 300, "p95": 700, "count": 8},
                    "tool": {"p50": 40, "p95": 90, "count": 40},
                },
            }
        )
        assert _run_over_matrix_json(tmp_path, {"arms": [arm]}) == []

    def test_a_renamed_csv_column_carrying_a_number_is_a_violation(self) -> None:
        text = "model_id,task_id,settlement\nopus,gt-01,0.47\n"
        violations = gate.find_currency_figures_in_csv(text, source="results/matrix.csv")
        assert violations, "a renamed numeric CSV column published no finding"

    def test_an_allowlisted_csv_column_does_not_false_fail(self) -> None:
        text = "model_id,task_id,correctness\nopus,gt-01,0.750000\n"
        assert gate.find_currency_figures_in_csv(text, source="results/matrix.csv") == []

    def test_the_allowlist_covers_every_numeric_field_of_the_rendered_artifacts(
        self, tmp_path: Path
    ) -> None:
        """Every numeric field `eval.matrix` renders is allowlisted; a renamed one is not."""
        pricing = _provisional_pricing_copy(tmp_path)
        results = _rendered_results(tmp_path)
        numeric: set[str] = set()

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if gate._is_published_currency_value(value):
                        numeric.add(key)
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(json.loads((results / "matrix.json").read_text(encoding="utf-8")))
        assert len(numeric) > 20, sorted(numeric)
        result = gate.run_gate_with_coverage(pricing_path=pricing, results_dir=results)
        assert result.artifacts_scanned > 0
        assert result.violations == (), result.violations

        document = json.loads((results / "matrix.json").read_text(encoding="utf-8"))
        document["arms"][0]["model_metrics"]["settlement"] = 0.47
        (results / "matrix.json").write_text(json.dumps(document), encoding="utf-8")
        planted = gate.run_gate(pricing_path=pricing, results_dir=results)
        assert [v for v in planted if "settlement" in v], planted


class TestSymbolAndCodeSpacingAreConsistent:
    """A currency symbol and an ISO code allow the same intra-line whitespace before the numeral."""

    @pytest.mark.parametrize(
        "line",
        [
            "the sweep cost $0.47",
            "the sweep cost $ 0.47",
            "the sweep cost $  0.47",
            "the sweep cost $\t0.47",
            "the sweep cost $\u00a00.47",
            "the sweep cost $\u00a0\u00a00.47",
            "the sweep cost $\u20090.47",
            "the sweep cost 0.47   $",
            "the sweep cost \uff04  0.47",
        ],
    )
    def test_a_symbol_separated_from_its_numeral_is_still_a_figure(self, line: str) -> None:
        violations = gate.find_currency_shapes_in_text(line + "\n", source="README.md")
        assert violations, f"{line!r} slipped the symbol-adjacency check"


class TestInvertedRuleStatedBoundaries:
    """What the inverted rule does not reach, each asserted as a stated boundary."""

    def test_an_unrecognised_prose_unit_word_is_not_detected(self) -> None:
        """A prose unit word outside the closed `_CURRENCY_UNIT_WORDS` set is not detected."""
        for line in ("the sweep cost 0.47 greenbacks\n", "the sweep cost 0.47 doubloons\n"):
            assert gate.find_currency_shapes_in_text(line, source="r") == [], line

    def test_a_renamed_cost_key_in_a_text_artifact_is_not_detected(self, tmp_path: Path) -> None:
        """A renamed cost key in the text-scanned `results/matrix.md` is not detected."""
        pricing = tmp_path / "pricing.yaml"
        _write_yaml(pricing, _PROVISIONAL_ALL_ZERO_TABLE)
        results = tmp_path / "results"
        results.mkdir()
        (results / "matrix.md").write_text("| fee_total | 0.47 |\n", encoding="utf-8")
        assert gate.run_gate(pricing_path=pricing, results_dir=results) == []
