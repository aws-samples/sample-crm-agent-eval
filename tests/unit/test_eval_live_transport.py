# SPDX-License-Identifier: MIT
"""Tests for the live wrapper's mid-stream transport classification and its non-blocking backoff.

Mid-stream failures are driven through the real adapters and vendor SDKs over an
``httpx2.MockTransport`` whose body fails after the stream opened, so the adapters' renderings and
the wrapper's classification are exercised together. No network is reached.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import anthropic
import httpx2
import openai
import pytest
from anthropic.lib.aws import _auth
from botocore.credentials import Credentials

from eval.live import (
    MID_STREAM_TRANSPORT_PREFIXES,
    LiveInvocationError,
    MidStreamTransportError,
    ThrottleRetryingProvider,
    is_mid_stream_transport_error,
    is_retryable_error,
)
from providers.anthropic_adapter import AnthropicAdapter
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, Tuning, validate_tuning
from providers.capabilities import load_capability_matrix
from providers.openai_adapter import OpenAIAdapter, mid_stream_error_message
from providers.sdk_transport import EXHAUSTED_PREFIX
from tests.integration._aws_eventstream import load_wire_fixture, split_frames, wire_body

_REGION = "us-east-1"
_EVENTSTREAM = "application/vnd.amazon.eventstream"
_SSE = "text/event-stream"

_SUCCESS_TURN = [
    ProviderEvent(type="text_delta", text="ok"),
    ProviderEvent(type="usage", input_tokens=10, output_tokens=2),
    ProviderEvent(type="completed", input_tokens=10, output_tokens=2),
]
_THROTTLED_TURN = [
    ProviderEvent(type="error", error_message="ThrottlingException: Too many requests.")
]


class _StaticSession:
    region_name = _REGION

    def get_credentials(self) -> Credentials:
        return Credentials("test-access-key-id", "test-signing-secret")


async def _no_sleep(*_: object, **__: object) -> None:
    return None


@pytest.fixture(autouse=True)
def _offline_sdks(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_BEDROCK_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_auth, "_get_session", lambda **_: _StaticSession())
    monkeypatch.setattr(anthropic.AsyncAnthropicBedrock, "_sleep_for_retry", _no_sleep)
    monkeypatch.setattr(openai.AsyncBedrockOpenAI, "_sleep_for_retry", _no_sleep)


def _invocation(family: str) -> ResolvedInvocation:
    model = load_capability_matrix().model_for(family, "supervisor")  # type: ignore[arg-type]
    return validate_tuning(model, Tuning(source="test", effort="low"))


class _ScriptedProvider(ModelProvider):
    """Yields one scripted event list per attempt; the last list repeats."""

    def __init__(self, turns: list[list[ProviderEvent]]) -> None:
        self._turns = turns
        self.attempts = 0

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        events = self._turns[min(self.attempts, len(self._turns) - 1)]
        self.attempts += 1
        for event in events:
            yield event


class _CountingProvider(ModelProvider):
    """Counts how many times the wrapper opened the wrapped adapter's stream."""

    def __init__(self, wrapped: ModelProvider) -> None:
        self._wrapped = wrapped
        self.streams = 0

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.streams += 1
        async for event in self._wrapped.stream(invocation, messages, tools):
            yield event


class _FailingBody(httpx2.AsyncByteStream):
    """A response body that sends ``chunks``, then fails the read with ``exc``."""

    def __init__(self, chunks: list[bytes], exc: Exception) -> None:
        self._chunks = chunks
        self._exc = exc

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        raise self._exc


def _openai_text_frame() -> bytes:
    delta = {
        "type": "response.output_text.delta",
        "sequence_number": 1,
        "item_id": "msg_1",
        "output_index": 0,
        "content_index": 0,
        "delta": "partial",
    }
    return f"data: {json.dumps(delta)}\n\n".encode()


def _anthropic_frames() -> list[bytes]:
    fixture = load_wire_fixture("anthropic_invoke_stream_opus55_success.json")
    return split_frames(wire_body(fixture))[:3]


def _dropping_adapter(family: str, exc: Exception) -> tuple[ModelProvider, list[int]]:
    hits: list[int] = []
    if family == "anthropic":
        body = _FailingBody(_anthropic_frames(), exc)
        content_type = _EVENTSTREAM
    else:
        body = _FailingBody([_openai_text_frame()], exc)
        content_type = _SSE

    def handle(_request: httpx2.Request) -> httpx2.Response:
        hits.append(1)
        return httpx2.Response(200, stream=body, headers={"content-type": content_type})

    adapter_cls: Callable[..., ModelProvider] = (
        AnthropicAdapter if family == "anthropic" else OpenAIAdapter
    )
    adapter = adapter_cls(
        region_name=_REGION, session=_StaticSession(), transport=httpx2.MockTransport(handle)
    )
    return adapter, hits


def _drain(provider: ModelProvider, invocation: ResolvedInvocation) -> list[ProviderEvent]:
    async def _go() -> list[ProviderEvent]:
        return [event async for event in provider.stream(invocation, [], [])]

    return asyncio.run(_go())


def _transport_error_names() -> set[str]:
    names: set[str] = set()
    pending: list[type[BaseException]] = [httpx2.TransportError]
    while pending:
        cls = pending.pop()
        names.add(cls.__name__)
        pending.extend(cls.__subclasses__())
    return names


# --- the prefix set is read from the transport library, not asserted from memory -------------


def test_every_httpx2_transport_error_subclass_is_a_mid_stream_prefix() -> None:
    missing = _transport_error_names() - MID_STREAM_TRANSPORT_PREFIXES
    assert not missing, f"httpx2 transport errors absent from the prefix set: {sorted(missing)}"


def test_every_sdk_connection_error_the_openai_adapter_renders_mid_stream_is_a_prefix() -> None:
    request = httpx2.Request("POST", "https://bedrock-runtime.us-east-1.amazonaws.com/")
    for exc in (openai.APIConnectionError(request=request), openai.APITimeoutError(request)):
        assert is_mid_stream_transport_error(mid_stream_error_message(exc))


def test_no_mid_stream_prefix_is_also_a_retryable_prefix() -> None:
    for prefix in MID_STREAM_TRANSPORT_PREFIXES:
        assert not is_retryable_error(f"{prefix}: The model provider request failed.")


@pytest.mark.parametrize(
    "message",
    [
        f"{EXHAUSTED_PREFIX}APIConnectionError.",
        "ValueError: The model provider request failed.",
        "ReadError: connection reset by peer",
        "ReadError",
        "ThrottlingException: Too many requests.",
        "",
    ],
    ids=["sdk-exhausted", "non-transport-type", "other-body", "no-separator", "throttle", "empty"],
)
def test_a_message_that_is_not_a_mid_stream_rendering_is_not_classified_as_one(
    message: str,
) -> None:
    assert not is_mid_stream_transport_error(message)


# --- through the real adapters --------------------------------------------------------------


@pytest.mark.parametrize("family", ["anthropic", "openai"])
@pytest.mark.parametrize(
    "exc",
    [
        httpx2.ReadError("reset"),
        httpx2.ReadTimeout("stalled"),
        httpx2.RemoteProtocolError("peer closed"),
        httpx2.ConnectError("refused"),
    ],
    ids=["ReadError", "ReadTimeout", "RemoteProtocolError", "ConnectError"],
)
def test_a_mid_stream_transport_failure_raises_once_without_a_retry(
    family: str, exc: Exception
) -> None:
    adapter, hits = _dropping_adapter(family, exc)
    counted = _CountingProvider(adapter)
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    provider = ThrottleRetryingProvider(counted, sleep=record_sleep)

    with pytest.raises(MidStreamTransportError) as raised:
        _drain(provider, _invocation(family))

    assert isinstance(raised.value, LiveInvocationError)
    assert is_mid_stream_transport_error(raised.value.error_message)
    assert raised.value.model_id == _invocation(family).model.model_id
    assert counted.streams == 1
    assert len(hits) == 1
    assert slept == []
    assert provider.drain_exclusions() == []


def test_a_mid_stream_failure_after_a_retried_throttle_keeps_the_throttle_exclusion() -> None:
    wrapped = _ScriptedProvider(
        [
            _THROTTLED_TURN,
            [
                ProviderEvent(
                    type="error",
                    error_message=mid_stream_error_message(httpx2.ReadError("reset")),
                )
            ],
        ]
    )
    provider = ThrottleRetryingProvider(wrapped, sleep=_no_sleep)

    with pytest.raises(MidStreamTransportError):
        _drain(provider, _invocation("anthropic"))

    assert wrapped.attempts == 2
    assert len(provider.drain_exclusions()) == 1


def test_a_non_transport_failure_is_still_a_plain_live_invocation_error() -> None:
    wrapped = _ScriptedProvider(
        [[ProviderEvent(type="error", error_message="ValidationException: malformed.")]]
    )
    provider = ThrottleRetryingProvider(wrapped, sleep=_no_sleep)

    with pytest.raises(LiveInvocationError) as raised:
        _drain(provider, _invocation("anthropic"))

    assert not isinstance(raised.value, MidStreamTransportError)


# --- backoff never blocks the event loop ----------------------------------------------------


def test_the_default_sleep_is_awaited_so_a_concurrent_coroutine_progresses_during_backoff() -> None:
    """While the wrapper backs off with its default sleep, another task on the loop keeps ticking."""
    wrapped = _ScriptedProvider([_THROTTLED_TURN, _SUCCESS_TURN])
    provider = ThrottleRetryingProvider(wrapped, backoff_seconds=0.2)
    ticks_during_backoff: list[int] = []

    async def _go() -> None:
        ticks = 0
        stop = asyncio.Event()

        async def ticker() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        before = ticks
        events = [event async for event in provider.stream(_invocation("anthropic"), [], [])]
        ticks_during_backoff.append(ticks - before)
        stop.set()
        await task
        assert [event.type for event in events] == ["text_delta", "usage", "completed"]

    asyncio.run(_go())

    assert wrapped.attempts == 2
    assert ticks_during_backoff[0] >= 5


def test_a_custom_async_sleep_is_awaited_with_the_exponential_backoff() -> None:
    wrapped = _ScriptedProvider([_THROTTLED_TURN, _THROTTLED_TURN, _SUCCESS_TURN])
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    provider = ThrottleRetryingProvider(wrapped, backoff_seconds=1.5, sleep=record_sleep)

    _drain(provider, _invocation("anthropic"))

    assert slept == [1.5, 3.0]


@pytest.mark.parametrize(
    "sleep",
    [lambda _seconds: None, list[float]().append, time.sleep],
    ids=["lambda", "bound-append", "time.sleep"],
)
def test_a_blocking_sleep_is_refused_at_construction(sleep: Any) -> None:
    with pytest.raises(TypeError, match="async function"):
        ThrottleRetryingProvider(_ScriptedProvider([_SUCCESS_TURN]), sleep=sleep)
