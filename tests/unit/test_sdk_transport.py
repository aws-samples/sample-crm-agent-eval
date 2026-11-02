# SPDX-License-Identifier: MIT
"""The shared SDK transport: attempt recording, deadline, lock-down, logger levels.

Retry, credential and logging assertions drive the real vendor SDK clients over an
``httpx2.MockTransport``. No network is reached and no AWS call is made.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from typing import Any

import anthropic
import httpx2
import openai
import pytest
from anthropic import AsyncAnthropicBedrock
from botocore.credentials import Credentials
from openai import AsyncBedrockOpenAI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from eval.live import is_retryable_error
from providers import sdk_transport
from providers.base import ProviderEvent, RetriedAttempt
from providers.sdk_transport import (
    MODEL_CALL_DEADLINE_SECONDS,
    AnthropicClientKwargs,
    AttemptRecord,
    AttemptRecorder,
    CredentialPolicyError,
    ModelCallDeadlineError,
    OpenAIClientKwargs,
    RecorderNotBoundError,
    anthropic_client_kwargs,
    assert_no_bearer_or_mantle,
    assert_runtime_client,
    build_http_client,
    credential_policy_cause,
    model_call_deadline,
    openai_client_kwargs,
    recording_attempts,
    render_deadline_exceeded,
    render_exhausted_transport_error,
    require_bound_recorder,
    require_credentials,
    session_credentials_provider,
)
from tests.integration._aws_eventstream import encode_chunk_event

REGION = "us-east-1"
#: The value planted in `AWS_BEARER_TOKEN_BEDROCK` to prove the SDK adopts it.
_ENV_ADOPTED_VALUE = "bearer-from-env"
RUNTIME_HOST = f"bedrock-runtime.{REGION}.amazonaws.com"
MODEL = "global.anthropic.claude-haiku-5-5"

#: The Bedrock fallback variables, written out independently of the module's own tuple.
BEDROCK_FALLBACK_ENV_VARS = (
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
)
QUIETED_LOGGERS = ("anthropic", "openai", "httpx2", "httpcore2")

Handler = Callable[[httpx2.Request], httpx2.Response]

_MESSAGE_JSON: dict[str, object] = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "ok"}],
    "model": MODEL,
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
_MODELS_JSON: dict[str, object] = {"object": "list", "data": []}


class _StaticSession:
    """A boto3-session stand-in resolving fixed fake credentials; counts resolutions."""

    region_name = REGION

    def __init__(self) -> None:
        self.calls = 0

    def get_credentials(self) -> Credentials:
        self.calls += 1
        return Credentials("AKIDEXAMPLE", "fake-secret-key")


@pytest.fixture(autouse=True)
def _offline_aws_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[None]:
    """Pin a fake static credential chain and clear every SDK fallback variable."""
    for name in (
        *BEDROCK_FALLBACK_ENV_VARS,
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret-key")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    yield
    sdk_transport.quiet_sdk_loggers()


@pytest.fixture
def anthropic_session(monkeypatch: pytest.MonkeyPatch) -> _StaticSession:
    """Replace the Anthropic SDK's cached boto3 session with a counting fake."""
    from anthropic.lib.bedrock import _auth

    session = _StaticSession()
    monkeypatch.setattr(_auth, "_get_session", lambda **_: session)
    return session


async def _no_sleep(*_: object, **__: object) -> None:
    return None


def _sequence(*steps: httpx2.Response | Exception) -> tuple[Handler, list[httpx2.Request]]:
    """Return a handler replaying ``steps`` in order, and the list of requests it saw."""
    seen: list[httpx2.Request] = []
    queue = list(steps)

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        step = queue.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    return handler, seen


def _anthropic(transport: httpx2.AsyncBaseTransport, **build: Any) -> AsyncAnthropicBedrock:
    client = AsyncAnthropicBedrock(
        **anthropic_client_kwargs(
            region=REGION, http_client=build_http_client(transport=transport, **build)
        )
    )
    client._sleep_for_retry = _no_sleep  # type: ignore[method-assign]
    return client


def _openai(
    transport: httpx2.AsyncBaseTransport, provider: Callable[[], object] | None = None
) -> AsyncBedrockOpenAI:
    client = AsyncBedrockOpenAI(
        **openai_client_kwargs(
            region=REGION,
            http_client=build_http_client(transport=transport),
            credentials_provider=provider or session_credentials_provider(_StaticSession()),
        )
    )
    client._sleep_for_retry = _no_sleep  # type: ignore[method-assign]
    return client


async def _create_message(client: AsyncAnthropicBedrock) -> None:
    await client.messages.create(
        model=MODEL, max_tokens=8, messages=[{"role": "user", "content": "hi"}]
    )


def _run_anthropic_call(*steps: httpx2.Response | Exception) -> AttemptRecorder:
    handler, _ = _sequence(*steps)
    client = _anthropic(httpx2.MockTransport(handler))

    async def go() -> AttemptRecorder:
        with recording_attempts() as recorder:
            await _create_message(client)
        return recorder

    return asyncio.run(go())


# --- AttemptRecord shape -------------------------------------------------------------------------


def test_attempt_record_holds_only_retry_count_start_status_and_error_type() -> None:
    names = [f.name for f in dataclasses.fields(AttemptRecord)]
    assert names == ["retry_count", "started_monotonic", "status", "error_type"]
    forbidden = ("header", "url", "body", "content", "request", "response", "auth")
    assert not [n for n in names if any(word in n for word in forbidden)]


def test_provider_event_retried_attempts_defaults_to_empty_tuple() -> None:
    event = ProviderEvent(type="usage", input_tokens=1, output_tokens=1)
    assert event.retried_attempts == ()
    assert event == ProviderEvent(type="usage", input_tokens=1, output_tokens=1)
    assert [f.name for f in dataclasses.fields(RetriedAttempt)] == ["status", "error_type"]


# --- Timeout and retry count ---------------------------------------------------------------------


def test_http_client_and_sdk_clients_carry_per_phase_5s_connect_120s_read_write_pool() -> None:
    expected = httpx2.Timeout(120.0, connect=5.0)
    assert expected.as_dict() == {"connect": 5.0, "read": 120.0, "write": 120.0, "pool": 120.0}
    transport = httpx2.MockTransport(lambda _: httpx2.Response(200))
    assert build_http_client(transport=transport).timeout == expected
    anthropic = _anthropic(transport)
    openai = _openai(transport)
    assert anthropic.timeout == expected and anthropic.max_retries == 2
    assert openai.timeout == expected and openai.max_retries == 2


# --- Attempt recording through the real SDK retry loop -------------------------------------------


def test_connection_error_is_recorded_at_the_request_then_retried_to_200() -> None:
    recorder = _run_anthropic_call(
        httpx2.ConnectError("refused"), httpx2.Response(200, json=_MESSAGE_JSON)
    )
    assert [(r.retry_count, r.status, r.error_type) for r in recorder.records] == [
        (0, None, "ConnectError"),
        (1, 200, None),
    ]
    assert recorder.retried_attempts() == (RetriedAttempt(None, "ConnectError"),)


@pytest.mark.parametrize("status", [429, 503])
def test_retryable_status_is_recorded_and_excluded_from_the_final_attempt(status: int) -> None:
    recorder = _run_anthropic_call(
        httpx2.Response(status, json={"message": "busy"}), httpx2.Response(200, json=_MESSAGE_JSON)
    )
    assert [(r.retry_count, r.status) for r in recorder.records] == [(0, status), (1, 200)]
    assert recorder.retried_attempts() == (RetriedAttempt(status, None),)


def test_openai_client_records_attempts_through_the_same_hooks() -> None:
    handler, _ = _sequence(httpx2.Response(500), httpx2.Response(200, json=_MODELS_JSON))
    client = _openai(httpx2.MockTransport(handler))

    async def go() -> AttemptRecorder:
        with recording_attempts() as recorder:
            await client.models.list()
        return recorder

    recorder = asyncio.run(go())
    assert [(r.retry_count, r.status) for r in recorder.records] == [(0, 500), (1, 200)]


def test_final_attempt_start_is_the_successful_attempts_clock_reading() -> None:
    ticks = iter([10.0, 20.0])
    handler, _ = _sequence(httpx2.Response(503), httpx2.Response(200, json=_MESSAGE_JSON))
    client = _anthropic(httpx2.MockTransport(handler), clock=lambda: next(ticks))

    async def go() -> AttemptRecorder:
        with recording_attempts() as recorder:
            await _create_message(client)
        return recorder

    recorder = asyncio.run(go())
    assert [r.started_monotonic for r in recorder.records] == [10.0, 20.0]
    assert recorder.final_attempt_start() == 20.0
    assert AttemptRecorder().final_attempt_start() is None


def test_concurrent_recorders_on_one_client_are_isolated() -> None:
    both_in_flight = asyncio.Event()
    calls: dict[str, int] = {"a": 0}

    async def handler(request: httpx2.Request) -> httpx2.Response:
        caller = request.headers["x-caller"]
        if caller == "a":
            calls["a"] += 1
            if calls["a"] == 1:
                await both_in_flight.wait()
                return httpx2.Response(503)
        else:
            both_in_flight.set()
        return httpx2.Response(200, json=_MESSAGE_JSON)

    client = _anthropic(httpx2.MockTransport(handler))

    async def call(caller: str) -> AttemptRecorder:
        with recording_attempts() as recorder:
            await client.messages.create(
                model=MODEL,
                max_tokens=8,
                messages=[{"role": "user", "content": "hi"}],
                extra_headers={"x-caller": caller},
            )
        return recorder

    async def go() -> tuple[AttemptRecorder, AttemptRecorder]:
        a, b = await asyncio.gather(call("a"), call("b"))
        return a, b

    a, b = asyncio.run(go())
    assert [r.status for r in a.records] == [503, 200]
    assert [r.status for r in b.records] == [200]


def test_hook_outside_a_recording_block_raises_rather_than_dropping_the_attempt() -> None:
    client = build_http_client(transport=httpx2.MockTransport(lambda _: httpx2.Response(200)))
    with pytest.raises(RecorderNotBoundError):
        asyncio.run(client.get(f"https://{RUNTIME_HOST}/"))


# --- Lock-down ------------------------------------------------------------------------------------


def _build_anthropic_kwargs() -> object:
    return anthropic_client_kwargs(region=REGION, http_client=build_http_client())


def _build_openai_kwargs() -> object:
    return openai_client_kwargs(
        region=REGION, http_client=build_http_client(), credentials_provider=lambda: None
    )


@pytest.mark.parametrize("name", BEDROCK_FALLBACK_ENV_VARS)
@pytest.mark.parametrize("build", [_build_anthropic_kwargs, _build_openai_kwargs])
def test_forbidden_env_var_refuses_client_build_even_when_empty(
    monkeypatch: pytest.MonkeyPatch, name: str, build: Callable[[], object]
) -> None:
    monkeypatch.setenv(name, "")
    with pytest.raises(CredentialPolicyError) as raised:
        build()
    assert raised.value.source == name
    assert raised.value.code == "validation_failed"


def test_control_anthropic_sdk_really_adopts_the_bearer_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = anthropic_client_kwargs(region=REGION, http_client=build_http_client())
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", _ENV_ADOPTED_VALUE)
    client = AsyncAnthropicBedrock(**kwargs)
    assert client.api_key == _ENV_ADOPTED_VALUE
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK")
    with pytest.raises(CredentialPolicyError) as raised:
        assert_runtime_client(client, region=REGION)
    assert raised.value.source == "api_key"


def test_control_openai_sdk_really_defaults_to_bedrock_mantle() -> None:
    client = AsyncBedrockOpenAI(aws_region=REGION, http_client=build_http_client())
    assert client.base_url.host == f"bedrock-mantle.{REGION}.api.aws"
    with pytest.raises(CredentialPolicyError) as raised:
        assert_runtime_client(client, region=REGION)
    assert raised.value.source == "base_url"


@pytest.mark.parametrize(
    "url",
    [
        f"https://bedrock-mantle.{REGION}.api.aws/openai/v1",
        "https://bedrock-runtime.evil.example/openai/v1",
        "https://bedrock-runtime.us-west-2.amazonaws.com",
        f"http://{RUNTIME_HOST}",
        "https://api.openai.com/v1",
    ],
)
def test_non_runtime_base_url_is_refused(url: str) -> None:
    with pytest.raises(CredentialPolicyError) as raised:
        assert_no_bearer_or_mantle(url, region=REGION)
    assert raised.value.source == "base_url"


def test_malformed_region_is_refused() -> None:
    with pytest.raises(CredentialPolicyError):
        anthropic_client_kwargs(region="us-east-1.evil.example/", http_client=build_http_client())


def test_built_clients_address_bedrock_runtime_with_no_api_key() -> None:
    transport = httpx2.MockTransport(lambda _: httpx2.Response(200))
    anthropic = _anthropic(transport)
    openai = _openai(transport)
    assert anthropic.api_key is None
    assert openai.api_key == ""
    assert anthropic.base_url.host == RUNTIME_HOST
    assert openai.base_url.host == RUNTIME_HOST
    assert openai.base_url.path.rstrip("/") == "/openai/v1"
    assert_runtime_client(anthropic, region=REGION)
    assert_runtime_client(openai, region=REGION)


#: The `ANTHROPIC_OPEN_TELEMETRY*` settings that ask the Anthropic SDK to trace request content.
_OTEL_CONTENT_ENV: dict[str, str] = {
    "ANTHROPIC_OPEN_TELEMETRY": "true",
    "ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE": "content",
    "ANTHROPIC_OPEN_TELEMETRY_PROPAGATION": "true",
}
_PROMPT_MARKER = "PROMPT-SECRET-7f3a"


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Register an in-memory SDK tracer provider as the global one and ask for content traces."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    for name, value in _OTEL_CONTENT_ENV.items():
        monkeypatch.setenv(name, value)
    return exporter


def _traced_call(client: AsyncAnthropicBedrock, seen: list[httpx2.Request]) -> None:
    async def go() -> None:
        with recording_attempts():
            await client.messages.create(
                model=MODEL, max_tokens=8, messages=[{"role": "user", "content": _PROMPT_MARKER}]
            )

    asyncio.run(go())
    assert len(seen) == 1


def test_anthropic_client_emits_no_span_or_trace_header_whatever_the_environment(
    anthropic_session: _StaticSession, span_exporter: InMemorySpanExporter
) -> None:
    """A tracer provider plus the content-mode variables still yield no span and no traceparent."""
    handler, seen = _sequence(httpx2.Response(200, json=_MESSAGE_JSON))
    client = _anthropic(httpx2.MockTransport(handler))
    assert client.open_telemetry["traces"]["enabled"] is False
    _traced_call(client, seen)
    assert span_exporter.get_finished_spans() == ()
    assert "traceparent" not in seen[0].headers
    assert_runtime_client(client, region=REGION)


def test_control_anthropic_sdk_really_traces_content_without_the_lock(
    anthropic_session: _StaticSession, span_exporter: InMemorySpanExporter
) -> None:
    """Without ``open_telemetry=False`` the same setup records the prompt and sends traceparent."""
    kwargs: dict[str, Any] = {
        **anthropic_client_kwargs(region=REGION, http_client=build_http_client())
    }
    del kwargs["open_telemetry"]
    handler, seen = _sequence(httpx2.Response(200, json=_MESSAGE_JSON))
    kwargs["http_client"] = build_http_client(transport=httpx2.MockTransport(handler))
    client = AsyncAnthropicBedrock(**kwargs)
    _traced_call(client, seen)
    spans = span_exporter.get_finished_spans()
    assert spans
    recorded = [str(v) for span in spans for v in (span.attributes or {}).values()]
    assert any(_PROMPT_MARKER in value for value in recorded)
    assert "traceparent" in seen[0].headers
    with pytest.raises(CredentialPolicyError) as raised:
        assert_runtime_client(client, region=REGION)
    assert raised.value.source == "open_telemetry"


def _openai_with_token_provider(transport: httpx2.AsyncBaseTransport) -> AsyncBedrockOpenAI:
    """An OpenAI Bedrock client on the repo's kwargs, but authenticating with a bearer provider."""

    async def token() -> str:
        return "BEARER-FROM-PROVIDER"

    kwargs: dict[str, Any] = {
        **openai_client_kwargs(
            region=REGION,
            http_client=build_http_client(transport=transport),
            credentials_provider=session_credentials_provider(_StaticSession()),
        )
    }
    del kwargs["aws_credentials_provider"]
    return AsyncBedrockOpenAI(**kwargs, bedrock_token_provider=token)


def test_openai_client_with_a_bedrock_token_provider_is_refused() -> None:
    client = _openai_with_token_provider(httpx2.MockTransport(lambda _: httpx2.Response(200)))
    assert client.api_key == ""
    with pytest.raises(CredentialPolicyError) as raised:
        assert_runtime_client(client, region=REGION)
    assert raised.value.source == "auth_mode"


def test_control_openai_token_provider_client_really_sends_a_bearer() -> None:
    """The refused mode is a real bearer: the request carries ``Authorization: Bearer``."""
    handler, seen = _sequence(httpx2.Response(200, json=_MODELS_JSON))
    client = _openai_with_token_provider(httpx2.MockTransport(handler))

    async def go() -> None:
        with recording_attempts():
            await client.models.list()

    asyncio.run(go())
    assert seen[0].headers["authorization"] == "Bearer BEARER-FROM-PROVIDER"


def test_openai_client_without_the_sigv4_predicate_is_refused() -> None:
    client = _openai(httpx2.MockTransport(lambda _: httpx2.Response(200)))
    client._uses_aws_auth = None  # type: ignore[assignment,method-assign]
    with pytest.raises(CredentialPolicyError) as raised:
        assert_runtime_client(client, region=REGION)
    assert raised.value.source == "auth_mode"


def test_kwargs_builders_expose_no_api_key_or_static_key_field() -> None:
    assert set(AnthropicClientKwargs.__annotations__) == {
        "aws_region",
        "base_url",
        "http_client",
        "max_retries",
        "timeout",
        "default_headers",
        "open_telemetry",
        "aws_profile",
    }
    assert set(OpenAIClientKwargs.__annotations__) == {
        "aws_region",
        "base_url",
        "aws_credentials_provider",
        "http_client",
        "max_retries",
        "timeout",
    }
    built = {**anthropic_client_kwargs(region=REGION, http_client=build_http_client())}
    assert "aws_profile" not in built
    assert built["open_telemetry"] is False
    profiled = anthropic_client_kwargs(
        region=REGION, http_client=build_http_client(), profile="named"
    )
    assert profiled.get("aws_profile") == "named"


def test_both_sdks_sign_sigv4_and_never_send_a_bearer(
    anthropic_session: _StaticSession,
) -> None:
    anthropic_handler, anthropic_seen = _sequence(httpx2.Response(200, json=_MESSAGE_JSON))
    openai_handler, openai_seen = _sequence(httpx2.Response(200, json=_MODELS_JSON))
    anthropic = _anthropic(httpx2.MockTransport(anthropic_handler))
    openai = _openai(httpx2.MockTransport(openai_handler))

    async def go() -> None:
        with recording_attempts():
            await _create_message(anthropic)
            await openai.models.list()

    asyncio.run(go())
    for request in (*anthropic_seen, *openai_seen):
        assert request.url.host == RUNTIME_HOST
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert "/bedrock/aws4_request" in anthropic_seen[0].headers["authorization"]
    assert "/bedrock/aws4_request" in openai_seen[0].headers["authorization"]


HOP_BY_HOP_HEADERS = (
    "connection",
    "keep-alive",
    "te",
    "transfer-encoding",
    "upgrade",
    "proxy-connection",
    "trailer",
)


def _signed_headers(request: httpx2.Request) -> set[str]:
    authorization = request.headers["authorization"]
    field = next(p for p in authorization.split(", ") if p.startswith("SignedHeaders="))
    return set(field.removeprefix("SignedHeaders=").split(";"))


def test_http_client_sends_no_default_connection_header() -> None:
    assert "connection" not in build_http_client().headers


def test_neither_sdk_sends_or_signs_a_hop_by_hop_header(
    anthropic_session: _StaticSession,
) -> None:
    anthropic_handler, anthropic_seen = _sequence(httpx2.Response(200, json=_MESSAGE_JSON))
    openai_handler, openai_seen = _sequence(httpx2.Response(200, json=_MODELS_JSON))
    anthropic = _anthropic(httpx2.MockTransport(anthropic_handler))
    openai = _openai(httpx2.MockTransport(openai_handler))

    async def go() -> None:
        with recording_attempts():
            await _create_message(anthropic)
            await openai.models.list()

    asyncio.run(go())
    for family, request in (("anthropic", anthropic_seen[0]), ("openai", openai_seen[0])):
        signed = _signed_headers(request)
        assert "host" in signed and "x-amz-date" in signed, (family, signed)
        assert not signed & set(HOP_BY_HOP_HEADERS), (family, sorted(signed))
    assert not set(openai_seen[0].headers) & set(HOP_BY_HOP_HEADERS)
    assert set(anthropic_seen[0].headers) & set(HOP_BY_HOP_HEADERS) == {"connection"}


# --- Credential resolution per request ------------------------------------------------------------


def test_anthropic_sdk_resolves_session_credentials_on_every_request(
    anthropic_session: _StaticSession,
) -> None:
    handler, _ = _sequence(
        httpx2.Response(200, json=_MESSAGE_JSON), httpx2.Response(200, json=_MESSAGE_JSON)
    )
    client = _anthropic(httpx2.MockTransport(handler))

    async def go() -> None:
        with recording_attempts():
            await _create_message(client)
            await _create_message(client)

    asyncio.run(go())
    assert anthropic_session.calls == 2


def test_openai_credentials_provider_is_called_on_every_request() -> None:
    session = _StaticSession()
    handler, _ = _sequence(
        httpx2.Response(200, json=_MODELS_JSON), httpx2.Response(200, json=_MODELS_JSON)
    )
    client = _openai(httpx2.MockTransport(handler), session_credentials_provider(session))

    async def go() -> None:
        with recording_attempts():
            await client.models.list()
            await client.models.list()

    asyncio.run(go())
    assert session.calls == 2


def test_session_credentials_provider_refuses_an_empty_chain() -> None:
    class _Empty:
        def get_credentials(self) -> None:
            return None

    with pytest.raises(CredentialPolicyError):
        session_credentials_provider(_Empty())()


# --- SDK loggers ----------------------------------------------------------------------------------


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _sdk_records_below_warning(records: list[logging.LogRecord]) -> list[str]:
    return [
        f"{r.name}:{r.levelname}"
        for r in records
        if r.levelno < logging.WARNING
        and any(r.name == n or r.name.startswith(n + ".") for n in QUIETED_LOGGERS)
    ]


@pytest.fixture
def capture() -> Iterator[_Capture]:
    """Capture every record reaching the root logger, with the root lowered to DEBUG."""
    handler = _Capture()
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def _retrying_call_through_both_sdks() -> None:
    anthropic_handler, _ = _sequence(httpx2.Response(503), httpx2.Response(200, json=_MESSAGE_JSON))
    openai_handler, _ = _sequence(httpx2.Response(503), httpx2.Response(200, json=_MODELS_JSON))
    anthropic = _anthropic(httpx2.MockTransport(anthropic_handler))
    openai = _openai(httpx2.MockTransport(openai_handler))

    async def go() -> None:
        with recording_attempts():
            await _create_message(anthropic)
            await openai.models.list()

    asyncio.run(go())


def test_no_sdk_record_below_warning_is_emitted_during_a_mocked_call(capture: _Capture) -> None:
    _retrying_call_through_both_sdks()
    assert _sdk_records_below_warning(capture.records) == []


def test_control_capture_sees_sdk_debug_records_when_the_level_is_lowered(
    capture: _Capture,
) -> None:
    logging.getLogger("httpx2").setLevel(logging.DEBUG)
    logging.getLogger("httpx2").debug("control record")
    assert "httpx2:DEBUG" in _sdk_records_below_warning(capture.records)

    async def go() -> None:
        handler, _ = _sequence(httpx2.Response(503), httpx2.Response(200, json=_MESSAGE_JSON))
        client = _anthropic(httpx2.MockTransport(handler))
        for name in QUIETED_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        with recording_attempts():
            await _create_message(client)

    asyncio.run(go())
    lowered = _sdk_records_below_warning(capture.records)
    assert any(entry.startswith("anthropic.") for entry in lowered)


def test_client_build_restores_warning_after_an_sdk_lowered_the_level() -> None:
    for name in QUIETED_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    anthropic_client_kwargs(region=REGION, http_client=build_http_client())
    assert {logging.getLogger(n).level for n in QUIETED_LOGGERS} == {logging.WARNING}


# --- Exhausted-error rendering --------------------------------------------------------------------

_STATUS_BY_CLASS = {
    "BadRequestError": 400,
    "AuthenticationError": 401,
    "PermissionDeniedError": 403,
    "NotFoundError": 404,
    "ConflictError": 409,
    "RequestTooLargeError": 413,
    "UnprocessableEntityError": 422,
    "RateLimitError": 429,
    "InternalServerError": 500,
    "ServiceUnavailableError": 503,
    "DeadlineExceededError": 504,
    "OverloadedError": 529,
}
_SDK_REQUEST = httpx2.Request("POST", f"https://{RUNTIME_HOST}/")


def _exhausted_exceptions() -> Iterator[BaseException]:
    for sdk in (anthropic, openai):
        for cls in (sdk.APIStatusError, *sdk.APIStatusError.__subclasses__()):
            status = _STATUS_BY_CLASS.get(cls.__name__, 500)
            response = httpx2.Response(status, request=_SDK_REQUEST)
            yield cls("boom", response=response, body=None)
        yield sdk.APITimeoutError(request=_SDK_REQUEST)
        yield sdk.APIConnectionError(request=_SDK_REQUEST)


def test_every_exhausted_sdk_exception_renders_to_a_non_retryable_message() -> None:
    base_names = {"APIStatusError", "APITimeoutError", "APIConnectionError"}
    exceptions = list(_exhausted_exceptions())
    assert len(exceptions) >= 2 * (8 + 1 + 2)
    for exc in exceptions:
        name = f"{type(exc).__module__}.{type(exc).__name__}"
        message = render_exhausted_transport_error(exc)
        assert message.startswith("The model provider request failed after SDK retries: "), name
        assert not is_retryable_error(message), (name, message)
        assert "http_status=" not in message, name
        if type(exc).__name__ not in base_names:
            assert type(exc).__name__ not in message, (name, message)


def test_exhausted_rendering_names_the_base_class_and_status() -> None:
    response = httpx2.Response(503, request=_SDK_REQUEST)
    assert render_exhausted_transport_error(
        anthropic.ServiceUnavailableError("x", response=response, body=None)
    ) == ("The model provider request failed after SDK retries: APIStatusError(status=503).")
    assert render_exhausted_transport_error(openai.APITimeoutError(request=_SDK_REQUEST)) == (
        "The model provider request failed after SDK retries: APITimeoutError."
    )
    assert render_exhausted_transport_error(anthropic.APIConnectionError(request=_SDK_REQUEST)) == (
        "The model provider request failed after SDK retries: APIConnectionError."
    )


def _sdk_exception_class_names() -> set[str]:
    """Every exception class either installed SDK exports, derived rather than listed."""
    return {
        name
        for sdk in (anthropic, openai)
        for name in dir(sdk)
        if isinstance(getattr(sdk, name), type) and issubclass(getattr(sdk, name), BaseException)
    }


def test_the_default_model_call_deadline_is_never_shorter_than_the_turn_cap() -> None:
    """The default model-call deadline is never shorter than the turn cap."""
    from agents.budget import EVAL_WALL_CLOCK_SECONDS

    assert MODEL_CALL_DEADLINE_SECONDS >= EVAL_WALL_CLOCK_SECONDS


def test_control_no_sdk_exception_class_name_is_a_retryable_prefix() -> None:
    """No installed SDK exception class name is a retryable prefix."""
    names = _sdk_exception_class_names()
    assert {"InternalServerError", "ServiceUnavailableError", "RateLimitError"} <= names
    retried = sorted(name for name in names if is_retryable_error(f"{name}: failed"))
    assert retried == []


# --- Whole-call wall-clock deadline ---------------------------------------------------------------


class _Empty:
    """A boto3-session stand-in whose credential chain resolves nothing."""

    def get_credentials(self) -> None:
        return None


#: Bounds the trickling stream, so a missing deadline ends it normally instead of hanging the suite.
_TRICKLE_CHUNKS = 200
_TRICKLE_INTERVAL_SECONDS = 0.01
#: The trickle test's budget: wide enough that host load cannot spend it before the first delta,
#: and at most half the stream's natural length, so the stream cannot end on its own first.
_TRICKLE_DEADLINE_SECONDS = 1.0


def _trickling_stream_handler(
    closed: list[bool], interval: float
) -> Callable[[httpx2.Request], httpx2.Response]:
    """Answer an Anthropic stream open with ``message_start``, then a text delta every ``interval``.

    Each read completes well inside the per-phase timeout, so only a whole-call bound ends it early.
    ``closed`` gains an entry when the SDK closes the response body.
    """

    def handler(_request: httpx2.Request) -> httpx2.Response:
        async def body() -> AsyncIterator[bytes]:
            try:
                yield encode_chunk_event(
                    {
                        "type": "message_start",
                        "message": {**_MESSAGE_JSON, "content": [], "stop_reason": None},
                    }
                )
                yield encode_chunk_event(
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    }
                )
                for _ in range(_TRICKLE_CHUNKS):
                    await asyncio.sleep(interval)
                    yield encode_chunk_event(
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": "."},
                        }
                    )
                yield encode_chunk_event({"type": "content_block_stop", "index": 0})
                yield encode_chunk_event({"type": "message_stop"})
            finally:
                closed.append(True)

        return httpx2.Response(
            200, headers={"content-type": "application/vnd.amazon.eventstream"}, content=body()
        )

    return handler


def test_the_production_deadline_is_the_120s_turn_budget() -> None:
    async def go() -> float:
        async with model_call_deadline() as deadline:
            return deadline.seconds

    assert MODEL_CALL_DEADLINE_SECONDS == 120.0
    assert asyncio.run(go()) == 120.0


def test_deadline_ends_a_stream_whose_every_read_is_within_the_per_phase_timeout(
    anthropic_session: _StaticSession,
) -> None:
    """Deadline expiry mid-stream closes the stream through the real SDK."""
    assert _TRICKLE_CHUNKS * _TRICKLE_INTERVAL_SECONDS >= 2 * _TRICKLE_DEADLINE_SECONDS
    closed: list[bool] = []
    handler = _trickling_stream_handler(closed, interval=_TRICKLE_INTERVAL_SECONDS)
    client = _anthropic(httpx2.MockTransport(handler))
    events: list[str] = []

    async def go() -> tuple[AttemptRecorder, ModelCallDeadlineError]:
        async with model_call_deadline(seconds=_TRICKLE_DEADLINE_SECONDS) as deadline:
            manager = client.messages.stream(
                model=MODEL, max_tokens=8, messages=[{"role": "user", "content": "hi"}]
            )
            with recording_attempts() as recorder:
                require_bound_recorder()
                stream = await deadline.run(manager.__aenter__())
            try:
                async for event in deadline.iterate(stream):
                    events.append(event.type)
            except ModelCallDeadlineError as exc:
                return recorder, exc
            finally:
                await manager.__aexit__(None, None, None)
        raise AssertionError("the deadline never fired")

    recorder, exc = asyncio.run(go())
    assert events[0] == "message_start"
    assert 1 <= events.count("content_block_delta") < _TRICKLE_CHUNKS
    assert "message_stop" not in events
    assert closed == [True]
    assert [(r.retry_count, r.status) for r in recorder.records] == [(0, 200)]
    assert exc.seconds == _TRICKLE_DEADLINE_SECONDS


def test_deadline_spans_every_sdk_retry_not_each_attempt() -> None:
    """The deadline spans every SDK retry: three 0.5s attempts cannot finish in a 1s budget."""
    attempts: list[int] = []

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        attempts.append(1)
        await asyncio.sleep(0.5)
        return httpx2.Response(503, json={"message": "busy"})

    client = _anthropic(httpx2.MockTransport(handler))

    async def go() -> AttemptRecorder:
        async with model_call_deadline(seconds=1.0) as deadline:
            with recording_attempts() as recorder, pytest.raises(ModelCallDeadlineError):
                await deadline.run(_create_message(client))
        return recorder

    recorder = asyncio.run(go())
    assert len(attempts) in (2, 3)
    assert [r.status for r in recorder.records] == [503] * (len(attempts) - 1) + [None]


def test_an_already_spent_budget_never_starts_the_awaitable() -> None:
    ticks = iter([0.0, 121.0])
    started: list[bool] = []

    async def call() -> None:
        started.append(True)

    async def go() -> None:
        async with model_call_deadline(clock=lambda: next(ticks)) as deadline:
            await deadline.run(call())

    with pytest.raises(ModelCallDeadlineError):
        asyncio.run(go())
    assert started == []


def test_a_timeout_error_raised_inside_the_budget_is_not_reported_as_the_deadline() -> None:
    async def call() -> None:
        raise TimeoutError("an SDK-internal timeout")

    async def go() -> None:
        async with model_call_deadline(seconds=10.0) as deadline:
            await deadline.run(call())

    with pytest.raises(TimeoutError, match="SDK-internal") as raised:
        asyncio.run(go())
    assert not isinstance(raised.value, ModelCallDeadlineError)


def test_a_consumer_await_is_never_cancelled_while_the_adapter_is_suspended() -> None:
    """No timeout scope spans a yield: the consumer outlives the budget undisturbed between items."""

    async def items() -> AsyncIterator[int]:
        yield 1
        yield 2

    async def go() -> list[int]:
        received: list[int] = []
        async with model_call_deadline(seconds=0.05) as deadline:
            iterator = deadline.iterate(items())
            received.append(await anext(iterator))
            await asyncio.sleep(0.15)
            with pytest.raises(ModelCallDeadlineError):
                await anext(iterator)
        return received

    assert asyncio.run(go()) == [1]


def test_deadline_rendering_is_marker_free_and_never_retried() -> None:
    message = render_deadline_exceeded(ModelCallDeadlineError(120.0))
    assert message == "The model provider call exceeded its 120s wall-clock deadline."
    assert not is_retryable_error(message)
    assert not is_retryable_error(str(ModelCallDeadlineError(120.0)))


# --- Unbound recorder through each SDK ------------------------------------------------------------


def test_require_bound_recorder_refuses_outside_a_block_and_returns_the_bound_one() -> None:
    with pytest.raises(RecorderNotBoundError):
        require_bound_recorder()
    with recording_attempts() as recorder:
        assert require_bound_recorder() is recorder


def test_anthropic_sdk_wraps_an_unbound_recorder_as_a_connection_error_with_no_request_sent(
    anthropic_session: _StaticSession,
) -> None:
    handler, seen = _sequence(httpx2.Response(200, json=_MESSAGE_JSON))
    client = _anthropic(httpx2.MockTransport(handler))
    with pytest.raises(anthropic.APIConnectionError) as raised:
        asyncio.run(_create_message(client))
    assert isinstance(raised.value.__cause__, RecorderNotBoundError)
    assert seen == []


def test_openai_sdk_propagates_an_unbound_recorder_with_no_request_sent() -> None:
    handler, seen = _sequence(httpx2.Response(200, json=_MODELS_JSON))
    client = _openai(httpx2.MockTransport(handler))

    async def go() -> None:
        await client.models.list()

    with pytest.raises(RecorderNotBoundError):
        asyncio.run(go())
    assert seen == []


def test_unbound_send_failure_reraises_the_original_exception_unmasked() -> None:
    client = build_http_client(transport=httpx2.MockTransport(lambda _: httpx2.Response(200)))
    with pytest.raises(RecorderNotBoundError) as raised:
        asyncio.run(client.get(f"https://{RUNTIME_HOST}/"))
    assert raised.value.__context__ is None


# --- Family-aware api_key post-check --------------------------------------------------------------


def test_control_anthropic_empty_string_bearer_env_var_is_refused_after_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = anthropic_client_kwargs(region=REGION, http_client=build_http_client())
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "")
    client = AsyncAnthropicBedrock(**kwargs)
    assert client.api_key == ""
    with pytest.raises(CredentialPolicyError) as raised:
        assert_runtime_client(client, region=REGION)
    assert raised.value.source == "api_key"


def test_an_unrecognised_client_is_refused() -> None:
    with pytest.raises(CredentialPolicyError) as raised:
        assert_runtime_client(object(), region=REGION)
    assert raised.value.source == "client"


# --- Recorder scope across an async-generator yield -----------------------------------------------


async def _documented_pattern() -> AsyncGenerator[int]:
    with recording_attempts() as recorder:
        require_bound_recorder()
    yield len(recorder.records)
    yield 0


async def _forbidden_pattern() -> AsyncGenerator[int]:
    with recording_attempts() as recorder:
        yield len(recorder.records)
        yield 0


def test_documented_pattern_survives_suspension_and_finalization_in_another_task() -> None:
    async def go() -> None:
        generator = _documented_pattern()
        assert await anext(generator) == 0
        with pytest.raises(RecorderNotBoundError):
            require_bound_recorder()
        await asyncio.create_task(generator.aclose())

    asyncio.run(go())


def test_control_a_block_spanning_a_yield_leaks_to_the_consumer_and_breaks_finalization() -> None:
    async def go() -> None:
        generator = _forbidden_pattern()
        await anext(generator)
        assert isinstance(require_bound_recorder(), AttemptRecorder)
        with pytest.raises(ValueError, match="different Context"):
            await asyncio.create_task(generator.aclose())

    asyncio.run(go())


# --- Credential failure shapes and the pre-flight -------------------------------------------------


def test_openai_sdk_surfaces_an_empty_chain_as_openai_error_caused_by_the_policy_error() -> None:
    handler, seen = _sequence(httpx2.Response(200, json=_MODELS_JSON))
    client = _openai(httpx2.MockTransport(handler), session_credentials_provider(_Empty()))

    async def go() -> None:
        with recording_attempts():
            await client.models.list()

    with pytest.raises(openai.OpenAIError) as raised:
        asyncio.run(go())
    assert not isinstance(raised.value, openai.APIError)
    assert isinstance(raised.value.__cause__, CredentialPolicyError)
    assert credential_policy_cause(raised.value) is raised.value.__cause__
    assert seen == []


def test_anthropic_sdk_surfaces_an_empty_chain_as_a_bare_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from anthropic.lib.bedrock import _auth

    monkeypatch.setattr(_auth, "_get_session", lambda **_: _Empty())
    handler, seen = _sequence(httpx2.Response(200, json=_MESSAGE_JSON))
    client = _anthropic(httpx2.MockTransport(handler))

    async def go() -> None:
        with recording_attempts():
            await _create_message(client)

    with pytest.raises(RuntimeError, match="could not resolve credentials") as raised:
        asyncio.run(go())
    assert type(raised.value) is RuntimeError
    assert credential_policy_cause(raised.value) is None
    assert seen == []


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_require_credentials_refuses_an_empty_chain_before_any_request(
    monkeypatch: pytest.MonkeyPatch, family: str
) -> None:
    from anthropic.lib.bedrock import _auth

    monkeypatch.setattr(_auth, "_get_session", lambda **_: _Empty())
    anthropic_handler, anthropic_seen = _sequence(httpx2.Response(200, json=_MESSAGE_JSON))
    openai_handler, openai_seen = _sequence(httpx2.Response(200, json=_MODELS_JSON))
    anthropic_client = _anthropic(httpx2.MockTransport(anthropic_handler))
    openai_client = _openai(
        httpx2.MockTransport(openai_handler), session_credentials_provider(_Empty())
    )

    async def go() -> None:
        require_credentials(_Empty())
        with recording_attempts():
            if family == "anthropic":
                await _create_message(anthropic_client)
            else:
                await openai_client.models.list()

    with pytest.raises(CredentialPolicyError) as raised:
        asyncio.run(go())
    assert raised.value.source == "boto3 credential chain"
    assert raised.value.code == "validation_failed"
    assert anthropic_seen == [] and openai_seen == []


def test_require_credentials_passes_a_resolving_session() -> None:
    session = _StaticSession()
    require_credentials(session)
    assert session.calls == 1


def test_credential_policy_cause_is_none_for_an_unrelated_failure() -> None:
    assert credential_policy_cause(RuntimeError("x")) is None
