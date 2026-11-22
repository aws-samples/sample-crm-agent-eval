# SPDX-License-Identifier: MIT
"""Layer 5 T3 smoke: a deployed chat turn resumes by replay, and signals when it cannot.

One disposable smoke user takes three requests in one fresh session S, back to back, through
CloudFront `/agent/*` -> relay -> Runtime:

1. **Seed.** A tool-free turn with no `resume`. Its recorded frames (every kind but `heartbeat`)
   number N. Row `seed_turn_completed`.
2. **Replay.** `resume: true, resume_from: k` with `k = N // 2`. Row `resume_replays_recorded_tail`
   passes when the response is exactly the seed's recorded frames after k, payload for payload, so
   every `event_id` matches and no new `usage` frame appears. The Runtime's session store lives in
   the microVM's memory: when the response opens with `replay_unavailable` instead, the session
   reached a microVM holding nothing for S, and the row is a `defer`
   (:data:`RECYCLED_MICROVM_REASON`), never a pass.
3. **Past the last frame.** `resume: true` with a `resume_from` at or past S's last recorded
   sequence. Row `resume_past_last_frame_signals_replay_unavailable` passes when the first frame is
   `replay_unavailable` and the turn then runs fresh to `completed`.

Model-backed turns are counted against this module's :data:`MODEL_TURN_ALLOTMENTS` share: the
seed, the third request, and the replay request only when it ran fresh. S's history item is deleted
in a `finally` and proved absent by re-read, before the user is removed.

Usage:
    python3 -m scripts.smoke.chat_resume [--profile P] [--region R] [--outputs PATH] [--json]
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final

from scripts.smoke.harness import (
    DEFER,
    CheckResult,
    ExpectedCheckCount,
    SmokeContext,
    exception_detail,
    run_main,
)
from scripts.smoke.history_cleanup import (
    HistoryItemsFactory,
    LiveHistoryItems,
    delete_session_history,
)
from scripts.smoke.three_tier_chat import ModelTurnBudget, budget_refused
from scripts.smoke_agent_stream import (
    LiveStreamPrincipalProvisioner,
    cloudfront_source,
    parse_sse_bytes,
)

from eval.verification import Status

LAYER = 5
RESOURCE_CLASS = "chat_resume"

SEED_CHECK: Final[str] = "seed_turn_completed"
REPLAY_CHECK: Final[str] = "resume_replays_recorded_tail"
UNAVAILABLE_CHECK: Final[str] = "resume_past_last_frame_signals_replay_unavailable"

#: The replay row's `defer_reason` when the resume reached a microVM holding nothing for S.
RECYCLED_MICROVM_REASON: Final[str] = "runtime_session_store_recycled"
#: The `defer_reason` of a check not issued because an earlier step did not complete.
UPSTREAM_REASON: Final[str] = "upstream_check_did_not_run"
#: The `defer_reason` of a check not issued because the module's model-turn share is spent.
SHARE_SPENT_REASON: Final[str] = "model_turn_share_spent"

REPLAY_UNAVAILABLE_KIND: Final[str] = "replay_unavailable"
_UNRECORDED_KINDS: Final[frozenset[str]] = frozenset({"heartbeat", REPLAY_UNAVAILABLE_KIND})

#: A turn needing no tool, so the seed is one short supervisor round.
SEED_PROMPT: Final[str] = "Reply with the single word: ready. Do not call any tool."

#: `(ctx, token, session_id, body) -> frame payloads`, one relay request. Injected by the test.
SendRequest = Callable[[SmokeContext, str, str, Mapping[str, Any]], list[dict[str, Any]]]
#: `(ctx) -> provisioner` exposing `provision()`, `tenant()` and `cleanup()`.
ProvisionerFactory = Callable[[SmokeContext], Any]


def new_session_id() -> str:
    """A fresh session id within InvokeAgentRuntime's 33-256 character rule."""
    return f"smoke-resume-{uuid.uuid4().hex}"


def recorded_frames(frames: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The frames the Runtime records for a turn: every kind but `heartbeat`."""
    return [frame for frame in frames if frame.get("kind") not in _UNRECORDED_KINDS]


def replay_cursor(recorded_count: int) -> int:
    """The `resume_from` of the replay request: half the seed's recorded frames, rounded down."""
    return recorded_count // 2


def _kinds(frames: Iterable[Mapping[str, Any]]) -> list[str]:
    return [str(frame.get("kind")) for frame in frames]


def _row(check: str, status: Status, detail: str, defer_reason: str = "") -> CheckResult:
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check,
        status=status,
        detail=detail,
        defer_reason=defer_reason,
    )


def evaluate_seed(frames: Sequence[Mapping[str, Any]]) -> CheckResult:
    """`seed_turn_completed`: the seed streamed a `completed` frame and no `error` frame."""
    kinds = _kinds(frames)
    ok = "completed" in kinds and "error" not in kinds
    return _row(SEED_CHECK, "pass" if ok else "fail", f"seed turn kinds={kinds}")


def evaluate_replay(
    seed: Sequence[Mapping[str, Any]], cursor: int, replay: Sequence[Mapping[str, Any]]
) -> CheckResult:
    """`resume_replays_recorded_tail`: the replay equals the seed's recorded frames after `cursor`.

    A response opening with `replay_unavailable` is a `defer` (:data:`RECYCLED_MICROVM_REASON`).
    """
    expected = recorded_frames(seed)[cursor:]
    received = [frame for frame in replay if frame.get("kind") != "heartbeat"]
    if received and received[0].get("kind") == REPLAY_UNAVAILABLE_KIND:
        return _row(
            REPLAY_CHECK,
            DEFER,
            f"resume_from={cursor} opened with replay_unavailable and ran fresh "
            f"(kinds={_kinds(received)}): the session reached a microVM holding nothing for it, so "
            "replay was not exercised",
            RECYCLED_MICROVM_REASON,
        )
    expected_ids = [frame.get("event_id") for frame in expected if frame.get("event_id")]
    received_ids = [frame.get("event_id") for frame in received if frame.get("event_id")]
    usage_expected = sum(1 for frame in expected if frame.get("kind") == "usage")
    usage_received = sum(1 for frame in received if frame.get("kind") == "usage")
    ok = bool(expected) and list(received) == list(expected)
    return _row(
        REPLAY_CHECK,
        "pass" if ok else "fail",
        f"resume_from={cursor}: {len(received)} frame(s) replayed, {len(expected)} recorded after "
        f"the cursor; kinds={_kinds(received)} expected {_kinds(expected)}; event ids "
        f"{received_ids} expected {expected_ids}; usage frames {usage_received} expected "
        f"{usage_expected}"
        + ("" if ok else "; the replay is not the recorded tail payload for payload"),
    )


def evaluate_unavailable(cursor: int, frames: Sequence[Mapping[str, Any]]) -> CheckResult:
    """`resume_past_last_frame_signals_replay_unavailable`: first frame is the signal, then a run."""
    received = [frame for frame in frames if frame.get("kind") != "heartbeat"]
    kinds = _kinds(received)
    signalled = bool(received) and received[0].get("kind") == REPLAY_UNAVAILABLE_KIND
    ran = "completed" in kinds and "error" not in kinds
    return _row(
        UNAVAILABLE_CHECK,
        "pass" if signalled and ran else "fail",
        f"resume_from={cursor}: first frame {kinds[0] if kinds else None!r}, kinds={kinds}"
        + ("" if signalled else "; expected replay_unavailable first")
        + ("" if ran else "; expected the turn to run fresh to completed"),
    )


def _not_run(check: str, reason: str, detail: str) -> CheckResult:
    return _row(check, DEFER, detail, reason)


def _live_send(
    ctx: SmokeContext, token: str, session_id: str, body: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """POST one request through CloudFront `/agent/*` and return its parsed frame payloads."""
    source = cloudfront_source(ctx, token=token, body=body, session_id=session_id)
    payloads: list[dict[str, Any]] = []
    for event in parse_sse_bytes(source()):
        try:
            decoded = json.loads(event.data)
        except TypeError, ValueError:
            continue
        if isinstance(decoded, dict):
            payloads.append(decoded)
    return payloads


def checks(
    ctx: SmokeContext,
    *,
    provisioner_factory: ProvisionerFactory = LiveStreamPrincipalProvisioner,
    send: SendRequest = _live_send,
    history_items: HistoryItemsFactory = LiveHistoryItems,
) -> Iterable[CheckResult]:
    """Seed, replay and past-the-end resume in one session; clean up in a `finally`.

    The three check rows are emitted on every path; a step not issued is a `defer` naming why.
    """
    budget = ModelTurnBudget(RESOURCE_CLASS)
    provisioner = provisioner_factory(ctx)
    session_id = new_session_id()
    sent = False
    results: list[CheckResult] = []
    try:
        try:
            token = provisioner.provision()
        except Exception as exc:  # noqa: BLE001 - reported as a failing row, never a crash
            results.append(
                _row(
                    SEED_CHECK,
                    "fail",
                    f"could not provision the smoke user: {exception_detail(exc)}",
                )
            )
            detail = "not issued: the smoke user could not be provisioned"
            results.append(_not_run(REPLAY_CHECK, UPSTREAM_REASON, detail))
            results.append(_not_run(UNAVAILABLE_CHECK, UPSTREAM_REASON, detail))
            return results
        if not budget.spend("seed turn"):
            results.append(budget_refused(budget, "seed turn"))
            detail = "not issued: this module's model-turn share is spent"
            results.append(_not_run(REPLAY_CHECK, SHARE_SPENT_REASON, detail))
            results.append(_not_run(UNAVAILABLE_CHECK, SHARE_SPENT_REASON, detail))
            return results
        sent = True
        try:
            seed = send(ctx, token, session_id, {"prompt": SEED_PROMPT, "session_id": session_id})
        except Exception as exc:  # noqa: BLE001 - reported as a failing row, never a crash
            seed = []
            results.append(_row(SEED_CHECK, "fail", f"seed turn failed: {exception_detail(exc)}"))
        else:
            results.append(evaluate_seed(seed))
        if results[-1].status != "pass":
            detail = "not issued: the seed turn did not complete"
            results.append(_not_run(REPLAY_CHECK, UPSTREAM_REASON, detail))
            results.append(_not_run(UNAVAILABLE_CHECK, UPSTREAM_REASON, detail))
            return results

        recorded = len(recorded_frames(seed))
        cursor = replay_cursor(recorded)
        resume_body = {"prompt": SEED_PROMPT, "session_id": session_id, "resume": True}
        ran_fresh_frames = 0
        try:
            replay = send(ctx, token, session_id, {**resume_body, "resume_from": cursor})
        except Exception as exc:  # noqa: BLE001 - reported as a failing row, never a crash
            results.append(
                _row(REPLAY_CHECK, "fail", f"replay request failed: {exception_detail(exc)}")
            )
        else:
            row = evaluate_replay(seed, cursor, replay)
            if row.status == DEFER:
                budget.spend("replay request that ran fresh")
                ran_fresh_frames = len(recorded_frames(replay))
            results.append(row)

        past_cursor = recorded + ran_fresh_frames
        if not budget.spend("past-the-last-frame resume"):
            results.append(budget_refused(budget, "past-the-last-frame resume"))
            results.append(
                _not_run(
                    UNAVAILABLE_CHECK,
                    SHARE_SPENT_REASON,
                    "not issued: this module's model-turn share is spent",
                )
            )
            return results
        try:
            past = send(ctx, token, session_id, {**resume_body, "resume_from": past_cursor})
        except Exception as exc:  # noqa: BLE001 - reported as a failing row, never a crash
            results.append(
                _row(UNAVAILABLE_CHECK, "fail", f"resume request failed: {exception_detail(exc)}")
            )
        else:
            results.append(evaluate_unavailable(past_cursor, past))
        return results
    finally:
        results.append(budget.result())
        if sent:
            results.extend(
                delete_session_history(
                    ctx,
                    history_items,
                    [(str(provisioner.tenant()), [session_id])],
                    resource=RESOURCE_CLASS,
                )
            )
        results.extend(provisioner.cleanup())


def expected_check_count(_ctx: SmokeContext) -> ExpectedCheckCount:
    """The rows every run emits: the three checks and the model-turn count."""
    return ExpectedCheckCount(
        minimum=4,
        basis=(
            f"{SEED_CHECK}, {REPLAY_CHECK}, {UNAVAILABLE_CHECK} and "
            "model_backed_turns_within_envelope on every path; the history-deletion row appears once "
            "the seed is sent, and provisioner cleanup rows are not counted"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the resume checks and return 0 pass / 1 fail. Declares no token flag."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
