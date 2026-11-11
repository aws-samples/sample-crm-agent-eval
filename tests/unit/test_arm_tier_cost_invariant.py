# SPDX-License-Identifier: MIT
"""Assert an arm's published all-tier cost total equals the sum of its per-tier cost totals.

The fixture is five 30K-token Haiku calls (150K together, each standard) and one 120K call (the
only long-context one), so pricing a summed group instead of each call breaks the equality, and
an arm figure scoped to the supervisor tier misses the tool tier's cost and long-context call.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from agents.tier_attribution import CacheCounts, InvocationAttribution, TokenCounts
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import harness
from eval.matrix import build_matrix_result, render_csv, render_json
from eval.metrics import (
    aggregate_tier_role_model,
    aggregate_tier_summary,
    not_measured_latency_metrics,
)
from eval.scoring import ObservedToolArguments, ToolCall, TurnOutcome
from eval.sweep_units import ArmSpec, assemble_arm, run_task_unit, usage_records_for_turn
from eval.turn_observation import ObservedTurn
from providers.capabilities import load_capability_matrix

MATRIX = load_capability_matrix()
HAIKU = MATRIX.model_for("anthropic", "tool").model_id
SUPERVISOR = MATRIX.model_for("anthropic", "supervisor").model_id


def _call(tier: str, role: str, model_id: str, input_tokens: int) -> InvocationAttribution:
    return InvocationAttribution(
        tier=tier,  # type: ignore[arg-type]
        role=role,
        model_id=model_id,
        tokens=TokenCounts(input_tokens=input_tokens, output_tokens=400),
        cache=CacheCounts(read_tokens=0, write_tokens=0),
    )


INVOCATIONS = (
    *(_call("tool", "research", HAIKU, 30_000) for _ in range(5)),
    _call("tool", "research", HAIKU, 120_000),
    _call("supervisor", "supervisor", SUPERVISOR, 8_000),
)


def test_the_fixture_tool_model_has_a_100k_long_context_threshold() -> None:
    """The fixture relies on the tool tier being Haiku 5.5, whose threshold sits at 100K."""
    assert HAIKU == "anthropic.claude-haiku-5-5"


def test_arm_cost_total_equals_the_sum_of_tier_cost_totals() -> None:
    """Σ per-call usage-record cost equals Σ tier_summary cost_total, to 1e-9."""
    records = usage_records_for_turn(INVOCATIONS, task_id="t-1", rep_id="rep-1", family="anthropic")
    arm_total = sum(record.cost.total for record in records)
    summary = aggregate_tier_summary(
        aggregate_tier_role_model(INVOCATIONS, withhold_cost=False),
        not_measured_latency_metrics(),
        [],
        withhold_cost=False,
    )
    tier_totals = [entry.cost_total for entry in summary.entries]
    assert all(total is not None for total in tier_totals)
    assert arm_total == pytest.approx(sum(t for t in tier_totals if t is not None), abs=1e-9)


def test_exactly_one_call_is_long_context_in_records_and_tier_summary() -> None:
    """Only the 120K call is long-context, in the usage records and in the tool tier's count."""
    records = usage_records_for_turn(INVOCATIONS, task_id="t-1", rep_id="rep-1", family="anthropic")
    assert len(records) == len(INVOCATIONS)
    long_records = [r for r in records if r.cost.pricing_tier == "long_context"]
    assert [r.usage.input_tokens for r in long_records] == [120_000]
    summary = aggregate_tier_summary(
        aggregate_tier_role_model(INVOCATIONS, withhold_cost=False),
        not_measured_latency_metrics(),
        [],
        withhold_cost=False,
    )
    counts = {entry.tier: entry.long_context_invocation_count for entry in summary.entries}
    assert counts == {"supervisor": 0, "subagent": 0, "tool": 1}
    tool_fields = summary.to_report_fields()["tool"]
    assert isinstance(tool_fields, dict)
    assert tool_fields["long_context_invocation_count"] == 1


_TENANT = "eval-arm-invariant"
_TIERS = ("supervisor", "subagent", "tool")


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _published_arm(store: Store) -> dict[str, Any]:
    """Run the fixture through a task unit and `assemble_arm`, and read the rendered arm back."""
    from eval.task_ids import localize_tasks

    task = localize_tasks(
        harness._load_golden_tasks(harness._DEFAULT_GOLDEN_TASKS_PATH), tenant=_TENANT
    )[0]
    turn = ObservedTurn(
        outcome=TurnOutcome(
            tenant=_TENANT,
            tool_calls=(ToolCall(tool_name="search_accounts"),),
            denial_outcome="data",
        ),
        tool_arguments=(ObservedToolArguments(tool_name="search_accounts", arguments={"q": "a"}),),
        invocations=INVOCATIONS,
        stream_calls=len(INVOCATIONS),
    )
    unit = run_task_unit(
        store=store, task=task, tenant=_TENANT, family="anthropic", turn_runner=lambda _t, _n: turn
    )
    arm = assemble_arm(
        [unit],
        matrix=MATRIX,
        arm_spec=ArmSpec(family="anthropic"),
        mode="stub",
        dataset_seed=1,
        git_revision="rev",
        runtime_identifier="test",
        price_table_version="1.3.0",
        cost_withheld=False,
        timestamp="2026-01-01T00:00:00+00:00",
    )
    matrix_result = build_matrix_result([arm.arm_result])
    (published,) = json.loads(render_json(matrix_result))["arms"]
    published["_csv"] = render_csv(matrix_result)
    assert isinstance(published, dict)
    return published


def test_the_published_arm_cost_total_equals_the_sum_of_the_published_tier_totals(
    store: Store,
) -> None:
    """matrix.json's arm_cost_total equals Σ tier_summary cost_total from the same artifact."""
    arm = _published_arm(store)
    tier_total = sum(arm["tier_summary"][tier]["cost_total"] for tier in _TIERS)
    assert arm["arm_cost_total"] == pytest.approx(tier_total, abs=1e-9)
    assert arm["arm_cost_total"] > arm["model_metrics"]["cost_total"]


def test_the_published_arm_long_context_count_covers_every_tier(store: Store) -> None:
    """The arm count includes the tool tier's long-context call; model_metrics stays supervisor."""
    arm = _published_arm(store)
    assert arm["arm_long_context_invocation_count"] == 1
    assert arm["arm_long_context_invocation_count"] == sum(
        arm["tier_summary"][tier]["long_context_invocation_count"] for tier in _TIERS
    )
    assert arm["model_metrics"]["long_context_invocation_count"] == 0
    keys = list(arm)
    assert keys.index("arm_cost_total") == keys.index("model_metrics") + 1


def test_the_csv_carries_the_same_arm_totals(store: Store) -> None:
    arm = _published_arm(store)
    row = next(csv.DictReader(io.StringIO(arm["_csv"])))
    assert float(row["arm_cost"]) == pytest.approx(arm["arm_cost_total"], abs=1e-6)
    assert row["arm_long_context_invocation_count"] == "1"
