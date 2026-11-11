# SPDX-License-Identifier: MIT
"""Contract between `infra/templates/eval_sweep.asl.json` and `eval.sfn_handlers`.

Both sides are read: each Task's `Payload` keys equal the keys its handler accepts, each
`ResultSelector` reads only keys the handler returns, the Map's `ItemSelector` reads the refs'
keys, and `infra/eval_sfn.tf`'s handler strings name the two entry points. A graph walk shows every
path passes Teardown then Finalize. Then a small interpreter of the template's own states runs the
real handlers against DynamoDB Local, with a fault planted in each state in turn, and shows the run
reaches a terminal status with the lock released on every path. A second execution for a live or
finished run leaves its record, tenant, lock and artefacts untouched; a run with no record seeds
nothing and leaves the lock as it was; and the execution input's mode and tenant are ignored in
favour of the record's. A run of several repeats runs every ``(arm, repeat, task)`` item in its own
unit tenant, and the state document stays far below the Step Functions payload limit at the largest
sweep the trigger admits.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest
from botocore.exceptions import ClientError
from starlette.testclient import TestClient

import agents.runtime_server as runtime_server
import crm.dispatch
from crm.authz import Identity
from crm.errors import Conflict
from crm.store.schema import TENANT_SCOPED_TABLES
from crm.store.store import Store
from crm.toolspec.definitions import ALL_TOOLS
from eval import sfn_handlers
from eval.harness import DEFAULT_ARM_SPECS
from eval.runs import (
    MAX_SWEEP_REPEATS,
    acquire_lock,
    create_run,
    job_row_id,
    read_lock,
    release_lock,
    require_run,
    result_id,
    set_execution_arn,
)
from eval.sfn_handlers import (
    FINALIZE_RESULT_KEYS,
    START_RESULT_KEYS,
    STEP_EVENT_KEYS,
    SWEEP_TASK_EVENT_KEYS,
    SWEEP_TASK_RESULT_KEYS,
    TASK_REF_KEYS,
    SweepControlConfig,
    SweepTaskConfig,
    sweep_control_handler,
    sweep_task_handler,
)
from eval.tenancy import allocate_run_id, tenant_for_run, unit_tenant, unit_tenants_for_run

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
TEMPLATE: Final[Path] = REPO_ROOT / "infra" / "templates" / "eval_sweep.asl.json"
EVAL_SFN_TF: Final[Path] = REPO_ROOT / "infra" / "eval_sfn.tf"
CONTROL_ARN: Final[str] = "arn:aws:lambda:us-east-1:123456789012:function:sweep-control"
TASK_ARN: Final[str] = "arn:aws:lambda:us-east-1:123456789012:function:sweep-task"
BUCKET: Final[str] = "artifacts"
CONTROL_CONFIG = SweepControlConfig(table_prefix="", artifacts_bucket=BUCKET, artifacts_prefix="p")
TASK_CONFIG = SweepTaskConfig(
    table_prefix="", runtime_arn="arn:runtime", runtime_qualifier="DEFAULT"
)
REFUSAL_STATE: Final[str] = "NotThisRunsExecution"

#: Step Functions' limit on a state's input or output, in bytes.
SFN_PAYLOAD_LIMIT_BYTES: Final[int] = 256 * 1024


def _definition() -> dict[str, Any]:
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("${sweep_control_arn}", CONTROL_ARN).replace("${sweep_task_arn}", TASK_ARN)
    document = json.loads(text)
    assert isinstance(document, dict)
    return document


def _all_states(states: Mapping[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for name, state in states.items():
        yield name, state
        processor = state.get("ItemProcessor")
        if processor is not None:
            yield from _all_states(processor["States"])


def _lambda_tasks() -> dict[str, tuple[str, dict[str, Any], dict[str, Any]]]:
    """`{state: (FunctionName, Payload, ResultSelector)}` for every Lambda Task in the template."""
    found = {}
    for name, state in _all_states(_definition()["States"]):
        if state["Type"] == "Task":
            assert state["Resource"] == "arn:aws:states:::lambda:invoke", name
            parameters = state["Parameters"]
            found[name] = (
                parameters["FunctionName"],
                parameters["Payload"],
                state.get("ResultSelector", {}),
            )
    return found


def _keys(payload: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(key.removesuffix(".$") for key in payload)


# -- both sides of the payload contract ----------------------------------------------------------


def test_every_control_payload_carries_exactly_the_keys_its_step_accepts() -> None:
    control_tasks = {
        name: payload
        for name, (function, payload, _selector) in _lambda_tasks().items()
        if function == CONTROL_ARN
    }
    steps = {payload["step"] for payload in control_tasks.values()}
    assert steps == set(STEP_EVENT_KEYS), "every handler step is reachable, and nothing else"
    for name, payload in control_tasks.items():
        expected = STEP_EVENT_KEYS[payload["step"]]
        assert _keys(payload) == expected.accepted, name


def test_the_item_payload_carries_exactly_the_keys_sweep_task_accepts() -> None:
    items = [p for f, p, _s in _lambda_tasks().values() if f == TASK_ARN]
    assert len(items) == 1
    assert _keys(items[0]) == SWEEP_TASK_EVENT_KEYS.accepted


def test_every_result_selector_reads_only_what_its_handler_returns() -> None:
    tasks = _lambda_tasks()
    start_reads = {path.removeprefix("$.Payload.") for path in tasks["Start"][2].values()}
    assert start_reads == START_RESULT_KEYS
    item_reads = {path.removeprefix("$.Payload.") for path in tasks["RunTask"][2].values()}
    assert item_reads == SWEEP_TASK_RESULT_KEYS
    finalize_reads = {path.removeprefix("$.Payload.") for path in tasks["Finalize"][2].values()}
    assert finalize_reads == {"refused"} and finalize_reads <= FINALIZE_RESULT_KEYS
    assert all(
        not selector
        for name, (_f, _p, selector) in tasks.items()
        if name not in {"Start", "RunTask", "Finalize"}
    )


def test_every_owned_step_names_the_execution() -> None:
    """Start, Seed, Aggregate, Teardown and Finalize carry `$$.Execution.Id`."""
    tasks = _lambda_tasks()
    for name in ("Start", "Seed", "Aggregate", "Teardown", "Finalize"):
        assert tasks[name][1]["execution_arn.$"] == "$$.Execution.Id", name


def test_the_map_reads_the_ref_keys_start_returns() -> None:
    selector = _definition()["States"]["RunTasks"]["ItemSelector"]
    read = {
        value.removeprefix("$$.Map.Item.Value.")
        for value in selector.values()
        if value.startswith("$$.Map.Item.Value.")
    }
    assert read == TASK_REF_KEYS
    assert _definition()["States"]["RunTasks"]["ItemsPath"] == "$.start.task_refs"


#: Headroom in the whole-run timeout for Start, Seed, Aggregate, Teardown and Finalize.
CONTROL_STEPS_HEADROOM_SECONDS: Final[int] = 3600

#: Step Functions' longest Standard execution, in seconds.
SFN_MAX_EXECUTION_SECONDS: Final[int] = 365 * 24 * 3600


def test_the_whole_run_timeout_fits_the_largest_sweep_with_every_item_at_its_own_timeout() -> None:
    """Every arm x `MAX_SWEEP_REPEATS` x golden task item may run to its `RunTask` timeout and the
    control steps still have their headroom before the execution times out."""
    from eval.harness import _DEFAULT_GOLDEN_TASKS_PATH, _load_golden_tasks

    definition = _definition()
    items = (
        len(DEFAULT_ARM_SPECS)
        * MAX_SWEEP_REPEATS
        * len(_load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH))
    )
    run_task = definition["States"]["RunTasks"]["ItemProcessor"]["States"]["RunTask"]
    needed = items * int(run_task["TimeoutSeconds"]) + CONTROL_STEPS_HEADROOM_SECONDS
    assert needed <= int(definition["TimeoutSeconds"]) <= SFN_MAX_EXECUTION_SECONDS, needed


def test_the_state_document_stays_far_below_the_payload_limit_at_the_largest_sweep() -> None:
    """At every arm x `MAX_SWEEP_REPEATS` x golden task, with the longest task id, the document
    after Start (the Map's input) is under a quarter of the limit, and the Map and each item keep no
    per-item result in it."""
    from eval.harness import _DEFAULT_GOLDEN_TASKS_PATH, _load_golden_tasks
    from eval.sweep_units import task_refs

    tasks = _load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH)
    longest = max((str(task["id"]) for task in tasks), key=len)
    refs = task_refs(
        [{**task, "id": longest} for task in tasks], DEFAULT_ARM_SPECS, MAX_SWEEP_REPEATS
    )
    assert len(refs) == len(DEFAULT_ARM_SPECS) * MAX_SWEEP_REPEATS * len(tasks)
    document = {
        "run_id": "f" * 32,
        "error": {"Error": "X" * 64, "Cause": "y" * 1024},
        "teardown_error": None,
        "start": {
            "refused": False,
            "cancelled_before_start": False,
            "task_refs": [
                {"arm_index": r.arm_index, "repeat_index": r.repeat_index, "task_id": r.task_id}
                for r in refs
            ],
        },
    }
    size = len(json.dumps(document).encode("utf-8"))
    assert size < SFN_PAYLOAD_LIMIT_BYTES // 4, size
    run_tasks = _definition()["States"]["RunTasks"]
    assert run_tasks["ResultPath"] is None
    assert run_tasks["MaxConcurrency"] == 1
    item_states = run_tasks["ItemProcessor"]["States"]
    assert item_states["RecordItemFailure"]["ResultPath"] is None
    assert set(item_states["RunTask"]["ResultSelector"]) == {"item_status.$"}


def test_the_composition_names_the_two_handler_entry_points() -> None:
    handlers = set(re.findall(r'handler\s*=\s*"([^"]+)"', EVAL_SFN_TF.read_text(encoding="utf-8")))
    assert handlers == {
        "eval.sfn_handlers.sweep_control_handler",
        "eval.sfn_handlers.sweep_task_handler",
    }
    for handler in handlers:
        assert callable(getattr(sfn_handlers, handler.rsplit(".", 1)[1]))


def _successors(state: Mapping[str, Any]) -> list[str]:
    targets = [state[key] for key in ("Next", "Default") if key in state]
    targets += [choice["Next"] for choice in state.get("Choices", [])]
    targets += [catch["Next"] for catch in state.get("Catch", [])]
    return targets


def _paths(states: Mapping[str, Any], start: str) -> list[list[str]]:
    paths = []
    stack = [[start]]
    while stack:
        path = stack.pop()
        state = states[path[-1]]
        if state["Type"] in {"Succeed", "Fail"} or state.get("End"):
            paths.append(path)
            continue
        for target in _successors(state):
            assert target not in path, f"cycle through {target}"
            stack.append([*path, target])
    return paths


def test_every_path_passes_teardown_then_finalize_unless_start_was_refused() -> None:
    """A refused Start ends at the Fail state having run nothing after it; every other path ends
    Teardown, Finalize, IsFinalized, then Done or the Fail state."""
    definition = _definition()
    paths = _paths(definition["States"], definition["StartAt"])
    refused = [p for p in paths if "Teardown" not in p]
    assert refused == [["Init", "Start", "IsRefused", REFUSAL_STATE]], refused
    owned = [p for p in paths if "Teardown" in p]
    assert len(owned) > 1
    for path in owned:
        assert path[-4:-1] == ["Teardown", "Finalize", "IsFinalized"], path
        assert path[-1] in {"Done", REFUSAL_STATE}, path
    assert {p[-1] for p in owned} == {"Done", REFUSAL_STATE}
    assert definition["States"][REFUSAL_STATE]["Type"] == "Fail"


def test_the_path_walk_detects_a_refusal_that_reaches_teardown() -> None:
    """Control: routing IsRefused's refusal to Teardown must red the walk."""
    definition = _definition()
    definition["States"]["IsRefused"]["Choices"][0]["Next"] = "Teardown"
    refused = [
        p for p in _paths(definition["States"], definition["StartAt"]) if "Teardown" not in p
    ]
    assert refused == []


# -- the template, executed ----------------------------------------------------------------------


class PlantedFailureError(Exception):
    """A fault injected into one state's Lambda invocation."""


@dataclass
class MemoryS3:
    """An object store refusing any write without SSE-KMS, as the bucket policy does."""

    objects: dict[str, bytes] = field(default_factory=dict)

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["Bucket"] == BUCKET
        if kwargs.get("ServerSideEncryption") != "aws:kms":
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
        self.objects[kwargs["Key"]] = bytes(kwargs["Body"])
        return {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}


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


@dataclass
class StarletteRuntime:
    client: TestClient
    requests: list[dict[str, Any]] = field(default_factory=list)

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        request = json.loads(kwargs["payload"])
        self.requests.append(request)
        response = self.client.post("/invocations", json=request)
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


class StepFailedError(Exception):
    def __init__(self, error: dict[str, str]) -> None:
        super().__init__(error["Error"])
        self.error = error


class ExecutionFailedError(Exception):
    """The execution reached a Fail state."""

    def __init__(self, state: str, error: str) -> None:
        super().__init__(f"{state}: {error}")
        self.state = state
        self.error = error


def _path_value(document: Any, context: Mapping[str, Any], path: str) -> Any:
    root, rest = (context, path[2:]) if path.startswith("$$") else (document, path[1:])
    value = root
    for part in [p for p in rest.split(".") if p]:
        value = value[part]
    return value


def _parameters(template: Any, document: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(template, dict):
        return {
            (key[:-2] if key.endswith(".$") else key): (
                _path_value(document, context, value)
                if key.endswith(".$")
                else _parameters(value, document, context)
            )
            for key, value in template.items()
        }
    return template


def _set_path(document: dict[str, Any], path: str | None, value: Any) -> dict[str, Any]:
    if path is None:
        return document
    if path == "$":
        assert isinstance(value, dict)
        return value
    assert path.startswith("$.") and "." not in path[2:], f"unsupported ResultPath {path}"
    return {**document, path[2:]: value}


@dataclass
class Interpreter:
    """Executes the template's own states: Pass, Task (lambda:invoke), Choice, Map, Succeed, Fail."""

    invoke: Callable[[str, dict[str, Any]], dict[str, Any]]
    visited: list[str] = field(default_factory=list)

    def run(
        self, definition: Mapping[str, Any], document: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        states = definition["States"]
        name = definition["StartAt"]
        while True:
            self.visited.append(name)
            state = states[name]
            kind = state["Type"]
            if kind == "Succeed":
                return document
            if kind == "Fail":
                raise ExecutionFailedError(name, state["Error"])
            if kind == "Pass":
                document = _parameters(state.get("Parameters", document), document, context)
            elif kind == "Choice":
                name = next(
                    (
                        choice["Next"]
                        for choice in state["Choices"]
                        if _path_value(document, context, choice["Variable"])
                        == choice["BooleanEquals"]
                    ),
                    state["Default"],
                )
                continue
            elif kind in {"Task", "Map"}:
                try:
                    result = (
                        self._task(state, document, context)
                        if kind == "Task"
                        else self._map(state, document, context)
                    )
                except StepFailedError as failure:
                    catch = next(
                        (
                            c
                            for c in state.get("Catch", [])
                            if self._matches(c["ErrorEquals"], failure.error)
                        ),
                        None,
                    )
                    if catch is None:
                        raise
                    document = _set_path(document, catch.get("ResultPath", "$"), failure.error)
                    name = catch["Next"]
                    continue
                document = _set_path(document, state.get("ResultPath", "$"), result)
            else:
                raise AssertionError(f"the interpreter does not model state type {kind!r}")
            if state.get("End"):
                return document
            name = state["Next"]

    @staticmethod
    def _matches(errors: list[str], error: Mapping[str, str]) -> bool:
        return "States.ALL" in errors or error["Error"] in errors

    def _task(self, state: Mapping[str, Any], document: Any, context: Mapping[str, Any]) -> Any:
        parameters = _parameters(state["Parameters"], document, context)
        attempts = 1 + max((r["MaxAttempts"] for r in state.get("Retry", [])), default=0)
        for attempt in range(attempts):
            try:
                payload = self.invoke(parameters["FunctionName"], parameters["Payload"])
            except Exception as exc:  # noqa: BLE001 - Lambda function error, to the Retry/Catch
                error = {
                    "Error": type(exc).__name__,
                    "Cause": json.dumps(
                        {"errorMessage": str(exc), "errorType": type(exc).__name__}
                    ),
                }
                retryable = any(
                    self._matches(r["ErrorEquals"], error) and attempt < r["MaxAttempts"]
                    for r in state.get("Retry", [])
                )
                if not retryable:
                    raise StepFailedError(error) from exc
                continue
            raw = {"Payload": payload, "StatusCode": 200}
            selector = state.get("ResultSelector")
            return raw if selector is None else _parameters(selector, raw, context)
        raise AssertionError("unreachable")

    def _map(self, state: Mapping[str, Any], document: Any, context: dict[str, Any]) -> Any:
        assert state["MaxConcurrency"] == 1
        for item in _path_value(document, context, state["ItemsPath"]):
            item_context = {**context, "Map": {"Item": {"Value": item}}}
            item_document = _parameters(state["ItemSelector"], document, item_context)
            self.run(state["ItemProcessor"], item_document, item_context)
        return None


@dataclass
class Deployment:
    store: Store
    s3: MemoryS3
    runtime: StarletteRuntime
    run_id: str
    fail_in: frozenset[str] = frozenset()
    invocations: list[tuple[str, str]] = field(default_factory=list)
    before_item: Callable[[int], None] | None = None
    before_step: Callable[[str], None] | None = None

    def invoke(self, function: str, payload: dict[str, Any]) -> dict[str, Any]:
        step = payload.get("step", "item")
        self.invocations.append((function, step))
        if self.before_step is not None:
            self.before_step(step)
        if step == "item" and self.before_item is not None:
            self.before_item(sum(1 for _f, s in self.invocations if s == "item") - 1)
        if step in self.fail_in:
            raise PlantedFailureError(f"planted failure in {step}")
        if function == CONTROL_ARN:
            return sweep_control_handler(
                payload, store=self.store, s3=self.s3, config=CONTROL_CONFIG
            )
        assert function == TASK_ARN
        return sweep_task_handler(
            payload, store=self.store, runtime=self.runtime, config=TASK_CONFIG
        )

    def execute(
        self,
        *,
        execution_name: str | None = None,
        run_id: str | None = None,
        overrides: Mapping[str, Any] | None = None,
        interpreter: Interpreter | None = None,
    ) -> Interpreter:
        """Run the template once; `run_id` and `overrides` change the execution input only."""
        interpreter = interpreter or Interpreter(self.invoke)
        target = run_id or self.run_id
        execution_input = {
            "run_id": target,
            "tenant": tenant_for_run(target),
            "mode": "stub",
            "provider_mode": "stub",
            **dict(overrides or {}),
        }
        name = execution_name or target
        context = {"Execution": {"Id": f"arn:aws:states:us-east-1:123456789012:execution:x:{name}"}}
        interpreter.run(_definition(), execution_input, context)
        return interpreter

    def execute_refused(self, **kwargs: Any) -> Interpreter:
        """Run the template expecting it to end at the refusal Fail state; returns the interpreter."""
        interpreter = Interpreter(self.invoke)
        with pytest.raises(ExecutionFailedError) as failed:
            self.execute(interpreter=interpreter, **kwargs)
        assert failed.value.state == REFUSAL_STATE
        assert failed.value.error == "SweepExecutionRefused"
        return interpreter


@pytest.fixture
def deployment(
    dynamodb_local_store: tuple[Store, str],
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Deployment:
    from eval.harness import _load_golden_tasks

    store, _prefix = dynamodb_local_store
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("ACTION_LAMBDA_PREFIX", raising=False)
    monkeypatch.setattr(crm.dispatch, "_lambda_client", InProcessLambda(store))
    monkeypatch.setattr(
        sfn_handlers, "_load_golden_tasks", lambda path: _load_golden_tasks(path)[:2]
    )
    runtime_server._matrix = None
    runtime_server._stub_provider = None
    repeats = int(getattr(request, "param", 1))
    run_id = allocate_run_id()
    assert acquire_lock(store, run_id=run_id)
    create_run(
        store,
        run_id=run_id,
        mode="stub",
        identity=Identity(rep_id="rep-admin", full_visibility=True),
        tasks_total=2 * repeats * len(DEFAULT_ARM_SPECS),
        repeats=repeats,
    )
    return Deployment(
        store=store,
        s3=MemoryS3(),
        runtime=StarletteRuntime(TestClient(runtime_server.app)),
        run_id=run_id,
    )


def _rows(store: Store, tenant: str) -> int:
    return sum(len(store.query_tenant_scan(t, tenant=tenant)[0]) for t in TENANT_SCOPED_TABLES)


def _tenant_rows(store: Store, tenant: str, *, repeats: int = 1) -> int:
    """Rows under the run tenant ``tenant`` and under every unit tenant of its run."""
    units = unit_tenants_for_run(
        tenant.removeprefix("eval-"), arm_count=len(DEFAULT_ARM_SPECS), repeats=repeats
    )
    return sum(_rows(store, each) for each in (tenant, *units))


def test_the_template_completes_a_stub_sweep(deployment: Deployment) -> None:
    interpreter = deployment.execute()
    record = require_run(deployment.store, deployment.run_id)
    assert interpreter.visited[-4:] == ["Teardown", "Finalize", "IsFinalized", "Done"]
    assert record.status == "completed" and record.tasks_done == 4
    assert read_lock(deployment.store) is None
    assert _tenant_rows(deployment.store, record.tenant) == 0
    assert f"p/{deployment.run_id}/matrix.json" in deployment.s3.objects


def test_every_turn_the_template_sends_declares_its_actors_principal_type(
    deployment: Deployment,
) -> None:
    """Through the template, each Runtime request carries the type its golden actor declares."""
    from eval.actor import actor_principal_type
    from eval.harness import _DEFAULT_GOLDEN_TASKS_PATH, _load_golden_tasks

    declared = {
        str(task["id"]): actor_principal_type(task["actor"])  # type: ignore[arg-type]
        for task in _load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH)
    }
    deployment.execute()
    requests = deployment.runtime.requests
    assert requests
    for request in requests:
        assert request["_agent_identity"]["principal_type"] == declared[request["task_id"]]


@pytest.mark.parametrize(
    ("fail_in", "expected_status", "teardown_status"),
    [
        ({"start"}, "failed", "ok"),
        ({"seed"}, "failed", "ok"),
        ({"item"}, "failed", "ok"),
        # RecordItemFailure exhausts its retries, so the Map-level Catch routes to Aggregate.
        ({"item", "item_failed"}, "failed", "ok"),
        ({"aggregate"}, "failed", "ok"),
        ({"teardown"}, "failed", "failed"),
    ],
)
def test_a_failure_in_any_state_still_tears_down_finalizes_and_releases_the_lock(
    deployment: Deployment, fail_in: set[str], expected_status: str, teardown_status: str
) -> None:
    """Whichever step fails, Teardown then Finalize run, the run is terminal, the
    lock is released, and (unless teardown itself failed) the tenant is empty."""
    deployment.fail_in = frozenset(fail_in)
    interpreter = deployment.execute()
    record = require_run(deployment.store, deployment.run_id)
    assert interpreter.visited[-4:] == ["Teardown", "Finalize", "IsFinalized", "Done"], (
        interpreter.visited
    )
    assert record.status == expected_status
    assert record.teardown_status == teardown_status
    assert record.failure_summary
    assert read_lock(deployment.store) is None
    if "teardown" not in fail_in:
        assert _tenant_rows(deployment.store, record.tenant) == 0


def test_a_failed_item_routes_to_record_item_failure_and_the_sweep_continues(
    deployment: Deployment,
) -> None:
    """One item's failure is recorded `failed` by RecordItemFailure; the later items still run."""
    real = deployment.invoke
    calls = {"item": 0}

    def _fail_first_item(function: str, payload: dict[str, Any]) -> dict[str, Any]:
        if function == TASK_ARN:
            calls["item"] += 1
            if calls["item"] == 1:
                raise PlantedFailureError("first item fails")
        return real(function, payload)

    deployment.invoke = _fail_first_item  # type: ignore[method-assign]
    deployment.execute()
    record = require_run(deployment.store, deployment.run_id)
    assert (record.tasks_done, record.tasks_failed) == (3, 1)
    assert record.status == "failed"
    assert record.matrix_report is not None


def _result_rows(deployment: Deployment) -> list[dict[str, Any]]:
    record = require_run(deployment.store, deployment.run_id)
    rows = [
        deployment.store.get_item(
            "eval_results",
            {
                "run_id": deployment.run_id,
                "result_id": result_id(ref.arm_index, ref.repeat_index, ref.task_id),
            },
        )
        for ref in sfn_handlers._sweep_refs(record)
    ]
    return [dict(row) for row in rows if row is not None]


def test_a_runtime_turn_timeout_is_recorded_as_a_turn_timeout_item(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real app's 504 crosses AgentCore as a fixed-message 424 and is still recorded
    `TurnTimeout`, not a generic failure. Its cost is recorded as unmeasured."""

    def _timed_out(**_: Any) -> dict[str, Any]:
        raise TimeoutError

    monkeypatch.setattr(runtime_server, "_run_eval_turn", _timed_out)
    deployment.execute()
    record = require_run(deployment.store, deployment.run_id)
    assert record.tasks_failed == 4
    rows = _result_rows(deployment)
    errors = [str(row["error"]) for row in rows]
    assert len(errors) == 4
    assert all(error.startswith("TurnTimeout:") for error in errors), errors
    assert [row["cost_measured"] for row in rows] == [False] * 4


def test_a_completed_sweep_records_no_cost_measured_field(deployment: Deployment) -> None:
    """A done item's cost is measured, so its row carries no `cost_measured`."""
    deployment.execute()
    rows = _result_rows(deployment)
    assert [row["item_status"] for row in rows] == ["done"] * 4
    assert all("cost_measured" not in row for row in rows)


def test_a_run_with_a_failed_item_publishes_the_cost_qualifier(deployment: Deployment) -> None:
    """Aggregate counts the recorded failure into matrix.md's cost qualifier."""
    real = deployment.invoke
    calls = {"item": 0}

    def _fail_first_item(function: str, payload: dict[str, Any]) -> dict[str, Any]:
        if function == TASK_ARN:
            calls["item"] += 1
            if calls["item"] == 1:
                raise PlantedFailureError("first item fails")
        return real(function, payload)

    deployment.invoke = _fail_first_item  # type: ignore[method-assign]
    deployment.execute()
    matrix_md = deployment.s3.objects[f"p/{deployment.run_id}/matrix.md"].decode("utf-8")
    assert "Cost excludes 1 item(s) that ended in an error" in matrix_md


def _snapshot(deployment: Deployment) -> tuple[Any, ...]:
    record = require_run(deployment.store, deployment.run_id)
    return (
        record,
        _tenant_rows(deployment.store, record.tenant),
        read_lock(deployment.store),
        dict(deployment.s3.objects),
    )


def test_a_second_execution_for_a_live_run_is_refused_and_changes_nothing(
    deployment: Deployment,
) -> None:
    """While execution A is mid-sweep, execution B for the same run ends at the refusal
    state having run only Start; A's tenant, status, lock and artefacts are untouched, and A then
    completes."""
    observed: dict[str, Any] = {}

    def _second_execution(index: int) -> None:
        if index != 1:
            return
        observed["before"] = _snapshot(deployment)
        calls_before = len(deployment.invocations)
        refused = deployment.execute_refused(execution_name="second")
        observed["visited"] = refused.visited
        observed["steps"] = [step for _f, step in deployment.invocations[calls_before:]]
        observed["after"] = _snapshot(deployment)

    deployment.before_item = _second_execution
    deployment.execute()
    assert observed["visited"] == ["Init", "Start", "IsRefused", REFUSAL_STATE]
    assert observed["steps"] == ["start"]
    before, after = observed["before"], observed["after"]
    assert after == before
    assert before[0].status == "running" and before[1] > 0
    assert before[2] is not None and before[2].holder_run_id == deployment.run_id
    record = require_run(deployment.store, deployment.run_id)
    assert record.status == "completed" and record.tasks_done == 4
    assert read_lock(deployment.store) is None


def _hand_run_to_another_execution_before(deployment: Deployment, target: str) -> None:
    """Before `target` is invoked, record another execution as the run's owner on the job row."""

    def _reassign(step: str) -> None:
        if step == target:
            deployment.store.update_item(
                "eval_runs",
                {"run_id": job_row_id(deployment.run_id)},
                set_values={"execution_arn": "arn:aws:states:us-east-1:123456789012:execution:x:b"},
                condition=None,
            )

    deployment.before_step = _reassign


def _steps(deployment: Deployment) -> list[str]:
    return [step for _f, step in deployment.invocations]


def test_a_seed_by_an_execution_that_no_longer_owns_the_run_seeds_nothing(
    deployment: Deployment,
) -> None:
    """Seed refuses without retry, Teardown and Finalize refuse, and the execution ends at the
    refusal state with no catalog row, no unit tenant row and no turn sent."""
    _hand_run_to_another_execution_before(deployment, "seed")
    refused = deployment.execute_refused()
    assert refused.visited[-6:] == [
        "IsCancelled",
        "Seed",
        "Teardown",
        "Finalize",
        "IsFinalized",
        REFUSAL_STATE,
    ]
    assert _steps(deployment) == ["start", "seed", "teardown", "finalize"]
    assert deployment.store.get_item("eval_runs", {"run_id": deployment.run_id}) is None
    assert _tenant_rows(deployment.store, tenant_for_run(deployment.run_id)) == 0
    assert deployment.runtime.requests == []


def test_an_aggregate_by_an_execution_that_no_longer_owns_the_run_publishes_nothing(
    deployment: Deployment,
) -> None:
    """Aggregate refuses without retry and ends at the refusal state: no matrix artefact and no
    `matrix_report` or `artifact_links` on the job row."""
    _hand_run_to_another_execution_before(deployment, "aggregate")
    refused = deployment.execute_refused()
    assert refused.visited[-5:] == [
        "Aggregate",
        "Teardown",
        "Finalize",
        "IsFinalized",
        REFUSAL_STATE,
    ]
    assert _steps(deployment).count("aggregate") == 1
    assert not any(key.endswith("/matrix.json") for key in deployment.s3.objects)
    job = deployment.store.get_item("eval_runs", {"run_id": job_row_id(deployment.run_id)})
    assert job is not None and "matrix_report" not in job and "artifact_links" not in job


def test_an_execution_for_a_terminal_run_reruns_nothing(deployment: Deployment) -> None:
    """A start on a finished run is refused: no Aggregate or Teardown, artefacts not rewritten."""
    deployment.execute()
    before = _snapshot(deployment)
    calls_before = len(deployment.invocations)
    refused = deployment.execute_refused(execution_name="again")
    assert refused.visited == ["Init", "Start", "IsRefused", REFUSAL_STATE]
    assert [step for _f, step in deployment.invocations[calls_before:]] == ["start"]
    assert _snapshot(deployment) == before


def test_an_execution_for_a_run_with_no_record_seeds_nothing(deployment: Deployment) -> None:
    """No job row means Start fails, nothing is seeded, and Teardown and Finalize refuse, so
    the lock another run holds is unchanged and no row is created for the ghost run."""
    ghost = allocate_run_id()
    lock_before = read_lock(deployment.store)
    refused = deployment.execute_refused(run_id=ghost)
    assert "Seed" not in refused.visited and "RunTasks" not in refused.visited
    assert refused.visited[-4:] == ["Teardown", "Finalize", "IsFinalized", REFUSAL_STATE]
    assert _tenant_rows(deployment.store, tenant_for_run(ghost)) == 0
    assert deployment.store.get_item("eval_runs", {"run_id": f"job#{ghost}"}) is None
    assert read_lock(deployment.store) == lock_before
    assert deployment.runtime.requests == []


def test_a_live_execution_input_on_a_stub_record_runs_stub_under_the_runs_tenant(
    deployment: Deployment,
) -> None:
    """Mode, provider mode and tenant come from the record, never the execution input."""
    deployment.execute(overrides={"mode": "live", "provider_mode": "live", "tenant": "demo"})
    requests = deployment.runtime.requests
    assert len(requests) == 4
    units = {
        unit_tenant(deployment.run_id, arm_index=arm, repeat_index=0)
        for arm in range(len(DEFAULT_ARM_SPECS))
    }
    for request in requests:
        assert request["provider_mode"] == "stub"
        assert request["_agent_identity"]["claims"]["custom:tenant"] in units
    assert require_run(deployment.store, deployment.run_id).status == "completed"


@pytest.mark.parametrize("deployment", [2], indirect=True, ids=["repeats-2"])
def test_a_two_repeat_execution_runs_every_item_in_its_unit_tenant(
    deployment: Deployment,
) -> None:
    """Through the template: every (arm, repeat, task) item is done, each turn names its own unit
    tenant, every unit tenant is recorded, seeded and then emptied, and the run tenant holds no
    row while the items run."""
    unit_rows: dict[str, int] = {}
    run_tenant_rows: list[int] = []

    def _count_units(index: int) -> None:
        if index == 0:
            run_tenant_rows.append(_rows(deployment.store, tenant_for_run(deployment.run_id)))
            for unit in unit_tenants_for_run(
                deployment.run_id, arm_count=len(DEFAULT_ARM_SPECS), repeats=2
            ):
                unit_rows[unit] = _rows(deployment.store, unit)

    deployment.before_item = _count_units
    deployment.execute()
    record = require_run(deployment.store, deployment.run_id)
    assert record.status == "completed"
    assert (record.repeats, record.tasks_done) == (2, 2 * 2 * len(DEFAULT_ARM_SPECS))
    rows = _result_rows(deployment)
    assert len(rows) == record.tasks_total
    assert {(row["arm"], row["repeat_index"]) for row in rows} == {
        (spec.family, repeat) for spec in DEFAULT_ARM_SPECS for repeat in (0, 1)
    }
    seen = [
        request["_agent_identity"]["claims"]["custom:tenant"]
        for request in deployment.runtime.requests
    ]
    expected = [
        unit_tenant(deployment.run_id, arm_index=ref.arm_index, repeat_index=ref.repeat_index)
        for ref in sfn_handlers._sweep_refs(record)
    ]
    assert seen == expected
    assert len(unit_rows) == 2 * len(DEFAULT_ARM_SPECS)
    assert all(count > 0 for count in unit_rows.values()), unit_rows
    assert run_tenant_rows == [0]
    assert _tenant_rows(deployment.store, record.tenant, repeats=2) == 0
    catalog = deployment.store.get_item("eval_runs", {"run_id": deployment.run_id})
    assert catalog is not None and catalog["status"] == "torn_down"
    assert catalog["unit_tenants"] == list(unit_rows)


def test_a_refused_execution_arn_write_changes_neither_row_on_dynamodb_local(
    deployment: Deployment,
) -> None:
    """On the real condition grammar: a lock naming another run, or a job row recording another
    execution, refuses the write and leaves both rows as they were."""
    store, run_id = deployment.store, deployment.run_id
    first = "arn:aws:states:us-east-1:123456789012:execution:x:first"
    set_execution_arn(store, run_id=run_id, execution_arn=first)
    with pytest.raises(Conflict):
        set_execution_arn(store, run_id=run_id, execution_arn=f"{first}-again")
    lock = read_lock(store)
    assert lock is not None and lock.execution_arn == first
    assert require_run(store, run_id).execution_arn == first

    other = allocate_run_id()
    assert release_lock(store, run_id=run_id)
    assert acquire_lock(store, run_id=other)
    create_run(
        store,
        run_id=other,
        mode="stub",
        identity=Identity(rep_id="rep-admin", full_visibility=True),
        tasks_total=1,
    )
    unlocked = allocate_run_id()
    create_run(
        store,
        run_id=unlocked,
        mode="stub",
        identity=Identity(rep_id="rep-admin", full_visibility=True),
        tasks_total=1,
    )
    with pytest.raises(Conflict):
        set_execution_arn(store, run_id=unlocked, execution_arn=first)
    assert require_run(store, unlocked).execution_arn is None
    assert require_run(store, other).execution_arn is None
    lock = read_lock(store)
    assert lock is not None and (lock.holder_run_id, lock.execution_arn) == (other, None)
