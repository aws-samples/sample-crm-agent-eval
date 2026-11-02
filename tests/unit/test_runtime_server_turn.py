# SPDX-License-Identifier: MIT
"""The runtime turn adapter: `_agent_events_for_turn` is a thin delegate over `turn_driver.drive_turn`.

Covers what the adapter forwards to `drive_turn` (prompt, session, page context, provider, matrix,
the invocation's own `TurnTuning`, an empty history for a new session, an identity-bound dispatch) and
the event framing of a real turn through it, and that only a non-cancelled ``completed`` turn is
recorded to conversation history.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, get_args

import pytest

from agents import runtime_server
from agents.budget import CHAT_BUDGET
from agents.conversation_history import InMemoryConversationHistory
from agents.events import AgentEvent, EventKind
from agents.registry import RegisteredTool, tool_registry_for
from agents.runtime_entrypoint import RuntimeInvocation, decode_invocation
from agents.supervisor import SUPERVISOR_ROLE, cancellation_event
from agents.turn_driver import TurnRecords
from crm.envelope import AGENT_IDENTITY_KEY, CLAIM_REP_ID, CLAIM_TENANT, IDENTITY_KEY
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from providers.capabilities import load_capability_matrix

_CLAIMS: dict[str, Any] = {
    CLAIM_REP_ID: "rep-1",
    "custom:book": "acc-1,acc-2",
    CLAIM_TENANT: "eval-smoke-unit",
}


@pytest.fixture(autouse=True)
def _reset_history_store() -> None:
    """Give each test a fresh process-local history store so recorded turns don't leak across tests."""
    runtime_server._history_store = InMemoryConversationHistory()


def _payload(
    *,
    prompt: str = "What is the status of Acme?",
    tuning: dict[str, Any] | None = None,
    session_id: str = "session-abc",
) -> dict[str, Any]:
    """Build an authenticated turn payload on the `agent` channel."""
    body: dict[str, Any] = {"prompt": prompt, "session_id": session_id}
    if tuning is not None:
        body["tuning"] = tuning
    return {**body, AGENT_IDENTITY_KEY: {"claims": dict(_CLAIMS), "session_id": session_id}}


def _invocation(
    *, prompt: str = "What is the status of Acme?", tuning: dict[str, Any] | None = None
) -> RuntimeInvocation:
    """Decode a RuntimeInvocation against the real capability matrix."""
    return decode_invocation(_payload(prompt=prompt, tuning=tuning), load_capability_matrix())


class _TextOnlyProvider(ModelProvider):
    """A provider that answers with text and never calls a tool (no real Lambda dispatch)."""

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(type="text_delta", text="Acme is healthy.")
        yield ProviderEvent(type="usage", input_tokens=10, output_tokens=5)
        yield ProviderEvent(
            type="completed", text="Acme is healthy.", input_tokens=10, output_tokens=5
        )


class _DriveTurnSpy:
    """Captures the kwargs the adapter passes to `drive_turn`, then yields a minimal valid stream."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def __call__(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.kwargs = kwargs

        async def _gen() -> AsyncIterator[Any]:
            from agents.events import AgentEvent

            yield AgentEvent(kind="usage", input_tokens=1, output_tokens=1)
            yield AgentEvent(kind="completed", text="ok", input_tokens=1, output_tokens=1)

        return _gen()


def _drain(invocation: RuntimeInvocation, provider: object, matrix: object) -> list[Any]:
    """Drive `_sync_agent_events` to completion and collect the events."""
    return list(
        runtime_server._sync_agent_events(invocation, provider, matrix=matrix)  # type: ignore[arg-type]
    )


# --- adapter forwarding contract -------------------------------------------------------------


def test_adapter_forwards_prompt_session_provider_and_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _DriveTurnSpy()
    monkeypatch.setattr(runtime_server, "drive_turn", spy)
    invocation = _invocation()
    matrix = load_capability_matrix()
    provider = _TextOnlyProvider()

    _drain(invocation, provider, matrix)

    assert spy.kwargs["prompt"] == invocation.prompt
    assert spy.kwargs["session_id"] == invocation.session_id
    assert spy.kwargs["providers"]("anthropic") is provider
    assert spy.kwargs["matrix"] is matrix
    assert spy.kwargs["budget"] is CHAT_BUDGET
    assert spy.kwargs["page_context"] == invocation.page_context


def test_adapter_maps_the_invocations_own_tuning_onto_every_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """drive_turn receives the invocation's decoded per-tier TurnTuning, unchanged."""
    spy = _DriveTurnSpy()
    monkeypatch.setattr(runtime_server, "drive_turn", spy)
    invocation = _invocation(tuning=None)
    assert invocation.tuning.tiers == {}  # precondition: the user requested no tier override

    _drain(invocation, _TextOnlyProvider(), load_capability_matrix())

    assert spy.kwargs["tuning"] is invocation.tuning


def test_adapter_passes_no_history_and_a_throwaway_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _DriveTurnSpy()
    monkeypatch.setattr(runtime_server, "drive_turn", spy)

    _drain(_invocation(), _TextOnlyProvider(), load_capability_matrix())

    # A fresh session has no prior turns, so the runtime passes an empty history.
    assert spy.kwargs["history_messages"] == []
    # drive_turn requires a records accumulator; the runtime passes a fresh throwaway one.
    assert isinstance(spy.kwargs["records"], TurnRecords)
    assert spy.kwargs["records"].invocations == []


def test_adapter_injects_an_identity_bound_dispatch_on_the_agent_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatch the adapter passes carries the caller's claims on the `agent` channel only."""
    spy = _DriveTurnSpy()
    monkeypatch.setattr(runtime_server, "drive_turn", spy)
    invocation = _invocation()
    _drain(invocation, _TextOnlyProvider(), load_capability_matrix())

    dispatch = spy.kwargs["dispatch"]

    sent: dict[str, Any] = {}

    class _FakeLambdaClient:
        def invoke(self, **kwargs: Any) -> dict[str, Any]:
            import io

            sent["FunctionName"] = kwargs["FunctionName"]
            sent["event"] = __import__("json").loads(kwargs["Payload"])
            return {"Payload": io.BytesIO(b'{"statusCode": 200, "body": {}}')}

    import crm.dispatch as crm_dispatch

    monkeypatch.setattr(crm_dispatch, "lambda_client", lambda: _FakeLambdaClient())
    monkeypatch.setenv("ACTION_LAMBDA_PREFIX", "crm-agent-eval")

    tool: RegisteredTool = next(iter(tool_registry_for(SUPERVISOR_ROLE)))
    dispatch(tool, {"account_id": "acc-1"})

    assert AGENT_IDENTITY_KEY in sent["event"]
    assert IDENTITY_KEY not in sent["event"]
    assert sent["event"][AGENT_IDENTITY_KEY]["claims"][CLAIM_REP_ID] == "rep-1"
    assert sent["event"][AGENT_IDENTITY_KEY]["claims"][CLAIM_TENANT] == "eval-smoke-unit"


# --- real end-to-end through the adapter -----------------------------------------------------


def test_a_text_only_turn_streams_closed_kinds_with_one_usage_and_completed_terminal() -> None:
    """A real turn streams closed-set kinds and ends in one summed usage then one completed."""
    invocation = _invocation()
    events = _drain(invocation, _TextOnlyProvider(), load_capability_matrix())

    closed: set[EventKind] = set(get_args(EventKind))
    assert {e.kind for e in events} <= closed
    assert sum(1 for e in events if e.kind == "completed") == 1
    assert sum(1 for e in events if e.kind == "usage") == 1
    assert events[-1].kind == "completed"
    assert events[-1].text == "Acme is healthy."
    # per-round usage is suppressed: the single usage event carries the summed turn totals
    usage = next(e for e in events if e.kind == "usage")
    assert usage.input_tokens == 10
    assert usage.output_tokens == 5


#: Prompts searched, in order, for one whose stub turn takes the tool branch.
_STUB_PROMPT_CANDIDATES = (
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliet",
    "kilo",
    "lima",
    "mike",
    "november",
    "oscar",
    "papa",
)


def test_a_stub_tool_branch_turn_flows_through_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A StubProvider tool turn round-trips a tool call and ends in one terminal.

    The prompt is the first of a fixed candidate set whose turn takes StubProvider's
    digest-selected tool branch; dispatch is replaced with a fake.
    """
    from providers.stub import StubProvider

    monkeypatch.setattr(
        runtime_server,
        "production_dispatch",
        lambda _identity: lambda _tool, _input: {"statusCode": 200, "body": {"items": []}},
    )
    matrix = load_capability_matrix()
    turns = (
        _drain(_invocation(prompt=prompt), StubProvider(), matrix)
        for prompt in _STUB_PROMPT_CANDIDATES
    )
    events = next((t for t in turns if any(e.kind == "tool_call" for e in t)), None)
    assert events is not None, "no candidate prompt takes the stub's tool branch"

    kinds = [e.kind for e in events]
    closed: set[EventKind] = set(get_args(EventKind))
    assert set(kinds) <= closed
    assert "tool_call" in kinds  # the tool branch was actually taken
    assert "tool_result" in kinds
    assert sum(1 for k in kinds if k == "completed") == 1
    assert sum(1 for k in kinds if k == "usage") == 1
    assert kinds[-1] == "completed"


# --- history recording -----------------------------------------------------------------------


def _recorded_answers(invocation: RuntimeInvocation) -> list[str]:
    """The answers the process-local history store holds for the invocation's session."""
    return [
        turn.answer
        for turn in runtime_server._history_store.history_for(
            invocation.session_id, identity=invocation.identity
        )
    ]


def test_a_completed_turn_records_its_answer() -> None:
    invocation = _invocation()
    _drain(invocation, _TextOnlyProvider(), load_capability_matrix())
    assert _recorded_answers(invocation) == ["Acme is healthy."]


def test_a_cancelled_turn_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _cancelled(**_kwargs: Any) -> AsyncIterator[AgentEvent]:
        async def _gen() -> AsyncIterator[AgentEvent]:
            yield cancellation_event(steps_completed=0, last_step=None)

        return _gen()

    monkeypatch.setattr(runtime_server, "drive_turn", _cancelled)
    invocation = _invocation()
    events = _drain(invocation, _TextOnlyProvider(), load_capability_matrix())
    assert events[-1].kind == "completed"
    assert _recorded_answers(invocation) == []
