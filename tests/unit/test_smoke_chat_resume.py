# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.chat_resume`: the deployed resume smoke's three rows.

The fake relay drives the real `agents.runtime_entrypoint.run_turn` over an
`InMemorySessionStore` per simulated microVM, so the rows are judged against the Runtime's actual
replay semantics. Variants model a recycled microVM and the broken behaviours each row must catch.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import scripts.smoke.three_tier_chat as three_tier_chat
from scripts.smoke import chat_resume
from scripts.smoke.chat_resume import (
    RECYCLED_MICROVM_REASON,
    REPLAY_CHECK,
    SEED_CHECK,
    SHARE_SPENT_REASON,
    UNAVAILABLE_CHECK,
    UPSTREAM_REASON,
    checks,
    expected_check_count,
)
from scripts.smoke.harness import CheckResult, SmokeContext, context_for_test
from scripts.smoke.history_cleanup import HISTORY_ITEMS_DELETED_CHECK

from agents.events import AgentEvent
from agents.runtime_entrypoint import InMemorySessionStore, run_turn
from crm.store.keys import history_key

_TENANT = "eval-smoke-resume"
_TOKEN = "token-a"
_THREE_ROWS = (SEED_CHECK, REPLAY_CHECK, UNAVAILABLE_CHECK)


def _turn_events(run: int) -> Iterator[AgentEvent]:
    """One fresh tool-free turn: 4 recorded frames and one unrecorded heartbeat."""
    event_id = f"ev-{run}"
    yield AgentEvent(kind="text_delta", text="re", event_id=event_id, partial=True)
    yield AgentEvent(kind="heartbeat")
    yield AgentEvent(kind="text_delta", text="ady", event_id=event_id, partial=True)
    yield AgentEvent(kind="usage", input_tokens=10 + run, output_tokens=2)
    yield AgentEvent(kind="completed", text="ready", input_tokens=10 + run, output_tokens=2)


class _FakeRelay:
    """`send` over the real `run_turn`; `recycle_on` names request indexes that hit a fresh microVM.

    `mode` selects a broken Runtime: `"ignore_resume"` runs every request fresh, `"off_by_one"`
    replays from one frame early, `"no_signal"` omits `replay_unavailable`.
    """

    def __init__(self, *, recycle_on: frozenset[int] = frozenset(), mode: str = "real") -> None:
        self.store = InMemorySessionStore()
        self.recycle_on = recycle_on
        self.mode = mode
        self.bodies: list[dict[str, Any]] = []
        self.fresh_runs = 0
        self.raise_on: frozenset[int] = frozenset()

    def __call__(
        self, _ctx: SmokeContext, token: str, session_id: str, body: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        index = len(self.bodies)
        self.bodies.append(dict(body))
        assert token == _TOKEN
        assert body["session_id"] == session_id
        if index in self.raise_on:
            raise TimeoutError("relay stream timed out")
        if index in self.recycle_on:
            self.store = InMemorySessionStore()
        resume = body.get("resume") is True and self.mode != "ignore_resume"
        resume_from = int(body.get("resume_from", 0))
        if self.mode == "off_by_one" and resume:
            resume_from -= 1
        last = self.store.last_sequence(session_id)
        if not (resume and 0 <= resume_from < last):
            self.fresh_runs += 1
        lines = list(
            run_turn(
                SimpleNamespace(session_id=session_id),  # type: ignore[arg-type]
                _turn_events(self.fresh_runs),
                store=self.store,
                resume=resume,
                resume_from=resume_from,
            )
        )
        frames = [json.loads(line.removeprefix("data: ")) for line in lines]
        if self.mode == "no_signal":
            frames = [frame for frame in frames if frame["kind"] != "replay_unavailable"]
        return frames


class _FakeProvisioner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.allocated = ""
        self.cleaned = False

    def provision(self) -> str:
        if self.fail:
            raise RuntimeError("cognito unavailable")
        self.allocated = _TENANT
        return _TOKEN

    def tenant(self) -> str:
        return self.allocated

    def cleanup(self) -> Iterator[CheckResult]:
        self.cleaned = True
        yield CheckResult(resource="smoke_principal", check="cleaned_up", status="pass")


class _FakeHistoryItems:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []

    def get(self, rep_id: str) -> dict[str, Any] | None:
        return self.items.get(rep_id)

    def delete(self, rep_id: str) -> None:
        self.deleted.append(rep_id)
        self.items.pop(rep_id, None)


def _run(
    relay: _FakeRelay, *, provisioner: _FakeProvisioner | None = None
) -> tuple[dict[str, CheckResult], list[CheckResult], _FakeHistoryItems, _FakeProvisioner]:
    prov = provisioner if provisioner is not None else _FakeProvisioner()
    history = _FakeHistoryItems()
    rows = list(
        checks(
            context_for_test(),
            provisioner_factory=lambda _ctx: prov,
            send=relay,
            history_items=lambda _ctx: history,
        )
    )
    by_check = {row.check: row for row in rows}
    return by_check, rows, history, prov


def test_a_replaying_runtime_passes_all_three_rows() -> None:
    relay = _FakeRelay()
    rows, _all, history, prov = _run(relay)
    for check in _THREE_ROWS:
        assert rows[check].status == "pass", rows[check].detail
    assert rows["model_backed_turns_within_envelope"].status == "pass"
    assert "2 model-backed turn(s)" in rows["model_backed_turns_within_envelope"].detail
    assert relay.fresh_runs == 2
    assert rows[HISTORY_ITEMS_DELETED_CHECK].status == "pass"
    assert history.deleted == [history_key(_TENANT, relay.bodies[0]["session_id"])]
    assert prov.cleaned


def test_the_requests_are_seed_then_replay_at_half_then_past_the_last_frame() -> None:
    relay = _FakeRelay()
    _run(relay)
    seed, replay, past = relay.bodies
    assert "resume" not in seed and "resume_from" not in seed
    assert replay["resume"] is True and replay["resume_from"] == 2
    assert past["resume"] is True and past["resume_from"] == 4
    assert {body["session_id"] for body in relay.bodies} == {seed["session_id"]}
    assert 33 <= len(seed["session_id"]) <= 256


def test_a_recycled_microvm_defers_the_replay_row_and_still_checks_the_signal() -> None:
    relay = _FakeRelay(recycle_on=frozenset({1}))
    rows, _all, _history, _prov = _run(relay)
    assert rows[REPLAY_CHECK].status == "defer"
    assert rows[REPLAY_CHECK].defer_reason == RECYCLED_MICROVM_REASON
    assert rows[UNAVAILABLE_CHECK].status == "pass", rows[UNAVAILABLE_CHECK].detail
    assert relay.bodies[2]["resume_from"] == 8
    assert "3 model-backed turn(s)" in rows["model_backed_turns_within_envelope"].detail


def test_a_runtime_that_ignores_resume_fails_both_resume_rows() -> None:
    rows, _all, _history, _prov = _run(_FakeRelay(mode="ignore_resume"))
    assert rows[SEED_CHECK].status == "pass"
    assert rows[REPLAY_CHECK].status == "fail"
    assert rows[UNAVAILABLE_CHECK].status == "fail"


def test_a_replay_from_the_wrong_offset_fails() -> None:
    rows, _all, _history, _prov = _run(_FakeRelay(mode="off_by_one"))
    assert rows[REPLAY_CHECK].status == "fail"
    assert "not the recorded tail" in rows[REPLAY_CHECK].detail


def test_a_runtime_without_the_signal_fails_the_unavailable_row() -> None:
    rows, _all, _history, _prov = _run(_FakeRelay(mode="no_signal"))
    assert rows[REPLAY_CHECK].status == "pass"
    assert rows[UNAVAILABLE_CHECK].status == "fail"
    assert "expected replay_unavailable first" in rows[UNAVAILABLE_CHECK].detail


def test_a_replay_with_a_new_usage_frame_fails() -> None:
    seed: list[dict[str, Any]] = [
        {"kind": "text_delta", "event_id": "ev-1"},
        {"kind": "usage", "input_tokens": 10},
        {"kind": "completed"},
    ]
    replay: list[dict[str, Any]] = [{"kind": "usage", "input_tokens": 99}, {"kind": "completed"}]
    row = chat_resume.evaluate_replay(seed, 1, replay)
    assert row.status == "fail"
    assert row.check == REPLAY_CHECK


def test_an_empty_replay_never_passes() -> None:
    seed = [{"kind": "completed"}]
    assert chat_resume.evaluate_replay(seed, 1, []).status == "fail"


def test_the_signal_without_a_fresh_run_fails() -> None:
    row = chat_resume.evaluate_unavailable(4, [{"kind": "replay_unavailable"}])
    assert row.status == "fail"
    assert "run fresh to completed" in row.detail


def test_a_failed_seed_defers_both_resume_rows_and_still_cleans_up() -> None:
    relay = _FakeRelay()
    relay.raise_on = frozenset({0})
    rows, _all, history, prov = _run(relay)
    assert rows[SEED_CHECK].status == "fail"
    for check in (REPLAY_CHECK, UNAVAILABLE_CHECK):
        assert rows[check].status == "defer"
        assert rows[check].defer_reason == UPSTREAM_REASON
    assert len(relay.bodies) == 1
    assert rows[HISTORY_ITEMS_DELETED_CHECK].status == "pass"
    assert history.deleted
    assert prov.cleaned


def test_a_provisioning_failure_sends_nothing_and_cleans_up() -> None:
    relay = _FakeRelay()
    rows, _all, history, prov = _run(relay, provisioner=_FakeProvisioner(fail=True))
    assert rows[SEED_CHECK].status == "fail"
    assert rows[REPLAY_CHECK].defer_reason == UPSTREAM_REASON
    assert rows[UNAVAILABLE_CHECK].defer_reason == UPSTREAM_REASON
    assert relay.bodies == []
    assert HISTORY_ITEMS_DELETED_CHECK not in rows
    assert history.deleted == []
    assert prov.cleaned


def test_a_failed_replay_request_fails_its_row_and_still_checks_the_signal() -> None:
    relay = _FakeRelay()
    relay.raise_on = frozenset({1})
    rows, _all, _history, _prov = _run(relay)
    assert rows[REPLAY_CHECK].status == "fail"
    assert "TimeoutError" in rows[REPLAY_CHECK].detail
    assert rows[UNAVAILABLE_CHECK].status == "pass"


def test_the_module_share_is_three_within_the_envelope() -> None:
    allotments = three_tier_chat.MODEL_TURN_ALLOTMENTS
    assert allotments[chat_resume.RESOURCE_CLASS] == 3
    assert sum(allotments.values()) <= three_tier_chat.MODEL_TURN_ENVELOPE


def test_a_spent_share_issues_no_turn_past_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        three_tier_chat,
        "MODEL_TURN_ALLOTMENTS",
        {**three_tier_chat.MODEL_TURN_ALLOTMENTS, chat_resume.RESOURCE_CLASS: 1},
    )
    relay = _FakeRelay()
    rows, _all, _history, _prov = _run(relay)
    assert rows[REPLAY_CHECK].status == "pass"
    assert rows[UNAVAILABLE_CHECK].status == "defer"
    assert rows[UNAVAILABLE_CHECK].defer_reason == SHARE_SPENT_REASON
    assert len(relay.bodies) == 2
    assert relay.fresh_runs == 1


@pytest.mark.parametrize(
    "relay",
    [
        _FakeRelay(),
        _FakeRelay(recycle_on=frozenset({1})),
        _FakeRelay(mode="ignore_resume"),
    ],
    ids=["replaying", "recycled", "ignores-resume"],
)
def test_every_path_emits_the_three_rows_and_meets_the_declared_minimum(relay: _FakeRelay) -> None:
    rows, all_rows, _history, _prov = _run(relay)
    assert set(_THREE_ROWS) <= set(rows)
    counted = [row for row in all_rows if row.resource == chat_resume.RESOURCE_CLASS]
    assert len(counted) >= expected_check_count(context_for_test()).minimum


def test_the_failed_provisioning_path_meets_the_declared_minimum() -> None:
    _rows, all_rows, _history, _prov = _run(_FakeRelay(), provisioner=_FakeProvisioner(fail=True))
    counted = [row for row in all_rows if row.resource == chat_resume.RESOURCE_CLASS]
    assert len(counted) >= expected_check_count(context_for_test()).minimum


def test_the_module_declares_no_token_flag_and_layer_five() -> None:
    source = chat_resume.__file__
    assert source is not None
    assert "--token" not in Path(source).read_text(encoding="utf-8")
    assert chat_resume.LAYER == 5
