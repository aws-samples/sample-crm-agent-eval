# SPDX-License-Identifier: MIT
"""Layer 6 T3 smoke: concurrent streaming sessions keep their `text_delta` frames apart.

Two turns run concurrently through CloudFront under distinct session ids; each must carry exactly
one `completed` frame, and their `text_delta` `event_id`s must be disjoint and carry their own
session id. Reuses `smoke_agent_stream`'s `LiveStreamPrincipalProvisioner`,
`cloudfront_source` and `measure_arrivals`; the `text_delta`-before-`completed` ordering of a turn is
`smoke_agent_stream`'s `text_delta_frames_arrive_before_completed`. A live run issues two turns.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Final

from scripts.smoke.harness import CheckResult, SmokeContext, run_main
from scripts.smoke_agent_stream import (
    ArrivalMeasurement,
    LiveStreamPrincipalProvisioner,
    SseEvent,
    cloudfront_source,
    measure_arrivals,
)

LAYER = 6
RESOURCE_CLASS = "text_delta_stream"

#: A prompt that elicits a text answer, so the streaming path emits `text_delta` frames.
_PROMPT: Final[str] = "In one sentence, what can you help me do?"

#: Turns issued at once under distinct session ids.
CONCURRENT_SESSIONS: Final[int] = 2

#: `(ctx, token, session_id) -> ArrivalMeasurement`; the live default times a CloudFront stream.
MeasureTurn = Callable[[SmokeContext, str, str], ArrivalMeasurement]


def _live_measure(ctx: SmokeContext, token: str, session_id: str) -> ArrivalMeasurement:
    """Issue one authenticated streaming turn through CloudFront and time each frame's arrival."""
    source = cloudfront_source(
        ctx,
        token=token,
        body={"prompt": _PROMPT, "session_id": session_id, "resume_from": 0},
        session_id=session_id,
    )
    return measure_arrivals(source, clock=time.monotonic)


def _event_kind(event: SseEvent) -> str:
    """The `kind` field of one SSE record's JSON payload, or `""` if it has none."""
    try:
        return str(json.loads(event.data).get("kind") or "")
    except (json.JSONDecodeError, AttributeError):
        return ""


def _text_delta_event_ids(measurement: ArrivalMeasurement) -> list[str]:
    """The `event_id` of every `text_delta` frame, in arrival order; `""` where one has none."""
    ids: list[str] = []
    for event in measurement.events:
        try:
            payload = json.loads(event.data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("kind") == "text_delta":
            ids.append(str(payload.get("event_id") or ""))
    return ids


def evaluate_concurrent_sessions(
    turns: Sequence[tuple[str, ArrivalMeasurement]],
) -> Iterable[CheckResult]:
    """Yield the concurrent-session rows for `(session_id, measurement)` pairs run together.

    Every turn must carry exactly one `completed` frame and at least one `text_delta` `event_id`;
    each id must start with its own session id, and no id may appear in two turns.
    """
    completed = [sum(1 for e in m.events if _event_kind(e) == "completed") == 1 for _, m in turns]
    all_completed = len(turns) >= 2 and all(completed)
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="concurrent_sessions_completed",
        status="pass" if all_completed else "fail",
        detail=f"{sum(completed)} of {len(turns)} concurrent turn(s) carried exactly one completed frame",
    )
    if not all_completed:
        return
    ids_by_session = {session_id: _text_delta_event_ids(m) for session_id, m in turns}
    problems: list[str] = []
    for session_id, ids in ids_by_session.items():
        if not ids:
            problems.append(f"{session_id}: no text_delta event_id")
        foreign = [i for i in ids if not i.startswith(f"{session_id}:")]
        if foreign:
            problems.append(
                f"{session_id}: event_id(s) not bound to the session {sorted(set(foreign))}"
            )
    seen: dict[str, str] = {}
    for session_id, ids in ids_by_session.items():
        for event_id in set(ids):
            if event_id in seen and seen[event_id] != session_id:
                problems.append(
                    f"event_id {event_id!r} emitted by {seen[event_id]} and {session_id}"
                )
            seen.setdefault(event_id, session_id)
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="concurrent_sessions_distinct_event_ids",
        status="fail" if problems else "pass",
        detail=(
            "; ".join(problems)
            if problems
            else f"{len(seen)} distinct text_delta event_id(s) across {len(turns)} concurrent "
            "sessions, each bound to its own session"
        ),
    )


def checks(
    ctx: SmokeContext,
    *,
    provisioner: LiveStreamPrincipalProvisioner | None = None,
    measure: MeasureTurn | None = None,
) -> Iterable[CheckResult]:
    """Provision a disposable user, measure concurrent streaming turns, assert isolation, clean up.

    Args:
        ctx: The smoke context.
        provisioner: Mints and removes the disposable MFA smoke user; the live entry point supplies
            a `LiveStreamPrincipalProvisioner`, the offline test injects a fake.
        measure: Issues and times one turn. Defaults to the live CloudFront path; the offline test
            injects a canned `ArrivalMeasurement` so no socket is opened.
    """
    prov = provisioner if provisioner is not None else LiveStreamPrincipalProvisioner(ctx)
    measurer = measure if measure is not None else _live_measure
    session_ids = [f"t3-text-delta-{_run_suffix()}" for _ in range(CONCURRENT_SESSIONS)]
    try:
        try:
            token = prov.provision()
        except Exception as exc:  # noqa: BLE001 - one actionable failing check, inventory preserved
            yield CheckResult(
                resource=RESOURCE_CLASS,
                check="provision_smoke_user",
                status="fail",
                detail=f"could not provision the streaming smoke user: {type(exc).__name__}: {exc}",
            )
            return
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="provision_smoke_user",
            status="pass",
            detail=f"disposable smoke user provisioned under tenant {prov.tenant()}",
        )
        try:
            with ThreadPoolExecutor(max_workers=len(session_ids)) as pool:
                futures = [pool.submit(measurer, ctx, token, sid) for sid in session_ids]
                measurements = [future.result() for future in futures]
        except Exception as exc:  # noqa: BLE001 - surfaced as one actionable failing check
            yield CheckResult(
                resource=RESOURCE_CLASS,
                check="stream_turn_completed",
                status="fail",
                detail=f"a streaming turn failed: {type(exc).__name__}: {exc}",
            )
            return
        yield from evaluate_concurrent_sessions(list(zip(session_ids, measurements, strict=True)))
    finally:
        yield from prov.cleanup()


def _run_suffix() -> str:
    """A unique 32-char `uuid4().hex` suffix; the prefixed session id meets InvokeAgentRuntime's
    33-256 character limit.
    """
    import uuid

    return uuid.uuid4().hex


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Live path provisions a user and runs two concurrent turns through CloudFront."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
    )


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
