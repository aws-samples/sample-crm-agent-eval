# SPDX-License-Identifier: MIT
"""Plan-time rendering of `lambda_action`'s optional per-grant `select_condition`.

Plans the real `infra/modules/lambda_action` module three times with one conditioned `gsi_tenant`
Query grant: `select_condition` omitted, set to `[]`, and set to `["COUNT"]`. The first two must render byte-identical policy documents carrying only the
`LeadingKeys` condition (so a caller that sets nothing is unchanged); the third must carry both
conditions on the one statement. The sweep-control `demo` grant that uses it is asserted in
`tests/unit/test_infra_eval_sfn.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)
from tests.unit.test_infra_inline_policy_budget import ACCOUNT, REGION, _copy_real_module

_TABLE_ARN: Final[str] = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/crm-agent-evalaccounts"
_INDEX_ARN: Final[str] = f"{_TABLE_ARN}/index/gsi_tenant"

#: Module instance name -> the HCL of its grant's `select_condition` attribute ("" omits it).
_INSTANCES: Final[dict[str, str]] = {
    "omitted": "",
    "empty": "select_condition = []",
    "select": 'select_condition = ["COUNT"]',
}

_LEADING_KEYS_CONDITION: Final[dict[str, Any]] = {
    "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": "demo"}
}


def _module_block(name: str, extra: str) -> str:
    return f"""
module "{name}" {{
  source = "./modules/lambda_action"

  function_name                     = "fixture-{name}"
  handler                           = "fixture.handler"
  filename                          = "fixture.zip"
  source_code_hash                  = "fixture"
  reserved_concurrent_executions    = 1
  vpc_subnet_ids                    = ["fixture-subnet-a", "fixture-subnet-b"]
  vpc_security_group_ids            = ["fixture-security-group"]
  eni_function_code_deny_policy_arn = "arn:aws:iam::{ACCOUNT}:policy/fixture-eni-deny"

  dynamodb_grants = [{{
    table_name            = "accounts"
    table_arn             = "{_TABLE_ARN}"
    index_arns            = ["{_INDEX_ARN}"]
    actions               = ["dynamodb:Query"]
    include_table_arn     = false
    leading_key_condition = {{ attribute = "dynamodb:LeadingKeys", values = ["demo"] }}
    {extra}
  }}]
}}
"""


def _require_harness() -> None:
    reason = terraform_unavailable_reason()
    if reason is None:
        return
    message = f"terraform plan harness unavailable: {reason}"
    if require_terraform():
        pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
    pytest.skip(message)


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _require_harness()
    work_dir: Path = tmp_path_factory.mktemp("lambda-action-extra-conditions")
    _copy_real_module(work_dir, "lambda_action")
    root = "".join(_module_block(name, extra) for name, extra in _INSTANCES.items())
    return plan_root(work_dir, root)


def _dynamodb_policy(plan: Plan, name: str) -> str:
    address = f"module.{name}.aws_iam_role_policy.dynamodb[0]"
    policy = plan.after(address)["policy"]
    assert isinstance(policy, str), f"{address} is not rendered at plan time"
    return policy


def _grant_statement(plan: Plan, name: str) -> dict[str, Any]:
    statements = json.loads(_dynamodb_policy(plan, name))["Statement"]
    allows = [s for s in statements if s["Effect"] == "Allow"]
    assert len(allows) == 1, allows
    statement: dict[str, Any] = allows[0]
    return statement


def test_an_omitted_and_an_empty_select_condition_render_byte_identical(plan: Plan) -> None:
    """A caller that sets no `select_condition` gets the same document as one that sets `[]`."""
    assert _dynamodb_policy(plan, "omitted") == _dynamodb_policy(plan, "empty")


def test_without_select_condition_the_statement_carries_only_leading_keys(plan: Plan) -> None:
    statement = _grant_statement(plan, "omitted")
    assert statement["Condition"] == _LEADING_KEYS_CONDITION
    assert statement["Action"] == "dynamodb:Query"
    assert statement["Resource"] == _INDEX_ARN


def test_a_select_condition_is_anded_onto_the_same_statement(plan: Plan) -> None:
    statement = _grant_statement(plan, "select")
    assert statement["Condition"] == {
        "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": "demo"},
        "StringEquals": {"dynamodb:Select": "COUNT"},
    }
    without = {k: v for k, v in statement.items() if k != "Condition"}
    baseline = {k: v for k, v in _grant_statement(plan, "omitted").items() if k != "Condition"}
    assert without == baseline
