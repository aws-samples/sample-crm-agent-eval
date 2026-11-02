# SPDX-License-Identifier: MIT
"""Layer 4 relay-execution-role IAM matrix, with a positive control for every deny row.

Simulates the deployed relay role: the allow rows (A1-A5), the deny rows (D1-D19, D21-D23), and the inverse
matrix (D15/D16) asserting every action-Lambda execution role is denied invoking the Runtime, and
`X1` asserting the chat-target role's `NotAction` Deny covers unpermitted actions on the runtime
and its endpoint. An unreadable Runtime invoker list is the failing row `I0`. `U1` reads the
deployed Function URL's `AuthType` and requires `AWS_IAM`.

`SimulatePrincipalPolicy` returns `implicitDeny` for a misspelled action or a resource ARN that names
nothing, so every deny row is paired with the same `(action, resource)` simulated against a
principal that legitimately holds it; if that positive control does not evaluate `allowed`, both
rows report `fail`. Each action string and resource ARN is one shared constant used by both rows,
and ARNs are read from deployed state wherever one exists (the Runtime's `BedrockInference`
statement for D9, `lambda list-functions` for D6, the relay's function configuration for D14).

A missing Terraform output is a `fail` naming the output.

Usage:
    python3 -m scripts.smoke.relay_iam [--profile P] [--region R] [--outputs PATH] [--json]
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Final

from scripts.smoke.harness import (
    CheckResult,
    ExpectedCheckCount,
    SmokeContext,
    Status,
    run_main,
)

# `_simulate` owns the throttle backoff and the `no_result` disposition; `_role_name`,
# `ROLE_NAME_TEMPLATE` and `_NAME_PREFIX` own the deployment's naming conventions.
from scripts.smoke.iam_roles import (
    _NAME_PREFIX,
    _SIMULATE_BACKOFF_SECONDS,
    _SIMULATE_MAX_ATTEMPTS,
    _function_grants,
    _is_no_such_entity,
    _is_throttle,
    _load_matrix,
    _role_name,
    _root_composition_text,
    _simulate,
)

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "relay_iam"

# ---------------------------------------------------------------------------
# Shared vocabulary. One spelling per action, used by the allow rows and the deny rows alike.
# ---------------------------------------------------------------------------

ACTION_INVOKE_RUNTIME: Final[str] = "bedrock-agentcore:InvokeAgentRuntime"
ACTION_INVOKE_RUNTIME_COMMAND: Final[str] = "bedrock-agentcore:InvokeAgentRuntimeCommand"
ACTION_INVOKE_RUNTIME_COMMAND_SHELL: Final[str] = "bedrock-agentcore:InvokeAgentRuntimeCommandShell"
ACTION_INVOKE_RUNTIME_FOR_USER: Final[str] = "bedrock-agentcore:InvokeAgentRuntimeForUser"
#: Three further routes to a runtime, from the service authorization reference's "Actions defined
#: by Amazon Bedrock Agentcore" table. Two are absent from the pinned botocore model.
ACTION_INVOKE_RUNTIME_WS_STREAM: Final[str] = (
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream"
)
ACTION_INVOKE_RUNTIME_WS_STREAM_FOR_USER: Final[str] = (
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStreamForUser"
)
ACTION_INVOKE_HARNESS: Final[str] = "bedrock-agentcore:InvokeHarness"

#: A name AWS will never ship, used as the positive control for coverage-by-exclusion.
ACTION_NEVER_SHIPPED: Final[str] = "bedrock-agentcore:NoSuchActionExistsWithThisName"
ACTION_CREATE_RUNTIME: Final[str] = "bedrock-agentcore:CreateAgentRuntime"
ACTION_UPDATE_RUNTIME: Final[str] = "bedrock-agentcore:UpdateAgentRuntime"
ACTION_LOGS_CREATE_STREAM: Final[str] = "logs:CreateLogStream"
ACTION_LOGS_PUT_EVENTS: Final[str] = "logs:PutLogEvents"
ACTION_SSM_GET_PARAMETER: Final[str] = "ssm:GetParameter"
ACTION_SSM_GET_PARAMETERS: Final[str] = "ssm:GetParameters"
ACTION_SSM_GET_PARAMETERS_BY_PATH: Final[str] = "ssm:GetParametersByPath"
ACTION_LAMBDA_INVOKE: Final[str] = "lambda:InvokeFunction"
ACTION_DYNAMODB_GET_ITEM: Final[str] = "dynamodb:GetItem"
ACTION_DYNAMODB_PUT_ITEM: Final[str] = "dynamodb:PutItem"
ACTION_BEDROCK_INVOKE_MODEL: Final[str] = "bedrock:InvokeModel"
ACTION_SECRETS_GET_VALUE: Final[str] = "secretsmanager:GetSecretValue"
ACTION_IAM_PASS_ROLE: Final[str] = "iam:PassRole"  # noqa: S105 - IAM action name
ACTION_KMS_DECRYPT: Final[str] = "kms:Decrypt"

#: Decisions that satisfy a deny row. `no_result` is not one of them.
DENIED: Final[frozenset[str]] = frozenset({"explicitDeny", "implicitDeny"})

#: `_simulate`'s verdict when IAM returned no `EvaluationResults`. It is neither a deny nor an allow,
#: so it gets `defer` and this reason rather than sharing `fail` with a determinate over-grant.
DECISION_NO_RESULT: Final[str] = "no_result"
DEFER_REASON_NO_EVALUATION: Final[str] = "simulate_returned_no_evaluation_result"

#: The stricter expectation for D1-D4: `infra/modules/agent_relay/main.tf`'s
#: `DenyEverythingButTheOnePermittedInvokeOnThisRuntime` statement makes these an explicit `Deny`,
#: so `implicitDeny` here is a failure.
EXPLICIT_DENY_ONLY: Final[frozenset[str]] = frozenset({"explicitDeny"})

#: The env var carrying the relay's one legitimate SSM parameter name (the expected JWT audience),
#: read from the deployed function's own environment.
_AUDIENCE_PARAM_ENV: Final[str] = "COGNITO_AUDIENCE_PARAMETER"
_QUALIFIER_ENV: Final[str] = "AGENT_RUNTIME_QUALIFIER"

#: Statement sid on the Runtime role whose `Resource` list carries the inference-profile ARNs.
_RUNTIME_BEDROCK_SID: Final[str] = "BedrockInference"

#: Statement sid on the Runtime role granting `lambda:InvokeFunction` on the action Lambdas -- the
#: positive control for D6.
_RUNTIME_INVOKE_LAMBDAS_SID: Final[str] = "InvokeActionLambdas"

__all__ = [
    "DENIED",
    "EXPLICIT_DENY_ONLY",
    "LAYER",
    "RESOURCE_CLASS",
    "DenyRow",
    "checks",
    "main",
    "name_from_arn",
    "other_runtime_arn",
]


@dataclass(frozen=True)
class DenyRow:
    """One deny row, with its positive-control pairing.

    `positive_role_arn` is the principal that legitimately holds `(action, resource)`. `None` means
    no principal in this deployment holds it, in which case the row carries an independent
    expressibility assertion instead (D12's `iam get-role`). `positive_custom_policy` replaces the
    principal with a synthetic Allow on exactly `(action, resource)` evaluated by
    `iam:SimulateCustomPolicy`, so expressibility does not depend on which role holds the grant.
    """

    row_id: str
    action: str
    resource: str
    expected: frozenset[str]
    severity: str
    positive_role_arn: str | None
    positive_label: str
    positive_custom_policy: bool = False


def _simulate_custom_allow(ctx: SmokeContext, action: str, resource: str) -> str:
    """The decision `iam:SimulateCustomPolicy` reaches for a synthetic Allow on `(action, resource)`.

    `no_result` when IAM returns no evaluation result; throttles are retried, then raised.
    """
    document = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": action, "Resource": resource}],
        }
    )
    iam = ctx.client("iam")
    for attempt in range(1, _SIMULATE_MAX_ATTEMPTS + 1):
        try:
            response = iam.simulate_custom_policy(
                PolicyInputList=[document], ActionNames=[action], ResourceArns=[resource]
            )
            break
        except Exception as exc:  # noqa: BLE001 - re-raised unless a retryable throttle
            if attempt == _SIMULATE_MAX_ATTEMPTS or not _is_throttle(exc):
                raise
            time.sleep(_SIMULATE_BACKOFF_SECONDS * attempt)
    results = response.get("EvaluationResults", [])
    return str(results[0]["EvalDecision"]) if results else DECISION_NO_RESULT


def other_runtime_arn(runtime_arn: str) -> str:
    """A syntactically valid ARN for a runtime this deployment does not own, for D5.

    Built by substituting only the final path segment of the real ARN, so every other field is
    known-good.
    """
    head, separator, tail = runtime_arn.rpartition("/")
    if not separator:
        return f"{runtime_arn}-not-this-one"
    return f"{head}/{tail}-not-this-one"


def harness_arn(runtime_arn: str) -> str:
    """A syntactically valid harness ARN in the runtime's account and region, for D23.

    `bedrock-agentcore:InvokeHarness` authorizes against the `harness*` resource types, not
    `runtime*`. Built from the runtime ARN's first five colon-fields; only the resource type and a
    fabricated id differ.
    """
    fields = runtime_arn.split(":")
    if len(fields) < 5:
        return f"{runtime_arn}:harness/smoke-nonexistent-harness"
    return f"{':'.join(fields[:5])}:harness/smoke-nonexistent-harness"


def name_from_arn(arn: str) -> str:
    """The trailing name of an ARN's path, or `""` if it has no path separator.

    The separator is checked, since `rpartition` on a string with no `/` returns the whole string.
    """
    head, separator, tail = arn.rpartition("/")
    return tail if separator and head else ""


def _decision_detail(row_id: str, action: str, resource: str, decision: str) -> str:
    return f"{row_id}: {action} on {resource} evaluated {decision}"


def _relay_function_configuration(ctx: SmokeContext, function_name: str) -> Mapping[str, Any]:
    configuration = ctx.client("lambda").get_function_configuration(FunctionName=function_name)
    return configuration if isinstance(configuration, Mapping) else {}


def _runtime_endpoint_arn(ctx: SmokeContext, runtime_arn: str, qualifier: str) -> str:
    """The endpoint ARN as AWS reports it, read from `list-agent-runtime-endpoints`.

    Returns `""` when it cannot be resolved; the caller reports that as a `fail`.
    """
    runtime_id = runtime_arn.rpartition("/")[2]
    if not runtime_id:
        return ""
    endpoints = ctx.client("bedrock-agentcore-control").list_agent_runtime_endpoints(
        agentRuntimeId=runtime_id
    )
    for endpoint in endpoints.get("runtimeEndpoints") or []:
        if not isinstance(endpoint, Mapping):
            continue
        if str(endpoint.get("name")) == qualifier or str(endpoint.get("id")) == qualifier:
            arn = endpoint.get("agentRuntimeEndpointArn")
            return arn if isinstance(arn, str) else ""
    return ""


def _role_policy_statements(ctx: SmokeContext, role_name: str) -> list[Mapping[str, Any]]:
    """Every inline-policy statement on `role_name`, flattened.

    Inline only: the ARNs read for D6/D9's positive controls are granted by the Runtime role's own
    inline policies.
    """
    iam = ctx.client("iam")
    statements: list[Mapping[str, Any]] = []
    for policy_name in iam.list_role_policies(RoleName=role_name).get("PolicyNames", []):
        document = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name).get(
            "PolicyDocument"
        )
        if isinstance(document, str):
            document = json.loads(document)
        if not isinstance(document, Mapping):
            continue
        raw = document.get("Statement", [])
        entries = raw if isinstance(raw, list) else [raw]
        statements.extend(entry for entry in entries if isinstance(entry, Mapping))
    return statements


def _resources_for_sid(statements: Sequence[Mapping[str, Any]], sid: str) -> list[str]:
    """Literal `Resource` entries of the statement carrying `sid`, in order."""
    for statement in statements:
        if statement.get("Sid") != sid:
            continue
        raw = statement.get("Resource", [])
        entries = raw if isinstance(raw, list) else [raw]
        return [entry for entry in entries if isinstance(entry, str)]
    return []


def _deployed_function_arns(ctx: SmokeContext, name_prefix: str) -> dict[str, str]:
    """`{function_name: arn}` for every deployed Lambda whose name starts with `name_prefix`.

    One paginated `list_functions` call; the ARNs are the deployed ones.
    """
    lambda_client = ctx.client("lambda")
    arns: dict[str, str] = {}
    marker: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Marker": marker} if marker else {}
        page = lambda_client.list_functions(**kwargs)
        for function in page.get("Functions", []):
            name = function.get("FunctionName")
            arn = function.get("FunctionArn")
            if isinstance(name, str) and isinstance(arn, str) and name.startswith(name_prefix):
                arns[name] = arn
        marker = page.get("NextMarker")
        if not marker:
            return arns


def _action_tool_names() -> list[str]:
    """The action Lambdas D6 covers, derived from the toolspec.

    Taken from `crm.toolspec.definitions.ALL_TOOLS`, not from the grant matrix, which also names
    non-tool functions the Runtime role does not invoke. Includes the agent-only `get_page_context`.
    """
    from crm.toolspec.definitions import ALL_TOOLS

    names = sorted(tool.name for tool in ALL_TOOLS)
    if not names:
        raise RuntimeError(
            "ALL_TOOLS is empty, so D6 would emit no rows and report a pass over nothing"
        )
    return names


_INVOKER_LIST_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*runtime_invoker_role_arns\s*=\s*\[(?P<body>[^\]]*)\]", re.MULTILINE
)
_INVOKER_MODULE_RE: Final[re.Pattern[str]] = re.compile(r"\bmodule\.(?P<module>\w+)\.role_arn\b")


def _runtime_invoker_functions() -> frozenset[str]:
    """Matrix function names whose roles `local.runtime_invoker_role_arns` admits to the Runtime.

    Read from the root `infra/*.tf` files: each `module.<name>.role_arn` in the list is resolved to
    that module block's `function_name`, minus the deployment prefix. Raises when the list is
    absent, empty, or names a module whose `function_name` cannot be read.
    """
    text = _root_composition_text()
    listed = _INVOKER_LIST_RE.search(text)
    if listed is None:
        raise RuntimeError("infra/*.tf declares no runtime_invoker_role_arns list")
    modules = _INVOKER_MODULE_RE.findall(listed.group("body"))
    if not modules:
        raise RuntimeError("runtime_invoker_role_arns names no module role")
    functions: set[str] = set()
    for module in modules:
        block = re.search(
            rf'^module\s+"{module}"\s*\{{(?P<body>.*?)^\}}', text, re.MULTILINE | re.DOTALL
        )
        name = (
            None
            if block is None
            else re.search(r'^\s*function_name\s*=\s*"([\w-]+)"', block.group("body"), re.MULTILINE)
        )
        if name is None:
            raise RuntimeError(f"cannot read function_name of runtime invoker module {module!r}")
        functions.add(name.group(1).removeprefix(f"{_NAME_PREFIX}-"))
    return frozenset(functions)


def _non_relay_role_arns(account_id: str, invokers: frozenset[str] | None = None) -> dict[str, str]:
    """Every action-Lambda execution role the Runtime must refuse, `{role_name: arn}`.

    Enumerated from the grant matrix + `ROLE_NAME_TEMPLATE` convention `iam_roles.py` uses,
    minus the Runtime invokers (`invokers`, read with `_runtime_invoker_functions` when omitted).
    `simulate_principal_policy` raises on a `PolicySourceArn` that does not exist.
    """
    if invokers is None:
        invokers = _runtime_invoker_functions()
    names = {
        _role_name(function)
        for function in _function_grants(_load_matrix())
        if function not in invokers
    }
    return {name: f"arn:aws:iam::{account_id}:role/{name}" for name in sorted(names)}


def _inverse_matrix_results(
    ctx: SmokeContext, *, role_name: str, role_arn: str, runtime_arn: str, endpoint_arn: str
) -> Iterator[CheckResult]:
    """D15/D16 for one non-relay role: it must be denied `InvokeAgentRuntime` on both resources.

    A simulate that raises fails that role's row only, so one absent or unreadable role cannot
    abort the rows that follow it.
    """
    for row_id, resource in (("D15", runtime_arn), ("D16", endpoint_arn)):
        check = f"{row_id}[{role_name}]_denied_invoking_runtime"
        try:
            decision, _ = _simulate(ctx, role_arn, ACTION_INVOKE_RUNTIME, resource)
        except Exception as exc:  # noqa: BLE001 - reported as this role's failing row, not a crash
            cause = (
                f"role {role_name} does not exist (NoSuchEntity). The enumeration names a role the "
                "deployment does not have; check the matrix key renders the deployed function name"
                if _is_no_such_entity(exc)
                else f"simulate_principal_policy raised {type(exc).__name__}: {exc}"
            )
            yield CheckResult(
                resource=role_arn,
                check=check,
                status="fail",
                detail=f"{row_id}: {ACTION_INVOKE_RUNTIME} on {resource} was not evaluated: "
                f"{cause}. Not a pass: no decision was observed",
            )
            continue
        satisfied = decision in DENIED
        yield CheckResult(
            resource=role_arn,
            check=check,
            status="pass" if satisfied else "fail",
            detail=(
                f"{row_id}: {ACTION_INVOKE_RUNTIME} on {resource} evaluated {decision}"
                + (
                    "; the relay's A1/A2 rows in this same run proved that exact action "
                    "string is allowable for somebody, so this deny is not a typo reading "
                    "as a pass"
                    if satisfied
                    else " -- expected a deny. Severity if allowed: Critical. The "
                    "trustworthiness of _agent_identity is a property of who can "
                    "invoke the Runtime"
                )
            ),
        )


def _paired_deny_results(
    ctx: SmokeContext, *, relay_role_arn: str, row: DenyRow, denied_label: str = "relay"
) -> list[CheckResult]:
    """The positive control and the deny row for one `DenyRow`, as two `CheckResult`s.

    `relay_role_arn` is the principal the deny row simulates; `denied_label` names it in the check.
    If the positive control does not evaluate `allowed`, both rows report `fail`.
    """
    results: list[CheckResult] = []
    if row.positive_custom_policy:
        try:
            positive_decision = _simulate_custom_allow(ctx, row.action, row.resource)
        except Exception as exc:  # noqa: BLE001 - a failing control, never a skip
            positive_decision = f"error ({type(exc).__name__}: {exc})"
    elif row.positive_role_arn is None:
        raise ValueError(f"{row.row_id} has no positive control; use an expressibility assertion")
    else:
        positive_decision, _ = _simulate(ctx, row.positive_role_arn, row.action, row.resource)
    pair_is_real = positive_decision == "allowed"
    results.append(
        CheckResult(
            resource=row.resource,
            check=f"{row.row_id}_positive_control",
            status="pass" if pair_is_real else "fail",
            detail=(
                f"{row.positive_label} evaluated {positive_decision} for {row.action} -- "
                + (
                    "the (action, resource) pair is expressible, so the deny row below means "
                    "something"
                    if pair_is_real
                    else "expected 'allowed'. The pair is malformed or the principal lost a "
                    "legitimate grant, so the deny row cannot distinguish a scoped policy from a "
                    "typo and is failed alongside this row rather than reported as a pass"
                )
            ),
        )
    )

    deny_decision, _ = _simulate(ctx, relay_role_arn, row.action, row.resource)
    satisfied = deny_decision in row.expected
    detail = _decision_detail(row.row_id, row.action, row.resource, deny_decision)
    defer_reason = ""
    if deny_decision == DECISION_NO_RESULT:
        # No evaluation result gets its own disposition, without a severity tag.
        status: Status = "defer"
        defer_reason = DEFER_REASON_NO_EVALUATION
        detail += (
            f"; expected one of {sorted(row.expected)} but IAM returned no evaluation result, so "
            "this row asserts nothing either way. Re-run before reading the inventory as complete"
        )
    elif not pair_is_real:
        status = "fail"
        detail += (
            f" (expected one of {sorted(row.expected)}; FAILED REGARDLESS because "
            f"{row.row_id}_positive_control did not establish the pair is expressible)"
        )
    elif satisfied:
        status = "pass"
        detail += (
            f", within the expected {sorted(row.expected)} (severity if allowed: {row.severity})"
        )
    else:
        status = "fail"
        detail += f", expected one of {sorted(row.expected)} -- severity if allowed: {row.severity}"
    results.append(
        CheckResult(
            resource=row.resource,
            check=f"{row.row_id}_{denied_label}_denied",
            status=status,
            detail=detail,
            defer_reason=defer_reason,
        )
    )
    return results


def _agent_less_rows(
    ctx: SmokeContext,
    *,
    function_key: str,
    function_arn: str,
    relay_role_arn: str,
    runtime_role_arn: str,
    granted_lambda_resources: set[str],
) -> list[CheckResult]:
    """D6 and R1 for a tool no agent holds: neither the relay nor the Runtime role may invoke it.

    No deployed role legitimately invokes such a tool, so each deny row's expressibility is a
    synthetic Allow on exactly the pair. The third row reads the policy text: the Runtime role's
    invoke statement must not name the function.
    """
    synthetic = (
        "a synthetic Allow on exactly this pair (no deployed role invokes an agent-less tool)"
    )
    results = list(
        _paired_deny_results(
            ctx,
            relay_role_arn=relay_role_arn,
            row=DenyRow(
                row_id=f"D6[{function_key}]",
                action=ACTION_LAMBDA_INVOKE,
                resource=function_arn,
                expected=DENIED,
                severity="Critical",
                positive_role_arn=None,
                positive_label=synthetic,
                positive_custom_policy=True,
            ),
        )
    )
    if runtime_role_arn:
        results += list(
            _paired_deny_results(
                ctx,
                relay_role_arn=runtime_role_arn,
                denied_label="runtime",
                row=DenyRow(
                    row_id=f"R1[{function_key}]",
                    action=ACTION_LAMBDA_INVOKE,
                    resource=function_arn,
                    expected=DENIED,
                    severity="High",
                    positive_role_arn=None,
                    positive_label=synthetic,
                    positive_custom_policy=True,
                ),
            )
        )
    named = any(fnmatchcase(function_arn, pattern) for pattern in granted_lambda_resources)
    results.append(
        CheckResult(
            resource=function_arn,
            check=f"R1[{function_key}]_not_granted_to_runtime_by_policy",
            status="fail" if named else "pass" if granted_lambda_resources else "skip",
            detail=(
                f"{function_key} has no agent in the toolspec, so the Runtime role's "
                f"{_RUNTIME_INVOKE_LAMBDAS_SID!r} statement must not name it; the statement names "
                f"{len(granted_lambda_resources)} resource pattern(s)"
                + (", one of which matches this function" if named else "")
                if granted_lambda_resources
                else f"no {_RUNTIME_INVOKE_LAMBDAS_SID!r} statement was readable on the Runtime "
                "role, so only the simulated R1 row covers this tool"
            ),
        )
    )
    return results


def _unpaired_deny_result(ctx: SmokeContext, *, relay_role_arn: str, row: DenyRow) -> CheckResult:
    """A deny row whose expressibility is established by something other than a positive control.

    Used only where no principal in the deployment legitimately holds the pair, so no positive
    control exists to run -- the caller must have already emitted the substitute assertion.
    """
    decision, _ = _simulate(ctx, relay_role_arn, row.action, row.resource)
    detail = _decision_detail(row.row_id, row.action, row.resource, decision)
    if decision == DECISION_NO_RESULT:
        return CheckResult(
            resource=row.resource,
            check=f"{row.row_id}_relay_denied",
            status="defer",
            detail=detail
            + (
                f"; expected one of {sorted(row.expected)} but IAM returned no evaluation result, "
                "so this row asserts nothing either way. Re-run before reading the inventory as "
                "complete"
            ),
            defer_reason=DEFER_REASON_NO_EVALUATION,
        )
    return CheckResult(
        resource=row.resource,
        check=f"{row.row_id}_relay_denied",
        status="pass" if decision in row.expected else "fail",
        detail=detail
        + f", expected one of {sorted(row.expected)} (severity if allowed: {row.severity})",
    )


def _allow_result(
    ctx: SmokeContext, *, role_arn: str, row_id: str, action: str, resource: str, why: str
) -> CheckResult:
    decision, _ = _simulate(ctx, role_arn, action, resource)
    return CheckResult(
        resource=resource,
        check=f"{row_id}_allowed",
        status="pass" if decision == "allowed" else "fail",
        detail=_decision_detail(row_id, action, resource, decision)
        + (", as required" if decision == "allowed" else f", expected 'allowed' -- {why}"),
    )


def _missing_output(name: str, remediation: str, exc: Exception) -> CheckResult:
    return CheckResult(
        resource=name,
        check="terraform_output_present",
        status="fail",
        detail=(
            f"required Terraform output {name!r} is absent ({exc}). {remediation} A missing output "
            "is a failure, not a skipped resource class: an ARN built from a template that no "
            "longer resolves returns implicitDeny and reads as a pass"
        ),
    )


def checks(ctx: SmokeContext) -> Iterator[CheckResult]:  # noqa: C901 - one row per matrix entry
    """Yield one `CheckResult` per matrix row, plus one per positive control.

    Never raises for a check failure: an absent output, an unresolvable ARN or an over-grant is a
    `fail` with an actionable detail.
    """

    try:
        relay_role_arn = ctx.outputs.require_str("agent_relay_role_arn")
        runtime_arn = ctx.outputs.require_str("agentcore_runtime_arn")
        relay_function_name = ctx.outputs.require_str("agent_relay_function_name")
    except Exception as exc:  # noqa: BLE001 - reported as a failing check, not a crash
        yield _missing_output(
            "agent_relay_role_arn|agentcore_runtime_arn|agent_relay_function_name",
            "These three are the matrix's own subject; without them no row can be simulated.",
            exc,
        )
        return

    account_id = str(ctx.client("sts").get_caller_identity()["Account"])
    configuration = _relay_function_configuration(ctx, relay_function_name)
    environment = configuration.get("Environment") or {}
    env_vars = environment.get("Variables") or {}
    qualifier = str(env_vars.get(_QUALIFIER_ENV) or "DEFAULT")
    endpoint_arn = _runtime_endpoint_arn(ctx, runtime_arn, qualifier)
    if not endpoint_arn:
        yield CheckResult(
            resource=runtime_arn,
            check="endpoint_arn_resolved_from_aws",
            status="fail",
            detail=(
                f"could not read the endpoint ARN for qualifier {qualifier!r} from "
                "list-agent-runtime-endpoints, so no endpoint row can be simulated. NOT falling back "
                "to a composed ARN: composing it is what made this matrix agree with a subject that "
                "shared the same wrong path segment."
            ),
        )
        return

    # --- A1-A4: the matrix's own non-vacuity proof. Never a `skip`. ---
    yield _allow_result(
        ctx,
        role_arn=relay_role_arn,
        row_id="A1",
        action=ACTION_INVOKE_RUNTIME,
        resource=runtime_arn,
        why="the relay's one reason to exist; if this is denied every deny row below "
        "is vacuously green",
    )
    yield _allow_result(
        ctx,
        role_arn=relay_role_arn,
        row_id="A2",
        action=ACTION_INVOKE_RUNTIME,
        resource=endpoint_arn,
        why="AgentCore authorizes hierarchically: the endpoint is a second resource and A1 "
        "passing does not imply A2",
    )

    try:
        log_group_name = ctx.outputs.require_str("agent_relay_log_group_name")
    except Exception as exc:  # noqa: BLE001
        log_group_name = ""
        yield _missing_output(
            "agent_relay_log_group_name",
            "The module already exposes it as `module.agent_relay.log_group_name`; re-export "
            "it at the root. A3/A4 cannot be simulated without it, and constructing the ARN "
            "from a naming convention is the exact false-pass this rule exists to prevent.",
            exc,
        )
    if log_group_name:
        # Both rows simulate the log-stream form (`:*`), the only form the relay's own policy grants
        # (`infra/modules/agent_relay/main.tf`).
        log_stream_arn = f"arn:aws:logs:{ctx.region}:{account_id}:log-group:{log_group_name}:*"
        yield _allow_result(
            ctx,
            role_arn=relay_role_arn,
            row_id="A3",
            action=ACTION_LOGS_CREATE_STREAM,
            resource=log_stream_arn,
            why="without it the relay cannot open a log stream and the only observability on "
            "the new front door is gone",
        )
        yield _allow_result(
            ctx,
            role_arn=relay_role_arn,
            row_id="A4",
            action=ACTION_LOGS_PUT_EVENTS,
            resource=log_stream_arn,
            why="log-stream ARNs carry the ':*' suffix, and that is the one form this role grants",
        )

    # --- A5: the one SSM parameter, and the positive control for D17-D19. ---
    parameter_arns: dict[str, str] = {}
    # A5's emitted status, which D17's expressibility guard asserts.
    allow_statuses: dict[str, Status] = {}
    for row_id, env_name, why in (
        (
            "A5",
            _AUDIENCE_PARAM_ENV,
            "the relay reads its expected JWT audience from this parameter at cold start "
            "and must fail hard rather than verify against nothing, so this is an availability "
            "control as much as a security one",
        ),
    ):
        parameter_name = env_vars.get(env_name)
        if not isinstance(parameter_name, str) or not parameter_name:
            yield CheckResult(
                resource=relay_function_name,
                check=f"{row_id}_allowed",
                status="fail",
                detail=(
                    f"the deployed relay sets no {env_name} environment variable, so the "
                    f"parameter ARN {row_id} must simulate against cannot be resolved from "
                    "deployed state. Constructing one would make this row self-certifying"
                ),
            )
            continue
        arn = f"arn:aws:ssm:{ctx.region}:{account_id}:parameter{parameter_name}"
        parameter_arns[env_name] = arn
        allow_result = _allow_result(
            ctx,
            role_arn=relay_role_arn,
            row_id=row_id,
            action=ACTION_SSM_GET_PARAMETER,
            resource=arn,
            why=why,
        )
        allow_statuses[row_id] = allow_result.status
        yield allow_result

    # --- D1-D4, D21-D23: every route to the runtime except the one permitted invoke. ---
    #
    # The deny is `NotAction`-shaped (`infra/modules/agent_relay/main.tf`), so a misspelled action also
    # returns `explicitDeny` and these rows do not certify action names; `tests/unit/test_infra_relay.py`
    # pins the statement's shape. D21-D23 are two WebSocket-stream invokes and the harness route.
    for row_id, action, resource, severity in (
        ("D1", ACTION_INVOKE_RUNTIME_COMMAND, runtime_arn, "Critical"),
        ("D2", ACTION_INVOKE_RUNTIME_COMMAND, endpoint_arn, "Critical"),
        ("D3", ACTION_INVOKE_RUNTIME_COMMAND_SHELL, runtime_arn, "Critical"),
        ("D4", ACTION_INVOKE_RUNTIME_FOR_USER, runtime_arn, "High"),
        ("D21", ACTION_INVOKE_RUNTIME_WS_STREAM, runtime_arn, "Critical"),
        ("D22", ACTION_INVOKE_RUNTIME_WS_STREAM_FOR_USER, runtime_arn, "High"),
        # D23 probes a harness ARN: InvokeHarness authorizes against `harness*`, not `runtime*`.
        ("D23", ACTION_INVOKE_HARNESS, harness_arn(runtime_arn), "High"),
    ):
        yield _unpaired_deny_result(
            ctx,
            relay_role_arn=relay_role_arn,
            row=DenyRow(
                row_id=row_id,
                action=action,
                resource=resource,
                expected=EXPLICIT_DENY_ONLY,
                severity=severity,
                positive_role_arn=None,
                positive_label="",
            ),
        )

    # A fabricated action name must also come back `explicitDeny`, showing coverage is by exclusion.
    unnamed_decision, _ = _simulate(ctx, relay_role_arn, ACTION_NEVER_SHIPPED, runtime_arn)
    yield CheckResult(
        resource=runtime_arn,
        check="deny_covers_actions_nobody_enumerated",
        status="pass" if unnamed_decision in EXPLICIT_DENY_ONLY else "fail",
        detail=(
            f"a deliberately non-existent action ({ACTION_NEVER_SHIPPED}) on this runtime evaluates "
            f"{unnamed_decision!r}"
            + (
                ", so the Deny is matching by exclusion: an invoke route AWS has not shipped yet is "
                "already denied. This is what an enumerated denylist could not give, and it is why "
                "D1-D4/D21-D23 no longer carry the expressibility burden"
                if unnamed_decision in EXPLICIT_DENY_ONLY
                else f", expected one of {sorted(EXPLICIT_DENY_ONLY)}. The Deny is NOT covering "
                "unenumerated actions, so it has reverted to an action list and a future service "
                "action would be uncovered -- the named deny rows above pass either way and cannot "
                "detect this"
            )
        ),
    )

    # --- D5: one-runtime scoping, with every ARN field but the last known-good. ---
    yield _unpaired_deny_result(
        ctx,
        relay_role_arn=relay_role_arn,
        row=DenyRow(
            row_id="D5",
            action=ACTION_INVOKE_RUNTIME,
            resource=other_runtime_arn(runtime_arn),
            expected=DENIED,
            severity="High",
            positive_role_arn=None,
            positive_label="",
        ),
    )
    yield CheckResult(
        resource=other_runtime_arn(runtime_arn),
        check="D5_expressibility",
        status="pass" if other_runtime_arn(runtime_arn) != runtime_arn else "fail",
        detail=(
            "D5's ARN is the real runtime ARN with only its final path segment substituted, so "
            "account, region, service and resource-type are known-good by construction and A1 "
            "already proved this exact shape is allowable. D5 failing therefore means the "
            "grant is scoped wider than one runtime, not that the ARN was typed badly"
        ),
    )

    # --- The Runtime role: positive control for D6 and D9. ---
    runtime_role_arn = ""
    runtime_statements: list[Mapping[str, Any]] = []
    try:
        runtime_role_arn = ctx.outputs.require_str("agentcore_runtime_role_arn")
    except Exception as exc:  # noqa: BLE001
        yield _missing_output(
            "agentcore_runtime_role_arn",
            "It is the positive control for D6 (lambda:InvokeFunction on the action Lambdas) "
            "and D9 (bedrock:InvokeModel), both of which it legitimately holds. Without it "
            "those deny rows cannot be distinguished from a typo and are failed, not passed.",
            exc,
        )
    if runtime_role_arn:
        runtime_statements = _role_policy_statements(ctx, name_from_arn(runtime_role_arn))

    # --- D6: the relay dispatches no tools. One row per deployed action Lambda. ---
    function_keys = _action_tool_names()
    agent_less = set(_agent_less_tool_names())
    deployed = _deployed_function_arns(ctx, f"{_NAME_PREFIX}-")
    granted_lambda_resources = set(
        _resources_for_sid(runtime_statements, _RUNTIME_INVOKE_LAMBDAS_SID)
    )
    for function_key in function_keys:
        function_name = f"{_NAME_PREFIX}-{function_key}"
        function_arn = deployed.get(function_name)
        if function_arn is None:
            yield CheckResult(
                resource=function_name,
                check="D6_positive_control",
                status="fail",
                detail=(
                    f"the matrix names function {function_key!r} but no deployed Lambda is "
                    f"called {function_name!r}. A constructed ARN for a function that does not "
                    "exist returns implicitDeny for D6 and reads as a pass, so this is a "
                    "failure rather than a skipped row"
                ),
            )
            continue
        if function_key in agent_less:
            yield from _agent_less_rows(
                ctx,
                function_key=function_key,
                function_arn=function_arn,
                relay_role_arn=relay_role_arn,
                runtime_role_arn=runtime_role_arn,
                granted_lambda_resources=granted_lambda_resources,
            )
            continue
        if runtime_role_arn:
            yield from _paired_deny_results(
                ctx,
                relay_role_arn=relay_role_arn,
                row=DenyRow(
                    row_id=f"D6[{function_key}]",
                    action=ACTION_LAMBDA_INVOKE,
                    resource=function_arn,
                    expected=DENIED,
                    severity="Critical",
                    positive_role_arn=runtime_role_arn,
                    positive_label=(
                        "the Runtime role, which legitimately holds InvokeActionLambdas"
                    ),
                ),
            )
        yield CheckResult(
            resource=function_arn,
            check=f"D6[{function_key}]_granted_to_runtime_by_policy",
            status="pass" if granted_lambda_resources else "skip",
            detail=(
                f"the Runtime role's {_RUNTIME_INVOKE_LAMBDAS_SID!r} statement scopes "
                f"{len(granted_lambda_resources)} resource entry/entries, corroborating the "
                "simulated positive control from the policy text as well as from IAM's decision"
                if granted_lambda_resources
                else f"no {_RUNTIME_INVOKE_LAMBDAS_SID!r} statement was readable on the "
                "Runtime role, so only the simulated positive control corroborates D6"
            ),
        )

    # --- D7/D8: the relay never reads or writes CRM data directly. ---
    try:
        table_names: Mapping[str, Any] = ctx.outputs.require_mapping("dynamodb_table_names")
    except Exception as exc:  # noqa: BLE001
        table_names = {}
        yield _missing_output(
            "dynamodb_table_names",
            "D7/D8 iterate it, so an absent or non-mapping value emits ZERO of the 2-per-table "
            "rows -- no skip, no fail, no trace, and the module still exits 0. A guard on each "
            "iteration says nothing about zero iterations.",
            exc,
        )
    # The table mapping's completeness is asserted directly against the tenant-scoped table set, since
    # `expected_check_count` scales with the same mapping.
    from crm.store.schema import TENANT_SCOPED_TABLES

    missing_tables = sorted(set(TENANT_SCOPED_TABLES) - set(table_names))
    yield CheckResult(
        resource="dynamodb_table_names",
        check="table_mapping_covers_every_tenant_scoped_table",
        status="pass" if not missing_tables else "fail",
        detail=(
            f"the output names all {len(TENANT_SCOPED_TABLES)} tenant-scoped table(s), so D7/D8 run "
            "2 rows against each of them"
            if not missing_tables
            else f"the output is missing {missing_tables} of "
            f"{sorted(TENANT_SCOPED_TABLES)}, so D7/D8 silently emit "
            f"{2 * len(missing_tables)} fewer High no-direct-CRM-data row(s). The per-table loop guards each "
            "iteration and says nothing about a table that never reached it"
        ),
    )
    for logical_name, physical_name in sorted(table_names.items()):
        table_arn = f"arn:aws:dynamodb:{ctx.region}:{account_id}:table/{physical_name}"
        for row_id, action in (("D7", ACTION_DYNAMODB_GET_ITEM), ("D8", ACTION_DYNAMODB_PUT_ITEM)):
            yield from _paired_deny_results(
                ctx,
                relay_role_arn=relay_role_arn,
                row=DenyRow(
                    row_id=f"{row_id}[{logical_name}]",
                    action=action,
                    resource=table_arn,
                    expected=DENIED,
                    severity="High",
                    positive_role_arn=None,
                    positive_label=(
                        f"a synthetic Allow on exactly ({action}, {table_arn}) via "
                        "iam:SimulateCustomPolicy"
                    ),
                    positive_custom_policy=True,
                ),
            )

    # --- D9: model access belongs to the Runtime role, and its spend to the instrument. ---
    profile_arns = [
        arn
        for arn in _resources_for_sid(runtime_statements, _RUNTIME_BEDROCK_SID)
        if ":inference-profile/" in arn
    ]
    if not runtime_role_arn:
        pass  # already reported as a missing output above
    elif not profile_arns:
        yield CheckResult(
            resource=runtime_role_arn,
            check="D9_positive_control",
            status="fail",
            detail=(
                f"the Runtime role's {_RUNTIME_BEDROCK_SID!r} statement named no "
                "inference-profile ARN, so D9 has no deployed ARN to deny against. "
                "Constructing one from config/model_capabilities.yaml would make the row "
                "self-certifying if the model id had drifted"
            ),
        )
    else:
        yield from _paired_deny_results(
            ctx,
            relay_role_arn=relay_role_arn,
            row=DenyRow(
                row_id="D9",
                action=ACTION_BEDROCK_INVOKE_MODEL,
                resource=profile_arns[0],
                expected=DENIED,
                severity="High",
                positive_role_arn=runtime_role_arn,
                positive_label=(
                    "the Runtime role, read from the very statement that grants it, so the "
                    "ARN cannot be the wrong one"
                ),
            ),
        )

    # --- D10/D11: control-plane separation. ---
    for row_id, action, resource, severity in (
        ("D10", ACTION_CREATE_RUNTIME, "*", "Medium"),
        ("D11", ACTION_UPDATE_RUNTIME, runtime_arn, "Medium"),
    ):
        yield _unpaired_deny_result(
            ctx,
            relay_role_arn=relay_role_arn,
            row=DenyRow(
                row_id=row_id,
                action=action,
                resource=resource,
                expected=DENIED,
                severity=severity,
                positive_role_arn=None,
                positive_label="",
            ),
        )

    # --- D12: PassRole. No principal here holds it, so expressibility is asserted directly. ---
    runtime_role_name = name_from_arn(runtime_role_arn) if runtime_role_arn else ""
    if runtime_role_name:
        try:
            resolved = ctx.client("iam").get_role(RoleName=runtime_role_name)
            resolved_arn = str((resolved.get("Role") or {}).get("Arn") or "")
        except Exception as exc:  # noqa: BLE001
            resolved_arn = ""
            yield CheckResult(
                resource=runtime_role_name,
                check="D12_target_role_resolves",
                status="fail",
                detail=(
                    f"iam get-role failed for {runtime_role_name!r} ({exc}), so D12's target "
                    "ARN names nothing and its implicitDeny would read as a pass. This is the "
                    "one ARN in the matrix with no positive control available, which is why "
                    "resolution is asserted instead"
                ),
            )
        if resolved_arn:
            yield CheckResult(
                resource=resolved_arn,
                check="D12_target_role_resolves",
                status="pass",
                detail=(
                    "the PassRole target resolves via iam get-role, so D12's implicitDeny is "
                    "about the grant rather than about a non-existent role"
                ),
            )
            yield _unpaired_deny_result(
                ctx,
                relay_role_arn=relay_role_arn,
                row=DenyRow(
                    row_id="D12",
                    action=ACTION_IAM_PASS_ROLE,
                    resource=resolved_arn,
                    expected=DENIED,
                    severity="High",
                    positive_role_arn=None,
                    positive_label="",
                ),
            )

    # --- D13: Secrets Manager. ---
    yield _unpaired_deny_result(
        ctx,
        relay_role_arn=relay_role_arn,
        row=DenyRow(
            row_id="D13",
            action=ACTION_SECRETS_GET_VALUE,
            resource="*",
            expected=DENIED,
            severity="Medium",
            positive_role_arn=None,
            positive_label="",
        ),
    )

    # --- D14: the environment CMK. ---
    env_cmk_arn = configuration.get("KMSKeyArn")
    if isinstance(env_cmk_arn, str) and env_cmk_arn:
        yield _unpaired_deny_result(
            ctx,
            relay_role_arn=relay_role_arn,
            row=DenyRow(
                row_id="D14",
                action=ACTION_KMS_DECRYPT,
                resource=env_cmk_arn,
                expected=DENIED,
                severity="Low",
                positive_role_arn=None,
                positive_label="",
            ),
        )
    else:
        yield CheckResult(
            resource=relay_function_name,
            check="D14_relay_denied",
            status="skip",
            detail=(
                "relay declares no environment CMK; nothing to deny against. A fabricated CMK "
                "ARN returns implicitDeny and reads as a pass, so this row is skipped rather "
                "than run"
            ),
        )

    # --- D17-D19: the SSM grant is scoped, singular, and cannot enumerate. ---
    audience_arn = parameter_arns.get(_AUDIENCE_PARAM_ENV)
    unrelated_arn = f"arn:aws:ssm:{ctx.region}:{account_id}:parameter/not-a-relay-parameter"
    # A5 is D17-D19's positive control, so the guard asserts A5's own outcome.
    a5_established = allow_statuses.get("A5") == "pass"
    yield CheckResult(
        resource=unrelated_arn,
        check="D17_expressibility",
        status="pass" if a5_established else "fail",
        detail=(
            "A5 established ssm:GetParameter is allowable for this role on a real parameter ARN in "
            "this account, so D17's deny is evidence of scoping and not of a malformed pair"
            if a5_established
            else f"A5 did not pass (status {allow_statuses.get('A5', 'never ran')!r}), so no "
            "principal established ssm:GetParameter is expressible for this role on a real "
            "parameter ARN. D17-D19's denies below cannot be distinguished from a malformed pair"
        ),
    )
    if not audience_arn:
        # Without the audience ARN the three rows are reported, not dropped.
        for row_id, severity in (("D17", "High"), ("D18", "Medium"), ("D19", "Medium")):
            yield CheckResult(
                resource=relay_function_name,
                check=f"{row_id}_relay_denied",
                status="skip",
                detail=(
                    f"no {_AUDIENCE_PARAM_ENV} parameter ARN could be resolved from deployed state "
                    f"(see A5 above), so {row_id} ({severity}) has no resource to deny against. "
                    "Constructing one would make the row self-certifying, so it is skipped rather "
                    "than run vacuously"
                ),
            )
    else:
        for row_id, action, resource, severity in (
            ("D17", ACTION_SSM_GET_PARAMETER, unrelated_arn, "High"),
            ("D18", ACTION_SSM_GET_PARAMETERS, audience_arn, "Medium"),
            ("D19", ACTION_SSM_GET_PARAMETERS_BY_PATH, f"{audience_arn}/", "Medium"),
        ):
            yield _unpaired_deny_result(
                ctx,
                relay_role_arn=relay_role_arn,
                row=DenyRow(
                    row_id=row_id,
                    action=action,
                    resource=resource,
                    expected=DENIED,
                    severity=severity,
                    positive_role_arn=None,
                    positive_label="",
                ),
            )

    # --- I0: the Runtime invoker list D15/D16, I1/I2 and X1 read. ---
    invokers: frozenset[str] | None = None
    try:
        invokers = _runtime_invoker_functions()
    except Exception as exc:  # noqa: BLE001 - reported as a failing row, not a crash
        yield CheckResult(
            resource="local.runtime_invoker_role_arns",
            check=_INVOKERS_RESOLVED_CHECK,
            status="fail",
            detail=f"the Runtime invoker list could not be read ({exc}), so no D15/D16 or I1/I2 "
            "row ran and X1 has no role to simulate",
        )

    # --- D15/D16: the inverse matrix. Same pinned action constant as A1/A2, same invocation. ---
    if invokers is not None:
        try:
            non_relay_roles = _non_relay_role_arns(account_id, invokers)
        except Exception as exc:  # noqa: BLE001 - reported as a failing row, not a crash
            non_relay_roles = {}
            yield CheckResult(
                resource="grant matrix",
                check=_INVERSE_MATRIX_ENUMERATED_CHECK,
                status="fail",
                detail=f"the roles D15/D16 must assert denied could not be enumerated "
                f"({type(exc).__name__}: {exc}), so no D15/D16 row ran",
            )
        for role_name, role_arn in non_relay_roles.items():
            yield from _inverse_matrix_results(
                ctx,
                role_name=role_name,
                role_arn=role_arn,
                runtime_arn=runtime_arn,
                endpoint_arn=endpoint_arn,
            )

    # --- I1/I2: every other Runtime invoker is allowed, so removing its grant is a red row. ---
    chat_target_i1 = f"I1[{_role_name(_CHAT_TARGET_FUNCTION)}]_allowed"
    chat_target_i1_status: Status | None = None
    if invokers is not None:
        for result in _invoker_allow_results(
            ctx,
            invokers=invokers,
            account_id=account_id,
            runtime_arn=runtime_arn,
            endpoint_arn=endpoint_arn,
        ):
            if result.check == chat_target_i1:
                chat_target_i1_status = result.status
            yield result

    # --- X1: chat-target's NotAction Deny, by exclusion; I1 above is its positive control. ---
    yield _chat_target_exclusion_result(
        ctx,
        invokers=invokers,
        account_id=account_id,
        runtime_arn=runtime_arn,
        endpoint_arn=endpoint_arn,
        positive_control_status=chat_target_i1_status,
    )

    # --- U1: the deployed Function URL enforces IAM auth. ---
    yield from _function_url_auth_type_check(ctx, relay_function_name)


#: The invoker A1/A2 already cover, under its own output-sourced role ARN.
_RELAY_FUNCTION: Final[str] = "agent-relay"
_INVOKERS_RESOLVED_CHECK: Final[str] = "I0_runtime_invokers_resolved"
_INVERSE_MATRIX_ENUMERATED_CHECK: Final[str] = "D15_D16_roles_enumerated"


def _invoker_allow_results(
    ctx: SmokeContext,
    *,
    invokers: frozenset[str],
    account_id: str,
    runtime_arn: str,
    endpoint_arn: str,
) -> Iterator[CheckResult]:
    """I1/I2 for each Runtime invoker other than the relay: `InvokeAgentRuntime` must be allowed.

    `invokers` is `_runtime_invoker_functions`; D15/D16 exclude exactly these. A simulate
    that raises fails that row rather than emitting nothing.
    """
    for function in sorted(invokers - {_RELAY_FUNCTION}):
        role_name = _role_name(function)
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        for row_id, resource in (("I1", runtime_arn), ("I2", endpoint_arn)):
            try:
                yield _allow_result(
                    ctx,
                    role_arn=role_arn,
                    row_id=f"{row_id}[{role_name}]",
                    action=ACTION_INVOKE_RUNTIME,
                    resource=resource,
                    why="local.runtime_invoker_role_arns admits this role, so its own policy must "
                    "grant the call; D15/D16 no longer assert anything about it",
                )
            except Exception as exc:  # noqa: BLE001 - this role's failing row, not a crash
                yield CheckResult(
                    resource=resource,
                    check=f"{row_id}[{role_name}]_allowed",
                    status="fail",
                    detail=f"{row_id}: simulating {role_name} raised {type(exc).__name__}: {exc}",
                )


#: The non-relay invoker whose role carries `DenyEverythingButInvokeOnThisRuntime`
#: (`infra/mcp_chat_gateway.tf`).
_CHAT_TARGET_FUNCTION: Final[str] = "chat-target"
_CHAT_TARGET_EXCLUSION_CHECK: Final[str] = "X1_chat_target_deny_covers_actions_nobody_enumerated"


def _chat_target_exclusion_result(
    ctx: SmokeContext,
    *,
    invokers: frozenset[str] | None,
    account_id: str,
    runtime_arn: str,
    endpoint_arn: str,
    positive_control_status: Status | None,
) -> CheckResult:
    """X1: unpermitted actions on the runtime and its endpoint, as chat-target, are `explicitDeny`.

    Probes a fabricated action and `InvokeAgentRuntimeCommand` on both resources, as the role
    I1/I2 resolve. `positive_control_status` is the status of that role's I1 row in this run.
    Passes only when every probe is `explicitDeny` and I1 passed; an unreadable invoker list
    (`None`), chat-target missing from it, or a simulate that raises is a `fail`.
    """
    if invokers is None:
        return CheckResult(
            resource=runtime_arn,
            check=_CHAT_TARGET_EXCLUSION_CHECK,
            status="fail",
            detail=f"X1: the Runtime invoker list could not be read (see "
            f"{_INVOKERS_RESOLVED_CHECK}), so X1 did not run",
        )
    if _CHAT_TARGET_FUNCTION not in invokers:
        return CheckResult(
            resource=runtime_arn,
            check=_CHAT_TARGET_EXCLUSION_CHECK,
            status="fail",
            detail=f"X1: {_CHAT_TARGET_FUNCTION!r} is not in local.runtime_invoker_role_arns "
            f"({sorted(invokers)}), so its Deny has no role to be simulated against",
        )
    role_name = _role_name(_CHAT_TARGET_FUNCTION)
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    probes: list[str] = []
    all_explicit = True
    for action in (ACTION_NEVER_SHIPPED, ACTION_INVOKE_RUNTIME_COMMAND):
        for resource in (runtime_arn, endpoint_arn):
            try:
                decision, _ = _simulate(ctx, role_arn, action, resource)
            except Exception as exc:  # noqa: BLE001 - reported as this row's failure, not a crash
                return CheckResult(
                    resource=runtime_arn,
                    check=_CHAT_TARGET_EXCLUSION_CHECK,
                    status="fail",
                    detail=f"X1: simulating {role_name} for {action} on {resource} raised "
                    f"{type(exc).__name__}: {exc}. Not a pass: no decision was observed",
                )
            all_explicit = all_explicit and decision in EXPLICIT_DENY_ONLY
            probes.append(_decision_detail(f"X1[{role_name}]", action, resource, decision))
    control_passed = positive_control_status == "pass"
    if not all_explicit:
        verdict = (
            f"; expected {sorted(EXPLICIT_DENY_ONLY)} for every probe. The deployed chat-target "
            "role's DenyEverythingButInvokeOnThisRuntime is absent or no longer matches by "
            "exclusion on the runtime and its endpoint, so an unenumerated invoke route is not "
            "explicitly denied"
        )
    elif not control_passed:
        verdict = (
            f"; but I1[{role_name}] reported {positive_control_status or 'no row'} in this run, so "
            "these denies cannot be told apart from a Deny that also blocks the one permitted invoke"
        )
    else:
        verdict = (
            f"; the role's NotAction Deny matches by exclusion on both resources, and "
            f"I1[{role_name}] passed in this run, so the one permitted invoke is still allowed"
        )
    return CheckResult(
        resource=runtime_arn,
        check=_CHAT_TARGET_EXCLUSION_CHECK,
        status="pass" if all_explicit and control_passed else "fail",
        detail="; ".join(probes) + verdict,
    )


#: The only Function URL authorization type that is not open to the internet. `NONE` means unauthenticated
#: invocation, whatever any IAM policy elsewhere says.
_REQUIRED_URL_AUTH_TYPE: Final[str] = "AWS_IAM"


def _function_url_auth_type_check(ctx: SmokeContext, function_name: str) -> Iterator[CheckResult]:
    """U1: `get_function_url_config`'s `AuthType` on the deployed relay must be `AWS_IAM`.

    Read from the deployed resource. An absent URL configuration is a `fail`, since the relay's
    ingress is its Function URL.
    """
    try:
        config = ctx.client("lambda").get_function_url_config(FunctionName=function_name)
    except Exception as exc:  # noqa: BLE001 - reported as a failing check, not a crash
        yield CheckResult(
            resource=function_name,
            check="U1_function_url_auth_type",
            status="fail",
            detail=(
                f"could not read the deployed Function URL configuration ({type(exc).__name__}: "
                f"{exc}). The relay's only ingress is its Function URL, so this is a broken "
                "deployment, not an inapplicable check"
            ),
        )
        return
    auth_type = str(config.get("AuthType") or "")
    satisfied = auth_type == _REQUIRED_URL_AUTH_TYPE
    yield CheckResult(
        resource=function_name,
        check="U1_function_url_auth_type",
        status="pass" if satisfied else "fail",
        detail=(
            f"the deployed Function URL enforces AuthType={auth_type}"
            if satisfied
            else (
                f"the deployed Function URL reports AuthType={auth_type!r}, not "
                f"{_REQUIRED_URL_AUTH_TYPE!r}. Severity: Critical -- AuthType=NONE admits "
                "unauthenticated invocation regardless of any IAM policy, which is a globally "
                "accessible resource. Source declares AWS_IAM "
                "(infra/modules/agent_relay/main.tf), so this is an authored-vs-deployed delta and "
                "the deployment is what serves traffic"
            )
        ),
    )


def _agent_less_tool_names() -> list[str]:
    """The toolspec tools no agent holds; the Runtime role must not invoke them."""
    from crm.toolspec.definitions import ALL_TOOLS

    return sorted(tool.name for tool in ALL_TOOLS if not tool.agents)


#: Rows emitted once per run when no row fails: A1-A5 (5), D1-D4 and D21-D23 (7), the
#: by-exclusion row (1), D5 and its expressibility (2), the table-mapping guard (1), D9 and its
#: control (2), D10/D11 (2), D12 and its resolution (2), D13 (1), D14 (1), D17-D19 and their
#: expressibility guard (4), X1 (1), U1 (1).
_FIXED_ROWS: Final[int] = 30
#: D6 rows per action tool: positive control, deny row, and the policy-text corroboration.
_ROWS_PER_TOOL: Final[int] = 3
#: Extra rows per agent-less tool: R1's synthetic positive control and Runtime-role deny row.
_ROWS_PER_AGENT_LESS_TOOL: Final[int] = 2
#: D7/D8 rows per table: two actions, each a positive control plus a deny row.
_ROWS_PER_TABLE: Final[int] = 4


def expected_check_count(ctx: SmokeContext) -> ExpectedCheckCount:
    """The checks a run with no failing row emits, derived from the inputs its loops iterate.

    Every family is counted exactly, so a compliant run that drops one falls below the floor. A
    branch that emits fewer rows does so only beside a `fail` row. A table added to
    `dynamodb_table_names`, a tool added to `ALL_TOOLS`, or a role added to the grant matrix raises
    the floor.
    """
    from crm.store.schema import TENANT_SCOPED_TABLES

    table_names = ctx.outputs.require_mapping("dynamodb_table_names")
    tables = len(table_names)
    # The same source D6's loop iterates (`_action_tool_names`).
    functions = len(_action_tool_names())
    agent_less = len(_agent_less_tool_names())
    # `tables` is read from deployed state and can shrink; the shortfall is recorded in the basis, and
    # `checks` fails it as `table_mapping_covers_every_tenant_scoped_table`.
    shortfall = sorted(set(TENANT_SCOPED_TABLES) - set(table_names))
    try:
        invokers = _runtime_invoker_functions()
        inverse_rows = 2 * len(_non_relay_role_arns("", invokers))
        invoker_rows = 2 * len(invokers - {_RELAY_FUNCTION})
        resolution_rows = 0
    except Exception:  # noqa: BLE001 - `checks` emits one failing I0 or D15/D16 row instead
        inverse_rows = invoker_rows = 0
        resolution_rows = 1
    return ExpectedCheckCount(
        minimum=(
            _FIXED_ROWS
            + _ROWS_PER_TOOL * functions
            + _ROWS_PER_AGENT_LESS_TOOL * agent_less
            + _ROWS_PER_TABLE * tables
            + inverse_rows
            + invoker_rows
            + resolution_rows
        ),
        basis=(
            f"{_FIXED_ROWS} fixed rows (A1-A5, D1-D5, D9-D14, D17-D19, D21-D23, the guards, U1, X1) + "
            f"{_ROWS_PER_TOOL} rows x {functions} action tool(s) from ALL_TOOLS (D6) + "
            f"{_ROWS_PER_AGENT_LESS_TOOL} rows x {agent_less} agent-less tool(s) (R1) + "
            f"{_ROWS_PER_TABLE} rows x {tables} table(s) in the dynamodb_table_names output "
            f"(D7/D8) + {inverse_rows} D15/D16 row(s) for the non-relay roles + {invoker_rows} "
            "I1/I2 row(s) for the non-relay Runtime invokers"
            + (" + 1 failing I0/D15-D16 resolution row" if resolution_rows else "")
            + ". Exact for a run with no failing row, so dropping a row family reds it"
            + (
                ""
                if not shortfall
                else f". WARNING: this multiplicand is SHORT by {len(shortfall)} table(s) "
                f"({shortfall}) against the schema's tenant-scoped set, so this floor shrank with the "
                "emission it measures and cannot detect the missing rows -- see the failing "
                "table_mapping_covers_every_tenant_scoped_table check"
            )
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: emit the relay IAM matrix inventory and exit non-zero on any `fail`."""
    return run_main(
        argv,
        description=__doc__ or "",
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    sys.exit(main())
