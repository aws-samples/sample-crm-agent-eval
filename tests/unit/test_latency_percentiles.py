# SPDX-License-Identifier: MIT
"""P50/P95 latency measurement and aggregation.

Covers the nearest-rank percentile method on known samples, per-tier call grouping, the
throttle-exclusion rule (a retried attempt is not a latency sample, exactly as it is not a cost
sample), the stub "not measured" null figures, and that the stub's null latency keeps a stub
sweep's artifacts byte-identical. No model call is made anywhere in this file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from eval.live import ThrottleRetryingProvider
from eval.metrics import (
    LATENCY_PERCENTILE_METHOD,
    CallLatencySample,
    LatencyMetrics,
    TierLatency,
    aggregate_latency_metrics,
    nearest_rank_percentile,
    not_measured_latency_metrics,
)
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from providers.capabilities import ModelCapability

# --- nearest-rank percentile on known samples (1, 2, 20 values) -------------------------------


def test_nearest_rank_single_value_is_that_value_at_every_percentile() -> None:
    assert nearest_rank_percentile([42.0], 50) == 42.0
    assert nearest_rank_percentile([42.0], 95) == 42.0


def test_nearest_rank_two_values_split_p50_low_p95_high() -> None:
    """n=2: rank(50)=ceil(1.0)=1 → first; rank(95)=ceil(1.9)=2 → second."""
    assert nearest_rank_percentile([10.0, 20.0], 50) == 10.0
    assert nearest_rank_percentile([10.0, 20.0], 95) == 20.0


def test_nearest_rank_twenty_values_land_on_the_documented_ranks() -> None:
    """n=20: rank(50)=ceil(10.0)=10 → values[9]; rank(95)=ceil(19.0)=19 → values[18]."""
    values = [float(n) for n in range(1, 21)]  # 1..20
    assert nearest_rank_percentile(values, 50) == 10.0
    assert nearest_rank_percentile(values, 95) == 19.0


def test_nearest_rank_sorts_unordered_input() -> None:
    assert nearest_rank_percentile([20.0, 1.0, 10.0], 50) == 10.0


def test_nearest_rank_rejects_an_empty_sample() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        nearest_rank_percentile([], 50)


def test_nearest_rank_rejects_a_percentile_outside_the_open_range() -> None:
    with pytest.raises(ValueError, match="percentile"):
        nearest_rank_percentile([1.0], 0)
    with pytest.raises(ValueError, match="percentile"):
        nearest_rank_percentile([1.0], 101)


# --- aggregation: task percentiles + per-tier call grouping -----------------------------------


def test_aggregate_groups_call_latency_by_tier() -> None:
    samples = [
        CallLatencySample(tier="supervisor", latency_ms=10.0),
        CallLatencySample(tier="supervisor", latency_ms=20.0),
        CallLatencySample(tier="subagent", latency_ms=100.0),
    ]
    metrics = aggregate_latency_metrics(
        measured=True, task_latencies_ms=[30.0, 40.0], call_samples=samples
    )
    assert metrics.measured is True
    assert metrics.task_p50_ms == 30.0
    assert metrics.task_p95_ms == 40.0
    assert metrics.by_tier["supervisor"].count == 2
    assert metrics.by_tier["supervisor"].p50 == 10.0
    assert metrics.by_tier["supervisor"].p95 == 20.0
    assert metrics.by_tier["subagent"].count == 1
    assert metrics.by_tier["subagent"].p50 == 100.0
    # A tier with no sample is present at count 0 with null figures, never absent.
    assert metrics.by_tier["tool"].count == 0
    assert metrics.by_tier["tool"].p50 is None
    assert metrics.by_tier["tool"].p95 is None


def test_report_fields_carry_the_exact_key_names_the_dashboard_reads() -> None:
    metrics = aggregate_latency_metrics(
        measured=True,
        task_latencies_ms=[5.0],
        call_samples=[CallLatencySample(tier="supervisor", latency_ms=5.0)],
    )
    fields = metrics.to_report_fields()
    assert fields["latency_measured"] is True
    assert fields["latency_percentile_method"] == "nearest-rank"
    assert fields["task_latency_ms_p50"] == 5.0
    assert fields["task_latency_ms_p95"] == 5.0
    by_tier = fields["call_latency_ms_by_tier"]
    assert isinstance(by_tier, dict)
    assert set(by_tier) == {"supervisor", "subagent", "tool"}
    assert by_tier["supervisor"] == {"p50": 5.0, "p95": 5.0, "count": 1}
    assert by_tier["tool"] == {"p50": None, "p95": None, "count": 0}


def test_percentile_method_constant_is_nearest_rank() -> None:
    assert LATENCY_PERCENTILE_METHOD == "nearest-rank"


# --- stub / not-measured: null figures, all tiers present -------------------------------------


def test_not_measured_reports_null_figures_for_every_field() -> None:
    metrics = not_measured_latency_metrics()
    assert metrics.measured is False
    assert metrics.task_p50_ms is None
    assert metrics.task_p95_ms is None
    fields = metrics.to_report_fields()
    assert fields["latency_measured"] is False
    assert fields["task_latency_ms_p50"] is None
    assert fields["task_latency_ms_p95"] is None
    by_tier = fields["call_latency_ms_by_tier"]
    assert isinstance(by_tier, dict)
    assert set(by_tier) == {"supervisor", "subagent", "tool"}
    for tier_fields in by_tier.values():
        assert tier_fields == {"p50": None, "p95": None, "count": 0}


def test_aggregate_with_measured_false_ignores_samples_and_reports_null() -> None:
    """With samples supplied, measured=False still yields the not-measured shape."""
    metrics = aggregate_latency_metrics(
        measured=False,
        task_latencies_ms=[1.0, 2.0, 3.0],
        call_samples=[CallLatencySample(tier="supervisor", latency_ms=1.0)],
    )
    assert metrics == not_measured_latency_metrics()


def test_measured_true_with_no_samples_is_measured_but_null() -> None:
    metrics = aggregate_latency_metrics(measured=True, task_latencies_ms=[], call_samples=[])
    assert metrics.measured is True
    assert metrics.task_p50_ms is None
    assert metrics.by_tier["supervisor"] == TierLatency(p50=None, p95=None, count=0)


def test_latency_metrics_is_frozen() -> None:
    metrics = not_measured_latency_metrics()
    assert isinstance(metrics, LatencyMetrics)
    with pytest.raises(AttributeError):
        metrics.measured = True  # type: ignore[misc]


# --- throttle exclusion: a retried attempt is not a latency sample ----------------------------


def _capability() -> ModelCapability:
    """A minimal resolved model so a fake provider's stream can carry a model id."""
    from providers.capabilities import load_capability_matrix

    matrix = load_capability_matrix()
    return matrix.model_for("anthropic", "supervisor")


class _ScriptedProvider(ModelProvider):
    """Yields a scripted sequence of attempts: each attempt is a list of ProviderEvents."""

    def __init__(self, attempts: list[list[ProviderEvent]]) -> None:
        self._attempts = attempts
        self._index = 0

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        events = self._attempts[self._index]
        self._index += 1
        for event in events:
            yield event


async def _drain(provider: ThrottleRetryingProvider, invocation: ResolvedInvocation) -> None:
    async for _event in provider.stream(invocation, [], []):
        pass


async def _collect(
    provider: ThrottleRetryingProvider, invocation: ResolvedInvocation
) -> list[ProviderEvent]:
    return [event async for event in provider.stream(invocation, [], [])]


def test_the_wrapper_re_yields_the_adapters_latency_unchanged() -> None:
    """The wrapper re-yields the adapter's usage event as built and stamps nothing on any event."""
    import asyncio

    resolved = ResolvedInvocation(model=_capability(), effort=None)
    usage = ProviderEvent(type="usage", input_tokens=10, output_tokens=5, latency_ms=41.5)
    done = ProviderEvent(type="completed", awaiting_tools=False)
    provider = ThrottleRetryingProvider(_ScriptedProvider([[usage, done]]))

    events = asyncio.run(_collect(provider, resolved))

    assert next(e for e in events if e.type == "usage").latency_ms == 41.5
    assert next(e for e in events if e.type == "completed").latency_ms is None


def test_only_the_successful_attempts_latency_reaches_the_caller() -> None:
    """A throttled-then-succeeded turn yields only the successful attempt's usage event, and the
    throttled attempt is recorded as an exclusion."""
    import asyncio

    resolved = ResolvedInvocation(model=_capability(), effort=None)
    throttle = ProviderEvent(type="error", error_message="ThrottlingException: slow down")
    usage = ProviderEvent(type="usage", input_tokens=10, output_tokens=5, latency_ms=7.0)
    ok = ProviderEvent(type="completed", input_tokens=10, output_tokens=5, awaiting_tools=False)
    scripted = _ScriptedProvider([[throttle], [usage, ok]])
    provider = ThrottleRetryingProvider(scripted, backoff_seconds=0.0, sleep=lambda _s: None)

    events = asyncio.run(_collect(provider, resolved))

    assert [e.latency_ms for e in events if e.type == "usage"] == [7.0]
    assert len(provider.drain_exclusions()) >= 1


def test_a_usage_event_without_latency_stays_unmeasured() -> None:
    """A usage event without a timing keeps ``latency_ms=None`` through the wrapper."""
    import asyncio

    resolved = ResolvedInvocation(model=_capability(), effort=None)
    usage = ProviderEvent(type="usage", input_tokens=10, output_tokens=5)
    provider = ThrottleRetryingProvider(_ScriptedProvider([[usage]]))

    events = asyncio.run(_collect(provider, resolved))

    assert events[0].latency_ms is None
    assert not hasattr(provider, "drain_latencies")


def test_a_real_adapters_latency_reaches_the_caller_through_the_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the real OpenAI adapter and SDK: the usage event arrives measured, from the
    adapter's transport recorder."""
    import asyncio

    import httpx2
    from botocore.credentials import Credentials

    from providers.openai_adapter import OpenAIAdapter
    from tests.integration._aws_eventstream import load_wire_fixture, wire_body

    class _Session:
        def get_credentials(self) -> Credentials:
            return Credentials("test", "test")

    for name in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_BEDROCK_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    body = wire_body(load_wire_fixture("openai_responses_luna_success.json"))
    transport = httpx2.MockTransport(
        lambda _r: httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})
    )
    adapter = OpenAIAdapter(region_name="us-east-1", session=_Session(), transport=transport)
    from providers.capabilities import load_capability_matrix

    luna = load_capability_matrix().model_for("openai", "tool")
    resolved = ResolvedInvocation(model=luna, effort="low")
    provider = ThrottleRetryingProvider(adapter)

    async def collect() -> list[ProviderEvent]:
        stream = provider.stream(resolved, [{"role": "user", "content": "hi"}], [])
        return [event async for event in stream]

    usage = next(e for e in asyncio.run(collect()) if e.type == "usage")
    assert usage.latency_ms is not None and usage.latency_ms >= 0.0
