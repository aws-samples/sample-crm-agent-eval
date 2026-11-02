# SPDX-License-Identifier: MIT
"""`eval.sfn_handlers`: the sweep_control steps and the sweep_task item.

Runs on moto DynamoDB and S3 with the real fixture seeding. The Runtime is the real
`agents.runtime_server` Starlette app in `eval_turn` mode with the real `StubProvider`; its tool
calls reach the real action handlers through `crm.dispatch.invoke_action`, whose Lambda client is
replaced by an in-process one. No model call and no AWS call.
"""

from __future__ import annotations

import contextlib
import io
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from moto import mock_aws
from scripts.smoke.eval_sfn import check_artifacts
from starlette.testclient import TestClient

import agents.runtime_server as runtime_server
import crm.dispatch
from agents.prompt_bundle import prompt_bundle_sha256
from crm.authz import Identity
from crm.errors import (
    Conflict,
    CrmError,
    NotFound,
    ProviderUnavailable,
    TurnTimeout,
    ValidationFailed,
)
from crm.store import keys
from crm.store.provisioning import create_all_tables
from crm.store.schema import TENANT_SCOPED_TABLES
from crm.store.store import Store
from crm.toolspec.definitions import ALL_TOOLS
from eval import sfn_handlers
from eval.harness import DEFAULT_ARM_SPECS, assert_artifacts
from eval.matrix import errored_items_cost_note
from eval.runs import (
    acquire_lock,
    create_run,
    read_lock,
    request_cancel,
    require_run,
    result_id,
    set_execution_arn,
)
from eval.sfn_handlers import (
    BUNDLE_MISMATCH_ATTRIBUTE,
    FINALIZE_RESULT_KEYS,
    START_RESULT_KEYS,
    SWEEP_TASK_RESULT_KEYS,
    TASK_REF_KEYS,
    SweepControlConfig,
    SweepTaskConfig,
    classify_runtime_failure,
    session_id_for,
    sweep_control_handler,
    sweep_task_handler,
    task_score_from_fields,
    tier_scores_from_fields,
    turn_from_row,
)
from eval.tenancy import allocate_run_id, tenant_for_run, unit_tenant, unit_tenants_for_run
from eval.turn_observation import PROMPT_BUNDLE_SHA256_KEY, serialise_turn
from providers.tier_tuning import WIRE_SCHEMA_VERSION

REGION = "us-east-1"
BUCKET = "eval-artifacts-test"
PREFIX = "eval-results"
CONTROL_CONFIG = SweepControlConfig(
    table_prefix="", artifacts_bucket=BUCKET, artifacts_prefix=PREFIX
)
TASK_CONFIG = SweepTaskConfig(
    table_prefix="",
    runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/crm_eval-abc",
    runtime_qualifier="DEFAULT",
)
STARTER = Identity(rep_id="rep-admin", full_visibility=True)
#: The steps whose event names the state machine execution.
OWNED_STEPS = frozenset({"start", "teardown", "finalize"})
OTHER_EXECUTION = "arn:aws:states:us-east-1:123456789012:execution:eval-sweep:second"


# -- harness -------------------------------------------------------------------------------------


class InProcessLambda:
    """A Lambda client whose `invoke` runs the named action handler in-process against `store`."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._handlers = {tool.name: tool.handler for tool in ALL_TOOLS}

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        import importlib

        module_path, _, attribute = self._handlers[kwargs["FunctionName"]].rpartition(".")
        handler = getattr(importlib.import_module(module_path), attribute)
        result = handler(json.loads(kwargs["Payload"]), store=self._store)
        return {"Payload": io.BytesIO(json.dumps(result).encode("utf-8"))}


def runtime_client_error(status: int) -> ClientError:
    """The `ClientError` AgentCore raises for a container non-2xx: a 424 whose fixed message embeds
    the container status and none of the Runtime's body."""
    return ClientError(
        {
            "Error": {
                "Code": "RuntimeClientError",
                "Message": (
                    f"Received error ({status}) from runtime. "
                    "Please check your CloudWatch logs for more information"
                ),
            },
            "ResponseMetadata": {"HTTPStatusCode": 424},
        },
        "InvokeAgentRuntime",
    )


@dataclass
class StarletteRuntime:
    """A `bedrock-agentcore` client that serves `invoke_agent_runtime` from the real Runtime app.

    A non-200 answer is raised as `InvokeAgentRuntime` raises it: a `RuntimeClientError`. Every
    request body is kept in `requests` and every 200 body in `bodies`; `fail_with` replaces the next
    answers with that failure.
    """

    client: TestClient
    requests: list[dict[str, Any]] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)
    fail_with: Callable[[], dict[str, Any]] | None = None

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        request = json.loads(kwargs["payload"])
        self.requests.append({**kwargs, "payload": request})
        if self.fail_with is not None:
            return self.fail_with()
        response = self.client.post("/invocations", json=request)
        if response.status_code != 200:
            raise runtime_client_error(response.status_code)
        self.bodies.append(response.json())
        return {
            "response": io.BytesIO(response.content),
            "statusCode": 200,
            "contentType": "application/json",
        }


@dataclass
class Sweep:
    """One moto-backed sweep environment: store, S3, Runtime and a submitted run."""

    store: Store
    s3: Any
    runtime: StarletteRuntime
    run_id: str
    repeats: int = 1

    @property
    def tenant(self) -> str:
        return tenant_for_run(self.run_id)

    @property
    def unit_tenants(self) -> tuple[str, ...]:
        """Every (arm, repeat) unit tenant of the run, arm-major."""
        return unit_tenants_for_run(
            self.run_id, arm_count=len(DEFAULT_ARM_SPECS), repeats=self.repeats
        )

    def unit_tenant(self, arm_index: int, repeat_index: int = 0) -> str:
        return unit_tenant(self.run_id, arm_index=arm_index, repeat_index=repeat_index)

    @property
    def execution_arn(self) -> str:
        """The execution `control` names on the steps that carry one, unless a test passes its own."""
        return f"arn:aws:states:us-east-1:123456789012:execution:eval-sweep:{self.run_id}"

    def control(self, step: str, **fields: Any) -> dict[str, Any]:
        if step in OWNED_STEPS:
            fields.setdefault("execution_arn", self.execution_arn)
        return sweep_control_handler(
            {"step": step, "run_id": self.run_id, **fields},
            store=self.store,
            s3=self.s3,
            config=CONTROL_CONFIG,
        )

    def task(self, arm_index: int, task_id: str, repeat_index: int = 0) -> dict[str, Any]:
        return sweep_task_handler(
            {
                "run_id": self.run_id,
                "arm_index": arm_index,
                "repeat_index": repeat_index,
                "task_id": task_id,
            },
            store=self.store,
            runtime=self.runtime,
            config=TASK_CONFIG,
        )

    def result_row(
        self, arm_index: int, task_id: str, repeat_index: int = 0
    ) -> dict[str, object] | None:
        return self.store.get_item(
            "eval_results",
            {"run_id": self.run_id, "result_id": result_id(arm_index, repeat_index, task_id)},
        )

    def objects(self) -> dict[str, bytes]:
        listed = self.s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{PREFIX}/{self.run_id}/")
        return {
            entry["Key"][len(f"{PREFIX}/{self.run_id}/") :]: self.s3.get_object(
                Bucket=BUCKET, Key=entry["Key"]
            )["Body"].read()
            for entry in listed.get("Contents", [])
        }

    def tenant_rows(self) -> int:
        """Rows under the run tenant and every unit tenant."""
        return sum(
            len(self.store.query_tenant_scan(table, tenant=tenant)[0])
            for tenant in (self.tenant, *self.unit_tenants)
            for table in TENANT_SCOPED_TABLES
        )

    def drive(
        self,
        *,
        before_item: Callable[[int, Mapping[str, Any]], None] | None = None,
        fail_teardown: bool = False,
    ) -> dict[str, Any]:
        """Run every step in the state machine's order, with its Catch routing."""
        error: dict[str, Any] | None = None
        teardown_error: dict[str, Any] | None = None
        start = self.control("start")
        if not start["cancelled_before_start"]:
            self.control("seed")
            for index, ref in enumerate(start["task_refs"]):
                if before_item is not None:
                    before_item(index, ref)
                try:
                    self.task(ref["arm_index"], ref["task_id"], ref["repeat_index"])
                except Exception as exc:  # noqa: BLE001 - the state machine's Catch
                    self.control(
                        "item_failed",
                        arm_index=ref["arm_index"],
                        repeat_index=ref["repeat_index"],
                        task_id=ref["task_id"],
                        error=_sfn_error(exc),
                    )
        self.control("aggregate", error=error)
        try:
            if fail_teardown:
                raise RuntimeError("planted teardown failure")
            self.control("teardown")
        except Exception as exc:  # noqa: BLE001 - the state machine's Catch
            teardown_error = _sfn_error(exc)
        return self.control("finalize", error=error, teardown_error=teardown_error)


def _sfn_error(exc: BaseException) -> dict[str, str]:
    """The `{Error, Cause}` object Step Functions hands a Catch for a Lambda function error."""
    return {
        "Error": type(exc).__name__,
        "Cause": json.dumps(
            {"errorMessage": str(exc), "errorType": type(exc).__name__, "stackTrace": ["  x"]}
        ),
    }


def submit(store: Store, *, tasks_total: int, repeats: int = 1) -> str:
    """Write a queued run and take the lock, as `eval.trigger.submit_sweep` does."""
    run_id = allocate_run_id()
    assert acquire_lock(store, run_id=run_id)
    create_run(
        store,
        run_id=run_id,
        mode="stub",
        identity=STARTER,
        tasks_total=tasks_total,
        repeats=repeats,
    )
    return run_id


def limit_tasks(monkeypatch: pytest.MonkeyPatch, count: int) -> list[str]:
    """Restrict the sweep to the first `count` golden tasks; returns their ids."""
    from eval.harness import _DEFAULT_GOLDEN_TASKS_PATH, _load_golden_tasks

    def _first(path: Path) -> list[dict[str, object]]:
        return _load_golden_tasks(path)[:count]

    monkeypatch.setattr(sfn_handlers, "_load_golden_tasks", _first)
    return [str(task["id"]) for task in _first(_DEFAULT_GOLDEN_TASKS_PATH)]


@contextlib.contextmanager
def mocked_aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """moto for every AWS service, with placeholder credentials and no ambient profile."""
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("ACTION_LAMBDA_PREFIX", raising=False)
    with mock_aws():
        yield


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with mocked_aws(monkeypatch):
        yield


def make_sweep(monkeypatch: pytest.MonkeyPatch, *, tasks: int, repeats: int = 1) -> Sweep:
    """A moto store and bucket, the in-process Runtime and a submitted run of `tasks` per arm and
    repeat."""
    client = boto3.client("dynamodb", region_name=REGION)
    create_all_tables(client)
    store = Store(client=client)
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    monkeypatch.setattr(crm.dispatch, "_lambda_client", InProcessLambda(store))
    runtime_server._matrix = None
    runtime_server._stub_provider = None
    task_ids = limit_tasks(monkeypatch, tasks)
    run_id = submit(
        store, tasks_total=len(DEFAULT_ARM_SPECS) * repeats * len(task_ids), repeats=repeats
    )
    return Sweep(
        store=store,
        s3=s3,
        runtime=StarletteRuntime(TestClient(runtime_server.app)),
        run_id=run_id,
        repeats=repeats,
    )


@pytest.fixture
def sweep(aws: None, monkeypatch: pytest.MonkeyPatch) -> Sweep:
    return make_sweep(monkeypatch, tasks=2)


# -- the full path -------------------------------------------------------------------------------


def test_a_full_stub_sweep_completes_and_publishes_what_the_smoke_reads(
    aws: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every golden task on both arms: `completed`, lock released, tenant gone, and the S3 layout
    `scripts/smoke/eval_sfn.py` downloads passes `assert_artifacts`."""
    sweep = make_sweep(monkeypatch, tasks=20)
    final = sweep.drive()

    record = require_run(sweep.store, sweep.run_id)
    assert final == {"run_id": sweep.run_id, "status": "completed", "refused": False}
    assert set(final) == FINALIZE_RESULT_KEYS
    assert (record.tasks_done, record.tasks_failed, record.tasks_skipped) == (40, 0, 0)
    assert record.teardown_status == "ok"
    assert read_lock(sweep.store) is None
    assert sweep.tenant_rows() == 0
    assert len(sweep.runtime.requests) == 40
    catalog = sweep.store.get_item("eval_runs", {"run_id": sweep.run_id})
    assert catalog is not None and catalog["status"] == "torn_down"

    assert record.matrix_report is not None and len(record.matrix_report["arms"]) == 2
    payload = record.to_payload()
    row = check_artifacts(
        sweep.s3, payload, bucket=BUCKET, prefix=PREFIX, arm_count=2, task_count=20
    )
    assert row.status == "pass", row.detail
    assert set(payload["artifacts"]) == {"matrix_json", "matrix_md", "matrix_csv", "raw"}
    assert len(payload["artifacts"]["raw"]) == 2


def test_every_artifact_object_is_written_under_sse_kms(sweep: Sweep) -> None:
    """The bucket policy refuses any PutObject without `aws:kms`; every object carries it."""
    sweep.drive()
    listed = sweep.s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{PREFIX}/{sweep.run_id}/")
    keys_written = [entry["Key"] for entry in listed["Contents"]]
    assert any(key.endswith("demo-baseline.json") for key in keys_written)
    assert any(key.endswith("verification-inventory/eval_sweep.json") for key in keys_written)
    for key in keys_written:
        head = sweep.s3.head_object(Bucket=BUCKET, Key=key)
        assert head["ServerSideEncryption"] == "aws:kms", key


def test_the_published_demo_delta_checks_pass(sweep: Sweep) -> None:
    """Teardown publishes both canary and demo checks at `pass` for a run that wrote under its own tenant."""
    sweep.drive()
    inventory = json.loads(sweep.objects()["verification-inventory/eval_sweep.json"])
    statuses = {entry["check"]: entry["status"] for entry in inventory["checks"]}
    assert statuses == {"demo_tenant_untouched": "pass", "writes_only_under_smoke_tenant": "pass"}


def test_the_partial_artifacts_still_parse_as_the_full_artifacts_do(
    sweep: Sweep, tmp_path: Path
) -> None:
    """A two-task sweep's artifacts pass `assert_artifacts` for its own task count."""
    sweep.drive()
    for relative, body in sweep.objects().items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    raw = require_run(sweep.store, sweep.run_id).to_payload()["artifacts"]["raw"]
    assert_artifacts(
        output_dir=tmp_path,
        expected_arm_count=2,
        expected_task_count=2,
        raw_archive_paths=tuple(tmp_path / path for path in raw),
    )


# -- start ---------------------------------------------------------------------------------------


def test_start_returns_the_arm_major_refs_and_marks_the_run_running(sweep: Sweep) -> None:
    result = sweep.control("start")
    assert set(result) == START_RESULT_KEYS
    assert result["cancelled_before_start"] is False
    refs = result["task_refs"]
    assert all(set(ref) == TASK_REF_KEYS for ref in refs)
    assert [ref["arm_index"] for ref in refs] == [0, 0, 1, 1]
    assert require_run(sweep.store, sweep.run_id).status == "running"
    assert "demo-baseline.json" in sweep.objects()


def test_start_records_the_execution_arn_on_the_run_and_the_lock_when_absent(sweep: Sweep) -> None:
    """A run whose submit never recorded its ARN gets it on both rows."""
    arn = f"arn:aws:states:us-east-1:123456789012:execution:sweep:{sweep.run_id}"
    sweep.control("start", execution_arn=arn)
    assert require_run(sweep.store, sweep.run_id).execution_arn == arn
    lock = read_lock(sweep.store)
    assert lock is not None and lock.execution_arn == arn


def test_start_refuses_a_task_count_the_sweep_does_not_have(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sweep = make_sweep(monkeypatch, tasks=2)
    limit_tasks(monkeypatch, 3)
    with pytest.raises(ValidationFailed, match="task count"):
        sweep.control("start")
    assert require_run(sweep.store, sweep.run_id).status == "queued"


def test_a_redelivered_start_on_a_running_run_proceeds(sweep: Sweep) -> None:
    first = sweep.control("start")
    assert sweep.control("start") == first


def test_start_on_a_terminal_run_is_refused_and_writes_nothing(sweep: Sweep) -> None:
    sweep.drive()
    before = (require_run(sweep.store, sweep.run_id), sweep.objects())
    assert sweep.control("start") == {
        "refused": True,
        "cancelled_before_start": False,
        "task_refs": [],
    }
    assert (require_run(sweep.store, sweep.run_id), sweep.objects()) == before


def test_start_requires_the_execution_arn(sweep: Sweep) -> None:
    with pytest.raises(ValidationFailed):
        sweep_control_handler(
            {"step": "start", "run_id": sweep.run_id}, store=sweep.store, config=CONTROL_CONFIG
        )
    assert require_run(sweep.store, sweep.run_id).status == "queued"


def _hand_lock_to_another_run(sweep: Sweep) -> str:
    other = allocate_run_id()
    sweep.store.delete_item("eval_runs", {"run_id": "lock#sweep"})
    assert acquire_lock(sweep.store, run_id=other)
    return other


@pytest.mark.parametrize("recorded", [False, True], ids=["no-arn-recorded", "own-arn-recorded"])
def test_start_is_refused_while_another_run_holds_the_lock(sweep: Sweep, recorded: bool) -> None:
    """The lock names another run, so start writes nothing, seeds nothing and leaves that
    run's lock as it was, whether or not this run already records this execution."""
    if recorded:
        sweep.control("start")
    other = _hand_lock_to_another_run(sweep)
    status_before = require_run(sweep.store, sweep.run_id).status
    result = sweep.control("start")
    assert result["refused"] is True and result["task_refs"] == []
    record = require_run(sweep.store, sweep.run_id)
    assert record.status == status_before
    assert record.execution_arn == (sweep.execution_arn if recorded else None)
    lock = read_lock(sweep.store)
    assert lock is not None and (lock.holder_run_id, lock.execution_arn) == (other, None)
    assert sweep.tenant_rows() == 0


def test_a_refused_execution_arn_write_leaves_the_job_row_unchanged(sweep: Sweep) -> None:
    """A lock naming another run refuses the write, and the job row keeps no ARN."""
    _hand_lock_to_another_run(sweep)
    with pytest.raises(Conflict):
        set_execution_arn(sweep.store, run_id=sweep.run_id, execution_arn=OTHER_EXECUTION)
    assert require_run(sweep.store, sweep.run_id).execution_arn is None


@pytest.mark.parametrize("lock", [True, False], ids=["with-lock", "job-row-only"])
def test_set_execution_arn_never_replaces_another_execution(sweep: Sweep, lock: bool) -> None:
    sweep.control("start")
    with pytest.raises(Conflict):
        set_execution_arn(
            sweep.store, run_id=sweep.run_id, execution_arn=OTHER_EXECUTION, lock=lock
        )
    assert require_run(sweep.store, sweep.run_id).execution_arn == sweep.execution_arn
    held = read_lock(sweep.store)
    assert held is not None and held.execution_arn == sweep.execution_arn


def test_a_record_naming_another_tenant_is_refused(sweep: Sweep) -> None:
    sweep.store.update_item(
        "eval_runs",
        {"run_id": f"job#{sweep.run_id}"},
        set_values={"tenant": "demo"},
        condition=None,
    )
    with pytest.raises(ValidationFailed, match="tenant"):
        sweep.control("start")


# -- sweep_task ----------------------------------------------------------------------------------


def test_sweep_task_sends_the_design_eval_turn_request(sweep: Sweep) -> None:
    """The arm's wire-schema tuning, the task's actor in the eval tenant, the run's
    provider mode, and a padded session id."""
    refs = sweep.control("start")["task_refs"]
    sweep.control("seed")
    assert set(sweep.task(1, refs[2]["task_id"])) == SWEEP_TASK_RESULT_KEYS
    sent = sweep.runtime.requests[0]
    request = sent["payload"]
    assert request["mode"] == "eval_turn"
    assert request["provider_mode"] == "stub"
    assert request["tuning"]["schema"] == WIRE_SCHEMA_VERSION
    assert {tier["family"] for tier in request["tuning"]["tiers"].values()} == {
        DEFAULT_ARM_SPECS[1].family
    }
    claims = request["_agent_identity"]["claims"]
    assert claims["custom:tenant"] == sweep.unit_tenant(1)
    assert sent["runtimeSessionId"] == request["_agent_identity"]["session_id"]
    assert sent["runtimeSessionId"] == session_id_for(sweep.run_id, 1, refs[2]["task_id"])
    assert len(sent["runtimeSessionId"]) >= 33
    assert sent["agentRuntimeArn"] == TASK_CONFIG.runtime_arn
    assert sent["qualifier"] == "DEFAULT"


def test_a_turn_under_another_prompt_bundle_is_refused_and_stops_later_turns(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unit whose Runtime digest differs from the run's first done unit is recorded failed and
    marked; every later unit is then refused before its turn is sent."""
    refs = sweep.control("start")["task_refs"]
    sweep.control("seed")
    assert sweep.task(refs[0]["arm_index"], refs[0]["task_id"]) == {"item_status": "done"}
    monkeypatch.setattr("agents.prompt_bundle.prompt_bundle_sha256", lambda: "b" * 64)

    with pytest.raises(Conflict, match="more than one prompt bundle"):
        sweep.task(refs[1]["arm_index"], refs[1]["task_id"])
    row = sweep.result_row(refs[1]["arm_index"], refs[1]["task_id"])
    assert row is not None and row["item_status"] == "failed"
    assert row[BUNDLE_MISMATCH_ATTRIBUTE] is True
    assert row[PROMPT_BUNDLE_SHA256_KEY] == "b" * 64

    sent = len(sweep.runtime.requests)
    with pytest.raises(Conflict, match="more than one prompt bundle"):
        sweep.task(refs[2]["arm_index"], refs[2]["task_id"])
    assert len(sweep.runtime.requests) == sent, "a refused item must send no turn"
    assert sweep.result_row(refs[2]["arm_index"], refs[2]["task_id"]) is None


def test_turns_under_the_same_prompt_bundle_are_all_recorded_done(sweep: Sweep) -> None:
    refs = sweep.control("start")["task_refs"]
    sweep.control("seed")
    for ref in refs[:3]:
        assert sweep.task(ref["arm_index"], ref["task_id"]) == {"item_status": "done"}


def test_a_done_item_stores_a_turn_that_rebuilds_exactly(sweep: Sweep) -> None:
    refs = sweep.control("start")["task_refs"]
    sweep.control("seed")
    assert sweep.task(0, refs[0]["task_id"]) == {"item_status": "done"}
    row = sweep.result_row(0, refs[0]["task_id"])
    assert row is not None and row["item_status"] == "done"
    turn = turn_from_row(row)
    assert turn.stream_calls >= 1 and turn.invocations
    assert serialise_turn(turn) == sweep.runtime.bodies[0], "the stored turn is the observed one"
    assert row[PROMPT_BUNDLE_SHA256_KEY] == prompt_bundle_sha256(), "the Runtime's live digest"
    score = task_score_from_fields(json.loads(str(row["task_score"])))
    assert score.to_report_fields() == json.loads(str(row["task_score"]))
    tiers = tier_scores_from_fields(json.loads(str(row["tier_scores"])))
    assert tiers.to_report_fields() == json.loads(str(row["tier_scores"]))
    assert require_run(sweep.store, sweep.run_id).tasks_done == 1


def test_a_recorded_item_is_not_run_again(sweep: Sweep) -> None:
    refs = sweep.control("start")["task_refs"]
    sweep.control("seed")
    sweep.task(0, refs[0]["task_id"])
    assert sweep.task(0, refs[0]["task_id"]) == {"item_status": "done"}
    assert len(sweep.runtime.requests) == 1
    assert require_run(sweep.store, sweep.run_id).tasks_done == 1


def test_a_cancelled_run_skips_the_item_without_invoking_the_runtime(sweep: Sweep) -> None:
    refs = sweep.control("start")["task_refs"]
    request_cancel(sweep.store, run_id=sweep.run_id, identity=STARTER)
    assert sweep.task(0, refs[0]["task_id"]) == {"item_status": "skipped_cancelled"}
    assert sweep.runtime.requests == []
    assert require_run(sweep.store, sweep.run_id).tasks_skipped == 1


@pytest.mark.parametrize(
    ("event", "match"),
    [
        ({"arm_index": 0, "repeat_index": 0, "task_id": "x"}, "not the shape"),
        ({"run_id": "r", "arm_index": 0, "task_id": "x"}, "not the shape"),
        (
            {"run_id": "r", "arm_index": 0, "repeat_index": 0, "task_id": "x", "tenant": "demo"},
            "not the shape",
        ),
        ({"run_id": "r", "arm_index": 9, "repeat_index": 0, "task_id": "x"}, "names no arm"),
        ({"run_id": "r", "arm_index": True, "repeat_index": 0, "task_id": "x"}, "integer"),
    ],
)
def test_sweep_task_refuses_a_malformed_item(
    sweep: Sweep, event: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValidationFailed, match=match):
        sweep_task_handler(event, store=sweep.store, runtime=sweep.runtime, config=TASK_CONFIG)
    assert sweep.runtime.requests == []


def test_an_unknown_task_id_is_refused_before_the_runtime(sweep: Sweep) -> None:
    sweep.control("start")
    with pytest.raises(ValidationFailed, match="golden task"):
        sweep.task(0, "gt-99-no-such-task")
    assert sweep.runtime.requests == []


def _body_failure(status: int, code: str) -> Callable[[], dict[str, Any]]:
    body = json.dumps({"statusCode": status, "error": {"code": code, "message": "x"}}).encode()
    return lambda: {"response": io.BytesIO(body), "statusCode": status}


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (_body_failure(504, "turn_timeout"), TurnTimeout),
        (_body_failure(503, "provider_unavailable"), ProviderUnavailable),
        (_body_failure(403, "scope_denied"), CrmError),
    ],
)
def test_a_runtime_failure_body_is_classified_and_raised(
    sweep: Sweep, failure: Callable[[], dict[str, Any]], expected: type[CrmError]
) -> None:
    refs = sweep.control("start")["task_refs"]
    sweep.runtime.fail_with = failure
    with pytest.raises(expected) as raised:
        sweep.task(0, refs[0]["task_id"])
    assert type(raised.value) is expected
    assert sweep.result_row(0, refs[0]["task_id"]) is None


def test_a_real_runtime_rejection_is_raised_not_recorded(sweep: Sweep) -> None:
    """A refusal from the real app (live provider mode on a stub Runtime is a 400) raises for the
    Catch, and records nothing."""
    refs = sweep.control("start")["task_refs"]
    sweep.store.update_item(
        "eval_runs",
        {"run_id": f"job#{sweep.run_id}"},
        set_values={"job_mode": "live"},
        condition=None,
    )
    with pytest.raises(CrmError, match="did not complete") as raised:
        sweep.task(0, refs[0]["task_id"])
    assert "RuntimeClientError" in str(raised.value.detail)
    assert sweep.result_row(0, refs[0]["task_id"]) is None


@pytest.mark.parametrize(
    ("turn_error", "expected", "status"),
    [("timeout", TurnTimeout, 504), ("provider", ProviderUnavailable, 503)],
)
def test_a_real_runtime_failure_is_classified_by_the_status_agentcore_embeds(
    sweep: Sweep,
    monkeypatch: pytest.MonkeyPatch,
    turn_error: str,
    expected: type[CrmError],
    status: int,
) -> None:
    """The real app's 504/503 reaches the sweep only as AgentCore's fixed-message 424."""
    from eval.live import LiveInvocationError

    def _failing_turn(**_: Any) -> dict[str, Any]:
        raise TimeoutError if turn_error == "timeout" else LiveInvocationError("x")

    monkeypatch.setattr(runtime_server, "_run_eval_turn", _failing_turn)
    refs = sweep.control("start")["task_refs"]
    with pytest.raises(CrmError) as raised:
        sweep.task(0, refs[0]["task_id"])
    assert type(raised.value) is expected
    assert f"Received error ({status}) from runtime" in str(raised.value.detail)
    assert sweep.result_row(0, refs[0]["task_id"]) is None


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ReadTimeoutError(endpoint_url="https://runtime"), TurnTimeout),
        (runtime_client_error(504), TurnTimeout),
        (runtime_client_error(503), ProviderUnavailable),
        (runtime_client_error(500), CrmError),
        (runtime_client_error(400), CrmError),
        (
            ClientError(
                {"Error": {"Code": "RuntimeClientError", "Message": "error 504 turn_timeout"}},
                "InvokeAgentRuntime",
            ),
            CrmError,
        ),
        (
            ClientError({"Error": {"Code": "ThrottlingException"}}, "InvokeAgentRuntime"),
            CrmError,
        ),
    ],
)
def test_a_transport_failure_is_classified(
    sweep: Sweep, exc: Exception, expected: type[CrmError]
) -> None:
    refs = sweep.control("start")["task_refs"]

    def _raise() -> dict[str, Any]:
        raise exc

    sweep.runtime.fail_with = _raise
    with pytest.raises(expected) as raised:
        sweep.task(0, refs[0]["task_id"])
    assert type(raised.value) is expected


@pytest.mark.parametrize(
    "body", [b"not json", b"[]", json.dumps({"schema": 1, "outcome": {}}).encode()]
)
def test_an_unreadable_runtime_body_is_a_failure(sweep: Sweep, body: bytes) -> None:
    refs = sweep.control("start")["task_refs"]
    sweep.runtime.fail_with = lambda: {"response": io.BytesIO(body), "statusCode": 200}
    with pytest.raises(CrmError, match="did not complete"):
        sweep.task(0, refs[0]["task_id"])


def test_a_full_shape_schema_1_body_fails_on_the_named_schema_mismatch(sweep: Sweep) -> None:
    """A schema-1 turn that is otherwise well formed fails sweep_task on the schema check alone."""
    refs = sweep.control("start")["task_refs"]

    def _schema_1_body() -> dict[str, Any]:
        request = sweep.runtime.requests[-1]["payload"]
        response = sweep.runtime.client.post("/invocations", json=request)
        assert response.status_code == 200
        body = response.json()
        assert body["schema"] == 2
        body["schema"] = 1
        return {"response": io.BytesIO(json.dumps(body).encode()), "statusCode": 200}

    sweep.runtime.fail_with = _schema_1_body
    with pytest.raises(CrmError, match="did not complete") as raised:
        sweep.task(0, refs[0]["task_id"])
    assert raised.value.detail == "malformed turn: turn.schema must be 2, got 1"


def test_classification_by_status_when_no_code_is_named() -> None:
    assert type(classify_runtime_failure(status=504, code=None, detail="")) is TurnTimeout
    assert type(classify_runtime_failure(status=503, code=None, detail="")) is ProviderUnavailable
    assert type(classify_runtime_failure(status=500, code=None, detail="")) is CrmError
    assert type(classify_runtime_failure(status=None, code="other", detail="")) is CrmError


# -- item_failed, aggregate, teardown, finalize --------------------------------------------------


def test_a_failed_item_is_counted_and_fails_the_run_with_a_per_family_summary(
    sweep: Sweep,
) -> None:
    """Any failed item makes the run `failed`; the summary keeps the message, not the trace."""

    seen: list[Mapping[str, Any]] = []

    def _fail_first(index: int, ref: Mapping[str, Any]) -> None:
        seen.append(ref)
        sweep.runtime.fail_with = _body_failure(504, "turn_timeout") if index == 0 else None

    final = sweep.drive(before_item=_fail_first)
    record = require_run(sweep.store, sweep.run_id)
    assert final["status"] == "failed"
    assert (record.tasks_done, record.tasks_failed) == (3, 1)
    assert record.failure_summary is not None
    assert f"{DEFAULT_ARM_SPECS[0].family}=1" in record.failure_summary
    assert "stackTrace" not in record.failure_summary
    first = sweep.result_row(0, seen[0]["task_id"])
    assert first is not None and str(first["error"]).startswith("TurnTimeout:")
    assert record.matrix_report is not None, "the done items are still aggregated"
    assert read_lock(sweep.store) is None


def test_only_a_failed_item_marks_its_cost_unmeasured(sweep: Sweep) -> None:
    """A failed row carries `cost_measured: false`; a done row carries no such field."""
    refs: list[Mapping[str, Any]] = []

    def _fail_first(index: int, ref: Mapping[str, Any]) -> None:
        refs.append(ref)
        sweep.runtime.fail_with = _body_failure(504, "turn_timeout") if index == 0 else None

    sweep.drive(before_item=_fail_first)
    rows = [sweep.result_row(ref["arm_index"], ref["task_id"]) for ref in refs]
    assert all(row is not None for row in rows)
    failed, *done = rows
    assert failed is not None and failed["item_status"] == "failed"
    assert failed["cost_measured"] is False
    assert [row is not None and row["item_status"] for row in done] == ["done"] * len(done)
    assert all(row is not None and "cost_measured" not in row for row in done)


def _published_matrix_md(sweep: Sweep) -> str:
    listed = sweep.s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{PREFIX}/{sweep.run_id}/")
    [key] = [entry["Key"] for entry in listed["Contents"] if entry["Key"].endswith("/matrix.md")]
    body: bytes = sweep.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    return body.decode("utf-8")


def test_a_run_with_errored_items_publishes_the_cost_qualifier(sweep: Sweep) -> None:
    """The published matrix.md says the cost excludes the run's errored items."""

    def _fail_first_two(index: int, ref: Mapping[str, Any]) -> None:
        sweep.runtime.fail_with = _body_failure(504, "turn_timeout") if index < 2 else None

    sweep.drive(before_item=_fail_first_two)
    assert require_run(sweep.store, sweep.run_id).tasks_failed == 2
    note = errored_items_cost_note(2)
    assert note is not None and note in _published_matrix_md(sweep)


def test_a_run_without_errored_items_publishes_no_cost_qualifier(sweep: Sweep) -> None:
    sweep.drive()
    assert require_run(sweep.store, sweep.run_id).tasks_failed == 0
    assert "Cost excludes" not in _published_matrix_md(sweep)


def test_a_retried_item_failure_keeps_the_first_record(sweep: Sweep) -> None:
    refs = sweep.control("start")["task_refs"]
    ref = refs[0]
    for _ in range(2):
        sweep.control(
            "item_failed",
            arm_index=ref["arm_index"],
            repeat_index=ref["repeat_index"],
            task_id=ref["task_id"],
            error={"Error": "X", "Cause": ""},
        )
    row = sweep.result_row(ref["arm_index"], ref["task_id"])
    assert row is not None and row["cost_measured"] is False
    assert require_run(sweep.store, sweep.run_id).tasks_failed == 1


def test_a_teardown_failure_fails_the_run_and_still_releases_the_lock(sweep: Sweep) -> None:
    final = sweep.drive(fail_teardown=True)
    record = require_run(sweep.store, sweep.run_id)
    assert final["status"] == "failed"
    assert record.teardown_status == "failed"
    assert record.failure_summary is not None and "teardown failed" in record.failure_summary
    assert read_lock(sweep.store) is None


def test_a_step_error_fails_the_run(sweep: Sweep) -> None:
    sweep.control("start")
    final = sweep.control(
        "finalize",
        error={"Error": "States.TaskFailed", "Cause": "seed failed"},
        teardown_error=None,
    )
    record = require_run(sweep.store, sweep.run_id)
    assert final["status"] == "failed"
    assert record.failure_summary == (
        "step failed: States.TaskFailed: seed failed; 4 task(s) never recorded"
    )
    assert record.teardown_status == "ok"
    assert read_lock(sweep.store) is None


def test_finalize_releases_the_lock_even_when_the_run_is_unreadable(sweep: Sweep) -> None:
    sweep.store.delete_item("eval_runs", {"run_id": f"job#{sweep.run_id}"})
    with pytest.raises(NotFound):
        sweep.control("finalize")
    assert read_lock(sweep.store) is None


def test_finalize_never_releases_another_runs_lock(sweep: Sweep) -> None:
    other = _hand_lock_to_another_run(sweep)
    sweep.control("finalize")
    lock = read_lock(sweep.store)
    assert lock is not None and lock.holder_run_id == other


def test_aggregate_with_no_done_item_writes_nothing(sweep: Sweep) -> None:
    sweep.control("start")
    assert sweep.control("aggregate", error=None) == {
        "run_id": sweep.run_id,
        "arms": 0,
        "items_unrecorded": 4,
    }
    assert "matrix.json" not in sweep.objects()
    assert require_run(sweep.store, sweep.run_id).matrix_report is None


# -- silent-failure findings -------------------------------------------------------------


def test_finalize_needs_no_artifacts_configuration_and_releases_the_lock(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the artefacts variables unset, finalize still finalizes and frees the lock."""
    monkeypatch.delenv("EVAL_ARTIFACTS_BUCKET", raising=False)
    monkeypatch.delenv("EVAL_ARTIFACTS_PREFIX", raising=False)
    sweep.control("start")
    final = sweep_control_handler(
        {
            "step": "finalize",
            "run_id": sweep.run_id,
            "execution_arn": sweep.execution_arn,
            "error": None,
            "teardown_error": None,
        },
        store=sweep.store,
    )
    assert final["status"] == "failed"
    assert read_lock(sweep.store) is None


def test_a_malformed_finalize_event_still_releases_the_lock(sweep: Sweep) -> None:
    sweep.control("start")
    with pytest.raises(ValidationFailed):
        sweep_control_handler(
            {
                "step": "finalize",
                "run_id": sweep.run_id,
                "execution_arn": sweep.execution_arn,
                "tenant": "demo",
            },
            store=sweep.store,
            config=CONTROL_CONFIG,
        )
    assert read_lock(sweep.store) is None


@pytest.mark.parametrize("execution_arn", [OTHER_EXECUTION, None], ids=["other", "absent"])
def test_a_malformed_finalize_event_from_another_execution_keeps_the_lock(
    sweep: Sweep, execution_arn: str | None
) -> None:
    sweep.control("start")
    event: dict[str, Any] = {"step": "finalize", "run_id": sweep.run_id, "tenant": "demo"}
    if execution_arn is not None:
        event["execution_arn"] = execution_arn
    with pytest.raises(ValidationFailed):
        sweep_control_handler(event, store=sweep.store, config=CONTROL_CONFIG)
    lock = read_lock(sweep.store)
    assert lock is not None and lock.holder_run_id == sweep.run_id


def test_an_unreadable_result_row_at_finalize_fails_the_run_and_frees_the_lock(
    sweep: Sweep,
) -> None:
    """A summary that cannot be computed marks the run failed instead of leaving it running."""
    refs = sweep.control("start")["task_refs"]
    sweep.control(
        "item_failed",
        arm_index=refs[0]["arm_index"],
        repeat_index=refs[0]["repeat_index"],
        task_id=refs[0]["task_id"],
        error={"Error": "X", "Cause": ""},
    )
    sweep.store.update_item(
        "eval_results",
        {"run_id": sweep.run_id, "result_id": keys.eval_result_id(0, refs[0]["task_id"])},
        set_values={"item_status": "garbled"},
        condition=None,
    )
    with pytest.raises(ValidationFailed):
        sweep.control("finalize", error=None, teardown_error=None)
    record = require_run(sweep.store, sweep.run_id)
    assert record.status == "failed"
    assert record.failure_summary is not None
    assert record.failure_summary.startswith("finalize could not summarise:")
    assert read_lock(sweep.store) is None


def test_a_run_with_no_baseline_publishes_a_failing_demo_check_and_fails(sweep: Sweep) -> None:
    """Teardown without a captured baseline publishes the delta as `fail`, never skips
    it, and finalize then marks the run `failed` naming the check. The run never seeded, so its
    `writes_only_under_smoke_tenant` failure is published but does not fail the run."""
    request_cancel(sweep.store, run_id=sweep.run_id, identity=STARTER)
    sweep.control("start")
    sweep.s3.delete_object(Bucket=BUCKET, Key=f"{PREFIX}/{sweep.run_id}/demo-baseline.json")
    sweep.control("aggregate", error=None)
    sweep.control("teardown")
    inventory = json.loads(sweep.objects()["verification-inventory/eval_sweep.json"])
    statuses = {entry["check"]: entry["status"] for entry in inventory["checks"]}
    assert statuses == {"demo_tenant_untouched": "fail", "writes_only_under_smoke_tenant": "fail"}
    final = sweep.control("finalize", error=None, teardown_error=None)
    record = require_run(sweep.store, sweep.run_id)
    assert final["status"] == "failed"
    assert record.failure_summary == ("isolation check(s) not passing: demo_tenant_untouched=fail")
    assert read_lock(sweep.store) is None


def test_a_demo_write_during_the_sweep_fails_the_run(sweep: Sweep) -> None:
    """A `demo` row written mid-sweep turns its delta check `fail`, and the run is `failed`
    with that check named, not `completed`."""

    def _write_demo(index: int, _ref: Mapping[str, Any]) -> None:
        if index == 0:
            sweep.store.put_item(
                "accounts",
                {
                    "account_id": "probe-demo-acct",
                    "tenant": "demo",
                    "created_at": "2026-10-08T00:00:00+00:00",
                    "name": "x",
                },
            )

    final = sweep.drive(before_item=_write_demo)
    record = require_run(sweep.store, sweep.run_id)
    assert final["status"] == "failed"
    assert record.tasks_failed == 0
    assert record.failure_summary == ("isolation check(s) not passing: demo_tenant_untouched=fail")
    assert read_lock(sweep.store) is None


@pytest.mark.parametrize("body", [b"[]", b'"x"', b"7"])
def test_a_baseline_that_is_not_an_object_is_refused(sweep: Sweep, body: bytes) -> None:
    """A valid-JSON baseline of the wrong shape raises instead of reading as unobserved."""
    sweep.control("start")
    sweep.s3.put_object(
        Bucket=BUCKET,
        Key=f"{PREFIX}/{sweep.run_id}/demo-baseline.json",
        Body=body,
        ServerSideEncryption="aws:kms",
    )
    with pytest.raises(ValidationFailed, match="baseline"):
        sweep.control("teardown")


def test_teardown_reads_the_catalog_row_consistently(sweep: Sweep) -> None:
    """The catalog row is read with a strongly consistent read."""
    sweep.control("start")
    sweep.control("seed")
    calls: list[tuple[str, dict[str, Any], bool]] = []
    original = sweep.store.get_item

    def _recording(table: str, key: Any, *, consistent: bool = False) -> Any:
        calls.append((table, dict(key), consistent))
        return original(table, key, consistent=consistent)

    sweep.store.get_item = _recording  # type: ignore[method-assign]
    sweep.control("teardown")
    catalog_reads = [c for c in calls if c[0] == "eval_runs" and c[1] == {"run_id": sweep.run_id}]
    assert catalog_reads and all(consistent for _t, _k, consistent in catalog_reads)
    catalog = original("eval_runs", {"run_id": sweep.run_id})
    assert catalog is not None and catalog["status"] == "torn_down"


def test_a_second_execution_is_refused_at_every_owned_step_and_changes_nothing(
    sweep: Sweep,
) -> None:
    """A second execution for a live run is refused at start, teardown and finalize;
    the first's record, tenant, lock and artefacts are as they were."""
    refs = sweep.control("start")["task_refs"]
    sweep.control("seed")
    sweep.task(refs[0]["arm_index"], refs[0]["task_id"])
    before = (
        require_run(sweep.store, sweep.run_id),
        sweep.tenant_rows(),
        read_lock(sweep.store),
        sweep.objects(),
    )
    assert before[1] > 0
    assert sweep.control("start", execution_arn=OTHER_EXECUTION)["refused"] is True
    assert sweep.control("teardown", execution_arn=OTHER_EXECUTION)["refused"] is True
    assert sweep.control(
        "finalize",
        execution_arn=OTHER_EXECUTION,
        error={"Error": "SweepExecutionRefused", "Cause": ""},
        teardown_error=None,
    ) == {"run_id": sweep.run_id, "status": None, "refused": True}
    after = (
        require_run(sweep.store, sweep.run_id),
        sweep.tenant_rows(),
        read_lock(sweep.store),
        sweep.objects(),
    )
    assert after == before
    assert after[0].status == "running"


def test_unrecorded_items_are_disclosed_and_fail_the_run(sweep: Sweep) -> None:
    """Items with no result row are counted, and the run is not reported `completed`."""
    refs = sweep.control("start")["task_refs"]
    sweep.control("seed")
    sweep.task(refs[0]["arm_index"], refs[0]["task_id"])
    assert sweep.control("aggregate", error=None)["items_unrecorded"] == 3
    sweep.control("teardown")
    final = sweep.control("finalize", error=None, teardown_error=None)
    record = require_run(sweep.store, sweep.run_id)
    assert final["status"] == "failed"
    assert record.failure_summary == "3 task(s) never recorded"


def test_a_sweep_task_failure_is_logged_with_its_item(
    sweep: Sweep, caplog: pytest.LogCaptureFixture
) -> None:
    """A raised item failure is logged with the run, arm and task before it propagates."""
    refs = sweep.control("start")["task_refs"]
    sweep.runtime.fail_with = _body_failure(504, "turn_timeout")
    with caplog.at_level("INFO", logger="eval.sfn_handlers"), pytest.raises(TurnTimeout):
        sweep.task(refs[0]["arm_index"], refs[0]["task_id"])
    failures = [
        json.loads(r.getMessage())
        for r in caplog.records
        if '"sweep_task_failed"' in r.getMessage()
    ]
    assert failures == [
        {
            "event": "sweep_task_failed",
            "correlation_id": sweep.run_id,
            "reason": (
                f"arm_index=0 repeat_index=0 task_id={refs[0]['task_id']} TurnTimeout "
                "code=turn_timeout "
                "detail=Runtime status 504 code turn_timeout: x"
            ),
        }
    ]


def test_a_sweep_task_failure_log_carries_the_agentcore_message_bounded(
    sweep: Sweep, caplog: pytest.LogCaptureFixture
) -> None:
    """The refused invoke's code and message are logged, cut to the error-text bound."""
    refs = sweep.control("start")["task_refs"]
    long_tail = "y" * 4096

    def _raise() -> dict[str, Any]:
        error = runtime_client_error(504)
        error.response["Error"]["Message"] += long_tail
        raise error

    sweep.runtime.fail_with = _raise
    with caplog.at_level("INFO", logger="eval.sfn_handlers"), pytest.raises(TurnTimeout):
        sweep.task(refs[0]["arm_index"], refs[0]["task_id"])
    [reason] = [
        json.loads(r.getMessage())["reason"]
        for r in caplog.records
        if '"sweep_task_failed"' in r.getMessage()
    ]
    prefix = (
        f"arm_index=0 repeat_index=0 task_id={refs[0]['task_id']} TurnTimeout "
        "code=turn_timeout detail="
    )
    assert reason.startswith(prefix + "InvokeAgentRuntime RuntimeClientError: Received error (504)")
    detail = reason.removeprefix(prefix)
    assert len(detail) == sfn_handlers._MAX_ERROR_TEXT and detail.endswith("...")


def test_a_non_taxonomy_sweep_task_failure_logs_its_type_only(
    sweep: Sweep, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = sweep.control("start")["task_refs"]

    def _boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("secret-ish text")

    monkeypatch.setattr(sfn_handlers, "_sweep_task", _boom)
    with caplog.at_level("INFO", logger="eval.sfn_handlers"), pytest.raises(RuntimeError):
        sweep.task(refs[0]["arm_index"], refs[0]["task_id"])
    [reason] = [
        json.loads(r.getMessage())["reason"]
        for r in caplog.records
        if '"sweep_task_failed"' in r.getMessage()
    ]
    assert reason == f"arm_index=0 repeat_index=0 task_id={refs[0]['task_id']} RuntimeError"


def test_a_task_without_a_prompt_is_refused_before_the_runtime(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = sweep.control("start")["task_refs"]
    original = sfn_handlers._sweep_tasks

    def _promptless() -> list[dict[str, object]]:
        return [{**task, "prompt": ""} for task in original()]

    monkeypatch.setattr(sfn_handlers, "_sweep_tasks", _promptless)
    with pytest.raises(ValidationFailed, match="prompt"):
        sweep.task(refs[0]["arm_index"], refs[0]["task_id"])
    assert sweep.runtime.requests == []


def test_teardown_never_touches_demo_or_canary(sweep: Sweep) -> None:
    """Teardown deletes only `eval-<run_id>`; a planted `canary` row fails it loudly."""
    from eval.tenancy import CanaryTenantNotEmptyError

    sweep.control("start")
    sweep.control("seed")
    sweep.store.put_item(
        "accounts",
        {
            "account_id": "acct-canary-0001",
            "tenant": "canary",
            "name": "planted",
            "name_lower": "planted",
        },
    )
    with pytest.raises(CanaryTenantNotEmptyError):
        sweep.control("teardown")
    assert sweep.store.get_item("accounts", {"account_id": "acct-canary-0001"}) is not None


def test_item_failed_keeps_only_the_error_message() -> None:
    cause = json.dumps({"errorMessage": "boom", "errorType": "X", "stackTrace": ["frame"]})
    assert sfn_handlers._error_text({"Error": "X", "Cause": cause}) == "X: boom"
    assert sfn_handlers._error_text({"Error": "States.Timeout", "Cause": ""}) == "States.Timeout"
    assert sfn_handlers._error_text(None) is None
    assert len(sfn_handlers._error_text({"Error": "E", "Cause": "c" * 5000}) or "") == 1024


# -- event shape ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        {"step": "launch", "run_id": "r"},
        {"run_id": "r"},
        {"step": "seed"},
        {"step": "seed", "run_id": "r", "tenant": "demo"},
        {"step": "teardown", "run_id": "r", "error": None},
    ],
)
def test_sweep_control_refuses_an_unrecognised_event(sweep: Sweep, event: dict[str, Any]) -> None:
    with pytest.raises(ValidationFailed):
        sweep_control_handler(event, store=sweep.store, s3=sweep.s3, config=CONTROL_CONFIG)
    assert require_run(sweep.store, sweep.run_id).status == "queued"


def test_the_configs_refuse_a_missing_variable() -> None:
    with pytest.raises(ValueError, match="EVAL_ARTIFACTS"):
        SweepControlConfig.from_env({"EVAL_ARTIFACTS_BUCKET": "b"})
    with pytest.raises(ValueError, match="AGENT_RUNTIME"):
        SweepTaskConfig.from_env({"AGENT_RUNTIME_ARN": "arn"})
    config = SweepControlConfig.from_env(
        {"EVAL_ARTIFACTS_BUCKET": "b", "EVAL_ARTIFACTS_PREFIX": "/p/", "CRM_TABLE_PREFIX": "x-"}
    )
    assert config.artifact_root("r") == "p/r/"
    assert config.table_prefix == "x-"


@pytest.mark.parametrize(
    ("prefix", "run_id", "relative"),
    [
        ("eval-runs", "run-1", "matrix.json"),
        ("eval-runs", "run-1", "raw/a_b+c.json"),
        ("a/b", "r", "x"),
    ],
)
def test_the_artefact_key_sweep_control_writes_is_the_one_artefact_url_signs(
    prefix: str, run_id: str, relative: str
) -> None:
    """Writer and reader derive `<prefix>/<run_id>/<relative>` from the one helper, byte-identically."""
    from eval.artefacts import ArtefactsConfig

    env = {"EVAL_ARTIFACTS_BUCKET": "b", "EVAL_ARTIFACTS_PREFIX": prefix}
    written = SweepControlConfig.from_env(env).artifact_key(run_id, relative)
    assert written == ArtefactsConfig.from_env(env).object_key(run_id, relative)
    assert written == f"{prefix}/{run_id}/{relative}"
    assert SweepControlConfig.from_env(env).artifact_root(run_id) == f"{prefix}/{run_id}/"


#: First-party packages zipped for the zipimport cell; only their `*.py` files are archived.
_ZIPPED_SRC_PACKAGES = ("crm", "eval", "agents", "providers", "gateway")


def test_the_handler_modules_import_from_a_zip_archive(tmp_path: Path) -> None:
    """`eval.scoring` derives its registry through the package loader, so the handler
    module imports from a zip as it does from a directory."""
    import sys
    import zipfile

    from scripts import proc_run

    repo = Path(__file__).resolve().parents[2]
    archive = tmp_path / "eval_trigger.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for package in _ZIPPED_SRC_PACKAGES:
            for path in sorted((repo / "src" / package).rglob("*.py")):
                bundle.write(path, Path("src") / path.relative_to(repo / "src"))
        for path in sorted((repo / "fixtures").rglob("*.py")):
            bundle.write(path, path.relative_to(repo))
        for path in sorted((repo / "src" / "eval").glob("*.yaml")):
            bundle.write(path, Path("src") / path.relative_to(repo / "src"))
    probe = (
        "import sys; sys.path[:0] = [sys.argv[1] + '/src', sys.argv[1]]\n"
        "import eval.scoring, eval.sfn_handlers\n"
        "assert '.zip' in eval.scoring.__file__, eval.scoring.__file__\n"
        "print(len(eval.scoring._STRUCTURALLY_FALSIFIABLE_FORBIDDEN_OUTCOMES))\n"
    )
    result = proc_run.run(
        [sys.executable, "-c", probe, str(archive)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONSAFEPATH": "1"},
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert int(result.stdout.strip()) == 15


def test_the_eval_turn_errors_are_one_pair_across_the_runtime_and_sweep_task() -> None:
    """`turn_timeout` 504 and `provider_unavailable` 503 live in `crm.errors` outside
    `TAXONOMY`; the Runtime raises exactly these classes and sweep_task maps both codes back."""
    from crm.errors import TAXONOMY
    from eval.live import LiveInvocationError

    assert (TurnTimeout.code, TurnTimeout.http_status) == ("turn_timeout", 504)
    assert (ProviderUnavailable.code, ProviderUnavailable.http_status) == (
        "provider_unavailable",
        503,
    )
    assert not {TurnTimeout, ProviderUnavailable} & set(TAXONOMY)
    assert type(runtime_server._eval_turn_failure(TimeoutError())) is TurnTimeout
    raised = runtime_server._eval_turn_failure(LiveInvocationError("x"))
    assert type(raised) is ProviderUnavailable
    for member in (TurnTimeout, ProviderUnavailable):
        mapped = classify_runtime_failure(status=None, code=member.code, detail="")
        assert type(mapped) is member


def test_a_retried_teardown_keeps_the_delta_checks_of_its_first_attempt(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A teardown that fails after deleting the tenant is retried without re-running the checks on
    the emptied tenant, so the run still completes."""
    refs = sweep.control("start")["task_refs"]
    sweep.control("seed")
    for ref in refs:
        sweep.task(ref["arm_index"], ref["task_id"])
    sweep.control("aggregate", error=None)
    from eval.tenancy import record_run_torn_down as real

    calls = {"n": 0}

    def _fails_once(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise CrmError("planted catalog write failure")
        real(*args, **kwargs)

    monkeypatch.setattr(sfn_handlers, "record_run_torn_down", _fails_once)
    with pytest.raises(CrmError, match="planted"):
        sweep.control("teardown")
    assert sweep.tenant_rows() == 0
    sweep.control("teardown")
    inventory = json.loads(sweep.objects()["verification-inventory/eval_sweep.json"])
    assert {entry["status"] for entry in inventory["checks"]} == {"pass"}
    job_row = sweep.store.get_item("eval_runs", {"run_id": f"job#{sweep.run_id}"})
    assert job_row is not None and job_row["isolation_failures"] == "[]"
    assert sweep.control("finalize", error=None, teardown_error=None)["status"] == "completed"


def test_the_isolation_record_is_written_only_once(sweep: Sweep) -> None:
    """F1: a second write of the delta-check record keeps the first, even when it differs."""
    sweep.control("start")
    sweep.control("seed")
    sweep.store.update_item(
        "eval_runs",
        {"run_id": f"job#{sweep.run_id}"},
        set_values={"isolation_failures": "[]"},
        condition=None,
    )
    sfn_handlers._publish_delta_checks(
        sweep.store,
        sweep.s3,
        CONTROL_CONFIG,
        run_id=sweep.run_id,
        tenant="eval-x",
        unit_tenants=sweep.unit_tenants,
        seeded=True,
    )
    job_row = sweep.store.get_item("eval_runs", {"run_id": f"job#{sweep.run_id}"})
    assert job_row is not None and job_row["isolation_failures"] == "[]"


def test_a_torn_down_catalog_skips_the_delta_checks(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: a teardown that finds the catalog already `torn_down` publishes no new checks."""
    refs = sweep.control("start")["task_refs"]
    sweep.control("seed")
    for ref in refs:
        sweep.task(ref["arm_index"], ref["task_id"])
    sweep.control("teardown")
    job_row = sweep.store.get_item("eval_runs", {"run_id": f"job#{sweep.run_id}"})
    assert job_row is not None
    del job_row["isolation_failures"]
    sweep.store.put_item("eval_runs", job_row)
    assert "isolation_failures" not in (
        sweep.store.get_item("eval_runs", {"run_id": f"job#{sweep.run_id}"}) or {}
    )
    published: list[str] = []

    def _publish(*_args: Any, **_kwargs: Any) -> None:
        published.append("again")

    monkeypatch.setattr(sfn_handlers, "_publish_delta_checks", _publish)
    sweep.control("teardown")
    assert published == []


def test_a_claim_error_at_finalize_still_releases_the_lock_this_execution_holds(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: ownership that cannot be read is re-raised, and the lock naming this run and this
    execution is still released."""
    sweep.control("start")

    def _unreadable(*_args: Any, **_kwargs: Any) -> str:
        raise CrmError("planted ownership read failure")

    monkeypatch.setattr(sfn_handlers, "claim_execution", _unreadable)
    with pytest.raises(CrmError, match="planted ownership"):
        sweep.control("finalize", error=None, teardown_error=None)
    assert read_lock(sweep.store) is None


def test_a_claim_error_at_finalize_keeps_a_lock_another_execution_holds(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    sweep.control("start")

    def _unreadable(*_args: Any, **_kwargs: Any) -> str:
        raise CrmError("planted ownership read failure")

    monkeypatch.setattr(sfn_handlers, "claim_execution", _unreadable)
    with pytest.raises(CrmError, match="planted ownership"):
        sweep.control("finalize", execution_arn=OTHER_EXECUTION, error=None, teardown_error=None)
    lock = read_lock(sweep.store)
    assert lock is not None and lock.execution_arn == sweep.execution_arn


def test_an_unclaimed_run_is_claimed_by_the_first_owned_step_to_reach_it(sweep: Sweep) -> None:
    """With no ARN on either row, Teardown claims the run for its execution and
    proceeds, so a Start that failed before claiming is still cleaned up."""
    assert require_run(sweep.store, sweep.run_id).execution_arn is None
    result = sweep.control("teardown", execution_arn=OTHER_EXECUTION)
    assert result["refused"] is False and result["torn_down"] is True
    assert require_run(sweep.store, sweep.run_id).execution_arn == OTHER_EXECUTION
    lock = read_lock(sweep.store)
    assert lock is not None and lock.execution_arn == OTHER_EXECUTION
    assert sweep.control("start")["refused"] is True


def test_a_claim_refused_between_read_and_write_is_logged_as_lock_changed(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """F6: the refusal names why: here the lock changed between the claim's read and write."""
    import eval.runs as runs_module

    def _lock_changed(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise Conflict("planted: the lock changed")

    monkeypatch.setattr(runs_module, "_write_execution_arn", _lock_changed)
    with caplog.at_level("INFO"):
        assert sweep.control("start")["refused"] is True
    assert "lock_changed:" in caplog.text


def test_a_claim_by_another_execution_is_logged_as_owned_by_other(
    sweep: Sweep, caplog: pytest.LogCaptureFixture
) -> None:
    sweep.control("start")
    with caplog.at_level("INFO"):
        assert sweep.control("teardown", execution_arn=OTHER_EXECUTION)["refused"] is True
    assert "owned_by_other:" in caplog.text


def test_an_arn_only_the_lock_records_is_copied_to_the_job_row(sweep: Sweep) -> None:
    """A claim whose job-row write was lost: the lock's ARN owns the run and is backfilled."""
    sweep.store.update_item(
        "eval_runs",
        {"run_id": "lock#sweep"},
        set_values={"execution_arn": sweep.execution_arn},
        condition=None,
    )
    assert require_run(sweep.store, sweep.run_id).execution_arn is None
    assert sweep.control("start")["refused"] is False
    assert require_run(sweep.store, sweep.run_id).execution_arn == sweep.execution_arn


def test_an_unreadable_ownership_keeps_a_malformed_finalize_events_own_error(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sweep.control("start")

    def _unreadable(*_args: Any, **_kwargs: Any) -> bool:
        raise CrmError("planted ownership read failure")

    monkeypatch.setattr(sfn_handlers, "claim_execution", _unreadable)
    with caplog.at_level("WARNING"), pytest.raises(ValidationFailed):
        sweep.control("finalize", tenant="demo")
    assert "sweep_finalize_lock_retained" in caplog.text
    lock = read_lock(sweep.store)
    assert lock is not None and lock.holder_run_id == sweep.run_id
