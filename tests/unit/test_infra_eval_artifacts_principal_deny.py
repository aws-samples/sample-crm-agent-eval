# SPDX-License-Identifier: MIT
"""Plan time: the eval-artefacts bucket policy and its CMK's key policy deny every outside principal.

The `eval_artifacts` module plans offline whole (`_terraform_fixture`). The root's
`aws_kms_key_policy.eval_artifacts` is planned from its committed text with every outside reference
replaced by a known literal. Every rendered statement is asserted, and the principal lists, the
`codebuild_role_arn` output and the operator wiring are read from source. Skips when terraform or
the cached providers are absent, unless `CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
from scripts.hcl_text import blank_comments, blank_heredoc_bodies, blank_string_literals

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
MODULE_DIR: Final = MODULES_DIR / "eval_artifacts"
AGENTCORE_OUTPUTS_TF: Final = MODULES_DIR / "agentcore" / "outputs.tf"

ACCOUNT_ID: Final = "123456789012"
PREFIX: Final = "crm-agent-eval"
ROOT_ARN: Final = f"arn:aws:iam::{ACCOUNT_ID}:root"
BUCKET_ARN: Final = f"arn:aws:s3:::{PREFIX}-eval-artifacts-{ACCOUNT_ID}"
KMS_KEY_ARN: Final = f"arn:aws:kms:us-east-1:{ACCOUNT_ID}:key/11111111-2222-3333-4444-5555dddd6666"
PROJECT_ROLES: Final = (
    f"arn:aws:iam::{ACCOUNT_ID}:role/crm-agent-eval-eval-trigger-role",
    f"arn:aws:iam::{ACCOUNT_ID}:role/crm-agent-eval-sweep-control-role",
    f"arn:aws:iam::{ACCOUNT_ID}:role/crm-agent-eval-codebuild-role",
)
OPERATORS: Final = (
    f"arn:aws:iam::{ACCOUNT_ID}:role/fixture-operator",
    f"arn:aws:iam::{ACCOUNT_ID}:role/service-role/fixture-second-operator",
)
DATA_PLANE: Final = (*PROJECT_ROLES, *OPERATORS)
ADMINISTRATORS: Final = (*OPERATORS, ROOT_ARN)

KEY_USE_ACTIONS: Final = (
    "kms:Decrypt",
    "kms:Encrypt",
    "kms:GenerateDataKey*",
    "kms:ReEncrypt*",
    "kms:CreateGrant",
)
STORE_KEY_ADMIN_ACTIONS: Final = (
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
S3_OBJECT_READ_ACTIONS: Final = (
    "s3:GetObject",
    "s3:GetObjectVersion",
    "s3:GetObjectAttributes",
    "s3:GetObjectVersionAttributes",
    "s3:GetObjectTagging",
    "s3:GetObjectVersionTagging",
    "s3:RestoreObject",
    "s3:ListBucket",
    "s3:ListBucketVersions",
    "s3:ListBucketMultipartUploads",
    "s3:ListMultipartUploadParts",
)
S3_OBJECT_WRITE_ACTIONS: Final = (
    "s3:PutObject",
    "s3:PutObjectTagging",
    "s3:DeleteObject",
    "s3:DeleteObjectVersion",
    "s3:DeleteObjectTagging",
    "s3:AbortMultipartUpload",
    "s3:PutObjectVersionTagging",
    "s3:DeleteObjectVersionTagging",
)
S3_BUCKET_ADMIN_ACTIONS: Final = (
    "s3:DeleteBucket",
    "s3:PutBucketPolicy",
    "s3:DeleteBucketPolicy",
    "s3:PutBucketVersioning",
    "s3:PutEncryptionConfiguration",
    "s3:PutLifecycleConfiguration",
    "s3:PutBucketLogging",
    "s3:PutBucketPublicAccessBlock",
    "s3:PutBucketOwnershipControls",
    "s3:PutBucketAcl",
    "s3:PutBucketTagging",
    "s3:PutReplicationConfiguration",
    "s3:PutInventoryConfiguration",
    "s3:PutBucketNotification",
    "s3:PutBucketObjectLockConfiguration",
    "s3:PutBucketRequestPayment",
    "s3:PutBucketWebsite",
    "s3:PutBucketCORS",
    "s3:PutAccelerateConfiguration",
    "s3:PutIntelligentTieringConfiguration",
    "s3:PutAnalyticsConfiguration",
    "s3:PutMetricsConfiguration",
)

KEY_ROOT_SID: Final = "Enable IAM User Permissions"
KEY_USE_SID: Final = "DenyKeyUseToNonProjectPrincipals"
KEY_ADMIN_SID: Final = "DenyKeyAdministrationToNonOperators"
BUCKET_DATA_SID: Final = "DenyObjectDataPlaneToNonProjectPrincipals"
BUCKET_ADMIN_SID: Final = "DenyBucketAdministrationToNonOperators"
NEW_SIDS: Final = frozenset({KEY_USE_SID, KEY_ADMIN_SID, BUCKET_DATA_SID, BUCKET_ADMIN_SID})
NOT_SERVICE: Final = {"Bool": {"aws:PrincipalIsAWSService": ["false"]}}

BUCKET_POLICY: Final = "module.subject.aws_s3_bucket_policy.this"
KEY_POLICY: Final = "aws_kms_key_policy.eval_artifacts"


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


def _hcl_list(values: tuple[str, ...] | list[str]) -> str:
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


# ---------------------------------------------------------------------------
# Rendered-document normalisation and the exact-shape checker.
# ---------------------------------------------------------------------------


def _as_tuple(value: Any) -> tuple[str, ...]:
    return tuple(sorted(value)) if isinstance(value, list) else (value,)


def _normalise(statement: dict[str, Any]) -> dict[str, Any]:
    """A statement with Action/Resource and condition values as sorted tuples."""
    out: dict[str, Any] = {}
    for key, value in statement.items():
        if key in ("Action", "Resource"):
            out[key] = _as_tuple(value)
        elif key == "Condition":
            out[key] = {
                op: {k: _as_tuple(v) for k, v in keys.items()} for op, keys in value.items()
            }
        elif key == "Principal" and isinstance(value, dict):
            out[key] = {k: _as_tuple(v) for k, v in value.items()}
        else:
            out[key] = value
    return out


def document_problems(policy_json: str, expected: list[dict[str, Any]]) -> list[str]:
    """Every difference between a rendered document and `expected`, statement by statement."""
    statements = json.loads(policy_json)["Statement"]
    got = {s.get("Sid"): _normalise(s) for s in statements}
    want = {s["Sid"]: _normalise(s) for s in expected}
    problems = []
    if len(got) != len(statements):
        problems.append("duplicate sids")
    for sid in sorted(set(got) | set(want), key=str):
        if got.get(sid) != want.get(sid):
            problems.append(f"{sid}: rendered {got.get(sid)!r} != expected {want.get(sid)!r}")
    return problems


def _deny(
    sid: str,
    actions: tuple[str, ...],
    resources: str | list[str],
    principals: tuple[str, ...],
    *,
    exempt_services: bool,
) -> dict[str, Any]:
    condition: dict[str, Any] = {"ArnNotEquals": {"aws:PrincipalArn": list(principals)}}
    if exempt_services:
        condition.update(copy.deepcopy(NOT_SERVICE))
    return {
        "Sid": sid,
        "Effect": "Deny",
        "Principal": "*",
        "Action": list(actions),
        "Resource": resources,
        "Condition": condition,
    }


EXPECTED: Final[dict[str, list[dict[str, Any]]]] = {
    BUCKET_POLICY: [
        {
            "Sid": "DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": {"AWS": "*"},
            "Action": "s3:*",
            "Resource": [BUCKET_ARN, f"{BUCKET_ARN}/*"],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        },
        {
            "Sid": "DenyUnencryptedObjectUploads",
            "Effect": "Deny",
            "Principal": {"AWS": "*"},
            "Action": "s3:PutObject",
            "Resource": f"{BUCKET_ARN}/*",
            "Condition": {"StringNotEquals": {"s3:x-amz-server-side-encryption": "aws:kms"}},
        },
        _deny(
            BUCKET_DATA_SID,
            (*S3_OBJECT_READ_ACTIONS, *S3_OBJECT_WRITE_ACTIONS),
            [BUCKET_ARN, f"{BUCKET_ARN}/*"],
            DATA_PLANE,
            exempt_services=False,
        ),
        _deny(
            BUCKET_ADMIN_SID,
            S3_BUCKET_ADMIN_ACTIONS,
            BUCKET_ARN,
            ADMINISTRATORS,
            exempt_services=False,
        ),
    ],
    KEY_POLICY: [
        {
            "Sid": KEY_ROOT_SID,
            "Effect": "Allow",
            "Principal": {"AWS": ROOT_ARN},
            "Action": "kms:*",
            "Resource": "*",
        },
        _deny(KEY_USE_SID, KEY_USE_ACTIONS, "*", DATA_PLANE, exempt_services=True),
        _deny(KEY_ADMIN_SID, STORE_KEY_ADMIN_ACTIONS, "*", ADMINISTRATORS, exempt_services=False),
    ],
}


# ---------------------------------------------------------------------------
# The module bucket policy.
# ---------------------------------------------------------------------------

VALID_MODULE_ARGUMENTS: Final = {
    "name_prefix": f'"{PREFIX}"',
    "data_classification": '"internal"',
    "account_id": f'"{ACCOUNT_ID}"',
    "kms_key_arn": f'"{KMS_KEY_ARN}"',
    "access_log_bucket_id": f'"{PREFIX}-frontend-cdn-logs-{ACCOUNT_ID}"',
    "caller_principal_arn": f'"{OPERATORS[0]}"',
    "data_plane_principal_arns": _hcl_list(DATA_PLANE),
    "administrator_principal_arns": _hcl_list(ADMINISTRATORS),
}


def _plan_module_with(work_dir: Path, overrides: dict[str, str] | None = None) -> Plan:
    arguments = {**VALID_MODULE_ARGUMENTS, **(overrides or {})}
    body = "\n".join(f"  {key} = {value}" for key, value in arguments.items())
    return plan_module(work_dir, "eval_artifacts", body)


@pytest.fixture(scope="module")
def module_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return _plan_module_with(tmp_path_factory.mktemp("eval-artifacts-principal-deny"))


# ---------------------------------------------------------------------------
# The root key policy, planned from its committed text.
# ---------------------------------------------------------------------------


def _root_key_policy_hcl() -> str:
    block = _block(_code(MAIN_TF), 'resource "aws_kms_key_policy" "eval_artifacts"')
    substitutions = {
        "aws_kms_key.eval_artifacts.id": '"fixture-eval-artifacts-key"',
        "data.aws_caller_identity.current.account_id": f'"{ACCOUNT_ID}"',
        "data.aws_iam_session_context.current.issuer_arn": "var.fixture_caller",
        "local.eval_artifacts_principal_arns": _hcl_list(PROJECT_ROLES),
        "var.crm_operator_principal_arns": _hcl_list(OPERATORS),
    }
    for old, new in substitutions.items():
        assert old in block, (
            f"{old!r} no longer appears in the extracted source; update the fixture"
        )
        block = block.replace(old, new)
    leftovers = set(
        re.findall(
            r"\b(?:module|local|var|data|aws_[a-z0-9_]+)\.[a-z_]+",
            blank_string_literals(block),
        )
    ) - {"var.fixture_caller"}
    assert leftovers == set(), f"unsubstituted references: {sorted(leftovers)}"
    return f'variable "fixture_caller" {{\n  type = string\n}}\n\n{block}'


def _plan_key_policy(work_dir: Path, caller: str) -> Plan:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "terraform.tfvars").write_text(f'fixture_caller = "{caller}"\n', encoding="utf-8")
    return plan_root(work_dir, _root_key_policy_hcl())


@pytest.fixture(scope="module")
def key_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return _plan_key_policy(tmp_path_factory.mktemp("eval-artifacts-key-policy"), OPERATORS[0])


def _rendered(module_plan: Plan, key_plan: Plan, address: str) -> str:
    plan = module_plan if address == BUCKET_POLICY else key_plan
    policy = plan.after(address)["policy"]
    assert isinstance(policy, str), f"{address} policy is not rendered at plan time"
    return policy


@pytest.mark.parametrize("address", sorted(EXPECTED))
def test_document_renders_exactly_the_expected_statements(
    module_plan: Plan, key_plan: Plan, address: str
) -> None:
    """Every statement of both documents, every element, as planned."""
    rendered = _rendered(module_plan, key_plan, address)
    assert json.loads(rendered)["Version"] == "2012-10-17"
    assert document_problems(rendered, EXPECTED[address]) == []


def test_key_policy_lists_each_tuple_in_contract_order(key_plan: Plan) -> None:
    """`jsonencode` keeps source order, so the rendered actions are the contract tuples verbatim."""
    statements = {
        s["Sid"]: s for s in json.loads(key_plan.after(KEY_POLICY)["policy"])["Statement"]
    }
    assert statements[KEY_USE_SID]["Action"] == list(KEY_USE_ACTIONS)
    assert statements[KEY_ADMIN_SID]["Action"] == list(STORE_KEY_ADMIN_ACTIONS)


@pytest.mark.parametrize(
    ("sid", "actions"),
    [
        (BUCKET_DATA_SID, (*S3_OBJECT_READ_ACTIONS, *S3_OBJECT_WRITE_ACTIONS)),
        (BUCKET_ADMIN_SID, S3_BUCKET_ADMIN_ACTIONS),
    ],
)
def test_bucket_source_lists_each_tuple_in_contract_order(
    sid: str, actions: tuple[str, ...]
) -> None:
    """The rendered bucket document reorders actions (a provider set), so order is pinned in source."""
    document = _block(_code(MODULE_DIR / "main.tf"), 'data "aws_iam_policy_document" "bucket"')
    bodies = re.split(r"\n  statement \{", document)
    body = next(b for b in bodies if f'sid    = "{sid}"' in b)
    listed = re.search(r"actions\s*=\s*\[(.*?)\]", body, re.DOTALL)
    assert listed is not None, body
    assert tuple(re.findall(r'"([^"]+)"', listed.group(1))) == actions


def test_service_exemption_is_on_key_use_only(module_plan: Plan, key_plan: Plan) -> None:
    carrying = sorted(
        f"{address}:{s['Sid']}"
        for address in EXPECTED
        for s in json.loads(_rendered(module_plan, key_plan, address))["Statement"]
        if "aws:PrincipalIsAWSService" in json.dumps(s)
    )
    assert carrying == [f"{KEY_POLICY}:{KEY_USE_SID}"]


def test_every_new_deny_names_principal_star_literally(module_plan: Plan, key_plan: Plan) -> None:
    for address in EXPECTED:
        statements = json.loads(_rendered(module_plan, key_plan, address))["Statement"]
        new = [s for s in statements if s["Sid"] in NEW_SIDS]
        assert len(new) == 2, address
        assert all(s["Principal"] == "*" for s in new), address


def test_no_document_uses_an_ifexists_or_via_service_variant(
    module_plan: Plan, key_plan: Plan
) -> None:
    for address in EXPECTED:
        rendered = _rendered(module_plan, key_plan, address)
        assert "IfExists" not in rendered
        assert "aws:ViaAWSService" not in rendered


def test_key_policy_keeps_the_lockout_check(key_plan: Plan) -> None:
    assert key_plan.after(KEY_POLICY)["bypass_policy_lockout_safety_check"] is False


# ---------------------------------------------------------------------------
# The checker discriminates: each element of each new statement is load-bearing.
# ---------------------------------------------------------------------------


def _mutants() -> list[tuple[str, str, tuple[str, str, Any]]]:
    out: list[tuple[str, str, tuple[str, str, Any]]] = []
    for address, expected in EXPECTED.items():
        for statement in expected:
            sid = statement["Sid"]
            if sid not in NEW_SIDS:
                continue
            for key in ("Effect", "Principal", "Action", "Resource", "Condition"):
                out.append((address, f"{sid}-drop-{key}", ("drop", sid, key)))
            for action in statement["Action"]:
                out.append((address, f"{sid}-drop-{action}", ("drop-action", sid, action)))
            for operator in statement["Condition"]:
                out.append((address, f"{sid}-drop-{operator}", ("drop-operator", sid, operator)))
            out.append((address, f"{sid}-ifexists", ("rename", sid, "ArnNotEqualsIfExists")))
            out.append((address, f"{sid}-stringnotequals", ("rename", sid, "StringNotEquals")))
            out.append((address, f"{sid}-drop-principal-value", ("drop-value", sid, None)))
            if "Bool" in statement["Condition"]:
                out.append((address, f"{sid}-swap-exemption", ("swap", sid, "Bool")))
            else:
                out.append((address, f"{sid}-add-exemption", ("add-exemption", sid, None)))
    return out


def _apply(expected: list[dict[str, Any]], mutation: tuple[str, str, Any]) -> str:
    kind, sid, arg = mutation
    statements = copy.deepcopy(expected)
    target = next(s for s in statements if s["Sid"] == sid)
    if kind == "drop":
        del target[arg]
    elif kind == "drop-action":
        target["Action"] = [a for a in target["Action"] if a != arg]
    elif kind == "drop-operator":
        del target["Condition"][arg]
    elif kind == "rename":
        target["Condition"][arg] = target["Condition"].pop("ArnNotEquals")
    elif kind == "drop-value":
        values = target["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]
        target["Condition"]["ArnNotEquals"]["aws:PrincipalArn"] = values[1:]
    elif kind == "add-exemption":
        target["Condition"].update(copy.deepcopy(NOT_SERVICE))
    elif kind == "swap":
        target["Condition"]["BoolIfExists"] = target["Condition"].pop(arg)
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


MUTANTS: Final = _mutants()


def test_the_checker_passes_an_unmutated_document() -> None:
    for address, expected in EXPECTED.items():
        rendered = json.dumps({"Version": "2012-10-17", "Statement": expected})
        assert document_problems(rendered, expected) == [], address


@pytest.mark.parametrize(
    ("address", "mutation"),
    [(address, mutation) for address, _, mutation in MUTANTS],
    ids=[f"{address.rsplit('.', 1)[-1]}-{mutant}" for address, mutant, _ in MUTANTS],
)
def test_the_checker_reds_without_each_element(
    address: str, mutation: tuple[str, str, Any]
) -> None:
    expected = EXPECTED[address]
    problems = document_problems(_apply(expected, mutation), expected)
    assert problems and all(mutation[1] in p for p in problems), problems


# ---------------------------------------------------------------------------
# Validation and the self-lock-out preconditions.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("data_plane_principal_arns", "[]"),
        ("data_plane_principal_arns", '["not-an-arn"]'),
        ("data_plane_principal_arns", _hcl_list([ROOT_ARN])),
        ("data_plane_principal_arns", f'["arn:aws:iam::{ACCOUNT_ID}:role/*"]'),
        ("administrator_principal_arns", "[]"),
        ("administrator_principal_arns", '["not-an-arn"]'),
        ("administrator_principal_arns", f'["arn:aws:iam::{ACCOUNT_ID}:role/ops-?"]'),
    ],
)
def test_module_validation_rejects(tmp_path: Path, variable: str, value: str) -> None:
    """An empty list, a non-ARN, a wildcard, or `:root` on the data plane fails the plan by name."""
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan_module_with(tmp_path, {variable: value})
    message = str(raised.value)
    assert "Invalid value for variable" in message, message
    assert variable in message, message


@pytest.mark.parametrize(
    "variable",
    ["data_plane_principal_arns", "administrator_principal_arns", "caller_principal_arn"],
)
def test_module_inputs_have_no_default(variable: str) -> None:
    block = _block(_code(MODULE_DIR / "variables.tf"), f'variable "{variable}"')
    assert not re.search(r"^\s*default\s*=", block, re.MULTILINE), block


def test_module_precondition_admits_a_listed_operator(tmp_path: Path) -> None:
    _skip_unless_terraform()
    planned = _plan_module_with(tmp_path, {"caller_principal_arn": f'"{OPERATORS[1]}"'})
    assert BUCKET_POLICY in planned.resource_changes


@pytest.mark.parametrize(
    "caller",
    [f"arn:aws:iam::{ACCOUNT_ID}:role/someone-else", PROJECT_ROLES[0], ROOT_ARN],
    ids=["unlisted", "data-plane-only", "root"],
)
def test_module_precondition_refuses_a_caller_that_is_not_an_operator(
    tmp_path: Path, caller: str
) -> None:
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan_module_with(tmp_path, {"caller_principal_arn": f'"{caller}"'})
    message = str(raised.value)
    assert "Resource precondition failed" in message, message
    assert "crm_operator_principal_arns" in message, message


def test_key_precondition_admits_a_listed_operator(tmp_path: Path) -> None:
    _skip_unless_terraform()
    assert KEY_POLICY in _plan_key_policy(tmp_path, OPERATORS[1]).resource_changes


@pytest.mark.parametrize(
    "caller",
    [f"arn:aws:iam::{ACCOUNT_ID}:role/someone-else", PROJECT_ROLES[2], ROOT_ARN],
    ids=["unlisted", "codebuild", "root"],
)
def test_key_precondition_refuses_a_caller_that_is_not_an_operator(
    tmp_path: Path, caller: str
) -> None:
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan_key_policy(tmp_path, caller)
    message = str(raised.value)
    assert "Resource precondition failed" in message, message
    assert "crm_operator_principal_arns" in message, message


def test_module_bucket_policy_carries_the_precondition() -> None:
    block = _block(_code(MODULE_DIR / "main.tf"), 'resource "aws_s3_bucket_policy" "this"')
    assert re.search(
        r"condition\s*=\s*contains\(var\.administrator_principal_arns,\s*var\.caller_principal_arn\)"
        r'\s*&&\s*var\.caller_principal_arn\s*!=\s*"arn:aws:iam::\$\{var\.account_id\}:root"',
        block,
    ), block


# ---------------------------------------------------------------------------
# Root wiring, read from source.
# ---------------------------------------------------------------------------

ROOT_ADMINISTRATORS: Final = (
    r"concat\(var\.crm_operator_principal_arns,\s*"
    r'\["arn:aws:iam::\$\{data\.aws_caller_identity\.current\.account_id\}:root"\]\)'
)


def test_the_principal_local_is_exactly_trigger_sweep_control_and_codebuild() -> None:
    match = re.search(r"\beval_artifacts_principal_arns\s*=\s*\[(.*?)\]", _code(MAIN_TF), re.DOTALL)
    assert match is not None, "no `eval_artifacts_principal_arns = [...]` local"
    entries = [e.strip() for e in match.group(1).split(",") if e.strip()]
    assert entries == [
        "module.lambda_action_eval_trigger.role_arn",
        "module.lambda_action_sweep_control.role_arn",
        "module.agentcore.codebuild_role_arn",
    ]


def test_agentcore_exports_the_codebuild_role_arn() -> None:
    block = _block(_code(AGENTCORE_OUTPUTS_TF), 'output "codebuild_role_arn"')
    assert re.search(r"value\s*=\s*aws_iam_role\.codebuild\.arn\s*$", block, re.MULTILINE), block


def test_the_root_wires_the_bucket_lists_and_the_caller() -> None:
    block = _block(_code(MAIN_TF), 'module "eval_artifacts"')
    assert re.search(
        r"data_plane_principal_arns\s*=\s*concat\(local\.eval_artifacts_principal_arns,\s*"
        r"var\.crm_operator_principal_arns\)",
        block,
    ), block
    assert re.search(rf"administrator_principal_arns\s*=\s*{ROOT_ADMINISTRATORS}", block), block
    assert re.search(
        r"caller_principal_arn\s*=\s*data\.aws_iam_session_context\.current\.issuer_arn", block
    ), block


def test_the_root_key_policy_reads_the_same_lists() -> None:
    block = _block(_code(MAIN_TF), 'resource "aws_kms_key_policy" "eval_artifacts"')
    assert re.search(
        r'"aws:PrincipalArn"\s*=\s*concat\(local\.eval_artifacts_principal_arns,\s*'
        r"var\.crm_operator_principal_arns\)",
        block,
    ), block
    assert re.search(rf'"aws:PrincipalArn"\s*=\s*{ROOT_ADMINISTRATORS}', block), block
    assert re.search(r"key_id\s*=\s*aws_kms_key\.eval_artifacts\.id", block), block
    assert re.search(r"bypass_policy_lockout_safety_check\s*=\s*false", block), block


def test_the_key_sets_no_inline_policy() -> None:
    """One owner of the document: the key carries no `policy`, and its skip names the resource."""
    block = _block(MAIN_TF.read_text(encoding="utf-8"), 'resource "aws_kms_key" "eval_artifacts"')
    assert not re.search(r"^\s*policy\s*=", blank_comments(block), re.MULTILINE), block
    assert "checkov:skip=CKV2_AWS_64: policy managed by aws_kms_key_policy.eval_artifacts" in block
