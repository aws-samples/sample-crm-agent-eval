# SPDX-License-Identifier: MIT
"""Tests for the Anthropic and OpenAI adapters behind the provider seam.

The Anthropic tests drive the REAL ``AsyncAnthropicBedrock`` (request building, SigV4 signing with
static fake credentials, retry loop, AWS event-stream decoding) over an ``httpx2.MockTransport``
serving the recorded and synthesized wire fixtures in ``tests/integration/fixtures/bedrock/``. No
network, no AWS call. The OpenAI adapter's own tests live in ``test_adapters_openai.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import socket
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.lib.bedrock import _auth
from botocore.credentials import Credentials

from crm.errors import ValidationFailed
from eval.live import is_retryable_error
from providers import prompt_caching
from providers.anthropic_adapter import BEDROCK_ANTHROPIC_VERSION, AnthropicAdapter, build_request
from providers.base import (
    ProviderEvent,
    ResolvedInvocation,
    RetriedAttempt,
    Tuning,
    validate_tuning,
)
from providers.capabilities import load_capability_matrix
from providers.openai_adapter import OpenAIAdapter
from providers.sdk_transport import CredentialPolicyError
from tests.integration._aws_eventstream import (
    encode_chunk_event,
    load_wire_fixture,
    split_frames,
    wire_body,
)

REGION = "us-east-1"
EVENTSTREAM = "application/vnd.amazon.eventstream"

#: Anthropic fixture stem → the matrix model it was recorded against.
ANTHROPIC_MODELS = {
    "opus55": "anthropic.claude-opus-5-5",
    "sonnet55": "anthropic.claude-sonnet-5-5",
    "haiku55": "anthropic.claude-haiku-5-5",
}

#: The anthropic tiers ``config/models.yaml`` assigns.
ANTHROPIC_TIERS = tuple(sorted(load_capability_matrix().tiers["anthropic"]))

Step = httpx2.Response | Exception


# --------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------


async def _collect(adapter_stream: AsyncIterator[ProviderEvent]) -> list[ProviderEvent]:
    """Drain an adapter's async event stream into a list for assertion."""
    return [event async for event in adapter_stream]


def _run(adapter_stream: AsyncIterator[ProviderEvent]) -> list[ProviderEvent]:
    """Run and collect an adapter's async event stream on a fresh event loop."""
    return asyncio.run(_collect(adapter_stream))


@pytest.fixture(scope="module")
def matrix():  # type: ignore[no-untyped-def]
    """The real, repo-committed capability matrix."""
    return load_capability_matrix()


def _resolve(matrix: Any, family: str, tier: str, **tuning_kwargs: Any) -> ResolvedInvocation:
    """Resolve one invocation from the tier's configured defaults plus any overrides."""
    model = matrix.model_for(family, tier)
    defaults = dict(matrix.tiers[family][tier].tuning)
    merged = {**defaults, **tuning_kwargs}
    return validate_tuning(model, Tuning(source="test", **merged))


def _resolve_explicit(
    matrix: Any, family: str, tier: str, **tuning_kwargs: Any
) -> ResolvedInvocation:
    """Resolve one invocation from exactly ``tuning_kwargs``, with no configured tier default."""
    return validate_tuning(matrix.model_for(family, tier), Tuning(source="test", **tuning_kwargs))


def _stream_invocation(
    invocation: ResolvedInvocation, *steps: Step
) -> tuple[list[ProviderEvent], _Wire]:
    wire = _Wire(*steps)
    events = _run(
        _anthropic_adapter(wire).stream(invocation, [{"role": "user", "content": "hi"}], [])
    )
    return events, wire


# --------------------------------------------------------------------------------------
# Anthropic — real SDK over a mock transport
# --------------------------------------------------------------------------------------


class _StaticSession:
    """Stands in for the SDK's cached boto3 session; resolves fixed fake credentials."""

    region_name = REGION

    def get_credentials(self) -> Credentials:
        return Credentials("AKIDEXAMPLE", "fake-secret-key")


async def _no_sleep(*_: object, **__: object) -> None:
    return None


@pytest.fixture(autouse=True)
def _anthropic_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake credentials, no retry sleeps, and none of the refused Bedrock env vars."""
    for name in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_BEDROCK_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_auth, "_get_session", lambda **_: _StaticSession())
    monkeypatch.setattr(anthropic.AsyncAnthropicBedrock, "_sleep_for_retry", _no_sleep)


def _fixture_response(name: str) -> httpx2.Response:
    return httpx2.Response(
        200, content=wire_body(load_wire_fixture(name)), headers={"content-type": EVENTSTREAM}
    )


class _Wire:
    """A mock transport replaying ``steps`` in order and recording every request it receives."""

    def __init__(self, *steps: Step) -> None:
        self.requests: list[httpx2.Request] = []
        self._steps = list(steps)
        self.transport = httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def body(self, index: int = 0) -> dict[str, Any]:
        parsed: dict[str, Any] = json.loads(self.requests[index].content)
        return parsed


def _anthropic_adapter(wire: _Wire, clock: Callable[[], float] | None = None) -> AnthropicAdapter:
    if clock is None:
        return AnthropicAdapter(
            region_name=REGION, session=_StaticSession(), transport=wire.transport
        )
    return AnthropicAdapter(
        region_name=REGION, session=_StaticSession(), transport=wire.transport, clock=clock
    )


def _stream_anthropic(
    matrix: Any,
    tier: str,
    *steps: Step,
    messages: list[dict[str, object]] | None = None,
    tools: list[dict[str, object]] | None = None,
) -> tuple[list[ProviderEvent], _Wire]:
    return _stream_anthropic_resolved(
        _resolve(matrix, "anthropic", tier), *steps, messages=messages, tools=tools
    )


def _stream_anthropic_model(
    matrix: Any, model_id: str, *steps: Step
) -> tuple[list[ProviderEvent], _Wire]:
    """Stream one turn against ``model_id`` at its own default effort."""
    invocation = validate_tuning(matrix.models[model_id], Tuning(source="test"))
    return _stream_anthropic_resolved(invocation, *steps)


def _stream_anthropic_resolved(
    invocation: ResolvedInvocation,
    *steps: Step,
    messages: list[dict[str, object]] | None = None,
    tools: list[dict[str, object]] | None = None,
) -> tuple[list[ProviderEvent], _Wire]:
    wire = _Wire(*steps)
    events = _run(
        _anthropic_adapter(wire).stream(
            invocation,
            messages or [{"role": "user", "content": "Call get_account for account a-1."}],
            tools if tools is not None else _GET_ACCOUNT_TOOL,
        )
    )
    return events, wire


_GET_ACCOUNT_TOOL: list[dict[str, object]] = [
    {
        "name": "get_account",
        "description": "Fetch one CRM account by id.",
        "input_schema": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}},
            "required": ["account_id"],
        },
    }
]


#: Stems recorded against a model still in the capability matrix, so the whole request shape
#: replays.
CURRENT_RECORDINGS = ("opus55", "sonnet55", "haiku55")


@pytest.mark.parametrize("stem", CURRENT_RECORDINGS)
def test_anthropic_request_matches_the_recorded_probe_shape_per_model(
    matrix: Any, stem: str
) -> None:
    fixture = load_wire_fixture(f"anthropic_invoke_stream_{stem}_tool_use.json")
    _, wire = _stream_anthropic_model(
        matrix, ANTHROPIC_MODELS[stem], _fixture_response(fixture_name(stem, "tool_use"))
    )
    request = wire.requests[0]
    sent = wire.body()
    recorded = fixture["request"]["body"]

    assert request.url.path == fixture["request"]["path"]
    assert request.url.host == f"bedrock-runtime.{REGION}.amazonaws.com"
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert set(sent) == set(recorded)
    assert sent["tools"] == recorded["tools"]
    assert sent["anthropic_version"] == recorded["anthropic_version"]


@pytest.mark.parametrize("tier", ANTHROPIC_TIERS)
def test_anthropic_request_pins_the_bedrock_api_version_over_the_sdk_default(
    matrix: Any, tier: str
) -> None:
    """Every tier's body carries the Bedrock ``anthropic_version``, not the SDK's API version."""
    _, wire = _stream_anthropic(matrix, tier, _fixture_response(fixture_name("opus55", "success")))
    sent = wire.body()

    assert sent["anthropic_version"] == BEDROCK_ANTHROPIC_VERSION == "bedrock-2023-05-31"
    assert anthropic._constants.ANTHROPIC_API_VERSION != BEDROCK_ANTHROPIC_VERSION


def test_anthropic_tool_tier_request_is_the_effort_shape(matrix: Any) -> None:
    """The tool tier's model sends adaptive thinking and its configured effort, no sampling."""
    fixture = load_wire_fixture(fixture_name("haiku55", "tool_use"))
    model = matrix.model_for("anthropic", "tool")
    _, wire = _stream_anthropic(
        matrix, "tool", _fixture_response(fixture_name("haiku55", "tool_use"))
    )
    request = wire.requests[0]
    sent = wire.body()
    recorded = fixture["request"]["body"]

    assert model.effort.supported
    assert request.url.path == f"/model/{model.inference_profile}/invoke-with-response-stream"
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "low"}
    assert not {"temperature", "top_p", "top_k", "extra_body"} & set(sent)
    assert set(sent) == set(recorded)
    assert sent["tools"] == recorded["tools"]


def fixture_name(stem: str, kind: str) -> str:
    return f"anthropic_invoke_stream_{stem}_{kind}.json"


@pytest.mark.parametrize(("tier", "effort"), [("supervisor", "high"), ("subagent", "medium")])
def test_anthropic_adapter_capability_effort_models_send_the_handed_effort_and_no_sampling(
    matrix: Any, tier: str, effort: str
) -> None:
    """An effort the adapter is handed is sent beside adaptive thinking, with no sampling."""
    _, wire = _stream_invocation(
        _resolve_explicit(matrix, "anthropic", tier, effort=effort),
        _fixture_response(fixture_name("opus55", "success")),
    )
    sent = wire.body()
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": effort}
    assert not {"temperature", "top_p", "top_k"} & set(sent)


def test_anthropic_request_for_a_model_without_effort_carries_no_effort_or_sampling(
    matrix: Any,
) -> None:
    """A model whose capability has no effort sends neither thinking nor sampling parameters."""
    model = matrix.model_for("anthropic", "tool")
    no_effort = dataclasses.replace(
        model, effort=dataclasses.replace(model.effort, supported=False)
    )
    request = build_request(
        ResolvedInvocation(model=no_effort, effort=None),
        [{"role": "user", "content": "hi"}],
        [],
    )
    assert not {"temperature", "top_p", "top_k", "thinking", "output_config", "extra_body"} & set(
        request
    )


@pytest.mark.parametrize("stem", list(ANTHROPIC_MODELS))
def test_anthropic_tool_use_fixture_normalises_to_one_tool_call_awaiting_tools(
    matrix: Any, stem: str
) -> None:
    events, _ = _stream_anthropic_model(
        matrix, ANTHROPIC_MODELS[stem], _fixture_response(fixture_name(stem, "tool_use"))
    )
    tool_calls = [e for e in events if e.type == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "get_account"
    assert tool_calls[0].tool_input == {"account_id": "a-1"}
    assert (tool_calls[0].tool_id or "").startswith("toolu_")
    assert [e.type for e in events[-2:]] == ["usage", "completed"]
    assert events[-1].awaiting_tools is True
    assert (events[-2].input_tokens or 0) > 0 and (events[-2].output_tokens or 0) > 0


@pytest.mark.parametrize("stem", list(ANTHROPIC_MODELS))
def test_anthropic_success_fixture_streams_text_and_ends_without_awaiting_tools(
    matrix: Any, stem: str
) -> None:
    events, _ = _stream_anthropic_model(
        matrix, ANTHROPIC_MODELS[stem], _fixture_response(fixture_name(stem, "success"))
    )
    text = "".join(e.text or "" for e in events if e.type == "text_delta")
    assert text
    assert [e.type for e in events[-2:]] == ["usage", "completed"]
    assert events[-1].awaiting_tools is False
    assert events[-2].retried_attempts == ()


def test_anthropic_lifts_system_role_to_top_level_system(matrix: Any) -> None:
    _, wire = _stream_anthropic(
        matrix,
        "supervisor",
        _fixture_response(fixture_name("opus55", "success")),
        messages=[
            {"role": "system", "content": "You see the Pipeline page."},
            {"role": "user", "content": "How many in Negotiation?"},
        ],
    )
    sent = wire.body()
    assert sent["system"] == "You see the Pipeline page."
    assert [m["role"] for m in sent["messages"]] == ["user"]


def test_anthropic_serializes_a_tool_round_as_tool_use_and_tool_result_blocks(
    matrix: Any,
) -> None:
    _, wire = _stream_anthropic(
        matrix,
        "supervisor",
        _fixture_response(fixture_name("opus55", "success")),
        messages=[
            {"role": "user", "content": "find a-1"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "toolu_1", "name": "get_account", "input": {"a": 1}}],
            },
            {"role": "tool", "tool_results": [{"id": "toolu_1", "output": '{"ok": true}'}]},
        ],
    )
    assert wire.body()["messages"][1:] == [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "get_account", "input": {"a": 1}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"ok": true}'}
            ],
        },
    ]


# --- In-band vs transport-exhausted errors ---------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "prefix"),
    [
        ("anthropic_invoke_stream_synth_model_stream_error.json", "ModelStreamErrorException: "),
        (
            "anthropic_invoke_stream_synth_internal_server_exception.json",
            "InternalServerException: ",
        ),
    ],
)
def test_anthropic_in_band_exception_is_one_error_the_wrapper_retries(
    matrix: Any, fixture: str, prefix: str
) -> None:
    events, wire = _stream_anthropic(matrix, "supervisor", _fixture_response(fixture))
    assert [e.type for e in events] == ["text_delta", "error"]
    assert events[-1].retried_attempts == ()
    message = events[-1].error_message or ""
    assert message.startswith(prefix)
    assert is_retryable_error(message)
    assert len(wire.requests) == 1


def test_anthropic_in_band_error_after_an_sdk_retry_carries_that_retried_attempt(
    matrix: Any,
) -> None:
    events, wire = _stream_anthropic(
        matrix,
        "supervisor",
        httpx2.Response(503),
        _fixture_response("anthropic_invoke_stream_synth_model_stream_error.json"),
    )
    assert events[-1].type == "error"
    assert events[-1].retried_attempts == (RetriedAttempt(503, None),)
    assert len(wire.requests) == 2


@pytest.mark.parametrize(
    ("status", "attempts"),
    [(503, 3), (500, 3), (429, 3), (400, 1), (403, 1)],
)
def test_anthropic_sdk_exhausted_status_is_prefixed_and_never_retried_again(
    matrix: Any, status: int, attempts: int
) -> None:
    steps = [httpx2.Response(status, json={"message": "no"}) for _ in range(3)]
    events, wire = _stream_anthropic(matrix, "supervisor", *steps)
    assert [e.type for e in events] == ["error"]
    message = events[0].error_message or ""
    assert message == (
        f"The model provider request failed after SDK retries: APIStatusError(status={status})."
    )
    assert not is_retryable_error(message)
    assert len(wire.requests) == attempts
    assert events[0].retried_attempts == (RetriedAttempt(status, None),) * (attempts - 1)


def test_anthropic_sdk_exhausted_connection_error_is_prefixed_and_never_retried_again(
    matrix: Any,
) -> None:
    events, wire = _stream_anthropic(
        matrix, "supervisor", *(httpx2.ConnectError("refused") for _ in range(3))
    )
    message = events[0].error_message or ""
    assert [e.type for e in events] == ["error"]
    assert message == "The model provider request failed after SDK retries: APIConnectionError."
    assert events[0].retried_attempts == (RetriedAttempt(None, "ConnectError"),) * 2
    assert not is_retryable_error(message)
    assert len(wire.requests) == 3


@pytest.mark.parametrize(
    ("status", "body", "retryable"),
    [
        (200, {"type": "error", "error": {"type": "modelStreamErrorException"}}, True),
        (200, {"type": "error"}, False),
        (500, {"type": "error", "error": {"type": "modelStreamErrorException"}}, False),
    ],
)
def test_anthropic_in_band_discrimination_is_by_status_200_and_body_type(
    status: int, body: dict[str, object], retryable: bool
) -> None:
    from providers.anthropic_adapter import error_message

    request = httpx2.Request("POST", f"https://bedrock-runtime.{REGION}.amazonaws.com/")
    exc = anthropic.APIStatusError(
        "x", response=httpx2.Response(status, request=request), body=body
    )
    assert is_retryable_error(error_message(exc)) is retryable


# --- Attempt records and the latency window -------------------------------------------


@pytest.mark.parametrize(
    ("first", "expected"),
    [
        (httpx2.Response(503), RetriedAttempt(503, None)),
        (httpx2.Response(429), RetriedAttempt(429, None)),
        (httpx2.ConnectError("refused"), RetriedAttempt(None, "ConnectError")),
    ],
)
def test_anthropic_usage_event_carries_the_sdk_retried_attempts(
    matrix: Any, first: Step, expected: RetriedAttempt
) -> None:
    events, wire = _stream_anthropic(
        matrix, "supervisor", first, _fixture_response(fixture_name("opus55", "success"))
    )
    usage = next(e for e in events if e.type == "usage")
    assert usage.retried_attempts == (expected,)
    assert events[-1].type == "completed"
    assert len(wire.requests) == 2


class _Clock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_anthropic_latency_runs_from_the_successful_attempt_to_the_end_of_the_stream(
    matrix: Any,
) -> None:
    clock = _Clock(100.0)
    frames = split_frames(wire_body(load_wire_fixture(fixture_name("opus55", "success"))))

    async def body() -> AsyncIterator[bytes]:
        for frame in frames:
            clock.now += 1.0
            yield frame

    def handler(request: httpx2.Request) -> httpx2.Response:
        if len(seen) == 0:
            seen.append(request)
            clock.now += 50.0
            return httpx2.Response(503)
        seen.append(request)
        return httpx2.Response(200, content=body(), headers={"content-type": EVENTSTREAM})

    seen: list[httpx2.Request] = []
    adapter = AnthropicAdapter(
        region_name=REGION,
        session=_StaticSession(),
        transport=httpx2.MockTransport(handler),
        clock=clock,
    )
    events = _run(
        adapter.stream(
            _resolve(matrix, "anthropic", "supervisor"), [{"role": "user", "content": "hi"}], []
        )
    )
    usage = next(e for e in events if e.type == "usage")
    assert usage.latency_ms == pytest.approx(len(frames) * 1000.0)
    assert usage.retried_attempts == (RetriedAttempt(503, None),)


# --- Whole-call deadline and credential pre-flight ------------------------------------

#: The adapter wall-clock deadline used by the deadline tests.
_DEADLINE_SECONDS = 1.0
_DEADLINE_MESSAGE = "The model provider call exceeded its 1s wall-clock deadline."


def _deadline_adapter(handler: Callable[[httpx2.Request], Any]) -> AnthropicAdapter:
    return AnthropicAdapter(
        region_name=REGION,
        session=_StaticSession(),
        transport=httpx2.MockTransport(handler),
        deadline_seconds=_DEADLINE_SECONDS,
    )


#: Outer bound for the deadline tests; only the adapter's own deadline ends a stall before it.
_STALL_BOUND_SECONDS = 30.0


def _run_bounded(adapter_stream: AsyncIterator[ProviderEvent]) -> list[ProviderEvent]:
    async def bounded() -> list[ProviderEvent]:
        return await asyncio.wait_for(_collect(adapter_stream), _STALL_BOUND_SECONDS)

    return asyncio.run(bounded())


def test_anthropic_deadline_bounds_the_stream_open(matrix: Any) -> None:
    cancelled: list[bool] = []

    async def stalled_open(request: httpx2.Request) -> httpx2.Response:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        raise AssertionError("unreachable: the event is never set")

    events = _run_bounded(
        _deadline_adapter(stalled_open).stream(
            _resolve(matrix, "anthropic", "supervisor"), [{"role": "user", "content": "hi"}], []
        )
    )
    assert [e.type for e in events] == ["error"]
    assert events[0].error_message == _DEADLINE_MESSAGE
    assert not is_retryable_error(_DEADLINE_MESSAGE)
    assert events[0].retried_attempts == ()
    assert cancelled == [True]


def test_anthropic_deadline_bounds_the_stream_body_and_closes_it(matrix: Any) -> None:
    frames = split_frames(wire_body(load_wire_fixture(fixture_name("opus55", "success"))))
    closed: list[bool] = []

    async def stalling_body() -> AsyncIterator[bytes]:
        try:
            for frame in frames[:3]:
                yield frame
            await asyncio.Event().wait()
            yield frames[3]
        finally:
            closed.append(True)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=stalling_body(), headers={"content-type": EVENTSTREAM})

    events = _run_bounded(
        _deadline_adapter(handler).stream(
            _resolve(matrix, "anthropic", "supervisor"), [{"role": "user", "content": "hi"}], []
        )
    )
    assert events[-1].type == "error"
    assert events[-1].error_message == _DEADLINE_MESSAGE
    assert "usage" not in {e.type for e in events}
    assert events[-1].retried_attempts == ()
    assert closed == [True]


def test_anthropic_deadline_error_carries_only_the_attempts_before_the_one_in_flight(
    matrix: Any,
) -> None:
    calls: list[int] = []

    async def retry_then_stall(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx2.Response(503)
        await asyncio.Event().wait()
        raise AssertionError("unreachable: the event is never set")

    events = _run_bounded(
        _deadline_adapter(retry_then_stall).stream(
            _resolve(matrix, "anthropic", "supervisor"), [{"role": "user", "content": "hi"}], []
        )
    )
    assert [e.type for e in events] == ["error"]
    assert events[0].error_message == _DEADLINE_MESSAGE
    assert events[0].retried_attempts == (RetriedAttempt(503, None),)
    assert len(calls) == 2


class _ClosableBody(httpx2.AsyncByteStream):
    """A response body that records whether the client closed it."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for frame in self._frames:
            yield frame

    async def aclose(self) -> None:
        self.closed = True


def test_anthropic_abandoned_stream_closes_the_sdk_response(matrix: Any) -> None:
    body = _ClosableBody(
        split_frames(wire_body(load_wire_fixture(fixture_name("opus55", "success"))))
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=body, headers={"content-type": EVENTSTREAM})

    adapter = _deadline_adapter(handler)

    async def take_first_text() -> bool:
        stream = adapter.stream(
            _resolve(matrix, "anthropic", "supervisor"), [{"role": "user", "content": "hi"}], []
        )
        async for event in stream:
            if event.type == "text_delta":
                break
        await stream.aclose()  # type: ignore[attr-defined]
        return body.closed

    assert asyncio.run(take_first_text()) is True


def test_anthropic_empty_credential_chain_is_one_taxonomy_error_before_any_request(
    matrix: Any,
) -> None:
    class _EmptySession:
        def get_credentials(self) -> None:
            return None

    wire = _Wire(_fixture_response(fixture_name("opus55", "success")))
    adapter = AnthropicAdapter(
        region_name=REGION, session=_EmptySession(), transport=wire.transport
    )
    events = _run(
        adapter.stream(
            _resolve(matrix, "anthropic", "supervisor"), [{"role": "user", "content": "hi"}], []
        )
    )
    assert [e.type for e in events] == ["error"]
    assert events[0].error_message == (
        "CredentialPolicyError: The boto3 session resolved no AWS credentials."
    )
    assert not is_retryable_error(events[0].error_message or "")
    assert wire.requests == []


# --- Cache-token mapping, recorder scope, mid-stream transport failure ----------------


def _stream_body(events: list[dict[str, object]]) -> httpx2.Response:
    return httpx2.Response(
        200,
        content=b"".join(encode_chunk_event(event) for event in events),
        headers={"content-type": EVENTSTREAM},
    )


def _message_events(usage: dict[str, object], text: str = "ok") -> list[dict[str, object]]:
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_cache",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": usage,
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]


def test_anthropic_cache_tokens_map_read_and_creation_to_distinct_seam_fields(
    matrix: Any,
) -> None:
    events, _ = _stream_anthropic(
        matrix,
        "supervisor",
        _stream_body(
            _message_events(
                {
                    "input_tokens": 50,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 7,
                    "cache_creation_input_tokens": 11,
                }
            )
        ),
    )
    usage = next(e for e in events if e.type == "usage")
    assert (usage.input_tokens, usage.output_tokens) == (50, 5)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (7, 11)


def test_anthropic_recorder_is_unbound_while_the_stream_is_suspended(matrix: Any) -> None:
    from providers import sdk_transport

    wire = _Wire(_fixture_response(fixture_name("opus55", "success")))
    adapter = _anthropic_adapter(wire)

    async def consume_then_finalize_elsewhere() -> tuple[ProviderEvent, object]:
        stream = adapter.stream(
            _resolve(matrix, "anthropic", "supervisor"), [{"role": "user", "content": "hi"}], []
        )
        first = await stream.__anext__()
        bound = sdk_transport._CURRENT_RECORDER.get()
        await asyncio.create_task(stream.aclose())  # type: ignore[attr-defined]
        return first, bound

    first, bound = asyncio.run(consume_then_finalize_elsewhere())
    assert first.type == "text_delta"
    assert bound is None


class _FailingBody(httpx2.AsyncByteStream):
    """An event-stream body that sends a few frames, then fails the read with ``exc``."""

    def __init__(self, frames: list[bytes], exc: Exception) -> None:
        self._frames = frames
        self._exc = exc

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for frame in self._frames:
            yield frame
        raise self._exc


@pytest.mark.parametrize(
    "exc",
    [httpx2.ReadError("reset"), httpx2.RemoteProtocolError("peer closed"), httpx2.ReadTimeout("x")],
    ids=["ReadError", "RemoteProtocolError", "ReadTimeout"],
)
def test_anthropic_transport_failure_mid_stream_is_one_error_event_not_a_raise(
    matrix: Any, exc: Exception
) -> None:
    frames = split_frames(wire_body(load_wire_fixture(fixture_name("opus55", "success"))))
    wire = _Wire(
        httpx2.Response(
            200, stream=_FailingBody(frames[:3], exc), headers={"content-type": EVENTSTREAM}
        )
    )
    events = _run(
        _anthropic_adapter(wire).stream(
            _resolve(matrix, "anthropic", "supervisor"), [{"role": "user", "content": "hi"}], []
        )
    )
    assert events[-1].type == "error"
    assert [e.type for e in events].count("error") == 1
    assert "usage" not in {e.type for e in events}
    message = events[-1].error_message or ""
    assert message == f"{type(exc).__name__}: The model provider request failed."
    assert not is_retryable_error(message)
    assert events[-1].retried_attempts == ()
    assert len(wire.requests) == 1


# --- Client lifecycle, lock-down, family guard ------------------------------------------


def test_anthropic_adapter_serves_turns_on_successive_event_loops(matrix: Any) -> None:
    wire = _Wire(
        _fixture_response(fixture_name("opus55", "success")),
        _fixture_response(fixture_name("opus55", "success")),
    )
    adapter = _anthropic_adapter(wire)
    invocation = _resolve(matrix, "anthropic", "supervisor")
    clients: list[object] = []

    async def turn() -> list[ProviderEvent]:
        events = await _collect(adapter.stream(invocation, [{"role": "user", "content": "hi"}], []))
        clients.append(adapter._sdk)
        return events

    for _ in range(2):
        assert asyncio.run(turn())[-1].type == "completed"
    assert clients[0] is not clients[1]


def test_control_anthropic_sdk_http_client_cannot_serve_a_second_event_loop() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=_keep_alive_ok, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    client = httpx2.AsyncClient()

    async def get() -> int:
        return (await client.get(f"http://127.0.0.1:{port}/")).status_code

    try:
        assert asyncio.run(get()) == 200
        with pytest.raises(RuntimeError, match="Event loop is closed"):
            asyncio.run(get())
    finally:
        server.close()


def _keep_alive_ok(conn: socket.socket) -> None:
    with conn:
        while True:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")


def test_anthropic_adapter_refuses_a_bearer_env_var_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "x")
    with pytest.raises(CredentialPolicyError):
        AnthropicAdapter(region_name=REGION)


def test_anthropic_adapter_refuses_an_openai_invocation_before_any_request(matrix: Any) -> None:
    wire = _Wire()
    with pytest.raises(ValueError, match="anthropic adapter received a openai invocation"):
        _run(
            _anthropic_adapter(wire).stream(
                _resolve(matrix, "openai", "supervisor"), [{"role": "user", "content": "hi"}], []
            )
        )
    assert wire.requests == []


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_anthropic_sonnet55_rejects_disputed_effort_before_any_adapter_call(
    matrix: Any, effort: str
) -> None:
    model = matrix.model_for("anthropic", "subagent")
    with pytest.raises(ValidationFailed) as excinfo:
        validate_tuning(model, Tuning(source="--effort", effort=effort))
    assert "anthropic.claude-sonnet-5-5" in excinfo.value.message
    assert excinfo.value.source == "--effort"


# --------------------------------------------------------------------------------------
# Cross-family: identical normalised event stream shape
# --------------------------------------------------------------------------------------


class _OpenAISession:
    """A boto3-session stand-in for the OpenAI adapter; resolves fixed fake credentials."""

    def get_credentials(self) -> Credentials:
        return Credentials("AKIDEXAMPLE", "fake-secret-key")


def test_both_families_emit_the_same_closed_event_type_vocabulary(matrix: Any) -> None:
    anthropic_events, _ = _stream_anthropic(
        matrix, "supervisor", _fixture_response(fixture_name("opus55", "success")), tools=[]
    )
    fixture = load_wire_fixture("openai_responses_luna_success.json")
    openai_wire = _Wire(
        httpx2.Response(
            200, content=wire_body(fixture), headers={"content-type": "text/event-stream"}
        )
    )
    openai_events = _run(
        OpenAIAdapter(
            region_name=REGION, session=_OpenAISession(), transport=openai_wire.transport
        ).stream(_resolve(matrix, "openai", "tool"), [{"role": "user", "content": "hi"}], [])
    )

    allowed_types = {"text_delta", "tool_call", "tool_result", "usage", "error", "completed"}
    for events in (anthropic_events, openai_events):
        assert {e.type for e in events} <= allowed_types
        assert [e.type for e in events[-2:]] == ["usage", "completed"]
        assert any(e.type == "text_delta" and e.text for e in events)


# --- prompt caching -----------------------------------------------------------------------------

_LONG_STABLE = "Stable supervisor instructions. " * 120
_BRIEFING = "You see the Pipeline page."


def _cached_turn(stable: str = _LONG_STABLE) -> list[dict[str, object]]:
    return [
        {"role": "system", "content": stable},
        {"role": "system", "content": _BRIEFING, prompt_caching.CACHE_SCOPE_KEY: "volatile"},
        {"role": "user", "content": "How many in Negotiation?"},
    ]


def _openai_body(
    matrix: Any, messages: list[dict[str, object]], tools: list[dict[str, object]]
) -> dict[str, Any]:
    adapter = OpenAIAdapter(region_name=REGION, session=_OpenAISession())
    return adapter._build_responses_body(_resolve(matrix, "openai", "supervisor"), messages, tools)


def test_anthropic_caches_the_stable_system_block_and_leaves_the_briefing_after_it(
    matrix: Any,
) -> None:
    request = build_request(_resolve(matrix, "anthropic", "supervisor"), _cached_turn(), [])
    assert request["system"] == [
        {"type": "text", "text": _LONG_STABLE, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _BRIEFING},
    ]
    assert [m["role"] for m in request["messages"]] == ["user"]


def test_anthropic_sends_no_breakpoint_when_the_stable_prefix_is_under_the_minimum(
    matrix: Any,
) -> None:
    request = build_request(_resolve(matrix, "anthropic", "supervisor"), _cached_turn("Short."), [])
    assert request["system"] == f"Short.\n\n{_BRIEFING}"
    assert "cache_control" not in json.dumps(request)


def test_anthropic_sends_no_breakpoint_without_a_stable_system_message(matrix: Any) -> None:
    tools: list[dict[str, object]] = [
        {"name": "t", "description": "d" * 4000, "input_schema": {"type": "object"}}
    ]
    request = build_request(
        _resolve(matrix, "anthropic", "subagent"), [{"role": "user", "content": "go"}], tools
    )
    assert "system" not in request
    assert "cache_control" not in json.dumps(request)


def test_the_cache_scope_marker_never_reaches_either_request(matrix: Any) -> None:
    anthropic_request = build_request(
        _resolve(matrix, "anthropic", "supervisor"), _cached_turn(), []
    )
    openai_body = _openai_body(matrix, _cached_turn(), [])
    assert prompt_caching.CACHE_SCOPE_KEY not in json.dumps(anthropic_request)
    assert prompt_caching.CACHE_SCOPE_KEY not in json.dumps(openai_body)


def test_both_families_place_the_breakpoint_on_the_same_message(matrix: Any) -> None:
    """Above both minimums, each family marks the same seam message and nothing else."""
    messages = _cached_turn()
    anthropic_system = build_request(_resolve(matrix, "anthropic", "supervisor"), messages, [])[
        "system"
    ]
    anthropic_marked = [i for i, b in enumerate(anthropic_system) if "cache_control" in b]
    openai_input = _openai_body(matrix, messages, [])["input"]
    openai_marked = [
        i
        for i, item in enumerate(openai_input)
        if isinstance(item["content"], list)
        and any("prompt_cache_breakpoint" in part for part in item["content"])
    ]
    assert anthropic_marked == openai_marked == [0]


def test_the_breakpoint_needs_a_prefix_that_can_reach_the_model_minimum() -> None:
    minimum = prompt_caching.min_prefix_tokens("anthropic.claude-opus-5-5")
    assert minimum == 512
    at_floor = [{"role": "system", "content": "x" * (minimum * 2 - 2)}]
    tools: list[dict[str, object]] = []
    assert len(json.dumps(tools, sort_keys=True)) == 2
    assert prompt_caching.breakpoint_index("anthropic.claude-opus-5-5", at_floor, tools) == 0
    one_short = [{"role": "system", "content": "x" * (minimum * 2 - 3)}]
    assert prompt_caching.breakpoint_index("anthropic.claude-opus-5-5", one_short, tools) is None


def test_a_volatile_message_ends_the_stable_run() -> None:
    assert prompt_caching.stable_system_end(_cached_turn()) == 0
    volatile_first = [
        {"role": "system", "content": "x", prompt_caching.CACHE_SCOPE_KEY: "volatile"},
        {"role": "system", "content": "y"},
    ]
    assert prompt_caching.stable_system_end(volatile_first) is None
    assert prompt_caching.stable_system_end([{"role": "user", "content": "hi"}]) is None


def test_every_configured_model_publishes_a_cache_minimum(matrix: Any) -> None:
    """The OpenAI models' published 1,024-token minimum, the Anthropic models' 512."""
    for family, expected in (("anthropic", 512), ("openai", 1024)):
        for tier in ("supervisor", "subagent", "tool"):
            model_id = matrix.model_for(family, tier).model_id
            assert prompt_caching.min_prefix_tokens(model_id) == expected, model_id
