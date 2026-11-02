# SPDX-License-Identifier: MIT
"""Offline tests for `scripts/smoke_agent_stream.py`'s live-path discriminators.

Each test replays a canned byte stream through an injected fake clock; each discriminator is shown
to pass on a conforming stream and fail on a violating one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator

import pytest
from scripts.smoke.harness import CheckResult
from scripts.smoke_agent_stream import (
    _dispatched_tool_name,
    _gap_alignment_check,
    _lambda_dispatch_check,
    checks_with_source,
    measure_arrivals,
)


class _FakeClock:
    """A deterministic, manually-advanced clock — never `time.sleep`."""

    def __init__(self, *, start: float = 0.0) -> None:
        self._now = start

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def __call__(self) -> float:
        return self._now


def _frame(kind: str) -> bytes:
    """Render one SSE frame in the shape `agents.runtime_entrypoint.format_sse_line` emits."""
    return f'data: {{"kind": "{kind}"}}\n\n'.encode()


#: The event sequence the deployed stub turn produces: a tool call, the action-Lambda result,
#: usage, then the error the boundary maps a text-less completion to.
_LIVE_KINDS = ("tool_call", "tool_result", "usage", "error")


def _find(results: Iterable[CheckResult], check: str) -> CheckResult:
    for result in results:
        if result.check == check:
            return result
    raise AssertionError(f"no CheckResult named {check!r} in {list(results)!r}")


def _incremental_stub_turn(
    clock: _FakeClock, *, ttfb: float = 1.0, tool_latency: float = 0.25
) -> Callable[[], Iterator[bytes]]:
    """A source replaying the live turn's real timing: a long lead-in, then the Lambda gap."""

    def source() -> Iterator[bytes]:
        clock.advance(ttfb)
        yield _frame("tool_call")
        clock.advance(tool_latency)
        yield _frame("tool_result")
        clock.advance(0.005)
        yield _frame("usage")
        clock.advance(0.0001)
        yield _frame("error")

    return source


def _buffered_stub_turn(clock: _FakeClock, *, ttfb: float = 1.25) -> Callable[[], Iterator[bytes]]:
    """A source replaying the *same* events as one buffered flush after the whole turn elapsed."""

    def source() -> Iterator[bytes]:
        clock.advance(ttfb)
        yield b"".join(_frame(kind) for kind in _LIVE_KINDS)

    return source


# ------------------------------------------------------------------------------------------------
# lead_in_fraction
# ------------------------------------------------------------------------------------------------


def test_lead_in_fraction_is_one_for_a_buffered_flush_and_below_one_when_incremental() -> None:
    """`lead_in_fraction` is 1 for a buffered flush and below 1 for an incremental stream."""
    buffered_clock = _FakeClock()
    buffered = measure_arrivals(_buffered_stub_turn(buffered_clock), clock=buffered_clock)
    assert buffered.event_count == 4
    assert buffered.lead_in_fraction == pytest.approx(1.0)
    assert buffered.spread_seconds == pytest.approx(0.0)

    incremental_clock = _FakeClock()
    incremental = measure_arrivals(
        _incremental_stub_turn(incremental_clock), clock=incremental_clock
    )
    assert incremental.event_count == 4
    assert incremental.lead_in_fraction < 1.0
    assert incremental.spread_seconds == pytest.approx(0.2551, abs=1e-4)


# ------------------------------------------------------------------------------------------------
# arrival_gap_aligns_with_tool_dispatch
# ------------------------------------------------------------------------------------------------


def test_gap_alignment_passes_when_the_delay_sits_at_the_tool_dispatch_boundary() -> None:
    clock = _FakeClock()
    measurement = measure_arrivals(_incremental_stub_turn(clock), clock=clock)
    result = _gap_alignment_check(measurement, resource="agent_stream")
    assert result.status == "pass"
    assert "'tool_call'" in result.detail


def test_gap_alignment_fails_on_a_buffered_replay_of_the_same_events() -> None:
    """The same events delivered in one flush fail, even though the tie-broken largest gap sits at
    `tool_call`."""
    clock = _FakeClock()
    measurement = measure_arrivals(_buffered_stub_turn(clock), clock=clock)
    result = _gap_alignment_check(measurement, resource="agent_stream")
    assert result.status == "fail"
    assert "indistinguishable noise" in result.detail


def test_gap_alignment_fails_when_a_real_delay_sits_at_the_wrong_boundary() -> None:
    """A stall after a coalesced tool_call/tool_result pair fails on the dispatch condition."""
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(1.0)
        yield _frame("tool_call")
        clock.advance(0.001)
        yield _frame("tool_result")
        clock.advance(0.9)  # the stall lands after tool_result, not after tool_call
        yield _frame("usage")
        clock.advance(0.001)
        yield _frame("error")

    measurement = measure_arrivals(source, clock=clock)
    result = _gap_alignment_check(measurement, resource="agent_stream")
    assert result.status == "fail"
    assert "the dispatch was coalesced into one flush" in result.detail
    assert "does not sit at the tool boundary" not in result.detail


#: The live sample's shape: round 1's lead-in, the warm
#: Lambda round trip, round 2's time to first token, then the second round's text in a dense burst.
_LIVE_SHAPE: tuple[tuple[float, str], ...] = (
    (1.0, "tool_call"),
    (0.29, "tool_result"),
    (3.38, "text_delta"),
    *((0.01, "text_delta") for _ in range(26)),
    (0.0, "usage"),
    (0.0, "completed"),
)


def _replay(shape: Iterable[tuple[float, str]], clock: _FakeClock) -> Callable[[], Iterator[bytes]]:
    """A source that advances `clock` by each step's delay, then yields that step's frame."""

    def source() -> Iterator[bytes]:
        for delay, kind in shape:
            clock.advance(delay)
            yield _frame(kind)

    return source


def _alignment(shape: Iterable[tuple[float, str]]) -> CheckResult:
    clock = _FakeClock()
    return _gap_alignment_check(
        measure_arrivals(_replay(shape, clock), clock=clock), resource="agent_stream"
    )


def test_gap_alignment_passes_when_post_tool_generation_is_the_largest_gap() -> None:
    """The live shape passes: the largest gap follows tool_result and the dispatch is visible."""
    result = _alignment(_LIVE_SHAPE)
    assert result.status == "pass", result.detail
    assert "'tool_result'" in result.detail
    assert "tool_call -> tool_result 0.2900s" in result.detail


def test_gap_alignment_fails_when_the_relay_buffers_the_live_shape_into_one_flush() -> None:
    """Every frame of the live shape arriving at once fails, however the turn was timed upstream."""
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(sum(delay for delay, _ in _LIVE_SHAPE))
        yield b"".join(_frame(kind) for _, kind in _LIVE_SHAPE)

    result = _gap_alignment_check(measure_arrivals(source, clock=clock), resource="agent_stream")
    assert result.status == "fail"
    assert "indistinguishable noise" in result.detail
    assert "coalesced into one flush" in result.detail


def test_gap_alignment_fails_when_the_largest_gap_sits_inside_the_text_phase() -> None:
    shape = ((1.0, "tool_call"), (0.29, "tool_result"), (0.2, "text_delta"), (2.0, "text_delta"))
    result = _alignment((*shape, (0.01, "completed")))
    assert result.status == "fail"
    assert "follows 'text_delta'" in result.detail


def test_gap_alignment_fails_when_the_largest_gap_follows_usage() -> None:
    shape = ((1.0, "tool_call"), (0.29, "tool_result"), (0.1, "usage"), (2.0, "completed"))
    result = _alignment(shape)
    assert result.status == "fail"
    assert "follows 'usage'" in result.detail


def test_gap_alignment_fails_when_no_tool_result_follows_the_tool_call() -> None:
    result = _alignment(((1.0, "tool_call"), (2.0, "error")))
    assert result.status == "fail"
    assert "no tool_result follows the tool_call" in result.detail


def test_gap_alignment_skips_a_text_only_turn_with_no_tool_call() -> None:
    """A text-only turn with no tool call reports skip."""
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(0.5)
        yield _frame("usage")
        clock.advance(0.3)
        yield _frame("completed")

    measurement = measure_arrivals(source, clock=clock)
    result = _gap_alignment_check(measurement, resource="agent_stream")
    assert result.status == "skip"


def test_gap_alignment_treats_an_unparseable_frame_as_kindless_rather_than_raising() -> None:
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(0.1)
        yield b"data: not-json\n\n"
        clock.advance(0.5)
        yield b"data: also-not-json\n\n"

    measurement = measure_arrivals(source, clock=clock)
    result = _gap_alignment_check(measurement, resource="agent_stream")
    assert result.status == "skip"


# ------------------------------------------------------------------------------------------------
# checks_with_source: the measured turn's verdict set, replayed
# ------------------------------------------------------------------------------------------------


def test_checks_with_source_greens_the_arrival_rows_on_an_incremental_replay() -> None:
    clock = _FakeClock()
    results = list(checks_with_source(_incremental_stub_turn(clock), clock=clock))
    for check in ("assert_incremental", "arrival_gap_aligns_with_tool_dispatch"):
        assert _find(results, check).status == "pass", check


def test_checks_with_source_fails_the_arrival_rows_when_cloudfront_buffers() -> None:
    clock = _FakeClock()
    results = list(checks_with_source(_buffered_stub_turn(clock), clock=clock))
    assert _find(results, "assert_incremental").status == "fail"
    assert _find(results, "arrival_gap_aligns_with_tool_dispatch").status == "fail"


def test_checks_with_source_reports_a_transport_failure_as_a_failing_check_not_a_crash() -> None:
    def broken() -> Iterator[bytes]:
        yield _frame("tool_call")
        raise ConnectionError("stream closed unexpectedly")

    result = _find(checks_with_source(broken, clock=_FakeClock()), "assert_incremental")
    assert result.status == "fail"
    assert "stream closed" in result.detail


# --------------------------------------------------------------------------------------
# `lambda_dispatch_observed`: the action Lambda's START-record corroboration
# --------------------------------------------------------------------------------------


class _FakeLogsClient:
    """A CloudWatch Logs stand-in: canned `filter_log_events` answers, one per call.

    `answers` is consumed left to right. `latest_timestamp_ms` is the newest indexed event
    `describe_log_streams` reports. `raises` makes every `filter_log_events` call fail.
    """

    def __init__(
        self,
        *,
        answers: list[list[dict[str, object]]],
        latest_timestamp_ms: int | None = None,
        streams: bool = True,
        raises: Exception | None = None,
    ) -> None:
        self._answers = answers
        self._latest = latest_timestamp_ms
        self._streams = streams
        self._raises = raises
        self.filter_calls = 0
        self.describe_calls = 0

    def filter_log_events(self, **kwargs: object) -> dict[str, object]:
        if self._raises is not None:
            raise self._raises
        self.filter_calls += 1
        self.last_filter_kwargs = kwargs
        if self._answers:
            return {"events": self._answers.pop(0)}
        return {"events": []}

    def describe_log_streams(self, **kwargs: object) -> dict[str, object]:
        self.describe_calls += 1
        if not self._streams:
            return {"logStreams": []}
        return {"logStreams": [{"lastEventTimestamp": self._latest}]}


_WINDOW_START_MS = 1_000_000
_WINDOW_END_MS = 1_005_000


def _start_record() -> dict[str, object]:
    return {"message": "START RequestId: 0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0 Version: $LATEST"}


def _dispatch_check(client: _FakeLogsClient, *, elapsed: list[float] | None = None) -> CheckResult:
    """Run the corroboration check against `client`, with a clock that never really waits."""
    ticks = iter(elapsed if elapsed is not None else [0.0, 0.0, 0.0, 1e9])
    slept: list[float] = []
    return _lambda_dispatch_check(
        logs_client=client,
        function_name="crm-agent-eval-search_opportunities",
        window_start_ms=_WINDOW_START_MS,
        window_end_ms=_WINDOW_END_MS,
        sleeper=slept.append,
        now=lambda: next(ticks),
    )


def test_dispatch_check_passes_when_start_records_land_in_the_measured_window() -> None:
    client = _FakeLogsClient(answers=[[_start_record(), _start_record()]])
    result = _dispatch_check(client)
    assert result.status == "pass"
    assert "2 'START RequestId' record(s)" in result.detail
    assert client.last_filter_kwargs["startTime"] == _WINDOW_START_MS
    assert client.last_filter_kwargs["endTime"] == _WINDOW_END_MS
    assert client.last_filter_kwargs["filterPattern"] == '"START RequestId"'
    assert (
        client.last_filter_kwargs["logGroupName"]
        == "/aws/lambda/crm-agent-eval-search_opportunities"
    )


def test_dispatch_check_re_polls_before_concluding_anything() -> None:
    """An empty first answer is re-polled before any verdict."""
    client = _FakeLogsClient(answers=[[], [_start_record()]])
    result = _dispatch_check(client, elapsed=[0.0, 0.0, 1.0, 1.0])
    assert result.status == "pass"
    assert client.filter_calls == 2


def test_dispatch_check_fails_when_the_record_is_genuinely_absent() -> None:
    """No START record once ingestion has reached the window is a `fail`."""
    client = _FakeLogsClient(answers=[[]], latest_timestamp_ms=_WINDOW_END_MS)
    result = _dispatch_check(client)
    assert result.status == "fail"
    assert "genuinely was not recorded" in result.detail


def test_dispatch_check_defers_rather_than_failing_on_ingestion_lag() -> None:
    """A group whose newest indexed event predates the window defers with the lag reason."""
    client = _FakeLogsClient(answers=[[]], latest_timestamp_ms=_WINDOW_START_MS - 60_000)
    result = _dispatch_check(client)
    assert result.status == "defer"
    assert result.defer_reason == "cloudwatch_log_indexing_lag"


def test_dispatch_check_defers_when_the_group_has_no_indexed_stream_at_all() -> None:
    client = _FakeLogsClient(answers=[[]], streams=False)
    result = _dispatch_check(client)
    assert result.status == "defer"
    assert result.defer_reason == "cloudwatch_log_indexing_lag"


def test_dispatch_check_fails_when_cloudwatch_cannot_be_read() -> None:
    """A CloudWatch read error is a `fail`."""
    client = _FakeLogsClient(answers=[], raises=PermissionError("logs:FilterLogEvents denied"))
    result = _dispatch_check(client)
    assert result.status == "fail"
    assert "PermissionError" in result.detail


def test_dispatched_tool_name_is_read_from_the_turns_own_tool_call_event() -> None:
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(1.0)
        yield b'data: {"kind": "tool_call", "tool_name": "search_opportunities"}\n\n'
        clock.advance(0.25)
        yield _frame("tool_result")

    measurement = measure_arrivals(source, clock=clock)
    assert _dispatched_tool_name(measurement) == "search_opportunities"


def test_dispatched_tool_name_is_empty_when_no_tool_call_was_emitted() -> None:
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(1.0)
        yield _frame("usage")

    assert _dispatched_tool_name(measure_arrivals(source, clock=clock)) == ""
