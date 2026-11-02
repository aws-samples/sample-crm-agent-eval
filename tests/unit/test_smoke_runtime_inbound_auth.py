# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.runtime_inbound_auth`: the authorizer-absence check and its guard.

Uses a fake `bedrock-agentcore-control` client and injected outputs; no credentials, no network.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError
from scripts.smoke import runtime_inbound_auth as module
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.runtime_inbound_auth import (
    ACCESS_DENIED_CODE,
    AUTHORIZER_MEMBER,
    CUSTOM_JWT_MEMBER,
    DECLARED_DENY_CHECK,
    GUARD_MEMBERS,
    NOT_RESOURCE_POLICY_DENY,
    OUTSIDE_VPC_BOUND,
    PRINCIPAL_SET_CHECK,
    checks,
    expected_check_count,
    resource_policy_arns,
    runtime_id_from_arn,
)
from scripts.smoke.runtime_network_posture import DeclaredDeny, DeclaredDenyState

_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/crm-agent-eval-abc123"


@pytest.fixture(autouse=True)
def _no_real_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may reach the real `aws` CLI; each injects a runner or fails here."""

    def refuse(argv: Any) -> tuple[int, str, str]:
        raise AssertionError(f"a test reached the real aws CLI: {list(argv)}")

    monkeypatch.setattr(module, "_run_cli", refuse)


#: The declared Deny state a test sees unless it supplies one; never this host's tfvars.
_HOST_DEFAULT = DeclaredDenyState(DeclaredDeny.UNKNOWN, "test: host tfvars not read")


@pytest.fixture(autouse=True)
def _no_host_tfvars(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads this host's tfvars for the declared `aws:SourceVpce` Deny state."""
    monkeypatch.setattr(module, "declared_source_vpce_deny", lambda: _HOST_DEFAULT)


_UNSUPPORTED_ERR = (
    "aws: [ERROR]: An error occurred (ParamValidation): argument operation: Found invalid choice "
    "'get-resource-policy'"
)


class _FakeCli:
    """`aws` CLI stand-in: `responses` maps a resource ARN to `(code, stdout, stderr)`."""

    def __init__(
        self,
        responses: dict[str, tuple[int, str, str]] | None = None,
        *,
        default: tuple[int, str, str] = (252, "", _UNSUPPORTED_ERR),
        absent: bool = False,
    ) -> None:
        self.responses = responses or {}
        self.default = default
        self.absent = absent
        self.calls: list[list[str]] = []

    def __call__(self, argv: Any) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if self.absent:
            raise FileNotFoundError("aws")
        arn = argv[argv.index("--resource-arn") + 1]
        return self.responses.get(arn, self.default)


_OUTPUTS = {"agentcore_runtime_arn": _RUNTIME_ARN}

#: A response shaped like the real resource: the three guard members present, no authorizer member.
_COMPLIANT_RESPONSE: dict[str, Any] = {
    "agentRuntimeArn": _RUNTIME_ARN,
    "agentRuntimeId": "crm-agent-eval-abc123",
    "roleArn": "arn:aws:iam::123456789012:role/crm-agent-eval-runtime-role",
    "networkConfiguration": {"networkMode": "PUBLIC"},
    "status": "READY",
}


class _FakeControlPlane:
    """Minimal `bedrock-agentcore-control` stand-in returning one canned `GetAgentRuntime` response."""

    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def get_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


_OPERATOR_ARN = "arn:aws:sts::123456789012:assumed-role/OperatorAdmin/smoke-session"


class _FakeSts:
    def __init__(self, arn: str = _OPERATOR_ARN) -> None:
        self.arn = arn

    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": "123456789012", "Arn": self.arn}


class _FakeBody:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


#: The real shape of a refusal by an explicit deny in the Runtime's resource-based policy.
_EXPLICIT_RESOURCE_DENY = (
    f"User: {_OPERATOR_ARN} is not authorized to perform: bedrock-agentcore:InvokeAgentRuntime on "
    f"resource: {_RUNTIME_ARN} with an explicit deny in a resource-based policy"
)

#: The real shape of an implicit deny: the caller holds no identity grant for the action.
_IMPLICIT_DENY = (
    f"User: {_OPERATOR_ARN} is not authorized to perform: bedrock-agentcore:InvokeAgentRuntime on "
    f"resource: {_RUNTIME_ARN} because no identity-based policy allows the "
    "bedrock-agentcore:InvokeAgentRuntime action"
)


class _FakeDataPlane:
    """`bedrock-agentcore` stand-in: refuses with `code` and `message`, or admits when `code` is
    `None`.
    """

    def __init__(
        self, code: str | None = ACCESS_DENIED_CODE, message: str = _EXPLICIT_RESOURCE_DENY
    ) -> None:
        self.code = code
        self.message = message
        self.calls: list[dict[str, Any]] = []
        self.body = _FakeBody()

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.code is not None:
            raise ClientError(
                {"Error": {"Code": self.code, "Message": self.message}}, "InvokeAgentRuntime"
            )
        return {"response": self.body}


def _run(
    response: dict[str, Any] | Exception,
    *,
    outputs: dict[str, Any] | None = None,
    data_plane: _FakeDataPlane | None = None,
    sts: _FakeSts | None = None,
    cli: _FakeCli | None = None,
    declared: DeclaredDenyState | None = None,
) -> list[CheckResult]:
    ctx = context_for_test(
        outputs=_OUTPUTS if outputs is None else outputs,
        clients={
            "bedrock-agentcore-control": _FakeControlPlane(response),
            "bedrock-agentcore": data_plane or _FakeDataPlane(),
            "sts": sts or _FakeSts(),
        },
    )
    return list(checks(ctx, cli_runner=cli or _FakeCli(), declared_deny=declared))


def _by_check(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {result.check: result for result in results}


class TestTheDerivedCoverageFloor:
    """The derived coverage floor tracks whether the runtime is addressable."""

    def _ctx(self, outputs: dict[str, Any] | None = None) -> Any:
        return context_for_test(
            outputs=_OUTPUTS if outputs is None else outputs,
            clients={"bedrock-agentcore-control": _FakeControlPlane(_COMPLIANT_RESPONSE)},
        )

    def test_an_addressable_runtime_expects_the_guard_verdict_and_premise(self) -> None:
        expectation = expected_check_count(self._ctx())
        assert expectation.minimum == 4
        assert expectation.minimum <= len(_run(_COMPLIANT_RESPONSE))

    def test_an_absent_output_drops_to_one_and_says_why(self) -> None:
        expectation = expected_check_count(self._ctx({}))
        assert expectation.minimum == 1
        assert "could not be read" in expectation.basis

    def test_an_unparseable_arn_drops_to_one_and_says_why(self) -> None:
        expectation = expected_check_count(self._ctx({"agentcore_runtime_arn": "arn:aws:x:y:z:r"}))
        assert expectation.minimum == 1
        assert "no final path segment" in expectation.basis

    def test_the_s1_path_still_emits_two_so_the_floor_does_not_red_it(self) -> None:
        """A present-authorizer response still emits at least the floor's two checks."""
        response = dict(
            _COMPLIANT_RESPONSE, **{AUTHORIZER_MEMBER: {CUSTOM_JWT_MEMBER: {"allowedClients": []}}}
        )
        assert len(_run(response)) >= expected_check_count(self._ctx()).minimum


class TestRuntimeIdParsing:
    def test_the_id_is_the_arns_final_path_segment(self) -> None:
        assert runtime_id_from_arn(_RUNTIME_ARN) == "crm-agent-eval-abc123"

    def test_an_arn_with_no_path_segment_yields_nothing(self) -> None:
        """An ARN with no final path segment parses to an empty id."""
        assert runtime_id_from_arn("arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime") == ""

    def test_an_arn_with_no_path_segment_is_a_failure_not_a_call(self) -> None:
        client = _FakeControlPlane(_COMPLIANT_RESPONSE)
        ctx = context_for_test(
            outputs={"agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime"},
            clients={"bedrock-agentcore-control": client},
        )
        results = list(checks(ctx))
        assert [r.status for r in results] == ["fail"]
        assert results[0].check == "runtime_id_resolves"
        assert client.calls == [], "the API must not be called with an unparseable id"


class TestCompliantDeployment:
    def test_an_absent_authorizer_with_the_guard_satisfied_passes(self) -> None:
        results = _by_check(_run(_COMPLIANT_RESPONSE))
        assert results["response_shape_guard"].status == "pass"
        assert results["no_custom_jwt_authorizer"].status == "pass"

    def test_an_explicitly_empty_authorizer_also_passes(self) -> None:
        """An authorizer member of `{}` or `None` passes like an absent one."""
        empties: tuple[dict[str, Any] | None, ...] = ({}, None)
        for empty in empties:
            response = dict(_COMPLIANT_RESPONSE, **{AUTHORIZER_MEMBER: empty})
            assert _by_check(_run(response))["no_custom_jwt_authorizer"].status == "pass"

    def test_the_whole_response_is_requested_by_id(self) -> None:
        client = _FakeControlPlane(_COMPLIANT_RESPONSE)
        ctx = context_for_test(outputs=_OUTPUTS, clients={"bedrock-agentcore-control": client})
        list(checks(ctx))
        assert client.calls == [{"agentRuntimeId": "crm-agent-eval-abc123"}]


class TestTheNonVacuityGuard:
    """An absent authorizer passes only when the response carries the guard members."""

    @pytest.mark.parametrize("dropped", GUARD_MEMBERS)
    def test_dropping_any_guard_member_fails_the_guard(self, dropped: str) -> None:
        response = {k: v for k, v in _COMPLIANT_RESPONSE.items() if k != dropped}
        results = _by_check(_run(response))
        assert results["response_shape_guard"].status == "fail"
        assert dropped in results["response_shape_guard"].detail

    @pytest.mark.parametrize("dropped", GUARD_MEMBERS)
    def test_a_failed_guard_makes_the_authorizer_verdict_fail_not_pass(self, dropped: str) -> None:
        """A failed guard makes the authorizer verdict `fail`, not `pass`."""
        response = {k: v for k, v in _COMPLIANT_RESPONSE.items() if k != dropped}
        results = _by_check(_run(response))
        assert results["no_custom_jwt_authorizer"].status == "fail"
        assert "not evidence" in results["no_custom_jwt_authorizer"].detail

    def test_an_empty_response_fails_both_rather_than_passing_either(self) -> None:
        """An empty response fails both the guard and the authorizer verdict."""
        results = _by_check(_run({}))
        assert results["response_shape_guard"].status == "fail"
        assert results["no_custom_jwt_authorizer"].status == "fail"

    def test_the_guard_uses_more_than_one_witness(self) -> None:
        """The guard checks at least three response members."""
        assert len(GUARD_MEMBERS) >= 3


class TestTheS1CriticalRealised:
    def test_a_custom_jwt_authorizer_is_a_failure(self) -> None:
        response = dict(
            _COMPLIANT_RESPONSE,
            **{
                AUTHORIZER_MEMBER: {
                    CUSTOM_JWT_MEMBER: {
                        "discoveryUrl": "https://cognito-idp.us-east-1.amazonaws.com/pool/.well-known/openid-configuration",
                        "allowedClients": ["spa-client-id"],
                    }
                }
            },
        )
        results = _by_check(_run(response))
        assert results["no_custom_jwt_authorizer"].status == "fail"
        assert "Severity: Critical" in results["no_custom_jwt_authorizer"].detail

    def test_the_authorizers_values_are_never_echoed_into_the_inventory(self) -> None:
        """The detail names the authorizer's members but never echoes their values."""
        response = dict(
            _COMPLIANT_RESPONSE,
            **{
                AUTHORIZER_MEMBER: {
                    CUSTOM_JWT_MEMBER: {
                        "discoveryUrl": "https://secret-pool.example/.well-known/openid-configuration",
                        "allowedClients": ["super-secret-client"],
                    }
                }
            },
        )
        detail = _by_check(_run(response))["no_custom_jwt_authorizer"].detail
        assert "secret-pool.example" not in detail
        assert "super-secret-client" not in detail
        assert "discoveryUrl" in detail, "the member names are what a reader needs"

    def test_a_present_authorizer_short_circuits_before_reporting_an_absence(self) -> None:
        """Exactly one authorizer verdict is emitted."""
        response = dict(
            _COMPLIANT_RESPONSE, **{AUTHORIZER_MEMBER: {CUSTOM_JWT_MEMBER: {"allowedClients": []}}}
        )
        statuses = [r.check for r in _run(response)]
        assert statuses.count("no_custom_jwt_authorizer") == 1


class TestMissingOutput:
    def test_a_missing_runtime_arn_output_is_a_failure_not_a_skip(self) -> None:
        results = _run(_COMPLIANT_RESPONSE, outputs={})
        assert [(r.check, r.status) for r in results] == [("terraform_output_present", "fail")]
        assert "not a skipped resource class" in results[0].detail


class TestAnUnlistedPrincipalIsDenied:
    """A real `InvokeAgentRuntime` from a principal outside the invoker list is refused."""

    def test_a_denied_unlisted_caller_passes_both_rows(self) -> None:
        data_plane = _FakeDataPlane()
        results = _by_check(_run(_COMPLIANT_RESPONSE, data_plane=data_plane))
        assert results["caller_is_not_a_runtime_invoker"].status == "pass"
        denied = results["unlisted_principal_denied_invoking_runtime"]
        assert denied.status == "pass"
        assert denied.detail.endswith(OUTSIDE_VPC_BOUND)
        assert "not proof the principal list is enforced" in denied.detail
        (call,) = data_plane.calls
        assert call["agentRuntimeArn"] == _RUNTIME_ARN
        assert call["payload"] == b"{}", "an admitted call must reach no model"
        assert len(call["runtimeSessionId"]) >= 33

    def test_an_admitted_unlisted_caller_is_a_critical_failure(self) -> None:
        data_plane = _FakeDataPlane(code=None)
        row = _by_check(_run(_COMPLIANT_RESPONSE, data_plane=data_plane))[
            "unlisted_principal_denied_invoking_runtime"
        ]
        assert row.status == "fail"
        assert "Severity: Critical" in row.detail
        assert data_plane.body.closed

    def test_another_refusal_code_is_not_the_access_denial(self) -> None:
        row = _by_check(
            _run(_COMPLIANT_RESPONSE, data_plane=_FakeDataPlane("ThrottlingException"))
        )["unlisted_principal_denied_invoking_runtime"]
        assert row.status == "fail"
        assert ACCESS_DENIED_CODE in row.detail

    def test_the_passing_row_quotes_the_explicit_deny_message(self) -> None:
        row = _by_check(_run(_COMPLIANT_RESPONSE))["unlisted_principal_denied_invoking_runtime"]
        assert row.status == "pass"
        assert repr(_EXPLICIT_RESOURCE_DENY) in row.detail

    def test_an_implicit_deny_is_a_failure_not_a_pass(self) -> None:
        """A missing identity grant refuses the call before the resource policy is read."""
        row = _by_check(
            _run(
                _COMPLIANT_RESPONSE,
                data_plane=_FakeDataPlane(ACCESS_DENIED_CODE, _IMPLICIT_DENY),
            )
        )["unlisted_principal_denied_invoking_runtime"]
        assert row.status == "fail"
        assert repr(_IMPLICIT_DENY) in row.detail
        assert row.detail.endswith(NOT_RESOURCE_POLICY_DENY)
        assert OUTSIDE_VPC_BOUND not in row.detail

    @pytest.mark.parametrize(
        "message",
        [
            f"User: {_OPERATOR_ARN} is not authorized to perform: "
            f"bedrock-agentcore:InvokeAgentRuntime on resource: {_RUNTIME_ARN} with an explicit "
            "deny in an identity-based policy",
            f"User: {_OPERATOR_ARN} is not authorized to perform: "
            f"bedrock-agentcore:InvokeAgentRuntime on resource: {_RUNTIME_ARN} with an explicit "
            "deny in a service control policy",
            "explicit deny",
        ],
        ids=["identity_policy_deny", "scp_deny", "unattributed_message"],
    )
    def test_an_access_denial_not_from_the_resource_policy_fails(self, message: str) -> None:
        row = _by_check(
            _run(_COMPLIANT_RESPONSE, data_plane=_FakeDataPlane(ACCESS_DENIED_CODE, message))
        )["unlisted_principal_denied_invoking_runtime"]
        assert row.status == "fail"
        assert repr(message) in row.detail
        assert row.detail.endswith(NOT_RESOURCE_POLICY_DENY)

    def test_a_resource_policy_deny_naming_another_runtime_fails(self) -> None:
        other = _EXPLICIT_RESOURCE_DENY.replace(_RUNTIME_ARN, f"{_RUNTIME_ARN[:-6]}zzz999")
        row = _by_check(
            _run(_COMPLIANT_RESPONSE, data_plane=_FakeDataPlane(ACCESS_DENIED_CODE, other))
        )["unlisted_principal_denied_invoking_runtime"]
        assert row.status == "fail"
        assert row.detail.endswith(NOT_RESOURCE_POLICY_DENY)

    def test_a_deny_on_the_default_endpoint_arn_passes(self) -> None:
        endpoint = _EXPLICIT_RESOURCE_DENY.replace(
            _RUNTIME_ARN, f"{_RUNTIME_ARN}/runtime-endpoint/DEFAULT"
        )
        row = _by_check(
            _run(_COMPLIANT_RESPONSE, data_plane=_FakeDataPlane(ACCESS_DENIED_CODE, endpoint))
        )["unlisted_principal_denied_invoking_runtime"]
        assert row.status == "pass"

    def test_a_non_client_error_fails_and_names_its_type(self) -> None:
        class _Raising(_FakeDataPlane):
            def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
                raise TimeoutError("read timed out")

        row = _by_check(_run(_COMPLIANT_RESPONSE, data_plane=_Raising()))[
            "unlisted_principal_denied_invoking_runtime"
        ]
        assert row.status == "fail"
        assert "TimeoutError" in row.detail
        assert "read timed out" in row.detail

    @pytest.mark.parametrize(
        "role", ["crm-agent-eval-agent-relay-role", "crm-agent-eval-chat-target-role"]
    )
    def test_a_caller_that_is_an_invoker_fails_the_premise_and_makes_no_call(
        self, role: str
    ) -> None:
        data_plane = _FakeDataPlane()
        sts = _FakeSts(f"arn:aws:sts::123456789012:assumed-role/{role}/fn")
        results = _by_check(_run(_COMPLIANT_RESPONSE, data_plane=data_plane, sts=sts))
        assert results["caller_is_not_a_runtime_invoker"].status == "fail"
        assert "unlisted_principal_denied_invoking_runtime" not in results
        assert data_plane.calls == []

    def test_the_call_also_runs_on_the_custom_jwt_path(self) -> None:
        response = dict(
            _COMPLIANT_RESPONSE, **{AUTHORIZER_MEMBER: {CUSTOM_JWT_MEMBER: {"allowedClients": []}}}
        )
        results = _by_check(_run(response))
        assert results["unlisted_principal_denied_invoking_runtime"].status == "pass"


_ENABLED = DeclaredDenyState(DeclaredDeny.ENABLED, "test: terraform.tfvars")


class TestTheDeclaredSourceVpceDeny:
    """The outside-VPC invoke is judged against the `aws:SourceVpce` Deny the deployment declares."""

    def test_declared_enabled_and_an_explicit_refusal_passes(self) -> None:
        data_plane = _FakeDataPlane()
        row = _by_check(_run(_COMPLIANT_RESPONSE, data_plane=data_plane, declared=_ENABLED))[
            DECLARED_DENY_CHECK
        ]
        assert row.status == "pass"
        assert "declared enabled (test: terraform.tfvars)" in row.detail
        assert row.detail.endswith(OUTSIDE_VPC_BOUND)
        assert len(data_plane.calls) == 1, "the verdict reuses the one unlisted-principal invoke"

    def test_declared_enabled_and_an_admitted_invoke_fails(self) -> None:
        data_plane = _FakeDataPlane(code=None)
        row = _by_check(_run(_COMPLIANT_RESPONSE, data_plane=data_plane, declared=_ENABLED))[
            DECLARED_DENY_CHECK
        ]
        assert row.status == "fail"
        assert "ADMITTED" in row.detail
        assert "declared enabled" in row.detail

    @pytest.mark.parametrize(
        ("code", "message"),
        [(ACCESS_DENIED_CODE, _IMPLICIT_DENY), ("ThrottlingException", "Rate exceeded")],
        ids=["implicit_deny", "throttled"],
    )
    def test_declared_enabled_and_another_error_defers(self, code: str, message: str) -> None:
        row = _by_check(
            _run(_COMPLIANT_RESPONSE, data_plane=_FakeDataPlane(code, message), declared=_ENABLED)
        )[DECLARED_DENY_CHECK]
        assert row.status == "defer"
        assert row.defer_reason == "inbound_deny_not_observed_other_error"

    @pytest.mark.parametrize("code", [ACCESS_DENIED_CODE, None], ids=["refused", "admitted"])
    def test_declared_disabled_defers_whatever_was_observed(self, code: str | None) -> None:
        declared = DeclaredDenyState(DeclaredDeny.DISABLED, "t.tfvars")
        row = _by_check(
            _run(_COMPLIANT_RESPONSE, data_plane=_FakeDataPlane(code), declared=declared)
        )[DECLARED_DENY_CHECK]
        assert row.status == "defer"
        assert row.defer_reason == "inbound_deny_disabled_by_config"
        assert "declared disabled (t.tfvars)" in row.detail

    @pytest.mark.parametrize("code", [ACCESS_DENIED_CODE, None], ids=["refused", "admitted"])
    def test_an_unknown_declared_state_defers_whatever_was_observed(self, code: str | None) -> None:
        declared = DeclaredDenyState(DeclaredDeny.UNKNOWN, "no tfvars")
        row = _by_check(
            _run(_COMPLIANT_RESPONSE, data_plane=_FakeDataPlane(code), declared=declared)
        )[DECLARED_DENY_CHECK]
        assert row.status == "defer"
        assert row.defer_reason == "inbound_deny_state_unknown"

    def test_the_default_reads_the_deployments_declared_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(module, "declared_source_vpce_deny", lambda: _ENABLED)
        row = _by_check(_run(_COMPLIANT_RESPONSE))[DECLARED_DENY_CHECK]
        assert row.status == "pass"
        assert "test: terraform.tfvars" in row.detail

    def test_an_invoker_caller_makes_no_call_and_publishes_no_verdict(self) -> None:
        sts = _FakeSts("arn:aws:sts::123456789012:assumed-role/crm-agent-eval-agent-relay-role/fn")
        results = _by_check(_run(_COMPLIANT_RESPONSE, sts=sts, declared=_ENABLED))
        assert DECLARED_DENY_CHECK not in results

    def test_the_verdict_also_runs_on_the_custom_jwt_path(self) -> None:
        response = dict(
            _COMPLIANT_RESPONSE, **{AUTHORIZER_MEMBER: {CUSTOM_JWT_MEMBER: {"allowedClients": []}}}
        )
        assert _by_check(_run(response, declared=_ENABLED))[DECLARED_DENY_CHECK].status == "pass"


_INVOKER_ARNS = [
    f"arn:aws:iam::123456789012:role/crm-agent-eval-{name}-role"
    for name in ("agent-relay", "sweep-task", "chat-target")
]


def _policy(principals: Any, *, deny: bool = True) -> str:
    statements: list[dict[str, Any]] = [
        {
            "Sid": "OnlyTheInvokerRolesMayInvokeThisRuntime",
            "Effect": "Allow",
            "Principal": principals if principals == "*" else {"AWS": principals},
            "Action": "bedrock-agentcore:InvokeAgentRuntime",
            "Resource": _RUNTIME_ARN,
        }
    ]
    if deny:
        statements.append(
            {
                "Effect": "Deny",
                "Principal": "*",
                "Action": "bedrock-agentcore:InvokeAgentRuntime",
                "Resource": _RUNTIME_ARN,
                "Condition": {"StringNotEquals": {"aws:SourceVpce": "vpce-1"}},
            }
        )
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


class _PolicyControlPlane(_FakeControlPlane):
    """A control plane that also models `get_resource_policy`, per resource ARN."""

    def __init__(self, policies: dict[str, str | Exception]) -> None:
        super().__init__(_COMPLIANT_RESPONSE)
        self.policies = policies
        self.policy_reads: list[str] = []

    def get_resource_policy(self, *, resourceArn: str) -> dict[str, str]:  # noqa: N803
        self.policy_reads.append(resourceArn)
        policy = self.policies[resourceArn]
        if isinstance(policy, Exception):
            raise policy
        return {"policy": policy}


def _principal_rows(policies: dict[str, str | Exception]) -> list[CheckResult]:
    control = _PolicyControlPlane(policies)
    ctx = context_for_test(
        outputs=_OUTPUTS,
        clients={
            "bedrock-agentcore-control": control,
            "bedrock-agentcore": _FakeDataPlane(),
            "sts": _FakeSts(),
        },
    )
    rows = [r for r in checks(ctx) if r.check == PRINCIPAL_SET_CHECK]
    assert control.policy_reads == list(resource_policy_arns(_RUNTIME_ARN))
    return rows


_RUNTIME, _ENDPOINT = resource_policy_arns(_RUNTIME_ARN)


class TestTheResourcePolicyPrincipalSet:
    """Both Runtime resource policies admit exactly the three invoker roles."""

    def test_the_endpoint_arn_is_the_default_runtime_endpoint(self) -> None:
        assert f"{_RUNTIME_ARN}/runtime-endpoint/DEFAULT" == _ENDPOINT

    def test_exactly_the_invokers_on_both_policies_passes_twice(self) -> None:
        rows = _principal_rows(
            {_RUNTIME: _policy(_INVOKER_ARNS), _ENDPOINT: _policy(_INVOKER_ARNS)}
        )
        assert [(r.resource, r.status) for r in rows] == [(_RUNTIME, "pass"), (_ENDPOINT, "pass")]

    @pytest.mark.parametrize(
        "principals",
        [
            [*_INVOKER_ARNS, "arn:aws:iam::123456789012:role/someone-else"],
            _INVOKER_ARNS[:2],
            "*",
            "arn:aws:iam::123456789012:root",
            [*_INVOKER_ARNS[:2], "AROAEXAMPLEUNIQUEID"],
        ],
        ids=["extra-role", "missing-role", "wildcard", "account-root", "recreated-role"],
    )
    def test_any_other_principal_set_on_either_policy_fails(self, principals: Any) -> None:
        rows = _principal_rows({_RUNTIME: _policy(_INVOKER_ARNS), _ENDPOINT: _policy(principals)})
        assert [(r.resource, r.status) for r in rows] == [(_RUNTIME, "pass"), (_ENDPOINT, "fail")]

    def test_a_deny_statements_wildcard_is_not_read_as_an_admitted_principal(self) -> None:
        rows = _principal_rows(
            {_RUNTIME: _policy(_INVOKER_ARNS, deny=True), _ENDPOINT: _policy(_INVOKER_ARNS)}
        )
        assert rows[0].status == "pass"

    def test_the_principal_deny_shape_still_passes_on_both_policies(self) -> None:
        """G-8: the `aws:PrincipalArn` Deny beside the SourceVpce Deny is not an admitted principal."""

        def with_principal_deny(resource: str) -> str:
            document = json.loads(_policy(_INVOKER_ARNS))
            document["Statement"].append(
                {
                    "Sid": "DenyRuntimeDataPlaneToAnyOtherPrincipal",
                    "Effect": "Deny",
                    "Principal": {"AWS": "*"},
                    "Action": [
                        "bedrock-agentcore:InvokeAgentRuntime",
                        "bedrock-agentcore:InvokeAgentRuntimeForUser",
                    ],
                    "Resource": resource,
                    "Condition": {"ArnNotEquals": {"aws:PrincipalArn": _INVOKER_ARNS}},
                }
            )
            return json.dumps(document)

        rows = _principal_rows(
            {_RUNTIME: with_principal_deny(_RUNTIME), _ENDPOINT: with_principal_deny(_ENDPOINT)}
        )
        assert [(r.resource, r.status) for r in rows] == [(_RUNTIME, "pass"), (_ENDPOINT, "pass")]

    def test_an_unreadable_policy_fails_rather_than_passing(self) -> None:
        rows = _principal_rows({_RUNTIME: RuntimeError("throttled"), _ENDPOINT: "not json"})
        assert [r.status for r in rows] == ["fail", "fail"]

    def test_neither_botocore_nor_the_cli_supporting_it_defers_both_and_says_so(self) -> None:
        rows = [r for r in _run(_COMPLIANT_RESPONSE) if r.check == PRINCIPAL_SET_CHECK]
        assert [(r.resource, r.status) for r in rows] == [(_RUNTIME, "defer"), (_ENDPOINT, "defer")]
        assert all(r.defer_reason == "get_resource_policy_unsupported" for r in rows)
        assert all("neither botocore nor the installed aws CLI" in r.detail for r in rows)

    def test_no_cli_on_path_defers_and_names_the_absence(self) -> None:
        rows = [
            r
            for r in _run(_COMPLIANT_RESPONSE, cli=_FakeCli(absent=True))
            if r.check == PRINCIPAL_SET_CHECK
        ]
        assert [r.status for r in rows] == ["defer", "defer"]
        assert all("no `aws` CLI is on PATH" in r.detail for r in rows)

    def test_the_expected_set_is_derived_from_the_iac_invoker_list(self) -> None:
        rows = _principal_rows(
            {_RUNTIME: _policy(_INVOKER_ARNS), _ENDPOINT: _policy(_INVOKER_ARNS)}
        )
        for arn in _INVOKER_ARNS:
            assert arn in rows[0].detail

    def test_the_outside_vpc_label_points_at_this_row(self) -> None:
        assert PRINCIPAL_SET_CHECK in OUTSIDE_VPC_BOUND
        assert "relay_iam" not in OUTSIDE_VPC_BOUND


def _cli_ok(principals: Any) -> tuple[int, str, str]:
    return 0, json.dumps({"resourceArn": "x", "policy": _policy(principals)}), ""


class TestTheCliFallback:
    """With botocore lacking the operation, the policies are read with the aws CLI."""

    def _rows(self, cli: _FakeCli) -> list[CheckResult]:
        return [r for r in _run(_COMPLIANT_RESPONSE, cli=cli) if r.check == PRINCIPAL_SET_CHECK]

    def test_the_exact_set_from_the_cli_passes_both(self) -> None:
        cli = _FakeCli({_RUNTIME: _cli_ok(_INVOKER_ARNS), _ENDPOINT: _cli_ok(_INVOKER_ARNS)})
        assert [(r.resource, r.status) for r in self._rows(cli)] == [
            (_RUNTIME, "pass"),
            (_ENDPOINT, "pass"),
        ]

    def test_an_extra_principal_from_the_cli_fails_that_policy(self) -> None:
        extra = [*_INVOKER_ARNS, "arn:aws:iam::123456789012:role/someone-else"]
        cli = _FakeCli({_RUNTIME: _cli_ok(_INVOKER_ARNS), _ENDPOINT: _cli_ok(extra)})
        rows = self._rows(cli)
        assert [(r.resource, r.status) for r in rows] == [(_RUNTIME, "pass"), (_ENDPOINT, "fail")]
        assert "someone-else" in rows[1].detail

    def test_a_cli_read_error_fails_rather_than_deferring(self) -> None:
        denied = (
            254,
            "",
            "An error occurred (AccessDeniedException) when calling GetResourcePolicy",
        )
        cli = _FakeCli({_RUNTIME: denied, _ENDPOINT: (0, "not json", "")})
        rows = self._rows(cli)
        assert [r.status for r in rows] == ["fail", "fail"]
        assert "AccessDeniedException" in rows[0].detail

    def test_the_argv_carries_the_resolved_profile_and_region_and_both_arns(self) -> None:
        cli = _FakeCli({_RUNTIME: _cli_ok(_INVOKER_ARNS), _ENDPOINT: _cli_ok(_INVOKER_ARNS)})
        ctx = context_for_test(
            outputs=_OUTPUTS,
            profile="crm-dev",
            region="us-east-1",
            clients={
                "bedrock-agentcore-control": _FakeControlPlane(_COMPLIANT_RESPONSE),
                "bedrock-agentcore": _FakeDataPlane(),
                "sts": _FakeSts(),
            },
        )
        list(checks(ctx, cli_runner=cli))
        assert [call[call.index("--resource-arn") + 1] for call in cli.calls] == [
            _RUNTIME,
            _ENDPOINT,
        ]
        for call in cli.calls:
            assert call[:3] == ["aws", "bedrock-agentcore-control", "get-resource-policy"]
            assert call[call.index("--profile") + 1] == "crm-dev"
            assert call[call.index("--region") + 1] == "us-east-1"
            assert "--no-cli-pager" in call and call[call.index("--output") + 1] == "json"

    def test_botocore_is_preferred_when_it_models_the_operation(self) -> None:
        cli = _FakeCli()
        control = _PolicyControlPlane(
            {_RUNTIME: _policy(_INVOKER_ARNS), _ENDPOINT: _policy(_INVOKER_ARNS)}
        )
        ctx = context_for_test(
            outputs=_OUTPUTS,
            clients={
                "bedrock-agentcore-control": control,
                "bedrock-agentcore": _FakeDataPlane(),
                "sts": _FakeSts(),
            },
        )
        rows = [r for r in checks(ctx, cli_runner=cli) if r.check == PRINCIPAL_SET_CHECK]
        assert [r.status for r in rows] == ["pass", "pass"]
        assert cli.calls == []


def _policy_with(*extra: dict[str, Any]) -> str:
    document = json.loads(_policy(_INVOKER_ARNS))
    document["Statement"] = [*document["Statement"], *extra]
    return json.dumps(document)


_ALLOW = {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime", "Resource": "*"}


class TestEveryAllowIsAwsOnly:
    """An Allow naming any other principal class fails, and names the statement."""

    @pytest.mark.parametrize(
        ("statement", "named"),
        [
            (
                {"Sid": "ServiceExtra", **_ALLOW, "Principal": {"Service": "lambda.amazonaws.com"}},
                "ServiceExtra",
            ),
            (
                {
                    "Sid": "AwsPlusService",
                    **_ALLOW,
                    "Principal": {"AWS": _INVOKER_ARNS, "Service": "lambda.amazonaws.com"},
                },
                "AwsPlusService",
            ),
            (
                {"Sid": "NotP", **_ALLOW, "NotPrincipal": {"AWS": "arn:aws:iam::1:role/x"}},
                "NotP",
            ),
            (
                {
                    "Sid": "Fed",
                    **_ALLOW,
                    "Principal": {"Federated": "cognito-identity.amazonaws.com"},
                },
                "Fed",
            ),
            ({**_ALLOW, "Principal": "*"}, "<statement 2>"),
        ],
        ids=["service-extra", "aws-plus-service", "not-principal", "federated", "star-no-sid"],
    )
    def test_a_non_aws_allow_fails_and_names_its_statement(
        self, statement: dict[str, Any], named: str
    ) -> None:
        rows = _principal_rows(
            {_RUNTIME: _policy(_INVOKER_ARNS), _ENDPOINT: _policy_with(statement)}
        )
        assert [(r.resource, r.status) for r in rows] == [(_RUNTIME, "pass"), (_ENDPOINT, "fail")]
        assert named in rows[1].detail and "non-AWS Allow" in rows[1].detail

    def test_a_deny_with_any_principal_class_is_not_an_allow(self) -> None:
        deny = {"Effect": "Deny", "Action": "*", "Resource": "*", "NotPrincipal": {"AWS": "x"}}
        rows = _principal_rows({_RUNTIME: _policy_with(deny), _ENDPOINT: _policy(_INVOKER_ARNS)})
        assert [r.status for r in rows] == ["pass", "pass"]
