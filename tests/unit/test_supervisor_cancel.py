# SPDX-License-Identifier: MIT
"""Tests for the between-steps cancellation check in :class:`agents.supervisor.TurnGuard`.

Covers: a marker set mid-turn stops the turn before the next step; a step in flight completes and
stays recorded; a cancel after a terminal event is a no-op; cancellation adds no delegation outcome.
"""

from __future__ import annotations

import pytest

from agents.budget import EVAL_WALL_CLOCK_SECONDS, TurnBudget
from agents.cancellation import InMemoryCancellationStore, SessionOwner
from agents.events import EVENT_KINDS, AgentEvent
from agents.supervisor import (
    CANCELLATION_TEXT_PREFIX,
    SubagentOutcome,
    SubagentOutcomeKind,
    TurnGuard,
    cancellation_event,
    is_cancellation_event,
    synthesize_reply,
)
from crm.authz import Identity
from crm.errors import ScopeDenied

_SESSION = "session-abc"


class _FakeClock:
    """A monotonic clock a test advances explicitly, in seconds."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _identity(*, rep_id: str = "rep-1", tenant: str = "demo") -> Identity:
    return Identity(
        rep_id=rep_id,
        book=frozenset({"acc-1"}),
        full_visibility=False,
        principal_type="user",
        session_id=_SESSION,
        source="apigw",
        tenant=tenant,
    )


def _guard(
    *,
    clock: _FakeClock | None = None,
    cancellation: InMemoryCancellationStore | None = None,
    max_tool_calls: int = 1_000,
) -> tuple[TurnGuard, InMemoryCancellationStore]:
    """Build a guard over a real budget and a real, owner-registered marker store."""
    store = cancellation if cancellation is not None else InMemoryCancellationStore()
    store.register_turn(_SESSION, owner=SessionOwner.of(_identity()))
    budget = TurnBudget(
        max_tool_calls=max_tool_calls,
        max_tokens=1_000_000,
        clock=clock if clock is not None else _FakeClock(),
    )
    return TurnGuard(session_id=_SESSION, budget=budget, cancellation=store), store


# --------------------------------------------------------------------------------------
# The guard continues while nothing has asked it to stop
# --------------------------------------------------------------------------------------


def test_an_unmarked_turn_is_not_asked_to_stop() -> None:
    guard, _ = _guard()
    guard.note_step("called get_account")
    assert guard.cancel_requested() is False
    assert guard.terminal_event() is None


def test_a_turn_with_no_cancel_channel_wired_can_still_only_be_stopped_by_its_budget() -> None:
    clock = _FakeClock()
    guard = TurnGuard(
        session_id=_SESSION,
        budget=TurnBudget(max_tool_calls=1_000, max_tokens=1_000_000, clock=clock),
        cancellation=None,
    )
    assert guard.cancel_requested() is False
    assert guard.terminal_event() is None

    clock.advance(EVAL_WALL_CLOCK_SECONDS)

    event = guard.terminal_event()
    assert event is not None
    assert event.error_kind == "budget_exhausted"


# --------------------------------------------------------------------------------------
# A marker set mid-turn terminates before the next step
# --------------------------------------------------------------------------------------


def test_a_marker_set_mid_turn_terminates_the_turn() -> None:
    guard, store = _guard()
    guard.note_step("called get_account")

    store.request_cancellation(_SESSION, requester=SessionOwner.of(_identity()))

    assert guard.cancel_requested() is True
    event = guard.terminal_event()
    assert event is not None
    assert event.kind == "completed"


def test_the_terminal_event_names_the_step_the_turn_stopped_at() -> None:
    """The terminal event records the turn as cancelled, with the step it stopped at."""
    guard, store = _guard()
    guard.note_step("called search_opportunities")
    guard.note_step("called get_opportunity")
    store.request_cancellation(_SESSION, requester=SessionOwner.of(_identity()))

    event = guard.terminal_event()

    assert event is not None
    assert event.text is not None
    assert "cancelled" in event.text.lower()
    assert "2 step(s)" in event.text
    assert "get_opportunity" in event.text


def test_a_cancel_before_any_step_completed_says_so_rather_than_naming_a_step() -> None:
    guard, store = _guard()
    store.request_cancellation(_SESSION, requester=SessionOwner.of(_identity()))

    event = guard.terminal_event()

    assert event is not None
    assert event.text == f"{CANCELLATION_TEXT_PREFIX} Turn cancelled before any step completed."


def test_the_next_step_never_starts_once_the_marker_is_set() -> None:
    """A marker landing between two steps of a check-step loop stops the loop before step two."""
    guard, store = _guard()
    planned_steps = ["called get_account", "called list_contacts", "called log_activity"]
    executed: list[str] = []

    for index, step in enumerate(planned_steps):
        if guard.terminal_event() is not None:
            break
        executed.append(step)
        guard.note_step(step)
        if index == 0:
            store.request_cancellation(_SESSION, requester=SessionOwner.of(_identity()))

    assert executed == ["called get_account"]
    assert guard.steps_completed == 1


# --------------------------------------------------------------------------------------
# A step already in flight completes and stays recorded
# --------------------------------------------------------------------------------------


def test_a_step_in_flight_when_the_marker_lands_completes_and_is_recorded() -> None:
    """A marker arriving during a step lets the step finish and be recorded against the budget."""
    guard, store = _guard()

    # The step starts...
    store.request_cancellation(_SESSION, requester=SessionOwner.of(_identity()))
    # ...and still completes, because nothing checks the marker inside a step.
    guard.note_step("committed write: updated opportunity opp_1")
    guard.budget.record_tool_call("committed write: updated opportunity opp_1")

    event = guard.terminal_event()

    assert event is not None
    assert guard.budget.completed_actions == ["committed write: updated opportunity opp_1"]
    assert event.text is not None
    assert "opp_1" in event.text
    assert "audit log" in event.text


def test_cancelling_never_erases_work_already_recorded_against_the_budget() -> None:
    guard, store = _guard()
    guard.note_step("committed write: updated opportunity opp_1")
    guard.budget.record_tool_call("committed write: updated opportunity opp_1")
    guard.budget.record_tokens(1_234)

    store.request_cancellation(_SESSION, requester=SessionOwner.of(_identity()))
    guard.terminal_event()

    assert guard.budget.tool_calls_made == 1
    assert guard.budget.tokens_consumed == 1_234


# --------------------------------------------------------------------------------------
# A cancel after a terminal event is a no-op, not a second terminal event
# --------------------------------------------------------------------------------------


def test_a_cancel_arriving_after_the_guard_produced_a_terminal_event_is_a_no_op() -> None:
    guard, store = _guard()
    guard.note_step("called get_account")
    store.request_cancellation(_SESSION, requester=SessionOwner.of(_identity()))

    first = guard.terminal_event()
    assert first is not None

    # The window is closed by the first terminal event, so a second request writes nothing.
    assert (
        store.request_cancellation(_SESSION, requester=SessionOwner.of(_identity()))
        == "already_terminal"
    )


def test_a_cancel_after_a_provider_terminal_event_is_a_no_op() -> None:
    """`note_terminal()` after a provider terminal event closes the cancel window."""
    guard, store = _guard()
    guard.note_step("called get_account")

    guard.note_terminal()

    assert (
        store.request_cancellation(_SESSION, requester=SessionOwner.of(_identity()))
        == "already_terminal"
    )
    assert guard.cancel_requested() is False
    assert guard.terminal_event() is None


def test_a_foreign_caller_is_still_denied_and_cannot_terminate_the_turn() -> None:
    """Another rep's cancel raises and stops nothing."""
    guard, store = _guard()
    guard.note_step("called get_account")

    with pytest.raises(ScopeDenied):
        store.request_cancellation(_SESSION, requester=SessionOwner.of(_identity(rep_id="rep-2")))

    assert guard.cancel_requested() is False
    assert guard.terminal_event() is None


# --------------------------------------------------------------------------------------
# Cancellation is checked before the budget, and reports the user's own request
# --------------------------------------------------------------------------------------


def test_cancellation_is_reported_in_preference_to_a_simultaneous_budget_expiry() -> None:
    clock = _FakeClock()
    guard, store = _guard(clock=clock)
    guard.note_step("called get_account")
    guard.budget.record_tool_call("called get_account")

    store.request_cancellation(_SESSION, requester=SessionOwner.of(_identity()))
    clock.advance(EVAL_WALL_CLOCK_SECONDS)

    event = guard.terminal_event()

    assert event is not None
    assert event.kind == "completed"
    assert event.error_kind is None


def test_the_budget_still_stops_a_turn_nobody_cancelled() -> None:
    clock = _FakeClock()
    guard, _ = _guard(clock=clock, max_tool_calls=1)
    guard.note_step("called get_account")
    guard.budget.record_tool_call("called get_account")

    event = guard.terminal_event()

    assert event is not None
    assert event.kind == "error"
    assert event.error_kind == "budget_exhausted"


def test_the_guard_closes_the_cancel_window_when_the_budget_ends_the_turn() -> None:
    clock = _FakeClock()
    guard, store = _guard(clock=clock, max_tool_calls=1)
    guard.budget.record_tool_call("called get_account")

    assert guard.terminal_event() is not None
    assert (
        store.request_cancellation(_SESSION, requester=SessionOwner.of(_identity()))
        == "already_terminal"
    )


# --------------------------------------------------------------------------------------
# Cancellation is not a fifth delegation outcome, and not a new event kind
# --------------------------------------------------------------------------------------


def test_the_four_delegation_outcomes_remain_the_only_four() -> None:
    kinds: tuple[SubagentOutcomeKind, ...] = ("data", "no_match", "denied", "tool_failure")
    for kind in kinds:
        if kind == "data":
            assert SubagentOutcome(kind=kind, data={}).kind == kind
        elif kind == "denied":
            assert (
                SubagentOutcome(kind=kind, missing_scope="account:a", denial_message="no").kind
                == kind
            )
        elif kind == "tool_failure":
            assert SubagentOutcome(kind=kind, error_message="boom").kind == kind
        else:
            assert SubagentOutcome(kind=kind).kind == kind

    with pytest.raises(ValueError, match="unrecognised subagent outcome kind"):
        SubagentOutcome(kind="cancelled")  # type: ignore[arg-type]


def test_cancellation_introduces_no_new_event_kind() -> None:
    """A cancelled turn ends on an existing event kind and adds no `cancelled` kind."""
    assert "cancelled" not in EVENT_KINDS
    assert cancellation_event(steps_completed=1, last_step="called get_account").kind in EVENT_KINDS


def test_a_cancellation_event_carries_no_invented_token_split() -> None:
    """A cancellation event carries no input/output token counts."""
    event = cancellation_event(steps_completed=2, last_step="called get_account")
    assert event.input_tokens is None
    assert event.output_tokens is None


# --------------------------------------------------------------------------------------
# The cancellation marker is machine-legible
# --------------------------------------------------------------------------------------


def test_a_cancellation_events_text_opens_with_the_stable_marker() -> None:
    """Every cancellation event's text starts with `CANCELLATION_TEXT_PREFIX`."""
    for steps, last in ((0, None), (3, "called get_account")):
        event = cancellation_event(steps_completed=steps, last_step=last)
        assert event.text is not None
        assert event.text.startswith(CANCELLATION_TEXT_PREFIX)


def test_is_cancellation_event_identifies_a_cancelled_turn() -> None:
    assert is_cancellation_event(cancellation_event(steps_completed=1, last_step="called x"))
    assert is_cancellation_event(cancellation_event(steps_completed=0, last_step=None))


def test_is_cancellation_event_does_not_claim_an_ordinary_successful_turn() -> None:
    """An ordinary successful `completed` event is not classified as a cancellation."""
    assert not is_cancellation_event(AgentEvent(kind="completed", text="Here is what I found."))


def test_is_cancellation_event_does_not_claim_a_non_terminal_event() -> None:
    assert not is_cancellation_event(
        AgentEvent(kind="tool_call", tool_name="get_account", tool_input={"account_id": "acc-1"})
    )
    assert not is_cancellation_event(
        AgentEvent(kind="error", error_kind="model_failure", error_message="boom")
    )


def test_the_guards_own_terminal_event_is_recognised_by_the_shared_predicate() -> None:
    """`is_cancellation_event` accepts the terminal event `TurnGuard` emits on cancel."""
    guard, store = _guard()
    guard.note_step("called get_account")
    store.request_cancellation(_SESSION, requester=SessionOwner.of(_identity()))

    event = guard.terminal_event()

    assert event is not None
    assert is_cancellation_event(event) is True


def test_a_budget_exhaustion_event_is_never_mistaken_for_a_cancellation() -> None:
    """A budget-exhaustion terminal event is not classified as a cancellation."""
    clock = _FakeClock()
    guard, _ = _guard(clock=clock, max_tool_calls=1)
    guard.budget.record_tool_call("called get_account")

    event = guard.terminal_event()

    assert event is not None
    assert is_cancellation_event(event) is False
    assert event.error_kind == "budget_exhausted"


def test_no_ordinary_synthesized_reply_can_collide_with_the_cancellation_marker() -> None:
    """`synthesize_reply` never starts with the cancellation marker for any of the four outcomes."""
    outcomes = (
        SubagentOutcome(kind="data", data={"items": []}),
        SubagentOutcome(kind="no_match"),
        SubagentOutcome(kind="denied", missing_scope="account:a", denial_message="Out of book."),
        SubagentOutcome(kind="tool_failure", error_message="Upstream failed."),
    )
    for outcome in outcomes:
        assert not synthesize_reply(outcome).startswith(CANCELLATION_TEXT_PREFIX)


def test_the_marker_is_a_fixed_token_not_a_sentence() -> None:
    """The marker is the fixed token `[cancelled]` with no spaces."""
    assert CANCELLATION_TEXT_PREFIX == "[cancelled]"
    assert " " not in CANCELLATION_TEXT_PREFIX
