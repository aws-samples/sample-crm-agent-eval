# SPDX-License-Identifier: MIT
"""Plan time: the pagination-cursor CMK, its five role grants, and the SPA CSP sources.

The root's `aws_kms_key.cursor`, its alias and `aws_kms_key_policy.cursor` are planned from their
committed text with every outside reference replaced by a known literal; the `lambda_action` module
is planned whole with the cursor grant on and off. The CSP locals are planned from their committed
text against the real `frontend/index.html`. Which functions receive the key, and which handlers
use the cursor codec, are read from source. Skips when terraform or the cached providers are
absent, unless `CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
import shutil
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
INDEX_HTML: Final = REPO_ROOT / "frontend" / "index.html"
SRC: Final = REPO_ROOT / "src"
LAMBDA_ACTION: Final = MODULES_DIR / "lambda_action"

ACCOUNT_ID: Final = "123456789012"
ROOT_ARN: Final = f"arn:aws:iam::{ACCOUNT_ID}:root"
KEY_ARN: Final = f"arn:aws:kms:us-east-1:{ACCOUNT_ID}:key/11111111-2222-3333-4444-5555cccc7777"
PURPOSE_KEY: Final = "kms:EncryptionContext:purpose"
PURPOSE: Final = "crm-cursor"
CONTEXT_KEYS: Final = ("operation", "purpose", "rep_id", "tenant")

CURSOR_TOOLS: Final = (
    "search_opportunities",
    "search_accounts",
    "list_activities",
    "list_audit_records",
    "list_proposals",
)
CURSOR_ROLES: Final = tuple(
    f"arn:aws:iam::{ACCOUNT_ID}:role/crm-agent-eval-{tool}-role" for tool in CURSOR_TOOLS
)
OPERATORS: Final = (
    f"arn:aws:iam::{ACCOUNT_ID}:role/fixture-operator",
    f"arn:aws:iam::{ACCOUNT_ID}:role/service-role/fixture-second-operator",
)

KEY_USE_ACTIONS: Final = (
    "kms:Decrypt",
    "kms:Encrypt",
    "kms:GenerateDataKey*",
    "kms:ReEncrypt*",
    "kms:CreateGrant",
)
CONTEXT_BOUND_ACTIONS: Final = (
    "kms:Decrypt",
    "kms:Encrypt",
    "kms:GenerateDataKey*",
    "kms:ReEncrypt*",
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

KEY: Final = "aws_kms_key.cursor"
ALIAS: Final = "aws_kms_alias.cursor"
KEY_POLICY: Final = "aws_kms_key_policy.cursor"
GRANT: Final = "module.subject.aws_iam_role_policy.dynamodb_kms[0]"
TABLE_KEY_ARN: Final = (
    f"arn:aws:kms:us-east-1:{ACCOUNT_ID}:key/11111111-2222-3333-4444-5555dddd8888"
)
TABLE_KEY_GRANT: Final = {
    "dynamodb_kms_key_arn": f'"{TABLE_KEY_ARN}"',
    "dynamodb_kms_grant_enabled": "true",
}


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


def _block_at(text: str, start: int) -> str:
    """The brace-matched block whose header line begins at offset `start`."""
    depth = 0
    for index in range(text.index("{", start), len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unterminated block at offset {start}")


def _locals_block_defining(text: str, name: str) -> str:
    """The one column-0 `locals {` block that defines `name` at two-space indent."""
    blocks = [_block_at(text, match.start()) for match in re.finditer(r"^locals \{", text, re.M)]
    found = [b for b in blocks if re.search(rf"^  {re.escape(name)}\s*=", b, re.MULTILINE)]
    assert len(found) == 1, f"expected one locals block defining {name!r}, got {len(found)}"
    return found[0]


def _hcl_list(values: tuple[str, ...] | list[str]) -> str:
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def _substitute(block: str, substitutions: dict[str, str], allowed: set[str]) -> str:
    for old, new in substitutions.items():
        assert old in block, (
            f"{old!r} no longer appears in the extracted source; update the fixture"
        )
        block = block.replace(old, new)
    leftovers = (
        set(
            re.findall(
                r"\b(?:module|local|var|data|aws_[a-z0-9_]+)\.[a-z_]+",
                blank_string_literals(block),
            )
        )
        - allowed
    )
    assert leftovers == set(), f"unsubstituted references: {sorted(leftovers)}"
    return block


def _as_set(value: Any) -> frozenset[str]:
    return frozenset(value) if isinstance(value, list) else frozenset({value})


# ---------------------------------------------------------------------------
# The root key, alias and key policy, planned from their committed text.
# ---------------------------------------------------------------------------


def _root_key_hcl() -> str:
    code = _code(MAIN_TF)
    blocks = "\n".join(
        _block(code, header)
        for header in (
            'resource "aws_kms_key" "cursor"',
            'resource "aws_kms_alias" "cursor"',
            'resource "aws_kms_key_policy" "cursor"',
        )
    )
    body = _substitute(
        blocks,
        {
            "local.name_prefix": '"crm-agent-eval"',
            "data.aws_caller_identity.current.account_id": f'"{ACCOUNT_ID}"',
            "data.aws_iam_session_context.current.issuer_arn": "var.fixture_caller",
            "local.cursor_key_principal_arns": _hcl_list(CURSOR_ROLES),
            "var.crm_operator_principal_arns": _hcl_list(OPERATORS),
        },
        {"var.fixture_caller", "aws_kms_key.cursor"},
    )
    return f'variable "fixture_caller" {{\n  type = string\n}}\n\n{body}'


def _plan_key(work_dir: Path, caller: str) -> Plan:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "terraform.tfvars").write_text(f'fixture_caller = "{caller}"\n', encoding="utf-8")
    return plan_root(work_dir, _root_key_hcl())


@pytest.fixture(scope="module")
def key_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return _plan_key(tmp_path_factory.mktemp("cursor-key"), OPERATORS[0])


def _key_policy_statements(key_plan: Plan) -> dict[str, dict[str, Any]]:
    policy = key_plan.after(KEY_POLICY)["policy"]
    assert isinstance(policy, str), "the cursor key policy is not rendered at plan time"
    statements = json.loads(policy)["Statement"]
    by_sid = {s.get("Sid"): s for s in statements}
    assert len(by_sid) == len(statements), "duplicate sids"
    return by_sid


def test_the_key_rotates_and_is_classified(key_plan: Plan) -> None:
    key = key_plan.after(KEY)
    assert key["enable_key_rotation"] is True
    assert key["tags"]["data-classification"] == "internal"
    assert key_plan.after(ALIAS)["name"] == "alias/crm-agent-eval-cursor"


def test_the_key_policy_has_exactly_the_designed_statements(key_plan: Plan) -> None:
    assert list(_key_policy_statements(key_plan)) == [
        "EnableIAMUserPermissions",
        "DenyCursorKeyUseOutsidePurpose",
        "DenyCursorKeyUnusedOperations",
        "DenyKeyUseToNonProjectPrincipals",
        "DenyKeyAdministrationToNonOperators",
    ]


def test_the_only_allow_is_the_account_root(key_plan: Plan) -> None:
    allows = [s for s in _key_policy_statements(key_plan).values() if s["Effect"] == "Allow"]
    assert allows == [
        {
            "Sid": "EnableIAMUserPermissions",
            "Effect": "Allow",
            "Principal": {"AWS": ROOT_ARN},
            "Action": "kms:*",
            "Resource": "*",
        }
    ]


def test_no_wildcard_principal_outside_a_deny(key_plan: Plan) -> None:
    for sid, statement in _key_policy_statements(key_plan).items():
        principal = statement.get("Principal")
        wildcard = principal == "*" or (isinstance(principal, dict) and "*" in principal.values())
        assert not wildcard or statement["Effect"] == "Deny", sid


def test_every_context_bound_action_is_refused_outside_the_cursor_purpose(key_plan: Plan) -> None:
    statement = _key_policy_statements(key_plan)["DenyCursorKeyUseOutsidePurpose"]
    assert statement["Effect"] == "Deny"
    assert statement["Principal"] == "*"
    assert _as_set(statement["Action"]) == frozenset(CONTEXT_BOUND_ACTIONS)
    assert statement["Resource"] == "*"
    assert statement["Condition"] == {"StringNotEquals": {PURPOSE_KEY: PURPOSE}}


def test_operations_the_codec_never_calls_are_refused_to_everyone(key_plan: Plan) -> None:
    statement = _key_policy_statements(key_plan)["DenyCursorKeyUnusedOperations"]
    assert statement["Effect"] == "Deny"
    assert statement["Principal"] == "*"
    assert _as_set(statement["Action"]) == {
        "kms:GenerateDataKey*",
        "kms:ReEncrypt*",
        "kms:CreateGrant",
    }
    assert statement["Resource"] == "*"
    assert "Condition" not in statement


def test_key_use_is_refused_to_everyone_but_the_five_roles_and_the_operators(
    key_plan: Plan,
) -> None:
    statement = _key_policy_statements(key_plan)["DenyKeyUseToNonProjectPrincipals"]
    assert statement["Effect"] == "Deny"
    assert statement["Principal"] == "*"
    assert _as_set(statement["Action"]) == frozenset(KEY_USE_ACTIONS)
    assert statement["Condition"] == {
        "ArnNotEquals": {"aws:PrincipalArn": [*CURSOR_ROLES, *OPERATORS]},
        "Bool": {"aws:PrincipalIsAWSService": "false"},
    }


def test_key_administration_is_refused_to_everyone_but_operators_and_root(key_plan: Plan) -> None:
    statement = _key_policy_statements(key_plan)["DenyKeyAdministrationToNonOperators"]
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
# The `lambda_action` cursor grant.
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


@pytest.fixture(scope="module")
def granted_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return _plan_lambda_action(
        tmp_path_factory.mktemp("cursor-grant"),
        {
            **TABLE_KEY_GRANT,
            "cursor_kms_key_arn": f'"{KEY_ARN}"',
            "cursor_kms_grant_enabled": "true",
        },
    )


def _grant_statements(plan: Plan) -> dict[str, dict[str, Any]]:
    grant = plan.after(GRANT)
    assert grant["name"] == "crm-agent-eval-search_accounts-dynamodb-kms"
    return {s["Sid"]: s for s in json.loads(grant["policy"])["Statement"]}


def test_the_grant_is_encrypt_and_decrypt_on_the_one_key_under_the_cursor_context(
    granted_plan: Plan,
) -> None:
    statements = _grant_statements(granted_plan)
    assert set(statements) == {"DecryptDynamoDbTableKey", "SealPaginationCursors"}
    statement = statements["SealPaginationCursors"]
    assert statement["Effect"] == "Allow"
    assert _as_set(statement["Action"]) == {"kms:Encrypt", "kms:Decrypt"}
    assert statement["Resource"] == KEY_ARN
    assert statement["Condition"] == {
        "StringEquals": {PURPOSE_KEY: PURPOSE},
        "ForAllValues:StringEquals": {"kms:EncryptionContextKeys": list(CONTEXT_KEYS)},
    }
    assert statements["DecryptDynamoDbTableKey"]["Resource"] == TABLE_KEY_ARN


def test_the_granted_context_keys_are_exactly_the_codecs() -> None:
    from crm.authz import Identity
    from crm.cursor import encryption_context

    identity = Identity(rep_id="rep-1", tenant="demo")
    assert tuple(sorted(encryption_context(identity, "op"))) == CONTEXT_KEYS


def test_no_cursor_statement_is_rendered_when_disabled(tmp_path: Path) -> None:
    _skip_unless_terraform()
    plan = _plan_lambda_action(tmp_path / "root", dict(TABLE_KEY_GRANT))
    assert set(_grant_statements(plan)) == {"DecryptDynamoDbTableKey"}
    assert "kms:Encrypt" not in plan.after(GRANT)["policy"]


def test_an_enabled_grant_without_a_key_is_refused(tmp_path: Path) -> None:
    _skip_unless_terraform()
    with pytest.raises(TerraformError, match="cursor_kms_key_arn is null"):
        _plan_lambda_action(
            tmp_path / "root", {**TABLE_KEY_GRANT, "cursor_kms_grant_enabled": "true"}
        )


def test_an_enabled_grant_without_the_table_key_grant_is_refused(tmp_path: Path) -> None:
    _skip_unless_terraform()
    with pytest.raises(TerraformError, match="requires dynamodb_kms_grant_enabled"):
        _plan_lambda_action(
            tmp_path / "root",
            {"cursor_kms_key_arn": f'"{KEY_ARN}"', "cursor_kms_grant_enabled": "true"},
        )


# ---------------------------------------------------------------------------
# Root wiring, read from source.
# ---------------------------------------------------------------------------


def _string_list(expression: str) -> list[str]:
    return re.findall(r'"([^"]+)"', expression)


def _cursor_locals() -> str:
    return _locals_block_defining(_code(MAIN_TF), "cursor_tools")


def test_the_cursor_tools_local_names_exactly_the_five_paginating_tools() -> None:
    match = re.search(r"^  cursor_tools\s*=\s*(\[[^\]]*\])", _cursor_locals(), re.MULTILINE)
    assert match is not None
    assert sorted(_string_list(match.group(1))) == sorted(CURSOR_TOOLS)


def test_the_key_policy_principals_are_the_five_roles_by_static_index() -> None:
    match = re.search(
        r"^  cursor_key_principal_arns\s*=\s*\[(.*?)\]\n", _cursor_locals(), re.MULTILINE | re.S
    )
    assert match is not None
    entries = re.findall(r'module\.lambda_action\["([a-z_]+)"\]\.role_arn', match.group(1))
    assert sorted(entries) == sorted(CURSOR_TOOLS)
    assert len(entries) == len([e for e in match.group(1).split(",") if e.strip()])


def _lambda_action_call() -> str:
    return _block(_code(MAIN_TF), 'module "lambda_action" {')


def test_the_key_arn_and_grant_go_only_to_the_cursor_tools() -> None:
    call = _lambda_action_call()
    assert re.search(
        r"contains\(local\.cursor_tools, each\.key\) \? \{ CRM_CURSOR_KMS_KEY_ARN = "
        r"aws_kms_key\.cursor\.arn \} : \{\}",
        call,
    ), "CRM_CURSOR_KMS_KEY_ARN is not set exactly when the tool is a cursor tool"
    assert re.search(
        r"^  cursor_kms_key_arn\s*= contains\(local\.cursor_tools, each\.key\) \? "
        r"aws_kms_key\.cursor\.arn : null$",
        call,
        re.MULTILINE,
    )
    assert re.search(
        r"^  cursor_kms_grant_enabled\s*= contains\(local\.cursor_tools, each\.key\)$",
        call,
        re.MULTILINE,
    )
    assert call.count("CRM_CURSOR_KMS_KEY_ARN") == 1


def test_no_other_module_call_receives_the_cursor_key() -> None:
    for path in sorted(INFRA.glob("*.tf")):
        code = _code(path)
        for match in re.finditer(r'^module "([a-z_]+)" \{', code, re.MULTILINE):
            if match.group(1) == "lambda_action":
                continue
            body = _block_at(code, match.start())
            assert "aws_kms_key.cursor" not in body, f"module.{match.group(1)} in {path.name}"
            assert "CRM_CURSOR_KMS_KEY_ARN" not in body, f"module.{match.group(1)} in {path.name}"


def _handler_module(tool: str) -> str:
    match = re.search(
        rf'^\s+{tool}\s*=\s*\{{\s*handler\s*=\s*"([\w.]+)\.handler"', _code(MAIN_TF), re.MULTILINE
    )
    assert match is not None, f"no local.tools entry for {tool}"
    return match.group(1)


def _tool_names() -> list[str]:
    tools = _locals_block_defining(_code(MAIN_TF), "tools")
    return re.findall(r"^\s{4}([a-z_]+)\s*=\s*\{\s*handler\s*=", tools, re.MULTILINE)


def _module_tree(dotted: str) -> ast.Module:
    return ast.parse((SRC / Path(*dotted.split("."))).with_suffix(".py").read_text("utf-8"))


_CODEC: Final = {"encode_cursor", "decode_cursor"}


def _search_functions_using_the_codec() -> set[str]:
    """Top-level `crm.search` functions that reach the cursor codec through calls in the module."""
    tree = _module_tree("crm.search")
    calls: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            calls[node.name] = {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
    users = {name for name, called in calls.items() if called & _CODEC}
    while True:
        more = {name for name, called in calls.items() if called & users} - users
        if not more:
            return users
        users |= more


def _uses_the_codec(dotted: str, search_users: set[str]) -> bool:
    wanted = {"crm.cursor": _CODEC, "crm.search": search_users}
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module in wanted
        and bool({alias.name for alias in node.names} & wanted[node.module])
        for node in ast.walk(_module_tree(dotted))
    )


def test_the_cursor_tools_are_exactly_the_tools_whose_handlers_use_the_codec() -> None:
    search_users = _search_functions_using_the_codec()
    assert {"search_accounts", "search_opportunities"} <= search_users
    users = {t for t in _tool_names() if _uses_the_codec(_handler_module(t), search_users)}
    assert len(_tool_names()) > len(CURSOR_TOOLS)
    assert users == set(CURSOR_TOOLS)


# ---------------------------------------------------------------------------
# CSP sources passed to `module.frontend_cdn`.
# ---------------------------------------------------------------------------


def _inline_script_hashes() -> list[str]:
    scripts = re.findall(r"<script>(.*?)</script>", INDEX_HTML.read_text("utf-8"), re.S)
    return [
        "sha256-" + base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode()
        for s in scripts
    ]


def _csp_locals_hcl(index_html: Path) -> str:
    block = _substitute(
        _locals_block_defining(_code(MAIN_TF), "spa_inline_script_hashes"),
        {
            "${path.module}/../frontend/index.html": str(index_html),
            "var.aws_region": '"us-east-1"',
            "module.cognito.domain": '"crm-fixture"',
        },
        set(),
    )
    return (
        f"{block}\n"
        'resource "terraform_data" "csp" {\n'
        "  input = {\n"
        "    hashes  = local.spa_inline_script_hashes\n"
        "    connect = local.spa_connect_src_extra\n"
        "  }\n"
        "}\n"
    )


def _planned_csp(work_dir: Path, index_html: Path) -> dict[str, list[str]]:
    plan = plan_root(work_dir, _csp_locals_hcl(index_html))
    value = plan.after("terraform_data.csp")["input"]
    assert isinstance(value, dict)
    return value


def test_the_script_hash_is_the_browser_hash_of_the_inline_theme_script(tmp_path: Path) -> None:
    _skip_unless_terraform()
    expected = _inline_script_hashes()
    assert len(expected) == 1, "frontend/index.html no longer holds exactly one inline script"
    assert _planned_csp(tmp_path / "root", INDEX_HTML)["hashes"] == expected


def test_the_script_hash_follows_an_edited_inline_script(tmp_path: Path) -> None:
    _skip_unless_terraform()
    edited = tmp_path / "index.html"
    edited.write_text(
        INDEX_HTML.read_text("utf-8").replace('"crm.theme"', '"crm.theme.v2"', 1), "utf-8"
    )
    planned = _planned_csp(tmp_path / "root", edited)["hashes"]
    assert planned != _inline_script_hashes()
    assert len(planned) == 1


def test_connect_src_is_the_regional_user_pool_endpoint_and_the_hosted_ui_domain(
    tmp_path: Path,
) -> None:
    _skip_unless_terraform()
    assert _planned_csp(tmp_path / "root", INDEX_HTML)["connect"] == [
        "https://cognito-idp.us-east-1.amazonaws.com",
        "https://crm-fixture.auth.us-east-1.amazoncognito.com",
    ]


def test_frontend_cdn_receives_both_csp_inputs_from_the_locals() -> None:
    call = _block(_code(MAIN_TF), 'module "frontend_cdn" {')
    assert re.search(r"^  csp_script_hashes\s*= local\.spa_inline_script_hashes$", call, re.M)
    assert re.search(r"^  csp_connect_src_extra\s*= local\.spa_connect_src_extra$", call, re.M)


# ---------------------------------------------------------------------------
# The `lambda_action` config-parameter read grant.
# ---------------------------------------------------------------------------

PARAMETER_ARN: Final = (
    f"arn:aws:ssm:us-east-1:{ACCOUNT_ID}:parameter/crm-agent-eval/api/allowed-client-ids"
)
LOGS_POLICY: Final = "module.subject.aws_iam_role_policy.logs"


def _plan_scratch_lambda_action(work_dir: Path, extra: dict[str, str]) -> Plan:
    """Plan a scratch copy whose log-group ARN is a literal, so the logs policy renders."""
    module_dir = work_dir / "lambda_action"
    shutil.copytree(LAMBDA_ACTION, module_dir, ignore=shutil.ignore_patterns(".terraform"))
    main = module_dir / "main.tf"
    text = main.read_text(encoding="utf-8")
    assert text.count("${aws_cloudwatch_log_group.this.arn}:*") == 1
    main.write_text(
        text.replace(
            "${aws_cloudwatch_log_group.this.arn}:*",
            f"arn:aws:logs:us-east-1:{ACCOUNT_ID}:log-group:/aws/lambda/fixture:*",
        ),
        encoding="utf-8",
    )
    return _plan_lambda_action(work_dir / "root", {"source": f'"{module_dir}"', **extra})


def _logs_statements(plan: Plan) -> dict[str, dict[str, Any]]:
    policy = plan.after(LOGS_POLICY)["policy"]
    assert isinstance(policy, str), "the logs policy is not rendered at plan time"
    return {s["Sid"]: s for s in json.loads(policy)["Statement"]}


def test_the_config_parameter_grant_is_get_parameter_on_exactly_the_named_arns(
    tmp_path: Path,
) -> None:
    _skip_unless_terraform()
    plan = _plan_scratch_lambda_action(
        tmp_path,
        {
            "config_parameter_arns": json.dumps([PARAMETER_ARN]),
            "config_parameter_kms_key_arn": f'"{KEY_ARN}"',
        },
    )
    statements = _logs_statements(plan)
    assert set(statements) == {"WriteOwnLogGroup", "ReadConfigParameters"}
    statement = statements["ReadConfigParameters"]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "ssm:GetParameter"
    assert statement["Resource"] == PARAMETER_ARN
    assert "Condition" not in statement


def test_no_config_parameter_statement_without_parameters(tmp_path: Path) -> None:
    _skip_unless_terraform()
    assert set(_logs_statements(_plan_scratch_lambda_action(tmp_path, {}))) == {"WriteOwnLogGroup"}


@pytest.mark.parametrize(
    "arn",
    [
        f"arn:aws:ssm:us-east-1:{ACCOUNT_ID}:parameter/*",
        f"arn:aws:ssm:us-east-1:{ACCOUNT_ID}:parameter/crm-agent-eval/*",
        "*",
        f"arn:aws:ssm:*:{ACCOUNT_ID}:parameter/crm-agent-eval/api/allowed-client-ids",
    ],
)
def test_a_wildcard_config_parameter_is_refused(tmp_path: Path, arn: str) -> None:
    _skip_unless_terraform()
    with pytest.raises(TerraformError, match="no wildcard"):
        _plan_lambda_action(tmp_path / "root", {"config_parameter_arns": json.dumps([arn])})


def test_approve_queries_only_the_opportunities_account_index(tmp_path: Path) -> None:
    """approve_proposal's Query renders on `opportunities/index/gsi_account` alone, never a table."""
    from tests.unit.test_infra_revert_child_check_grants import _render, _table_arn

    grants = _render(tmp_path, "approve_proposal")
    query = [g for g in grants if "dynamodb:Query" in g["actions"]]
    assert query, "approve_proposal has no Query grant"
    assert all(g["actions"] == ["dynamodb:Query"] for g in query), query
    assert all(g["include_table_arn"] is False for g in query), query
    assert {arn for g in query for arn in g["index_arns"]} == {
        f"{_table_arn('opportunities')}/index/gsi_account"
    }
    for grant in grants:
        if "dynamodb:Query" not in grant["actions"]:
            assert grant["index_arns"] == [] and grant["include_table_arn"] is True, grant
