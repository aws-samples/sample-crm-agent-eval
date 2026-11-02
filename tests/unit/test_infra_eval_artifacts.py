# SPDX-License-Identifier: MIT
"""Plan-time assertions on `infra/modules/eval_artifacts`.

Asserts Block Public Access on all four flags, SSE-KMS with the supplied CMK, a bucket policy of
DENY statements only (including a non-TLS deny), an exact writer action set with no `s3:ListBucket`,
the KMS actions an SSE-KMS put and get need, and the module's input validations. The live presigned
GET and unauthenticated-denial checks are in `scripts/smoke/`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    TerraformError,
    plan_module,
    require_terraform,
    terraform_unavailable_reason,
)

BUCKET = "module.subject.aws_s3_bucket.this"
ENCRYPTION = "module.subject.aws_s3_bucket_server_side_encryption_configuration.this"
PUBLIC_ACCESS = "module.subject.aws_s3_bucket_public_access_block.this"
VERSIONING = "module.subject.aws_s3_bucket_versioning.this"
OWNERSHIP = "module.subject.aws_s3_bucket_ownership_controls.this"
LOGGING = "module.subject.aws_s3_bucket_logging.this"
LIFECYCLE = "module.subject.aws_s3_bucket_lifecycle_configuration.this"
WRITER_POLICY = "module.subject.aws_iam_role_policy.writer[0]"

WRITER_ROLE_NAME = "crm-agent-eval-artifacts-writer-role"
KMS_KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-5555dddd6666"
LOG_BUCKET = "crm-agent-eval-frontend-cdn-logs-123456789012"

#: Exactly the actions the writer role may take.
EXPECTED_WRITER_ACTIONS = {
    "s3:PutObject",
    "s3:GetObject",
    "kms:GenerateDataKey",
    "kms:Decrypt",
}

#: Actions that must never appear in the writer grant.
FORBIDDEN_WRITER_ACTIONS = {
    "s3:ListBucket",
    "s3:ListBucketVersions",
    "s3:DeleteObject",
    "s3:*",
    "kms:*",
}


def _module_arguments(**overrides: str) -> str:
    arguments = {
        "name_prefix": '"crm-agent-eval"',
        "data_classification": '"internal"',
        "account_id": '"123456789012"',
        "kms_key_arn": f'"{KMS_KEY_ARN}"',
        "access_log_bucket_id": f'"{LOG_BUCKET}"',
        "writer_role_name": f'"{WRITER_ROLE_NAME}"',
        "caller_principal_arn": '"arn:aws:iam::123456789012:role/fixture-operator"',
        "data_plane_principal_arns": '["arn:aws:iam::123456789012:role/fixture-operator"]',
        "administrator_principal_arns": (
            '["arn:aws:iam::123456789012:role/fixture-operator", "arn:aws:iam::123456789012:root"]'
        ),
    }
    arguments.update(overrides)
    return "\n".join(f"  {key} = {value}" for key, value in arguments.items())


def _plan(work_dir: Path, **overrides: str) -> Plan:
    return plan_module(work_dir, "eval_artifacts", _module_arguments(**overrides))


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)
    return _plan(tmp_path_factory.mktemp("eval-artifacts-plan"))


def _statements(plan: Plan, address: str) -> list[dict[str, Any]]:
    """Statements of the rendered policy at `address`; fails if unknown at plan time or empty."""
    policy = plan.after(address).get("policy")
    assert isinstance(policy, str), f"{address} policy is not rendered at plan time: {policy!r}"
    parsed = json.loads(policy)
    statements = parsed.get("Statement")
    assert isinstance(statements, list) and statements, f"{address} has no statements"
    return statements


def _actions(statements: list[dict[str, Any]]) -> set[str]:
    collected: set[str] = set()
    for statement in statements:
        action = statement.get("Action", [])
        collected.update([action] if isinstance(action, str) else action)
    return collected


# ---------------------------------------------------------------------------
# Non-vacuity first: everything below reads this plan.
# ---------------------------------------------------------------------------


def test_the_plan_creates_every_resource_the_module_promises(plan: Plan) -> None:
    """Every address the assertions below address is present."""
    for address in (
        BUCKET,
        ENCRYPTION,
        PUBLIC_ACCESS,
        VERSIONING,
        OWNERSHIP,
        LOGGING,
        LIFECYCLE,
        WRITER_POLICY,
    ):
        assert plan.after(address), f"{address} planned nothing"


# ---------------------------------------------------------------------------
# What makes a presigned GET meaningful.
# ---------------------------------------------------------------------------


def test_block_public_access_is_on_for_all_four_flags(plan: Plan) -> None:
    """All four Block Public Access flags are enabled."""
    after = plan.after(PUBLIC_ACCESS)
    for flag in (
        "block_public_acls",
        "block_public_policy",
        "ignore_public_acls",
        "restrict_public_buckets",
    ):
        assert after[flag] is True, f"{flag} is not enabled"


def test_objects_are_encrypted_with_this_modules_own_cmk(plan: Plan) -> None:
    """Default encryption is SSE-KMS with the supplied CMK and bucket keys enabled."""
    rule = plan.after(ENCRYPTION)["rule"][0]
    default = rule["apply_server_side_encryption_by_default"][0]
    assert default["sse_algorithm"] == "aws:kms"
    assert default["kms_master_key_id"] == KMS_KEY_ARN, (
        "the bucket does not name the CMK it was given; a different key would leave the writer grant "
        "pointing at a key the bucket does not use, which fails only on a real PutObject"
    )
    assert rule["bucket_key_enabled"] is True


def test_the_bucket_policy_denies_non_tls_requests_and_grants_nothing(plan: Plan) -> None:
    """The bucket policy has only Deny statements, including `DenyInsecureTransport`."""
    statements = _statements(plan, "module.subject.aws_s3_bucket_policy.this")
    effects = {statement["Effect"] for statement in statements}
    assert effects == {"Deny"}, f"the bucket policy contains a non-Deny statement: {effects}"

    sids = {statement.get("Sid") for statement in statements}
    assert "DenyInsecureTransport" in sids
    tls = next(s for s in statements if s.get("Sid") == "DenyInsecureTransport")
    assert tls["Condition"]["Bool"]["aws:SecureTransport"] == "false"


def test_versioning_and_ownership_are_configured(plan: Plan) -> None:
    """Versioning is enabled and object ownership is `BucketOwnerEnforced`."""
    assert plan.after(VERSIONING)["versioning_configuration"][0]["status"] == "Enabled"
    assert plan.after(OWNERSHIP)["rule"][0]["object_ownership"] == "BucketOwnerEnforced"


def test_access_logging_targets_the_supplied_bucket(plan: Plan) -> None:
    """Logging is enabled and points at the caller's log bucket, not at itself."""
    after = plan.after(LOGGING)
    assert after["target_bucket"] == LOG_BUCKET
    assert after["target_prefix"].endswith("eval-artifacts/")


def test_the_lifecycle_rule_expires_noncurrent_versions_too(plan: Plan) -> None:
    """The lifecycle rule expires current and noncurrent versions and aborts incomplete uploads."""
    rule = plan.after(LIFECYCLE)["rule"][0]
    assert rule["status"] == "Enabled"
    assert rule["expiration"][0]["days"] > 0
    assert rule["noncurrent_version_expiration"][0]["noncurrent_days"] > 0
    assert rule["abort_incomplete_multipart_upload"][0]["days_after_initiation"] > 0


# ---------------------------------------------------------------------------
# The writer grant — what the role can do, and what it must not.
# ---------------------------------------------------------------------------


def test_the_writer_grant_is_exactly_the_four_needed_actions(plan: Plan) -> None:
    """The writer grant's action set equals `EXPECTED_WRITER_ACTIONS` exactly."""
    actions = _actions(_statements(plan, WRITER_POLICY))
    assert actions == EXPECTED_WRITER_ACTIONS, (
        f"writer grant drifted: unexpected {sorted(actions - EXPECTED_WRITER_ACTIONS)}, "
        f"missing {sorted(EXPECTED_WRITER_ACTIONS - actions)}"
    )


def test_the_writer_cannot_enumerate_other_runs_artefacts(plan: Plan) -> None:
    """No forbidden action is granted, and object resources are prefix-scoped object ARNs."""
    statements = _statements(plan, WRITER_POLICY)
    actions = _actions(statements)
    for forbidden in sorted(FORBIDDEN_WRITER_ACTIONS):
        assert forbidden not in actions, (
            f"the writer grant includes {forbidden!r} — a compromised worker could then enumerate or "
            "destroy other runs' artefacts"
        )

    object_statement = next(s for s in statements if "s3:PutObject" in s["Action"])
    resources = object_statement["Resource"]
    resources = [resources] if isinstance(resources, str) else resources
    for resource in resources:
        assert "/sweeps/*" in resource, f"object resource is not prefix-scoped: {resource}"
        assert not resource.endswith(":eval-artifacts"), (
            f"a bare bucket ARN appears in the object statement: {resource}"
        )


def test_no_writer_grant_exists_when_no_role_is_supplied(tmp_path: Path) -> None:
    """With `writer_role_name = null` no role policy is planned, while the bucket still is."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)
    unattached = _plan(tmp_path / "no-writer", writer_role_name="null")
    assert unattached.addresses_of_type("aws_iam_role_policy") == []
    assert unattached.after(BUCKET)


# ---------------------------------------------------------------------------
# Inputs the module must refuse.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"artifact_prefix": '"/sweeps"'}, "must be lowercase alphanumeric"),
        ({"artifact_prefix": '"sweeps/"'}, "must be lowercase alphanumeric"),
        ({"artifact_retention_days": "0"}, "must be between 1 and 90"),
        ({"artifact_retention_days": "365"}, "must be between 1 and 90"),
        ({"account_id": '"12345"'}, "must be the 12-digit AWS account id"),
        ({"kms_key_arn": '"alias/eval-artifacts"'}, "must be a KMS key ARN"),
    ],
)
def test_the_module_refuses_a_configuration_that_would_deploy_and_misbehave(
    tmp_path: Path, overrides: dict[str, str], expected: str
) -> None:
    """Each invalid prefix, retention, account id, or key ARN fails variable validation."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path / "refused", **overrides)
    assert expected in str(raised.value)


# ---------------------------------------------------------------------------
# The contract with the rest of the deployment.
# ---------------------------------------------------------------------------

ROOT_TF = Path(__file__).resolve().parents[2] / "infra" / "main.tf"
MODULE_DIR = Path(__file__).resolve().parents[2] / "infra" / "modules" / "eval_artifacts"


def test_the_root_wires_the_module_and_leaves_the_writer_to_sweep_control() -> None:
    """The root wires the module with no writer role, the CDN log bucket, account id and CMK; the
    writer is `sweep_control`, whose own policy grants the artefact prefix and key."""
    root = ROOT_TF.read_text(encoding="utf-8")
    assert 'module "eval_artifacts"' in root
    code = "\n".join(line.split("#", 1)[0] for line in root.splitlines())
    block = code[code.index('module "eval_artifacts"') :]
    block = block[: block.index("\n}")]
    assert re.search(r"^\s*writer_role_name\s*=\s*null\s*$", block, re.M), block
    sfn = (ROOT_TF.parent / "eval_sfn.tf").read_text(encoding="utf-8")
    grant = sfn[sfn.index('data "aws_iam_policy_document" "sweep_control_artifacts"') :]
    grant = grant[: grant.index("\n}\n")]
    assert '"s3:PutObject"' in grant and "module.eval_artifacts.artifact_prefix" in grant
    assert "aws_kms_key.eval_artifacts.arn" in grant
    assert "module.frontend_cdn.logs_bucket_name" in block
    assert "data.aws_caller_identity.current.account_id" in block
    assert "aws_kms_key.eval_artifacts.arn" in block
    assert "aws_kms_key" in code and 'resource "aws_kms_key" "eval_artifacts"' in root


def test_this_bucket_is_not_a_cloudfront_origin() -> None:
    """No code in `frontend_cdn`'s `.tf` files references this module."""
    cdn = Path(__file__).resolve().parents[2] / "infra" / "modules" / "frontend_cdn"
    checked = 0
    for source in sorted(cdn.glob("*.tf")):
        # Comments are stripped; only code references count.
        code = "\n".join(
            line.split("#", 1)[0] for line in source.read_text(encoding="utf-8").splitlines()
        )
        checked += 1
        assert "eval_artifacts" not in code, (
            f"{source.name} references eval_artifacts in CODE; this bucket must never be a CloudFront "
            "origin — BPA and the bucket policy do not restrict reads THROUGH CloudFront, so that "
            "mistake would pass every other assertion in this file"
        )
    assert checked, f"no .tf files found under {cdn}; this test verified nothing"


def test_the_module_exports_the_names_a_consumer_must_not_reconstruct() -> None:
    """The module exports `bucket_name`, `bucket_arn` and `artifact_prefix`."""
    outputs = (MODULE_DIR / "outputs.tf").read_text(encoding="utf-8")
    assert "aws_s3_bucket.this.id" in outputs
    assert "aws_s3_bucket.this.arn" in outputs
    for name in ("bucket_name", "bucket_arn", "artifact_prefix"):
        assert f'output "{name}"' in outputs, f"output {name!r} is missing"


def test_a_whole_bucket_rule_covers_keys_outside_the_artifact_prefix(plan: Plan) -> None:
    """A second rule, unfiltered, expires noncurrent versions and aborts uploads bucket-wide, and
    never expires a current object."""
    rules = plan.after(LIFECYCLE)["rule"]
    whole_bucket = [r for r in rules if all(not f.get("prefix") for f in r["filter"])]
    assert len(whole_bucket) == 1, rules
    rule = whole_bucket[0]
    assert rule["status"] == "Enabled"
    assert rule["expiration"] == []
    assert rule["noncurrent_version_expiration"][0]["noncurrent_days"] > 0
    assert rule["abort_incomplete_multipart_upload"][0]["days_after_initiation"] > 0
