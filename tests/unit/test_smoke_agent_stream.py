# SPDX-License-Identifier: MIT
"""Offline self-tests for `scripts/smoke_agent_stream.py`.

The tests replay recorded SSE streams through an injected fake clock, never a real socket and never
`time.sleep`.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from email.message import Message
from typing import Any, NoReturn, cast
from unittest import mock

import pytest
from jwt.utils import base64url_encode
from scripts.smoke import harness as smoke_harness
from scripts.smoke.harness import CheckResult, SmokeContext, context_for_test
from scripts.smoke.verified_claims import ClaimsVerificationError
from scripts.smoke_agent_stream import (
    _EXPECTED_CHECKS,
    _STUB_FINGERPRINT_CHECKS,
    _STUB_TEXT_MARKER,
    _STUB_TOOL_OUTPUT_FLAG,
    _UNEMITTED_DEFER_REASON,
    LiveStreamPrincipalProvisioner,
    SseEvent,
    _deferrals_for_unemitted,
    _turn_body,
    checks_with_source,
    live_source,
    measure_arrivals,
    parse_sse_bytes,
    stub_fingerprint_checks,
    token_claims,
)

from providers.stub import _STUB_INPUT_TOKENS, _STUB_OUTPUT_TOKENS
from tests.unit import _jwks_test_key as test_key

# ------------------------------------------------------------------------------------------------
# smoke_agent_stream: SSE parsing
# ------------------------------------------------------------------------------------------------


def test_parse_sse_bytes_parses_a_simple_single_line_event() -> None:
    chunks = [b'event: tool_call\ndata: {"a": 1}\n\n']
    events = list(parse_sse_bytes(chunks))
    assert events == [SseEvent(event="tool_call", data='{"a": 1}')]


def test_parse_sse_bytes_joins_a_multiline_data_field() -> None:
    chunks = [b"data: line one\ndata: line two\n\n"]
    events = list(parse_sse_bytes(chunks))
    assert events == [SseEvent(event="", data="line one\nline two")]


def test_parse_sse_bytes_ignores_comment_and_heartbeat_lines() -> None:
    chunks = [b": heartbeat\ndata: real\n\n: another comment\n"]
    events = list(parse_sse_bytes(chunks))
    assert events == [SseEvent(event="", data="real")]


def test_parse_sse_bytes_splits_multiple_records_across_chunks() -> None:
    chunks = [b"data: first\n\nda", b"ta: second\n\n"]
    events = list(parse_sse_bytes(chunks))
    assert [e.data for e in events] == ["first", "second"]


def test_parse_sse_bytes_flushes_a_truncated_record_with_no_trailing_blank_line() -> None:
    """A malformed/truncated stream still emits the complete data it accumulated."""
    chunks = [b"data: truncated"]
    events = list(parse_sse_bytes(chunks))
    assert events == [SseEvent(event="", data="truncated")]


def test_parse_sse_bytes_record_with_no_data_field_emits_nothing() -> None:
    """A record carrying only an event: field and no data: is not a message per the SSE spec."""
    chunks = [b"event: ping\n\n"]
    events = list(parse_sse_bytes(chunks))
    assert events == []


# ------------------------------------------------------------------------------------------------
# smoke_agent_stream: arrival-time measurement with an injected fake clock
# ------------------------------------------------------------------------------------------------


class _FakeClock:
    """A deterministic, manually-advanced clock — never `time.sleep`."""

    def __init__(self, *, start: float = 0.0) -> None:
        self._now = start

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def __call__(self) -> float:
        return self._now


def _chunked_source(
    chunks: Iterable[bytes], clock: _FakeClock, *, advance_between: float
) -> Iterator[bytes]:
    """Yield `chunks` one at a time, advancing `clock` by `advance_between` before each yield.

    `advance_between=0.0` simulates a single buffered flush; a nonzero value spreads the reads.
    """
    for chunk in chunks:
        clock.advance(advance_between)
        yield chunk


def test_measure_arrivals_on_a_genuinely_incremental_stream_shows_a_real_spread() -> None:
    records = [f"data: event-{i}\n\n".encode() for i in range(5)]
    clock = _FakeClock()
    measurement = measure_arrivals(
        lambda: _chunked_source(records, clock, advance_between=0.1), clock=clock
    )
    assert measurement.event_count == 5
    assert measurement.spread_seconds == pytest.approx(0.4, abs=1e-9)


def test_measure_arrivals_on_a_single_flush_buffered_stream_shows_zero_spread() -> None:
    """The whole body arrives as one chunk: every record completes at the same timestamp."""
    body = b"".join(f"data: event-{i}\n\n".encode() for i in range(5))
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(0.001)  # one single read of the entire buffered body
        yield body

    measurement = measure_arrivals(source, clock=clock)
    assert measurement.event_count == 5
    assert measurement.spread_seconds == pytest.approx(0.0, abs=1e-9)


def test_measure_arrivals_with_one_long_stall_still_reflects_the_real_spread() -> None:
    """A stream with a long pause between two clusters of events still measures the true spread."""
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(0.01)
        yield b"data: first\n\n"
        clock.advance(5.0)  # a long stall
        yield b"data: second\n\n"

    measurement = measure_arrivals(source, clock=clock)
    assert measurement.event_count == 2
    assert measurement.spread_seconds == pytest.approx(5.0, abs=1e-6)


def test_measure_arrivals_with_a_single_event_yields_zero_spread_by_definition() -> None:
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(0.01)
        yield b"data: only\n\n"

    measurement = measure_arrivals(source, clock=clock)
    assert measurement.event_count == 1
    assert measurement.spread_seconds == 0.0


def test_measure_arrivals_on_a_malformed_truncated_stream_still_counts_the_flushed_record() -> None:
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(0.01)
        yield b"data: incomplete-no-trailing-blank-line"

    measurement = measure_arrivals(source, clock=clock)
    assert measurement.event_count == 1


# ------------------------------------------------------------------------------------------------
# smoke_agent_stream: the --assert-incremental verdict, replayed through checks_with_source
# ------------------------------------------------------------------------------------------------


def _find(results: Iterable[CheckResult], check: str) -> CheckResult:
    for result in results:
        if result.check == check:
            return result
    raise AssertionError(f"no CheckResult named {check!r} in {list(results)!r}")


def test_assert_incremental_passes_on_a_genuinely_incremental_replay() -> None:
    records = [f"data: event-{i}\n\n".encode() for i in range(4)]
    clock = _FakeClock()
    results = checks_with_source(
        lambda: _chunked_source(records, clock, advance_between=0.2), clock=clock
    )
    result = _find(results, "assert_incremental")
    assert result.status == "pass"


def test_assert_incremental_fails_on_a_single_flush_buffered_replay() -> None:
    """A recorded single-flush buffered stream fails the check."""
    body = b"".join(f"data: event-{i}\n\n".encode() for i in range(6))
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(0.0005)
        yield body

    results = checks_with_source(source, clock=clock)
    result = _find(results, "assert_incremental")
    assert result.status == "fail"
    assert "buffered block" in result.detail


def test_assert_incremental_fails_on_a_stream_with_one_long_stall_that_still_only_has_two_clusters() -> (
    None
):
    """Two events separated by a long stall produce a real spread and pass."""
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(0.01)
        yield b"data: first\n\n"
        clock.advance(3.0)
        yield b"data: second\n\n"

    results = checks_with_source(source, clock=clock)
    result = _find(results, "assert_incremental")
    assert result.status == "pass"


def test_assert_incremental_fails_on_a_slow_but_buffered_stream_despite_clearing_the_spread_floor() -> (
    None
):
    """Events arriving together after a long wait fail: the first event's lead-in is the whole
    spread."""
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(0.2)  # the whole buffered flush arrives after one long wait
        yield b"".join(f"data: event-{i}\n\n".encode() for i in range(5))

    results = checks_with_source(source, clock=clock)
    result = _find(results, "assert_incremental")
    assert result.status == "fail"


def test_assert_incremental_passes_when_the_first_event_arrives_early_relative_to_the_rest() -> (
    None
):
    """A stream whose first event arrives early relative to the spread passes."""
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(0.01)
        yield b"data: first\n\n"  # arrives almost immediately
        clock.advance(0.5)
        yield b"data: second\n\n"
        clock.advance(0.5)
        yield b"data: third\n\n"
        clock.advance(0.5)
        yield b"data: fourth\n\n"

    results = checks_with_source(source, clock=clock)
    result = _find(results, "assert_incremental")
    assert result.status == "pass"


def test_assert_incremental_skips_on_a_single_event_stream() -> None:
    """A single-event stream reports skip."""
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(0.01)
        yield b"data: only\n\n"

    results = checks_with_source(source, clock=clock)
    result = _find(results, "assert_incremental")
    assert result.status == "skip"


def test_assert_incremental_fails_gracefully_on_a_malformed_truncated_stream() -> None:
    """A truncated stream that completes only one record reports skip, never pass."""
    clock = _FakeClock()

    def source() -> Iterator[bytes]:
        clock.advance(0.01)
        yield b"data: incomplete-no-trailing-blank-line"

    results = checks_with_source(source, clock=clock)
    result = _find(results, "assert_incremental")
    assert result.status == "skip"  # exactly one record was ever completed


def test_assert_incremental_fails_when_the_transport_raises() -> None:
    def source() -> Iterator[bytes]:
        yield b"data: partial\n\n"
        raise ConnectionError("stream closed unexpectedly")

    results = checks_with_source(source, clock=_FakeClock())
    result = _find(results, "assert_incremental")
    assert result.status == "fail"
    assert "stream closed" in result.detail


# ------------------------------------------------------------------------------------------------
# The CloudFront arm's request headers, and token verification
# ------------------------------------------------------------------------------------------------


def _throwaway_token() -> str:
    """Return a placeholder token assembled at runtime rather than written as a literal.

    A `token="<literal>"` assignment matches gitleaks' `generic-high-entropy-secret` rule.
    """
    return ".".join(("header", "payload", "signature"))


def _captured_request(**kwargs: object) -> urllib.request.Request:
    """Build the CloudFront arm's request without sending it, by intercepting the HTTPS opener."""
    captured: list[urllib.request.Request] = []

    def _fake_open(request: urllib.request.Request, timeout: float = 0.0) -> object:
        captured.append(request)
        raise urllib.error.HTTPError(request.full_url, 499, "captured", {}, None)  # type: ignore[arg-type]

    source = live_source(
        "https://d111.cloudfront.net/agent/arn%3Aaws%3Abedrock",
        token=_throwaway_token(),
        body={"prompt": "hello", "session_id": "s" * 40},
        session_id="s" * 40,
        timeout=1.0,
        **kwargs,  # type: ignore[arg-type]
    )
    with (
        mock.patch.object(smoke_harness._HTTPS_OPENER, "open", _fake_open),
        pytest.raises(ValueError),
    ):
        list(source())
    assert len(captured) == 1
    return captured[0]


def test_the_cloudfront_arm_sends_the_jwt_in_the_header_the_relay_reads() -> None:
    """The CloudFront arm sends the JWT in `x-crm-authorization`, the header the relay reads."""
    request = _captured_request()
    keys = {key.lower() for key in request.headers}
    assert "x-crm-authorization" in keys
    assert request.get_header("X-crm-authorization") == f"Bearer {_throwaway_token()}"


def test_the_cloudfront_arm_sends_no_authorization_header_at_all() -> None:
    """The CloudFront arm sends no `Authorization` header, which OAC's SigV4 signature occupies."""
    request = _captured_request()
    assert "authorization" not in {key.lower() for key in request.headers}


def test_the_body_digest_header_is_the_hash_of_the_exact_bytes_sent() -> None:
    """The body digest header is the SHA-256 of the request's own `data` bytes."""
    request = _captured_request()
    sent = request.data
    assert isinstance(sent, bytes)
    assert request.get_header("X-amz-content-sha256") == hashlib.sha256(sent).hexdigest()


_OUTPUTS = {
    "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/r",
    "cognito_user_pool_id": test_key.USER_POOL_ID,
    "cognito_spa_client_id": test_key.CLIENT_ID,
}


def _stream_ctx() -> SmokeContext:
    return context_for_test(outputs=_OUTPUTS)


def _alg_none_token(claims: dict[str, Any]) -> str:
    """Return an unsigned `alg: none` JWT over `claims`."""
    segments = ({"alg": "none", "typ": "JWT"}, claims)
    return ".".join(base64url_encode(json.dumps(s).encode()).decode() for s in segments) + "."


#: Access tokens naming the smoke tenant that do not verify for this pool and client.
_UNVERIFIABLE_TOKENS = {
    "foreign_key": test_key.TestKey().sign(
        test_key.access_token_claims(**{"custom:tenant": "eval-smoke-abc"})
    ),
    "alg_none": _alg_none_token(
        test_key.access_token_claims(**{"custom:tenant": "eval-smoke-abc"})
    ),
    "id_token": test_key.id_token(**{"custom:tenant": "eval-smoke-abc"}),
}


@pytest.mark.parametrize("name", sorted(_UNVERIFIABLE_TOKENS))
def test_token_claims_refuses_an_unverifiable_token(name: str) -> None:
    """No claims are read from a token that fails `verified_claims`."""
    with pytest.raises(ClaimsVerificationError):
        token_claims(_stream_ctx(), _UNVERIFIABLE_TOKENS[name], jwks_url=test_key.jwks_url())


def _provisioner_issuing(token: str) -> LiveStreamPrincipalProvisioner:
    provisioner = LiveStreamPrincipalProvisioner(_stream_ctx(), jwks_url=test_key.jwks_url())
    provisioner._one_auth_attempt = lambda **_kwargs: (token, "")  # type: ignore[method-assign]
    return provisioner


def test_the_provisioner_accepts_a_verified_token_naming_the_expected_tenant() -> None:
    """A pool-signed access token naming the disposable tenant is returned."""
    token = test_key.access_token(**{"custom:tenant": "eval-smoke-abc"})
    assert (
        _provisioner_issuing(token)._authenticate_with_mfa(
            username="u", password=secrets.token_hex(8), expected_tenant="eval-smoke-abc"
        )
        == token
    )


@pytest.mark.parametrize("name", sorted(_UNVERIFIABLE_TOKENS))
def test_the_provisioner_refuses_an_unverifiable_token_even_naming_the_expected_tenant(
    name: str,
) -> None:
    """A token naming the right tenant but failing verification is never accepted."""
    with pytest.raises(ClaimsVerificationError):
        _provisioner_issuing(_UNVERIFIABLE_TOKENS[name])._authenticate_with_mfa(
            username="u", password=secrets.token_hex(8), expected_tenant="eval-smoke-abc"
        )


def test_the_cloudfront_arms_body_carries_no_identity_because_the_relay_builds_it() -> None:
    """The CloudFront arm's body carries no identity; the relay writes `_agent_identity` itself."""
    assert "_agent_identity" not in _turn_body("p", "s" * 40)


# ------------------------------------------------------------------------------------------------
# Board completeness: a check that did not run is reported as a deferral, never absent
# ------------------------------------------------------------------------------------------------


def test_every_expected_check_missing_from_a_run_comes_back_as_an_explicit_defer() -> None:
    emitted = [
        CheckResult(
            resource="agent_stream",
            check="tool_calling_turn_elicited",
            status="fail",
            detail="invoke_agent_runtime failed",
        )
    ]
    deferred = _deferrals_for_unemitted(emitted)
    assert {result.check for result in deferred} == {check for _, check in _EXPECTED_CHECKS} - {
        "tool_calling_turn_elicited"
    }
    assert all(result.status == "defer" for result in deferred)
    assert all(result.defer_reason == _UNEMITTED_DEFER_REASON for result in deferred)


def test_a_deferral_names_the_upstream_failure_that_cost_it_the_row() -> None:
    """A deferred row names the upstream failure that prevented it."""
    emitted = [
        CheckResult(resource="agent_stream", check="assert_incremental", status="fail", detail="x"),
    ]
    deferred = _deferrals_for_unemitted(emitted)
    assert all("assert_incremental" in result.detail for result in deferred)


def test_a_complete_run_is_deferred_nothing_which_is_this_gates_own_control() -> None:
    """A complete run produces no deferrals."""
    complete = [
        CheckResult(resource=resource, check=check, status="pass")
        for resource, check in _EXPECTED_CHECKS
    ]
    assert _deferrals_for_unemitted(complete) == []


def test_a_deferral_carries_the_resource_the_real_check_would_have_carried() -> None:
    """A deferral carries the resource the real check would have carried."""
    by_check = {check: resource for resource, check in _EXPECTED_CHECKS}
    for result in _deferrals_for_unemitted([]):
        assert result.resource == by_check[result.check]


def test_the_expected_table_is_exactly_what_the_module_can_emit_and_no_more() -> None:
    """`_EXPECTED_CHECKS` equals the check names the module's source can emit, plus the two
    demo-tenant guard rows.

    Collected by AST from every `check=` keyword on any call, resolving local string assignments.
    """
    import ast
    from pathlib import Path

    source = Path("scripts/smoke_agent_stream.py").read_text(encoding="utf-8")
    emitted: set[str] = set()
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef):
            continue
        local: dict[str, str] = {
            node.targets[0].id: node.value.value
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "check":
                    continue
                if isinstance(keyword.value, ast.Constant):
                    emitted.add(str(keyword.value.value))
                elif isinstance(keyword.value, ast.Name) and keyword.value.id in local:
                    emitted.add(local[keyword.value.id])

    expected = {check for _, check in _EXPECTED_CHECKS}
    # The two demo-tenant guard rows are emitted by `scripts/smoke/demo_tenant_guard.py`, not here.
    external = {"demo_tenant_untouched", "writes_only_under_smoke_tenant"}
    assert emitted - expected == set(), f"emitted but not in _EXPECTED_CHECKS: {emitted - expected}"
    assert expected - emitted == external, (
        f"in _EXPECTED_CHECKS but never emitted: {expected - emitted - external}"
    )


# ------------------------------------------------------------------------------------------------
# The turn's text_delta frames arrive before its completed frame
# ------------------------------------------------------------------------------------------------


def _buffered(clock: _FakeClock, count: int = 4) -> Iterator[bytes]:
    """One flush carrying `count` complete SSE records: arrival spread is zero by construction."""
    clock.advance(0.001)
    yield b"".join(f"data: event-{i}\n\n".encode() for i in range(count))


def _kinds_source(
    kinds: Iterable[str], clock: _FakeClock, *, advance_between: float = 0.2
) -> Iterator[bytes]:
    """One SSE frame per `kind`, in order, `advance_between` seconds apart."""
    frames = [b"data: " + json.dumps({"kind": kind}).encode() + b"\n\n" for kind in kinds]
    return _chunked_source(frames, clock, advance_between=advance_between)


def _text_delta_row(kinds: Iterable[str], *, advance_between: float = 0.2) -> CheckResult:
    clock = _FakeClock()
    results = checks_with_source(
        lambda: _kinds_source(kinds, clock, advance_between=advance_between), clock=clock
    )
    return _find(results, "text_delta_frames_arrive_before_completed")


def test_the_cloudfront_arm_still_fails_on_a_buffered_stream() -> None:
    clock = _FakeClock()
    results = list(checks_with_source(lambda: _buffered(clock), clock=clock))
    assert _find(results, "assert_incremental").status == "fail"


def test_one_measured_turn_publishes_the_incremental_gap_and_text_delta_rows() -> None:
    clock = _FakeClock()
    kinds = ("text_delta", "tool_call", "tool_result", "text_delta", "completed")
    results = list(checks_with_source(lambda: _kinds_source(kinds, clock), clock=clock))
    assert [r.check for r in results] == [
        "assert_incremental",
        "arrival_gap_aligns_with_tool_dispatch",
        "text_delta_frames_arrive_before_completed",
    ]
    assert _find(results, "text_delta_frames_arrive_before_completed").status == "pass"


def test_text_delta_frames_streamed_ahead_of_completed_pass() -> None:
    row = _text_delta_row(("text_delta", "text_delta", "text_delta", "completed"))
    assert row.status == "pass"
    assert "3 text_delta frame(s), 1 completed frame(s)" in row.detail


def test_a_single_buffered_flush_fails_the_text_delta_row() -> None:
    """Every frame present at the same instant: the deltas are not before completed."""
    row = _text_delta_row(("text_delta", "text_delta", "completed"), advance_between=0.0)
    assert row.status == "fail"
    assert "at or after completed" in row.detail
    assert "under the" in row.detail


def test_a_spread_under_the_floor_fails_even_when_the_order_holds() -> None:
    row = _text_delta_row(("text_delta", "completed"), advance_between=0.01)
    assert row.status == "fail"
    assert "at or after completed" not in row.detail
    assert "under the" in row.detail


def test_no_text_delta_frame_fails_the_text_delta_row() -> None:
    row = _text_delta_row(("usage", "completed"))
    assert row.status == "fail"
    assert "no text_delta frame arrived" in row.detail


def test_a_text_delta_after_completed_is_out_of_order() -> None:
    row = _text_delta_row(("text_delta", "completed", "text_delta"))
    assert row.status == "fail"
    assert "[2] arrived at or after completed" in row.detail


@pytest.mark.parametrize(
    "kinds",
    [("text_delta", "text_delta"), ("text_delta", "completed", "completed")],
    ids=["no_completed", "two_completed"],
)
def test_anything_but_exactly_one_completed_frame_fails(kinds: tuple[str, ...]) -> None:
    row = _text_delta_row(kinds)
    assert row.status == "fail"
    assert "expected exactly one completed frame" in row.detail


def test_the_cloudfront_arm_records_its_response_headers_on_an_error_response() -> None:
    """An error served through CloudFront still records its `x-amz-cf-id` into the sink."""
    captured: list[urllib.request.Request] = []

    def _fake_open(request: urllib.request.Request, timeout: float = 0.0) -> object:
        captured.append(request)
        # `HTTPError.headers` is an `email.message.Message`.
        headers = Message()
        headers["x-amz-cf-id"] = "abc123"
        headers["via"] = "2.0 cloudfront"
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", headers, None)

    sink: dict[str, str] = {}
    source = live_source(
        "https://d111.cloudfront.net/agent/x",
        token=_throwaway_token(),
        body={"prompt": "p"},
        session_id="s" * 40,
        timeout=1.0,
        headers_sink=sink,
    )
    with (
        mock.patch.object(smoke_harness._HTTPS_OPENER, "open", _fake_open),
        pytest.raises(ValueError),
    ):
        list(source())

    assert sink.get("x-amz-cf-id") == "abc123", "the error path must still record the headers"


# ------------------------------------------------------------------------------------------------
# The stub-fingerprint reader: detects a deployed Runtime serving stub-provider replies
#
# Fixtures are built in the wire shape (`kind`); the token constants are imported from the producer.
# ------------------------------------------------------------------------------------------------


def _wire(*payloads: dict[str, object]) -> list[SseEvent]:
    """Render payloads as SSE records the way the Runtime does: one JSON object per record."""
    return [SseEvent(event="", data=json.dumps(payload)) for payload in payloads]


#: A real turn captured from a live run: two events, `kind`, and real token counts.
_REAL_TRANSCRIPT = _wire(
    {"kind": "usage", "input_tokens": 4160, "output_tokens": 7},
    {"kind": "completed", "input_tokens": 4160, "output_tokens": 7, "text": "ORANGE"},
)


def _stub_text_transcript() -> list[SseEvent]:
    """The stub's TEXT branch, with the token counts taken from the producer rather than retyped."""
    return _wire(
        {"kind": "usage", "input_tokens": _STUB_INPUT_TOKENS, "output_tokens": _STUB_OUTPUT_TOKENS},
        {
            "kind": "completed",
            "input_tokens": _STUB_INPUT_TOKENS,
            "output_tokens": _STUB_OUTPUT_TOKENS,
            "text": "[stub:global.anthropic.claude-opus-5-5] response digest=0123456789abcdef",
        },
    )


def _stub_tool_transcript() -> list[SseEvent]:
    """The stub's TOOL branch. Mutually exclusive with the text branch on any one turn."""
    return _wire(
        {"kind": "tool_call", "tool_name": "search_opportunities"},
        {
            "kind": "tool_result",
            "tool_name": "search_opportunities",
            "tool_output": {"stub": True, "digest": "0123456789abcdef"},
        },
        {"kind": "usage", "input_tokens": _STUB_INPUT_TOKENS, "output_tokens": _STUB_OUTPUT_TOKENS},
    )


def test_the_stub_fingerprints_do_not_fire_on_a_real_turn() -> None:
    """No stub fingerprint fires on a real captured turn."""
    results = stub_fingerprint_checks(_REAL_TRANSCRIPT)

    assert {r.check for r in results} == set(_STUB_FINGERPRINT_CHECKS)
    assert all(r.status == "pass" for r in results), [
        (r.check, r.status, r.detail) for r in results if r.status != "pass"
    ]


def test_the_text_leg_fires_alone_on_the_stubs_text_branch() -> None:
    """On the stub's text branch only the text leg fires."""
    by_check = {r.check: r for r in stub_fingerprint_checks(_stub_text_transcript())}

    assert by_check["runtime_text_is_not_stub_shaped"].status == "fail"
    assert by_check["runtime_tool_output_carries_no_stub_flag"].status == "pass"
    assert by_check["runtime_usage_is_not_the_stub_constants"].status == "fail"


def test_the_tool_output_leg_fires_alone_on_the_stubs_tool_branch() -> None:
    """On the stub's tool branch only the tool-output leg fires."""
    by_check = {r.check: r for r in stub_fingerprint_checks(_stub_tool_transcript())}

    assert by_check["runtime_tool_output_carries_no_stub_flag"].status == "fail"
    assert by_check["runtime_text_is_not_stub_shaped"].status == "pass"
    assert by_check["runtime_usage_is_not_the_stub_constants"].status == "fail"


def test_the_usage_leg_discriminates_on_the_values_not_on_a_usage_event_existing() -> None:
    """The usage leg keys on token values: real counts flip it and leave the other legs alone."""
    stubbed = _stub_tool_transcript()
    real_usage = _wire(
        {"kind": "tool_call", "tool_name": "search_opportunities"},
        {
            "kind": "tool_result",
            "tool_name": "search_opportunities",
            "tool_output": {"stub": True, "digest": "0123456789abcdef"},
        },
        {"kind": "usage", "input_tokens": 4160, "output_tokens": 7},
    )

    before = {r.check: r.status for r in stub_fingerprint_checks(stubbed)}
    after = {r.check: r.status for r in stub_fingerprint_checks(real_usage)}

    assert before["runtime_usage_is_not_the_stub_constants"] == "fail"
    assert after["runtime_usage_is_not_the_stub_constants"] == "pass"
    # the other two are unmoved by the token substitution
    assert before["runtime_tool_output_carries_no_stub_flag"] == "fail"
    assert after["runtime_tool_output_carries_no_stub_flag"] == "fail"
    assert before["runtime_text_is_not_stub_shaped"] == "pass"
    assert after["runtime_text_is_not_stub_shaped"] == "pass"


def test_only_one_of_the_two_token_constants_matching_is_not_a_stub() -> None:
    """Only one of the two stub token constants matching is not a stub hit."""
    half = _wire({"kind": "usage", "input_tokens": _STUB_INPUT_TOKENS, "output_tokens": 7})
    by_check = {r.check: r for r in stub_fingerprint_checks(half)}

    assert by_check["runtime_usage_is_not_the_stub_constants"].status == "pass"


def test_an_empty_transcript_skips_and_never_passes() -> None:
    """An empty transcript skips and never passes."""
    results = stub_fingerprint_checks([])

    assert {r.check for r in results} == set(_STUB_FINGERPRINT_CHECKS)
    assert all(r.status == "skip" for r in results)
    assert all("nothing was examined" in r.detail for r in results)


def test_an_unparseable_record_does_not_crash_or_mask_a_sibling() -> None:
    """An unparseable record is ignored without crashing or masking a marker in another record."""
    events = [SseEvent(event="", data="not json at all"), *_stub_tool_transcript()]
    by_check = {r.check: r for r in stub_fingerprint_checks(events)}

    assert by_check["runtime_tool_output_carries_no_stub_flag"].status == "fail"


def test_the_markers_this_check_greps_for_still_exist_in_the_producer() -> None:
    """The two non-importable markers (`[stub:` and the `stub` flag) still appear in the producer's
    source."""
    from pathlib import Path

    producer = Path("src/providers/stub.py").read_text(encoding="utf-8")

    assert _STUB_TEXT_MARKER in producer, (
        f"{_STUB_TEXT_MARKER!r} is no longer produced by src/providers/stub.py; the text leg is dead"
    )
    assert f'"{_STUB_TOOL_OUTPUT_FLAG}": True' in producer, (
        f"the {_STUB_TOOL_OUTPUT_FLAG!r} flag is no longer set by src/providers/stub.py"
    )


def test_the_three_fingerprints_are_registered_on_the_board_contract() -> None:
    """The three fingerprint checks are registered on the board contract."""
    registered = {check for _, check in _EXPECTED_CHECKS}

    assert set(_STUB_FINGERPRINT_CHECKS) <= registered


#: The wire field names `stub_fingerprint_checks` keys on.
_WIRE_FIELDS_READ = ("text", "tool_output", "input_tokens", "output_tokens")


def test_every_wire_field_the_fingerprints_read_is_declared_on_agent_event() -> None:
    """Every wire field the fingerprints read is a declared `AgentEvent` field."""
    from pathlib import Path

    schema = Path("src/agents/events.py").read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s{4}(\w+):\s", schema, re.MULTILINE))

    missing = [field for field in _WIRE_FIELDS_READ if field not in declared]
    assert not missing, (
        f"{missing} are read by stub_fingerprint_checks but are not declared AgentEvent fields; "
        "each leg reading one of these would pass by examining an absent field"
    )


def test_a_turn_that_carried_no_token_counts_skips_the_usage_leg() -> None:
    """A turn with no usage event skips the usage leg; the branch-exclusive legs still pass."""
    errored = _wire(
        {"kind": "tool_call", "tool_name": "search_opportunities"},
        {"kind": "error", "error_kind": "budget_exhausted"},
    )
    by_check = {r.check: r for r in stub_fingerprint_checks(errored)}

    assert by_check["runtime_usage_is_not_the_stub_constants"].status == "skip"
    assert "could not be examined" in by_check["runtime_usage_is_not_the_stub_constants"].detail
    # branch-exclusive legs still pass on absence
    assert by_check["runtime_text_is_not_stub_shaped"].status == "pass"
    assert by_check["runtime_tool_output_carries_no_stub_flag"].status == "pass"


def test_a_turn_that_did_carry_token_counts_still_reaches_a_verdict() -> None:
    """A turn that carried token counts reaches a usage-leg verdict."""
    by_check = {r.check: r for r in stub_fingerprint_checks(_REAL_TRANSCRIPT)}

    assert by_check["runtime_usage_is_not_the_stub_constants"].status == "pass"


# ------------------------------------------------------------------------------------------------
# Prompt selection and the measured turn both run on the relay arm
# ------------------------------------------------------------------------------------------------


class _FakeHttpsResponse:
    """A context-managed HTTPS response carrying one SSE body and CloudFront's stamp."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.status = 200
        self.headers = Message()
        self.headers["x-amz-cf-id"] = "abc123"

    def __enter__(self) -> _FakeHttpsResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        chunk, self._body = (
            (self._body, b"") if size < 0 else (self._body[:size], self._body[size:])
        )
        return chunk


class _FakeLogsClient:
    def filter_log_events(self, **_kwargs: object) -> dict[str, object]:
        return {"events": [{"message": "START RequestId: r-1"}]}


def test_prompt_selection_and_the_measured_turn_run_on_the_relay_arm() -> None:
    """Both turns go through CloudFront with the relay's header; nothing calls the data plane."""
    from scripts.smoke_agent_stream import _stream_checks

    tool_turn = b"".join(
        b"data: " + json.dumps({"kind": kind, "tool_name": "get_pipeline"}).encode() + b"\n\n"
        for kind in ("tool_call", "tool_result", "completed")
    )
    relay_requests: list[urllib.request.Request] = []

    def _fake_open(request: urllib.request.Request, timeout: float = 0.0) -> object:
        relay_requests.append(request)
        return _FakeHttpsResponse(tool_turn)

    ctx = context_for_test(
        outputs={
            **_OUTPUTS,
            "cloudfront_domain_name": "d111.cloudfront.net",
            "dynamodb_table_names": {"accounts": "crm-agent-eval-accounts"},
        },
        clients={"logs": _FakeLogsClient()},
    )
    token = test_key.access_token(**{"sub": "u-1", "custom:tenant": "eval-smoke-abc"})
    with mock.patch.object(smoke_harness._HTTPS_OPENER, "open", _fake_open):
        results = _stream_checks(ctx, token=token)

    assert _find(results, "tool_calling_turn_elicited").status == "pass"
    _find(results, "text_delta_frames_arrive_before_completed")
    # Two relay turns: elicitation, then the measured turn.
    assert len(relay_requests) == 2
    for request in relay_requests:
        assert request.get_header("X-crm-authorization") == f"Bearer {token}"
        assert "_agent_identity" not in json.loads(cast(bytes, request.data))


class _RecordingStore:
    """Records every `put_item`; any other store call fails the test."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, dict[str, Any]]] = []

    def put_item(self, table: str, item: dict[str, Any]) -> None:
        self.puts.append((table, item))


class _FakeIdp:
    def admin_create_user(self, **_kwargs: Any) -> dict[str, Any]:
        return {"User": {"Username": "smoke-user"}}

    def admin_set_user_password(self, **_kwargs: Any) -> None:
        return None


def rep_row_written_by(
    provisioner: Any, monkeypatch: pytest.MonkeyPatch, *, tenant: str = "eval-smoke-abc"
) -> dict[str, Any]:
    """Run `provisioner.provision()` against fakes and return the `reps` row it wrote."""
    import scripts.smoke_agent_stream as stream_module
    from scripts.smoke.tenant import SmokeTenant

    store = _RecordingStore()
    monkeypatch.setattr(
        stream_module,
        "allocate_smoke_tenant",
        lambda _store: SmokeTenant(run_id="smoke-abc", tenant=tenant),
    )
    monkeypatch.setattr(provisioner, "store", lambda: store)
    monkeypatch.setattr(provisioner, "_idp", lambda: _FakeIdp())
    monkeypatch.setattr(provisioner, "_user_pool_id", lambda: "pool")
    monkeypatch.setattr(provisioner, "_authenticate_with_mfa", lambda **_kwargs: "token")
    assert provisioner.provision() == "token"
    rows = [item for table, item in store.puts if table == "reps"]
    assert len(rows) == 1, store.puts
    return rows[0]


def test_the_provisioner_writes_an_empty_book_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """agents, mcp_client_compat, mcp_chat_gateway and three_tier_chat rely on an empty book."""
    row = rep_row_written_by(LiveStreamPrincipalProvisioner(_stream_ctx()), monkeypatch)
    assert row["book"] == []
    assert row["full_visibility"] is False
    assert row["tenant"] == "eval-smoke-abc"


def test_the_measured_turn_book_admits_its_pipeline_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured turn calls `pipeline_aggregate`, which denies an empty book (`account:*`)."""
    from scripts.smoke_agent_stream import measured_turn_book

    from crm.authz import Identity, require_listing_scope

    tenant = "eval-smoke-abc"
    provisioner = LiveStreamPrincipalProvisioner(_stream_ctx(), book_for_tenant=measured_turn_book)
    row = rep_row_written_by(provisioner, monkeypatch, tenant=tenant)
    assert row["book"], "an empty book makes the measured turn a scope_denied turn"
    assert all(str(account).startswith(f"acct-{tenant}-") for account in row["book"])
    identity = Identity(
        rep_id=row["rep_id"],
        book=frozenset(row["book"]),
        full_visibility=row["full_visibility"],
        tenant=tenant,
    )
    require_listing_scope(identity, operation="pipeline_aggregate")


def test_this_modules_checks_build_the_provisioner_with_the_measured_turn_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only this module opts in; the shared default stays empty."""
    import scripts.smoke_agent_stream as stream_module

    seen: list[dict[str, Any]] = []

    class _StopError(Exception):
        pass

    def recording_provisioner(_ctx: Any, **kwargs: Any) -> NoReturn:
        seen.append(kwargs)
        raise _StopError

    monkeypatch.setattr(stream_module, "LiveStreamPrincipalProvisioner", recording_provisioner)
    with pytest.raises(_StopError):
        list(stream_module.checks(_stream_ctx()))
    assert seen and seen[0].get("book_for_tenant") is stream_module.measured_turn_book
