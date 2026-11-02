# SPDX-License-Identifier: MIT
"""Tests for the OpenAI adapter on ``AsyncBedrockOpenAI``.

Every test drives the real ``openai`` SDK — request building, SigV4 signing with fake static
credentials, its retry loop, and its SSE decoding — over an ``httpx2.MockTransport`` injected through
``OpenAIAdapter(transport=…)``. Response bodies are the live-recorded and synthesized wire fixtures
under ``tests/integration/fixtures/bedrock/`` or small inline SSE bodies. No network, no real
credential, no model call.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx2
import pytest
from botocore.credentials import Credentials

from eval.live import is_retryable_error
from providers import prompt_caching
from providers.base import (
    ProviderEvent,
    ResolvedInvocation,
    RetriedAttempt,
    Tuning,
    validate_tuning,
)
from providers.capabilities import CapabilityMatrix, load_capability_matrix
from providers.openai_adapter import OpenAIAdapter, provider_error_message
from providers.sdk_transport import CredentialPolicyError
from tests.integration._aws_eventstream import load_wire_fixture, wire_body

Handler = Callable[[httpx2.Request], httpx2.Response]

_REGION = "us-east-1"
_RESPONSES_URL = f"https://bedrock-runtime.{_REGION}.amazonaws.com/openai/v1/responses"
_SSE = {"content-type": "text/event-stream"}


class _StaticSession:
    """A boto3-session stand-in resolving fixed fake credentials."""

    def get_credentials(self) -> Credentials:
        return Credentials("test", "test")


class _Clock:
    """A manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _offline_aws_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[None]:
    for name in (
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_BEDROCK_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    yield


@pytest.fixture(scope="module")
def matrix() -> CapabilityMatrix:
    return load_capability_matrix()


async def _no_sleep(*_: object, **__: object) -> None:
    return None


def _adapter(handler: Handler, clock: Callable[[], float] = time.monotonic) -> OpenAIAdapter:
    adapter = OpenAIAdapter(
        region_name=_REGION,
        session=_StaticSession(),
        transport=httpx2.MockTransport(handler),
        clock=clock,
    )
    adapter._sdk._sleep_for_retry = _no_sleep  # type: ignore[method-assign]
    return adapter


def _resolve(matrix: CapabilityMatrix, tier: str, **tuning: Any) -> ResolvedInvocation:
    model = matrix.model_for("openai", tier)  # type: ignore[arg-type]
    merged = {**matrix.tiers["openai"][tier].tuning, **tuning}  # type: ignore[index]
    return validate_tuning(model, Tuning(source="test", **merged))


def _run(
    adapter: OpenAIAdapter,
    invocation: ResolvedInvocation,
    messages: list[dict[str, object]] | None = None,
    tools: list[dict[str, object]] | None = None,
) -> list[ProviderEvent]:
    async def collect() -> list[ProviderEvent]:
        stream = adapter.stream(
            invocation, messages or [{"role": "user", "content": "hi"}], tools or []
        )
        return [event async for event in stream]

    return asyncio.run(collect())


def _sse(events: list[dict[str, Any]]) -> bytes:
    frames = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    return (frames + "data: [DONE]\n\n").encode("utf-8")


def _completed(
    input_tokens: int = 5, output_tokens: int = 2, cached: int = 0, cache_write: int = 0
) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "sequence_number": 9,
        "response": {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "output": [],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "input_tokens_details": {
                    "cached_tokens": cached,
                    "cache_write_tokens": cache_write,
                },
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    }


def _text(delta: str) -> dict[str, Any]:
    return {
        "type": "response.output_text.delta",
        "sequence_number": 1,
        "item_id": "msg_1",
        "output_index": 0,
        "content_index": 0,
        "delta": delta,
    }


def _replying(body: bytes, seen: list[httpx2.Request] | None = None) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return httpx2.Response(200, headers=_SSE, content=body)

    return handler


def _sequence(*steps: httpx2.Response | Exception) -> tuple[Handler, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []
    queue = list(steps)

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        step = queue.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    return handler, seen


def _fixture_response(name: str) -> httpx2.Response:
    fixture = load_wire_fixture(name)
    return httpx2.Response(
        fixture["response"]["status"],
        headers={"content-type": fixture["response"]["content_type"]},
        content=wire_body(fixture),
    )


def _body(request: httpx2.Request) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(request.content)
    return parsed


def _single_error(events: list[ProviderEvent]) -> str:
    assert [e.type for e in events] == ["error"], [e.type for e in events]
    message = events[0].error_message
    assert message is not None
    return message


_GET_ACCOUNT_TOOL: dict[str, object] = {
    "name": "get_account",
    "description": "Fetch one CRM account by id.",
    "input_schema": {
        "type": "object",
        "properties": {"account_id": {"type": "string"}},
        "required": ["account_id"],
    },
}


# --- request shape -------------------------------------------------------------------------------


def test_the_supervisor_request_goes_to_bedrock_runtime_responses_with_reasoning_effort(
    matrix: CapabilityMatrix,
) -> None:
    seen: list[httpx2.Request] = []
    adapter = _adapter(_replying(_sse([_text("Sure"), _completed()]), seen))

    _run(adapter, _resolve(matrix, "supervisor", effort="max"))

    (request,) = seen
    assert str(request.url) == _RESPONSES_URL
    assert request.method == "POST"
    assert _body(request) == {
        "model": "global.openai.gpt-6.1-sol",
        "input": [{"role": "user", "content": "hi"}],
        "stream": True,
        "reasoning": {"effort": "max"},
        "prompt_cache_options": {"mode": "implicit", "ttl": "30m"},
    }


def test_the_request_is_sigv4_signed_for_bedrock_and_never_signs_connection(
    matrix: CapabilityMatrix,
) -> None:
    seen: list[httpx2.Request] = []
    adapter = _adapter(_replying(_sse([_completed()]), seen))

    _run(adapter, _resolve(matrix, "tool"))

    authorization = seen[0].headers["authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=test/")
    assert f"/{_REGION}/bedrock/aws4_request" in authorization
    signed = authorization.split("SignedHeaders=")[1].split(",")[0].split(";")
    assert "connection" not in signed
    assert "connection" not in seen[0].headers


def test_the_region_selects_the_regional_runtime_endpoint(matrix: CapabilityMatrix) -> None:
    seen: list[httpx2.Request] = []
    adapter = OpenAIAdapter(
        region_name="eu-west-1",
        session=_StaticSession(),
        transport=httpx2.MockTransport(_replying(_sse([_completed()]), seen)),
    )

    _run(adapter, _resolve(matrix, "tool"))

    assert seen[0].url.host == "bedrock-runtime.eu-west-1.amazonaws.com"
    assert seen[0].url.path == "/openai/v1/responses"


def test_the_body_matches_the_recorded_live_request_shape(matrix: CapabilityMatrix) -> None:
    recorded = load_wire_fixture("openai_responses_luna_tool_use.json")["request"]["body"]
    seen: list[httpx2.Request] = []
    adapter = _adapter(_replying(_sse([_completed()]), seen))

    _run(
        adapter,
        _resolve(matrix, "tool", effort="low"),
        [{"role": "user", "content": recorded["input"]}],
        [_GET_ACCOUNT_TOOL],
    )

    body = _body(seen[0])
    assert body["model"] == recorded["model"]
    assert body["reasoning"] == recorded["reasoning"]
    assert body["stream"] is recorded["stream"] is True
    assert body["tools"] == recorded["tools"]
    assert body["input"] == [{"role": "user", "content": recorded["input"]}]
    assert body["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
    assert set(body) - {"max_output_tokens", "prompt_cache_options"} == set(recorded) - {
        "max_output_tokens"
    }


def test_no_effort_value_sends_a_sampling_parameter(matrix: CapabilityMatrix) -> None:
    """Every effort the tool model accepts, ``none`` included, is sent with no sampling key."""
    values = matrix.model_for("openai", "tool").effort.values
    assert "none" in values
    for effort in values:
        seen: list[httpx2.Request] = []
        adapter = _adapter(_replying(_sse([_completed()]), seen))

        _run(adapter, _resolve(matrix, "tool", effort=effort))

        body = _body(seen[0])
        assert body["reasoning"] == {"effort": effort}
        assert not {"temperature", "top_p"} & body.keys(), effort


def test_a_default_tier_request_carries_no_sampling_parameters(matrix: CapabilityMatrix) -> None:
    seen: list[httpx2.Request] = []
    adapter = _adapter(_replying(_sse([_completed()]), seen))

    _run(adapter, _resolve(matrix, "subagent"))

    body = _body(seen[0])
    assert body["reasoning"] == {"effort": "low"}
    assert not {"temperature", "top_p"} & body.keys()


def test_an_anthropic_invocation_is_refused_before_any_request(matrix: CapabilityMatrix) -> None:
    seen: list[httpx2.Request] = []
    adapter = _adapter(_replying(_sse([_completed()]), seen))
    anthropic = ResolvedInvocation(model=matrix.model_for("anthropic", "supervisor"), effort=None)

    with pytest.raises(ValueError, match="openai adapter received a anthropic invocation"):
        _run(adapter, anthropic)
    assert seen == []


def test_a_tool_round_serialises_function_call_and_output_items(matrix: CapabilityMatrix) -> None:
    seen: list[httpx2.Request] = []
    adapter = _adapter(_replying(_sse([_completed()]), seen))
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "Tell me about Acme."},
        {
            "role": "assistant",
            "content": "Looking it up.",
            "tool_calls": [{"id": "call_1", "name": "get_account", "input": {"account_id": "a-1"}}],
        },
        {"role": "tool", "tool_results": [{"id": "call_1", "output": '{"name": "Acme"}'}]},
    ]

    _run(adapter, _resolve(matrix, "tool"), messages)

    assert _body(seen[0])["input"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "Tell me about Acme."},
        {"role": "assistant", "content": "Looking it up."},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "get_account",
            "arguments": '{"account_id": "a-1"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": '{"name": "Acme"}'},
    ]


# --- lock-down -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AWS_BEARER_TOKEN_BEDROCK", ""),
        ("AWS_BEDROCK_BASE_URL", "https://bedrock-mantle.us-east-1.api.aws/v1"),
    ],
    ids=["bearer-empty", "mantle-base-url"],
)
def test_a_bearer_or_base_url_fallback_refuses_construction(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)
    seen: list[httpx2.Request] = []

    with pytest.raises(CredentialPolicyError):
        _adapter(_replying(_sse([_completed()]), seen))
    assert seen == []


def test_the_built_client_resolves_to_bedrock_runtime_with_no_api_key() -> None:
    adapter = _adapter(_replying(b""))

    assert str(adapter._sdk.base_url).rstrip("/") == (
        f"https://bedrock-runtime.{_REGION}.amazonaws.com/openai/v1"
    )
    assert not getattr(adapter._sdk, "api_key", None)


# --- recorded live fixtures through the SDK -------------------------------------------------------


def test_the_recorded_luna_success_normalises_to_text_usage_and_completed(
    matrix: CapabilityMatrix,
) -> None:
    fixture = load_wire_fixture("openai_responses_luna_success.json")
    completed = next(
        e for e in fixture["decoded_events"] if e != "[DONE]" and e["type"] == "response.completed"
    )
    usage = completed["response"]["usage"]
    adapter = _adapter(lambda _r: _fixture_response("openai_responses_luna_success.json"))

    events = _run(adapter, _resolve(matrix, "tool"))

    assert [e.type for e in events][-2:] == ["usage", "completed"]
    text = "".join(e.text or "" for e in events if e.type == "text_delta")
    assert text == completed["response"]["output"][0]["content"][0]["text"]
    usage_event = events[-2]
    assert usage_event.input_tokens == usage["input_tokens"]
    assert usage_event.output_tokens == usage["output_tokens"]
    assert usage_event.cache_read_tokens == usage["input_tokens_details"]["cached_tokens"]
    assert usage_event.cache_write_tokens == usage["input_tokens_details"]["cache_write_tokens"]
    assert usage_event.retried_attempts == ()
    assert events[-1].awaiting_tools is False


def test_the_recorded_luna_tool_use_normalises_to_one_tool_call_awaiting_tools(
    matrix: CapabilityMatrix,
) -> None:
    adapter = _adapter(lambda _r: _fixture_response("openai_responses_luna_tool_use.json"))

    events = _run(adapter, _resolve(matrix, "tool"), tools=[_GET_ACCOUNT_TOOL])

    tool_calls = [e for e in events if e.type == "tool_call"]
    assert len(tool_calls) == 1
    call = tool_calls[0]
    assert call.tool_name == "get_account"
    assert call.tool_input == {"account_id": "a-1"}
    assert call.tool_id is not None and call.tool_id.startswith("call_")
    assert [e.type for e in events][-2:] == ["usage", "completed"]
    assert events[-1].awaiting_tools is True


# --- in-band failures: retryable server_error -----------------------------------------------------


def test_the_live_in_band_server_error_becomes_one_retryable_server_error_event(
    matrix: CapabilityMatrix,
) -> None:
    adapter = _adapter(lambda _r: _fixture_response("openai_responses_luna_live_server_error.json"))

    message = _single_error(_run(adapter, _resolve(matrix, "tool")))

    assert message.startswith("server_error: ")
    assert is_retryable_error(message)


def test_a_yielded_bare_response_failed_becomes_one_retryable_server_error_event(
    matrix: CapabilityMatrix,
) -> None:
    adapter = _adapter(
        lambda _r: _fixture_response("openai_responses_luna_synth_response_failed.json")
    )

    message = _single_error(_run(adapter, _resolve(matrix, "tool")))

    assert message == "server_error: The server had an error while processing your request."
    assert is_retryable_error(message)


def test_an_in_band_server_error_is_not_retried_by_the_sdk(matrix: CapabilityMatrix) -> None:
    handler, seen = _sequence(_fixture_response("openai_responses_luna_live_server_error.json"))
    adapter = _adapter(handler)

    _single_error(_run(adapter, _resolve(matrix, "tool")))

    assert len(seen) == 1


# --- SDK-exhausted transport failures: never retryable -------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
def test_an_exhausted_retryable_status_is_rendered_non_retryable(
    status: int, matrix: CapabilityMatrix
) -> None:
    responses = [
        httpx2.Response(
            status, json={"error": {"code": "server_error", "message": "InternalServer"}}
        )
        for _ in range(3)
    ]
    handler, seen = _sequence(*responses)
    adapter = _adapter(handler)

    message = _single_error(_run(adapter, _resolve(matrix, "tool")))

    assert len(seen) == 3, "the SDK retries a retryable status twice before giving up"
    assert message == (
        f"The model provider request failed after SDK retries: APIStatusError(status={status})."
    )
    assert not is_retryable_error(message)


@pytest.mark.parametrize("status", [400, 403])
def test_a_client_fault_aborts_on_the_first_attempt(status: int, matrix: CapabilityMatrix) -> None:
    handler, seen = _sequence(httpx2.Response(status, json={"error": {"message": "secret body"}}))
    adapter = _adapter(handler)

    events = _run(adapter, _resolve(matrix, "tool"))
    message = _single_error(events)

    assert len(seen) == 1
    assert events[0].retried_attempts == ()
    assert f"status={status}" in message and "secret body" not in message
    assert not is_retryable_error(message)


def test_an_exhausted_connection_error_is_rendered_by_type_and_non_retryable(
    matrix: CapabilityMatrix,
) -> None:
    handler, seen = _sequence(*(httpx2.ConnectError("refused") for _ in range(3)))
    adapter = _adapter(handler)

    message = _single_error(_run(adapter, _resolve(matrix, "tool")))

    assert len(seen) == 3
    assert message == "The model provider request failed after SDK retries: APIConnectionError."
    assert not is_retryable_error(message)


def test_the_error_renderer_discriminates_in_band_from_exhausted_status_errors() -> None:
    import openai

    request = httpx2.Request("POST", _RESPONSES_URL)
    in_band = openai.APIError(
        "upstream", request, body={"code": "server_error", "type": "server_error"}
    )
    exhausted = openai.InternalServerError(
        "boom", response=httpx2.Response(500, request=request), body={"code": "server_error"}
    )

    assert provider_error_message(in_band) == "server_error: upstream"
    assert is_retryable_error(provider_error_message(in_band))
    assert "InternalServer" not in provider_error_message(exhausted)
    assert not is_retryable_error(provider_error_message(exhausted))


# --- retried attempts and latency -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "expected"),
    [
        (httpx2.Response(503), RetriedAttempt(503, None)),
        (httpx2.Response(429), RetriedAttempt(429, None)),
        (httpx2.ConnectError("refused"), RetriedAttempt(None, "ConnectError")),
    ],
    ids=["503", "429", "connect-error"],
)
def test_a_retried_attempt_is_reported_on_the_usage_event(
    first: httpx2.Response | Exception, expected: RetriedAttempt, matrix: CapabilityMatrix
) -> None:
    handler, seen = _sequence(first, _fixture_response("openai_responses_luna_success.json"))
    adapter = _adapter(handler)

    events = _run(adapter, _resolve(matrix, "tool"))

    assert len(seen) == 2
    assert [h.headers["x-stainless-retry-count"] for h in seen] == ["0", "1"]
    usage = next(e for e in events if e.type == "usage")
    assert usage.retried_attempts == (expected,)
    assert events[-1].type == "completed"


class _TickingBody(httpx2.AsyncByteStream):
    """An SSE body that advances ``clock`` by one second per chunk as it is read.

    Single-pass like a socket: a second iteration (the SDK's post-``[DONE]`` drain) yields nothing.
    """

    def __init__(self, chunks: list[bytes], clock: _Clock) -> None:
        self._chunks = chunks
        self._clock = clock
        self._read = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._read:
            return
        self._read = True
        for chunk in self._chunks:
            self._clock.now += 1.0
            yield chunk


def test_latency_runs_from_the_successful_attempt_to_the_end_of_the_stream(
    matrix: CapabilityMatrix,
) -> None:
    clock = _Clock()
    chunks = [f"data: {json.dumps(e)}\n\n".encode() for e in [_text("a"), _text("b"), _completed()]]
    chunks.append(b"data: [DONE]\n\n")
    attempts = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            clock.now += 10.0
            return httpx2.Response(503)
        clock.now += 0.5
        return httpx2.Response(200, headers=_SSE, stream=_TickingBody(chunks, clock))

    adapter = _adapter(handler, clock=clock)

    events = _run(adapter, _resolve(matrix, "tool"))

    usage = next(e for e in events if e.type == "usage")
    assert usage.retried_attempts == (RetriedAttempt(503, None),)
    assert usage.latency_ms == (0.5 + len(chunks)) * 1000.0


# --- malformed and unrecognisable streams -----------------------------------------------------------


def test_malformed_tool_arguments_end_in_an_error_without_the_payload(
    matrix: CapabilityMatrix,
) -> None:
    done = {
        "type": "response.output_item.done",
        "sequence_number": 3,
        "output_index": 0,
        "item": {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "get_account",
            "arguments": '{"account_id": "secret-frag',
            "status": "completed",
        },
    }
    adapter = _adapter(_replying(_sse([done, _completed()])))

    message = _single_error(_run(adapter, _resolve(matrix, "tool")))

    assert "secret-frag" not in message
    assert message == "The model provider request failed: JSONDecodeError."


def test_a_malformed_frame_ends_in_an_error_without_the_payload(matrix: CapabilityMatrix) -> None:
    adapter = _adapter(_replying(b'data: {"type": "internal diagnostic frag\n\n'))

    message = _single_error(_run(adapter, _resolve(matrix, "tool")))

    assert "diagnostic" not in message


@pytest.mark.parametrize(
    "body",
    [
        b"data: [DONE]\n\n",
        _sse(
            [
                {
                    "id": "c",
                    "object": "chat.completion.chunk",
                    "choices": [{"delta": {"content": "x"}}],
                }
            ]
        ),
    ],
    ids=["zero-frames", "chat-completions-shaped"],
)
def test_a_stream_with_no_recognisable_event_ends_in_an_error(
    body: bytes, matrix: CapabilityMatrix
) -> None:
    adapter = _adapter(_replying(body))

    message = _single_error(_run(adapter, _resolve(matrix, "tool")))

    assert "no recognisable Responses API events" in message


def test_a_text_only_stream_completes_without_awaiting_tools(matrix: CapabilityMatrix) -> None:
    adapter = _adapter(_replying(_sse([_text("hello")])))

    events = _run(adapter, _resolve(matrix, "tool"))

    assert [e.type for e in events] == ["text_delta", "usage", "completed"]
    assert events[-1].awaiting_tools is False


def test_a_completed_event_without_usage_details_counts_the_missing_fields_as_zero(
    matrix: CapabilityMatrix,
) -> None:
    completed = {
        "type": "response.completed",
        "sequence_number": 2,
        "response": {
            "id": "r",
            "object": "response",
            "status": "completed",
            "output": [],
            "usage": {"input_tokens": 10},
        },
    }
    adapter = _adapter(_replying(_sse([_text("x"), completed])))

    events = _run(adapter, _resolve(matrix, "tool"))

    usage = next(e for e in events if e.type == "usage")
    assert (usage.input_tokens, usage.output_tokens) == (10, 0)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (0, 0)
    assert events[-1].type == "completed"


# --- attempts on the error event -------------------------------------------------------------


def test_an_exhausted_call_stamps_the_retried_attempts_on_the_error_event(
    matrix: CapabilityMatrix,
) -> None:
    handler, seen = _sequence(*(httpx2.Response(503) for _ in range(3)))
    adapter = _adapter(handler)

    events = _run(adapter, _resolve(matrix, "tool"))

    _single_error(events)
    assert len(seen) == 3
    assert events[0].retried_attempts == (RetriedAttempt(503, None),) * 2


def test_an_in_band_error_after_a_transport_retry_carries_that_retry(
    matrix: CapabilityMatrix,
) -> None:
    handler, _ = _sequence(
        httpx2.Response(503), _fixture_response("openai_responses_luna_live_server_error.json")
    )
    adapter = _adapter(handler)

    events = _run(adapter, _resolve(matrix, "tool"))

    assert _single_error(events).startswith("server_error: ")
    assert events[0].retried_attempts == (RetriedAttempt(503, None),)


# --- error frames, incomplete responses, and stream close failures --------------------------------


def test_a_flat_error_frame_ends_in_one_error_event_leading_with_its_code(
    matrix: CapabilityMatrix,
) -> None:
    flat = {
        "type": "error",
        "sequence_number": 2,
        "code": "server_error",
        "message": "flat",
        "param": None,
    }
    adapter = _adapter(_replying(_sse([_text("partial"), flat, _completed()])))

    events = _run(adapter, _resolve(matrix, "tool"))

    assert [e.type for e in events] == ["text_delta", "error"]
    assert events[-1].error_message == "server_error: flat"
    assert is_retryable_error(events[-1].error_message)


def test_response_incomplete_is_ignored_as_the_pre_spec_adapter_did(
    matrix: CapabilityMatrix,
) -> None:
    incomplete = {
        "type": "response.incomplete",
        "sequence_number": 3,
        "response": {
            "id": "r",
            "object": "response",
            "status": "incomplete",
            "output": [],
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"input_tokens": 9, "output_tokens": 256},
        },
    }
    adapter = _adapter(_replying(_sse([_text("cut"), incomplete])))

    events = _run(adapter, _resolve(matrix, "tool"))

    assert [e.type for e in events] == ["text_delta", "usage", "completed"]
    assert (events[1].input_tokens, events[1].output_tokens) == (0, 0)


def test_an_in_band_rate_limit_failure_is_not_retryable_as_before(
    matrix: CapabilityMatrix,
) -> None:
    failed = {
        "type": "response.failed",
        "sequence_number": 1,
        "response": {
            "id": "r",
            "object": "response",
            "status": "failed",
            "output": [],
            "error": {"code": "rate_limit_exceeded", "message": "slow down"},
        },
    }
    adapter = _adapter(_replying(_sse([failed])))

    message = _single_error(_run(adapter, _resolve(matrix, "tool")))

    assert message == "rate_limit_exceeded: slow down"
    assert not is_retryable_error(message)


@pytest.mark.parametrize("terminal", ["completed", "error"])
def test_a_failing_close_after_the_terminal_event_is_not_raised(
    terminal: str, matrix: CapabilityMatrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    import openai

    async def failing_close(self: object) -> None:
        raise httpx2.ReadError("connection reset during close")

    monkeypatch.setattr(openai.AsyncStream, "close", failing_close)
    failed = load_wire_fixture("openai_responses_luna_synth_response_failed.json")
    body = _sse([_text("done"), _completed()]) if terminal == "completed" else wire_body(failed)
    adapter = _adapter(_replying(body))

    events = _run(adapter, _resolve(matrix, "tool"))

    assert events[-1].type == terminal


def test_a_failing_close_before_any_terminal_event_is_raised(
    matrix: CapabilityMatrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    import openai

    async def failing_close(self: object) -> None:
        raise httpx2.ReadError("connection reset during close")

    monkeypatch.setattr(openai.AsyncStream, "close", failing_close)
    adapter = _adapter(_replying(_sse([_text("a"), _text("b"), _completed()])))

    async def abandon_after_first_event() -> None:
        stream = adapter.stream(_resolve(matrix, "tool"), [{"role": "user", "content": "hi"}], [])
        await stream.__anext__()
        await stream.aclose()  # type: ignore[attr-defined]

    with pytest.raises(httpx2.ReadError):
        asyncio.run(abandon_after_first_event())


def test_a_malformed_seam_message_raises_key_error_before_any_request(
    matrix: CapabilityMatrix,
) -> None:
    seen: list[httpx2.Request] = []
    adapter = _adapter(_replying(_sse([_completed()]), seen))

    with pytest.raises(KeyError):
        _run(adapter, _resolve(matrix, "tool"), [{"content": "no role"}])
    assert seen == []


# --- one SDK client per event loop ----------------------------------------------------------------


def test_each_event_loop_gets_its_own_sdk_client(matrix: CapabilityMatrix) -> None:
    seen: list[httpx2.Request] = []
    adapter = _adapter(_replying(_sse([_text("x"), _completed()]), seen))
    clients: list[object] = []

    async def one_turn() -> list[ProviderEvent]:
        events = [
            e
            async for e in adapter.stream(
                _resolve(matrix, "tool"), [{"role": "user", "content": "hi"}], []
            )
        ]
        clients.append(adapter._client())
        return events

    first = asyncio.run(one_turn())
    second = asyncio.run(one_turn())

    assert first[-1].type == second[-1].type == "completed"
    assert len(seen) == 2
    assert clients[0] is not clients[1]


# --- wall-clock deadline, credential pre-flight, recorder scope -----------------------------------


#: The adapter wall-clock deadline used by the deadline tests; each stalling body outlasts it.
_DEADLINE_SECONDS = 1.0
_DEADLINE_MESSAGE = "The model provider call exceeded its 1s wall-clock deadline."


def _deadline_adapter(handler: Any) -> OpenAIAdapter:
    adapter = OpenAIAdapter(
        region_name=_REGION,
        session=_StaticSession(),
        transport=httpx2.MockTransport(handler),
        deadline_seconds=_DEADLINE_SECONDS,
    )
    adapter._sdk._sleep_for_retry = _no_sleep  # type: ignore[method-assign]
    return adapter


class _SlowBody(httpx2.AsyncByteStream):
    """An SSE body that sends one text frame, then stalls before the rest."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield f"data: {json.dumps(_text('early'))}\n\n".encode()
        await asyncio.sleep(5)
        yield _sse([_completed()])


def test_a_stream_that_outruns_the_deadline_ends_in_one_non_retryable_error(
    matrix: CapabilityMatrix,
) -> None:
    adapter = _deadline_adapter(lambda _r: httpx2.Response(200, headers=_SSE, stream=_SlowBody()))

    events = _run(adapter, _resolve(matrix, "tool"))

    assert [e.type for e in events] == ["text_delta", "error"]
    message = events[-1].error_message
    assert message == _DEADLINE_MESSAGE
    assert not is_retryable_error(message)
    assert events[-1].retried_attempts == ()


def test_an_open_that_outruns_the_deadline_ends_in_one_non_retryable_error(
    matrix: CapabilityMatrix,
) -> None:
    async def stalled(_request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200, headers=_SSE, content=_sse([_completed()]))

    adapter = _deadline_adapter(stalled)

    events = _run(adapter, _resolve(matrix, "tool"))
    message = _single_error(events)

    assert message == _DEADLINE_MESSAGE
    assert not is_retryable_error(message)
    assert events[0].retried_attempts == ()


class _NoCredentials:
    def get_credentials(self) -> None:
        return None


class _CredentialsOnce:
    """Resolves credentials for the pre-flight only; every later resolution is empty."""

    def __init__(self) -> None:
        self.calls = 0

    def get_credentials(self) -> Credentials | None:
        self.calls += 1
        return Credentials("test", "test") if self.calls == 1 else None


@pytest.mark.parametrize(
    "session", [_NoCredentials(), _CredentialsOnce()], ids=["empty", "expired"]
)
def test_a_credential_failure_maps_to_the_taxonomy_without_any_request(
    session: Any, matrix: CapabilityMatrix
) -> None:
    seen: list[httpx2.Request] = []
    adapter = OpenAIAdapter(
        region_name=_REGION,
        session=session,
        transport=httpx2.MockTransport(_replying(_sse([_completed()]), seen)),
    )

    message = _single_error(_run(adapter, _resolve(matrix, "tool")))

    assert message == "The model provider request failed: CredentialPolicyError."
    assert not is_retryable_error(message)
    assert seen == []


def test_no_recorder_binding_is_visible_to_the_consumer_while_the_stream_is_suspended(
    matrix: CapabilityMatrix,
) -> None:
    from providers import sdk_transport

    adapter = _adapter(_replying(_sse([_text("a"), _text("b"), _completed()])))

    async def consume_then_finalize_elsewhere() -> Any:
        stream = adapter.stream(_resolve(matrix, "tool"), [{"role": "user", "content": "hi"}], [])
        first = await stream.__anext__()
        bound = sdk_transport._CURRENT_RECORDER.get()
        await asyncio.create_task(stream.aclose())  # type: ignore[attr-defined]
        return first, bound

    first, bound = asyncio.run(consume_then_finalize_elsewhere())

    assert first.type == "text_delta"
    assert bound is None


# --- a transport failure after the stream opened is not an exhausted call -------------------------


class _DroppingBody(httpx2.AsyncByteStream):
    """An SSE body that sends one text frame, then fails the read with ``exc``."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield f"data: {json.dumps(_text('partial'))}\n\n".encode()
        raise self._exc


@pytest.mark.parametrize(
    ("exc", "sdk_type"),
    [
        (httpx2.ReadError("reset"), "APIConnectionError"),
        (httpx2.RemoteProtocolError("peer closed"), "APIConnectionError"),
        (httpx2.ReadTimeout("stalled"), "APITimeoutError"),
    ],
    ids=["ReadError", "RemoteProtocolError", "ReadTimeout"],
)
def test_a_connection_failure_after_200_is_one_unretried_non_retryable_error(
    exc: Exception, sdk_type: str, matrix: CapabilityMatrix
) -> None:
    handler, seen = _sequence(httpx2.Response(200, headers=_SSE, stream=_DroppingBody(exc)))
    adapter = _adapter(handler)

    events = _run(adapter, _resolve(matrix, "tool"))

    assert [e.type for e in events] == ["text_delta", "error"]
    message = events[-1].error_message or ""
    assert message == f"{sdk_type}: The model provider request failed."
    assert "after SDK retries" not in message
    assert not is_retryable_error(message)
    assert events[-1].retried_attempts == ()
    assert len(seen) == 1


def test_a_connection_failure_after_a_retried_open_carries_only_the_retried_attempt(
    matrix: CapabilityMatrix,
) -> None:
    handler, seen = _sequence(
        httpx2.Response(503),
        httpx2.Response(200, headers=_SSE, stream=_DroppingBody(httpx2.ReadError("reset"))),
    )
    adapter = _adapter(handler)

    events = _run(adapter, _resolve(matrix, "tool"))

    assert events[-1].error_message == "APIConnectionError: The model provider request failed."
    assert events[-1].retried_attempts == (RetriedAttempt(503, None),)
    assert len(seen) == 2


def test_a_deadline_after_a_retried_open_carries_only_the_attempts_before_the_one_in_flight(
    matrix: CapabilityMatrix,
) -> None:
    handler, seen = _sequence(
        httpx2.Response(503), httpx2.Response(200, headers=_SSE, stream=_SlowBody())
    )
    adapter = _deadline_adapter(handler)

    events = _run(adapter, _resolve(matrix, "tool"))

    assert events[-1].error_message == _DEADLINE_MESSAGE
    assert events[-1].retried_attempts == (RetriedAttempt(503, None),)
    assert len(seen) == 2


# --- prompt caching -----------------------------------------------------------------------

_LONG_STABLE = "Stable supervisor instructions. " * 120
_BRIEFING = "You see the Pipeline page."


def _cached_messages(stable: str) -> list[dict[str, object]]:
    return [
        {"role": "system", "content": stable},
        {"role": "system", "content": _BRIEFING, prompt_caching.CACHE_SCOPE_KEY: "volatile"},
        {"role": "user", "content": "How many in Negotiation?"},
    ]


def _sent_body(
    matrix: CapabilityMatrix, messages: list[dict[str, object]], tier: str = "supervisor"
) -> dict[str, Any]:
    seen: list[httpx2.Request] = []
    _run(_adapter(_replying(_sse([_completed()]), seen)), _resolve(matrix, tier), messages)
    return _body(seen[0])


def test_the_stable_system_block_carries_the_breakpoint_and_the_briefing_follows_it(
    matrix: CapabilityMatrix,
) -> None:
    body = _sent_body(matrix, _cached_messages(_LONG_STABLE))
    assert body["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
    assert body["input"] == [
        {
            "role": "system",
            "content": [
                {
                    "type": "input_text",
                    "text": _LONG_STABLE,
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        },
        {"role": "system", "content": _BRIEFING},
        {"role": "user", "content": "How many in Negotiation?"},
    ]


def test_a_prefix_under_the_minimum_gets_no_breakpoint_and_stays_implicit(
    matrix: CapabilityMatrix,
) -> None:
    body = _sent_body(matrix, _cached_messages("Short."))
    assert body["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
    assert "prompt_cache_breakpoint" not in json.dumps(body)
    assert body["input"][0] == {"role": "system", "content": "Short."}


def test_a_request_without_a_stable_system_message_gets_no_breakpoint(
    matrix: CapabilityMatrix,
) -> None:
    body = _sent_body(matrix, [{"role": "user", "content": "x" * 5000}], tier="subagent")
    assert body["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
    assert "prompt_cache_breakpoint" not in json.dumps(body)
