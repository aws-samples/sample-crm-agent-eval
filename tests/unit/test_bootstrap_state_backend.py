# SPDX-License-Identifier: MIT
"""Behaviour of ``infra/bootstrap_state_backend.sh`` against a stub ``aws`` on ``PATH``.

A copy of the script runs under ``tmp_path`` with a stub ``aws`` that records each argv as a JSON
line and answers the few reads the script parses. Covers the env-credential refusal, the mandatory
expected-account assertion, the operator list and the caller-is-an-operator precondition, the log
bucket's SSE-S3 / ``BucketOwnerEnforced`` / scoped log-delivery grant, the state bucket's SSE-KMS
baseline, the principal Deny documents on the key, lock table and both buckets, and convergence of
an existing backend.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest
from scripts import proc_run
from scripts.smoke import dynamodb_principal_deny as smoke

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
BOOTSTRAP: Final = REPO_ROOT / "infra" / "bootstrap_state_backend.sh"
ACCOUNT: Final = "123456789012"
OTHER_ACCOUNT: Final = "111122223333"
REGION: Final = "us-east-1"
PROFILE: Final = "crm-test"
KEY_ID: Final = "key-stub-0001"
STATE_BUCKET: Final = f"crm-agent-eval-tfstate-{ACCOUNT}-{REGION}"
LOG_BUCKET: Final = f"crm-agent-eval-tfstate-logs-{ACCOUNT}-{REGION}"
LOG_PREFIX: Final = "state-access/"
ENV_CRED_PHRASE: Final = "Static credentials take precedence over AWS_PROFILE"
LOCK_TABLE: Final = "crm-agent-eval-tflock"
LOCK_TABLE_ARN: Final = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{LOCK_TABLE}"
ROOT_ARN: Final = f"arn:aws:iam::{ACCOUNT}:root"
OPERATOR_ROLE_NAME: Final = "AWSReservedSSO_Operator_0123abcd"
OPERATOR_ROLE: Final = (
    f"arn:aws:iam::{ACCOUNT}:role/aws-reserved/sso.amazonaws.com/{OPERATOR_ROLE_NAME}"
)
OPERATOR_USER: Final = f"arn:aws:iam::{ACCOUNT}:" + "user/ci/deployer"
OPERATORS: Final = (OPERATOR_USER, OPERATOR_ROLE)
CALLER_ROLE_SESSION: Final = f"arn:aws:sts::{ACCOUNT}:assumed-role/{OPERATOR_ROLE_NAME}/operator"
#: The operator and administrator lists as the rendered documents carry them.
RENDERED_OPERATORS: Final = sorted(OPERATORS)
RENDERED_ADMINISTRATORS: Final = [*RENDERED_OPERATORS, ROOT_ARN]

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
#: The DynamoDB CMK's key-admin tuple plus the alias, tag and description actions.
STORE_KEY_ADMIN_ACTIONS: Final = (
    *smoke.KEY_ADMIN_ACTIONS,
    "kms:CreateAlias",
    "kms:UpdateAlias",
    "kms:DeleteAlias",
    "kms:TagResource",
    "kms:UntagResource",
    "kms:UpdateKeyDescription",
)
S3_DATA_PLANE_SID: Final = "DenyObjectDataPlaneToNonProjectPrincipals"
S3_ADMIN_SID: Final = "DenyBucketAdministrationToNonOperators"

#: The calls that change nothing; every other recorded call is a mutating one.
READ_ONLY_CALLS: Final = frozenset(
    {
        ("sts", "get-caller-identity"),
        ("iam", "get-role"),
        ("kms", "describe-key"),
        ("s3api", "head-bucket"),
        ("dynamodb", "describe-table"),
        ("dynamodb", "wait"),
    }
)

STUB_AWS: Final = f"""#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["STUB_AWS_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
existing = os.environ.get("STUB_EXISTING") == "1"
cmd = tuple(argv[:2])
if cmd == ("sts", "get-caller-identity"):
    print(json.dumps({{"Account": os.environ["STUB_ACCOUNT"], "Arn": os.environ["STUB_CALLER_ARN"]}}))
elif cmd == ("iam", "get-role"):
    role_arn = os.environ["STUB_ROLE_ARN"]
    if argv[argv.index("--role-name") + 1] != role_arn.rsplit("/", 1)[1]:
        sys.stderr.write("NoSuchEntity\\n")
        sys.exit(254)
    if os.environ.get("STUB_ROLE_WITHOUT_ARN") == "1":
        print(json.dumps({{"Role": {{}}}}))
    else:
        print(json.dumps({{"Role": {{"Arn": role_arn}}}}))
elif cmd == ("kms", "describe-key"):
    if not existing:
        sys.stderr.write("NotFoundException\\n")
        sys.exit(254)
    print(json.dumps({{"KeyMetadata": {{"KeyId": "{KEY_ID}"}}}}))
elif cmd == ("kms", "create-key"):
    print(json.dumps({{"KeyMetadata": {{"KeyId": "{KEY_ID}"}}}}))
elif cmd in (("s3api", "head-bucket"), ("dynamodb", "describe-table")):
    sys.exit(0 if existing else 254)
else:
    print("{{}}")
"""


Call = list[str]


def _run(
    tmp_path: Path,
    *args: str,
    env_extra: Mapping[str, str] | None = None,
    existing: bool = False,
    sts_account: str = ACCOUNT,
    script_text: str | None = None,
    operators_env: str | None = ",".join(OPERATORS),
    caller_arn: str = CALLER_ROLE_SESSION,
    role_arn: str = OPERATOR_ROLE,
    region: str = REGION,
) -> tuple[subprocess.CompletedProcess[str], list[Call], Path]:
    """Run a copy of the bootstrap with the stub ``aws``; return (result, aws calls, repo root).

    ``operators_env`` is ``CRM_OPERATOR_PRINCIPAL_ARNS`` (``None`` leaves it unset); ``caller_arn``
    is what ``sts get-caller-identity`` answers and ``role_arn`` what ``iam get-role`` answers;
    ``region`` is passed as ``--region``.
    """
    repo = tmp_path / "repo"
    (repo / "infra").mkdir(parents=True)
    (repo / ".gitignore").write_text("infra/backend.hcl\n", encoding="utf-8")
    script = repo / "infra" / "bootstrap_state_backend.sh"
    script.write_text(
        script_text if script_text is not None else BOOTSTRAP.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "aws"
    stub.write_text(STUB_AWS, encoding="utf-8")
    stub.chmod(0o755)
    jq = shutil.which("jq")
    assert jq is not None, "jq is required by the bootstrap script"
    log = tmp_path / "aws-calls.jsonl"
    log.touch()
    env = {
        "PATH": f"{bin_dir}:{Path(jq).parent}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LOG_FILE": str(tmp_path / "bootstrap.log"),
        "STUB_AWS_LOG": str(log),
        "STUB_ACCOUNT": sts_account,
        "STUB_EXISTING": "1" if existing else "0",
        "STUB_CALLER_ARN": caller_arn,
        "STUB_ROLE_ARN": role_arn,
        **({"CRM_OPERATOR_PRINCIPAL_ARNS": operators_env} if operators_env is not None else {}),
        **(env_extra or {}),
    }
    result = proc_run.run(
        ["bash", str(script), "--profile", PROFILE, "--region", region, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return result, calls, repo


def _ok(tmp_path: Path, **kwargs: Any) -> tuple[list[Call], Path]:
    result, calls, repo = _run(tmp_path, env_extra={"CRM_EXPECTED_ACCOUNT_ID": ACCOUNT}, **kwargs)
    assert result.returncode == 0, result.stderr
    return calls, repo


def _value(call: Call, flag: str) -> str:
    return call[call.index(flag) + 1]


def _calls_for(calls: list[Call], service: str, op: str, bucket: str | None = None) -> list[Call]:
    return [
        c
        for c in calls
        if c[:2] == [service, op] and (bucket is None or _value(c, "--bucket") == bucket)
    ]


def _one(calls: list[Call], service: str, op: str, bucket: str | None = None) -> Call:
    matches = _calls_for(calls, service, op, bucket)
    assert len(matches) == 1, f"expected one {service} {op} {bucket or ''}, got {len(matches)}"
    return matches[0]


def _sse_algorithm(calls: list[Call], bucket: str) -> dict[str, Any]:
    config = json.loads(
        _value(
            _one(calls, "s3api", "put-bucket-encryption", bucket),
            "--server-side-encryption-configuration",
        )
    )
    default: dict[str, Any] = config["Rules"][0]["ApplyServerSideEncryptionByDefault"]
    return default


def _policy(calls: list[Call], bucket: str) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = json.loads(
        _value(_one(calls, "s3api", "put-bucket-policy", bucket), "--policy")
    )["Statement"]
    return statements


def _assert_log_bucket_grant(calls: list[Call]) -> None:
    """The log bucket policy carries the TLS deny and the scoped log-delivery Allow."""
    statements = _policy(calls, LOG_BUCKET)
    deny = next(s for s in statements if s["Sid"] == "DenyInsecureTransport")
    assert deny["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert set(deny["Resource"]) == {f"arn:aws:s3:::{LOG_BUCKET}", f"arn:aws:s3:::{LOG_BUCKET}/*"}
    (allow,) = [s for s in statements if s["Effect"] == "Allow"]
    assert allow["Principal"] == {"Service": "logging.s3.amazonaws.com"}
    assert allow["Action"] == "s3:PutObject"
    assert allow["Resource"] == f"arn:aws:s3:::{LOG_BUCKET}/{LOG_PREFIX}*"
    assert allow["Condition"] == {
        "ArnLike": {"aws:SourceArn": f"arn:aws:s3:::{STATE_BUCKET}"},
        "StringEquals": {"aws:SourceAccount": ACCOUNT},
    }


def _assert_grant_prefix_equals_target_prefix(calls: list[Call]) -> None:
    """The grant's object prefix is the state bucket's ``TargetPrefix``, read from both calls."""
    allow = next(s for s in _policy(calls, LOG_BUCKET) if s["Effect"] == "Allow")
    status = json.loads(
        _value(_one(calls, "s3api", "put-bucket-logging", STATE_BUCKET), "--bucket-logging-status")
    )
    target = status["LoggingEnabled"]
    assert target["TargetBucket"] == LOG_BUCKET
    assert allow["Resource"] == f"arn:aws:s3:::{target['TargetBucket']}/{target['TargetPrefix']}*"


class TestEnvironmentCredentialRefusal:
    @pytest.mark.parametrize("name", ["AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN"])
    def test_env_static_credentials_refuse_before_any_aws_call(
        self, tmp_path: Path, name: str
    ) -> None:
        result, calls, _repo = _run(
            tmp_path, env_extra={"CRM_EXPECTED_ACCOUNT_ID": ACCOUNT, name: "stub-value"}
        )
        assert result.returncode != 0
        assert ENV_CRED_PHRASE in result.stderr
        assert calls == []

    def test_the_refusal_wording_matches_deploy_sh(self) -> None:
        deploy = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        assert ENV_CRED_PHRASE in deploy
        assert ENV_CRED_PHRASE in BOOTSTRAP.read_text(encoding="utf-8")


class TestExpectedAccountAssertion:
    def test_unset_expected_account_refuses_before_any_aws_call(self, tmp_path: Path) -> None:
        result, calls, _repo = _run(tmp_path)
        assert result.returncode != 0
        assert "CRM_EXPECTED_ACCOUNT_ID" in result.stderr
        assert calls == []

    def test_malformed_expected_account_refuses(self, tmp_path: Path) -> None:
        result, calls, _repo = _run(tmp_path, env_extra={"CRM_EXPECTED_ACCOUNT_ID": "12345"})
        assert result.returncode != 0
        assert calls == []

    def test_mismatched_env_account_refuses_after_sts_and_before_any_create(
        self, tmp_path: Path
    ) -> None:
        result, calls, repo = _run(
            tmp_path, env_extra={"CRM_EXPECTED_ACCOUNT_ID": ACCOUNT}, sts_account=OTHER_ACCOUNT
        )
        assert result.returncode != 0
        assert "WRONG ACCOUNT" in result.stderr
        assert [c[:2] for c in calls] == [["sts", "get-caller-identity"]]
        assert not (repo / "infra" / "backend.hcl").exists()

    def test_mismatched_flag_account_refuses(self, tmp_path: Path) -> None:
        result, calls, _repo = _run(
            tmp_path,
            "--expected-account-id",
            OTHER_ACCOUNT,
            operators_env=f"arn:aws:iam::{OTHER_ACCOUNT}:role/ops",
        )
        assert result.returncode != 0
        assert "WRONG ACCOUNT" in result.stderr
        assert [c[:2] for c in calls] == [["sts", "get-caller-identity"]]

    def test_flag_and_env_disagreeing_refuses_before_any_aws_call(self, tmp_path: Path) -> None:
        result, calls, _repo = _run(
            tmp_path,
            "--expected-account-id",
            OTHER_ACCOUNT,
            env_extra={"CRM_EXPECTED_ACCOUNT_ID": ACCOUNT},
        )
        assert result.returncode != 0
        assert calls == []

    def test_matching_flag_alone_is_accepted(self, tmp_path: Path) -> None:
        result, calls, _repo = _run(tmp_path, "--expected-account-id", ACCOUNT)
        assert result.returncode == 0, result.stderr
        assert _calls_for(calls, "s3api", "create-bucket")


class TestFreshBootstrap:
    def test_every_aws_call_pins_profile_region_and_no_pager(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path)
        assert calls
        for call in calls:
            assert _value(call, "--profile") == PROFILE, call
            assert _value(call, "--region") == REGION, call
            assert "--no-cli-pager" in call, call

    def test_log_bucket_is_sse_s3(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path)
        assert _sse_algorithm(calls, LOG_BUCKET) == {"SSEAlgorithm": "AES256"}

    def test_state_bucket_stays_sse_kms(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path)
        assert _sse_algorithm(calls, STATE_BUCKET) == {
            "SSEAlgorithm": "aws:kms",
            "KMSMasterKeyID": KEY_ID,
        }

    def test_log_bucket_is_bucket_owner_enforced(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path)
        call = _one(calls, "s3api", "put-bucket-ownership-controls", LOG_BUCKET)
        assert (
            _value(call, "--ownership-controls") == "Rules=[{ObjectOwnership=BucketOwnerEnforced}]"
        )
        assert not _calls_for(calls, "s3api", "put-bucket-ownership-controls", STATE_BUCKET)

    def test_log_bucket_policy_is_tls_deny_plus_scoped_delivery_grant(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path)
        _assert_log_bucket_grant(calls)

    def test_state_bucket_policy_keeps_the_tls_deny_first(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path)
        statements = _policy(calls, STATE_BUCKET)
        assert statements[0]["Sid"] == "DenyInsecureTransport"
        assert statements[0]["Effect"] == "Deny"
        assert statements[0]["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}

    def test_grant_prefix_equals_the_state_bucket_target_prefix(self, tmp_path: Path) -> None:
        """The grant and the logging config agree, compared with each other, not a constant."""
        calls, _repo = _ok(tmp_path)
        _assert_grant_prefix_equals_target_prefix(calls)

    def test_state_bucket_logs_under_the_granted_prefix(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path)
        status = json.loads(
            _value(
                _one(calls, "s3api", "put-bucket-logging", STATE_BUCKET), "--bucket-logging-status"
            )
        )
        assert status == {
            "LoggingEnabled": {"TargetBucket": LOG_BUCKET, "TargetPrefix": LOG_PREFIX}
        }
        assert not _calls_for(calls, "s3api", "put-bucket-logging", LOG_BUCKET)

    def test_both_buckets_keep_bpa_versioning_and_tags(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path)
        for bucket in (LOG_BUCKET, STATE_BUCKET):
            bpa = _value(
                _one(calls, "s3api", "put-public-access-block", bucket),
                "--public-access-block-configuration",
            )
            assert bpa == (
                "BlockPublicAcls=true,IgnorePublicAcls=true,"
                "BlockPublicPolicy=true,RestrictPublicBuckets=true"
            )
            versioning = _one(calls, "s3api", "put-bucket-versioning", bucket)
            assert _value(versioning, "--versioning-configuration") == "Status=Enabled"
            tags = json.loads(
                _value(_one(calls, "s3api", "put-bucket-tagging", bucket), "--tagging")
            )
            keys = {t["Key"]: t["Value"] for t in tags["TagSet"]}
            assert keys["data-classification"] == "internal"
            assert keys["AppName"] == "crm-agent-eval"
            assert {"service", "environment", "owner", "cost-center"} <= keys.keys()

    def test_lock_table_is_sse_kms_with_pitr(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path)
        create = _one(calls, "dynamodb", "create-table")
        assert _value(create, "--sse-specification") == (
            f"Enabled=true,SSEType=KMS,KMSMasterKeyId={KEY_ID}"
        )
        pitr = _one(calls, "dynamodb", "update-continuous-backups")
        assert _value(pitr, "--point-in-time-recovery-specification") == (
            "PointInTimeRecoveryEnabled=true"
        )

    def test_backend_hcl_is_written(self, tmp_path: Path) -> None:
        _calls, repo = _ok(tmp_path)
        text = (repo / "infra" / "backend.hcl").read_text(encoding="utf-8")
        assert f'bucket         = "{STATE_BUCKET}"' in text
        assert f'kms_key_id     = "{KEY_ID}"' in text


OTHER_REGION: Final = "us-west-2"


def _assert_region_threaded(calls: list[Call], repo: Path, region: str) -> None:
    """`region` reaches backend.hcl, both bucket names, every create-bucket and every aws call."""
    state_bucket = f"crm-agent-eval-tfstate-{ACCOUNT}-{region}"
    log_bucket = f"crm-agent-eval-tfstate-logs-{ACCOUNT}-{region}"
    lines = (repo / "infra" / "backend.hcl").read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line.startswith("region ")] == [
        f'region         = "{region}"'
    ]
    assert f'bucket         = "{state_bucket}"' in lines
    created = _calls_for(calls, "s3api", "create-bucket")
    assert sorted(_value(c, "--bucket") for c in created) == sorted([state_bucket, log_bucket])
    for call in created:
        assert "--create-bucket-configuration" in call, call
        assert _value(call, "--create-bucket-configuration") == f"LocationConstraint={region}"
    for call in calls:
        assert "--region" in call and _value(call, "--region") == region, call


class TestNonDefaultRegion:
    """A map region other than us-east-1 is threaded through the backend; it is not edited by R3."""

    def test_us_west_2_reaches_backend_hcl_buckets_and_location_constraint(
        self, tmp_path: Path
    ) -> None:
        calls, repo = _ok(tmp_path / "run", region=OTHER_REGION)
        _assert_region_threaded(calls, repo, OTHER_REGION)

    def test_a_region_dropped_from_backend_hcl_fails_the_assertion(self, tmp_path: Path) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        original = 'region         = "${AWS_REGION_ARG}"\n'
        assert text.count(original) == 1, f"control anchor {original!r} not found once"
        calls, repo = _ok(
            tmp_path / "run", region=OTHER_REGION, script_text=text.replace(original, "")
        )
        with pytest.raises(AssertionError):
            _assert_region_threaded(calls, repo, OTHER_REGION)

    def test_a_dropped_location_constraint_fails_the_assertion(self, tmp_path: Path) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        original = '--create-bucket-configuration "LocationConstraint=${AWS_REGION_ARG}" \\\n'
        assert text.count(original) == 1, f"control anchor {original!r} not found once"
        calls, repo = _ok(
            tmp_path / "run", region=OTHER_REGION, script_text=text.replace(original, "")
        )
        with pytest.raises(AssertionError):
            _assert_region_threaded(calls, repo, OTHER_REGION)


class TestExistingBackendConverges:
    def test_rerun_creates_nothing_and_reapplies_the_log_bucket_settings(
        self, tmp_path: Path
    ) -> None:
        calls, _repo = _ok(tmp_path, existing=True)
        for op in (("s3api", "create-bucket"), ("kms", "create-key"), ("dynamodb", "create-table")):
            assert not _calls_for(calls, *op), op
        assert _sse_algorithm(calls, LOG_BUCKET) == {"SSEAlgorithm": "AES256"}
        _one(calls, "s3api", "put-bucket-ownership-controls", LOG_BUCKET)
        _assert_log_bucket_grant(calls)
        assert _sse_algorithm(calls, STATE_BUCKET)["SSEAlgorithm"] == "aws:kms"


def _assert_key_and_table_tagged(calls: list[Call]) -> None:
    """The key and the lock table are tagged with the full set, AppName included."""
    key_tags = json.loads(_value(_one(calls, "kms", "tag-resource"), "--tags"))
    assert _value(_one(calls, "kms", "tag-resource"), "--key-id") == KEY_ID
    table_call = _one(calls, "dynamodb", "tag-resource")
    assert _value(table_call, "--resource-arn") == LOCK_TABLE_ARN
    table_tags = json.loads(_value(table_call, "--tags"))
    for rendered in (
        {t["TagKey"]: t["TagValue"] for t in key_tags},
        {t["Key"]: t["Value"] for t in table_tags},
    ):
        assert rendered.get("AppName") == "crm-agent-eval", rendered
        assert {"service", "environment", "owner", "cost-center", "data-classification"} <= set(
            rendered
        )


class TestStoreTagsConverge:
    @pytest.mark.parametrize("existing", [False, True], ids=["fresh", "existing"])
    def test_the_key_and_lock_table_are_tagged_on_every_run(
        self, tmp_path: Path, existing: bool
    ) -> None:
        calls, _repo = _ok(tmp_path, existing=existing)
        _assert_key_and_table_tagged(calls)

    def test_dropping_app_name_reds_the_assertion(self, tmp_path: Path) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        original = ',{Key:"AppName",Value:$appname}]'
        assert text.count(original) == 1
        calls, _repo = _ok(tmp_path, existing=True, script_text=text.replace(original, "]"))
        with pytest.raises(AssertionError):
            _assert_key_and_table_tagged(calls)

    def test_dropping_the_converge_step_reds_the_assertion(self, tmp_path: Path) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        original = "    converge_key_and_table_tags\n"
        assert text.count(original) == 1
        calls, _repo = _ok(tmp_path, existing=True, script_text=text.replace(original, ""))
        with pytest.raises(AssertionError):
            _assert_key_and_table_tagged(calls)


class TestTheAssertionsDiscriminate:
    """Each mutant copy of the script reds the assertion that guards it; the unmutated copy passes."""

    def test_an_unmutated_copy_passes(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path, script_text=BOOTSTRAP.read_text(encoding="utf-8"))
        _assert_log_bucket_grant(calls)

    @pytest.mark.parametrize(
        ("original", "mutated"),
        [
            ('StringEquals: {"aws:SourceAccount": $account}', "StringEquals: {}"),
            ('--arg source "arn:aws:s3:::${STATE_BUCKET}"', '--arg source "arn:aws:s3:::*"'),
            ('STATE_LOG_PREFIX="state-access/"', 'STATE_LOG_PREFIX=""'),
        ],
        ids=["no-source-account", "wildcard-source-arn", "unscoped-prefix"],
    )
    def test_a_loosened_grant_fails(self, tmp_path: Path, original: str, mutated: str) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        assert text.count(original) == 1, f"control anchor {original!r} not found once"
        calls, _repo = _ok(tmp_path, script_text=text.replace(original, mutated))
        with pytest.raises(AssertionError):
            _assert_log_bucket_grant(calls)

    def test_a_target_prefix_diverging_from_the_grant_fails(self, tmp_path: Path) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        original = """--arg prefix "$STATE_LOG_PREFIX" '{"""
        assert text.count(original) == 1, f"control anchor {original!r} not found once"
        mutated = """--arg prefix "${STATE_BUCKET}/" '{"""
        calls, _repo = _ok(tmp_path, script_text=text.replace(original, mutated))
        _assert_log_bucket_grant(calls)
        with pytest.raises(AssertionError):
            _assert_grant_prefix_equals_target_prefix(calls)

    def test_a_kms_log_bucket_fails(self, tmp_path: Path) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        original = 'apply_bucket_baseline "$LOG_BUCKET" "$(sse_s3_encryption_config)"'
        assert text.count(original) == 1
        mutated = 'apply_bucket_baseline "$LOG_BUCKET" "$(kms_encryption_config)"'
        calls, _repo = _ok(tmp_path, script_text=text.replace(original, mutated))
        assert _sse_algorithm(calls, LOG_BUCKET) != {"SSEAlgorithm": "AES256"}

    def test_a_removed_account_check_fails(self, tmp_path: Path) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        original = '[[ "$ACCOUNT_ID" == "$EXPECTED_ACCOUNT_ID" ]]'
        assert text.count(original) == 1
        result, calls, _repo = _run(
            tmp_path,
            env_extra={"CRM_EXPECTED_ACCOUNT_ID": ACCOUNT},
            sts_account=OTHER_ACCOUNT,
            script_text=text.replace(original, "true"),
        )
        assert result.returncode == 0
        assert _calls_for(calls, "s3api", "create-bucket")


# --- principal Deny: operator list, caller precondition, rendered documents -----------------------


def _deny(
    sid: str,
    actions: tuple[str, ...],
    resource: str | list[str],
    principals: list[str],
    *,
    exemption: str | None = None,
) -> dict[str, Any]:
    """``exemption`` is the condition operator (``Bool``/``BoolIfExists``) exempting services."""
    condition: dict[str, Any] = {"ArnNotEquals": {"aws:PrincipalArn": principals}}
    if exemption is not None:
        condition[exemption] = {"aws:PrincipalIsAWSService": "false"}
    return {
        "Sid": sid,
        "Effect": "Deny",
        "Principal": "*",
        "Action": list(actions),
        "Resource": resource,
        "Condition": condition,
    }


def _s3_data_plane(bucket: str, *, exemption: str | None) -> dict[str, Any]:
    return _deny(
        S3_DATA_PLANE_SID,
        (*S3_OBJECT_READ_ACTIONS, *S3_OBJECT_WRITE_ACTIONS),
        [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
        RENDERED_OPERATORS,
        exemption=exemption,
    )


def _s3_admin(bucket: str) -> dict[str, Any]:
    return _deny(
        S3_ADMIN_SID, S3_BUCKET_ADMIN_ACTIONS, f"arn:aws:s3:::{bucket}", RENDERED_ADMINISTRATORS
    )


EXPECTED_KEY_STATEMENTS: Final = [
    {
        "Sid": "EnableRootAccountPermissions",
        "Effect": "Allow",
        "Principal": {"AWS": ROOT_ARN},
        "Action": "kms:*",
        "Resource": "*",
    },
    _deny(
        smoke.KEY_USE_SID,
        smoke.KEY_USE_ACTIONS,
        "*",
        RENDERED_OPERATORS,
        exemption="Bool",
    ),
    _deny(smoke.KEY_ADMIN_SID, STORE_KEY_ADMIN_ACTIONS, "*", RENDERED_ADMINISTRATORS),
]
EXPECTED_LOCK_TABLE_STATEMENTS: Final = [
    _deny(
        smoke.TABLE_DATA_PLANE_SID,
        smoke.TABLE_DATA_PLANE_ACTIONS,
        [LOCK_TABLE_ARN, f"{LOCK_TABLE_ARN}/index/*"],
        RENDERED_OPERATORS,
    ),
    _deny(
        smoke.TABLE_ADMIN_SID,
        smoke.TABLE_ADMIN_ACTIONS,
        [LOCK_TABLE_ARN, f"{LOCK_TABLE_ARN}/index/*"],
        RENDERED_ADMINISTRATORS,
    ),
]
EXPECTED_LOG_BUCKET_DENIES: Final = [
    _s3_data_plane(LOG_BUCKET, exemption="BoolIfExists"),
    _s3_admin(LOG_BUCKET),
]
EXPECTED_STATE_BUCKET_DENIES: Final = [
    _s3_data_plane(STATE_BUCKET, exemption=None),
    _s3_admin(STATE_BUCKET),
]


def _document(call: Call) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(_value(call, "--policy"))
    assert document["Version"] == "2012-10-17"
    return document


def _key_statements(calls: list[Call]) -> list[dict[str, Any]]:
    call = _one(calls, "kms", "put-key-policy")
    assert _value(call, "--key-id") == KEY_ID
    assert _value(call, "--policy-name") == "default"
    statements: list[dict[str, Any]] = _document(call)["Statement"]
    return statements


def _lock_table_statements(calls: list[Call]) -> list[dict[str, Any]]:
    call = _one(calls, "dynamodb", "put-resource-policy")
    assert _value(call, "--resource-arn") == LOCK_TABLE_ARN
    statements: list[dict[str, Any]] = _document(call)["Statement"]
    return statements


def _assert_documents(calls: list[Call]) -> None:
    """Every rendered document equals the contract: sids, ordered tuples, ``*``, exact conditions."""
    assert _key_statements(calls) == EXPECTED_KEY_STATEMENTS
    assert _lock_table_statements(calls) == EXPECTED_LOCK_TABLE_STATEMENTS
    log = _policy(calls, LOG_BUCKET)
    assert [s["Sid"] for s in log[:2]] == [
        "DenyInsecureTransport",
        "AllowStateBucketAccessLogDelivery",
    ]
    assert log[2:] == EXPECTED_LOG_BUCKET_DENIES
    state = _policy(calls, STATE_BUCKET)
    assert [s["Sid"] for s in state[:1]] == ["DenyInsecureTransport"]
    assert state[1:] == EXPECTED_STATE_BUCKET_DENIES


def _mutating(calls: list[Call]) -> list[Call]:
    return [c for c in calls if (c[0], c[1]) not in READ_ONLY_CALLS]


def _assert_refused_naming_no_value(
    result: subprocess.CompletedProcess[str], calls: list[Call], tmp_path: Path, *values: str
) -> None:
    assert result.returncode != 0, result.stdout
    assert _mutating(calls) == [], _mutating(calls)
    assert "Nothing was created" in result.stderr, result.stderr
    combined = (
        result.stdout + result.stderr + (tmp_path / "bootstrap.log").read_text(encoding="utf-8")
    )
    for value in (*values, *OPERATORS, CALLER_ROLE_SESSION, OPERATOR_ROLE_NAME, ACCOUNT):
        assert value not in combined, f"the refusal names a value: {value!r}"


def _with_expected_account(**env: str) -> dict[str, str]:
    return {"CRM_EXPECTED_ACCOUNT_ID": ACCOUNT, **env}


_EMPTY: Final = "is required and must not be empty"
_SHAPE: Final = "is not an IAM role or user ARN"
_ACCOUNT: Final = "an account other than the expected account"
_ROOT: Final = "account root is not accepted"


class TestOperatorListRefusals:
    @pytest.mark.parametrize(
        ("operators_env", "offending", "reason"),
        [
            (None, "", _EMPTY),
            ("", "", _EMPTY),
            (",", "", _EMPTY),
            ("not-an-arn", "not-an-arn", _SHAPE),
            (f"{OPERATOR_ROLE},", "", _SHAPE),
            (f"{OPERATOR_ROLE},,{OPERATOR_USER}", "", _SHAPE),
            (f"arn:aws:iam::{ACCOUNT}:role/*", "role/*", _SHAPE),
            (f"arn:aws:iam::{ACCOUNT}:role/ops-?", "role/ops-?", _SHAPE),
            (f"arn:aws:iam::{OTHER_ACCOUNT}:role/ops", OTHER_ACCOUNT, _ACCOUNT),
            (ROOT_ARN, ROOT_ARN, _ROOT),
            (f"{OPERATOR_ROLE},{ROOT_ARN}", ROOT_ARN, _ROOT),
        ],
        ids=[
            "unset",
            "empty",
            "only-separator",
            "malformed",
            "trailing-empty-element",
            "empty-element",
            "star",
            "question-mark",
            "other-account",
            "root",
            "root-among-operators",
        ],
    )
    def test_a_bad_list_refuses_before_any_aws_call(
        self, tmp_path: Path, operators_env: str | None, offending: str, reason: str
    ) -> None:
        result, calls, _repo = _run(
            tmp_path, env_extra=_with_expected_account(), operators_env=operators_env
        )
        _assert_refused_naming_no_value(
            result, calls, tmp_path, *([offending] if offending else [])
        )
        assert reason in result.stderr, result.stderr
        assert calls == []

    def test_flag_and_env_disagreeing_refuses_before_any_aws_call(self, tmp_path: Path) -> None:
        result, calls, _repo = _run(
            tmp_path,
            "--operator-principal-arn",
            OPERATOR_ROLE,
            env_extra=_with_expected_account(),
        )
        _assert_refused_naming_no_value(result, calls, tmp_path)
        assert "disagree" in result.stderr
        assert calls == []

    def test_an_empty_flag_value_refuses(self, tmp_path: Path) -> None:
        result, calls, _repo = _run(
            tmp_path,
            "--operator-principal-arn",
            "",
            env_extra=_with_expected_account(),
            operators_env=None,
        )
        _assert_refused_naming_no_value(result, calls, tmp_path)
        assert _EMPTY in result.stderr, result.stderr
        assert calls == []


class TestCallerPrecondition:
    def test_a_caller_outside_the_list_refuses_before_any_mutating_call(
        self, tmp_path: Path
    ) -> None:
        result, calls, _repo = _run(
            tmp_path, env_extra=_with_expected_account(), operators_env=OPERATOR_USER
        )
        _assert_refused_naming_no_value(result, calls, tmp_path)
        assert "not in the operator list" in result.stderr
        assert [c[:2] for c in calls] == [["sts", "get-caller-identity"], ["iam", "get-role"]]

    def test_a_failing_get_role_refuses_rather_than_guessing(self, tmp_path: Path) -> None:
        result, calls, _repo = _run(
            tmp_path,
            env_extra=_with_expected_account(),
            role_arn=f"arn:aws:iam::{ACCOUNT}:role/some-other-role",
        )
        _assert_refused_naming_no_value(result, calls, tmp_path)
        assert "iam get-role" in result.stderr
        assert [c[:2] for c in calls] == [["sts", "get-caller-identity"], ["iam", "get-role"]]

    def test_a_get_role_answer_without_an_arn_refuses(self, tmp_path: Path) -> None:
        result, calls, _repo = _run(
            tmp_path,
            env_extra=_with_expected_account(STUB_ROLE_WITHOUT_ARN="1"),
        )
        _assert_refused_naming_no_value(result, calls, tmp_path)
        assert "iam get-role" in result.stderr
        assert [c[:2] for c in calls] == [["sts", "get-caller-identity"], ["iam", "get-role"]]

    @pytest.mark.parametrize(
        "caller_arn",
        [ROOT_ARN, f"arn:aws:sts::{ACCOUNT}:federated-user/someone"],
        ids=["root", "federated-user"],
    )
    def test_a_caller_that_is_neither_role_nor_user_refuses(
        self, tmp_path: Path, caller_arn: str
    ) -> None:
        result, calls, _repo = _run(
            tmp_path, env_extra=_with_expected_account(), caller_arn=caller_arn
        )
        _assert_refused_naming_no_value(result, calls, tmp_path, caller_arn)
        assert [c[:2] for c in calls] == [["sts", "get-caller-identity"]]

    def test_a_path_bearing_role_is_resolved_through_get_role(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path, operators_env=OPERATOR_ROLE)
        get_role = _one(calls, "iam", "get-role")
        assert _value(get_role, "--role-name") == OPERATOR_ROLE_NAME
        assert calls.index(get_role) < calls.index(_mutating(calls)[0])

    def test_an_iam_user_caller_is_compared_as_is(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path, caller_arn=OPERATOR_USER)
        assert not _calls_for(calls, "iam", "get-role")

    def test_flag_and_env_agreeing_in_another_order_are_accepted(self, tmp_path: Path) -> None:
        result, calls, _repo = _run(
            tmp_path,
            "--operator-principal-arn",
            OPERATOR_ROLE,
            "--operator-principal-arn",
            OPERATOR_USER,
            env_extra=_with_expected_account(),
        )
        assert result.returncode == 0, result.stderr
        _assert_documents(calls)

    def test_the_flag_alone_is_accepted(self, tmp_path: Path) -> None:
        result, calls, _repo = _run(
            tmp_path,
            "--operator-principal-arn",
            OPERATOR_USER,
            "--operator-principal-arn",
            OPERATOR_ROLE,
            env_extra=_with_expected_account(),
            operators_env=None,
        )
        assert result.returncode == 0, result.stderr
        _assert_documents(calls)


class TestPrincipalDenyDocuments:
    def test_a_fresh_bootstrap_renders_every_document(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path)
        _assert_documents(calls)

    def test_an_existing_backend_converges_every_document(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path, existing=True)
        for op in (("s3api", "create-bucket"), ("kms", "create-key"), ("dynamodb", "create-table")):
            assert not _calls_for(calls, *op), op
        _assert_documents(calls)

    @pytest.mark.parametrize("existing", [False, True], ids=["fresh", "existing"])
    def test_the_puts_run_in_the_lock_out_safe_order(self, tmp_path: Path, existing: bool) -> None:
        calls, _repo = _ok(tmp_path, existing=existing)
        order = [
            calls.index(_one(calls, "kms", "put-key-policy")),
            calls.index(_one(calls, "dynamodb", "put-resource-policy")),
            calls.index(_one(calls, "s3api", "put-bucket-policy", LOG_BUCKET)),
            calls.index(_one(calls, "s3api", "put-bucket-policy", STATE_BUCKET)),
        ]
        assert order == sorted(order), order

    @pytest.mark.parametrize("existing", [False, True], ids=["fresh", "existing"])
    def test_no_lock_out_override_is_ever_passed(self, tmp_path: Path, existing: bool) -> None:
        calls, _repo = _ok(tmp_path, existing=existing)
        for call in calls:
            assert "--confirm-remove-self-resource-access" not in call, call
            assert "--bypass-policy-lockout-safety-check" not in call, call

    def test_the_exemption_is_on_the_key_and_the_log_bucket_only(self, tmp_path: Path) -> None:
        calls, _repo = _ok(tmp_path)
        documents = [
            ("key", _key_statements(calls)),
            ("lock-table", _lock_table_statements(calls)),
            ("log-bucket", _policy(calls, LOG_BUCKET)),
            ("state-bucket", _policy(calls, STATE_BUCKET)),
        ]
        exempt = {
            (name, s["Sid"], operator)
            for name, statements in documents
            for s in statements
            for operator, keys in s.get("Condition", {}).items()
            if "aws:PrincipalIsAWSService" in keys
        }
        assert exempt == {
            ("key", smoke.KEY_USE_SID, "Bool"),
            ("log-bucket", S3_DATA_PLANE_SID, "BoolIfExists"),
        }

    def test_the_bootstrap_tuples_equal_the_gate_tuples(self) -> None:
        """The bootstrap's KMS and DynamoDB tuples are the ones the gate and the smoke hold."""
        text = BOOTSTRAP.read_text(encoding="utf-8")
        for name, expected in (
            ("KEY_USE_ACTIONS", smoke.KEY_USE_ACTIONS),
            ("STORE_KEY_ADMIN_ACTIONS", STORE_KEY_ADMIN_ACTIONS),
            ("TABLE_DATA_PLANE_ACTIONS", smoke.TABLE_DATA_PLANE_ACTIONS),
            ("TABLE_ADMIN_ACTIONS", smoke.TABLE_ADMIN_ACTIONS),
            ("S3_OBJECT_READ_ACTIONS", S3_OBJECT_READ_ACTIONS),
            ("S3_OBJECT_WRITE_ACTIONS", S3_OBJECT_WRITE_ACTIONS),
            ("S3_BUCKET_ADMIN_ACTIONS", S3_BUCKET_ADMIN_ACTIONS),
        ):
            (raw,) = [
                line.split("=", 1)[1].strip("'")
                for line in text.splitlines()
                if line.startswith(f"{name}=")
            ]
            assert tuple(json.loads(raw)) == expected, name

    def test_usage_lists_the_operator_flag(self, tmp_path: Path) -> None:
        result, _calls, _repo = _run(tmp_path, "--help")
        assert result.returncode == 0
        assert "--operator-principal-arn ARN [--operator-principal-arn ARN]..." in result.stdout
        assert "  --operator-principal-arn ARN\n" in result.stdout
        assert "CRM_OPERATOR_PRINCIPAL_ARNS" in result.stdout


class TestTheDenyAssertionsDiscriminate:
    """Each mutant copy of the script reds the cell that guards it; the unmutated copy passes."""

    @pytest.mark.parametrize(
        ("original", "mutated"),
        [
            ("}, $use, $admin]", "}, $admin]"),
            ("}, $use, $admin]", "}, $use]"),
            ("Statement: [$table_data, $table_admin]", "Statement: [$table_admin]"),
            ("Statement: [$table_data, $table_admin]", "Statement: [$table_data]"),
            ("[$tls, $grant, $log_data, $log_admin]", "[$tls, $grant, $log_admin]"),
            ("[$tls, $grant, $log_data, $log_admin]", "[$tls, $grant, $log_data]"),
            ("[$tls, $state_data, $state_admin]", "[$tls, $state_admin]"),
            ("[$tls, $state_data, $state_admin]", "[$tls, $state_data]"),
            ('"$OPERATORS_JSON" Bool)"', '"$OPERATORS_JSON")"'),
            ('"$LOG_BUCKET" BoolIfExists)"', '"$LOG_BUCKET")"'),
            ('"$OPERATORS_JSON" Bool)"', '"$OPERATORS_JSON" BoolIfExists)"'),
            ('"$LOG_BUCKET" BoolIfExists)"', '"$LOG_BUCKET" Bool)"'),
            (
                's3_data_plane_deny_statement "$STATE_BUCKET")"',
                's3_data_plane_deny_statement "$STATE_BUCKET" BoolIfExists)"',
            ),
            (
                '{($exempt): {"aws:PrincipalIsAWSService"',
                '{StringNotEquals: {"aws:PrincipalIsAWSService"',
            ),
            ("{ArnNotEquals:", "{ArnNotEqualsIfExists:"),
            ("'. + [$root]'", "'.'"),
            (
                'Principal: "*",\n        Action: $actions',
                'Principal: {AWS: "*"},\n        Action: $actions',
            ),
            ("'$read + $write'", "'$read'"),
        ],
        ids=[
            "no-key-use-deny",
            "no-key-admin-deny",
            "no-table-data-plane-deny",
            "no-table-admin-deny",
            "no-log-data-plane-deny",
            "no-log-admin-deny",
            "no-state-data-plane-deny",
            "no-state-admin-deny",
            "no-key-use-exemption",
            "no-log-bucket-exemption",
            "key-use-exemption-if-exists",
            "log-bucket-exemption-plain-bool",
            "exemption-on-state-bucket",
            "wrong-exemption-operator",
            "if-exists-operator",
            "admin-list-without-root",
            "non-wildcard-principal",
            "no-object-writes",
        ],
    )
    def test_a_weakened_document_fails(self, tmp_path: Path, original: str, mutated: str) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        assert text.count(original) == 1, f"control anchor {original!r} not found once"
        calls, _repo = _ok(tmp_path, existing=True, script_text=text.replace(original, mutated))
        with pytest.raises(AssertionError):
            _assert_documents(calls)

    def test_an_unmutated_copy_renders_the_contract(self, tmp_path: Path) -> None:
        calls, _repo = _ok(
            tmp_path, existing=True, script_text=BOOTSTRAP.read_text(encoding="utf-8")
        )
        _assert_documents(calls)

    def test_a_name_only_caller_comparison_refuses_a_path_bearing_operator(
        self, tmp_path: Path
    ) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        original = (
            'CALLER_PRINCIPAL_ARN="$(aws iam get-role --role-name "$caller_role_name" '
            '"${AWS_ARGS[@]}" 2>/dev/null | jq -r \'.Role.Arn\')"'
        )
        assert text.count(original) == 1, f"control anchor {original!r} not found once"
        mutated = 'CALLER_PRINCIPAL_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${caller_role_name}"'
        result, calls, _repo = _run(
            tmp_path,
            env_extra=_with_expected_account(),
            script_text=text.replace(original, mutated),
        )
        assert result.returncode != 0
        assert "not in the operator list" in result.stderr
        assert _mutating(calls) == []

    @pytest.mark.parametrize(
        "original",
        [
            '[[ "$caller_is_operator" -eq 1 ]] \\',
            '[[ "$operator_arn" =~ ^arn:aws:iam::[0-9]{12}:(role|user)/[^*?]+$ ]] \\',
        ],
        ids=["no-caller-precondition", "no-arn-shape-check"],
    )
    def test_a_removed_guard_lets_the_run_through(self, tmp_path: Path, original: str) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        assert text.count(original) == 1, f"control anchor {original!r} not found once"
        operators = (
            OPERATOR_USER
            if "caller" in original
            else f"{OPERATOR_ROLE},arn:aws:iam::{ACCOUNT}:role/*"
        )
        result, calls, _repo = _run(
            tmp_path,
            env_extra=_with_expected_account(),
            operators_env=operators,
            script_text=text.replace(original, "true \\"),
        )
        assert result.returncode == 0, result.stderr
        assert _mutating(calls)
