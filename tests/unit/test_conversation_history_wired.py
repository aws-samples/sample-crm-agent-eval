# SPDX-License-Identifier: MIT
"""Server-side conversation history wired into the production turn path in ``runtime_server``.

- a completed turn records its ``(prompt, answer)`` and the next same-owner turn on that session sees
  it prepended as history;
- a foreign identity reusing another principal's ``session_id`` gets no prior history in its
  messages, and cannot write into the owner's history either;
- a continued turn arriving with ``resume_from`` equal to the session's cumulative event count runs
  as a new turn, not a replay;
- a cancelled turn records nothing, so the next turn's history carries no cancellation marker.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agents import runtime_server
from agents.conversation_history import InMemoryConversationHistory
from agents.events import AgentEvent
from agents.runtime_entrypoint import (
    InMemorySessionStore,
    RuntimeInvocation,
    decode_invocation,
    run_turn,
)
from agents.supervisor import CANCELLATION_TEXT_PREFIX, cancellation_event
from crm.envelope import AGENT_IDENTITY_KEY, CLAIM_REP_ID, CLAIM_TENANT
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from providers.capabilities import load_capability_matrix

_SESSION = "session-shared"
_ANSWER = "Acme is healthy."


@pytest.fixture(autouse=True)
def _reset_history_store() -> None:
    """Fresh process-local history store per test, so recorded turns don't leak across tests."""
    runtime_server._history_store = InMemoryConversationHistory()


def _invocation(
    *, rep_id: str = "rep-1", tenant: str = "eval-smoke-unit", prompt: str = "status of Acme?"
) -> RuntimeInvocation:
    claims = {
        CLAIM_REP_ID: rep_id,
        "custom:book": "acc-1",
        "custom:full_visibility": True,
        CLAIM_TENANT: tenant,
    }
    payload: dict[str, Any] = {
        "prompt": prompt,
        "session_id": _SESSION,
        AGENT_IDENTITY_KEY: {"claims": claims, "session_id": _SESSION},
    }
    return decode_invocation(payload, load_capability_matrix())


class _TextOnlyProvider(ModelProvider):
    """Answers with a fixed text and never calls a tool, so a turn records a deterministic answer."""

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(type="text_delta", text=_ANSWER)
        yield ProviderEvent(type="usage", input_tokens=10, output_tokens=5)
        yield ProviderEvent(type="completed", text=_ANSWER, input_tokens=10, output_tokens=5)


class _DriveTurnSpy:
    """Captures the kwargs the adapter passes to drive_turn, then yields a minimal valid stream."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def __call__(self, **kwargs: Any) -> AsyncIterator[AgentEvent]:
        self.kwargs = kwargs

        async def _gen() -> AsyncIterator[AgentEvent]:
            yield AgentEvent(kind="usage", input_tokens=1, output_tokens=1)
            yield AgentEvent(kind="completed", text="second", input_tokens=1, output_tokens=1)

        return _gen()


def _run_real(invocation: RuntimeInvocation) -> list[AgentEvent]:
    """Drive a real turn (records history on completed)."""
    return list(
        runtime_server._sync_agent_events(
            invocation, _TextOnlyProvider(), matrix=load_capability_matrix()
        )
    )


# --- record + read on the same owner ---------------------------------------------------------


def test_a_completed_turn_is_recorded_and_the_next_same_owner_turn_sees_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Turn 1 (real): completes and records (prompt1, answer).
    _run_real(_invocation(prompt="first question"))

    # Turn 2 (spied): the adapter reads the prior turn and passes it as history_messages.
    spy = _DriveTurnSpy()
    monkeypatch.setattr(runtime_server, "drive_turn", spy)
    list(
        runtime_server._sync_agent_events(
            _invocation(prompt="second question"),
            _TextOnlyProvider(),
            matrix=load_capability_matrix(),
        )
    )
    assert spy.kwargs["history_messages"] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": _ANSWER},
    ]


# --- foreign identity on a reused session ----------------------------------------------------


def test_a_foreign_identity_on_a_reused_session_gets_no_prior_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Owner A records a turn on the shared session id.
    _run_real(_invocation(rep_id="rep-owner", prompt="A's private question"))

    # B reuses the same session_id: the store refuses the read and B's turn runs with no history.
    spy = _DriveTurnSpy()
    monkeypatch.setattr(runtime_server, "drive_turn", spy)
    list(
        runtime_server._sync_agent_events(
            _invocation(rep_id="rep-attacker", prompt="B's question"),
            _TextOnlyProvider(),
            matrix=load_capability_matrix(),
        )
    )
    assert spy.kwargs["history_messages"] == []


def test_a_foreign_turn_does_not_write_into_the_owners_history() -> None:
    # A records turn 1.
    _run_real(_invocation(rep_id="rep-owner", prompt="A-1"))
    # B runs a real turn on A's session id; its record_turn is refused by the store.
    _run_real(_invocation(rep_id="rep-attacker", prompt="B-1"))
    # A's history is unchanged: it still holds only A's own turn, never B's.
    history = runtime_server._history_store.history_for(
        _SESSION, identity=_invocation(rep_id="rep-owner").identity
    )
    assert [t.prompt for t in history] == ["A-1"]


# --- continued turn at the cumulative count runs new, not replay -----------------------------


def test_a_continued_turn_at_the_cumulative_count_runs_a_new_turn_not_a_replay() -> None:
    store = InMemorySessionStore()
    invocation = _invocation()

    # Turn 1 records two events; the session's cumulative count is then its last sequence.
    turn_one = iter(
        [
            AgentEvent(kind="usage", input_tokens=1, output_tokens=1),
            AgentEvent(kind="completed", text="first"),
        ]
    )
    list(run_turn(invocation, turn_one, store=store, resume_from=0))
    cumulative = store.last_sequence(invocation.session_id)
    assert cumulative == 2

    # resume_from < last replays; resume_from == last runs the new turn.
    turn_two = iter([AgentEvent(kind="completed", text="second")])
    lines = list(run_turn(invocation, turn_two, store=store, resume_from=cumulative))
    assert any("second" in line for line in lines)  # the NEW turn ran
    assert not any("first" in line for line in lines)  # nothing replayed


# --- a cancelled turn records no history ------------------------------------------------------


def _cancelled_drive_turn(**_kwargs: Any) -> AsyncIterator[AgentEvent]:
    """A drive_turn stand-in whose turn ends in the supervisor's cancellation terminal event."""

    async def _gen() -> AsyncIterator[AgentEvent]:
        yield AgentEvent(kind="usage", input_tokens=1, output_tokens=1)
        yield cancellation_event(steps_completed=1, last_step="read account acc-1")

    return _gen()


def test_a_cancelled_turn_records_no_history_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_server, "drive_turn", _cancelled_drive_turn)
    events = list(
        runtime_server._sync_agent_events(
            _invocation(prompt="cancelled question"),
            _TextOnlyProvider(),
            matrix=load_capability_matrix(),
        )
    )
    assert events[-1].kind == "completed"
    assert (events[-1].text or "").startswith(CANCELLATION_TEXT_PREFIX)
    history = runtime_server._history_store.history_for(_SESSION, identity=_invocation().identity)
    assert history == ()


def test_the_turn_after_a_cancelled_one_sees_only_completed_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_real(_invocation(prompt="first question"))
    monkeypatch.setattr(runtime_server, "drive_turn", _cancelled_drive_turn)
    list(
        runtime_server._sync_agent_events(
            _invocation(prompt="cancelled question"),
            _TextOnlyProvider(),
            matrix=load_capability_matrix(),
        )
    )

    spy = _DriveTurnSpy()
    monkeypatch.setattr(runtime_server, "drive_turn", spy)
    list(
        runtime_server._sync_agent_events(
            _invocation(prompt="third question"),
            _TextOnlyProvider(),
            matrix=load_capability_matrix(),
        )
    )
    assert spy.kwargs["history_messages"] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": _ANSWER},
    ]
    assert not any(
        CANCELLATION_TEXT_PREFIX in str(message["content"])
        for message in spy.kwargs["history_messages"]
    )
