# SPDX-License-Identifier: MIT
"""Tests for :func:`eval.metrics.completion_over_repeats` and its place in the tier summary: a task
counts at a tier when it passed in a strict majority of its applicable repeats.
"""

from __future__ import annotations

import pytest

from eval.metrics import (
    RepeatCompletion,
    TierSuccess,
    TierSummaryEntry,
    aggregate_tier_summary,
    completion_over_repeats,
    not_measured_latency_metrics,
)
from eval.scoring import SCORED_TIERS, TierScore, TierScores


def _scores(task_id: str, **passed: bool | None) -> TierScores:
    by_tier = {
        tier: TierScore(tier=tier, applicable=passed.get(tier) is not None, passed=passed.get(tier))
        for tier in SCORED_TIERS
    }
    return TierScores(task_id=task_id, **by_tier)


def _repeats(
    task_id: str, outcomes: list[bool | None], tier: str = "supervisor"
) -> list[TierScores]:
    return [_scores(task_id, **{tier: outcome}) for outcome in outcomes]


def test_three_passes_of_five_completes() -> None:
    result = completion_over_repeats(_repeats("t1", [True, True, True, False, False]), "supervisor")
    assert result == RepeatCompletion(completed=1, applicable=1, repeats=5)
    assert result.rate == 1.0


def test_two_passes_of_five_does_not_complete() -> None:
    result = completion_over_repeats(
        _repeats("t1", [True, True, False, False, False]), "supervisor"
    )
    assert result == RepeatCompletion(completed=0, applicable=1, repeats=5)
    assert result.rate == 0.0


def test_an_even_split_is_not_a_strict_majority() -> None:
    result = completion_over_repeats(_repeats("t1", [True, False]), "supervisor")
    assert result.completed == 0


def test_majority_counts_only_the_repeats_where_the_tier_was_applicable() -> None:
    turns = _repeats("t1", [True, True, None, None, False])
    result = completion_over_repeats(turns, "supervisor")
    assert result == RepeatCompletion(completed=1, applicable=1, repeats=5)


def test_one_applicable_pass_among_mostly_inapplicable_repeats_completes() -> None:
    turns = _repeats("t1", [None, None, None, None, True])
    assert completion_over_repeats(turns, "supervisor").completed == 1


def test_a_tier_never_applicable_for_a_task_leaves_it_out_of_the_denominator() -> None:
    turns = _repeats("t1", [True, True, True]) + _repeats("t2", [None, None, None])
    result = completion_over_repeats(turns, "supervisor")
    assert result == RepeatCompletion(completed=1, applicable=1, repeats=3)


def test_tasks_are_grouped_by_id_regardless_of_turn_order() -> None:
    turns = [
        _scores("t1", supervisor=True),
        _scores("t2", supervisor=False),
        _scores("t1", supervisor=True),
        _scores("t2", supervisor=False),
        _scores("t1", supervisor=False),
        _scores("t2", supervisor=True),
    ]
    result = completion_over_repeats(turns, "supervisor")
    assert result == RepeatCompletion(completed=1, applicable=2, repeats=3)
    assert result.rate == 0.5


def test_each_tier_is_counted_independently() -> None:
    turns = [
        _scores("t1", supervisor=True, subagent=False, tool=None),
        _scores("t1", supervisor=True, subagent=False, tool=None),
        _scores("t1", supervisor=False, subagent=True, tool=None),
    ]
    assert completion_over_repeats(turns, "supervisor").completed == 1
    assert completion_over_repeats(turns, "subagent").completed == 0
    assert completion_over_repeats(turns, "tool").applicable == 0


@pytest.mark.parametrize("tier", SCORED_TIERS)
def test_one_repeat_equals_the_per_turn_success_figure(tier: str) -> None:
    turns = [
        _scores("t1", supervisor=True, subagent=True, tool=None),
        _scores("t2", supervisor=False, subagent=True, tool=True),
        _scores("t3", supervisor=None, subagent=False, tool=False),
        _scores("t4", supervisor=True, subagent=None, tool=True),
    ]
    applicable = [turn.by_tier()[tier] for turn in turns if turn.by_tier()[tier].applicable]
    per_turn = TierSuccess(
        passed=sum(1 for score in applicable if score.passed), applicable=len(applicable)
    )
    result = completion_over_repeats(turns, tier)
    assert result.repeats == 1
    assert (result.completed, result.applicable, result.rate) == (
        per_turn.passed,
        per_turn.applicable,
        per_turn.rate,
    )


def test_unequal_repeat_counts_raise() -> None:
    turns = _repeats("t1", [True, True, True]) + _repeats("t2", [True, True])
    with pytest.raises(ValueError, match="unequal repeat counts"):
        completion_over_repeats(turns, "supervisor")


def test_an_unknown_tier_raises() -> None:
    with pytest.raises(ValueError, match="tier must be one of"):
        completion_over_repeats(_repeats("t1", [True]), "planner")


def test_rate_is_none_at_zero_applicable() -> None:
    result = completion_over_repeats(_repeats("t1", [None, None]), "supervisor")
    assert result == RepeatCompletion(completed=0, applicable=0, repeats=2)
    assert result.rate is None


def test_no_turns_gives_zero_repeats_and_a_null_rate() -> None:
    result = completion_over_repeats((), "tool")
    assert result == RepeatCompletion(completed=0, applicable=0, repeats=0)
    assert result.rate is None


def test_report_fields_are_completed_applicable_repeats_rate_in_order() -> None:
    fields = RepeatCompletion(completed=3, applicable=4, repeats=5).to_report_fields()
    assert list(fields) == ["completed", "applicable", "repeats", "rate"]
    assert fields == {"completed": 3, "applicable": 4, "repeats": 5, "rate": 0.75}


def test_tier_summary_entry_report_fields_carry_completion_over_repeats() -> None:
    entry = TierSummaryEntry(
        tier="tool",
        input_tokens=1,
        output_tokens=1,
        cost_total=None,
        latency_p50_ms=None,
        latency_p95_ms=None,
        success=TierSuccess(passed=1, applicable=2),
        completion_over_repeats=RepeatCompletion(completed=1, applicable=1, repeats=2),
    )
    fields = entry.to_report_fields()
    assert fields["completion_over_repeats"] == {
        "completed": 1,
        "applicable": 1,
        "repeats": 2,
        "rate": 1.0,
    }
    assert list(fields)[-1] == "completion_over_repeats"


def test_aggregate_tier_summary_computes_completion_over_repeats_per_tier() -> None:
    turns = [
        _scores("t1", supervisor=True, subagent=False, tool=None),
        _scores("t2", supervisor=False, subagent=False, tool=None),
        _scores("t1", supervisor=True, subagent=True, tool=None),
        _scores("t2", supervisor=False, subagent=False, tool=None),
        _scores("t1", supervisor=False, subagent=True, tool=None),
        _scores("t2", supervisor=True, subagent=False, tool=None),
    ]
    summary = aggregate_tier_summary(
        (), not_measured_latency_metrics(), turns, withhold_cost=True
    ).to_report_fields()
    assert summary["supervisor"]["completion_over_repeats"] == {  # type: ignore[index]
        "completed": 1,
        "applicable": 2,
        "repeats": 3,
        "rate": 0.5,
    }
    assert summary["subagent"]["completion_over_repeats"] == {  # type: ignore[index]
        "completed": 1,
        "applicable": 2,
        "repeats": 3,
        "rate": 0.5,
    }
    assert summary["tool"]["completion_over_repeats"] == {  # type: ignore[index]
        "completed": 0,
        "applicable": 0,
        "repeats": 3,
        "rate": None,
    }
    assert summary["supervisor"]["success"] == {  # type: ignore[index]
        "passed": 3,
        "applicable": 6,
        "rate": 0.5,
    }
