# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.chat_path_iam`.

IAM, Lambda and STS are fakes: the IAM fake encodes the chat-path grants, and the Lambda fake
serves resource policies in `GetPolicy`'s rendered shape. A test mutates either to show its row reds.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from scripts.smoke import chat_path_iam as mod
from scripts.smoke.harness import CheckResult, context_for_test

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_PREFIX = "crm-agent-eval"
_GATEWAY_ID = "crm-agent-eval-chat-gateway-abc123"
_GATEWAY_URL = f"https://{_GATEWAY_ID}.gateway.bedrock-agentcore.{_REGION}.amazonaws.com/mcp"
_GATEWAY_ARN = f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:gateway/{_GATEWAY_ID}"
_G2_ROLE = f"arn:aws:iam::{_ACCOUNT}:role/{_PREFIX}-chat-gateway-role"
_CT_ROLE = f"arn:aws:iam::{_ACCOUNT}:role/{_PREFIX}-chat-target-role"
_G1_ROLE = f"arn:aws:iam::{_ACCOUNT}:role/{_PREFIX}-gateway-role"
_TABLES = {
    "reps": f"{_PREFIX}-reps",
    "accounts": f"{_PREFIX}-accounts",
    "eval_runs": f"{_PREFIX}-eval_runs",
}
_OUTPUTS: dict[str, Any] = {
    "eval_trigger_function_name": f"{_PREFIX}-eval-trigger",
    "mcp_chat_gateway_url": _GATEWAY_URL,
    "agentcore_runtime_arn": f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:runtime/r",
    "dynamodb_table_names": _TABLES,
}


def _fn_arn(name: str) -> str:
    return f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:{name}"


def _role_grant(principal: Any = _G2_ROLE) -> dict[str, Any]:
    return {
        "Sid": "AllowGatewayTargetRoleInvoke",
        "Effect": "Allow",
        "Principal": {"AWS": principal},
        "Action": "lambda:InvokeFunction",
        "Resource": _fn_arn("x"),
    }


def _service_grant(
    *, source_arn: str | None = _GATEWAY_ARN, account: str | None = _ACCOUNT
) -> dict[str, Any]:
    condition: dict[str, Any] = {}
    if source_arn is not None:
        condition["ArnLike"] = {"AWS:SourceArn": source_arn}
    if account is not None:
        condition["StringEquals"] = {"AWS:SourceAccount": account}
    statement: dict[str, Any] = {
        "Sid": "AllowChatGatewayInvokeIdentityInterceptor",
        "Effect": "Allow",
        "Principal": {"Service": mod.GATEWAY_SERVICE_PRINCIPAL},
        "Action": "lambda:InvokeFunction",
        "Resource": _fn_arn("x"),
    }
    if condition:
        statement["Condition"] = condition
    return statement


def _doc(*statements: dict[str, Any]) -> str:
    return json.dumps({"Version": "2012-10-17", "Statement": list(statements)})


class FakeLambda:
    def __init__(self, policies: dict[str, str | None] | None = None) -> None:
        self.policies = policies or {
            f"{_PREFIX}-chat-target": _doc(_role_grant()),
            f"{_PREFIX}-chat-identity-interceptor": _doc(_role_grant(), _service_grant()),
        }

    def get_function_configuration(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        return {
            "FunctionArn": _fn_arn(FunctionName),
            "Role": f"arn:aws:iam::{_ACCOUNT}:role/{FunctionName}-role",
        }

    def get_policy(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        policy = self.policies.get(FunctionName)
        if policy is None:
            exc = RuntimeError("ResourceNotFoundException")
            exc.response = {"Error": {"Code": "ResourceNotFoundException"}}  # type: ignore[attr-defined]
            raise exc
        return {"Policy": policy}


#: (role, action, resource-suffix-or-key-prefix) grants a correctly deployed chat path holds.
_GRANTED: frozenset[tuple[str, str, str]] = frozenset(
    {
        (_G2_ROLE, "lambda:InvokeFunction", f"function:{_PREFIX}-chat-target"),
        (_G2_ROLE, "lambda:InvokeFunction", f"function:{_PREFIX}-chat-identity-interceptor"),
        (_CT_ROLE, "dynamodb:GetItem", "key:usersettings#"),
        (_CT_ROLE, "dynamodb:GetItem", "key:conversation#"),
        (_CT_ROLE, "dynamodb:PutItem", "key:conversation#"),
    }
)


class FakeIam:
    def __init__(self, extra: frozenset[tuple[str, str, str]] = frozenset()) -> None:
        self.granted = _GRANTED | extra

    def simulate_principal_policy(self, **request: Any) -> dict[str, Any]:
        role = request["PolicySourceArn"]
        (action,) = request["ActionNames"]
        (resource,) = request["ResourceArns"]
        keys = request.get("ContextEntries", [{}])[0].get("ContextKeyValues", [])
        allowed = False
        for g_role, g_action, qualifier in self.granted:
            if g_role != role or g_action != action:
                continue
            if qualifier.startswith("key:"):
                if (
                    resource.endswith(f"table/{_TABLES['reps']}")
                    and all(k.startswith(qualifier[len("key:") :]) for k in keys)
                    and keys
                ):
                    allowed = True
            elif resource.endswith(qualifier):
                allowed = True
        return {"EvaluationResults": [{"EvalDecision": "allowed" if allowed else "implicitDeny"}]}


class FakeSts:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": _ACCOUNT}


def _run(
    *, iam: FakeIam | None = None, lambda_client: FakeLambda | None = None
) -> dict[str, CheckResult]:
    ctx = context_for_test(
        outputs=_OUTPUTS,
        region=_REGION,
        clients={
            "iam": iam or FakeIam(),
            "lambda": lambda_client or FakeLambda(),
            "sts": FakeSts(),
        },
    )
    results = list(mod.checks(ctx))
    by_check = {r.check: r for r in results}
    assert len(by_check) == len(results)
    return by_check


def _failing(by_check: dict[str, CheckResult]) -> set[str]:
    return {c for c, r in by_check.items() if r.status != "pass"}


def test_a_correct_chat_path_passes_every_row_and_meets_the_derived_floor() -> None:
    by_check = _run()
    assert {c: r.detail for c, r in by_check.items() if r.status != "pass"} == {}
    floor = mod.expected_check_count(context_for_test(outputs=_OUTPUTS)).minimum
    assert len(by_check) == floor
    assert {"ct_denies_GetItem@accounts", "ct_denies_PutItem@eval_runs"} <= set(by_check)
    assert "ct_denies_GetItem@reps" not in by_check


@pytest.mark.parametrize(
    ("extra", "reddened"),
    [
        (
            (_G2_ROLE, "lambda:InvokeFunction", f"function:{_PREFIX}-identity-interceptor"),
            "g2_denies_invoke_gateway1_interceptor",
        ),
        (
            (_G2_ROLE, "lambda:InvokeFunction", f"function:{_PREFIX}-eval-trigger"),
            "g2_denies_invoke_eval_trigger",
        ),
        (
            (_G2_ROLE, "bedrock-agentcore:InvokeAgentRuntime", "runtime/r"),
            "g2_denies_invoke_runtime",
        ),
        (
            (_CT_ROLE, "dynamodb:GetItem", "table/crm-agent-eval-accounts"),
            "ct_denies_GetItem@accounts",
        ),
        ((_CT_ROLE, "dynamodb:PutItem", "key:usersettings#"), "ct_denies_put_usersettings"),
        ((_CT_ROLE, "dynamodb:GetItem", "key:rep-"), "ct_denies_get_rep_item"),
    ],
)
def test_a_surplus_grant_reds_exactly_its_row(extra: tuple[str, str, str], reddened: str) -> None:
    assert _failing(_run(iam=FakeIam(frozenset({extra})))) == {reddened}


def test_a_missing_gateway_grant_reds_its_allow_row() -> None:
    iam = FakeIam()
    iam.granted = iam.granted - {
        (_G2_ROLE, "lambda:InvokeFunction", f"function:{_PREFIX}-chat-target")
    }
    assert _failing(_run(iam=iam)) == {"g2_allows_invoke_chat_target"}


@pytest.mark.parametrize(
    ("target", "statements", "fragment"),
    [
        ("chat-target", [_role_grant("*")], "AWS principal '*'"),
        ("chat-target", [{**_role_grant(), "Principal": "*"}], "principal '*'"),
        ("chat-target", [_role_grant(f"arn:aws:iam::{_ACCOUNT}:role/other")], "is not"),
        ("chat-target", [_role_grant(), _service_grant(source_arn=None)], "AWS:SourceArn"),
        (
            "chat-identity-interceptor",
            [_role_grant(), _service_grant(account=None)],
            "SourceAccount",
        ),
        (
            "chat-identity-interceptor",
            [_role_grant(), _service_grant(source_arn=f"{_GATEWAY_ARN[:-6]}other1")],
            "AWS:SourceArn",
        ),
        ("chat-identity-interceptor", [_role_grant()], "no Gateway #2 service grant"),
        ("chat-target", [], "no invoke grant"),
    ],
)
def test_a_resource_policy_granting_anything_else_fails(
    target: str, statements: list[dict[str, Any]], fragment: str
) -> None:
    lambda_client = FakeLambda()
    lambda_client.policies[f"{_PREFIX}-{target}"] = _doc(*statements)
    by_check = _run(lambda_client=lambda_client)
    check = f"{target.replace('chat-identity-interceptor', 'chat_interceptor').replace('-', '_')}"
    row = by_check[f"{check}_invoked_only_by_gateway2"]
    assert row.status == "fail" and fragment in row.detail, row.detail
    assert _failing(by_check) == {row.check}


def test_an_absent_resource_policy_fails() -> None:
    lambda_client = FakeLambda()
    lambda_client.policies[f"{_PREFIX}-chat-target"] = None
    row = _run(lambda_client=lambda_client)["chat_target_invoked_only_by_gateway2"]
    assert row.status == "fail" and "no resource policy" in row.detail


def test_condition_keys_match_case_insensitively() -> None:
    lowered = _service_grant()
    lowered["Condition"] = {
        "ArnLike": {"aws:SourceArn": _GATEWAY_ARN},
        "StringEquals": {"aws:sourceaccount": _ACCOUNT},
    }
    problems, grants = mod.policy_violations(
        {"Statement": [lowered]},
        gateway_role_arn=_G2_ROLE,
        gateway_arn=_GATEWAY_ARN,
        account_id=_ACCOUNT,
    )
    assert problems == [] and grants == 1


def test_gateway_arn_derives_from_the_url_and_refuses_other_hosts() -> None:
    assert (
        mod.gateway_arn_from_url(_GATEWAY_URL, region=_REGION, account_id=_ACCOUNT) == _GATEWAY_ARN
    )
    with pytest.raises(ValueError):
        mod.gateway_arn_from_url("https://example.test/mcp", region=_REGION, account_id=_ACCOUNT)


def test_names_follow_the_iac_templates() -> None:
    assert mod.chat_names(_PREFIX) == {
        "chat_target": "crm-agent-eval-chat-target",
        "chat_interceptor": "crm-agent-eval-chat-identity-interceptor",
        "gateway1_interceptor": "crm-agent-eval-identity-interceptor",
        "gateway_role": "crm-agent-eval-chat-gateway-role",
        "gateway1_role": "crm-agent-eval-gateway-role",
    }


def test_the_name_templates_match_what_the_iac_declares() -> None:
    from pathlib import Path

    infra = (Path(__file__).resolve().parents[2] / "infra").glob("*.tf")
    text = "\n".join(p.read_text(encoding="utf-8") for p in infra)
    for name in mod.chat_names("${local.name_prefix}").values():
        literal = name.replace("${local.name_prefix}", _PREFIX)
        assert f'"{name}"' in text or f'"{literal}"' in text, name


@pytest.mark.parametrize("tool", ["get_account", "approve_proposal", "update_opportunity"])
def test_a_gateway2_grant_on_a_crm_action_lambda_reds_exactly_that_tools_row(tool: str) -> None:
    """Gateway #2's role invokes no CRM action Lambda."""
    extra = (_G2_ROLE, "lambda:InvokeFunction", f"function:{_PREFIX}-{tool}")
    assert _failing(_run(iam=FakeIam(frozenset({extra})))) == {f"g2_denies_invoke_tool@{tool}"}


def test_a_gateway1_grant_on_chat_target_reds_its_row() -> None:
    """Gateway #1's role cannot invoke `chat_target`."""
    extra = (_G1_ROLE, "lambda:InvokeFunction", f"function:{_PREFIX}-chat-target")
    assert _failing(_run(iam=FakeIam(frozenset({extra})))) == {"g1_denies_invoke_chat_target"}


def test_every_crm_action_lambda_has_a_gateway2_deny_row() -> None:
    from crm.toolspec.definitions import ALL_TOOLS

    rows = {c for c in _run() if c.startswith("g2_denies_invoke_tool@")}
    assert rows == {f"g2_denies_invoke_tool@{tool.name}" for tool in ALL_TOOLS}
    assert len(rows) == len(ALL_TOOLS) > 0


def test_the_tool_function_names_follow_the_iac_template() -> None:
    import re
    from pathlib import Path

    main_tf = (Path(__file__).resolve().parents[2] / "infra" / "main.tf").read_text(
        encoding="utf-8"
    )
    block = main_tf[main_tf.index('module "lambda_action" {') :]
    block = block[: block.index("\n}\n")]
    assert "for_each = local.tools" in block
    assert re.search(r'function_name = "crm-agent-eval-\$\{each\.key\}"', block)
    tools = re.search(r"(?ms)^  tools = \{\n(?P<body>.*?)^  \}$", main_tf)
    assert tools is not None
    keys = set(re.findall(r"^    ([a-z_]+)\s*=\s*\{", tools.group("body"), re.MULTILINE))
    assert set(mod.tool_function_names(_PREFIX)) == keys
    assert mod.tool_function_names(_PREFIX)["get_account"] == "crm-agent-eval-get_account"


def test_every_dynamodb_row_carries_a_leading_key() -> None:
    """`simulation_result` fails a row IAM reports `MissingContextValues` for."""
    rows = mod.chat_target_rows(
        role_arn=_CT_ROLE, table_arns={t: f"arn:t/{n}" for t, n in _TABLES.items()}
    )
    dynamodb = [r for r in rows if r.action.startswith("dynamodb:")]
    assert dynamodb and all(r.leading_keys for r in dynamodb), [
        r.check for r in dynamodb if not r.leading_keys
    ]
    lambda_rows = mod.gateway_role_rows(
        role_arn="r",
        chat_target_arn="f",
        chat_interceptor_arn="f",
        gateway1_interceptor_arn="f",
        eval_trigger_arn="f",
        runtime_arn="a",
        tool_function_arns=mod.tool_function_names("p"),
    ) + mod.gateway1_role_rows(role_arn="r", chat_target_arn="f")
    assert not [r.check for r in lambda_rows if r.action.startswith("dynamodb:")]


def test_the_other_table_denies_hold_under_the_permitted_conversation_key() -> None:
    """The deny on another table is evaluated with a key the reps grant would admit."""
    extra = (_CT_ROLE, "dynamodb:GetItem", "table/crm-agent-eval-accounts")
    by_check = _run(iam=FakeIam(frozenset({extra})))
    assert by_check["ct_denies_GetItem@accounts"].status == "fail"
