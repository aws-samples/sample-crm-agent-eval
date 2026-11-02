# SPDX-License-Identifier: MIT
"""Per-tier completed-task latency, observed efforts and delegation counts (aggregate_tier_task_metrics)."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from agents.supervisor import SUPERVISOR_ROLE as AGENTS_SUPERVISOR_ROLE
from agents.tier_attribution import (
    CacheCounts,
    InvocationAttribution,
    OutcomeKind,
    TokenCounts,
    ToolCallAttribution,
)
from crm.metering import Tier
from eval.metrics import (
    ALL_TIERS,
    DELEGATION_OUTCOME_NONE,
    SUPERVISOR_ROLE,
    TaskTierObservation,
    aggregate_tier_task_metrics,
    nearest_rank_percentile,
    retried_failure_positions,
)
from eval.scoring import TierScore, TierScores

_CONFIGURED: dict[Tier, str] = {
    "supervisor": "anthropic.sup-model",
    "subagent": "anthropic.sub-model",
    "tool": "anthropic.tool-model",
}


def _score(tier: str, passed: bool | None) -> TierScore:
    return TierScore(tier=tier, applicable=passed is not None, passed=passed)


def _call(tier: Tier, latency_ms: float | None) -> InvocationAttribution:
    return InvocationAttribution(
        tier=tier,
        role=f"{tier}-role",
        model_id=f"model-{tier}",
        tokens=TokenCounts(input_tokens=1, output_tokens=1),
        cache=CacheCounts(),
        latency_ms=latency_ms,
    )


def _obs(
    task_id: str,
    *,
    supervisor: bool | None = None,
    subagent: bool | None = None,
    tool: bool | None = None,
    calls: tuple[InvocationAttribution, ...] = (),
    outcome: str | None = None,
    efforts: Mapping[Tier, str | None] | None = None,
    tool_calls: tuple[ToolCallAttribution, ...] = (),
) -> TaskTierObservation:
    return TaskTierObservation(
        task_id=task_id,
        tier_scores=TierScores(
            task_id=task_id,
            supervisor=_score("supervisor", supervisor),
            subagent=_score("subagent", subagent),
            tool=_score("tool", tool),
        ),
        invocations=calls,
        delegation_outcome=outcome,
        resolved_efforts=efforts,
        tool_calls=tool_calls,
    )


def _by_tier(
    observations: list[TaskTierObservation], *, measured: bool = True
) -> dict[str, object]:
    fields = aggregate_tier_task_metrics(
        observations, measured=measured, family="anthropic", configured_model_ids=_CONFIGURED
    ).to_report_fields()
    by_tier = fields["by_tier"]
    assert isinstance(by_tier, dict)
    return by_tier


def _tier(observations: list[TaskTierObservation], tier: Tier, **kwargs: bool) -> dict[str, object]:
    entry = _by_tier(observations, **kwargs)[tier]
    assert isinstance(entry, dict)
    return entry


def test_per_task_sum_and_nearest_rank_over_completed_tasks_only() -> None:
    observations = [
        _obs("t1", supervisor=True, calls=(_call("supervisor", 100.0), _call("supervisor", 200.0))),
        _obs("t2", supervisor=True, calls=(_call("supervisor", 100.0),)),
        _obs("t3", supervisor=True, calls=(_call("supervisor", 500.0),)),
        _obs("t4", supervisor=True, calls=(_call("supervisor", 150.0), _call("supervisor", 250.0))),
        _obs("t5", supervisor=True, calls=(_call("supervisor", 200.0),)),
        _obs("failed", supervisor=False, calls=(_call("supervisor", 9000.0),)),
        _obs("not-applicable", calls=(_call("supervisor", 8000.0),)),
    ]
    entry = _tier(observations, "supervisor")
    # Per-task sums of completed tasks: [300, 100, 500, 400, 200] -> sorted [100, 200, 300, 400, 500].
    # P50 rank ceil(0.5*5)=3 -> 300; P95 rank ceil(0.95*5)=5 -> 500.
    assert entry["latency_sample_count"] == 5
    assert entry["completed_task_latency_ms_p50"] == 300.0
    assert entry["completed_task_latency_ms_p95"] == 500.0


def test_only_calls_at_the_tier_count_toward_that_tiers_sum() -> None:
    observations = [
        _obs("t1", subagent=True, calls=(_call("subagent", 40.0), _call("supervisor", 999.0))),
        _obs("t2", subagent=True, calls=(_call("subagent", 10.0), _call("tool", 999.0))),
        _obs("t3", subagent=True, calls=(_call("subagent", 30.0), _call("subagent", 30.0))),
    ]
    entry = _tier(observations, "subagent")
    # Sums [40, 10, 60] -> sorted [10, 40, 60]; P50 rank 2 -> 40; P95 rank 3 -> 60.
    assert entry["latency_sample_count"] == 3
    assert entry["completed_task_latency_ms_p50"] == 40.0
    assert entry["completed_task_latency_ms_p95"] == 60.0


def test_completed_task_without_a_call_at_the_tier_contributes_no_sample() -> None:
    observations = [
        _obs("t1", subagent=True, calls=(_call("subagent", 100.0),)),
        _obs("t2", subagent=True, calls=(_call("subagent", 200.0),)),
        _obs("t3", subagent=True, calls=(_call("subagent", 300.0),)),
        _obs("direct-read", subagent=True, calls=(_call("supervisor", 50.0),)),
    ]
    entry = _tier(observations, "subagent")
    assert entry["latency_sample_count"] == 3
    assert entry["completed_task_latency_ms_p50"] == 200.0

    zero_counting_variant = [0.0, 100.0, 200.0, 300.0]
    assert (
        nearest_rank_percentile(zero_counting_variant, 50) != entry["completed_task_latency_ms_p50"]
    )
    assert len(zero_counting_variant) != entry["latency_sample_count"]


def test_unrecorded_call_latency_is_skipped_within_a_task() -> None:
    observations = [
        _obs("t1", tool=True, calls=(_call("tool", 70.0), _call("tool", None))),
        _obs("t2", tool=True, calls=(_call("tool", None),)),
    ]
    entry = _tier(observations, "tool")
    assert entry["latency_sample_count"] == 1
    assert entry["completed_task_latency_ms_p50"] == 70.0
    assert entry["completed_task_latency_ms_p95"] == 70.0


def test_unmeasured_arm_reports_no_latency_even_with_samples() -> None:
    observations = [
        _obs(
            "t1",
            supervisor=True,
            subagent=True,
            tool=True,
            calls=tuple(_call(tier, 100.0) for tier in ALL_TIERS),
        ),
    ]
    for tier in ALL_TIERS:
        entry = _tier(observations, tier, measured=False)
        assert entry["latency_sample_count"] == 0
        assert entry["completed_task_latency_ms_p50"] is None
        assert entry["completed_task_latency_ms_p95"] is None


def test_tier_with_no_completed_task_reports_none_at_count_zero() -> None:
    entry = _tier([_obs("t1", tool=False, calls=(_call("tool", 10.0),))], "tool")
    assert entry == {
        "configured_model_id": "anthropic.tool-model",
        "efforts": [],
        "latency_sample_count": 0,
        "completed_task_latency_ms_p50": None,
        "completed_task_latency_ms_p95": None,
        "retry_count": 0,
    }


def test_delegation_outcomes_cover_every_task_with_a_none_bucket() -> None:
    outcomes = ["data", "data", "denied", "no_match", "tool_failure", None, None]
    observations = [_obs(f"t{i}", outcome=outcome) for i, outcome in enumerate(outcomes)]
    fields = aggregate_tier_task_metrics(
        observations, measured=True, family="openai", configured_model_ids=_CONFIGURED
    ).to_report_fields()
    counts = fields["delegation_outcomes"]
    assert counts == {"data": 2, "no_match": 1, "denied": 1, "tool_failure": 1, "none": 2}
    assert isinstance(counts, dict)
    assert list(counts) == ["data", "no_match", "denied", "tool_failure", DELEGATION_OUTCOME_NONE]
    assert sum(counts.values()) == len(observations)


def test_delegation_outcomes_present_at_zero_with_no_observation() -> None:
    fields = aggregate_tier_task_metrics(
        [], measured=True, family="anthropic", configured_model_ids=_CONFIGURED
    ).to_report_fields()
    assert fields["delegation_outcomes"] == {
        "data": 0,
        "no_match": 0,
        "denied": 0,
        "tool_failure": 0,
        "none": 0,
    }


def test_unknown_delegation_outcome_raises() -> None:
    with pytest.raises(ValueError, match="'escalated'"):
        aggregate_tier_task_metrics(
            [_obs("t1", outcome="escalated")],
            measured=True,
            family="anthropic",
            configured_model_ids=_CONFIGURED,
        )


def test_missing_configured_tier_raises() -> None:
    partial: dict[Tier, str] = {"supervisor": "a", "subagent": "b"}
    with pytest.raises(ValueError, match="tool"):
        aggregate_tier_task_metrics(
            [], measured=True, family="anthropic", configured_model_ids=partial
        )


def test_efforts_are_distinct_sorted_with_none_last_and_independent_of_measured() -> None:
    observations = [
        _obs("t1", efforts={"supervisor": "medium", "subagent": "low", "tool": None}),
        _obs("t2", efforts={"supervisor": "high", "subagent": "low", "tool": None}),
        _obs("t3", efforts={"supervisor": "medium", "subagent": None}),
        _obs("archived", efforts=None),
    ]
    by_tier = _by_tier(observations, measured=False)
    efforts = {tier: entry["efforts"] for tier, entry in by_tier.items() if isinstance(entry, dict)}
    assert efforts == {
        "supervisor": ["high", "medium"],
        "subagent": ["low", None],
        "tool": [None],
    }


def test_efforts_empty_when_no_turn_recorded_resolved_tiers() -> None:
    by_tier = _by_tier([_obs("t1"), _obs("t2")])
    for tier in ALL_TIERS:
        entry = by_tier[tier]
        assert isinstance(entry, dict)
        assert entry["efforts"] == []


def test_report_fields_key_order() -> None:
    fields = aggregate_tier_task_metrics(
        [_obs("t1", supervisor=True, calls=(_call("supervisor", 5.0),))],
        measured=True,
        family="anthropic",
        configured_model_ids=_CONFIGURED,
    ).to_report_fields()
    assert list(fields) == ["family", "by_tier", "delegation_outcomes"]
    assert fields["family"] == "anthropic"
    by_tier = fields["by_tier"]
    assert isinstance(by_tier, dict)
    assert list(by_tier) == ["supervisor", "subagent", "tool"]
    for tier in ALL_TIERS:
        entry = by_tier[tier]
        assert isinstance(entry, dict)
        assert list(entry) == [
            "configured_model_id",
            "efforts",
            "latency_sample_count",
            "completed_task_latency_ms_p50",
            "completed_task_latency_ms_p95",
            "retry_count",
        ]
        assert entry["configured_model_id"] == _CONFIGURED[tier]


def test_all_tiers_matches_report_order() -> None:
    assert ALL_TIERS == ("supervisor", "subagent", "tool")


def _tc(tool_name: str, role: str, kind: OutcomeKind) -> ToolCallAttribution:
    return ToolCallAttribution(
        tool_name=tool_name, role=role, latency_ms=None, outcome_kind=kind, truncated=False
    )


@pytest.mark.parametrize(
    ("calls", "expected"),
    [
        ((("get", "research", "tool_failure"), ("get", "research", "data")), {0}),
        ((("get", "research", "tool_failure"), ("get", "research", "no_match")), {0}),
        ((("get", "research", "tool_failure"),), set()),
        ((("get", "research", "tool_failure"), ("list", "research", "data")), set()),
        ((("get", "research", "tool_failure"), ("get", "mutation", "data")), set()),
        ((("get", "research", "data"), ("get", "research", "tool_failure")), set()),
        ((("get", "research", "denied"), ("get", "research", "data")), set()),
        ((("get", "research", "tool_failure"), ("get", "research", "tool_failure")), set()),
        (
            (
                ("get", "research", "tool_failure"),
                ("get", "research", "tool_failure"),
                ("get", "research", "data"),
            ),
            {0, 1},
        ),
    ],
    ids=[
        "retried-into-data",
        "retried-into-no_match",
        "never-retried",
        "different-tool",
        "different-role",
        "failure-after-data",
        "denied-is-never-a-retry",
        "retried-into-another-failure",
        "two-failures-one-retry",
    ],
)
def test_retried_failure_positions_follow_the_owner_rule(
    calls: tuple[tuple[str, str, OutcomeKind], ...], expected: set[int]
) -> None:
    attributed = tuple(_tc(name, role, kind) for name, role, kind in calls)
    assert retried_failure_positions(attributed) == frozenset(expected)


def test_retry_count_is_reported_per_tier_by_dispatching_role() -> None:
    supervisor_retry = (
        _tc("get", SUPERVISOR_ROLE, "tool_failure"),
        _tc("get", SUPERVISOR_ROLE, "data"),
    )
    subagent_retries = (
        _tc("get", "research", "tool_failure"),
        _tc("get", "research", "data"),
        _tc("update", "mutation", "tool_failure"),
        _tc("update", "mutation", "no_match"),
    )
    unretried = (_tc("get", "research", "tool_failure"), _tc("list", "research", "data"))
    by_tier = _by_tier(
        [
            _obs("t1", tool_calls=supervisor_retry),
            _obs("t2", tool_calls=subagent_retries),
            _obs("t3", tool_calls=unretried),
        ],
        measured=False,
    )
    counts = {
        tier: entry["retry_count"] for tier, entry in by_tier.items() if isinstance(entry, dict)
    }
    assert counts == {"supervisor": 1, "subagent": 2, "tool": 0}


def test_retry_count_is_zero_without_tool_calls() -> None:
    by_tier = _by_tier([_obs("t1"), _obs("t2")])
    for tier in ALL_TIERS:
        entry = by_tier[tier]
        assert isinstance(entry, dict)
        assert entry["retry_count"] == 0


def test_the_supervisor_role_matches_the_agents_constant() -> None:
    assert SUPERVISOR_ROLE == AGENTS_SUPERVISOR_ROLE
