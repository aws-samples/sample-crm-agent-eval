# SPDX-License-Identifier: MIT
"""Unit tests for :mod:`agents.turn_driver`, the three-tier turn orchestrator.

Drives real turns with a scripted provider and a stub dispatch: delegation and attribution, direct
tool calls, budget and cancellation between steps, a single terminal event, and denied delegation.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

from agents.answer_style import answer_style_system_message
from agents.budget import (
    CHAT_BUDGET,
    CHAT_WALL_CLOCK_SECONDS,
    EVAL_BUDGET,
    EVAL_WALL_CLOCK_SECONDS,
    TurnBudgetSpec,
)
from agents.events import AgentEvent
from agents.registry import RegisteredTool
from agents.supervisor import CANCELLATION_TEXT_PREFIX
from agents.tier_attribution import InvocationAttribution
from agents.turn_driver import (
    TurnRecords,
    drive_turn,
    session_turn_tuning,
    supervisor_system_message,
)
from crm.toolspec.page_context import EntityPageContext, PageContext
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, Tuning
from providers.capabilities import CapabilityMatrix, TierAssignment, load_capability_matrix
from providers.prompt_caching import CACHE_SCOPE_KEY, VOLATILE

MATRIX = load_capability_matrix()
TUNING = Tuning(source="test.turn_driver", family="anthropic", effort=None)
SUPERVISOR_MODEL = MATRIX.model_for("anthropic", "supervisor").model_id
SUBAGENT_MODEL = MATRIX.model_for("anthropic", "subagent").model_id
TOOL_MODEL = MATRIX.model_for("anthropic", "tool").model_id
_UNMETERED_CALLS = re.compile(r"\b(\d+) model call\(s\) ended before reporting token usage")


def _unmetered_call_counts(message: str | None) -> list[int]:
    """Every "N model call(s) ended before reporting token usage" count in `message`, in order."""
    return [int(count) for count in _UNMETERED_CALLS.findall(message or "")]


@dataclass
class _QueueProvider(ModelProvider):
    """Emits one scripted round per `stream` call, popped in call order (shared across all tiers)."""

    rounds: list[list[ProviderEvent]]

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        assert self.rounds, "provider.stream called more times than the test scripted"
        return self._emit(self.rounds.pop(0))

    async def _emit(self, events: list[ProviderEvent]) -> AsyncIterator[ProviderEvent]:
        for event in events:
            yield event


@dataclass
class _StubDispatch:
    """Records dispatch calls and returns a fixed action-Lambda response."""

    response: dict[str, Any]
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def __call__(self, tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        self.calls.append((tool.name, dict(tool_input)))
        return self.response


@dataclass
class _TrippingCancel:
    """A cancel store that reports not-cancelled on the pre-flight check, then cancelled after."""

    trip: int = 2
    calls: int = 0
    terminals: list[str] = field(default_factory=list)

    def is_cancelled(self, session_id: str) -> bool:
        self.calls += 1
        return self.calls >= self.trip

    def mark_terminal(self, session_id: str) -> None:
        self.terminals.append(session_id)


def _drive(
    *,
    rounds: list[list[ProviderEvent]],
    dispatch: _StubDispatch,
    prompt: str = "find acme",
    max_tool_calls: int = 8,
    max_tokens: int = 100_000,
    max_rounds: int | None = None,
    cancellation: Any = None,
) -> tuple[list[AgentEvent], TurnRecords]:
    records = TurnRecords()
    provider = _QueueProvider(rounds)

    async def _run() -> list[AgentEvent]:
        return [
            event
            async for event in drive_turn(
                prompt=prompt,
                session_id="sess-1",
                providers=lambda _family: provider,
                matrix=MATRIX,
                tuning=session_turn_tuning(TUNING),
                budget=TurnBudgetSpec(
                    max_tool_calls=max_tool_calls,
                    max_wall_clock_seconds=EVAL_WALL_CLOCK_SECONDS,
                    max_tokens=max_tokens,
                ),
                dispatch=dispatch,
                records=records,
                max_rounds=max_rounds,
                cancellation=cancellation,
            )
        ]

    return asyncio.run(_run()), records


def _terminal(events: list[AgentEvent]) -> list[AgentEvent]:
    return [e for e in events if e.kind in ("completed", "error")]


def _nested(output: object, *keys: str) -> object:
    """Walk a nested dict-of-dicts tool_output by `keys`, returning None on any non-dict step."""
    cursor: object = output
    for key in keys:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key)
    return cursor


def _delegate_then_answer() -> list[list[ProviderEvent]]:
    return [
        # supervisor round 1: delegate to the research subagent
        [
            ProviderEvent(
                type="tool_call", tool_name="research", tool_input={"intent": "acme"}, tool_id="t1"
            ),
            ProviderEvent(type="usage", input_tokens=100, output_tokens=20),
            ProviderEvent(type="completed", awaiting_tools=True),
        ],
        # subagent round: research calls its get_account leaf tool
        [
            ProviderEvent(
                type="tool_call",
                tool_name="get_account",
                tool_input={"account_id": "acc-1"},
                tool_id="s1",
            ),
            ProviderEvent(type="usage", input_tokens=40, output_tokens=10),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
        # tool round: the actor formulates get_account's args
        [
            ProviderEvent(
                type="tool_call", tool_name="get_account", tool_input={"account_id": "acc-1"}
            ),
            ProviderEvent(type="usage", input_tokens=8, output_tokens=4),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
        # supervisor round 2: final answer
        [
            ProviderEvent(type="text_delta", text="Here is Acme."),
            ProviderEvent(type="usage", input_tokens=50, output_tokens=15),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
    ]


class TestThreeTierDelegation:
    def test_end_to_end_stream_and_per_tier_attribution(self) -> None:
        dispatch = _StubDispatch(
            {"statusCode": 200, "body": {"account_id": "acc-1", "name": "Acme"}}
        )
        events, records = _drive(rounds=_delegate_then_answer(), dispatch=dispatch)

        completed = _terminal(events)
        assert len(completed) == 1
        assert completed[0].kind == "completed"
        assert completed[0].text == "Here is Acme."
        # The UI usage figure covers the WHOLE turn across every tier: supervisor 100+50, subagent
        # 40, tool 8 in => 198; supervisor 20+15, subagent 10, tool 4 out => 49.
        assert completed[0].input_tokens == 198
        assert completed[0].output_tokens == 49

        by_tier: dict[str, list[InvocationAttribution]] = {}
        for inv in records.invocations:
            by_tier.setdefault(inv.tier, []).append(inv)
        assert len(by_tier["supervisor"]) == 2
        assert by_tier["supervisor"][0].model_id == SUPERVISOR_MODEL
        assert len(by_tier["subagent"]) == 1 and by_tier["subagent"][0].model_id == SUBAGENT_MODEL
        assert len(by_tier["tool"]) == 1 and by_tier["tool"][0].model_id == TOOL_MODEL
        assert all(inv.latency_ms is None for inv in records.invocations)  # stub: no wall clock

        assert len(records.tool_calls) == 1
        leaf = records.tool_calls[0]
        assert (leaf.tool_name, leaf.role, leaf.outcome_kind) == ("get_account", "research", "data")

        # The delegation's outcome is fed back to the supervisor as a data tool_result.
        results = [e for e in events if e.kind == "tool_result"]
        assert any(_nested(e.tool_output, "body", "account_id") == "acc-1" for e in results)

    def test_dispatch_receives_the_actor_validated_args(self) -> None:
        dispatch = _StubDispatch({"statusCode": 200, "body": {"account_id": "acc-1"}})
        _drive(rounds=_delegate_then_answer(), dispatch=dispatch)
        assert dispatch.calls == [("get_account", {"account_id": "acc-1"})]


class TestDirectToolCall:
    def test_direct_tool_dispatches_and_is_attributed_to_the_supervisor(self) -> None:
        rounds = [
            [
                ProviderEvent(
                    type="tool_call",
                    tool_name="search_accounts",
                    tool_input={"q": "acme"},
                    tool_id="t1",
                ),
                ProviderEvent(type="usage", input_tokens=30, output_tokens=6),
                ProviderEvent(type="completed", awaiting_tools=True),
            ],
            [
                ProviderEvent(type="text_delta", text="Found them."),
                ProviderEvent(type="usage", input_tokens=20, output_tokens=4),
                ProviderEvent(type="completed", awaiting_tools=False),
            ],
        ]
        dispatch = _StubDispatch({"statusCode": 200, "body": {"items": [{"account_id": "acc-1"}]}})
        events, records = _drive(rounds=rounds, dispatch=dispatch)

        assert dispatch.calls == [("search_accounts", {"q": "acme"})]
        assert _terminal(events)[0].text == "Found them."
        assert len(records.tool_calls) == 1
        call = records.tool_calls[0]
        assert (call.tool_name, call.role, call.outcome_kind) == (
            "search_accounts",
            "supervisor",
            "data",
        )
        # No subagent/tool tiers on a direct call; only supervisor invocations.
        assert {inv.tier for inv in records.invocations} == {"supervisor"}


class TestBudgetAndCancellation:
    def test_budget_exhaustion_names_completed_work(self) -> None:
        # max_tool_calls=1 and the model keeps calling a tool: the one dispatched call completes
        # and is named in partial_work; the next step never starts.
        rounds = [
            [
                ProviderEvent(
                    type="tool_call", tool_name="search_accounts", tool_input={}, tool_id="t1"
                ),
                ProviderEvent(type="usage", input_tokens=10, output_tokens=2),
                ProviderEvent(type="completed", awaiting_tools=True),
            ]
        ]
        dispatch = _StubDispatch({"statusCode": 200, "body": {"items": []}})
        events, _ = _drive(rounds=rounds, dispatch=dispatch, max_tool_calls=1)

        assert dispatch.calls == [("search_accounts", {})]  # the in-flight call completed
        terminal = _terminal(events)
        assert len(terminal) == 1
        assert terminal[0].kind == "error"
        assert terminal[0].error_kind == "budget_exhausted"
        assert "search_accounts" in (terminal[0].partial_work or "")

    def test_subagent_tokens_alone_exhaust_the_budget(self) -> None:
        # The supervisor round spends no tokens; the subagent (40+10) and tool (8+4) tiers spend
        # 62, which alone exceeds max_tokens=30, so the turn ends budget_exhausted.
        rounds = [
            [
                ProviderEvent(
                    type="tool_call",
                    tool_name="research",
                    tool_input={"intent": "acme"},
                    tool_id="t1",
                ),
                ProviderEvent(type="completed", awaiting_tools=True),
            ],
            [
                ProviderEvent(
                    type="tool_call",
                    tool_name="get_account",
                    tool_input={"account_id": "acc-1"},
                    tool_id="s1",
                ),
                ProviderEvent(type="usage", input_tokens=40, output_tokens=10),
                ProviderEvent(type="completed", awaiting_tools=False),
            ],
            [
                ProviderEvent(
                    type="tool_call", tool_name="get_account", tool_input={"account_id": "acc-1"}
                ),
                ProviderEvent(type="usage", input_tokens=8, output_tokens=4),
                ProviderEvent(type="completed", awaiting_tools=False),
            ],
        ]
        dispatch = _StubDispatch({"statusCode": 200, "body": {"account_id": "acc-1"}})
        events, _ = _drive(rounds=rounds, dispatch=dispatch, max_tokens=30)

        terminal = _terminal(events)
        assert len(terminal) == 1
        assert terminal[0].kind == "error"
        assert terminal[0].error_kind == "budget_exhausted"

    def test_in_flight_call_completes_then_between_steps_cancel_terminates(self) -> None:
        # A cancel that lands after the dispatch: the tool_result stands, and the next step is a
        # cancellation terminal, not a second dispatch.
        rounds = [
            [
                ProviderEvent(
                    type="tool_call", tool_name="search_accounts", tool_input={}, tool_id="t1"
                ),
                ProviderEvent(type="usage", input_tokens=10, output_tokens=2),
                ProviderEvent(type="completed", awaiting_tools=True),
            ]
        ]
        dispatch = _StubDispatch({"statusCode": 200, "body": {"items": []}})
        cancel = _TrippingCancel(trip=2)
        events, _ = _drive(rounds=rounds, dispatch=dispatch, cancellation=cancel)

        assert dispatch.calls == [("search_accounts", {})]  # in-flight completed and stays audited
        assert any(e.kind == "tool_result" for e in events)
        terminal = _terminal(events)
        assert len(terminal) == 1  # exactly one terminal event
        assert terminal[0].kind == "completed"
        assert (terminal[0].text or "").startswith(CANCELLATION_TEXT_PREFIX)
        assert cancel.terminals == ["sess-1"]

    def test_a_pre_flight_cancel_returns_before_any_dispatch(self) -> None:
        dispatch = _StubDispatch({"statusCode": 200, "body": {"items": []}})
        cancel = _TrippingCancel(trip=1)  # cancelled from the very first check
        events, _ = _drive(rounds=_delegate_then_answer(), dispatch=dispatch, cancellation=cancel)
        assert dispatch.calls == []  # nothing dispatched
        assert len(_terminal(events)) == 1
        assert (_terminal(events)[0].text or "").startswith(CANCELLATION_TEXT_PREFIX)

    def test_happy_turn_has_exactly_one_terminal_event(self) -> None:
        dispatch = _StubDispatch({"statusCode": 200, "body": {"account_id": "acc-1"}})
        events, _ = _drive(rounds=_delegate_then_answer(), dispatch=dispatch)
        assert len(_terminal(events)) == 1


class TestSecurityMandates:
    def test_drive_turn_holds_no_identity_and_takes_an_injected_dispatch(self) -> None:
        # The driver builds no identity literal; dispatch is the only tool path.
        params = inspect.signature(drive_turn).parameters
        assert "identity" not in params
        assert "dispatch" in params

    def test_out_of_scope_delegation_is_denied(self) -> None:
        # An out-of-scope call is refused: the delegation outcome is denied and fed back verbatim,
        # and the leaf tool-call attribution records the denial.
        rounds = _delegate_then_answer()
        dispatch = _StubDispatch(
            {
                "statusCode": 403,
                "error": {
                    "code": "scope_denied",
                    "missing_scope": "account:acc-1",
                    "message": "Outside your book.",
                },
            }
        )
        events, records = _drive(rounds=rounds, dispatch=dispatch)

        assert records.tool_calls[0].outcome_kind == "denied"
        results = [e for e in events if e.kind == "tool_result"]
        assert any(_nested(e.tool_output, "error", "code") == "scope_denied" for e in results)


@dataclass
class _MessageCapturingProvider(ModelProvider):
    """Records the `messages` of the first `stream` call, then answers with text and terminates."""

    captured: list[list[dict[str, object]]] = field(default_factory=list)

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.captured.append([dict(message) for message in messages])
        return self._emit()

    async def _emit(self) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(type="text_delta", text="ok")
        yield ProviderEvent(type="usage", input_tokens=1, output_tokens=1)
        yield ProviderEvent(type="completed", input_tokens=1, output_tokens=1)


def _capture_first_messages(
    *,
    answer_style_message: dict[str, object] | None,
    supervisor_message: dict[str, object] | None = None,
    page_context: PageContext | None = None,
) -> list[dict[str, object]]:
    provider = _MessageCapturingProvider()
    records = TurnRecords()

    async def _run() -> None:
        async for _event in drive_turn(
            prompt="hi",
            session_id="sess-style",
            providers=lambda _family: provider,
            matrix=MATRIX,
            tuning=session_turn_tuning(TUNING),
            budget=EVAL_BUDGET,
            dispatch=_StubDispatch({"statusCode": 200, "body": {}}),
            records=records,
            answer_style_message=answer_style_message,
            supervisor_message=supervisor_message,
            page_context=page_context,
        ):
            pass

    asyncio.run(_run())
    return provider.captured[0]


def test_drive_turn_sends_the_supervisor_prompt_then_the_answer_style_then_the_prompt() -> None:
    # With no overrides, the driver self-loads both shipped system messages, supervisor first.
    messages = _capture_first_messages(answer_style_message=None)
    assert messages == [
        supervisor_system_message(),
        answer_style_system_message(),
        {"role": "user", "content": "hi"},
    ]
    assert messages[0]["role"] == "system"
    assert str(messages[0]["content"]).startswith("You are the assistant in a CRM")


def test_drive_turn_uses_the_answer_style_override_when_one_is_passed() -> None:
    # When supplied, the override replaces the self-loaded message in second place.
    override: dict[str, object] = {"role": "system", "content": "OVERRIDE STYLE"}
    messages = _capture_first_messages(answer_style_message=override)
    assert messages[1] == override
    assert messages[1] != answer_style_system_message()


def test_only_the_page_briefing_is_marked_volatile_for_prompt_caching() -> None:
    """The per-request briefing carries the volatile cache scope; the stable prefix does not."""
    context = EntityPageContext(route="/accounts/a-1", entity_type="accounts", entity_id="a-1")
    messages = _capture_first_messages(answer_style_message=None, page_context=context)
    assert [m.get(CACHE_SCOPE_KEY) for m in messages] == [None, None, VOLATILE, None]
    assert messages[2]["role"] == "system"


def test_drive_turn_uses_the_supervisor_override_when_one_is_passed() -> None:
    override: dict[str, object] = {"role": "system", "content": "OVERRIDE SUPERVISOR"}
    messages = _capture_first_messages(answer_style_message=None, supervisor_message=override)
    assert messages[0] == override
    assert messages[1] == answer_style_system_message()


@dataclass
class _CapturingQueueProvider(_QueueProvider):
    """A `_QueueProvider` that also records the messages of every `stream` call, in call order."""

    seen: list[list[dict[str, object]]] = field(default_factory=list)

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.seen.append([dict(message) for message in messages])
        return super().stream(invocation, messages, tools)


def test_an_empty_intent_delegates_the_users_prompt_not_the_supervisors_narration() -> None:
    """A blank `intent` falls back to the user's own request, never to text the model emitted."""
    narration = "Let me check that."
    provider = _CapturingQueueProvider(
        [
            [
                ProviderEvent(type="text_delta", text=narration),
                ProviderEvent(
                    type="tool_call", tool_name="research", tool_input={"intent": " "}, tool_id="t1"
                ),
                ProviderEvent(type="usage", input_tokens=10, output_tokens=2),
                ProviderEvent(type="completed", awaiting_tools=True),
            ],
            [
                ProviderEvent(type="text_delta", text="Nothing to read."),
                ProviderEvent(type="usage", input_tokens=5, output_tokens=2),
                ProviderEvent(type="completed", awaiting_tools=False),
            ],
            [
                ProviderEvent(type="text_delta", text="Done."),
                ProviderEvent(type="usage", input_tokens=5, output_tokens=2),
                ProviderEvent(type="completed", awaiting_tools=False),
            ],
        ]
    )

    async def _run() -> None:
        async for _event in drive_turn(
            prompt="which accounts slipped this quarter?",
            session_id="sess-intent",
            providers=lambda _family: provider,
            matrix=MATRIX,
            tuning=session_turn_tuning(TUNING),
            budget=EVAL_BUDGET,
            dispatch=_StubDispatch({"statusCode": 200, "body": {}}),
            records=TurnRecords(),
        ):
            pass

    asyncio.run(_run())
    subagent_messages = provider.seen[1]
    delegated = [m["content"] for m in subagent_messages if m["role"] == "user"]
    assert delegated[-1] == "which accounts slipped this quarter?"
    assert all(narration not in str(m["content"]) for m in subagent_messages)


@dataclass
class _EffortRecordingProvider(ModelProvider):
    """Scripts one round per `stream` call (like `_QueueProvider`) and records `(model_id, effort)`."""

    rounds: list[list[ProviderEvent]]
    seen: list[tuple[str, str | None]] = field(default_factory=list)

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.seen.append((invocation.model.model_id, invocation.effort))
        assert self.rounds, "provider.stream called more times than the test scripted"
        return self._emit(self.rounds.pop(0))

    async def _emit(self, events: list[ProviderEvent]) -> AsyncIterator[ProviderEvent]:
        for event in events:
            yield event


def _matrix_with_the_unassigned_anthropic_model_on_the_supervisor_tier() -> CapabilityMatrix:
    """The real matrix with its unassigned anthropic model configured on the supervisor tier."""
    assigned = {assignment.model_id for assignment in MATRIX.tiers["anthropic"].values()}
    unassigned = next(
        model_id
        for model_id, model in MATRIX.models.items()
        if model.family == "anthropic" and model_id not in assigned
    )
    tiers = {family: dict(assignments) for family, assignments in MATRIX.tiers.items()}
    tiers["anthropic"]["supervisor"] = TierAssignment(
        model_id=unassigned, tuning=MATRIX.tiers["anthropic"]["supervisor"].tuning
    )
    return replace(MATRIX, tiers=tiers)


def test_effort_applies_to_the_supervisor_tier_only_so_a_delegating_turn_completes() -> None:
    """A requested effort reaches the supervisor tier only; lower tiers run their models.yaml effort.

    The matrix puts a model on the supervisor tier that no lower tier shares. `effort="max"` is
    accepted by that model but not by the subagent model, and differs from every tier's configured
    effort.
    """
    matrix = _matrix_with_the_unassigned_anthropic_model_on_the_supervisor_tier()
    supervisor_model = matrix.model_for("anthropic", "supervisor").model_id
    assert supervisor_model not in {SUBAGENT_MODEL, TOOL_MODEL}
    assert "max" in matrix.models[supervisor_model].effort.values
    assert "max" not in matrix.models[SUBAGENT_MODEL].effort.values
    tuning = Tuning(source="test.effort", family="anthropic", effort="max")
    provider = _EffortRecordingProvider(_delegate_then_answer())
    records = TurnRecords()
    dispatch = _StubDispatch({"statusCode": 200, "body": {"account_id": "acc-1", "name": "Acme"}})

    async def _run() -> list[AgentEvent]:
        return [
            event
            async for event in drive_turn(
                prompt="acme",
                session_id="s-effort",
                providers=lambda _family: provider,
                matrix=matrix,
                tuning=session_turn_tuning(tuning),
                budget=EVAL_BUDGET,
                dispatch=dispatch,
                records=records,
            )
        ]

    events = asyncio.run(_run())

    # The turn completes rather than raising ValidationFailed at the tool tier.
    terminal = _terminal(events)
    assert len(terminal) == 1
    assert terminal[0].kind == "completed"

    efforts: dict[str, set[str | None]] = {}
    for model_id, effort in provider.seen:
        efforts.setdefault(model_id, set()).add(effort)
    configured = matrix.tiers["anthropic"]
    assert efforts[supervisor_model] == {"max"}
    assert efforts[SUBAGENT_MODEL] == {configured["subagent"].tuning["effort"]}
    assert efforts[TOOL_MODEL] == {configured["tool"].tuning["effort"]}
    assert efforts[TOOL_MODEL] != {MATRIX.models[TOOL_MODEL].effort.default}


# --- the turn's wall clock bounds model waits and delegations --------------------------

#: Outer bound on each wall-clock test; a turn that ignores its clock stalls far past it.
_STALL_TEST_CAP_SECONDS = 5.0


def _supervisor_delegates() -> list[ProviderEvent]:
    return [
        ProviderEvent(
            type="tool_call", tool_name="research", tool_input={"intent": "acme"}, tool_id="t1"
        ),
        ProviderEvent(type="usage", input_tokens=100, output_tokens=20),
        ProviderEvent(type="completed", awaiting_tools=True),
    ]


def _subagent_calls_a_leaf_and_continues() -> list[ProviderEvent]:
    return [
        ProviderEvent(
            type="tool_call",
            tool_name="get_account",
            tool_input={"account_id": "acc-1"},
            tool_id="s1",
        ),
        ProviderEvent(type="usage", input_tokens=40, output_tokens=10),
        ProviderEvent(type="completed", awaiting_tools=True),
    ]


def _tool_tier_formulates() -> list[ProviderEvent]:
    return [
        ProviderEvent(
            type="tool_call", tool_name="get_account", tool_input={"account_id": "acc-1"}
        ),
        ProviderEvent(type="usage", input_tokens=8, output_tokens=4),
        ProviderEvent(type="completed", awaiting_tools=False),
    ]


@dataclass
class _StallingQueueProvider(ModelProvider):
    """Plays the scripted rounds in call order; every call after them never answers."""

    rounds: list[list[ProviderEvent]]
    calls: int = 0

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.calls += 1
        return self._emit(self.rounds.pop(0)) if self.rounds else self._stall()

    async def _emit(self, events: list[ProviderEvent]) -> AsyncIterator[ProviderEvent]:
        for event in events:
            yield event

    async def _stall(self) -> AsyncIterator[ProviderEvent]:
        await asyncio.sleep(3600)
        yield ProviderEvent(type="completed", awaiting_tools=False)


def _drive_bounded(
    *,
    provider: ModelProvider,
    dispatch: Any,
    budget: TurnBudgetSpec,
    clock: Any = None,
) -> tuple[list[AgentEvent], TurnRecords]:
    records = TurnRecords()

    async def _run() -> list[AgentEvent]:
        return [
            event
            async for event in drive_turn(
                prompt="find acme",
                session_id="sess-clock",
                providers=lambda _family: provider,
                matrix=MATRIX,
                tuning=session_turn_tuning(TUNING),
                budget=budget,
                dispatch=dispatch,
                records=records,
                clock=clock,
            )
        ]

    return asyncio.run(asyncio.wait_for(_run(), timeout=_STALL_TEST_CAP_SECONDS)), records


class TestWallClockInsideDelegation:
    def test_a_stalled_subagent_round_ends_the_turn_budget_exhausted_with_its_partial_work(
        self,
    ) -> None:
        """The subagent's second round never answers; the turn ends at its cap, work preserved."""
        provider = _StallingQueueProvider(
            [
                _supervisor_delegates(),
                _subagent_calls_a_leaf_and_continues(),
                _tool_tier_formulates(),
            ]
        )
        dispatch = _StubDispatch({"statusCode": 200, "body": {"account_id": "acc-1"}})
        events, records = _drive_bounded(
            provider=provider,
            dispatch=dispatch,
            budget=TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=0.3),
        )
        terminal = _terminal(events)
        assert len(terminal) == 1
        assert terminal[0].error_kind == "budget_exhausted"
        assert "wall_clock" in (terminal[0].error_message or "")
        assert _unmetered_call_counts(terminal[0].error_message) == [2]
        assert "called get_account" in (terminal[0].partial_work or "")
        assert dispatch.calls == [("get_account", {"account_id": "acc-1"})]
        assert [e.kind for e in events if e.kind in ("tool_call", "tool_result")] == [
            "tool_call",
            "tool_call",
            "tool_result",
        ]
        assert sorted(inv.tier for inv in records.invocations) == ["subagent", "tool"]
        assert [call.tool_name for call in records.tool_calls] == ["get_account"]

    def test_a_stalled_supervisor_round_ends_the_turn_budget_exhausted(self) -> None:
        """A supervisor model wait past the cap ends the turn with the budget event, not a hang."""
        provider = _StallingQueueProvider([])
        events, _ = _drive_bounded(
            provider=provider,
            dispatch=_StubDispatch({"statusCode": 200, "body": {}}),
            budget=TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=0.2),
        )
        terminal = _terminal(events)
        assert len(terminal) == 1
        assert terminal[0].error_kind == "budget_exhausted"
        assert _unmetered_call_counts(terminal[0].error_message) == [1]
        assert "no tool calls completed" in (terminal[0].partial_work or "")

    def test_the_chat_cap_is_checked_between_subagent_rounds(self) -> None:
        """Under CHAT_BUDGET, a leaf dispatch that runs past 600 s starts no further subagent round."""
        assert CHAT_BUDGET.max_wall_clock_seconds == CHAT_WALL_CLOCK_SECONDS
        now = [0.0]
        response = {"statusCode": 200, "body": {"account_id": "acc-1"}}

        def _slow_dispatch(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
            now[0] += CHAT_WALL_CLOCK_SECONDS
            return response

        provider = _StallingQueueProvider(
            [
                _supervisor_delegates(),
                _subagent_calls_a_leaf_and_continues(),
                _tool_tier_formulates(),
                [ProviderEvent(type="completed", awaiting_tools=False, text="unreached")],
            ]
        )
        events, _ = _drive_bounded(
            provider=provider, dispatch=_slow_dispatch, budget=CHAT_BUDGET, clock=lambda: now[0]
        )
        assert provider.calls == 3, "a further subagent round started after the chat cap"
        terminal = _terminal(events)
        assert len(terminal) == 1
        assert terminal[0].error_kind == "budget_exhausted"
        assert "600s cap" in (terminal[0].error_message or "")

    def test_a_stalled_tool_tier_counts_each_of_its_three_unmetered_calls(self) -> None:
        """The tool tier stalls past the cap: it, its subagent round and the supervisor round count."""
        provider = _StallingQueueProvider(
            [_supervisor_delegates(), _subagent_calls_a_leaf_and_continues()]
        )
        events, records = _drive_bounded(
            provider=provider,
            dispatch=_StubDispatch({"statusCode": 200, "body": {"account_id": "acc-1"}}),
            budget=TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=0.3),
        )
        assert provider.calls == 3
        assert records.invocations == []
        terminal = _terminal(events)
        assert len(terminal) == 1
        assert terminal[0].error_kind == "budget_exhausted"
        assert _unmetered_call_counts(terminal[0].error_message) == [3]

    def test_the_chat_cap_counts_the_subagent_round_it_cut_after_its_first_event(self) -> None:
        """A leaf dispatch runs past the chat cap mid-round: that round and the supervisor's count."""
        now = [0.0]
        response = {"statusCode": 200, "body": {"account_id": "acc-1"}}

        def _slow_dispatch(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
            now[0] += CHAT_WALL_CLOCK_SECONDS
            return response

        provider = _StallingQueueProvider(
            [
                _supervisor_delegates(),
                _subagent_calls_a_leaf_and_continues(),
                _tool_tier_formulates(),
            ]
        )
        events, records = _drive_bounded(
            provider=provider, dispatch=_slow_dispatch, budget=CHAT_BUDGET, clock=lambda: now[0]
        )
        assert sorted(inv.tier for inv in records.invocations) == ["tool"]
        terminal = _terminal(events)
        assert len(terminal) == 1
        assert terminal[0].error_kind == "budget_exhausted"
        assert _unmetered_call_counts(terminal[0].error_message) == [2]


class TestUsageOfAStoppedRound:
    def _stopped_after_one_call(self, *, usage_first: bool) -> AgentEvent:
        call = ProviderEvent(
            type="tool_call", tool_name="search_accounts", tool_input={}, tool_id="t1"
        )
        usage = ProviderEvent(type="usage", input_tokens=10, output_tokens=2)
        round_events = [usage, call] if usage_first else [call, usage]
        rounds = [[*round_events, ProviderEvent(type="completed", awaiting_tools=True)]]
        dispatch = _StubDispatch({"statusCode": 200, "body": {"items": []}})
        events, _ = _drive(rounds=rounds, dispatch=dispatch, max_tool_calls=1)
        terminal = _terminal(events)
        assert len(terminal) == 1
        assert terminal[0].error_kind == "budget_exhausted"
        return terminal[0]

    def test_a_round_stopped_before_its_usage_flags_the_usage_as_a_lower_bound(self) -> None:
        """The tool-call cap stops the round mid-stream; its unreported usage is named."""
        terminal = self._stopped_after_one_call(usage_first=False)
        assert _unmetered_call_counts(terminal.error_message) == [1]

    def test_a_round_whose_usage_arrived_first_is_not_flagged(self) -> None:
        """When the round's usage was already recorded, nothing is flagged as unmetered."""
        terminal = self._stopped_after_one_call(usage_first=True)
        assert "ended before reporting" not in (terminal.error_message or "")


@dataclass
class _PacedQueueProvider(ModelProvider):
    """As :class:`_StallingQueueProvider`, but a float in a round sleeps that many seconds."""

    rounds: list[list[ProviderEvent | float]]
    calls: int = 0

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.calls += 1
        return self._emit(self.rounds.pop(0)) if self.rounds else self._stall()

    async def _emit(self, items: list[ProviderEvent | float]) -> AsyncIterator[ProviderEvent]:
        for item in items:
            if isinstance(item, float):
                await asyncio.sleep(item)
                continue
            yield item

    async def _stall(self) -> AsyncIterator[ProviderEvent]:
        await asyncio.sleep(3600)
        yield ProviderEvent(type="completed", awaiting_tools=False)


def _subagent_tokens(records: TurnRecords) -> list[tuple[int, int]]:
    return [
        (inv.tokens.input_tokens, inv.tokens.output_tokens)
        for inv in records.invocations
        if inv.tier == "subagent"
    ]


class TestAMeteredCallIsCountedOnce:
    """A call that reported usage is an invocation and never also an unmetered note."""

    _CAP = TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=0.3)

    def test_a_subagent_round_cut_after_its_usage_is_metered_and_not_noted(self) -> None:
        """Probe A: usage and completed arrive, then the stream's close outlives the cap."""
        provider = _PacedQueueProvider(
            [
                [*_supervisor_delegates()],
                [
                    ProviderEvent(type="usage", input_tokens=40, output_tokens=10),
                    ProviderEvent(type="completed", awaiting_tools=False),
                    1.0,
                ],
            ]
        )
        events, records = _drive_bounded(
            provider=provider,
            dispatch=_StubDispatch({"statusCode": 200, "body": {}}),
            budget=self._CAP,
        )
        [terminal] = _terminal(events)
        assert terminal.error_kind == "budget_exhausted"
        assert _unmetered_call_counts(terminal.error_message) == [1]
        assert _subagent_tokens(records) == [(40, 10)]

    def test_a_leaf_deadline_after_the_rounds_usage_keeps_the_rounds_tokens(self) -> None:
        """Probe B: usage first, then the leaf's tool tier stalls past the cap."""
        provider = _PacedQueueProvider(
            [
                [*_supervisor_delegates()],
                [
                    ProviderEvent(type="usage", input_tokens=40, output_tokens=10),
                    ProviderEvent(
                        type="tool_call",
                        tool_name="get_account",
                        tool_input={"account_id": "acc-1"},
                        tool_id="s1",
                    ),
                    ProviderEvent(type="completed", awaiting_tools=True),
                ],
            ]
        )
        events, records = _drive_bounded(
            provider=provider,
            dispatch=_StubDispatch({"statusCode": 200, "body": {}}),
            budget=self._CAP,
        )
        assert provider.calls == 3
        [terminal] = _terminal(events)
        assert terminal.error_kind == "budget_exhausted"
        assert _unmetered_call_counts(terminal.error_message) == [2]
        assert _subagent_tokens(records) == [(40, 10)]

    def test_a_supervisor_round_cut_after_its_usage_is_not_noted(self) -> None:
        """The supervisor's usage arrives, then its next item outlives the cap: no note."""
        provider = _PacedQueueProvider(
            [[ProviderEvent(type="usage", input_tokens=10, output_tokens=2), 1.0]]
        )
        events, records = _drive_bounded(
            provider=provider,
            dispatch=_StubDispatch({"statusCode": 200, "body": {}}),
            budget=self._CAP,
        )
        [terminal] = _terminal(events)
        assert terminal.error_kind == "budget_exhausted"
        assert "ended before reporting" not in (terminal.error_message or "")
        assert [inv.tier for inv in records.invocations] == ["supervisor"]
