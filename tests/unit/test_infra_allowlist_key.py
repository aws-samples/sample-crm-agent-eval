# SPDX-License-Identifier: MIT
"""Plan time: the API client-allowlist CMK, its SecureString parameter and the consumer grant.

The root's `aws_kms_key.api_allowlist`, its alias, `aws_kms_key_policy.api_allowlist` and
`aws_ssm_parameter.api_allowed_client_ids` are planned from their committed text with every outside
reference replaced by a known literal; the `lambda_action` module is planned whole with and without
config parameters. Skips when terraform or the cached providers are absent, unless
`CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
from scripts.hcl_text import blank_comments, blank_heredoc_bodies, blank_string_literals

from tests.unit import _eni_function_code_deny as eni_deny
from tests.unit._terraform_fixture import (
    MODULES_DIR,
    REPO_ROOT,
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    TerraformError,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)

INFRA: Final = REPO_ROOT / "infra"
MAIN_TF: Final = INFRA / "main.tf"
OUTPUTS_TF: Final = INFRA / "outputs.tf"
LAMBDA_ACTION: Final = MODULES_DIR / "lambda_action"

ACCOUNT_ID: Final = "123456789012"
ROOT_ARN: Final = f"arn:aws:iam::{ACCOUNT_ID}:root"
KEY_ARN: Final = f"arn:aws:kms:us-east-1:{ACCOUNT_ID}:key/11111111-2222-3333-4444-5555aaaa9999"
PARAMETER_NAME: Final = "/crm-agent-eval/api/allowed-client-ids"
PARAMETER_ARN: Final = f"arn:aws:ssm:us-east-1:{ACCOUNT_ID}:parameter{PARAMETER_NAME}"
SSM_SERVICE: Final = "ssm.us-east-1.amazonaws.com"
SPA_CLIENT_ID: Final = "fixturespaclient000000000000"

CONSUMERS: Final = (
    f"arn:aws:iam::{ACCOUNT_ID}:role/crm-agent-eval-search_accounts-role",
    f"arn:aws:iam::{ACCOUNT_ID}:role/crm-agent-eval-eval-trigger-role",
)
OPERATORS: Final = (
    f"arn:aws:iam::{ACCOUNT_ID}:role/fixture-operator",
    f"arn:aws:iam::{ACCOUNT_ID}:role/service-role/fixture-second-operator",
)

WRITE_ACTIONS: Final = (
    "kms:Encrypt",
    "kms:GenerateDataKey*",
    "kms:ReEncrypt*",
    "kms:CreateGrant",
)
KEY_USE_ACTIONS: Final = (
    "kms:Decrypt",
    "kms:Encrypt",
    "kms:GenerateDataKey*",
    "kms:ReEncrypt*",
    "kms:CreateGrant",
)
KEY_ADMIN_ACTIONS: Final = (
    "kms:PutKeyPolicy",
    "kms:ScheduleKeyDeletion",
    "kms:DisableKey",
    "kms:RevokeGrant",
    "kms:DisableKeyRotation",
    "kms:CreateAlias",
    "kms:UpdateAlias",
    "kms:DeleteAlias",
    "kms:TagResource",
    "kms:UntagResource",
    "kms:UpdateKeyDescription",
)

KEY: Final = "aws_kms_key.api_allowlist"
ALIAS: Final = "aws_kms_alias.api_allowlist"
KEY_POLICY: Final = "aws_kms_key_policy.api_allowlist"
PARAMETER: Final = "aws_ssm_parameter.api_allowed_client_ids"
GRANT: Final = "module.subject.aws_iam_role_policy.config_parameter_kms[0]"


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


def _hcl_list(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def _as_set(value: Any) -> frozenset[str]:
    return frozenset(value) if isinstance(value, list) else frozenset({value})


def _root_hcl() -> str:
    code = _code(MAIN_TF)
    blocks = "\n".join(
        _block(code, header)
        for header in (
            'resource "aws_kms_key" "api_allowlist"',
            'resource "aws_kms_alias" "api_allowlist"',
            'resource "aws_kms_key_policy" "api_allowlist"',
            'resource "aws_ssm_parameter" "api_allowed_client_ids"',
        )
    )
    substitutions = {
        "local.name_prefix": '"crm-agent-eval"',
        "data.aws_caller_identity.current.account_id": f'"{ACCOUNT_ID}"',
        "data.aws_iam_session_context.current.issuer_arn": "var.fixture_caller",
        "local.api_allowlist_key_consumer_arns": _hcl_list(CONSUMERS),
        "local.api_allowlist_ssm_service": f'"{SSM_SERVICE}"',
        "local.api_allowed_client_ids_parameter_arn": f'"{PARAMETER_ARN}"',
        "local.api_allowed_client_ids_parameter_name": f'"{PARAMETER_NAME}"',
        "module.cognito.spa_client_id": f'"{SPA_CLIENT_ID}"',
        "var.crm_operator_principal_arns": _hcl_list(OPERATORS),
    }
    for old, new in substitutions.items():
        assert old in blocks, f"{old!r} no longer appears in the extracted source"
        blocks = blocks.replace(old, new)
    leftovers = set(
        re.findall(
            r"\b(?:module|local|var|data|aws_[a-z0-9_]+)\.[a-z_]+", blank_string_literals(blocks)
        )
    ) - {"var.fixture_caller", "aws_kms_key.api_allowlist", "aws_kms_key_policy.api_allowlist"}
    assert leftovers == set(), f"unsubstituted references: {sorted(leftovers)}"
    return f'variable "fixture_caller" {{\n  type = string\n}}\n\n{blocks}'


def _plan_key(work_dir: Path, caller: str) -> Plan:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "terraform.tfvars").write_text(f'fixture_caller = "{caller}"\n', encoding="utf-8")
    return plan_root(work_dir, _root_hcl())


@pytest.fixture(scope="module")
def key_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return _plan_key(tmp_path_factory.mktemp("allowlist-key"), OPERATORS[0])


def _statements(key_plan: Plan) -> dict[str, dict[str, Any]]:
    policy = key_plan.after(KEY_POLICY)["policy"]
    assert isinstance(policy, str), "the allowlist key policy is not rendered at plan time"
    statements = json.loads(policy)["Statement"]
    by_sid = {s.get("Sid"): s for s in statements}
    assert len(by_sid) == len(statements), "duplicate sids"
    return by_sid


# ---------------------------------------------------------------------------
# The key, alias and parameter.
# ---------------------------------------------------------------------------


def test_the_key_rotates_and_is_classified(key_plan: Plan) -> None:
    key = key_plan.after(KEY)
    assert key["enable_key_rotation"] is True
    assert key["deletion_window_in_days"] == 7
    assert key["tags"]["data-classification"] == "internal"
    assert key_plan.after(ALIAS)["name"] == "alias/crm-agent-eval-api-allowlist"


def test_the_parameter_is_a_securestring_under_the_cmk(key_plan: Plan) -> None:
    parameter = key_plan.after(PARAMETER)
    assert parameter["type"] == "SecureString"
    assert parameter["tier"] == "Standard"
    assert parameter["name"] == PARAMETER_NAME
    assert parameter["tags"]["data-classification"] == "internal"
    assert key_plan.after_unknown(PARAMETER).get("key_id") is True
    expressions = key_plan.configuration["root_module"]["resources"]
    (resource,) = [r for r in expressions if r["address"] == PARAMETER]
    assert resource["expressions"]["key_id"]["references"][0] == "aws_kms_key.api_allowlist.arn"
    assert resource["depends_on"] == [KEY_POLICY]


# ---------------------------------------------------------------------------
# The key policy.
# ---------------------------------------------------------------------------


def test_the_key_policy_has_exactly_the_designed_statements(key_plan: Plan) -> None:
    assert list(_statements(key_plan)) == [
        "EnableIAMUserPermissions",
        "AllowOperatorsUseThroughSsm",
        "AllowConsumersDecryptTheParameter",
        "DenyKeyWriteToNonOperators",
        "DenyKeyUseToNonProjectPrincipals",
        "DenyKeyAdministrationToNonOperators",
    ]


def test_the_account_root_statement_is_kept(key_plan: Plan) -> None:
    assert _statements(key_plan)["EnableIAMUserPermissions"] == {
        "Sid": "EnableIAMUserPermissions",
        "Effect": "Allow",
        "Principal": {"AWS": ROOT_ARN},
        "Action": "kms:*",
        "Resource": "*",
    }


def test_no_allow_names_a_wildcard_principal(key_plan: Plan) -> None:
    for sid, statement in _statements(key_plan).items():
        if statement["Effect"] != "Allow":
            continue
        principal = statement["Principal"]
        assert isinstance(principal, dict) and set(principal) == {"AWS"}, sid
        assert "*" not in _as_set(principal["AWS"]), sid


def test_operators_encrypt_and_decrypt_only_through_ssm(key_plan: Plan) -> None:
    statement = _statements(key_plan)["AllowOperatorsUseThroughSsm"]
    assert statement["Effect"] == "Allow"
    assert _as_set(statement["Principal"]["AWS"]) == frozenset(OPERATORS)
    assert _as_set(statement["Action"]) == {"kms:Encrypt", "kms:Decrypt", "kms:DescribeKey"}
    assert statement["Condition"] == {"StringEquals": {"kms:ViaService": SSM_SERVICE}}


def test_consumers_decrypt_only_the_parameter_through_ssm(key_plan: Plan) -> None:
    statement = _statements(key_plan)["AllowConsumersDecryptTheParameter"]
    assert statement["Effect"] == "Allow"
    assert _as_set(statement["Principal"]["AWS"]) == frozenset(CONSUMERS)
    assert statement["Action"] == "kms:Decrypt"
    assert statement["Condition"] == {
        "StringEquals": {
            "kms:ViaService": SSM_SERVICE,
            "kms:EncryptionContext:PARAMETER_ARN": PARAMETER_ARN,
        }
    }


def test_no_allow_grants_a_consumer_anything_but_decrypt(key_plan: Plan) -> None:
    for sid, statement in _statements(key_plan).items():
        if (
            statement["Effect"] == "Allow"
            and sid != "EnableIAMUserPermissions"
            and _as_set(statement["Principal"]["AWS"]) & frozenset(CONSUMERS)
        ):
            assert _as_set(statement["Action"]) == {"kms:Decrypt"}, sid


def test_writes_are_refused_to_everyone_but_the_operators(key_plan: Plan) -> None:
    statement = _statements(key_plan)["DenyKeyWriteToNonOperators"]
    assert statement["Effect"] == "Deny"
    assert statement["Principal"] == "*"
    assert _as_set(statement["Action"]) == frozenset(WRITE_ACTIONS)
    assert statement["Resource"] == "*"
    assert statement["Condition"] == {
        "ArnNotEquals": {"aws:PrincipalArn": list(OPERATORS)},
        "Bool": {"aws:PrincipalIsAWSService": "false"},
    }


def test_key_use_is_refused_outside_the_consumers_and_operators(key_plan: Plan) -> None:
    statement = _statements(key_plan)["DenyKeyUseToNonProjectPrincipals"]
    assert statement["Effect"] == "Deny"
    assert statement["Principal"] == "*"
    assert _as_set(statement["Action"]) == frozenset(KEY_USE_ACTIONS)
    assert statement["Condition"] == {
        "ArnNotEquals": {"aws:PrincipalArn": [*CONSUMERS, *OPERATORS]},
        "Bool": {"aws:PrincipalIsAWSService": "false"},
    }


def test_key_administration_is_refused_to_everyone_but_operators_and_root(key_plan: Plan) -> None:
    statement = _statements(key_plan)["DenyKeyAdministrationToNonOperators"]
    assert statement["Effect"] == "Deny"
    assert statement["Principal"] == "*"
    assert _as_set(statement["Action"]) == frozenset(KEY_ADMIN_ACTIONS)
    assert statement["Condition"] == {"ArnNotEquals": {"aws:PrincipalArn": [*OPERATORS, ROOT_ARN]}}


def test_the_key_policy_keeps_the_lockout_check(key_plan: Plan) -> None:
    assert key_plan.after(KEY_POLICY)["bypass_policy_lockout_safety_check"] is False


def test_a_caller_that_is_not_an_operator_cannot_plan_the_key_policy(tmp_path: Path) -> None:
    _skip_unless_terraform()
    with pytest.raises(TerraformError, match="not an operator"):
        _plan_key(tmp_path / "root", f"arn:aws:iam::{ACCOUNT_ID}:role/someone-else")


def test_the_account_root_cannot_plan_the_key_policy(tmp_path: Path) -> None:
    _skip_unless_terraform()
    with pytest.raises(TerraformError, match="not an operator"):
        _plan_key(tmp_path / "root", ROOT_ARN)


# ---------------------------------------------------------------------------
# Root wiring, read from source.
# ---------------------------------------------------------------------------


def test_the_parameter_pins_the_standard_tier_in_source() -> None:
    """The allowlist parameter declares the Standard tier explicitly rather than by default."""
    block = _block(_code(MAIN_TF), 'resource "aws_ssm_parameter" "api_allowed_client_ids"')
    assert re.search(r'^  tier\s*=\s*"Standard"$', block, re.MULTILINE)


def test_the_ssm_service_is_the_deployment_regions() -> None:
    assert re.search(
        r'^  api_allowlist_ssm_service\s*=\s*"ssm\.\$\{var\.aws_region\}\.amazonaws\.com"$',
        _code(MAIN_TF),
        re.MULTILINE,
    )


def test_the_key_arn_is_an_output_for_smoke() -> None:
    output = _block(_code(OUTPUTS_TF), 'output "api_allowlist_kms_key_arn"')
    assert re.search(r"^  value\s*=\s*aws_kms_key\.api_allowlist\.arn$", output, re.MULTILINE)


def test_no_other_parameter_or_key_reference_uses_the_allowlist_key() -> None:
    """Only the parameter, the alias, the policy, the output and the consumer calls name the key."""
    for path in sorted(INFRA.glob("*.tf")):
        code = _code(path)
        for match in re.finditer(r'^resource "(\w+)" "(\w+)" \{', code, re.MULTILINE):
            kind, name = match.groups()
            if (kind, name) in {
                ("aws_kms_alias", "api_allowlist"),
                ("aws_kms_key_policy", "api_allowlist"),
                ("aws_ssm_parameter", "api_allowed_client_ids"),
            }:
                continue
            body = _block(code[match.start() :], f'resource "{kind}" "{name}"')
            assert "aws_kms_key.api_allowlist" not in body, f"{kind}.{name} in {path.name}"


# ---------------------------------------------------------------------------
# The `lambda_action` consumer grant.
# ---------------------------------------------------------------------------


def _plan_lambda_action(work_dir: Path, extra: dict[str, str]) -> Plan:
    work_dir.mkdir(parents=True, exist_ok=True)
    function_zip = work_dir / "function.zip"
    function_zip.write_bytes(b"")
    arguments = {
        "source": f'"{LAMBDA_ACTION}"',
        "function_name": '"crm-agent-eval-search_accounts"',
        "handler": '"crm.handlers.search_accounts.handler"',
        "filename": f'"{function_zip}"',
        "source_code_hash": '"0000000000000000000000000000000000000000000="',
        "reserved_concurrent_executions": "5",
        "dynamodb_grants": "[]",
        "vpc_subnet_ids": json.dumps(["subnet-fixturezonea", "subnet-fixturezoneb"]),
        "vpc_security_group_ids": json.dumps(["sg-fixturelambda"]),
        "eni_function_code_deny_policy_arn": f'"{eni_deny.POLICY_ARN}"',
        **extra,
    }
    body = "\n".join(f"  {key} = {value}" for key, value in arguments.items())
    return plan_root(work_dir, f'module "subject" {{\n{body}\n}}\n')


_GRANTED: Final = {
    "config_parameter_arns": json.dumps([PARAMETER_ARN]),
    "config_parameter_kms_key_arn": f'"{KEY_ARN}"',
}


@pytest.fixture(scope="module")
def granted_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return _plan_lambda_action(tmp_path_factory.mktemp("allowlist-grant"), dict(_GRANTED))


def _grant_statements(plan: Plan) -> dict[str, dict[str, Any]]:
    grant = plan.after(GRANT)
    assert grant["name"] == "crm-agent-eval-search_accounts-config-kms"
    return {s["Sid"]: s for s in json.loads(grant["policy"])["Statement"]}


def test_the_grant_has_exactly_the_decrypt_and_the_deny(granted_plan: Plan) -> None:
    assert set(_grant_statements(granted_plan)) == {
        "DecryptConfigParameters",
        "DenyConfigParameterDecryptUnderOtherKeys",
    }


def test_the_grant_decrypts_only_the_named_parameters_through_ssm(granted_plan: Plan) -> None:
    statement = _grant_statements(granted_plan)["DecryptConfigParameters"]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "kms:Decrypt"
    assert statement["Resource"] == KEY_ARN
    assert statement["Condition"] == {
        "StringEquals": {
            "kms:ViaService": SSM_SERVICE,
            "kms:EncryptionContext:PARAMETER_ARN": PARAMETER_ARN,
        }
    }


def test_decrypt_through_ssm_under_any_other_key_is_denied(granted_plan: Plan) -> None:
    statement = _grant_statements(granted_plan)["DenyConfigParameterDecryptUnderOtherKeys"]
    assert statement["Effect"] == "Deny"
    assert statement["Action"] == "kms:Decrypt"
    assert statement["NotResource"] == KEY_ARN
    assert "Resource" not in statement
    assert statement["Condition"] == {"StringLike": {"kms:ViaService": "ssm.*.amazonaws.com"}}


def test_no_grant_without_config_parameters(tmp_path: Path) -> None:
    _skip_unless_terraform()
    plan = _plan_lambda_action(tmp_path / "root", {})
    assert plan.addresses_of_type("aws_iam_role_policy").count(GRANT) == 0


def test_parameters_without_a_key_are_refused(tmp_path: Path) -> None:
    _skip_unless_terraform()
    with pytest.raises(TerraformError, match="config_parameter_kms_key_arn is null"):
        _plan_lambda_action(
            tmp_path / "root", {"config_parameter_arns": json.dumps([PARAMETER_ARN])}
        )


@pytest.mark.parametrize(
    "arn",
    ["*", f"arn:aws:kms:us-east-1:{ACCOUNT_ID}:key/*", f"arn:aws:kms:*:{ACCOUNT_ID}:alias/x"],
)
def test_a_wildcard_key_is_refused(tmp_path: Path, arn: str) -> None:
    _skip_unless_terraform()
    with pytest.raises(TerraformError, match="no wildcard"):
        _plan_lambda_action(
            tmp_path / "root", {**_GRANTED, "config_parameter_kms_key_arn": f'"{arn}"'}
        )


# ---------------------------------------------------------------------------
# The `store_principal_deny` source rows for the allowlist key, against planted defects.
# ---------------------------------------------------------------------------

#: Only the `.tf` sources the source gate reads; `.terraform` holds ~1 GB of provider binaries.
_TREE_EXCLUDED_PARTS: Final = frozenset({".terraform", "build"})

_POLICY_ROW: Final = "./aws_kms_key_policy.api_allowlist"
_PARAMETER_ROW: Final = "./aws_ssm_parameter.api_allowed_client_ids"
_GRANT_ROW: Final = "modules/lambda_action/data.aws_iam_policy_document.config_parameter_kms"
_CONSUMERS_ROW: Final = "local.api_allowlist_key_consumer_arns"


def _gate() -> Any:
    from scripts import check_plan_security_baseline as gate

    return gate


def _infra_copy(root: Path) -> Path:
    for path in INFRA.rglob("*.tf"):
        relative = path.relative_to(INFRA)
        if _TREE_EXCLUDED_PARTS & set(relative.parts):
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    return root


def _allowlist_rows(infra: Path) -> dict[str, str]:
    gate = _gate()
    control = next(c for c in gate.CONTROLS if c.control_id == "store_principal_deny")
    original = gate.INFRA_DIR
    try:
        gate.INFRA_DIR = infra
        findings = control.fn(gate.Plan(resource_changes=[]))
    finally:
        gate.INFRA_DIR = original
    return {
        f.resource: f.status
        for f in findings
        if "api_allowlist" in f.resource
        or f.resource in {_PARAMETER_ROW, _GRANT_ROW, _CONSUMERS_ROW}
    }


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} occurs {text.count(old)} times in {path.name}"
    path.write_text(text.replace(old, new), encoding="utf-8")


@pytest.fixture
def infra_copy(tmp_path: Path) -> Path:
    return _infra_copy(tmp_path / "infra")


def test_every_allowlist_row_passes_on_the_repository_tree() -> None:
    rows = _allowlist_rows(INFRA)
    assert {
        f"(count) {_POLICY_ROW}",
        f"{_POLICY_ROW} (statements)",
        f"{_POLICY_ROW}#AllowOperatorsUseThroughSsm",
        f"{_POLICY_ROW}#AllowConsumersDecryptTheParameter",
        f"{_POLICY_ROW}#DenyKeyWriteToNonOperators",
        f"{_POLICY_ROW}#DenyKeyUseToNonProjectPrincipals",
        f"{_POLICY_ROW}#DenyKeyAdministrationToNonOperators",
        f"{_POLICY_ROW} (precondition)",
        "./aws_kms_key.api_allowlist (rotation)",
        _PARAMETER_ROW,
        _GRANT_ROW,
        _CONSUMERS_ROW,
    } <= set(rows)
    assert {row for row, status in rows.items() if status != "pass"} == set()


def test_the_unmutated_copy_passes(infra_copy: Path) -> None:
    assert {row for row, status in _allowlist_rows(infra_copy).items() if status != "pass"} == set()


def _failing(infra: Path) -> set[str]:
    return {row for row, status in _allowlist_rows(infra).items() if status != "pass"}


def test_a_string_parameter_fails_the_parameter_row(infra_copy: Path) -> None:
    _replace(infra_copy / "main.tf", 'type   = "SecureString"', 'type   = "String"')
    assert _failing(infra_copy) == {_PARAMETER_ROW}


def test_a_dropped_consumer_deny_fails_the_grant_row(infra_copy: Path) -> None:
    module = infra_copy / "modules" / "lambda_action" / "main.tf"
    _replace(module, '"DenyConfigParameterDecryptUnderOtherKeys"', '"Unrelated"')
    assert _failing(infra_copy) == {_GRANT_ROW}


def test_a_missing_consumer_role_fails_the_consumers_row(infra_copy: Path) -> None:
    _replace(
        infra_copy / "main.tf",
        "      module.lambda_action_capabilities.role_arn,\n"
        "      module.lambda_action_user_settings.role_arn,\n",
        "      module.lambda_action_user_settings.role_arn,\n",
    )
    assert _failing(infra_copy) == {_CONSUMERS_ROW}


def test_an_operator_allow_without_the_ssm_condition_fails_its_row(infra_copy: Path) -> None:
    _replace(
        infra_copy / "main.tf",
        """        Condition = {
          StringEquals = { "kms:ViaService" = local.api_allowlist_ssm_service }
        }
""",
        "",
    )
    assert _failing(infra_copy) == {f"{_POLICY_ROW}#AllowOperatorsUseThroughSsm"}


def test_a_write_deny_admitting_the_consumers_fails_its_row(infra_copy: Path) -> None:
    _replace(
        infra_copy / "main.tf",
        'ArnNotEquals = { "aws:PrincipalArn" = var.crm_operator_principal_arns }',
        'ArnNotEquals = { "aws:PrincipalArn" = concat(local.api_allowlist_key_consumer_arns, '
        "var.crm_operator_principal_arns) }",
    )
    assert _failing(infra_copy) == {f"{_POLICY_ROW}#DenyKeyWriteToNonOperators"}


def test_a_consumer_given_encrypt_fails_its_row(infra_copy: Path) -> None:
    _replace(
        infra_copy / "main.tf",
        '        Action    = "kms:Decrypt"\n        Resource  = "*"\n        Condition = {\n'
        "          StringEquals = {\n",
        '        Action    = ["kms:Decrypt", "kms:Encrypt"]\n        Resource  = "*"\n'
        "        Condition = {\n          StringEquals = {\n",
    )
    assert _failing(infra_copy) == {f"{_POLICY_ROW}#AllowConsumersDecryptTheParameter"}
