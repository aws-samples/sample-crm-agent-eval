# SPDX-License-Identifier: MIT
"""Plan time: the frontend CMK policy and the origin bucket policy deny every outside principal
key use, object writes and administration, and leave CDN reads and the OAC grants as they were.

`frontend_cdn` reads live AWS data, so its two policy resources and their documents are copied from
the committed HCL into a scratch module, with the account, key, bucket and distribution references
replaced by known literals, beside the module's own principal variables. That module is planned
offline (`_terraform_fixture`) and every rendered statement is asserted; the plan-baseline TLS-deny
check runs over the same plan. Skips when terraform or the cached providers are absent, unless
`CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path
from typing import Any, Final

import pytest
from scripts import check_plan_security_baseline, proc_run
from scripts.hcl_text import blank_comments

from tests.unit._terraform_fixture import (
    _PROVIDER_BLOCK,
    MODULES_DIR,
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    TerraformError,
    plan_directory,
    require_terraform,
    terraform_unavailable_reason,
)

MODULE_DIR: Final = MODULES_DIR / "frontend_cdn"
KMS_TF: Final = MODULE_DIR / "kms.tf"
S3_TF: Final = MODULE_DIR / "s3.tf"
VARIABLES_TF: Final = MODULE_DIR / "variables.tf"

ACCOUNT_ID: Final = "123456789012"
ROOT_ARN: Final = f"arn:aws:iam::{ACCOUNT_ID}:root"
BUCKET_NAME: Final = f"frontend-cdn-origin-{ACCOUNT_ID}"
BUCKET_ARN: Final = f"arn:aws:s3:::{BUCKET_NAME}"
DISTRIBUTION_ARN: Final = f"arn:aws:cloudfront::{ACCOUNT_ID}:distribution/EFIXTURE"
OPERATORS: Final = (
    f"arn:aws:iam::{ACCOUNT_ID}:role/fixture-operator",
    f"arn:aws:iam::{ACCOUNT_ID}:role/service-role/fixture-second-operator",
)
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
#: The object-read actions a read Deny would carry; none may be denied on the origin.
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

KEY_USE_SID: Final = "DenyKeyUseToNonProjectPrincipals"
KEY_ADMIN_SID: Final = "DenyKeyAdministrationToNonOperators"
WRITE_SID: Final = "DenyObjectWritesToNonOperators"
BUCKET_ADMIN_SID: Final = "DenyBucketAdministrationToNonOperators"
NEW_SIDS: Final = frozenset({KEY_USE_SID, KEY_ADMIN_SID, WRITE_SID, BUCKET_ADMIN_SID})
NOT_SERVICE: Final = {"Bool": {"aws:PrincipalIsAWSService": ["false"]}}

KEY_POLICY: Final = "module.subject.aws_kms_key_policy.frontend"
ORIGIN_POLICY: Final = "module.subject.aws_s3_bucket_policy.origin"

EXTRACTED_BLOCKS: Final = {
    KMS_TF: (
        'resource "aws_kms_key_policy" "frontend"',
        'data "aws_iam_policy_document" "frontend_kms"',
    ),
    S3_TF: (
        'resource "aws_s3_bucket_policy" "origin"',
        'data "aws_iam_policy_document" "origin"',
    ),
    VARIABLES_TF: (
        'variable "operator_principal_arns"',
        'variable "administrator_principal_arns"',
        'variable "caller_principal_arn"',
    ),
}

SUBSTITUTIONS: Final = {
    "data.aws_caller_identity.current.account_id": f'"{ACCOUNT_ID}"',
    "aws_kms_key.frontend.id": '"fixture-frontend-key"',
    "aws_cloudfront_distribution.frontend.arn": f'"{DISTRIBUTION_ARN}"',
    "aws_s3_bucket.origin.arn": f'"{BUCKET_ARN}"',
    "aws_s3_bucket.origin.id": f'"{BUCKET_NAME}"',
}
OWN_REFERENCES: Final = frozenset(
    {"data.aws_iam_policy_document.frontend_kms", "data.aws_iam_policy_document.origin"}
)


def _skip_unless_terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)


def _hcl_list(values: tuple[str, ...] | list[str]) -> str:
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def _block(text: str, header: str) -> str:
    """The one column-0 block starting with `header`, brace-matched over comment-blanked text."""
    starts = [m.start() for m in re.finditer(rf"(?m)^{re.escape(header)}", text)]
    assert len(starts) == 1, f"expected one {header!r} block, got {len(starts)}"
    depth = 0
    opened = False
    for index in range(starts[0], len(text)):
        if text[index] == "{":
            depth += 1
            opened = True
        elif text[index] == "}":
            depth -= 1
            if opened and depth == 0:
                return text[starts[0] : index + 1]
    raise AssertionError(f"unterminated {header!r}")


def _statement_source(document_block: str, sid: str) -> str:
    for match in re.finditer(r"(?m)^\s*statement\s*\{", document_block):
        body = _block(document_block[match.start() :].lstrip(), "statement")
        if re.search(rf'(?m)^\s*sid\s*=\s*"{re.escape(sid)}"\s*$', body):
            return body
    raise AssertionError(f"no statement with sid {sid!r}")


def _scratch_module_hcl() -> str:
    blocks = [
        _block(blank_comments(path.read_text(encoding="utf-8")), header)
        for path, headers in EXTRACTED_BLOCKS.items()
        for header in headers
    ]
    text = "\n\n".join(blocks) + "\n"
    for old, new in SUBSTITUTIONS.items():
        assert old in text, f"{old!r} no longer appears in the extracted source; update the fixture"
        text = text.replace(old, new)
    references = set(re.findall(r"\b(?:data\.)?aws_[a-z0-9_]+\.[a-z0-9_]+", text))
    leftovers = sorted(references - OWN_REFERENCES)
    assert leftovers == [], f"unsubstituted references: {leftovers}"
    return text


VALID_ARGUMENTS: Final = {
    "operator_principal_arns": _hcl_list(OPERATORS),
    "administrator_principal_arns": _hcl_list(ADMINISTRATORS),
    "caller_principal_arn": f'"{OPERATORS[0]}"',
}


def _write_root(work_dir: Path, overrides: dict[str, str] | None = None) -> None:
    subject = work_dir / "subject"
    subject.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(MODULE_DIR / "versions.tf", subject / "versions.tf")
    (subject / "policies.tf").write_text(_scratch_module_hcl(), encoding="utf-8")
    arguments = {**VALID_ARGUMENTS, **(overrides or {})}
    body = "\n".join(f"  {key} = {value}" for key, value in arguments.items())
    (work_dir / "main.tf").write_text(
        f'{_PROVIDER_BLOCK}\nmodule "subject" {{\n  source = "./subject"\n{body}\n}}\n',
        encoding="utf-8",
    )


def _plan(work_dir: Path, overrides: dict[str, str] | None = None) -> Plan:
    _write_root(work_dir, overrides)
    return plan_directory(work_dir)


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
    KEY_POLICY: [
        {
            "Sid": "EnableIAMUserPermissions",
            "Effect": "Allow",
            "Principal": {"AWS": ROOT_ARN},
            "Action": "kms:*",
            "Resource": "*",
        },
        {
            "Sid": "AllowCloudFrontOACDecrypt",
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "kms:Decrypt",
            "Resource": "*",
            "Condition": {"StringEquals": {"AWS:SourceArn": DISTRIBUTION_ARN}},
        },
        _deny(KEY_USE_SID, KEY_USE_ACTIONS, "*", OPERATORS, exempt_services=True),
        _deny(KEY_ADMIN_SID, STORE_KEY_ADMIN_ACTIONS, "*", ADMINISTRATORS, exempt_services=False),
    ],
    ORIGIN_POLICY: [
        {
            "Sid": "DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": {"AWS": "*"},
            "Action": "s3:*",
            "Resource": [BUCKET_ARN, f"{BUCKET_ARN}/*"],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        },
        {
            "Sid": "AllowCloudFrontOACRead",
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": f"{BUCKET_ARN}/*",
            "Condition": {"StringEquals": {"AWS:SourceArn": DISTRIBUTION_ARN}},
        },
        _deny(
            WRITE_SID,
            S3_OBJECT_WRITE_ACTIONS,
            [BUCKET_ARN, f"{BUCKET_ARN}/*"],
            OPERATORS,
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
}


# ---------------------------------------------------------------------------
# The rendered documents.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def plan_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    _skip_unless_terraform()
    work_dir = tmp_path_factory.mktemp("frontend-principal-deny")
    _write_root(work_dir)
    return work_dir


@pytest.fixture(scope="module")
def plan(plan_dir: Path) -> Plan:
    return plan_directory(plan_dir)


@pytest.mark.parametrize("address", sorted(EXPECTED))
def test_document_renders_exactly_the_expected_statements(plan: Plan, address: str) -> None:
    """Every statement of both documents, every element, as planned."""
    rendered = plan.after(address)["policy"]
    assert json.loads(rendered)["Version"] == "2012-10-17"
    assert document_problems(rendered, EXPECTED[address]) == []


@pytest.mark.parametrize("address", sorted(EXPECTED))
def test_every_new_deny_names_principal_star_literally(plan: Plan, address: str) -> None:
    statements = json.loads(plan.after(address)["policy"])["Statement"]
    new = [s for s in statements if s["Sid"] in NEW_SIDS]
    assert len(new) == 2
    assert all(s["Principal"] == "*" for s in new)


def test_service_exemption_is_on_key_use_only(plan: Plan) -> None:
    carrying = sorted(
        f"{address}:{s['Sid']}"
        for address in EXPECTED
        for s in json.loads(plan.after(address)["policy"])["Statement"]
        if "aws:PrincipalIsAWSService" in json.dumps(s)
    )
    assert carrying == [f"{KEY_POLICY}:{KEY_USE_SID}"]


def test_origin_denies_no_object_read(plan: Plan) -> None:
    """CDN reads stay governed by the OAC Allow alone: no new Deny names a read or list action."""
    statements = json.loads(plan.after(ORIGIN_POLICY)["policy"])["Statement"]
    denied = {
        action for s in statements if s["Sid"] in NEW_SIDS for action in _as_tuple(s["Action"])
    }
    assert denied.isdisjoint(S3_OBJECT_READ_ACTIONS), sorted(denied & set(S3_OBJECT_READ_ACTIONS))


def test_no_document_uses_an_ifexists_or_via_service_variant(plan: Plan) -> None:
    for address in EXPECTED:
        rendered = plan.after(address)["policy"]
        assert "IfExists" not in rendered
        assert "aws:ViaAWSService" not in rendered


def test_root_is_an_administrator_and_never_a_use_principal(plan: Plan) -> None:
    for address in EXPECTED:
        for statement in json.loads(plan.after(address)["policy"])["Statement"]:
            allowed = statement.get("Condition", {}).get("ArnNotEquals", {}).get("aws:PrincipalArn")
            if allowed is None:
                continue
            is_admin = statement["Sid"] in (KEY_ADMIN_SID, BUCKET_ADMIN_SID)
            assert (ROOT_ARN in allowed) is is_admin, (address, statement["Sid"])


def test_key_policy_keeps_the_lockout_check(plan: Plan) -> None:
    assert plan.after(KEY_POLICY)["bypass_policy_lockout_safety_check"] is False


def test_tls_deny_gate_passes_the_origin_policy(plan: Plan, plan_dir: Path) -> None:
    """`check_s3_tls_deny` still finds a valid non-TLS deny beside the new statements."""
    shown = proc_run.run(
        ["terraform", "show", "-json", "plan.bin"],
        cwd=plan_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert shown.returncode == 0, shown.stderr
    shown_path = plan_dir / "show.json"
    shown_path.write_text(shown.stdout, encoding="utf-8")
    findings = check_plan_security_baseline.check_s3_tls_deny(
        check_plan_security_baseline.load_plan(shown_path)
    )
    assert [(f.resource, f.status) for f in findings] == [(ORIGIN_POLICY, "pass")], findings


@pytest.mark.parametrize(
    ("path", "document", "sid", "actions"),
    [
        (KMS_TF, "frontend_kms", KEY_USE_SID, KEY_USE_ACTIONS),
        (KMS_TF, "frontend_kms", KEY_ADMIN_SID, STORE_KEY_ADMIN_ACTIONS),
        (S3_TF, "origin", WRITE_SID, S3_OBJECT_WRITE_ACTIONS),
        (S3_TF, "origin", BUCKET_ADMIN_SID, S3_BUCKET_ADMIN_ACTIONS),
    ],
)
def test_source_lists_each_tuple_in_contract_order(
    path: Path, document: str, sid: str, actions: tuple[str, ...]
) -> None:
    """The rendered document reorders actions (a provider set), so order is pinned in source."""
    block = _block(
        blank_comments(path.read_text(encoding="utf-8")),
        f'data "aws_iam_policy_document" "{document}"',
    )
    body = _statement_source(block, sid)
    listed = re.search(r"actions\s*=\s*\[(.*?)\]", body, re.DOTALL)
    assert listed is not None, body
    assert tuple(re.findall(r'"([^"]+)"', listed.group(1))) == actions


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
# Validation and the self-lock-out precondition.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("operator_principal_arns", "[]"),
        ("operator_principal_arns", '["not-an-arn"]'),
        ("operator_principal_arns", _hcl_list([ROOT_ARN])),
        ("operator_principal_arns", f'["arn:aws:iam::{ACCOUNT_ID}:role/*"]'),
        ("administrator_principal_arns", "[]"),
        ("administrator_principal_arns", '["not-an-arn"]'),
        ("administrator_principal_arns", f'["arn:aws:iam::{ACCOUNT_ID}:role/ops-?"]'),
    ],
)
def test_module_validation_rejects(tmp_path: Path, variable: str, value: str) -> None:
    """An empty list, a non-ARN, a wildcard, or `:root` in the use list fails the plan by name."""
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path, {variable: value})
    message = str(raised.value)
    assert "Invalid value for variable" in message, message
    assert variable in message, message


@pytest.mark.parametrize(
    "variable", ["operator_principal_arns", "administrator_principal_arns", "caller_principal_arn"]
)
def test_module_inputs_have_no_default(variable: str) -> None:
    text = blank_comments(VARIABLES_TF.read_text(encoding="utf-8"))
    block = _block(text, f'variable "{variable}"')
    assert not re.search(r"^\s*default\s*=", block, re.MULTILINE), block


def test_precondition_admits_a_listed_operator(tmp_path: Path) -> None:
    """The control for the refusals: another listed operator plans both resources."""
    _skip_unless_terraform()
    planned = _plan(tmp_path, {"caller_principal_arn": f'"{OPERATORS[1]}"'})
    assert set(EXPECTED) <= set(planned.resource_changes)


@pytest.mark.parametrize(
    "caller", [f"arn:aws:iam::{ACCOUNT_ID}:role/someone-else", ROOT_ARN], ids=["unlisted", "root"]
)
def test_precondition_refuses_a_caller_that_is_not_an_operator(tmp_path: Path, caller: str) -> None:
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path, {"caller_principal_arn": f'"{caller}"'})
    message = str(raised.value)
    assert message.count("Resource precondition failed") == 2, message
    assert "crm_operator_principal_arns" in message, message


@pytest.mark.parametrize(
    ("path", "header"),
    [
        (KMS_TF, 'resource "aws_kms_key_policy" "frontend"'),
        (S3_TF, 'resource "aws_s3_bucket_policy" "origin"'),
    ],
)
def test_each_policy_resource_carries_the_precondition(path: Path, header: str) -> None:
    block = _block(blank_comments(path.read_text(encoding="utf-8")), header)
    assert re.search(
        r"condition\s*=\s*contains\(var\.administrator_principal_arns,\s*var\.caller_principal_arn\)"
        r"\s*&&\s*var\.caller_principal_arn\s*!=\s*"
        r'"arn:aws:iam::\$\{data\.aws_caller_identity\.current\.account_id\}:root"',
        block,
    ), block
    message = re.search(r'error_message\s*=\s*"([^"]*)"', block)
    assert message is not None
    assert "${" not in message.group(1), "the message names the variable, never a value"
