# SPDX-License-Identifier: MIT
"""Tests for the per-tier ``tier_summary`` block: :func:`eval.metrics.aggregate_tier_summary` and its
rendering through :class:`eval.matrix.ArmResult` into ``matrix.json``, ``matrix.md`` and
``matrix.csv``.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import json

import pytest

from eval import harness
from eval.matrix import (
    ArmResult,
    arm_from_report_fields,
    build_matrix_result,
    render_csv,
    render_json,
    render_markdown,
)
from eval.metrics import (
    TIER_SUMMARY_REPORT_KEY,
    CostTotals,
    LatencyMetrics,
    TierLatency,
    TierRoleModelMetrics,
    UsageTotals,
    aggregate_tier_summary,
    not_measured_latency_metrics,
)
from eval.scoring import SCORED_TIERS, TierScore, TierScores
from tests.unit.test_matrix import _sample_arm


def _row(
    tier: str, *, role: str, input_tokens: int, output_tokens: int, cost: float | None
) -> TierRoleModelMetrics:
    return TierRoleModelMetrics(
        tier=tier,  # type: ignore[arg-type]
        role=role,
        model_id=f"model-{tier}",
        included_invocation_count=1,
        usage=UsageTotals(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_write_tokens=0,
            cache_read_tokens=0,
        ),
        cost=None
        if cost is None
        else CostTotals(
            input=cost, output=0.0, cache_write=0.0, cache_read=0.0, price_table_version="v1"
        ),
        call_latency_measured=False,
        call_latency_ms_p50=None,
        call_latency_ms_p95=None,
    )


def _scores(task_id: str, **passed: bool | None) -> TierScores:
    by_tier = {
        tier: TierScore(tier=tier, applicable=passed.get(tier) is not None, passed=passed.get(tier))
        for tier in SCORED_TIERS
    }
    return TierScores(task_id=task_id, **by_tier)


_ROWS = (
    _row("supervisor", role="supervisor", input_tokens=100, output_tokens=10, cost=0.5),
    _row("subagent", role="research", input_tokens=40, output_tokens=4, cost=0.25),
    _row("subagent", role="mutation", input_tokens=60, output_tokens=6, cost=0.125),
    _row("tool", role="research", input_tokens=0, output_tokens=0, cost=None),
)

_LATENCY = LatencyMetrics(
    measured=True,
    task_p50_ms=1000.0,
    task_p95_ms=2000.0,
    by_tier={
        "supervisor": TierLatency(p50=300.0, p95=900.0, count=4),
        "subagent": TierLatency(p50=120.0, p95=400.0, count=3),
        "tool": TierLatency(p50=None, p95=None, count=0),
    },
)

_TIER_SCORES = (
    _scores("t1", supervisor=True, subagent=True, tool=None),
    _scores("t2", supervisor=False, subagent=True, tool=None),
    _scores("t3", supervisor=None, subagent=False, tool=None),
)


def _summary_fields(*, withhold_cost: bool = False) -> dict[str, object]:
    return aggregate_tier_summary(
        _ROWS, _LATENCY, _TIER_SCORES, withhold_cost=withhold_cost
    ).to_report_fields()


# ------------------------------------------------------------------------------ aggregation


def test_every_tier_is_present_in_order() -> None:
    assert list(_summary_fields()) == list(SCORED_TIERS)


def test_tokens_are_summed_over_every_row_at_a_tier() -> None:
    fields = _summary_fields()
    assert fields["subagent"]["input_tokens"] == 100  # type: ignore[index]
    assert fields["subagent"]["output_tokens"] == 10  # type: ignore[index]
    assert fields["supervisor"]["input_tokens"] == 100  # type: ignore[index]


def test_cost_is_summed_per_tier_when_not_withheld() -> None:
    fields = _summary_fields()
    assert fields["supervisor"]["cost_total"] == pytest.approx(0.5)  # type: ignore[index]
    assert fields["subagent"]["cost_total"] == pytest.approx(0.375)  # type: ignore[index]
    assert fields["tool"]["cost_total"] == 0.0  # type: ignore[index]


def test_cost_is_null_at_every_tier_when_withheld() -> None:
    fields = _summary_fields(withhold_cost=True)
    for tier in SCORED_TIERS:
        assert fields[tier]["cost_total"] is None  # type: ignore[index]


def test_latency_is_read_from_the_arms_per_tier_latency() -> None:
    fields = _summary_fields()
    assert fields["supervisor"]["latency_p50_ms"] == 300.0  # type: ignore[index]
    assert fields["supervisor"]["latency_p95_ms"] == 900.0  # type: ignore[index]
    assert fields["tool"]["latency_p50_ms"] is None  # type: ignore[index]


def test_success_counts_only_applicable_turns() -> None:
    fields = _summary_fields()
    assert fields["supervisor"]["success"] == {  # type: ignore[index]
        "passed": 1,
        "applicable": 2,
        "rate": 0.5,
    }
    assert fields["subagent"]["success"] == {  # type: ignore[index]
        "passed": 2,
        "applicable": 3,
        "rate": pytest.approx(2 / 3),
    }


def test_a_tier_with_no_applicable_turn_reports_a_null_rate_not_zero() -> None:
    assert _summary_fields()["tool"]["success"] == {  # type: ignore[index]
        "passed": 0,
        "applicable": 0,
        "rate": None,
    }


def test_an_empty_arm_still_reports_every_tier() -> None:
    fields = aggregate_tier_summary(
        (), not_measured_latency_metrics(), (), withhold_cost=False
    ).to_report_fields()
    assert list(fields) == list(SCORED_TIERS)
    assert fields["supervisor"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_total": 0.0,
        "long_context_invocation_count": 0,
        "latency_p50_ms": None,
        "latency_p95_ms": None,
        "success": {"passed": 0, "applicable": 0, "rate": None},
        "completion_over_repeats": {"completed": 0, "applicable": 0, "repeats": 0, "rate": None},
    }


def test_the_report_key_is_tier_summary() -> None:
    assert TIER_SUMMARY_REPORT_KEY == "tier_summary"


# ------------------------------------------------------------------------------ rendering


def _arm_with_summary(*, withhold_cost: bool = True) -> ArmResult:
    return dataclasses.replace(
        _sample_arm(), tier_summary_fields=_summary_fields(withhold_cost=withhold_cost)
    )


def test_an_arm_without_a_summary_renders_an_empty_object_in_json() -> None:
    payload = json.loads(render_json(build_matrix_result([_sample_arm()])))
    assert payload["arms"][0]["tier_summary"] == {}


def test_an_arm_with_a_summary_renders_it_in_json() -> None:
    arm = _arm_with_summary()
    payload = json.loads(render_json(build_matrix_result([arm])))
    assert payload["arms"][0]["tier_summary"] == json.loads(json.dumps(arm.tier_summary_fields))


def test_markdown_renders_the_summary_subtable_only_when_present() -> None:
    assert "Per-tier summary:" not in render_markdown(build_matrix_result([_sample_arm()]))
    markdown = render_markdown(build_matrix_result([_arm_with_summary()]))
    assert "Per-tier summary:" in markdown
    assert "| supervisor | 100 | 10 | — | 300.000000 | 900.000000 | 1/2 | 0.500000 |" in markdown
    assert "| tool | 0 | 0 | — | — | — | not applicable | — |" in markdown


def test_markdown_with_a_summary_carries_no_verdict_word() -> None:
    markdown = render_markdown(build_matrix_result([_arm_with_summary(withhold_cost=False)]))
    assert harness._rendered_verdict_words(markdown) == []


def _csv_rows(arm: ArmResult) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(render_csv(build_matrix_result([arm])))))


def test_csv_carries_the_summary_columns_on_every_task_row() -> None:
    rows = _csv_rows(_arm_with_summary(withhold_cost=False))
    assert len(rows) == 2
    for row in rows:
        assert row["tier_supervisor_input_tokens"] == "100"
        assert row["tier_subagent_cost"] == "0.375000"
        assert row["tier_supervisor_success_passed"] == "1"
        assert row["tier_supervisor_success_applicable"] == "2"
        assert row["tier_supervisor_success_rate"] == "0.500000"
        assert row["tier_tool_success_rate"] == ""


def test_a_withheld_summary_leaves_the_csv_cost_cells_empty() -> None:
    rows = _csv_rows(_arm_with_summary(withhold_cost=True))
    for tier in SCORED_TIERS:
        assert rows[0][f"tier_{tier}_cost"] == ""
    assert rows[0]["tier_supervisor_input_tokens"] == "100"


def test_a_withheld_summary_names_no_cost_total_in_markdown_or_csv() -> None:
    matrix = build_matrix_result([_arm_with_summary(withhold_cost=True)])
    assert "cost_total" not in render_markdown(matrix)
    assert "cost_total" not in render_csv(matrix)


def test_csv_summary_cells_are_empty_but_present_without_a_summary() -> None:
    rows = _csv_rows(_sample_arm())
    for tier in SCORED_TIERS:
        assert rows[0][f"tier_{tier}_input_tokens"] == ""


def test_the_summary_round_trips_through_a_raw_archive() -> None:
    arm = _arm_with_summary()
    rebuilt = arm_from_report_fields(json.loads(json.dumps(arm.to_report_fields())))
    assert rebuilt.tier_summary_fields == json.loads(json.dumps(arm.tier_summary_fields))


def test_an_archive_without_a_summary_rebuilds_an_empty_one() -> None:
    fields = _sample_arm().to_report_fields()
    del fields["tier_summary"]
    assert arm_from_report_fields(fields).tier_summary_fields == {}


def test_an_archive_with_a_non_object_summary_is_refused() -> None:
    fields = _sample_arm().to_report_fields()
    fields["tier_summary"] = ["supervisor"]
    with pytest.raises(ValueError, match="'tier_summary' is not an object"):
        arm_from_report_fields(fields)
