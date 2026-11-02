# SPDX-License-Identifier: MIT
"""`eval.runs` repeats: the admitted range, the job row and payload field, and per-repeat item keys.

Harness: `test_runs_record` (a `moto`-backed `Store`, channel-decoded identities).
"""

from __future__ import annotations

from typing import Any

import pytest

from crm.errors import ValidationFailed
from crm.store import keys
from crm.store.store import Store
from eval import runs
from tests.unit.test_runs_record import _create, store

__all__ = ["store"]


@pytest.mark.parametrize("raw", [1, 2, runs.MAX_SWEEP_REPEATS])
def test_validate_repeats_admits_one_to_the_maximum(raw: int) -> None:
    assert runs.validate_repeats(raw) == raw


def test_an_absent_repeats_is_one() -> None:
    assert runs.validate_repeats(None) == 1


@pytest.mark.parametrize("raw", [0, -1, runs.MAX_SWEEP_REPEATS + 1, True, False, 2.0, "2", [2]])
def test_validate_repeats_refuses_everything_else_naming_repeats(raw: Any) -> None:
    with pytest.raises(ValidationFailed) as refused:
        runs.validate_repeats(raw)
    assert refused.value.source == "repeats"


def test_a_job_row_records_its_repeats_and_the_payload_carries_it(store: Store) -> None:
    record = _create(store, repeats=3, tasks_total=6)
    row = store.get_item("eval_runs", {"run_id": runs.job_row_id("r1")})
    assert row is not None and row["repeats"] == 3
    assert runs.require_run(store, "r1").repeats == 3
    assert record.to_payload()["repeats"] == 3


def test_a_job_row_written_without_repeats_reads_as_one(store: Store) -> None:
    _create(store)
    row = store.get_item("eval_runs", {"run_id": runs.job_row_id("r1")})
    assert row is not None
    store.put_item("eval_runs", {name: value for name, value in row.items() if name != "repeats"})
    record = runs.require_run(store, "r1")
    assert record.repeats == 1 and record.to_payload()["repeats"] == 1


@pytest.mark.parametrize("stored", [0, runs.MAX_SWEEP_REPEATS + 1, "2", 1.5])
def test_an_unreadable_stored_repeats_is_refused(store: Store, stored: object) -> None:
    _create(store)
    store.update_item(
        "eval_runs",
        {"run_id": runs.job_row_id("r1")},
        set_values={"repeats": stored},
        condition=None,
    )
    with pytest.raises(ValidationFailed) as refused:
        runs.require_run(store, "r1")
    assert refused.value.source == "repeats"


def test_create_run_refuses_a_repeats_out_of_range(store: Store) -> None:
    with pytest.raises(ValidationFailed):
        _create(store, repeats=runs.MAX_SWEEP_REPEATS + 1)
    assert runs.read_run(store, "r1") is None


def test_repeat_zero_keeps_the_single_repeat_result_id() -> None:
    assert runs.result_id(1, 0, "gt-01") == keys.eval_result_id(1, "gt-01")


def test_result_ids_are_distinct_across_every_arm_repeat_and_task() -> None:
    cells = [
        (arm, repeat, task)
        for arm in range(2)
        for repeat in range(runs.MAX_SWEEP_REPEATS)
        for task in ("gt-01", "gt-02")
    ]
    assert len({runs.result_id(*cell) for cell in cells}) == len(cells)


@pytest.mark.parametrize("repeat_index", [-1, runs.MAX_SWEEP_REPEATS, True])
def test_result_id_refuses_a_repeat_index_out_of_range(repeat_index: Any) -> None:
    with pytest.raises(ValueError, match="repeat_index"):
        runs.result_id(0, repeat_index, "gt-01")


def test_each_repeat_of_one_task_is_its_own_counted_row(store: Store) -> None:
    _create(store, repeats=2, tasks_total=2)
    for repeat_index in (0, 1):
        assert runs.record_item(
            store,
            run_id="r1",
            arm_index=0,
            repeat_index=repeat_index,
            task_id="gt-01",
            arm="anthropic",
            item_status="done",
        )
    assert not runs.record_item(
        store,
        run_id="r1",
        arm_index=0,
        repeat_index=1,
        task_id="gt-01",
        arm="anthropic",
        item_status="done",
    ), "a retried repeat is not counted twice"
    rows = [
        store.get_item("eval_results", {"run_id": "r1", "result_id": runs.result_id(0, r, "gt-01")})
        for r in (0, 1)
    ]
    assert [row is not None and row["repeat_index"] for row in rows] == [0, 1]
    assert runs.require_run(store, "r1").tasks_done == 2
