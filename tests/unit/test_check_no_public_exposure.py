# SPDX-License-Identifier: MIT
"""Tests for `scripts/check_no_public_exposure.py`, the no-public-surface gate.

Each control has a fixture that must fail it and one that must pass it; a scan that examines nothing
reports SKIP.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from scripts import check_no_public_exposure as gate
from scripts.hcl_text import blank_comments

COMPLIANT_LAMBDA = """
resource "aws_lambda_function" "this" {
  function_name = "x"
  role          = aws_iam_role.this.arn

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = [aws_security_group.this.id]
  }
}
"""

COMPLIANT_URL = """
resource "aws_lambda_function_url" "this" {
  function_name      = aws_lambda_function.this.function_name
  authorization_type = "AWS_IAM"
  invoke_mode        = "RESPONSE_STREAM"
}
"""

#: An `AWS_IAM`-conditioned invoke grant that makes `COMPLIANT_URL` compliant under control 6.
COMPLIANT_URL_GRANT = """
resource "aws_lambda_permission" "url" {
  statement_id           = "AllowCloudFrontOacInvokeFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.this.function_name
  principal              = "cloudfront.amazonaws.com"
  source_arn             = aws_cloudfront_distribution.frontend.arn
  function_url_auth_type = "AWS_IAM"
}
"""

COMPLIANT_API = """
resource "aws_apigatewayv2_api" "this" {
  name                         = "x"
  protocol_type                = "HTTP"
  disable_execute_api_endpoint = true
}
"""


def _scan(tmp_path: Path, *sources: str) -> gate.Report:
    for index, source in enumerate(sources):
        (tmp_path / f"m{index}.tf").write_text(source, encoding="utf-8")
    return gate.scan(tmp_path)


def _controls(report: gate.Report) -> set[str]:
    return {finding.control for finding in report.findings}


# --------------------------------------------------------------------------------------------------
# Control 1 -- Function URL authorization
# --------------------------------------------------------------------------------------------------


def test_none_authorized_function_url_is_a_finding(tmp_path: Path) -> None:
    source = """
    resource "aws_lambda_function_url" "relay" {
      function_name      = "r"
      authorization_type = "NONE"
    }
    """
    assert "function-url-auth" in _controls(_scan(tmp_path, COMPLIANT_LAMBDA, source))


def test_function_url_with_unset_authorization_is_a_finding(tmp_path: Path) -> None:
    """A Function URL with no `authorization_type` fails exactly like an explicit `NONE`."""
    source = """
    resource "aws_lambda_function_url" "relay" {
      function_name = "r"
    }
    """
    assert "function-url-auth" in _controls(_scan(tmp_path, COMPLIANT_LAMBDA, source))


def test_iam_authorized_function_url_passes(tmp_path: Path) -> None:
    report = _scan(tmp_path, COMPLIANT_LAMBDA, COMPLIANT_URL)
    assert "function-url-auth" not in _controls(report)


# --------------------------------------------------------------------------------------------------
# Control 2 -- Lambda resource policy
# --------------------------------------------------------------------------------------------------


def test_wildcard_principal_permission_is_a_finding(tmp_path: Path) -> None:
    source = """
    resource "aws_lambda_permission" "public" {
      action        = "lambda:InvokeFunctionUrl"
      function_name = "r"
      principal     = "*"
    }
    """
    assert "lambda-wildcard-principal" in _controls(_scan(tmp_path, COMPLIANT_LAMBDA, source))


def test_service_principal_permission_passes(tmp_path: Path) -> None:
    source = """
    resource "aws_lambda_permission" "apigw" {
      action        = "lambda:InvokeFunction"
      function_name = "r"
      principal     = "apigateway.amazonaws.com"
    }
    """
    assert "lambda-wildcard-principal" not in _controls(_scan(tmp_path, COMPLIANT_LAMBDA, source))


# --------------------------------------------------------------------------------------------------
# Control 3 -- API Gateway default endpoint
# --------------------------------------------------------------------------------------------------


def test_api_without_disabled_default_endpoint_is_a_finding(tmp_path: Path) -> None:
    source = """
    resource "aws_apigatewayv2_api" "this" {
      name          = "x"
      protocol_type = "HTTP"
    }
    """
    assert "api-default-endpoint" in _controls(_scan(tmp_path, COMPLIANT_LAMBDA, source))


def test_api_with_disabled_default_endpoint_passes(tmp_path: Path) -> None:
    report = _scan(tmp_path, COMPLIANT_LAMBDA, COMPLIANT_API)
    assert "api-default-endpoint" not in _controls(report)


# --------------------------------------------------------------------------------------------------
# Control 4 -- Lambda VPC attachment
# --------------------------------------------------------------------------------------------------


def test_lambda_without_vpc_config_is_a_finding(tmp_path: Path) -> None:
    source = """
    resource "aws_lambda_function" "bare" {
      function_name = "x"
      role          = "r"
    }
    """
    assert "lambda-vpc" in _controls(_scan(tmp_path, source))


def test_lambda_with_vpc_config_passes(tmp_path: Path) -> None:
    assert "lambda-vpc" not in _controls(_scan(tmp_path, COMPLIANT_LAMBDA))


# --------------------------------------------------------------------------------------------------
# Control 5 -- S3
# --------------------------------------------------------------------------------------------------


def test_partial_block_public_access_is_a_finding(tmp_path: Path) -> None:
    source = """
    resource "aws_s3_bucket_public_access_block" "this" {
      bucket                  = "b"
      block_public_acls       = true
      block_public_policy     = true
      ignore_public_acls      = true
      restrict_public_buckets = false
    }
    """
    assert "s3-block-public-access" in _controls(_scan(tmp_path, COMPLIANT_LAMBDA, source))


def test_public_read_acl_is_a_finding(tmp_path: Path) -> None:
    source = """
    resource "aws_s3_bucket_acl" "this" {
      bucket = "b"
      acl    = "public-read"
    }
    """
    assert "s3-public-acl" in _controls(_scan(tmp_path, COMPLIANT_LAMBDA, source))


def test_wildcard_allow_bucket_policy_is_a_finding(tmp_path: Path) -> None:
    source = """
    resource "aws_s3_bucket_policy" "this" {
      bucket = "b"
      policy = jsonencode({
        Statement = [{
          "Effect" = "Allow"
          "Principal" = "*"
          "Action" = "s3:GetObject"
        }]
      })
    }
    """
    assert "s3-wildcard-allow" in _controls(_scan(tmp_path, COMPLIANT_LAMBDA, source))


def test_wildcard_deny_for_tls_enforcement_is_not_a_finding(tmp_path: Path) -> None:
    """A `Principal: "*"` statement under `Deny` (TLS enforcement) is not a finding."""
    source = """
    resource "aws_s3_bucket_policy" "tls" {
      bucket = "b"
      policy = jsonencode({
        Statement = [{
          "Effect" = "Deny"
          "Principal" = "*"
          "Action" = "s3:*"
          "Condition" = { Bool = { "aws:SecureTransport" = "false" } }
        }]
      })
    }
    """
    assert "s3-wildcard-allow" not in _controls(_scan(tmp_path, COMPLIANT_LAMBDA, source))


# --------------------------------------------------------------------------------------------------
# Parsing and vacuity
# --------------------------------------------------------------------------------------------------


def test_commented_out_violation_is_not_a_finding(tmp_path: Path) -> None:
    """A violation inside a comment is not a finding."""
    source = """
    # resource "aws_lambda_function_url" "old" {
    #   authorization_type = "NONE"
    # }
    /* authorization_type = "NONE" was the earlier choice */
    """
    report = _scan(tmp_path, COMPLIANT_LAMBDA, source)
    assert report.findings == []


def test_comment_stripping_preserves_line_numbers(tmp_path: Path) -> None:
    text = 'a\n# c\n/* x\ny */\nresource "aws_lambda_function" "z" {\n}\n'
    assert blank_comments(text).count("\n") == text.count("\n")


def test_directory_with_no_governed_resource_skips(tmp_path: Path) -> None:
    """A directory with no governed resource exits with the SKIP status."""
    (tmp_path / "only.tf").write_text('resource "aws_iam_role" "r" {\n  name = "x"\n}\n', "utf-8")
    assert gate.main(["--infra-dir", str(tmp_path)]) == gate.SKIP_EXIT_STATUS


def test_missing_directory_skips(tmp_path: Path) -> None:
    assert gate.main(["--infra-dir", str(tmp_path / "absent")]) == gate.SKIP_EXIT_STATUS


def test_skip_exit_status_matches_run_gates(tmp_path: Path) -> None:
    """The gate's SKIP exit status equals `run_gates`'s."""
    from scripts import run_gates

    assert gate.SKIP_EXIT_STATUS == run_gates.GATE_SKIP_EXIT_STATUS


def test_provider_cache_is_not_scanned(tmp_path: Path) -> None:
    cache = tmp_path / ".terraform" / "modules"
    cache.mkdir(parents=True)
    (cache / "vendored.tf").write_text(
        'resource "aws_lambda_function" "v" {\n  function_name = "v"\n}\n', "utf-8"
    )
    (tmp_path / "own.tf").write_text(COMPLIANT_LAMBDA, encoding="utf-8")
    assert gate.scan(tmp_path).findings == []


# --------------------------------------------------------------------------------------------------
# Control 6 -- the `lambda:FunctionUrlAuthType` condition key on the invoke grant
#
# Independent of control 1: `authorization_type` governs what the URL demands, this governs what the
# grant permits.
# --------------------------------------------------------------------------------------------------

_URL_GRANT_WITHOUT_CONDITION = """
resource "aws_lambda_permission" "url" {
  action        = "lambda:InvokeFunctionUrl"
  function_name = "r"
  principal     = "cloudfront.amazonaws.com"
}
"""


def test_invoke_function_url_grant_without_the_condition_is_a_finding(tmp_path: Path) -> None:
    """An `InvokeFunctionUrl` grant with no auth-type condition is a finding."""
    report = _scan(tmp_path, COMPLIANT_LAMBDA, COMPLIANT_URL, _URL_GRANT_WITHOUT_CONDITION)
    assert "function-url-auth-condition" in _controls(report)


def test_grant_conditioned_on_none_is_a_finding(tmp_path: Path) -> None:
    """A grant conditioned on `NONE` is a finding."""
    source = """
    resource "aws_lambda_permission" "url" {
      action                 = "lambda:InvokeFunctionUrl"
      function_name          = "r"
      principal              = "cloudfront.amazonaws.com"
      function_url_auth_type = "NONE"
    }
    """
    assert "function-url-auth-condition" in _controls(
        _scan(tmp_path, COMPLIANT_LAMBDA, COMPLIANT_URL, source)
    )


def test_grant_conditioned_on_an_unrecognised_value_is_a_finding(tmp_path: Path) -> None:
    """A grant conditioned on any value other than `AWS_IAM` is a finding."""
    source = """
    resource "aws_lambda_permission" "url" {
      action                 = "lambda:InvokeFunctionUrl"
      function_name          = "r"
      principal              = "cloudfront.amazonaws.com"
      function_url_auth_type = "AWS_IAM_BUT_NOT_REALLY"
    }
    """
    assert "function-url-auth-condition" in _controls(
        _scan(tmp_path, COMPLIANT_LAMBDA, COMPLIANT_URL, source)
    )


def test_conditioned_grant_passes(tmp_path: Path) -> None:
    report = _scan(tmp_path, COMPLIANT_LAMBDA, COMPLIANT_URL, COMPLIANT_URL_GRANT)
    assert "function-url-auth-condition" not in _controls(report)
    assert "function-url-grant-missing" not in _controls(report)


def test_lowercase_condition_value_passes(tmp_path: Path) -> None:
    """The condition value comparison is case-insensitive."""
    source = COMPLIANT_URL_GRANT.replace('"AWS_IAM"', '"aws_iam"')
    assert "function-url-auth-condition" not in _controls(
        _scan(tmp_path, COMPLIANT_LAMBDA, COMPLIANT_URL, source)
    )


def test_grant_for_another_action_is_out_of_scope(tmp_path: Path) -> None:
    """A `lambda:InvokeFunction` grant without the condition key is not a finding."""
    source = """
    resource "aws_lambda_permission" "apigw" {
      action        = "lambda:InvokeFunction"
      function_name = "r"
      principal     = "apigateway.amazonaws.com"
    }
    """
    report = _scan(tmp_path, COMPLIANT_LAMBDA, COMPLIANT_URL, COMPLIANT_URL_GRANT, source)
    assert "function-url-auth-condition" not in _controls(report)


# ---- Branch 2: a Function URL with no conditioned grant at all --------------------------------


def test_function_url_with_no_grant_at_all_is_a_finding(tmp_path: Path) -> None:
    """A Function URL with no conditioned grant is a finding that names the URL."""
    report = _scan(tmp_path, COMPLIANT_LAMBDA, COMPLIANT_URL)
    assert "function-url-grant-missing" in _controls(report)
    detail = next(f.detail for f in report.findings if f.control == "function-url-grant-missing")
    assert "this" in detail and "0 lambda:InvokeFunctionUrl grant(s)" in detail


def test_second_function_url_without_its_own_grant_is_a_finding(tmp_path: Path) -> None:
    """A second Function URL without its own conditioned grant is a finding."""
    second = COMPLIANT_URL.replace('"this"', '"second"')
    report = _scan(tmp_path, COMPLIANT_LAMBDA, COMPLIANT_URL, COMPLIANT_URL_GRANT, second)
    assert "function-url-grant-missing" in _controls(report)


def test_grant_without_a_url_is_not_a_finding(tmp_path: Path) -> None:
    """A conditioned grant with no Function URL is not a finding."""
    report = _scan(tmp_path, COMPLIANT_LAMBDA, COMPLIANT_URL_GRANT)
    assert "function-url-grant-missing" not in _controls(report)


def test_tree_with_no_function_url_is_not_a_finding(tmp_path: Path) -> None:
    """A tree with no Function URL produces no missing-grant finding."""
    report = _scan(tmp_path, COMPLIANT_LAMBDA, COMPLIANT_API)
    assert "function-url-grant-missing" not in _controls(report)


def test_the_real_tree_complies_with_control_six(tmp_path: Path) -> None:
    """The real infra tree has Function URLs and every one has a conditioned grant."""
    report = gate.scan(gate.DEFAULT_INFRA_DIR)
    urls, conditioned = report.url_grants
    assert urls > 0, "no Function URL found; control 6 would have verified nothing"
    assert conditioned >= urls
    assert "function-url-auth-condition" not in _controls(report)
    assert "function-url-grant-missing" not in _controls(report)


# --------------------------------------------------------------------------------------------------
# Brace matching is not fooled by braces that are data (heredocs, unterminated quotes)
#
# Two mechanisms are tested:
#   1. heredoc bodies are blanked length-preservingly, before comment-stripping;
#   2. every block's body is bounded by the next `resource` header.
# --------------------------------------------------------------------------------------------------

_BAD_URL_WITH_HEREDOC_BRACE = """
resource "aws_lambda_function_url" "relay" {
  function_name = "r"
  note          = <<-EOT
    a description ending in a brace {
  EOT
}
"""

_COMPLIANT_NEIGHBOUR = """
resource "aws_lambda_function_url" "other" {
  function_name      = "o"
  authorization_type = "AWS_IAM"
}
"""

_BAD_URL_UNTERMINATED_QUOTE = """
resource "aws_lambda_function_url" "relay" {
  function_name = "r
}
"""

_BAD_URL_HASH_IN_HEREDOC = """
resource "aws_lambda_function_url" "relay" {
  function_name = "r"
  note          = <<-EOT
    # note the brace {
  EOT
}
"""


def test_heredoc_brace_does_not_hide_the_next_blocks_violation(tmp_path: Path) -> None:
    """An unbalanced `{` in a heredoc does not hide the next block's violation."""
    report = _scan(tmp_path, COMPLIANT_LAMBDA, _BAD_URL_WITH_HEREDOC_BRACE + _COMPLIANT_NEIGHBOUR)
    assert "function-url-auth" in _controls(report)


def test_unterminated_quote_does_not_hide_the_next_blocks_violation(tmp_path: Path) -> None:
    """An unterminated quote does not hide the next block's violation."""
    report = _scan(tmp_path, COMPLIANT_LAMBDA, _BAD_URL_UNTERMINATED_QUOTE + _COMPLIANT_NEIGHBOUR)
    assert "function-url-auth" in _controls(report)


def test_hash_inside_a_heredoc_does_not_hide_the_next_blocks_violation(tmp_path: Path) -> None:
    """A `#` line inside a heredoc does not hide the next block's violation."""
    report = _scan(tmp_path, COMPLIANT_LAMBDA, _BAD_URL_HASH_IN_HEREDOC + _COMPLIANT_NEIGHBOUR)
    assert "function-url-auth" in _controls(report)


def test_the_full_shape_that_returned_zero_findings(tmp_path: Path) -> None:
    """A heredoc-brace URL alongside two conditioned grants is still reported."""
    grants = COMPLIANT_URL_GRANT + COMPLIANT_URL_GRANT.replace('"url"', '"url_two"')
    report = _scan(
        tmp_path, COMPLIANT_LAMBDA, _BAD_URL_WITH_HEREDOC_BRACE + _COMPLIANT_NEIGHBOUR + grants
    )
    assert "function-url-auth" in _controls(report)
    assert report.findings, "the scan reported nothing at all, which is the defect itself"


_COMPLIANT_URL_HEREDOC_CLOSING_BRACE = """
resource "aws_lambda_function_url" "relay" {
  note               = <<-EOT
    a description ending in a closing brace }
  EOT
  function_name      = "r"
  authorization_type = "AWS_IAM"
}
"""


def test_a_heredoc_closing_brace_does_not_fire_on_compliant_code(tmp_path: Path) -> None:
    """An unbalanced `}` in a heredoc does not end the block early and fire on compliant code."""
    report = _scan(tmp_path, COMPLIANT_LAMBDA, _COMPLIANT_URL_HEREDOC_CLOSING_BRACE)
    assert "function-url-auth" not in _controls(report), (
        "the gate reported a compliant Function URL, because a heredoc's closing brace ended its block "
        "early and hid its own authorization_type"
    )


def test_the_reported_line_number_is_still_the_real_one(tmp_path: Path) -> None:
    """A finding after heredoc blanking reports the block's real line number."""
    source = _BAD_URL_WITH_HEREDOC_BRACE + _COMPLIANT_NEIGHBOUR
    (tmp_path / "m0.tf").write_text(COMPLIANT_LAMBDA, encoding="utf-8")
    (tmp_path / "m1.tf").write_text(source, encoding="utf-8")
    report = gate.scan(tmp_path)

    finding = next(f for f in report.findings if f.control == "function-url-auth")
    lines = source.split(chr(10))
    expected = lines.index('resource "aws_lambda_function_url" "relay" {') + 1
    assert finding.line == expected, f"reported line {finding.line}, real line {expected}"


def test_every_real_heredoc_form_still_parses() -> None:
    """The real infra tree, with its heredocs, parses to the expected resources without findings."""
    report = gate.scan(gate.DEFAULT_INFRA_DIR)
    assert report.counts.get("aws_lambda_function_url", 0) == 1
    assert report.counts.get("aws_lambda_function", 0) >= 2
    assert "function-url-auth" not in _controls(report)


def test_clean_tree_exits_zero(tmp_path: Path) -> None:
    for index, source in enumerate(
        (COMPLIANT_LAMBDA, COMPLIANT_URL, COMPLIANT_URL_GRANT, COMPLIANT_API)
    ):
        (tmp_path / f"m{index}.tf").write_text(source, encoding="utf-8")
    assert gate.main(["--infra-dir", str(tmp_path)]) == 0


def test_violating_tree_exits_one(tmp_path: Path) -> None:
    (tmp_path / "m.tf").write_text(
        'resource "aws_lambda_function" "bare" {\n  function_name = "x"\n}\n', encoding="utf-8"
    )
    assert gate.main(["--infra-dir", str(tmp_path)]) == 1


def test_json_report_is_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "m.tf").write_text(
        'resource "aws_lambda_function" "bare" {\n  function_name = "x"\n}\n', encoding="utf-8"
    )
    assert gate.main(["--infra-dir", str(tmp_path), "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["counts"]["aws_lambda_function"] == 1
    assert [finding["control"] for finding in document["findings"]] == ["lambda-vpc"]


def test_summary_reports_a_denominator(tmp_path: Path) -> None:
    """The summary reports governed-resource and Function URL grant denominators."""
    report = _scan(tmp_path, COMPLIANT_LAMBDA, COMPLIANT_URL, COMPLIANT_URL_GRANT, COMPLIANT_API)
    summary = report.summary()
    assert "4 governed resource(s)" in summary
    assert "0 finding(s)" in summary
    # Control 6 reports its own denominator separately from the governed-resource total.
    assert "1 Function URL(s) with 1 AWS_IAM-conditioned invoke grant(s)" in summary


def test_summary_names_the_directory_actually_scanned(tmp_path: Path) -> None:
    """The summary names the directory that was scanned."""
    report = _scan(tmp_path, COMPLIANT_LAMBDA)
    assert f"scanned {tmp_path}" in report.summary()
    assert str(gate.DEFAULT_INFRA_DIR) not in report.summary()


def test_json_report_names_the_directory_actually_scanned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The JSON report names the directory that was scanned."""
    (tmp_path / "m.tf").write_text(COMPLIANT_LAMBDA, encoding="utf-8")
    assert gate.main(["--infra-dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["scanned"] == str(tmp_path)


def test_the_default_directory_is_resolved_from_the_script_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`DEFAULT_INFRA_DIR` is the absolute repository `infra/` path, independent of cwd."""
    monkeypatch.chdir(tmp_path)
    assert gate.DEFAULT_INFRA_DIR == gate.REPO_ROOT / "infra"
    assert gate.DEFAULT_INFRA_DIR.is_absolute()


def test_real_infra_tree_is_examined_not_skipped() -> None:
    """The gate examines this repository's Terraform files and Lambda functions."""
    report = gate.scan(gate.DEFAULT_INFRA_DIR)
    assert report.files > 0
    assert report.counts.get("aws_lambda_function", 0) > 0


# --------------------------------------------------------------------------------------------------
# Control 7 -- AgentCore Gateway inbound auth
# --------------------------------------------------------------------------------------------------

_JWT_GATEWAY = """
resource "aws_bedrockagentcore_gateway" "this" {
  name            = "g"
  role_arn        = var.role_arn
  authorizer_type = "CUSTOM_JWT"

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url  = var.discovery_url
      allowed_scopes = ["gateway/invoke"]
    }
  }
}
"""

#: The attribute line, not a comment that quotes it.
_GATEWAY_REAL_AUTH_LINE = re.compile(r'^(\s+authorizer_type\s*=\s*)"CUSTOM_JWT"', re.MULTILINE)


def _gateway(authorizer_line: str, *, with_jwt_block: bool = False) -> str:
    block = (
        "  authorizer_configuration {\n    custom_jwt_authorizer {\n"
        "      discovery_url = var.discovery_url\n    }\n  }\n"
        if with_jwt_block
        else ""
    )
    return (
        'resource "aws_bedrockagentcore_gateway" "g" {\n  name = "g"\n'
        f"  {authorizer_line}\n{block}}}\n"
    )


def test_custom_jwt_gateway_passes(tmp_path: Path) -> None:
    report = _scan(tmp_path, COMPLIANT_LAMBDA, _JWT_GATEWAY)
    assert "gateway-inbound-auth" not in _controls(report)
    assert report.counts["aws_bedrockagentcore_gateway"] == 1


def test_aws_iam_gateway_passes(tmp_path: Path) -> None:
    report = _scan(tmp_path, COMPLIANT_LAMBDA, _gateway('authorizer_type = "AWS_IAM"'))
    assert "gateway-inbound-auth" not in _controls(report)


@pytest.mark.parametrize(
    "authorizer_line",
    [
        'authorizer_type = "NONE"',
        "",
        "authorizer_type = var.authorizer_type",
        'authorizer_type = "custom_jwt"',
        'authorizer_type = "CUSTOM_JWT_X"',
    ],
    ids=["none", "absent", "non-literal", "wrong-case", "unknown-value"],
)
def test_a_gateway_without_a_literal_inbound_authorizer_is_a_finding(
    tmp_path: Path, authorizer_line: str
) -> None:
    report = _scan(tmp_path, COMPLIANT_LAMBDA, _gateway(authorizer_line, with_jwt_block=True))
    assert "gateway-inbound-auth" in _controls(report)
    assert gate.main(["--infra-dir", str(tmp_path)]) == 1


def test_custom_jwt_without_an_authorizer_block_is_a_finding(tmp_path: Path) -> None:
    report = _scan(tmp_path, COMPLIANT_LAMBDA, _gateway('authorizer_type = "CUSTOM_JWT"'))
    detail = next(f.detail for f in report.findings if f.control == "gateway-inbound-auth")
    assert "custom_jwt_authorizer" in detail


def test_a_commented_authorizer_type_does_not_satisfy_the_control(tmp_path: Path) -> None:
    source = _gateway('# authorizer_type = "CUSTOM_JWT"', with_jwt_block=True)
    assert "gateway-inbound-auth" in _controls(_scan(tmp_path, COMPLIANT_LAMBDA, source))


def test_a_gateway_alone_is_a_governed_resource(tmp_path: Path) -> None:
    """A tree whose only governed resource is a NONE Gateway fails rather than skipping."""
    (tmp_path / "g.tf").write_text(_gateway('authorizer_type = "NONE"'), encoding="utf-8")
    assert gate.main(["--infra-dir", str(tmp_path)]) == 1


def _real_gateway_files() -> list[Path]:
    """Every authored `.tf` under the real `infra/` that declares a Gateway, derived from source."""
    header = re.compile(r'^resource\s+"aws_bedrockagentcore_gateway"\s', re.MULTILINE)
    return [
        path
        for path in gate.terraform_sources(gate.DEFAULT_INFRA_DIR)
        if header.search(path.read_text(encoding="utf-8"))
    ]


def test_the_real_tree_examines_every_declared_gateway() -> None:
    files = _real_gateway_files()
    assert files, "no Gateway in infra/; control 7 would verify nothing"
    declared = sum(
        len(re.findall(r'^resource\s+"aws_bedrockagentcore_gateway"\s', p.read_text("utf-8"), re.M))
        for p in files
    )
    report = gate.scan(gate.DEFAULT_INFRA_DIR)
    assert report.counts.get("aws_bedrockagentcore_gateway", 0) == declared
    assert "gateway-inbound-auth" not in _controls(report)


def _copy_tf_sources(target: Path) -> dict[Path, Path]:
    """Copy only the gate's own source set (authored `*.tf`) into `target`; `{real: copy}`."""
    copies: dict[Path, Path] = {}
    for path in gate.terraform_sources(gate.DEFAULT_INFRA_DIR):
        copy = target / path.relative_to(gate.DEFAULT_INFRA_DIR)
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(path.read_bytes())
        copies[path] = copy
    return copies


def test_mutating_each_real_gateways_authorizer_to_none_reds_the_gate(tmp_path: Path) -> None:
    """Scratch copy of `infra/`: flipping any one Gateway's `authorizer_type` reds exactly it."""
    copies = _copy_tf_sources(tmp_path / "infra")
    assert gate.scan(tmp_path / "infra").findings == [], "the unmutated copy must pass"
    for real in _real_gateway_files():
        copy = copies[real]
        original = copy.read_text(encoding="utf-8")
        mutated, replaced = _GATEWAY_REAL_AUTH_LINE.subn(r'\1"NONE"', original)
        assert replaced == 1, (real, replaced)
        copy.write_text(mutated, encoding="utf-8")
        findings = gate.scan(tmp_path / "infra").findings
        copy.write_text(original, encoding="utf-8")
        assert [(f.control, f.path) for f in findings] == [("gateway-inbound-auth", copy)], real
