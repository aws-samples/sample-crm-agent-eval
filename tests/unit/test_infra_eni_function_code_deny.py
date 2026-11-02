# SPDX-License-Identifier: MIT
"""Plan-time assertions on `infra/modules/eni_function_code_deny`, and its root wiring.

The policy holds one Deny on `*` for exactly the granted ENI actions plus `DetachNetworkInterface`,
conditioned on `lambda:SourceFunctionArn` being present, and names no X-Ray, Logs or other
non-ec2 action. The root passes its ARN to every `lambda_action` and `agent_relay` block. The live
checks are `scripts/smoke/eni_function_code_deny.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest

from tests.unit import _eni_function_code_deny as eni_deny
from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_module,
    require_terraform,
    terraform_unavailable_reason,
)

POLICY = "module.subject.aws_iam_policy.this"
XRAY_WRITE_ACTIONS: Final[frozenset[str]] = frozenset(
    {"xray:PutTraceSegments", "xray:PutTelemetryRecords"}
)

_INFRA: Final[Path] = Path(__file__).resolve().parents[2] / "infra"
_WIRING: Final[str] = "eni_function_code_deny_policy_arn = module.eni_function_code_deny.policy_arn"


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)
    return plan_module(
        tmp_path_factory.mktemp("eni-function-code-deny-plan"),
        "eni_function_code_deny",
        '  name_prefix = "crm-agent-eval"',
    )


def _statements(plan: Plan) -> list[dict[str, Any]]:
    policy = plan.after(POLICY).get("policy")
    assert isinstance(policy, str), f"the policy is not rendered at plan time: {policy!r}"
    statements = json.loads(policy)["Statement"]
    return statements if isinstance(statements, list) else [statements]


def test_the_policy_is_one_deny_on_star_conditioned_on_function_code(plan: Plan) -> None:
    after = plan.after(POLICY)
    assert after["name"] == "crm-agent-eval-deny-eni-actions-to-function-code"
    (statement,) = _statements(plan)
    assert statement["Sid"] == eni_deny.SID
    assert statement["Effect"] == "Deny"
    assert eni_deny.as_set(statement["Resource"]) == {"*"}
    assert statement["Condition"] == eni_deny.EXPECTED_CONDITION, statement["Condition"]


def test_the_deny_names_exactly_the_granted_eni_actions_plus_detach(plan: Plan) -> None:
    """Pinned exactly: a widened Deny (e.g. `logs:*`) would stop Lambda's own log delivery."""
    (statement,) = _statements(plan)
    assert eni_deny.as_set(statement["Action"]) == eni_deny.DENIED_ACTIONS


def test_the_deny_leaves_xray_writes_alone(plan: Plan) -> None:
    (statement,) = _statements(plan)
    overlap = eni_deny.as_set(statement["Action"]) & XRAY_WRITE_ACTIONS
    assert not overlap, f"the Deny would block active tracing: {sorted(overlap)}"


def _root_source() -> str:
    """Every root `.tf` file, concatenated."""
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(_INFRA.glob("*.tf")))


def _vpc_lambda_module_blocks(text: str) -> dict[str, str]:
    blocks = re.findall(r'^module "([^"]+)" \{\n(.*?)^\}$', text, re.DOTALL | re.MULTILINE)
    return {
        name: body
        for name, body in blocks
        if re.search(r'source\s*=\s*"\./modules/(lambda_action|agent_relay)"', body)
    }


def test_every_vpc_lambda_module_attaches_the_deny() -> None:
    blocks = _vpc_lambda_module_blocks(_root_source())
    assert len(blocks) == 11, sorted(blocks)
    missing = sorted(name for name, body in blocks.items() if _WIRING not in body)
    assert not missing, f"these VPC Lambda modules do not attach the ENI Deny: {missing}"


def test_the_root_wiring_guard_reds_when_one_module_drops_the_argument() -> None:
    text = _root_source()
    mutated = text.replace(f"  {_WIRING}\n", "", 1)
    assert mutated != text, "the mutation removed nothing, so the control asserts nothing"
    blocks = _vpc_lambda_module_blocks(mutated)
    assert any(_WIRING not in body for body in blocks.values())
