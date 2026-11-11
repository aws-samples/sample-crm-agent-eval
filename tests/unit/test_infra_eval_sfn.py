# SPDX-License-Identifier: MIT
"""Plan-time assertions on `infra/eval_sfn.tf`: the eval_sweep state machine, its role and its two Lambdas.

Plans `infra/eval_sfn.tf` verbatim beside `infra/main.tf`'s `locals` blocks, with the real
`lambda_action` module and `test_infra_inline_policy_budget`'s stub modules and literal
substitutions, so every policy document and the rendered ASL definition are known at plan time.

The ASL is checked as a graph (every path from `StartAt` passes Teardown then Finalize before it
ends; MaxConcurrency 1; no StopExecution; payloads carry `run_id` and step fields only, never the
execution input's tenant/mode/provider_mode or an identity), and each role's planned DynamoDB,
S3, Runtime and Logs grants are evaluated statement by statement against
the least-privilege rows asserted below.
"""

from __future__ import annotations

import fnmatch
import json
import re
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final, cast

import pytest
from scripts.smoke.iam_policy_budget import (
    FAIL_USAGE_FRACTION,
    INLINE_POLICY_AGGREGATE_LIMIT_CHARS,
    MANAGED_POLICY_DOCUMENT_LIMIT_CHARS,
    _policy_document_chars,
)

from crm.authz import DEMO_TENANT
from crm.store.store import Store
from eval.harness import _read_demo_baseline
from eval.tenancy import CANARY_TENANT, allocate_run_id, teardown_tenant, tenant_for_run
from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)
from tests.unit.test_infra_inline_policy_budget import (
    ACCOUNT,
    INFRA,
    REGION,
    ROOT_SUBSTITUTIONS,
    _copy_real_module,
    _dynamodb_outputs,
    _top_level_blocks,
    _variables,
    _write_placeholder_build,
    _write_stub,
)
from tests.unit.test_sweep_seed_key_patterns import (
    parse_sweep_seed_key_patterns,
    sweep_seed_key_patterns,
)

EVAL_SFN_TF: Final[Path] = INFRA / "eval_sfn.tf"
ASL_TEMPLATE: Final[Path] = INFRA / "templates" / "eval_sweep.asl.json"

#: Modules planned from their real source; every other module `eval_sfn.tf` reads is a stub.
PLANNED_MODULES: Final[frozenset[str]] = frozenset({"lambda_action"})

STATE_MACHINE: Final[str] = "aws_sfn_state_machine.eval_sweep"
SFN_ROLE: Final[str] = "aws_iam_role.eval_sweep"
SFN_POLICY: Final[str] = "aws_iam_role_policy.eval_sweep"
SFN_LOG_GROUP: Final[str] = "aws_cloudwatch_log_group.eval_sweep"
RENDERED_ASL: Final[str] = "terraform_data.rendered_asl"

CONTROL: Final[str] = "module.lambda_action_sweep_control"
TASK: Final[str] = "module.lambda_action_sweep_task"
CONTROL_NAME: Final[str] = "crm-agent-eval-sweep-control"
TASK_NAME: Final[str] = "crm-agent-eval-sweep-task"
CONTROL_ARN: Final[str] = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{CONTROL_NAME}"
TASK_ARN: Final[str] = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{TASK_NAME}"
STATE_MACHINE_NAME: Final[str] = "crm-agent-eval-eval-sweep"
STATE_MACHINE_ARN: Final[str] = (
    f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:{STATE_MACHINE_NAME}"
)

ARTIFACTS_BUCKET_ARN: Final[str] = f"arn:aws:s3:::crm-agent-eval-eval-artifacts-{ACCOUNT}"
RUNTIME_ARN_PREFIX: Final[str] = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/"

#: The Step Functions vended-logs actions, the only ones allowed on `Resource: "*"`.
SFN_LOGS_1_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "logs:CreateLogDelivery",
        "logs:GetLogDelivery",
        "logs:UpdateLogDelivery",
        "logs:DeleteLogDelivery",
        "logs:ListLogDeliveries",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:PutResourcePolicy",
        "logs:DescribeResourcePolicies",
        "logs:DescribeLogGroups",
    }
)

#: SFN-XRAY-1: the X-Ray actions Step Functions tracing needs, the only others on `Resource: "*"`.
SFN_XRAY_1_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets",
    }
)

#: The tracked record of SFN-LOGS-1, SFN-XRAY-1 and SWEEP-CONTROL-1.
SECURITY_BASELINE: Final[Path] = INFRA.parent / "docs" / "security-baseline.md"
ACCEPTED_GRANTS_HEADING: Final[str] = "## Accepted wildcard and base-table grants"

#: `sweep_control_handler`'s `event["step"]` values the ASL may send.
CONTROL_STEPS: Final[frozenset[str]] = frozenset(
    {"start", "seed", "item_failed", "aggregate", "teardown", "finalize"}
)

#: Execution-input fields no step may take from the input.
EXECUTION_INPUT_ONLY_FIELDS: Final[tuple[str, ...]] = ("tenant", "mode", "provider_mode")

#: Substrings no state's payload may carry (execution history keeps state input/output).
IDENTITY_MARKERS: Final[tuple[str, ...]] = ("_agent_identity", "_identity", "claims", "token")

TENANT_TABLES: Final[tuple[str, ...]] = (
    "accounts",
    "contacts",
    "opportunities",
    "activities",
    "notes",
    "audit_records",
    "proposals",
    "reps",
    "usage_records",
)

_EXTRA_SUBSTITUTIONS: Final[dict[str, str]] = {
    "module.eval_artifacts.bucket_arn": f'"{ARTIFACTS_BUCKET_ARN}"',
}


# ---------------------------------------------------------------------------
# Fixture root
# ---------------------------------------------------------------------------


_DEFINITION = re.compile(r"^  definition = (templatefile\(.*?^  \}\))$", re.DOTALL | re.MULTILINE)


def _definition_expression(text: str) -> str:
    """The state machine's `definition` expression, exactly as `eval_sfn.tf` writes it."""
    matches = _DEFINITION.findall(text)
    assert len(matches) == 1, f"expected one `definition = templatefile(...)`, found {len(matches)}"
    expression = matches[0]
    assert isinstance(expression, str)
    return expression


def _offline_state_machine(text: str) -> str:
    """`text` with the definition left unknown at plan time, rendered into `RENDERED_ASL` instead.

    The provider calls `ValidateStateMachineDefinition` while planning a known definition, which the
    mock credentials cannot; an unknown one is not sent. The same expression feeds a
    `terraform_data` whose planned `input` is the rendered document.
    """
    expression = _definition_expression(text)
    offline = text.replace(
        f"  definition = {expression}",
        f'  definition = join("", [{expression}, substr(aws_iam_role.eval_sweep.unique_id, 0, 0)])',
    )
    assert offline != text
    return f'{offline}\n\nresource "terraform_data" "rendered_asl" {{\n  input = {expression}\n}}\n'


def _fixture_root(work_dir: Path, eval_sfn_text: str | None = None) -> str:
    """`main.tf`'s locals plus `eval_sfn.tf` (or `eval_sfn_text`), with stubs for every module
    neither declares. The `dynamodb_data_plane_role_arns`, `logs_key_user_role_arns` and
    `cursor_key_principal_arns` and `api_allowlist_key_consumer_arns` lists are left out: they
    reference `module.lambda_action`, which no stub can stand in for."""
    main_locals = [
        block
        for block in _top_level_blocks((INFRA / "main.tf").read_text(encoding="utf-8"))
        if block.startswith("locals ")
        and "dynamodb_data_plane_role_arns" not in block
        and "logs_key_user_role_arns" not in block
        and "cursor_key_principal_arns" not in block
        and "api_allowlist_key_consumer_arns" not in block
    ]
    text = EVAL_SFN_TF.read_text("utf-8") if eval_sfn_text is None else eval_sfn_text
    root = "\n\n".join([*main_locals, _offline_state_machine(text)])
    for expression, literal in {**ROOT_SUBSTITUTIONS, **_EXTRA_SUBSTITUTIONS}.items():
        if not expression.startswith("local."):
            root = root.replace(expression, literal)

    for module in PLANNED_MODULES:
        _copy_real_module(work_dir, module)
    (work_dir / "templates").mkdir()
    (work_dir / "templates" / ASL_TEMPLATE.name).write_bytes(ASL_TEMPLATE.read_bytes())

    declared = {b.split('"')[1] for b in _top_level_blocks(root) if b.startswith("module ")}
    references: dict[str, set[str]] = {}
    for module, output in re.findall(r"\bmodule\.([a-z_]+)\.([a-z_]+)", root):
        references.setdefault(module, set()).add(output)
    stubs = {m: outputs for m, outputs in references.items() if m not in declared}
    for module, outputs in stubs.items():
        _write_stub(work_dir, module, outputs)
    stub_blocks = "\n\n".join(
        f'module "{module}" {{\n  source = "./modules/{module}"\n}}' for module in sorted(stubs)
    )
    build_dir = work_dir / "build"
    _write_placeholder_build(root, build_dir)
    return f"{_variables(root, build_dir)}\n\n{stub_blocks}\n\n{root}\n"


def _require_harness() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _require_harness()
    work_dir = tmp_path_factory.mktemp("eval-sfn")
    return plan_root(work_dir, _fixture_root(work_dir))


@pytest.fixture(scope="module")
def definition(plan: Plan) -> dict[str, Any]:
    raw = plan.after(RENDERED_ASL)["input"]
    assert isinstance(raw, str), f"the definition is not rendered at plan time: {raw!r}"
    document = json.loads(raw)
    assert isinstance(document, dict)
    return document


# ---------------------------------------------------------------------------
# ASL graph helpers
# ---------------------------------------------------------------------------


def _successors(state: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    if "Next" in state:
        targets.append(state["Next"])
    if "Default" in state:
        targets.append(state["Default"])
    targets.extend(choice["Next"] for choice in state.get("Choices", []))
    targets.extend(catch["Next"] for catch in state.get("Catch", []))
    return targets


def _is_terminal(state: dict[str, Any]) -> bool:
    return state.get("End") is True or state["Type"] in {"Succeed", "Fail"}


def _paths(states: dict[str, Any], start: str) -> Iterator[list[str]]:
    """Every acyclic path from `start` to a terminal state; a cycle fails the test."""
    stack: list[list[str]] = [[start]]
    while stack:
        path = stack.pop()
        state = states[path[-1]]
        if _is_terminal(state):
            yield path
            continue
        successors = _successors(state)
        assert successors, f"{path[-1]} is neither terminal nor has a successor"
        for target in successors:
            assert target in states, f"{path[-1]} transitions to undeclared state {target!r}"
            assert target not in path, f"cycle through {target}: {path}"
            stack.append([*path, target])


def _all_states(definition: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every state, including those inside a Map's ItemProcessor."""
    pending = [definition["States"]]
    while pending:
        states = pending.pop()
        for name, state in states.items():
            yield name, state
            processor = state.get("ItemProcessor") or state.get("Iterator")
            if processor is not None:
                pending.append(processor["States"])


def _task_payloads(definition: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """`{state: (FunctionName, Payload)}` for every Lambda Task."""
    tasks: dict[str, tuple[str, dict[str, Any]]] = {}
    for name, state in _all_states(definition):
        if state["Type"] != "Task":
            continue
        assert state["Resource"] == "arn:aws:states:::lambda:invoke", (name, state["Resource"])
        parameters = state["Parameters"]
        assert set(parameters) == {"FunctionName", "Payload"}, (name, sorted(parameters))
        tasks[name] = (parameters["FunctionName"], parameters["Payload"])
    return tasks


# ---------------------------------------------------------------------------
# A minimal identity-policy evaluator for the statements this file plans
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def _condition_holds(
    statement: dict[str, Any], leading_keys: list[str] | None, select: str | None = None
) -> bool:
    """Whether `statement`'s Condition holds for the request's LeadingKeys and `Select` values.

    A request without `Select` does not match a `dynamodb:Select` condition, as in IAM.
    """
    condition = statement.get("Condition")
    if not condition:
        return True
    for operator, keys in condition.items():
        for key, values in keys.items():
            if key == "dynamodb:Select":
                assert operator == "StringEquals", f"unhandled Select operator {operator}"
                if select is None or select not in _as_list(values):
                    return False
                continue
            assert key == "dynamodb:LeadingKeys", f"unhandled condition key {key}"
            assert leading_keys is not None, (
                f"statement {statement.get('Sid')} is conditioned on LeadingKeys, so an evaluation "
                "without a key would pass vacuously"
            )
            patterns = _as_list(values)
            if operator == "ForAllValues:StringEquals":
                matched = all(k in patterns for k in leading_keys)
            elif operator == "ForAllValues:StringLike":
                matched = all(
                    any(fnmatch.fnmatchcase(k, p) for p in patterns) for k in leading_keys
                )
            else:
                raise AssertionError(f"unhandled condition operator {operator}")
            if not matched:
                return False
    return True


def _matches(statement: dict[str, Any], action: str, resource: str) -> bool:
    actions = _as_list(statement.get("Action", []))
    resources = _as_list(statement.get("Resource", []))
    return any(fnmatch.fnmatchcase(action, a) for a in actions) and any(
        fnmatch.fnmatchcase(resource, r) for r in resources
    )


def _decision(
    statements: list[dict[str, Any]],
    action: str,
    resource: str,
    leading_keys: list[str] | None = None,
    select: str | None = None,
) -> str:
    """`allowed`, `explicitDeny` or `implicitDeny`, as `simulate-principal-policy` names them."""
    allowed = False
    for statement in statements:
        if not _matches(statement, action, resource):
            continue
        if not _condition_holds(statement, leading_keys, select):
            continue
        if statement["Effect"] == "Deny":
            return "explicitDeny"
        allowed = True
    return "allowed" if allowed else "implicitDeny"


def _statements(document: str) -> list[dict[str, Any]]:
    parsed = json.loads(document)
    statements = parsed["Statement"]
    return [statements] if isinstance(statements, dict) else list(statements)


def _role_statements(plan: Plan, module: str, role_name: str) -> list[dict[str, Any]]:
    """Every planned inline and module-managed statement on `role_name`."""
    statements: list[dict[str, Any]] = []
    for address in plan.addresses_of_type("aws_iam_role_policy"):
        after = plan.after(address)
        if after.get("role") == role_name:
            policy = after.get("policy")
            assert isinstance(policy, str), f"{address} is not rendered at plan time"
            statements.extend(_statements(policy))
    for address in plan.addresses_of_type("aws_iam_policy"):
        if address.startswith(f"{module}."):
            policy = plan.after(address).get("policy")
            assert isinstance(policy, str), f"{address} is not rendered at plan time"
            statements.extend(_statements(policy))
    assert statements, f"no statements planned for {role_name}"
    return statements


def _table_arn(table: str) -> str:
    arns = _dynamodb_outputs()["table_arns"]
    assert isinstance(arns, dict)
    arn = arns[table]
    assert isinstance(arn, str)
    return arn


def _gsi_arn(table: str, index: str) -> str:
    arns = _dynamodb_outputs()["gsi_arns"]
    assert isinstance(arns, dict)
    arn = arns[table][index]
    assert isinstance(arn, str)
    return arn


@pytest.fixture(scope="module")
def control(plan: Plan) -> list[dict[str, Any]]:
    return _role_statements(plan, CONTROL, f"{CONTROL_NAME}-role")


@pytest.fixture(scope="module")
def task(plan: Plan) -> list[dict[str, Any]]:
    return _role_statements(plan, TASK, f"{TASK_NAME}-role")


@pytest.fixture(scope="module")
def run_id() -> str:
    return allocate_run_id()


# ---------------------------------------------------------------------------
# The ASL template and the rendered definition
# ---------------------------------------------------------------------------


def test_the_template_parses_as_json_with_only_the_two_function_placeholders() -> None:
    text = ASL_TEMPLATE.read_text(encoding="utf-8")
    placeholders = set(re.findall(r"\$\{([^}]*)\}", text))
    assert placeholders == {"sweep_control_arn", "sweep_task_arn"}, placeholders
    assert "%{" not in text
    json.loads(text)


def test_the_definition_is_the_asl_template_with_the_two_module_arns() -> None:
    expression = _definition_expression(EVAL_SFN_TF.read_text(encoding="utf-8"))
    assert expression.startswith('templatefile("${path.module}/templates/eval_sweep.asl.json", {')
    assert "sweep_control_arn = module.lambda_action_sweep_control.function_arn" in expression
    assert "sweep_task_arn    = module.lambda_action_sweep_task.function_arn" in expression


def test_the_definition_is_rendered_from_the_template_with_both_function_arns(
    definition: dict[str, Any],
) -> None:
    functions = {function for function, _ in _task_payloads(definition).values()}
    assert functions == {CONTROL_ARN, TASK_ARN}, functions


REFUSAL_STATE = "NotThisRunsExecution"


def test_every_path_from_start_passes_teardown_then_finalize(definition: dict[str, Any]) -> None:
    """No route, success or Catch, ends without Teardown → Finalize, except a
    refused Start, which ends at the refusal Fail state having run nothing after Start."""
    states = definition["States"]
    paths = list(_paths(states, definition["StartAt"]))
    assert len(paths) > 1, "the graph walk found one path, so it exercises no Catch route"
    refused = [path for path in paths if "Teardown" not in path]
    assert refused == [["Init", "Start", "IsRefused", REFUSAL_STATE]], refused
    for path in paths:
        if path in refused:
            continue
        assert "Teardown" in path and "Finalize" in path, path
        teardown, finalize = path.index("Teardown"), path.index("Finalize")
        assert teardown < finalize, path
        after = [states[name]["Type"] for name in path[finalize + 1 :]]
        assert after in (["Choice", "Succeed"], ["Choice", "Fail"]), path
        assert path[-1] in {"Done", REFUSAL_STATE}, path


def test_every_top_level_task_before_teardown_catches_every_error(
    definition: dict[str, Any],
) -> None:
    states = definition["States"]
    for name, state in states.items():
        if name in {"Teardown", "Finalize"} or state["Type"] not in {"Task", "Map"}:
            continue
        catches = state.get("Catch", [])
        assert any("States.ALL" in c["ErrorEquals"] for c in catches), name


def test_the_graph_walk_detects_a_path_that_skips_teardown(definition: dict[str, Any]) -> None:
    """Control: rewiring Aggregate's Catch straight to Finalize must red the walk."""
    mutated = json.loads(json.dumps(definition))
    mutated["States"]["Aggregate"]["Catch"][0]["Next"] = "Finalize"
    states = mutated["States"]
    with pytest.raises(AssertionError):
        for path in _paths(states, mutated["StartAt"]):
            assert "Teardown" in path, path


def test_no_stop_execution_and_one_fail_state_for_a_refusal(definition: dict[str, Any]) -> None:
    assert "StopExecution" not in json.dumps(definition)
    fails = [name for name, state in _all_states(definition) if state["Type"] == "Fail"]
    assert fails == [REFUSAL_STATE]
    assert definition["States"][REFUSAL_STATE]["Error"] == "SweepExecutionRefused"
    entered_from = {
        name for name, state in _all_states(definition) if REFUSAL_STATE in _successors(state)
    }
    assert entered_from == {"IsRefused", "IsFinalized"}
    assert all(definition["States"][name]["Type"] == "Choice" for name in entered_from)


def test_the_map_runs_one_item_at_a_time_with_a_bounded_task(definition: dict[str, Any]) -> None:
    run_tasks = definition["States"]["RunTasks"]
    assert run_tasks["Type"] == "Map"
    assert run_tasks["MaxConcurrency"] == 1
    assert run_tasks["ResultPath"] is None, "Map output would carry 40 results (256 KB limit)"
    item_states = run_tasks["ItemProcessor"]["States"]
    run_task = item_states[run_tasks["ItemProcessor"]["StartAt"]]
    assert run_task["TimeoutSeconds"] == 330
    assert run_task["Catch"][0]["Next"] == "RecordItemFailure"
    assert item_states["RecordItemFailure"]["End"] is True
    assert definition["TimeoutSeconds"] == 86400


def test_only_run_task_invokes_sweep_task(definition: dict[str, Any]) -> None:
    tasks = _task_payloads(definition)
    assert [name for name, (fn, _) in tasks.items() if fn == TASK_ARN] == ["RunTask"]


def test_control_payloads_name_a_known_step_and_the_run_id(definition: dict[str, Any]) -> None:
    for name, (function, payload) in _task_payloads(definition).items():
        assert payload.get("run_id.$") == "$.run_id", (name, payload)
        if function == CONTROL_ARN:
            assert payload.get("step") in CONTROL_STEPS, (name, payload)
        else:
            assert "step" not in payload, (name, payload)
    steps = {p["step"] for fn, p in _task_payloads(definition).values() if fn == CONTROL_ARN}
    assert steps == CONTROL_STEPS, steps


def test_no_payload_reads_tenant_mode_or_provider_mode_from_the_input(
    definition: dict[str, Any],
) -> None:
    """Those come from the job record; Init drops them from the state data."""
    init = definition["States"][definition["StartAt"]]
    assert init["Type"] == "Pass"
    assert set(init["Parameters"]) == {"run_id.$", "error", "teardown_error"}
    for name, (_, payload) in _task_payloads(definition).items():
        keys = {key.removesuffix(".$") for key in payload}
        assert not keys & set(EXECUTION_INPUT_ONLY_FIELDS), (name, payload)
        for value in payload.values():
            for field in EXECUTION_INPUT_ONLY_FIELDS:
                assert f"$.{field}" != value, (name, payload)


def test_no_state_carries_an_identity_or_token(definition: dict[str, Any]) -> None:
    """Execution history keeps state input and output whatever the logging level."""
    for name, state in _all_states(definition):
        rendered = json.dumps(
            {k: state.get(k) for k in ("Parameters", "ItemSelector", "ResultSelector")}
        )
        for marker in IDENTITY_MARKERS:
            assert marker not in rendered, (name, marker)


# ---------------------------------------------------------------------------
# State machine, log group, role
# ---------------------------------------------------------------------------


def test_the_state_machine_is_standard_and_logs_without_execution_data(plan: Plan) -> None:
    after = plan.after(STATE_MACHINE)
    assert after["name"] == STATE_MACHINE_NAME
    assert after["type"] == "STANDARD"
    [logging] = after["logging_configuration"]
    assert logging["level"] == "ALL"
    assert logging["include_execution_data"] is False
    assert after["tags"]["data-classification"]


def test_the_state_machine_traces_with_xray(plan: Plan) -> None:
    [tracing] = plan.after(STATE_MACHINE)["tracing_configuration"]
    assert tracing["enabled"] is True


def test_the_log_group_is_kms_encrypted_with_explicit_retention(plan: Plan) -> None:
    after = plan.after(SFN_LOG_GROUP)
    assert after["name"] == f"/aws/vendedlogs/states/{STATE_MACHINE_NAME}"
    assert after["retention_in_days"] == 365
    assert str(after["kms_key_id"]).startswith(f"arn:aws:kms:{REGION}:{ACCOUNT}:key/")
    assert after["tags"]["data-classification"]


def test_the_state_machine_logs_to_its_own_group(plan: Plan) -> None:
    resource = next(
        r for r in plan.configuration["root_module"]["resources"] if r["address"] == STATE_MACHINE
    )
    destination = resource["expressions"]["logging_configuration"][0]["log_destination"]
    assert f"{SFN_LOG_GROUP}.arn" in destination["references"], destination


def test_the_role_trusts_only_states_for_this_state_machine(plan: Plan) -> None:
    """Confused-deputy conditions on both source account and source ARN."""
    trust = json.loads(plan.after(SFN_ROLE)["assume_role_policy"])
    [statement] = _as_list_of_dicts(trust["Statement"])
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "sts:AssumeRole"
    assert statement["Principal"] == {"Service": "states.amazonaws.com"}
    condition = statement["Condition"]
    assert condition["StringEquals"]["aws:SourceAccount"] == ACCOUNT
    assert condition["ArnEquals"]["aws:SourceArn"] == STATE_MACHINE_ARN


def _as_list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [value] if isinstance(value, dict) else list(value)


def test_the_role_may_invoke_exactly_the_two_sweep_functions(plan: Plan) -> None:
    statements = _statements(plan.after(SFN_POLICY)["policy"])
    invoke = [s for s in statements if s.get("Sid") == "InvokeSweepLambdas"]
    assert len(invoke) == 1
    assert _as_list(invoke[0]["Action"]) == ["lambda:InvokeFunction"]
    assert set(_as_list(invoke[0]["Resource"])) == {
        CONTROL_ARN,
        f"{CONTROL_ARN}:*",
        TASK_ARN,
        f"{TASK_ARN}:*",
    }
    assert {s.get("Sid") for s in statements} == {
        "InvokeSweepLambdas",
        "SfnVendedLogDelivery",
        "SfnXRayTracing",
    }
    eval_trigger = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:crm-agent-eval-eval-trigger"
    for action, resource in [
        ("lambda:InvokeFunction", eval_trigger),
        (
            "lambda:InvokeFunction",
            f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:crm-agent-eval-get_account",
        ),
        ("dynamodb:GetItem", _table_arn("eval_runs")),
        ("s3:PutObject", f"{ARTIFACTS_BUCKET_ARN}/sweeps/x"),
        ("bedrock-agentcore:InvokeAgentRuntime", f"{RUNTIME_ARN_PREFIX}x"),
        ("states:StopExecution", STATE_MACHINE_ARN),
        ("xray:BatchGetTraces", "*"),
        ("xray:PutEncryptionConfig", "*"),
    ]:
        assert _decision(statements, action, resource) == "implicitDeny", (action, resource)


def _statement(plan: Plan, sid: str) -> dict[str, Any]:
    [statement] = [s for s in _statements(plan.after(SFN_POLICY)["policy"]) if s.get("Sid") == sid]
    return statement


def test_the_wildcard_resource_statements_are_exactly_sfn_logs_1_and_sfn_xray_1(
    plan: Plan,
) -> None:
    statements = _statements(plan.after(SFN_POLICY)["policy"])
    wildcard = [s for s in statements if "*" in _as_list(s["Resource"])]
    assert {s["Sid"] for s in wildcard} == {"SfnVendedLogDelivery", "SfnXRayTracing"}
    assert len(wildcard) == 2
    logs = _statement(plan, "SfnVendedLogDelivery")
    assert set(_as_list(logs["Action"])) == SFN_LOGS_1_ACTIONS
    xray = _statement(plan, "SfnXRayTracing")
    assert xray["Effect"] == "Allow"
    assert "Condition" not in xray
    assert _as_list(xray["Resource"]) == ["*"]
    assert set(_as_list(xray["Action"])) == SFN_XRAY_1_ACTIONS


@pytest.mark.parametrize("action", sorted(SFN_XRAY_1_ACTIONS))
def test_the_role_may_send_each_xray_tracing_action(plan: Plan, action: str) -> None:
    statements = _statements(plan.after(SFN_POLICY)["policy"])
    assert _decision(statements, action, "*") == "allowed", action


def _accepted_grant_entry(entry_id: str) -> str:
    """The body of `### <entry_id>` under the accepted-grants section of `docs/security-baseline.md`."""
    text = SECURITY_BASELINE.read_text(encoding="utf-8")
    sections = text.split(f"\n{ACCEPTED_GRANTS_HEADING}\n", 1)
    assert len(sections) == 2, f"{SECURITY_BASELINE} has no {ACCEPTED_GRANTS_HEADING!r} section"
    parts = sections[1].split(f"\n### {entry_id}\n", 1)
    assert len(parts) == 2, f"no ### {entry_id} entry"
    return re.split(r"\n#{2,3} ", parts[1], maxsplit=1)[0]


def _bullet(entry: str, label: str) -> str:
    """The text of the top-level `- **<label>**` bullet in `entry`, continuation lines included."""
    parts = entry.split(f"\n- **{label}**", 1)
    assert len(parts) == 2, f"no {label!r} bullet"
    return re.split(r"\n- ", parts[1], maxsplit=1)[0]


def _recorded_actions(entry_id: str) -> set[str]:
    return set(
        re.findall(
            r"`([a-z]+:[A-Za-z]+)`", _bullet(_accepted_grant_entry(entry_id), "Actions, exactly:")
        )
    )


def test_the_security_baseline_records_sfn_logs_1_with_the_planned_action_list(
    plan: Plan,
) -> None:
    assert _recorded_actions("SFN-LOGS-1") == SFN_LOGS_1_ACTIONS
    planned = _statement(plan, "SfnVendedLogDelivery")
    assert _recorded_actions("SFN-LOGS-1") == set(_as_list(planned["Action"]))


def test_the_security_baseline_records_sfn_xray_1_with_the_planned_action_list(
    plan: Plan,
) -> None:
    planned = _statement(plan, "SfnXRayTracing")
    assert _recorded_actions("SFN-XRAY-1") == set(_as_list(planned["Action"]))


def _hcl_string_list_local(text: str, name: str) -> set[str]:
    """The string members of the list-valued local `name` in a Terraform `locals` block."""
    match = re.search(rf"^\s*{name}\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    assert match, f"no list local {name!r}"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_the_security_baseline_records_sfn_xray_1_from_the_source_local() -> None:
    """SFN-XRAY-1 doc parity against `local.sfn_xray_tracing_actions`, needing no terraform."""
    source = _hcl_string_list_local(
        EVAL_SFN_TF.read_text(encoding="utf-8"), "sfn_xray_tracing_actions"
    )
    assert source == SFN_XRAY_1_ACTIONS
    assert _recorded_actions("SFN-XRAY-1") == source


# ---------------------------------------------------------------------------
# The two Lambdas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "handler", "timeout", "environment"),
    [
        (
            CONTROL,
            "eval.sfn_handlers.sweep_control_handler",
            120,
            {"CRM_TABLE_PREFIX", "PYTHONPATH", "EVAL_ARTIFACTS_BUCKET", "EVAL_ARTIFACTS_PREFIX"},
        ),
        (
            TASK,
            "eval.sfn_handlers.sweep_task_handler",
            300,
            {"CRM_TABLE_PREFIX", "PYTHONPATH", "AGENT_RUNTIME_ARN", "AGENT_RUNTIME_QUALIFIER"},
        ),
    ],
)
def test_each_function_is_vpc_attached_kms_encrypted_and_declares_its_surface(
    plan: Plan, module: str, handler: str, timeout: int, environment: set[str]
) -> None:
    after = plan.after(f"{module}.aws_lambda_function.this")
    assert after["handler"] == handler
    assert after["timeout"] == timeout
    assert Path(after["filename"]).name == "eval_trigger.zip"
    [vpc] = after["vpc_config"]
    assert len(vpc["subnet_ids"]) >= 2 and vpc["security_group_ids"], vpc
    assert str(after["kms_key_arn"]).startswith(f"arn:aws:kms:{REGION}:{ACCOUNT}:key/")
    [env] = after["environment"]
    assert set(env["variables"]) == environment
    assert after["tags"]["data-classification"]


def test_reserved_concurrency_comes_from_the_shared_map(plan: Plan) -> None:
    """sweep_task 1 (one task at a time), sweep_control 2."""
    assert plan.after(f"{TASK}.aws_lambda_function.this")["reserved_concurrent_executions"] == 1
    assert plan.after(f"{CONTROL}.aws_lambda_function.this")["reserved_concurrent_executions"] == 2
    text = EVAL_SFN_TF.read_text(encoding="utf-8")
    assert 'local.reserved_concurrency["sweep_control"]' in text
    assert 'local.reserved_concurrency["sweep_task"]' in text


def test_sweep_task_targets_the_runtime_default_endpoint(plan: Plan) -> None:
    env = plan.after(f"{TASK}.aws_lambda_function.this")["environment"][0]["variables"]
    assert env["AGENT_RUNTIME_ARN"].startswith(RUNTIME_ARN_PREFIX)
    assert env["AGENT_RUNTIME_QUALIFIER"] == "DEFAULT"


def test_the_run_row_pattern_matches_allocated_run_ids_only(run_id: str) -> None:
    pattern = re.search(
        r'eval_run_row_key_pattern = join\("", \[for i in range\((\d+)\) : "\?"\]\)',
        EVAL_SFN_TF.read_text(encoding="utf-8"),
    )
    assert pattern is not None
    glob = "?" * int(pattern.group(1))
    assert fnmatch.fnmatchcase(run_id, glob)
    assert not fnmatch.fnmatchcase(f"job#{run_id}", glob)
    assert not fnmatch.fnmatchcase("lock#sweep", glob)


def test_sweep_control_dynamodb_grants(control: list[dict[str, Any]], run_id: str) -> None:
    """Job rows Get/Update, run rows Get/Put, lock Get/Delete only."""
    runs = _table_arn("eval_runs")
    for action in ("dynamodb:GetItem", "dynamodb:UpdateItem"):
        assert _decision(control, action, runs, [f"job#{run_id}"]) == "allowed", action
    for action in ("dynamodb:GetItem", "dynamodb:PutItem"):
        assert _decision(control, action, runs, [run_id]) == "allowed", action
    assert _decision(control, "dynamodb:PutItem", runs, [f"job#{run_id}"]) != "allowed"
    assert _decision(control, "dynamodb:UpdateItem", runs, [run_id]) != "allowed"
    assert _decision(control, "dynamodb:GetItem", runs, ["lock#sweep"]) == "allowed"
    assert _decision(control, "dynamodb:DeleteItem", runs, ["lock#sweep"]) == "allowed"
    assert _decision(control, "dynamodb:DeleteItem", runs, [f"job#{run_id}"]) != "allowed"
    assert _decision(control, "dynamodb:DeleteItem", runs, [run_id]) != "allowed"
    assert _decision(control, "dynamodb:PutItem", runs, ["lock#sweep"]) != "allowed"
    assert _decision(control, "dynamodb:Scan", runs, [run_id]) != "allowed"

    results = _table_arn("eval_results")
    for action in ("dynamodb:GetItem", "dynamodb:PutItem"):
        assert _decision(control, action, results) == "allowed", action

    for table in TENANT_TABLES:
        arn = _table_arn(table)
        keys = [f"eval-{run_id}"]
        assert _decision(control, "dynamodb:BatchWriteItem", arn, keys) == "allowed", table
        assert _decision(control, "dynamodb:Scan", arn, keys) != "allowed", table
        assert _decision(control, "dynamodb:Query", arn, keys) != "allowed", table


def test_sweep_control_queries_eval_and_canary_rows_and_only_counts_demo_on_gsi_tenant(
    control: list[dict[str, Any]], run_id: str
) -> None:
    """Every tenant-table Query is `gsi_tenant`: rows under `eval-*` or `canary` (teardown's
    emptiness assertion), and only `Select = COUNT` under `demo`."""
    gsi_arns = _dynamodb_outputs()["gsi_arns"]
    assert isinstance(gsi_arns, dict)
    other_indexes = 0
    for table in TENANT_TABLES:
        tenant_index = _gsi_arn(table, "gsi_tenant")
        for keys in ([f"eval-{run_id}"], ["canary"]):
            assert _decision(control, "dynamodb:Query", tenant_index, keys) == "allowed", (
                table,
                keys,
            )
        assert _demo_count_failure(control, table) is None, table
        for keys in (["demo#x"], ["canary#x"], ["smoke-x"], [f"eval-{run_id}", "smoke-x"]):
            assert _decision(control, "dynamodb:Query", tenant_index, keys) != "allowed", (
                table,
                keys,
            )
        for name, arn in gsi_arns[table].items():
            if name == "gsi_tenant":
                continue
            other_indexes += 1
            assert _decision(control, "dynamodb:Query", arn, [f"eval-{run_id}"]) != "allowed", (
                table,
                name,
            )
    assert other_indexes, "no non-gsi_tenant index exists, so the index scoping is untested"


class _RecordingDynamoDB:
    """A DynamoDB client that answers every Query empty and records `(index, partition value)`."""

    def __init__(self) -> None:
        self.queried: set[tuple[str, str, str | None]] = set()

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queried.add(
            (
                str(kwargs.get("IndexName")),
                str(kwargs["ExpressionAttributeValues"][":pk"]["S"]),
                kwargs.get("Select"),
            )
        )
        return {"Items": [], "Count": 0}


def queries_sweep_control_issues(run_id: str) -> set[tuple[str, str | None]]:
    """`(partition value, Select)` of every `gsi_tenant` Query `teardown_tenant` and the demo
    baseline read issue.

    Runs both through a real `Store` over :class:`_RecordingDynamoDB`, so the set comes from the code
    sweep-control runs, not from a list kept beside it.
    """
    client = _RecordingDynamoDB()
    store = Store(client=cast("Any", client))
    teardown_tenant(store, tenant=tenant_for_run(run_id))
    snapshot = _read_demo_baseline(store)
    assert snapshot.counts is not None, snapshot.error
    assert {index for index, _value, _select in client.queried} == {"gsi_tenant"}, client.queried
    return {(value, select) for _index, value, select in client.queried}


def tenants_sweep_control_queries(run_id: str) -> set[str]:
    """The `gsi_tenant` partition values of :func:`queries_sweep_control_issues`."""
    return {tenant for tenant, _select in queries_sweep_control_issues(run_id)}


def test_the_demo_baseline_read_issues_only_count_queries(run_id: str) -> None:
    """The code reads `demo` only with `Select = COUNT`, the one Query the demo grant admits."""
    demo_selects = {
        select for tenant, select in queries_sweep_control_issues(run_id) if tenant == DEMO_TENANT
    }
    assert demo_selects == {"COUNT"}


def test_the_queried_tenant_set_is_derived_from_the_code(run_id: str) -> None:
    """Control: the derivation reads the run's tenant, `CANARY_TENANT` and `DEMO_TENANT`."""
    assert tenants_sweep_control_queries(run_id) == {
        tenant_for_run(run_id),
        CANARY_TENANT,
        DEMO_TENANT,
    }


def test_sweep_control_can_query_every_tenant_its_teardown_reads(
    control: list[dict[str, Any]], run_id: str
) -> None:
    """Every `gsi_tenant` Query `teardown_tenant` and the demo baseline read issue, with its
    `Select`, is inside sweep-control's Query scope; the set is derived from the code."""
    queries = queries_sweep_control_issues(run_id)
    tenants = {tenant for tenant, _select in queries}
    for table in TENANT_TABLES:
        tenant_index = _gsi_arn(table, "gsi_tenant")
        for tenant, select in sorted(queries, key=str):
            decision = _decision(control, "dynamodb:Query", tenant_index, [tenant], select)
            assert decision == "allowed", (table, tenant, select)
        assert tenants
        for action in ("dynamodb:PutItem", "dynamodb:BatchWriteItem", "dynamodb:DeleteItem"):
            assert _decision(control, action, tenant_index, [CANARY_TENANT]) != "allowed", (
                table,
                action,
            )


def test_the_sweep_base_table_residual_is_recorded() -> None:
    """SWEEP-CONTROL-1 names exactly the tenant tables holding an unconditioned base-table grant
    on either role."""
    bullet = _bullet(
        _accepted_grant_entry("SWEEP-CONTROL-1"), "Tables with an unconditioned base-table grant:"
    )
    listed = re.split(r"\n  - ", bullet, maxsplit=1)[0]
    assert set(re.findall(r"`([a-z_]+)`", listed)) == set(UNCONDITIONED_BASE_TABLE_GRANTS)


def test_sweep_task_dynamodb_grants_are_read_only_on_tenant_tables(
    task: list[dict[str, Any]], run_id: str
) -> None:
    """Job-row Get/Update only; never the lock; no tenant-table write."""
    runs = _table_arn("eval_runs")
    assert _decision(task, "dynamodb:GetItem", runs, [f"job#{run_id}"]) == "allowed"
    assert _decision(task, "dynamodb:UpdateItem", runs, [f"job#{run_id}"]) == "allowed"
    for action in ("dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"):
        assert _decision(task, action, runs, ["lock#sweep"]) != "allowed", action
    assert _decision(task, "dynamodb:UpdateItem", runs, [run_id]) != "allowed"
    assert _decision(task, "dynamodb:PutItem", runs, [f"job#{run_id}"]) != "allowed"

    results = _table_arn("eval_results")
    for action in ("dynamodb:GetItem", "dynamodb:PutItem"):
        assert _decision(task, action, results) == "allowed", action

    patterns = sweep_seed_key_patterns()
    for table in TENANT_TABLES:
        arn = _table_arn(table)
        keys = [f"eval-{run_id}"]
        if table in patterns:
            read_key = [_in_scope_key(table, run_id)]
        elif table in SWEEP_TASK_UNCONDITIONED_READS:
            read_key = keys
        else:
            read_key = None
        if read_key is not None:
            assert _decision(task, "dynamodb:GetItem", arn, read_key) == "allowed", table
        expected_query = "allowed" if table in patterns else "explicitDeny"
        assert _decision(task, "dynamodb:Query", _gsi_arn(table, "gsi_tenant"), keys) == (
            expected_query
        ), table
        assert _decision(task, "dynamodb:Query", _gsi_arn(table, "gsi_tenant"), ["demo"]) != (
            "allowed"
        ), table
        for action in (
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "dynamodb:BatchWriteItem",
        ):
            assert _decision(task, action, arn, keys) != "allowed", (table, action)
    for key in ("usersettings#demo#rep-1", "conversation#conv-1", "demo#rep-1"):
        assert _decision(task, "dynamodb:GetItem", _table_arn("reps"), [key]) != "allowed", key


# ---------------------------------------------------------------------------
# Removed sweep grants and `<kind>-eval-*` key conditions
# ---------------------------------------------------------------------------

#: `(module, table, action, index)` for every removed grant; `index` names the GSI an
#: index-only grant covered, else `None`.
REMOVED_GRANTS: Final[tuple[tuple[str, str, str, str | None], ...]] = (
    *((CONTROL, table, "dynamodb:GetItem", None) for table in TENANT_TABLES),
    *(
        (CONTROL, table, "dynamodb:PutItem", None)
        for table in ("accounts", "contacts", "opportunities", "activities", "notes")
    ),
    (CONTROL, "eval_results", "dynamodb:Query", None),
    (CONTROL, "eval_results", "dynamodb:BatchWriteItem", None),
    (CONTROL, "eval_runs", "dynamodb:Query", "gsi_status"),
    (TASK, "eval_results", "dynamodb:UpdateItem", None),
    (TASK, "audit_records", "dynamodb:GetItem", None),
    (TASK, "usage_records", "dynamodb:GetItem", None),
    *(
        (TASK, table, "dynamodb:Query", "gsi_tenant")
        for table in ("audit_records", "proposals", "usage_records", "reps")
    ),
    (TASK, "reps", "dynamodb:GetItem", None),
)

#: Tenant tables sweep_task reads by key without a key condition (agent-generated `proposal_id`).
SWEEP_TASK_UNCONDITIONED_READS: Final[tuple[str, ...]] = ("proposals",)

#: `(module, action)` of the grants conditioned on each seeded table's key pattern.
CONDITIONED_ACTIONS: Final[tuple[tuple[str, str], ...]] = ((TASK, "dynamodb:GetItem"),)

#: `Select` values a `demo` Query from sweep-control must be refused with (`None`: no `Select`).
DEMO_REFUSED_SELECTS: Final[tuple[str | None, ...]] = (None, "ALL_PROJECTED_ATTRIBUTES")

#: Tenant tables holding an unconditioned base-table grant on either role (SWEEP-CONTROL-1).
UNCONDITIONED_BASE_TABLE_GRANTS: Final[tuple[str, ...]] = (
    "accounts",
    "contacts",
    "opportunities",
    "activities",
    "notes",
    "audit_records",
    "proposals",
    "usage_records",
)

#: A fixed run id for key probes, so a cell id names the same keys on every run.
PROBE_RUN_ID: Final[str] = "0123456789abcdef0123456789abcdef"


def _in_scope_key(table: str, run_id: str) -> str:
    """A key of `table`'s seeded shape, `<kind>-eval-<run_id>-0001`."""
    pattern = sweep_seed_key_patterns()[table]
    assert pattern.endswith("-eval-*"), pattern
    return f"{pattern.removesuffix('*')}{run_id}-0001"


def _out_of_scope_keys(table: str) -> list[str]:
    """A `demo` key of `table`'s kind and an agent-created uuid."""
    kind = sweep_seed_key_patterns()[table].split("-", 1)[0]
    return [f"{kind}-{DEMO_TENANT}-0000", str(uuid.uuid4())]


def _conditioned_cells() -> list[tuple[str, str, str]]:
    return [
        (module, table, action)
        for module, action in CONDITIONED_ACTIONS
        for table in sweep_seed_key_patterns()
    ]


def _allows(statements: list[dict[str, Any]], action: str, resource: str) -> list[dict[str, Any]]:
    return [s for s in statements if s["Effect"] == "Allow" and _matches(s, action, resource)]


def _removed_grant_failure(
    statements: list[dict[str, Any]], table: str, action: str, index: str | None
) -> str | None:
    """Why a removed grant is not removed, or `None`: no Allow covers it and a Deny names it."""
    resource = _gsi_arn(table, index) if index else _table_arn(table)
    allows = _allows(statements, action, resource)
    if allows:
        return f"Allow statements still cover it: {[s.get('Sid') for s in allows]}"
    patterns = sweep_seed_key_patterns()
    key = _in_scope_key(table, PROBE_RUN_ID) if table in patterns else f"eval-{PROBE_RUN_ID}"
    decision = _decision(statements, action, resource, [key])
    return None if decision == "explicitDeny" else f"decision {decision}, not explicitDeny"


def _conditioned_grant_failure(
    statements: list[dict[str, Any]], table: str, action: str
) -> str | None:
    """Why a conditioned grant is not exactly one Allow under `StringLike` on its pattern."""
    pattern = sweep_seed_key_patterns()[table]
    allows = _allows(statements, action, _table_arn(table))
    if len(allows) != 1:
        return f"{len(allows)} Allow statements cover it, not 1"
    condition = {
        operator: {key: _as_list(values) for key, values in keys.items()}
        for operator, keys in (allows[0].get("Condition") or {}).items()
    }
    expected = {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [pattern]}}
    if condition != expected:
        return f"condition {condition!r}, not {expected!r}"
    arn = _table_arn(table)
    if _decision(statements, action, arn, [_in_scope_key(table, PROBE_RUN_ID)]) != "allowed":
        return "an in-scope key is not allowed"
    for key in _out_of_scope_keys(table):
        if _decision(statements, action, arn, [key]) == "allowed":
            return f"out-of-scope key {key!r} is allowed"
    return None


def _demo_count_failure(statements: list[dict[str, Any]], table: str) -> str | None:
    """Why sweep-control's `demo` Query on `table`'s `gsi_tenant` is not COUNT-only, or `None`."""
    tenant_index = _gsi_arn(table, "gsi_tenant")
    if _decision(statements, "dynamodb:Query", tenant_index, [DEMO_TENANT], "COUNT") != "allowed":
        return "a Select=COUNT demo Query is not allowed"
    for select in DEMO_REFUSED_SELECTS:
        decision = _decision(statements, "dynamodb:Query", tenant_index, [DEMO_TENANT], select)
        if decision == "allowed":
            return f"a demo Query with Select={select!r} is allowed"
    return None


def _cell_failures(
    statements_by_module: Mapping[str, list[dict[str, Any]]],
) -> dict[tuple[str, str, str], str]:
    """Every failing removed-grant, conditioned-grant and demo-count cell, keyed
    `(module, table, action)`."""
    failures: dict[tuple[str, str, str], str] = {}
    for table in TENANT_TABLES:
        reason = _demo_count_failure(statements_by_module[CONTROL], table)
        if reason is not None:
            failures[(CONTROL, table, "dynamodb:Query")] = reason
    for module, table, action, index in REMOVED_GRANTS:
        reason = _removed_grant_failure(statements_by_module[module], table, action, index)
        if reason is not None:
            failures[(module, table, action)] = reason
    for module, table, action in _conditioned_cells():
        reason = _conditioned_grant_failure(statements_by_module[module], table, action)
        if reason is not None:
            failures[(module, table, action)] = reason
    return failures


def _unconditioned_base_table_grant_tables(*roles: list[dict[str, Any]]) -> set[str]:
    """Tenant tables on whose base-table ARN some role holds a DynamoDB Allow with no Condition."""
    tables: set[str] = set()
    for statements in roles:
        for statement in statements:
            if statement["Effect"] != "Allow" or statement.get("Condition"):
                continue
            actions = _as_list(statement.get("Action", []))
            if not any(a.startswith("dynamodb:") for a in actions):
                continue
            resources = _as_list(statement.get("Resource", []))
            for table in TENANT_TABLES:
                if any(fnmatch.fnmatchcase(_table_arn(table), r) for r in resources):
                    tables.add(table)
    return tables


def test_the_unconditioned_base_table_list_is_derived_from_the_plan(
    control: list[dict[str, Any]], task: list[dict[str, Any]]
) -> None:
    """`UNCONDITIONED_BASE_TABLE_GRANTS` equals the set the planned statements grant."""
    assert _unconditioned_base_table_grant_tables(control, task) == set(
        UNCONDITIONED_BASE_TABLE_GRANTS
    )


def test_the_removed_and_conditioned_sets_are_disjoint_and_cover_every_seeded_table() -> None:
    """Each seeded table is conditioned once per role; no cell is both removed and kept."""
    assert len(set(REMOVED_GRANTS)) == len(REMOVED_GRANTS)
    assert len(_conditioned_cells()) == len(CONDITIONED_ACTIONS) * len(sweep_seed_key_patterns())
    assert not {(m, t, a) for m, t, a, _ in REMOVED_GRANTS} & set(_conditioned_cells())
    assert set(sweep_seed_key_patterns()) < set(UNCONDITIONED_BASE_TABLE_GRANTS)


@pytest.mark.parametrize(
    ("module", "table", "action", "index"),
    REMOVED_GRANTS,
    ids=[f"{m.rsplit('_', 1)[1]}-{t}-{a.split(':')[1]}" for m, t, a, _ in REMOVED_GRANTS],
)
def test_a_removed_grant_has_no_allow_and_an_explicit_deny(
    control: list[dict[str, Any]],
    task: list[dict[str, Any]],
    module: str,
    table: str,
    action: str,
    index: str | None,
) -> None:
    statements = {CONTROL: control, TASK: task}[module]
    assert _removed_grant_failure(statements, table, action, index) is None


@pytest.mark.parametrize(
    ("module", "table", "action"),
    _conditioned_cells(),
    ids=[f"{m.rsplit('_', 1)[1]}-{t}-{a.split(':')[1]}" for m, t, a in _conditioned_cells()],
)
def test_a_conditioned_grant_carries_its_tables_key_pattern(
    control: list[dict[str, Any]],
    task: list[dict[str, Any]],
    module: str,
    table: str,
    action: str,
) -> None:
    statements = {CONTROL: control, TASK: task}[module]
    assert _conditioned_grant_failure(statements, table, action) is None


def test_sweep_task_reads_proposals_by_key_without_a_condition(
    task: list[dict[str, Any]],
) -> None:
    """Proposals are read by agent-generated `proposal_id` (SWEEP-CONTROL-1)."""
    for table in SWEEP_TASK_UNCONDITIONED_READS:
        (statement,) = _allows(task, "dynamodb:GetItem", _table_arn(table))
        assert not statement.get("Condition"), (table, statement)


def test_teardown_batch_write_stays_unconditioned_on_the_seeded_tables(
    control: list[dict[str, Any]],
) -> None:
    """Teardown deletes agent-created rows whose ids carry no tenant (SWEEP-CONTROL-1)."""
    for table in sweep_seed_key_patterns():
        (statement,) = _allows(control, "dynamodb:BatchWriteItem", _table_arn(table))
        assert not statement.get("Condition"), (table, statement)
        assert _as_list(statement["Action"]) == ["dynamodb:BatchWriteItem"], statement


#: Scratch edits to `eval_sfn.tf`, each turning exactly its cells red: re-adding a removed grant,
#: dropping a key condition, and dropping the demo grant's `Select = COUNT` condition.
_MUTATIONS: Final[tuple[tuple[str, str, tuple[tuple[str, str, str], ...]], ...]] = (
    (
        '{ table = "eval_results", actions = ["dynamodb:GetItem", "dynamodb:PutItem"], '
        'index_names = [] },\n\n    { table = "accounts"',
        '{ table = "eval_results", actions = ["dynamodb:GetItem", "dynamodb:PutItem", '
        '"dynamodb:Query"], index_names = [] },\n\n    { table = "accounts"',
        ((CONTROL, "eval_results", "dynamodb:Query"),),
    ),
    (
        '{ table = "notes", actions = ["dynamodb:GetItem"], index_names = [], '
        'leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", '
        'values = [local.sweep_seed_key_patterns["notes"]] } },',
        '{ table = "notes", actions = ["dynamodb:GetItem"], index_names = [] },',
        ((TASK, "notes", "dynamodb:GetItem"),),
    ),
    (
        '= ["COUNT"]\n',
        "= []\n",
        tuple((CONTROL, table, "dynamodb:Query") for table in TENANT_TABLES),
    ),
)


def test_a_scratch_mutation_reds_exactly_its_cell(
    tmp_path_factory: pytest.TempPathFactory,
    control: list[dict[str, Any]],
    task: list[dict[str, Any]],
) -> None:
    """Control: each scratch edit to a copy of `eval_sfn.tf` reds exactly its cells and no other."""
    assert _cell_failures({CONTROL: control, TASK: task}) == {}
    text = EVAL_SFN_TF.read_text(encoding="utf-8")
    for old, new, _cell in _MUTATIONS:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    assert parse_sweep_seed_key_patterns(text) == sweep_seed_key_patterns()
    _require_harness()
    work_dir = tmp_path_factory.mktemp("eval-sfn-mutated")
    mutated = plan_root(work_dir, _fixture_root(work_dir, text))
    failures = _cell_failures(
        {
            CONTROL: _role_statements(mutated, CONTROL, f"{CONTROL_NAME}-role"),
            TASK: _role_statements(mutated, TASK, f"{TASK_NAME}-role"),
        }
    )
    assert set(failures) == {cell for _old, _new, cells in _MUTATIONS for cell in cells}, failures


def test_only_sweep_control_reaches_the_artifacts_prefix(
    control: list[dict[str, Any]], task: list[dict[str, Any]]
) -> None:
    obj = f"{ARTIFACTS_BUCKET_ARN}/stub-eval_artifacts-artifact_prefix/{allocate_run_id()}/matrix.json"
    for action in ("s3:PutObject", "s3:GetObject"):
        assert _decision(control, action, obj) == "allowed", action
        assert _decision(task, action, obj) == "implicitDeny", action
    assert _decision(control, "s3:PutObject", f"{ARTIFACTS_BUCKET_ARN}/agent-source/agent.zip") == (
        "implicitDeny"
    )
    assert _decision(control, "s3:ListBucket", ARTIFACTS_BUCKET_ARN) == "implicitDeny"


def test_only_sweep_task_invokes_the_runtime_on_both_arns(
    plan: Plan, control: list[dict[str, Any]], task: list[dict[str, Any]]
) -> None:
    runtime = plan.after(f"{TASK}.aws_lambda_function.this")["environment"][0]["variables"][
        "AGENT_RUNTIME_ARN"
    ]
    endpoint = f"{runtime}/runtime-endpoint/DEFAULT"
    for arn in (runtime, endpoint):
        assert _decision(task, "bedrock-agentcore:InvokeAgentRuntime", arn) == "allowed", arn
        assert _decision(control, "bedrock-agentcore:InvokeAgentRuntime", arn) == "implicitDeny"
    for statements in (control, task):
        for action in ("lambda:InvokeFunction", "states:StartExecution", "states:StopExecution"):
            assert _decision(statements, action, STATE_MACHINE_ARN) == "implicitDeny", action
            assert _decision(statements, action, CONTROL_ARN) == "implicitDeny", action


def test_the_evaluator_refuses_an_unkeyed_leading_keys_evaluation(
    task: list[dict[str, Any]],
) -> None:
    """Control: a conditioned statement without a key is a defect, never a Deny."""
    with pytest.raises(AssertionError, match="vacuously"):
        _decision(task, "dynamodb:UpdateItem", _table_arn("eval_runs"))


def _policy_size_failures(plan: Plan) -> list[tuple[str, int]]:
    """`(address or role, chars)` for every sweep-role policy over the IAM budget."""
    failures: list[tuple[str, int]] = []
    for module, role in ((CONTROL, f"{CONTROL_NAME}-role"), (TASK, f"{TASK_NAME}-role")):
        inline = 0
        for address in plan.addresses_of_type("aws_iam_role_policy"):
            after = plan.after(address)
            if after.get("role") == role:
                inline += _policy_document_chars(json.loads(after["policy"]))
        if inline > INLINE_POLICY_AGGREGATE_LIMIT_CHARS * FAIL_USAGE_FRACTION:
            failures.append((role, inline))
        for address in plan.addresses_of_type("aws_iam_policy"):
            if address.startswith(f"{module}."):
                size = _policy_document_chars(json.loads(plan.after(address)["policy"]))
                if size > MANAGED_POLICY_DOCUMENT_LIMIT_CHARS * FAIL_USAGE_FRACTION:
                    failures.append((address, size))
    return failures


def test_policy_sizes_stay_under_the_iam_budget(plan: Plan) -> None:
    assert _policy_size_failures(plan) == []


def _managed_dynamodb_keys(plan: Plan, module: str) -> list[str]:
    prefix = f'{module}.aws_iam_policy.dynamodb["'
    return [
        address.removeprefix(prefix).removesuffix('"]')
        for address in plan.addresses_of_type("aws_iam_policy")
        if address.startswith(prefix)
    ]


def _grant_row_count(local_name: str) -> int:
    """The number of grant objects `local.<local_name>` in `eval_sfn.tf` declares, comprehensions
    over a string-list local counted once per listed table."""
    text = EVAL_SFN_TF.read_text(encoding="utf-8")
    start = text.index(f"  {local_name} = ")
    body = text[
        start : text.index("\n  ]\n", start)
        if local_name.endswith("control_dynamodb_grants")
        else text.index("\n  )\n", start)
    ]
    literal = len(re.findall(r"^\s*\{ table = \"", body, re.MULTILINE))
    comprehended = 0
    for list_name in re.findall(r"\[for table in local\.(\w+) :", body):
        listed = re.search(rf"^  {list_name}\s*=\s*\[([^\]]*)\]", text, re.MULTILINE)
        assert listed is not None, list_name
        comprehended += len(re.findall(r'"[^"]+"', listed.group(1)))
    return literal + comprehended


def _allow_sid_indexes(statements: list[dict[str, Any]]) -> list[int]:
    indexes = []
    for statement in statements:
        match = re.fullmatch(r"DynamoDb[A-Za-z]+(\d+)", str(statement.get("Sid")))
        assert match is not None, statement.get("Sid")
        indexes.append(int(match.group(1)))
    return sorted(indexes)


def test_sweep_control_splits_its_allows_into_ordered_chunks(plan: Plan) -> None:
    """The chunks together hold exactly the document's Allow statements, each once."""
    keys = _managed_dynamodb_keys(plan, CONTROL)
    assert sorted(keys) == ["allow", "allow-2", "deny"], keys
    chunks: list[dict[str, Any]] = []
    for key in ("allow", "allow-2"):
        address = f'{CONTROL}.aws_iam_policy.dynamodb["{key}"]'
        statements = _statements(plan.after(address)["policy"])
        assert statements and all(s["Effect"] == "Allow" for s in statements), key
        chunks.extend(statements)
    rows = _grant_row_count("sweep_control_dynamodb_grants")
    assert _allow_sid_indexes(chunks) == list(range(rows))
    attachments = [
        a
        for a in plan.addresses_of_type("aws_iam_role_policy_attachment")
        if a.startswith(f"{CONTROL}.aws_iam_role_policy_attachment.dynamodb_allow[")
    ]
    assert attachments == [
        f"{CONTROL}.aws_iam_role_policy_attachment.dynamodb_allow[0]",
        f"{CONTROL}.aws_iam_role_policy_attachment.dynamodb_allow[1]",
    ], attachments


def test_an_unsplit_module_user_keeps_its_single_allow_address(plan: Plan) -> None:
    """sweep_task sets no chunk size: one `allow` policy and one `dynamodb_allow[0]`, as before."""
    assert sorted(_managed_dynamodb_keys(plan, TASK)) == ["allow", "deny"]
    attachments = [
        a
        for a in plan.addresses_of_type("aws_iam_role_policy_attachment")
        if a.startswith(f"{TASK}.aws_iam_role_policy_attachment.dynamodb_")
    ]
    assert attachments == [
        f"{TASK}.aws_iam_role_policy_attachment.dynamodb_allow[0]",
        f"{TASK}.aws_iam_role_policy_attachment.dynamodb_deny[0]",
    ], attachments
    allow = _statements(plan.after(f'{TASK}.aws_iam_policy.dynamodb["allow"]')["policy"])
    assert _allow_sid_indexes(allow) == list(range(_grant_row_count("sweep_task_dynamodb_grants")))


def test_every_allow_attachment_depends_on_the_deny_attachment(plan: Plan) -> None:
    calls = plan.configuration["root_module"]["module_calls"]
    for module in (CONTROL, TASK):
        resources = calls[module.removeprefix("module.")]["module"]["resources"]
        (allow,) = [
            r for r in resources if r["address"] == "aws_iam_role_policy_attachment.dynamodb_allow"
        ]
        assert allow.get("depends_on") == ["aws_iam_role_policy_attachment.dynamodb_deny"], allow


def test_an_oversized_chunk_reds_the_size_cell(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Control: without the chunk size, sweep_control's single Allow policy exceeds the budget."""
    text = EVAL_SFN_TF.read_text(encoding="utf-8")
    setting = re.search(r"^  dynamodb_allow_statements_per_policy = \d+\n", text, re.MULTILINE)
    assert setting is not None
    _require_harness()
    work_dir = tmp_path_factory.mktemp("eval-sfn-unchunked")
    mutated = plan_root(work_dir, _fixture_root(work_dir, text.replace(setting.group(0), "")))
    assert [address for address, _ in _policy_size_failures(mutated)] == [
        f'{CONTROL}.aws_iam_policy.dynamodb["allow"]'
    ]


def test_the_state_machine_arn_is_a_root_output(plan: Plan) -> None:
    outputs = plan.configuration["root_module"]["outputs"]
    assert STATE_MACHINE in outputs["eval_state_machine_arn"]["expression"]["references"]
