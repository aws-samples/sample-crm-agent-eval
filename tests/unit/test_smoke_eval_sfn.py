# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.eval_sfn`.

The run API is a stateful fake of the `eval_trigger` actions, and the store and AWS clients are
fakes. The clock and sleeper are injected, so nothing opens a socket, provisions a user or sleeps.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from scripts.smoke import eval_sfn as mod
from scripts.smoke.admin_authz import SimulationRow, simulation_result
from scripts.smoke.cognito_claims import ProvisionedSmokeUser
from scripts.smoke.harness import CheckResult, SmokeContext, context_for_test
from scripts.smoke.iam_roles import (
    SWEEP_GRANT_LOCALS,
    ExtraCondition,
    LocalGrant,
    condition_matches,
    terraform_local_grants,
)

from agents.prompt_bundle import prompt_bundle_sha256
from crm.authz import DEMO_TENANT
from crm.store.keys import SWEEP_LOCK_ROW_ID
from crm.store.schema import TABLES
from eval.harness import ArtifactAssertionError
from eval.matrix import TIER_TASK_METRICS_KEY
from eval.runs import EVAL_RUNS_TABLE
from eval.sweep_units import DEFAULT_ARM_EFFORT
from eval.tenancy import CANARY_TENANT, unit_tenants_for_run
from eval.trigger import sweep_shape
from providers.capabilities import Family, Tier, load_capability_matrix
from providers.tier_tuning import TIERS

REPO_ROOT = Path(__file__).resolve().parents[2]
ASL_TEMPLATE = REPO_ROOT / "infra" / "templates" / "eval_sweep.asl.json"
EVAL_SFN_TF = REPO_ROOT / "infra" / "eval_sfn.tf"

_FAMILIES, _TASKS = sweep_shape()
_TOTAL = len(_FAMILIES) * _TASKS
_REPEATS = mod.REHEARSAL_REPEATS
_REHEARSAL_TOTAL = len(_FAMILIES) * _REPEATS * _TASKS
_SM_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:crm-agent-eval-eval-sweep"
_REP = "rep-admin"
_OUTPUTS: dict[str, Any] = {
    "eval_state_machine_arn": _SM_ARN,
    "eval_artifacts_bucket_name": "artifacts-bucket",
    "eval_artifacts_bucket_arn": "arn:aws:s3:::artifacts-bucket",
    "eval_artifacts_prefix": "eval-artifacts/",
    "eval_trigger_function_name": "crm-agent-eval-eval-trigger",
    "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/r",
    "dynamodb_table_names": {table: f"crm-agent-eval-{table}" for table in TABLES},
}


_CAPABILITIES = load_capability_matrix()


def _configured(family: str, tier: str) -> str:
    return _CAPABILITIES.tiers[cast(Family, family)][cast(Tier, tier)].model_id


def _matrix() -> dict[str, Any]:
    return {
        "arms": [
            {
                "provenance": {
                    "model_id": _configured(family, "supervisor"),
                    "effort": DEFAULT_ARM_EFFORT,
                    "prompt_bundle_sha256": prompt_bundle_sha256(),
                },
                "tier_summary": {tier: {} for tier in TIERS},
                TIER_TASK_METRICS_KEY: {
                    "by_tier": {
                        tier: {"configured_model_id": _configured(family, tier)} for tier in TIERS
                    }
                },
            }
            for family in _FAMILIES
        ]
    }


def _tier_table(arms: int) -> dict[str, Any]:
    """A `tier_comparison_table` in `eval.matrix.tier_comparison_table`'s shape for `arms` arms."""
    return {
        "headers": ["Metric", *(f"arm-{index}" for index in range(arms))],
        "sections": [
            {"tier": tier, "heading": heading, "rows": [["Model", *(["m"] * arms)]]}
            for tier, heading in (
                ("supervisor", "Supervisor"),
                ("subagent", "Subagent"),
                ("tool", "ToolCall"),
            )
        ],
        "notes": [],
    }


class FakeEvalApi:
    """A stateful `/api/eval_trigger`: each `status` advances a running sweep by `step` tasks."""

    def __init__(
        self,
        *,
        step: int = _TOTAL // 2,
        single_flight: bool = True,
        stuck: bool = False,
        final_status: str = "completed",
    ) -> None:
        self.step = step
        self.single_flight = single_flight
        self.stuck = stuck
        self.final_status = final_status
        self.runs: dict[str, dict[str, Any]] = {}
        self.lock: str | None = None
        self.calls: list[dict[str, Any]] = []
        self._next = 0

    def __call__(
        self, route: str, token: str | None, body: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        assert route == "eval_trigger"
        assert token == "id-token"  # nosec B105  # fake id-token fixture, not a credential
        self.calls.append(dict(body))
        action = body["action"]
        if action == "estimate":
            return 200, {"estimate_usd": 1.25, "basis": "static", "per_arm_usd": {}}
        if action == "submit":
            return self._submit(body)
        if action == "list":
            return 200, {"runs": [dict(r) for r in reversed(self.runs.values())]}
        run = self.runs[str(body["run_id"])]
        if action == "status":
            self._advance(run)
            return 200, dict(run)
        if action == "artefact_url":
            key = str(body["key"])
            recorded = run.get("artifacts", {})
            if key not in recorded.get("raw", []) and key != recorded.get("matrix_md"):
                return 404, {"error": {"code": "not_found", "message": "no such artefact"}}
            url = f"https://artifacts-bucket.s3.amazonaws.com/eval-artifacts/{run['run_id']}/{key}?X-Amz-Signature=sig"
            if body.get("download") is True:
                url += "&response-content-disposition=attachment"
            return 200, {"run_id": run["run_id"], "key": key, "url": url, "expires_in": 300}
        if action == "cancel":
            if run["is_terminal"]:
                return 200, {"disposition": "already_terminal", "run": dict(run)}
            run["cancel_requested"] = True
            return 200, {"disposition": "accepted", "run": dict(run)}
        raise AssertionError(action)

    def _submit(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.single_flight and self.lock is not None:
            return 409, {"error": {"code": "conflict", "message": "A sweep is already running"}}
        self._next += 1
        run_id = f"run{self._next:04d}"
        repeats = int(body.get("repeats", 1))
        run = {
            "run_id": run_id,
            "tenant": f"eval-{run_id}",
            "status": "queued",
            "mode": body.get("mode", "stub"),
            "is_terminal": False,
            "repeats": repeats,
            "tasks_total": len(_FAMILIES) * repeats * _TASKS,
            "tasks_done": 0,
            "tasks_failed": 0,
            "tasks_skipped": 0,
            "cost_spent_usd": 0.0,
            "cancel_requested": False,
            "started_by_rep_id": _REP,
        }
        self.runs[run_id] = run
        self.lock = run_id
        return 200, dict(run)

    def _finish(self, run: dict[str, Any], status: str) -> None:
        run.update(
            status=status,
            is_terminal=True,
            teardown_status="ok",
            matrix=_matrix(),
            artifacts={"raw": ["raw/arm-0.json", "raw/arm-1.json"], "matrix_md": "matrix.md"},
            tier_comparison_table=_tier_table(len(_FAMILIES)),
            cost_spent_usd=0.5 if run["mode"] == "live" else 0.0,
        )
        if self.lock == run["run_id"]:
            self.lock = None

    def _advance(self, run: dict[str, Any]) -> None:
        if run["is_terminal"] or self.stuck:
            return
        total = run["tasks_total"]
        if run["cancel_requested"]:
            run["tasks_skipped"] = total - run["tasks_done"]
            self._finish(run, "cancelled")
            return
        run["status"] = "running"
        run["tasks_done"] = min(total, run["tasks_done"] + self.step)
        if run["tasks_done"] == total:
            self._finish(run, self.final_status)


class FakeStore:
    """Tenant rows by (table, tenant), the lock row read from the fake API, and each run's
    `eval_runs` catalog status (`torn_down` unless set; `None` means no catalog row).

    The catalog row records the run's unit tenants (`unit_tenants` overrides them), and each unit
    tenant holds one seeded `accounts` row while its run is running, unless listed in `unseeded`.
    """

    def __init__(self, api: FakeEvalApi, *, demo_rows: int = 3) -> None:
        self._api = api
        self.rows: dict[tuple[str, str], list[dict[str, object]]] = {
            ("accounts", DEMO_TENANT): [{"account_id": f"a{i}"} for i in range(demo_rows)]
        }
        self.catalog: dict[str, str | None] = {}
        self.catalog_error: Exception | None = None
        self.unit_tenants: dict[str, object] = {}
        self.unseeded: set[str] = set()

    def _units(self, run_id: str) -> tuple[str, ...]:
        run = self._api.runs.get(run_id)
        if run is None:
            return ()
        return unit_tenants_for_run(run_id, arm_count=len(_FAMILIES), repeats=run["repeats"])

    def query_tenant_scan(
        self,
        table: str,
        *,
        tenant: str,
        limit: int | None = None,
        exclusive_start_key: Mapping[str, object] | None = None,
        scan_forward: bool = True,
    ) -> tuple[list[dict[str, object]], None]:
        rows = list(self.rows.get((table, tenant), []))
        if table == "accounts" and tenant not in self.unseeded:
            for run_id, run in self._api.runs.items():
                if run["status"] == "running" and tenant in self._units(run_id):
                    rows.append({"account_id": "seeded"})
        return rows, None

    def get_item(self, table: str, key: Mapping[str, str]) -> dict[str, object] | None:
        assert table == EVAL_RUNS_TABLE
        if key == {"run_id": SWEEP_LOCK_ROW_ID}:
            return None if self._api.lock is None else {"holder_run_id": self._api.lock}
        if self.catalog_error is not None:
            raise self.catalog_error
        run_id = key["run_id"]
        status = self.catalog.get(run_id, "torn_down")
        if status is None:
            return None
        units = self.unit_tenants.get(run_id, list(self._units(run_id)))
        return {"run_id": run_id, "status": status, "unit_tenants": units}

    def physical_name(self, table: str) -> str:
        return f"crm-agent-eval-{table}"


class FakeSfn:
    def __init__(self, *, events: Sequence[str] = ("ExecutionStarted", "ExecutionSucceeded")):
        self.events = list(events)
        self.described: list[str] = []

    def describe_execution(self, *, executionArn: str) -> dict[str, Any]:  # noqa: N803
        self.described.append(executionArn)
        return {"status": "SUCCEEDED"}

    def get_execution_history(self, **_: Any) -> dict[str, Any]:
        return {"events": [{"type": t} for t in self.events]}


class FakeDynamoDb:
    """The rehearsal's `eval_results` rows: one per (arm, repeat, task) at `repeats` repeats."""

    def __init__(
        self, *, stream_calls: int = 3, repeats: int = _REPEATS, duplicate_ids: bool = False
    ) -> None:
        self.stream_calls = stream_calls
        self.repeats = repeats
        self.duplicate_ids = duplicate_ids

    def query(self, **request: Any) -> dict[str, Any]:
        return {
            "Items": [
                {
                    "run_id": {"S": request["ExpressionAttributeValues"][":r"]["S"]},
                    "result_id": {
                        "S": "dup" if self.duplicate_ids else f"{arm:02d}#t{task}#r{repeat:02d}"
                    },
                    "repeat_index": {"N": str(repeat)},
                    "item_status": {"S": "done"},
                    "stream_calls": {"N": str(self.stream_calls)},
                }
                for arm in range(len(_FAMILIES))
                for repeat in range(self.repeats)
                for task in range(_TASKS)
            ]
        }


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    def __init__(self, keys: Iterable[str] = ()) -> None:
        self.keys = list(keys)

    def list_objects_v2(self, *, Bucket: str, Prefix: str, **_: Any) -> dict[str, Any]:  # noqa: N803
        return {"Contents": [{"Key": k} for k in self.keys if k.startswith(Prefix)]}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"Body": _Body(Key.encode())}


class FakeProvisioner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.cleaned = False

    def provision(self) -> ProvisionedSmokeUser:
        if self.fail:
            raise RuntimeError("cognito unavailable")
        return ProvisionedSmokeUser(
            username=_REP,
            rep_id=_REP,
            tenant="smoke-x",
            book=frozenset(),
            id_token="id-token",  # nosec B106  # fake id-token fixture, not a credential
            detail="TOTP",
        )

    def cleanup(self) -> Iterator[CheckResult]:
        self.cleaned = True
        yield CheckResult(resource="smoke_user", check="cognito_user_deleted", status="pass")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _artifacts_pass(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(mod, "assert_artifacts", lambda **kwargs: calls.append(kwargs))
    return calls


def _ctx(
    *, sfn: FakeSfn | None = None, dynamodb: FakeDynamoDb | None = None, s3: FakeS3 | None = None
) -> SmokeContext:
    return context_for_test(
        outputs=_OUTPUTS,
        clients={
            "stepfunctions": sfn or FakeSfn(),
            "dynamodb": dynamodb or FakeDynamoDb(),
            "s3": s3 or FakeS3(),
        },
    )


class FakeFetch:
    """S3's answer to a presigned GET: 200 over https for an untampered key; plain http 403
    `AccessDenied`; a tampered key 403 `SignatureDoesNotMatch` (measured live, 2026-10-13)."""

    def __init__(
        self,
        *,
        https_status: int = 200,
        http_status: int = 403,
        tampered_status: int = 403,
        http_code: str | None = "AccessDenied",
        tampered_code: str | None = "SignatureDoesNotMatch",
    ) -> None:
        self.https_status = https_status
        self.http_status = http_status
        self.tampered_status = tampered_status
        self.http_code = http_code
        self.tampered_code = tampered_code
        self.urls: list[str] = []

    def __call__(self, url: str) -> tuple[int, str | None]:
        self.urls.append(url)
        if ".tampered" in url:
            return self.tampered_status, self.tampered_code
        if url.startswith("https://"):
            return self.https_status, None
        return self.http_status, self.http_code


class FakeArtefactGet:
    """S3's answer to an https GET of a presigned artefact URL: the same bytes either way, with
    `Content-Disposition: attachment` when the URL asked for it."""

    def __init__(
        self,
        *,
        disposition: str | None = 'attachment; filename="matrix-run.md"',
        download_body: bytes = b"# matrix\n",
        plain_body: bytes = b"# matrix\n",
        status: int = 200,
    ) -> None:
        self.disposition = disposition
        self.download_body = download_body
        self.plain_body = plain_body
        self.status = status
        self.urls: list[str] = []

    def __call__(self, url: str) -> tuple[int, Mapping[str, str], bytes]:
        self.urls.append(url)
        if "response-content-disposition" in url:
            headers = {"content-disposition": self.disposition} if self.disposition else {}
            return self.status, headers, self.download_body
        return 200, {}, self.plain_body


def _run(
    api: FakeEvalApi,
    *,
    ctx: SmokeContext | None = None,
    store: FakeStore | None = None,
    provisioner: FakeProvisioner | None = None,
    fetch: FakeFetch | None = None,
    artefact_get: FakeArtefactGet | None = None,
) -> dict[str, CheckResult]:
    clock = Clock()
    results = list(
        mod.checks(
            ctx or _ctx(),
            provisioner=provisioner or FakeProvisioner(),
            post=api,
            store=store or FakeStore(api),
            simulations=lambda _ctx: iter(()),
            clock=clock,
            sleeper=clock.sleep,
            fetch=fetch or FakeFetch(),
            artefact_get=artefact_get or FakeArtefactGet(),
        )
    )
    by_check = {r.check: r for r in results}
    assert len(by_check) == len(results), [r.check for r in results]
    return by_check


_STUB_FLOW_CHECKS = (
    "provision_admin_user",
    "estimate_returns_basis_and_number",
    "rehearsal_submitted",
    "second_submit_refused_409",
    "unit_tenants_recorded",
    "unit_tenants_seeded",
    "run_completed_all_tasks",
    "run_records_repeats",
    *(f"arm_effort_is_default:{family}" for family in _FAMILIES),
    *(f"arm_models_are_configured:{family}" for family in _FAMILIES),
    "prompt_bundle_matches_repo",
    "rehearsal_execution_succeeded",
    "results_done_with_runtime_streams",
    "artifacts_pass_assert_artifacts",
    "artefact_url_issued",
    "artefact_url_https_get_succeeds",
    "artefact_url_plain_http_denied",
    "artefact_url_tampered_key_denied",
    "status_carries_tier_comparison_table",
    "matrix_md_download_is_attachment",
    "matrix_md_download_bytes_match_artefact",
    "rehearsal_tenant_empty",
    "tenant_emptied_by_the_workers_own_role",
    "demo_tenant_untouched",
    "canary_tenant_empty",
    "lock_released_after_rehearsal",
    "list_shows_run_with_starter",
    "cancel_proof_submitted",
    "cancel_accepted",
    "cancelled_run_partial_and_torn_down",
    "cancel_proof_tenant_empty",
    "lock_released_after_cancel",
    "second_cancel_already_terminal",
    "cancelled_execution_not_aborted",
    "cognito_user_deleted",
)


def test_a_correct_stack_passes_every_stub_flow_check() -> None:
    by_check = _run(FakeEvalApi())
    assert tuple(by_check) == _STUB_FLOW_CHECKS
    failing = {c: r.detail for c, r in by_check.items() if r.status != "pass"}
    assert failing == {}


def test_a_second_sweep_admitted_while_one_runs_fails_and_is_settled() -> None:
    api = FakeEvalApi(single_flight=False)
    by_check = _run(api)
    assert by_check["second_submit_refused_409"].status == "fail"
    stray = [run for run in api.runs.values() if run["run_id"] != "run0001"][0]
    assert stray["cancel_requested"] is True


def test_an_aborted_execution_fails_the_cancel_proof() -> None:
    sfn = FakeSfn(events=("ExecutionStarted", "ExecutionAborted"))
    by_check = _run(FakeEvalApi(), ctx=_ctx(sfn=sfn))
    assert by_check["cancelled_execution_not_aborted"].status == "fail"
    assert "ExecutionAborted events=1" in by_check["cancelled_execution_not_aborted"].detail


def test_results_without_runtime_streams_fail() -> None:
    by_check = _run(FakeEvalApi(), ctx=_ctx(dynamodb=FakeDynamoDb(stream_calls=0)))
    assert by_check["results_done_with_runtime_streams"].status == "fail"


def test_a_changed_demo_row_count_fails() -> None:
    api = FakeEvalApi()
    store = FakeStore(api)
    original = api._advance

    def advance_and_write_demo(run: dict[str, Any]) -> None:
        original(run)
        store.rows[("accounts", DEMO_TENANT)].append({"account_id": "intruder"})

    api._advance = advance_and_write_demo  # type: ignore[method-assign]
    assert _run(api, store=store)["demo_tenant_untouched"].status == "fail"


def test_a_tenant_left_with_rows_fails_and_is_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeEvalApi()
    store = FakeStore(api)
    store.rows[("contacts", "eval-run0001")] = [{"contact_id": "c1"}]
    reaped: list[str] = []
    monkeypatch.setattr(mod, "teardown_tenant", lambda _store, *, tenant: reaped.append(tenant))
    by_check = _run(api, store=store)
    assert by_check["rehearsal_tenant_empty"].status == "fail"
    assert by_check["leftover_tenant_reaped"].status == "fail"
    assert reaped == ["eval-run0001"]


def test_rows_surviving_a_teardown_that_ran_are_a_grant_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeEvalApi()
    store = FakeStore(api)
    store.rows[("contacts", "eval-run0001")] = [{"contact_id": "c1"}]
    monkeypatch.setattr(mod, "teardown_tenant", lambda *_a, **_k: None)
    row = _run(api, store=store)["tenant_emptied_by_the_workers_own_role"]
    assert row.status == "fail"
    assert "'torn_down'" in row.detail and "{'contacts': 1}" in row.detail
    assert "a grant the role needs is missing" in row.detail


@pytest.mark.parametrize("catalog_status", ["running", None])
def test_rows_left_by_a_teardown_that_did_not_run_are_not_attributed_to_the_grants(
    monkeypatch: pytest.MonkeyPatch, catalog_status: str | None
) -> None:
    api = FakeEvalApi()
    store = FakeStore(api)
    store.catalog["run0001"] = catalog_status
    store.rows[("contacts", "eval-run0001")] = [{"contact_id": "c1"}]
    monkeypatch.setattr(mod, "teardown_tenant", lambda *_a, **_k: None)
    row = _run(api, store=store)["tenant_emptied_by_the_workers_own_role"]
    assert row.status == "fail"
    assert f"{catalog_status or 'absent'!r}, not 'torn_down'" in row.detail
    assert "NOT attributed" in row.detail
    assert "grant the role needs" not in row.detail


def test_an_empty_tenant_whose_catalog_row_is_not_torn_down_still_fails() -> None:
    api = FakeEvalApi()
    store = FakeStore(api)
    store.catalog["run0001"] = "running"
    assert _run(api, store=store)["tenant_emptied_by_the_workers_own_role"].status == "fail"


def test_an_unreadable_catalog_row_fails_the_emptied_row() -> None:
    api = FakeEvalApi()
    store = FakeStore(api)
    store.catalog_error = RuntimeError("throttled")
    row = _run(api, store=store)["tenant_emptied_by_the_workers_own_role"]
    assert row.status == "fail"
    assert "unreadable: RuntimeError: throttled" in row.detail


def test_a_failed_rehearsal_names_its_status() -> None:
    by_check = _run(FakeEvalApi(final_status="failed"))
    row = by_check["run_completed_all_tasks"]
    assert row.status == "fail" and "status='failed'" in row.detail


def test_a_sweep_that_finishes_before_progress_fails_the_cancel_proof() -> None:
    by_check = _run(FakeEvalApi(step=_REHEARSAL_TOTAL))
    assert by_check["cancel_proof_reached_progress"].status == "fail"
    assert "cancel_accepted" not in by_check


def test_a_sweep_that_never_finishes_times_out_and_is_cancelled_at_cleanup() -> None:
    api = FakeEvalApi(stuck=True)
    by_check = _run(api)
    assert by_check["rehearsal_reached_terminal"].status == "fail"
    assert by_check["no_run_left_running"].status == "fail"
    assert api.runs["run0001"]["cancel_requested"] is True


def test_a_provisioning_failure_still_cleans_up_and_submits_nothing() -> None:
    api = FakeEvalApi()
    provisioner = FakeProvisioner(fail=True)
    by_check = _run(api, provisioner=provisioner)
    assert by_check["provision_admin_user"].status == "fail"
    assert provisioner.cleaned and api.calls == []


def test_the_rehearsal_submits_repeats_and_expects_every_item() -> None:
    """The rehearsal submits `REHEARSAL_REPEATS` (more than one) and its completed row expects
    arms x repeats x tasks items; the cancel proof submits no `repeats`."""
    assert _REPEATS > 1
    api = FakeEvalApi()
    by_check = _run(api)
    submits = [call for call in api.calls if call["action"] == "submit"]
    assert submits[0]["repeats"] == _REPEATS
    assert all("repeats" not in call for call in submits[1:])
    assert f"(expected {_REHEARSAL_TOTAL})" in by_check["run_completed_all_tasks"].detail


def test_a_run_that_drops_repeats_reds_the_repeats_and_completed_rows() -> None:
    api = FakeEvalApi()
    original = api._submit

    def submit_ignoring_repeats(body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        return original({key: value for key, value in body.items() if key != "repeats"})

    api._submit = submit_ignoring_repeats  # type: ignore[method-assign]
    by_check = _run(api)
    assert by_check["run_records_repeats"].status == "fail"
    assert by_check["run_completed_all_tasks"].status == "fail"


def test_a_catalog_row_missing_a_unit_tenant_fails_the_recorded_row() -> None:
    api = FakeEvalApi()
    store = FakeStore(api)
    every = unit_tenants_for_run("run0001", arm_count=len(_FAMILIES), repeats=_REPEATS)
    store.unit_tenants["run0001"] = list(every[:-1])
    row = _run(api, store=store)["unit_tenants_recorded"]
    assert row.status == "fail" and every[-1] in row.detail


def test_an_absent_unit_tenants_list_fails_the_recorded_row() -> None:
    api = FakeEvalApi()
    store = FakeStore(api)
    store.unit_tenants["run0001"] = None
    row = _run(api, store=store)["unit_tenants_recorded"]
    assert row.status == "fail" and "absent" in row.detail


def test_an_unseeded_unit_tenant_fails_the_seeded_row() -> None:
    api = FakeEvalApi()
    store = FakeStore(api)
    every = unit_tenants_for_run("run0001", arm_count=len(_FAMILIES), repeats=_REPEATS)
    store.unseeded.add(every[1])
    row = _run(api, store=store)["unit_tenants_seeded"]
    assert row.status == "fail" and f"unseeded: [{every[1]!r}]" in row.detail


def test_a_rehearsal_already_terminal_when_observed_cannot_pass_the_seeded_row() -> None:
    row = _run(FakeEvalApi(step=_REHEARSAL_TOTAL))["unit_tenants_seeded"]
    assert row.status == "fail" and "not observable" in row.detail


def test_a_unit_tenant_left_with_rows_fails_the_emptied_row_and_is_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeEvalApi()
    store = FakeStore(api)
    unit = unit_tenants_for_run("run0001", arm_count=len(_FAMILIES), repeats=_REPEATS)[-1]
    store.rows[("notes", unit)] = [{"note_id": "n1"}]
    reaped: list[str] = []
    monkeypatch.setattr(mod, "teardown_tenant", lambda _store, *, tenant: reaped.append(tenant))
    by_check = _run(api, store=store)
    row = by_check["tenant_emptied_by_the_workers_own_role"]
    assert row.status == "fail" and f"{unit!r}: {{'notes': 1}}" in row.detail
    assert by_check["rehearsal_tenant_empty"].status == "pass"
    assert reaped == [unit]


def test_results_at_one_repeat_red_the_results_row() -> None:
    by_check = _run(FakeEvalApi(), ctx=_ctx(dynamodb=FakeDynamoDb(repeats=1)))
    assert by_check["results_done_with_runtime_streams"].status == "fail"


def test_results_sharing_a_result_id_red_the_results_row() -> None:
    by_check = _run(FakeEvalApi(), ctx=_ctx(dynamodb=FakeDynamoDb(duplicate_ids=True)))
    row = by_check["results_done_with_runtime_streams"]
    assert row.status == "fail" and "1 distinct result_id" in row.detail


def test_the_artefacts_are_asserted_at_tasks_times_repeats(
    _artifacts_pass: list[dict[str, Any]],
) -> None:
    _run(FakeEvalApi())
    assert [call["expected_task_count"] for call in _artifacts_pass] == [_TASKS * _REPEATS]


def test_check_artifacts_downloads_the_run_root_and_passes_the_record_raw_paths(
    tmp_path: Path, _artifacts_pass: list[dict[str, Any]]
) -> None:
    keys = ["eval-artifacts/run1/matrix.json", "eval-artifacts/run1/raw/a.json", "other/x"]
    run = {"run_id": "run1", "artifacts": {"raw": ["raw/a.json"]}}
    row = mod.check_artifacts(
        FakeS3(keys), run, bucket="b", prefix="eval-artifacts/", arm_count=2, task_count=20
    )
    assert row.status == "pass" and "2 object(s)" in row.detail
    (call,) = _artifacts_pass
    assert call["raw_archive_paths"][0].parts[-2:] == ("raw", "a.json")
    assert call["expected_arm_count"] == 2 and call["expected_task_count"] == 20


def test_check_artifacts_fails_without_a_raw_list_and_on_an_assertion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_raw = mod.check_artifacts(
        FakeS3(), {"run_id": "r"}, bucket="b", prefix="p", arm_count=2, task_count=20
    )
    assert no_raw.status == "fail"

    def broken(**_: Any) -> None:
        raise ArtifactAssertionError(["matrix.json missing"])

    monkeypatch.setattr(mod, "assert_artifacts", broken)
    row = mod.check_artifacts(
        FakeS3(),
        {"run_id": "r", "artifacts": {"raw": ["raw/a.json"]}},
        bucket="b",
        prefix="p",
        arm_count=2,
        task_count=20,
    )
    assert row.status == "fail" and "matrix.json missing" in row.detail


def test_completed_record_requires_a_tier_summary_for_every_tier() -> None:
    matrix = _matrix()
    run: dict[str, Any] = {
        "status": "completed",
        "tasks_total": _TOTAL,
        "tasks_done": _TOTAL,
        "tasks_failed": 0,
        "teardown_status": "ok",
        "matrix": matrix,
    }
    assert mod.check_completed_record(run, expected_total=_TOTAL, arm_count=2).status == "pass"
    matrix["arms"][0]["tier_summary"].pop("tool")
    assert mod.check_completed_record(run, expected_total=_TOTAL, arm_count=2).status == "fail"


def _effort_rows(run: Mapping[str, Any]) -> dict[str, CheckResult]:
    return {row.check: row for row in mod.check_arm_efforts(run, _FAMILIES)}


def test_the_default_sweep_families_are_anthropic_then_openai_at_medium() -> None:
    assert _FAMILIES == ("anthropic", "openai")
    assert DEFAULT_ARM_EFFORT == "medium"


def test_arms_at_the_default_effort_pass_one_row_per_family() -> None:
    rows = _effort_rows({"matrix": _matrix()})
    assert list(rows) == [f"arm_effort_is_default:{family}" for family in _FAMILIES]
    assert {row.status for row in rows.values()} == {"pass"}


@pytest.mark.parametrize("index", range(len(_FAMILIES)))
def test_an_arm_recorded_at_low_reds_only_its_family_row(index: int) -> None:
    matrix = _matrix()
    matrix["arms"][index]["provenance"]["effort"] = "low"
    rows = _effort_rows({"matrix": matrix})
    failed = [check for check, row in rows.items() if row.status == "fail"]
    assert failed == [f"arm_effort_is_default:{_FAMILIES[index]}"]
    assert "provenance.effort='low'" in rows[failed[0]].detail


@pytest.mark.parametrize(
    ("mutate", "what"),
    [
        (lambda arm: arm["provenance"].pop("effort"), "effort key"),
        (lambda arm: arm.pop("provenance"), "provenance block"),
        (lambda arm: arm["provenance"].update(effort=None), "null effort"),
    ],
    ids=["no-effort", "no-provenance", "null-effort"],
)
def test_an_absent_arm_effort_fails_rather_than_skips(
    mutate: Callable[[dict[str, Any]], object], what: str
) -> None:
    matrix = _matrix()
    mutate(matrix["arms"][0])
    row = _effort_rows({"matrix": matrix})[f"arm_effort_is_default:{_FAMILIES[0]}"]
    assert row.status == "fail", what
    assert "provenance.effort=absent" in row.detail


def test_a_missing_arm_or_matrix_fails_every_unmatched_family_row() -> None:
    matrix = _matrix()
    matrix["arms"].pop()
    rows = _effort_rows({"matrix": matrix})
    assert rows[f"arm_effort_is_default:{_FAMILIES[-1]}"].status == "fail"
    assert {row.status for row in _effort_rows({}).values()} == {"fail"}


def test_a_low_arm_effort_reds_the_stub_flow_end_to_end() -> None:
    api = FakeEvalApi()
    original = api._finish

    def finish_at_low(run: dict[str, Any], status: str) -> None:
        original(run, status)
        for arm in run.get("matrix", {}).get("arms", []):
            arm["provenance"]["effort"] = "low"

    api._finish = finish_at_low  # type: ignore[method-assign]
    by_check = _run(api)
    for family in _FAMILIES:
        assert by_check[f"arm_effort_is_default:{family}"].status == "fail"
    assert by_check["run_completed_all_tasks"].status == "pass"


def _model_rows(run: Mapping[str, Any]) -> dict[str, CheckResult]:
    return {row.check: row for row in mod.check_arm_models(run, _FAMILIES)}


def _other_model(family: str, tier: str) -> str:
    """A model of `family` that `models.yaml` does not assign to `tier`."""
    return next(
        model_id
        for model_id, model in sorted(_CAPABILITIES.models.items())
        if model.family == family and model_id != _configured(family, tier)
    )


def test_arms_at_the_configured_models_pass_one_row_per_family() -> None:
    rows = _model_rows({"matrix": _matrix()})
    assert list(rows) == [f"arm_models_are_configured:{family}" for family in _FAMILIES]
    assert {row.status for row in rows.values()} == {"pass"}, rows


@pytest.mark.parametrize("index", range(len(_FAMILIES)))
def test_an_arm_recording_a_stale_supervisor_reds_only_its_family_row(index: int) -> None:
    family = _FAMILIES[index]
    stale = _other_model(family, "supervisor")
    matrix = _matrix()
    matrix["arms"][index]["provenance"]["model_id"] = stale
    rows = _model_rows({"matrix": matrix})
    failed = [check for check, row in rows.items() if row.status == "fail"]
    assert failed == [f"arm_models_are_configured:{family}"]
    assert f"provenance.model_id={stale!r}" in rows[failed[0]].detail


@pytest.mark.parametrize("tier", TIERS)
def test_a_stale_configured_model_at_any_tier_fails(tier: str) -> None:
    family = _FAMILIES[-1]
    stale = _other_model(family, tier)
    matrix = _matrix()
    matrix["arms"][-1][TIER_TASK_METRICS_KEY]["by_tier"][tier]["configured_model_id"] = stale
    row = _model_rows({"matrix": matrix})[f"arm_models_are_configured:{family}"]
    assert row.status == "fail"
    assert f"{tier}.configured_model_id={stale!r}" in row.detail


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda arm: arm.pop("provenance"), "provenance.model_id=absent"),
        (lambda arm: arm["provenance"].pop("model_id"), "provenance.model_id=absent"),
        (lambda arm: arm.pop(TIER_TASK_METRICS_KEY), "supervisor.configured_model_id=absent"),
        (
            lambda arm: arm[TIER_TASK_METRICS_KEY]["by_tier"].pop("tool"),
            "tool.configured_model_id=absent",
        ),
    ],
    ids=["no-provenance", "no-model-id", "no-tier-metrics", "no-tool-tier"],
)
def test_an_absent_model_record_fails_rather_than_skips(
    mutate: Callable[[dict[str, Any]], object], field: str
) -> None:
    matrix = _matrix()
    mutate(matrix["arms"][0])
    row = _model_rows({"matrix": matrix})[f"arm_models_are_configured:{_FAMILIES[0]}"]
    assert row.status == "fail"
    assert field in row.detail


def test_a_missing_arm_or_matrix_fails_every_unmatched_model_row() -> None:
    matrix = _matrix()
    matrix["arms"].pop()
    rows = _model_rows({"matrix": matrix})
    assert rows[f"arm_models_are_configured:{_FAMILIES[-1]}"].status == "fail"
    assert {row.status for row in _model_rows({}).values()} == {"fail"}


def test_a_stale_supervisor_model_reds_the_stub_flow_end_to_end() -> None:
    api = FakeEvalApi()
    original = api._finish

    def finish_with_stale_models(run: dict[str, Any], status: str) -> None:
        original(run, status)
        for arm, family in zip(run.get("matrix", {}).get("arms", []), _FAMILIES, strict=True):
            arm["provenance"]["model_id"] = _other_model(family, "supervisor")

    api._finish = finish_with_stale_models  # type: ignore[method-assign]
    by_check = _run(api)
    for family in _FAMILIES:
        assert by_check[f"arm_models_are_configured:{family}"].status == "fail"
    assert by_check["run_completed_all_tasks"].status == "pass"


def test_every_arm_at_the_repos_prompt_bundle_passes() -> None:
    row = mod.check_prompt_bundle({"matrix": _matrix()}, _FAMILIES)
    assert row.check == "prompt_bundle_matches_repo"
    assert row.status == "pass", row.detail


@pytest.mark.parametrize("index", range(len(_FAMILIES)))
def test_an_arm_at_another_prompt_bundle_fails(index: int) -> None:
    matrix = _matrix()
    matrix["arms"][index]["provenance"]["prompt_bundle_sha256"] = "0" * 64
    row = mod.check_prompt_bundle({"matrix": matrix}, _FAMILIES)
    assert row.status == "fail"
    assert f"{_FAMILIES[index]}='{'0' * 64}'" in row.detail


@pytest.mark.parametrize(
    ("mutate", "what"),
    [
        (lambda arm: arm["provenance"].pop("prompt_bundle_sha256"), "digest key"),
        (lambda arm: arm["provenance"].update(prompt_bundle_sha256=None), "null digest"),
        (lambda arm: arm.pop("provenance"), "provenance block"),
    ],
    ids=["no-digest", "null-digest", "no-provenance"],
)
def test_an_absent_prompt_bundle_digest_fails_rather_than_skips(
    mutate: Callable[[dict[str, Any]], object], what: str
) -> None:
    matrix = _matrix()
    mutate(matrix["arms"][0])
    row = mod.check_prompt_bundle({"matrix": matrix}, _FAMILIES)
    assert row.status == "fail", what
    assert f"{_FAMILIES[0]}=absent" in row.detail


def test_a_missing_arm_or_matrix_fails_the_prompt_bundle_row() -> None:
    matrix = _matrix()
    matrix["arms"].pop()
    assert mod.check_prompt_bundle({"matrix": matrix}, _FAMILIES).status == "fail"
    assert mod.check_prompt_bundle({}, _FAMILIES).status == "fail"


def test_a_stale_prompt_bundle_reds_the_stub_flow_end_to_end() -> None:
    api = FakeEvalApi()
    original = api._finish

    def finish_stale(run: dict[str, Any], status: str) -> None:
        original(run, status)
        for arm in run.get("matrix", {}).get("arms", []):
            arm["provenance"]["prompt_bundle_sha256"] = "f" * 64

    api._finish = finish_stale  # type: ignore[method-assign]
    by_check = _run(api)
    assert by_check["prompt_bundle_matches_repo"].status == "fail"
    assert by_check["run_completed_all_tasks"].status == "pass"


def test_poll_run_stops_at_its_deadline() -> None:
    api = FakeEvalApi(stuck=True)
    api.runs["r"] = {"run_id": "r", "is_terminal": False, "cancel_requested": False}
    clock = Clock()
    last = mod.poll_run(
        mod.RunApi(api, "id-token"),
        "r",
        until=mod.is_terminal,
        seconds=60,
        clock=clock,
        sleeper=clock.sleep,
    )
    assert last["is_terminal"] is False and clock.now >= 60


# -- role simulations ----------------------------------------------------------------------------


def _rendered_definition() -> dict[str, Any]:
    text = ASL_TEMPLATE.read_text(encoding="utf-8")
    text = text.replace(
        "${sweep_control_arn}",
        "arn:aws:lambda:us-east-1:123456789012:function:crm-agent-eval-sweep-control",
    ).replace(
        "${sweep_task_arn}",
        "arn:aws:lambda:us-east-1:123456789012:function:crm-agent-eval-sweep-task",
    )
    parsed: dict[str, Any] = json.loads(text)
    return parsed


def test_sweep_functions_resolve_from_the_real_asl_template() -> None:
    found = mod.sweep_function_arns(_rendered_definition())
    assert set(found) == {"sweep-control", "sweep-task"}
    assert found["sweep-task"].endswith(":function:crm-agent-eval-sweep-task")


#: The non-DynamoDB grants a correctly deployed stack holds, by (role, action). The
#: DynamoDB decisions come from `infra/eval_sfn.tf`'s grant locals (:class:`GrantEvaluator`).
_GRANTED: frozenset[tuple[str, str]] = frozenset(
    {
        ("control-role", "s3:PutObject@inside"),
        ("task-role", "bedrock-agentcore:InvokeAgentRuntime"),
        ("machine-role", "lambda:InvokeFunction@sweep"),
        *(("machine-role", action) for action in mod.SFN_XRAY_TRACING_ACTIONS),
        ("trigger-role", "states:StartExecution"),
        ("trigger-role", "states:DescribeExecution"),
        ("trigger-role", "s3:GetObject@inside"),
    }
)


def _missing_keys(request: Mapping[str, Any]) -> list[str]:
    """What IAM reports for a DynamoDB simulation sent without a `dynamodb:LeadingKeys` entry."""
    unkeyed = "ContextEntries" not in request
    return (
        ["dynamodb:LeadingKeys"]
        if unkeyed and request["ActionNames"][0].startswith("dynamodb:")
        else []
    )


_ROLE_FUNCTIONS: dict[str, str] = {"control-role": "sweep-control", "task-role": "sweep-task"}


def _parse_dynamodb_arn(resource: str) -> tuple[str, str | None]:
    """`(logical table, index or None)` from a `table/crm-agent-eval-<t>[/index/<i>]` ARN."""
    tail = resource.split(":table/", 1)[1]
    physical, _, index = tail.partition("/index/")
    return physical.removeprefix("crm-agent-eval-"), index or None


class GrantEvaluator:
    """Evaluates a DynamoDB request against grant objects as IAM would (ForAllValues conditions).

    An `extra_conditions` variable absent from the context fails its grant, and is reported missing
    when the grant's LeadingKeys condition held (measured with `simulate-custom-policy`).
    """

    def __init__(self, grants: Mapping[str, Sequence[LocalGrant]]) -> None:
        self.grants = grants
        self.missing: list[str] = []

    def allowed(
        self,
        role: str,
        action: str,
        resource: str,
        keys: Sequence[str],
        context: Mapping[str, str] | None = None,
    ) -> bool:
        table, index = _parse_dynamodb_arn(resource)
        context = context or {}
        self.missing = []
        for grant in self.grants.get(role, ()):
            covers = index in grant.index_names if index else grant.include_table_arn
            if grant.table != table or action not in grant.actions or not covers:
                continue
            if grant.key_values is not None and grant.operator is not None:
                operator = grant.operator
                if not all(
                    any(condition_matches(v, k, operator) for v in grant.key_values) for k in keys
                ):
                    continue
            unset = [c.variable for c in grant.extra_conditions if c.variable not in context]
            self.missing += unset
            if unset or any(context[c.variable] not in c.values for c in grant.extra_conditions):
                continue
            return True
        return False

    def condition_variables(self, role: str) -> list[str]:
        """Every `extra_conditions` variable on the role's grants: IAM reports each one unset from
        a DynamoDB simulation it implicitly denies (measured live on the deployed role)."""
        return sorted({c.variable for g in self.grants.get(role, ()) for c in g.extra_conditions})

    def holds(self, role: str, action: str, resource: str) -> bool:
        """Whether any grant names `action` on the resource's table: else the deny statement covers it."""
        table, _index = _parse_dynamodb_arn(resource)
        return any(g.table == table and action in g.actions for g in self.grants.get(role, ()))


def _real_grants() -> dict[str, list[LocalGrant]]:
    return {
        role: terraform_local_grants(SWEEP_GRANT_LOCALS[function])
        for role, function in _ROLE_FUNCTIONS.items()
    }


class FakeIam:
    """Decides from `_GRANTED` and the grant locals; `extra`/`grants`/`missing` mutate the deployed
    policy, and an action in `raising` answers with a `ClientError`."""

    def __init__(
        self,
        extra: frozenset[tuple[str, str]] = frozenset(),
        *,
        grants: Mapping[str, Sequence[LocalGrant]] | None = None,
        flip: Callable[[str, str, str, tuple[str, ...]], bool] | None = None,
        deny_statement: bool = True,
        missing: frozenset[tuple[str, str]] = frozenset(),
        raising: frozenset[str] = frozenset(),
    ) -> None:
        self.granted = (_GRANTED | extra) - missing
        self.deny_statement = deny_statement
        self.evaluator = GrantEvaluator(_real_grants() if grants is None else grants)
        self.flip = flip
        self.raising = raising
        self.requests: list[dict[str, Any]] = []

    def simulate_principal_policy(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        role = request["PolicySourceArn"]
        (action,) = request["ActionNames"]
        (resource,) = request["ResourceArns"]
        if action in self.raising:
            raise ClientError(
                {"Error": {"Code": "InvalidInput", "Message": "simulated"}},
                "SimulatePrincipalPolicy",
            )
        entries = request.get("ContextEntries", [])
        keys: list[str] = next(
            (
                e["ContextKeyValues"]
                for e in entries
                if e["ContextKeyName"] == "dynamodb:LeadingKeys"
            ),
            [],
        )
        context = {
            e["ContextKeyName"]: e["ContextKeyValues"][0]
            for e in entries
            if e["ContextKeyName"] != "dynamodb:LeadingKeys"
        }
        qualified = action
        if action.startswith("dynamodb:") and ":table/" in resource:
            granted = (role, action) in self.granted
            ok = granted or self.evaluator.allowed(role, action, resource, keys, context)
            missing = [] if granted else list(self.evaluator.missing)
            if self.flip is not None and self.flip(role, action, resource, tuple(keys)):
                ok = not ok
            explicit = self.deny_statement and not self.evaluator.holds(role, action, resource)
            if not ok and not explicit:
                missing += [
                    v
                    for v in self.evaluator.condition_variables(role)
                    if v not in context and v not in missing
                ]
            decision = "allowed" if ok else ("explicitDeny" if explicit else "implicitDeny")
            return {
                "EvaluationResults": [
                    {
                        "EvalDecision": decision,
                        "MissingContextValues": _missing_keys(request) + missing,
                    }
                ]
            }
        if action in ("s3:PutObject", "s3:GetObject"):
            qualified = f"{action}@inside" if "/eval-artifacts/" in resource else action
        elif action == "lambda:InvokeFunction":
            qualified = f"{action}@sweep" if "sweep-" in resource else action
        decision = "allowed" if (role, qualified) in self.granted else "implicitDeny"
        return {
            "EvaluationResults": [
                {"EvalDecision": decision, "MissingContextValues": _missing_keys(request)}
            ]
        }


class FakeLambda:
    def get_function_configuration(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        if FunctionName.endswith("sweep-control"):
            return {"Role": "control-role"}
        if FunctionName.endswith("sweep-task"):
            return {"Role": "task-role"}
        return {"Role": "trigger-role", "FunctionArn": f"arn:aws:lambda:::function:{FunctionName}"}


class FakeSfnDescribe(FakeSfn):
    def __init__(self, *, tracing: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.tracing = {"enabled": True} if tracing is None else tracing

    def describe_state_machine(self, *, stateMachineArn: str) -> dict[str, Any]:  # noqa: N803
        described: dict[str, Any] = {
            "definition": json.dumps(_rendered_definition()),
            "roleArn": "machine-role",
        }
        if self.tracing:
            described["tracingConfiguration"] = self.tracing
        return described


class FakeSts:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": "123456789012"}


def _simulate(iam: FakeIam, *, sfn: FakeSfnDescribe | None = None) -> dict[str, CheckResult]:
    ctx = context_for_test(
        outputs=_OUTPUTS,
        clients={
            "stepfunctions": sfn or FakeSfnDescribe(),
            "lambda": FakeLambda(),
            "sts": FakeSts(),
            "iam": iam,
        },
    )
    return {r.check: r for r in mod.role_simulations(ctx)}


def test_every_design_9_row_passes_against_correct_grants() -> None:
    rows = _simulate(FakeIam())
    assert len(rows) == mod.simulation_row_count()
    assert {c: r.detail for c, r in rows.items() if r.status != "pass"} == {}


@pytest.mark.parametrize(
    ("extra", "reddened"),
    [
        (("control-role", "bedrock-agentcore:InvokeAgentRuntime"), "control_denies_invoke_runtime"),
        (("trigger-role", "states:StopExecution"), "trigger_denies_stop_execution"),
        (("machine-role", "lambda:InvokeFunction"), "machine_denies_invoke_trigger"),
    ],
)
def test_a_surplus_grant_reds_exactly_its_row(extra: tuple[str, str], reddened: str) -> None:
    rows = _simulate(FakeIam(frozenset({extra})))
    assert {c for c, r in rows.items() if r.status != "pass"} == {reddened}


def test_expected_check_count_is_derived_from_the_simulation_rows() -> None:
    expected = mod.expected_check_count(context_for_test())
    assert expected.minimum == mod.simulation_row_count() + 1


# -- state machine tracing and the SFN-XRAY-1 grant ----------------------------------------------

_XRAY_ALLOW_ROWS: dict[str, str] = {
    f"machine_allows_{action.partition(':')[2]}": action for action in mod.SFN_XRAY_TRACING_ACTIONS
}
_XRAY_DENY_ROWS: dict[str, str] = {
    f"machine_denies_{action.partition(':')[2]}": action for action in mod.SFN_XRAY_DENIED_ACTIONS
}


def _failing(rows: Mapping[str, CheckResult]) -> set[str]:
    return {c for c, r in rows.items() if r.status != "pass"}


def test_the_tracing_row_and_every_xray_row_pass_on_the_machine_role_from_describe() -> None:
    iam = FakeIam()
    rows = _simulate(iam)
    assert rows["machine_tracing_enabled"].status == "pass"
    for check in (*_XRAY_ALLOW_ROWS, *_XRAY_DENY_ROWS):
        assert rows[check].status == "pass", rows[check].detail
    xray = [r for r in iam.requests if r["ActionNames"][0].startswith("xray:")]
    assert {r["ActionNames"][0] for r in xray} == {
        *mod.SFN_XRAY_TRACING_ACTIONS,
        *mod.SFN_XRAY_DENIED_ACTIONS,
    }
    assert {(r["PolicySourceArn"], tuple(r["ResourceArns"])) for r in xray} == {
        ("machine-role", ("*",))
    }


def test_the_positive_control_invokes_sweep_control_as_the_machine_role() -> None:
    iam = FakeIam()
    rows = _simulate(iam)
    control_arn = "arn:aws:lambda:us-east-1:123456789012:function:crm-agent-eval-sweep-control"
    [control] = [
        r
        for r in iam.requests
        if r["PolicySourceArn"] == "machine-role"
        and r["ActionNames"] == ["lambda:InvokeFunction"]
        and r["ResourceArns"][0].endswith(":function:crm-agent-eval-sweep-control")
    ]
    assert control == {
        "PolicySourceArn": "machine-role",
        "ActionNames": ["lambda:InvokeFunction"],
        "ResourceArns": [control_arn],
    }
    row = rows["machine_allows_invoke_control"]
    assert (row.status, row.detail) == (
        "pass",
        f"lambda:InvokeFunction on {control_arn}: allowed (expected allowed) for machine-role",
    )


@pytest.mark.parametrize("tracing", [{"enabled": False}, {}], ids=["disabled", "absent"])
def test_tracing_not_enabled_reds_exactly_the_tracing_row(tracing: dict[str, Any]) -> None:
    rows = _simulate(FakeIam(), sfn=FakeSfnDescribe(tracing=tracing))
    assert _failing(rows) == {"machine_tracing_enabled"}


@pytest.mark.parametrize(("check", "action"), sorted(_XRAY_ALLOW_ROWS.items()))
def test_a_denied_xray_tracing_action_reds_exactly_its_row(check: str, action: str) -> None:
    rows = _simulate(FakeIam(missing=frozenset({("machine-role", action)})))
    assert _failing(rows) == {check}


@pytest.mark.parametrize(("check", "action"), sorted(_XRAY_DENY_ROWS.items()))
def test_a_non_granted_xray_action_allowed_reds_exactly_its_row(check: str, action: str) -> None:
    rows = _simulate(FakeIam(frozenset({("machine-role", action)})))
    assert _failing(rows) == {check}


def test_a_simulate_client_error_is_a_failing_row_never_a_skip() -> None:
    raising = frozenset({*mod.SFN_XRAY_TRACING_ACTIONS, *mod.SFN_XRAY_DENIED_ACTIONS})
    rows = _simulate(FakeIam(raising=raising))
    xray_rows = {*_XRAY_ALLOW_ROWS, *_XRAY_DENY_ROWS}
    assert _failing(rows) == xray_rows
    for check in xray_rows:
        assert rows[check].status == "fail"
        assert "ClientError" in rows[check].detail, rows[check].detail


def test_the_role_simulations_run_before_any_sweep_is_submitted() -> None:
    api = FakeEvalApi()
    seen: list[int] = []

    def simulations(_ctx: SmokeContext) -> Iterator[CheckResult]:
        seen.append(len(api.calls))
        yield CheckResult(resource="eval_sfn", check="machine_tracing_enabled", status="pass")

    clock = Clock()
    list(
        mod.checks(
            _ctx(),
            provisioner=FakeProvisioner(),
            post=api,
            store=FakeStore(api),
            simulations=simulations,
            clock=clock,
            sleeper=clock.sleep,
        )
    )
    assert seen == [0] and _submits(api)


def test_the_xray_actions_match_the_sfn_role_grant_in_infra() -> None:
    """Contract with `infra/eval_sfn.tf`: `SfnXRayTracing` grants `local.sfn_xray_tracing_actions`,
    and that list is exactly the actions the smoke expects allowed."""
    text = EVAL_SFN_TF.read_text(encoding="utf-8")
    statement = text.split('sid       = "SfnXRayTracing"', 1)
    assert len(statement) == 2, "no SfnXRayTracing statement in infra/eval_sfn.tf"
    assert "actions   = local.sfn_xray_tracing_actions" in statement[1].split("}", 1)[0]
    body = text.split("sfn_xray_tracing_actions = [", 1)[1].split("]", 1)[0]
    assert tuple(sorted(re.findall(r'"([^"]+)"', body))) == tuple(
        sorted(mod.SFN_XRAY_TRACING_ACTIONS)
    )
    assert not set(mod.SFN_XRAY_DENIED_ACTIONS) & set(mod.SFN_XRAY_TRACING_ACTIONS)


# -- live once -----------------------------------------------------------------------------------

_MODE = "DEFAULT"


def _live(
    api: FakeEvalApi,
    runner: Callable[[Sequence[str]], tuple[int, str, str]],
    *,
    verdict: str | None = _MODE,
) -> dict[str, CheckResult]:
    clock = Clock()
    results = list(
        mod.live_once_checks(
            _ctx(),
            provisioner=FakeProvisioner(),
            post=api,
            store=FakeStore(api),
            runner=runner,
            retention_verdict=verdict,
            clock=clock,
            sleeper=clock.sleep,
        )
    )
    return {r.check: r for r in results}


def _retention(mode_json: str) -> Callable[[Sequence[str]], tuple[int, str, str]]:
    def runner(argv: Sequence[str]) -> tuple[int, str, str]:
        assert argv[:3] == ["aws", "bedrock", "get-account-data-retention"]
        assert "--profile" in argv and "--region" in argv
        return 0, mode_json, ""

    return runner


_retention_ok = _retention(json.dumps({"dataRetentionMode": _MODE}))


def _submits(api: FakeEvalApi) -> list[dict[str, Any]]:
    return [c for c in api.calls if c["action"] == "submit"]


def test_live_once_with_a_matching_verdict_submits_exactly_one_confirmed_live_sweep() -> None:
    api = FakeEvalApi(step=_TOTAL)
    by_check = _live(api, _retention_ok)
    assert _submits(api) == [{"action": "submit", "mode": "live", "confirmed": True}]
    assert by_check["data_retention_read"].status == "pass"
    assert by_check["retention_verdict_matches"].status == "pass"
    assert {c: r.detail for c, r in by_check.items() if r.status != "pass"} == {}
    assert by_check["live_cost_recorded"].status == "pass"


@pytest.mark.parametrize("verdict", [None, "SOMETHING_ELSE", ""])
def test_live_once_without_a_matching_verdict_submits_nothing(verdict: str | None) -> None:
    api = FakeEvalApi(step=_TOTAL)
    by_check = _live(api, _retention_ok, verdict=verdict)
    assert by_check["retention_verdict_matches"].status == "fail"
    assert by_check["live_sweep_submitted"].status == "fail"
    assert _submits(api) == [] and api.calls == []
    if verdict is None:
        assert (
            f"{mod.RETENTION_VERDICT_FLAG}={_MODE}" in by_check["retention_verdict_matches"].detail
        )


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        json.dumps({"retention": {}}),
        json.dumps({"dataRetentionMode": "A", "other": {"mode": "B"}}),
    ],
)
def test_an_unreadable_mode_fails_even_against_any_verdict(response: str) -> None:
    api = FakeEvalApi()
    by_check = _live(api, _retention(response), verdict="A")
    assert by_check["data_retention_read"].status == "fail"
    assert by_check["retention_verdict_matches"].status == "fail"
    assert api.calls == []


def test_live_once_submits_nothing_when_the_read_fails() -> None:
    api = FakeEvalApi()
    by_check = _live(api, lambda _argv: (254, "", "Invalid choice"))
    assert by_check["data_retention_read"].status == "fail"
    assert by_check["live_sweep_submitted"].status == "fail"
    assert api.calls == []


def test_the_mode_is_found_at_any_depth() -> None:
    assert mod.retention_mode(json.dumps({"a": {"retentionMode": "X"}})) == "X"
    assert mod.retention_mode(json.dumps({"mode": "X", "b": [{"Mode": "X"}]})) == "X"


def test_live_once_reports_a_failed_run_as_failed() -> None:
    by_check = _live(FakeEvalApi(step=_TOTAL, final_status="failed"), _retention_ok)
    assert by_check["run_completed_all_tasks"].status == "fail"


def test_live_once_without_the_billable_flag_exits_2_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: Any, **__: Any) -> int:
        raise AssertionError("run_main must not be reached")

    monkeypatch.setattr(mod, "run_main", forbidden)
    assert mod.main([mod.LIVE_ONCE_FLAG]) == 2
    assert mod.main([mod.BILLABLE_FLAG]) == 2
    assert mod.main([f"{mod.RETENTION_VERDICT_FLAG}={_MODE}"]) == 2


@pytest.mark.parametrize(
    ("argv", "verdict"),
    [
        ([f"{mod.RETENTION_VERDICT_FLAG}={_MODE}"], _MODE),
        ([mod.RETENTION_VERDICT_FLAG, _MODE], _MODE),
        ([], None),
    ],
)
def test_live_once_routes_the_verdict_to_the_live_checks(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], verdict: str | None
) -> None:
    seen: dict[str, Any] = {}

    def capture(_argv: Sequence[str], **kwargs: Any) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(mod, "run_main", capture)
    assert mod.main([mod.LIVE_ONCE_FLAG, mod.BILLABLE_FLAG, *argv]) == 0
    assert seen["checks"].func is mod.live_once_checks
    assert seen["checks"].keywords == {"retention_verdict": verdict}
    assert mod.RETENTION_VERDICT_FLAG in seen["extra_arguments"]
    assert mod.main([]) == 0 and seen["checks"] is mod.checks


def test_a_verdict_mismatch_exits_non_zero_through_run_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.smoke.harness as harness

    api = FakeEvalApi()
    outputs = tmp_path / "tf-outputs.json"
    outputs.write_text(json.dumps({k: {"value": v} for k, v in _OUTPUTS.items()}))
    monkeypatch.setattr(mod, "_run_cli", _retention_ok)
    monkeypatch.setattr(mod, "live_api_post", lambda _ctx: api)
    monkeypatch.setattr(mod, "live_store", lambda _ctx: FakeStore(api))
    monkeypatch.setattr(harness, "assert_target_matches_outputs", lambda _ctx: ())
    rc = mod.main(
        [
            mod.LIVE_ONCE_FLAG,
            mod.BILLABLE_FLAG,
            f"{mod.RETENTION_VERDICT_FLAG}=WRONG",
            "--outputs",
            str(outputs),
            "--profile",
            "default",
            "--region",
            "us-east-1",
            "--json",
        ]
    )
    rows = {c["check"]: c["status"] for c in json.loads(capsys.readouterr().out)["checks"]}
    assert rc != 0 and api.calls == []
    assert rows["data_retention_read"] == "pass"
    assert rows["retention_verdict_matches"] == "fail"
    assert rows["live_sweep_submitted"] == "fail"


# -- A-1 / A-3 -----------------------------------------------------------------------------------


def test_a_settle_step_that_raises_becomes_a_row_and_the_user_is_still_removed() -> None:
    api = FakeEvalApi(stuck=True)
    provisioner = FakeProvisioner()
    store = FakeStore(api)
    clock = Clock()
    settling = False

    def post(route: str, token: str | None, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if settling and body["action"] == "status":
            raise OSError("connection reset")
        return api(route, token, body)

    by_check: dict[str, CheckResult] = {}
    for row in mod.checks(
        _ctx(),
        provisioner=provisioner,
        post=post,
        store=store,
        simulations=lambda _ctx: iter(()),
        clock=clock,
        sleeper=clock.sleep,
    ):
        by_check[row.check] = row
        if row.check == "cancel_proof_submitted":
            settling = True
    assert by_check["run_settled"].status == "fail"
    assert "OSError" in by_check["run_settled"].detail
    assert provisioner.cleaned and by_check["cognito_user_deleted"].status == "pass"


def test_a_raising_reap_count_becomes_a_row(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_store: Any, _tenant: str) -> dict[str, int]:
        raise RuntimeError("throttled")

    monkeypatch.setattr(mod, "tenant_row_counts", boom)
    rows = list(mod._reap_tenant(object(), "eval-run0001"))
    assert [r.check for r in rows] == ["leftover_tenant_counted"] and rows[0].status == "fail"


def test_poll_run_retries_transient_errors_up_to_its_bound() -> None:
    api = FakeEvalApi()
    api.runs["r"] = {"run_id": "r", "is_terminal": True, "cancel_requested": False}
    failures = {"left": mod.POLL_MAX_CONSECUTIVE_ERRORS - 1}

    def flaky(route: str, token: str | None, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if failures["left"]:
            failures["left"] -= 1
            raise TimeoutError("read timed out")
        return api(route, token, body)

    clock = Clock()
    run = mod.poll_run(
        mod.RunApi(flaky, "id-token"),
        "r",
        until=mod.is_terminal,
        seconds=600,
        clock=clock,
        sleeper=clock.sleep,
    )
    assert run["is_terminal"] is True

    def always(
        route: str, token: str | None, body: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        raise OSError("down")

    with pytest.raises(OSError):
        mod.poll_run(
            mod.RunApi(always, "id-token"),
            "r",
            until=mod.is_terminal,
            seconds=600,
            clock=clock,
            sleeper=clock.sleep,
        )


# -------------------------------------------------------------------------------------------------


def _answering(
    statuses: list[int], api: FakeEvalApi
) -> Callable[[str, str | None, Mapping[str, Any]], tuple[int, dict[str, Any]]]:
    """A post whose `status` calls answer `statuses` in order, then defer to `api`."""

    def post(route: str, token: str | None, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if body["action"] == "status" and statuses:
            api.calls.append(dict(body))
            code = statuses.pop(0)
            return code, {"error": {"code": f"http_{code}", "message": "refused"}}
        return api(route, token, body)

    return post


@pytest.mark.parametrize("code", [401, 403, 500, 502])
def test_a_non_200_settle_status_fails_run_settled_and_skips_the_reap(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    reaped: list[str] = []

    def reap(_store: Any, tenant: str) -> Iterator[CheckResult]:
        reaped.append(tenant)
        yield from ()

    monkeypatch.setattr(mod, "_reap_tenant", reap)
    api = FakeEvalApi(stuck=True)
    api.runs["r"] = {"run_id": "r", "is_terminal": False, "cancel_requested": False}
    rows = list(
        mod._settle_runs(
            mod.RunApi(_answering([code], api), "id-token"), FakeStore(api), {"r": "eval-r"}
        )
    )
    assert [(r.check, r.status) for r in rows] == [("run_settled", "fail")]
    assert f"HTTP {code}" in rows[0].detail and "not reaped" in rows[0].detail
    assert reaped == []
    assert [c["action"] for c in api.calls] == ["status"]


def test_a_200_settle_status_still_reaps_the_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    reaped: list[str] = []

    def reap(_store: Any, tenant: str) -> Iterator[CheckResult]:
        reaped.append(tenant)
        yield from ()

    monkeypatch.setattr(mod, "_reap_tenant", reap)
    api = FakeEvalApi()
    api.runs["r"] = {"run_id": "r", "is_terminal": True, "cancel_requested": False}
    assert (
        list(mod._settle_runs(mod.RunApi(api, "id-token"), FakeStore(api), {"r": "eval-r"})) == []
    )
    assert reaped == ["eval-r"]


@pytest.mark.parametrize("code", [401, 403])
def test_poll_run_raises_at_once_when_status_refuses_the_admin(code: int) -> None:
    api = FakeEvalApi()
    api.runs["r"] = {"run_id": "r", "is_terminal": True, "cancel_requested": False}
    clock = Clock()
    with pytest.raises(mod.RunStatusError, match=f"HTTP {code}"):
        mod.poll_run(
            mod.RunApi(_answering([code], api), "id-token"),
            "r",
            until=mod.is_terminal,
            seconds=600,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert len(api.calls) == 1 and clock.now == 0


def test_poll_run_counts_non_200_answers_toward_its_error_bound() -> None:
    api = FakeEvalApi()
    api.runs["r"] = {"run_id": "r", "is_terminal": True, "cancel_requested": False}
    clock = Clock()
    run = mod.poll_run(
        mod.RunApi(_answering([500] * (mod.POLL_MAX_CONSECUTIVE_ERRORS - 1), api), "id-token"),
        "r",
        until=mod.is_terminal,
        seconds=600,
        clock=clock,
        sleeper=clock.sleep,
    )
    assert run["is_terminal"] is True

    api.calls.clear()
    with pytest.raises(mod.RunStatusError, match="HTTP 502"):
        mod.poll_run(
            mod.RunApi(_answering([502] * 1000, api), "id-token"),
            "r",
            until=mod.is_terminal,
            seconds=1_000_000,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert len(api.calls) == mod.POLL_MAX_CONSECUTIVE_ERRORS


def test_poll_run_resets_its_error_count_on_a_200() -> None:
    api = FakeEvalApi(stuck=True)
    api.runs["r"] = {"run_id": "r", "is_terminal": False, "cancel_requested": False}
    below = mod.POLL_MAX_CONSECUTIVE_ERRORS - 1
    answers = [500] * below + [200] + [500] * below
    clock = Clock()

    def post(route: str, token: str | None, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if body["action"] == "status" and answers:
            code = answers.pop(0)
            if code != 200:
                return code, {}
        return api(route, token, body)

    last = mod.poll_run(
        mod.RunApi(post, "id-token"),
        "r",
        until=mod.is_terminal,
        seconds=(2 * below + 3) * mod.POLL_INTERVAL_SECONDS,
        clock=clock,
        sleeper=clock.sleep,
    )
    assert last["is_terminal"] is False and answers == []


def test_an_empty_demo_baseline_fails_rather_than_proving_untouched() -> None:
    empty = {"accounts": 0, "contacts": 0}
    row = mod.check_demo_untouched(empty, dict(empty))
    assert row.status == "fail" and "blind" in row.detail
    assert mod.check_demo_untouched({"accounts": 3}, {"accounts": 3}).status == "pass"


def test_every_simulated_dynamodb_row_sends_leading_keys() -> None:
    rows = _simulate(FakeIam())
    assert rows["task_denies_tenant_write"].status == "pass"
    assert "MissingContextValues" not in rows["task_denies_tenant_write"].detail


@pytest.mark.parametrize("live", [False, True])
def test_the_user_is_removed_even_when_settling_raises(
    monkeypatch: pytest.MonkeyPatch, live: bool
) -> None:
    def broken_settle(*_: Any) -> Iterator[CheckResult]:
        yield from ()
        raise RuntimeError("settle crashed")

    monkeypatch.setattr(mod, "_settle_runs", broken_settle)
    api = FakeEvalApi(step=_TOTAL)
    provisioner = FakeProvisioner()
    clock = Clock()
    rows: list[CheckResult] = []
    if live:
        flow = mod.live_once_checks(
            _ctx(),
            provisioner=provisioner,
            post=api,
            store=FakeStore(api),
            runner=_retention_ok,
            retention_verdict=_MODE,
            clock=clock,
            sleeper=clock.sleep,
        )
    else:
        flow = mod.checks(
            _ctx(),
            provisioner=provisioner,
            post=api,
            store=FakeStore(api),
            simulations=lambda _ctx: iter(()),
            clock=clock,
            sleeper=clock.sleep,
        )
    with pytest.raises(RuntimeError, match="settle crashed"):
        for row in flow:
            rows.append(row)
    assert provisioner.cleaned
    assert rows[-1].check == "cognito_user_deleted"


# -- narrowed sweep grants ---------------------------------------------


def _sweep_rows() -> tuple[SimulationRow, ...]:
    return mod.sweep_grant_rows(
        control_role="control-role", task_role="task-role", table_arn=_physical_table_arn
    )


def _physical_table_arn(table: str, index: str | None) -> str:
    return f"arn:aws:dynamodb:us-east-1:123456789012:table/crm-agent-eval-{table}" + (
        f"/index/{index}" if index else ""
    )


@pytest.mark.parametrize(
    ("extra", "reddened"),
    [
        (("control-role", "dynamodb:DeleteItem"), "control_denies_job_delete"),
        (("task-role", "dynamodb:PutItem"), "task_denies_tenant_write"),
    ],
)
def test_a_blanket_dynamodb_surplus_reds_its_design_row_and_the_sweep_denies(
    extra: tuple[str, str], reddened: str
) -> None:
    rows = _simulate(FakeIam(frozenset({extra})))
    failed = {c for c, r in rows.items() if r.status != "pass"}
    assert reddened in failed
    action = extra[1].partition(":")[2]
    assert all(f"_{action}@" in c or c == reddened for c in failed), sorted(failed)


def test_every_sweep_row_is_simulated_and_passes_against_the_real_grants() -> None:
    rows = _simulate(FakeIam())
    expected = {r.check for r in _sweep_rows()}
    assert expected <= set(rows)
    assert {c: rows[c].detail for c in expected if rows[c].status != "pass"} == {}


@pytest.mark.parametrize("row", _sweep_rows(), ids=lambda r: r.check)
def test_each_sweep_row_fails_without_its_subject(row: SimulationRow) -> None:
    """The deployed decision for exactly this row's request flips; this row reds, and only it."""
    (key,) = row.leading_keys or ("",)

    def flip(role: str, action: str, resource: str, keys: tuple[str, ...]) -> bool:
        return (role, action, resource, keys) == (
            row.role_arn,
            row.action,
            row.resource_arn,
            (key,),
        )

    rows = _simulate(FakeIam(flip=flip))
    failed = {c for c, r in rows.items() if r.status != "pass"}
    assert row.check in failed
    same_request = {
        r.check
        for r in _sweep_rows()
        if (r.role_arn, r.action, r.resource_arn, r.leading_keys)
        == (row.role_arn, row.action, row.resource_arn, row.leading_keys)
    }
    # The rows that send an identical request.
    assert failed <= same_request | {
        "control_allows_job_get",
        "control_allows_lock_delete",
        "task_allows_job_update",
    }


def test_every_sweep_dynamodb_row_carries_leading_keys() -> None:
    assert all(r.leading_keys for r in _sweep_rows())


def test_a_dynamodb_row_without_context_is_refused_by_the_module() -> None:
    bare = SimulationRow("bare", "control-role", "dynamodb:GetItem", "arn:t", True, None)
    with pytest.raises(ValueError, match="without dynamodb:LeadingKeys"):
        mod.require_leading_keys([bare])
    keyed = SimulationRow("keyed", "control-role", "dynamodb:GetItem", "arn:t", True, ("k",))
    s3 = SimulationRow("s3", "control-role", "s3:PutObject", "arn:b", True, None)
    assert mod.require_leading_keys([keyed, s3]) == (keyed, s3)


def test_an_unkeyed_allow_fails_the_strict_missing_context_rule() -> None:
    """A row sent with no key is a fail even when IAM answers `allowed` (ForAllValues vacuity)."""
    row = next(r for r in _sweep_rows() if r.allowed)
    unkeyed = SimulationRow(row.check, row.role_arn, row.action, row.resource_arn, True, None)
    result = simulation_result(
        FakeIam(frozenset({(row.role_arn, row.action)})), unkeyed, resource="eval_sweep_roles"
    )
    assert result.status == "fail" and "MissingContextValues" in result.detail


def test_allow_keys_are_admitted_and_deny_keys_refused_by_their_grants() -> None:
    grants = _real_grants()
    evaluator = GrantEvaluator(grants)
    for row in _sweep_rows():
        context = dict(row.context) if isinstance(row, mod.ContextRow) else {}
        assert (
            evaluator.allowed(
                row.role_arn, row.action, row.resource_arn, row.leading_keys or (), context
            )
            is row.allowed
        ), row.check


def _demo_count_rows() -> list[SimulationRow]:
    return [
        r
        for r in _sweep_rows()
        if r.check.startswith("control_allows_Query@") and "[demo]" in r.check
    ]


def test_each_demo_count_query_allow_row_sends_select_count() -> None:
    """sweep_control's demo Query grants are COUNT-only, so their allow rows carry
    `dynamodb:Select=COUNT`; one row per table the grant local lists."""
    rows = _demo_count_rows()
    tables = {g.table for g in _real_grants()["control-role"] if g.extra_conditions}
    assert tables and {r.check.split("@")[1].split("/")[0] for r in rows} == tables
    for row in rows:
        assert isinstance(row, mod.ContextRow) and row.allowed
        assert row.context == (("dynamodb:Select", "COUNT"),)
        assert row.leading_keys == (DEMO_TENANT,)


def test_a_demo_count_allow_row_passes_with_its_context_and_fails_without_it() -> None:
    iam = FakeIam()
    row = _demo_count_rows()[0]
    assert isinstance(row, mod.ContextRow)
    with_context = mod.context_result(iam, row, resource="eval_sweep_roles")
    assert with_context.status == "pass", with_context.detail
    bare = SimulationRow(
        row.check, row.role_arn, row.action, row.resource_arn, row.allowed, row.leading_keys
    )
    without = simulation_result(iam, bare, resource="eval_sweep_roles")
    assert without.status == "fail" and "dynamodb:Select" in without.detail


def test_a_demo_grant_without_its_count_condition_is_still_allowed_with_it() -> None:
    """A positive control: dropping the COUNT condition keeps the allow row green, so the row
    proves COUNT is admitted, not that only COUNT is; the denials are iam_roles' rows."""
    grants = _real_grants()
    widened = {
        role: [g._replace(extra_conditions=()) for g in role_grants]
        for role, role_grants in grants.items()
    }
    row = _demo_count_rows()[0]
    assert isinstance(row, mod.ContextRow)
    result = mod.context_result(FakeIam(grants=widened), row, resource="eval_sweep_roles")
    assert result.status == "pass"


def _select_of(request: Mapping[str, Any]) -> list[str] | None:
    return next(
        (
            e["ContextKeyValues"]
            for e in request.get("ContextEntries", [])
            if e["ContextKeyName"] == "dynamodb:Select"
        ),
        None,
    )


def test_the_control_role_context_is_select_at_the_value_its_demo_grants_admit() -> None:
    grants = _real_grants()
    assert mod.role_condition_context(grants["control-role"]) == (("dynamodb:Select", "COUNT"),)
    assert mod.role_condition_context(grants["task-role"]) == ()


def test_conditioned_grants_with_no_common_select_value_are_refused() -> None:
    conditioned = [g for g in _real_grants()["control-role"] if g.extra_conditions][:2]
    other = conditioned[1]._replace(
        extra_conditions=(ExtraCondition("StringEquals", "dynamodb:Select", ("ALL_ATTRIBUTES",)),)
    )
    with pytest.raises(ValueError, match="dynamodb:Select"):
        mod.role_condition_context([conditioned[0], other])


def test_every_dynamodb_simulation_of_the_control_role_sends_select_count() -> None:
    """Allow and deny rows alike, design and removed rows included, send the admitted value, so
    a denial cannot be the Select condition's doing; the task role's grants carry no Select."""
    iam = FakeIam()
    rows = _simulate(iam)
    assert {c: r.detail for c, r in rows.items() if r.status != "pass"} == {}
    dynamodb = [r for r in iam.requests if r["ActionNames"][0].startswith("dynamodb:")]
    control = [r for r in dynamodb if r["PolicySourceArn"] == "control-role"]
    assert control
    assert all(_select_of(r) == ["COUNT"] for r in control)
    assert all(_select_of(r) is None for r in dynamodb if r["PolicySourceArn"] == "task-role")


def test_control_deny_rows_without_the_select_context_fail_as_indecisive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deny answered with `dynamodb:Select` missing is never a pass: withholding the role
    context reds the control role's implicit denials, and only those."""
    monkeypatch.setattr(mod, "with_role_context", lambda rows, _contexts: tuple(rows))
    failed = {c: r for c, r in _simulate(FakeIam()).items() if r.status != "pass"}
    assert failed
    assert all(c.startswith("control_denies_") for c in failed), sorted(failed)
    assert all("MissingContextValues=['dynamodb:Select']" in r.detail for r in failed.values())
    assert "control_denies_job_delete" in failed


def test_the_design_and_ruling_removals_each_have_a_deny_row() -> None:
    """Each removed sweep grant is named by the row it must produce."""
    checks = {r.check for r in _sweep_rows()}
    for check in (
        "control_denies_removed_Query@eval_runs/gsi_status",
        "control_denies_removed_GetItem@accounts",
        "control_denies_removed_GetItem@audit_records",
        "control_denies_removed_GetItem@reps",
        "control_denies_removed_Query@eval_results/gsi_task",
        "control_denies_removed_BatchWriteItem@eval_results",
        "task_denies_removed_UpdateItem@eval_results",
        "task_denies_removed_GetItem@audit_records",
        "task_denies_removed_GetItem@usage_records",
        "task_denies_removed_GetItem@reps",
        "task_denies_removed_Query@audit_records/gsi_tenant",
        "task_denies_removed_Query@proposals/gsi_tenant",
        "task_denies_removed_Query@usage_records/gsi_tenant",
        "task_denies_removed_Query@reps/gsi_tenant",
    ):
        assert check in checks, check


def test_each_seed_condition_has_its_allow_and_out_of_scope_denies() -> None:
    checks = {r.check: r for r in _sweep_rows()}
    patterns = {
        "accounts": "acct",
        "contacts": "cont",
        "opportunities": "opp",
        "activities": "act",
        "notes": "note",
    }
    for table, kind in patterns.items():
        allow = checks[f"task_allows_GetItem@{table}[{kind}-eval-*]"]
        assert allow.leading_keys and allow.leading_keys[0].startswith(f"{kind}-eval-")
        assert f"task_denies_GetItem@{table}[{kind}-demo-0000]" in checks
        assert f"task_denies_GetItem@{table}[00000000-0000-4000-8000-000000000000]" in checks
        minted = checks[f"task_denies_GetItem@{table}[{kind}-00000000-0000-4000-8000-000000000000]"]
        assert minted.leading_keys == (f"{kind}-00000000-0000-4000-8000-000000000000",)
        assert not minted.allowed


def test_the_removed_seed_put_item_is_an_explicit_deny_row_on_each_seed_table() -> None:
    """Seeding writes through BatchWriteItem only, so PutItem on each seed table is a removed
    grant whose row requires explicitDeny."""
    checks = {r.check: r for r in _sweep_rows()}
    for table in ("accounts", "contacts", "opportunities", "activities", "notes"):
        row = checks[f"control_denies_removed_PutItem@{table}"]
        assert isinstance(row, mod.ExplicitDenyRow) and not row.allowed
        assert f"control_allows_PutItem@{table}[" not in " ".join(checks)


def test_sweep_task_is_denied_the_demo_tenant_on_every_tenant_index_query() -> None:
    checks = {r.check: r for r in _sweep_rows()}
    for table in ("accounts", "contacts", "opportunities", "activities", "notes"):
        row = checks[f"task_denies_Query@{table}/gsi_tenant[{DEMO_TENANT}]"]
        assert row.leading_keys == (DEMO_TENANT,) and not row.allowed


def test_a_condition_widened_in_the_deployed_policy_reds_its_deny_rows() -> None:
    grants = _real_grants()
    widened = {
        role: [
            g._replace(key_values=None, operator=None)
            if g.table == "accounts" and "dynamodb:GetItem" in g.actions
            else g
            for g in role_grants
        ]
        for role, role_grants in grants.items()
    }
    rows = _simulate(FakeIam(grants=widened))
    failed = {c for c, r in rows.items() if r.status != "pass"}
    assert failed == {
        "task_denies_GetItem@accounts[00000000-0000-4000-8000-000000000000]",
        "task_denies_GetItem@accounts[acct-demo-0000]",
        "task_denies_GetItem@accounts[acct-00000000-0000-4000-8000-000000000000]",
    }


def test_a_removed_grant_restored_in_the_deployed_policy_reds_its_deny_row() -> None:
    grants = _real_grants()
    restored = {
        **grants,
        "task-role": [
            *grants["task-role"],
            LocalGrant("audit_records", ("dynamodb:GetItem",), (), True, None, None),
        ],
    }
    rows = _simulate(FakeIam(grants=restored))
    assert {c for c, r in rows.items() if r.status != "pass"} == {
        "task_denies_removed_GetItem@audit_records"
    }


def test_the_sweep_rows_follow_the_grant_locals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expectations are read from `eval_sfn.tf`: a changed pattern changes the rows' keys."""
    real = terraform_local_grants

    def renamed(name: str, text: str | None = None) -> list[LocalGrant]:
        return [
            g._replace(key_values=("acct-sweep-*",)) if g.key_values == ("acct-eval-*",) else g
            for g in real(name, text)
        ]

    monkeypatch.setattr(mod, "terraform_local_grants", renamed)
    checks = {r.check: r for r in _sweep_rows()}
    keys = checks["task_allows_GetItem@accounts[acct-sweep-*]"].leading_keys or ("",)
    assert keys[0].startswith("acct-sweep-")
    assert "task_allows_GetItem@accounts[acct-eval-*]" not in checks


def test_a_removed_grant_answered_with_implicit_deny_reds_its_row() -> None:
    """Removed rows require the deny statement; conditioned out-of-scope denies accept either kind."""
    rows = _simulate(FakeIam(deny_statement=False))
    removed = {r.check for r in _sweep_rows() if isinstance(r, mod.ExplicitDenyRow)}
    failed = {c for c, r in rows.items() if r.status != "pass"}
    assert removed and removed <= failed
    conditioned_denies = {
        r.check for r in _sweep_rows() if not r.allowed and not isinstance(r, mod.ExplicitDenyRow)
    }
    assert conditioned_denies and not conditioned_denies & failed
    assert "expected explicitDeny" in rows[sorted(removed)[0]].detail


def test_only_removed_rows_require_explicit_deny() -> None:
    for row in _sweep_rows():
        assert isinstance(row, mod.ExplicitDenyRow) == ("_denies_removed_" in row.check), row.check
        assert not (isinstance(row, mod.ExplicitDenyRow) and row.allowed), row.check


def test_an_explicit_deny_row_with_missing_context_fails() -> None:
    row = next(r for r in _sweep_rows() if isinstance(r, mod.ExplicitDenyRow))
    unkeyed = mod.ExplicitDenyRow(
        row.check, row.role_arn, row.action, row.resource_arn, False, None
    )
    result = mod.explicit_deny_result(FakeIam(), unkeyed, resource="eval_sweep_roles")
    assert result.status == "fail" and "MissingContextValues" in result.detail


def test_a_removed_base_table_query_is_probed_on_the_table_arn() -> None:
    """sweep_control holds no base-table Query on eval_results; its row hits table/."""
    rows = {r.check: r for r in _sweep_rows()}
    row = rows["control_denies_removed_Query@eval_results"]
    assert isinstance(row, mod.ExplicitDenyRow)
    assert row.resource_arn == _physical_table_arn("eval_results", None)
    assert "/index/" not in row.resource_arn


def test_every_removed_grant_has_an_explicit_deny_row() -> None:
    """Each `test_infra_eval_sfn.REMOVED_GRANTS` entry maps to one `ExplicitDenyRow`."""
    from tests.unit.test_infra_eval_sfn import REMOVED_GRANTS

    rows = {r.check: r for r in _sweep_rows()}
    assert REMOVED_GRANTS
    for module_address, table, action, index in REMOVED_GRANTS:
        who = "control" if module_address.endswith("sweep_control") else "task"
        where = table if index is None else f"{table}/{index}"
        check = f"{who}_denies_removed_{action.partition(':')[2]}@{where}"
        assert isinstance(rows.get(check), mod.ExplicitDenyRow), check
        assert rows[check].resource_arn == _physical_table_arn(table, index), check


def test_a_tenant_index_deny_sends_each_protected_tenant_it_does_not_admit() -> None:
    """S2: sweep_task's `eval-*` Query is probed with `demo` and `canary`; sweep_control admits both."""
    checks = {r.check for r in _sweep_rows()}
    for table in ("accounts", "contacts", "opportunities", "activities", "notes"):
        for tenant in (DEMO_TENANT, CANARY_TENANT):
            assert f"task_denies_Query@{table}/gsi_tenant[{tenant}]" in checks
            assert f"control_denies_Query@{table}/gsi_tenant[{tenant}]" not in checks


# -- artefact_url ----------------------------------------------------------------------


def test_artefact_url_rows_pass_and_probe_https_http_and_tampered() -> None:
    fetch = FakeFetch()
    rows = _run(FakeEvalApi(), fetch=fetch)
    for check in (
        "artefact_url_issued",
        "artefact_url_https_get_succeeds",
        "artefact_url_plain_http_denied",
        "artefact_url_tampered_key_denied",
    ):
        assert rows[check].status == "pass", rows[check].detail
    assert [u.split("://", 1)[0] for u in fetch.urls] == ["https", "http", "https"]
    assert ".tampered" in fetch.urls[2] and ".tampered" not in fetch.urls[0]


def test_the_tampered_probe_changes_the_path_when_the_key_is_percent_encoded_in_the_url() -> None:
    """A recorded key with `+` and `:` appears percent-encoded in a presigned URL's path, so the
    probe must alter the URL's path rather than search the URL for the raw key."""
    key = "raw/anthropic.claude-opus-5-5_2026-10-13T04:12:01+00-00.json"

    class EncodingApi(FakeEvalApi):
        def __call__(
            self, route: str, token: str | None, body: Mapping[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            status, payload = super().__call__(route, token, body)
            if body.get("action") == "artefact_url" and status == 200:
                quoted = urllib.parse.quote(str(body["key"]))
                payload["url"] = (
                    f"https://artifacts-bucket.s3.amazonaws.com/eval-artifacts/"
                    f"{payload['run_id']}/{quoted}?X-Amz-Signature=sig"
                )
            return status, payload

        def _finish(self, run: dict[str, Any], status: str) -> None:
            super()._finish(run, status)
            run["artifacts"] = {"raw": [key]}

    fetch = FakeFetch()
    rows = _run(EncodingApi(), fetch=fetch)
    assert "%2B" in fetch.urls[0] and key not in fetch.urls[0]
    assert rows["artefact_url_tampered_key_denied"].status == "pass", rows[
        "artefact_url_tampered_key_denied"
    ].detail
    issued, tampered = urllib.parse.urlsplit(fetch.urls[0]), urllib.parse.urlsplit(fetch.urls[2])
    assert tampered.path == issued.path + ".tampered"
    assert (tampered.scheme, tampered.netloc, tampered.query) == (
        issued.scheme,
        issued.netloc,
        issued.query,
    )


@pytest.mark.parametrize(
    ("fetch", "reddened"),
    [
        (FakeFetch(https_status=403), "artefact_url_https_get_succeeds"),
        (FakeFetch(http_status=200), "artefact_url_plain_http_denied"),
        (FakeFetch(tampered_status=200), "artefact_url_tampered_key_denied"),
        (FakeFetch(http_code="SignatureDoesNotMatch"), "artefact_url_plain_http_denied"),
        (FakeFetch(http_code=None), "artefact_url_plain_http_denied"),
        (FakeFetch(tampered_code="AccessDenied"), "artefact_url_tampered_key_denied"),
        (FakeFetch(tampered_code=None), "artefact_url_tampered_key_denied"),
    ],
)
def test_each_artefact_url_probe_reds_on_its_own_failure(fetch: FakeFetch, reddened: str) -> None:
    rows = _run(FakeEvalApi(), fetch=fetch)
    failing = {c for c, r in rows.items() if r.status != "pass" and c.startswith("artefact_url")}
    assert failing == {reddened}


def test_a_refused_artefact_url_fails_and_fetches_nothing() -> None:
    class RefusingApi(FakeEvalApi):
        def __call__(
            self, route: str, token: str | None, body: Mapping[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            if body["action"] == "artefact_url":
                self.calls.append(dict(body))
                return 403, {"error": {"code": "scope_denied", "message": "no"}}
            return super().__call__(route, token, body)

    fetch = FakeFetch()
    rows = _run(RefusingApi(), fetch=fetch)
    assert rows["artefact_url_issued"].status == "fail"
    assert "artefact_url_https_get_succeeds" not in rows
    assert fetch.urls == []


@pytest.mark.parametrize(
    "check",
    [
        "trigger_denies_get_outside_prefix",
        "trigger_denies_artifact_put",
        "trigger_denies_list_bucket",
    ],
)
def test_a_surplus_trigger_s3_grant_reds_its_row(check: str) -> None:
    extra = {
        "trigger_denies_get_outside_prefix": ("trigger-role", "s3:GetObject"),
        "trigger_denies_artifact_put": ("trigger-role", "s3:PutObject@inside"),
        "trigger_denies_list_bucket": ("trigger-role", "s3:ListBucket"),
    }[check]
    rows = _simulate(FakeIam(frozenset({extra})))
    assert {c for c, r in rows.items() if r.status != "pass"} == {check}


def test_a_missing_trigger_artifact_get_reds_its_row() -> None:
    rows = _simulate(FakeIam(missing=frozenset({("trigger-role", "s3:GetObject@inside")})))
    assert {c for c, r in rows.items() if r.status != "pass"} == {"trigger_allows_artifact_get"}


# -- S3 error codes --------------------------------------------------------------------


def test_the_s3_error_code_is_read_from_the_xml_error_body() -> None:
    body = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>SignatureDoesNotMatch</Code>'
        b"<Message>The request signature we calculated does not match</Message></Error>"
    )
    assert mod.s3_error_code(body) == "SignatureDoesNotMatch"
    assert mod.s3_error_code(b"<html>forbidden</html>") is None
    assert mod.s3_error_code(b"") is None


def test_the_probe_codes_are_the_measured_s3_codes() -> None:
    assert mod.PLAIN_HTTP_ERROR_CODE == "AccessDenied"
    assert mod.TAMPERED_KEY_ERROR_CODE == "SignatureDoesNotMatch"


def test_a_refused_probe_detail_names_the_observed_and_expected_code() -> None:
    rows = _run(FakeEvalApi(), fetch=FakeFetch(tampered_code="AccessDenied"))
    detail = rows["artefact_url_tampered_key_denied"].detail
    assert "code='AccessDenied'" in detail and "code='SignatureDoesNotMatch'" in detail


def test_fetch_status_returns_the_status_and_the_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fetch_status` reads the body's `<Code>` on a refusal and reads no body on a 2xx."""

    class _Response:
        def __init__(self, status: int, body: bytes) -> None:
            self.status = status
            self._body = body
            self.read_calls = 0

        def read(self, _limit: int = -1) -> bytes:
            self.read_calls += 1
            return self._body

    responses: list[_Response] = []

    class _Connection:
        def __init__(self, netloc: str, timeout: int) -> None:
            self.netloc = netloc

        def request(self, method: str, target: str) -> None:
            assert method == "GET"

        def getresponse(self) -> _Response:
            return responses[-1]

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", _Connection)
    monkeypatch.setattr(http.client, "HTTPConnection", _Connection)
    responses.append(_Response(403, b"<Error><Code>AccessDenied</Code></Error>"))
    assert mod.fetch_status("http://bucket.s3.amazonaws.com/k?X-Amz-Signature=s") == (
        403,
        "AccessDenied",
    )
    responses.append(_Response(200, b"<Error><Code>AccessDenied</Code></Error>"))
    assert mod.fetch_status("https://bucket.s3.amazonaws.com/k?X-Amz-Signature=s") == (200, None)
    assert responses[-1].read_calls == 0


# -- tier_comparison_table and the matrix.md download -------------------------------------


_DOWNLOAD_ROWS = (
    "status_carries_tier_comparison_table",
    "matrix_md_download_is_attachment",
    "matrix_md_download_bytes_match_artefact",
)


def _failing_download_rows(rows: Mapping[str, CheckResult]) -> set[str]:
    return {check for check in _DOWNLOAD_ROWS if rows[check].status != "pass"}


def test_the_tier_table_and_download_rows_pass_on_a_correct_run() -> None:
    get = FakeArtefactGet()
    rows = _run(FakeEvalApi(), artefact_get=get)
    assert _failing_download_rows(rows) == set(), {c: rows[c].detail for c in _DOWNLOAD_ROWS}
    assert len(get.urls) == 2
    assert "response-content-disposition" in get.urls[0]
    assert "response-content-disposition" not in get.urls[1]


class _TableApi(FakeEvalApi):
    def __init__(self, table: object) -> None:
        super().__init__()
        self.table = table

    def _finish(self, run: dict[str, Any], status: str) -> None:
        super()._finish(run, status)
        if self.table is _ABSENT:
            run.pop("tier_comparison_table")
        else:
            run["tier_comparison_table"] = self.table


_ABSENT = object()


def _narrow_rows() -> dict[str, Any]:
    table = _tier_table(len(_FAMILIES))
    table["sections"][1]["rows"] = [["Model", "m"]]
    return table


def _reordered() -> dict[str, Any]:
    table = _tier_table(len(_FAMILIES))
    table["sections"].reverse()
    return table


@pytest.mark.parametrize(
    "table",
    [
        _ABSENT,
        "not-a-table",
        _tier_table(len(_FAMILIES) - 1),
        _tier_table(len(_FAMILIES) + 1),
        {**_tier_table(len(_FAMILIES)), "headers": ["Tier", "arm-0", "arm-1"]},
        _narrow_rows(),
        _reordered(),
    ],
    ids=[
        "absent",
        "not-object",
        "one-column-short",
        "one-column-extra",
        "no-metric",
        "narrow-row",
        "reordered",
    ],
)
def test_a_broken_tier_table_reds_exactly_its_row(table: object) -> None:
    rows = _run(_TableApi(table))
    assert _failing_download_rows(rows) == {"status_carries_tier_comparison_table"}


@pytest.mark.parametrize(
    ("get", "reddened"),
    [
        (FakeArtefactGet(disposition=None), {"matrix_md_download_is_attachment"}),
        (FakeArtefactGet(disposition="inline"), {"matrix_md_download_is_attachment"}),
        (FakeArtefactGet(download_body=b"other"), {"matrix_md_download_bytes_match_artefact"}),
        (
            FakeArtefactGet(download_body=b"", plain_body=b""),
            {"matrix_md_download_bytes_match_artefact"},
        ),
        (
            FakeArtefactGet(status=403),
            {"matrix_md_download_is_attachment", "matrix_md_download_bytes_match_artefact"},
        ),
    ],
    ids=["no-disposition", "inline", "different-bytes", "empty-bytes", "download-refused"],
)
def test_a_broken_download_reds_exactly_its_row(get: FakeArtefactGet, reddened: set[str]) -> None:
    rows = _run(FakeEvalApi(), artefact_get=get)
    assert _failing_download_rows(rows) == reddened


def test_the_download_flag_is_sent_and_a_refusal_fetches_nothing() -> None:
    class NoDownloadApi(FakeEvalApi):
        def __call__(
            self, route: str, token: str | None, body: Mapping[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            if body.get("action") == "artefact_url" and body.get("download") is True:
                self.calls.append(dict(body))
                return 400, {"error": {"code": "validation_failed", "message": "download"}}
            return super().__call__(route, token, body)

    get = FakeArtefactGet()
    api = NoDownloadApi()
    rows = _run(api, artefact_get=get)
    assert _failing_download_rows(rows) == {
        "matrix_md_download_is_attachment",
        "matrix_md_download_bytes_match_artefact",
    }
    assert get.urls == []
    assert any(c.get("download") is True and c.get("key") == "matrix.md" for c in api.calls)


def test_a_run_without_a_matrix_md_artefact_fails_both_download_rows() -> None:
    class NoMarkdownApi(FakeEvalApi):
        def _finish(self, run: dict[str, Any], status: str) -> None:
            super()._finish(run, status)
            run["artifacts"].pop("matrix_md")

    rows = _run(NoMarkdownApi())
    assert _failing_download_rows(rows) == {
        "matrix_md_download_is_attachment",
        "matrix_md_download_bytes_match_artefact",
    }


def test_get_artefact_refuses_a_non_https_url() -> None:
    with pytest.raises(ValueError, match="non-https"):
        mod.get_artefact("http://bucket.s3.amazonaws.com/k")
