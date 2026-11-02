# SPDX-License-Identifier: MIT
"""Tests for `run_subagent`: the four `SubagentOutcome` kinds, truncation, proposals, tier model.

The leaf tool call goes through an injected fake `ToolActor`, so the runner is tested without a real
dispatch.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from agents.budget import TurnBudget, TurnBudgetSpec
from agents.registry import RegisteredTool
from agents.subagent_runner import SubagentRunResult, run_subagent, run_subagent_async
from agents.subagents import SUBAGENT_SPECS_BY_ROLE
from agents.subagents.base import SUBAGENT_TIER, SubagentSpec
from agents.tier_attribution import CacheCounts, InvocationAttribution, TokenCounts
from agents.turn_driver import session_turn_tuning
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, Tuning
from providers.capabilities import load_capability_matrix
from providers.tier_tuning import resolve_turn

_TOOL_MODEL_ID = load_capability_matrix().model_for("anthropic", "tool").model_id


# --- test doubles -----------------------------------------------------------------------------


@dataclass
class _ScriptedProvider(ModelProvider):
    """Emits one scripted subagent-tier round: a tool_call for `tool_name`, then usage+completed."""

    tool_name: str
    tool_input: dict[str, object] = field(default_factory=lambda: {"account_id": "acct-1"})
    call_tool: bool = True

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[ProviderEvent]:
        if self.call_tool:
            yield ProviderEvent(
                type="tool_call",
                tool_name=self.tool_name,
                tool_input=dict(self.tool_input),
                tool_id="tc-1",
            )
        yield ProviderEvent(type="usage", input_tokens=40, output_tokens=12)
        yield ProviderEvent(type="completed", awaiting_tools=False)


@dataclass
class _FakeActorResult:
    output: dict[str, object]
    invocation: InvocationAttribution
    dispatch_latency_ms: float | None = None


@dataclass
class _FakeActor:
    """Records the (tool, intent) it was called with and returns a configured raw output."""

    output: dict[str, object]
    role: str
    calls: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, tool: RegisteredTool, intent: str) -> _FakeActorResult:
        self.calls.append((tool.name, intent))
        return _FakeActorResult(
            output=self.output,
            invocation=InvocationAttribution(
                tier="tool",
                role=self.role,
                model_id=_TOOL_MODEL_ID,
                tokens=TokenCounts(input_tokens=8, output_tokens=4),
                cache=CacheCounts(),
                latency_ms=None,
            ),
        )


def _ok(items: list[dict[str, object]] | None = None, **body: object) -> dict[str, object]:
    payload: dict[str, object] = dict(body)
    if items is not None:
        payload["items"] = items
    return {"statusCode": 200, "body": payload}


def _spec_and_tool(role: str = "research") -> tuple[SubagentSpec, str]:
    spec = SUBAGENT_SPECS_BY_ROLE[role]
    return spec, spec.tools[0].name


def _run(
    *, spec: SubagentSpec, tool_name: str, actor: _FakeActor, call_tool: bool = True
) -> SubagentRunResult:
    return run_subagent(
        spec=spec,
        intent="find account acct-1",
        resolved=resolve_turn(
            load_capability_matrix(),
            session_turn_tuning(Tuning(source="test", family="anthropic", effort=None)),
        )["subagent"],
        provider=_ScriptedProvider(tool_name=tool_name, call_tool=call_tool),
        tool_actor=actor,
    )


# --- the four outcomes ------------------------------------------------------------------------


def test_data_outcome_carries_the_payload() -> None:
    spec, tool_name = _spec_and_tool()
    actor = _FakeActor(output=_ok(items=[{"account_id": "acct-1", "name": "Acme"}]), role=spec.role)
    result = _run(spec=spec, tool_name=tool_name, actor=actor)
    assert result.outcome.kind == "data"
    assert result.outcome.data is not None


def test_no_match_outcome_on_empty_items() -> None:
    spec, tool_name = _spec_and_tool()
    actor = _FakeActor(output=_ok(items=[]), role=spec.role)
    assert _run(spec=spec, tool_name=tool_name, actor=actor).outcome.kind == "no_match"


def test_denied_outcome_names_the_missing_scope() -> None:
    """A scope-denied payload classifies as denied, and the denial names the missing scope."""
    spec, tool_name = _spec_and_tool()
    denied = {
        "statusCode": 403,
        "error": {
            "code": "scope_denied",
            "message": "You do not have access to that account.",
            "missing_scope": "account:acct-999",
        },
    }
    actor = _FakeActor(output=denied, role=spec.role)
    result = _run(spec=spec, tool_name=tool_name, actor=actor)
    assert result.outcome.kind == "denied"
    assert result.outcome.missing_scope == "account:acct-999"
    assert result.outcome.denial_message


def test_tool_failure_outcome_carries_a_user_safe_message() -> None:
    spec, tool_name = _spec_and_tool()
    failed = {"statusCode": 500, "error": {"code": "internal_error", "message": "Try again later."}}
    actor = _FakeActor(output=failed, role=spec.role)
    result = _run(spec=spec, tool_name=tool_name, actor=actor)
    assert result.outcome.kind == "tool_failure"
    assert result.outcome.error_message == "Try again later."


def test_no_leaf_call_is_no_match_not_fabricated_data() -> None:
    spec, tool_name = _spec_and_tool()
    actor = _FakeActor(output=_ok(items=[{"x": 1}]), role=spec.role)
    result = _run(spec=spec, tool_name=tool_name, actor=actor, call_tool=False)
    assert result.outcome.kind == "no_match"
    assert actor.calls == []  # the actor was never called, so nothing was dispatched


# --- truncation disclosed ---------------------------------------------------------------------


def test_search_truncation_is_disclosed_on_the_tool_call_attribution() -> None:
    spec, tool_name = _spec_and_tool()
    actor = _FakeActor(
        output=_ok(items=[{"id": "o-1"}], truncated=True, examined_count=50), role=spec.role
    )
    result = _run(spec=spec, tool_name=tool_name, actor=actor)
    assert result.tool_calls[0].truncated is True


def test_byte_ceiling_truncation_sets_the_tool_result_event_truncated_flag() -> None:
    spec, tool_name = _spec_and_tool()
    big_items: list[dict[str, object]] = [{"id": f"o-{n}", "blob": "x" * 400} for n in range(300)]
    actor = _FakeActor(output=_ok(items=big_items), role=spec.role)
    result = _run(spec=spec, tool_name=tool_name, actor=actor)
    tool_results = [e for e in result.events if e.kind == "tool_result"]
    assert tool_results and tool_results[0].truncated is True


# --- proposal surfaced structurally -----------------------------------------------------------


def test_a_submit_proposal_result_surfaces_a_proposal_event() -> None:
    spec = SUBAGENT_SPECS_BY_ROLE["mutation"]
    assert "submit_proposal" in {tool.name for tool in spec.tools}
    proposal_output = {
        "statusCode": 200,
        "body": {
            "proposal": {
                "proposal_id": "prop-1",
                "before_snapshot": {"stage": "proposal"},
                "after_payload": {"stage": "closed_won"},
                "reason": "verbal",
                "target_table": "opportunities",
                "target_id": "opp-1",
            }
        },
    }
    actor = _FakeActor(output=proposal_output, role=spec.role)
    result = _run(spec=spec, tool_name="submit_proposal", actor=actor)
    proposal_events = [e for e in result.events if e.kind == "proposal"]
    assert len(proposal_events) == 1
    assert proposal_events[0].proposal_id == "prop-1"


# --- resolves the subagent-tier model, never the supervisor tier ------------------------------


def test_resolves_the_subagent_tier_model_for_the_session_family() -> None:
    spec, tool_name = _spec_and_tool()
    actor = _FakeActor(output=_ok(items=[{"a": 1}]), role=spec.role)
    result = _run(spec=spec, tool_name=tool_name, actor=actor)
    subagent_invocations = [i for i in result.invocations if i.tier == SUBAGENT_TIER]
    assert subagent_invocations, "the subagent-tier model call must be attributed"
    matrix = load_capability_matrix()
    expected = matrix.model_for("anthropic", SUBAGENT_TIER).model_id
    assert subagent_invocations[0].model_id == expected
    assert subagent_invocations[0].role == spec.role
    # The tool-tier call is attributed separately.
    assert any(i.tier == "tool" for i in result.invocations)


# --- the runner never dispatches or builds identity itself ------------------------------------


def test_the_leaf_call_goes_only_through_the_injected_actor() -> None:
    """The runner hands the subagent's serialized tool_input to the actor and dispatches nothing."""
    spec, tool_name = _spec_and_tool()
    actor = _FakeActor(output=_ok(items=[{"a": 1}]), role=spec.role)
    _run(spec=spec, tool_name=tool_name, actor=actor)
    assert len(actor.calls) == 1
    called_tool, intent = actor.calls[0]
    assert called_tool == tool_name
    assert "account_id" in intent  # the subagent's tool_input, serialized, became the intent


# --- stub determinism -------------------------------------------------------------------------


def test_two_identical_runs_are_byte_reproducible() -> None:
    spec, tool_name = _spec_and_tool()
    out = _ok(items=[{"account_id": "acct-1"}])
    first = _run(spec=spec, tool_name=tool_name, actor=_FakeActor(output=out, role=spec.role))
    second = _run(spec=spec, tool_name=tool_name, actor=_FakeActor(output=out, role=spec.role))
    assert first == second
    # No wall clock leaks onto the stub path.
    assert all(i.latency_ms is None for i in first.invocations)
    assert all(t.latency_ms is None for t in first.tool_calls)


def test_events_pair_a_tool_call_with_a_tool_result() -> None:
    spec, tool_name = _spec_and_tool()
    actor = _FakeActor(output=_ok(items=[{"a": 1}]), role=spec.role)
    kinds = [e.kind for e in _run(spec=spec, tool_name=tool_name, actor=actor).events]
    assert kinds[:2] == ["tool_call", "tool_result"]


# --- a re-invoked round feeds the model the real tool output ---------------------------------


@dataclass
class _TwoRoundProvider(ModelProvider):
    """Round 1 calls a tool and awaits more; round 2 records the messages it is re-invoked with.

    `run_subagent` mutates one `messages` list in place across rounds, so each round is snapshotted
    with `deepcopy` at its own start — a later append cannot backfill what an earlier round saw.
    """

    tool_name: str
    tool_input: dict[str, object] = field(default_factory=lambda: {"account_id": "acct-1"})
    rounds_seen: list[list[dict[str, object]]] = field(default_factory=list)

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.rounds_seen.append(copy.deepcopy(messages))
        return self._events(len(self.rounds_seen))

    async def _events(self, round_number: int) -> AsyncIterator[ProviderEvent]:
        if round_number == 1:
            yield ProviderEvent(
                type="tool_call",
                tool_name=self.tool_name,
                tool_input=dict(self.tool_input),
                tool_id="tc-1",
            )
            yield ProviderEvent(type="usage", input_tokens=40, output_tokens=12)
            yield ProviderEvent(type="completed", awaiting_tools=True)
            return
        yield ProviderEvent(type="usage", input_tokens=10, output_tokens=4)
        yield ProviderEvent(type="completed", awaiting_tools=False)


def test_reinvoked_round_carries_the_real_serialized_tool_output_not_dispatched() -> None:
    """Round 2's `tool` message carries the actor's real output, JSON-serialized."""
    spec, tool_name = _spec_and_tool()
    real_output = _ok(items=[{"account_id": "acct-1", "name": "Acme"}])
    provider = _TwoRoundProvider(tool_name=tool_name)
    run_subagent(
        spec=spec,
        intent="find account acct-1",
        resolved=resolve_turn(
            load_capability_matrix(),
            session_turn_tuning(Tuning(source="test", family="anthropic", effort=None)),
        )["subagent"],
        provider=provider,
        tool_actor=_FakeActor(output=real_output, role=spec.role),
    )
    assert len(provider.rounds_seen) == 2, "the subagent must be re-invoked for a second round"
    round2 = provider.rounds_seen[1]
    tool_messages = [m for m in round2 if m.get("role") == "tool"]
    assert tool_messages, "round 2 must carry a tool-result message"
    results = tool_messages[-1]["tool_results"]
    assert isinstance(results, list)
    outputs = [r["output"] for r in results if isinstance(r, dict)]
    assert outputs == [json.dumps(real_output)]
    assert "dispatched" not in outputs


# --- the turn's wall clock inside a delegation -----------------------------------------

#: Outer bound on each wall-clock test; a delegation that ignores its budget stalls far past it.
_STALL_TEST_CAP_SECONDS = 5.0


@dataclass
class _StallsOnSecondRoundProvider(ModelProvider):
    """Round 1 calls one leaf and asks for another round; round 2 never answers."""

    tool_name: str
    rounds: int = 0

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.rounds += 1
        return self._first() if self.rounds == 1 else self._stall()

    async def _first(self) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(
            type="tool_call", tool_name=self.tool_name, tool_input={"q": "a"}, tool_id="t1"
        )
        yield ProviderEvent(type="usage", input_tokens=40, output_tokens=12)
        yield ProviderEvent(type="completed", awaiting_tools=True)

    async def _stall(self) -> AsyncIterator[ProviderEvent]:
        await asyncio.sleep(3600)
        yield ProviderEvent(type="completed", awaiting_tools=False)


def _subagent_resolved() -> ResolvedInvocation:
    return resolve_turn(
        load_capability_matrix(),
        session_turn_tuning(Tuning(source="test", family="anthropic", effort=None)),
    )["subagent"]


def _run_bounded(
    *, provider: ModelProvider, actor: object, budget: TurnBudget, spec: SubagentSpec
) -> SubagentRunResult:
    return asyncio.run(
        asyncio.wait_for(
            run_subagent_async(
                spec=spec,
                intent="find account acct-1",
                resolved=_subagent_resolved(),
                provider=provider,
                tool_actor=actor,  # type: ignore[arg-type]
                budget=budget,
            ),
            timeout=_STALL_TEST_CAP_SECONDS,
        )
    )


def test_a_subagent_round_outlasting_the_turn_clock_ends_the_delegation_with_its_partial_work() -> (
    None
):
    """The stalled round is cut at the turn's cap; round 1's leaf call and usage are kept."""
    spec, tool_name = _spec_and_tool()
    actor = _FakeActor(output=_ok(items=[{"account_id": "acct-1"}]), role=spec.role)
    budget = TurnBudget.from_spec(TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=0.2))
    result = _run_bounded(
        provider=_StallsOnSecondRoundProvider(tool_name=tool_name),
        actor=actor,
        budget=budget,
        spec=spec,
    )
    assert budget.is_deadline_expired()
    assert result.outcome.kind == "data"
    assert [e.kind for e in result.events] == ["tool_call", "tool_result"]
    assert [t.tool_name for t in result.tool_calls] == [tool_name]
    assert [(i.tier, i.tokens.input_tokens) for i in result.invocations] == [
        ("tool", 8),
        (SUBAGENT_TIER, 40),
    ]


def test_a_spent_turn_clock_starts_no_further_subagent_round() -> None:
    """Between rounds the runner checks the turn's wall clock and stops without a model call."""
    spec, tool_name = _spec_and_tool()
    now = [0.0]
    budget = TurnBudget.from_spec(
        TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=10.0), clock=lambda: now[0]
    )
    now[0] = 10.0
    provider = _StallsOnSecondRoundProvider(tool_name=tool_name)
    result = _run_bounded(
        provider=provider,
        actor=_FakeActor(output=_ok(items=[]), role=spec.role),
        budget=budget,
        spec=spec,
    )
    assert provider.rounds == 0
    assert result.outcome.kind == "no_match"
    assert result.invocations == ()


@dataclass
class _SlowSecondLeafActor:
    """An async tool-tier actor whose first call answers and whose second never does."""

    role: str
    calls: int = 0

    async def __call__(self, tool: RegisteredTool, intent: str) -> _FakeActorResult:
        self.calls += 1
        if self.calls > 1:
            await asyncio.sleep(3600)
        return _FakeActor(output=_ok(items=[{"account_id": "acct-1"}]), role=self.role)(
            tool, intent
        )


@dataclass
class _TwoLeafProvider(ModelProvider):
    """One round naming the same leaf twice."""

    tool_name: str

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[ProviderEvent]:
        for call_id in ("t1", "t2"):
            yield ProviderEvent(
                type="tool_call", tool_name=self.tool_name, tool_input={}, tool_id=call_id
            )
        yield ProviderEvent(type="usage", input_tokens=5, output_tokens=5)
        yield ProviderEvent(type="completed", awaiting_tools=False)


def test_a_tool_tier_call_outlasting_the_turn_clock_ends_the_delegation_keeping_earlier_leaves() -> (
    None
):
    """The stalled tool-tier call is abandoned unrecorded; the leaf before it is kept."""
    spec, tool_name = _spec_and_tool()
    budget = TurnBudget.from_spec(TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=0.2))
    result = _run_bounded(
        provider=_TwoLeafProvider(tool_name=tool_name),
        actor=_SlowSecondLeafActor(role=spec.role),
        budget=budget,
        spec=spec,
    )
    assert [t.tool_name for t in result.tool_calls] == [tool_name]
    assert [i.tier for i in result.invocations] == ["tool"]
    assert result.outcome.kind == "data"


# --- metering edge cases: each round is an invocation or a note, never both --------------------


@dataclass
class _ScriptedEventsProvider(ModelProvider):
    """One round of scripted events, then (with `stall`) a wait that outlasts any test cap."""

    events: list[ProviderEvent]
    stall: bool = False

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[ProviderEvent]:
        for event in self.events:
            yield event
        if self.stall:
            await asyncio.sleep(3600)
        yield ProviderEvent(type="completed", awaiting_tools=False)


def _short_budget() -> TurnBudget:
    return TurnBudget.from_spec(TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=0.2))


def _subagent_rounds(result: SubagentRunResult) -> list[TokenCounts]:
    return [i.tokens for i in result.invocations if i.tier == SUBAGENT_TIER]


def test_usage_before_a_stalled_leaf_is_still_recorded_once() -> None:
    """S1: a round's usage that precedes a leaf call which then stalls is kept as one invocation."""
    spec, tool_name = _spec_and_tool()
    budget = _short_budget()
    result = _run_bounded(
        provider=_ScriptedEventsProvider(
            events=[
                ProviderEvent(type="usage", input_tokens=40, output_tokens=12),
                ProviderEvent(type="tool_call", tool_name=tool_name, tool_input={}, tool_id="t1"),
            ]
        ),
        actor=_SlowSecondLeafActor(role=spec.role, calls=1),
        budget=budget,
        spec=spec,
    )
    assert _subagent_rounds(result) == [TokenCounts(input_tokens=40, output_tokens=12)]
    assert budget.calls_without_usage == 1, "only the abandoned tool-tier call is unmetered"


def test_a_complete_usage_report_is_one_invocation_and_no_note() -> None:
    spec, tool_name = _spec_and_tool()
    budget = TurnBudget.from_spec(TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=60.0))
    result = _run_bounded(
        provider=_ScriptedEventsProvider(
            events=[ProviderEvent(type="usage", input_tokens=40, output_tokens=12)]
        ),
        actor=_FakeActor(output=_ok(items=[]), role=spec.role),
        budget=budget,
        spec=spec,
    )
    assert _subagent_rounds(result) == [TokenCounts(input_tokens=40, output_tokens=12)]
    assert budget.calls_without_usage == 0


_INCOMPLETE_USAGE = [
    ProviderEvent(type="usage", input_tokens=40, output_tokens=None),
    ProviderEvent(type="usage", input_tokens=None, output_tokens=12),
]


@pytest.mark.parametrize("usage", _INCOMPLETE_USAGE, ids=["no-output", "no-input"])
def test_a_usage_report_missing_one_count_is_noted_unmetered_not_recorded(
    usage: ProviderEvent,
) -> None:
    """The round is noted unmetered, so the turn reads as a lower bound, not too low."""
    spec, _tool_name = _spec_and_tool()
    budget = TurnBudget.from_spec(TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=60.0))
    result = _run_bounded(
        provider=_ScriptedEventsProvider(events=[usage]),
        actor=_FakeActor(output=_ok(items=[]), role=spec.role),
        budget=budget,
        spec=spec,
    )
    assert _subagent_rounds(result) == []
    assert budget.calls_without_usage == 1


def test_an_incomplete_report_and_a_complete_one_in_a_round_is_still_unmetered() -> None:
    spec, _tool_name = _spec_and_tool()
    budget = TurnBudget.from_spec(TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=60.0))
    result = _run_bounded(
        provider=_ScriptedEventsProvider(
            events=[
                ProviderEvent(type="usage", input_tokens=40, output_tokens=12),
                ProviderEvent(type="usage", input_tokens=3, output_tokens=None),
            ]
        ),
        actor=_FakeActor(output=_ok(items=[]), role=spec.role),
        budget=budget,
        spec=spec,
    )
    assert _subagent_rounds(result) == []
    assert budget.calls_without_usage == 1


def test_an_incomplete_report_then_a_stream_cut_is_noted_exactly_once() -> None:
    spec, _tool_name = _spec_and_tool()
    budget = _short_budget()
    result = _run_bounded(
        provider=_ScriptedEventsProvider(events=[_INCOMPLETE_USAGE[0]], stall=True),
        actor=_FakeActor(output=_ok(items=[]), role=spec.role),
        budget=budget,
        spec=spec,
    )
    assert budget.is_deadline_expired()
    assert _subagent_rounds(result) == []
    assert budget.calls_without_usage == 1


def test_an_incomplete_report_before_a_stalled_leaf_notes_each_call_once() -> None:
    spec, tool_name = _spec_and_tool()
    budget = _short_budget()
    result = _run_bounded(
        provider=_ScriptedEventsProvider(
            events=[
                _INCOMPLETE_USAGE[0],
                ProviderEvent(type="tool_call", tool_name=tool_name, tool_input={}, tool_id="t1"),
            ]
        ),
        actor=_SlowSecondLeafActor(role=spec.role, calls=1),
        budget=budget,
        spec=spec,
    )
    assert _subagent_rounds(result) == []
    assert budget.calls_without_usage == 2, "one subagent round and one tool-tier call"


# --- a round ending on a provider error is noted unmetered once ---------------------


def _error_event() -> ProviderEvent:
    return ProviderEvent(type="error", error_message="The model is unavailable.")


@pytest.mark.parametrize(
    "before",
    [
        [],
        [ProviderEvent(type="usage", input_tokens=40, output_tokens=None)],
    ],
    ids=["error-before-usage", "error-after-partial-usage"],
)
def test_a_round_ending_on_a_provider_error_is_noted_unmetered_once(
    before: list[ProviderEvent], caplog: pytest.LogCaptureFixture
) -> None:
    spec, _tool_name = _spec_and_tool()
    budget = TurnBudget.from_spec(TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=60.0))
    caplog.set_level(logging.WARNING)
    result = _run_bounded(
        provider=_ScriptedEventsProvider(events=[*before, _error_event()]),
        actor=_FakeActor(output=_ok(items=[]), role=spec.role),
        budget=budget,
        spec=spec,
    )
    assert _subagent_rounds(result) == []
    notes = [
        json.loads(r.getMessage())["reason"]
        for r in caplog.records
        if "model_call_usage_unrecorded" in r.getMessage()
    ]
    assert notes == [f"tier={SUBAGENT_TIER} role={spec.role} cause=provider_error"]
    assert budget.calls_without_usage == 1


def test_a_provider_error_after_a_complete_usage_report_is_recorded_not_noted() -> None:
    spec, _tool_name = _spec_and_tool()
    budget = TurnBudget.from_spec(TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=60.0))
    result = _run_bounded(
        provider=_ScriptedEventsProvider(
            events=[ProviderEvent(type="usage", input_tokens=40, output_tokens=12), _error_event()]
        ),
        actor=_FakeActor(output=_ok(items=[]), role=spec.role),
        budget=budget,
        spec=spec,
    )
    assert _subagent_rounds(result) == [TokenCounts(input_tokens=40, output_tokens=12)]
    assert budget.calls_without_usage == 0


# --- system prompt, full results, self-corrected retries --------------------------------


@dataclass
class _RoundsProvider(ModelProvider):
    """Plays one scripted round per list entry, each calling the named tools in order."""

    rounds: list[list[str]]
    seen: list[list[dict[str, object]]] = field(default_factory=list)

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.seen.append(copy.deepcopy(messages))
        return self._events(len(self.seen) - 1)

    async def _events(self, index: int) -> AsyncIterator[ProviderEvent]:
        calls = self.rounds[index] if index < len(self.rounds) else []
        for position, tool_name in enumerate(calls):
            yield ProviderEvent(
                type="tool_call",
                tool_name=tool_name,
                tool_input={"n": f"{index}-{position}"},
                tool_id=f"t{index}-{position}",
            )
        yield ProviderEvent(type="usage", input_tokens=10, output_tokens=4)
        yield ProviderEvent(type="completed", awaiting_tools=index + 1 < len(self.rounds))


@dataclass
class _SequenceActor:
    """Returns the next configured output on each call."""

    outputs: list[dict[str, object]]
    role: str
    calls: list[str] = field(default_factory=list)

    def __call__(self, tool: RegisteredTool, intent: str) -> _FakeActorResult:
        self.calls.append(tool.name)
        return _FakeActorResult(
            output=self.outputs[len(self.calls) - 1],
            invocation=InvocationAttribution(
                tier="tool",
                role=self.role,
                model_id=_TOOL_MODEL_ID,
                tokens=TokenCounts(input_tokens=8, output_tokens=4),
                cache=CacheCounts(),
                latency_ms=None,
            ),
        )


def _failure(message: str = "bad argument") -> dict[str, object]:
    return {"statusCode": 400, "error": {"code": "validation_failed", "message": message}}


def _denial() -> dict[str, object]:
    return {
        "statusCode": 403,
        "error": {"code": "scope_denied", "message": "Denied.", "missing_scope": "crm.read"},
    }


def _run_rounds(
    rounds: list[list[str]], outputs: list[dict[str, object]], role: str = "research"
) -> tuple[SubagentRunResult, _RoundsProvider]:
    spec = SUBAGENT_SPECS_BY_ROLE[role]
    provider = _RoundsProvider(rounds=rounds)
    result = run_subagent(
        spec=spec,
        intent="the task",
        resolved=_subagent_resolved(),
        provider=provider,
        tool_actor=_SequenceActor(outputs=outputs, role=role),
    )
    return result, provider


def _two_tools(role: str = "research") -> tuple[str, str]:
    names = sorted(tool.name for tool in SUBAGENT_SPECS_BY_ROLE[role].tools)
    return names[0], names[1]


@pytest.mark.parametrize("role", sorted(SUBAGENT_SPECS_BY_ROLE))
def test_every_delegation_opens_with_the_rendered_subagent_system_prompt(role: str) -> None:
    from agents.agent_prompts import load_agent_prompts

    spec = SUBAGENT_SPECS_BY_ROLE[role]
    _, provider = _run_rounds([[]], [], role=role)
    first, second = provider.seen[0][:2]
    template = load_agent_prompts()["subagent"]
    assert first == {
        "role": "system",
        "content": template.format(role=spec.role, purpose=spec.purpose),
    }
    assert second == {"role": "user", "content": "the task"}
    assert f"You are the {role} subagent" in str(first["content"])
    assert spec.purpose in str(first["content"])
    assert "{" not in str(first["content"])


def test_a_data_outcome_returns_every_successful_leaf_result_in_call_order() -> None:
    a, b = _two_tools()
    result, _ = _run_rounds(
        [[a], [b]], [_ok(account_id="acct-1"), _ok(items=[{"contact_id": "c-1"}])]
    )
    assert result.outcome.kind == "data"
    assert result.outcome.data == {
        "results": [
            {"tool": a, "body": {"account_id": "acct-1"}},
            {"tool": b, "body": {"items": [{"contact_id": "c-1"}]}},
        ]
    }


def test_control_the_last_leaf_alone_is_not_the_whole_result() -> None:
    a, b = _two_tools()
    result, _ = _run_rounds([[a, b]], [_ok(first=1), _ok(second=2)])
    assert result.outcome.data is not None
    results = result.outcome.data["results"]
    assert isinstance(results, list)
    assert [entry["body"] for entry in results] == [{"first": 1}, {"second": 2}]


def test_an_empty_read_beside_a_successful_one_keeps_the_data_outcome() -> None:
    a, b = _two_tools()
    result, _ = _run_rounds([[a, b]], [_ok(items=[]), _ok(found=True)])
    assert result.outcome.kind == "data"
    assert result.outcome.data == {"results": [{"tool": b, "body": {"found": True}}]}


def test_results_over_the_byte_ceiling_are_cut_at_an_entry_and_marked_truncated() -> None:
    from agents.events import TOOL_RESULT_BYTE_CEILING, TRUNCATION_MARKER

    a, _ = _two_tools()
    big = "x" * (TOOL_RESULT_BYTE_CEILING // 2)
    result, _ = _run_rounds([[a, a, a]], [_ok(blob=big), _ok(blob=big), _ok(blob=big)])
    data = result.outcome.data
    assert data is not None and data["truncated"] is True
    results = data["results"]
    assert isinstance(results, list)
    assert results[-1] == {TRUNCATION_MARKER: True}
    assert len(results) < 4


def test_a_failure_the_same_tool_later_recovers_from_is_cleared_to_data() -> None:
    a, _ = _two_tools()
    result, _ = _run_rounds([[a], [a]], [_failure(), _ok(account_id="acct-1")])
    assert result.outcome.kind == "data"
    assert result.outcome.data == {"results": [{"tool": a, "body": {"account_id": "acct-1"}}]}
    assert [call.outcome_kind for call in result.tool_calls] == ["tool_failure", "data"]


def test_a_failure_the_same_tool_later_recovers_from_with_no_rows_is_no_match() -> None:
    a, _ = _two_tools()
    result, _ = _run_rounds([[a], [a]], [_failure(), _ok(items=[])])
    assert result.outcome.kind == "no_match"


def test_a_failure_followed_by_another_tools_success_stays_a_tool_failure() -> None:
    a, b = _two_tools()
    result, _ = _run_rounds([[a], [b]], [_failure("first failure"), _ok(found=True)])
    assert result.outcome.kind == "tool_failure"
    assert result.outcome.error_message == "first failure"


def test_a_denial_is_never_cleared_by_a_later_success_from_the_same_tool() -> None:
    a, _ = _two_tools()
    result, _ = _run_rounds([[a], [a]], [_denial(), _ok(found=True)])
    assert result.outcome.kind == "denied"
    assert result.outcome.missing_scope == "crm.read"
    assert result.outcome.data is None


def test_a_failure_after_a_success_from_the_same_tool_stays_open() -> None:
    a, _ = _two_tools()
    result, _ = _run_rounds([[a], [a]], [_ok(found=True), _failure("late failure")])
    assert result.outcome.kind == "tool_failure"
    assert result.outcome.error_message == "late failure"


def test_the_subagent_system_prompt_is_the_stable_cacheable_prefix() -> None:
    """The subagent path sends one stable system message, unmarked, and no volatile system one."""
    from providers.prompt_caching import CACHE_SCOPE_KEY, VOLATILE, stable_system_end

    _, provider = _run_rounds([[]], [])
    messages = provider.seen[0]
    assert CACHE_SCOPE_KEY not in messages[0]
    assert stable_system_end(messages) == 0
    assert not [m for m in messages if m.get(CACHE_SCOPE_KEY) == VOLATILE]
    assert [m["role"] for m in messages] == ["system", "user"]


# --- the aggregate is bounded once, never dropping the first entry (P1-H-1) -------------------


def _opportunity(index: int) -> dict[str, object]:
    return {
        "opportunity_id": f"opp-{index:04d}",
        "tenant": "demo",
        "account_id": f"acct-{index % 41:03d}",
        "name": f"Renewal opportunity number {index}",
        "stage": "Negotiation",
        "owner_rep_id": "rep-1",
        "value": 15000.0 + index,
        "close_date": "2026-06-30",
        "primary_contact_id": f"contact-{index:04d}",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def _search_body(count: int) -> dict[str, object]:
    return {"items": [_opportunity(i) for i in range(count)], "next_cursor": None}


def _bounded(results: list[dict[str, object]]) -> tuple[list[dict[str, object]], bool]:
    from agents.subagent_runner import bound_results

    return bound_results(results)


def _size(value: object) -> int:
    return len(repr(value).encode("utf-8"))


def test_a_300_item_search_keeps_its_entry_with_trimmed_items() -> None:
    from agents.events import TOOL_RESULT_BYTE_CEILING, TRUNCATION_MARKER

    a, _ = _two_tools()
    result, _ = _run_rounds([[a]], [{"statusCode": 200, "body": _search_body(300)}])
    data = result.outcome.data
    assert result.outcome.kind == "data" and data is not None
    assert data["truncated"] is True
    results = data["results"]
    assert isinstance(results, list) and len(results) == 1
    entry = results[0]
    assert entry["tool"] == a
    items = entry["body"]["items"]
    assert items[-1] == {TRUNCATION_MARKER: True}
    assert len(items) > 50
    assert items[0] == _opportunity(0)
    assert _size(data) <= TOOL_RESULT_BYTE_CEILING + _size({TRUNCATION_MARKER: True})


def test_results_exactly_at_the_ceiling_are_kept_whole() -> None:
    from agents.events import TOOL_RESULT_BYTE_CEILING
    from agents.subagent_runner import bound_results

    envelope = _size({"results": [], "truncated": True})
    entry: dict[str, object] = {"tool": "t", "body": {"note": ""}}
    pad = TOOL_RESULT_BYTE_CEILING - envelope - _size(entry)
    entry = {"tool": "t", "body": {"note": "x" * pad}}
    assert envelope + _size(entry) == TOOL_RESULT_BYTE_CEILING
    assert bound_results([entry]) == ([entry], False)
    over: dict[str, object] = {"tool": "t", "body": {"note": "x" * (pad + 1)}}
    kept, truncated = bound_results([over])
    assert truncated and kept[0]["body"] == over["body"]


def test_an_entry_one_record_over_the_ceiling_is_trimmed_not_dropped() -> None:
    from agents.events import TRUNCATION_MARKER

    body = _search_body(200)
    kept, truncated = _bounded([{"tool": "search_opportunities", "body": body}])
    assert truncated
    assert len(kept) == 1
    items = kept[0]["body"]["items"]  # type: ignore[index]
    assert isinstance(items, list)
    assert 0 < len(items) - 1 < 200
    assert items[-1] == {TRUNCATION_MARKER: True}
    assert kept[0]["body"]["next_cursor"] is None  # type: ignore[index]


def test_an_oversize_first_entry_without_items_is_kept_and_marked() -> None:
    from agents.events import TOOL_RESULT_BYTE_CEILING, TRUNCATION_MARKER

    body: dict[str, object] = {
        "account_id": "acct-1",
        "notes": "n" * (TOOL_RESULT_BYTE_CEILING + 10),
    }
    kept, truncated = _bounded([{"tool": "get_account", "body": body}])
    assert truncated
    assert kept[0] == {"tool": "get_account", "body": body, "truncated": True}
    assert kept[-1] == {TRUNCATION_MARKER: True}


def test_an_oversize_body_with_another_record_list_trims_that_list() -> None:
    from agents.events import TRUNCATION_MARKER

    contacts = [{"contact_id": f"c-{i}", "name": "N" * 200} for i in range(400)]
    body: dict[str, object] = {"account_id": "acct-1", "contacts": contacts}
    kept, truncated = _bounded([{"tool": "get_account", "body": body}])
    assert truncated
    trimmed = kept[0]["body"]["contacts"]  # type: ignore[index]
    assert isinstance(trimmed, list)
    assert 0 < len(trimmed) - 1 < 400
    assert trimmed[-1] == {TRUNCATION_MARKER: True}
    assert kept[0]["body"]["account_id"] == "acct-1"  # type: ignore[index]


def test_a_later_entry_over_the_room_is_trimmed_and_the_earlier_one_kept_whole() -> None:
    from agents.events import TRUNCATION_MARKER

    small: dict[str, object] = {"tool": "get_account", "body": {"account_id": "acct-1"}}
    big: dict[str, object] = {"tool": "search_opportunities", "body": _search_body(300)}
    tail: dict[str, object] = {"tool": "get_contact", "body": {"contact_id": "c-1"}}
    kept, truncated = _bounded([small, big, tail])
    assert truncated
    assert kept[0] == small
    assert kept[1]["tool"] == "search_opportunities"
    assert kept[1]["body"]["items"][-1] == {TRUNCATION_MARKER: True}  # type: ignore[index]
    assert kept[-1] == {TRUNCATION_MARKER: True}
    assert tail not in kept


def test_results_under_the_ceiling_pass_unchanged() -> None:
    entries: list[dict[str, object]] = [
        {"tool": "get_account", "body": {"account_id": "acct-1"}},
        {"tool": "search_opportunities", "body": _search_body(10)},
    ]
    assert _bounded(entries) == (entries, False)
