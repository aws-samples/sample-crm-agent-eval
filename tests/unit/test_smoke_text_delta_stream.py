# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke_text_delta_stream`.

The provisioner and turn measurement are injected. Concurrent turns must each complete and carry
disjoint, session-bound `event_id`s; the provisioned user is always cleaned up.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable

from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke_agent_stream import ArrivalMeasurement, SseEvent
from scripts.smoke_text_delta_stream import CONCURRENT_SESSIONS, RESOURCE_CLASS, checks


def _measurement(pairs: list[tuple[str, float]]) -> ArrivalMeasurement:
    """Build an ArrivalMeasurement from (kind, arrival_offset) pairs in arrival order."""
    events = tuple(SseEvent(event="", data=json.dumps({"kind": k})) for k, _ in pairs)
    offsets = tuple(off for _, off in pairs)
    spread = (offsets[-1] - offsets[0]) if len(offsets) >= 2 else 0.0
    return ArrivalMeasurement(
        event_count=len(pairs),
        spread_seconds=spread,
        events=events,
        arrival_offsets=offsets,
    )


class _FakeProvisioner:
    def __init__(self, *, raise_on_provision: bool = False) -> None:
        self._raise = raise_on_provision
        self.cleaned_up = False

    def provision(self) -> str:
        if self._raise:
            raise RuntimeError("pool unavailable")
        return "header.payload.sig"

    def tenant(self) -> str:
        return "eval-smoke-tds"

    def cleanup(self) -> Iterable[CheckResult]:
        self.cleaned_up = True
        yield CheckResult(
            resource="smoke_user", check="cleaned_up", status="pass", detail="removed"
        )


def _run(
    measurement: ArrivalMeasurement, *, prov: _FakeProvisioner | None = None
) -> dict[str, str]:
    provisioner = prov if prov is not None else _FakeProvisioner()
    ctx = context_for_test(outputs={}, clients={})
    results = list(
        checks(
            ctx,
            provisioner=provisioner,  # type: ignore[arg-type]
            measure=lambda *_: measurement,
        )
    )
    return {c.check: c.status for c in results}


def test_cleanup_runs_even_when_provisioning_fails() -> None:
    prov = _FakeProvisioner(raise_on_provision=True)
    by_check = _run(_measurement([("completed", 0.10)]), prov=prov)
    assert by_check["provision_smoke_user"] == "fail"
    assert by_check["cleaned_up"] == "pass"
    assert prov.cleaned_up is True


def _kinds_seen(measurement: ArrivalMeasurement) -> list[str]:
    return [json.loads(e.data)["kind"] for e in measurement.events]


# --- session-id length (InvokeAgentRuntime requires 33..256 chars) ---

#: Closed range of session-id lengths InvokeAgentRuntime accepts.
_MIN_SESSION_ID_LEN = 33
_MAX_SESSION_ID_LEN = 256


def test_constructed_session_id_meets_the_invoke_agent_runtime_length_rule() -> None:
    """The session id built inside `checks()` is within 33..256 chars."""
    captured: list[str] = []

    def measure(_ctx: object, _token: str, session_id: str) -> ArrivalMeasurement:
        captured.append(session_id)
        return _measurement([("text_delta", 0.1), ("completed", 0.3)])

    ctx = context_for_test(outputs={}, clients={})
    list(checks(ctx, provisioner=_FakeProvisioner(), measure=measure))  # type: ignore[arg-type]

    assert captured  # the run actually issued a turn
    for session_id in captured:
        assert _MIN_SESSION_ID_LEN <= len(session_id) <= _MAX_SESSION_ID_LEN, session_id


def test_fixture_shape_matches_measure_arrivals_contract() -> None:
    """The fixture's events and arrival_offsets are parallel, as `measure_arrivals` returns."""
    m = _measurement([("text_delta", 0.1), ("completed", 0.2)])
    assert len(m.events) == len(m.arrival_offsets)
    assert _kinds_seen(m) == ["text_delta", "completed"]
    assert isinstance(m, ArrivalMeasurement)


# --- concurrent sessions ---


def _session_turn(session_id: str, *, event_id: str | None = None) -> ArrivalMeasurement:
    """A completed turn whose `text_delta` frames carry `event_id` (default `<session_id>:r1`)."""
    delta_id = f"{session_id}:r1" if event_id is None else event_id
    frames: list[dict[str, object]] = [
        {"kind": "text_delta", "text": "a", "event_id": delta_id, "partial": True},
        {"kind": "text_delta", "text": "b", "event_id": delta_id, "partial": True},
        {"kind": "completed"},
    ]
    events = tuple(SseEvent(event="", data=json.dumps(f)) for f in frames)
    return ArrivalMeasurement(
        event_count=3, spread_seconds=0.2, events=events, arrival_offsets=(0.1, 0.2, 0.3)
    )


def _run_concurrent(
    turn_for: Callable[[str], ArrivalMeasurement],
) -> tuple[dict[str, CheckResult], list[str]]:
    sessions: list[str] = []
    barrier = threading.Barrier(CONCURRENT_SESSIONS, timeout=5)

    def measure(_ctx: object, _token: str, session_id: str) -> ArrivalMeasurement:
        sessions.append(session_id)
        barrier.wait()
        return turn_for(session_id)

    ctx = context_for_test(outputs={}, clients={})
    rows = list(checks(ctx, provisioner=_FakeProvisioner(), measure=measure))  # type: ignore[arg-type]
    return {r.check: r for r in rows}, sessions


def test_concurrent_sessions_run_together_and_get_distinct_event_ids() -> None:
    """Both turns are in flight at once (the barrier would time out otherwise) and pass."""
    by_check, sessions = _run_concurrent(_session_turn)
    assert len(sessions) == CONCURRENT_SESSIONS == len(set(sessions))
    assert by_check["concurrent_sessions_completed"].status == "pass"
    assert by_check["concurrent_sessions_distinct_event_ids"].status == "pass"
    assert set(by_check) == {
        "provision_smoke_user",
        "concurrent_sessions_completed",
        "concurrent_sessions_distinct_event_ids",
        "cleaned_up",
    }
    assert all(
        row.resource == RESOURCE_CLASS for row in by_check.values() if row.check != "cleaned_up"
    )


def test_an_event_id_shared_across_sessions_fails() -> None:
    """A per-request id captured once and reused by every instance is the V2 snapshot hazard."""
    by_check, _ = _run_concurrent(lambda sid: _session_turn(sid, event_id=f"{sid}:r1-shared"))
    assert by_check["concurrent_sessions_distinct_event_ids"].status == "pass"
    by_check, _ = _run_concurrent(lambda sid: _session_turn(sid, event_id="snapshot:r1"))
    row = by_check["concurrent_sessions_distinct_event_ids"]
    assert row.status == "fail"
    assert "emitted by" in row.detail


def test_an_event_id_not_bound_to_its_session_fails() -> None:
    by_check, _ = _run_concurrent(lambda sid: _session_turn(sid, event_id=f"other-{sid}:r1"))
    assert by_check["concurrent_sessions_distinct_event_ids"].status == "fail"


def test_a_turn_without_event_ids_fails() -> None:
    by_check, _ = _run_concurrent(lambda sid: _session_turn(sid, event_id=""))
    assert by_check["concurrent_sessions_distinct_event_ids"].status == "fail"


def test_an_incomplete_concurrent_turn_fails_and_stops() -> None:
    first: list[str] = []

    def turn(sid: str) -> ArrivalMeasurement:
        if not first:
            first.append(sid)
            return _session_turn(sid)
        return _measurement([("text_delta", 0.1)])

    by_check, _ = _run_concurrent(turn)
    assert by_check["concurrent_sessions_completed"].status == "fail"
    assert "concurrent_sessions_distinct_event_ids" not in by_check
