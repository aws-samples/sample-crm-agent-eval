# SPDX-License-Identifier: MIT
"""Layer 4 T3 smoke: IAM rows for the chat path (Gateway #2, chat_target, its interceptor).

Read-only:
- `iam:SimulatePrincipalPolicy`;
- `lambda:GetPolicy` and `lambda:GetFunctionConfiguration`;
- `sts:GetCallerIdentity`.

Targets come from the `eval_trigger_function_name` (name prefix), `mcp_chat_gateway_url`,
`agentcore_runtime_arn` and `dynamodb_table_names` outputs, plus the `<prefix>-<function>` and
`<prefix>-chat-gateway-role` name templates.

Checks:
1. Gateway #2's role is allowed `lambda:InvokeFunction` on `chat_target` and the chat interceptor,
   and denied it on Gateway #1's interceptor, on `eval_trigger` and on every CRM action Lambda. It
   is also denied `InvokeAgentRuntime`. Gateway #1's role is denied it on `chat_target`.
2. Each of the two functions' resource policies grants invoke only to Gateway #2:
   - its role ARN as an `AWS` principal; or
   - `bedrock-agentcore.amazonaws.com` conditioned on Gateway #2's `AWS:SourceArn` and this
     account's `AWS:SourceAccount`.
   A wildcard or any other principal fails, and so does a service grant missing either condition.
3. `chat_target`'s role:
   - `reps` GetItem is allowed under `usersettings#` and `conversation#`, and PutItem under
     `conversation#` only;
   - a rep item and DeleteItem are denied;
   - GetItem/PutItem on every other table is denied.

`chat_target`'s `InvokeAgentRuntime` grant is relay_iam's I1/I2. The Runtime resource policy's
principal set is runtime_inbound_auth's `resource_policy_principals_are_the_invokers`.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final

from scripts.smoke.admin_authz import (
    SimulationRow,
    function_name_prefix,
    simulation_result,
)
from scripts.smoke.harness import (
    CheckResult,
    ExpectedCheckCount,
    SmokeContext,
    error_code,
    run_main,
)

from crm.toolspec.definitions import ALL_TOOLS

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "chat_path_iam"

GATEWAY_SERVICE_PRINCIPAL: Final[str] = "bedrock-agentcore.amazonaws.com"
SETTINGS_TABLE: Final[str] = "reps"
_GATEWAY_HOST_MARKER: Final[str] = ".gateway.bedrock-agentcore."


def chat_names(prefix: str) -> dict[str, str]:
    """The deployed names the chat path's IaC renders from the `name_prefix`."""
    return {
        "chat_target": f"{prefix}-chat-target",
        "chat_interceptor": f"{prefix}-chat-identity-interceptor",
        "gateway1_interceptor": f"{prefix}-identity-interceptor",
        "gateway_role": f"{prefix}-chat-gateway-role",
        "gateway1_role": f"{prefix}-gateway-role",
    }


def tool_function_names(prefix: str) -> dict[str, str]:
    """Each CRM action Lambda's deployed name, keyed by tool (`local.tools` in `infra/main.tf`)."""
    return {tool.name: f"{prefix}-{tool.name}" for tool in ALL_TOOLS}


def gateway_arn_from_url(url: str, *, region: str, account_id: str) -> str:
    """Gateway #2's ARN from its MCP URL `https://<gateway-id>.gateway.bedrock-agentcore.<region>…`.

    Raises:
        ValueError: The URL's host is not an AgentCore Gateway host.
    """
    host = urllib.parse.urlparse(url).hostname or ""
    if _GATEWAY_HOST_MARKER not in host:
        raise ValueError(f"mcp_chat_gateway_url host {host!r} is not an AgentCore Gateway host")
    gateway_id = host.split(".", 1)[0]
    return f"arn:aws:bedrock-agentcore:{region}:{account_id}:gateway/{gateway_id}"


# -- simulation rows -----------------------------------------------------------------------------


def gateway_role_rows(
    *,
    role_arn: str,
    chat_target_arn: str,
    chat_interceptor_arn: str,
    gateway1_interceptor_arn: str,
    eval_trigger_arn: str,
    runtime_arn: str,
    tool_function_arns: Mapping[str, str],
) -> tuple[SimulationRow, ...]:
    """Gateway #2 role row: invoke its own two functions only."""
    invoke = "lambda:InvokeFunction"
    tool_rows = tuple(
        SimulationRow(f"g2_denies_invoke_tool@{tool}", role_arn, invoke, arn, False)
        for tool, arn in sorted(tool_function_arns.items())
    )
    return tool_rows + (
        SimulationRow("g2_allows_invoke_chat_target", role_arn, invoke, chat_target_arn, True),
        SimulationRow(
            "g2_allows_invoke_chat_interceptor", role_arn, invoke, chat_interceptor_arn, True
        ),
        SimulationRow(
            "g2_denies_invoke_gateway1_interceptor",
            role_arn,
            invoke,
            gateway1_interceptor_arn,
            False,
        ),
        SimulationRow("g2_denies_invoke_eval_trigger", role_arn, invoke, eval_trigger_arn, False),
        SimulationRow(
            "g2_denies_invoke_runtime",
            role_arn,
            "bedrock-agentcore:InvokeAgentRuntime",
            runtime_arn,
            False,
        ),
    )


def gateway1_role_rows(*, role_arn: str, chat_target_arn: str) -> tuple[SimulationRow, ...]:
    """Gateway #1's role cannot invoke Gateway #2's `chat_target`."""
    return (
        SimulationRow(
            "g1_denies_invoke_chat_target",
            role_arn,
            "lambda:InvokeFunction",
            chat_target_arn,
            False,
        ),
    )


def chat_target_rows(*, role_arn: str, table_arns: Mapping[str, str]) -> tuple[SimulationRow, ...]:
    """chat_target row: `reps` by key prefix only; every other table denied."""
    reps = table_arns[SETTINGS_TABLE]
    settings, conversation, rep = ("usersettings#t#r",), ("conversation#c",), ("rep-r",)
    rows = [
        SimulationRow(
            "ct_allows_get_usersettings", role_arn, "dynamodb:GetItem", reps, True, settings
        ),
        SimulationRow(
            "ct_allows_get_conversation", role_arn, "dynamodb:GetItem", reps, True, conversation
        ),
        SimulationRow(
            "ct_allows_put_conversation", role_arn, "dynamodb:PutItem", reps, True, conversation
        ),
        SimulationRow(
            "ct_denies_put_usersettings", role_arn, "dynamodb:PutItem", reps, False, settings
        ),
        SimulationRow("ct_denies_get_rep_item", role_arn, "dynamodb:GetItem", reps, False, rep),
        SimulationRow(
            "ct_denies_delete_conversation",
            role_arn,
            "dynamodb:DeleteItem",
            reps,
            False,
            conversation,
        ),
    ]
    for table in sorted(set(table_arns) - {SETTINGS_TABLE}):
        for action in ("dynamodb:GetItem", "dynamodb:PutItem"):
            rows.append(
                SimulationRow(
                    f"ct_denies_{action.split(':')[1]}@{table}",
                    role_arn,
                    action,
                    table_arns[table],
                    False,
                    conversation,
                )
            )
    return tuple(rows)


# -- resource policies ---------------------------------------------------------------------------


def _condition_value(condition: Mapping[str, Any], key: str) -> list[str]:
    """Every value any operator in `condition` binds to `key` (case-insensitive key match)."""
    values: list[str] = []
    for block in condition.values():
        if not isinstance(block, Mapping):
            continue
        for name, value in block.items():
            if str(name).lower() == key.lower():
                values.extend([value] if isinstance(value, str) else [str(v) for v in value])
    return values


def _invoke_statements(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    statements = document.get("Statement", [])
    if isinstance(statements, Mapping):
        statements = [statements]
    found: list[Mapping[str, Any]] = []
    for statement in statements:
        if not isinstance(statement, Mapping) or statement.get("Effect") != "Allow":
            continue
        actions = statement.get("Action")
        actions = [actions] if isinstance(actions, str) else list(actions or [])
        if any(str(a).startswith("lambda:Invoke") or a in ("*", "lambda:*") for a in actions):
            found.append(statement)
    return found


def policy_violations(
    document: Mapping[str, Any],
    *,
    gateway_role_arn: str,
    gateway_arn: str,
    account_id: str,
) -> tuple[list[str], int]:
    """Why `document` grants invoke to anything but Gateway #2, and how many grants it holds."""
    problems: list[str] = []
    statements = _invoke_statements(document)
    for statement in statements:
        sid = str(statement.get("Sid") or "<no sid>")
        principal = statement.get("Principal")
        if not isinstance(principal, Mapping):
            problems.append(f"{sid}: principal {principal!r}")
            continue
        aws = principal.get("AWS")
        service = principal.get("Service")
        condition = statement.get("Condition")
        condition = condition if isinstance(condition, Mapping) else {}
        if aws is not None and service is None and set(principal) == {"AWS"}:
            if aws != gateway_role_arn:
                problems.append(f"{sid}: AWS principal {aws!r} is not {gateway_role_arn}")
        elif service is not None and aws is None and set(principal) == {"Service"}:
            if service != GATEWAY_SERVICE_PRINCIPAL:
                problems.append(f"{sid}: service principal {service!r}")
            if _condition_value(condition, "AWS:SourceArn") != [gateway_arn]:
                problems.append(
                    f"{sid}: AWS:SourceArn {_condition_value(condition, 'AWS:SourceArn')} is not "
                    f"[{gateway_arn}]"
                )
            if _condition_value(condition, "AWS:SourceAccount") != [account_id]:
                problems.append(f"{sid}: AWS:SourceAccount is not this account")
        else:
            problems.append(f"{sid}: principal {json.dumps(principal)}")
    return problems, len(statements)


def check_resource_policy(
    lambda_client: Any,
    *,
    function_name: str,
    check: str,
    gateway_role_arn: str,
    gateway_arn: str,
    account_id: str,
    require_service_grant: bool,
) -> CheckResult:
    """One function's resource policy grants invoke to Gateway #2 only, and at least once."""
    try:
        document = json.loads(str(lambda_client.get_policy(FunctionName=function_name)["Policy"]))
    except Exception as exc:  # noqa: BLE001 - absent policy or failed read is a failing row
        reason = "no resource policy" if error_code(exc) == "ResourceNotFoundException" else ""
        return CheckResult(
            resource=function_name,
            check=check,
            status="fail",
            detail=f"get_policy: {reason or type(exc).__name__}: {exc}",
        )
    problems, grants = policy_violations(
        document, gateway_role_arn=gateway_role_arn, gateway_arn=gateway_arn, account_id=account_id
    )
    services = [
        s
        for s in _invoke_statements(document)
        if isinstance(s.get("Principal"), Mapping) and "Service" in s["Principal"]
    ]
    if grants == 0:
        problems.append("no invoke grant at all")
    if require_service_grant and not services:
        problems.append("no Gateway #2 service grant (source_arn/source_account)")
    return CheckResult(
        resource=function_name,
        check=check,
        status="fail" if problems else "pass",
        detail="; ".join(problems)
        if problems
        else f"{grants} invoke grant(s), every one Gateway #2's role or its conditioned service grant",
    )


# -- orchestration -------------------------------------------------------------------------------


def _table_arns(ctx: SmokeContext, account_id: str) -> dict[str, str]:
    names = ctx.outputs.require_mapping("dynamodb_table_names")
    return {
        str(logical): f"arn:aws:dynamodb:{ctx.region}:{account_id}:table/{physical}"
        for logical, physical in names.items()
    }


def checks(ctx: SmokeContext) -> Iterator[CheckResult]:
    """Yield every chat-path IAM row: Gateway #2 role, two resource policies, chat_target role."""
    prefix = function_name_prefix(ctx)
    names = chat_names(prefix)
    account_id = str(ctx.client("sts").get_caller_identity()["Account"])
    lambda_client, iam = ctx.client("lambda"), ctx.client("iam")
    config = {
        key: lambda_client.get_function_configuration(FunctionName=names[key])
        for key in ("chat_target", "chat_interceptor", "gateway1_interceptor")
    }
    trigger = lambda_client.get_function_configuration(
        FunctionName=ctx.outputs.require_str("eval_trigger_function_name")
    )
    tool_arns = {
        tool: str(lambda_client.get_function_configuration(FunctionName=name)["FunctionArn"])
        for tool, name in tool_function_names(prefix).items()
    }
    gateway_role_arn = f"arn:aws:iam::{account_id}:role/{names['gateway_role']}"
    gateway_arn = gateway_arn_from_url(
        ctx.outputs.require_str("mcp_chat_gateway_url"), region=ctx.region, account_id=account_id
    )

    for row in gateway_role_rows(
        role_arn=gateway_role_arn,
        chat_target_arn=str(config["chat_target"]["FunctionArn"]),
        chat_interceptor_arn=str(config["chat_interceptor"]["FunctionArn"]),
        gateway1_interceptor_arn=str(config["gateway1_interceptor"]["FunctionArn"]),
        eval_trigger_arn=str(trigger["FunctionArn"]),
        runtime_arn=ctx.outputs.require_str("agentcore_runtime_arn"),
        tool_function_arns=tool_arns,
    ):
        yield simulation_result(iam, row, resource=names["gateway_role"])

    for row in gateway1_role_rows(
        role_arn=f"arn:aws:iam::{account_id}:role/{names['gateway1_role']}",
        chat_target_arn=str(config["chat_target"]["FunctionArn"]),
    ):
        yield simulation_result(iam, row, resource=names["gateway1_role"])

    for key, check, service in (
        ("chat_target", "chat_target_invoked_only_by_gateway2", False),
        ("chat_interceptor", "chat_interceptor_invoked_only_by_gateway2", True),
    ):
        yield check_resource_policy(
            lambda_client,
            function_name=names[key],
            check=check,
            gateway_role_arn=gateway_role_arn,
            gateway_arn=gateway_arn,
            account_id=account_id,
            require_service_grant=service,
        )

    for row in chat_target_rows(
        role_arn=str(config["chat_target"]["Role"]), table_arns=_table_arns(ctx, account_id)
    ):
        yield simulation_result(iam, row, resource=names["chat_target"])


def expected_check_count(ctx: SmokeContext) -> ExpectedCheckCount:
    """The Gateway #2 and #1 rows, two policy rows, and the chat_target rows the outputs imply."""
    tables = ctx.outputs.require_mapping("dynamodb_table_names")
    placeholder = {str(t): "arn" for t in tables}
    target_rows = len(chat_target_rows(role_arn="r", table_arns=placeholder))
    gateway_rows = len(
        gateway_role_rows(
            role_arn="r",
            chat_target_arn="f",
            chat_interceptor_arn="f",
            gateway1_interceptor_arn="f",
            eval_trigger_arn="f",
            runtime_arn="a",
            tool_function_arns=tool_function_names("p"),
        )
    )
    gateway1_rows = len(gateway1_role_rows(role_arn="r", chat_target_arn="f"))
    return ExpectedCheckCount(
        minimum=gateway_rows + gateway1_rows + 2 + target_rows,
        basis=(
            f"{gateway_rows} Gateway #2 role rows ({len(ALL_TOOLS)} of them one per CRM action "
            f"Lambda), {gateway1_rows} Gateway #1 role row, 2 resource-policy rows and "
            f"{target_rows} chat_target rows derived from {len(tables)} dynamodb_table_names "
            "entries"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the chat-path IAM checks and return the process exit code."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
