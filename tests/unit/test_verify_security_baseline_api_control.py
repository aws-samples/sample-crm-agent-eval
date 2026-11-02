# SPDX-License-Identifier: MIT
"""Tests for control 6 (`control_api_gateway_authorizer`) of `scripts/verify_security_baseline.sh`.

The control enumerates a private REST API by its `api_id` output. The control body and its jq
program are extracted from the script's own text; the body is also executed with a stubbed `aws`.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
from scripts import proc_run

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SCRIPT: Final[Path] = _REPO_ROOT / "scripts" / "verify_security_baseline.sh"


def _control_source() -> str:
    """The body of `control_api_gateway_authorizer`, isolated from the rest of the script."""
    text = _SCRIPT.read_text(encoding="utf-8")
    start = text.index("control_api_gateway_authorizer() {")
    end = text.index("\n}\n", start)
    body = text[start:end]
    assert len(body) > 200, "the control body was not isolated; the delimiters have moved"
    return body


def _extracted_jq_program() -> str:
    """Return the jq program the control runs, extracted from the script text."""
    body = _control_source()
    match = re.search(r"\| jq -c '(.*?)'", body, re.DOTALL)
    assert match, "no `jq -c` program found in control_api_gateway_authorizer"
    return match.group(1)


def _run_jq(program: str, document: dict[str, Any]) -> list[dict[str, Any]]:
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is not installed")
    completed = proc_run.run(
        [jq, "-c", program],
        input=json.dumps(document),
        capture_output=True,
        text=True,
        check=True,
    )
    return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# It reads the output that names what it needs
# ---------------------------------------------------------------------------


def test_the_control_reads_api_id_and_not_api_endpoint() -> None:
    """The control reads the `api_id` output, never the `api_endpoint` hostname output."""
    body = _control_source()
    assert "tf_output api_id" in body
    assert "tf_output api_endpoint" not in body


def test_the_control_makes_no_apigatewayv2_call() -> None:
    """The control enumerates with v1 `get-resources --embed methods`, never `apigatewayv2`."""
    body = _control_source()
    assert "apigatewayv2" not in body
    assert "ApiEndpoint" not in body
    assert "get-resources" in body
    assert "--embed methods" in body


def test_the_reported_command_matches_what_the_control_runs() -> None:
    """The reported `cmd` string names the v1 `apigateway get-resources` call the control runs."""
    body = _control_source()
    reported = re.search(r'local cmd="([^"]+)"', body)
    assert reported, "no reported command found"
    assert "apigateway get-resources" in reported.group(1)
    assert "apigatewayv2" not in reported.group(1)


def test_the_control_asserts_the_authorization_type_not_merely_an_authorizer_id() -> None:
    """The control branches on `authorizationType` as well as requiring an `authorizerId`.

    The assertion matches the branch condition itself, since `COGNITO_USER_POOLS` also occurs in the
    PASS message.
    """
    body = _control_source()
    assert '"${auth_type}" != "COGNITO_USER_POOLS"' in body, (
        "the control no longer branches on authorizationType, so an attached authorizer with "
        "authorizationType=NONE would be reported as covered"
    )
    assert '-z "${authorizer_id}"' in body, "the control no longer requires an authorizerId either"


def test_an_empty_method_enumeration_is_a_failure_not_a_pass() -> None:
    """The control treats an enumeration of zero methods as a broken enumeration."""
    body = _control_source()
    assert "no methods found" in body
    assert "broken enumeration" in body


# ---------------------------------------------------------------------------
# The shipped jq program, exercised against real-shaped v1 documents
# ---------------------------------------------------------------------------


def _resources_document() -> dict[str, Any]:
    """A `get-resources --embed methods` response, shaped as botocore's model declares.

    `resourceMethods` is a map of verb -> method, and with `--embed methods` each method carries
    `authorizationType` and `authorizerId` directly.
    """
    return {
        "items": [
            {
                "id": "r1",
                "path": "/accounts",
                "resourceMethods": {
                    "GET": {"authorizationType": "COGNITO_USER_POOLS", "authorizerId": "auth1"},
                    "OPTIONS": {"authorizationType": "NONE"},
                },
            },
            {"id": "r2", "path": "/no-methods-here"},
            {
                "id": "r3",
                "path": "/opportunities",
                "resourceMethods": {"POST": {"authorizationType": "NONE"}},
            },
        ]
    }


def test_the_jq_program_flattens_resources_into_one_row_per_method() -> None:
    rows = _run_jq(_extracted_jq_program(), _resources_document())
    assert [(r["path"], r["verb"]) for r in rows] == [
        ("/accounts", "GET"),
        ("/accounts", "OPTIONS"),
        ("/opportunities", "POST"),
    ]


def test_a_resource_with_no_methods_contributes_no_row() -> None:
    """A resource with no `resourceMethods` member yields no row and does not abort the program."""
    rows = _run_jq(_extracted_jq_program(), _resources_document())
    assert all(row["path"] != "/no-methods-here" for row in rows)


def test_the_jq_program_surfaces_the_fields_the_loop_branches_on() -> None:
    """A missing field is emitted as `""`, not `null`."""
    rows = _run_jq(_extracted_jq_program(), _resources_document())
    unauthorized = next(row for row in rows if row["verb"] == "POST")
    assert unauthorized["authorizationType"] == "NONE"
    assert unauthorized["authorizerId"] == ""


def test_the_jq_program_would_expose_an_unauthorized_method() -> None:
    """A non-OPTIONS method with `authorizationType` `NONE` is the one offending row."""
    rows = _run_jq(_extracted_jq_program(), _resources_document())
    offending = [
        row
        for row in rows
        if row["verb"] != "OPTIONS"
        and (row["authorizationType"] != "COGNITO_USER_POOLS" or not row["authorizerId"])
    ]
    assert [(r["verb"], r["path"]) for r in offending] == [("POST", "/opportunities")]


def test_a_fully_authorized_api_yields_no_offending_method() -> None:
    """The offending-method predicate clears a fully authorized API."""
    document = {
        "items": [
            {
                "id": "r1",
                "path": "/accounts",
                "resourceMethods": {
                    "GET": {"authorizationType": "COGNITO_USER_POOLS", "authorizerId": "auth1"},
                    "OPTIONS": {"authorizationType": "NONE"},
                },
            }
        ]
    }
    rows = _run_jq(_extracted_jq_program(), document)
    offending = [
        row
        for row in rows
        if row["verb"] != "OPTIONS"
        and (row["authorizationType"] != "COGNITO_USER_POOLS" or not row["authorizerId"])
    ]
    assert offending == []


def test_options_is_excluded_deliberately_and_the_reason_is_recorded() -> None:
    """The control skips `OPTIONS` methods and names preflight as the reason."""
    body = _control_source()
    assert 'if [[ "${verb}" == "OPTIONS" ]]' in body
    assert "preflight" in body


# ---------------------------------------------------------------------------
# The shipped control body, executed under `set -euo pipefail` with a stubbed `aws`; the sentinel
# prints only if the control returned instead of aborting the shell.
# ---------------------------------------------------------------------------

_SENTINEL: Final[str] = "SENTINEL_INVENTORY_CONTINUED"


def _run_control(aws_body: str) -> subprocess.CompletedProcess[str]:
    """Execute the shipped `control_api_gateway_authorizer` with `aws` stubbed by ``aws_body``.

    `report` and `tf_output` are stubbed; the trailing sentinel prints only if the control returned.
    """
    bash = shutil.which("bash")
    if bash is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are both required to execute the shipped control body")
    script = "\n".join(
        [
            "set -euo pipefail",
            "DRY_RUN=false",
            "AWS_ARGS=(--profile p --region r --no-cli-pager --output json)",
            'report() { printf \'[%s] %s :: %s\\n\' "$3" "$1" "${4:-}"; }',
            'tf_output() { TF_OUTPUT_VALUE="abc123"; }',
            "aws() { " + aws_body + " }",
            _control_source(),
            "}",
            "control_api_gateway_authorizer",
            f"printf '%s\\n' {_SENTINEL}",
        ]
    )
    return proc_run.run([bash, "-c", script], capture_output=True, text=True, check=False)


def _emit(document: dict[str, Any]) -> str:
    """An `aws` stub body printing ``document`` and succeeding."""
    return f"printf '%s' {shlex.quote(json.dumps(document))};"


def test_an_enumeration_that_could_not_run_reports_fail_and_does_not_abort_the_inventory() -> None:
    """A failing `get-resources` reports a broken-enumeration FAIL and the inventory continues."""
    result = _run_control('echo "AccessDeniedException" >&2; return 254;')
    assert "[FAIL] API Gateway authorizer coverage" in result.stdout, (
        "a get-resources failure produced no FAIL line; the control aborted the script instead of "
        f"reporting. stdout={result.stdout!r} stderr={result.stderr!r} rc={result.returncode}"
    )
    assert "broken enumeration" in result.stdout, (
        "the failure did not name itself as a broken enumeration, so a reader cannot tell an "
        "unresolvable enumeration apart from an authorized API"
    )
    assert _SENTINEL in result.stdout, (
        "the inventory did not continue past control 6; every later control was silently skipped"
    )


def test_a_successful_call_returning_zero_methods_is_also_a_named_failure() -> None:
    """A successful call returning zero methods reports a broken-enumeration FAIL."""
    result = _run_control(_emit({"items": []}))
    assert "[FAIL] API Gateway authorizer coverage" in result.stdout
    assert "no methods found" in result.stdout
    assert "broken enumeration" in result.stdout
    assert _SENTINEL in result.stdout


def test_a_method_with_authorization_none_fails_even_with_an_authorizer_attached() -> None:
    """A method with `authorizationType` `NONE` fails even when it carries an `authorizerId`."""
    result = _run_control(
        _emit(
            {
                "items": [
                    {
                        "id": "r1",
                        "path": "/get_account",
                        "resourceMethods": {
                            "POST": {"authorizationType": "NONE", "authorizerId": "auth1"}
                        },
                    }
                ]
            }
        )
    )
    assert "[FAIL] API Gateway authorizer coverage" in result.stdout, result.stdout
    assert "POST /get_account" in result.stdout, "the offending method is not named in the detail"
    assert _SENTINEL in result.stdout


def test_a_correctly_authorized_api_passes_end_to_end() -> None:
    """A correctly authorized API reports PASS when the control body is executed."""
    result = _run_control(
        _emit(
            {
                "items": [
                    {"id": "r0", "path": "/"},
                    {
                        "id": "r1",
                        "path": "/get_account",
                        "resourceMethods": {
                            "POST": {
                                "authorizationType": "COGNITO_USER_POOLS",
                                "authorizerId": "auth1",
                            },
                            "OPTIONS": {"authorizationType": "NONE", "authorizerId": ""},
                        },
                    },
                ]
            }
        )
    )
    assert "[PASS] API Gateway authorizer coverage" in result.stdout, result.stdout
    assert _SENTINEL in result.stdout


def test_an_unresolvable_api_id_reports_fail_rather_than_examining_nothing() -> None:
    """An unavailable `api_id` output reports FAIL without calling `aws`."""
    bash = shutil.which("bash")
    if bash is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are both required to execute the shipped control body")
    script = "\n".join(
        [
            "set -euo pipefail",
            "DRY_RUN=false",
            "AWS_ARGS=(--profile p --region r)",
            'report() { printf \'[%s] %s :: %s\\n\' "$3" "$1" "${4:-}"; }',
            "tf_output() { return 2; }",
            'aws() { echo "aws must not be called without an api id" >&2; return 1; }',
            _control_source(),
            "}",
            "control_api_gateway_authorizer",
            f"printf '%s\\n' {_SENTINEL}",
        ]
    )
    result = proc_run.run([bash, "-c", script], capture_output=True, text=True, check=False)
    assert "[FAIL] API Gateway authorizer coverage" in result.stdout, result.stdout
    assert "api_id output unavailable" in result.stdout
    assert _SENTINEL in result.stdout
