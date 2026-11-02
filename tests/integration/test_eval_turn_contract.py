# SPDX-License-Identifier: MIT
"""The sweep_task ↔ Runtime `eval_turn` seam, on DynamoDB Local.

The request is the one `eval.sfn_handlers` builds and the server is the real
`agents.runtime_server` Starlette app with the real `StubProvider`. The Runtime's tool calls reach
the real action handlers on the `_agent_identity` channel through `crm.dispatch.invoke_action`,
whose Lambda client is replaced by an in-process one. A whole sweep through the state machine's
handlers then scores exactly as the in-process harness does for the same arms and tasks.
"""

from __future__ import annotations

import io
import json
import re
from typing import Any

import pytest
from starlette.testclient import TestClient

import agents.runtime_server as runtime_server
import crm.dispatch
from crm.authz import Identity
from crm.errors import CrmError
from crm.store.keys import eval_result_id
from crm.store.store import Store
from crm.toolspec.definitions import ALL_TOOLS
from eval import harness, sfn_handlers
from eval.harness import (
    _DEFAULT_GOLDEN_TASKS_PATH,
    DEFAULT_ARM_SPECS,
    _load_golden_tasks,
)
from eval.matrix import build_matrix_result
from eval.pricing import is_cost_provisional, load_price_table_status
from eval.runs import acquire_lock, create_run, require_run
from eval.sfn_handlers import (
    SweepControlConfig,
    SweepTaskConfig,
    session_id_for,
    sweep_control_handler,
    sweep_task_handler,
)
from eval.task_ids import localize_tasks
from eval.tenancy import (
    allocate_run_id,
    seed_tenant,
    teardown_tenant,
    tenant_for_run,
    unit_tenant,
)
from eval.turn_observation import deserialise_turn, serialise_turn
from providers.capabilities import load_capability_matrix


class InProcessLambda:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._handlers = {tool.name: tool.handler for tool in ALL_TOOLS}

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        import importlib

        module_path, _, attribute = self._handlers[kwargs["FunctionName"]].rpartition(".")
        handler = getattr(importlib.import_module(module_path), attribute)
        result = handler(json.loads(kwargs["Payload"]), store=self._store)
        return {"Payload": io.BytesIO(json.dumps(result).encode("utf-8"))}


class StarletteRuntime:
    """Serves `invoke_agent_runtime` from the real app; a non-200 answer raises as the SDK does."""

    def __init__(self) -> None:
        self.client = TestClient(runtime_server.app)
        self.calls = 0

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        self.calls += 1
        response = self.client.post("/invocations", json=json.loads(kwargs["payload"]))
        if response.status_code != 200:
            raise ClientError(
                {
                    "Error": {
                        "Code": "RuntimeClientError",
                        "Message": (
                            f"Received error ({response.status_code}) from runtime. "
                            "Please check your CloudWatch logs for more information"
                        ),
                    },
                    "ResponseMetadata": {"HTTPStatusCode": 424},
                },
                "InvokeAgentRuntime",
            )
        return {"response": io.BytesIO(response.content), "statusCode": 200}


class MemoryS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs.get("ServerSideEncryption") == "aws:kms"
        self.objects[kwargs["Key"]] = bytes(kwargs["Body"])
        return {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        if kwargs["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}


CONTROL = SweepControlConfig(table_prefix="", artifacts_bucket="b", artifacts_prefix="p")
TASK = SweepTaskConfig(table_prefix="", runtime_arn="arn:runtime", runtime_qualifier="DEFAULT")


@pytest.fixture
def store(dynamodb_local_store: tuple[Store, str], monkeypatch: pytest.MonkeyPatch) -> Store:
    local_store, _prefix = dynamodb_local_store
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("ACTION_LAMBDA_PREFIX", raising=False)
    monkeypatch.setattr(crm.dispatch, "_lambda_client", InProcessLambda(local_store))
    runtime_server._matrix = None
    runtime_server._stub_provider = None
    return local_store


def _request_for(task_index: int, *, tenant: str, arm_index: int = 0) -> dict[str, Any]:
    task = localize_tasks(_load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH), tenant=tenant)[task_index]
    run_id = tenant.removeprefix("eval-")
    return sfn_handlers._eval_turn_request(
        task=task,
        tenant=tenant,
        arm_spec=DEFAULT_ARM_SPECS[arm_index],
        provider_mode="stub",
        session_id=session_id_for(run_id, arm_index, str(task["id"])),
    )


def test_the_runtime_accepts_the_request_sweep_task_builds(store: Store) -> None:
    """The request is accepted (200), the body is exactly `serialise_turn`'s shape, and it
    deserialises to a turn that ran in the request's eval tenant."""
    tenant = tenant_for_run(allocate_run_id())
    seed_tenant(store, tenant=tenant)
    try:
        response = TestClient(runtime_server.app).post(
            "/invocations", json=_request_for(0, tenant=tenant)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        turn = deserialise_turn(body)
        assert body == serialise_turn(turn)
        assert turn.outcome.tenant == tenant
        assert turn.stream_calls >= 1
        assert turn.invocations, "the stub supervisor's invocation is attributed"
    finally:
        teardown_tenant(store, tenant=tenant)


def test_the_runtime_refuses_a_request_outside_an_eval_tenant(store: Store) -> None:
    """A `demo` identity is a 403; through sweep_task's client that is a raised failure."""
    request = _request_for(0, tenant="eval-x")
    request["_agent_identity"]["claims"]["custom:tenant"] = "demo"
    response = TestClient(runtime_server.app).post("/invocations", json=request)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scope_denied"
    with pytest.raises(CrmError, match="did not complete") as raised:
        sfn_handlers._invoke_eval_turn(StarletteRuntime(), TASK, request)
    assert type(raised.value) is CrmError
    assert "Received error (403) from runtime" in str(raised.value.detail)


def test_an_eval_turn_carrying_a_temperature_is_a_400_before_any_provider(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep_task request with a tier `temperature` is a 400 naming it; no provider is built."""

    def _never(provider_mode: str) -> Any:
        raise AssertionError("a refused eval turn must never build a provider")

    monkeypatch.setattr(runtime_server, "_eval_turn_providers", _never)
    request = _request_for(0, tenant="eval-x")
    tier = next(iter(request["tuning"]["tiers"]))
    request["tuning"]["tiers"][tier]["temperature"] = 0.2
    response = TestClient(runtime_server.app).post("/invocations", json=request)
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["source"] == f"tuning.tiers.{tier}.temperature"


def test_an_identity_smuggled_on_another_channel_is_ignored(store: Store) -> None:
    """The Runtime keeps only the `_agent_identity` channel: a planted `_identity` changes nothing."""
    tenant = tenant_for_run(allocate_run_id())
    seed_tenant(store, tenant=tenant)
    try:
        request = _request_for(0, tenant=tenant)
        request["_identity"] = {"rep_id": "rep-intruder", "tenant": "demo", "full_visibility": True}
        response = TestClient(runtime_server.app).post("/invocations", json=request)
        assert response.status_code == 200, response.text
        assert deserialise_turn(response.json()).outcome.tenant == tenant
    finally:
        teardown_tenant(store, tenant=tenant)


def _scores_by_arm(matrix_report: dict[str, Any], tenant: str) -> list[dict[str, Any]]:
    """Each arm's task scores, aggregate and tier summary, with the run's tenant id normalised."""
    pattern = re.compile(re.escape(tenant.removeprefix("eval-")))
    arms = []
    for arm in matrix_report["arms"]:
        rendered = json.dumps(
            {
                "task_scores": arm["task_scores"],
                "aggregate_score": arm["aggregate_score"],
                "tier_summary": arm["tier_summary"],
                "model": arm["provenance"]["model_id"],
            },
            sort_keys=True,
        )
        arms.append(json.loads(pattern.sub("RUN", rendered)))
    return arms


def _in_process_reference_in(store: Store, *, run_id: str) -> dict[str, Any]:
    """Every default arm through the harness's `_run_arm`, each in the run's repeat-0 unit tenant
    for that arm, seeded and then torn down, as `run_sweep` runs one repeat."""
    tasks = _load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH)
    matrix = load_capability_matrix()
    price_status = load_price_table_status()
    units = [
        unit_tenant(run_id, arm_index=arm_index, repeat_index=0)
        for arm_index in range(len(DEFAULT_ARM_SPECS))
    ]
    try:
        arm_results = []
        for arm_index, spec in enumerate(DEFAULT_ARM_SPECS):
            seed_tenant(store, tenant=units[arm_index])
            arm_results.append(
                harness._run_arm(
                    provider=harness._build_provider("stub", family=spec.family),
                    matrix=matrix,
                    arm_spec=spec,
                    tasks=tasks,
                    store=store,
                    mode="stub",
                    dataset_seed=harness.DEFAULT_DATASET_SEED,
                    git_revision=harness._git_revision(),
                    runtime_identifier="local-stub",
                    price_table_version=price_status.price_table_version,
                    cost_withheld=is_cost_provisional(),
                    unit_tenants=[units[arm_index]],
                    arm_index=arm_index,
                ).arm_result
            )
    finally:
        for tenant in units:
            teardown_tenant(store, tenant=tenant)
    report: dict[str, Any] = json.loads(
        json.dumps(build_matrix_result(arm_results).to_report_fields())
    )
    return report


def test_a_state_machine_sweep_scores_exactly_as_the_in_process_harness(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every golden task on both arms through sweep_task -> Runtime -> agent-channel handlers scores
    and aggregates identically to the harness's in-process `_run_arm` path, task for task."""
    monkeypatch.setattr("eval.tenancy._default_seeder", lambda: _seed_corpus)
    run_id = allocate_run_id()
    assert acquire_lock(store, run_id=run_id)
    create_run(
        store,
        run_id=run_id,
        mode="stub",
        identity=Identity(rep_id="rep-admin", full_visibility=True),
        tasks_total=2 * len(_load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH)),
    )
    s3 = MemoryS3()
    runtime = StarletteRuntime()

    def control(step: str, **fields: Any) -> dict[str, Any]:
        if step in {"start", "teardown", "finalize"}:
            fields["execution_arn"] = f"arn:aws:states:us-east-1:123456789012:execution:x:{run_id}"
        return sweep_control_handler(
            {"step": step, "run_id": run_id, **fields}, store=store, s3=s3, config=CONTROL
        )

    refs = control("start")["task_refs"]
    control("seed")
    for ref in refs:
        assert sweep_task_handler(
            {"run_id": run_id, **ref}, store=store, runtime=runtime, config=TASK
        ) == {"item_status": "done"}
    control("aggregate", error=None)
    control("teardown")
    assert control("finalize", error=None, teardown_error=None)["status"] == "completed"
    assert runtime.calls == len(refs) == 2 * len(_load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH))
    machine = require_run(store, run_id).matrix_report
    assert machine is not None

    # The stub digests the prompt, which carries tenant-localized ids, so the reference runs in the
    # SFN's own unit tenants after they are torn down.
    tenant = tenant_for_run(run_id)
    reference = _in_process_reference_in(store, run_id=run_id)
    machine_scores = _scores_by_arm(machine, tenant)
    assert machine_scores == _scores_by_arm(reference, tenant)
    assert any(score["correctness"] for arm in machine_scores for score in arm["task_scores"])


def test_a_runtime_turn_flagged_usage_lower_bound_reaches_the_run_records_matrix(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime observation -> sweep_task row -> aggregate -> the run record's matrix arm.

    The Runtime is the real app; only its observation for one task is marked as a lower bound,
    as a deadline-cut call would mark it.
    """
    monkeypatch.setattr("eval.tenancy._default_seeder", lambda: _seed_corpus)
    real_run_eval_turn = runtime_server._run_eval_turn
    flagged: list[str] = []

    def _flag_first(**kwargs: Any) -> dict[str, Any]:
        body = real_run_eval_turn(**kwargs)
        if not flagged:
            flagged.append(str(kwargs["task_id"]))
            body["usage_lower_bound"] = True
        return body

    monkeypatch.setattr(runtime_server, "_run_eval_turn", _flag_first)
    run_id = allocate_run_id()
    assert acquire_lock(store, run_id=run_id)
    create_run(
        store,
        run_id=run_id,
        mode="stub",
        identity=Identity(rep_id="rep-admin", full_visibility=True),
        tasks_total=2 * len(_load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH)),
    )
    s3 = MemoryS3()
    runtime = StarletteRuntime()

    def control(step: str, **fields: Any) -> dict[str, Any]:
        if step in {"start", "teardown", "finalize"}:
            fields["execution_arn"] = f"arn:aws:states:us-east-1:123456789012:execution:x:{run_id}"
        return sweep_control_handler(
            {"step": step, "run_id": run_id, **fields}, store=store, s3=s3, config=CONTROL
        )

    refs = control("start")["task_refs"]
    control("seed")
    for ref in refs:
        assert sweep_task_handler(
            {"run_id": run_id, **ref}, store=store, runtime=runtime, config=TASK
        ) == {"item_status": "done"}
    control("aggregate", error=None)
    control("teardown")
    assert control("finalize", error=None, teardown_error=None)["status"] == "completed"
    machine = require_run(store, run_id).matrix_report
    assert machine is not None

    published = [arm.get("usage_lower_bound_task_ids") for arm in machine["arms"]]
    assert len(flagged) == 1
    assert sorted(ids for ids in published if ids is not None) == [flagged]
    assert published.count(None) == len(published) - 1


def test_the_runtimes_resolved_efforts_survive_the_stored_row(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime body -> sweep_task's `eval_results` row on DynamoDB Local -> `turn_from_row`
    keeps every tier's resolved effort, exactly as the Runtime reported it."""
    monkeypatch.setattr("eval.tenancy._default_seeder", lambda: _seed_corpus)
    run_id = allocate_run_id()
    assert acquire_lock(store, run_id=run_id)
    create_run(
        store,
        run_id=run_id,
        mode="stub",
        identity=Identity(rep_id="rep-admin", full_visibility=True),
        tasks_total=2 * len(_load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH)),
    )
    s3 = MemoryS3()
    runtime = StarletteRuntime()
    bodies: list[dict[str, Any]] = []
    serve = runtime.client.post

    def _recording_post(url: str, **kwargs: Any) -> Any:
        response = serve(url, **kwargs)
        bodies.append(response.json())
        return response

    monkeypatch.setattr(runtime.client, "post", _recording_post)

    def control(step: str, **fields: Any) -> dict[str, Any]:
        if step in {"start", "teardown", "finalize"}:
            fields["execution_arn"] = f"arn:aws:states:us-east-1:123456789012:execution:x:{run_id}"
        return sweep_control_handler(
            {"step": step, "run_id": run_id, **fields}, store=store, s3=s3, config=CONTROL
        )

    ref = control("start")["task_refs"][0]
    control("seed")
    try:
        assert sweep_task_handler(
            {"run_id": run_id, **ref}, store=store, runtime=runtime, config=TASK
        ) == {"item_status": "done"}
        row = store.get_item(
            "eval_results",
            {"run_id": run_id, "result_id": eval_result_id(ref["arm_index"], ref["task_id"])},
            consistent=True,
        )
        assert row is not None
        [body] = bodies
        reported = deserialise_turn(body).resolved_efforts
        assert reported is not None and set(reported) == {"supervisor", "subagent", "tool"}
        assert sfn_handlers.turn_from_row(row).resolved_efforts == reported
    finally:
        control("teardown")


def _seed_corpus(store: Store, *, tenant: str) -> None:
    from fixtures.generate import seed_tenant as generate

    generate(store, tenant=tenant)
