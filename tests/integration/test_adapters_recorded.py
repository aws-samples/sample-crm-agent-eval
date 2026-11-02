# SPDX-License-Identifier: MIT
"""Integration tests: both real adapters replay live-recorded Bedrock wire bytes.

The vendor SDKs run through the real adapters over an ``httpx2.MockTransport`` replaying the
fixtures under ``tests/integration/fixtures/bedrock/``; expected values are read from each fixture's
decoded events. No network call and no real credential is used.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable, Iterator
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.lib.bedrock import _auth
from botocore.credentials import Credentials

from eval.live import is_retryable_error
from providers.anthropic_adapter import AnthropicAdapter
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, Tuning, validate_tuning
from providers.capabilities import CapabilityMatrix, load_capability_matrix
from providers.openai_adapter import OpenAIAdapter
from tests.integration._aws_eventstream import (
    FIXTURES_DIR,
    encode_chunk_event,
    load_wire_fixture,
    split_frames,
    wire_body,
)

_REGION = "us-east-1"
_EVENTSTREAM = "application/vnd.amazon.eventstream"
_SSE = "text/event-stream"

#: Recorded Anthropic fixtures by (tier, kind); the tier selects the model the fixture was recorded
#: against (``config/models.yaml``).
_ANTHROPIC_RECORDED: dict[tuple[str, str], str] = {
    ("supervisor", "success"): "anthropic_invoke_stream_sonnet55_success.json",
    ("supervisor", "tool_use"): "anthropic_invoke_stream_sonnet55_tool_use.json",
    ("subagent", "success"): "anthropic_invoke_stream_sonnet55_success.json",
    ("subagent", "tool_use"): "anthropic_invoke_stream_sonnet55_tool_use.json",
    ("tool", "success"): "anthropic_invoke_stream_haiku55_success.json",
    ("tool", "tool_use"): "anthropic_invoke_stream_haiku55_tool_use.json",
}
_OPENAI_RECORDED: dict[tuple[str, str], str] = {
    ("supervisor", "success"): "openai_responses_sol_success.json",
    ("supervisor", "tool_use"): "openai_responses_sol_tool_use.json",
    ("subagent", "success"): "openai_responses_terra_success.json",
    ("subagent", "tool_use"): "openai_responses_terra_tool_use.json",
    ("tool", "success"): "openai_responses_luna_success.json",
    ("tool", "tool_use"): "openai_responses_luna_tool_use.json",
}
#: Responses recordings whose request is the shipped adapter's body (list ``input`` items and
#: ``prompt_cache_options``); the Luna pair's request is not that body, so it is
#: replayed for its response only.
_OPENAI_CURRENT_SHAPE: tuple[str, ...] = ("supervisor", "subagent")
#: Output-token caps the recordings set to bound their cost; the adapters send none of their own.
_RECORDING_CAPS: frozenset[str] = frozenset({"max_tokens", "max_output_tokens"})
_PROFILES: list[tuple[str, str]] = [
    (family, tier)
    for family in ("anthropic", "openai")
    for tier in ("supervisor", "subagent", "tool")
]

_GET_ACCOUNT_TOOL: dict[str, object] = {
    "name": "get_account",
    "description": "Fetch one CRM account by id.",
    "input_schema": {
        "type": "object",
        "properties": {"account_id": {"type": "string"}},
        "required": ["account_id"],
    },
}
_PROMPT: list[dict[str, object]] = [
    {"role": "user", "content": "Call get_account for account a-1."}
]


class _StaticSession:
    """A boto3-session stand-in resolving fixed fake credentials."""

    region_name = _REGION

    def get_credentials(self) -> Credentials:
        return Credentials("test", "test")


async def _no_sleep(*_: object, **__: object) -> None:
    return None


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[None]:
    """Clear every credential and Bedrock fallback variable; sign with fake keys; no retry sleep."""
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_BEDROCK_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(_auth, "_get_session", lambda **_: _StaticSession())
    monkeypatch.setattr(anthropic.AsyncAnthropicBedrock, "_sleep_for_retry", _no_sleep)
    import openai

    monkeypatch.setattr(openai.AsyncBedrockOpenAI, "_sleep_for_retry", _no_sleep)
    yield


@pytest.fixture(scope="module")
def matrix() -> CapabilityMatrix:
    return load_capability_matrix()


class _Wire:
    """A mock transport replaying responses in order and recording every request it receives."""

    def __init__(self, *steps: httpx2.Response | Callable[[], httpx2.Response]) -> None:
        self.requests: list[httpx2.Request] = []
        self._steps = list(steps)
        self.transport = httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        step = self._steps.pop(0)
        return step() if callable(step) else step


def _raw_fixture(name: str) -> dict[str, Any]:
    """Load a committed fixture that is not a wire fixture (error shapes, constructed streams)."""
    data: dict[str, Any] = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    assert "_provenance" in data, f"{name} is missing its required _provenance field"
    return data


def _fixture_response(name: str) -> httpx2.Response:
    fixture = load_wire_fixture(name)
    return httpx2.Response(
        fixture["response"]["status"],
        headers={"content-type": fixture["response"]["content_type"]},
        content=wire_body(fixture),
    )


def _adapter(family: str, wire: _Wire) -> ModelProvider:
    if family == "anthropic":
        return AnthropicAdapter(
            region_name=_REGION, session=_StaticSession(), transport=wire.transport
        )
    return OpenAIAdapter(region_name=_REGION, session=_StaticSession(), transport=wire.transport)


def _resolve(matrix: CapabilityMatrix, family: str, tier: str, **tuning: Any) -> ResolvedInvocation:
    """Resolve ``tier``'s model with exactly ``tuning`` (unset fields take the model defaults)."""
    model = matrix.model_for(family, tier)  # type: ignore[arg-type]
    return validate_tuning(model, Tuning(source="test", **tuning))


def _run(
    adapter: ModelProvider,
    invocation: ResolvedInvocation,
    messages: list[dict[str, object]] | None = None,
    tools: list[dict[str, object]] | None = None,
) -> list[ProviderEvent]:
    async def collect() -> list[ProviderEvent]:
        stream = adapter.stream(invocation, messages or _PROMPT, tools or [])
        return [event async for event in stream]

    return asyncio.run(collect())


def _anthropic_expected(name: str) -> dict[str, Any]:
    """Read the text, tool call, usage and stop reason a recorded Anthropic fixture carries."""
    events = [frame["event"] for frame in load_wire_fixture(name)["decoded_events"]]
    text = "".join(
        e["delta"]["text"]
        for e in events
        if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta"
    )
    tool_starts = [
        e["content_block"]
        for e in events
        if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use"
    ]
    arguments = "".join(
        e["delta"]["partial_json"]
        for e in events
        if e["type"] == "content_block_delta" and e["delta"]["type"] == "input_json_delta"
    )
    start_usage = next(e for e in events if e["type"] == "message_start")["message"]["usage"]
    delta = next(e for e in events if e["type"] == "message_delta")
    return {
        "text": text,
        "tools": [(t["name"], t["id"], json.loads(arguments)) for t in tool_starts],
        "input_tokens": start_usage["input_tokens"],
        "output_tokens": delta["usage"]["output_tokens"],
        "cache_read": start_usage.get("cache_read_input_tokens") or 0,
        "cache_write": start_usage.get("cache_creation_input_tokens") or 0,
        "stop_reason": delta["delta"]["stop_reason"],
    }


def _openai_expected(name: str) -> dict[str, Any]:
    """Read the text, function calls and usage a recorded Responses fixture carries."""
    events = [e for e in load_wire_fixture(name)["decoded_events"] if e != "[DONE]"]
    completed = next(e for e in events if e["type"] == "response.completed")["response"]
    calls = [
        e["item"]
        for e in events
        if e["type"] == "response.output_item.done" and e["item"]["type"] == "function_call"
    ]
    return {
        "text": "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta"),
        "tools": [(c["name"], c["call_id"], json.loads(c["arguments"])) for c in calls],
        "usage": completed["usage"],
    }


# --------------------------------------------------------------------------------------
# Structural guards: this module makes no live call and reads no credential.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_no_test_in_this_module_touches_the_network(
    family: str, matrix: CapabilityMatrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _forbidden_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test module must never open a network connection")

    monkeypatch.setattr(socket, "create_connection", _forbidden_connect)
    monkeypatch.setattr(socket.socket, "connect", _forbidden_connect)
    name = (
        _ANTHROPIC_RECORDED[("supervisor", "success")]
        if family == "anthropic"
        else _OPENAI_RECORDED[("supervisor", "success")]
    )
    wire = _Wire(_fixture_response(name))

    events = _run(_adapter(family, wire), _resolve(matrix, family, "supervisor"))

    assert events[-1].type == "completed"
    assert len(wire.requests) == 1


# --------------------------------------------------------------------------------------
# Recorded happy paths through the real SDK decoders.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("tier", "kind"), sorted(_ANTHROPIC_RECORDED))
def test_a_recorded_anthropic_stream_normalises_to_its_fixture_content(
    tier: str, kind: str, matrix: CapabilityMatrix
) -> None:
    name = _ANTHROPIC_RECORDED[(tier, kind)]
    expected = _anthropic_expected(name)
    wire = _Wire(_fixture_response(name))

    events = _run(
        _adapter("anthropic", wire), _resolve(matrix, "anthropic", tier), tools=[_GET_ACCOUNT_TOOL]
    )

    assert "".join(e.text or "" for e in events if e.type == "text_delta") == expected["text"]
    tool_calls = [(e.tool_name, e.tool_id, e.tool_input) for e in events if e.type == "tool_call"]
    assert tool_calls == expected["tools"]
    assert bool(tool_calls) is (kind == "tool_use")
    usage = events[-2]
    assert usage.type == "usage"
    assert (usage.input_tokens, usage.output_tokens) == (
        expected["input_tokens"],
        expected["output_tokens"],
    )
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (
        expected["cache_read"],
        expected["cache_write"],
    )
    assert usage.retried_attempts == ()
    assert events[-1].type == "completed"
    assert events[-1].awaiting_tools is (expected["stop_reason"] == "tool_use")


@pytest.mark.parametrize(("tier", "kind"), sorted(_ANTHROPIC_RECORDED))
def test_the_anthropic_request_matches_the_recorded_live_request(
    tier: str, kind: str, matrix: CapabilityMatrix
) -> None:
    """The adapter, given the recording's effort, sends the recorded live request's shape."""
    fixture = load_wire_fixture(_ANTHROPIC_RECORDED[(tier, kind)])
    recorded = fixture["request"]["body"]
    model = matrix.model_for("anthropic", tier)  # type: ignore[arg-type]
    wire = _Wire(_fixture_response(_ANTHROPIC_RECORDED[(tier, kind)]))

    _run(
        _adapter("anthropic", wire),
        _resolve(matrix, "anthropic", tier, effort=recorded["output_config"]["effort"]),
        tools=[_GET_ACCOUNT_TOOL] if kind == "tool_use" else None,
    )

    (request,) = wire.requests
    sent = json.loads(request.content)
    expected_keys = set(recorded) - _RECORDING_CAPS
    assert request.url.path == fixture["request"]["path"]
    assert request.url.path == f"/model/{model.inference_profile}/invoke-with-response-stream"
    for key in ("thinking", "output_config", "anthropic_version", "tools"):
        assert sent.get(key) == recorded.get(key), key
    assert not {"temperature", "top_p", "top_k", "extra_body"} & set(sent)
    assert set(sent) - {"system", "max_tokens", "stream"} == expected_keys


@pytest.mark.parametrize(("tier", "kind"), sorted(_OPENAI_RECORDED))
def test_a_recorded_responses_stream_normalises_to_its_fixture_content(
    tier: str, kind: str, matrix: CapabilityMatrix
) -> None:
    name = _OPENAI_RECORDED[(tier, kind)]
    expected = _openai_expected(name)
    wire = _Wire(_fixture_response(name))

    events = _run(
        _adapter("openai", wire), _resolve(matrix, "openai", tier), tools=[_GET_ACCOUNT_TOOL]
    )

    assert "".join(e.text or "" for e in events if e.type == "text_delta") == expected["text"]
    tool_calls = [(e.tool_name, e.tool_id, e.tool_input) for e in events if e.type == "tool_call"]
    assert tool_calls == expected["tools"]
    assert bool(tool_calls) is (kind == "tool_use")
    usage = events[-2]
    assert usage.type == "usage"
    assert (usage.input_tokens, usage.output_tokens) == (
        expected["usage"]["input_tokens"],
        expected["usage"]["output_tokens"],
    )
    details = expected["usage"]["input_tokens_details"]
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (
        details["cached_tokens"],
        details["cache_write_tokens"],
    )
    assert events[-1].type == "completed"
    assert events[-1].awaiting_tools is (kind == "tool_use")


@pytest.mark.parametrize(
    ("tier", "kind"), sorted(k for k in _OPENAI_RECORDED if k[0] in _OPENAI_CURRENT_SHAPE)
)
def test_the_responses_request_matches_the_recorded_live_request(
    tier: str, kind: str, matrix: CapabilityMatrix
) -> None:
    """The adapter, given the recording's effort, sends the recorded live body, less its cap."""
    fixture = load_wire_fixture(_OPENAI_RECORDED[(tier, kind)])
    recorded = fixture["request"]["body"]
    wire = _Wire(_fixture_response(_OPENAI_RECORDED[(tier, kind)]))

    _run(
        _adapter("openai", wire),
        _resolve(matrix, "openai", tier, effort=recorded["reasoning"]["effort"]),
        messages=[{"role": "user", "content": recorded["input"][0]["content"]}],
        tools=[_GET_ACCOUNT_TOOL] if kind == "tool_use" else None,
    )

    (request,) = wire.requests
    sent = json.loads(request.content)
    assert request.url.path == fixture["request"]["path"]
    assert recorded["model"] == matrix.model_for("openai", tier).inference_profile  # type: ignore[arg-type]
    assert sent == {k: v for k, v in recorded.items() if k not in _RECORDING_CAPS}
    assert not {"temperature", "top_p"} & set(sent)


def test_only_the_luna_pair_predates_the_shipped_responses_body() -> None:
    """The request-shape test skips exactly the tiers whose recording is not the current body."""
    for (tier, _kind), name in _OPENAI_RECORDED.items():
        body = load_wire_fixture(name)["request"]["body"]
        current = isinstance(body["input"], list) and "prompt_cache_options" in body
        assert current is (tier in _OPENAI_CURRENT_SHAPE), name


def test_the_anthropic_recordings_differ_from_the_shipped_body_only_by_their_cap() -> None:
    """Every Anthropic recording's body is the shipped shape plus the recording's ``max_tokens``."""
    for name in _ANTHROPIC_RECORDED.values():
        body = load_wire_fixture(name)["request"]["body"]
        assert body["max_tokens"] == 256, name
        assert "temperature" not in body, name


# --------------------------------------------------------------------------------------
# Cross-family normalised-stream equivalence.
# --------------------------------------------------------------------------------------


def test_every_recorded_stream_emits_the_same_closed_event_vocabulary_and_ordering(
    matrix: CapabilityMatrix,
) -> None:
    """Every recorded stream normalises to one closed vocabulary with usage before completed."""
    allowed = {"text_delta", "tool_call", "tool_result", "usage", "error", "completed"}
    runs: list[list[ProviderEvent]] = []
    for (tier, _kind), name in sorted(_ANTHROPIC_RECORDED.items()):
        runs.append(
            _run(
                _adapter("anthropic", _Wire(_fixture_response(name))),
                _resolve(matrix, "anthropic", tier),
            )
        )
    for (tier, _kind), name in sorted(_OPENAI_RECORDED.items()):
        runs.append(
            _run(
                _adapter("openai", _Wire(_fixture_response(name))),
                _resolve(matrix, "openai", tier),
            )
        )

    for events in runs:
        assert {str(e.type) for e in events} <= allowed
        assert [e.type for e in events][-2:] == ["usage", "completed"]
        for event in events:
            if event.type == "text_delta":
                assert isinstance(event.text, str)
            elif event.type == "tool_call":
                assert isinstance(event.tool_name, str) and isinstance(event.tool_input, dict)
            elif event.type in ("usage", "completed"):
                assert isinstance(event.input_tokens, int) and isinstance(event.output_tokens, int)


# --------------------------------------------------------------------------------------
# Transport failures on every profile: never a raised exception, never the provider's text.
# --------------------------------------------------------------------------------------


def _error_body(name: str) -> httpx2.Response:
    """A Bedrock error response built from a committed error fixture's status, type and message."""
    fixture = _raw_fixture(name)
    return httpx2.Response(
        fixture["http_status_code"],
        headers={"x-amzn-errortype": f"{fixture['error_code']}:http://internal.amazon.com/"},
        json={"message": fixture["message"]},
    )


@pytest.mark.parametrize(("family", "tier"), _PROFILES)
def test_an_exhausted_throttle_is_one_non_retryable_error_carrying_the_retried_attempts(
    family: str, tier: str, matrix: CapabilityMatrix
) -> None:
    wire = _Wire(*(lambda: _error_body("throttling_error.json") for _ in range(3)))

    events = _run(_adapter(family, wire), _resolve(matrix, family, tier))

    assert [e.type for e in events] == ["error"]
    message = events[0].error_message or ""
    assert message.endswith("APIStatusError(status=429).")
    assert "Too many requests" not in message and "ThrottlingException" not in message
    assert not is_retryable_error(message)
    assert len(wire.requests) == 3
    assert [a.status for a in events[0].retried_attempts] == [429, 429]


@pytest.mark.parametrize(("family", "tier"), _PROFILES)
def test_a_validation_error_aborts_on_the_first_attempt_without_the_provider_text(
    family: str, tier: str, matrix: CapabilityMatrix
) -> None:
    wire = _Wire(_error_body("validation_error.json"))

    events = _run(_adapter(family, wire), _resolve(matrix, family, tier))

    assert [e.type for e in events] == ["error"]
    message = events[0].error_message or ""
    assert message.endswith("APIStatusError(status=400).")
    assert "output_config.effort" not in message
    assert not is_retryable_error(message)
    assert len(wire.requests) == 1
    assert events[0].retried_attempts == ()


# --------------------------------------------------------------------------------------
# In-band failures: retryable, with any text already streamed preserved.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        ("anthropic_invoke_stream_synth_model_stream_error.json", "ModelStreamErrorException: "),
        (
            "anthropic_invoke_stream_synth_internal_server_exception.json",
            "InternalServerException: ",
        ),
    ],
)
def test_an_in_band_bedrock_exception_frame_is_one_retryable_error_after_the_text(
    name: str, prefix: str, matrix: CapabilityMatrix
) -> None:
    fixture = load_wire_fixture(name)
    wire = _Wire(_fixture_response(name))

    events = _run(_adapter("anthropic", wire), _resolve(matrix, "anthropic", "supervisor"))

    streamed = "".join(
        f["event"]["delta"]["text"]
        for f in fixture["decoded_events"]
        if "event" in f and f["event"]["type"] == "content_block_delta"
    )
    assert "".join(e.text or "" for e in events if e.type == "text_delta") == streamed
    assert [e.type for e in events if e.type != "text_delta"] == ["error"]
    message = events[-1].error_message or ""
    assert message == prefix + fixture["decoded_events"][-1]["payload"]["message"]
    assert is_retryable_error(message)
    assert len(wire.requests) == 1


@pytest.mark.parametrize(
    "name",
    [
        "openai_responses_luna_live_server_error.json",
        "openai_responses_luna_synth_response_failed.json",
    ],
)
def test_an_in_band_responses_failure_is_one_retryable_server_error(
    name: str, matrix: CapabilityMatrix
) -> None:
    wire = _Wire(_fixture_response(name))

    events = _run(_adapter("openai", wire), _resolve(matrix, "openai", "tool"))

    assert [e.type for e in events] == ["error"]
    assert (events[0].error_message or "").startswith("server_error: ")
    assert is_retryable_error(events[0].error_message or "")
    assert len(wire.requests) == 1


# --------------------------------------------------------------------------------------
# Truncated and malformed streams.
# --------------------------------------------------------------------------------------


def test_defect_a_truncated_messages_stream_is_reported_as_a_normal_completion(
    matrix: CapabilityMatrix,
) -> None:
    """Pins current behaviour: a truncated Messages stream reads as a normal completion.

    A Messages stream that ends after a tool_use block's ``input_json_delta`` but before
    ``content_block_stop`` / ``message_delta`` / ``message_stop`` yields no ``error`` event and no
    ``tool_call``; it ends in a normal ``usage`` / ``completed`` pair. The body is the recorded
    supervisor-tier tool-use stream cut after its first ``input_json_delta`` frame.
    """
    fixture = load_wire_fixture(_ANTHROPIC_RECORDED[("supervisor", "tool_use")])
    frames = split_frames(wire_body(fixture))
    kinds = [
        f["event"]["delta"]["type"]
        if f["event"]["type"] == "content_block_delta"
        else f["event"]["type"]
        for f in fixture["decoded_events"]
    ]
    cut = kinds.index("input_json_delta") + 1
    wire = _Wire(
        httpx2.Response(200, headers={"content-type": _EVENTSTREAM}, content=b"".join(frames[:cut]))
    )

    events = _run(_adapter("anthropic", wire), _resolve(matrix, "anthropic", "supervisor"))

    assert not any(e.type in ("error", "tool_call") for e in events), (
        "if this fails the defect was fixed upstream; update this test and its docstring"
    )
    assert events[-1].type == "completed"


def test_defect_a_truncated_responses_stream_is_reported_as_a_normal_completion(
    matrix: CapabilityMatrix,
) -> None:
    """Pins current behaviour: a truncated Responses stream reads as a normal completion.

    The recorded Luna success stream with ``response.completed`` and ``[DONE]`` removed still ends
    in ``usage`` / ``completed``, with zero token counts.
    """
    fixture = load_wire_fixture(_OPENAI_RECORDED[("tool", "success")])
    kept = [
        e for e in fixture["decoded_events"] if e != "[DONE]" and e["type"] != "response.completed"
    ]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in kept).encode()
    wire = _Wire(httpx2.Response(200, headers={"content-type": _SSE}, content=body))

    events = _run(_adapter("openai", wire), _resolve(matrix, "openai", "tool"))

    assert not any(e.type == "error" for e in events), (
        "if this fails the defect was fixed upstream; update this test and its docstring"
    )
    assert [e.type for e in events][-2:] == ["usage", "completed"]
    assert (events[-2].input_tokens, events[-2].output_tokens) == (0, 0)


def test_a_malformed_function_call_arguments_string_is_one_error_without_the_payload(
    matrix: CapabilityMatrix,
) -> None:
    """``openai_responses_malformed_stream.json``'s truncated arguments become one ``error`` event."""
    fixture = _raw_fixture("openai_responses_malformed_stream.json")
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in fixture["events"]).encode()
    wire = _Wire(httpx2.Response(200, headers={"content-type": _SSE}, content=body))

    events = _run(_adapter("openai", wire), _resolve(matrix, "openai", "tool", effort="none"))

    assert [e.type for e in events] == ["error"]
    assert events[0].error_message == "The model provider request failed: JSONDecodeError."


def test_a_constructed_messages_stream_round_trips_through_the_encoder_and_adapter(
    matrix: CapabilityMatrix,
) -> None:
    """``anthropic_messages_truncated_stream.json``'s events, encoded as frames, replay as the
    same silent completion as the recorded cut, so both truncation sources agree."""
    fixture = _raw_fixture("anthropic_messages_truncated_stream.json")
    body = b"".join(encode_chunk_event(e) for e in fixture["events"])
    wire = _Wire(httpx2.Response(200, headers={"content-type": _EVENTSTREAM}, content=body))

    events = _run(_adapter("anthropic", wire), _resolve(matrix, "anthropic", "subagent"))

    assert not any(e.type in ("error", "tool_call") for e in events)
    assert events[-1].type == "completed"


def test_anthropic_cache_counts_are_read_from_message_start(matrix: CapabilityMatrix) -> None:
    """The recorded streams all carry zero cache tokens, so this re-encodes the recorded
    supervisor-tier ``message_start`` frame with non-zero cache counts and keeps every other
    recorded frame."""
    fixture = load_wire_fixture(_ANTHROPIC_RECORDED[("supervisor", "success")])
    frames = split_frames(wire_body(fixture))
    start = json.loads(json.dumps(fixture["decoded_events"][0]["event"]))
    start["message"]["usage"].update(cache_read_input_tokens=256, cache_creation_input_tokens=128)
    body = encode_chunk_event(start) + b"".join(frames[1:])
    wire = _Wire(httpx2.Response(200, headers={"content-type": _EVENTSTREAM}, content=body))

    events = _run(_adapter("anthropic", wire), _resolve(matrix, "anthropic", "supervisor"))

    usage = events[-2]
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (256, 128)
    assert usage.input_tokens == start["message"]["usage"]["input_tokens"]
