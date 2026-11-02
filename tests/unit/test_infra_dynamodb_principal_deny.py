# SPDX-License-Identifier: MIT
"""Plan time: the table resource policies and the CMK key policy deny every outside principal.

The `dynamodb` module is planned through the root's committed `module "dynamodb"` block, with every
outside reference replaced by a known literal (`_terraform_fixture`, mock credentials), so each
rendered statement is asserted whole. Validation and the operator precondition are planned from their
committed text. The data-plane allowlist is read from source against every `lambda_action` call with
`dynamodb_kms_grant_enabled = true`. Skips when terraform or the cached providers are absent, unless
`CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
from scripts.hcl_text import blank_comments, blank_heredoc_bodies, blank_string_literals

from crm.store.schema import TABLES
from tests.unit._terraform_fixture import (
    MODULES_DIR,
    REPO_ROOT,
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    TerraformError,
    plan_module,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)

INFRA: Final = REPO_ROOT / "infra"
MAIN_TF: Final = INFRA / "main.tf"
VARIABLES_TF: Final = INFRA / "variables.tf"
OUTPUTS_TF: Final = INFRA / "outputs.tf"
TFVARS_EXAMPLE: Final = INFRA / "terraform.tfvars.example"

ACCOUNT_ID: Final = "123456789012"
REGION: Final = "us-east-1"
PREFIX: Final = "t-"
ROOT_ARN: Final = f"arn:aws:iam::{ACCOUNT_ID}:root"
LAMBDA_ROLES: Final = tuple(
    f"arn:aws:iam::{ACCOUNT_ID}:role/crm-agent-eval-fixture-{n}-role" for n in range(9)
)
OPERATORS: Final = (
    f"arn:aws:iam::{ACCOUNT_ID}:role/fixture-operator",
    f"arn:aws:iam::{ACCOUNT_ID}:role/fixture-second-operator",
)
DATA_PLANE: Final = [*LAMBDA_ROLES, *OPERATORS]
ADMINISTRATORS: Final = [*OPERATORS, ROOT_ARN]

TABLE_DATA_PLANE_ACTIONS: Final = (
    "dynamodb:GetItem",
    "dynamodb:BatchGetItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:ConditionCheckItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:PartiQLSelect",
    "dynamodb:PartiQLInsert",
    "dynamodb:PartiQLUpdate",
    "dynamodb:PartiQLDelete",
    "dynamodb:CreateBackup",
    "dynamodb:ExportTableToPointInTime",
    "dynamodb:RestoreTableToPointInTime",
    "dynamodb:EnableKinesisStreamingDestination",
)
TABLE_ADMINISTRATION_ACTIONS: Final = (
    "dynamodb:DeleteTable",
    "dynamodb:UpdateTable",
    "dynamodb:UpdateContinuousBackups",
    "dynamodb:UpdateTimeToLive",
    "dynamodb:PutResourcePolicy",
    "dynamodb:DeleteResourcePolicy",
    "dynamodb:TagResource",
    "dynamodb:UntagResource",
    "dynamodb:UpdateContributorInsights",
)
KEY_USE_ACTIONS: Final = (
    "kms:Decrypt",
    "kms:Encrypt",
    "kms:GenerateDataKey*",
    "kms:ReEncrypt*",
    "kms:CreateGrant",
)
KEY_ADMINISTRATION_ACTIONS: Final = (
    "kms:PutKeyPolicy",
    "kms:ScheduleKeyDeletion",
    "kms:DisableKey",
    "kms:RevokeGrant",
    "kms:DisableKeyRotation",
)

KEY_ROOT_SID: Final = "Enable IAM User Permissions"
POLICY_TYPE: Final = "aws_dynamodb_resource_policy"
KEY_POLICY_ADDRESS: Final = "module.dynamodb.aws_kms_key_policy.dynamodb"


def _skip_unless_terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)


def _code(path: Path) -> str:
    return blank_comments(blank_heredoc_bodies(path.read_text(encoding="utf-8")))


def _block(text: str, header: str) -> str:
    """The one column-0 block whose first line starts with `header`, brace-matched."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith(header)]
    assert len(starts) == 1, f"expected one {header!r} block, got {len(starts)}"
    depth = 0
    for offset, line in enumerate(lines[starts[0] :], start=starts[0]):
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return "".join(lines[starts[0] : offset + 1])
    raise AssertionError(f"unterminated {header!r}")


def _hcl_list(values: list[str] | tuple[str, ...]) -> str:
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def _substitute(
    text: str, substitutions: dict[str, str], allowed: frozenset[str] = frozenset()
) -> str:
    for old, new in substitutions.items():
        assert old in text, f"{old!r} no longer appears in the extracted source; update the fixture"
        text = text.replace(old, new)
    leftovers = sorted(
        set(
            re.findall(
                r"\b(?:module|local|var|data)\.[a-z_]+",
                blank_string_literals(blank_comments(text)),
            )
        )
    )
    leftovers = [ref for ref in leftovers if ref not in allowed]
    assert leftovers == [], f"unsubstituted references: {leftovers}"
    return text


def _statements(policy_json: str) -> list[dict[str, Any]]:
    statements = json.loads(policy_json)["Statement"]
    assert isinstance(statements, list)
    return statements


def _by_sid(policy_json: str) -> dict[str, dict[str, Any]]:
    statements = _statements(policy_json)
    sids = [s.get("Sid") for s in statements]
    assert len(set(sids)) == len(sids), f"duplicate sids: {sids}"
    return {s["Sid"]: s for s in statements}


# ---------------------------------------------------------------------------
# The root's `module "dynamodb"` block, planned with literal outside references.
# ---------------------------------------------------------------------------


def _root_dynamodb_block() -> str:
    block = _block(_code(MAIN_TF), 'module "dynamodb"')
    return _substitute(
        block,
        {
            '"./modules/dynamodb"': f'"{MODULES_DIR / "dynamodb"}"',
            "local.name_prefix": f'"{PREFIX}"',
            "var.data_classification": '"confidential"',
            "data.aws_caller_identity.current.account_id": f'"{ACCOUNT_ID}"',
            "data.aws_iam_session_context.current.issuer_arn": f'"{OPERATORS[0]}"',
            "local.dynamodb_data_plane_role_arns": _hcl_list(LAMBDA_ROLES),
            "var.crm_operator_principal_arns": _hcl_list(OPERATORS),
        },
    )


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return plan_root(tmp_path_factory.mktemp("dynamodb-principal-deny"), _root_dynamodb_block())


def _table_arn(table: str) -> str:
    return f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{PREFIX}{table}"


def test_one_resource_policy_per_table(plan: Plan) -> None:
    """Exactly the eleven schema tables carry a resource policy, keyed by logical name."""
    addresses = plan.addresses_of_type(POLICY_TYPE)
    expected = sorted(f'module.dynamodb.{POLICY_TYPE}.tables["{name}"]' for name in TABLES)
    assert len(TABLES) == 11
    assert addresses == expected


@pytest.mark.parametrize("table", sorted(TABLES))
def test_table_policy_renders_exactly_the_two_denies(plan: Plan, table: str) -> None:
    """Each table policy is the two Deny statements, every element pinned."""
    policy = plan.after(f'module.dynamodb.{POLICY_TYPE}.tables["{table}"]')["policy"]
    resources = [_table_arn(table), f"{_table_arn(table)}/index/*"]
    document = json.loads(policy)
    assert document["Version"] == "2012-10-17"
    assert _statements(policy) == [
        {
            "Sid": "DenyDataPlaneToNonProjectPrincipals",
            "Effect": "Deny",
            "Principal": "*",
            "Action": list(TABLE_DATA_PLANE_ACTIONS),
            "Resource": resources,
            "Condition": {"ArnNotEquals": {"aws:PrincipalArn": DATA_PLANE}},
        },
        {
            "Sid": "DenyTableAdministrationToNonOperators",
            "Effect": "Deny",
            "Principal": "*",
            "Action": list(TABLE_ADMINISTRATION_ACTIONS),
            "Resource": resources,
            "Condition": {"ArnNotEquals": {"aws:PrincipalArn": ADMINISTRATORS}},
        },
    ]


def test_table_policy_targets_its_own_table(plan: Plan) -> None:
    """`resource_arn` is the table resource's ARN, so each policy waits on its own table."""
    for table in TABLES:
        address = f'module.dynamodb.{POLICY_TYPE}.tables["{table}"]'
        assert plan.after_unknown(address).get("resource_arn") is True, address


def test_table_denies_carry_no_service_exemption(plan: Plan) -> None:
    """Neither table Deny exempts service principals or service-mediated calls."""
    for address in plan.addresses_of_type(POLICY_TYPE):
        rendered = plan.after(address)["policy"]
        assert "aws:PrincipalIsAWSService" not in rendered
        assert "aws:ViaAWSService" not in rendered
        assert "IfExists" not in rendered


def test_key_policy_renders_root_and_the_two_denies(plan: Plan) -> None:
    """The CMK policy keeps the account-root Allow and adds the two Deny statements."""
    after = plan.after(KEY_POLICY_ADDRESS)
    assert after["bypass_policy_lockout_safety_check"] is False
    assert _statements(after["policy"]) == [
        {
            "Sid": KEY_ROOT_SID,
            "Effect": "Allow",
            "Principal": {"AWS": ROOT_ARN},
            "Action": "kms:*",
            "Resource": "*",
        },
        {
            "Sid": "DenyKeyUseToNonProjectPrincipals",
            "Effect": "Deny",
            "Principal": "*",
            "Action": list(KEY_USE_ACTIONS),
            "Resource": "*",
            "Condition": {
                "ArnNotEquals": {"aws:PrincipalArn": DATA_PLANE},
                "Bool": {"aws:PrincipalIsAWSService": "false"},
            },
        },
        {
            "Sid": "DenyKeyAdministrationToNonOperators",
            "Effect": "Deny",
            "Principal": "*",
            "Action": list(KEY_ADMINISTRATION_ACTIONS),
            "Resource": "*",
            "Condition": {"ArnNotEquals": {"aws:PrincipalArn": ADMINISTRATORS}},
        },
    ]


def test_service_exemption_is_on_key_use_only(plan: Plan) -> None:
    """`aws:PrincipalIsAWSService` appears on `DenyKeyUseToNonProjectPrincipals` and nowhere else."""
    statements = _by_sid(plan.after(KEY_POLICY_ADDRESS)["policy"])
    carrying = sorted(
        sid for sid, s in statements.items() if "aws:PrincipalIsAWSService" in json.dumps(s)
    )
    assert carrying == ["DenyKeyUseToNonProjectPrincipals"]


def test_key_policy_targets_the_module_key(plan: Plan) -> None:
    """The key policy is a separate resource on the module's CMK; the key sets no inline policy."""
    assert plan.after_unknown(KEY_POLICY_ADDRESS).get("key_id") is True
    key = plan.after("module.dynamodb.aws_kms_key.dynamodb")
    assert key.get("policy") in (None, "")
    assert key["enable_key_rotation"] is True
    assert key["deletion_window_in_days"] == 30


def test_root_is_an_administrator_and_not_a_data_plane_principal(plan: Plan) -> None:
    """The account root is break-glass for administration only."""
    for address in plan.addresses_of_type(POLICY_TYPE):
        statements = _by_sid(plan.after(address)["policy"])
        data_plane = statements["DenyDataPlaneToNonProjectPrincipals"]
        administration = statements["DenyTableAdministrationToNonOperators"]
        assert ROOT_ARN not in data_plane["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]
        assert ROOT_ARN in administration["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]


def test_every_deny_names_principal_star_literally(plan: Plan) -> None:
    """Each Deny's principal renders as the string `*`, not `{"AWS": "*"}`."""
    documents = [plan.after(a)["policy"] for a in plan.addresses_of_type(POLICY_TYPE)]
    documents.append(plan.after(KEY_POLICY_ADDRESS)["policy"])
    denies = [s for d in documents for s in _statements(d) if s["Effect"] == "Deny"]
    assert len(denies) == 2 * len(TABLES) + 2
    assert all(s["Principal"] == "*" for s in denies)


def test_key_root_sid_is_the_live_default_sid(plan: Plan) -> None:
    """The account-root statement keeps the default key policy's Sid verbatim."""
    statements = _by_sid(plan.after(KEY_POLICY_ADDRESS)["policy"])
    assert statements[KEY_ROOT_SID]["Effect"] == "Allow"
    assert statements[KEY_ROOT_SID]["Principal"] == {"AWS": ROOT_ARN}


def test_administration_denies_cover_tags_insights_and_rotation(plan: Plan) -> None:
    """Tag, contributor-insights and key-rotation changes are operator-only; RetireGrant is not
    listed, because a key policy cannot deny it."""
    for address in plan.addresses_of_type(POLICY_TYPE):
        actions = _by_sid(plan.after(address)["policy"])["DenyTableAdministrationToNonOperators"][
            "Action"
        ]
        assert actions[-3:] == [
            "dynamodb:TagResource",
            "dynamodb:UntagResource",
            "dynamodb:UpdateContributorInsights",
        ]
    key = plan.after(KEY_POLICY_ADDRESS)["policy"]
    assert _by_sid(key)["DenyKeyAdministrationToNonOperators"]["Action"][-1] == (
        "kms:DisableKeyRotation"
    )
    assert "kms:RetireGrant" not in key


def test_dynamodb_self_lockout_check_stays_on(plan: Plan) -> None:
    """No table policy sets `confirm_remove_self_resource_access`."""
    for address in plan.addresses_of_type(POLICY_TYPE):
        assert plan.after(address).get("confirm_remove_self_resource_access") in (None, False)
    source = _code(MODULES_DIR / "dynamodb" / "policy.tf")
    assert "confirm_remove_self_resource_access" not in source


# ---------------------------------------------------------------------------
# Validation: module inputs and the root operator variable.
# ---------------------------------------------------------------------------

_VALID_MODULE_ARGUMENTS: Final = {
    "name_prefix": f'"{PREFIX}"',
    "account_id": f'"{ACCOUNT_ID}"',
    "caller_principal_arn": f'"{OPERATORS[0]}"',
    "data_plane_principal_arns": _hcl_list(DATA_PLANE),
    "administrator_principal_arns": _hcl_list(ADMINISTRATORS),
}


def _plan_module_with(work_dir: Path, overrides: dict[str, str]) -> Plan:
    arguments = {**_VALID_MODULE_ARGUMENTS, **overrides}
    body = "\n".join(f"  {key} = {value}" for key, value in arguments.items())
    return plan_module(work_dir, "dynamodb", body)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("data_plane_principal_arns", "[]"),
        ("data_plane_principal_arns", '["not-an-arn"]'),
        ("data_plane_principal_arns", _hcl_list([ROOT_ARN])),
        ("data_plane_principal_arns", '["arn:aws:iam::12345:role/short-account"]'),
        ("administrator_principal_arns", "[]"),
        ("administrator_principal_arns", '["not-an-arn"]'),
        ("account_id", '"12345"'),
    ],
)
def test_module_validation_rejects(tmp_path: Path, variable: str, value: str) -> None:
    """An empty list, a non-ARN, `:root` on the data plane, or a malformed account id fails the plan."""
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan_module_with(tmp_path, {variable: value})
    message = str(raised.value)
    assert "Invalid value for variable" in message, message
    assert variable in message, message


@pytest.mark.parametrize(
    ("variable", "name"),
    [
        (variable, name)
        for variable in ("data_plane_principal_arns", "administrator_principal_arns")
        for name in ("role/*", "role/ops-?", "role/team/*-admin")
    ],
)
def test_module_validation_rejects_wildcards(tmp_path: Path, variable: str, name: str) -> None:
    """A `*` or `?` in the name part would match roles the list never names."""
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan_module_with(tmp_path, {variable: f'["arn:aws:iam::{ACCOUNT_ID}:{name}"]'})
    message = str(raised.value)
    assert "Invalid value for variable" in message, message
    assert variable in message, message


@pytest.mark.parametrize(
    ("variable", "valid"),
    [("data_plane_principal_arns", DATA_PLANE), ("administrator_principal_arns", ADMINISTRATORS)],
)
def test_module_validation_accepts_a_path_role(
    tmp_path: Path, variable: str, valid: list[str]
) -> None:
    """The control for the wildcard refusals: a role under an IAM path plans."""
    _skip_unless_terraform()
    path_role = f"arn:aws:iam::{ACCOUNT_ID}:role/service-role/crm/fixture-path-role"
    planned = _plan_module_with(tmp_path, {variable: _hcl_list([*valid, path_role])})
    policy = planned.after(f'module.subject.{POLICY_TYPE}.tables["accounts"]')["policy"]
    assert path_role in policy


def test_module_validation_accepts_root_as_administrator(tmp_path: Path) -> None:
    """The control for the rejections: the valid inputs, root in the administrator list, plan."""
    _skip_unless_terraform()
    planned = _plan_module_with(tmp_path, {})
    assert len(planned.addresses_of_type(POLICY_TYPE)) == len(TABLES)


@pytest.mark.parametrize("variable", ["data_plane_principal_arns", "administrator_principal_arns"])
def test_module_inputs_have_no_default(variable: str) -> None:
    block = _block(_code(MODULES_DIR / "dynamodb" / "variables.tf"), f'variable "{variable}"')
    assert not re.search(r"^\s*default\s*=", block, re.MULTILINE), block


def _plan_operator_variable(work_dir: Path, value: str) -> Plan:
    variable = _block(
        VARIABLES_TF.read_text(encoding="utf-8"), 'variable "crm_operator_principal_arns"'
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "terraform.tfvars").write_text(
        f"crm_operator_principal_arns = {value}\n", encoding="utf-8"
    )
    root = f'{variable}\nresource "terraform_data" "operators" {{\n  input = var.crm_operator_principal_arns\n}}\n'
    return plan_root(work_dir, root)


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        '["not-an-arn"]',
        _hcl_list([ROOT_ARN]),
        f'["arn:aws:iam::{ACCOUNT_ID}:role/*"]',
        f'["arn:aws:iam::{ACCOUNT_ID}:role/ops-?"]',
    ],
    ids=["empty", "non-arn", "root", "star", "question-mark"],
)
def test_operator_variable_validation_rejects(tmp_path: Path, value: str) -> None:
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan_operator_variable(tmp_path, value)
    message = str(raised.value)
    assert "Invalid value for variable" in message, message
    assert "crm_operator_principal_arns" in message, message


def test_operator_variable_accepts_role_arns(tmp_path: Path) -> None:
    _skip_unless_terraform()
    operators = [*OPERATORS, f"arn:aws:iam::{ACCOUNT_ID}:role/service-role/crm/fixture-path-role"]
    planned = _plan_operator_variable(tmp_path, _hcl_list(operators))
    assert planned.after("terraform_data.operators")["input"] == operators


def test_operator_variable_has_no_default() -> None:
    block = _block(_code(VARIABLES_TF), 'variable "crm_operator_principal_arns"')
    assert not re.search(r"^\s*default\s*=", block, re.MULTILINE), block


def test_example_carries_a_placeholder_operator_that_passes_validation() -> None:
    """The example sets the variable to the documentation account and a `REPLACE_ME` role."""
    text = TFVARS_EXAMPLE.read_text(encoding="utf-8")
    match = re.search(r"^crm_operator_principal_arns\s*=\s*(\[.*\])\s*$", text, re.MULTILINE)
    assert match is not None
    values = json.loads(match.group(1))
    assert values == [f"arn:aws:iam::{ACCOUNT_ID}:role/REPLACE_ME-operator"]
    assert all(re.fullmatch(r"arn:aws:iam::[0-9]{12}:(role|user)/.+", v) for v in values)


# ---------------------------------------------------------------------------
# The self-lock-out precondition on the policy resources.
# ---------------------------------------------------------------------------


POLICY_RESOURCE_HEADERS: Final = {
    "policy.tf": 'resource "aws_dynamodb_resource_policy" "tables"',
    "kms.tf": 'resource "aws_kms_key_policy" "dynamodb"',
}


def test_precondition_admits_a_listed_operator(tmp_path: Path) -> None:
    """The control for the refusals: an operator caller plans every policy resource."""
    _skip_unless_terraform()
    planned = _plan_module_with(tmp_path, {"caller_principal_arn": f'"{OPERATORS[1]}"'})
    assert len(planned.addresses_of_type(POLICY_TYPE)) == len(TABLES)
    assert "module.subject.aws_kms_key_policy.dynamodb" in planned.resource_changes


@pytest.mark.parametrize(
    "caller",
    [f"arn:aws:iam::{ACCOUNT_ID}:role/someone-else", LAMBDA_ROLES[0], ROOT_ARN],
    ids=["unlisted", "data-plane-only", "root"],
)
def test_precondition_refuses_a_caller_that_is_not_an_operator(tmp_path: Path, caller: str) -> None:
    """A caller outside the operators, a Lambda role, or the root fails the plan by name."""
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan_module_with(tmp_path, {"caller_principal_arn": f'"{caller}"'})
    message = str(raised.value)
    assert "Resource precondition failed" in message, message
    assert "crm_operator_principal_arns" in message, message


@pytest.mark.parametrize("file_name", sorted(POLICY_RESOURCE_HEADERS))
def test_each_policy_resource_carries_the_precondition(file_name: str) -> None:
    """On the policy resources themselves, so `-target` cannot plan a policy past it."""
    block = _block(_code(MODULES_DIR / "dynamodb" / file_name), POLICY_RESOURCE_HEADERS[file_name])
    assert re.search(
        r"condition\s*=\s*contains\(var\.administrator_principal_arns,\s*var\.caller_principal_arn\)"
        r'\s*&&\s*var\.caller_principal_arn\s*!=\s*"arn:aws:iam::\$\{var\.account_id\}:root"',
        block,
    ), block
    message = re.search(r'error_message\s*=\s*"([^"]*)"', block)
    assert message is not None
    assert "${" not in message.group(1), "the message names the variable, never a value"


def test_root_passes_the_session_issuer_as_the_caller() -> None:
    block = _block(_code(MAIN_TF), 'module "dynamodb"')
    assert re.search(
        r"caller_principal_arn\s*=\s*data\.aws_iam_session_context\.current\.issuer_arn", block
    ), block
    session = _block(_code(MAIN_TF), 'data "aws_iam_session_context" "current"')
    assert re.search(r"arn\s*=\s*data\.aws_caller_identity\.current\.arn", session), session


# ---------------------------------------------------------------------------
# Root wiring read from source.
# ---------------------------------------------------------------------------


def _grant_holding_modules(files: dict[str, str]) -> set[str]:
    """Names of `lambda_action` module calls whose body sets `dynamodb_kms_grant_enabled = true`."""
    found: set[str] = set()
    for text in files.values():
        for match in re.finditer(r'^module "([a-z_]+)" \{', text, re.MULTILINE):
            body = _block(text[match.start() :], f'module "{match.group(1)}"')
            if re.search(r'source\s*=\s*"\./modules/lambda_action"', body) and re.search(
                r"^\s*dynamodb_kms_grant_enabled\s*=\s*true\s*$", body, re.MULTILINE
            ):
                found.add(match.group(1))
    return found


def _allowlisted_modules(main_tf: str) -> set[str]:
    """Module names referenced by `local.dynamodb_data_plane_role_arns`."""
    match = re.search(
        r"dynamodb_data_plane_role_arns\s*=\s*concat\((.*?)\n  \)", main_tf, re.DOTALL
    )
    assert match is not None, "no `dynamodb_data_plane_role_arns = concat(...)` local"
    body = match.group(1)
    names = set(re.findall(r"\bmodule\.([a-z_]+)\.role_arn\b", body))
    names |= set(re.findall(r"\bin module\.([a-z_]+)\s*:", body))
    return names


def _root_files() -> dict[str, str]:
    return {p.name: _code(p) for p in sorted(INFRA.glob("*.tf"))}


def test_allowlist_is_exactly_the_grant_holding_lambda_actions() -> None:
    """Two-sided: every KMS-granted `lambda_action` is listed, and nothing else is."""
    files = _root_files()
    holding = _grant_holding_modules(files)
    assert len(holding) == 9, sorted(holding)
    assert _allowlisted_modules(files["main.tf"]) == holding


def test_allowlist_check_reds_on_a_dropped_entry() -> None:
    """Control: removing one entry from the local makes the sets differ."""
    files = _root_files()
    mutated = files["main.tf"].replace(
        "      module.lambda_action_user_settings.role_arn,\n", "", 1
    )
    assert mutated != files["main.tf"]
    assert _allowlisted_modules(mutated) != _grant_holding_modules(files)


def test_root_passes_operators_to_both_lists_and_root_to_administrators_only() -> None:
    block = _block(_code(MAIN_TF), 'module "dynamodb"')
    assert re.search(
        r"data_plane_principal_arns\s*=\s*concat\(local\.dynamodb_data_plane_role_arns,\s*"
        r"var\.crm_operator_principal_arns\)",
        block,
    ), block
    assert re.search(
        r"administrator_principal_arns\s*=\s*concat\(var\.crm_operator_principal_arns,\s*"
        r'\["arn:aws:iam::\$\{data\.aws_caller_identity\.current\.account_id\}:root"\]\)',
        block,
    ), block
    assert re.search(r"account_id\s*=\s*data\.aws_caller_identity\.current\.account_id", block)


def test_operator_output_is_the_variable() -> None:
    block = _block(_code(OUTPUTS_TF), 'output "crm_operator_principal_arns"')
    assert re.search(r"value\s*=\s*var\.crm_operator_principal_arns\s*$", block, re.MULTILINE)
