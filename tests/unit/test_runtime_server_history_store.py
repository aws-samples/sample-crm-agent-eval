# SPDX-License-Identifier: MIT
"""Conversation history through the real ``/invocations`` ASGI app, and the store selection.

- ``CONVERSATION_HISTORY_STORE`` selects the in-memory or DynamoDB store, and an unknown value fails
  at import;
- a same-session follow-up with no ``resume_from`` (the Gateway chat target's shape) or with
  ``resume_from=0`` runs a fresh turn that carries the earlier turn as history, while a reconnect
  with ``0 < resume_from < last`` replays only the tail;
- history is recorded even when the client disconnects right after the ``completed`` frame;
- a history read or write failure is logged by kind only and never stops the turn's answer;
- the DynamoDB store carries a fact across two turns of the real app.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from scripts import proc_run
from starlette.testclient import TestClient

from agents import runtime_server
from agents.cancellation import InMemoryCancellationStore
from agents.conversation_history import (
    DynamoDbConversationHistory,
    HistoryTurn,
    HistoryWriteConflict,
    InMemoryConversationHistory,
)
from agents.events import AgentEvent
from agents.runtime_entrypoint import InMemorySessionStore, decode_invocation
from crm.authz import Identity
from crm.envelope import (
    AGENT_IDENTITY_KEY,
    CLAIM_BOOK,
    CLAIM_REP_ID,
    CLAIM_TENANT,
    identity_from_claims,
)
from crm.errors import CrmError, ServiceBusy
from crm.store.provisioning import create_all_tables
from crm.store.store import (
    BOUNDED_CONNECT_TIMEOUT_SECONDS,
    BOUNDED_READ_TIMEOUT_SECONDS,
    BOUNDED_TOTAL_MAX_ATTEMPTS,
    Store,
)
from gateway.chat_target import runtime_payload
from providers.base import ProviderEvent, ResolvedInvocation
from providers.stub import StubProvider

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

REPO_ROOT = Path(__file__).resolve().parents[2]

_CLAIMS: dict[str, Any] = {
    CLAIM_REP_ID: "rep-ashford",
    CLAIM_BOOK: "acct-0000",
    CLAIM_TENANT: "demo",
}
_FACT_PROMPT = "Remember the code word ALPHA-7."
_ASK_PROMPT = "What is the code word?"


class _RecordingStub(StubProvider):
    """A stub provider that is offered no tools and records every message list it is sent."""

    def __init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.requests.append([dict(message) for message in messages])
        async for event in super().stream(invocation, messages, []):
            yield event


@pytest.fixture
def stub() -> Iterator[_RecordingStub]:
    """Fresh process-local state with a recording stub provider."""
    recording = _RecordingStub()
    runtime_server._matrix = None
    runtime_server._stub_provider = recording
    runtime_server._session_store = InMemorySessionStore()
    runtime_server._cancellation_store = InMemoryCancellationStore()
    runtime_server._history_store = InMemoryConversationHistory()
    yield recording
    runtime_server._stub_provider = None
    runtime_server._history_store = InMemoryConversationHistory()


def _relay_body(session: str, prompt: str, **extra: object) -> dict[str, Any]:
    return {
        **extra,
        "prompt": prompt,
        "session_id": session,
        AGENT_IDENTITY_KEY: {
            "claims": dict(_CLAIMS),
            "principal_type": "user",
            "session_id": session,
        },
    }


def _gateway_body(session: str, prompt: str) -> dict[str, Any]:
    identity = identity_from_claims(dict(_CLAIMS), source="gateway")
    body = runtime_payload(message=prompt, conversation_id=session, tuning={}, identity=identity)
    body.pop("tuning")
    return body


def _frames(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def _completed_text(frames: list[dict[str, Any]]) -> str | None:
    done = [frame for frame in frames if frame["kind"] == "completed"]
    return done[-1]["text"] if done else None


def _carries_prior_turn(request: list[dict[str, object]], prompt: str, answer: str) -> bool:
    users = [message.get("content") for message in request if message.get("role") == "user"]
    assistants = [
        message.get("content") for message in request if message.get("role") == "assistant"
    ]
    return prompt in users[:-1] and answer in assistants


def _owner() -> Identity:
    return identity_from_claims(dict(_CLAIMS), source="agent")


# --- store selection -------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "memory", " MEMORY "])
def test_unset_or_memory_selects_the_in_memory_store(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("CONVERSATION_HISTORY_STORE", raising=False)
    else:
        monkeypatch.setenv("CONVERSATION_HISTORY_STORE", value)
    assert isinstance(runtime_server.build_history_store(), InMemoryConversationHistory)


def test_dynamodb_selects_the_durable_store_on_the_prefixed_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONVERSATION_HISTORY_STORE", "dynamodb")
    monkeypatch.setenv("CRM_TABLE_PREFIX", "crm-unit-")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    store = runtime_server.build_history_store()
    assert isinstance(store, DynamoDbConversationHistory)
    assert store._store.physical_name("reps") == "crm-unit-reps"
    config = store._store._client.meta.config
    assert config.connect_timeout == BOUNDED_CONNECT_TIMEOUT_SECONDS == 2.0
    assert config.read_timeout == BOUNDED_READ_TIMEOUT_SECONDS == 3.0
    assert config.retries == {"mode": "standard", "total_max_attempts": BOUNDED_TOTAL_MAX_ATTEMPTS}
    assert BOUNDED_TOTAL_MAX_ATTEMPTS == 2


def test_an_unknown_store_value_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVERSATION_HISTORY_STORE", "redis")
    with pytest.raises(RuntimeError, match="CONVERSATION_HISTORY_STORE"):
        runtime_server.build_history_store()


def test_an_unknown_store_value_fails_the_module_import() -> None:
    completed = proc_run.run(
        [sys.executable, "-c", "import agents.runtime_server"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={"CONVERSATION_HISTORY_STORE": "redis", "PATH": "/usr/bin:/bin", "HOME": "/tmp"},
        check=False,
    )
    assert completed.returncode != 0
    assert "unrecognised CONVERSATION_HISTORY_STORE value: 'redis'" in completed.stderr


# --- follow-ups run fresh; only a real reconnect replays --------------------------------------


def test_a_gateway_style_follow_up_without_resume_from_runs_with_history(
    stub: _RecordingStub,
) -> None:
    session = f"conv-{uuid.uuid4()}"
    with TestClient(runtime_server.app) as client:
        first = _frames(client.post("/invocations", json=_gateway_body(session, _FACT_PROMPT)).text)
        second = _frames(client.post("/invocations", json=_gateway_body(session, _ASK_PROMPT)).text)
    answer = _completed_text(first)
    assert answer is not None
    assert len(stub.requests) == 2
    assert _carries_prior_turn(stub.requests[-1], _FACT_PROMPT, answer)
    assert second != first


def test_a_follow_up_with_resume_from_zero_runs_with_history(stub: _RecordingStub) -> None:
    session = f"sess-{uuid.uuid4()}"
    with TestClient(runtime_server.app) as client:
        first = _frames(
            client.post("/invocations", json=_relay_body(session, _FACT_PROMPT, resume_from=0)).text
        )
        client.post("/invocations", json=_relay_body(session, _ASK_PROMPT, resume_from=0))
    answer = _completed_text(first)
    assert answer is not None
    assert len(stub.requests) == 2
    assert _carries_prior_turn(stub.requests[-1], _FACT_PROMPT, answer)


def test_a_reconnect_inside_the_last_turn_replays_only_the_tail(stub: _RecordingStub) -> None:
    session = f"sess-{uuid.uuid4()}"
    with TestClient(runtime_server.app) as client:
        first = _frames(client.post("/invocations", json=_relay_body(session, _FACT_PROMPT)).text)
        assert len(first) >= 2
        replay = _frames(
            client.post(
                "/invocations", json=_relay_body(session, _FACT_PROMPT, resume_from=1, resume=True)
            ).text
        )
    assert len(stub.requests) == 1
    assert replay == first[1:]


def test_a_zero_frame_reconnect_replays_the_whole_turn(stub: _RecordingStub) -> None:
    session = f"sess-{uuid.uuid4()}"
    with TestClient(runtime_server.app) as client:
        first = _frames(client.post("/invocations", json=_relay_body(session, _FACT_PROMPT)).text)
        replay = _frames(
            client.post(
                "/invocations", json=_relay_body(session, _FACT_PROMPT, resume_from=0, resume=True)
            ).text
        )
    assert len(stub.requests) == 1
    assert replay == first


@pytest.mark.parametrize("resume", ["true", 1, False, None])
def test_a_resume_value_other_than_json_true_runs_a_fresh_turn(
    stub: _RecordingStub, resume: object
) -> None:
    session = f"sess-{uuid.uuid4()}"
    with TestClient(runtime_server.app) as client:
        client.post("/invocations", json=_relay_body(session, _FACT_PROMPT))
        client.post(
            "/invocations", json=_relay_body(session, _ASK_PROMPT, resume_from=0, resume=resume)
        )
    assert len(stub.requests) == 2


# --- history is recorded before `completed` reaches the client --------------------------------


def _drive_with_disconnect_after_completed(body: dict[str, Any]) -> list[bytes]:
    """Run one ``/invocations`` request whose client disconnects right after ``completed``."""
    raw = json.dumps(body).encode()

    async def drive() -> list[bytes]:
        sent: list[bytes] = []
        completed_sent = asyncio.Event()
        requested = False

        async def receive() -> dict[str, Any]:
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": raw, "more_body": False}
            await completed_sent.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                sent.append(chunk)
                if b'"kind": "completed"' in chunk:
                    completed_sent.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/invocations",
            "raw_path": b"/invocations",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8080),
        }
        await runtime_server.app(scope, receive, send)  # type: ignore[arg-type]
        return sent

    return asyncio.run(drive())


def test_a_disconnect_right_after_completed_still_records_history(stub: _RecordingStub) -> None:
    session = f"sess-{uuid.uuid4()}"
    sent = _drive_with_disconnect_after_completed(_relay_body(session, _FACT_PROMPT))
    assert any(b'"kind": "completed"' in chunk for chunk in sent)
    turns = runtime_server._history_store.history_for(session, identity=_owner())
    assert [turn.prompt for turn in turns] == [_FACT_PROMPT]


# --- the sync bridge closes its async generator from inside a running loop --------------------


def test_closing_the_turn_bridge_inside_a_running_loop_closes_the_turn(
    stub: _RecordingStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    async def agent_events(*_args: Any, **_kwargs: Any) -> AsyncIterator[AgentEvent]:
        try:
            yield AgentEvent(kind="usage", input_tokens=1, output_tokens=1)
            yield AgentEvent(kind="completed", text="done")
        finally:
            closed.append(True)

    monkeypatch.setattr(runtime_server, "_agent_events_for_turn", agent_events)
    invocation = decode_invocation(
        _relay_body("sess-close", "p"), runtime_server._capability_matrix()
    )
    bridge = runtime_server._sync_agent_events(
        invocation, stub, matrix=runtime_server._capability_matrix()
    )
    assert next(bridge).kind == "usage"

    async def close_inside_loop() -> None:
        bridge.close()  # type: ignore[attr-defined]

    asyncio.run(close_inside_loop())
    assert closed == [True]


# --- failures are logged by kind and never stop the answer ------------------------------------


class _FailingHistory(InMemoryConversationHistory):
    """An in-memory store whose reads or writes raise a chosen error."""

    def __init__(self, *, read: CrmError | None = None, write: CrmError | None = None) -> None:
        super().__init__()
        self._read = read
        self._write = write

    def history_for(self, session_id: str, *, identity: Identity) -> tuple[HistoryTurn, ...]:
        if self._read is not None:
            raise self._read
        return super().history_for(session_id, identity=identity)

    def record_turn(self, session_id: str, *, identity: Identity, prompt: str, answer: str) -> None:
        if self._write is not None:
            raise self._write
        super().record_turn(session_id, identity=identity, prompt=prompt, answer=answer)


def _history_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for record in caplog.records:
        try:
            parsed = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(parsed, dict) and str(parsed.get("event", "")).startswith("history_"):
            events.append(parsed)
    return events


@pytest.mark.parametrize(
    ("write_error", "expected"),
    [
        (
            ServiceBusy("busy", detail="d"),
            {"event": "history_write_failed", "reason": "ServiceBusy"},
        ),
        (
            CrmError("broken", detail="d"),
            {"event": "history_write_failed", "reason": "CrmError"},
        ),
        (HistoryWriteConflict("raced", detail="d"), {"event": "history_write_conflict"}),
    ],
)
def test_a_failed_history_write_is_logged_by_kind_and_the_answer_is_delivered(
    stub: _RecordingStub,
    caplog: pytest.LogCaptureFixture,
    write_error: CrmError,
    expected: dict[str, str],
) -> None:
    runtime_server._history_store = _FailingHistory(write=write_error)
    with caplog.at_level(logging.INFO), TestClient(runtime_server.app) as client:
        frames = _frames(client.post("/invocations", json=_relay_body("sess-w", _FACT_PROMPT)).text)
    assert _completed_text(frames) is not None
    assert not any(frame["kind"] == "error" for frame in frames)
    assert _history_events(caplog) == [expected]
    assert "ALPHA-7" not in caplog.text


def test_a_failed_history_read_is_logged_and_the_turn_runs_without_history(
    stub: _RecordingStub, caplog: pytest.LogCaptureFixture
) -> None:
    runtime_server._history_store = _FailingHistory(read=ServiceBusy("busy", detail="d"))
    with caplog.at_level(logging.INFO), TestClient(runtime_server.app) as client:
        frames = _frames(client.post("/invocations", json=_relay_body("sess-r", _ASK_PROMPT)).text)
    assert _completed_text(frames) is not None
    assert {"event": "history_read_failed", "reason": "ServiceBusy"} in _history_events(caplog)
    assert [m.get("role") for m in stub.requests[-1]].count("assistant") == 0


# --- slow history calls neither stall heartbeats nor hold `completed` past the deadline ---------


class _SlowHistory(InMemoryConversationHistory):
    """An in-memory store whose reads and writes each sleep ``delay`` seconds first."""

    def __init__(self, *, read_delay: float = 0.0, write_delay: float = 0.0) -> None:
        super().__init__()
        self._read_delay = read_delay
        self._write_delay = write_delay

    def history_for(self, session_id: str, *, identity: Identity) -> tuple[HistoryTurn, ...]:
        time.sleep(self._read_delay)
        return super().history_for(session_id, identity=identity)

    def record_turn(self, session_id: str, *, identity: Identity, prompt: str, answer: str) -> None:
        time.sleep(self._write_delay)
        super().record_turn(session_id, identity=identity, prompt=prompt, answer=answer)


def _bridge_kinds(stub: _RecordingStub, prompt: str) -> tuple[list[str], float]:
    invocation = decode_invocation(
        _relay_body("sess-slow", prompt), runtime_server._capability_matrix()
    )
    started = time.monotonic()
    kinds: list[str] = [
        event.kind
        for event in runtime_server._sync_agent_events(
            invocation, stub, matrix=runtime_server._capability_matrix(), heartbeat_interval=0.05
        )
    ]
    return kinds, time.monotonic() - started


def test_heartbeats_keep_flowing_while_the_history_write_is_slow(stub: _RecordingStub) -> None:
    runtime_server._history_store = _SlowHistory(write_delay=0.4)
    kinds, _ = _bridge_kinds(stub, _FACT_PROMPT)
    assert kinds[-1] == "completed"
    assert "heartbeat" in kinds[-4:-1]
    turns = runtime_server._history_store.history_for("sess-slow", identity=_owner())
    assert [turn.prompt for turn in turns] == [_FACT_PROMPT]


def test_a_history_write_past_its_deadline_still_delivers_completed(
    stub: _RecordingStub, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(runtime_server, "HISTORY_WRITE_DEADLINE_SECONDS", 0.1)
    runtime_server._history_store = _SlowHistory(write_delay=2.0)
    with caplog.at_level(logging.INFO):
        kinds, elapsed = _bridge_kinds(stub, _FACT_PROMPT)
    assert kinds[-1] == "completed"
    assert elapsed < 1.5
    assert {"event": "history_write_failed", "reason": "TimeoutError"} in _history_events(caplog)


def test_a_history_read_past_its_deadline_runs_the_turn_without_history(
    stub: _RecordingStub, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(runtime_server, "HISTORY_READ_DEADLINE_SECONDS", 0.1)
    runtime_server._history_store = _SlowHistory(read_delay=2.0)
    with caplog.at_level(logging.INFO):
        kinds, elapsed = _bridge_kinds(stub, _ASK_PROMPT)
    assert kinds[-1] == "completed"
    assert elapsed < 1.5
    assert {"event": "history_read_failed", "reason": "TimeoutError"} in _history_events(caplog)
    assert [m.get("role") for m in stub.requests[-1]].count("assistant") == 0


# --- the durable store end to end through the app ----------------------------------------------


def test_the_dynamodb_store_carries_a_fact_across_two_turns(stub: _RecordingStub) -> None:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        runtime_server._history_store = DynamoDbConversationHistory(Store(client=client))
        session = f"conv-{uuid.uuid4()}"
        with TestClient(runtime_server.app) as app_client:
            first = _frames(
                app_client.post("/invocations", json=_gateway_body(session, _FACT_PROMPT)).text
            )
            app_client.post("/invocations", json=_gateway_body(session, _ASK_PROMPT))
    answer = _completed_text(first)
    assert answer is not None
    assert _carries_prior_turn(stub.requests[-1], _FACT_PROMPT, answer)
