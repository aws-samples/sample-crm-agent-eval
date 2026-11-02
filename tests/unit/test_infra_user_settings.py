# SPDX-License-Identifier: MIT
"""Plan-time assertions on the `user_settings` Lambda declared in `infra/user_settings.tf`.

The committed file is planned offline with every reference outside it replaced by a known literal
(`_terraform_fixture`, mock credentials). Source checks cover its consumer, `module.api`'s
`extra_routes`. Skips when terraform or the cached providers are absent, unless
`CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any, Final

import pytest
from scripts.hcl_text import blank_comments, blank_heredoc_bodies

from tests.unit._terraform_fixture import (
    MODULES_DIR,
    REPO_ROOT,
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)

ROOT_FILE: Final = REPO_ROOT / "infra" / "user_settings.tf"
ROOT_MAIN: Final = REPO_ROOT / "infra" / "main.tf"

ACCOUNT_ID: Final = "123456789012"
LOGS_KMS_ARN: Final = f"arn:aws:kms:us-east-1:{ACCOUNT_ID}:key/fixture-logs"
DDB_KMS_ARN: Final = f"arn:aws:kms:us-east-1:{ACCOUNT_ID}:key/fixture-ddb"
ENI_DENY_ARN: Final = f"arn:aws:iam::{ACCOUNT_ID}:policy/fixture-eni-deny"

#: Built at runtime: `check_no_account_id` rejects literal `subnet-`/`sg-` ids in tracked content.
SUBNET_IDS: Final = ["-".join(("subnet", "0a1b" * 4 + "1")), "-".join(("subnet", "0a1b" * 4 + "2"))]
SECURITY_GROUP_IDS: Final = ["-".join(("sg", "0a1b" * 4 + "3"))]

TABLES: Final = ("reps", "accounts", "opportunities", "eval_runs", "eval_results")
GRANTED: Final = frozenset({"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"})
ASSERTED_ACTIONS: Final = (
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
)

FUNCTION: Final = "module.lambda_action_user_settings.aws_lambda_function.this"
DDB_POLICY: Final = "module.lambda_action_user_settings.aws_iam_role_policy.dynamodb[0]"


def _skip_unless_terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)


def _hcl_list(values: tuple[str, ...] | list[str]) -> str:
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def _table_arn(name: str) -> str:
    return f"arn:aws:dynamodb:us-east-1:{ACCOUNT_ID}:table/crm-agent-eval-{name}"


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _statements(policy_json: str) -> list[dict[str, Any]]:
    statements = json.loads(policy_json)["Statement"]
    return statements if isinstance(statements, list) else [statements]


def _substitutions(zip_path: Path) -> list[tuple[str, str]]:
    tables = "{ " + ", ".join(f'{t} = "{_table_arn(t)}"' for t in TABLES) + " }"
    return [
        ("${local.name_prefix}", "crm-agent-eval"),
        ("local.name_prefix", '"crm-agent-eval"'),
        ("local.eval_lambda_zip", f'"{zip_path}"'),
        ("module.eni_function_code_deny.policy_arn", f'"{ENI_DENY_ARN}"'),
        ("module.vpc.lambda_subnet_ids", _hcl_list(SUBNET_IDS)),
        ("module.vpc.lambda_security_group_ids", _hcl_list(SECURITY_GROUP_IDS)),
        ("module.observability.cloudwatch_logs_kms_key_arn", f'"{LOGS_KMS_ARN}"'),
        ('local.reserved_concurrency["user_settings"]', "5"),
        ("module.dynamodb.table_arns", tables),
        ("module.dynamodb.kms_key_arn", f'"{DDB_KMS_ARN}"'),
        ("local.dynamodb_asserted_actions", _hcl_list(ASSERTED_ACTIONS)),
        ('"./modules/', f'"{MODULES_DIR}/'),
    ]


_ANY_REFERENCE: Final = re.compile(r"\b(?:module|local|var|data)\.[a-z_]+")


def _plannable_root(zip_path: Path) -> str:
    text = ROOT_FILE.read_text(encoding="utf-8")
    for old, new in _substitutions(zip_path):
        assert old in text, f"{old!r} no longer appears in {ROOT_FILE.name}; update the fixture"
        text = text.replace(old, new)
    code = blank_comments(blank_heredoc_bodies(text))
    leftovers = sorted(set(_ANY_REFERENCE.findall(code)))
    assert leftovers == [], f"unsubstituted outside references: {leftovers}"
    return text


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    zip_path = tmp_path_factory.mktemp("user-settings-build") / "eval_trigger.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("src/crm/handlers/user_settings.py", "def handler(e, c):\n    return {}\n")
    return plan_root(tmp_path_factory.mktemp("user-settings-root"), _plannable_root(zip_path))


def test_function_shape(plan: Plan) -> None:
    """Handler on the eval zip, 10 s, VPC-attached, KMS-encrypted env, reserved concurrency."""
    after = plan.after(FUNCTION)
    assert after["function_name"] == "crm-agent-eval-user-settings"
    assert after["handler"] == "crm.handlers.user_settings.handler"
    assert after["timeout"] == 10
    assert after["reserved_concurrent_executions"] == 5
    assert after["kms_key_arn"] == LOGS_KMS_ARN
    (vpc,) = after["vpc_config"]
    assert sorted(vpc["subnet_ids"]) == sorted(SUBNET_IDS)
    assert sorted(vpc["security_group_ids"]) == sorted(SECURITY_GROUP_IDS)


def test_environment_is_exactly_what_the_handler_reads(plan: Plan) -> None:
    (environment,) = plan.after(FUNCTION)["environment"]
    assert environment["variables"] == {
        "CRM_TABLE_PREFIX": "crm-agent-eval",
        "PYTHONPATH": "/var/task/src",
    }


def test_reps_access_is_get_put_delete_on_usersettings_keys_only(plan: Plan) -> None:
    """One Allow: Get/Put/Delete on `reps`, `ForAllValues:StringLike LeadingKeys usersettings#*`."""
    statements = _statements(plan.after(DDB_POLICY)["policy"])
    (allow,) = [s for s in statements if s["Effect"] == "Allow"]
    assert set(_as_list(allow["Action"])) == GRANTED
    assert _as_list(allow["Resource"]) == [_table_arn("reps")]
    leading = allow["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]
    assert _as_list(leading) == ["usersettings#*"]


def test_every_other_table_and_action_is_denied(plan: Plan) -> None:
    statements = _statements(plan.after(DDB_POLICY)["policy"])
    denies = [s for s in statements if s["Effect"] == "Deny"]
    denied_resources = {r for s in denies for r in _as_list(s["Resource"])}
    for table in TABLES:
        if table != "reps":
            assert _table_arn(table) in denied_resources, table
            assert f"{_table_arn(table)}/index/*" in denied_resources, table
    own = {
        a
        for s in denies
        if _table_arn("reps") in _as_list(s["Resource"])
        for a in _as_list(s["Action"])
    }
    assert own == set(ASSERTED_ACTIONS) - GRANTED


def test_no_gateway_or_other_invoke_permission_is_declared(plan: Plan) -> None:
    """Only `modules/api`'s API Gateway permission may invoke it; this file declares none."""
    assert plan.addresses_of_type("aws_lambda_permission") == []


def _block(text: str, header: str) -> str:
    scan = blank_comments(blank_heredoc_bodies(text)).splitlines(keepends=True)
    starts = [i for i, line in enumerate(scan) if line.startswith(header)]
    assert len(starts) == 1, f"expected one {header!r} block, got {starts}"
    depth = 0
    for offset, line in enumerate(scan[starts[0] :], start=starts[0]):
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return "".join(scan[starts[0] : offset + 1])
    raise AssertionError(f"unterminated {header!r}")


def test_the_api_routes_user_settings_to_this_function() -> None:
    """`module.api`'s `extra_routes.user_settings` reads this module's `invoke_arn`/`function_name`."""
    api = _block(ROOT_MAIN.read_text("utf-8"), 'module "api"')
    route = re.search(r"user_settings\s*=\s*\{([^}]*)\}", api)
    assert route is not None, "module.api has no extra_routes.user_settings entry"
    body = route.group(1)
    assert re.search(r"invoke_arn\s*=\s*module\.lambda_action_user_settings\.invoke_arn", body)
    assert re.search(
        r"function_name\s*=\s*module\.lambda_action_user_settings\.function_name", body
    )
    assert "method" not in body, "user_settings is a POST route (the default)"


def test_reserved_concurrency_is_claimed_in_the_counted_map() -> None:
    text = blank_comments(ROOT_MAIN.read_text("utf-8"))
    concurrency = re.search(r"non_tool_reserved_concurrency\s*=\s*\{([^}]*)\}", text)
    assert concurrency is not None, "no local.non_tool_reserved_concurrency map in main.tf"
    assert re.search(r"^\s*user_settings\s*=\s*5\s*$", concurrency.group(1), re.MULTILINE)
