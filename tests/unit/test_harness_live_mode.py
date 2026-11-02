# SPDX-License-Identifier: MIT
"""Tests for the harness's live mode: the pairing guard, the family seam, and exclusion accounting.

Every test is offline. The wrapper's retry split is driven through the real adapters and vendor
SDKs over an ``httpx2.MockTransport`` replaying the recorded wire fixtures, so the SDK's retry loop,
the adapters' error prefixes and the wrapper's classification are all exercised. No network is
reached and no credential is read.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import anthropic
import boto3
import httpx2
import openai
import pytest
from anthropic.lib.bedrock import _auth
from botocore.credentials import Credentials
from moto import mock_aws

from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import harness
from eval.harness import ArmSpec, LiveSubstratePairingError, main, run_sweep
from eval.live import (
    LiveInvocationError,
    ThrottleRetryingProvider,
    build_live_provider,
    is_retryable_error,
)
from eval.metrics import ExclusionCategory
from eval.tenancy import find_orphaned_runs
from providers.anthropic_adapter import AnthropicAdapter
from providers.base import (
    ModelProvider,
    ProviderEvent,
    ResolvedInvocation,
    RetriedAttempt,
    Tuning,
    validate_tuning,
)
from providers.capabilities import load_capability_matrix
from providers.factory import build_provider_for_family
from providers.openai_adapter import OpenAIAdapter
from providers.sdk_transport import ModelCallDeadlineError, render_deadline_exceeded
from tests.integration._aws_eventstream import load_wire_fixture, wire_body


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _invocation(family: str = "anthropic") -> ResolvedInvocation:
    matrix = load_capability_matrix()
    model = matrix.model_for(family, "supervisor")  # type: ignore[arg-type]
    return validate_tuning(model, Tuning(source="test", effort="low"))


class _ScriptedProvider(ModelProvider):
    """Yields a scripted sequence of turns: each entry is that attempt's event list."""

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


def _drain(provider: ModelProvider, invocation: ResolvedInvocation) -> list[ProviderEvent]:
    async def _go() -> list[ProviderEvent]:
        return [event async for event in provider.stream(invocation, [], [])]

    return asyncio.run(_go())


_SUCCESS_TURN = [
    ProviderEvent(type="text_delta", text="ok"),
    ProviderEvent(type="usage", input_tokens=10, output_tokens=2),
    ProviderEvent(type="completed", input_tokens=10, output_tokens=2),
]
_THROTTLED_TURN = [
    ProviderEvent(type="error", error_message="ThrottlingException: Too many requests.")
]
_VALIDATION_FAILED_TURN = [
    ProviderEvent(type="error", error_message="ValidationException: The request was malformed.")
]


# --------------------------------------------------------------------------------------
# The family seam: one lookup, no vendor key anywhere on the path
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_build_provider_for_family_returns_a_provider_for_every_configured_family(
    family: str,
) -> None:
    provider = build_provider_for_family(family, region_name="us-east-1")  # type: ignore[arg-type]

    assert isinstance(provider, ModelProvider)


def test_build_provider_for_family_refuses_an_unregistered_family_rather_than_substituting_one() -> (
    None
):
    """An unregistered family is refused rather than substituted with another family's model."""
    with pytest.raises(ValueError, match="no live adapter is registered"):
        build_provider_for_family("mistral", region_name="us-east-1")  # type: ignore[arg-type]


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_harness_live_provider_is_wrapped_for_retry_so_throttles_are_tallied(family: str) -> None:
    provider = harness._build_provider("live", family=family)  # type: ignore[arg-type]

    assert isinstance(provider, ThrottleRetryingProvider)


def test_every_model_in_the_matrix_carries_a_global_prefixed_inference_profile() -> None:
    """Every model in the matrix carries a ``global.``-prefixed inference profile id."""
    matrix = load_capability_matrix()

    for model in matrix.models.values():
        assert model.inference_profile == f"global.{model.model_id}"


# --------------------------------------------------------------------------------------
# --live must be paired with the live-AWS substrate, or refused
# --------------------------------------------------------------------------------------


def test_live_mode_with_the_offline_dispatcher_is_refused_before_the_tenant_is_seeded(
    store: Store,
) -> None:
    """An unpaired live sweep is refused before ``record_run_started``/``seed_tenant``, leaving no
    run row and no fixture data."""
    with pytest.raises(LiveSubstratePairingError, match="requires the live-AWS substrate"):
        run_sweep(store=store, mode="live", arm_specs=(ArmSpec(family="anthropic"),))

    assert find_orphaned_runs(store, older_than=timedelta(0)) == []


def test_live_mode_with_the_live_dispatcher_passes_the_pairing_guard() -> None:
    """The pairing guard accepts the live dispatcher without invoking anything."""
    harness._assert_live_mode_is_paired_with_the_live_substrate(
        "live", harness.live_lambda_dispatch_factory
    )


def test_stub_mode_is_never_constrained_by_the_pairing_guard() -> None:
    harness._assert_live_mode_is_paired_with_the_live_substrate(
        "stub", harness.in_process_dispatch_factory
    )


def test_cli_refuses_live_without_the_live_substrate_and_exits_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--live`` alone exits non-zero *before* building a store or a provider, so no credential is
    resolved and no model is invoked."""
    monkeypatch.delenv("EVAL_SUBSTRATE", raising=False)

    exit_code = main(["--live", "--output-dir", str(tmp_path)])

    assert exit_code == 2
    assert "requires the live-AWS substrate" in capsys.readouterr().err


# --------------------------------------------------------------------------------------
# A transient failure is a categorised exclusion; anything else stops the sweep
# --------------------------------------------------------------------------------------


def test_a_clean_turn_is_passed_through_with_no_exclusion_recorded() -> None:
    wrapped = _ScriptedProvider([_SUCCESS_TURN])
    provider = ThrottleRetryingProvider(wrapped, sleep=lambda _seconds: None)

    events = _drain(provider, _invocation())

    assert [event.type for event in events] == ["text_delta", "usage", "completed"]
    assert wrapped.attempts == 1
    assert provider.drain_exclusions() == []


def test_a_throttled_turn_is_retried_and_tallied_once_as_a_throttle() -> None:
    """One retried attempt is exactly one exclusion, categorised by its cause."""
    wrapped = _ScriptedProvider([_THROTTLED_TURN, _SUCCESS_TURN])
    slept: list[float] = []
    provider = ThrottleRetryingProvider(wrapped, sleep=slept.append)

    events = _drain(provider, _invocation())

    assert [event.type for event in events] == ["text_delta", "usage", "completed"]
    assert wrapped.attempts == 2
    categories = [exclusion.category for exclusion in provider.drain_exclusions()]
    assert categories == [ExclusionCategory.THROTTLE]
    assert slept == [2.0]


def test_a_throttle_that_exhausts_every_attempt_stops_the_sweep_rather_than_shrinking_it() -> None:
    """A throttle exhausting every attempt raises; only the retried attempt is an exclusion, the
    final non-retried one is not."""
    wrapped = _ScriptedProvider([_THROTTLED_TURN])
    provider = ThrottleRetryingProvider(wrapped, max_attempts=2, sleep=lambda _seconds: None)

    with pytest.raises(LiveInvocationError, match="failed on attempt 2 of 2"):
        _drain(provider, _invocation())

    assert wrapped.attempts == 2
    assert [exclusion.category for exclusion in provider.drain_exclusions()] == [
        ExclusionCategory.THROTTLE
    ]


def test_a_non_transient_failure_raises_immediately_and_is_not_miscategorised() -> None:
    """A ``ValidationException`` raises immediately and is not tallied as a cold start, a retry,
    or a throttle."""
    wrapped = _ScriptedProvider([_VALIDATION_FAILED_TURN])
    provider = ThrottleRetryingProvider(wrapped, sleep=lambda _seconds: None)

    with pytest.raises(LiveInvocationError, match="not a categorisable exclusion"):
        _drain(provider, _invocation())

    assert wrapped.attempts == 1
    assert provider.drain_exclusions() == []


def test_max_attempts_below_one_is_refused_rather_than_invoking_nothing() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ThrottleRetryingProvider(_ScriptedProvider([_SUCCESS_TURN]), max_attempts=0)


# --------------------------------------------------------------------------------------
# Two retry layers, driven through the real adapters and SDKs over MockTransport
# --------------------------------------------------------------------------------------

_EVENTSTREAM = "application/vnd.amazon.eventstream"
_SSE = "text/event-stream"
_REGION = "us-east-1"


class _StaticSession:
    """A boto3-session stand-in resolving fixed fake credentials; the Anthropic signer reads its
    ``region_name``."""

    region_name = _REGION

    def get_credentials(self) -> Credentials:
        return Credentials("test-access-key-id", "test-signing-secret")


class _EmptySession:
    """A boto3-session stand-in whose credential chain resolves nothing."""

    def get_credentials(self) -> None:
        return None


async def _no_sleep(*_: object, **__: object) -> None:
    return None


@pytest.fixture(autouse=True)
def _offline_sdks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake credentials for the Anthropic signer, no SDK retry sleeps, no refused Bedrock env var."""
    for name in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_BEDROCK_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_auth, "_get_session", lambda **_: _StaticSession())
    monkeypatch.setattr(anthropic.AsyncAnthropicBedrock, "_sleep_for_retry", _no_sleep)
    monkeypatch.setattr(openai.AsyncBedrockOpenAI, "_sleep_for_retry", _no_sleep)


def _fixture(name: str) -> httpx2.Response:
    fixture = load_wire_fixture(f"{name}.json")
    return httpx2.Response(
        200,
        content=wire_body(fixture),
        headers={"content-type": fixture["response"]["content_type"]},
    )


class _Wire:
    """A MockTransport replaying ``steps`` in order and counting the requests that reached it."""

    def __init__(self, *steps: httpx2.Response) -> None:
        self.hits = 0
        self._steps = list(steps)
        self.transport = httpx2.MockTransport(self._handle)

    def _handle(self, _request: httpx2.Request) -> httpx2.Response:
        self.hits += 1
        return self._steps.pop(0)


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


def _real_adapter(family: str, wire: _Wire, **kwargs: Any) -> ModelProvider:
    session = kwargs.pop("session", _StaticSession())
    adapter_cls: Callable[..., ModelProvider] = (
        AnthropicAdapter if family == "anthropic" else OpenAIAdapter
    )
    return adapter_cls(region_name=_REGION, session=session, transport=wire.transport, **kwargs)


def _live(
    family: str, wire: _Wire, **kwargs: Any
) -> tuple[ThrottleRetryingProvider, _CountingProvider]:
    counted = _CountingProvider(_real_adapter(family, wire, **kwargs))
    return ThrottleRetryingProvider(counted, sleep=lambda _s: None), counted


def _run_live(family: str, provider: ModelProvider) -> list[ProviderEvent]:
    async def collect() -> list[ProviderEvent]:
        stream = provider.stream(_invocation(family), [{"role": "user", "content": "hi"}], [])
        return [event async for event in stream]

    return asyncio.run(collect())


_SUCCESS_FIXTURE = {
    "anthropic": "anthropic_invoke_stream_opus55_success",
    "openai": "openai_responses_luna_success",
}
_IN_BAND_FIXTURE = {
    "anthropic": "anthropic_invoke_stream_synth_model_stream_error",
    "openai": "openai_responses_luna_live_server_error",
}


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_a_503_is_retried_by_the_sdk_only_never_by_the_wrapper(family: str) -> None:
    """The SDK makes both transport attempts inside one wrapper attempt, and the retried 503 is
    tallied as one RETRY from ``retried_attempts``."""
    wire = _Wire(httpx2.Response(503, json={"message": "busy"}), _fixture(_SUCCESS_FIXTURE[family]))
    provider, counted = _live(family, wire)

    events = _run_live(family, provider)

    assert events[-1].type == "completed"
    assert (counted.streams, wire.hits) == (1, 2)
    usage = next(event for event in events if event.type == "usage")
    assert usage.retried_attempts == (RetriedAttempt(503, None),)
    assert [e.category for e in provider.drain_exclusions()] == [ExclusionCategory.RETRY]


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_an_in_band_server_error_is_retried_by_the_wrapper_only_never_by_the_sdk(
    family: str,
) -> None:
    """Each wrapper attempt is exactly one transport attempt: the SDK does not retry an HTTP-200
    stream that fails in-band."""
    wire = _Wire(_fixture(_IN_BAND_FIXTURE[family]), _fixture(_SUCCESS_FIXTURE[family]))
    provider, counted = _live(family, wire)

    events = _run_live(family, provider)

    assert events[-1].type == "completed"
    assert (counted.streams, wire.hits) == (2, 2)
    assert [e.category for e in provider.drain_exclusions()] == [ExclusionCategory.RETRY]


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_a_429_the_sdk_retried_is_tallied_as_a_throttle(family: str) -> None:
    wire = _Wire(httpx2.Response(429, json={"message": "slow"}), _fixture(_SUCCESS_FIXTURE[family]))
    provider, counted = _live(family, wire)

    events = _run_live(family, provider)

    assert events[-1].type == "completed"
    assert (counted.streams, wire.hits) == (1, 2)
    assert [e.category for e in provider.drain_exclusions()] == [ExclusionCategory.THROTTLE]


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_an_sdk_exhausted_5xx_aborts_on_wrapper_attempt_one_and_tallies_its_retried_attempts(
    family: str,
) -> None:
    """After the SDK retries to exhaustion (3 attempts) the wrapper does not retry; the two retried
    attempts are exclusions and the final one is not."""
    wire = _Wire(*(httpx2.Response(503, json={"message": "busy"}) for _ in range(3)))
    provider, counted = _live(family, wire)

    with pytest.raises(LiveInvocationError, match=r"attempt 1 of 3.*APIStatusError\(status=503\)"):
        _run_live(family, provider)

    assert (counted.streams, wire.hits) == (1, 3)
    assert [e.category for e in provider.drain_exclusions()] == [ExclusionCategory.RETRY] * 2


@pytest.mark.parametrize("family", ["anthropic", "openai"])
@pytest.mark.parametrize("status", [400, 403])
def test_a_client_fault_is_not_retried_by_either_layer(family: str, status: int) -> None:
    """A client fault is retried by neither layer, and its single attempt is no exclusion."""
    wire = _Wire(httpx2.Response(status, json={"message": "no"}))
    provider, counted = _live(family, wire)

    with pytest.raises(LiveInvocationError, match="not a categorisable exclusion"):
        _run_live(family, provider)

    assert (counted.streams, wire.hits) == (1, 1)
    assert provider.drain_exclusions() == []


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_an_empty_credential_chain_aborts_before_any_request_with_no_retry(family: str) -> None:
    wire = _Wire()
    provider, counted = _live(family, wire, session=_EmptySession())

    with pytest.raises(LiveInvocationError, match="CredentialPolicyError"):
        _run_live(family, provider)

    assert (counted.streams, wire.hits) == (1, 0)
    assert provider.drain_exclusions() == []


def test_an_expired_deadline_aborts_on_attempt_one_with_its_recorded_attempts() -> None:
    """An expired deadline aborts the wrapper, tallying the attempts that preceded the one in
    flight."""
    expired = ProviderEvent(
        type="error",
        error_message=render_deadline_exceeded(ModelCallDeadlineError(120.0)),
        retried_attempts=(RetriedAttempt(503, None), RetriedAttempt(None, None)),
    )
    wrapped = _ScriptedProvider([[expired], _SUCCESS_TURN])
    provider = ThrottleRetryingProvider(wrapped, sleep=lambda _s: None)

    with pytest.raises(LiveInvocationError, match="wall-clock deadline"):
        _drain(provider, _invocation())

    assert wrapped.attempts == 1
    assert [e.category for e in provider.drain_exclusions()] == [ExclusionCategory.RETRY] * 2


def test_an_in_band_failure_after_an_sdk_retry_tallies_both_layers() -> None:
    """The SDK's retried 429, carried on the in-band error event, is tallied ahead of the
    wrapper's own in-band retry."""
    in_band = ProviderEvent(
        type="error",
        error_message="server_error: The server had an error.",
        retried_attempts=(RetriedAttempt(429, None),),
    )
    wrapped = _ScriptedProvider([[in_band], _SUCCESS_TURN])
    provider = ThrottleRetryingProvider(wrapped, sleep=lambda _s: None)

    _drain(provider, _invocation("openai"))

    assert [e.category for e in provider.drain_exclusions()] == [
        ExclusionCategory.THROTTLE,
        ExclusionCategory.RETRY,
    ]


def test_the_wrapper_passes_the_adapters_latency_and_attempts_through_unchanged() -> None:
    """The wrapper re-yields the adapter's usage event unchanged."""
    usage = ProviderEvent(
        type="usage",
        input_tokens=1,
        output_tokens=1,
        latency_ms=12.5,
        retried_attempts=(RetriedAttempt(503, None),),
    )
    wrapped = _ScriptedProvider([[usage, _SUCCESS_TURN[-1]]])
    provider = ThrottleRetryingProvider(wrapped, sleep=lambda _s: None)

    events = _drain(provider, _invocation())

    assert events[0] is usage


class _ManualClock:
    """A monotonic clock that moves only when the test advances it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_wrapper_buffers_so_consumer_time_never_enters_the_adapters_latency() -> None:
    """The wrapper drains a whole attempt before yielding, so a consumer taking 10s per event
    leaves the adapter's ``latency_ms`` at 0ms."""
    clock = _ManualClock()
    wire = _Wire(_fixture(_SUCCESS_FIXTURE["openai"]))
    provider, _ = _live("openai", wire, clock=clock)

    async def consume() -> list[ProviderEvent]:
        received: list[ProviderEvent] = []
        stream = provider.stream(_invocation("openai"), [{"role": "user", "content": "hi"}], [])
        async for event in stream:
            received.append(event)
            clock.now += 10.0
        return received

    events = asyncio.run(consume())

    assert len(events) >= 3
    assert next(event for event in events if event.type == "usage").latency_ms == 0.0


# --------------------------------------------------------------------------------------
# Only the leading ``<prefix>:`` token classifies
# --------------------------------------------------------------------------------------

_RETRYABLE_PREFIXES = {
    "ThrottlingException": ExclusionCategory.THROTTLE,
    "TooManyRequestsException": ExclusionCategory.THROTTLE,
    "ServiceUnavailableException": ExclusionCategory.THROTTLE,
    "ServiceQuotaExceededException": ExclusionCategory.THROTTLE,
    "ModelTimeoutException": ExclusionCategory.THROTTLE,
    "ModelNotReadyException": ExclusionCategory.THROTTLE,
    "InternalServerException": ExclusionCategory.RETRY,
    "ModelErrorException": ExclusionCategory.RETRY,
    "ModelStreamErrorException": ExclusionCategory.RETRY,
    "server_error": ExclusionCategory.RETRY,
}


@pytest.mark.parametrize("prefix", sorted(_RETRYABLE_PREFIXES))
def test_every_retryable_prefix_is_retried_only_when_it_leads(prefix: str) -> None:
    assert is_retryable_error(f"{prefix}: transient detail")
    assert not is_retryable_error(f"invalid_request_error: upstream said {prefix}: detail")
    assert not is_retryable_error(f"ValidationException: {prefix}")
    assert not is_retryable_error(f"{prefix}Suffix: detail")
    assert not is_retryable_error(f" {prefix}: leading space")
    assert not is_retryable_error(prefix)


@pytest.mark.parametrize("prefix", sorted(_RETRYABLE_PREFIXES))
def test_every_retryable_prefix_is_tallied_in_its_category(prefix: str) -> None:
    wrapped = _ScriptedProvider(
        [[ProviderEvent(type="error", error_message=f"{prefix}: detail")], _SUCCESS_TURN]
    )
    provider = ThrottleRetryingProvider(wrapped, sleep=lambda _s: None)

    _drain(provider, _invocation())

    assert [e.category for e in provider.drain_exclusions()] == [_RETRYABLE_PREFIXES[prefix]]


def test_a_non_retryable_code_mentioning_internal_server_in_its_text_is_not_retried() -> None:
    message = "invalid_request_error: upstream InternalServerException while validating"
    wrapped = _ScriptedProvider([[ProviderEvent(type="error", error_message=message)]])
    provider = ThrottleRetryingProvider(wrapped, sleep=lambda _s: None)

    with pytest.raises(LiveInvocationError, match="not a categorisable exclusion"):
        _drain(provider, _invocation())

    assert wrapped.attempts == 1
    assert provider.drain_exclusions() == []


@pytest.mark.parametrize(
    "message",
    [
        "The model provider request failed after SDK retries: APIStatusError(status=503).",
        "The model provider request failed after SDK retries: APIStatusError(status=429).",
        "The model provider request failed after SDK retries: APIConnectionError.",
        "The model provider request failed after SDK retries: APITimeoutError.",
        "The model provider call exceeded its 120s wall-clock deadline.",
        "The model provider request failed: CredentialPolicyError.",
        "CredentialPolicyError: The boto3 session resolved no AWS credentials.",
        "ValidationException: The request was malformed.",
        "AccessDeniedException: Not authorised.",
        "invalid_request_error: the request was malformed",
        "InternalServerError: an SDK class name, never an adapter prefix",
        "ServiceUnavailableError: an SDK class name, never an adapter prefix",
    ],
)
def test_non_transient_failures_are_not_recognised_as_retryable(message: str) -> None:
    assert not is_retryable_error(message)


def test_draining_exclusions_empties_the_buffer_so_one_arm_never_inherits_anothers() -> None:
    wrapped = _ScriptedProvider([_THROTTLED_TURN, _SUCCESS_TURN])
    provider = ThrottleRetryingProvider(wrapped, sleep=lambda _seconds: None)
    _drain(provider, _invocation())

    assert len(provider.drain_exclusions()) == 1
    assert provider.drain_exclusions() == []


def test_the_harness_reads_a_providers_exclusions_into_the_arms_published_tally() -> None:
    """An arm's metrics report the provider's own recorded exclusions, by category."""
    wrapped = _ScriptedProvider([_THROTTLED_TURN, _SUCCESS_TURN])
    provider = ThrottleRetryingProvider(wrapped, sleep=lambda _seconds: None)
    _drain(provider, _invocation())

    drained = harness._drain_provider_exclusions(provider)

    assert [exclusion.category for exclusion in drained] == [ExclusionCategory.THROTTLE]


def test_the_stub_provider_reports_no_exclusions_and_needs_no_mode_conditional() -> None:
    from providers.stub import StubProvider

    assert harness._drain_provider_exclusions(StubProvider()) == []


def test_build_live_provider_needs_no_api_key_parameter_to_construct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live provider builder takes no API-key parameter and reads none from the
    environment."""
    for variable in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(variable, raising=False)

    provider = build_live_provider("anthropic", region_name="us-east-1")

    assert isinstance(provider, ThrottleRetryingProvider)
