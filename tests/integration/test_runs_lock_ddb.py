# SPDX-License-Identifier: MIT
"""`eval.runs` conditional writes against DynamoDB Local under real concurrency.

Concurrent acquires, cancels and daily-cap admissions race on threads, each with its own client;
the engine's conditional writes decide the winners.
"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest

from crm.authz import Identity
from crm.envelope import CLAIM_REP_ID
from crm.errors import Conflict
from crm.handlers._common import decode
from crm.store.store import Store
from eval import runs

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "events"
_REST_V1 = json.loads((_FIXTURE / "apigw_v1_proxy_post_get_account.json").read_text("utf-8"))
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def identity(rep_id: str) -> Identity:
    event: dict[str, Any] = copy.deepcopy(_REST_V1)
    event["requestContext"]["authorizer"]["claims"][CLAIM_REP_ID] = rep_id
    event["body"] = "{}"
    return decode(event)[0]


def _race[T](
    store: Store, prefix: str, count: int, work: Callable[[Store, int], T]
) -> list[T | Exception]:
    """Run ``work`` on ``count`` threads released together, each with its own client."""
    endpoint = store._client.meta.endpoint_url  # noqa: SLF001
    barrier = threading.Barrier(count)
    results: list[T | Exception] = [RuntimeError("not run")] * count

    def _one(index: int) -> None:
        client = boto3.client(
            "dynamodb",
            endpoint_url=endpoint,
            region_name="us-east-1",
            aws_access_key_id="local",
            aws_secret_access_key="local",  # pragma: allowlist secret  # nosec B106
        )
        own = Store(table_prefix=prefix, client=client)
        barrier.wait()
        try:
            results[index] = work(own, index)
        except Exception as exc:  # noqa: BLE001 - collected and asserted by the caller
            results[index] = exc

    threads = [threading.Thread(target=_one, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


@pytest.fixture
def ddb(dynamodb_local_store: tuple[Store, str]) -> tuple[Store, str]:
    return dynamodb_local_store


def test_concurrent_acquires_yield_exactly_one_holder(ddb: tuple[Store, str]) -> None:
    store, prefix = ddb
    results = _race(store, prefix, 8, lambda s, i: runs.acquire_lock(s, run_id=f"run-{i}", now=NOW))
    assert sorted(results, key=str) == [False] * 7 + [True]
    winner = results.index(True)
    lock = runs.read_lock(store)
    assert lock is not None and lock.holder_run_id == f"run-{winner}"


def test_a_non_holder_cannot_release_on_the_real_engine(ddb: tuple[Store, str]) -> None:
    store, _prefix = ddb
    assert runs.acquire_lock(store, run_id="a", now=NOW)
    assert runs.release_lock(store, run_id="b") is False
    assert runs.read_lock(store) is not None
    assert runs.release_lock(store, run_id="a") is True
    assert runs.read_lock(store) is None


def test_concurrent_cancels_resolve_to_one_accepted(ddb: tuple[Store, str]) -> None:
    store, prefix = ddb
    runs.create_run(
        store, run_id="r1", mode="live", identity=identity("s"), tasks_total=40, now=NOW
    )
    results = _race(
        store,
        prefix,
        6,
        lambda s, i: runs.request_cancel(s, run_id="r1", identity=identity(f"adm-{i}"), now=NOW)[0],
    )
    assert sorted(results, key=str) == ["accepted"] + ["already_requested"] * 5
    record = runs.require_run(store, "r1")
    assert record.cancel_requested_by == f"adm-{results.index('accepted')}"


def test_concurrent_live_submissions_admit_exactly_the_daily_cap(ddb: tuple[Store, str]) -> None:
    store, prefix = ddb
    results = _race(store, prefix, 13, lambda s, _i: runs.consume_live_quota(s, now=NOW))
    admitted = sorted(r for r in results if isinstance(r, int))
    refused = [r for r in results if isinstance(r, Conflict)]
    assert admitted == list(range(1, runs.LIVE_SWEEPS_PER_UTC_DAY + 1))
    assert len(refused) == 13 - runs.LIVE_SWEEPS_PER_UTC_DAY


def test_record_item_counts_once_under_a_concurrent_retry(ddb: tuple[Store, str]) -> None:
    store, prefix = ddb
    runs.create_run(store, run_id="r1", mode="stub", identity=identity("s"), tasks_total=1, now=NOW)
    results = _race(
        store,
        prefix,
        4,
        lambda s, _i: runs.record_item(
            s, run_id="r1", arm_index=0, task_id="t1", arm="a", item_status="done", cost_usd=0.25
        ),
    )
    assert results.count(True) == 1
    assert all(r is True or r is False for r in results), results
    record = runs.require_run(store, "r1")
    assert record.tasks_done == 1
    assert record.cost_spent_usd == pytest.approx(0.25)
