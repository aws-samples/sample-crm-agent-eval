# SPDX-License-Identifier: MIT
"""`Store.update_item`, `Store.put_if_absent` and `query_gsi` ordering against DynamoDB Local.

Covers condition evaluation by the real engine, `ADD` counter atomicity under concurrency, a
conditional-create race, the sparse `reps` settings and conversation items, and descending GSI
pagination.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from crm.errors import Conflict
from crm.store import keys
from crm.store.store import Store

_RACERS = 8


def _run_item(run_id: str, started_at: str, **extra: object) -> dict[str, object]:
    return {"run_id": run_id, "status": "job", "started_at": started_at, **extra}


def test_conditional_update_applies_when_the_condition_holds(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """A satisfied condition applies `SET` + `ADD` and returns the whole new item."""
    store, _ = dynamodb_local_store
    store.put_item("eval_runs", _run_item("job#a", "2026-10-07T00:00:00Z", job_status="queued"))
    updated = store.update_item(
        "eval_runs",
        {"run_id": "job#a"},
        set_values={"job_status": "running", "running_at": "2026-10-07T00:00:01Z"},
        add_values={"tasks_done": 1, "cost_spent_usd": Decimal("0.0125")},
        condition="#js = :queued",
        names={"#js": "job_status"},
        values={":queued": "queued"},
    )
    assert updated == {
        "run_id": "job#a",
        "status": "job",
        "started_at": "2026-10-07T00:00:00Z",
        "job_status": "running",
        "running_at": "2026-10-07T00:00:01Z",
        "tasks_done": 1,
        "cost_spent_usd": 0.0125,
    }


def test_conditional_update_failure_is_conflict_and_writes_nothing(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """A false condition is `Conflict` and leaves the item as it was."""
    store, _ = dynamodb_local_store
    store.put_item("eval_runs", _run_item("job#b", "2026-10-07", job_status="completed"))
    with pytest.raises(Conflict):
        store.update_item(
            "eval_runs",
            {"run_id": "job#b"},
            set_values={"job_status": "cancelling"},
            add_values={"tasks_done": 1},
            condition="#js IN (:queued, :running)",
            names={"#js": "job_status"},
            values={":queued": "queued", ":running": "running"},
        )
    item = store.get_item("eval_runs", {"run_id": "job#b"})
    assert item is not None and item["job_status"] == "completed" and "tasks_done" not in item


def test_update_without_opt_in_never_upserts_and_reports_target_missing(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """The real engine refuses an update to an absent key; with opt-in it creates the item."""
    store, _ = dynamodb_local_store
    with pytest.raises(Conflict) as unconditioned:
        store.update_item(
            "eval_runs", {"run_id": "job#missing"}, set_values={"a": 1}, condition=None
        )
    assert unconditioned.value.reason == "target_missing"
    with pytest.raises(Conflict) as excinfo:
        store.update_item(
            "eval_runs",
            {"run_id": "job#missing"},
            set_values={"job_status": "running"},
            condition="#js = :queued",
            names={"#js": "job_status"},
            values={":queued": "queued"},
        )
    assert excinfo.value.reason == "target_missing"
    assert store.get_item("eval_runs", {"run_id": "job#missing"}) is None
    created = store.update_item(
        "eval_runs",
        {"run_id": "job#missing"},
        set_values={"job_status": "queued"},
        condition=None,
        create_if_absent=True,
    )
    assert created == {"run_id": "job#missing", "job_status": "queued"}


def test_condition_false_on_a_present_item_carries_no_target_missing_reason(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """`ALL_OLD` on failure lets the store tell a changed item from an absent one."""
    store, _ = dynamodb_local_store
    store.put_item("eval_runs", _run_item("job#d", "2026-10-07", job_status="completed"))
    with pytest.raises(Conflict) as excinfo:
        store.update_item(
            "eval_runs",
            {"run_id": "job#d"},
            set_values={"job_status": "running"},
            condition="#js = :queued",
            names={"#js": "job_status"},
            values={":queued": "queued"},
        )
    assert excinfo.value.reason is None


def test_concurrent_add_updates_are_all_counted(dynamodb_local_store: tuple[Store, str]) -> None:
    """`ADD` increments from concurrent writers are atomic: none is lost."""
    store, _ = dynamodb_local_store
    store.put_item("eval_runs", _run_item("job#c", "2026-10-07", tasks_done=0))

    def increment(_: int) -> None:
        store.update_item(
            "eval_runs",
            {"run_id": "job#c"},
            set_values={},
            add_values={"tasks_done": 1},
            condition="attribute_exists(run_id)",
        )

    with ThreadPoolExecutor(max_workers=_RACERS) as pool:
        list(pool.map(increment, range(_RACERS * 4)))
    item = store.get_item("eval_runs", {"run_id": "job#c"})
    assert item is not None and item["tasks_done"] == _RACERS * 4


def test_put_if_absent_race_has_exactly_one_winner(dynamodb_local_store: tuple[Store, str]) -> None:
    """Concurrent creates of the sweep lock row: exactly one reports `True`, its write survives."""
    store, _ = dynamodb_local_store

    def acquire(holder: int) -> bool:
        return store.put_if_absent(
            "eval_runs", {"run_id": keys.SWEEP_LOCK_ROW_ID, "holder_run_id": f"job#{holder}"}
        )

    with ThreadPoolExecutor(max_workers=_RACERS) as pool:
        outcomes = list(pool.map(acquire, range(_RACERS)))
    assert outcomes.count(True) == 1
    winner = outcomes.index(True)
    lock = store.get_item("eval_runs", {"run_id": keys.SWEEP_LOCK_ROW_ID})
    assert lock is not None and lock["holder_run_id"] == f"job#{winner}"


def test_settings_and_conversation_items_are_absent_from_the_reps_tenant_index(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """`user_settings_key`/`conversation_key` items carry no `tenant`; `gsi_tenant` never lists them."""
    store, _ = dynamodb_local_store
    store.put_item("reps", {"rep_id": "rep-1", "tenant": "demo", "name": "Rep One"})
    assert store.put_if_absent(
        "reps",
        {"rep_id": keys.user_settings_key("demo", "rep-1"), "kind": "user_settings"},
    )
    assert store.put_if_absent(
        "reps", {"rep_id": keys.conversation_key("conv-1"), "owner_rep_id": "rep-1"}
    )
    listed, _ = store.query_tenant_scan("reps", tenant="demo")
    assert [item["rep_id"] for item in listed] == ["rep-1"]
    assert store.get_item("reps", {"rep_id": "usersettings#demo#rep-1"}) is not None
    assert store.get_item("reps", {"rep_id": "conversation#conv-1"}) is not None


def test_eval_result_ids_sort_by_arm_then_task(dynamodb_local_store: tuple[Store, str]) -> None:
    """`eval_result_id` sort keys come back from the base table in arm order, arm 10 last."""
    store, prefix = dynamodb_local_store
    for arm_index in (10, 2, 0):
        store.put_item(
            "eval_results",
            {
                "run_id": "r1",
                "result_id": keys.eval_result_id(arm_index, "t1"),
                "task_id": "t1",
                "arm": str(arm_index),
            },
        )
    response = store._client.query(
        TableName=f"{prefix}eval_results",
        KeyConditionExpression="run_id = :r",
        ExpressionAttributeValues={":r": {"S": "r1"}},
    )
    assert [item["result_id"]["S"] for item in response["Items"]] == ["00#t1", "02#t1", "10#t1"]


def test_query_gsi_descending_pagination_covers_every_item_once(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """`scan_forward=False` with a cursor returns newest-first, no gaps, no duplicates."""
    store, _ = dynamodb_local_store
    started = [f"2026-10-{day:02d}T00:00:00Z" for day in range(1, 6)]
    for index, started_at in enumerate(started):
        store.put_item("eval_runs", _run_item(f"job#{index}", started_at))
    seen: list[object] = []
    cursor: dict[str, object] | None = None
    while True:
        page, cursor = store.query_gsi(
            "eval_runs",
            "gsi_status",
            partition_value="job",
            limit=2,
            scan_forward=False,
            exclusive_start_key=cursor,
        )
        seen.extend(item["started_at"] for item in page)
        if cursor is None:
            break
    assert seen == list(reversed(started))
