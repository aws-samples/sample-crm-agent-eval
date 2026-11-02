# SPDX-License-Identifier: MIT
"""Tests for :func:`eval.scoring.score_tiers` and :func:`eval.scoring.expected_tiers_of`.

Each tier rule is exercised on its own: supervisor = the task's correctness is a full 1.0,
subagent = the observed delegation outcome equals the expected one, tool = an expected tool was
called with arguments passing the toolspec validator. A tier the task does not exercise is reported
not applicable, never as a pass.
"""

from __future__ import annotations

from typing import Any

import pytest

from eval.scoring import (
    SCORED_TIERS,
    ObservedToolArguments,
    TaskScore,
    TierScore,
    TurnOutcome,
    expected_tiers_of,
    score_tiers,
)


def _task(subagent: object = None, tool: object = None) -> dict[str, Any]:
    return {"id": "gt-99-lookup-probe", "expected_tiers": {"subagent": subagent, "tool": tool}}


def _score(correctness: float | None) -> TaskScore:
    return TaskScore(
        task_id="gt-99-lookup-probe",
        scoring_mode="machine",
        correctness=correctness,
        unscored_reason=None if correctness is not None else "no positive assertion",
        bad_write=False,
        tool_call_correct=None,
    )


def _outcome(denial_outcome: str | None = None) -> TurnOutcome:
    return TurnOutcome(tenant="eval-probe", denial_outcome=denial_outcome)


_VALID_GET = ObservedToolArguments(tool_name="get_opportunity", arguments={"opportunity_id": "o1"})
_INVALID_GET = ObservedToolArguments(tool_name="get_opportunity", arguments={"opportunity_id": ""})
_VALID_OTHER = ObservedToolArguments(tool_name="get_account", arguments={"account_id": "a1"})


# ------------------------------------------------------------------------------ supervisor


def test_supervisor_is_met_at_full_correctness() -> None:
    scores = score_tiers(_task(), _outcome(), (), task_score=_score(1.0))
    assert scores.supervisor == TierScore(tier="supervisor", applicable=True, passed=True)


def test_supervisor_is_not_met_below_full_correctness() -> None:
    scores = score_tiers(_task(), _outcome(), (), task_score=_score(0.75))
    assert scores.supervisor.applicable is True
    assert scores.supervisor.passed is False
    assert "0.750000" in scores.supervisor.detail


def test_supervisor_is_not_applicable_for_an_unscored_task() -> None:
    scores = score_tiers(_task(), _outcome(), (), task_score=_score(None))
    assert scores.supervisor == TierScore(tier="supervisor", applicable=False, passed=None)


# ------------------------------------------------------------------------------ subagent


@pytest.mark.parametrize("outcome", ["data", "no_match", "denied", "tool_failure"])
def test_subagent_is_met_when_the_observed_outcome_equals_the_expected(outcome: str) -> None:
    scores = score_tiers(
        _task(subagent={"outcome": outcome}), _outcome(outcome), (), task_score=_score(1.0)
    )
    assert scores.subagent.passed is True


def test_subagent_is_not_met_on_a_different_outcome() -> None:
    scores = score_tiers(
        _task(subagent={"outcome": "denied"}), _outcome("data"), (), task_score=_score(1.0)
    )
    assert scores.subagent.passed is False
    assert "'data'" in scores.subagent.detail and "'denied'" in scores.subagent.detail


def test_subagent_is_not_met_when_no_outcome_was_observed() -> None:
    scores = score_tiers(
        _task(subagent={"outcome": "data"}), _outcome(None), (), task_score=_score(1.0)
    )
    assert scores.subagent.applicable is True
    assert scores.subagent.passed is False


def test_subagent_null_is_not_applicable_even_when_an_outcome_was_observed() -> None:
    scores = score_tiers(_task(subagent=None), _outcome("data"), (), task_score=_score(1.0))
    assert scores.subagent == TierScore(tier="subagent", applicable=False, passed=None)


# ------------------------------------------------------------------------------ tool


def test_tool_is_met_by_an_expected_tool_with_valid_arguments() -> None:
    scores = score_tiers(
        _task(tool={"tools": ["get_opportunity"]}),
        _outcome(),
        (_VALID_GET,),
        task_score=_score(1.0),
    )
    assert scores.tool.passed is True


def test_tool_is_not_met_when_every_expected_call_has_invalid_arguments() -> None:
    scores = score_tiers(
        _task(tool={"tools": ["get_opportunity"]}),
        _outcome(),
        (_INVALID_GET,),
        task_score=_score(1.0),
    )
    assert scores.tool.passed is False
    assert "schema-invalid" in scores.tool.detail


def test_tool_is_met_when_one_of_several_expected_calls_is_valid() -> None:
    scores = score_tiers(
        _task(tool={"tools": ["get_opportunity"]}),
        _outcome(),
        (_INVALID_GET, _VALID_GET),
        task_score=_score(1.0),
    )
    assert scores.tool.passed is True


def test_tool_is_not_met_by_a_valid_call_to_an_unexpected_tool() -> None:
    scores = score_tiers(
        _task(tool={"tools": ["get_opportunity"]}),
        _outcome(),
        (_VALID_OTHER,),
        task_score=_score(1.0),
    )
    assert scores.tool.passed is False
    assert "was called" in scores.tool.detail


def test_tool_is_not_met_when_no_tool_was_called() -> None:
    scores = score_tiers(
        _task(tool={"tools": ["get_opportunity"]}), _outcome(), (), task_score=_score(1.0)
    )
    assert scores.tool.passed is False


def test_tool_rejects_an_unknown_argument_rather_than_ignoring_it() -> None:
    """`additionalProperties: false` is enforced, so an extra argument is schema-invalid."""
    extra = ObservedToolArguments(
        tool_name="get_opportunity", arguments={"opportunity_id": "o1", "verbose": True}
    )
    scores = score_tiers(
        _task(tool={"tools": ["get_opportunity"]}), _outcome(), (extra,), task_score=_score(1.0)
    )
    assert scores.tool.passed is False


def test_tool_null_is_not_applicable_even_when_tools_were_called() -> None:
    scores = score_tiers(_task(tool=None), _outcome(), (_VALID_GET,), task_score=_score(1.0))
    assert scores.tool == TierScore(tier="tool", applicable=False, passed=None)


# ------------------------------------------------------------------------------ report shape


def test_report_fields_carry_every_tier_in_order() -> None:
    scores = score_tiers(
        _task(subagent={"outcome": "data"}, tool={"tools": ["get_opportunity"]}),
        _outcome("data"),
        (_VALID_GET,),
        task_score=_score(None),
    )
    fields = scores.to_report_fields()
    assert list(fields) == ["task_id", *SCORED_TIERS]
    assert fields["supervisor"] == {"applicable": False, "passed": None, "detail": ""}
    assert fields["tool"] == {"applicable": True, "passed": True, "detail": ""}


def test_a_tier_score_refuses_a_pass_value_on_a_not_applicable_tier() -> None:
    with pytest.raises(ValueError, match="exactly when the tier is not applicable"):
        TierScore(tier="tool", applicable=False, passed=True)
    with pytest.raises(ValueError, match="exactly when the tier is not applicable"):
        TierScore(tier="tool", applicable=True, passed=None)


def test_a_tier_score_refuses_an_unknown_tier() -> None:
    with pytest.raises(ValueError, match="tier must be one of"):
        TierScore(tier="planner", applicable=False, passed=None)


# ------------------------------------------------------------------------------ malformed blocks


@pytest.mark.parametrize(
    ("task", "message"),
    [
        ({"id": "t"}, "must map exactly"),
        ({"id": "t", "expected_tiers": {"subagent": None}}, "must map exactly"),
        (
            {"id": "t", "expected_tiers": {"subagent": None, "tool": None, "supervisor": None}},
            "must map exactly",
        ),
        (
            {"id": "t", "expected_tiers": {"subagent": {"outcome": "ok"}, "tool": None}},
            "subagent.outcome must be one of",
        ),
        (
            {"id": "t", "expected_tiers": {"subagent": {"result": "data"}, "tool": None}},
            "subagent must be",
        ),
        (
            {"id": "t", "expected_tiers": {"subagent": None, "tool": {"tools": []}}},
            "tool must be",
        ),
        (
            {"id": "t", "expected_tiers": {"subagent": None, "tool": {"tools": ["get_nothing"]}}},
            "unknown tool",
        ),
    ],
)
def test_a_malformed_expected_tiers_block_raises(task: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        expected_tiers_of(task)
    with pytest.raises(ValueError, match=message):
        score_tiers(task, _outcome(), (), task_score=_score(1.0))


def test_expected_tiers_of_returns_the_parsed_expectations() -> None:
    task = _task(
        subagent={"outcome": "no_match"}, tool={"tools": ["get_opportunity", "get_account"]}
    )
    assert expected_tiers_of(task) == ("no_match", ("get_opportunity", "get_account"))
    assert expected_tiers_of(_task()) == (None, None)
