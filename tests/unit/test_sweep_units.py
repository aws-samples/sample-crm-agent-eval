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
    assert len(unit.usage_records) == 1
    assert unit.usage_records[0].usage.input_tokens == 20


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
    with pytest.raises(AssertionError, match="included_invocation_count"):
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
