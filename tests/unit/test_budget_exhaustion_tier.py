# SPDX-License-Identifier: MIT
"""Where a turn's budget ran out: :attr:`agents.budget.TurnBudget.exhausted_tier`.

Each detection site records its tier once, the first writer wins, and
:attr:`agents.turn_driver.TurnRecords.budget_exhausted_tier` exposes it after the turn. Also covers
the supervisor direct call's truncation flag, read from the tool result itself.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from agents.budget import CHAT_BUDGET, TurnBudget, TurnBudgetSpec
from agents.events import AgentEvent
from agents.registry import RegisteredTool
from agents.turn_driver import TurnRecords, drive_turn
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from providers.capabilities import CapabilityMatrix, Family, Tier
from tests.unit.test_turn_driver_tiers import MATRIX, MIXED, _delegating_rounds


@dataclass
class _Clock:
    """A settable monotonic clock shared by the budget and the scripted providers."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class _Round:
    """One scripted provider round.

    ``advance_after`` moves the settable clock past any deadline before the event at that index
    (``len(events)``: after the last one); ``sleep_seconds`` waits in real time before the first.
    """

    events: list[ProviderEvent]
    advance_after: int | None = None
    sleep_seconds: float = 0.0


@dataclass
class _Script:
    rounds: list[_Round]
    clock: _Clock


@dataclass
class _Provider(ModelProvider):
    family: Family
    script: _Script

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools
        assert invocation.model.family == self.family
        assert self.script.rounds, "provider.stream called more times than scripted"
        return self._emit(self.script.rounds.pop(0))

    async def _emit(self, scripted: _Round) -> AsyncIterator[ProviderEvent]:
        if scripted.sleep_seconds:
            await asyncio.sleep(scripted.sleep_seconds)
        for index, event in enumerate(scripted.events):
            if scripted.advance_after == index:
                self.script.clock.now += 10_000.0
            yield event
        if scripted.advance_after == len(scripted.events):
            self.script.clock.now += 10_000.0


def _ok_dispatch(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
    del tool, tool_input
    return {"statusCode": 200, "body": {"account_id": "acc-1", "name": "Acme"}}


def _drive(
    rounds: list[_Round],
    *,
    budget: TurnBudgetSpec = CHAT_BUDGET,
    matrix: CapabilityMatrix = MATRIX,
    dispatch: Callable[[RegisteredTool, dict[str, object]], dict[str, Any]] = _ok_dispatch,
    max_rounds: int | None = None,
    clock: _Clock | None = None,
    real_clock: bool = False,
) -> tuple[list[AgentEvent], TurnRecords]:
    settable = clock if clock is not None else _Clock()
    turn_clock: Callable[[], float] = time.monotonic if real_clock else settable
    script = _Script(rounds, settable)
    records = TurnRecords()

    async def _run() -> list[AgentEvent]:
        return [
            event
            async for event in drive_turn(
                prompt="find acme",
                session_id="sess-exhaustion",
                providers=lambda family: _Provider(family, script),
                matrix=matrix,
                tuning=MIXED,
                budget=budget,
                dispatch=dispatch,
                records=records,
                max_rounds=max_rounds,
                clock=turn_clock,
            )
        ]

    return asyncio.run(_run()), records


def _direct_call_round(tool_name: str = "search_accounts") -> _Round:
    return _Round(
        [
            ProviderEvent(
                type="tool_call", tool_name=tool_name, tool_input={"q": "a"}, tool_id="t1"
            ),
            ProviderEvent(type="usage", input_tokens=1, output_tokens=1),
            ProviderEvent(type="completed", awaiting_tools=True),
        ]
    )


def _answer_round() -> _Round:
    return _Round(
        [
            ProviderEvent(type="text_delta", text="Done."),
            ProviderEvent(type="usage", input_tokens=1, output_tokens=1),
            ProviderEvent(type="completed", awaiting_tools=False),
        ]
    )


def _assert_exhausted(events: list[AgentEvent]) -> None:
    assert events[-1].kind == "error"
    assert events[-1].error_kind == "budget_exhausted"


def _with_limit(tier: Tier, limit: int) -> CapabilityMatrix:
    return dataclasses.replace(
        MATRIX, request_prompt_limits={**MATRIX.request_prompt_limits, tier: limit}
    )


# -- TurnBudget ---------------------------------------------------------------------------------


def test_a_fresh_budget_records_no_exhaustion_tier() -> None:
    assert TurnBudget().exhausted_tier is None


def test_the_first_recorded_tier_wins() -> None:
    budget = TurnBudget()
    budget.note_exhausted_at("tool")
    budget.note_exhausted_at("supervisor")
    assert budget.exhausted_tier == "tool"


def test_a_refused_request_records_the_refusals_tier() -> None:
    budget = TurnBudget()
    budget.refuse_request(estimated_prompt_tokens=10, limit=5, tier="subagent", role="research")
    assert budget.exhausted_tier == "subagent"


def test_a_call_abandoned_at_the_deadline_records_its_tier() -> None:
    budget = TurnBudget()
    budget.note_call_without_usage(tier="tool", role="research", cause="deadline")
    assert budget.exhausted_tier == "tool"


@pytest.mark.parametrize("cause", ["turn_stopped", "provider_error", "partial_usage"])
def test_a_call_unmetered_for_another_cause_records_no_tier(cause: str) -> None:
    budget = TurnBudget()
    budget.note_call_without_usage(tier="tool", role="research", cause=cause)
    assert budget.exhausted_tier is None


def test_an_unknown_tier_is_refused() -> None:
    with pytest.raises(ValueError, match="tier must be one of"):
        TurnBudget().note_call_without_usage(tier="router", role="r", cause="deadline")


# -- drive_turn detection sites -----------------------------------------------------------------


def test_a_completed_turn_records_no_exhaustion_tier() -> None:
    events, records = _drive([_answer_round()])
    assert events[-1].kind == "completed"
    assert records.budget_exhausted_tier is None


def test_records_report_no_tier_before_a_turn_starts() -> None:
    assert TurnRecords().budget_exhausted_tier is None


def test_the_tool_call_cap_is_recorded_at_the_supervisor() -> None:
    events, records = _drive(
        [_direct_call_round()], budget=TurnBudgetSpec(max_tool_calls=1, max_wall_clock_seconds=60.0)
    )
    _assert_exhausted(events)
    assert records.budget_exhausted_tier == "supervisor"


def test_a_leaf_refused_at_the_tool_call_cap_is_recorded_at_the_tool_tier() -> None:
    supervisor, subagent, tool, answer = _delegating_rounds()
    two_leaves = [subagent[0], *subagent]
    events, records = _drive(
        [_Round(supervisor), _Round(two_leaves), _Round(tool), _Round(tool), _Round(answer)],
        budget=TurnBudgetSpec(max_tool_calls=1, max_wall_clock_seconds=60.0),
    )
    _assert_exhausted(events)
    assert records.budget_exhausted_tier == "tool"
    assert len(records.tool_calls) == 1


def test_a_cap_reached_exactly_by_leaves_is_detected_at_the_supervisor() -> None:
    rounds = [_Round(events) for events in _delegating_rounds()]
    events, records = _drive(
        rounds, budget=TurnBudgetSpec(max_tool_calls=1, max_wall_clock_seconds=60.0)
    )
    _assert_exhausted(events)
    assert records.budget_exhausted_tier == "supervisor"
    assert len(records.tool_calls) == 1


def test_the_token_cap_is_recorded_at_the_supervisor() -> None:
    events, records = _drive(
        [_direct_call_round()],
        budget=TurnBudgetSpec(max_tool_calls=10, max_tokens=2, max_wall_clock_seconds=60.0),
    )
    _assert_exhausted(events)
    assert records.budget_exhausted_tier == "supervisor"


def test_the_round_backstop_is_recorded_at_the_supervisor() -> None:
    events, records = _drive([_direct_call_round()], max_rounds=1)
    _assert_exhausted(events)
    assert records.budget_exhausted_tier == "supervisor"


def test_a_supervisor_call_cut_off_at_the_deadline_is_recorded_at_the_supervisor() -> None:
    events, records = _drive(
        [_Round(_direct_call_round().events, advance_after=1), _answer_round()],
        budget=TurnBudgetSpec(max_tool_calls=10, max_wall_clock_seconds=60.0),
    )
    _assert_exhausted(events)
    assert records.budget_exhausted_tier == "supervisor"


def test_a_refused_supervisor_request_is_recorded_at_the_supervisor() -> None:
    events, records = _drive([_answer_round()], matrix=_with_limit("supervisor", 1))
    _assert_exhausted(events)
    assert records.budget_exhausted_tier == "supervisor"


def test_a_refused_subagent_request_is_recorded_at_the_subagent() -> None:
    rounds = [_Round(events) for events in _delegating_rounds()]
    events, records = _drive(rounds, matrix=_with_limit("subagent", 1))
    _assert_exhausted(events)
    assert records.budget_exhausted_tier == "subagent"


def test_a_refused_tool_tier_request_is_recorded_at_the_tool_tier() -> None:
    rounds = [_Round(events) for events in _delegating_rounds()]
    events, records = _drive(rounds, matrix=_with_limit("tool", 1))
    _assert_exhausted(events)
    assert records.budget_exhausted_tier == "tool"


def _cut_off_round() -> _Round:
    """A round whose clock passes the deadline before its first event, so it ends unmetered."""
    return _Round(
        [
            ProviderEvent(type="text_delta", text="working"),
            ProviderEvent(type="usage", input_tokens=1, output_tokens=1),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
        advance_after=0,
    )


def test_a_subagent_call_cut_off_at_the_deadline_is_recorded_at_the_subagent() -> None:
    supervisor, *_ = _delegating_rounds()
    events, records = _drive(
        [_Round(supervisor), _cut_off_round()],
        budget=TurnBudgetSpec(max_tool_calls=10, max_wall_clock_seconds=60.0),
    )
    _assert_exhausted(events)
    assert records.budget_exhausted_tier == "subagent"


def test_a_tool_tier_call_cut_off_at_the_deadline_is_recorded_at_the_tool_tier() -> None:
    supervisor, subagent, tool, _ = _delegating_rounds()
    events, records = _drive(
        [_Round(supervisor), _Round(subagent), _Round(tool, sleep_seconds=5.0)],
        budget=TurnBudgetSpec(max_tool_calls=10, max_wall_clock_seconds=0.5),
        real_clock=True,
    )
    _assert_exhausted(events)
    assert records.budget_exhausted_tier == "tool"


def test_a_deadline_seen_at_the_subagent_round_boundary_is_recorded_at_the_subagent() -> None:
    supervisor, subagent, tool, _ = _delegating_rounds()
    subagent_continues = [*subagent[:-1], ProviderEvent(type="completed", awaiting_tools=True)]
    events, records = _drive(
        [
            _Round(supervisor),
            _Round(subagent_continues, advance_after=len(subagent_continues)),
            _Round(tool),
        ],
        budget=TurnBudgetSpec(max_tool_calls=10, max_wall_clock_seconds=60.0),
    )
    _assert_exhausted(events)
    assert records.budget_exhausted_tier == "subagent"


# -- supervisor direct-call truncation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "truncated"),
    [
        ({"items": [], "truncated": True, "examined_count": 50}, True),
        ({"items": [], "truncated": False}, False),
        ({"items": []}, False),
    ],
)
def test_a_supervisor_direct_call_records_the_truncation_its_result_reports(
    body: dict[str, object], truncated: bool
) -> None:
    def _dispatch(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        del tool, tool_input
        return {"statusCode": 200, "body": body}

    _, records = _drive(
        [_direct_call_round("search_opportunities"), _answer_round()], dispatch=_dispatch
    )
    (call,) = records.tool_calls
    assert call.role == "supervisor"
    assert call.truncated is truncated
