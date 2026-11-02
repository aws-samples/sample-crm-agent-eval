# SPDX-License-Identifier: MIT
"""Plan time: the logs and CloudTrail CMK policies and the CloudTrail bucket policy deny every
outside principal.

`observability` reads live AWS data, so its policy-bearing blocks are copied from the committed HCL
into a scratch module, with the account, region and bucket references replaced by known literals,
beside the module's own `variables.tf`. That module is planned offline (`_terraform_fixture`) and
every rendered statement is asserted. A plan against a seeded state holding the pre-Deny documents
shows each policy change is an in-place update. Skips when terraform or the cached providers are
absent, unless `CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Final

import pytest
from scripts import proc_run
from scripts.hcl_text import blank_comments

from tests.unit._terraform_fixture import (
    _PROVIDER_BLOCK,
    MODULES_DIR,
    PLUGIN_DIR,
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    TerraformError,
    plan_directory,
    require_terraform,
    terraform_unavailable_reason,
)

MODULE_DIR: Final = MODULES_DIR / "observability"
KMS_TF: Final = MODULE_DIR / "kms.tf"
CLOUDTRAIL_TF: Final = MODULE_DIR / "cloudtrail.tf"

ACCOUNT_ID: Final = "123456789012"
REGION: Final = "us-east-1"
ROOT_ARN: Final = f"arn:aws:iam::{ACCOUNT_ID}:root"
BUCKET_NAME: Final = f"cloudtrail-logs-{ACCOUNT_ID}"
BUCKET_ARN: Final = f"arn:aws:s3:::{BUCKET_NAME}"
LAMBDA_ROLES: Final = tuple(
    f"arn:aws:iam::{ACCOUNT_ID}:role/crm-agent-eval-fixture-{n}-role" for n in range(11)
)
OPERATORS: Final = (
    f"arn:aws:iam::{ACCOUNT_ID}:role/fixture-operator",
    f"arn:aws:iam::{ACCOUNT_ID}:role/service-role/fixture-second-operator",
)
LOGS_KEY_USERS: Final = (*LAMBDA_ROLES, *OPERATORS)
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

KEY_USE_SID: Final = "DenyKeyUseToNonProjectPrincipals"
KEY_ADMIN_SID: Final = "DenyKeyAdministrationToNonOperators"
BUCKET_DATA_SID: Final = "DenyObjectDataPlaneToNonProjectPrincipals"
BUCKET_ADMIN_SID: Final = "DenyBucketAdministrationToNonOperators"
NEW_SIDS: Final = frozenset({KEY_USE_SID, KEY_ADMIN_SID, BUCKET_DATA_SID, BUCKET_ADMIN_SID})
NOT_SERVICE: Final = {"Bool": {"aws:PrincipalIsAWSService": ["false"]}}

LOGS_KEY: Final = "module.subject.aws_kms_key.logs"
CLOUDTRAIL_KEY: Final = "module.subject.aws_kms_key.cloudtrail"
BUCKET_POLICY: Final = "module.subject.aws_s3_bucket_policy.cloudtrail"
POLICY_ATTRIBUTE: Final = {LOGS_KEY: "policy", CLOUDTRAIL_KEY: "policy", BUCKET_POLICY: "policy"}

#: The policy-bearing blocks, by source file, copied into the scratch module.
EXTRACTED_BLOCKS: Final = {
    KMS_TF: (
        'data "aws_iam_policy_document" "cloudwatch_logs_cmk"',
        'resource "aws_kms_key" "logs"',
    ),
    CLOUDTRAIL_TF: (
        "locals {\n  cloudtrail_name",
        'resource "aws_s3_bucket_policy" "cloudtrail"',
        'data "aws_iam_policy_document" "cloudtrail_bucket"',
        'data "aws_iam_policy_document" "cloudtrail_cmk"',
        'resource "aws_kms_key" "cloudtrail"',
    ),
}

#: Live or out-of-scope references in the extracted blocks, and their literal stand-ins.
SUBSTITUTIONS: Final = {
    "data.aws_caller_identity.current.account_id": f'"{ACCOUNT_ID}"',
    "data.aws_region.current.region": f'"{REGION}"',
    "aws_s3_bucket.cloudtrail.arn": f'"{BUCKET_ARN}"',
    "aws_s3_bucket.cloudtrail.id": f'"{BUCKET_NAME}"',
}


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
    leftovers = sorted(
        set(re.findall(r"\b(?:data\.)?aws_[a-z0-9_]+\.[a-z0-9_]+", text))
        - {
            "data.aws_iam_policy_document.cloudwatch_logs_cmk",
            "data.aws_iam_policy_document.cloudtrail_bucket",
            "data.aws_iam_policy_document.cloudtrail_cmk",
        }
    )
    leftovers = [ref for ref in leftovers if not ref.startswith(("aws_kms_key.", "aws_s3_bucket_"))]
    assert leftovers == [], f"unsubstituted references: {leftovers}"
    return text


VALID_ARGUMENTS: Final = {
    "logs_key_user_principal_arns": _hcl_list(LOGS_KEY_USERS),
    "operator_principal_arns": _hcl_list(OPERATORS),
    "administrator_principal_arns": _hcl_list(ADMINISTRATORS),
    "caller_principal_arn": f'"{OPERATORS[0]}"',
}


def _write_root(work_dir: Path, overrides: dict[str, str] | None = None) -> None:
    subject = work_dir / "subject"
    subject.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(MODULE_DIR / "variables.tf", subject / "variables.tf")
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
    exemption_operator: str = "Bool",
) -> dict[str, Any]:
    condition: dict[str, Any] = {"ArnNotEquals": {"aws:PrincipalArn": list(principals)}}
    if exempt_services:
        condition[exemption_operator] = copy.deepcopy(NOT_SERVICE["Bool"])
    return {
        "Sid": sid,
        "Effect": "Deny",
        "Principal": "*",
        "Action": list(actions),
        "Resource": resources,
        "Condition": condition,
    }


ROOT_ALLOW: Final = {
    "Sid": "EnableIAMUserPermissions",
    "Effect": "Allow",
    "Principal": {"AWS": ROOT_ARN},
    "Action": "kms:*",
    "Resource": "*",
}
CLOUDTRAIL_ARN_PATTERN: Final = f"arn:aws:cloudtrail:*:{ACCOUNT_ID}:trail/*"
TRAIL_ARN: Final = f"arn:aws:cloudtrail:{REGION}:{ACCOUNT_ID}:trail/crm-agent-eval-trail"

EXPECTED: Final[dict[str, list[dict[str, Any]]]] = {
    LOGS_KEY: [
        ROOT_ALLOW,
        {
            "Sid": "AllowCloudWatchLogsUseOfTheKey",
            "Effect": "Allow",
            "Principal": {"Service": f"logs.{REGION}.amazonaws.com"},
            "Action": [
                "kms:Encrypt",
                "kms:Decrypt",
                "kms:ReEncrypt*",
                "kms:GenerateDataKey*",
                "kms:Describe*",
            ],
            "Resource": "*",
            "Condition": {
                "ArnLike": {
                    "kms:EncryptionContext:aws:logs:arn": (
                        f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:*"
                    )
                }
            },
        },
        _deny(KEY_USE_SID, KEY_USE_ACTIONS, "*", LOGS_KEY_USERS, exempt_services=True),
        _deny(KEY_ADMIN_SID, STORE_KEY_ADMIN_ACTIONS, "*", ADMINISTRATORS, exempt_services=False),
    ],
    CLOUDTRAIL_KEY: [
        ROOT_ALLOW,
        {
            "Sid": "AllowCloudTrailToEncryptLogs",
            "Effect": "Allow",
            "Principal": {"Service": "cloudtrail.amazonaws.com"},
            "Action": "kms:GenerateDataKey*",
            "Resource": "*",
            "Condition": {
                "StringLike": {"kms:EncryptionContext:aws:cloudtrail:arn": CLOUDTRAIL_ARN_PATTERN}
            },
        },
        {
            "Sid": "AllowCloudTrailToDescribeKey",
            "Effect": "Allow",
            "Principal": {"Service": "cloudtrail.amazonaws.com"},
            "Action": "kms:DescribeKey",
            "Resource": "*",
        },
        {
            "Sid": "AllowPrincipalsInAccountToDecryptLogFiles",
            "Effect": "Allow",
            "Principal": {"AWS": "*"},
            "Action": ["kms:Decrypt", "kms:ReEncryptFrom"],
            "Resource": "*",
            "Condition": {
                "StringEquals": {"kms:CallerAccount": ACCOUNT_ID},
                "StringLike": {"kms:EncryptionContext:aws:cloudtrail:arn": CLOUDTRAIL_ARN_PATTERN},
            },
        },
        _deny(KEY_USE_SID, KEY_USE_ACTIONS, "*", OPERATORS, exempt_services=True),
        _deny(KEY_ADMIN_SID, STORE_KEY_ADMIN_ACTIONS, "*", ADMINISTRATORS, exempt_services=False),
    ],
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
            "Sid": "AWSCloudTrailAclCheck",
            "Effect": "Allow",
            "Principal": {"Service": "cloudtrail.amazonaws.com"},
            "Action": "s3:GetBucketAcl",
            "Resource": BUCKET_ARN,
            "Condition": {"StringEquals": {"aws:SourceArn": TRAIL_ARN}},
        },
        {
            "Sid": "AWSCloudTrailWrite",
            "Effect": "Allow",
            "Principal": {"Service": "cloudtrail.amazonaws.com"},
            "Action": "s3:PutObject",
            "Resource": f"{BUCKET_ARN}/AWSLogs/{ACCOUNT_ID}/*",
            "Condition": {
                "StringEquals": {
                    "aws:SourceArn": TRAIL_ARN,
                    "s3:x-amz-acl": "bucket-owner-full-control",
                }
            },
        },
        _deny(
            BUCKET_DATA_SID,
            (*S3_OBJECT_READ_ACTIONS, *S3_OBJECT_WRITE_ACTIONS),
            [BUCKET_ARN, f"{BUCKET_ARN}/*"],
            OPERATORS,
            exempt_services=True,
            exemption_operator="BoolIfExists",
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
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return _plan(tmp_path_factory.mktemp("observability-principal-deny"))


@pytest.mark.parametrize("address", sorted(EXPECTED))
def test_document_renders_exactly_the_expected_statements(plan: Plan, address: str) -> None:
    """Every statement of the three documents, every element, as planned."""
    rendered = plan.after(address)[POLICY_ATTRIBUTE[address]]
    assert json.loads(rendered)["Version"] == "2012-10-17"
    assert document_problems(rendered, EXPECTED[address]) == []


@pytest.mark.parametrize("address", sorted(EXPECTED))
def test_every_deny_names_principal_star_literally(plan: Plan, address: str) -> None:
    """Each new Deny renders `"Principal": "*"`, not `{"AWS": "*"}`."""
    statements = json.loads(plan.after(address)["policy"])["Statement"]
    new = [s for s in statements if s["Sid"] in NEW_SIDS]
    assert len(new) == 2
    assert all(s["Principal"] == "*" for s in new)


def test_service_exemption_sits_exactly_on_the_named_statements(plan: Plan) -> None:
    """`aws:PrincipalIsAWSService` is on both key-use Denies and the bucket data-plane Deny only."""
    carrying = sorted(
        f"{address}:{s['Sid']}"
        for address in EXPECTED
        for s in json.loads(plan.after(address)["policy"])["Statement"]
        if "aws:PrincipalIsAWSService" in json.dumps(s)
    )
    assert carrying == sorted(
        [
            f"{LOGS_KEY}:{KEY_USE_SID}",
            f"{CLOUDTRAIL_KEY}:{KEY_USE_SID}",
            f"{BUCKET_POLICY}:{BUCKET_DATA_SID}",
        ]
    )


def test_ifexists_appears_only_on_the_bucket_exemption(plan: Plan) -> None:
    """`ArnNotEquals` never has an `IfExists` form; the exemption is `BoolIfExists` on the bucket
    data-plane Deny, so unsigned requests are denied too, and `Bool` on the key-use Denies."""
    operators = {}
    for address in EXPECTED:
        rendered = plan.after(address)["policy"]
        assert "aws:ViaAWSService" not in rendered
        for statement in json.loads(rendered)["Statement"]:
            for operator in statement.get("Condition", {}):
                if operator.endswith("IfExists") or "aws:PrincipalIsAWSService" in json.dumps(
                    statement["Condition"][operator]
                ):
                    operators[f"{address}:{statement['Sid']}"] = operator
    assert operators == {
        f"{LOGS_KEY}:{KEY_USE_SID}": "Bool",
        f"{CLOUDTRAIL_KEY}:{KEY_USE_SID}": "Bool",
        f"{BUCKET_POLICY}:{BUCKET_DATA_SID}": "BoolIfExists",
    }


def test_root_is_an_administrator_and_never_a_use_principal(plan: Plan) -> None:
    for address in EXPECTED:
        for statement in json.loads(plan.after(address)["policy"])["Statement"]:
            allowed = statement.get("Condition", {}).get("ArnNotEquals", {}).get("aws:PrincipalArn")
            if allowed is None:
                continue
            is_admin = statement["Sid"] in (KEY_ADMIN_SID, BUCKET_ADMIN_SID)
            assert (ROOT_ARN in allowed) is is_admin, (address, statement["Sid"])


@pytest.mark.parametrize("address", [LOGS_KEY, CLOUDTRAIL_KEY])
def test_keys_keep_rotation_window_and_the_lockout_check(plan: Plan, address: str) -> None:
    key = plan.after(address)
    assert key["bypass_policy_lockout_safety_check"] is False
    assert key["enable_key_rotation"] is True
    assert key["deletion_window_in_days"] == 30


@pytest.mark.parametrize(
    ("path", "document", "sid", "actions"),
    [
        (KMS_TF, "cloudwatch_logs_cmk", KEY_USE_SID, KEY_USE_ACTIONS),
        (KMS_TF, "cloudwatch_logs_cmk", KEY_ADMIN_SID, STORE_KEY_ADMIN_ACTIONS),
        (CLOUDTRAIL_TF, "cloudtrail_cmk", KEY_USE_SID, KEY_USE_ACTIONS),
        (CLOUDTRAIL_TF, "cloudtrail_cmk", KEY_ADMIN_SID, STORE_KEY_ADMIN_ACTIONS),
        (
            CLOUDTRAIL_TF,
            "cloudtrail_bucket",
            BUCKET_DATA_SID,
            (*S3_OBJECT_READ_ACTIONS, *S3_OBJECT_WRITE_ACTIONS),
        ),
        (CLOUDTRAIL_TF, "cloudtrail_bucket", BUCKET_ADMIN_SID, S3_BUCKET_ADMIN_ACTIONS),
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


def _mutants() -> list[tuple[str, str, Any]]:
    """(address, mutant id, mutation) for every element of every new Deny."""
    out: list[tuple[str, str, Any]] = []
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
            exemption = {"Bool", "BoolIfExists"} & set(statement["Condition"])
            if exemption:
                out.append((address, f"{sid}-swap-exemption", ("swap", sid, exemption.pop())))
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
        target["Condition"]["ArnNotEquals"]["aws:PrincipalArn"] = target["Condition"][
            "ArnNotEquals"
        ]["aws:PrincipalArn"][1:]
    elif kind == "add-exemption":
        target["Condition"].update(copy.deepcopy(NOT_SERVICE))
    elif kind == "swap":
        other = "BoolIfExists" if arg == "Bool" else "Bool"
        target["Condition"][other] = target["Condition"].pop(arg)
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


MUTANTS: Final = _mutants()


def test_the_checker_passes_an_unmutated_document() -> None:
    """Control for the mutants below."""
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
        (variable, value)
        for variable in ("logs_key_user_principal_arns", "operator_principal_arns")
        for value in (
            "[]",
            '["not-an-arn"]',
            _hcl_list([ROOT_ARN]),
            f'["arn:aws:iam::{ACCOUNT_ID}:role/*"]',
        )
    ]
    + [
        ("administrator_principal_arns", "[]"),
        ("administrator_principal_arns", '["not-an-arn"]'),
        ("administrator_principal_arns", f'["arn:aws:iam::{ACCOUNT_ID}:role/ops-?"]'),
    ],
)
def test_module_validation_rejects(tmp_path: Path, variable: str, value: str) -> None:
    """An empty list, a non-ARN, a wildcard, or `:root` in a use list fails the plan by name."""
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path, {variable: value})
    message = str(raised.value)
    assert "Invalid value for variable" in message, message
    assert variable in message, message


@pytest.mark.parametrize(
    "variable",
    [
        "logs_key_user_principal_arns",
        "operator_principal_arns",
        "administrator_principal_arns",
        "caller_principal_arn",
    ],
)
def test_module_inputs_have_no_default(variable: str) -> None:
    block = _block(
        blank_comments((MODULE_DIR / "variables.tf").read_text(encoding="utf-8")),
        f'variable "{variable}"',
    )
    assert not re.search(r"^\s*default\s*=", block, re.MULTILINE), block


def test_precondition_admits_a_listed_operator(tmp_path: Path) -> None:
    """The control for the refusals: another listed operator plans all three resources."""
    _skip_unless_terraform()
    planned = _plan(tmp_path, {"caller_principal_arn": f'"{OPERATORS[1]}"'})
    assert set(EXPECTED) <= set(planned.resource_changes)


@pytest.mark.parametrize(
    "caller",
    [f"arn:aws:iam::{ACCOUNT_ID}:role/someone-else", LAMBDA_ROLES[0], ROOT_ARN],
    ids=["unlisted", "key-user-only", "root"],
)
def test_precondition_refuses_a_caller_that_is_not_an_operator(tmp_path: Path, caller: str) -> None:
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path, {"caller_principal_arn": f'"{caller}"'})
    message = str(raised.value)
    assert message.count("Resource precondition failed") == 3, message
    assert "crm_operator_principal_arns" in message, message


@pytest.mark.parametrize(
    ("path", "header"),
    [
        (KMS_TF, 'resource "aws_kms_key" "logs"'),
        (CLOUDTRAIL_TF, 'resource "aws_kms_key" "cloudtrail"'),
        (CLOUDTRAIL_TF, 'resource "aws_s3_bucket_policy" "cloudtrail"'),
    ],
)
def test_each_policy_resource_carries_the_precondition(path: Path, header: str) -> None:
    """On the policy-bearing resources themselves, so `-target` cannot plan a policy past it."""
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


# ---------------------------------------------------------------------------
# An existing key or bucket policy is updated in place, never replaced.
# ---------------------------------------------------------------------------


def _without_new_statements(policy_json: str) -> str:
    document = json.loads(policy_json)
    document["Statement"] = [s for s in document["Statement"] if s["Sid"] not in NEW_SIDS]
    return json.dumps(document)


def _seeded_state(planned: Plan) -> dict[str, Any]:
    """A state holding the three resources as planned, but with their pre-Deny documents."""
    seeds = {
        LOGS_KEY: {
            "arn": "arn:aws:kms:us-east-1:123456789012:key/logs",
            "id": "logs",
            "key_id": "logs",
        },
        CLOUDTRAIL_KEY: {
            "arn": "arn:aws:kms:us-east-1:123456789012:key/trail",
            "id": "trail",
            "key_id": "trail",
        },
        BUCKET_POLICY: {"id": BUCKET_NAME},
    }
    resources = []
    for address, computed in seeds.items():
        attributes = {k: v for k, v in planned.after(address).items() if v is not None} | computed
        attributes["policy"] = _without_new_statements(attributes["policy"])
        if "tags" in attributes:
            attributes["tags_all"] = attributes["tags"]
        _, _, resource_type, name = address.split(".")
        resources.append(
            {
                "module": "module.subject",
                "mode": "managed",
                "type": resource_type,
                "name": name,
                "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
                "instances": [{"schema_version": 0, "attributes": attributes}],
            }
        )
    return {
        "version": 4,
        "terraform_version": "1.5.7",
        "serial": 1,
        "lineage": str(uuid.uuid4()),
        "outputs": {},
        "resources": resources,
    }


def _terraform(work_dir: Path, *args: str) -> str:
    result = proc_run.run(
        ["terraform", *args], cwd=work_dir, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"terraform {args[0]} failed:\n{result.stdout}{result.stderr}"
    return str(result.stdout)


def _seeded_plan_changes(
    plan: Plan, work_dir: Path, overrides: dict[str, dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    """`change` per address, planned with `-refresh=false` against the seeded state."""
    state = _seeded_state(plan)
    for resource in state["resources"]:
        address = f"module.subject.{resource['type']}.{resource['name']}"
        resource["instances"][0]["attributes"].update((overrides or {}).get(address, {}))
    _write_root(work_dir)
    (work_dir / "terraform.tfstate").write_text(json.dumps(state), encoding="utf-8")
    _terraform(
        work_dir, "init", "-input=false", "-backend=false", "-no-color", f"-plugin-dir={PLUGIN_DIR}"
    )
    _terraform(work_dir, "plan", "-input=false", "-no-color", "-refresh=false", "-out=plan.bin")
    shown = json.loads(_terraform(work_dir, "show", "-json", "plan.bin"))
    return {change["address"]: change["change"] for change in shown["resource_changes"]}


def test_adding_the_denies_updates_each_resource_in_place(plan: Plan, tmp_path: Path) -> None:
    """Against a state holding the pre-Deny documents, each resource is an `update` of `policy`."""
    _skip_unless_terraform()
    changes = _seeded_plan_changes(plan, tmp_path)
    assert {address: changes[address]["actions"] for address in EXPECTED} == {
        address: ["update"] for address in EXPECTED
    }
    for address in EXPECTED:
        before, after = changes[address]["before"]["policy"], changes[address]["after"]["policy"]
        assert NEW_SIDS.isdisjoint(s["Sid"] for s in json.loads(before)["Statement"])
        assert document_problems(after, EXPECTED[address]) == []


def test_the_in_place_check_sees_a_replacement(plan: Plan, tmp_path: Path) -> None:
    """Control: a force-new difference in the seeded state plans as a replacement."""
    _skip_unless_terraform()
    changes = _seeded_plan_changes(plan, tmp_path, {LOGS_KEY: {"key_usage": "SIGN_VERIFY"}})
    assert changes[LOGS_KEY]["actions"] == ["delete", "create"]
