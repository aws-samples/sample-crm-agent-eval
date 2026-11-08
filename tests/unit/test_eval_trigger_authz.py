# SPDX-License-Identifier: MIT
"""`eval_trigger`'s run API through the real handler: authz per action, submit, 409s, daily cap.

Requests are the recorded REST v1 proxy event with claims edited, so every identity is decoded by
the production channel decoder. The store is a real `moto`-backed `Store`; Step Functions is an
injected client recording `start_execution` and answering `describe_execution`. Assertions read
the store and the proxy response.
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
import yaml
from botocore.exceptions import ClientError
from moto import mock_aws

from crm.envelope import (
    AGENT_IDENTITY_KEY,
    CLAIM_FULL_VISIBILITY,
    CLAIM_GROUPS,
    CLAIM_REP_ID,
    CLAIM_TENANT,
)
from crm.errors import CrmError
from crm.handlers import eval_trigger as handler_module
from crm.handlers._common import decode
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import runs
from eval.trigger import EVAL_ADMIN_SCOPE, EVAL_STATE_MACHINE_ARN_ENV_VAR, sweep_shape

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "events"
_REST_V1 = json.loads((_FIXTURE / "apigw_v1_proxy_post_get_account.json").read_text("utf-8"))
STATE_MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:eval_sweep"


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Op")


class Sfn:
    """Records ``start_execution``; ``describe_execution`` answers per ARN (default RUNNING).

    ``start_error`` makes the start raise, after starting when ``accepted`` is set; ``missing`` ARNs
    describe as ``ExecutionDoesNotExist``; ``describe_error`` fails every describe.
    """

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.statuses: dict[str, str] = {}
        self.start_error: BaseException | None = None
        self.accepted: bool = False
        self.no_arn: bool = False
        self.missing: set[str] = set()
        self.describe_error: str | None = None

    def start_execution(self, **kwargs: Any) -> dict[str, Any]:
        arn = f"{STATE_MACHINE.replace('stateMachine', 'execution')}:{kwargs['name']}"
        if self.start_error is not None:
            if self.accepted:
                self.started.append(kwargs)
            else:
                self.missing.add(arn)
            raise self.start_error
        self.started.append(kwargs)
        return {} if self.no_arn else {"executionArn": arn}

    def describe_execution(self, *, executionArn: str) -> dict[str, Any]:  # noqa: N803
        if self.describe_error is not None:
            raise _client_error(self.describe_error)
        if executionArn in self.missing:
            raise _client_error("ExecutionDoesNotExist")
        return {"status": self.statuses.get(executionArn, "RUNNING")}


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


@pytest.fixture
def sfn(monkeypatch: pytest.MonkeyPatch) -> Sfn:
    monkeypatch.setenv(EVAL_STATE_MACHINE_ARN_ENV_VAR, STATE_MACHINE)
    return Sfn()


def _event(
    body: dict[str, Any], *, admin: bool = False, full: bool = False, rep: str = "rep-1"
) -> dict[str, Any]:
    event: dict[str, Any] = copy.deepcopy(_REST_V1)
    claims = event["requestContext"]["authorizer"]["claims"]
    claims[CLAIM_REP_ID] = rep
    claims[CLAIM_FULL_VISIBILITY] = "true" if full else "false"
    if admin:
        claims[CLAIM_GROUPS] = "admin"
    event["path"] = "/eval_trigger"
    event["body"] = json.dumps(body)
    return event


def _call(store: Store, sfn: Sfn, event: dict[str, Any]) -> tuple[int, Any]:
    response = handler_module.handler(event, None, store=store, sfn=sfn)
    assert isinstance(response["body"], str), "proxy bodies must be JSON strings"
    return response["statusCode"], json.loads(response["body"])


def _job_rows(store: Store) -> list[dict[str, object]]:
    return store.query_gsi("eval_runs", "gsi_status", partition_value="job")[0]


def _quota(store: Store) -> object:
    for offset in (-1, 0, 1):
        day = (datetime.now(UTC).date() + timedelta(days=offset)).isoformat()
        row = store.get_item("eval_runs", {"run_id": f"livequota#{day}"})
        if row is not None:
            return row["live_sweeps"]
    return None


def _finish(store: Store, run_id: str) -> None:
    runs.finalize(store, run_id=run_id, teardown_ok=True)


class TestAuthentication:
    @pytest.mark.parametrize("action", ["submit", "estimate", "status", "list", "cancel", "unlock"])
    def test_an_unauthenticated_call_is_401(self, store: Store, sfn: Sfn, action: str) -> None:
        event = _event({"action": action})
        del event["requestContext"]["authorizer"]
        status, _body = _call(store, sfn, event)
        assert status == 401
        assert _job_rows(store) == [] and runs.read_lock(store) is None


class TestAdminOnly:
    @pytest.mark.parametrize("full", [False, True])
    def test_a_non_admin_live_submit_is_403_and_writes_nothing(
        self, store: Store, sfn: Sfn, full: bool
    ) -> None:
        status, body = _call(
            store, sfn, _event({"action": "submit", "mode": "live", "confirmed": True}, full=full)
        )
        assert status == 403
        assert body["error"]["missing_scope"] == EVAL_ADMIN_SCOPE
        assert _job_rows(store) == [] and runs.read_lock(store) is None and sfn.started == []

    def test_admin_claims_on_the_agent_channel_are_not_admin(self, store: Store, sfn: Sfn) -> None:
        event = {
            AGENT_IDENTITY_KEY: {
                "claims": {CLAIM_REP_ID: "rep-1", CLAIM_TENANT: "demo", CLAIM_GROUPS: ["admin"]},
                "session_id": "s",
            },
            "action": "submit",
            "mode": "live",
            "confirmed": True,
        }
        response = handler_module.handler(event, None, store=store, sfn=sfn)
        assert response["statusCode"] == 403
        assert sfn.started == []

    def test_a_non_admin_cancel_is_403_and_leaves_the_run(self, store: Store, sfn: Sfn) -> None:
        _status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        status, body = _call(
            store, sfn, _event({"action": "cancel", "run_id": run["run_id"]}, full=True)
        )
        assert status == 403 and body["error"]["missing_scope"] == EVAL_ADMIN_SCOPE
        assert runs.require_run(store, run["run_id"]).cancel_requested is False

    def test_a_non_admin_unlock_is_403_and_leaves_the_lock(self, store: Store, sfn: Sfn) -> None:
        _call(store, sfn, _event({"action": "submit"}, full=True))
        status, body = _call(store, sfn, _event({"action": "unlock"}, full=True))
        assert status == 403 and body["error"]["missing_scope"] == EVAL_ADMIN_SCOPE
        assert runs.read_lock(store) is not None

    def test_an_admin_cancel_is_accepted_then_already_requested(
        self, store: Store, sfn: Sfn
    ) -> None:
        _status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        first = _call(store, sfn, _event({"action": "cancel", "run_id": run["run_id"]}, admin=True))
        second = _call(
            store, sfn, _event({"action": "cancel", "run_id": run["run_id"]}, admin=True)
        )
        assert first[0] == 200 and first[1]["disposition"] == "accepted"
        assert first[1]["run"]["status"] == "cancelling"
        assert second[1]["disposition"] == "already_requested"

    def test_an_admin_cancel_of_a_terminal_run_is_already_terminal(
        self, store: Store, sfn: Sfn
    ) -> None:
        _status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        _finish(store, run["run_id"])
        status, body = _call(
            store, sfn, _event({"action": "cancel", "run_id": run["run_id"]}, admin=True)
        )
        assert status == 200 and body["disposition"] == "already_terminal"

    def test_an_admin_unlock_of_a_running_sweep_is_409(self, store: Store, sfn: Sfn) -> None:
        _call(store, sfn, _event({"action": "submit"}, full=True))
        status, _body = _call(store, sfn, _event({"action": "unlock"}, admin=True))
        assert status == 409
        assert runs.read_lock(store) is not None

    def test_an_admin_unlock_after_an_aborted_execution_releases(
        self, store: Store, sfn: Sfn
    ) -> None:
        _status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        arn = runs.require_run(store, run["run_id"]).execution_arn
        assert arn is not None
        sfn.statuses[arn] = "ABORTED"
        status, body = _call(store, sfn, _event({"action": "unlock"}, admin=True))
        assert status == 200 and body["released"] is True
        assert runs.read_lock(store) is None


class TestSubmit:
    def test_a_stub_submit_requires_full_visibility(self, store: Store, sfn: Sfn) -> None:
        status, body = _call(store, sfn, _event({"action": "submit"}, admin=True))
        assert status == 403 and body["error"]["missing_scope"] == "eval:trigger"

    def test_a_live_submit_requires_confirmation(self, store: Store, sfn: Sfn) -> None:
        status, body = _call(store, sfn, _event({"action": "submit", "mode": "live"}, admin=True))
        assert status == 400 and body["error"]["source"] == "confirmed"
        assert _job_rows(store) == [] and runs.read_lock(store) is None

    def test_an_admin_live_submit_starts_the_execution_named_by_the_run(
        self, store: Store, sfn: Sfn
    ) -> None:
        status, run = _call(
            store,
            sfn,
            _event({"action": "submit", "mode": "live", "confirmed": True}, admin=True, rep="adm"),
        )
        assert status == 200
        (started,) = sfn.started
        assert started["stateMachineArn"] == STATE_MACHINE
        assert started["name"] == run["run_id"]
        assert json.loads(started["input"]) == {
            "run_id": run["run_id"],
            "tenant": f"eval-{run['run_id']}",
            "mode": "live",
            "provider_mode": "live",
        }
        record = runs.require_run(store, run["run_id"])
        assert record.execution_arn is not None and record.execution_arn.endswith(run["run_id"])
        assert record.status == "queued" and record.mode == "live"
        assert record.started_by_rep_id == "adm"
        families, task_count = sweep_shape()
        assert record.tasks_total == len(families) * task_count
        assert record.estimate_usd is not None and record.estimate_basis == "static"
        lock = runs.read_lock(store)
        assert lock is not None and lock.holder_run_id == run["run_id"]
        assert lock.execution_arn == record.execution_arn

    def test_a_stub_submit_carries_stub_provider_mode(self, store: Store, sfn: Sfn) -> None:
        _status, run = _call(store, sfn, _event({"action": "submit", "mode": "stub"}, full=True))
        assert json.loads(sfn.started[0]["input"])["provider_mode"] == "stub"
        assert run["mode"] == "stub" and "estimate_usd" not in run

    def test_a_second_submit_while_one_runs_is_409_and_starts_nothing(
        self, store: Store, sfn: Sfn
    ) -> None:
        _call(store, sfn, _event({"action": "submit"}, full=True))
        status, body = _call(store, sfn, _event({"action": "submit"}, full=True))
        assert status == 409 and "already running" in body["error"]["message"]
        assert len(sfn.started) == 1 and len(_job_rows(store)) == 1

    def test_after_a_terminal_state_a_new_sweep_can_start(self, store: Store, sfn: Sfn) -> None:
        _status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        _finish(store, run["run_id"])
        status, _body = _call(store, sfn, _event({"action": "submit"}, full=True))
        assert status == 200 and len(sfn.started) == 2

    def test_a_crashed_execution_frees_the_next_submit(self, store: Store, sfn: Sfn) -> None:
        _status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        arn = runs.require_run(store, run["run_id"]).execution_arn
        assert arn is not None
        sfn.statuses[arn] = "FAILED"
        status, _body = _call(store, sfn, _event({"action": "submit"}, full=True))
        assert status == 200
        assert runs.require_run(store, run["run_id"]).status == "failed"

    def test_the_eleventh_live_sweep_of_the_day_is_409_and_stubs_are_uncapped(
        self, store: Store, sfn: Sfn
    ) -> None:
        live = {"action": "submit", "mode": "live", "confirmed": True}
        for _ in range(runs.LIVE_SWEEPS_PER_UTC_DAY):
            status, run = _call(store, sfn, _event(live, admin=True))
            assert status == 200
            _finish(store, run["run_id"])
        status, body = _call(store, sfn, _event(live, admin=True))
        assert status == 409 and "daily limit" in body["error"]["message"]
        assert runs.read_lock(store) is None
        for _ in range(3):
            status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
            assert status == 200
            _finish(store, run["run_id"])

    def test_a_rejected_start_fails_the_run_frees_the_lock_and_refunds_the_quota(
        self, store: Store, sfn: Sfn
    ) -> None:
        sfn.start_error = _client_error("StateMachineDoesNotExist")
        live = {"action": "submit", "mode": "live", "confirmed": True}
        status, _body = _call(store, sfn, _event(live, admin=True))
        assert status >= 400
        (row,) = _job_rows(store)
        record = runs.require_run(store, runs.run_id_of_job_row(str(row["run_id"])))
        assert record.status == "failed" and record.failure_summary
        assert runs.read_lock(store) is None
        assert _quota(store) == 0

    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError("read timed out"),
            _client_error("ThrottlingException"),
            _client_error("ExecutionAlreadyExists"),
        ],
        ids=["client-timeout", "throttled", "already-exists"],
    )
    def test_an_unclear_start_that_did_start_keeps_the_lock_and_records_the_arn(
        self, store: Store, sfn: Sfn, error: BaseException
    ) -> None:
        """Step Functions accepted the execution but the client saw an error."""
        sfn.start_error = error
        sfn.accepted = True
        status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        assert status == 200
        record = runs.require_run(store, run["run_id"])
        assert record.status == "queued" and record.execution_arn is not None
        assert record.execution_arn.endswith(f":{run['run_id']}")
        lock = runs.read_lock(store)
        assert lock is not None and lock.holder_run_id == run["run_id"]
        assert _call(store, sfn, _event({"action": "submit"}, full=True))[0] == 409

    def test_a_start_response_without_an_arn_is_probed(self, store: Store, sfn: Sfn) -> None:
        sfn.no_arn = True
        status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        assert status == 200
        assert runs.require_run(store, run["run_id"]).execution_arn is not None

    def test_an_unclear_start_that_did_not_start_fails_and_refunds(
        self, store: Store, sfn: Sfn
    ) -> None:
        sfn.start_error = TimeoutError("read timed out")
        live = {"action": "submit", "mode": "live", "confirmed": True}
        status, _body = _call(store, sfn, _event(live, admin=True))
        assert status == 500
        (row,) = _job_rows(store)
        assert (
            runs.require_run(store, runs.run_id_of_job_row(str(row["run_id"]))).status == "failed"
        )
        assert runs.read_lock(store) is None
        assert _quota(store) == 0

    def test_an_unresolvable_start_leaves_the_run_queued_and_the_lock_held(
        self, store: Store, sfn: Sfn
    ) -> None:
        sfn.start_error = TimeoutError("read timed out")
        sfn.accepted = True
        sfn.describe_error = "ServiceUnavailable"
        status, body = _call(store, sfn, _event({"action": "submit"}, full=True))
        assert status == 500 and "may have started" in body["error"]["message"]
        (row,) = _job_rows(store)
        assert (
            runs.require_run(store, runs.run_id_of_job_row(str(row["run_id"]))).status == "queued"
        )
        assert runs.read_lock(store) is not None

    def test_an_unrecorded_arn_leaves_the_sweep_running_and_the_lock_held(
        self, store: Store, sfn: Sfn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The execution started and both ARN writes failed."""
        calls: list[str] = []

        def _fails(*_a: Any, **kwargs: Any) -> None:
            calls.append(str(kwargs.get("execution_arn")))
            raise RuntimeError("write failed")

        monkeypatch.setattr(runs, "set_execution_arn", _fails)
        status, body = _call(store, sfn, _event({"action": "submit"}, full=True))
        assert status == 500 and "started" in body["error"]["message"]
        assert len(calls) == 2 and len(sfn.started) == 1
        (row,) = _job_rows(store)
        assert (
            runs.require_run(store, runs.run_id_of_job_row(str(row["run_id"]))).status == "queued"
        )
        assert runs.read_lock(store) is not None

    def test_one_failed_arn_write_is_retried(
        self, store: Store, sfn: Sfn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = runs.set_execution_arn
        attempts: list[int] = []

        def _flaky(*args: Any, **kwargs: Any) -> runs.RunRecord:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("blip")
            return real(*args, **kwargs)

        monkeypatch.setattr(runs, "set_execution_arn", _flaky)
        status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        assert status == 200 and len(attempts) == 2
        assert runs.require_run(store, run["run_id"]).execution_arn is not None

    def test_a_cleanup_failure_does_not_mask_the_original_error(
        self,
        store: Store,
        sfn: Sfn,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failing mark_failed still releases the lock, and the start error is reported."""
        sfn.start_error = _client_error("StateMachineDoesNotExist")

        def _broken(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("store down")

        monkeypatch.setattr(runs, "mark_failed", _broken)
        with caplog.at_level("ERROR", logger="eval.trigger"):
            status, _body = _call(store, sfn, _event({"action": "submit"}, full=True))
        assert status == 500
        assert runs.read_lock(store) is None
        assert any("eval_sweep_cleanup_failed" in r.getMessage() for r in caplog.records)
        assert any("eval_sweep_start_rejected" in r.getMessage() for r in caplog.records)

    def test_a_failed_estimate_consumes_no_quota_and_frees_the_lock(
        self, store: Store, sfn: Sfn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The quota is taken only once the estimate succeeded."""

        def _no_estimate(*_a: Any, **_k: Any) -> None:
            raise CrmError("The live-sweep estimate could not be computed.")

        monkeypatch.setattr(runs, "estimate", _no_estimate)
        live = {"action": "submit", "mode": "live", "confirmed": True}
        assert _call(store, sfn, _event(live, admin=True))[0] == 500
        assert _quota(store) is None and runs.read_lock(store) is None and _job_rows(store) == []

    def test_a_failed_record_write_refunds_the_quota(
        self, store: Store, sfn: Sfn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_record(*_a: Any, **_k: Any) -> None:
            raise CrmError("write failed")

        monkeypatch.setattr(runs, "create_run", _no_record)
        live = {"action": "submit", "mode": "live", "confirmed": True}
        assert _call(store, sfn, _event(live, admin=True))[0] == 500
        assert _quota(store) == 0 and runs.read_lock(store) is None

    def test_an_invalid_state_machine_arn_is_500_and_writes_nothing(
        self, store: Store, sfn: Sfn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(EVAL_STATE_MACHINE_ARN_ENV_VAR, "eval_sweep")
        assert _call(store, sfn, _event({"action": "submit"}, full=True))[0] == 500
        assert _job_rows(store) == [] and runs.read_lock(store) is None

    def test_no_state_machine_configured_is_500_and_writes_nothing(
        self, store: Store, sfn: Sfn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(EVAL_STATE_MACHINE_ARN_ENV_VAR)
        status, _body = _call(store, sfn, _event({"action": "submit"}, full=True))
        assert status == 500
        assert _job_rows(store) == [] and runs.read_lock(store) is None


class TestErrors:
    def test_an_unreadable_estimate_file_is_a_500_not_a_crash(
        self, store: Store, sfn: Sfn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The handler renders an estimate failure as the taxonomy's internal error."""

        def _unparseable(*_a: Any, **_k: Any) -> None:
            raise yaml.YAMLError("bad yaml")

        monkeypatch.setattr(runs, "load_static_estimate", _unparseable)
        status, body = _call(store, sfn, _event({"action": "estimate"}, full=True))
        assert status == 500 and body["error"]["code"] == "internal_error"


class TestReads:
    def test_a_full_visibility_user_reads_status_list_and_estimate(
        self, store: Store, sfn: Sfn
    ) -> None:
        _status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        status, body = _call(
            store, sfn, _event({"action": "status", "run_id": run["run_id"]}, full=True)
        )
        assert status == 200 and body["run_id"] == run["run_id"] and body["is_terminal"] is False
        status, page = _call(store, sfn, _event({"action": "list"}, full=True))
        assert status == 200 and [r["run_id"] for r in page["runs"]] == [run["run_id"]]
        assert page["next_cursor"] is None
        status, estimate = _call(store, sfn, _event({"action": "estimate"}, full=True))
        assert status == 200 and estimate["basis"] == "static" and estimate["estimate_usd"] > 0

    def test_status_reconciles_a_failed_execution(self, store: Store, sfn: Sfn) -> None:
        _status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        arn = runs.require_run(store, run["run_id"]).execution_arn
        assert arn is not None
        sfn.statuses[arn] = "TIMED_OUT"
        _status, body = _call(
            store, sfn, _event({"action": "status", "run_id": run["run_id"]}, full=True)
        )
        assert body["status"] == "failed" and body["teardown_status"] == "unknown"
        assert runs.read_lock(store) is None

    def test_status_of_an_unknown_run_is_404(self, store: Store, sfn: Sfn) -> None:
        assert (
            _call(store, sfn, _event({"action": "status", "run_id": "nope"}, full=True))[0] == 404
        )

    def test_the_payload_carries_no_execution_arn_or_account(self, store: Store, sfn: Sfn) -> None:
        _status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        _status, body = _call(
            store, sfn, _event({"action": "status", "run_id": run["run_id"]}, full=True)
        )
        assert "123456789012" not in json.dumps(body) and "execution_arn" not in body


class TestArguments:
    @pytest.mark.parametrize(
        ("body", "source"),
        [
            ({"action": "explode"}, "action"),
            ({}, "action"),
            ({"action": "list", "rep_id": "x"}, "arguments"),
            ({"action": "estimate", "run_id": "x"}, "arguments"),
            ({"action": "submit", "mode": "turbo"}, "mode"),
            ({"action": "submit", "confirmed": "yes"}, "confirmed"),
            ({"action": "status"}, "run_id"),
            ({"action": "status", "run_id": "job#x"}, "run_id"),
            ({"action": "list", "cursor": ""}, "cursor"),
            ({"action": "list", "cursor": "AAAA"}, "cursor"),
        ],
    )
    def test_invalid_arguments_are_400_naming_the_source(
        self, store: Store, sfn: Sfn, body: dict[str, Any], source: str
    ) -> None:
        status, response = _call(store, sfn, _event(body, admin=True, full=True))
        assert status == 400 and response["error"]["source"] == source
        assert sfn.started == []


def test_a_refused_request_builds_no_step_functions_client(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default client is built on first use, so a 401/403/400 path needs no AWS client."""

    def _no_client(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a Step Functions client was built")

    monkeypatch.setattr(boto3, "client", _no_client)
    event = _event({"action": "submit", "mode": "live", "confirmed": True})
    response = handler_module.handler(event, None, store=store)
    assert response["statusCode"] == 403
    unauthenticated = _event({"action": "list"})
    del unauthenticated["requestContext"]["authorizer"]
    assert handler_module.handler(unauthenticated, None, store=store)["statusCode"] == 401


def test_the_default_client_uses_the_resolved_region(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Step Functions client's region comes from `crm.config.resolve_aws_config`."""
    built: list[dict[str, Any]] = []

    class _Client:
        def describe_execution(self, **_k: Any) -> dict[str, Any]:
            return {"status": "RUNNING"}

    def _record(service: str, **kwargs: Any) -> _Client:
        built.append({"service": service, **kwargs})
        return _Client()

    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setattr(boto3, "client", _record)
    lock_holder = _event({"action": "unlock"}, admin=True)
    assert runs.acquire_lock(store, run_id="r1")
    runs.create_run(
        store,
        run_id="r1",
        mode="stub",
        identity=decode(_event({"action": "list"}))[0],
        tasks_total=1,
    )
    runs.set_execution_arn(
        store, run_id="r1", execution_arn="arn:aws:states:us-east-1:123456789012:execution:x:r1"
    )
    response = handler_module.handler(lock_holder, None, store=store)
    assert response["statusCode"] == 409
    from crm.config import resolve_aws_config

    assert built == [
        {"service": "stepfunctions", "region_name": resolve_aws_config(validate=False).region}
    ]


def _unlocked_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    lines = []
    for record in caplog.records:
        try:
            parsed = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("event") == "eval_run_unlocked":
            lines.append(parsed)
    return lines


class TestUnlockAttribution:
    def test_an_admin_release_logs_one_line_naming_the_releasing_rep(
        self, store: Store, sfn: Sfn, caplog: pytest.LogCaptureFixture
    ) -> None:
        _status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        arn = runs.require_run(store, run["run_id"]).execution_arn
        assert arn is not None
        sfn.statuses[arn] = "ABORTED"
        caplog.set_level("INFO")
        status, body = _call(store, sfn, _event({"action": "unlock"}, admin=True, rep="rep-adm"))
        assert status == 200 and body["released"] is True
        assert _unlocked_lines(caplog) == [
            {"event": "eval_run_unlocked", "rep_id": "rep-adm", "correlation_id": run["run_id"]}
        ]

    def test_a_forced_release_fails_the_holder_with_a_summary_naming_the_rep(
        self, store: Store, sfn: Sfn, caplog: pytest.LogCaptureFixture
    ) -> None:
        _status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        arn = runs.require_run(store, run["run_id"]).execution_arn
        assert arn is not None
        sfn.statuses[arn] = "PENDING_REDRIVE"
        caplog.set_level("INFO")
        status, body = _call(store, sfn, _event({"action": "unlock"}, admin=True, rep="rep-adm"))
        assert status == 200 and body == {"released": True, "holder_run_id": run["run_id"]}
        record = runs.require_run(store, run["run_id"])
        assert record.status == "failed"
        assert record.failure_summary == "An administrator (rep rep-adm) released the sweep lock."
        assert [line["rep_id"] for line in _unlocked_lines(caplog)] == ["rep-adm"]

    def test_a_refused_or_empty_unlock_logs_no_release(
        self, store: Store, sfn: Sfn, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO")
        assert _call(store, sfn, _event({"action": "unlock"}, admin=True))[1]["released"] is False
        _call(store, sfn, _event({"action": "submit"}, full=True))
        assert _call(store, sfn, _event({"action": "unlock"}, full=True))[0] == 403
        assert _call(store, sfn, _event({"action": "unlock"}, admin=True))[0] == 409
        assert _unlocked_lines(caplog) == []
        assert runs.read_lock(store) is not None


class TestReadScope:
    @pytest.mark.parametrize("action", ["estimate", "status", "list"])
    def test_a_book_limited_caller_is_403_eval_trigger(
        self, store: Store, sfn: Sfn, action: str
    ) -> None:
        _status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        body: dict[str, Any] = {"action": action}
        if action == "status":
            body["run_id"] = run["run_id"]
        status, response = _call(store, sfn, _event(body, rep="rep-book"))
        assert status == 403
        assert response["error"]["missing_scope"] == "eval:trigger"
        assert run["run_id"] not in json.dumps(response)

    def test_a_book_limited_caller_learns_nothing_about_an_unknown_run(
        self, store: Store, sfn: Sfn
    ) -> None:
        status, response = _call(store, sfn, _event({"action": "status", "run_id": "nope"}))
        assert status == 403 and response["error"]["missing_scope"] == "eval:trigger"

    @pytest.mark.parametrize(("admin", "full"), [(True, False), (False, True), (True, True)])
    @pytest.mark.parametrize("action", ["estimate", "status", "list"])
    def test_an_admin_or_full_visibility_caller_still_reads(
        self, store: Store, sfn: Sfn, action: str, admin: bool, full: bool
    ) -> None:
        _status, run = _call(store, sfn, _event({"action": "submit"}, full=True))
        body: dict[str, Any] = {"action": action}
        if action == "status":
            body["run_id"] = run["run_id"]
        status, response = _call(store, sfn, _event(body, admin=admin, full=full))
        assert status == 200, response

    def test_admin_claims_on_the_agent_channel_do_not_grant_reads(
        self, store: Store, sfn: Sfn
    ) -> None:
        event = {
            AGENT_IDENTITY_KEY: {
                "claims": {CLAIM_REP_ID: "rep-1", CLAIM_TENANT: "demo", CLAIM_GROUPS: ["admin"]},
                "session_id": "s",
            },
            "action": "list",
        }
        response = handler_module.handler(event, None, store=store, sfn=sfn)
        assert response["statusCode"] == 403
        assert response["error"]["missing_scope"] == "eval:trigger"
