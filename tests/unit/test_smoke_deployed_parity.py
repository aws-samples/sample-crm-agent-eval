# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.deployed_parity`.

Every case is exercised against a fake `lambda` client and temporary zips: no AWS call, no
credential, no network.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import sys
from pathlib import Path
from typing import Any, Final

from botocore.exceptions import ClientError, EndpointConnectionError
from scripts import proc_run
from scripts.smoke import deployed_parity
from scripts.smoke.harness import context_for_test
from scripts.smoke.lambda_actions import function_name_for

from crm.toolspec.definitions import ALL_TOOLS

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent


class FakeLambdaClient:
    """Minimal `lambda` client double returning canned configurations per function name."""

    def __init__(
        self,
        configurations: dict[str, dict[str, Any]],
        *,
        raises: dict[str, Exception] | None = None,
    ) -> None:
        self._configurations = configurations
        self._raises = raises or {}
        self.calls: list[str] = []

    def get_function_configuration(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        """Return the canned configuration for `FunctionName`, or raise what the test declared."""
        self.calls.append(FunctionName)
        if FunctionName in self._raises:
            raise self._raises[FunctionName]
        if FunctionName not in self._configurations:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
                "GetFunctionConfiguration",
            )
        return self._configurations[FunctionName]


def _b64_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _write_all_zips(directory: Path, *, content_for: Any = None) -> dict[str, bytes]:
    """Write one zip per action tool plus the two non-tool zips; return name -> bytes."""
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, bytes] = {}
    for tool in ALL_TOOLS:
        payload = content_for(tool.name) if content_for else f"zip-bytes-{tool.name}".encode()
        (directory / f"{tool.name}.zip").write_bytes(payload)
        written[f"{tool.name}.zip"] = payload
    for _, zip_name in deployed_parity.NON_TOOL_FUNCTIONS:
        payload = f"zip-bytes-{zip_name}".encode()
        (directory / zip_name).write_bytes(payload)
        written[zip_name] = payload
    return written


def _configurations_matching(
    written: dict[str, bytes], *, include_non_tool: bool = True
) -> dict[str, dict[str, Any]]:
    """Canned configurations whose digests match every zip.

    `include_non_tool=False` makes the two non-tool functions report stale deployed bytes.
    """
    configurations: dict[str, dict[str, Any]] = {}
    for tool in ALL_TOOLS:
        payload = written[f"{tool.name}.zip"]
        configurations[function_name_for(tool)] = {
            "CodeSha256": _b64_sha256(payload),
            "CodeSize": len(payload),
            "LastModified": "2026-09-06T09:00:00.000+0000",
        }
    for function_name, zip_name in deployed_parity.NON_TOOL_FUNCTIONS:
        payload = written[zip_name] if include_non_tool else b"stale-deployed-bytes"
        configurations[function_name] = {
            "CodeSha256": _b64_sha256(payload),
            "CodeSize": len(payload),
            "LastModified": "2026-09-06T20:42:24.000+0000",
        }
    return configurations


def _run_checks(client: Any, directory: Path) -> list[Any]:
    ctx = context_for_test(clients={"lambda": client})
    return list(deployed_parity.checks(ctx, directory=directory))


# ---------------------------------------------------------------------------
# The digest encoding is the one Lambda reports.
# ---------------------------------------------------------------------------


def test_local_zip_digest_is_base64_sha256_matching_openssl(tmp_path: Path) -> None:
    """`local_zip_digest` equals `openssl dgst -sha256 -binary <file> | base64`."""
    target = tmp_path / "artifact.zip"
    target.write_bytes(b"deterministic content for the digest test")
    expected = _b64_sha256(target.read_bytes())
    assert deployed_parity.local_zip_digest(target) == expected

    openssl = proc_run.run(
        ["/usr/bin/openssl", "dgst", "-sha256", "-binary", str(target)],
        capture_output=True,
        check=False,
    )
    if openssl.returncode == 0:
        assert base64.b64encode(openssl.stdout).decode("ascii") == expected


# ---------------------------------------------------------------------------
# Coverage: every action Lambda, derived from ALL_TOOLS.
# ---------------------------------------------------------------------------


def test_every_action_tool_is_covered_and_derived_from_all_tools(tmp_path: Path) -> None:
    """One target per `ALL_TOOLS` entry, with the physical name Terraform assigns."""
    targets = deployed_parity.action_targets(tmp_path)
    assert len(targets) == len(ALL_TOOLS)
    assert {t.function_name for t in targets} == {
        f"crm-agent-eval-{tool.name}" for tool in ALL_TOOLS
    }
    assert {t.zip_path.name for t in targets} == {f"{tool.name}.zip" for tool in ALL_TOOLS}


def test_non_tool_function_names_appear_verbatim_in_infra_main_tf() -> None:
    """The two non-tool function names are the ones `infra/main.tf` declares — no silent drift."""
    main_tf = (REPO_ROOT / "infra" / "main.tf").read_text(encoding="utf-8")
    for function_name, _ in deployed_parity.NON_TOOL_FUNCTIONS:
        assert f'"{function_name}"' in main_tf, f"{function_name} not declared in infra/main.tf"


def test_all_matching_yields_pass_for_every_lambda_including_the_non_tool_two(
    tmp_path: Path,
) -> None:
    """Every function, action and non-tool alike, gets a `pass` and nothing defers."""
    written = _write_all_zips(tmp_path)
    client = FakeLambdaClient(_configurations_matching(written))
    results = _run_checks(client, tmp_path)

    parity = [r for r in results if r.check == deployed_parity.PARITY_CHECK]
    assert len(parity) == len(ALL_TOOLS) + len(deployed_parity.NON_TOOL_FUNCTIONS)
    assert all(r.status == "pass" for r in parity)
    assert [r for r in results if r.status == "fail"] == []
    assert [r for r in results if r.status == "defer"] == [], (
        "no verdict in this class may defer once the exemption is retired"
    )
    assert all(not r.defer_reason for r in results)
    coverage = next(r for r in results if r.check == "coverage_is_non_vacuous")
    assert coverage.status == "pass"
    non_tool_coverage = next(r for r in results if r.check == "non_tool_coverage_is_non_vacuous")
    assert non_tool_coverage.status == "pass"


# ---------------------------------------------------------------------------
# Fail-closed behaviour, case by case.
# ---------------------------------------------------------------------------


def test_a_single_stale_function_fails_and_names_both_digests(tmp_path: Path) -> None:
    written = _write_all_zips(tmp_path)
    configurations = _configurations_matching(written)
    stale_tool = ALL_TOOLS[0]
    stale_function = function_name_for(stale_tool)
    configurations[stale_function]["CodeSha256"] = _b64_sha256(b"previously-deployed-bytes")
    client = FakeLambdaClient(configurations)

    results = _run_checks(client, tmp_path)
    failed = [r for r in results if r.status == "fail"]
    assert len(failed) == 1
    detail = failed[0].detail
    assert failed[0].resource == stale_tool.name
    assert stale_function in detail
    assert _b64_sha256(b"previously-deployed-bytes") in detail
    assert _b64_sha256(written[f"{stale_tool.name}.zip"]) in detail


def test_missing_local_zip_is_a_failure_never_a_pass(tmp_path: Path) -> None:
    written = _write_all_zips(tmp_path)
    missing = ALL_TOOLS[0]
    (tmp_path / f"{missing.name}.zip").unlink()
    client = FakeLambdaClient(_configurations_matching(written))

    results = _run_checks(client, tmp_path)
    verdict = next(
        r for r in results if r.resource == missing.name and r.check == deployed_parity.PARITY_CHECK
    )
    assert verdict.status == "fail"
    assert "could not be read" in verdict.detail


def test_no_zips_at_all_fails_every_check_and_never_reads_as_parity(tmp_path: Path) -> None:
    empty = tmp_path / "empty-build"
    empty.mkdir()
    client = FakeLambdaClient({})
    results = _run_checks(client, empty)

    build = next(r for r in results if r.check == "local_build_present")
    assert build.status == "fail"
    assert "no zips built is not parity" in build.detail
    parity = [r for r in results if r.check == deployed_parity.PARITY_CHECK]
    assert len(parity) == len(ALL_TOOLS) + len(deployed_parity.NON_TOOL_FUNCTIONS)
    assert all(r.status == "fail" for r in parity), (
        "a missing build must never yield a pass, a defer or a bare skip"
    )


def test_absent_build_directory_is_a_failure(tmp_path: Path) -> None:
    results = _run_checks(FakeLambdaClient({}), tmp_path / "does-not-exist")
    build = next(r for r in results if r.check == "local_build_present")
    assert build.status == "fail"
    assert "does not exist" in build.detail


def test_function_that_does_not_exist_is_a_failure(tmp_path: Path) -> None:
    written = _write_all_zips(tmp_path)
    configurations = _configurations_matching(written)
    absent_tool = ALL_TOOLS[1]
    del configurations[function_name_for(absent_tool)]
    client = FakeLambdaClient(configurations)

    results = _run_checks(client, tmp_path)
    verdict = next(
        r
        for r in results
        if r.resource == absent_tool.name and r.check == deployed_parity.PARITY_CHECK
    )
    assert verdict.status == "fail"
    assert "does not exist" in verdict.detail


def test_response_without_code_sha256_is_a_failure(tmp_path: Path) -> None:
    written = _write_all_zips(tmp_path)
    configurations = _configurations_matching(written)
    tool = ALL_TOOLS[2]
    configurations[function_name_for(tool)].pop("CodeSha256")
    client = FakeLambdaClient(configurations)

    results = _run_checks(client, tmp_path)
    verdict = next(
        r for r in results if r.resource == tool.name and r.check == deployed_parity.PARITY_CHECK
    )
    assert verdict.status == "fail"
    assert "no CodeSha256" in verdict.detail


def test_transport_error_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    written = _write_all_zips(tmp_path)
    tool = ALL_TOOLS[3]
    client = FakeLambdaClient(
        _configurations_matching(written),
        raises={
            function_name_for(tool): EndpointConnectionError(endpoint_url="https://lambda.invalid")
        },
    )
    results = _run_checks(client, tmp_path)
    verdict = next(
        r for r in results if r.resource == tool.name and r.check == deployed_parity.PARITY_CHECK
    )
    assert verdict.status == "fail"
    assert "parity cannot be established" in verdict.detail


def test_non_not_found_client_error_is_a_failure(tmp_path: Path) -> None:
    written = _write_all_zips(tmp_path)
    tool = ALL_TOOLS[4]
    client = FakeLambdaClient(
        _configurations_matching(written),
        raises={
            function_name_for(tool): ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                "GetFunctionConfiguration",
            )
        },
    )
    results = _run_checks(client, tmp_path)
    verdict = next(
        r for r in results if r.resource == tool.name and r.check == deployed_parity.PARITY_CHECK
    )
    assert verdict.status == "fail"
    assert "AccessDeniedException" in verdict.detail


def test_coverage_check_fails_when_no_verdict_was_reached() -> None:
    """`check_coverage` fails on zero or missing verdicts and passes on the full expected set."""
    expected = deployed_parity.expected_action_resources()
    assert deployed_parity.check_coverage(()).status == "fail"
    assert deployed_parity.check_coverage(expected).status == "pass"
    assert deployed_parity.check_coverage(expected[:-1]).status == "fail"


def test_coverage_check_discriminates_by_resource_name_not_only_by_count() -> None:
    """A duplicated, renamed or extra resource fails coverage and names the discrepancy."""
    expected = deployed_parity.expected_action_resources()

    duplicated = (*expected[:-1], expected[0])
    result = deployed_parity.check_coverage(duplicated)
    assert result.status == "fail"
    assert f"duplicated=['{expected[0]}']" in result.detail
    assert f"missing=['{expected[-1]}']" in result.detail

    renamed = (*expected[:-1], "not_a_declared_tool")
    result = deployed_parity.check_coverage(renamed)
    assert result.status == "fail"
    assert "unexpected=['not_a_declared_tool']" in result.detail

    extra = (*expected, "one_target_too_many")
    assert deployed_parity.check_coverage(extra).status == "fail"


def test_expected_action_resources_is_derived_from_all_tools() -> None:
    """`expected_action_resources` is the tool names of `ALL_TOOLS`, in order."""
    assert deployed_parity.expected_action_resources() == tuple(t.name for t in ALL_TOOLS)


# ---------------------------------------------------------------------------
# The two non-tool functions: ordinary parity targets, pass/fail like every other function.
# ---------------------------------------------------------------------------


def test_the_two_non_tool_functions_pass_when_their_digests_match(tmp_path: Path) -> None:
    """A matching digest on a non-tool function earns a `pass` with no defer reason."""
    written = _write_all_zips(tmp_path)
    client = FakeLambdaClient(_configurations_matching(written))
    results = _run_checks(client, tmp_path)

    non_tool_names = {name for name, _ in deployed_parity.NON_TOOL_FUNCTIONS}
    non_tool_results = [
        r
        for r in results
        if r.resource in non_tool_names and r.check == deployed_parity.PARITY_CHECK
    ]
    assert len(non_tool_results) == 2
    for result in non_tool_results:
        assert result.status == "pass"
        assert not result.defer_reason
        assert "==" in result.detail, "the digests must still be published for an operator"


def test_a_stale_non_tool_function_now_fails_instead_of_deferring(tmp_path: Path) -> None:
    """A divergent non-tool function is a `fail`, never a `defer`."""
    written = _write_all_zips(tmp_path)
    client = FakeLambdaClient(_configurations_matching(written, include_non_tool=False))
    results = _run_checks(client, tmp_path)

    non_tool_names = {name for name, _ in deployed_parity.NON_TOOL_FUNCTIONS}
    failed = [r for r in results if r.status == "fail"]
    assert {r.resource for r in failed} == non_tool_names
    assert [r for r in results if r.status == "defer"] == []
    for result in failed:
        assert result.check == deployed_parity.PARITY_CHECK
        assert "!=" in result.detail
        assert "apply the current zips" in result.detail


def test_a_missing_non_tool_zip_is_a_failure_never_a_defer(tmp_path: Path) -> None:
    """An unreadable non-tool zip is a `fail` with no defer reason."""
    written = _write_all_zips(tmp_path)
    name, zip_name = deployed_parity.NON_TOOL_FUNCTIONS[0]
    (tmp_path / zip_name).unlink()
    client = FakeLambdaClient(_configurations_matching(written))
    results = _run_checks(client, tmp_path)

    verdict = next(
        r for r in results if r.resource == name and r.check == deployed_parity.PARITY_CHECK
    )
    assert verdict.status == "fail"
    assert not verdict.defer_reason
    assert "could not be read" in verdict.detail


def test_a_nonexistent_non_tool_function_is_a_failure(tmp_path: Path) -> None:
    """A non-tool function that does not exist is a `fail`."""
    written = _write_all_zips(tmp_path)
    configurations = _configurations_matching(written)
    name, _ = deployed_parity.NON_TOOL_FUNCTIONS[1]
    del configurations[name]
    results = _run_checks(FakeLambdaClient(configurations), tmp_path)

    verdict = next(
        r for r in results if r.resource == name and r.check == deployed_parity.PARITY_CHECK
    )
    assert verdict.status == "fail"
    assert "does not exist" in verdict.detail


def test_non_tool_coverage_is_a_separate_identity_check(tmp_path: Path) -> None:
    """The non-tool names get their own identity check, disjoint from `check_coverage`'s targets."""
    expected = deployed_parity.expected_non_tool_resources()
    assert expected == tuple(name for name, _ in deployed_parity.NON_TOOL_FUNCTIONS)
    assert set(expected).isdisjoint(deployed_parity.expected_action_resources())

    assert deployed_parity.check_non_tool_coverage(()).status == "fail"
    assert deployed_parity.check_non_tool_coverage(expected).status == "pass"
    assert deployed_parity.check_non_tool_coverage(expected[:1]).status == "fail"

    duplicated = deployed_parity.check_non_tool_coverage((expected[0], expected[0]))
    assert duplicated.status == "fail"
    assert f"duplicated=['{expected[0]}']" in duplicated.detail
    assert f"missing=['{expected[1]}']" in duplicated.detail

    renamed = deployed_parity.check_non_tool_coverage((expected[0], "not_a_declared_function"))
    assert renamed.status == "fail"
    assert "unexpected=['not_a_declared_function']" in renamed.detail

    # The action-tool check must not be satisfied by the non-tool verdicts, or vice versa.
    assert deployed_parity.check_coverage(expected).status == "fail"
    assert (
        deployed_parity.check_non_tool_coverage(deployed_parity.expected_action_resources()).status
        == "fail"
    )


# ---------------------------------------------------------------------------
# The deploy.sh post-apply gate.
# ---------------------------------------------------------------------------


def test_deploy_gate_returns_zero_when_every_lambda_matches(tmp_path: Path) -> None:
    written = _write_all_zips(tmp_path)
    client = FakeLambdaClient(_configurations_matching(written))
    stream = io.StringIO()
    exit_code, results = deployed_parity.deploy_gate(client, directory=tmp_path, stream=stream)
    assert exit_code == 0
    expected_total = len(ALL_TOOLS) + len(deployed_parity.NON_TOOL_FUNCTIONS)
    assert f"OK: all {expected_total} Lambda function(s)" in stream.getvalue()
    assert [r for r in results if r.status == "fail"] == []
    assert {name for name, _ in deployed_parity.NON_TOOL_FUNCTIONS} <= {
        r.resource for r in results
    }, "the post-apply gate now covers the two non-tool functions too"


def test_deploy_gate_returns_one_on_a_stale_non_tool_function(tmp_path: Path) -> None:
    """The post-apply gate exits 1 on a stale non-tool function."""
    written = _write_all_zips(tmp_path)
    stream = io.StringIO()
    exit_code, results = deployed_parity.deploy_gate(
        FakeLambdaClient(_configurations_matching(written, include_non_tool=False)),
        directory=tmp_path,
        stream=stream,
    )
    assert exit_code == 1
    assert "FAIL:" in stream.getvalue()
    assert {r.resource for r in results if r.status == "fail"} == {
        name for name, _ in deployed_parity.NON_TOOL_FUNCTIONS
    }


def test_deploy_gate_returns_one_on_a_single_stale_function(tmp_path: Path) -> None:
    written = _write_all_zips(tmp_path)
    configurations = _configurations_matching(written)
    stale = function_name_for(ALL_TOOLS[0])
    configurations[stale]["CodeSha256"] = _b64_sha256(b"stale")
    stream = io.StringIO()
    exit_code, _ = deployed_parity.deploy_gate(
        FakeLambdaClient(configurations), directory=tmp_path, stream=stream
    )
    assert exit_code == 1
    assert "FAIL:" in stream.getvalue()
    assert stale in stream.getvalue()


def test_deploy_gate_returns_one_when_no_zips_were_built(tmp_path: Path) -> None:
    stream = io.StringIO()
    exit_code, _ = deployed_parity.deploy_gate(
        FakeLambdaClient({}), directory=tmp_path / "nothing-here", stream=stream
    )
    assert exit_code == 1
    assert "FAIL:" in stream.getvalue()


# ---------------------------------------------------------------------------
# Architecture: this check must never enter the offline, credential-free gate suite.
# ---------------------------------------------------------------------------


def test_run_gates_does_not_reference_the_credentialled_parity_check() -> None:
    """`run_gates.py` never references the credentialled parity check."""
    text = (REPO_ROOT / "scripts" / "run_gates.py").read_text(encoding="utf-8")
    assert "deployed_parity" not in text
    assert "get_function_configuration" not in text


def test_deploy_sh_runs_the_gate_after_apply_and_dies_on_failure() -> None:
    """`deploy.sh` must invoke the gate after apply and treat a non-zero exit as fatal."""
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    apply_index = text.index('terraform apply "${TF_APPLY_ARGS[@]}"')
    gate_index = text.index("scripts.smoke.deployed_parity --deploy-gate")
    drift_index = text.index("classify_post_apply_drift.py")
    assert apply_index < gate_index < drift_index
    assert "post-apply deployed<->zip parity FAILED" in text


def test_deploy_sh_fails_legibly_when_the_gate_interpreter_cannot_import_boto3() -> None:
    """`deploy.sh` probes for boto3 before `terraform apply` and dies with an actionable message."""
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    probe_index = text.index("\"$ZIP_CHECK_PYTHON\" -c 'import boto3'")
    apply_index = text.index('terraform apply "${TF_APPLY_ARGS[@]}"')
    assert probe_index < apply_index
    assert "cannot import boto3" in text
    assert "nothing was applied" in text
    assert "pip install -r" in text


def test_the_boto3_probe_command_actually_detects_a_missing_boto3(tmp_path: Path) -> None:
    """The `import boto3` probe succeeds with boto3 importable and fails without it."""
    probe = ["-c", "import boto3"]
    present = proc_run.run([sys.executable, *probe], capture_output=True, check=False)
    assert present.returncode == 0, present.stderr

    # `-S -I` with an empty search path cannot import boto3.
    absent = proc_run.run(
        [sys.executable, "-S", "-I", *probe],
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        check=False,
    )
    assert absent.returncode != 0
    assert b"boto3" in absent.stderr


def test_orchestrator_registers_the_module_in_layer_4() -> None:
    text = (REPO_ROOT / "scripts" / "verify_deployment.sh").read_text(encoding="utf-8")
    layer_4 = text.split("LAYER_4_MODULES=(", 1)[1].split(")", 1)[0]
    assert "scripts.smoke.deployed_parity" in layer_4
