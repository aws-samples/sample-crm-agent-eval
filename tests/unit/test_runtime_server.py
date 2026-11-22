# SPDX-License-Identifier: MIT
"""Tests for the AgentCore Runtime HTTP adapter, `agents.runtime_server.app`.

Covers `GET /ping`, `POST /invocations` SSE streaming and its typed error mapping, identity
channels, the isolation-probe and cancel modes, and the retired `delegation` mode taking the turn
path. Uses :class:`starlette.testclient.TestClient` with fake providers and no AWS call.
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Final

import pytest
from starlette.testclient import TestClient

import agents.runtime_server as runtime_server
import crm.dispatch as crm_dispatch
from agents.cancellation import InMemoryCancellationStore, SessionOwner
from agents.conversation_history import InMemoryConversationHistory
from agents.registry import RegisteredTool, tool_registry_for
from agents.runtime_entrypoint import (
    InMemorySessionStore,
    resolve_identity,
)
from agents.supervisor import SUPERVISOR_ROLE
from agents.tool_dispatch import dispatch_tool_call
from crm.authz import DEMO_TENANT
from crm.envelope import (
    AGENT_IDENTITY_KEY,
    CLAIM_REP_ID,
    CLAIM_TENANT,
    IDENTITY_KEY,
    REQUEST_CONTEXT_KEY,
    Identity,
    decode_identity,
    strip_identity,
)
from crm.errors import AuthenticationFailed
from crm.toolspec.definitions import BY_NAME
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from providers.tier_tuning import WIRE_SCHEMA_VERSION
from tests.conftest import TEST_API_CLIENT_ID


def _identity_block(claims: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    """Build the `_agent_identity` block the relay places in the SigV4-authenticated payload.

    This block is the only channel by which identity reaches the container.
    """
    block: dict[str, Any] = {"claims": claims, "principal_type": "user"}
    if session_id is not None:
        block["session_id"] = session_id
    return {AGENT_IDENTITY_KEY: block}


#: Claims a legitimately authenticated turn caller presents.
_TURN_CLAIMS: dict[str, Any] = {
    CLAIM_REP_ID: "rep-1",
    "custom:book": "acc-1,acc-2",
    CLAIM_TENANT: "eval-smoke-unit",
}


def _authed(payload: dict[str, Any], claims: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return `payload` carrying the identity block a legitimately authenticated caller sends."""
    return {**payload, **_identity_block(claims if claims is not None else dict(_TURN_CLAIMS))}


#: The turn-path payload without identity; `_authed` merges the channel in.
_TURN_PAYLOAD: dict[str, Any] = {
    "prompt": "What is the status of Acme?",
    "session_id": "session-abc",
}

#: The full turn-path request a legitimately authenticated caller sends.
_TURN_REQUEST: dict[str, Any] = _authed(_TURN_PAYLOAD)


class _TextOnlyProvider(ModelProvider):
    """A fake provider that always answers with text and never calls a tool."""

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(type="text_delta", text="hello from the fake provider")
        yield ProviderEvent(type="usage", input_tokens=10, output_tokens=5)
        yield ProviderEvent(type="completed", input_tokens=10, output_tokens=5)


@pytest.fixture(autouse=True)
def _reset_process_state() -> AsyncIterator[None]:  # type: ignore[misc]
    """Reset the module's process-local caches between tests so no test leaks state into another."""
    runtime_server._matrix = None
    runtime_server._stub_provider = None
    runtime_server._live_providers_by_family = {}
    runtime_server._store = None
    runtime_server._session_store = InMemorySessionStore()
    runtime_server._cancellation_store = InMemoryCancellationStore()
    runtime_server._history_store = InMemoryConversationHistory()
    yield
    runtime_server._matrix = None
    runtime_server._stub_provider = None
    runtime_server._live_providers_by_family = {}
    runtime_server._store = None


@pytest.fixture
def client() -> TestClient:
    return TestClient(runtime_server.app)


def test_ping_returns_healthy(client: TestClient) -> None:
    """`GET /ping` reports `{"status": "Healthy"}` with HTTP 200, per the AgentCore contract."""
    response = client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "Healthy"}


def test_invocations_rejects_non_json_body(client: TestClient) -> None:
    """A body that is not valid JSON surfaces a 400 validation error, not an unhandled 500."""
    response = client.post(
        "/invocations", content=b"not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


def test_invocations_rejects_a_json_body_that_is_not_an_object(client: TestClient) -> None:
    """A JSON array or scalar body is rejected the same way as malformed JSON."""
    response = client.post("/invocations", json=[1, 2, 3])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


def test_invocations_rejects_a_payload_with_no_recognised_identity_channel(
    client: TestClient,
) -> None:
    """A payload carrying none of the recognised identity channels surfaces 401, not a 500."""
    response = client.post("/invocations", json={"prompt": "hello", "session_id": "s1"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


def test_invocations_decodes_identity_from_the_payloads_agent_channel(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity is decoded from the payload's `AGENT_IDENTITY_KEY` block."""
    monkeypatch.setattr(runtime_server, "_model_provider", lambda *_a: _TextOnlyProvider())

    with client.stream(
        "POST",
        "/invocations",
        json=_authed({"prompt": "hello", "session_id": "s1"}),
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    events = _parse_sse_events(body)
    assert any(event.get("kind") == "completed" for event in events)


@pytest.mark.parametrize(
    ("header_value", "label"),
    [
        ("Bearer not-a-real-jwt", "malformed"),
        (
            "Bearer eyJhbGciOiJIUzI1NiJ9"
            + "."
            + "eyJjdXN0b206cmVwX2lk"
            + "IjoicmVwLTEifQ"
            + ".sig",
            "well_formed",
        ),
    ],
)
def test_a_forwarded_authorization_header_authenticates_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, header_value: str, label: str
) -> None:
    """A forwarded `Authorization` header, garbage or an unsigned JWT, authenticates nothing."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("a header-supplied identity must never reach a model provider")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)

    response = client.post(
        "/invocations",
        json={"prompt": "hello", "session_id": "s1"},
        headers={"Authorization": header_value},
    )
    assert response.status_code == 401, label
    assert response.json()["error"]["code"] == "authentication_failed"


def test_the_server_holds_no_authorization_header_decode_path_at_all(client: TestClient) -> None:
    """The server module contains no `Authorization`-header decode path."""
    source = Path(runtime_server.__file__).read_text(encoding="utf-8")

    assert not hasattr(runtime_server, "_claims_from_authorization_header")
    assert not hasattr(runtime_server, "_inject_authorizer_claims")
    for forbidden in (
        'headers.get("Authorization")',
        "headers.get('Authorization')",
        '"Bearer "',
        "jwt.decode",
        "verify_signature",
        "import jwt",
    ):
        assert forbidden not in source, forbidden


def test_honouring_the_payload_channel_adds_no_fourth_identity_channel() -> None:
    """`decode_identity` reads exactly three caller-supplied identity keys."""
    import ast
    import inspect

    from crm.envelope import IDENTITY_CHANNEL_KEYS

    tree = ast.parse(inspect.getsource(decode_identity))
    read_keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and isinstance(node.left, ast.Name)
        ):
            read_keys.add(node.left.id)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            read_keys.add(node.args[0].id)

    channel_names = {"IDENTITY_KEY", "AGENT_IDENTITY_KEY", "REQUEST_CONTEXT_KEY"}
    assert read_keys & channel_names == channel_names
    assert len(read_keys & channel_names) == 3
    assert set(IDENTITY_CHANNEL_KEYS) == {IDENTITY_KEY, AGENT_IDENTITY_KEY, REQUEST_CONTEXT_KEY}
    assert len(IDENTITY_CHANNEL_KEYS) == 3


def test_no_module_under_src_agents_constructs_an_identity() -> None:
    """No module under `src/agents/` constructs an `Identity` directly."""
    import ast

    package_root = Path(runtime_server.__file__).parent
    offenders: list[str] = []
    scanned: list[str] = []
    for module_path in sorted(package_root.rglob("*.py")):
        scanned.append(module_path.relative_to(package_root).as_posix())
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"Identity", "identity_from_claims"}
            ):
                offenders.append(f"{module_path.name}:{node.lineno} {node.func.id}(...)")
            if isinstance(node, ast.Attribute) and node.attr == "identity_from_claims":
                offenders.append(f"{module_path.name}:{node.lineno} .identity_from_claims")

    # Non-vacuity: the scan must have walked a non-trivial package, including `subagents/`.
    assert len(scanned) >= 12, scanned
    for expected in ("runtime_server.py", "supervisor.py", "subagents/base.py"):
        assert expected in scanned, (expected, scanned)

    assert offenders == [], offenders


def test_invocations_ignores_a_body_supplied_requestcontext_and_honours_only_the_agent_channel(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A body-supplied `requestContext` never authenticates and never overrides the agent channel.

    Alone it is a `401`; alongside a legitimate `_agent_identity` the agent block's rep is resolved.
    """
    monkeypatch.setattr(runtime_server, "_model_provider", lambda *_a: _TextOnlyProvider())
    forged_context = {
        "requestContext": {
            "requestId": "req-1",
            "authorizer": {
                "jwt": {"claims": {CLAIM_REP_ID: "rep-attacker", "custom:full_visibility": "true"}}
            },
        }
    }

    anonymous = client.post("/invocations", json={**_TURN_PAYLOAD, **forged_context})
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "authentication_failed"

    with (
        caplog.at_level(logging.INFO),
        client.stream(
            "POST",
            "/invocations",
            json={**_TURN_REQUEST, **forged_context},
        ) as response,
    ):
        assert response.status_code == 200
        "".join(response.iter_text())

    started = [
        json.loads(record.message)
        for record in caplog.records
        if record.message.startswith("{") and '"invocation_started"' in record.message
    ]
    assert started, "no invocation_started record was logged"
    assert started[-1]["rep_id"] == "rep-1"


def test_invocations_streams_sse_for_a_valid_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid, authenticated payload streams `text/event-stream` SSE lines ending in `completed`.

    Every `data:` line must parse as exactly one JSON event object, which a double-framed response
    fails.
    """
    monkeypatch.setattr(runtime_server, "_model_provider", lambda *_a: _TextOnlyProvider())

    with client.stream("POST", "/invocations", json=_TURN_REQUEST) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        body = "".join(response.iter_text())

    events = _parse_sse_events(body)
    assert events, f"expected at least one SSE event, got none from body: {body!r}"
    assert any(event.get("kind") == "completed" for event in events)
    assert any("hello from the fake provider" in (event.get("text") or "") for event in events)


def _parse_sse_events(body: str) -> list[dict[str, Any]]:
    """Parse an SSE body into one dict per `data:` line, failing if any line is not JSON."""
    import json as _json

    events: list[dict[str, Any]] = []
    for raw_line in body.splitlines():
        if not raw_line.startswith("data:"):
            continue
        payload_text = raw_line[len("data:") :].strip()
        if not payload_text:
            continue
        parsed = _json.loads(payload_text)
        assert isinstance(parsed, dict), f"expected one JSON object per data line, got: {parsed!r}"
        events.append(parsed)
    return events


def test_invocations_uses_stub_provider_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no `AGENT_PROVIDER` set, `_model_provider` resolves to the zero-cost stub."""
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    runtime_server._stub_provider = None
    runtime_server._live_providers_by_family = {}
    from providers.stub import StubProvider

    provider = runtime_server._model_provider()
    assert isinstance(provider, StubProvider)


def test_model_provider_rejects_an_unrecognised_family(monkeypatch: pytest.MonkeyPatch) -> None:
    """An `AGENT_PROVIDER` value outside {stub, anthropic, openai, live} fails fast, never silently."""
    monkeypatch.setenv("AGENT_PROVIDER", "not-a-real-family")
    runtime_server._stub_provider = None
    runtime_server._live_providers_by_family = {}
    with pytest.raises(RuntimeError, match="not-a-real-family"):
        runtime_server._model_provider()


# --------------------------------------------------------------------------------------
# Live mode: the adapter is chosen per invocation from the resolved family
# --------------------------------------------------------------------------------------


class _RecordingProvider(ModelProvider):
    """A family-tagged fake that records each `stream` call and answers with one line of text."""

    def __init__(self, family: str) -> None:
        self.family = family
        self.stream_calls = 0

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.stream_calls += 1
        yield ProviderEvent(type="text_delta", text=f"answered by the {self.family} adapter")
        yield ProviderEvent(type="usage", input_tokens=1, output_tokens=1)
        yield ProviderEvent(type="completed", input_tokens=1, output_tokens=1)


def _install_per_family_fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, _RecordingProvider]:
    """Point `build_provider_for_family` at per-family recording fakes and select a live mode.

    `_model_provider` imports `build_provider_for_family` from `providers.factory` at call time, so
    the factory module's attribute is what gets patched.
    """
    fakes = {"anthropic": _RecordingProvider("anthropic"), "openai": _RecordingProvider("openai")}

    def _fake_build(family: str, *, region_name: str) -> _RecordingProvider:
        return fakes[family]

    monkeypatch.setattr("providers.factory.build_provider_for_family", _fake_build)
    monkeypatch.setenv("AGENT_PROVIDER", "live")
    return fakes


def _every_tier(family: str) -> dict[str, Any]:
    """A current-schema wire tuning block running every tier in ``family``."""
    return {
        "schema": WIRE_SCHEMA_VERSION,
        "source": "ui.settings",
        "tiers": {tier: {"family": family} for tier in ("supervisor", "subagent", "tool")},
    }


def _supervisor_only(**fields: object) -> dict[str, Any]:
    """A current-schema wire tuning block setting only the supervisor tier's ``fields``."""
    return {
        "schema": WIRE_SCHEMA_VERSION,
        "source": "ui.settings",
        "tiers": {"supervisor": dict(fields)},
    }


def test_a_live_openai_turn_selects_the_openai_adapter_not_the_anthropic_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn whose resolved family is OpenAI reaches the OpenAI adapter, not the Anthropic one."""
    fakes = _install_per_family_fakes(monkeypatch)

    with client.stream(
        "POST", "/invocations", json=_authed({**_TURN_PAYLOAD, "tuning": _every_tier("openai")})
    ) as response:
        assert response.status_code == 200
        "".join(response.iter_text())

    assert fakes["openai"].stream_calls == 1
    assert fakes["anthropic"].stream_calls == 0


def test_a_live_anthropic_turn_selects_the_anthropic_adapter_not_the_openai_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reverse: a default-family (Anthropic) turn reaches the Anthropic adapter alone."""
    fakes = _install_per_family_fakes(monkeypatch)

    with client.stream(
        "POST", "/invocations", json=_authed({**_TURN_PAYLOAD, "tuning": _every_tier("anthropic")})
    ) as response:
        assert response.status_code == 200
        "".join(response.iter_text())

    assert fakes["anthropic"].stream_calls == 1
    assert fakes["openai"].stream_calls == 0


class _ScriptedFamilyProvider(ModelProvider):
    """Serves one family from a shared script; refuses another family's model, as live adapters do."""

    def __init__(self, family: str, rounds: list[list[ProviderEvent]], calls: list[str]) -> None:
        self.family = family
        self._rounds = rounds
        self._calls = calls

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        assert invocation.model.family == self.family, (
            f"{self.family} adapter handed a {invocation.model.family} model"
        )
        self._calls.append(self.family)
        return self._emit(self._rounds.pop(0))

    async def _emit(self, events: list[ProviderEvent]) -> AsyncIterator[ProviderEvent]:
        for event in events:
            yield event


def _delegating_rounds() -> list[list[ProviderEvent]]:
    """Supervisor delegates to `research`; the subagent calls `get_account`; then a final answer."""
    return [
        [
            ProviderEvent(
                type="tool_call", tool_name="research", tool_input={"intent": "acme"}, tool_id="t1"
            ),
            ProviderEvent(type="usage", input_tokens=10, output_tokens=2),
            ProviderEvent(type="completed", awaiting_tools=True),
        ],
        [
            ProviderEvent(
                type="tool_call",
                tool_name="get_account",
                tool_input={"account_id": "acc-1"},
                tool_id="s1",
            ),
            ProviderEvent(type="usage", input_tokens=4, output_tokens=1),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
        [
            ProviderEvent(
                type="tool_call", tool_name="get_account", tool_input={"account_id": "acc-1"}
            ),
            ProviderEvent(type="usage", input_tokens=2, output_tokens=1),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
        [
            ProviderEvent(type="text_delta", text="Here is Acme."),
            ProviderEvent(type="usage", input_tokens=5, output_tokens=2),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
    ]


def test_a_mixed_family_chat_turn_reaches_each_tiers_own_family_adapter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Supervisor and tool tiers on OpenAI, subagent on Anthropic, each on its own adapter."""
    rounds = _delegating_rounds()
    calls: list[str] = []
    adapters = {
        family: _ScriptedFamilyProvider(family, rounds, calls) for family in ("anthropic", "openai")
    }
    built: list[str] = []

    def _fake_build(family: str, *, region_name: str) -> ModelProvider:
        built.append(family)
        return adapters[family]

    monkeypatch.setattr("providers.factory.build_provider_for_family", _fake_build)
    monkeypatch.setenv("AGENT_PROVIDER", "live")
    monkeypatch.setattr(
        runtime_server,
        "production_dispatch",
        lambda _identity: (
            lambda _tool, _input: {
                "statusCode": 200,
                "body": {"account_id": "acc-1", "name": "Acme"},
            }
        ),
    )
    tuning = {
        "schema": WIRE_SCHEMA_VERSION,
        "source": "ui.settings",
        "tiers": {
            "supervisor": {"family": "openai"},
            "subagent": {"family": "anthropic"},
            "tool": {"family": "openai"},
        },
    }

    with client.stream(
        "POST", "/invocations", json=_authed({**_TURN_PAYLOAD, "tuning": tuning})
    ) as response:
        assert response.status_code == 200
        events = _parse_sse_events("".join(response.iter_text()))

    assert calls == ["openai", "anthropic", "openai", "openai"]
    assert sorted(set(built)) == ["anthropic", "openai"]
    completed = [event for event in events if event["kind"] == "completed"]
    assert len(completed) == 1, events
    assert completed[0]["text"] == "Here is Acme."
    assert [(t["tier"], t["family"]) for t in completed[0]["resolved_tiers"]] == [
        ("supervisor", "openai"),
        ("subagent", "anthropic"),
        ("tool", "openai"),
    ]


def test_an_unrecognised_agent_provider_fails_before_the_turn_stream_starts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad `AGENT_PROVIDER` is a taxonomy 500 before streaming, and binds no session."""
    monkeypatch.setenv("AGENT_PROVIDER", "not-a-real-mode")
    with pytest.raises(RuntimeError, match="not-a-real-mode"):
        runtime_server._turn_provider_resolver()

    response = client.post("/invocations", json=_TURN_REQUEST)

    assert response.status_code == 500
    assert "text/event-stream" not in response.headers.get("content-type", "")
    assert response.json()["error"]["code"] == "internal_error"
    stranger = SessionOwner(rep_id="someone-else", tenant=DEMO_TENANT)
    runtime_server._cancellation_store.register_turn(
        str(_TURN_PAYLOAD["session_id"]), owner=stranger
    )


def test_a_live_adapter_that_cannot_be_built_fails_before_streaming_not_as_a_model_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every tier's adapter is built up front, so a config fault is a 500, not an SSE error."""

    def _broken_build(family: str, *, region_name: str) -> ModelProvider:
        raise RuntimeError(f"no adapter for {family}")

    monkeypatch.setattr("providers.factory.build_provider_for_family", _broken_build)
    monkeypatch.setenv("AGENT_PROVIDER", "live")

    response = client.post("/invocations", json=_TURN_REQUEST)

    assert response.status_code == 500
    assert "text/event-stream" not in response.headers.get("content-type", "")
    assert response.json()["error"]["code"] == "internal_error"


def test_a_single_injected_provider_serves_every_family() -> None:
    """`_as_resolver` keeps test injection: one provider answers whichever family is asked."""
    provider = _TextOnlyProvider()
    resolver = runtime_server._as_resolver(provider)
    assert resolver("anthropic") is provider
    assert resolver("openai") is provider
    named = runtime_server._as_resolver(lambda _family: provider)
    assert named("openai") is provider


def test_a_rejected_invocation_logs_the_error_source(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The `invocation_rejected` log line carries the offending control `source` in `reason`."""
    with (
        caplog.at_level(logging.INFO, logger=runtime_server.__name__),
        client.stream(
            "POST",
            "/invocations",
            json=_authed({**_TURN_PAYLOAD, "tuning": _supervisor_only(family="gemini")}),
        ) as response,
    ):
        assert response.status_code == 200
        "".join(response.iter_text())

    rejected = [
        json.loads(record.message)
        for record in caplog.records
        if record.message.startswith("{") and '"invocation_rejected"' in record.message
    ]
    assert rejected, "no invocation_rejected record was logged"
    assert "tuning.tiers.supervisor.family" in rejected[-1]["reason"]
    assert "validation_failed" in rejected[-1]["reason"]


def test_a_bad_tuning_turn_is_a_200_sse_error_event_carrying_the_message_and_calls_no_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn-level validation failure is one `error` event in a 200 SSE stream; no model call."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("a validation failure must never reach a model provider")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)

    with client.stream(
        "POST",
        "/invocations",
        json=_authed(
            {**_TURN_PAYLOAD, "tuning": _supervisor_only(effort="definitely-not-an-effort")}
        ),
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    events = _parse_sse_events(body)
    assert len(events) == 1, events
    assert events[0]["kind"] == "error"
    assert events[0]["error_kind"] == runtime_server._VALIDATION_ERROR_KIND
    # The user-safe validation message survives the hop: it names the rejected effort value.
    assert "definitely-not-an-effort" in (events[0]["error_message"] or "")


def test_a_flat_schema_one_tuning_block_is_refused_as_an_sse_error_and_calls_no_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The older flat `{family, effort}` block is a deliberate wire break, never reinterpreted."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("a refused tuning block must never reach a model provider")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)
    with client.stream(
        "POST",
        "/invocations",
        json=_authed({**_TURN_PAYLOAD, "tuning": {"family": "openai", "effort": "low"}}),
    ) as response:
        assert response.status_code == 200
        events = _parse_sse_events("".join(response.iter_text()))

    assert len(events) == 1
    assert events[0]["kind"] == "error"
    assert events[0]["error_kind"] == runtime_server._VALIDATION_ERROR_KIND
    assert f"schema {WIRE_SCHEMA_VERSION}" in (events[0]["error_message"] or "")


@pytest.mark.parametrize("schema", [2, WIRE_SCHEMA_VERSION])
def test_a_chat_turn_carrying_a_temperature_is_an_sse_error_naming_it_and_calls_no_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, schema: int
) -> None:
    """A tier `temperature` is one `invalid_request` frame naming its path; no model."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("a refused tuning block must never reach a model provider")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)
    tuning = {"schema": schema, "source": "ui.settings", "tiers": {"tool": {"temperature": 0.3}}}
    with client.stream(
        "POST", "/invocations", json=_authed({**_TURN_PAYLOAD, "tuning": tuning})
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse_events("".join(response.iter_text()))

    assert len(events) == 1, events
    assert events[0]["kind"] == "error"
    assert events[0]["error_kind"] == "invalid_request"
    assert "tuning.tiers.tool.temperature" in (events[0]["error_message"] or "")


def test_an_unknown_family_turn_is_also_a_200_sse_error_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconfigured family is delivered as a 200 SSE `error` event, with no model call."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("a validation failure must never reach a model provider")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)

    with client.stream(
        "POST",
        "/invocations",
        json=_authed({**_TURN_PAYLOAD, "tuning": _supervisor_only(family="gemini")}),
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    events = _parse_sse_events(body)
    assert len(events) == 1
    assert events[0]["kind"] == "error"
    assert "gemini" in (events[0]["error_message"] or "")


def test_the_d11_sse_split_is_gated_on_source_page_context_stays_400_tuning_is_a_stream(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page-context validation failure stays a 400; a tuning failure is a 200 SSE `error`."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("a validation failure must never reach a model provider")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)

    # Page-context descriptor failure (valid prompt + malformed descriptor): a hard 400, no stream.
    page_context = client.post(
        "/invocations", json=_authed({**_TURN_PAYLOAD, "page_context": {"kind": "entity"}})
    )
    assert page_context.status_code == 400
    body = page_context.json()
    assert body["error"]["code"] == "validation_failed"
    assert body["error"]["source"].startswith("page_context")
    assert "text/event-stream" not in page_context.headers["content-type"]

    # Turn-level tuning failure: a 200 SSE stream carrying exactly one `invalid_request` error event.
    with client.stream(
        "POST",
        "/invocations",
        json=_authed({**_TURN_PAYLOAD, "tuning": _supervisor_only(effort="nope")}),
    ) as response:
        assert response.status_code == 200
        stream_body = "".join(response.iter_text())
    events = _parse_sse_events(stream_body)
    assert len(events) == 1
    assert events[0]["kind"] == "error"
    assert events[0]["error_kind"] == runtime_server._VALIDATION_ERROR_KIND


def test_app_exposes_exactly_the_two_contract_routes_and_nothing_else() -> None:
    """The route table is exactly `/ping` and `/invocations`.

    `InvokeAgentRuntime` has no path parameter, so the relay cannot reach any other route.
    """
    paths = {route.path for route in runtime_server.app.routes}  # type: ignore[attr-defined]
    assert paths == {"/ping", "/invocations"}


def test_cancellation_is_a_mode_on_invocations_not_a_route() -> None:
    """No route path mentions cancellation."""
    paths = {route.path for route in runtime_server.app.routes}  # type: ignore[attr-defined]
    assert not [path for path in paths if "cancel" in path.lower()]
    assert runtime_server._CANCEL_MODE == "cancel"


def _probe_claims() -> dict[str, Any]:
    """The claims a probe caller's identity block carries."""
    return {"custom:rep_id": "rep-1", "custom:book": "acc-1,acc-2"}


def test_isolation_probe_refuses_an_unauthenticated_caller(client: TestClient) -> None:
    """The probe refuses an unauthenticated caller with the same 401 a normal turn returns."""
    response = client.post("/invocations", json={"mode": "isolation_probe", "role": None})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


def test_isolation_probe_reports_the_supervisors_registry_derived_surface(
    client: TestClient,
) -> None:
    """The supervisor probe advertises its direct tools plus every dispatchable subagent role.

    Asserted against `agents.registry.tool_registry_for` and `SUBAGENT_SPECS_BY_ROLE`.
    """
    from agents.registry import tool_registry_for
    from agents.subagents import SUBAGENT_SPECS_BY_ROLE
    from agents.supervisor import SUPERVISOR_ROLE

    response = client.post(
        "/invocations",
        json=_authed({"mode": "isolation_probe", "role": None}, _probe_claims()),
    )
    assert response.status_code == 200
    body = response.json()
    expected = {tool.name for tool in tool_registry_for(SUPERVISOR_ROLE)} | set(
        SUBAGENT_SPECS_BY_ROLE
    )
    assert set(body["advertised_tools"]) == expected
    assert body["role"] is None
    assert "outcome" not in body


@pytest.mark.parametrize("role", sorted(("research", "mutation", "analytics", "composer")))
def test_isolation_probe_reports_each_subagents_exact_tool_surface(
    client: TestClient, role: str
) -> None:
    """Each subagent probe advertises exactly `tool_registry_for(role)`, nothing more or less."""
    from agents.subagents import SUBAGENT_SPECS_BY_ROLE

    response = client.post(
        "/invocations",
        json=_authed({"mode": "isolation_probe", "role": role}, _probe_claims()),
    )
    assert response.status_code == 200
    assert set(response.json()["advertised_tools"]) == set(SUBAGENT_SPECS_BY_ROLE[role].tool_names)


@pytest.mark.parametrize(
    ("trigger", "expected_outcome"),
    [
        ("scope_denial", "denied"),
        ("empty_result", "no_match"),
        ("handler_error", "tool_failure"),
    ],
)
def test_isolation_probe_classifies_each_outcome_trigger_distinctly(
    client: TestClient, trigger: str, expected_outcome: str
) -> None:
    """The three synthetic triggers classify to three distinct outcomes.

    Classification runs through the production `agents.supervisor.classify_tool_result`.
    """
    response = client.post(
        "/invocations",
        json=_authed(
            {"mode": "isolation_probe", "role": "mutation", "trigger": trigger}, _probe_claims()
        ),
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == expected_outcome


def test_isolation_probe_rejects_an_unknown_role(client: TestClient) -> None:
    """An unrecognised role is a typed 400, never an empty tool surface that would read as a pass."""
    response = client.post(
        "/invocations",
        json=_authed({"mode": "isolation_probe", "role": "not-a-real-role"}, _probe_claims()),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


def test_isolation_probe_rejects_an_unknown_trigger(client: TestClient) -> None:
    """An unrecognised trigger is a typed 400, never a silently absent `outcome` field."""
    response = client.post(
        "/invocations",
        json=_authed(
            {"mode": "isolation_probe", "role": "mutation", "trigger": "nope"}, _probe_claims()
        ),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


def test_isolation_probe_never_invokes_a_provider_or_a_store(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe path never calls the model-provider seam or constructs a `Store`."""

    def _fail_provider(*_a: object) -> Any:
        raise AssertionError("isolation probe must never resolve a model provider")

    def _fail_store() -> Any:
        raise AssertionError("isolation probe must never construct a Store")

    monkeypatch.setattr(runtime_server, "_model_provider", _fail_provider)
    monkeypatch.setattr(runtime_server, "_record_store", _fail_store)

    response = client.post(
        "/invocations",
        json=_authed(
            {"mode": "isolation_probe", "role": "mutation", "trigger": "scope_denial"},
            _probe_claims(),
        ),
    )
    assert response.status_code == 200


def test_a_payload_without_the_probe_mode_still_takes_the_turn_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal payload without the probe field streams SSE unchanged."""
    monkeypatch.setattr(runtime_server, "_model_provider", lambda *_a: _TextOnlyProvider())
    with client.stream("POST", "/invocations", json=_TURN_REQUEST) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    assert any(event.get("kind") == "completed" for event in _parse_sse_events(body))


class _StubAccountStore:
    """A minimal `crm.store.store.Store` stand-in for `get_account`'s two reads.

    Returns one account under `tenant` and no contacts, so `dispatch_tool_call` reaches
    `require_scope`.
    """

    def __init__(self, *, tenant: str, account_id: str) -> None:
        self._tenant = tenant
        self._account_id = account_id

    def get_item(self, table: str, key: dict[str, str]) -> dict[str, object] | None:
        del table
        if key.get("account_id") != self._account_id:
            return None
        return {"account_id": self._account_id, "tenant": self._tenant, "name": "Acme"}

    def query_gsi(self, *args: object, **kwargs: object) -> tuple[list[dict[str, object]], None]:
        del args, kwargs
        return [], None


def _get_account_tool() -> RegisteredTool:
    """Return the real registered `get_account` tool, from the registry rather than a literal."""
    for tool in tool_registry_for(SUPERVISOR_ROLE):
        if tool.name == "get_account":
            return tool
    raise AssertionError("get_account is not a registered tool")


def _agent_identity(*, tenant: str, book: frozenset[str]) -> Identity:
    """Build the identity the Runtime holds for an HTTPS caller: `source='apigw'`, never `gateway`."""
    return Identity(
        rep_id="rep-1",
        book=book,
        full_visibility=False,
        principal_type="user",
        session_id="session-abc",
        source="apigw",
        tenant=tenant,
    )


class _FakeLambdaClient:
    """A `lambda:Invoke` stand-in that runs the real action handler on the invoked payload."""

    def __init__(self, store: object) -> None:
        self._store = store
        self.invocations: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, object]:
        import io

        function_name = str(kwargs["FunctionName"])
        event = json.loads(kwargs["Payload"])
        self.invocations.append({"function_name": function_name, "event": event})
        assert kwargs["InvocationType"] == "RequestResponse"
        tool_name = function_name.rpartition("-")[2]
        definition = BY_NAME[tool_name]
        module_path, _, attr = definition.handler.rpartition(".")
        handler = getattr(importlib.import_module(module_path), attr)
        result = handler(event, store=self._store)
        return {"Payload": io.BytesIO(json.dumps(result).encode("utf-8"))}


def _install_fake_lambda(monkeypatch: pytest.MonkeyPatch, store: object) -> _FakeLambdaClient:
    """Point `dispatch_tool_call` at a `_FakeLambdaClient` and set the function-name prefix."""
    client = _FakeLambdaClient(store)
    monkeypatch.setattr(crm_dispatch, "lambda_client", lambda: client)
    monkeypatch.setenv("ACTION_LAMBDA_PREFIX", "crm-agent-eval")
    return client


def test_dispatch_tool_call_authorises_an_in_app_agent_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool call from the agent path authenticates on `_agent_identity` and reaches the action.

    The Runtime's own identity has `source='apigw'`, which the Gateway `_identity` channel refuses.
    """
    identity = _agent_identity(tenant="eval-smoke-unit", book=frozenset({"acc-1"}))
    store = _StubAccountStore(tenant="eval-smoke-unit", account_id="acc-1")
    _install_fake_lambda(monkeypatch, store)

    result = dispatch_tool_call(_get_account_tool(), {"account_id": "acc-1"}, identity=identity)

    assert result.get("error", {}).get("code") != "authentication_failed"
    assert result["statusCode"] == 200
    assert result["body"]["account"]["account_id"] == "acc-1"


def test_dispatch_tool_call_forwards_the_book_without_widening_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The forwarded claims carry the caller's own book, so an out-of-book account is denied."""
    identity = _agent_identity(tenant="eval-smoke-unit", book=frozenset({"acc-1"}))
    store = _StubAccountStore(tenant="eval-smoke-unit", account_id="acc-9")
    _install_fake_lambda(monkeypatch, store)

    result = dispatch_tool_call(_get_account_tool(), {"account_id": "acc-9"}, identity=identity)

    assert result["error"]["code"] == "scope_denied"


def test_dispatch_tool_call_uses_the_agent_channel_and_never_the_gateway_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invoked payload carries `_agent_identity` alone — no `_identity` key, ever."""
    store = _StubAccountStore(tenant="eval-smoke-unit", account_id="acc-1")
    client = _install_fake_lambda(monkeypatch, store)

    dispatch_tool_call(
        _get_account_tool(),
        {"account_id": "acc-1"},
        identity=_agent_identity(tenant="eval-smoke-unit", book=frozenset({"acc-1"})),
    )

    sent = client.invocations[0]
    assert sent["function_name"] == "crm-agent-eval-get_account"
    event = sent["event"]
    assert AGENT_IDENTITY_KEY in event
    assert IDENTITY_KEY not in event
    assert event[AGENT_IDENTITY_KEY]["claims"][CLAIM_REP_ID] == "rep-1"
    assert event[AGENT_IDENTITY_KEY]["claims"][CLAIM_TENANT] == "eval-smoke-unit"
    assert event["account_id"] == "acc-1"


def test_dispatch_tool_call_reports_an_unhandled_function_error_as_a_typed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Lambda `FunctionError` becomes a taxonomy-shaped payload, never an escaping exception.

    The function's internal traceback does not cross the boundary.
    """
    import io

    class _ErroringClient:
        def invoke(self, **_kwargs: object) -> dict[str, object]:
            return {
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(b'{"errorMessage": "KeyError: internal detail"}'),
            }

    monkeypatch.setattr(crm_dispatch, "lambda_client", lambda: _ErroringClient())
    monkeypatch.setenv("ACTION_LAMBDA_PREFIX", "crm-agent-eval")

    result = dispatch_tool_call(
        _get_account_tool(),
        {"account_id": "acc-1"},
        identity=_agent_identity(tenant="eval-smoke-unit", book=frozenset({"acc-1"})),
    )

    assert result["statusCode"] == 500
    assert result["error"]["code"] == "internal_error"
    assert "internal detail" not in json.dumps(result)


def test_action_function_name_composes_from_the_configured_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The function name is `<ACTION_LAMBDA_PREFIX>-<tool>`, matching `module.lambda_action`."""
    monkeypatch.setenv("ACTION_LAMBDA_PREFIX", "crm-agent-eval")
    assert runtime_server.action_function_name("get_account") == "crm-agent-eval-get_account"
    monkeypatch.delenv("ACTION_LAMBDA_PREFIX", raising=False)
    assert runtime_server.action_function_name("get_account") == "get_account"


#: A forged Gateway identity with `full_visibility` on the `demo` tenant, using the one `source`
#: value `crm.envelope._from_gateway_identity` accepts.
_FORGED_GATEWAY_IDENTITY: dict[str, Any] = {
    "rep_id": "rep-attacker",
    "book": "",
    "full_visibility": True,
    "principal_type": "user",
    "source": "gateway",
    "tenant": DEMO_TENANT,
}

_FORGED_AGENT_IDENTITY: dict[str, Any] = {
    "claims": {
        CLAIM_REP_ID: "rep-attacker",
        "custom:full_visibility": "true",
        CLAIM_TENANT: DEMO_TENANT,
    },
    "principal_type": "user",
}

#: The same forgery on the `apigw` channel, in the shape an API Gateway Cognito authorizer
#: produces.
_FORGED_APIGW_CONTEXT: dict[str, Any] = {
    "requestId": "req-forged",
    "authorizer": {
        "jwt": {
            "claims": {
                CLAIM_REP_ID: "rep-attacker",
                "custom:full_visibility": "true",
                CLAIM_TENANT: DEMO_TENANT,
            }
        }
    },
}


def test_the_forged_identity_blocks_would_authenticate_if_they_reached_the_decoder() -> None:
    """The forged blocks authenticate at the decoder, and `_from_gateway_identity` still refuses a
    non-`gateway` `source`.

    This anchors the `401`s asserted by the forgery tests below.
    """
    gateway = decode_identity({IDENTITY_KEY: dict(_FORGED_GATEWAY_IDENTITY)})
    assert gateway.rep_id == "rep-attacker"
    assert gateway.full_visibility is True
    assert gateway.source == "gateway"

    agent = decode_identity({AGENT_IDENTITY_KEY: dict(_FORGED_AGENT_IDENTITY)})
    assert agent.rep_id == "rep-attacker"
    assert agent.full_visibility is True

    with pytest.raises(AuthenticationFailed):
        decode_identity({IDENTITY_KEY: {**_FORGED_GATEWAY_IDENTITY, "source": "apigw"}})

    stripped = strip_identity(
        {
            IDENTITY_KEY: dict(_FORGED_GATEWAY_IDENTITY),
            AGENT_IDENTITY_KEY: dict(_FORGED_AGENT_IDENTITY),
            "requestContext": {"authorizer": {"jwt": {"claims": {CLAIM_REP_ID: "rep-attacker"}}}},
            "account_id": "acc-1",
        }
    )
    assert stripped == {"account_id": "acc-1"}


@pytest.mark.parametrize(
    "channel", [IDENTITY_KEY, REQUEST_CONTEXT_KEY], ids=["gateway_channel", "apigw_channel"]
)
def test_the_turn_path_refuses_a_body_supplied_identity_channel(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, channel: str
) -> None:
    """The turn path refuses a body-supplied identity with a `401` and starts no turn."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("a forged identity must never reach a model provider")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)
    forged = (
        dict(_FORGED_GATEWAY_IDENTITY) if channel == IDENTITY_KEY else dict(_FORGED_APIGW_CONTEXT)
    )

    response = client.post("/invocations", json={**_TURN_PAYLOAD, channel: forged})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


def test_the_isolation_probe_refuses_a_body_supplied_identity_channel(client: TestClient) -> None:
    """The isolation probe refuses a body-supplied identity with a `401`."""
    response = client.post(
        "/invocations",
        json={
            "mode": "isolation_probe",
            "role": None,
            IDENTITY_KEY: dict(_FORGED_GATEWAY_IDENTITY),
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


def test_the_offline_identity_seam_is_unreachable_over_http(client: TestClient) -> None:
    """A hand-built `requestContext` sent as an HTTP body is stripped and does not authenticate.

    `resolve_identity` accepts that structure only on an in-process call.
    """
    seam_payload: dict[str, Any] = {
        "requestContext": {
            "requestId": "req-1",
            "authorizer": {
                "jwt": {
                    "claims": {
                        CLAIM_REP_ID: "rep-seam",
                        "token_use": "id",
                        "aud": TEST_API_CLIENT_ID,
                    }
                }
            },
        },
        "prompt": "hello",
        "session_id": "s1",
    }

    assert resolve_identity(seam_payload).rep_id == "rep-seam"

    response = client.post("/invocations", json=seam_payload)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


def test_invocations_rejects_an_empty_prompt_as_a_200_sse_error_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty `prompt` is a 200 SSE stream holding one `error` event, with no model call."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("an empty prompt must never reach a model provider")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)

    for prompt in ("", "   ", None):
        with client.stream(
            "POST", "/invocations", json=_authed({**_TURN_PAYLOAD, "prompt": prompt})
        ) as response:
            assert response.status_code == 200, prompt
            assert response.headers["content-type"].startswith("text/event-stream")
            body = "".join(response.iter_text())
        events = _parse_sse_events(body)
        assert len(events) == 1, (prompt, events)
        assert events[0]["kind"] == "error"
        assert events[0]["error_message"], prompt


def test_invocations_with_no_prompt_key_at_all_is_the_same_200_sse_error_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payload omitting `prompt` is delivered identically to an empty one."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("a missing prompt must never reach a model provider")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)

    payload = {k: v for k, v in _TURN_PAYLOAD.items() if k != "prompt"}
    with client.stream("POST", "/invocations", json=_authed(payload)) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    events = _parse_sse_events(body)
    assert len(events) == 1
    assert events[0]["kind"] == "error"


def test_an_unauthenticated_empty_prompt_is_still_a_401_not_a_400(client: TestClient) -> None:
    """Authentication precedes prompt validation: an anonymous caller learns nothing about input."""
    response = client.post("/invocations", json={"prompt": ""})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


class _NoTextCompletedProvider(ModelProvider):
    """A provider whose `completed` event carries no text and no preceding `text_delta`.

    That shape makes `agents.supervisor.provider_event_to_agent_event` raise `ValueError`.
    """

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(type="usage", input_tokens=1, output_tokens=1)
        yield ProviderEvent(type="completed", input_tokens=1, output_tokens=1)


def test_a_mid_stream_failure_becomes_a_typed_error_event_not_a_truncated_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise inside the already-started SSE body ends the stream with an `error` event."""
    monkeypatch.setattr(runtime_server, "_model_provider", lambda *_a: _NoTextCompletedProvider())

    with client.stream("POST", "/invocations", json=_TURN_REQUEST) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    events = _parse_sse_events(body)
    assert events[-1]["kind"] == "error"
    assert events[-1]["error_kind"] == "model_failure"
    assert events[-1]["error_message"] == "The assistant could not complete this turn."


def test_the_mid_stream_guard_logs_the_suppressed_traceback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A mid-stream raise reaches the client as a typed event and is logged with its traceback."""
    monkeypatch.setattr(runtime_server, "_model_provider", lambda *_a: _NoTextCompletedProvider())
    with (
        caplog.at_level(logging.ERROR, logger=runtime_server.__name__),
        client.stream("POST", "/invocations", json=_TURN_REQUEST) as response,
    ):
        "".join(response.iter_text())

    assert any("turn_failed" in record.message for record in caplog.records)
    assert any(record.exc_info is not None for record in caplog.records)


def test_configure_logging_attaches_a_root_handler() -> None:
    """`configure_logging` attaches a handler to the root logger."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        root.handlers.clear()
        runtime_server.configure_logging()
        assert root.handlers
        assert root.level <= logging.INFO
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_configure_logging_honours_log_level_and_ignores_a_bogus_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LOG_LEVEL` selects the root level; an unrecognised value falls back rather than raising."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        root.handlers.clear()
        monkeypatch.setenv("LOG_LEVEL", "debug")
        runtime_server.configure_logging()
        assert root.level == logging.DEBUG

        root.handlers.clear()
        monkeypatch.setenv("LOG_LEVEL", "not-a-level")
        runtime_server.configure_logging()
        assert root.level == logging.INFO
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


# ================================================================================================
# A retired `mode` value has no handler of its own
# ================================================================================================

_RETIRED_MODE: Final[str] = "delegation"
_UNRECOGNISED_MODE: Final[str] = "no_such_mode"

#: The fields a retired-mode caller sent: a tool to call and its arguments, with no prompt.
_RETIRED_MODE_FIELDS: dict[str, Any] = {
    "tool_name": "get_account",
    "tool_input": {"account_id": "acc-1"},
}


def _never_a_provider(*_a: object) -> ModelProvider:
    raise AssertionError("a body with no prompt must be refused before a provider is built")


def _outcome(response: Any) -> tuple[int, str, object]:
    """Reduce a response to its status, media type and payload: SSE event kinds or the JSON body."""
    media_type = response.headers["content-type"].split(";")[0]
    if media_type == "text/event-stream":
        events = _parse_sse_events(response.text)
        return response.status_code, media_type, [(e["kind"], e.get("error_kind")) for e in events]
    return response.status_code, media_type, response.json()


def _post_mode(client: TestClient, mode: str, payload: dict[str, Any]) -> tuple[int, str, object]:
    return _outcome(client.post("/invocations", json={runtime_server._MODE_KEY: mode, **payload}))


def test_a_retired_delegation_body_is_refused_exactly_like_an_unrecognised_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `delegation` body naming a tool and no prompt gets the turn path's one-`error` stream.

    No action Lambda is invoked and no provider is built, as for any unrecognised mode.
    """
    monkeypatch.setattr(runtime_server, "_model_provider", _never_a_provider)
    fake = _install_fake_lambda(
        monkeypatch, _StubAccountStore(tenant="eval-smoke-unit", account_id="acc-1")
    )

    retired = _post_mode(client, _RETIRED_MODE, _authed(_RETIRED_MODE_FIELDS))
    unrecognised = _post_mode(client, _UNRECOGNISED_MODE, _authed(_RETIRED_MODE_FIELDS))

    assert retired == unrecognised
    status, media_type, kinds = retired
    assert (status, media_type) == (200, "text/event-stream")
    assert isinstance(kinds, list) and [kind for kind, _ in kinds] == ["error"]
    assert fake.invocations == []


def test_a_retired_delegation_body_with_a_prompt_runs_an_ordinary_turn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a prompt, a `delegation` body streams the same turn as an unrecognised mode."""
    monkeypatch.setattr(runtime_server, "_model_provider", lambda *_a: _TextOnlyProvider())
    fake = _install_fake_lambda(
        monkeypatch, _StubAccountStore(tenant="eval-smoke-unit", account_id="acc-1")
    )
    fields = {**_RETIRED_MODE_FIELDS, **_TURN_PAYLOAD}

    retired = _post_mode(client, _RETIRED_MODE, _authed(fields))
    runtime_server._session_store = InMemorySessionStore()
    runtime_server._history_store = InMemoryConversationHistory()
    unrecognised = _post_mode(client, _UNRECOGNISED_MODE, _authed(fields))

    assert retired == unrecognised
    status, media_type, kinds = retired
    assert (status, media_type) == (200, "text/event-stream")
    assert isinstance(kinds, list) and kinds[-1][0] == "completed"
    assert fake.invocations == []


@pytest.mark.parametrize(
    "identity_fields",
    [
        {},
        {IDENTITY_KEY: dict(_FORGED_GATEWAY_IDENTITY)},
        {REQUEST_CONTEXT_KEY: dict(_FORGED_APIGW_CONTEXT)},
    ],
    ids=["no_identity", "gateway_channel", "apigw_channel"],
)
def test_a_retired_delegation_body_without_a_valid_identity_is_refused_like_any_turn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, identity_fields: dict[str, Any]
) -> None:
    """An unauthenticated or forged-identity `delegation` body gets the turn path's `401`."""
    monkeypatch.setattr(runtime_server, "_model_provider", _never_a_provider)
    fake = _install_fake_lambda(
        monkeypatch, _StubAccountStore(tenant="eval-smoke-unit", account_id="acc-1")
    )
    payload = {**_RETIRED_MODE_FIELDS, **identity_fields}

    retired = _post_mode(client, _RETIRED_MODE, payload)

    assert retired == _post_mode(client, _UNRECOGNISED_MODE, payload)
    status, _media_type, body = retired
    assert status == 401
    assert isinstance(body, dict) and body["error"]["code"] == "authentication_failed"
    assert fake.invocations == []


class _GetAccountThenAnswer(ModelProvider):
    """Calls `get_account` for `acc-1` directly, then answers with fixed text."""

    def __init__(self) -> None:
        self.calls = 0

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.calls += 1
        return self._emit(first=self.calls == 1)

    async def _emit(self, *, first: bool) -> AsyncIterator[ProviderEvent]:
        if first:
            yield ProviderEvent(
                type="tool_call",
                tool_name="get_account",
                tool_input={"account_id": "acc-1"},
                tool_id="t1",
            )
            yield ProviderEvent(type="usage", input_tokens=1, output_tokens=1)
            yield ProviderEvent(type="completed", awaiting_tools=True)
            return
        yield ProviderEvent(type="text_delta", text="Here is Acme.")
        yield ProviderEvent(type="usage", input_tokens=1, output_tokens=1)
        yield ProviderEvent(type="completed", awaiting_tools=False)


def test_a_body_supplied_identity_cannot_widen_an_authenticated_turns_dispatched_claims(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legitimate turn adding forged identity blocks dispatches with the token holder's claims."""
    fake = _install_fake_lambda(
        monkeypatch, _StubAccountStore(tenant="eval-smoke-unit", account_id="acc-1")
    )
    monkeypatch.setattr(runtime_server, "_model_provider", lambda *_a: _GetAccountThenAnswer())
    claims = {**_TURN_CLAIMS, "custom:book": "acc-1", "custom:full_visibility": "false"}

    with client.stream(
        "POST",
        "/invocations",
        json=_authed(
            {
                **_TURN_PAYLOAD,
                IDENTITY_KEY: dict(_FORGED_GATEWAY_IDENTITY),
                REQUEST_CONTEXT_KEY: dict(_FORGED_APIGW_CONTEXT),
            },
            claims,
        ),
    ) as response:
        assert response.status_code == 200
        "".join(response.iter_text())

    assert len(fake.invocations) == 1
    dispatched = fake.invocations[0]["event"]
    assert IDENTITY_KEY not in dispatched
    assert REQUEST_CONTEXT_KEY not in dispatched
    forwarded = dispatched[AGENT_IDENTITY_KEY]["claims"]
    assert forwarded[CLAIM_REP_ID] == "rep-1"
    assert forwarded[CLAIM_TENANT] == "eval-smoke-unit"
    assert forwarded["custom:full_visibility"] is False


# ================================================================================================
# The authenticated `{"mode": "cancel"}` side channel
# ================================================================================================

_CANCEL_SESSION: Final[str] = "session-cancel-1"
_CANCEL_TENANT: Final[str] = "eval-cancel-unit"


def _cancel_claims(*, rep_id: str = "rep-1", tenant: str = _CANCEL_TENANT) -> dict[str, Any]:
    """The claims a cancel caller's identity block carries."""
    return {CLAIM_REP_ID: rep_id, "custom:book": "acc-1", CLAIM_TENANT: tenant}


def _cancel_request(
    *,
    session_id: str = _CANCEL_SESSION,
    rep_id: str = "rep-1",
    tenant: str = _CANCEL_TENANT,
) -> dict[str, Any]:
    """One authenticated cancel body: the session to stop plus the caller's identity block."""
    return _authed({"session_id": session_id}, _cancel_claims(rep_id=rep_id, tenant=tenant))


def _post_cancel(client: TestClient, body: dict[str, Any]) -> Any:
    """Post one cancel as the relay does: the cancel `mode` on `/invocations`.

    The mode value is read from `runtime_server`.
    """
    return client.post(
        "/invocations", json={**body, runtime_server._MODE_KEY: runtime_server._CANCEL_MODE}
    )


def _cancel_identity(*, rep_id: str = "rep-1", tenant: str = _CANCEL_TENANT) -> Identity:
    """The identity the Runtime resolves for the cancel caller."""
    return Identity(
        rep_id=rep_id,
        book=frozenset({"acc-1"}),
        full_visibility=False,
        principal_type="user",
        session_id=_CANCEL_SESSION,
        source="apigw",
        tenant=tenant,
    )


def _register(*, rep_id: str = "rep-1", tenant: str = _CANCEL_TENANT) -> None:
    """Register the turn the way `invocations` does, so a cancel has something to authorize against."""
    runtime_server._cancellation_store.register_turn(
        _CANCEL_SESSION, owner=SessionOwner.of(_cancel_identity(rep_id=rep_id, tenant=tenant))
    )


def test_cancel_accepts_the_owning_callers_request_and_marks_the_session(
    client: TestClient,
) -> None:
    _register()

    response = _post_cancel(client, _cancel_request())

    assert response.status_code == 200
    assert response.json() == {"session_id": _CANCEL_SESSION, "disposition": "accepted"}
    assert runtime_server._cancellation_store.is_cancelled(_CANCEL_SESSION) is True


def test_cancel_is_idempotent_for_the_owner(client: TestClient) -> None:
    _register()

    first = _post_cancel(client, _cancel_request())
    second = _post_cancel(client, _cancel_request())

    assert first.json()["disposition"] == "accepted"
    assert second.json()["disposition"] == "already_requested"
    assert second.status_code == 200


def test_an_unauthenticated_cancel_is_denied(client: TestClient) -> None:
    """An unauthenticated cancel is denied and writes no marker."""
    _register()

    response = _post_cancel(client, {"session_id": _CANCEL_SESSION})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"
    assert runtime_server._cancellation_store.is_cancelled(_CANCEL_SESSION) is False


def test_a_cancel_with_a_malformed_bearer_token_is_denied(client: TestClient) -> None:
    """A cancel carrying only a malformed bearer token is refused for lacking an identity block."""
    _register()

    response = client.post(
        "/invocations",
        json={
            "session_id": _CANCEL_SESSION,
            runtime_server._MODE_KEY: runtime_server._CANCEL_MODE,
        },
        headers={"Authorization": "Bearer not-a-jwt"},
    )

    assert response.status_code == 401
    assert runtime_server._cancellation_store.is_cancelled(_CANCEL_SESSION) is False


def test_a_cross_session_cancel_is_denied(client: TestClient) -> None:
    """A different rep cannot cancel another user's session_id."""
    _register(rep_id="rep-1")

    response = _post_cancel(
        client,
        _cancel_request(rep_id="rep-2"),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scope_denied"
    assert runtime_server._cancellation_store.is_cancelled(_CANCEL_SESSION) is False


def test_a_cross_tenant_cancel_is_denied_even_for_an_identical_rep_id(client: TestClient) -> None:
    _register(tenant=_CANCEL_TENANT)

    response = _post_cancel(
        client,
        _cancel_request(tenant="eval-other-unit"),
    )

    assert response.status_code == 403
    assert runtime_server._cancellation_store.is_cancelled(_CANCEL_SESSION) is False


def test_cancelling_an_unknown_session_is_denied_identically_to_a_foreign_one(
    client: TestClient,
) -> None:
    """The two denials are indistinguishable, so the cancel mode is not a session-id oracle."""
    _register(rep_id="rep-1")

    unknown = _post_cancel(client, _cancel_request(session_id="session-never-seen"))
    foreign = _post_cancel(client, _cancel_request(rep_id="rep-2"))

    assert unknown.status_code == foreign.status_code == 403
    assert unknown.json() == foreign.json()


def test_a_cancel_cannot_authenticate_itself_with_another_channel(client: TestClient) -> None:
    """A cancel honours only the `_agent_identity` channel and refuses the other two."""
    _register()

    for channel_key, forged in (
        (IDENTITY_KEY, dict(_FORGED_GATEWAY_IDENTITY)),
        (REQUEST_CONTEXT_KEY, dict(_FORGED_APIGW_CONTEXT)),
    ):
        response = _post_cancel(client, {"session_id": _CANCEL_SESSION, channel_key: forged})
        assert response.status_code == 401, channel_key
        assert runtime_server._cancellation_store.is_cancelled(_CANCEL_SESSION) is False


@pytest.mark.parametrize(
    ("block", "label"),
    [
        ("not-an-object", "not_an_object"),
        ({"principal_type": "user"}, "no_claims"),
        ({"claims": "not-an-object", "principal_type": "user"}, "claims_not_an_object"),
        ({"claims": {}, "principal_type": "user"}, "empty_claims"),
        ({"claims": {CLAIM_TENANT: _CANCEL_TENANT}, "principal_type": "user"}, "no_rep_id"),
        ({"session_id": _CANCEL_SESSION, "principal_type": "user"}, "session_id_only"),
    ],
)
def test_a_malformed_agent_identity_block_is_denied_rather_than_defaulted(
    client: TestClient, block: object, label: str
) -> None:
    """A present but malformed `_agent_identity` block is a `401`, never a permissive default."""
    _register()

    response = _post_cancel(client, {"session_id": _CANCEL_SESSION, AGENT_IDENTITY_KEY: block})

    assert response.status_code == 401, label
    assert response.json()["error"]["code"] == "authentication_failed"
    assert runtime_server._cancellation_store.is_cancelled(_CANCEL_SESSION) is False


@pytest.mark.parametrize(
    ("block", "label"),
    [
        ("not-an-object", "not_an_object"),
        ({"principal_type": "user"}, "no_claims"),
        ({"claims": {}, "principal_type": "user"}, "empty_claims"),
        ({"claims": {CLAIM_TENANT: "eval-smoke-unit"}, "principal_type": "user"}, "no_rep_id"),
    ],
)
def test_the_turn_path_denies_a_malformed_agent_identity_block(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, block: object, label: str
) -> None:
    """The turn path applies the same rule: an unusable identity block starts no turn."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("an unusable identity block must never reach a model provider")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)

    response = client.post("/invocations", json={**_TURN_PAYLOAD, AGENT_IDENTITY_KEY: block})

    assert response.status_code == 401, label
    assert response.json()["error"]["code"] == "authentication_failed"


def test_a_cancel_with_a_missing_session_id_is_rejected_without_writing_a_marker(
    client: TestClient,
) -> None:
    _register()

    response = _post_cancel(client, _authed({}, _cancel_claims()))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"
    assert runtime_server._cancellation_store.is_cancelled(_CANCEL_SESSION) is False


@pytest.mark.parametrize("body", ["not json at all", "[1, 2, 3]"])
def test_a_malformed_cancel_body_is_a_typed_400_not_a_500(client: TestClient, body: str) -> None:
    """A cancel body that is not a JSON object is a typed `400`, never a `500`."""
    response = client.post(
        "/invocations",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


def test_a_cancel_after_the_turn_reached_a_terminal_event_is_a_no_op(client: TestClient) -> None:
    """A cancel after the turn's terminal event reports success and writes no marker."""
    _register()
    runtime_server._cancellation_store.mark_terminal(_CANCEL_SESSION)

    response = _post_cancel(client, _cancel_request())

    assert response.status_code == 200
    assert response.json()["disposition"] == "already_terminal"
    assert runtime_server._cancellation_store.is_cancelled(_CANCEL_SESSION) is False


def test_a_cancel_starts_no_turn_and_reaches_no_model_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel is dispatched before `decode_invocation` and never reaches the model provider."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("a cancel must never construct a model provider")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)
    _register()

    response = _post_cancel(client, _cancel_request())

    assert response.status_code == 200
    assert response.json()["disposition"] == "accepted"
    assert response.headers["content-type"].startswith("application/json")


def test_a_cancel_carrying_a_prompt_still_cancels_and_starts_no_turn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body carrying both the cancel mode and a prompt cancels and starts no turn."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("a cancel must never start a turn, even carrying a prompt")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)
    _register()

    response = _post_cancel(client, {**_cancel_request(), "prompt": "keep going"})

    assert response.status_code == 200
    assert response.json() == {"session_id": _CANCEL_SESSION, "disposition": "accepted"}
    assert runtime_server._cancellation_store.is_cancelled(_CANCEL_SESSION) is True


def test_an_unrecognised_mode_takes_the_turn_path_rather_than_cancelling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A near-miss mode value takes the turn path, writes no cancel marker and calls no model."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("a body with no prompt must be refused before a provider is built")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)
    _register()

    with client.stream(
        "POST", "/invocations", json={**_cancel_request(), runtime_server._MODE_KEY: "cancel_turn"}
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    events = _parse_sse_events(body)
    assert len(events) == 1 and events[0]["kind"] == "error"
    assert runtime_server._cancellation_store.is_cancelled(_CANCEL_SESSION) is False


# ------------------------------------------------------------------------------------------------
# The turn loop's between-steps cancellation check
# ------------------------------------------------------------------------------------------------


def test_a_turn_registers_its_session_so_a_later_cancel_can_be_authorized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn driven over HTTP registers its session, so the owner's cancel is accepted and a
    stranger's is refused.
    """
    monkeypatch.setattr(runtime_server, "_model_provider", lambda *_a: _TextOnlyProvider())
    turn = client.post(
        "/invocations",
        json=_authed({"prompt": "hello", "session_id": _CANCEL_SESSION}, _cancel_claims()),
    )
    assert turn.status_code == 200
    "".join(turn.iter_text())

    stranger = _post_cancel(client, _cancel_request(rep_id="rep-2"))
    assert stranger.status_code == 403


def test_a_second_callers_turn_cannot_hijack_an_existing_sessions_cancel_rights(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second caller's turn on an existing session gets a typed 403 before streaming."""
    monkeypatch.setattr(runtime_server, "_model_provider", lambda *_a: _TextOnlyProvider())
    _register(rep_id="rep-1")

    response = client.post(
        "/invocations",
        json=_authed(
            {"prompt": "hello", "session_id": _CANCEL_SESSION}, _cancel_claims(rep_id="rep-2")
        ),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scope_denied"
    assert not response.headers["content-type"].startswith("text/event-stream")
