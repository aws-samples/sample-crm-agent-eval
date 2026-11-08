# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.harness`."""

from __future__ import annotations

import ast
import http.client
import io
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from scripts.smoke import harness
from scripts.smoke.harness import (
    COVERAGE_CHECK,
    DEFAULT_PROFILE,
    DEFAULT_REGION,
    DEFER,
    UNKNOWN,
    CheckResult,
    LayerStep,
    MissingTerraformOutputError,
    Provenance,
    ResourceClassResult,
    Status,
    build_parser,
    context_for_test,
    context_from_args,
    emit,
    load_outputs,
    outputs_path,
    resolve_profile,
    resolve_region,
    run_layers,
    run_main,
)

from eval import verification

# ---------------------------------------------------------------------------
# resolve_profile / resolve_region
# ---------------------------------------------------------------------------


def test_resolve_profile_flag_wins_over_env() -> None:
    assert resolve_profile("flag-profile", env={"AWS_PROFILE": "env-profile"}) == "flag-profile"


def test_resolve_profile_env_wins_over_default() -> None:
    assert resolve_profile(None, env={"AWS_PROFILE": "env-profile"}) == "env-profile"


def test_resolve_profile_default_when_all_omitted() -> None:
    assert resolve_profile(None, env={}) == DEFAULT_PROFILE


def test_resolve_profile_default_module_constant() -> None:
    assert DEFAULT_PROFILE == "default"


def test_resolve_region_flag_wins_over_env() -> None:
    assert resolve_region("us-west-2", env={"AWS_REGION": "eu-west-1"}) == "us-west-2"


def test_resolve_region_env_wins_over_default() -> None:
    assert resolve_region(None, env={"AWS_REGION": "eu-west-1"}) == "eu-west-1"


def test_resolve_region_default_when_all_omitted() -> None:
    assert resolve_region(None, env={}) == DEFAULT_REGION


def test_resolve_region_default_module_constant() -> None:
    assert DEFAULT_REGION == "us-east-1"


def test_resolve_profile_does_not_read_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolver takes `env` explicitly rather than mutating/reading `os.environ`."""
    monkeypatch.setenv("AWS_PROFILE", "should-not-be-used")
    assert resolve_profile(None, env={}) == DEFAULT_PROFILE


# ---------------------------------------------------------------------------
# load_outputs / TerraformOutputs / MissingTerraformOutputError
# ---------------------------------------------------------------------------


def test_load_outputs_terraform_json_shape(tmp_path: Path) -> None:
    path = tmp_path / "outputs.json"
    path.write_text(
        json.dumps({"api_endpoint": {"value": "https://example.test", "type": "string"}})
    )
    outputs = load_outputs(path)
    assert outputs.require_str("api_endpoint") == "https://example.test"


def test_load_outputs_flattened_shape(tmp_path: Path) -> None:
    path = tmp_path / "outputs.json"
    path.write_text(json.dumps({"api_endpoint": "https://example.test"}))
    outputs = load_outputs(path)
    assert outputs.require_str("api_endpoint") == "https://example.test"


def test_load_outputs_missing_file_raises_clear_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(RuntimeError, match="not found"):
        load_outputs(missing)


def test_load_outputs_malformed_json_raises_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "outputs.json"
    path.write_text("{not valid json")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        load_outputs(path)


def test_load_outputs_non_object_raises_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "outputs.json"
    path.write_text(json.dumps(["not", "an", "object"]))
    with pytest.raises(RuntimeError, match="JSON object"):
        load_outputs(path)


def test_require_missing_output_names_it_and_lists_available() -> None:
    outputs = context_for_test(outputs={"api_endpoint": "https://x"}).outputs
    with pytest.raises(MissingTerraformOutputError) as exc_info:
        outputs.require("agentcore_gateway_url")
    message = str(exc_info.value)
    assert "agentcore_gateway_url" in message
    assert "api_endpoint" in message


def test_require_is_never_a_bare_keyerror() -> None:
    outputs = context_for_test(outputs={}).outputs
    with pytest.raises(MissingTerraformOutputError):
        outputs.require("cognito_user_pool_id")


def test_require_str_returns_string() -> None:
    outputs = context_for_test(outputs={"cognito_user_pool_id": "us-east-1_abc123"}).outputs
    assert outputs.require_str("cognito_user_pool_id") == "us-east-1_abc123"


def test_require_mapping_returns_mapping() -> None:
    outputs = context_for_test(
        outputs={"dynamodb_table_names": {"accounts": "accounts-table"}}
    ).outputs
    assert outputs.require_mapping("dynamodb_table_names")["accounts"] == "accounts-table"


def test_require_mapping_raises_when_not_a_mapping() -> None:
    outputs = context_for_test(outputs={"api_endpoint": "https://x"}).outputs
    with pytest.raises(MissingTerraformOutputError):
        outputs.require_mapping("api_endpoint")


def test_get_returns_default_when_absent() -> None:
    outputs = context_for_test(outputs={}).outputs
    assert outputs.get("missing", "fallback") == "fallback"


def test_outputs_path_flag_wins() -> None:
    assert outputs_path("custom.json", env={"CRM_TF_OUTPUTS": "env.json"}) == Path("custom.json")


def test_outputs_path_env_wins_over_default() -> None:
    assert outputs_path(None, env={"CRM_TF_OUTPUTS": "env.json"}) == Path("env.json")


def test_outputs_path_default_when_all_omitted() -> None:
    assert outputs_path(None, env={}) == Path("results/tf-outputs.json")


def test_load_outputs_never_shells_out_or_calls_aws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`load_outputs` reads the file without launching a subprocess."""
    import subprocess

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("load_outputs must never shell out")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "check_output", _boom)
    path = tmp_path / "outputs.json"
    path.write_text(json.dumps({"api_endpoint": "https://example.test"}))
    load_outputs(path)  # must not raise


# ---------------------------------------------------------------------------
# SmokeContext.client — injection seam
# ---------------------------------------------------------------------------


def test_client_returns_injected_client_without_building_a_session() -> None:
    import boto3

    real_client = boto3.client("iam", region_name="us-east-1")
    stub = Stubber(real_client)
    stub.activate()

    ctx = context_for_test(clients={"iam": real_client})
    returned = ctx.client("iam")
    assert returned is real_client
    stub.deactivate()


def test_client_caches_lazily_built_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no client is injected, a client is lazily built and cached per service."""
    build_calls: list[str] = []

    class _FakeSession:
        def client(self, service_name: str, **kwargs: Any) -> object:
            build_calls.append(service_name)
            return object()

    ctx = context_for_test()
    ctx._session = _FakeSession()
    first = ctx.client("dynamodb")
    second = ctx.client("dynamodb")
    assert first is second
    assert build_calls == ["dynamodb"]


# ---------------------------------------------------------------------------
# ResourceClassResult — pinned JSON shape
# ---------------------------------------------------------------------------


def test_ok_true_when_no_failures() -> None:
    result = ResourceClassResult(
        layer=4,
        resource_class="dynamodb_tables",
        checks=(CheckResult(resource="accounts", check="pitr", status="pass"),),
    )
    assert result.ok is True
    assert result.exit_code() == 0
    assert result.failed == ()


def test_ok_false_when_any_failure() -> None:
    result = ResourceClassResult(
        layer=4,
        resource_class="dynamodb_tables",
        checks=(
            CheckResult(resource="accounts", check="pitr", status="pass"),
            CheckResult(resource="deals", check="pitr", status="fail", detail="disabled"),
        ),
    )
    assert result.ok is False
    assert result.exit_code() == 1
    assert result.failed == (
        CheckResult(resource="deals", check="pitr", status="fail", detail="disabled"),
    )


def test_ok_true_when_only_skip_and_pass() -> None:
    result = ResourceClassResult(
        layer=4,
        resource_class="dynamodb_tables",
        checks=(
            CheckResult(resource="accounts", check="pitr", status="pass"),
            CheckResult(resource="deals", check="pitr", status="skip", detail="not applicable"),
        ),
    )
    assert result.ok is True
    assert result.exit_code() == 0


def test_to_json_dict_exact_shape_and_key_set() -> None:
    # A hand-constructed result carries no provenance, so its top-level shape is three keys.
    result = ResourceClassResult(
        layer=4,
        resource_class="dynamodb_tables",
        checks=(CheckResult(resource="accounts", check="pitr", status="pass", detail="ok"),),
    )
    payload = result.to_json_dict()
    assert set(payload.keys()) == {"layer", "resource_class", "checks"}
    assert payload["layer"] == 4
    assert payload["resource_class"] == "dynamodb_tables"
    assert len(payload["checks"]) == 1
    check_payload = payload["checks"][0]
    assert set(check_payload.keys()) == {"resource", "check", "status", "detail"}
    assert check_payload == {
        "resource": "accounts",
        "check": "pitr",
        "status": "pass",
        "detail": "ok",
    }


def test_to_json_dict_round_trips_through_json_dumps() -> None:
    result = ResourceClassResult(
        layer=5,
        resource_class="api_routes",
        checks=(
            CheckResult(resource="/deals", check="authorizer", status="fail", detail="missing"),
        ),
    )
    round_tripped = json.loads(json.dumps(result.to_json_dict()))
    assert round_tripped == result.to_json_dict()


# ---------------------------------------------------------------------------
# emit
# ---------------------------------------------------------------------------


def test_emit_json_writes_parseable_json_to_given_stream() -> None:
    result = ResourceClassResult(
        layer=4,
        resource_class="iam_roles",
        checks=(CheckResult(resource="role-a", check="allow", status="pass"),),
    )
    stream = io.StringIO()
    emit(result, as_json=True, stream=stream)
    parsed = json.loads(stream.getvalue())
    assert parsed == result.to_json_dict()


def test_emit_human_readable_is_not_json() -> None:
    result = ResourceClassResult(
        layer=4,
        resource_class="iam_roles",
        checks=(CheckResult(resource="role-a", check="allow", status="pass", detail="ok"),),
    )
    stream = io.StringIO()
    emit(result, as_json=False, stream=stream)
    output = stream.getvalue()
    with pytest.raises(json.JSONDecodeError):
        json.loads(output)
    assert "role-a" in output
    assert "allow" in output
    assert "pass" in output


def test_emit_defaults_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    result = ResourceClassResult(
        layer=4,
        resource_class="iam_roles",
        checks=(CheckResult(resource="role-a", check="allow", status="pass"),),
    )
    emit(result, as_json=True)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == result.to_json_dict()


# ---------------------------------------------------------------------------
# build_parser / context_from_args
# ---------------------------------------------------------------------------


def test_build_parser_has_pinned_flags() -> None:
    parser = build_parser("test")
    args = parser.parse_args(
        ["--profile", "p", "--region", "r", "--tenant", "t", "--json", "--outputs", "o.json"]
    )
    assert args.profile == "p"
    assert args.region == "r"
    assert args.tenant == "t"
    assert args.as_json is True
    assert args.outputs == "o.json"


def test_context_from_args_loads_outputs_and_resolves_profile_region(tmp_path: Path) -> None:
    outputs_file = tmp_path / "outputs.json"
    outputs_file.write_text(json.dumps({"api_endpoint": "https://example.test"}))
    parser = build_parser("test")
    args = parser.parse_args(
        ["--profile", "myprofile", "--region", "us-west-2", "--outputs", str(outputs_file)]
    )
    ctx = context_from_args(args)
    assert ctx.profile == "myprofile"
    assert ctx.region == "us-west-2"
    assert ctx.outputs.require_str("api_endpoint") == "https://example.test"


# ---------------------------------------------------------------------------
# context_for_test
# ---------------------------------------------------------------------------


def test_context_for_test_defaults() -> None:
    ctx = context_for_test()
    assert ctx.profile == DEFAULT_PROFILE
    assert ctx.region == DEFAULT_REGION
    assert ctx.tenant is None
    assert ctx.as_json is False


def test_context_for_test_never_builds_a_real_session(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("context_for_test must never build a real boto3 session")

    monkeypatch.setattr(boto3.session, "Session", _boom)
    ctx = context_for_test(clients={"dynamodb": object()})
    ctx.client("dynamodb")  # must not raise; uses the injected client


# ---------------------------------------------------------------------------
# run_main
# ---------------------------------------------------------------------------


def _write_outputs(tmp_path: Path, values: dict[str, Any]) -> Path:
    path = tmp_path / "outputs.json"
    path.write_text(json.dumps(values))
    return path


def test_run_main_returns_zero_when_all_checks_pass(tmp_path: Path) -> None:
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    exit_code = run_main(
        ["--outputs", str(outputs_file)],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    assert exit_code == 0


def test_run_main_returns_one_when_any_check_fails(tmp_path: Path) -> None:
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="fail", detail="bad")]

    exit_code = run_main(
        ["--outputs", str(outputs_file)],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    assert exit_code == 1


def test_run_main_returns_zero_when_only_skip_statuses(tmp_path: Path) -> None:
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="skip", detail="n/a")]

    exit_code = run_main(
        ["--outputs", str(outputs_file)],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    assert exit_code == 0


def test_run_main_catches_missing_terraform_output_error(tmp_path: Path) -> None:
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        ctx.outputs.require("agentcore_gateway_url")
        return []

    exit_code = run_main(
        ["--outputs", str(outputs_file)],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    assert exit_code == 1


def test_run_main_catches_missing_terraform_output_error_emits_actionable_detail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        ctx.outputs.require("agentcore_gateway_url")
        return []

    run_main(
        ["--outputs", str(outputs_file), "--json"],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"][0]["status"] == "fail"
    assert "agentcore_gateway_url" in payload["checks"][0]["detail"]


def test_run_main_catches_client_error(tmp_path: Path) -> None:
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "DescribeTable")

    exit_code = run_main(
        ["--outputs", str(outputs_file)],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    assert exit_code == 1


def test_run_main_json_emits_parseable_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    run_main(
        ["--outputs", str(outputs_file), "--json"],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["resource_class"] == "fake"


def test_run_main_human_readable_is_not_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    run_main(
        ["--outputs", str(outputs_file)],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    output = capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError):
        json.loads(output)


# ---------------------------------------------------------------------------
# run_layers — fail-fast layer runner
# ---------------------------------------------------------------------------


def _make_result(layer: int, resource_class: str, status: Status) -> ResourceClassResult:
    return ResourceClassResult(
        layer=layer,
        resource_class=resource_class,
        checks=(CheckResult(resource=resource_class, check="c", status=status),),
    )


def test_run_layers_layer4_failure_prevents_layer5_and_layer6_from_running() -> None:
    invoked: list[str] = []

    def layer4_step() -> ResourceClassResult:
        invoked.append("layer4")
        return _make_result(4, "layer4", "fail")

    def layer5_step() -> ResourceClassResult:
        invoked.append("layer5")
        return _make_result(5, "layer5", "pass")

    def layer6_step() -> ResourceClassResult:
        invoked.append("layer6")
        return _make_result(6, "layer6", "pass")

    exit_code, results = run_layers(
        [
            LayerStep(layer=4, resource_class="layer4", run=layer4_step),
            LayerStep(layer=5, resource_class="layer5", run=layer5_step),
            LayerStep(layer=6, resource_class="layer6", run=layer6_step),
        ]
    )
    assert exit_code == 1
    assert invoked == ["layer4"]
    assert "layer5" not in invoked
    assert "layer6" not in invoked


def test_run_layers_failing_step_does_not_abort_rest_of_its_own_layer() -> None:
    invoked: list[str] = []

    def failing_step() -> ResourceClassResult:
        invoked.append("failing")
        return _make_result(4, "failing", "fail")

    def sibling_step_a() -> ResourceClassResult:
        invoked.append("sibling_a")
        return _make_result(4, "sibling_a", "pass")

    def sibling_step_b() -> ResourceClassResult:
        invoked.append("sibling_b")
        return _make_result(4, "sibling_b", "pass")

    exit_code, results = run_layers(
        [
            LayerStep(layer=4, resource_class="failing", run=failing_step),
            LayerStep(layer=4, resource_class="sibling_a", run=sibling_step_a),
            LayerStep(layer=4, resource_class="sibling_b", run=sibling_step_b),
        ]
    )
    assert exit_code == 1
    assert set(invoked) == {"failing", "sibling_a", "sibling_b"}
    assert len(results) == 3


def test_run_layers_all_pass_runs_every_layer() -> None:
    invoked: list[str] = []

    def make_step(name: str, layer: int) -> LayerStep:
        def _run() -> ResourceClassResult:
            invoked.append(name)
            return _make_result(layer, name, "pass")

        return LayerStep(layer=layer, resource_class=name, run=_run)

    exit_code, results = run_layers(
        [make_step("layer4", 4), make_step("layer5", 5), make_step("layer6", 6)]
    )
    assert exit_code == 0
    assert invoked == ["layer4", "layer5", "layer6"]
    assert len(results) == 3


def test_run_layers_groups_out_of_order_steps_ascending_by_layer() -> None:
    invoked: list[str] = []

    def make_step(name: str, layer: int) -> LayerStep:
        def _run() -> ResourceClassResult:
            invoked.append(name)
            return _make_result(layer, name, "pass")

        return LayerStep(layer=layer, resource_class=name, run=_run)

    # Supplied out of ascending layer order.
    exit_code, results = run_layers(
        [make_step("layer6", 6), make_step("layer4", 4), make_step("layer5", 5)]
    )
    assert exit_code == 0
    assert invoked == ["layer4", "layer5", "layer6"]


def test_run_layers_empty_steps_passes_trivially() -> None:
    exit_code, results = run_layers([])
    assert exit_code == 0
    assert results == []


def test_run_main_accepts_documented_script_specific_flag_via_extra_arguments(
    tmp_path: Path,
) -> None:
    """A script-specific flag declared via `extra_arguments` does not make the CLI exit 2."""
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    exit_code = run_main(
        ["--token", "XYZ", "--outputs", str(outputs_file)],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
        extra_arguments=["--token"],
    )
    assert exit_code == 0


def test_run_main_undeclared_script_specific_flag_now_rejected(tmp_path: Path) -> None:
    """A script-specific flag not declared via `extra_arguments` is a usage error (exit 2)."""
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    with pytest.raises(SystemExit) as exc_info:
        run_main(
            ["--token", "XYZ", "--outputs", str(outputs_file)],
            layer=4,
            resource_class="fake",
            description="test",
            checks=checks,
        )
    assert exc_info.value.code == 2


def test_run_main_accepts_multiple_documented_script_specific_flags(tmp_path: Path) -> None:
    """Several flags declared via `extra_arguments` are all accepted by the parser."""
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    exit_code = run_main(
        [
            "--token",
            "XYZ",
            "--in-book-account",
            "acct1",
            "--out-of-book-account",
            "acct2",
            "--outputs",
            str(outputs_file),
        ],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
        extra_arguments=["--token", "--in-book-account", "--out-of-book-account"],
    )
    assert exit_code == 0


def test_run_main_typo_of_pinned_flag_still_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    """A typo of a pinned flag (e.g. `--regionn`) is a usage error (exit 2)."""

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    with pytest.raises(SystemExit) as exc_info:
        run_main(
            ["--regionn", "us-west-2"],
            layer=4,
            resource_class="fake",
            description="test",
            checks=checks,
        )
    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "--regionn" in stderr
    assert "--region" in stderr


def test_run_main_extra_arguments_parameter_declares_a_flag_explicitly(tmp_path: Path) -> None:
    """A flag declared through the `extra_arguments` parameter is accepted."""
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    exit_code = run_main(
        ["--widget", "abc", "--outputs", str(outputs_file)],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
        extra_arguments=["--widget"],
    )
    assert exit_code == 0


def test_run_main_missing_outputs_file_does_not_raise_and_exits_one(tmp_path: Path) -> None:
    """A missing outputs file becomes exit 1 with an actionable `CheckResult`, not a traceback."""

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    missing = tmp_path / "does-not-exist.json"
    exit_code = run_main(
        ["--outputs", str(missing)],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    assert exit_code == 1


def test_run_main_missing_outputs_file_emits_one_actionable_line_on_stderr_not_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without `--json`, a missing outputs file is one stderr line and no JSON on stdout."""

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    missing = tmp_path / "does-not-exist.json"
    run_main(
        ["--outputs", str(missing)],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "does-not-exist.json" in captured.err
    assert "not found" in captured.err


def test_run_main_missing_outputs_file_emits_pinned_json_shape_on_stdout_when_json_requested(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With `--json`, a missing outputs file is the pinned JSON object on stdout."""

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    missing = tmp_path / "does-not-exist.json"
    exit_code = run_main(
        ["--outputs", str(missing), "--json"],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["checks"][0]["status"] == "fail"
    assert "does-not-exist.json" in payload["checks"][0]["detail"]
    assert exit_code == 1


def test_run_main_malformed_outputs_json_does_not_raise_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Malformed JSON in the outputs file becomes exit 1, not a traceback."""
    path = tmp_path / "outputs.json"
    path.write_text("{not valid json")

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    exit_code = run_main(
        ["--outputs", str(path)],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    assert exit_code == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_run_layers_multiple_layer4_failures_all_recorded_before_stopping() -> None:
    invoked: list[str] = []

    def make_failing(name: str) -> LayerStep:
        def _run() -> ResourceClassResult:
            invoked.append(name)
            return _make_result(4, name, "fail")

        return LayerStep(layer=4, resource_class=name, run=_run)

    def layer5_step() -> ResourceClassResult:
        invoked.append("layer5")
        return _make_result(5, "layer5", "pass")

    exit_code, results = run_layers(
        [
            make_failing("a"),
            make_failing("b"),
            LayerStep(layer=5, resource_class="layer5", run=layer5_step),
        ]
    )
    assert exit_code == 1
    assert invoked == ["a", "b"]
    assert len(results) == 2


# ---------------------------------------------------------------------------
# run_main — accumulate partial results on any exception
# ---------------------------------------------------------------------------


def test_run_main_no_credentials_error_does_not_escape_as_a_traceback(tmp_path: Path) -> None:
    """`NoCredentialsError` from `checks` becomes a failing check and exit 1, not a traceback."""
    from botocore.exceptions import NoCredentialsError

    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        raise NoCredentialsError

    exit_code = run_main(
        ["--outputs", str(outputs_file)],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    assert exit_code == 1


def test_run_main_no_credentials_error_emits_actionable_detail_not_uncaught(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        from botocore.exceptions import NoCredentialsError

        raise NoCredentialsError

    run_main(
        ["--outputs", str(outputs_file), "--json"],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"][0]["status"] == "fail"
    assert "NoCredentialsError" in payload["checks"][0]["detail"]
    assert "UNCAUGHT" not in payload["checks"][0]["detail"]


def test_run_main_value_error_does_not_escape_as_a_traceback(tmp_path: Path) -> None:
    """An unrelated `ValueError` from `checks` becomes a failing check and exit 1."""
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        raise ValueError("boom")

    exit_code = run_main(
        ["--outputs", str(outputs_file)],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    assert exit_code == 1


def test_run_main_accumulates_partial_results_before_an_unexpected_exception(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Results yielded before an exception are reported alongside the synthesised failure."""
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> Iterable[CheckResult]:
        yield CheckResult(resource="a", check="c", status="pass")
        yield CheckResult(resource="b", check="c", status="pass")
        yield CheckResult(resource="c", check="c", status="fail", detail="bad")
        raise RuntimeError("unexpected mid-stream failure")

    exit_code = run_main(
        ["--outputs", str(outputs_file), "--json"],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    payload = json.loads(capsys.readouterr().out)
    reported_checks = payload["checks"]
    resources = [c["resource"] for c in reported_checks]
    assert "a" in resources
    assert "b" in resources
    assert "c" in resources
    assert any(c["check"] == "checks" and c["status"] == "fail" for c in reported_checks)
    # 3 partial results + the synthesised failure + the coverage check `run_main` always appends.
    assert len(reported_checks) == 5
    assert sum(1 for c in reported_checks if c["check"] == COVERAGE_CHECK) == 1
    assert exit_code == 1


# ---------------------------------------------------------------------------
# run_main / _reject_unrecognized_arguments
# ---------------------------------------------------------------------------


def test_run_main_typo_of_extra_argument_flag_is_rejected_with_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A typo of a declared flag (`--tokenn`) exits 2 with a "did you mean" hint naming it."""

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    with pytest.raises(SystemExit) as exc_info:
        run_main(
            ["--tokenn", "XYZ"],
            layer=4,
            resource_class="fake",
            description="test",
            checks=checks,
            extra_arguments=["--token"],
        )
    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "--tokenn" in stderr
    assert "--token" in stderr


def test_run_main_totally_bogus_flag_now_exits_two_even_without_a_near_miss(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A flag resembling no pinned or declared flag is rejected with exit 2."""

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    with pytest.raises(SystemExit) as exc_info:
        run_main(
            ["--totally-bogus-flag", "x"],
            layer=4,
            resource_class="fake",
            description="test",
            checks=checks,
        )
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# DEFER — a disclosed, unmet-prerequisite gap, distinct from pass and skip.
# ---------------------------------------------------------------------------


def _defer_check(check: str = "configured_tls_minimum") -> CheckResult:
    return CheckResult(
        resource="distribution",
        check=check,
        status=DEFER,
        detail="NOT VERIFIED and NOT ENFORCED",
        defer_reason="missing_custom_acm_certificate",
    )


def test_defer_status_requires_a_machine_readable_reason() -> None:
    with pytest.raises(ValueError, match="requires a machine-readable defer_reason"):
        CheckResult(resource="distribution", check="configured_tls_minimum", status=DEFER)


def test_defer_reason_is_rejected_on_every_non_defer_status() -> None:
    statuses: tuple[Status, ...] = ("pass", "fail", "skip")
    for status in statuses:
        with pytest.raises(ValueError, match="only valid with status"):
            CheckResult(
                resource="distribution",
                check="configured_tls_minimum",
                status=status,
                defer_reason="missing_custom_acm_certificate",
            )


def test_defer_is_recorded_distinctly_from_pass_and_skip_in_the_inventory_json() -> None:
    result = ResourceClassResult(
        layer=4,
        resource_class="cloudfront",
        checks=(
            CheckResult(resource="origin:s3", check="oac", status="pass", detail="ok"),
            CheckResult(resource="distribution", check="probe", status="skip", detail="local"),
            _defer_check(),
        ),
    )
    payload = result.to_json_dict()
    by_check = {c["check"]: c for c in payload["checks"]}

    # A deferred control is neither pass nor skip, and carries its reason.
    assert by_check["configured_tls_minimum"]["status"] == "defer"
    assert by_check["configured_tls_minimum"]["defer_reason"] == "missing_custom_acm_certificate"
    # The pinned shape is unchanged for every other status: no stray defer_reason key.
    assert set(by_check["oac"].keys()) == {"resource", "check", "status", "detail"}
    assert set(by_check["probe"].keys()) == {"resource", "check", "status", "detail"}
    assert json.loads(json.dumps(payload)) == payload


def test_defer_alone_does_not_make_the_run_non_zero_but_is_reported_as_deferred() -> None:
    result = ResourceClassResult(
        layer=4,
        resource_class="cloudfront",
        checks=(
            CheckResult(resource="origin:s3", check="oac", status="pass"),
            _defer_check(),
        ),
    )
    assert result.failed == ()
    assert result.ok is True
    assert result.exit_code() == 0
    assert [c.check for c in result.deferred] == ["configured_tls_minimum"]


def test_defer_does_not_mask_a_fail_on_a_different_check() -> None:
    """A deferred check does not hide another check's failure from `failed` or the exit code."""
    real_failure = CheckResult(
        resource="origin:s3",
        check="direct_origin_request_refused",
        status="fail",
        detail="got 200 - publicly readable origin",
    )
    without_defer = ResourceClassResult(
        layer=4, resource_class="cloudfront", checks=(real_failure,)
    )
    with_defer = ResourceClassResult(
        layer=4,
        resource_class="cloudfront",
        checks=(_defer_check(), real_failure, _defer_check("second_defer")),
    )

    assert without_defer.exit_code() == 1
    assert with_defer.exit_code() == 1
    assert with_defer.ok is False
    assert with_defer.failed == (real_failure,)
    assert len(with_defer.deferred) == 2


def test_run_layers_treats_a_defer_only_layer_as_passing_but_still_stops_on_a_real_fail() -> None:
    invoked: list[str] = []

    def deferring_layer4() -> ResourceClassResult:
        invoked.append("layer4")
        return ResourceClassResult(layer=4, resource_class="cloudfront", checks=(_defer_check(),))

    def layer5() -> ResourceClassResult:
        invoked.append("layer5")
        return _make_result(5, "layer5", "pass")

    exit_code, _results = run_layers(
        [
            LayerStep(layer=4, resource_class="cloudfront", run=deferring_layer4),
            LayerStep(layer=5, resource_class="layer5", run=layer5),
        ]
    )
    assert exit_code == 0
    assert invoked == ["layer4", "layer5"]

    # ...and the same deferred check alongside a genuine failure still halts the next layer.
    invoked.clear()

    def deferring_and_failing_layer4() -> ResourceClassResult:
        invoked.append("layer4")
        return ResourceClassResult(
            layer=4,
            resource_class="cloudfront",
            checks=(
                _defer_check(),
                CheckResult(resource="origin:s3", check="oac", status="fail", detail="public"),
            ),
        )

    exit_code, _results = run_layers(
        [
            LayerStep(layer=4, resource_class="cloudfront", run=deferring_and_failing_layer4),
            LayerStep(layer=5, resource_class="layer5", run=layer5),
        ]
    )
    assert exit_code == 1
    assert invoked == ["layer4"]


def test_emit_human_output_names_the_defer_reason() -> None:
    result = ResourceClassResult(layer=4, resource_class="cloudfront", checks=(_defer_check(),))
    buffer = io.StringIO()
    emit(result, as_json=False, stream=buffer)
    line = buffer.getvalue()
    assert "= defer" in line
    assert "reason=missing_custom_acm_certificate" in line


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

_PROVENANCE_KEYS = {
    "generated_at",
    "git_commit",
    "git_tree_dirty",
    "region",
    "resource_class",
}


def test_provenance_to_json_dict_exact_key_set_and_values() -> None:
    provenance = Provenance(
        generated_at="2026-09-03T18:00:00Z",
        git_commit="0" * 40,
        git_tree_dirty=False,
        region="us-east-1",
        resource_class="iam_roles",
    )
    payload = provenance.to_json_dict()
    assert set(payload.keys()) == _PROVENANCE_KEYS
    assert payload == {
        "generated_at": "2026-09-03T18:00:00Z",
        "git_commit": "0" * 40,
        "git_tree_dirty": False,
        "region": "us-east-1",
        "resource_class": "iam_roles",
    }


def test_provenance_renders_unknown_dirty_state_explicitly() -> None:
    provenance = Provenance(
        generated_at="2026-09-03T18:00:00Z",
        git_commit=UNKNOWN,
        git_tree_dirty=None,
        region="us-east-1",
        resource_class="cloudfront",
    )
    assert provenance.to_json_dict()["git_tree_dirty"] == UNKNOWN
    assert provenance.to_json_dict()["git_commit"] == UNKNOWN


def test_provenance_never_carries_an_account_id_field() -> None:
    # No account id and no profile name may appear in the provenance block.
    payload = Provenance.capture(region="us-east-1", resource_class="iam_roles").to_json_dict()
    for key in payload:
        assert "account" not in key
        assert "profile" not in key


def test_provenance_capture_reads_the_repository_revision() -> None:
    provenance = Provenance.capture(region="eu-west-1", resource_class="dynamodb_tables")
    assert provenance.region == "eu-west-1"
    assert provenance.resource_class == "dynamodb_tables"
    assert provenance.generated_at.endswith("Z")
    # Parsed rather than regex-matched, so this fails on a non-ISO-8601 string.
    assert datetime.fromisoformat(provenance.generated_at.replace("Z", "+00:00")).tzinfo is not None
    assert provenance.git_commit == UNKNOWN or len(provenance.git_commit) == 40
    assert provenance.git_tree_dirty in (True, False, None)


def test_to_json_dict_includes_provenance_when_present() -> None:
    result = ResourceClassResult(
        layer=4,
        resource_class="iam_roles",
        checks=(CheckResult(resource="role-a", check="allow", status="pass"),),
        provenance=Provenance(
            generated_at="2026-09-03T18:00:00Z",
            git_commit="a" * 40,
            git_tree_dirty=True,
            region="us-east-1",
            resource_class="iam_roles",
        ),
    )
    payload = result.to_json_dict()
    assert set(payload.keys()) == {"layer", "resource_class", "provenance", "checks"}
    assert set(payload["provenance"].keys()) == _PROVENANCE_KEYS
    assert payload["provenance"]["git_tree_dirty"] is True


def test_run_main_json_output_carries_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outputs_file = _write_outputs(tmp_path, {"api_endpoint": "https://example.test"})

    def checks(ctx: Any) -> list[CheckResult]:
        return [CheckResult(resource="r", check="c", status="pass")]

    exit_code = run_main(
        ["--outputs", str(outputs_file), "--region", "us-west-2", "--json"],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload.keys()) == {"layer", "resource_class", "provenance", "checks"}
    assert set(payload["provenance"].keys()) == _PROVENANCE_KEYS
    assert payload["provenance"]["region"] == "us-west-2"
    assert payload["provenance"]["resource_class"] == "fake"


def test_run_main_outputs_failure_path_also_carries_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "absent.json"

    def checks(ctx: Any) -> list[CheckResult]:
        return []

    exit_code = run_main(
        ["--outputs", str(missing), "--json"],
        layer=4,
        resource_class="fake",
        description="test",
        checks=checks,
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["provenance"].keys()) == _PROVENANCE_KEYS


def test_git_helpers_are_total_when_git_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patched on `eval.verification`, the module whose globals `git_commit` resolves `_run_git`
    # from.
    monkeypatch.setattr(verification, "_run_git", lambda *_arguments: None)
    assert harness.git_commit() == UNKNOWN
    assert harness.git_tree_dirty() is None
    # The `scripts.smoke.harness` re-export is the same object.
    assert harness.git_commit is verification.git_commit


class TestAnOfflineContextHasNoAws:
    """`context_for_test` never builds a real boto3 client for an un-injected service."""

    def test_an_uninjected_service_raises_instead_of_building_a_real_client(self) -> None:
        ctx = context_for_test(outputs={}, clients={"iam": object()})
        with pytest.raises(harness.UninjectedAwsClientError) as raised:
            ctx.client("dynamodb")
        message = str(raised.value)
        assert "dynamodb" in message, message
        # The message says how to fix it.
        assert "context_for_test" in message and "clients=" in message, message

    def test_an_injected_service_is_still_returned(self) -> None:
        """An injected client is still returned by `client()`."""
        sentinel = object()
        assert context_for_test(outputs={}, clients={"iam": sentinel}).client("iam") is sentinel

    def test_a_fake_session_is_still_allowed_to_serve_uninjected_services(self) -> None:
        """An injected fake `_session` may still lazily build un-injected clients."""

        class _FakeSession:
            def client(self, service_name: str, **kwargs: Any) -> object:
                return object()

        ctx = context_for_test(outputs={})
        ctx._session = _FakeSession()
        assert ctx.client("dynamodb") is ctx.client("dynamodb")

    def test_a_production_context_is_not_affected(self) -> None:
        """Only `context_for_test` sets the offline flag; a production context does not."""
        production = harness.SmokeContext(
            profile="p",
            region="r",
            tenant=None,
            as_json=False,
            outputs=harness.TerraformOutputs(values={}),
        )
        assert production._offline_only is False
        assert context_for_test(outputs={})._offline_only is True


# ---------------------------------------------------------------------------
# Bounded time: client timeouts, deadlines, and the retry predicate.
# ---------------------------------------------------------------------------


class _BoomError(Exception):
    """A non-AWS exception with no `.response` attribute."""


def _times_out(deadline: harness.Deadline) -> CheckResult:
    """A check whose poll loop asks the deadline and never gets to assert anything."""
    deadline.check("waiting for the consumer to observe the message")
    raise AssertionError("unreachable: the deadline above is already expired")


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Invoke")


class TestBoundedClientConfig:
    def test_every_smoke_client_is_built_with_bounded_timeouts_and_attempts(self) -> None:
        """Every client built through the context seam carries bounded timeouts and attempts."""
        config = harness.smoke_client_config()
        assert config.connect_timeout == harness.SMOKE_CONNECT_TIMEOUT_SECONDS
        assert config.read_timeout == harness.SMOKE_READ_TIMEOUT_SECONDS
        assert config.retries["max_attempts"] == harness.SMOKE_MAX_ATTEMPTS

    def test_the_ceilings_are_below_botocores_defaults(self) -> None:
        """The configured ceilings are below botocore's 60s/60s, 3-retry defaults."""
        assert harness.SMOKE_CONNECT_TIMEOUT_SECONDS < 60
        assert harness.SMOKE_READ_TIMEOUT_SECONDS < 60

    def test_the_client_seam_passes_the_config(self) -> None:
        """The client seam passes the bounded config to the client constructor."""
        captured: dict[str, Any] = {}

        class _Session:
            def client(self, service_name: str, **kwargs: Any) -> object:
                captured.update({"service": service_name, **kwargs})
                return object()

        ctx = context_for_test(outputs={})
        ctx._session = _Session()
        ctx.client("lambda")
        assert captured["service"] == "lambda"
        assert captured["config"].read_timeout == harness.SMOKE_READ_TIMEOUT_SECONDS

    def test_the_iam_client_retries_adaptively_with_at_least_ten_attempts(self) -> None:
        """The context's IAM client is built with adaptive retries and >= 10 attempts."""
        captured: dict[str, Any] = {}

        class _Session:
            def client(self, service_name: str, **kwargs: Any) -> object:
                captured[service_name] = kwargs["config"]
                return object()

        ctx = context_for_test(outputs={})
        ctx._session = _Session()
        ctx.client("iam")
        ctx.client("lambda")
        assert captured["iam"].retries == {"max_attempts": 10, "mode": "adaptive"}
        assert captured["iam"].read_timeout == harness.SMOKE_READ_TIMEOUT_SECONDS
        assert captured["lambda"].retries == {
            "max_attempts": harness.SMOKE_MAX_ATTEMPTS,
            "mode": "standard",
        }


class TestDeadline:
    def test_an_expired_deadline_raises_with_its_elapsed_time(self) -> None:
        deadline = harness.Deadline(seconds=0.0)
        with pytest.raises(harness.SmokeTimeoutError) as caught:
            deadline.check("polling for the consumer")
        assert caught.value.elapsed_seconds >= 0.0
        assert "polling for the consumer" in str(caught.value)

    def test_an_unexpired_deadline_does_not_raise(self) -> None:
        harness.Deadline(seconds=30.0).check("still fine")

    def test_remaining_never_goes_negative(self) -> None:
        """`remaining` is never negative."""
        assert harness.Deadline(seconds=0.0).remaining == 0.0

    def test_the_deadline_uses_a_monotonic_clock(self) -> None:
        """The deadline reads a monotonic clock rather than `time.time`."""
        source = Path("scripts/smoke/harness.py").read_text(encoding="utf-8")
        block = source[source.index("class Deadline") : source.index("def call_with_eni_retry")]
        assert "time.monotonic" in block
        assert "time.time()" not in block


class TestEniRetryIsNarrow:
    def test_a_denial_is_never_retried(self) -> None:
        """An `AccessDeniedException` propagates on the first attempt and is never retried."""
        attempts = 0

        def operation() -> None:
            nonlocal attempts
            attempts += 1
            raise _client_error("AccessDeniedException")

        with pytest.raises(ClientError) as caught:
            harness.call_with_eni_retry(
                operation, deadline=harness.Deadline(seconds=30.0), sleeper=lambda _: None
            )
        assert attempts == 1, f"a denial was retried {attempts} times"
        assert harness.error_code(caught.value) == "AccessDeniedException"

    @pytest.mark.parametrize(
        "code",
        [
            "AccessDeniedException",
            "ResourceNotFoundException",
            "ValidationException",
            "ThrottlingException",
            "UnrecognizedClientException",
            "ENINotReady",
            "eninotreadyexception",
        ],
    )
    def test_no_other_error_code_is_retryable(self, code: str) -> None:
        """No other error code, including near-miss spellings, is retryable."""
        assert code not in harness.RETRYABLE_ERROR_CODES

    def test_a_non_aws_exception_propagates_and_does_not_break_the_predicate(self) -> None:
        """A non-AWS exception resolves to error code `""` and propagates."""
        assert harness.error_code(_BoomError("no response attribute")) == ""
        with pytest.raises(_BoomError):
            harness.call_with_eni_retry(
                lambda: (_ for _ in ()).throw(_BoomError("x")),
                deadline=harness.Deadline(seconds=30.0),
                sleeper=lambda _: None,
            )

    def test_the_eni_error_is_retried_and_then_succeeds(self) -> None:
        attempts = 0

        def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _client_error("ENINotReadyException")
            return "ok"

        slept: list[float] = []
        result = harness.call_with_eni_retry(
            operation, deadline=harness.Deadline(seconds=30.0), sleeper=slept.append
        )
        assert result == "ok"
        assert attempts == 2
        assert slept, "the retry did not back off at all"

    def test_a_permanently_unready_eni_stops_at_the_attempt_ceiling(self) -> None:
        """A permanently retryable error stops at the attempt ceiling."""
        attempts = 0

        def operation() -> None:
            nonlocal attempts
            attempts += 1
            raise _client_error("ENINotReadyException")

        with pytest.raises(ClientError):
            harness.call_with_eni_retry(
                operation, deadline=harness.Deadline(seconds=30.0), sleeper=lambda _: None
            )
        assert attempts == harness.SMOKE_MAX_ATTEMPTS

    def test_the_retry_respects_an_expired_deadline(self) -> None:
        """A retryable error is not retried once the deadline has expired."""
        with pytest.raises(harness.SmokeTimeoutError):
            harness.call_with_eni_retry(
                lambda: (_ for _ in ()).throw(_client_error("ENINotReadyException")),
                deadline=harness.Deadline(seconds=0.0),
                sleeper=lambda _: None,
            )


class TestTimedCheckReportsATimeoutAsADistinguishableFailure:
    def test_a_timeout_is_a_failure_and_never_a_defer(self) -> None:
        """A timeout is reported as `fail`, never `defer`."""
        result = harness.timed_check("relay", "invoke", _times_out, timeout_seconds=0.0)
        assert result.status == "fail"
        assert result.status != DEFER
        assert result.defer_reason == ""

    def test_a_timeout_carries_its_elapsed_time_and_is_marked(self) -> None:
        """A timeout's detail carries its elapsed time and the timeout marker."""
        result = harness.timed_check("relay", "invoke", _times_out, timeout_seconds=0.0)
        assert result.detail.startswith(harness.TIMEOUT_DETAIL_PREFIX)
        assert "s" in result.detail
        assert "Nothing was verified" in result.detail

    def test_an_assertion_failure_is_not_marked_as_a_timeout(self) -> None:
        """An assertion failure is a `fail` without the timeout marker."""
        result = harness.timed_check(
            "relay",
            "invoke",
            lambda _: CheckResult(
                resource="relay", check="invoke", status="fail", detail="FunctionError: Unhandled"
            ),
        )
        assert result.status == "fail"
        assert not result.detail.startswith(harness.TIMEOUT_DETAIL_PREFIX)

    def test_a_botocore_timeout_beneath_the_check_is_classified_as_a_timeout(self) -> None:
        class ReadTimeoutError(Exception):
            """Named to match botocore's type, which is how `is_timeout` recognises it."""

        result = harness.timed_check(
            "relay",
            "invoke",
            lambda _: (_ for _ in ()).throw(ReadTimeoutError("no answer")),
        )
        assert result.status == "fail"
        assert result.detail.startswith(harness.TIMEOUT_DETAIL_PREFIX)
        assert "reachability, not" in result.detail

    def test_a_passing_check_is_returned_unchanged(self) -> None:
        expected = CheckResult(resource="relay", check="invoke", status="pass", detail="ok")
        assert harness.timed_check("relay", "invoke", lambda _: expected) is expected

    def test_an_unrelated_exception_is_not_swallowed(self) -> None:
        """An exception that is not a timeout propagates."""
        with pytest.raises(_BoomError):
            harness.timed_check(
                "relay", "invoke", lambda _: (_ for _ in ()).throw(_BoomError("bug"))
            )


# ---------------------------------------------------------------------------
# The asserted target: the resolved region and account must match the outputs.
# ---------------------------------------------------------------------------

#: The documentation placeholder account id (`check_no_account_id` permits exactly this one).
_PLACEHOLDER_ACCOUNT = "123456789012"
#: A different account, derived from the placeholder; `check_no_account_id` permits no second
#: literal.
_OTHER_ACCOUNT = _PLACEHOLDER_ACCOUNT[::-1]


def _outputs_with_arn(region: str, account: str = _PLACEHOLDER_ACCOUNT) -> harness.TerraformOutputs:
    return harness.TerraformOutputs(
        values={
            "agent_relay_role_arn": f"arn:aws:iam::{account}:role/relay",
            "agentcore_runtime_arn": (f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/abc"),
        }
    )


class TestTheTargetIsDerivedFromTheOutputs:
    def test_the_expected_region_comes_from_the_outputs_not_a_literal(self) -> None:
        """The expected region is derived from the outputs' ARNs."""
        assert harness.outputs_regions(_outputs_with_arn("us-east-1")) == frozenset({"us-east-1"})

    def test_a_global_arn_contributes_no_region(self) -> None:
        """A global ARN, with an empty region field, contributes no region."""
        outputs = harness.TerraformOutputs(
            values={"role": f"arn:aws:iam::{_PLACEHOLDER_ACCOUNT}:role/r"}
        )
        assert harness.outputs_regions(outputs) == frozenset()
        assert harness.outputs_accounts(outputs) == frozenset({_PLACEHOLDER_ACCOUNT})

    def test_the_refusal_names_both_the_resolved_and_the_expected_account(self) -> None:
        """The refusal names both the resolved and the expected account id."""
        outputs = _outputs_with_arn("us-east-1")
        assert harness.outputs_accounts(outputs) == frozenset({_PLACEHOLDER_ACCOUNT})
        ctx = context_for_test(
            outputs=dict(outputs.values), clients={"sts": _StsClient(_OTHER_ACCOUNT)}
        )
        with pytest.raises(harness.WrongTargetError) as caught:
            harness.assert_account_matches_outputs(ctx)
        message = str(caught.value)
        assert _OTHER_ACCOUNT in message, (
            "the resolved account is not named — 'mismatch' sends someone hunting"
        )
        assert _PLACEHOLDER_ACCOUNT in message, "the expected account is not named"
        assert "AWS_PROFILE" in message, "the message does not say where the wrong value came from"

    def test_provenance_still_records_no_account_id(self) -> None:
        """`Provenance` still carries no account id."""
        provenance = harness.Provenance.capture(region="us-east-1", resource_class="relay")
        assert _PLACEHOLDER_ACCOUNT not in json.dumps(provenance.to_json_dict())
        assert "account" not in json.dumps(provenance.to_json_dict()).lower()


class _StsClient:
    def __init__(self, account: str) -> None:
        self._account = account

    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": self._account}


class _FakeSession:
    """Stands in for `boto3.session.Session`, the seam a live context builds clients through."""

    def __init__(self, account: str) -> None:
        self._account = account

    def client(self, service_name: str, **_kwargs: Any) -> Any:
        if service_name != "sts":
            raise AssertionError(
                f"the target assertion built an unexpected client: {service_name!r}"
            )
        return _StsClient(self._account)


class _NoSessionAllowed:
    """A session that fails if anything asks it for a client."""

    def client(self, service_name: str, **_kwargs: Any) -> Any:
        raise AssertionError(
            f"a {service_name!r} client was built although the outputs name no account to compare "
            "against, so the call could not have asserted anything"
        )


class TestAWrongRegionStopsTheRunBeforeAnyCheck:
    def test_a_mismatched_region_raises(self) -> None:
        """The common operator error: `AWS_REGION=us-west-2` against a us-east-1 stack."""
        with pytest.raises(harness.WrongTargetError) as caught:
            harness.assert_region_matches_outputs("us-west-2", _outputs_with_arn("us-east-1"))
        message = str(caught.value)
        assert "us-west-2" in message and "us-east-1" in message
        assert "Nothing has been checked" in message
        # The message names where the wrong value came from.
        assert "AWS_REGION" in message

    def test_a_matching_region_passes(self) -> None:
        harness.assert_region_matches_outputs("us-east-1", _outputs_with_arn("us-east-1"))

    def test_outputs_with_no_regional_arn_are_silent(self) -> None:
        """Outputs with no regional ARN raise nothing."""
        harness.assert_region_matches_outputs(
            "us-west-2",
            harness.TerraformOutputs(
                values={"role": f"arn:aws:iam::{_PLACEHOLDER_ACCOUNT}:role/r"}
            ),
        )

    def test_the_cli_path_asserts_and_the_test_path_does_not(self, tmp_path: Path) -> None:
        """`context_from_args` asserts the target region; `context_for_test` does not."""
        outputs_file = tmp_path / "tf-outputs.json"
        outputs_file.write_text(
            json.dumps(dict(_outputs_with_arn("us-east-1").values)), encoding="utf-8"
        )
        args = build_parser("t").parse_args(
            ["--outputs", str(outputs_file), "--region", "us-west-2", "--profile", "p"]
        )
        with pytest.raises(harness.WrongTargetError):
            harness.context_from_args(args)

        # Same outputs, correct region: constructs normally.
        good = build_parser("t").parse_args(
            ["--outputs", str(outputs_file), "--region", "us-east-1", "--profile", "p"]
        )
        assert harness.context_from_args(good).region == "us-east-1"

        # And the offline path is unaffected, however wrong its region looks.
        assert context_for_test(outputs={}, region="us-west-2").region == "us-west-2"


class TestAWrongAccountStopsTheRun:
    def test_a_mismatched_account_raises(self) -> None:
        ctx = context_for_test(
            outputs=dict(_outputs_with_arn("us-east-1").values),
            clients={"sts": _StsClient(_OTHER_ACCOUNT)},
        )
        with pytest.raises(harness.WrongTargetError):
            harness.assert_account_matches_outputs(ctx)

    def test_a_matching_account_passes(self) -> None:
        ctx = context_for_test(
            outputs=dict(_outputs_with_arn("us-east-1").values),
            clients={"sts": _StsClient(_PLACEHOLDER_ACCOUNT)},
        )
        harness.assert_account_matches_outputs(ctx)

    def test_outputs_with_no_account_are_silent_and_make_no_sts_call(self) -> None:
        """Outputs naming no account raise nothing and make no STS call."""

        class _Exploding:
            def get_caller_identity(self) -> dict[str, str]:
                raise AssertionError("sts must not be called when the outputs name no account")

        ctx = context_for_test(outputs={"name": "plain-string"}, clients={"sts": _Exploding()})
        harness.assert_account_matches_outputs(ctx)


class TestAWrongTargetIsReportedUnderItsOwnCheckName:
    """A target mismatch is reported under the `target` check, not as `load_outputs`."""

    def _run(self, tmp_path: Path, region: str) -> tuple[int, str, str]:
        outputs_file = tmp_path / "tf-outputs.json"
        outputs_file.write_text(
            json.dumps(dict(_outputs_with_arn("us-east-1").values)), encoding="utf-8"
        )

        # One passing check, since `coverage_is_non_vacuous` fails a class emitting nothing.
        def _one_check(_ctx: Any) -> Iterable[CheckResult]:
            yield CheckResult(resource="relay", check="present", status="pass")

        out, err = io.StringIO(), io.StringIO()
        original_out, original_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = harness.run_main(
                description="t",
                layer=4,
                resource_class="relay",
                checks=_one_check,
                argv=[
                    "--outputs",
                    str(outputs_file),
                    "--region",
                    region,
                    "--profile",
                    "p",
                    "--json",
                ],
            )
        finally:
            sys.stdout, sys.stderr = original_out, original_err
        # The inventory is the stdout half; stderr may carry the target-assertion warning.
        return code, out.getvalue(), err.getvalue()

    def test_a_wrong_region_reports_check_target_and_exits_non_zero(self, tmp_path: Path) -> None:
        """A wrong region appears in the JSON inventory as check `target` and exits non-zero."""
        code, output, _stderr = self._run(tmp_path, "us-west-2")
        assert code != 0, "a wrong-target run must not exit 0"
        names = {check["check"] for check in json.loads(output)["checks"]}
        assert "target" in names, f"the mismatch is not reported under its own check name: {names}"
        assert "load_outputs" not in names, (
            "a target mismatch reported as `load_outputs` points the reader at the one file that is "
            "correct in that scenario — the outputs describe the deployment faithfully"
        )

    def test_a_correct_region_still_runs_the_checks(self, tmp_path: Path) -> None:
        """A correct region still runs the checks."""
        code, output, stderr = self._run(tmp_path, "us-east-1")
        assert code == 0, f"a correctly-targeted run must not fail: {output}{stderr}"
        assert "target" not in {check["check"] for check in json.loads(output)["checks"]}


class TestTheSeamForNonArgparseCallers:
    """`smoke_context`, the entry point `scripts/verify_deployment.sh`'s tenant snippets use."""

    def _outputs_file(self, tmp_path: Path, region: str) -> Path:
        path = tmp_path / "tf-outputs.json"
        path.write_text(json.dumps(dict(_outputs_with_arn(region).values)), encoding="utf-8")
        return path

    def test_a_mismatched_region_refuses_before_a_client_exists(self, tmp_path: Path) -> None:
        with pytest.raises(harness.WrongTargetError) as caught:
            harness.smoke_context(
                profile="p",
                region="us-west-2",
                outputs=self._outputs_file(tmp_path, "us-east-1"),
            )
        assert "Nothing has been checked" in str(caught.value)

    def test_a_matching_region_builds_a_usable_context(self, tmp_path: Path) -> None:
        """A matching region builds a usable context."""
        ctx = harness.smoke_context(
            profile="p", region="us-east-1", outputs=self._outputs_file(tmp_path, "us-east-1")
        )
        assert ctx.region == "us-east-1"
        assert ctx.profile == "p"
        assert ctx.outputs.values, "the outputs must be loaded, not left empty"

    def test_it_resolves_region_from_the_environment_like_the_cli_seam(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`smoke_context` resolves the region from the environment as the CLI seam does."""
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        with pytest.raises(harness.WrongTargetError):
            harness.smoke_context(
                profile="p", region=None, outputs=self._outputs_file(tmp_path, "us-east-1")
            )


class TestTheTenantPathsDoNotBypassTheHarness:
    """The tenant snippets in `scripts/verify_deployment.sh` build their context via
    `smoke_context`.
    """

    SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_deployment.sh"

    def _snippet(self, function_name: str) -> str:
        """The function's body with whole-line comments dropped; trailing comments are kept."""
        text = self.SCRIPT.read_text(encoding="utf-8")
        start = text.index(f"{function_name}() {{")
        end = text.index("\n}\n", start)
        lines = text[start:end].splitlines()
        code = [line for line in lines if not line.lstrip().startswith("#")]
        assert len(code) < len(lines), (
            f"{function_name}: no comment was stripped — check the parser"
        )
        assert code, f"{function_name}: nothing but comments — the snippet was not located"
        return "\n".join(code)

    @pytest.mark.parametrize("function_name", ["provision_tenant", "teardown_tenant"])
    def test_the_snippet_goes_through_the_seam(self, function_name: str) -> None:
        snippet = self._snippet(function_name)
        assert "smoke_context" in snippet, (
            f"{function_name} no longer builds its context through the harness seam, so its DynamoDB "
            "client has neither the wrong-target refusal nor the bounded timeouts"
        )
        assert "boto3" not in snippet, (
            f"{function_name} constructs its own boto3 session again — that is the bypass this "
            "contract exists to catch"
        )

    def test_teardown_does_not_take_the_live_account_assertion(self) -> None:
        """The teardown snippet does not take the live account assertion."""
        assert "assert_account_matches_outputs" in self._snippet("provision_tenant")
        assert "assert_account_matches_outputs" not in self._snippet("teardown_tenant")

    def test_the_contract_rejects_the_pre_fix_source(self, tmp_path: Path) -> None:
        """The checks reject a snippet that builds its own session instead of `smoke_context`."""
        script = tmp_path / "verify_deployment.sh"
        script.write_text(
            "provision_tenant() {\n"
            "  # a comment, so the strip has something to remove\n"
            "  session = boto3.session.Session(profile_name=profile, region_name=region)\n"
            "}\n",
            encoding="utf-8",
        )
        probe = type(self)()
        probe.SCRIPT = script
        with pytest.raises(AssertionError, match="harness seam"):
            probe.test_the_snippet_goes_through_the_seam("provision_tenant")


class TestTheAccountHalfIsAssertedByTheSharedEntryPath:
    """`run_main` asserts the resolved account against the outputs for direct module runs.

    `scripts/verify_deployment.sh` makes the same assertion itself; these cells drive the real
    `run_main` with a fake `boto3.session.Session`.
    """

    def _outputs_file(self, tmp_path: Path, *, with_arn: bool) -> Path:
        path = tmp_path / "tf-outputs.json"
        values = (
            dict(_outputs_with_arn("us-east-1").values)
            if with_arn
            else {"api_endpoint": "https://example.test"}
        )
        path.write_text(json.dumps(values), encoding="utf-8")
        return path

    def _run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        with_arn: bool = True,
        session_factory: Any = None,
    ) -> tuple[int, str, str]:
        if session_factory is not None:
            # Patched on `boto3.session` itself, which is the object `harness` holds a reference to.
            import boto3.session

            monkeypatch.setattr(boto3.session, "Session", session_factory)

        def _one_check(_ctx: Any) -> Iterable[CheckResult]:
            yield CheckResult(resource="r", check="present", status="pass")

        out, err = io.StringIO(), io.StringIO()
        original_out, original_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = harness.run_main(
                [
                    "--outputs",
                    str(self._outputs_file(tmp_path, with_arn=with_arn)),
                    "--region",
                    "us-east-1",
                    "--profile",
                    "p",
                    "--json",
                ],
                layer=4,
                resource_class="r",
                description="t",
                checks=_one_check,
            )
        finally:
            sys.stdout, sys.stderr = original_out, original_err
        return code, out.getvalue(), err.getvalue()

    def test_a_mismatched_account_refuses_before_any_check_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mismatched account is refused by `run_main` before any check runs."""
        code, stdout, _stderr = self._run(
            tmp_path, monkeypatch, session_factory=lambda **_kw: _FakeSession(_OTHER_ACCOUNT)
        )

        assert code != 0, "a wrong-account run must not exit 0"
        names = {check["check"] for check in json.loads(stdout)["checks"]}
        assert "target" in names, f"the mismatch is not reported under `target`: {names}"
        assert "present" not in names, (
            "a check ran despite the wrong-account refusal, so the refusal is not before the first "
            f"AWS call a module makes: {names}"
        )

    def test_a_matching_account_runs_the_checks_and_warns_about_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A matching account runs the checks with no warning."""
        code, stdout, stderr = self._run(
            tmp_path, monkeypatch, session_factory=lambda **_kw: _FakeSession(_PLACEHOLDER_ACCOUNT)
        )

        assert code == 0, f"a correctly-targeted run must not fail: {stdout}{stderr}"
        names = {check["check"] for check in json.loads(stdout)["checks"]}
        assert "present" in names, f"the checks did not run: {names}"
        assert "target" not in names
        assert harness.TARGET_UNASSERTED_WARNING_PREFIX not in stderr, (
            f"an assertion that was made still warned that it was not: {stderr}"
        )

    def test_an_absent_account_expectation_warns_loudly_and_never_passes_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Outputs naming no account proceed with a loud warning, as `scripts/deploy.sh` does."""
        code, stdout, stderr = self._run(
            tmp_path,
            monkeypatch,
            with_arn=False,
            session_factory=lambda **_kw: _NoSessionAllowed(),
        )

        assert code == 0, f"an unasserted-target run still runs its checks: {stdout}{stderr}"
        assert harness.TARGET_UNASSERTED_WARNING_PREFIX in stderr, (
            f"no expectation was derivable and nothing said so: {stderr!r}"
        )
        # The region and account halves are named separately.
        assert "no account" in stderr, f"the account half is not named as unasserted: {stderr!r}"
        assert "no region" in stderr, f"the region half is not named as unasserted: {stderr!r}"
        # `_NoSessionAllowed` raises if the absent-expectation path reaches AWS.
        assert "AssertionError" not in stderr, (
            f"a session was built for an expectation that does not exist: {stderr!r}"
        )

    def test_an_unusable_credential_warns_rather_than_reporting_a_wrong_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unusable credential produces a warning rather than a `target` failure."""

        def _explode(**_kw: Any) -> Any:
            raise RuntimeError("ProfileNotFound: the config profile (p) could not be found")

        code, stdout, stderr = self._run(tmp_path, monkeypatch, session_factory=_explode)

        names = {check["check"] for check in json.loads(stdout)["checks"]}
        assert "target" not in names, (
            f"an unusable credential was reported as a wrong target: {names}"
        )
        assert harness.TARGET_UNASSERTED_WARNING_PREFIX in stderr, (
            f"the account half was skipped with no warning: {stderr!r}"
        )
        assert "could not be checked" in stderr, stderr
        assert code == 0, "the fixture's one check still passes; only the account half was skipped"


# ---------------------------------------------------------------------------
# open_https: the https-only transport every smoke HTTP call goes through.
# ---------------------------------------------------------------------------

_NON_HTTPS_URLS: tuple[str, ...] = (
    "http://example.invalid/api",
    "HTTP://example.invalid/api",
    "file:///etc/hosts",
    "ftp://example.invalid/file",
    "data:,payload",
)


def _refuse_any_open(*args: object, **kwargs: object) -> object:
    raise AssertionError(f"the opener was reached: {args!r} {kwargs!r}")


@pytest.mark.parametrize("url", _NON_HTTPS_URLS)
def test_open_https_refuses_a_non_https_scheme_before_reaching_the_opener(url: str) -> None:
    request = urllib.request.Request(url)
    with (
        mock.patch.object(harness._HTTPS_OPENER, "open", _refuse_any_open),
        pytest.raises(harness.InsecureSchemeError, match="https-only"),
    ):
        harness.open_https(request, timeout=1.0)


@pytest.mark.parametrize("url", ["https://example.invalid/api", "HTTPS://example.invalid/api"])
def test_open_https_hands_an_https_request_to_the_opener(url: str) -> None:
    sent: list[tuple[urllib.request.Request, float]] = []
    response = object()

    def _record(request: urllib.request.Request, *, timeout: float) -> object:
        sent.append((request, timeout))
        return response

    request = urllib.request.Request(url)
    with mock.patch.object(harness._HTTPS_OPENER, "open", _record):
        assert harness.open_https(request, timeout=7.5) is response
    assert sent == [(request, 7.5)]


@pytest.mark.parametrize("url", ["http://example.invalid/", "file:///etc/hosts", "data:,x"])
def test_the_https_only_opener_has_no_transport_for_another_scheme(url: str) -> None:
    scheme = url.split(":", 1)[0]
    with pytest.raises(urllib.error.URLError) as excinfo:
        harness.https_only_opener().open(urllib.request.Request(url), timeout=1.0)
    assert excinfo.value.reason == f"unknown url type: {scheme}"


def test_the_default_opener_control_does_open_a_scheme_the_https_only_opener_refuses() -> None:
    """Control: `urlopen`'s default handler set serves `data:`, so the refusal above is the opener's."""
    with urllib.request.build_opener().open("data:,x", timeout=1.0) as response:
        assert response.read() == b"x"


def test_a_redirect_that_leaves_https_is_refused_rather_than_followed() -> None:
    opener = harness.https_only_opener()
    handlers = opener.handlers  # type: ignore[attr-defined]
    redirect = next(h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler))
    request = urllib.request.Request("https://example.invalid/start")
    request.timeout = 1.0
    headers = http.client.HTTPMessage()
    headers["Location"] = "http://example.invalid/downgraded"
    with pytest.raises(urllib.error.URLError) as excinfo:
        redirect.http_error_302(request, io.BytesIO(b""), 302, "Found", headers)
    assert excinfo.value.reason == "unknown url type: http"


def _default_callers() -> list[tuple[str, Callable[[str], object]]]:
    from scripts.smoke import (
        api_routes,
        cloudfront,
        gateway_tools,
        integration_proposal_chain,
        integration_ui_api_ddb,
    )

    return [
        ("api_routes", lambda url: api_routes.default_http_caller("GET", url, {}, "")),
        ("cloudfront.get", cloudfront.default_http_caller),
        ("cloudfront.method", lambda url: cloudfront.default_http_method_caller(url, "POST")),
        (
            "gateway_tools",
            lambda url: gateway_tools.default_mcp_caller("tools/list", {}, None, gateway_url=url),
        ),
        (
            "integration_proposal_chain",
            lambda url: integration_proposal_chain.default_http_caller("GET", url, {}, ""),
        ),
        (
            "integration_ui_api_ddb",
            lambda url: integration_ui_api_ddb.default_http_caller("GET", url, {}, ""),
        ),
    ]


@pytest.mark.parametrize(
    ("name", "caller"), [pytest.param(n, c, id=n) for n, c in _default_callers()]
)
def test_every_default_http_caller_raises_on_http_rather_than_reporting_a_transport_result(
    name: str, caller: Callable[[str], object]
) -> None:
    with (
        mock.patch.object(harness._HTTPS_OPENER, "open", _refuse_any_open),
        pytest.raises(harness.InsecureSchemeError),
    ):
        caller("http://example.invalid/api")


def _direct_urlopen_calls(source: str) -> list[int]:
    """Line numbers of every `urlopen`/`build_opener` call in `source`."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in {"urlopen", "build_opener"}:
            lines.append(node.lineno)
    return lines


def test_no_smoke_module_opens_a_url_except_through_open_https() -> None:
    scripts_dir = Path(harness.__file__).resolve().parents[1]
    modules = sorted((scripts_dir / "smoke").glob("*.py")) + sorted(scripts_dir.glob("smoke_*.py"))
    assert len(modules) > 20, f"the smoke module scan found too few files: {modules}"
    offenders = {
        str(path.relative_to(scripts_dir)): calls
        for path in modules
        if (calls := _direct_urlopen_calls(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, f"smoke modules bypassing open_https: {offenders}"


def test_the_direct_urlopen_scan_control_finds_a_planted_call() -> None:
    planted = "import urllib.request\nurllib.request.urlopen('https://x', timeout=1)\n"
    assert _direct_urlopen_calls(planted) == [2]
    assert _direct_urlopen_calls("from urllib.request import urlopen\nurlopen('x')\n") == [2]


def test_is_timeout_follows_the_cause_chain_of_a_re_raised_transport_error() -> None:
    """A taxonomy error raised `from` a botocore transport error is still a timeout."""

    class EndpointConnectionError(Exception):
        """Named to match botocore's type, which is how `is_timeout` recognises it."""

    class StoreBusyError(Exception):
        """Stands in for the store layer's retryable taxonomy error."""

    def _raise() -> None:
        try:
            raise EndpointConnectionError("unreachable")
        except EndpointConnectionError as exc:
            raise StoreBusyError("busy") from exc

    with pytest.raises(StoreBusyError) as raised:
        _raise()
    assert harness.is_timeout(raised.value) is True
    assert harness.is_timeout(StoreBusyError("busy")) is False
