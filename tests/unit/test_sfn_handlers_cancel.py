# SPDX-License-Identifier: MIT
"""Cooperative cancel through `eval.sfn_handlers`, checked at each item.

(a) a run cancelled while `queued` seeds nothing and invokes no Runtime; (b) each item reads the
flag first and skips without invoking the Runtime; (c) aggregate, teardown and finalize always run,
so partial results are published and the lock is released. Harness: `test_sfn_handlers`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from eval.harness import DEFAULT_ARM_SPECS
from eval.runs import mark_running, read_lock, request_cancel, require_run
from tests.unit.test_sfn_handlers import STARTER, Sweep, make_sweep, mocked_aws


@pytest.fixture
def sweep(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sweep]:
    with mocked_aws(monkeypatch):
        yield make_sweep(monkeypatch, tasks=3)


def _cancel(sweep: Sweep) -> None:
    disposition, _ = request_cancel(sweep.store, run_id=sweep.run_id, identity=STARTER)
    assert disposition == "accepted"


def test_a_run_cancelled_while_queued_seeds_nothing_and_runs_no_task(sweep: Sweep) -> None:
    _cancel(sweep)
    start = sweep.control("start")
    assert start == {"refused": False, "cancelled_before_start": True, "task_refs": []}

    final = sweep.drive()
    record = require_run(sweep.store, sweep.run_id)
    assert final["status"] == "cancelled"
    assert (record.tasks_done, record.tasks_failed, record.tasks_skipped) == (0, 0, 6)
    assert sweep.runtime.requests == []
    assert sweep.store.get_item("eval_runs", {"run_id": sweep.run_id}) is None, "no catalog row"
    assert sweep.tenant_rows() == 0
    assert "demo-baseline.json" in sweep.objects(), "the delta check needs its baseline"
    inventory = json.loads(sweep.objects()["verification-inventory/eval_sweep.json"])
    assert {entry["check"]: entry["status"] for entry in inventory["checks"]} == {
        "demo_tenant_untouched": "pass",
        "writes_only_under_smoke_tenant": "fail",
    }, "a run that seeded nothing wrote no row of its own"
    assert record.failure_summary is None
    assert "matrix.json" not in sweep.objects()
    assert record.matrix_report is None
    assert record.cancel_requested is True
    assert read_lock(sweep.store) is None


def test_a_cancel_racing_start_is_honoured_before_seeding(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel landing between start's read and its `queued -> running` write still wins."""
    from eval import sfn_handlers

    def _cancel_then_mark(store: Any, **kwargs: Any) -> Any:
        _cancel(sweep)
        return mark_running(store, **kwargs)

    monkeypatch.setattr(sfn_handlers, "mark_running", _cancel_then_mark)
    assert sweep.control("start") == {
        "refused": False,
        "cancelled_before_start": True,
        "task_refs": [],
    }
    assert require_run(sweep.store, sweep.run_id).tasks_skipped == 6


def test_a_cancel_mid_sweep_skips_every_later_item_and_publishes_the_partial(
    sweep: Sweep,
) -> None:
    """Items before the cancel are done and counted; every later one is skipped without a
    Runtime call; the run is `cancelled` and its partial matrix covers the done items only."""

    def _cancel_before_third(index: int, _ref: Mapping[str, Any]) -> None:
        if index == 2:
            _cancel(sweep)

    final = sweep.drive(before_item=_cancel_before_third)
    record = require_run(sweep.store, sweep.run_id)
    assert final["status"] == "cancelled"
    assert (record.tasks_done, record.tasks_skipped, record.tasks_failed) == (2, 4, 0)
    assert len(sweep.runtime.requests) == 2
    assert record.cost_spent_usd >= 0
    assert record.matrix_report is not None
    arms = record.matrix_report["arms"]
    assert len(arms) == 1, "only the first arm has a done item"
    assert (
        arms[0]["aggregate_score"]["scored_task_count"]
        + arms[0]["aggregate_score"]["unscored_task_count"]
        == 2
    )
    assert sweep.tenant_rows() == 0
    assert record.teardown_status == "ok"
    assert read_lock(sweep.store) is None


def test_an_in_flight_item_completes_when_the_cancel_lands_during_it(sweep: Sweep) -> None:
    """The cancel lands while item 0's turn runs: item 0 is done and counted, item 1 is skipped."""
    refs = sweep.control("start")["task_refs"]
    sweep.control("seed")
    original = sweep.runtime.invoke_agent_runtime

    def _cancel_mid_turn(**kwargs: Any) -> dict[str, Any]:
        _cancel(sweep)
        return original(**kwargs)

    sweep.runtime.invoke_agent_runtime = _cancel_mid_turn  # type: ignore[method-assign]
    assert sweep.task(refs[0]["arm_index"], refs[0]["task_id"]) == {"item_status": "done"}
    assert sweep.task(refs[1]["arm_index"], refs[1]["task_id"]) == {
        "item_status": "skipped_cancelled"
    }
    record = require_run(sweep.store, sweep.run_id)
    assert (record.tasks_done, record.tasks_skipped) == (1, 1)


def test_a_cancel_after_the_last_item_still_completes(sweep: Sweep) -> None:
    """No item was skipped, so the run is `completed`; the flag stays for audit."""
    start = sweep.control("start")
    sweep.control("seed")
    for ref in start["task_refs"]:
        sweep.task(ref["arm_index"], ref["task_id"])
    _cancel(sweep)
    sweep.control("aggregate", error=None)
    sweep.control("teardown")
    final = sweep.control("finalize", error=None, teardown_error=None)
    record = require_run(sweep.store, sweep.run_id)
    assert final["status"] == "completed"
    assert record.cancel_requested is True
    assert record.tasks_done == len(DEFAULT_ARM_SPECS) * 3
    assert read_lock(sweep.store) is None
