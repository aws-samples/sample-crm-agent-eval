# SPDX-License-Identifier: MIT
"""`assemble_arm` carries `aggregate_tier_task_metrics` over its units as the arm's tier_task_metrics."""

from __future__ import annotations

from typing import Any

import pytest

from agents.tier_attribution import CacheCounts, InvocationAttribution, TokenCounts
from eval.matrix import MatrixResult, tier_comparison
from eval.metrics import ALL_TIERS, TaskTierObservation, aggregate_tier_task_metrics
from eval.scoring import TaskScore, TierScore, TierScores, TurnOutcome
from eval.sweep_units import (
    ArmSpec,
    TaskUnitResult,
    arm_turn_tuning,
    assemble_arm,
    usage_records_for_turn,
)
from eval.turn_observation import ObservedTurn
from providers.capabilities import Family, Tier, load_capability_matrix
from providers.tier_tuning import resolve_turn

MATRIX = load_capability_matrix()
TENANT = "eval-tier-task"
FAMILIES: tuple[Family, ...] = ("anthropic", "openai")
REP_ID = "rep-tier-task"


def _configured_efforts(family: Family) -> dict[Tier, str | None]:
    resolved = resolve_turn(MATRIX, arm_turn_tuning(ArmSpec(family=family)))
    return {tier: invocation.effort for tier, invocation in resolved.items()}


def _call(family: Family, tier: Tier, latency_ms: float | None) -> InvocationAttribution:
    return InvocationAttribution(
        tier=tier,
        role=tier,
        model_id=MATRIX.model_for(family, tier).model_id,
        tokens=TokenCounts(input_tokens=10, output_tokens=2),
        cache=CacheCounts(),
        latency_ms=latency_ms,
    )


def _score(tier: str, passed: bool | None) -> TierScore:
    return TierScore(tier=tier, applicable=passed is not None, passed=passed)


def _unit(
    family: Family,
    task_id: str,
    *,
    tiers: tuple[bool | None, bool | None, bool | None],
    calls: tuple[tuple[Tier, float | None], ...],
    outcome: str | None,
    resolved_efforts: dict[Tier, str | None] | None,
) -> TaskUnitResult:
    invocations = tuple(_call(family, tier, latency) for tier, latency in calls)
    turn = ObservedTurn(
        outcome=TurnOutcome(tenant=TENANT, tool_calls=(), denial_outcome=outcome),
        invocations=invocations,
        stream_calls=len(invocations),
        turn_latency_ms=sum(latency or 0.0 for _, latency in calls),
        resolved_efforts=resolved_efforts,
    )
    supervisor, subagent, tool = tiers
    return TaskUnitResult(
        task_id=task_id,
        task_score=TaskScore(
            task_id=task_id,
            scoring_mode="machine",
            correctness=1.0 if supervisor else 0.0,
            unscored_reason=None,
            bad_write=False,
            tool_call_correct=True,
        ),
        tier_scores=TierScores(
            task_id=task_id,
            supervisor=_score("supervisor", supervisor),
            subagent=_score("subagent", subagent),
            tool=_score("tool", tool),
        ),
        turn=turn,
        usage_records=usage_records_for_turn(
            invocations, task_id=task_id, rep_id=REP_ID, family=family
        ),
    )


def units_for(family: Family) -> list[TaskUnitResult]:
    """Four units with non-zero subagent/tool work, one task with no subagent call and one unrecorded latency."""
    efforts = _configured_efforts(family)
    return [
        _unit(
            family,
            "t1",
            tiers=(True, True, True),
            calls=(("supervisor", 100.0), ("subagent", 40.0), ("tool", 10.0), ("tool", 5.0)),
            outcome="data",
            resolved_efforts=efforts,
        ),
        _unit(
            family,
            "t2",
            tiers=(True, True, False),
            calls=(("supervisor", 300.0), ("tool", 20.0)),
            outcome="no_match",
            resolved_efforts=efforts,
        ),
        _unit(
            family,
            "t3",
            tiers=(False, None, True),
            calls=(("supervisor", 900.0), ("tool", None)),
            outcome="denied",
            resolved_efforts=None,
        ),
        _unit(
            family,
            "t4",
            tiers=(True, True, True),
            calls=(("supervisor", 200.0), ("subagent", 60.0), ("tool", 30.0)),
            outcome=None,
            resolved_efforts=efforts,
        ),
    ]


def assemble(units: list[TaskUnitResult], family: Family, mode: str, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "matrix": MATRIX,
        "arm_spec": ArmSpec(family=family),
        "mode": mode,
        "dataset_seed": 1,
        "git_revision": "rev",
        "runtime_identifier": "test",
        "price_table_version": "1.0.0",
        "cost_withheld": True,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    kwargs.update(overrides)
    return assemble_arm(units, **kwargs)


def _by_tier(fields: dict[str, object]) -> dict[str, dict[str, object]]:
    by_tier = fields["by_tier"]
    assert isinstance(by_tier, dict)
    return by_tier


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_assembled_arm_carries_the_aggregation_over_its_own_units(family: Family) -> None:
    units = units_for(family)
    arm = assemble(units, family, "live").arm_result
    expected = aggregate_tier_task_metrics(
        [
            TaskTierObservation(
                task_id=unit.task_id,
                tier_scores=unit.tier_scores,
                invocations=unit.turn.invocations,
                delegation_outcome=unit.turn.outcome.denial_outcome,
                resolved_efforts=unit.turn.resolved_efforts,
            )
            for unit in units
        ],
        measured=True,
        family=family,
        configured_model_ids={tier: MATRIX.model_for(family, tier).model_id for tier in ALL_TIERS},
    ).to_report_fields()
    assert arm.tier_task_metrics_fields == expected
    assert arm.to_report_fields()["tier_task_metrics"] == expected


def test_live_arm_figures_match_their_hand_computed_values() -> None:
    fields = assemble(units_for("anthropic"), "anthropic", "live").arm_result
    by_tier = _by_tier(fields.tier_task_metrics_fields)
    # Supervisor completed t1, t2, t4: sums [100, 300, 200] -> P50 rank 2 = 200, P95 rank 3 = 300.
    assert by_tier["supervisor"]["latency_sample_count"] == 3
    assert by_tier["supervisor"]["completed_task_latency_ms_p50"] == 200.0
    assert by_tier["supervisor"]["completed_task_latency_ms_p95"] == 300.0
    # Subagent completed t1, t2, t4; t2 made no subagent call: sums [40, 60].
    assert by_tier["subagent"]["latency_sample_count"] == 2
    assert by_tier["subagent"]["completed_task_latency_ms_p50"] == 40.0
    assert by_tier["subagent"]["completed_task_latency_ms_p95"] == 60.0
    # Tool completed t1, t3, t4; t3's only call is unrecorded: sums [15, 30].
    assert by_tier["tool"]["latency_sample_count"] == 2
    assert by_tier["tool"]["completed_task_latency_ms_p50"] == 15.0
    assert by_tier["tool"]["completed_task_latency_ms_p95"] == 30.0
    assert fields.tier_task_metrics_fields["delegation_outcomes"] == {
        "data": 1,
        "no_match": 1,
        "denied": 1,
        "tool_failure": 0,
        "none": 1,
    }


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_configured_model_ids_and_family_come_from_the_capability_matrix(family: Family) -> None:
    fields = assemble(units_for(family), family, "live").arm_result.tier_task_metrics_fields
    assert fields["family"] == family
    by_tier = _by_tier(fields)
    assert list(by_tier) == list(ALL_TIERS)
    for tier in ALL_TIERS:
        assert by_tier[tier]["configured_model_id"] == MATRIX.model_for(family, tier).model_id


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_observed_efforts_are_the_turns_resolved_tiers(family: Family) -> None:
    by_tier = _by_tier(
        assemble(units_for(family), family, "stub").arm_result.tier_task_metrics_fields
    )
    configured = _configured_efforts(family)
    for tier in ALL_TIERS:
        assert by_tier[tier]["efforts"] == [configured[tier]]


def test_stub_arm_is_not_measured_but_still_counts_outcomes() -> None:
    fields = assemble(units_for("anthropic"), "anthropic", "stub").arm_result
    by_tier = _by_tier(fields.tier_task_metrics_fields)
    for tier in ALL_TIERS:
        assert by_tier[tier]["latency_sample_count"] == 0
        assert by_tier[tier]["completed_task_latency_ms_p50"] is None
        assert by_tier[tier]["completed_task_latency_ms_p95"] is None
    outcomes = fields.tier_task_metrics_fields["delegation_outcomes"]
    assert isinstance(outcomes, dict)
    assert sum(outcomes.values()) == 4


def test_tier_comparison_over_assembled_arms_populates_every_tier_entry() -> None:
    arms = tuple(assemble(units_for(family), family, "live").arm_result for family in FAMILIES)
    summary = tier_comparison(MatrixResult(arms=arms))
    columns = summary["columns"]
    assert isinstance(columns, list)
    assert [column["family"] for column in columns] == list(FAMILIES)
    tiers = summary["tiers"]
    assert isinstance(tiers, dict)
    assert list(tiers) == list(ALL_TIERS)
    for tier in ALL_TIERS:
        entries = tiers[tier]
        assert len(entries) == 2
        for family, entry in zip(FAMILIES, entries, strict=True):
            assert MATRIX.model_for(family, tier).model_id in entry["model_ids"]
            assert entry["efforts"] == [_configured_efforts(family)[tier]]
            assert entry["latency_sample_count"] > 0
            assert entry["completed_task_latency_ms_p50"] is not None
            assert entry["completed_task_latency_ms_p95"] is not None
    assert tiers["subagent"][0]["delegation_outcomes"] == {
        "data": 1,
        "no_match": 1,
        "denied": 1,
        "tool_failure": 0,
        "none": 1,
    }
