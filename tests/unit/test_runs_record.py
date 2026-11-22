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
from decimal import Decimal
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
from eval.matrix import RECORD_SHAPE_VERSION, RECORD_SHAPE_VERSION_KEY

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "events"
_REST_V1 = json.loads((_FIXTURE / "apigw_v1_proxy_post_get_account.json").read_text("utf-8"))
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def identity(rep_id: str = "rep-admin") -> Identity:
    event: dict[str, Any] = copy.deepcopy(_REST_V1)
    event["requestContext"]["authorizer"]["claims"][CLAIM_REP_ID] = rep_id
    event["body"] = "{}"
    return decode(event)[0]


@pytest.fixture(autouse=True)
def _fresh_earlier_format_log() -> Iterator[None]:
    """Each test starts as a fresh process that has logged no earlier-format run."""
    runs._EARLIER_FORMAT_LOGGED_RUN_IDS.clear()
    yield
    runs._EARLIER_FORMAT_LOGGED_RUN_IDS.clear()


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


_ARTIFACTS: dict[str, Any] = {"matrix_json": "s3://b/r1/matrix.json", "raw": []}


def _stored_payload(
    store: Store, matrix_report: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> dict[str, Any]:
    """Store a completed run ``r1`` with ``matrix_report`` and artifact links; return its payload."""
    _create(store)
    store.update_item(
        runs.EVAL_RUNS_TABLE,
        {"run_id": runs.job_row_id("r1")},
        set_values={
            "job_status": "completed",
            "finished_at": NOW.isoformat(),
            "tasks_done": 40,
            "cost_spent_usd": Decimal("1.25"),
            "matrix_report": json.dumps(matrix_report),
            "artifact_links": json.dumps(_ARTIFACTS),
        },
        condition=None,
    )
    caplog.set_level("INFO")
    return runs.require_run(store, "r1").to_payload()


def _store_matrix(store: Store, run_id: str, matrix_report: dict[str, Any], minute: int) -> None:
    """Create a completed run ``run_id`` whose stored matrix is ``matrix_report``."""
    _create(store, run_id=run_id, now=NOW + timedelta(minutes=minute))
    store.update_item(
        runs.EVAL_RUNS_TABLE,
        {"run_id": runs.job_row_id(run_id)},
        set_values={"job_status": "completed", "matrix_report": json.dumps(matrix_report)},
        condition=None,
    )


def _format_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    events = []
    for record in caplog.records:
        try:
            parsed = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("event") == "eval_record_format_earlier":
            events.append(parsed)
    return events


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
            assert record.failure_summary is None
            assert (record.error_code, record.error_detail) == ("unclassified", None)
            payload = record.to_payload()
            assert payload["error_code"] == "unclassified"
            assert "error" not in payload and "error_detail" not in payload
            assert "boom" not in json.dumps(payload)

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

    def test_an_earlier_format_record_omits_its_results_and_says_so(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        payload = _stored_payload(store, {"arms": [], "tier_comparison": {"columns": []}}, caplog)
        assert payload["record_format"] == "earlier"
        assert not {"matrix", "tier_comparison_table", "artifacts"} & set(payload)
        assert payload["status"] == "completed"
        assert payload["tasks_done"] == 40
        assert payload["cost_spent_usd"] == pytest.approx(1.25)
        assert payload["finished_at"] == NOW.isoformat()
        assert _format_events(caplog) == [
            {"event": "eval_record_format_earlier", "correlation_id": "r1"}
        ]

    @pytest.mark.parametrize(
        "version",
        [1, 2, "3", 3.0, True, None],
        ids=["v1", "v2", "string", "float", "bool", "null"],
    )
    def test_any_record_shape_other_than_the_current_int_is_earlier(
        self, store: Store, caplog: pytest.LogCaptureFixture, version: object
    ) -> None:
        payload = _stored_payload(store, {RECORD_SHAPE_VERSION_KEY: version, "arms": []}, caplog)
        assert payload["record_format"] == "earlier"
        assert "matrix" not in payload and "artifacts" not in payload

    def test_a_current_shape_record_keeps_its_results_and_no_marker(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        report = {RECORD_SHAPE_VERSION_KEY: RECORD_SHAPE_VERSION, "arms": []}
        payload = _stored_payload(store, report, caplog)
        assert "record_format" not in payload
        assert payload["matrix"] == report
        assert payload["artifacts"] == _ARTIFACTS
        assert _format_events(caplog) == []

    def test_a_run_without_a_matrix_carries_no_marker(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO")
        _create(store)
        payload = runs.require_run(store, "r1").to_payload()
        assert "record_format" not in payload
        assert _format_events(caplog) == []

    def test_an_earlier_format_run_is_still_listed(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        _stored_payload(store, {"arms": []}, caplog)
        _create(store, run_id="r2", now=NOW + timedelta(minutes=1))
        page, _cursor = runs.list_runs(store)
        listed = {record.run_id: record.to_payload() for record in page}
        assert set(listed) == {"r1", "r2"}
        assert listed["r1"]["record_format"] == "earlier"
        assert "record_format" not in listed["r2"]

    def test_stale_queued_age_constant(self) -> None:
        assert timedelta(seconds=runs.STALE_QUEUED_SECONDS) == timedelta(minutes=5)


class TestEarlierFormatLogOncePerRun:
    """``eval_record_format_earlier`` is logged once per run id per process, not per page."""

    def _history(self, store: Store) -> None:
        _store_matrix(store, "e1", {"arms": []}, minute=1)
        _store_matrix(store, "e2", {RECORD_SHAPE_VERSION_KEY: 1, "arms": []}, minute=2)
        _store_matrix(store, "c1", {RECORD_SHAPE_VERSION_KEY: RECORD_SHAPE_VERSION}, minute=3)

    def _page_everything(self, store: Store) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page, cursor = runs.list_runs(store, cursor=cursor, limit=2)
            payloads.extend(record.to_payload() for record in page)
            if cursor is None:
                return payloads

    def test_paging_the_history_twice_logs_one_line_per_earlier_run(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._history(store)
        _page, cursor = runs.list_runs(store, limit=2)
        assert cursor is not None, "the history must span more than one page"
        caplog.set_level("INFO")
        first = self._page_everything(store)
        second = self._page_everything(store)
        assert [p["run_id"] for p in first] == [p["run_id"] for p in second] == ["c1", "e2", "e1"]
        assert {p["run_id"] for p in second if p.get("record_format") == "earlier"} == {"e1", "e2"}
        assert sorted(event["correlation_id"] for event in _format_events(caplog)) == ["e1", "e2"]

    def test_a_fresh_process_logs_the_earlier_run_again(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._history(store)
        caplog.set_level("INFO")
        self._page_everything(store)
        runs._EARLIER_FORMAT_LOGGED_RUN_IDS.clear()
        self._page_everything(store)
        assert sorted(event["correlation_id"] for event in _format_events(caplog)) == [
            "e1",
            "e1",
            "e2",
            "e2",
        ]


_ARN = "arn:aws:states:us-east-1:123456789012:execution:eval_sweep:r1"


class _Sfn:
    """A Step Functions client answering ``describe_execution`` with one fixed status."""

    def __init__(self, status: str) -> None:
        self.status = status

    def describe_execution(self, *, executionArn: str) -> dict[str, Any]:  # noqa: N803
        return {"executionArn": executionArn, "status": self.status}


def _started(store: Store) -> None:
    assert runs.acquire_lock(store, run_id="r1", now=NOW)
    _create(store)
    runs.set_execution_arn(store, run_id="r1", execution_arn=_ARN)


class TestErrorCodes:
    def test_mark_failed_stores_and_returns_the_code_and_detail(self, store: Store) -> None:
        _create(store)
        runs.mark_failed(
            store,
            run_id="r1",
            failure_summary="step failed: turn_timeout",
            error_code="turn_timeout",
            error_detail="TurnTimeout",
            now=NOW,
        )
        row = store.get_item("eval_runs", {"run_id": "job#r1"}, consistent=True)
        assert row is not None
        assert (row["error_code"], row["error_detail"]) == ("turn_timeout", "TurnTimeout")
        payload = runs.require_run(store, "r1").to_payload()
        assert payload["error_code"] == "turn_timeout"
        assert payload["error_detail"] == "TurnTimeout"
        assert payload["error"] == "step failed: turn_timeout"

    def test_mark_failed_without_a_code_records_unclassified_and_no_detail(
        self, store: Store
    ) -> None:
        _create(store)
        runs.mark_failed(store, run_id="r1", failure_summary="x", now=NOW)
        row = store.get_item("eval_runs", {"run_id": "job#r1"}, consistent=True)
        assert row is not None and row["error_code"] == runs.UNCLASSIFIED_ERROR_CODE
        assert "error_detail" not in row
        payload = runs.require_run(store, "r1").to_payload()
        assert payload["error_code"] == "unclassified" and "error_detail" not in payload

    def test_a_run_that_never_failed_carries_no_error_code(self, store: Store) -> None:
        _create(store)
        payload = runs.require_run(store, "r1").to_payload()
        assert "error_code" not in payload and "error_detail" not in payload

    def test_a_reconciled_run_carries_the_unfinalized_code(self, store: Store) -> None:
        _started(store)
        record = runs.reconcile(store, _Sfn("FAILED"), runs.require_run(store, "r1"), now=NOW)
        assert record.error_code == runs.EXECUTION_UNFINALIZED_ERROR_CODE == "execution_unfinalized"

    def test_an_admin_released_run_carries_the_lock_released_code(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _started(store)
        monkeypatch.setattr(runs, "reconcile_lock", lambda *_a, **_k: runs.read_lock(store))
        admin = Identity(rep_id="rep-admin", full_visibility=True)
        runs.unlock(store, _Sfn("ABORTED"), identity=admin, now=NOW)
        record = runs.require_run(store, "r1")
        assert record.error_code == runs.LOCK_RELEASED_ERROR_CODE == "lock_released"
