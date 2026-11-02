# SPDX-License-Identifier: MIT
"""Plan-time assertions on `infra/modules/lambda_action`'s X-Ray active tracing.

Every action Lambda runs with `tracing_config.mode = "Active"`, and its execution role carries the
two X-Ray write actions that active tracing sends segments with. The relay's equivalent cells are in
`test_infra_relay.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit import _eni_function_code_deny as eni_deny
from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_module,
    require_terraform,
    terraform_unavailable_reason,
)

FUNCTION_ADDRESS = "module.subject.aws_lambda_function.this"
VPC_ACCESS_POLICY_ADDRESS = "module.subject.aws_iam_role_policy.vpc_access"

#: The X-Ray actions Lambda's active tracing needs on the execution role.
XRAY_WRITE_ACTIONS = {"xray:PutTraceSegments", "xray:PutTelemetryRecords"}

SUBNET_IDS = ["subnet-fixturezonea", "subnet-fixturezoneb"]
SECURITY_GROUP_IDS = ["sg-fixturelambda"]


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)
    work_dir: Path = tmp_path_factory.mktemp("lambda-action-xray-plan")
    function_zip = work_dir / "function.zip"
    function_zip.write_bytes(b"")
    arguments = {
        "function_name": '"crm-agent-eval-get-account"',
        "handler": '"crm.handlers.get_account.handler"',
        "filename": f'"{function_zip}"',
        "source_code_hash": '"0000000000000000000000000000000000000000000="',
        "reserved_concurrent_executions": "5",
        "dynamodb_grants": "[]",
        "vpc_subnet_ids": json.dumps(SUBNET_IDS),
        "vpc_security_group_ids": json.dumps(SECURITY_GROUP_IDS),
        "eni_function_code_deny_policy_arn": f'"{eni_deny.POLICY_ARN}"',
    }
    body = "\n".join(f"  {key} = {value}" for key, value in arguments.items())
    return plan_module(work_dir, "lambda_action", body)


def _statements(plan: Plan, address: str) -> list[dict[str, Any]]:
    rendered = plan.after(address).get("policy")
    if not isinstance(rendered, str):
        raise AssertionError(f"{address}'s policy document is not known at plan time")
    statements = json.loads(rendered)["Statement"]
    return statements if isinstance(statements, list) else [statements]


def _as_set(value: Any) -> set[str]:
    return {value} if isinstance(value, str) else set(value)


def test_the_function_traces_actively(plan: Plan) -> None:
    """`tracing_config.mode` is `Active`, not the `PassThrough` default."""
    tracing = plan.after(FUNCTION_ADDRESS).get("tracing_config")
    assert tracing == [{"mode": "Active"}], tracing


def test_the_role_may_write_xray_segments(plan: Plan) -> None:
    """One Allow grants exactly the two X-Ray write actions; neither takes a resource type."""
    xray = [
        statement
        for statement in _statements(plan, VPC_ACCESS_POLICY_ADDRESS)
        if statement["Effect"] == "Allow" and _as_set(statement["Action"]) & XRAY_WRITE_ACTIONS
    ]
    assert len(xray) == 1, xray
    assert _as_set(xray[0]["Action"]) == XRAY_WRITE_ACTIONS
    assert _as_set(xray[0]["Resource"]) == {"*"}


# ---------------------------------------------------------------------------
# The ENI function-code Deny is attached, not inline.
# ---------------------------------------------------------------------------


def test_the_eni_function_code_deny_policy_is_attached_to_the_role(plan: Plan) -> None:
    after = plan.after("module.subject.aws_iam_role_policy_attachment.eni_function_code_deny")
    assert after["policy_arn"] == eni_deny.POLICY_ARN
    assert after["role"] == "crm-agent-eval-get-account-role"


def test_vpc_access_grants_exactly_the_eni_actions_the_shared_deny_covers(plan: Plan) -> None:
    granted = eni_deny.allowed_eni_actions(_statements(plan, VPC_ACCESS_POLICY_ADDRESS))
    assert granted == eni_deny.ALLOWED_ENI_ACTIONS, granted


def test_vpc_access_holds_no_deny(plan: Plan) -> None:
    """The Deny stays out of the inline document, which counts toward the role's inline quota."""
    denies = [s for s in _statements(plan, VPC_ACCESS_POLICY_ADDRESS) if s.get("Effect") == "Deny"]
    assert not denies, denies
