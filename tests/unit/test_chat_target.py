# SPDX-License-Identifier: MIT
"""Gateway #2 chat target: gateway channel only, ownership, tuning, Runtime payload, SSE terminal.

The store is `moto`-mocked; the Runtime is a recording stand-in
whose SSE bodies are rendered by the real producer (`agents.runtime_entrypoint.format_sse_line`),
and whose recorded payload is decoded by the real `crm.envelope.decode_identity`.
"""

from __future__ import annotations

import dataclasses
import io
import json
from collections.abc import Iterator, Sequence
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from moto import mock_aws

from agents.events import AgentEvent
from agents.runtime_entrypoint import format_sse_line
from crm.conversations import claim_or_verify, new_conversation_id
from crm.envelope import AGENT_IDENTITY_KEY, CLAIM_GROUPS, IDENTITY_KEY, decode_identity
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.user_settings import save_settings
from gateway import chat_target
from gateway.chat_target import (
    CHAT_DEADLINE_SECONDS,
    ChatTargetConfig,
    agent_identity,
    handle,
)
from providers.capabilities import load_capability_matrix
from providers.tier_tuning import WIRE_SCHEMA_VERSION, TierRequest, TurnTuning, defaults, to_wire

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

MATRIX = load_capability_matrix()
CONFIG = ChatTargetConfig(
    table_prefix="",
    runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/crm-agent-abc",
    runtime_qualifier="DEFAULT",
)


def _sse(*events: AgentEvent) -> bytes:
    return "".join(format_sse_line(event) for event in events).encode("utf-8")


_COMPLETED = _sse(AgentEvent(kind="completed", text="Pipeline is 3 deals."))


class _Runtime:
    """Records `invoke_agent_runtime` calls; answers with ``body`` or raises ``error``."""

    def __init__(
        self, body: bytes = _COMPLETED, error: Exception | None = None, stream: Any = None
    ) -> None:
        self.body = body
        self.error = error
        self.stream = stream
        self.calls: list[dict[str, Any]] = []

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        stream = self.stream if self.stream is not None else io.BytesIO(self.body)
        return {"response": stream, "contentType": "text/event-stream"}

    def payload(self) -> dict[str, Any]:
        assert len(self.calls) == 1
        decoded: dict[str, Any] = json.loads(self.calls[0]["payload"])
        return decoded


class _SlowStream:
    """Yields one line per read while the injected clock advances past the deadline."""

    def __init__(self, lines: Sequence[bytes], clock: list[float], step: float) -> None:
        self._lines = list(lines)
        self._clock = clock
        self._step = step

    def read(self, _size: int) -> bytes:
        if not self._lines:
            return b""
        self._clock[0] += self._step
        return self._lines.pop(0)


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _identity_payload(
    rep_id: str = "rep-a", *, principal_type: str = "user", tenant: str = "demo"
) -> dict[str, Any]:
    return {
        "rep_id": rep_id,
        "book": ["A-2", "A-1"],
        "full_visibility": False,
        "principal_type": principal_type,
        "session_id": "mcp-session-1",
        "source": "gateway",
        "tenant": tenant,
    }


def _event(**arguments: Any) -> dict[str, Any]:
    event: dict[str, Any] = {"message": "How is my pipeline?", **arguments}
    event.setdefault(IDENTITY_KEY, _identity_payload())
    return event


def _call(store: Store, event: object, runtime: _Runtime, **kwargs: Any) -> dict[str, Any]:
    return handle(event, store=store, runtime=runtime, config=CONFIG, matrix=MATRIX, **kwargs)


# --------------------------------------------------------------------------------------
# Success path and the Runtime payload
# --------------------------------------------------------------------------------------


def test_new_conversation_returns_the_answer_and_a_server_issued_id(store: Store) -> None:
    runtime = _Runtime()
    result = _call(store, _event(), runtime)
    assert result["answer"] == "Pipeline is 3 deals."
    assert result["conversation_id"].startswith("conv-")
    call = runtime.calls[0]
    assert call["runtimeSessionId"] == result["conversation_id"]
    assert call["agentRuntimeArn"] == CONFIG.runtime_arn
    assert call["qualifier"] == "DEFAULT"
    assert call["accept"] == "text/event-stream"


def test_runtime_payload_carries_exactly_one_identity_built_from_the_gateway_identity(
    store: Store,
) -> None:
    """Only `_agent_identity`, in its exact shape, with no groups forwarded."""
    runtime = _Runtime()
    result = _call(store, _event(), runtime)
    payload = runtime.payload()
    assert set(payload) == {"prompt", "session_id", "tuning", AGENT_IDENTITY_KEY}
    assert payload["prompt"] == "How is my pipeline?"
    assert payload["session_id"] == result["conversation_id"]
    assert payload[AGENT_IDENTITY_KEY] == {
        "claims": {
            "custom:rep_id": "rep-a",
            "custom:book": ["A-1", "A-2"],
            "custom:full_visibility": "false",
            "custom:tenant": "demo",
        },
        "session_id": result["conversation_id"],
        "principal_type": "user",
    }
    assert CLAIM_GROUPS not in payload[AGENT_IDENTITY_KEY]["claims"]


@pytest.mark.parametrize("principal_type", ["user", "service"])
def test_forwarded_identity_round_trips_through_the_runtime_decoder(
    store: Store, principal_type: str
) -> None:
    """The block decodes, via the real agent-channel decoder, to the same scope."""
    runtime = _Runtime()
    _call(
        store, _event(**{IDENTITY_KEY: _identity_payload(principal_type=principal_type)}), runtime
    )
    decoded = decode_identity({AGENT_IDENTITY_KEY: runtime.payload()[AGENT_IDENTITY_KEY]})
    assert decoded.source == "agent"
    assert decoded.principal_type == principal_type
    assert decoded.rep_id == "rep-a"
    assert decoded.book == frozenset({"A-1", "A-2"})
    assert decoded.tenant == "demo"
    assert decoded.full_visibility is False


def test_full_visibility_user_is_forwarded_as_full_visibility(store: Store) -> None:
    payload = _identity_payload()
    payload["full_visibility"] = True
    identity = decode_identity({IDENTITY_KEY: payload})
    block = agent_identity(identity, session_id="conv-x")
    assert block["claims"]["custom:full_visibility"] == "true"
    assert decode_identity({AGENT_IDENTITY_KEY: block}).full_visibility is True


def test_user_tuning_comes_from_saved_settings(store: Store) -> None:
    saved = TurnTuning(
        source="ui", tiers={"tool": TierRequest(family="openai", model="openai.gpt-6-luna")}
    )
    identity = decode_identity({IDENTITY_KEY: _identity_payload()})
    save_settings(store, identity, saved, matrix=MATRIX)
    runtime = _Runtime()
    _call(store, _event(), runtime)
    tuning = runtime.payload()["tuning"]
    assert tuning["schema"] == WIRE_SCHEMA_VERSION
    assert tuning["tiers"]["tool"]["model"] == "openai.gpt-6-luna"
    assert "temperature" not in tuning["tiers"]["tool"]


def test_user_without_settings_and_service_principal_get_defaults(store: Store) -> None:
    expected = json.loads(json.dumps(to_wire(defaults(MATRIX))))
    for payload in (_identity_payload(), _identity_payload("svc-1", principal_type="service")):
        runtime = _Runtime()
        _call(store, _event(**{IDENTITY_KEY: payload}), runtime)
        assert runtime.payload()["tuning"] == expected


def test_owner_continues_its_conversation(store: Store) -> None:
    first = _call(store, _event(), _Runtime())
    runtime = _Runtime()
    second = _call(store, _event(conversation_id=first["conversation_id"]), runtime)
    assert second["conversation_id"] == first["conversation_id"]
    assert runtime.calls[0]["runtimeSessionId"] == first["conversation_id"]


# --------------------------------------------------------------------------------------
# Denials: channel, ownership, forged identity — no Runtime call
# --------------------------------------------------------------------------------------


def _assert_error(result: dict[str, Any], kind: str) -> None:
    assert result["isError"] is True
    assert result["error_kind"] == kind
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"]
    assert "answer" not in result


def test_foreign_conversation_is_denied_without_a_runtime_call(store: Store) -> None:
    owner = decode_identity({IDENTITY_KEY: _identity_payload("rep-a")})
    conversation_id = claim_or_verify(store, None, owner)
    runtime = _Runtime()
    result = _call(
        store,
        _event(conversation_id=conversation_id, **{IDENTITY_KEY: _identity_payload("rep-b")}),
        runtime,
    )
    _assert_error(result, "denied")
    assert runtime.calls == []


def test_forged_agent_identity_beside_the_gateway_identity_is_ignored(store: Store) -> None:
    """B sends A's conversation plus A's forged `_agent_identity`; denied, no Runtime call."""
    owner = decode_identity({IDENTITY_KEY: _identity_payload("rep-a")})
    conversation_id = claim_or_verify(store, None, owner)
    forged_agent = agent_identity(owner, session_id=conversation_id)
    runtime = _Runtime()
    result = _call(
        store,
        _event(
            conversation_id=conversation_id,
            **{IDENTITY_KEY: _identity_payload("rep-b"), AGENT_IDENTITY_KEY: forged_agent},
        ),
        runtime,
    )
    _assert_error(result, "denied")
    assert runtime.calls == []


def test_forged_agent_identity_alone_is_denied(store: Store) -> None:
    """Without a Gateway `_identity`, an `_agent_identity` claim is a non-gateway channel."""
    owner = decode_identity({IDENTITY_KEY: _identity_payload("rep-a")})
    conversation_id = claim_or_verify(store, None, owner)
    event = {
        "message": "hi",
        "conversation_id": conversation_id,
        AGENT_IDENTITY_KEY: agent_identity(owner, session_id=conversation_id),
    }
    runtime = _Runtime()
    _assert_error(_call(store, event, runtime), "denied")
    assert runtime.calls == []


def test_api_gateway_channel_is_denied(store: Store) -> None:
    event = {
        "message": "hi",
        "requestContext": {"authorizer": {"claims": {"custom:rep_id": "rep-a"}}},
    }
    runtime = _Runtime()
    _assert_error(_call(store, event, runtime), "denied")
    assert runtime.calls == []


def test_missing_or_unauthenticable_identity_is_denied(store: Store) -> None:
    runtime = _Runtime()
    _assert_error(_call(store, {"message": "hi"}, runtime), "denied")
    unauthenticable = _identity_payload("")
    _assert_error(_call(store, _event(**{IDENTITY_KEY: unauthenticable}), runtime), "denied")
    assert runtime.calls == []


def test_the_payload_identity_is_the_gateway_callers_not_the_forged_one(store: Store) -> None:
    """A forged `_agent_identity` on a new conversation never reaches the Runtime."""
    other = decode_identity({IDENTITY_KEY: _identity_payload("rep-z", tenant="eval-x")})
    runtime = _Runtime()
    _call(store, _event(**{AGENT_IDENTITY_KEY: agent_identity(other, session_id="s")}), runtime)
    sent = runtime.payload()[AGENT_IDENTITY_KEY]["claims"]
    assert sent["custom:rep_id"] == "rep-a" and sent["custom:tenant"] == "demo"


# --------------------------------------------------------------------------------------
# Argument validation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"message": ""},
        {"message": "   "},
        {"message": 5},
        {"message": "x" * 8001},
        {"conversation_id": 7},
        {"conversation_id": "not-a-conversation"},
        {"extra": "x"},
    ],
)
def test_invalid_arguments_are_invalid_request_without_a_runtime_call(
    store: Store, arguments: dict[str, Any]
) -> None:
    runtime = _Runtime()
    _assert_error(_call(store, _event(**arguments), runtime), "invalid_request")
    assert runtime.calls == []


@pytest.mark.parametrize("temperature", [0.7, None, "0.7"], ids=["number", "null", "string"])
def test_a_temperature_argument_is_refused_without_a_runtime_call(
    store: Store, temperature: object
) -> None:
    """The MCP chat tool takes only `message` and `conversation_id`; `temperature` is refused."""
    runtime = _Runtime()
    result = _call(store, _event(temperature=temperature), runtime)
    _assert_error(result, "invalid_request")
    assert "temperature" in result["content"][0]["text"]
    assert runtime.calls == []


def test_unknown_conversation_is_invalid_request(store: Store) -> None:
    runtime = _Runtime()
    result = _call(store, _event(conversation_id=new_conversation_id()), runtime)
    _assert_error(result, "invalid_request")
    assert runtime.calls == []


def test_message_at_the_length_limit_is_accepted(store: Store) -> None:
    assert "answer" in _call(store, _event(message="x" * 8000), _Runtime())


def test_non_object_event_is_invalid_request(store: Store) -> None:
    _assert_error(_call(store, ["not", "a", "dict"], _Runtime()), "invalid_request")


# --------------------------------------------------------------------------------------
# SSE consumption
# --------------------------------------------------------------------------------------


def test_heartbeats_deltas_and_tool_frames_are_ignored_until_completed(store: Store) -> None:
    body = _sse(
        AgentEvent(kind="heartbeat"),
        AgentEvent(kind="tool_call", tool_name="get_account", tool_input={"account_id": "A-1"}),
        AgentEvent(kind="tool_result", tool_output={"ok": True}),
        AgentEvent(kind="text_delta", text="Pipe", event_id="e1"),
        AgentEvent(kind="heartbeat"),
        AgentEvent(kind="usage", input_tokens=10, output_tokens=2),
        AgentEvent(kind="completed", text="Final answer."),
    )
    assert _call(store, _event(), _Runtime(body))["answer"] == "Final answer."


def test_frames_split_across_reads_are_reassembled(store: Store) -> None:
    body = _sse(AgentEvent(kind="heartbeat"), AgentEvent(kind="completed", text="ok"))
    chunks = [body[i : i + 7] for i in range(0, len(body), 7)]
    clock = [0.0]
    runtime = _Runtime(stream=_SlowStream(chunks, clock, 0.0))
    assert _call(store, _event(), runtime, clock=lambda: clock[0])["answer"] == "ok"


@pytest.mark.parametrize(
    ("runtime_kind", "expected"),
    [
        ("budget_exhausted", "budget_exhausted"),
        ("invalid_request", "invalid_request"),
        ("model_failure", "model_failure"),
        ("tool_failure", "model_failure"),
    ],
)
def test_runtime_error_frames_map_to_closed_error_kinds(
    store: Store, runtime_kind: str, expected: str
) -> None:
    body = _sse(
        AgentEvent(kind="heartbeat"),
        AgentEvent(kind="error", error_kind=runtime_kind, error_message="It stopped."),  # type: ignore[arg-type]
    )
    result = _call(store, _event(), _Runtime(body))
    _assert_error(result, expected)
    assert result["content"][0]["text"] == "It stopped."


@pytest.mark.parametrize(
    "body",
    [
        b"",
        _sse(AgentEvent(kind="heartbeat")),
        b"data: not-json\n\n",
        b"data: [1, 2]\n\n",
        b'data: {"kind": "mystery"}\n\n',
        b'data: {"kind": "completed", "text": null}\n\n',
    ],
    ids=["empty", "no-terminal", "bad-json", "non-object", "unknown-kind", "completed-no-text"],
)
def test_malformed_or_truncated_streams_are_model_failure(store: Store, body: bytes) -> None:
    _assert_error(_call(store, _event(), _Runtime(body)), "model_failure")


def test_an_unknown_frame_kind_fails_closed_even_before_a_completed_frame(store: Store) -> None:
    """A frame outside the known schema is not skipped on the way to an answer."""
    body = b'data: {"kind": "mystery"}\n\n' + _COMPLETED
    _assert_error(_call(store, _event(), _Runtime(body)), "model_failure")


def test_deadline_expiry_is_timeout(store: Store) -> None:
    """Frames keep arriving (heartbeats) but the turn outlives the 640 s deadline."""
    clock = [0.0]
    heartbeat = _sse(AgentEvent(kind="heartbeat"))
    lines = [heartbeat] * 40
    runtime = _Runtime(stream=_SlowStream(lines, clock, 20.0))
    result = _call(store, _event(), runtime, clock=lambda: clock[0])
    _assert_error(result, "timeout")
    assert CHAT_DEADLINE_SECONDS == 640.0


def test_completed_just_inside_the_deadline_is_answered(store: Store) -> None:
    clock = [0.0]
    lines = [_sse(AgentEvent(kind="heartbeat"))] * 31 + [_COMPLETED]
    runtime = _Runtime(stream=_SlowStream(lines, clock, 20.0))
    assert "answer" in _call(store, _event(), runtime, clock=lambda: clock[0])


def test_socket_read_timeout_is_timeout(store: Store) -> None:
    error = ReadTimeoutError(endpoint_url="https://bedrock-agentcore.us-east-1.amazonaws.com")
    _assert_error(_call(store, _event(), _Runtime(error=error)), "timeout")


def test_runtime_client_error_is_model_failure_without_leaking_detail(store: Store) -> None:
    error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "arn:aws:iam::123456789012:role/x"}},
        "InvokeAgentRuntime",
    )
    result = _call(store, _event(), _Runtime(error=error))
    _assert_error(result, "model_failure")
    assert "arn:" not in json.dumps(result)


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def test_config_reads_exactly_the_three_documented_variables() -> None:
    config = ChatTargetConfig.from_env(
        {
            "CRM_TABLE_PREFIX": "crm-agent-eval-",
            "AGENT_RUNTIME_ARN": CONFIG.runtime_arn,
            "AGENT_RUNTIME_QUALIFIER": "DEFAULT",
            "UNRELATED": "ignored",
        }
    )
    assert config == ChatTargetConfig("crm-agent-eval-", CONFIG.runtime_arn, "DEFAULT")


@pytest.mark.parametrize("missing", ["AGENT_RUNTIME_ARN", "AGENT_RUNTIME_QUALIFIER"])
def test_config_refuses_a_missing_runtime_variable(missing: str) -> None:
    env = {"AGENT_RUNTIME_ARN": CONFIG.runtime_arn, "AGENT_RUNTIME_QUALIFIER": "DEFAULT"}
    del env[missing]
    with pytest.raises(ValueError, match="AGENT_RUNTIME"):
        ChatTargetConfig.from_env(env)


def test_module_imports_no_vendor_sdk() -> None:
    """The target ships in a Lambda zip and must not reach a model adapter."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(chat_target.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imported & {"anthropic", "openai", "httpx2", "agents"}


# --------------------------------------------------------------------------------------
# Failure reporting
# --------------------------------------------------------------------------------------


class _ClosingStream(io.BytesIO):
    """A byte stream that records whether it was closed."""

    closed_by_target = False

    def close(self) -> None:
        self.closed_by_target = True
        super().close()


class _RaisingStream:
    """A stream whose first read raises ``error``; records ``close``."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.closed = False

    def read(self, _size: int) -> bytes:
        raise self.error

    def close(self) -> None:
        self.closed = True


def _log_records(caplog: pytest.LogCaptureFixture) -> list[tuple[str, dict[str, Any]]]:
    return [
        (record.levelname, json.loads(record.getMessage()))
        for record in caplog.records
        if record.name == "gateway.chat_target"
    ]


def test_default_client_read_timeout_is_short_enough_to_report_a_stall() -> None:
    """F1: one stalled read ends as `timeout` before the 660 s Lambda timeout."""
    assert 20 < chat_target.READ_TIMEOUT_SECONDS <= 60
    deadline = chat_target.effective_deadline_seconds(660.0)
    assert deadline + chat_target.READ_TIMEOUT_SECONDS < 660
    assert deadline >= 600
    chat_target._default_runtime_client.cache_clear()
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("AWS_DEFAULT_REGION", "us-east-1")
            client: Any = chat_target._default_runtime_client()
        assert client.meta.config.read_timeout == chat_target.READ_TIMEOUT_SECONDS
        assert client.meta.config.connect_timeout == 10
        assert client.meta.config.retries["total_max_attempts"] == 1
    finally:
        chat_target._default_runtime_client.cache_clear()


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [(None, 640.0), (700.0, 640.0), (660.0, 625.0), (100.0, 65.0), (10.0, 0.0)],
)
def test_effective_deadline_leaves_room_for_one_last_read(
    remaining: float | None, expected: float
) -> None:
    assert chat_target.effective_deadline_seconds(remaining) == expected


def test_lambda_handler_derives_the_deadline_from_the_invocation_context(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_handle(event: object, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {}

    class _Context:
        def get_remaining_time_in_millis(self) -> int:
            return 660_000

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(chat_target, "handle", fake_handle)
    monkeypatch.setattr(chat_target, "_default_config", lambda: CONFIG)
    monkeypatch.setattr(chat_target, "_default_runtime_client", lambda: _Runtime())
    monkeypatch.setattr(chat_target, "_default_matrix", lambda: MATRIX)
    chat_target.lambda_handler({}, _Context())
    assert seen["deadline_seconds"] == 625.0


def test_a_read_timeout_while_streaming_is_timeout_and_closes_the_stream(store: Store) -> None:
    """F1/F7: a stall mid-stream maps to `timeout`, not a raw botocore error."""
    stream = _RaisingStream(ReadTimeoutError(endpoint_url="https://bedrock-agentcore"))
    result = _call(store, _event(), _Runtime(stream=stream))
    _assert_error(result, "timeout")
    assert stream.closed is True


def test_an_unexpected_exception_is_model_failure_logged_at_error(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """F2: nothing escapes as a raw Lambda error."""
    with caplog.at_level("INFO", logger="gateway.chat_target"):
        result = _call(store, _event(), _Runtime(error=RuntimeError("boom")))
    _assert_error(result, "model_failure")
    [(level, record)] = _log_records(caplog)
    assert level == "ERROR"
    assert record["reason"] == "unhandled_RuntimeError"
    assert record["rep_id"] == "rep-a" and record["principal_type"] == "user"
    assert "boom" not in json.dumps(result)


def test_a_key_error_is_not_disguised_as_a_runtime_failure(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """F5: a KeyError is a defect, reported as unhandled rather than a transport failure."""
    with caplog.at_level("INFO", logger="gateway.chat_target"):
        _call(store, _event(), _Runtime(error=KeyError("x")))
    [(level, record)] = _log_records(caplog)
    assert level == "ERROR" and record["reason"] == "unhandled_KeyError"


def test_a_response_without_a_stream_is_model_failure(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = _Runtime()
    runtime.invoke_agent_runtime = lambda **_: {"contentType": "text/event-stream"}  # type: ignore[method-assign]
    with caplog.at_level("INFO", logger="gateway.chat_target"):
        _assert_error(_call(store, _event(), runtime), "model_failure")
    assert _log_records(caplog)[0][1]["reason"] == "no_response_stream"


def test_a_client_error_logs_its_error_code(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    """F4: the reason is the AWS error code, not the exception class name."""
    error = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "InvokeAgentRuntime"
    )
    with caplog.at_level("INFO", logger="gateway.chat_target"):
        _call(store, _event(), _Runtime(error=error))
    [(level, record)] = _log_records(caplog)
    assert level == "ERROR" and record["reason"] == "ThrottlingException"


def test_a_refusal_logs_at_warning_with_the_requested_id_and_caller(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """F3: a failed claim still names the conversation and the caller."""
    unknown = new_conversation_id()
    with caplog.at_level("INFO", logger="gateway.chat_target"):
        _call(store, _event(conversation_id=unknown), _Runtime())
    [(level, record)] = _log_records(caplog)
    assert level == "WARNING"
    assert record == {
        "event": "chat_target_refused",
        "reason": "not_found",
        "session_id": unknown,
        "rep_id": "rep-a",
        "principal_type": "user",
    }


def test_a_settings_notice_is_logged(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    """F6: a saved tuning replaced by defaults is visible in the target's log."""
    identity = decode_identity({IDENTITY_KEY: _identity_payload()})
    saved = TurnTuning(
        source="ui", tiers={"tool": TierRequest(family="openai", model="openai.gpt-6-luna")}
    )
    save_settings(store, identity, saved, matrix=MATRIX)
    shrunk = dataclasses.replace(
        MATRIX, models={k: v for k, v in MATRIX.models.items() if k != "openai.gpt-6-luna"}
    )
    with caplog.at_level("INFO", logger="gateway.chat_target"):
        result = handle(_event(), store=store, runtime=_Runtime(), config=CONFIG, matrix=shrunk)
    assert "answer" in result
    [(level, record)] = _log_records(caplog)
    assert level == "WARNING"
    assert record["event"] == "chat_target_settings_replaced_by_defaults"
    assert record["reason"] == "saved_model_unavailable"


@pytest.mark.parametrize(
    "body",
    [_COMPLETED, _sse(AgentEvent(kind="error", error_kind="model_failure", error_message="x"))],
    ids=["completed", "error-frame"],
)
def test_the_response_stream_is_closed_on_every_path(store: Store, body: bytes) -> None:
    """F7."""
    stream = _ClosingStream(body)
    _call(store, _event(), _Runtime(stream=stream))
    assert stream.closed_by_target is True
