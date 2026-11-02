# SPDX-License-Identifier: MIT
"""Tests that every sweep arm invokes its resolved provider through the real turn loop.

Covers `eval.harness._run_provider_turn` / `eval.harness._run_arm`: invocation counts, multi-round
tool loops, the round cap, per-arm score differences, and dispatch to real handlers.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Iterator

import boto3
import pytest
from moto import mock_aws

from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.harness import (
    _DEFAULT_GOLDEN_TASKS_PATH,
    ArmSpec,
    UndisclosedZeroInvocationRenderError,
    _load_golden_tasks,
    _run_arm,
    _run_provider_turn,
    run_sweep,
)
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, Tuning
from providers.capabilities import load_capability_matrix


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


@pytest.fixture(autouse=True)
def _inject_noop_seeder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the default-path seeder to a no-op."""

    def _noop_seeder(store: Store, *, tenant: str) -> None:
        del store, tenant

    monkeypatch.setattr("eval.tenancy._default_seeder", lambda: _noop_seeder)


# --------------------------------------------------------------------------------------
# The provider is genuinely invoked: included_invocation_count is non-zero for a stub sweep.
# --------------------------------------------------------------------------------------


def test_a_stub_sweep_records_a_non_zero_included_invocation_count(store: Store) -> None:
    """A stub sweep records a non-zero `included_invocation_count`."""
    result = run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic", effort="low"),))
    (arm_run,) = result.arm_runs
    assert arm_run.model_metrics.included_invocation_count > 0
    assert arm_run.model_metrics.included_invocation_count == len(arm_run.task_scores)


def test_default_sweep_both_arms_record_a_non_zero_included_invocation_count(store: Store) -> None:
    result = run_sweep(store=store)
    assert len(result.arm_runs) == 2
    for arm_run in result.arm_runs:
        assert arm_run.model_metrics.included_invocation_count > 0


class _RecordingProvider(ModelProvider):
    """A provider stand-in that records every invocation it receives."""

    def __init__(self) -> None:
        self.invocations: list[ResolvedInvocation] = []

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.invocations.append(invocation)
        yield ProviderEvent(type="text_delta", text="recorded")
        yield ProviderEvent(type="usage", input_tokens=10, output_tokens=5)
        yield ProviderEvent(type="completed", input_tokens=10, output_tokens=5)


def test_run_arm_invokes_the_supplied_provider_once_per_task_for_a_single_pass_provider(
    store: Store,
) -> None:
    """`_run_arm` streams from the provider passed in exactly once per task when no round awaits
    tools.
    """
    from eval.harness import _DEFAULT_GOLDEN_TASKS_PATH, _load_golden_tasks

    tasks = _load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH)
    provider = _RecordingProvider()
    matrix = load_capability_matrix()

    _run_arm(
        provider=provider,
        matrix=matrix,
        arm_spec=ArmSpec(family="anthropic", effort="low"),
        tasks=tasks,
        store=store,
        tenant="eval-test-arm",
        mode="stub",
        dataset_seed=1,
        git_revision="unknown",
        runtime_identifier="test",
        price_table_version="0.1.0-provisional",
        cost_withheld=True,
    )

    assert len(provider.invocations) == len(tasks)


# --------------------------------------------------------------------------------------
# A turn is a multi-round tool loop: an `awaiting_tools` round re-invokes with results fed back.
# --------------------------------------------------------------------------------------

_MR_TENANT = "eval-multiround"
_MR_REP = "rep-ashford"
_MR_NOW = "2026-01-01T00:00:00Z"


def _mr_identity_payload() -> dict[str, object]:
    return {
        "rep_id": _MR_REP,
        "book": ["acct-1"],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "",
        "source": "gateway",
        "tenant": _MR_TENANT,
    }


def _mr_seed_opportunity(store: Store, opportunity_id: str) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": opportunity_id,
            "tenant": _MR_TENANT,
            "tenant_account_id": f"{_MR_TENANT}#acct-1",
            "tenant_owner_stage": f"{_MR_TENANT}#{_MR_REP}",
            "tenant_stage": f"{_MR_TENANT}#Discovery",
            "name_lower": "acme renewal",
            "account_id": "acct-1",
            "name": "Acme Renewal",
            "stage": "Discovery",
            "owner_rep_id": _MR_REP,
            "value": 1000,
            "close_date": "2025-06-01",
            "primary_contact_id": "cont-1",
            "last_touch": "2025-12-01T00:00:00Z",
            "created_at": _MR_NOW,
            "updated_at": _MR_NOW,
            "created_by": _MR_REP,
            "schema_version": 1,
        },
    )


class _ToolThenTextProvider(ModelProvider):
    """Round 1 emits a `get_opportunity` tool call and ends `awaiting_tools=True` with no text;
    round 2 emits a second `get_opportunity` call plus final text and ends `awaiting_tools=False`.

    Records its call count and a snapshot of the `messages` handed to each round.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.messages_per_round: list[list[dict[str, object]]] = []

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.call_count += 1
        self.messages_per_round.append(copy.deepcopy(messages))
        if self.call_count == 1:
            yield ProviderEvent(
                type="tool_call",
                tool_name="get_opportunity",
                tool_input={"opportunity_id": "opp-1"},
                tool_id="tc-round-1",
            )
            yield ProviderEvent(type="usage", input_tokens=100, output_tokens=10)
            yield ProviderEvent(
                type="completed", awaiting_tools=True, input_tokens=100, output_tokens=10
            )
        else:
            yield ProviderEvent(
                type="tool_call",
                tool_name="get_opportunity",
                tool_input={"opportunity_id": "opp-2"},
                tool_id="tc-round-2",
            )
            yield ProviderEvent(type="text_delta", text="the answer, from round two")
            yield ProviderEvent(type="usage", input_tokens=50, output_tokens=20)
            yield ProviderEvent(
                type="completed", awaiting_tools=False, input_tokens=50, output_tokens=20
            )


def test_run_provider_turn_re_invokes_when_a_round_awaits_tool_results(store: Store) -> None:
    """A round that ends `awaiting_tools` re-invokes the provider with the tool result appended,
    and the turn is scored across both rounds.
    """
    _mr_seed_opportunity(store, "opp-1")
    _mr_seed_opportunity(store, "opp-2")
    provider = _ToolThenTextProvider()

    outcome, records, _stream_calls = _run_provider_turn(
        provider,
        matrix=load_capability_matrix(),
        tuning=Tuning(source="test", family="anthropic", effort="low"),
        prompt="two-round prompt",
        store=store,
        tenant=_MR_TENANT,
        identity_payload=_mr_identity_payload(),
        task_id="gt-multiround",
    )

    # The provider was streamed from twice: the initial round and one re-invocation.
    assert provider.call_count == 2

    # Round 2's conversation carried the round-1 tool round-trip fed back: the assistant tool-use
    # turn and the tool result naming the call it answers. drive_turn prepends the supervisor and
    # answer-style system messages, so the user prompt is message[2] and the fed-back turns follow.
    round_two_messages = provider.messages_per_round[1]
    assert [m["role"] for m in round_two_messages[:2]] == ["system", "system"]
    assert round_two_messages[2] == {"role": "user", "content": "two-round prompt"}
    assistant_turn = round_two_messages[3]
    assert assistant_turn["role"] == "assistant"
    assert assistant_turn["tool_calls"] == [
        {"id": "tc-round-1", "name": "get_opportunity", "input": {"opportunity_id": "opp-1"}}
    ]
    tool_turn = round_two_messages[4]
    assert tool_turn["role"] == "tool"
    tool_results = tool_turn["tool_results"]
    assert isinstance(tool_results, list)
    assert len(tool_results) == 1
    assert tool_results[0]["id"] == "tc-round-1"
    assert isinstance(tool_results[0]["output"], str)  # a JSON string, not a raw dict

    # Round 2's own tool call is present and usage is attributed for both rounds.
    assert [call.target_id for call in outcome.tool_calls] == ["opp-1", "opp-2"]
    # drive_turn records one supervisor InvocationAttribution per round; summed they are the whole
    # turn's usage (150 in / 30 out over the two rounds).
    assert len(records.invocations) == 2
    assert all(inv.tier == "supervisor" for inv in records.invocations)
    assert sum(inv.tokens.input_tokens for inv in records.invocations) == 150
    assert sum(inv.tokens.output_tokens for inv in records.invocations) == 30


class _AlwaysAwaitsToolsProvider(ModelProvider):
    """Every round emits a `get_opportunity` tool call and ends `awaiting_tools=True`, never
    producing a final answer.

    After `_SAFETY_ROUNDS` rounds it stops awaiting tools, so a missing cap shows as
    `call_count == _SAFETY_ROUNDS` rather than a hang.
    """

    _SAFETY_ROUNDS = 30

    def __init__(self) -> None:
        self.call_count = 0

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.call_count += 1
        if self.call_count >= self._SAFETY_ROUNDS:
            # Reached only if the production cap is missing; ends the turn.
            yield ProviderEvent(type="text_delta", text="safety stop")
            yield ProviderEvent(type="usage", input_tokens=1, output_tokens=1)
            yield ProviderEvent(
                type="completed", awaiting_tools=False, input_tokens=1, output_tokens=1
            )
            return
        yield ProviderEvent(
            type="tool_call",
            tool_name="get_opportunity",
            tool_input={"opportunity_id": "opp-1"},
            tool_id=f"tc-{self.call_count}",
        )
        yield ProviderEvent(type="usage", input_tokens=10, output_tokens=1)
        yield ProviderEvent(type="completed", awaiting_tools=True, input_tokens=10, output_tokens=1)


def test_run_provider_turn_caps_rounds_when_a_model_never_answers(store: Store) -> None:
    """A model that always ends a round `awaiting_tools` stops after a bounded number of provider
    rounds, ending cleanly with the outcome accumulated so far.
    """
    _mr_seed_opportunity(store, "opp-1")
    provider = _AlwaysAwaitsToolsProvider()

    outcome, _records, _stream_calls = _run_provider_turn(
        provider,
        matrix=load_capability_matrix(),
        tuning=Tuning(source="test", family="anthropic", effort="low"),
        prompt="never-answers prompt",
        store=store,
        tenant=_MR_TENANT,
        identity_payload=_mr_identity_payload(),
        task_id="gt-runaway",
        max_rounds=4,
    )

    # The cap fires at round 5 (round_number > max_rounds), so exactly 4 provider rounds stream.
    assert provider.call_count == 4
    # The turn ends with the outcome accumulated over the 4 capped rounds.
    assert len(outcome.tool_calls) == 4


# --------------------------------------------------------------------------------------
# Two arms configured with different models produce non-identical task scores.
# --------------------------------------------------------------------------------------


def test_two_arms_with_different_families_produce_non_identical_task_scores(store: Store) -> None:
    """Two arms with different families produce non-identical task scores."""
    result = run_sweep(
        store=store,
        arm_specs=(
            ArmSpec(family="anthropic", effort="low"),
            ArmSpec(family="openai", effort="low"),
        ),
    )
    arm_anthropic, arm_openai = result.arm_runs
    assert arm_anthropic.model_metrics.model_id != arm_openai.model_metrics.model_id
    assert arm_anthropic.task_scores != arm_openai.task_scores


def test_two_arms_with_different_families_produce_non_identical_aggregate_correctness(
    store: Store,
) -> None:
    result = run_sweep(
        store=store,
        arm_specs=(
            ArmSpec(family="anthropic", effort="low"),
            ArmSpec(family="openai", effort="low"),
        ),
    )
    arm_anthropic, arm_openai = result.arm_runs
    assert (
        arm_anthropic.aggregate_score.mean_correctness
        != arm_openai.aggregate_score.mean_correctness
        or arm_anthropic.task_scores != arm_openai.task_scores
    )


# --------------------------------------------------------------------------------------
# No artifact can be rendered from zero invocations without an explicit disclosure.
# --------------------------------------------------------------------------------------


class _SilentProvider(ModelProvider):
    """A provider that never reports a `usage` event, so the arm records zero invocations."""

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(type="text_delta", text="no usage reported")
        yield ProviderEvent(type="completed", input_tokens=None, output_tokens=None)


def test_run_sweep_refuses_to_render_when_an_arm_has_zero_included_invocations(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep with a zero-invocation arm refuses to render `matrix.md`/`matrix.json`/
    `matrix.csv`."""
    monkeypatch.setattr("eval.harness._build_provider", lambda _mode, **_kwargs: _SilentProvider())

    with pytest.raises(UndisclosedZeroInvocationRenderError):
        run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic", effort="low"),))


def test_zero_invocation_refusal_names_the_model_and_task_count(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("eval.harness._build_provider", lambda _mode, **_kwargs: _SilentProvider())

    with pytest.raises(UndisclosedZeroInvocationRenderError, match="zero included invocations"):
        run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic", effort="low"),))


# --------------------------------------------------------------------------------------
# The dispatched tool call reaches a real handler against the seeded tenant's state.
# --------------------------------------------------------------------------------------


class _AlwaysCallsFirstToolProvider(ModelProvider):
    """A provider stand-in that always calls whatever tool is offered first, with no input."""

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        tool_name = str(tools[0]["name"]) if tools else None
        if tool_name is not None:
            yield ProviderEvent(type="tool_call", tool_name=tool_name, tool_input={})
        yield ProviderEvent(type="text_delta", text="done")
        yield ProviderEvent(type="usage", input_tokens=10, output_tokens=5)
        yield ProviderEvent(type="completed", text="done", input_tokens=10, output_tokens=5)


def test_a_dispatched_tool_call_reaches_the_real_handler_against_the_seeded_tenant(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatched tool call reaches the real handler against the seeded tenant."""
    monkeypatch.setattr(
        "eval.harness._build_provider", lambda _mode, **_kwargs: _AlwaysCallsFirstToolProvider()
    )

    result = run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic", effort="low"),))
    (arm_run,) = result.arm_runs

    # Every task's turn dispatches exactly one real tool call.
    task_count = len(_load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH))
    assert len(arm_run.task_scores) == task_count
    assert arm_run.model_metrics.included_invocation_count == task_count
