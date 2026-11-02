# SPDX-License-Identifier: MIT
"""Tests for scripts/verify_security_baseline.sh, run with `--dry-run` or a stubbed `aws` on PATH.

Covers dry-run never invoking `aws`, control coverage derived from the `docs/security-baseline.md`
`blocks completion` rows, and a complete inventory with a non-zero exit when a control fails.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_security_baseline.sh"
SECURITY_BASELINE_PATH = REPO_ROOT / "docs" / "security-baseline.md"
SECURITY_BASELINE_HEADER = "| Control | Requirement | Verify | Class |"

_FAKE_AWS_STUB = """#!/usr/bin/env bash
# Recording stub: exits loudly and records that it was called, so a test can assert `aws` was
# never invoked during --dry-run rather than merely trusting the source.
set -euo pipefail
printf '%s\\n' "$*" >> "${AWS_STUB_CALL_LOG:?AWS_STUB_CALL_LOG must be set}"
echo "aws-stub: unexpected invocation: $*" >&2
exit 99
"""


def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        ["bash", str(SCRIPT_PATH), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_refuses_to_run_without_billable_flag_or_dry_run() -> None:
    """No opt-in and no --dry-run must refuse before touching AWS, with a non-zero exit."""
    result = _run([])
    assert result.returncode != 0
    assert "billable" in result.stderr.lower()


def test_dry_run_requires_no_billable_flag(tmp_path: Path) -> None:
    """--dry-run alone (no --i-understand-this-is-billable) must succeed."""
    outputs = tmp_path / "tf-outputs.json"
    outputs.write_text("{}", encoding="utf-8")
    result = _run(["--dry-run", "--outputs", str(outputs)])
    assert result.returncode == 0, result.stderr


def test_dry_run_never_invokes_aws(tmp_path: Path) -> None:
    """--dry-run never invokes a recording `aws` stub on PATH."""
    stub_dir = tmp_path / "stubpath"
    stub_dir.mkdir()
    aws_stub = stub_dir / "aws"
    aws_stub.write_text(_FAKE_AWS_STUB, encoding="utf-8")
    aws_stub.chmod(0o755)

    call_log = tmp_path / "aws_calls.log"
    call_log.write_text("", encoding="utf-8")

    outputs = tmp_path / "tf-outputs.json"
    outputs.write_text("{}", encoding="utf-8")

    # The script requires jq even under --dry-run; only `aws` resolves to the stub.
    real_path = proc_run.run(
        ["bash", "-lc", "command -v jq"], capture_output=True, text=True, check=True
    ).stdout.strip()
    jq_dir = str(Path(real_path).parent) if real_path else ""

    env = {
        "PATH": f"{stub_dir}:{jq_dir}:/usr/bin:/bin",
        "AWS_STUB_CALL_LOG": str(call_log),
        "HOME": str(tmp_path),
    }

    result = _run(["--dry-run", "--outputs", str(outputs)], env=env)

    assert result.returncode == 0, result.stderr
    assert call_log.read_text(encoding="utf-8") == "", (
        "the aws stub was invoked during --dry-run: " + call_log.read_text(encoding="utf-8")
    )
    assert "unexpected invocation" not in result.stdout
    assert "unexpected invocation" not in result.stderr


def test_dry_run_prints_one_line_per_control_with_command_and_outcome(tmp_path: Path) -> None:
    """Each control's dry-run line carries an outcome and the command it would run."""
    outputs = tmp_path / "tf-outputs.json"
    outputs.write_text("{}", encoding="utf-8")
    result = _run(["--dry-run", "--outputs", str(outputs)])
    assert result.returncode == 0, result.stderr

    control_lines = [
        line
        for line in result.stdout.splitlines()
        if line.startswith(("[SKIP]", "[PASS]", "[FAIL]", "[DEFER]"))
    ]
    # 15 blocks-completion rows; Lambda role uniqueness prints two lines (plan-time delegation +
    # live cross-check), so at least 15 lines are expected.
    assert len(control_lines) >= 15
    for line in control_lines:
        assert "ran `" in line, f"missing command-run marker: {line}"


def test_network_control_is_explicitly_skipped_not_fabricated(tmp_path: Path) -> None:
    """The Network row is a SKIP naming an unimplemented check over the deployed security groups."""
    outputs = tmp_path / "tf-outputs.json"
    outputs.write_text("{}", encoding="utf-8")
    result = _run(["--dry-run", "--outputs", str(outputs)])
    assert result.returncode == 0, result.stderr
    assert "[SKIP] Network:" in result.stdout
    assert "UNIMPLEMENTED live check" in result.stdout
    assert "three deployed security groups" in result.stdout, (
        "the Network row's reason no longer states that security groups are deployed"
    )
    assert "architecture has no VPC/security group;" not in result.stdout, (
        "the Network row is claiming again that no VPC or security group exists — the three "
        "groups are in the plan and in the account"
    )


def test_reports_every_control_and_fails_when_an_early_control_fails(tmp_path: Path) -> None:
    """With missing outputs and a failing `aws`, every control is reported and the run fails."""
    stub_dir = tmp_path / "stubpath"
    stub_dir.mkdir()
    aws_stub = stub_dir / "aws"
    # An `aws` that always exits 1.
    aws_stub.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\necho '{}' \nexit 1\n", encoding="utf-8"
    )
    aws_stub.chmod(0o755)

    real_path = proc_run.run(
        ["bash", "-lc", "command -v jq"], capture_output=True, text=True, check=True
    ).stdout.strip()
    jq_dir = str(Path(real_path).parent) if real_path else ""

    outputs = tmp_path / "tf-outputs.json"
    # Missing every output the live controls need -> every output-dependent control fails fast.
    outputs.write_text("{}", encoding="utf-8")

    env = {
        "PATH": f"{stub_dir}:{jq_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
    }

    result = _run(
        ["--i-understand-this-is-billable", "--outputs", str(outputs)],
        env=env,
    )

    assert result.returncode != 0, "expected non-zero exit when controls fail"
    expected_control_labels = [
        "DynamoDB encryption at rest",
        "DynamoDB PITR",
        "DynamoDB IAM",
        "S3 frontend bucket",
        "S3 encryption",
        "API Gateway authorizer coverage",
        "Lambda: one execution role per function",
        "Cognito: MFA/password policy/account recovery",
        "Gateway: CUSTOM_JWT configuration",
        "Gateway: REQUEST interceptor",
        "Action Lambdas: resource policy",
        "Network: no internet-facing SG ingress",
        "Secrets: no vendor API key anywhere",
        "Bedrock: assumed role + inference profile only",
        "Transport: TLS 1.2+ everywhere",
    ]
    for label in expected_control_labels:
        assert label in result.stdout, f"missing control in inventory: {label}"


# ---------------------------------------------------------------------------
# Coverage derivation from the docs/security-baseline.md table.
# ---------------------------------------------------------------------------


def _parse_blocks_completion_rows(baseline_text: str) -> list[str]:
    """Return the `Control` cell of every `blocks completion` row in the security-baseline table."""
    lines = baseline_text.splitlines()
    try:
        start = lines.index(SECURITY_BASELINE_HEADER)
    except ValueError:
        raise ValueError(f"could not locate the {SECURITY_BASELINE_HEADER!r} table") from None
    rows: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4 or set(cells[0]) <= {"-", " "}:
            continue
        control = re.sub(r"`", "", cells[0]).strip()
        row_class = re.sub(r"`|\*\*", "", cells[-1]).strip()
        if not control or row_class != "blocks completion":
            continue
        rows.append(control)
    return rows


# Each distinct security-baseline `Control` cell, mapped to a substring of the script's label.
_CONTROL_TO_LABEL_SUBSTRING: dict[str, str] = {
    "DynamoDB encryption at rest": "DynamoDB encryption at rest",
    "DynamoDB PITR": "DynamoDB PITR",
    "DynamoDB IAM": "DynamoDB IAM",
    "S3 frontend bucket": "S3 frontend bucket",
    "S3 encryption": "S3 encryption",
    "API Gateway": "API Gateway authorizer coverage",
    "Lambda": "Lambda: one execution role per function",
    "Cognito": "Cognito: MFA/password policy/account recovery",
    "Gateway": "Gateway: CUSTOM_JWT configuration",
    "Action Lambdas": "Action Lambdas: resource policy",
    "Network": "Network: no internet-facing SG ingress",
    "Secrets": "Secrets: no vendor API key anywhere",  # pragma: allowlist secret
    "Bedrock": "Bedrock: assumed role + inference profile only",
    "Transport": "Transport: TLS 1.2+ everywhere",
}


def test_script_covers_every_real_spec_blocks_completion_control() -> None:
    """Every distinct `blocks completion` Control in the baseline table has a script label."""
    spec_text = SECURITY_BASELINE_PATH.read_text(encoding="utf-8")
    controls = _parse_blocks_completion_rows(spec_text)
    assert len(controls) >= 14, f"expected at least 14 blocks-completion rows, found {controls!r}"

    distinct_controls = set(controls)
    uncovered = distinct_controls - set(_CONTROL_TO_LABEL_SUBSTRING)
    assert not uncovered, (
        f"security-baseline blocks-completion controls with no script coverage: {uncovered}"
    )


def test_coverage_derivation_detects_a_newly_added_row(tmp_path: Path) -> None:
    """A newly added `blocks completion` row in a copy of the table is flagged as uncovered."""
    spec_text = SECURITY_BASELINE_PATH.read_text(encoding="utf-8")
    marker = SECURITY_BASELINE_HEADER + "\n"
    assert marker in spec_text, (
        "security-baseline table header not found; cannot augment for this test"
    )

    new_row = (
        "| Totally New Control | some new requirement | `aws some-new-service describe-thing` "
        "| blocks completion |\n"
    )
    augmented = spec_text.replace(marker, marker + new_row, 1)

    augmented_path = tmp_path / "spec_augmented.md"
    augmented_path.write_text(augmented, encoding="utf-8")

    controls = _parse_blocks_completion_rows(augmented_path.read_text(encoding="utf-8"))
    assert "Totally New Control" in controls, "augmentation fixture itself failed to parse"

    distinct_controls = set(controls)
    uncovered = distinct_controls - set(_CONTROL_TO_LABEL_SUBSTRING)
    assert uncovered == {"Totally New Control"}, (
        f"expected exactly the injected row to be flagged uncovered, got {uncovered}"
    )


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_flag_exits_zero_with_no_aws_call(flag: str) -> None:
    result = _run([flag])
    assert result.returncode == 0
    assert "Usage:" in result.stdout


def test_unknown_argument_is_a_usage_error() -> None:
    result = _run(["--not-a-real-flag"])
    assert result.returncode == 2


def test_help_documents_name_prefix_flag() -> None:
    """--help documents the --name-prefix flag."""
    result = _run(["--help"])
    assert result.returncode == 0
    assert "--name-prefix" in result.stdout


# ---------------------------------------------------------------------------
# DynamoDB controls resolve tables from the dynamodb_table_names output, never `list-tables`.
# ---------------------------------------------------------------------------


def test_dynamodb_controls_use_the_scoped_table_list_not_account_wide_list_tables(
    tmp_path: Path,
) -> None:
    """With a `dynamodb_table_names` output present, `dynamodb list-tables` is never called."""
    stub_dir = tmp_path / "stubpath"
    stub_dir.mkdir()
    aws_stub = stub_dir / "aws"
    # Records any `dynamodb list-tables` call; other subcommands return minimal passing shapes.
    aws_stub.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "dynamodb" && "$2" == "list-tables" ]]; then
  echo "UNSCOPED_LIST_TABLES_CALLED" >> "${AWS_STUB_CALL_LOG:?}"
  echo '{"TableNames":["SpringClean-Unrelated-Table-0000"]}'
  exit 0
fi
if [[ "$1" == "dynamodb" && "$2" == "describe-table" ]]; then
  echo '{"Table":{"SSEDescription":{"SSEType":"KMS","KMSMasterKeyArn":"arn:aws:kms:us-east-1:000000000000:key/x"}}}'
  exit 0
fi
if [[ "$1" == "dynamodb" && "$2" == "describe-continuous-backups" ]]; then
  echo '{"ContinuousBackupsDescription":{"PointInTimeRecoveryDescription":{"PointInTimeRecoveryStatus":"ENABLED"}}}'
  exit 0
fi
if [[ "$1" == "lambda" ]]; then
  echo '{"Functions":[]}'
  exit 0
fi
echo '{}'
exit 0
""",
        encoding="utf-8",
    )
    aws_stub.chmod(0o755)

    call_log = tmp_path / "aws_calls.log"
    call_log.write_text("", encoding="utf-8")

    outputs = tmp_path / "tf-outputs.json"
    outputs.write_text(
        '{"dynamodb_table_names": {"accounts": "crm-agent-evalaccounts"}}', encoding="utf-8"
    )

    real_path = proc_run.run(
        ["bash", "-lc", "command -v jq"], capture_output=True, text=True, check=True
    ).stdout.strip()
    jq_dir = str(Path(real_path).parent) if real_path else ""

    env = {
        "PATH": f"{stub_dir}:{jq_dir}:/usr/bin:/bin",
        "AWS_STUB_CALL_LOG": str(call_log),
        "HOME": str(tmp_path),
    }

    result = _run(
        ["--i-understand-this-is-billable", "--outputs", str(outputs)],
        env=env,
    )

    assert "UNSCOPED_LIST_TABLES_CALLED" not in call_log.read_text(encoding="utf-8"), (
        "dynamodb list-tables was called; DynamoDB controls must scope from the "
        "dynamodb_table_names Terraform output, never an account-wide list-tables call"
    )
    assert "[FAIL] DynamoDB encryption at rest" not in result.stdout
    assert "[FAIL] DynamoDB PITR" not in result.stdout


def test_dynamodb_control_fails_closed_when_table_names_output_is_missing(tmp_path: Path) -> None:
    """A missing dynamodb_table_names output fails the DynamoDB controls."""
    outputs = tmp_path / "tf-outputs.json"
    outputs.write_text("{}", encoding="utf-8")

    stub_dir = tmp_path / "stubpath"
    stub_dir.mkdir()
    aws_stub = stub_dir / "aws"
    aws_stub.write_text("#!/usr/bin/env bash\necho '{}'\nexit 0\n", encoding="utf-8")
    aws_stub.chmod(0o755)
    real_path = proc_run.run(
        ["bash", "-lc", "command -v jq"], capture_output=True, text=True, check=True
    ).stdout.strip()
    jq_dir = str(Path(real_path).parent) if real_path else ""
    env = {"PATH": f"{stub_dir}:{jq_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}

    result = _run(["--i-understand-this-is-billable", "--outputs", str(outputs)], env=env)
    assert result.returncode != 0
    assert "[FAIL] DynamoDB encryption at rest" in result.stdout
    assert "[FAIL] DynamoDB PITR" in result.stdout


def test_lambda_scoped_controls_fail_closed_when_name_prefix_matches_nothing(
    tmp_path: Path,
) -> None:
    """A name prefix matching zero functions fails the Lambda-scoped controls."""
    stub_dir = tmp_path / "stubpath"
    stub_dir.mkdir()
    aws_stub = stub_dir / "aws"
    aws_stub.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "lambda" && "$2" == "list-functions" ]]; then
  echo '{"Functions":[{"FunctionName":"SpringClean-foo"},{"FunctionName":"ai-obs-workshop-bar"}]}'
  exit 0
fi
echo '{}'
exit 0
""",
        encoding="utf-8",
    )
    aws_stub.chmod(0o755)
    real_path = proc_run.run(
        ["bash", "-lc", "command -v jq"], capture_output=True, text=True, check=True
    ).stdout.strip()
    jq_dir = str(Path(real_path).parent) if real_path else ""
    env = {"PATH": f"{stub_dir}:{jq_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}

    outputs = tmp_path / "tf-outputs.json"
    outputs.write_text('{"dynamodb_table_names": {}}', encoding="utf-8")

    result = _run(
        [
            "--i-understand-this-is-billable",
            "--outputs",
            str(outputs),
            "--name-prefix",
            "crm-agent-eval",
        ],
        env=env,
    )
    assert result.returncode != 0
    assert "[FAIL] DynamoDB IAM" in result.stdout
    assert "[FAIL] Action Lambdas: resource policy" in result.stdout
    assert "[FAIL] Bedrock: assumed role + inference profile only" in result.stdout


# ---------------------------------------------------------------------------
# CloudFront origin selection: the S3 origin (has S3OriginConfig), not the first origin.
# ---------------------------------------------------------------------------


def _cloudfront_multi_origin_stub(tmp_path: Path, bucket_domain: str) -> Path:
    stub_dir = tmp_path / "stubpath"
    stub_dir.mkdir()
    aws_stub = stub_dir / "aws"
    aws_stub.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "cloudfront" && "$2" == "list-distributions" ]]; then
  echo '{{"DistributionList":{{"Items":[{{"Id":"DIST1","DomainName":"d123.cloudfront.net"}}]}}}}'
  exit 0
fi
if [[ "$1" == "cloudfront" && "$2" == "get-distribution-config" ]]; then
  cat <<'JSON'
{{"DistributionConfig":{{"Origins":{{"Items":[
  {{"Id":"api-gateway-origin","DomainName":"f19qt8lkd0.execute-api.us-east-1.amazonaws.com","OriginAccessControlId":""}},
  {{"Id":"agent-relay-origin","DomainName":"abcdefghij1234567890.lambda-url.us-east-1.on.aws","OriginAccessControlId":"OACLAMBDA1"}},
  {{"Id":"s3-frontend-origin","DomainName":"{bucket_domain}","S3OriginConfig":{{"OriginAccessIdentity":""}},"OriginAccessControlId":"OAC123"}}
]}},"ViewerCertificate":{{"MinimumProtocolVersion":"TLSv1.2_2021","CloudFrontDefaultCertificate":false}},"DefaultCacheBehavior":{{"ViewerProtocolPolicy":"redirect-to-https"}}}}}}
JSON
  exit 0
fi
if [[ "$1" == "s3api" ]]; then
  case "$2" in
    get-public-access-block) echo '{{"PublicAccessBlockConfiguration":{{"BlockPublicAcls":true,"BlockPublicPolicy":true,"IgnorePublicAcls":true,"RestrictPublicBuckets":true}}}}'; exit 0 ;;
    get-bucket-policy) echo '{{"Policy":"{{\\"Statement\\":[{{\\"Effect\\":\\"Deny\\",\\"Condition\\":{{\\"Bool\\":{{\\"aws:SecureTransport\\":\\"false\\"}}}}}}]}}"}}'; exit 0 ;;
    get-bucket-encryption) echo '{{"ServerSideEncryptionConfiguration":{{"Rules":[{{"ApplyServerSideEncryptionByDefault":{{"SSEAlgorithm":"aws:kms","KMSMasterKeyID":"arn:aws:kms:us-east-1:000000000000:key/x"}}}}]}}}}'; exit 0 ;;
  esac
fi
echo '{{}}'
exit 0
""",
        encoding="utf-8",
    )
    aws_stub.chmod(0o755)
    return stub_dir


def test_s3_frontend_bucket_control_selects_the_s3_origin_not_the_first_origin(
    tmp_path: Path,
) -> None:
    """With API Gateway, relay and S3 origins, the S3 controls pass only on the S3 origin."""
    stub_dir = _cloudfront_multi_origin_stub(
        tmp_path, "crm-agent-evalfrontend-cdn-origin-000000000000.s3.us-east-1.amazonaws.com"
    )
    real_path = proc_run.run(
        ["bash", "-lc", "command -v jq"], capture_output=True, text=True, check=True
    ).stdout.strip()
    jq_dir = str(Path(real_path).parent) if real_path else ""
    env = {"PATH": f"{stub_dir}:{jq_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}

    outputs = tmp_path / "tf-outputs.json"
    outputs.write_text(
        '{"cloudfront_domain_name": "d123.cloudfront.net", "dynamodb_table_names": {}}',
        encoding="utf-8",
    )

    result = _run(["--i-understand-this-is-billable", "--outputs", str(outputs)], env=env)
    assert "[PASS] S3 frontend bucket" in result.stdout, result.stdout
    assert "[PASS] S3 encryption" in result.stdout, result.stdout
    assert "f19qt8lkd0" not in result.stdout
    # The relay Function URL origin also carries an OAC id, so selection must key on S3OriginConfig.
    assert "lambda-url" not in [line for line in result.stdout.splitlines() if "S3" in line]


# ---------------------------------------------------------------------------
# AgentCore gateway id parsing: the id is the URL's first hostname label, not a path segment.
# ---------------------------------------------------------------------------


def test_gateway_controls_parse_the_id_from_the_url_subdomain_not_a_path_segment(
    tmp_path: Path,
) -> None:
    stub_dir = tmp_path / "stubpath"
    stub_dir.mkdir()
    aws_stub = stub_dir / "aws"
    aws_stub.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "bedrock-agentcore-control" && "$2" == "get-gateway" ]]; then
  # Assert the exact identifier the script resolved -- proves the parse, not just that some call
  # eventually returned a passing shape.
  for ((i=3;i<=$#;i++)); do
    if [[ "${!i}" == "--gateway-identifier" ]]; then
      j=$((i+1))
      if [[ "${!j}" != "crm-agent-eval-gateway-smbcbcyim1" ]]; then
        echo "WRONG_GATEWAY_ID:${!j}" >&2
        exit 1
      fi
    fi
  done
  cat <<'JSON'
{"authorizerType":"CUSTOM_JWT","authorizerConfiguration":{"customJWTAuthorizer":{"discoveryUrl":"https://example.com/.well-known","allowedClients":["c1"]}},"interceptorConfigurations":[{"interceptor":{"lambda":{"arn":"arn:aws:lambda:us-east-1:000000000000:function:x"}},"interceptionPoints":["REQUEST"],"inputConfiguration":{"passRequestHeaders":true}}]}
JSON
  exit 0
fi
if [[ "$1" == "dynamodb" && "$2" == "describe-table" ]]; then
  echo '{"Table":{"SSEDescription":{"SSEType":"KMS","KMSMasterKeyArn":"arn:aws:kms:us-east-1:000000000000:key/x"}}}'
  exit 0
fi
if [[ "$1" == "dynamodb" && "$2" == "describe-continuous-backups" ]]; then
  echo '{"ContinuousBackupsDescription":{"PointInTimeRecoveryDescription":{"PointInTimeRecoveryStatus":"ENABLED"}}}'
  exit 0
fi
if [[ "$1" == "lambda" && "$2" == "list-functions" ]]; then
  echo '{"Functions":[{"FunctionName":"crm-agent-eval-x"}]}'
  exit 0
fi
if [[ "$1" == "lambda" && "$2" == "get-function-configuration" ]]; then
  echo '{"Role":"arn:aws:iam::000000000000:role/crm-agent-eval-x-role","Environment":{"Variables":{}}}'
  exit 0
fi
if [[ "$1" == "lambda" && "$2" == "get-policy" ]]; then
  echo '{"Policy":"{\\"Statement\\":[{\\"Principal\\":{\\"Service\\":\\"apigateway.amazonaws.com\\"}}]}"}'
  exit 0
fi
if [[ "$1" == "iam" && "$2" == "simulate-principal-policy" ]]; then
  echo '{"EvaluationResults":[{"EvalDecision":"explicitDeny"}]}'
  exit 0
fi
if [[ "$1" == "cloudfront" && "$2" == "list-distributions" ]]; then
  echo '{"DistributionList":{"Items":[]}}'
  exit 0
fi
if [[ "$1" == "apigatewayv2" && "$2" == "get-apis" ]]; then
  echo '{"Items":[]}'
  exit 0
fi
echo '{}'
exit 0
""",
        encoding="utf-8",
    )
    aws_stub.chmod(0o755)
    real_path = proc_run.run(
        ["bash", "-lc", "command -v jq"], capture_output=True, text=True, check=True
    ).stdout.strip()
    jq_dir = str(Path(real_path).parent) if real_path else ""
    env = {"PATH": f"{stub_dir}:{jq_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}

    outputs = tmp_path / "tf-outputs.json"
    outputs.write_text(
        '{"agentcore_gateway_url": '
        '"https://crm-agent-eval-gateway-smbcbcyim1.gateway.bedrock-agentcore.us-east-1.amazonaws.com", '
        '"dynamodb_table_names": {"accounts": "crm-agent-evalaccounts"}}',
        encoding="utf-8",
    )

    result = _run(["--i-understand-this-is-billable", "--outputs", str(outputs)], env=env)
    assert "could not resolve gateway id" not in result.stdout
    assert "[PASS] Gateway: CUSTOM_JWT configuration" in result.stdout, result.stdout
    assert "[PASS] Gateway: REQUEST interceptor" in result.stdout, result.stdout


def test_gateway_interceptor_control_fails_on_the_real_response_shape_without_pass_headers(
    tmp_path: Path,
) -> None:
    """A `get-gateway` response with `passRequestHeaders` false fails the interceptor control."""
    stub_dir = tmp_path / "stubpath"
    stub_dir.mkdir()
    aws_stub = stub_dir / "aws"
    aws_stub.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "bedrock-agentcore-control" && "$2" == "get-gateway" ]]; then
  cat <<'JSON'
{"authorizerType":"CUSTOM_JWT","authorizerConfiguration":{"customJWTAuthorizer":{"discoveryUrl":"https://example.com/.well-known","allowedClients":["c1"]}},"interceptorConfigurations":[{"interceptor":{"lambda":{"arn":"arn:aws:lambda:us-east-1:000000000000:function:x"}},"interceptionPoints":["REQUEST"],"inputConfiguration":{"passRequestHeaders":false}}]}
JSON
  exit 0
fi
echo '{}'
exit 0
""",
        encoding="utf-8",
    )
    aws_stub.chmod(0o755)
    real_path = proc_run.run(
        ["bash", "-lc", "command -v jq"], capture_output=True, text=True, check=True
    ).stdout.strip()
    jq_dir = str(Path(real_path).parent) if real_path else ""
    env = {"PATH": f"{stub_dir}:{jq_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}

    outputs = tmp_path / "tf-outputs.json"
    outputs.write_text(
        '{"agentcore_gateway_url": '
        '"https://crm-agent-eval-gateway-smbcbcyim1.gateway.bedrock-agentcore.us-east-1.amazonaws.com", '
        '"dynamodb_table_names": {"accounts": "crm-agent-evalaccounts"}}',
        encoding="utf-8",
    )

    result = _run(["--i-understand-this-is-billable", "--outputs", str(outputs)], env=env)
    assert "[FAIL] Gateway: REQUEST interceptor" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# TLS on the default CloudFront certificate is reported as DEFER, never PASS or a generic FAIL.
# ---------------------------------------------------------------------------


def test_transport_tls_control_defers_honestly_on_the_default_certificate(tmp_path: Path) -> None:
    stub_dir = tmp_path / "stubpath"
    stub_dir.mkdir()
    aws_stub = stub_dir / "aws"
    aws_stub.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "cloudfront" && "$2" == "list-distributions" ]]; then
  echo '{"DistributionList":{"Items":[{"Id":"DIST1","DomainName":"d123.cloudfront.net"}]}}'
  exit 0
fi
if [[ "$1" == "cloudfront" && "$2" == "get-distribution-config" ]]; then
  echo '{"DistributionConfig":{"ViewerCertificate":{"MinimumProtocolVersion":"TLSv1","CloudFrontDefaultCertificate":true},"DefaultCacheBehavior":{"ViewerProtocolPolicy":"redirect-to-https"}}}'
  exit 0
fi
if [[ "$1" == "dynamodb" && "$2" == "describe-table" ]]; then
  echo '{"Table":{"SSEDescription":{"SSEType":"KMS","KMSMasterKeyArn":"arn:aws:kms:us-east-1:000000000000:key/x"}}}'
  exit 0
fi
if [[ "$1" == "dynamodb" && "$2" == "describe-continuous-backups" ]]; then
  echo '{"ContinuousBackupsDescription":{"PointInTimeRecoveryDescription":{"PointInTimeRecoveryStatus":"ENABLED"}}}'
  exit 0
fi
if [[ "$1" == "lambda" && "$2" == "list-functions" ]]; then
  echo '{"Functions":[{"FunctionName":"crm-agent-eval-x"}]}'
  exit 0
fi
if [[ "$1" == "lambda" && "$2" == "get-function-configuration" ]]; then
  echo '{"Role":"arn:aws:iam::000000000000:role/crm-agent-eval-x-role","Environment":{"Variables":{}}}'
  exit 0
fi
if [[ "$1" == "lambda" && "$2" == "get-policy" ]]; then
  echo '{"Policy":"{\\"Statement\\":[{\\"Principal\\":{\\"Service\\":\\"apigateway.amazonaws.com\\"}}]}"}'
  exit 0
fi
if [[ "$1" == "iam" && "$2" == "simulate-principal-policy" ]]; then
  echo '{"EvaluationResults":[{"EvalDecision":"explicitDeny"}]}'
  exit 0
fi
if [[ "$1" == "apigatewayv2" && "$2" == "get-apis" ]]; then
  echo '{"Items":[]}'
  exit 0
fi
echo '{}'
exit 0
""",
        encoding="utf-8",
    )
    aws_stub.chmod(0o755)
    real_path = proc_run.run(
        ["bash", "-lc", "command -v jq"], capture_output=True, text=True, check=True
    ).stdout.strip()
    jq_dir = str(Path(real_path).parent) if real_path else ""
    env = {"PATH": f"{stub_dir}:{jq_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}

    outputs = tmp_path / "tf-outputs.json"
    outputs.write_text(
        '{"cloudfront_domain_name": "d123.cloudfront.net", '
        '"dynamodb_table_names": {"accounts": "crm-agent-evalaccounts"}}',
        encoding="utf-8",
    )

    result = _run(["--i-understand-this-is-billable", "--outputs", str(outputs)], env=env)
    assert result.returncode != 0, "a DEFER must still force a non-zero exit"
    assert "[DEFER] Transport: TLS 1.2+ everywhere" in result.stdout
    assert "[FAIL] Transport: TLS 1.2+ everywhere" not in result.stdout
    assert "INERT without a custom ACM certificate" in result.stdout
    assert "README" in result.stdout


def test_transport_tls_control_still_fails_a_sub_1_2_minimum_on_a_custom_certificate(
    tmp_path: Path,
) -> None:
    """A sub-1.2 minimum on a custom certificate fails rather than deferring."""
    stub_dir = tmp_path / "stubpath"
    stub_dir.mkdir()
    aws_stub = stub_dir / "aws"
    aws_stub.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "cloudfront" && "$2" == "list-distributions" ]]; then
  echo '{"DistributionList":{"Items":[{"Id":"DIST1","DomainName":"d123.cloudfront.net"}]}}'
  exit 0
fi
if [[ "$1" == "cloudfront" && "$2" == "get-distribution-config" ]]; then
  echo '{"DistributionConfig":{"ViewerCertificate":{"MinimumProtocolVersion":"TLSv1","CloudFrontDefaultCertificate":false},"DefaultCacheBehavior":{"ViewerProtocolPolicy":"redirect-to-https"}}}'
  exit 0
fi
if [[ "$1" == "dynamodb" && "$2" == "describe-table" ]]; then
  echo '{"Table":{"SSEDescription":{"SSEType":"KMS","KMSMasterKeyArn":"arn:aws:kms:us-east-1:000000000000:key/x"}}}'
  exit 0
fi
if [[ "$1" == "dynamodb" && "$2" == "describe-continuous-backups" ]]; then
  echo '{"ContinuousBackupsDescription":{"PointInTimeRecoveryDescription":{"PointInTimeRecoveryStatus":"ENABLED"}}}'
  exit 0
fi
if [[ "$1" == "lambda" && "$2" == "list-functions" ]]; then
  echo '{"Functions":[{"FunctionName":"crm-agent-eval-x"}]}'
  exit 0
fi
if [[ "$1" == "lambda" && "$2" == "get-function-configuration" ]]; then
  echo '{"Role":"arn:aws:iam::000000000000:role/crm-agent-eval-x-role","Environment":{"Variables":{}}}'
  exit 0
fi
if [[ "$1" == "lambda" && "$2" == "get-policy" ]]; then
  echo '{"Policy":"{\\"Statement\\":[{\\"Principal\\":{\\"Service\\":\\"apigateway.amazonaws.com\\"}}]}"}'
  exit 0
fi
if [[ "$1" == "iam" && "$2" == "simulate-principal-policy" ]]; then
  echo '{"EvaluationResults":[{"EvalDecision":"explicitDeny"}]}'
  exit 0
fi
if [[ "$1" == "apigatewayv2" && "$2" == "get-apis" ]]; then
  echo '{"Items":[]}'
  exit 0
fi
echo '{}'
exit 0
""",
        encoding="utf-8",
    )
    aws_stub.chmod(0o755)
    real_path = proc_run.run(
        ["bash", "-lc", "command -v jq"], capture_output=True, text=True, check=True
    ).stdout.strip()
    jq_dir = str(Path(real_path).parent) if real_path else ""
    env = {"PATH": f"{stub_dir}:{jq_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}

    outputs = tmp_path / "tf-outputs.json"
    outputs.write_text(
        '{"cloudfront_domain_name": "d123.cloudfront.net", '
        '"dynamodb_table_names": {"accounts": "crm-agent-evalaccounts"}}',
        encoding="utf-8",
    )

    result = _run(["--i-understand-this-is-billable", "--outputs", str(outputs)], env=env)
    assert result.returncode != 0
    assert "[FAIL] Transport: TLS 1.2+ everywhere" in result.stdout
    assert "[DEFER] Transport: TLS 1.2+ everywhere" not in result.stdout
