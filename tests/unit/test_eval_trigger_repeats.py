# SPDX-License-Identifier: MIT
"""`eval_trigger`'s `repeats` argument through the real handler: submit, estimate, run payload.

Same harness as `test_eval_trigger_authz.py`: the recorded REST v1 proxy event, a `moto`-backed
`Store` and a recording Step Functions client. Assertions read the store and the proxy response.
"""

from __future__ import annotations

from typing import Any

import pytest

from crm.store.store import Store
from eval import runs
from eval.trigger import sweep_shape
from tests.unit.test_eval_trigger_authz import Sfn, _call, _event, _job_rows, sfn, store

__all__ = ["sfn", "store"]


@pytest.mark.parametrize("repeats", [0, -1, runs.MAX_SWEEP_REPEATS + 1, "2", True, 2.0, [2]])
@pytest.mark.parametrize("action", ["submit", "estimate"])
def test_a_repeats_out_of_range_or_not_an_integer_is_400_and_writes_nothing(
    store: Store, sfn: Sfn, action: str, repeats: Any
) -> None:
    status, body = _call(store, sfn, _event({"action": action, "repeats": repeats}, full=True))
    assert status == 400
    assert body["error"]["code"] == "validation_failed"
    assert body["error"]["source"] == "repeats"
    assert _job_rows(store) == [] and runs.read_lock(store) is None and sfn.started == []


def test_a_stub_submit_without_repeats_runs_one_repeat(store: Store, sfn: Sfn) -> None:
    status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
    families, task_count = sweep_shape()
    assert status == 200
    assert run["repeats"] == 1 and run["tasks_total"] == len(families) * task_count


@pytest.mark.parametrize("repeats", [2, runs.MAX_SWEEP_REPEATS])
def test_a_stub_submit_records_its_repeats_and_counts_every_item(
    store: Store, sfn: Sfn, repeats: int
) -> None:
    status, run = _call(store, sfn, _event({"action": "submit", "repeats": repeats}, full=True))
    families, task_count = sweep_shape()
    assert status == 200
    assert run["repeats"] == repeats
    assert run["tasks_total"] == len(families) * repeats * task_count
    (row,) = _job_rows(store)
    stored = store.get_item("eval_runs", {"run_id": str(row["run_id"])})
    assert stored is not None and stored["repeats"] == repeats
    status, polled = _call(
        store, sfn, _event({"action": "status", "run_id": run["run_id"]}, full=True)
    )
    assert status == 200 and polled["repeats"] == repeats


def test_the_estimate_scales_with_repeats(store: Store, sfn: Sfn) -> None:
    status_one, one = _call(store, sfn, _event({"action": "estimate"}, full=True))
    status_three, three = _call(store, sfn, _event({"action": "estimate", "repeats": 3}, full=True))
    assert (status_one, status_three) == (200, 200)
    assert (one["repeats"], three["repeats"]) == (1, 3)
    assert one["task_count"] == three["task_count"]
    assert three["estimate_usd"] == pytest.approx(3 * one["estimate_usd"], abs=0.02)
    assert set(three["per_arm_usd"]) == set(one["per_arm_usd"])
    for arm, cost in one["per_arm_usd"].items():
        assert three["per_arm_usd"][arm] == pytest.approx(3 * cost, abs=0.02)


def test_a_live_submit_records_the_estimate_for_its_repeats(store: Store, sfn: Sfn) -> None:
    single = runs.estimate(store, task_count=sweep_shape()[1], families=sweep_shape()[0])
    status, run = _call(
        store,
        sfn,
        _event({"action": "submit", "mode": "live", "confirmed": True, "repeats": 2}, admin=True),
    )
    assert status == 200
    record = runs.require_run(store, run["run_id"])
    assert record.repeats == 2
    assert record.estimate_usd == pytest.approx(2 * single.estimate_usd)
