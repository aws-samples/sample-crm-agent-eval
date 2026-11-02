# SPDX-License-Identifier: MIT
"""Each tier's resolved effort on the observed turn, its wire key and its `eval_results` row."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from eval.scoring import TaskScore, TierScore, TierScores, TurnOutcome
from eval.sfn_handlers import _unit_fields, turn_from_row
from eval.sweep_units import ArmSpec, TaskUnitResult, arm_turn_tuning
from eval.turn_observation import (
    RESOLVED_TIERS_KEY,
    ObservedTurn,
    deserialise_turn,
    observe_turn,
    serialise_turn,
)
from providers.capabilities import Family, Tier, load_capability_matrix
from providers.stub import StubProvider
from providers.tier_tuning import resolve_turn

MATRIX = load_capability_matrix()
ALL_TIERS: tuple[Tier, ...] = ("supervisor", "subagent", "tool")


def _stub_turn(family: Family) -> ObservedTurn:
    return asyncio.run(
        observe_turn(
            prompt="which accounts slipped?",
            task_id="gt-unit",
            tenant="eval-unit",
            providers=lambda _family: StubProvider(),
            matrix=MATRIX,
            tuning=arm_turn_tuning(ArmSpec(family=family)),
            dispatch=lambda tool, _input: {"statusCode": 200, "body": {"tool": tool.name}},
        )
    )


def _configured_efforts(family: Family) -> dict[Tier, str | None]:
    resolved = resolve_turn(MATRIX, arm_turn_tuning(ArmSpec(family=family)))
    return {tier: invocation.effort for tier, invocation in resolved.items()}


def _plain_turn(resolved_efforts: dict[Tier, str | None] | None) -> ObservedTurn:
    return ObservedTurn(
        outcome=TurnOutcome(tenant="eval-abc", tool_calls=()),
        resolved_efforts=resolved_efforts,
    )


def _unit(turn: ObservedTurn) -> TaskUnitResult:
    tier = TierScore(tier="supervisor", applicable=False, passed=None)
    return TaskUnitResult(
        task_id="gt-unit",
        task_score=TaskScore(
            task_id="gt-unit",
            scoring_mode="machine",
            correctness=1.0,
            unscored_reason=None,
            bad_write=False,
            tool_call_correct=True,
        ),
        tier_scores=TierScores(
            task_id="gt-unit",
            supervisor=tier,
            subagent=TierScore(tier="subagent", applicable=False, passed=None),
            tool=TierScore(tier="tool", applicable=False, passed=None),
        ),
        turn=turn,
        usage_records=(),
    )


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_a_stub_turn_records_the_effort_every_tier_resolved_to(family: Family) -> None:
    turn = _stub_turn(family)
    assert turn.resolved_efforts is not None
    assert set(turn.resolved_efforts) == set(ALL_TIERS)
    assert dict(turn.resolved_efforts) == _configured_efforts(family)


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_a_stub_turn_round_trips_its_efforts_through_strict_json(family: Family) -> None:
    turn = _stub_turn(family)
    wire = json.loads(json.dumps(serialise_turn(turn), allow_nan=False))
    assert wire[RESOLVED_TIERS_KEY] == dict(turn.resolved_efforts or {})
    assert deserialise_turn(wire) == turn


def test_a_tier_without_effort_control_round_trips_as_null() -> None:
    turn = _plain_turn({"supervisor": "medium", "subagent": None, "tool": "low"})
    wire = json.loads(json.dumps(serialise_turn(turn)))
    assert wire[RESOLVED_TIERS_KEY]["subagent"] is None
    assert deserialise_turn(wire) == turn


def test_a_turn_without_efforts_omits_the_key() -> None:
    assert RESOLVED_TIERS_KEY not in serialise_turn(_plain_turn(None))


def test_a_wire_turn_from_a_runtime_without_the_key_still_deserialises() -> None:
    wire = serialise_turn(_stub_turn("anthropic"))
    del wire[RESOLVED_TIERS_KEY]
    assert deserialise_turn(wire).resolved_efforts is None


@pytest.mark.parametrize(
    "value",
    [
        ["supervisor", "medium"],
        {"orchestrator": "medium"},
        {"supervisor": 3},
        {"supervisor": True},
    ],
    ids=lambda value: json.dumps(value),
)
def test_a_malformed_resolved_tiers_object_is_refused(value: object) -> None:
    wire = serialise_turn(_plain_turn({"supervisor": "medium"}))
    wire[RESOLVED_TIERS_KEY] = value
    with pytest.raises(ValueError, match=RESOLVED_TIERS_KEY):
        deserialise_turn(wire)


def _stored_row(turn: ObservedTurn) -> dict[str, Any]:
    """The unit's fields as DynamoDB returns them: the same attributes, round-tripped as JSON."""
    row: dict[str, Any] = json.loads(json.dumps(_unit_fields(_unit(turn))))
    return row


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_a_stored_row_rebuilds_the_turn_with_its_efforts(family: Family) -> None:
    turn = _stub_turn(family)
    row = _stored_row(turn)
    assert json.loads(row["resolved_efforts"]) == dict(turn.resolved_efforts or {})
    assert turn_from_row(row) == turn


def test_a_row_without_the_attribute_rebuilds_as_unrecorded_and_differs_from_the_turn() -> None:
    """Control: a store that drops the attribute loses the efforts, which the round-trip sees."""
    turn = _stub_turn("anthropic")
    row = _stored_row(turn)
    del row["resolved_efforts"]
    rebuilt = turn_from_row(row)
    assert rebuilt.resolved_efforts is None
    assert rebuilt != turn


def test_a_turn_without_efforts_stores_no_attribute() -> None:
    row = _stored_row(_plain_turn(None))
    assert "resolved_efforts" not in row
    assert turn_from_row(row).resolved_efforts is None
