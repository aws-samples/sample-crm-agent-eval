# SPDX-License-Identifier: MIT
"""`eval.runs.list_runs` against DynamoDB Local: 20 per page, newest first, opaque cursor.

45 runs page as 20/20/5 through the real `gsi_status` index; lock and quota rows never appear.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from crm.authz import Identity
from crm.errors import ValidationFailed
from crm.handlers._common import decode
from crm.store.store import Store
from eval import runs

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "events"
_REST_V1 = json.loads((_FIXTURE / "apigw_v1_proxy_post_get_account.json").read_text("utf-8"))
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def identity() -> Identity:
    event: dict[str, Any] = copy.deepcopy(_REST_V1)
    event["body"] = "{}"
    return decode(event)[0]


@pytest.fixture
def store(dynamodb_local_store: tuple[Store, str]) -> Store:
    store, _prefix = dynamodb_local_store
    for index in range(45):
        runs.create_run(
            store,
            run_id=f"r{index:02d}",
            mode="stub",
            identity=identity(),
            tasks_total=40,
            now=NOW + timedelta(minutes=index),
        )
    runs.acquire_lock(store, run_id="r44", now=NOW)
    runs.consume_live_quota(store, now=NOW)
    return store


def test_45_runs_page_as_20_20_5_newest_first(store: Store) -> None:
    pages: list[list[str]] = []
    cursor: str | None = None
    while True:
        records, cursor = runs.list_runs(store, cursor=cursor)
        if records:
            pages.append([record.run_id for record in records])
        if cursor is None:
            break
    assert [len(page) for page in pages] == [20, 20, 5]
    flat = [run_id for page in pages for run_id in page]
    assert flat == [f"r{index:02d}" for index in reversed(range(45))]


def test_each_page_carries_full_rows_not_the_index_projection(store: Store) -> None:
    records, _cursor = runs.list_runs(store)
    assert all(record.tasks_total == 40 and record.mode == "stub" for record in records)
    assert all(record.started_by_rep_id for record in records)


def test_the_cursor_is_opaque_text(store: Store) -> None:
    _records, cursor = runs.list_runs(store)
    assert cursor is not None
    assert "{" not in cursor and "job#" not in cursor


@pytest.mark.parametrize(
    "tampered",
    [
        "not base64 !!",
        runs.encode_cursor({"run_id": {"S": "job#r1"}}),
        runs.encode_cursor(
            {"run_id": {"S": "lock#sweep"}, "status": {"S": "job"}, "started_at": {"S": "x"}}
        ),
        runs.encode_cursor(
            {"run_id": {"S": "job#r1"}, "status": {"S": "other"}, "started_at": {"S": "x"}}
        ),
        runs.encode_cursor(
            {"run_id": {"N": "1"}, "status": {"S": "job"}, "started_at": {"S": "x"}}
        ),
    ],
    ids=["garbage", "missing-keys", "lock-row", "other-partition", "wrong-type"],
)
def test_a_tampered_cursor_is_refused(store: Store, tampered: str) -> None:
    with pytest.raises(ValidationFailed) as exc_info:
        runs.list_runs(store, cursor=tampered)
    assert exc_info.value.source == "cursor"
