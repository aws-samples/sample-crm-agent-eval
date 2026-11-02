# SPDX-License-Identifier: MIT
"""The chat turn's idle heartbeat in :mod:`agents.runtime_server`.

A slow provider stands in for a long model call; the heartbeat interval is shortened per test so
no test waits 20 s. No model call and no AWS call.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

import agents.runtime_server as runtime_server
from agents.events import AgentEvent
from agents.runtime_entrypoint import (
    InMemorySessionStore,
    RuntimeInvocation,
    decode_invocation,
    run_turn,
)
from crm.envelope import AGENT_IDENTITY_KEY, CLAIM_REP_ID, CLAIM_TENANT
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from providers.capabilities import CapabilityMatrix, load_capability_matrix

MATRIX: CapabilityMatrix = load_capability_matrix()

#: A heartbeat interval short enough that a test sees several within one slow provider call.
_FAST_INTERVAL = 0.02


class _SlowProvider(ModelProvider):
    """Answers with one line of text after ``delay`` seconds of silence, counting its calls."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.stream_calls = 0
        self.finished = False

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.stream_calls += 1
        await asyncio.sleep(self.delay)
        yield ProviderEvent(type="text_delta", text="Acme is healthy.")
        yield ProviderEvent(type="usage", input_tokens=1, output_tokens=1)
        yield ProviderEvent(type="completed", input_tokens=1, output_tokens=1)
        self.finished = True


def _invocation(session_id: str) -> RuntimeInvocation:
    payload: dict[str, Any] = {
        "prompt": "What is the status of Acme?",
        "session_id": session_id,
        AGENT_IDENTITY_KEY: {
            "claims": {CLAIM_REP_ID: "rep-1", "custom:book": "acc-1", CLAIM_TENANT: "demo"},
            "session_id": session_id,
        },
    }
    return decode_invocation(payload, MATRIX)


def _events(provider: ModelProvider, *, session_id: str, interval: float) -> Iterator[AgentEvent]:
    return runtime_server._sync_agent_events(
        _invocation(session_id), provider, matrix=MATRIX, heartbeat_interval=interval
    )


def test_the_runtime_exports_the_designed_twenty_second_interval() -> None:
    """`HEARTBEAT_INTERVAL_SECONDS` is 20 s, the value `test_timeout_chain` reads."""
    assert runtime_server.HEARTBEAT_INTERVAL_SECONDS == 20.0


def test_the_chat_bridge_defaults_to_the_exported_interval() -> None:
    """`_sync_agent_events` ticks at `HEARTBEAT_INTERVAL_SECONDS` unless told otherwise."""
    import inspect

    default = inspect.signature(runtime_server._sync_agent_events).parameters["heartbeat_interval"]
    assert default.default == runtime_server.HEARTBEAT_INTERVAL_SECONDS


def test_an_idle_turn_emits_heartbeats_and_still_completes_uninterrupted() -> None:
    """A silent provider call yields heartbeats, then the same call's answer: nothing is cancelled."""
    provider = _SlowProvider(delay=_FAST_INTERVAL * 6)
    kinds = [e.kind for e in _events(provider, session_id="hb-idle", interval=_FAST_INTERVAL)]

    first_answer = kinds.index("text_delta")
    assert "heartbeat" in kinds[:first_answer]
    assert kinds[-1] == "completed"
    assert "heartbeat" not in kinds[first_answer:]
    assert provider.stream_calls == 1


def test_a_turn_faster_than_the_interval_emits_no_heartbeat() -> None:
    """Heartbeats fill idle gaps only; a turn with no gap carries none."""
    provider = _SlowProvider(delay=0.0)
    kinds = [e.kind for e in _events(provider, session_id="hb-fast", interval=5.0)]
    assert "heartbeat" not in kinds
    assert kinds[-1] == "completed"


def test_heartbeats_are_streamed_but_not_stored_for_resume() -> None:
    """Through `run_turn`, heartbeat frames reach the client and the session store skips them."""
    store = InMemorySessionStore()
    invocation = _invocation("hb-store")
    provider = _SlowProvider(delay=_FAST_INTERVAL * 6)
    events = runtime_server._sync_agent_events(
        invocation, provider, matrix=MATRIX, heartbeat_interval=_FAST_INTERVAL
    )
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in run_turn(invocation, events, store=store)
    ]

    streamed = [frame["kind"] for frame in frames]
    stored = [event.kind for event in store.events_after("hb-store", sequence=0)]
    assert "heartbeat" in streamed
    assert "heartbeat" not in stored
    assert stored == [kind for kind in streamed if kind != "heartbeat"]
    assert store.last_sequence("hb-store") == len(stored)


def test_a_heartbeat_frame_carries_no_fields() -> None:
    """The emitted heartbeat is the bare SSE 2.1.0 `heartbeat` kind."""
    provider = _SlowProvider(delay=_FAST_INTERVAL * 4)
    heartbeat = next(
        e
        for e in _events(provider, session_id="hb-bare", interval=_FAST_INTERVAL)
        if e.kind == "heartbeat"
    )
    assert heartbeat == AgentEvent(kind="heartbeat")


def test_closing_the_stream_mid_wait_cancels_the_pending_step_cleanly(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """Closing the stream during an idle gap raises no never-awaited or task-destroyed warning."""
    provider = _SlowProvider(delay=5.0)
    events = _events(provider, session_id="hb-close", interval=_FAST_INTERVAL)
    assert next(events).kind == "heartbeat"
    events.close()  # type: ignore[attr-defined]

    assert not provider.finished
    assert not [
        w for w in recwarn if "never awaited" in str(w.message) or "destroyed" in str(w.message)
    ]
