# SPDX-License-Identifier: MIT
"""Tests for :mod:`eval.sweep_units`: the sweep grid, one task unit, and arm assembly."""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

import eval.sweep_units as sweep_units
from agents.tier_attribution import CacheCounts, InvocationAttribution, TokenCounts
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.user_settings import save_settings
from eval import harness
from eval.metrics import Exclusion, ExclusionCategory
from eval.scoring import ObservedToolArguments, ToolCall, TurnOutcome
from eval.sweep_units import (
    ArmSpec,
    TaskUnitResult,
    arm_turn_tuning,
    assemble_arm,
    run_task_unit,
    task_actor,
    task_refs,
    usage_records_for_turn,
)
from eval.turn_observation import ObservedTurn
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from providers.capabilities import load_capability_matrix
from providers.stub import StubProvider
from providers.tier_tuning import TierRequest, TurnTuning

MATRIX = load_capability_matrix()
TENANT = "eval-units"
_SRC = Path(sweep_units.__file__).resolve().parents[1]


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _golden(count: int) -> list[dict[str, object]]:
    from eval.task_ids import localize_tasks

    tasks = harness._load_golden_tasks(harness._DEFAULT_GOLDEN_TASKS_PATH)
    return localize_tasks(tasks, tenant=TENANT)[:count]


def _turn(
    *, stream_calls: int = 1, tokens: int = 10, tool: str = "search_accounts"
) -> ObservedTurn:
    return ObservedTurn(
        outcome=TurnOutcome(
            tenant=TENANT, tool_calls=(ToolCall(tool_name=tool),), denial_outcome="data"
        ),
        tool_arguments=(ObservedToolArguments(tool_name=tool, arguments={"q": "acme"}),),
        invocations=tuple(
            InvocationAttribution(
                tier="supervisor",
                role="supervisor",
                model_id=MATRIX.model_for("anthropic", "supervisor").model_id,
                tokens=TokenCounts(input_tokens=tokens, output_tokens=2),
                cache=CacheCounts(),
            )
            for _ in range(stream_calls)
        ),
        stream_calls=stream_calls,
    )


def _unit(store: Store, task: dict[str, object], turn: ObservedTurn) -> TaskUnitResult:
    return run_task_unit(
        store=store, task=task, tenant=TENANT, family="anthropic", turn_runner=lambda _t, _n: turn
    )


def _assemble(units: list[TaskUnitResult], **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "matrix": MATRIX,
        "arm_spec": ArmSpec(family="anthropic"),
        "mode": "stub",
        "dataset_seed": 1,
        "git_revision": "rev",
        "runtime_identifier": "test",
        "price_table_version": "1.0.0",
        "cost_withheld": True,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    kwargs.update(overrides)
    return assemble_arm(units, **kwargs)


# --- the grid ----------------------------------------------------------------------------------


def test_task_refs_is_arm_major_and_covers_every_cell() -> None:
    tasks = [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}]
    refs = task_refs(tasks, harness.DEFAULT_ARM_SPECS)
    assert [(ref.arm_index, ref.task_id) for ref in refs] == [
        (0, "t1"),
        (0, "t2"),
        (0, "t3"),
        (1, "t1"),
        (1, "t2"),
        (1, "t3"),
    ]


def test_the_default_sweep_is_one_cell_per_arm_and_golden_task() -> None:
    golden = harness._load_golden_tasks(harness._DEFAULT_GOLDEN_TASKS_PATH)
    assert len(harness.DEFAULT_ARM_SPECS) == 2
    assert len(task_refs(_golden(1000), harness.DEFAULT_ARM_SPECS)) == 2 * len(golden)


def test_task_actor_refuses_a_task_without_an_actor() -> None:
    with pytest.raises(ValueError, match="actor"):
        task_actor({"id": "t"})


# --- arm tuning --------------------------------------------------------------------------------


def test_arm_tuning_runs_the_family_everywhere_with_effort_on_the_supervisor() -> None:
    tuning = arm_turn_tuning(ArmSpec(family="openai", effort="low"))
    assert tuning.tiers == {
        "supervisor": TierRequest(family="openai", effort="low"),
        "subagent": TierRequest(family="openai"),
        "tool": TierRequest(family="openai"),
    }


def test_no_eval_module_reads_user_settings() -> None:
    """The eval path imports nothing from ``crm.user_settings``."""
    for name in ("sweep_units.py", "turn_observation.py", "harness.py"):
        tree = ast.parse((_SRC / "eval" / name).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "crm.user_settings" not in imported, name


class _RecordingStub(ModelProvider):
    def __init__(self) -> None:
        self.inner = StubProvider()
        self.models: list[str] = []

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.models.append(invocation.model.model_id)
        return self.inner.stream(invocation, messages, tools)


def _arm_models(store: Store, task: dict[str, object]) -> list[str]:
    provider = _RecordingStub()
    harness._run_arm(
        provider=provider,
        matrix=MATRIX,
        arm_spec=ArmSpec(family="anthropic", effort="low"),
        tasks=[task],
        store=store,
        tenant=TENANT,
        mode="stub",
        dataset_seed=1,
        git_revision="rev",
        runtime_identifier="test",
        price_table_version="1.0.0",
        cost_withheld=True,
    )
    return provider.models


def test_a_saved_user_setting_does_not_change_an_eval_turn(store: Store) -> None:
    """The actor's saved per-tier tuning is ignored; the arm's tuning runs."""
    task = _golden(1)[0]
    before = _arm_models(store, task)
    identity = harness._agent_identity_for_actor(task_actor(task), tenant=TENANT)
    save_settings(
        store,
        identity,
        TurnTuning(
            source="settings.tuning",
            tiers={
                "supervisor": TierRequest(family="anthropic", model="anthropic.claude-sonnet-5-5")
            },
        ),
        matrix=MATRIX,
    )
    after = _arm_models(store, task)
    assert before == after
    assert set(after) == {MATRIX.model_for("anthropic", "supervisor").model_id}


# --- one task unit -----------------------------------------------------------------------------


def test_run_task_unit_scores_meters_and_tier_scores_one_turn(store: Store) -> None:
    task = _golden(1)[0]
    seen: list[Mapping[str, object]] = []

    def runner(runner_task: Mapping[str, object], tenant: str) -> ObservedTurn:
        seen.append(runner_task)
        assert tenant == TENANT
        return _turn(stream_calls=2)

    unit = run_task_unit(
        store=store, task=task, tenant=TENANT, family="anthropic", turn_runner=runner
    )
    assert seen == [task]
    assert unit.task_id == task["id"]
    assert unit.task_score.task_id == task["id"]
    assert unit.tier_scores.task_id == task["id"]
    assert len(unit.usage_records) == 2
    assert [record.usage.input_tokens for record in unit.usage_records] == [10, 10]


def test_a_task_without_expected_tiers_is_not_applicable_at_every_tier(store: Store) -> None:
    task = {k: v for k, v in _golden(1)[0].items() if k != "expected_tiers"}
    scores = _unit(store, task, _turn()).tier_scores.by_tier()
    assert all(not score.applicable and score.passed is None for score in scores.values())


def test_a_malformed_expected_tiers_block_still_raises(store: Store) -> None:
    task = {**_golden(1)[0], "expected_tiers": {"tool": None}}
    with pytest.raises(ValueError, match="expected_tiers"):
        _unit(store, task, _turn())


def test_run_task_unit_refuses_a_task_without_an_actor(store: Store) -> None:
    task = {k: v for k, v in _golden(1)[0].items() if k != "actor"}
    with pytest.raises(ValueError, match="actor"):
        _unit(store, task, _turn())


# --- arm assembly ------------------------------------------------------------------------------


def test_assemble_arm_reports_a_tier_summary_for_every_tier(store: Store) -> None:
    tasks = _golden(2)
    arm = _assemble([_unit(store, task, _turn()) for task in tasks])
    summary = arm.arm_result.tier_summary_fields
    assert list(summary) == ["supervisor", "subagent", "tool"]
    assert summary["supervisor"]["input_tokens"] == 20
    assert summary["supervisor"]["cost_total"] is None


def test_assemble_arm_refuses_a_stream_call_mismatch(store: Store) -> None:
    unit = _unit(store, _golden(1)[0], _turn(stream_calls=1))
    with pytest.raises(AssertionError, match="its turn drivers made"):
        _assemble([unit], provider_stream_calls=2)
    _assemble([unit], provider_stream_calls=1)


def test_assemble_arm_without_an_arm_counter_checks_against_the_units(store: Store) -> None:
    """An out-of-process arm still refuses more metered invocations than its turns' stream calls."""
    overcounted = ObservedTurn(
        outcome=_turn().outcome,
        tool_arguments=_turn().tool_arguments,
        invocations=_turn(stream_calls=2).invocations,
        stream_calls=0,
    )
    with pytest.raises(AssertionError, match="double-recorded usage"):
        _assemble([_unit(store, _golden(1)[0], overcounted)])


def test_assemble_arm_counts_unit_and_residual_exclusions(store: Store) -> None:
    turn = ObservedTurn(
        outcome=_turn().outcome,
        invocations=_turn().invocations,
        stream_calls=1,
        exclusions=(Exclusion("inv-1", ExclusionCategory.THROTTLE),),
    )
    arm = _assemble(
        [_unit(store, _golden(1)[0], turn)],
        residual_exclusions=[Exclusion("inv-2", ExclusionCategory.RETRY)],
    )
    exclusions = arm.arm_result.model_metrics_fields["exclusions"]
    assert exclusions["by_category"]["throttle"] == 1
    assert exclusions["by_category"]["retry"] == 1


def test_assemble_arm_takes_latency_only_from_live_turns(store: Store) -> None:
    timed = ObservedTurn(
        outcome=_turn().outcome,
        invocations=_turn().invocations,
        stream_calls=1,
        turn_latency_ms=900.0,
    )
    units = [_unit(store, _golden(1)[0], timed)]
    assert _assemble(units, mode="stub").model_metrics.latency.measured is False
    live = _assemble(units, mode="live").model_metrics.latency
    assert live.measured is True and live.task_p50_ms == pytest.approx(900.0)


# --- per-call pricing --------------------------------------------------------------------------


def _haiku_call(input_tokens: int) -> InvocationAttribution:
    return InvocationAttribution(
        tier="tool",
        role="research",
        model_id=MATRIX.model_for("anthropic", "tool").model_id,
        tokens=TokenCounts(input_tokens=input_tokens, output_tokens=5),
        cache=CacheCounts(),
    )


def test_usage_records_for_turn_prices_each_attribution_on_its_own() -> None:
    """Two 60K calls stay standard; summed they would cross Haiku's 100K threshold."""
    records = usage_records_for_turn(
        (_haiku_call(60_000), _haiku_call(60_000)),
        task_id="t-1",
        rep_id="rep-1",
        family="anthropic",
    )
    assert [r.usage.input_tokens for r in records] == [60_000, 60_000]
    assert [r.cost.pricing_tier for r in records] == ["standard", "standard"]
    assert {r.cost.route for r in records} == {"global"}
    assert len({r.invocation_id for r in records}) == 2


def test_usage_records_for_turn_prices_under_the_configured_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record's route follows the model's configured inference profile."""
    monkeypatch.setattr(sweep_units, "inference_profile_for", lambda model_id: f"eu.{model_id}")
    (record,) = usage_records_for_turn(
        (_haiku_call(10_000),), task_id="t-1", rep_id="rep-1", family="anthropic"
    )
    assert record.cost.route == "geo"
    assert record.cost.input == pytest.approx(10_000 / 1e6 * 0.10 * 1.10)


def test_assemble_arm_reports_long_context_counts_per_tier_and_row(store: Store) -> None:
    """The tool tier's summary and its row carry the count of long-context calls."""
    base = _turn()
    turn = ObservedTurn(
        outcome=base.outcome,
        tool_arguments=base.tool_arguments,
        invocations=(*base.invocations, _haiku_call(30_000), _haiku_call(100_001)),
        stream_calls=3,
    )
    arm = _assemble([_unit(store, _golden(1)[0], turn)])
    summary = arm.arm_result.tier_summary_fields
    assert summary["tool"]["long_context_invocation_count"] == 1
    assert summary["supervisor"]["long_context_invocation_count"] == 0
    assert arm.arm_result.model_metrics_fields["long_context_invocation_count"] == 0


# --- attribution bound -------------------------------------------------------------------------


def _delegating_turn(*, duplicate_supervisor: bool) -> ObservedTurn:
    """Two supervisor rounds delegating to a subagent round and a tool call: four stream calls."""
    base = _turn(stream_calls=2)
    subagent = InvocationAttribution(
        tier="subagent",
        role="research",
        model_id=MATRIX.model_for("anthropic", "subagent").model_id,
        tokens=TokenCounts(input_tokens=40, output_tokens=4),
        cache=CacheCounts(),
    )
    supervisor = base.invocations[:1] * (2 if duplicate_supervisor else 1)
    return ObservedTurn(
        outcome=base.outcome,
        tool_arguments=base.tool_arguments,
        invocations=(*supervisor, *base.invocations[1:], subagent, _haiku_call(500)),
        stream_calls=4,
    )


def test_a_delegating_turn_with_every_call_attributed_once_assembles(store: Store) -> None:
    turn = _delegating_turn(duplicate_supervisor=False)
    assert len(turn.invocations) == turn.stream_calls == 4
    arm = _assemble([_unit(store, _golden(1)[0], turn)], provider_stream_calls=4)
    assert arm.arm_result.model_metrics_fields["included_invocation_count"] == 2


def test_a_duplicated_supervisor_attribution_cannot_hide_under_delegated_calls(
    store: Store,
) -> None:
    """One extra supervisor usage on a delegating turn: 3 supervisor records against 4 calls
    passed the old supervisor-only bound, and the all-tier bound refuses it."""
    turn = _delegating_turn(duplicate_supervisor=True)
    supervisor_records = sum(1 for a in turn.invocations if a.tier == "supervisor")
    assert supervisor_records < turn.stream_calls < len(turn.invocations)
    with pytest.raises(AssertionError, match="task unit .* double-recorded usage"):
        _assemble([_unit(store, _golden(1)[0], turn)], provider_stream_calls=4)


def test_the_bound_is_per_unit_so_another_turns_slack_cannot_absorb_a_surplus(
    store: Store,
) -> None:
    """A turn with a usage-less call does not cover a duplicate recorded in a different turn."""
    tasks = _golden(2)
    surplus = _delegating_turn(duplicate_supervisor=True)
    slack = ObservedTurn(
        outcome=_turn().outcome,
        tool_arguments=_turn().tool_arguments,
        invocations=_turn().invocations,
        stream_calls=2,
    )
    units = [_unit(store, tasks[0], surplus), _unit(store, tasks[1], slack)]
    assert sum(len(u.turn.invocations) for u in units) == sum(u.turn.stream_calls for u in units)
    with pytest.raises(AssertionError, match="task unit .* double-recorded usage"):
        _assemble(units, provider_stream_calls=6)
