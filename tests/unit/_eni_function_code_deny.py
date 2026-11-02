# SPDX-License-Identifier: MIT
"""Shared constants for the ENI function-code Deny plan-time tests.

`test_infra_eni_function_code_deny.py` reads the Deny from `modules/eni_function_code_deny`;
`test_infra_xray_tracing.py` (lambda_action) and `test_infra_relay.py` (agent_relay) read each
role's attachment and its granted ENI actions. Both sides compare against `ALLOWED_ENI_ACTIONS`.
"""

from __future__ import annotations

from typing import Any, Final

SID: Final[str] = "DenyEniActionsToFunctionCode"

#: The literal ARN the module-level plans pass as `eni_function_code_deny_policy_arn`.
POLICY_ARN: Final[str] = (
    "arn:aws:iam::123456789012:policy/crm-agent-eval-deny-eni-actions-to-function-code"
)

#: The ENI actions each role's `ManageOwnHyperplaneEnis` Allow grants.
ALLOWED_ENI_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "ec2:CreateNetworkInterface",
        "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeSubnets",
        "ec2:DeleteNetworkInterface",
        "ec2:AssignPrivateIpAddresses",
        "ec2:UnassignPrivateIpAddresses",
    }
)

#: Exactly what the Deny names: the granted set plus the Lambda guide's `DetachNetworkInterface`.
DENIED_ACTIONS: Final[frozenset[str]] = ALLOWED_ENI_ACTIONS | {"ec2:DetachNetworkInterface"}

#: Present on calls made by function code, absent on Lambda's own ENI management.
EXPECTED_CONDITION: Final[dict[str, dict[str, str]]] = {
    "Null": {"lambda:SourceFunctionArn": "false"}
}


def as_set(value: Any) -> set[str]:
    return {value} if isinstance(value, str) else set(value)


def allowed_eni_actions(statements: list[dict[str, Any]]) -> set[str]:
    """The `ec2:` actions the policy's Allow statements grant."""
    granted: set[str] = set()
    for statement in statements:
        if statement.get("Effect") == "Allow":
            granted |= {a for a in as_set(statement.get("Action", [])) if a.startswith("ec2:")}
    return granted
