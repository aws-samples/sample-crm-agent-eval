# SPDX-License-Identifier: MIT
"""`eval.runs` run records: the status vocabulary, legacy read map, transitions, terminal rule, payload.

The store is a real `moto`-backed `Store`; every assertion reads back a written row. Identities are
channel-decoded from the recorded REST v1 event, never constructed.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.authz import Identity
from crm.envelope import CLAIM_REP_ID
from crm.errors import Conflict, NotFound, ValidationFailed
from crm.handlers._common import decode
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import runs

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "events"
_REST_V1 = json.loads((_FIXTURE / "apigw_v1_proxy_post_get_account.json").read_text("utf-8"))
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def identity(rep_id: str = "rep-admin") -> Identity:
    event: dict[str, Any] = copy.deepcopy(_REST_V1)
    event["requestContext"]["authorizer"]["claims"][CLAIM_REP_ID] = rep_id
    event["body"] = "{}"
    return decode(event)[0]


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _create(store: Store, run_id: str = "r1", **overrides: Any) -> runs.RunRecord:
    return runs.create_run(
        store,
        run_id=run_id,
        mode=overrides.pop("mode", "stub"),
        identity=identity(),
        tasks_total=overrides.pop("tasks_total", 40),
        now=overrides.pop("now", NOW),
        **overrides,
    )


class TestVocabulary:
    def test_the_six_statuses_and_three_terminal_ones(self) -> None:
        assert runs.JOB_STATUSES == (
            "queued",
            "running",
            "cancelling",
            "completed",
            "cancelled",
            "failed",
        )
        assert {"completed", "cancelled", "failed"} == runs.TERMINAL_JOB_STATUSES

    @pytest.mark.parametrize(
        ("stored", "read"),
        [
            ("submitted", "queued"),
            ("succeeded", "completed"),
            ("running", "running"),
            ("failed", "failed"),
        ],
    )
    def test_legacy_rows_read_through_the_map(self, store: Store, stored: str, read: str) -> None:
        store.put_item(
            "eval_runs",
            {
                "run_id": "job#old",
                "status": "job",
                "job_status": stored,
                "job_mode": "stub",
                "started_at": NOW.isoformat(),
                "error": "boom" if stored == "failed" else None,
            },
        )
        record = runs.require_run(store, "old")
        assert record.status == read
        assert record.tasks_total == 0
        if stored == "failed":
            assert record.failure_summary == "boom"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("job_status", "exploded"),
            ("job_mode", "turbo"),
            ("tasks_done", -1),
            ("teardown_status", "meh"),
        ],
    )
    def test_an_unreadable_stored_value_is_refused(
        self, store: Store, field: str, value: object
    ) -> None:
        _create(store)
        store.update_item(
            "eval_runs", {"run_id": "job#r1"}, set_values={field: value}, condition=None
        )
        with pytest.raises(ValidationFailed) as exc_info:
            runs.require_run(store, "r1")
        assert exc_info.value.source == field

    def test_an_absent_run_is_not_found(self, store: Store) -> None:
        assert runs.read_run(store, "nope") is None
        with pytest.raises(NotFound):
            runs.require_run(store, "nope")


class TestCreateAndTransitions:
    def test_create_writes_a_queued_job_row_under_the_status_sentinel(self, store: Store) -> None:
        record = _create(store, mode="live", estimate_usd=3.1, estimate_basis="static")
        row = store.get_item("eval_runs", {"run_id": "job#r1"})
        assert row is not None
        assert (row["status"], row["job_status"], row["job_mode"]) == ("job", "queued", "live")
        assert row["started_by_rep_id"] == "rep-admin"
        assert row["tenant"] == "eval-r1"
        assert record.status == "queued" and record.tasks_total == 40
        assert record.estimate_usd == pytest.approx(3.1)

    def test_a_second_create_for_the_same_run_conflicts(self, store: Store) -> None:
        _create(store)
        with pytest.raises(Conflict):
            _create(store)

    def test_mark_running_is_conditional_on_queued(self, store: Store) -> None:
        _create(store)
        assert runs.mark_running(store, run_id="r1", now=NOW).status == "running"
        with pytest.raises(Conflict):
            runs.mark_running(store, run_id="r1", now=NOW)

    def test_mark_failed_never_overwrites_a_terminal_run(self, store: Store) -> None:
        _create(store)
        runs.finalize(store, run_id="r1", teardown_ok=True, now=NOW)
        record = runs.mark_failed(store, run_id="r1", failure_summary="late", now=NOW)
        assert record.status == "completed"
        assert record.failure_summary is None

    def test_a_failure_summary_is_bounded(self, store: Store) -> None:
        _create(store)
        record = runs.mark_failed(store, run_id="r1", failure_summary="x" * 5000, now=NOW)
        assert record.failure_summary is not None
        assert len(record.failure_summary) == 1024
        assert record.teardown_status == "unknown"


class TestTerminalRule:
    @pytest.mark.parametrize(
        ("failed", "skipped", "teardown_ok", "expected"),
        [
            (0, 0, True, "completed"),
            (0, 3, True, "cancelled"),
            (1, 3, True, "failed"),
            (1, 0, True, "failed"),
            (0, 0, False, "failed"),
            (0, 3, False, "failed"),
        ],
    )
    def test_d11(self, failed: int, skipped: int, teardown_ok: bool, expected: str) -> None:
        assert (
            runs.terminal_status(
                tasks_failed=failed, tasks_skipped=skipped, teardown_ok=teardown_ok
            )
            == expected
        )

    def test_finalize_applies_the_rule_to_recorded_items(self, store: Store) -> None:
        _create(store, tasks_total=3)
        runs.record_item(
            store, run_id="r1", arm_index=0, task_id="t1", arm="a", item_status="done", cost_usd=0.5
        )
        runs.record_item(
            store, run_id="r1", arm_index=0, task_id="t2", arm="a", item_status="skipped_cancelled"
        )
        record = runs.finalize(store, run_id="r1", teardown_ok=True, now=NOW)
        assert record.status == "cancelled"
        assert (record.tasks_done, record.tasks_skipped, record.tasks_failed) == (1, 1, 0)
        assert record.cost_spent_usd == pytest.approx(0.5)
        assert record.teardown_status == "ok"
        assert record.finished_at == NOW.isoformat()

    def test_a_late_cancel_after_every_item_finished_is_completed(self, store: Store) -> None:
        _create(store, tasks_total=1)
        runs.record_item(store, run_id="r1", arm_index=0, task_id="t1", arm="a", item_status="done")
        assert (
            runs.request_cancel(store, run_id="r1", identity=identity(), now=NOW)[0] == "accepted"
        )
        record = runs.finalize(store, run_id="r1", teardown_ok=True, now=NOW)
        assert record.status == "completed"
        assert record.cancel_requested is True

    def test_a_retried_item_is_counted_once(self, store: Store) -> None:
        _create(store)
        first = runs.record_item(
            store, run_id="r1", arm_index=1, task_id="t1", arm="b", item_status="done", cost_usd=1.0
        )
        second = runs.record_item(
            store, run_id="r1", arm_index=1, task_id="t1", arm="b", item_status="done", cost_usd=1.0
        )
        assert (first, second) == (True, False)
        record = runs.require_run(store, "r1")
        assert record.tasks_done == 1
        assert record.cost_spent_usd == pytest.approx(1.0)
        row = store.get_item("eval_results", {"run_id": "r1", "result_id": "01#t1"})
        assert row is not None and row["item_status"] == "done"
        assert row["run_id_task_id"] == "r1#t1"


class TestPayload:
    def test_the_payload_omits_absent_fields_and_never_carries_the_execution_arn(
        self, store: Store
    ) -> None:
        _create(store)
        assert runs.acquire_lock(store, run_id="r1")
        runs.set_execution_arn(
            store, run_id="r1", execution_arn="arn:aws:states:us-east-1:123456789012:execution:x:r1"
        )
        payload = runs.require_run(store, "r1").to_payload()
        assert payload["status"] == "queued"
        assert payload["is_terminal"] is False
        assert payload["submitted_at"] == NOW.isoformat()
        assert "finished_at" not in payload and "error" not in payload and "matrix" not in payload
        assert not any("arn" in key for key in payload)
        assert "123456789012" not in json.dumps(payload)

    def test_stale_queued_age_constant(self) -> None:
        assert timedelta(seconds=runs.STALE_QUEUED_SECONDS) == timedelta(minutes=5)
