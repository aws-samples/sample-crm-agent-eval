# SPDX-License-Identifier: MIT
"""`eval.runs` single-flight lock, reconcile-on-read, admin unlock and the daily live cap.

The store is a real `moto`-backed `Store`. Step Functions is an injected client exposing only
`describe_execution`; every assertion reads the store, never the client.
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
from botocore.exceptions import ClientError
from moto import mock_aws

from crm.authz import Identity
from crm.errors import Conflict, CrmError, ValidationFailed
from crm.handlers._common import decode
from crm.store import keys
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import runs

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "events"
_REST_V1 = json.loads((_FIXTURE / "apigw_v1_proxy_post_get_account.json").read_text("utf-8"))
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:eval_sweep"
ARN = "arn:aws:states:us-east-1:123456789012:execution:eval_sweep:r1"


def identity() -> Identity:
    event: dict[str, Any] = copy.deepcopy(_REST_V1)
    event["body"] = "{}"
    return decode(event)[0]


class Sfn:
    """An injected Step Functions client answering ``describe_execution`` with a fixed status."""

    def __init__(self, status: str | None = None, *, error_code: str | None = None) -> None:
        self.status = status
        self.error_code = error_code
        self.calls: list[str] = []

    def describe_execution(self, *, executionArn: str) -> dict[str, Any]:  # noqa: N803
        self.calls.append(executionArn)
        if self.error_code is not None:
            raise ClientError(
                {"Error": {"Code": self.error_code, "Message": "x"}}, "DescribeExecution"
            )
        return {"executionArn": executionArn, "status": self.status}


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _started(
    store: Store, run_id: str = "r1", *, arn: str | None = ARN, now: datetime = NOW
) -> None:
    assert runs.acquire_lock(store, run_id=run_id, now=now)
    runs.create_run(store, run_id=run_id, mode="stub", identity=identity(), tasks_total=40, now=now)
    if arn is not None:
        runs.set_execution_arn(store, run_id=run_id, execution_arn=arn)


class TestLock:
    def test_only_one_acquire_wins(self, store: Store) -> None:
        assert runs.acquire_lock(store, run_id="a", now=NOW) is True
        assert runs.acquire_lock(store, run_id="b", now=NOW) is False
        lock = runs.read_lock(store)
        assert lock is not None and lock.holder_run_id == "a"

    def test_the_lock_row_is_not_in_the_history_index(self, store: Store) -> None:
        runs.acquire_lock(store, run_id="a", now=NOW)
        rows, _ = store.query_gsi("eval_runs", "gsi_status", partition_value="job")
        assert rows == []

    def test_only_the_holder_releases(self, store: Store) -> None:
        runs.acquire_lock(store, run_id="a", now=NOW)
        assert runs.release_lock(store, run_id="b") is False
        assert runs.read_lock(store) is not None
        assert runs.release_lock(store, run_id="a") is True
        assert runs.read_lock(store) is None
        assert runs.release_lock(store, run_id="a") is False

    def test_release_is_a_conditional_delete_item(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[dict[str, Any]]] = []
        real = store.transact_write

        def _spy(operations: Any, **kwargs: Any) -> None:
            ops = list(operations)
            seen.append(ops)
            real(ops, **kwargs)

        monkeypatch.setattr(store, "transact_write", _spy)
        runs.acquire_lock(store, run_id="a", now=NOW)
        runs.release_lock(store, run_id="a")
        (delete,) = seen[0]
        assert set(delete) == {"Delete"}
        assert delete["Delete"]["ExpressionAttributeValues"] == {":run_id": {"S": "a"}}
        assert runs.read_lock(store) is None

    def test_a_failed_release_while_still_holding_raises(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs.acquire_lock(store, run_id="a", now=NOW)

        def _cancelled(*_a: Any, **_k: Any) -> None:
            raise Conflict("throttled")

        monkeypatch.setattr(store, "transact_write", _cancelled)
        with pytest.raises(CrmError, match="could not be released"):
            runs.release_lock(store, run_id="a")

    def test_finalize_releases_after_a_teardown_failure(self, store: Store) -> None:
        _started(store)
        record = runs.finalize(store, run_id="r1", teardown_ok=False, now=NOW)
        assert record.status == "failed" and record.teardown_status == "failed"
        assert runs.read_lock(store) is None

    def test_finalize_of_a_terminal_run_still_releases(self, store: Store) -> None:
        _started(store)
        runs.mark_failed(store, run_id="r1", failure_summary="x", now=NOW)
        runs.finalize(store, run_id="r1", teardown_ok=True, now=NOW)
        assert runs.read_lock(store) is None
        assert runs.require_run(store, "r1").status == "failed"

    def test_the_execution_arn_is_recorded_on_record_and_lock(self, store: Store) -> None:
        _started(store)
        lock = runs.read_lock(store)
        assert lock is not None and lock.execution_arn == ARN
        assert runs.require_run(store, "r1").execution_arn == ARN

    def test_the_lock_arn_is_not_set_for_a_non_holder(self, store: Store) -> None:
        _started(store)
        runs.create_run(
            store, run_id="r2", mode="stub", identity=identity(), tasks_total=1, now=NOW
        )
        with pytest.raises(Conflict):
            runs.set_execution_arn(store, run_id="r2", execution_arn="arn:other")
        lock = runs.read_lock(store)
        assert lock is not None and lock.execution_arn == ARN


class TestReconcile:
    @pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT", "ABORTED"])
    def test_an_ended_execution_fails_the_run_and_releases_the_lock(
        self, store: Store, status: str
    ) -> None:
        _started(store)
        record = runs.reconcile(store, Sfn(status), runs.require_run(store, "r1"), now=NOW)
        assert record.status == "failed"
        assert record.teardown_status == "unknown"
        assert record.failure_summary is not None and status in record.failure_summary
        assert runs.read_lock(store) is None

    def test_a_missing_execution_fails_the_run(self, store: Store) -> None:
        _started(store)
        sfn = Sfn(error_code="ExecutionDoesNotExist")
        assert runs.reconcile(store, sfn, runs.require_run(store, "r1"), now=NOW).status == "failed"

    def test_a_running_execution_leaves_the_run(self, store: Store) -> None:
        _started(store)
        record = runs.reconcile(store, Sfn("RUNNING"), runs.require_run(store, "r1"), now=NOW)
        assert record.status == "queued"
        assert runs.read_lock(store) is not None

    def test_a_succeeded_execution_over_an_unfinalized_run_fails_it(self, store: Store) -> None:
        _started(store)
        record = runs.reconcile(store, Sfn("SUCCEEDED"), runs.require_run(store, "r1"), now=NOW)
        assert record.status == "failed"
        assert record.failure_summary is not None and "never finalized" in record.failure_summary
        assert runs.read_lock(store) is None

    def test_another_describe_error_is_raised_not_swallowed(self, store: Store) -> None:
        _started(store)
        with pytest.raises(CrmError):
            runs.reconcile(
                store, Sfn(error_code="AccessDeniedException"), runs.require_run(store, "r1")
            )
        assert runs.require_run(store, "r1").status == "queued"

    def test_a_stale_queued_run_whose_execution_never_started_fails(self, store: Store) -> None:
        _started(store, arn=None)
        later = NOW + timedelta(seconds=runs.STALE_QUEUED_SECONDS + 1)
        sfn = Sfn(error_code="ExecutionDoesNotExist")
        record = runs.reconcile(
            store, sfn, runs.require_run(store, "r1"), now=later, state_machine_arn=MACHINE
        )
        assert sfn.calls == [ARN]
        assert record.status == "failed"
        assert runs.read_lock(store) is None

    def test_a_stale_queued_run_whose_execution_is_running_gets_its_arn(self, store: Store) -> None:
        """An execution that started but whose ARN was never written is found, not failed."""
        _started(store, arn=None)
        later = NOW + timedelta(seconds=runs.STALE_QUEUED_SECONDS + 1)
        record = runs.reconcile(
            store,
            Sfn("RUNNING"),
            runs.require_run(store, "r1"),
            now=later,
            state_machine_arn=MACHINE,
        )
        assert record.status == "queued"
        assert record.execution_arn == ARN
        lock = runs.read_lock(store)
        assert lock is not None and lock.execution_arn == ARN

    def test_the_state_machine_arn_falls_back_to_the_environment(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(runs.EVAL_STATE_MACHINE_ARN_ENV_VAR, MACHINE)
        _started(store, arn=None)
        later = NOW + timedelta(seconds=runs.STALE_QUEUED_SECONDS + 1)
        sfn = Sfn("RUNNING")
        runs.reconcile(store, sfn, runs.require_run(store, "r1"), now=later)
        assert sfn.calls == [ARN]

    def test_with_no_state_machine_known_a_stale_queued_run_is_left(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(runs.EVAL_STATE_MACHINE_ARN_ENV_VAR, raising=False)
        _started(store, arn=None)
        later = NOW + timedelta(seconds=runs.STALE_QUEUED_SECONDS + 1)
        sfn = Sfn("RUNNING")
        assert (
            runs.reconcile(store, sfn, runs.require_run(store, "r1"), now=later).status == "queued"
        )
        assert sfn.calls == []

    def test_an_unreadable_started_at_is_refused_not_treated_as_stale(self, store: Store) -> None:
        _started(store, arn=None)
        store.update_item(
            "eval_runs",
            {"run_id": "job#r1"},
            set_values={"started_at": "yesterday"},
            condition=None,
        )
        with pytest.raises(ValidationFailed):
            runs.reconcile(store, Sfn(), runs.require_run(store, "r1"), state_machine_arn=MACHINE)
        assert runs.require_run(store, "r1").status == "queued"

    def test_a_fresh_queued_run_without_an_execution_is_left(self, store: Store) -> None:
        _started(store, arn=None)
        later = NOW + timedelta(seconds=runs.STALE_QUEUED_SECONDS - 1)
        assert (
            runs.reconcile(store, Sfn(), runs.require_run(store, "r1"), now=later).status
            == "queued"
        )

    def test_a_terminal_run_is_not_described(self, store: Store) -> None:
        _started(store)
        runs.finalize(store, run_id="r1", teardown_ok=True, now=NOW)
        sfn = Sfn("FAILED")
        assert runs.reconcile(store, sfn, runs.require_run(store, "r1")).status == "completed"
        assert sfn.calls == []

    def test_reconciling_does_not_release_another_runs_lock(self, store: Store) -> None:
        runs.create_run(
            store, run_id="old", mode="stub", identity=identity(), tasks_total=1, now=NOW
        )
        store.update_item(
            "eval_runs",
            {"run_id": "job#old"},
            set_values={"execution_arn": "arn:old"},
            condition=None,
        )
        _started(store, "r1")
        runs.reconcile(store, Sfn("FAILED"), runs.require_run(store, "old"), now=NOW)
        lock = runs.read_lock(store)
        assert lock is not None and lock.holder_run_id == "r1"


class TestReconcileLockAndUnlock:
    def test_a_lock_whose_holder_finished_is_released(self, store: Store) -> None:
        _started(store)
        store.update_item(
            "eval_runs",
            {"run_id": "job#r1"},
            set_values={"job_status": "completed"},
            condition=None,
        )
        assert runs.reconcile_lock(store, Sfn("SUCCEEDED"), now=NOW) is None

    def test_a_lock_with_no_record_is_released_only_once_stale(self, store: Store) -> None:
        runs.acquire_lock(store, run_id="ghost", now=NOW)
        assert runs.reconcile_lock(store, Sfn(), now=NOW + timedelta(seconds=10)) is not None
        later = NOW + timedelta(seconds=runs.STALE_QUEUED_SECONDS + 1)
        assert runs.reconcile_lock(store, Sfn(), now=later) is None

    def test_unlock_refuses_a_running_execution(self, store: Store) -> None:
        _started(store)
        with pytest.raises(Conflict, match="still running"):
            runs.unlock(store, Sfn("RUNNING"), now=NOW)
        assert runs.read_lock(store) is not None

    def test_unlock_refuses_a_fresh_queued_holder(self, store: Store) -> None:
        _started(store, arn=None)
        with pytest.raises(Conflict, match="starting"):
            runs.unlock(store, Sfn(), now=NOW + timedelta(seconds=5))
        assert runs.read_lock(store) is not None

    def test_unlock_releases_after_an_aborted_execution(self, store: Store) -> None:
        _started(store)
        assert runs.unlock(store, Sfn("ABORTED"), now=NOW) == {
            "released": True,
            "holder_run_id": "r1",
        }
        assert runs.read_lock(store) is None
        assert runs.require_run(store, "r1").status == "failed"

    def test_unlock_reports_not_released_when_another_caller_released_first(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The release result is reported, not assumed."""
        _started(store)
        monkeypatch.setattr(runs, "reconcile_lock", lambda *_a, **_k: runs.read_lock(store))
        monkeypatch.setattr(runs, "release_lock", lambda *_a, **_k: False)
        assert runs.unlock(store, Sfn("ABORTED"), now=NOW) == {
            "released": False,
            "holder_run_id": "r1",
        }
        assert runs.require_run(store, "r1").status == "queued"

    def test_unlock_releases_a_succeeded_execution_that_never_finalized(self, store: Store) -> None:
        _started(store)
        assert runs.unlock(store, Sfn("SUCCEEDED"), now=NOW)["released"] is True
        assert runs.read_lock(store) is None
        assert runs.require_run(store, "r1").status == "failed"

    def test_unlock_with_no_lock_is_a_no_op(self, store: Store) -> None:
        assert runs.unlock(store, Sfn(), now=NOW) == {"released": False, "holder_run_id": None}


class TestDailyLiveCap:
    def test_the_eleventh_live_sweep_of_a_utc_day_is_refused(self, store: Store) -> None:
        for count in range(1, runs.LIVE_SWEEPS_PER_UTC_DAY + 1):
            assert runs.consume_live_quota(store, now=NOW) == count
        with pytest.raises(Conflict, match="daily limit of 10 live sweeps"):
            runs.consume_live_quota(store, now=NOW)

    def test_the_cap_resets_at_utc_midnight(self, store: Store) -> None:
        for _ in range(runs.LIVE_SWEEPS_PER_UTC_DAY):
            runs.consume_live_quota(store, now=NOW)
        assert runs.consume_live_quota(store, now=NOW + timedelta(days=1)) == 1

    def test_the_day_is_the_utc_day(self, store: Store) -> None:
        late_evening_west = datetime(2026, 10, 8, 23, 30, tzinfo=UTC) - timedelta(hours=7)
        runs.consume_live_quota(store, now=late_evening_west.astimezone())
        row = store.get_item("eval_runs", {"run_id": "livequota#2026-10-08"})
        assert row is not None and row["live_sweeps"] == 1

    def test_the_quota_row_is_not_in_the_history_index(self, store: Store) -> None:
        runs.consume_live_quota(store, now=NOW)
        assert store.query_gsi("eval_runs", "gsi_status", partition_value="job")[0] == []

    def test_the_cap_constant(self) -> None:
        assert runs.LIVE_SWEEPS_PER_UTC_DAY == 10
        assert keys.SWEEP_LOCK_ROW_ID == "lock#sweep"


class TestExecutionArn:
    def test_the_execution_arn_is_derived_from_the_state_machine(self) -> None:
        assert runs.execution_arn_for(MACHINE, "r1") == ARN

    @pytest.mark.parametrize(
        "bad", ["", "eval_sweep", ARN, "arn:aws:lambda:us-east-1:123456789012:function:x"]
    )
    def test_a_non_state_machine_arn_is_refused(self, bad: str) -> None:
        with pytest.raises(ValueError):
            runs.execution_arn_for(bad, "r1")

    def test_the_quota_refund_returns_one_slot_and_never_goes_negative(self, store: Store) -> None:
        runs.consume_live_quota(store, now=NOW)
        runs.refund_live_quota(store, now=NOW)
        row = store.get_item("eval_runs", {"run_id": "livequota#2026-10-08"})
        assert row is not None and row["live_sweeps"] == 0
        with pytest.raises(Conflict):
            runs.refund_live_quota(store, now=NOW)
