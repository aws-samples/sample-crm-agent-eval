# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.eni_function_code_deny`.

The Lambda and IAM clients are fakes and time is injected, so no function is created and nothing
waits. The zipped probe handler is imported from a temp file and run against a fake EC2 client.
"""

from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from scripts.smoke import eni_function_code_deny as mod
from scripts.smoke.eni_function_code_deny import (
    EXPECTED_PROBE_ERROR,
    PROBE_PREFIX,
    PROBE_SOURCE_FUNCTION,
    XRAY_ACTIONS,
    checks,
    expected_check_count,
)
from scripts.smoke.harness import CheckResult, context_for_test

_ACCOUNT = "123456789012"
_RUN = "run0001"
_PROBE = f"{PROBE_PREFIX}{_RUN}"


def _role(name: str) -> str:
    return f"arn:aws:iam::{_ACCOUNT}:role/{name}-role"


def _function(name: str) -> dict[str, Any]:
    return {
        "FunctionName": name,
        "FunctionArn": f"arn:aws:lambda:us-east-1:{_ACCOUNT}:function:{name}",
        "Role": _role(name),
    }


_VPC = {"SubnetIds": ["subnet-a"], "SecurityGroupIds": ["sg-a"]}

_FUNCTIONS = [
    _function(PROBE_SOURCE_FUNCTION),
    _function("crm-agent-eval-get-account"),
    _function("crm-agent-eval-agent-relay"),
]


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeLambda:
    def __init__(
        self,
        *,
        functions: list[dict[str, Any]] | None = None,
        probe_state: str = "Active",
        payload: Any = None,
        function_error: str | None = None,
        create_raises: bool = False,
        create_lands_anyway: bool = False,
        delete_works: bool = True,
        lifecycle: tuple[str, str] = ("Active", "Successful"),
        probe_vpc: dict[str, list[str]] | None = None,
        source_vpc: dict[str, list[str]] | None = None,
        stale: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._functions = list(_FUNCTIONS if functions is None else functions)
        self._probe_state = probe_state
        self._payload = (
            payload
            if payload is not None
            else {
                "describe_subnets": EXPECTED_PROBE_ERROR,
                "delete_network_interface": EXPECTED_PROBE_ERROR,
            }
        )
        self._function_error = function_error
        self._create_raises = create_raises
        self._create_lands_anyway = create_lands_anyway
        self._delete_works = delete_works
        self._source_vpc = source_vpc if source_vpc is not None else _VPC
        self._probe_vpc = probe_vpc if probe_vpc is not None else self._source_vpc
        #: Earlier probes still in the account, by name, with their tags.
        self.stale = dict(
            stale if stale is not None else {f"{PROBE_PREFIX}stale": {"smoke-run": "x"}}
        )
        self._lifecycle = lifecycle
        self.probe_exists = False
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def list_functions(self, **kwargs: Any) -> dict[str, Any]:
        if not kwargs:
            strays = [_function(name) for name in self.stale]
            return {"Functions": self._functions[:2] + strays, "NextMarker": "page2"}
        return {"Functions": self._functions[2:]}

    def list_tags(self, *, Resource: str) -> dict[str, Any]:  # noqa: N803
        return {"Tags": self.stale[Resource.rsplit(":", 1)[1]]}

    def get_function_configuration(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        if FunctionName == _PROBE:
            return {"State": self._probe_state, "VpcConfig": self._probe_vpc}
        if FunctionName == PROBE_SOURCE_FUNCTION:
            return {
                "Role": _role(PROBE_SOURCE_FUNCTION),
                "VpcConfig": self._source_vpc,
                "State": self._lifecycle[0],
                "LastUpdateStatus": self._lifecycle[1],
            }
        return {"State": self._lifecycle[0], "LastUpdateStatus": self._lifecycle[1]}

    def create_function(self, **kwargs: Any) -> dict[str, Any]:
        if self._create_raises:
            if self._create_lands_anyway:
                self.probe_exists = True
            raise _ClientError("ResourceConflictException")
        self.created.append(kwargs)
        self.probe_exists = True
        return {}

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["FunctionName"] == _PROBE
        response: dict[str, Any] = {
            "StatusCode": 200,
            "Payload": io.BytesIO(json.dumps(self._payload).encode()),
        }
        if self._function_error:
            response["FunctionError"] = self._function_error
        return response

    def delete_function(self, *, FunctionName: str) -> None:  # noqa: N803
        self.deleted.append(FunctionName)
        if FunctionName in self.stale:
            del self.stale[FunctionName]
            return
        if FunctionName != _PROBE or not self.probe_exists:
            raise _ClientError("ResourceNotFoundException")
        if self._delete_works:
            self.probe_exists = False

    def get_function(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        if (FunctionName == _PROBE and self.probe_exists) or FunctionName in self.stale:
            return {}
        raise _ClientError("ResourceNotFoundException")


class _FakeIam:
    def __init__(
        self,
        *,
        deny_missing_for: str | None = None,
        xray_denied: bool = False,
        service_denied: str | None = None,
        drop_from_service: str | None = None,
    ) -> None:
        self._deny_missing_for = deny_missing_for
        self._xray_denied = xray_denied
        self._service_denied = service_denied
        self._drop_from_service = drop_from_service

    def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:
        role = kwargs["PolicySourceArn"]
        code_path = "ContextEntries" in kwargs
        results = []
        for action in kwargs["ActionNames"]:
            if action in XRAY_ACTIONS:
                decision = "explicitDeny" if (self._xray_denied and code_path) else "allowed"
            elif code_path and role != self._deny_missing_for:
                decision = "explicitDeny"
            else:
                decision = "allowed"
            if not code_path and action == self._service_denied:
                decision = "explicitDeny"
            if not code_path and action == self._drop_from_service:
                continue
            results.append({"EvalActionName": action, "EvalDecision": decision})
        return {"EvaluationResults": results}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 10.0
        return self.now


def _run(lambda_client: _FakeLambda, iam: _FakeIam | None = None) -> list[CheckResult]:
    ctx = context_for_test(clients={"lambda": lambda_client, "iam": iam or _FakeIam()})
    return list(checks(ctx, run_id=_RUN, clock=_Clock(), sleeper=lambda _s: None))


def _by(results: list[CheckResult], check: str) -> list[CheckResult]:
    return [r for r in results if r.check == check]


def test_a_healthy_deployment_passes_and_tears_the_probe_down() -> None:
    fake = _FakeLambda()
    results = _run(fake)
    assert {r.status for r in results} == {"pass"}, [r for r in results if r.status != "pass"]
    assert len(_by(results, "function_lifecycle_healthy")) == 3
    assert len(_by(results, "function_code_denied_eni_not_xray")) == 3
    assert fake.deleted == [f"{PROBE_PREFIX}stale", _PROBE]
    assert not fake.probe_exists
    assert not fake.stale


def test_earlier_probes_are_not_treated_as_deployed_functions() -> None:
    results = _run(_FakeLambda())
    assert not any("stale" in r.resource for r in results)


def test_the_probe_uses_the_source_function_role_and_network() -> None:
    fake = _FakeLambda()
    _run(fake)
    (created,) = fake.created
    assert created["FunctionName"] == _PROBE
    assert created["Role"] == _role(PROBE_SOURCE_FUNCTION)
    assert created["VpcConfig"] == {"SubnetIds": ["subnet-a"], "SecurityGroupIds": ["sg-a"]}


def test_a_role_without_the_deny_fails_its_code_path_row() -> None:
    role = _role("crm-agent-eval-get-account")
    results = _run(_FakeLambda(), _FakeIam(deny_missing_for=role))
    failed = [r for r in _by(results, "function_code_denied_eni_not_xray") if r.status == "fail"]
    assert [r.resource for r in failed] == [role]


def test_a_deny_that_reaches_xray_fails() -> None:
    results = _run(_FakeLambda(), _FakeIam(xray_denied=True))
    assert all(r.status == "fail" for r in _by(results, "function_code_denied_eni_not_xray"))


def test_an_unhealthy_function_fails_its_lifecycle_row() -> None:
    results = _run(_FakeLambda(lifecycle=("Active", "Failed")))
    assert all(r.status == "fail" for r in _by(results, "function_lifecycle_healthy"))


@pytest.mark.parametrize(
    "payload,function_error",
    [
        ({"describe_subnets": "Succeeded", "delete_network_interface": EXPECTED_PROBE_ERROR}, None),
        (
            {
                "describe_subnets": EXPECTED_PROBE_ERROR,
                "delete_network_interface": "DryRunOperation",
            },
            None,
        ),
        (
            {
                "describe_subnets": "ConnectTimeoutError",
                "delete_network_interface": "ConnectTimeoutError",
            },
            None,
        ),
        ({"errorType": "Runtime.ImportModuleError"}, "Unhandled"),
    ],
    ids=["describe-allowed", "delete-dry-run-allowed", "unreachable", "function-error"],
)
def test_a_probe_that_was_not_denied_fails_and_is_still_deleted(
    payload: dict[str, Any], function_error: str | None
) -> None:
    fake = _FakeLambda(payload=payload, function_error=function_error)
    results = _run(fake)
    assert _by(results, "probe_code_denied_eni_actions")[0].status == "fail"
    assert _by(results, "probe_torn_down")[0].status == "pass"
    assert not fake.probe_exists


def test_a_probe_that_never_becomes_active_fails_and_is_deleted() -> None:
    fake = _FakeLambda(probe_state="Pending")
    results = _run(fake)
    assert _by(results, "probe_created_active")[0].status == "fail"
    assert not _by(results, "probe_code_denied_eni_actions")
    assert fake.deleted[-1] == _PROBE


def test_a_failed_create_that_created_nothing_still_tears_down_cleanly() -> None:
    fake = _FakeLambda(create_raises=True)
    results = _run(fake)
    assert _by(results, "probe_created_active")[0].status == "fail"
    assert fake.deleted[-1] == _PROBE, "deletion is attempted whenever creation was"
    assert _by(results, "probe_torn_down")[0].status == "pass"


def test_a_create_that_raised_after_landing_is_still_deleted() -> None:
    """A lost response then a retried create's conflict: the function exists and must go."""
    fake = _FakeLambda(create_raises=True, create_lands_anyway=True)
    results = _run(fake)
    assert _by(results, "probe_created_active")[0].status == "fail"
    assert not fake.probe_exists
    assert _by(results, "probe_torn_down")[0].status == "pass"


@pytest.mark.parametrize(
    "source_vpc",
    [
        {"SubnetIds": [], "SecurityGroupIds": ["sg-a"]},
        {"SubnetIds": ["subnet-a"], "SecurityGroupIds": []},
        {},
    ],
    ids=["no-subnets", "no-groups", "no-vpc"],
)
def test_a_source_without_a_vpc_refuses_to_create_a_probe(source_vpc: dict[str, list[str]]) -> None:
    fake = _FakeLambda(source_vpc=source_vpc)
    results = _run(fake)
    assert _by(results, "probe_created_active")[0].status == "fail"
    assert fake.created == []


def test_a_probe_that_lands_outside_the_source_vpc_fails_created_active() -> None:
    fake = _FakeLambda(probe_vpc={"SubnetIds": [], "SecurityGroupIds": []})
    results = _run(fake)
    assert _by(results, "probe_created_active")[0].status == "fail"
    assert not _by(results, "probe_code_denied_eni_actions")
    assert not fake.probe_exists


def test_an_untagged_function_under_the_probe_prefix_is_not_deleted() -> None:
    fake = _FakeLambda(stale={f"{PROBE_PREFIX}foreign": {}})
    results = _run(fake)
    assert _by(results, "stale_probes_absent")[0].status == "fail"
    assert f"{PROBE_PREFIX}foreign" not in fake.deleted


def test_sigterm_becomes_system_exit() -> None:
    with pytest.raises(SystemExit):
        mod._exit_on_sigterm(15, None)


def test_a_probe_left_behind_fails_the_teardown_row() -> None:
    results = _run(_FakeLambda(delete_works=False))
    assert _by(results, "probe_torn_down")[0].status == "fail"


def test_an_empty_function_set_fails() -> None:
    results = _run(_FakeLambda(functions=[]))
    assert _by(results, "function_set_non_empty")[0].status == "fail"


def test_expected_check_count_matches_a_healthy_run() -> None:
    ctx = context_for_test(clients={"lambda": _FakeLambda(), "iam": _FakeIam()})
    assert expected_check_count(ctx).minimum == len(_run(_FakeLambda()))
    assert mod.LAYER == 4


class _ProbeClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeEc2:
    def describe_subnets(self, **_kwargs: Any) -> None:
        raise _ProbeClientError("UnauthorizedOperation")

    def delete_network_interface(self, **kwargs: Any) -> None:
        assert kwargs["DryRun"] is True
        assert kwargs["NetworkInterfaceId"] == "eni-" + "0" * 17
        raise _ProbeClientError("UnauthorizedOperation")


def test_the_probe_handler_reports_each_call_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zipped handler imports, maps a ClientError to its code, and dry-runs the delete."""
    import boto3
    import botocore.exceptions

    archive = zipfile.ZipFile(io.BytesIO(mod._probe_zip()))
    assert archive.namelist() == ["probe.py"]
    probe_path = tmp_path / "probe.py"
    probe_path.write_bytes(archive.read("probe.py"))

    monkeypatch.setattr(botocore.exceptions, "ClientError", _ProbeClientError)
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: _FakeEc2())
    spec = importlib.util.spec_from_file_location("eni_probe_under_test", probe_path)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    assert probe.handler({}, None) == {
        "describe_subnets": "UnauthorizedOperation",
        "delete_network_interface": "UnauthorizedOperation",
    }


def test_a_deny_that_also_blocks_lambdas_own_eni_work_fails_the_service_row() -> None:
    """The service path (no `lambda:SourceFunctionArn`) must stay allowed every ENI action."""
    results = _run(_FakeLambda(), _FakeIam(service_denied="ec2:CreateNetworkInterface"))
    assert all(r.status == "fail" for r in _by(results, "service_path_allowed_eni_and_xray"))


def test_a_short_simulation_result_fails_the_service_row() -> None:
    results = _run(_FakeLambda(), _FakeIam(drop_from_service="xray:PutTraceSegments"))
    assert all(r.status == "fail" for r in _by(results, "service_path_allowed_eni_and_xray"))
