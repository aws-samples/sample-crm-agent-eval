# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.public_exposure`.

Every client is a fake injected through `context_for_test`. Fixtures follow the shapes of real
`lambda`/`apigateway` API responses, with the account id replaced by the placeholder `123456789012`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Final

import pytest
import scripts.smoke.public_exposure as public_exposure
from scripts.hcl_text import blank_comments
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.public_exposure import (
    EXIT_VERIFIED_NOTHING,
    NON_TOOL_FUNCTION_SUFFIXES,
    REST_API_CHECK,
    TLS_OWNER_CHECK,
    check_function_url_auth,
    check_no_public_invoke_grant,
    check_tls_floor_is_owned_elsewhere,
    check_vpc_attached,
    checks,
    conditioned_on_auth_type_none,
    credential_failure_reason,
    discover_stack_functions,
    expected_check_count,
    outputs_staleness_reason,
    public_url_grant_statements,
    rest_api_private_vpce_only,
    source_declared_function_names,
    vacuity_reason,
    vpc_config_attached,
)

from crm.toolspec.definitions import ALL_TOOLS

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_MAIN_TF: Final[Path] = _REPO_ROOT / "infra" / "main.tf"
_CLOUDFRONT_SMOKE: Final[Path] = _REPO_ROOT / "scripts" / "smoke" / "cloudfront.py"

#: The documentation placeholder, never a real account id (`check_no_account_id`).
_ACCOUNT: Final[str] = "123456789012"

#: Hex bodies for VPC resource ids, which are assembled at runtime so no tracked line carries a
#: resource prefix followed by hex (`check_no_account_id`).
_HEX_8: Final[str] = "0123abcd"
_HEX_8_ALT: Final[str] = "4567ef01"
_HEX_8_SG: Final[str] = "89abcdef"


def _resource_id(prefix: str, body: str) -> str:
    return f"{prefix}-{body}"


_SUBNET_IDS: Final[tuple[str, str]] = (
    _resource_id("subnet", _HEX_8),
    _resource_id("subnet", _HEX_8_ALT),
)
_SECURITY_GROUP_ID: Final[str] = _resource_id("sg", _HEX_8_SG)
_RELAY: Final[str] = "crm-agent-eval-agent-relay"
_RELAY_ARN: Final[str] = f"arn:aws:lambda:us-east-1:{_ACCOUNT}:function:{_RELAY}"

#: The public Function URL invoke statement `aws lambda add-permission --principal '*'
#: --function-url-auth-type NONE` writes, as `aws lambda get-policy` returns it.
_D333_PUBLIC_INVOKE_STATEMENT: Final[dict[str, Any]] = {
    "Sid": "AllowPublicFunctionUrlInvoke",
    "Effect": "Allow",
    "Principal": "*",
    "Action": "lambda:InvokeFunctionUrl",
    "Resource": _RELAY_ARN,
    "Condition": {"StringEquals": {"lambda:FunctionUrlAuthType": "NONE"}},
}

_SCOPED_STATEMENT: Final[dict[str, Any]] = {
    "Sid": "AllowApiGatewayInvoke",
    "Effect": "Allow",
    "Principal": {"Service": "apigateway.amazonaws.com"},
    "Action": "lambda:InvokeFunction",
    "Resource": _RELAY_ARN,
}


class _NotFoundError(Exception):
    """Stands in for botocore's dynamically-built `ResourceNotFoundException`.

    The production code matches on `type(exc).__name__`, which the assignment below sets.
    """


_NotFoundError.__name__ = "ResourceNotFoundException"


class FakeLambda:
    """Minimal `lambda` client double covering only the four calls this module makes."""

    def __init__(
        self,
        *,
        functions: list[str] | None = None,
        url_configs: dict[str, dict[str, Any]] | None = None,
        policies: dict[str, dict[str, Any]] | None = None,
        configurations: dict[str, dict[str, Any]] | None = None,
        pages: list[list[str]] | None = None,
    ) -> None:
        self._pages = pages if pages is not None else [list(functions or [])]
        self._url_configs = url_configs or {}
        self._policies = policies or {}
        self._configurations = configurations or {}

    def get_paginator(self, operation: str) -> Any:
        assert operation == "list_functions"
        pages = self._pages

        class _Paginator:
            def paginate(self) -> Any:
                for page in pages:
                    yield {"Functions": [{"FunctionName": name} for name in page]}

        return _Paginator()

    def get_function_url_config(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        if FunctionName not in self._url_configs:
            raise _NotFoundError(f"no url config for {FunctionName}")
        return self._url_configs[FunctionName]

    def get_policy(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        if FunctionName not in self._policies:
            raise _NotFoundError(f"no policy for {FunctionName}")
        return {"Policy": json.dumps(self._policies[FunctionName])}

    def get_function_configuration(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        return self._configurations.get(FunctionName, {})


#: The stack's REST (v1) API id (from the `api_id` output) and its one VPC endpoint id.
_API_ID: Final[str] = "hvx42fc7i5"
_VPCE_ID: Final[str] = _resource_id("vpce", _HEX_8)
#: The private-invoke hostname the `api_endpoint` output carries; its first DNS label is
#: `<id>-vpce-<x>`, not the API id.
_VPCE_HOSTNAME: Final[str] = f"{_API_ID}-vpce-{_HEX_8_ALT}.execute-api.us-east-1.amazonaws.com"


def _private_rest_api(*, vpce_id: str = _VPCE_ID) -> dict[str, Any]:
    """A get_rest_api response for a correctly-locked-down private REST API.

    `endpointConfiguration.types == ["PRIVATE"]`, one `vpcEndpointIds` entry, and a `policy` JSON
    string whose Deny bounds `execute-api:Invoke` to that endpoint via `aws:sourceVpce`.
    """
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": "*",
                "Action": "execute-api:Invoke",
                "Resource": "execute-api:/*",
            },
            {
                "Effect": "Deny",
                "Principal": "*",
                "Action": "execute-api:Invoke",
                "Resource": "execute-api:/*",
                "Condition": {"StringNotEquals": {"aws:sourceVpce": vpce_id}},
            },
        ],
    }
    return {
        "id": _API_ID,
        "endpointConfiguration": {"types": ["PRIVATE"], "vpcEndpointIds": [vpce_id]},
        "policy": json.dumps(policy),
    }


class FakeApiGateway:
    """Minimal `apigateway` (REST v1) client double covering only `get_rest_api`.

    `error`, when set, is raised from the lookup.
    """

    def __init__(
        self, *, apis: dict[str, dict[str, Any]] | None = None, error: Exception | None = None
    ) -> None:
        self._apis = apis or {}
        self._error = error

    def get_rest_api(self, *, restApiId: str) -> dict[str, Any]:  # noqa: N803
        if self._error is not None:
            raise self._error
        return self._apis.get(restApiId, {})


class _ForbiddenApiGatewayV2:
    """An `apigatewayv2` client double that fails on any use; the API is REST v1."""

    def __getattr__(self, _name: str) -> Any:
        raise AssertionError("apigatewayv2 must never be used: the stack's API is REST (v1)")


def _ctx(
    *,
    lambda_client: FakeLambda | None = None,
    apigw: FakeApiGateway | None = None,
    outputs: dict[str, Any] | None = None,
) -> Any:
    """Build an offline context.

    Outputs are passed flat, as `load_outputs` presents them after unwrapping `{"value": ...}`. The
    `apigatewayv2` client is a double that fails on any use.
    """
    return context_for_test(
        outputs=outputs if outputs is not None else {public_exposure.KNOWN_PRESENT_OUTPUT: _RELAY},
        clients={
            "lambda": lambda_client if lambda_client is not None else FakeLambda(),
            "apigateway": apigw if apigw is not None else FakeApiGateway(),
            "apigatewayv2": _ForbiddenApiGatewayV2(),
        },
    )


def _by_check(results: list[CheckResult], name: str) -> list[CheckResult]:
    return [result for result in results if result.check == name]


# ---------------------------------------------------------------------------
# Property 1: Function URL auth type
# ---------------------------------------------------------------------------


def test_an_iam_authenticated_function_url_passes() -> None:
    ctx = _ctx(lambda_client=FakeLambda(url_configs={_RELAY: {"AuthType": "AWS_IAM"}}))
    assert check_function_url_auth(ctx, _RELAY).status == "pass"


def test_a_function_url_with_auth_type_none_fails() -> None:
    """`AuthType: NONE` fails on its own, independently of any resource policy."""
    ctx = _ctx(lambda_client=FakeLambda(url_configs={_RELAY: {"AuthType": "NONE"}}))
    result = check_function_url_auth(ctx, _RELAY)
    assert result.status == "fail"
    assert "NONE" in result.detail


def test_a_function_with_no_url_configured_passes_rather_than_erroring() -> None:
    """`ResourceNotFoundException` here means no URL exists, which cannot be reached through one."""
    ctx = _ctx(lambda_client=FakeLambda(url_configs={}))
    result = check_function_url_auth(ctx, "crm-agent-eval-get_account")
    assert result.status == "pass"
    assert "no Function URL" in result.detail


def test_an_unexpected_lambda_error_is_not_swallowed_as_a_pass() -> None:
    """Only `ResourceNotFoundException` is absorbed; any other error propagates."""

    class DeniedError(Exception):
        pass

    class DenyingLambda(FakeLambda):
        def get_function_url_config(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
            raise DeniedError("AccessDeniedException")

    with pytest.raises(DeniedError):
        check_function_url_auth(_ctx(lambda_client=DenyingLambda()), _RELAY)


# ---------------------------------------------------------------------------
# Property 2: the resource policy, as an independent check
# ---------------------------------------------------------------------------


def test_a_wildcard_grant_conditioned_on_auth_type_none_fails() -> None:
    """A wildcard-principal Function URL grant conditioned on `AuthType: NONE` fails."""
    policy = {"Version": "2012-10-17", "Statement": [_D333_PUBLIC_INVOKE_STATEMENT]}
    ctx = _ctx(lambda_client=FakeLambda(policies={_RELAY: policy}))
    result = check_no_public_invoke_grant(ctx, _RELAY)
    assert result.status == "fail"
    assert "AllowPublicFunctionUrlInvoke" in result.detail
    assert "FunctionUrlAuthType=NONE" in result.detail


def test_no_resource_policy_at_all_is_a_pass_not_an_error() -> None:
    """A `ResourceNotFoundException` from `get-policy` (no policy at all) passes."""
    ctx = _ctx(lambda_client=FakeLambda(policies={}))
    result = check_no_public_invoke_grant(ctx, _RELAY)
    assert result.status == "pass"
    assert "no resource policy" in result.detail


def test_a_scoped_service_principal_passes() -> None:
    policy = {"Version": "2012-10-17", "Statement": [_SCOPED_STATEMENT]}
    ctx = _ctx(lambda_client=FakeLambda(policies={_RELAY: policy}))
    assert check_no_public_invoke_grant(ctx, _RELAY).status == "pass"


def test_an_unconditioned_wildcard_grant_also_fails() -> None:
    """A public `Allow` with no condition is a broader exposure, not a narrower one."""
    statement = {k: v for k, v in _D333_PUBLIC_INVOKE_STATEMENT.items() if k != "Condition"}
    policy = {"Version": "2012-10-17", "Statement": [statement]}
    ctx = _ctx(lambda_client=FakeLambda(policies={_RELAY: policy}))
    result = check_no_public_invoke_grant(ctx, _RELAY)
    assert result.status == "fail"
    assert "FunctionUrlAuthType=NONE" not in result.detail


@pytest.mark.parametrize(
    "principal",
    [
        "*",
        {"AWS": "*"},
        {"AWS": ["*"]},
        {"AWS": ["arn:aws:iam::123456789012:root", "*"]},
    ],
    ids=["bare", "aws_string", "aws_list", "aws_list_mixed"],
)
def test_every_spelling_of_a_wildcard_principal_is_detected(principal: Any) -> None:
    """`"*"` and `{"AWS": "*"}` are the same grant, and the CLI writes the latter."""
    statement = dict(_D333_PUBLIC_INVOKE_STATEMENT, Principal=principal)
    assert public_url_grant_statements({"Statement": [statement]}) == (statement,)


def test_a_deny_statement_with_a_wildcard_principal_is_not_a_grant() -> None:
    """A wildcard `Deny` is the AWS-recommended TLS-enforcement pattern, not an exposure."""
    statement = dict(_D333_PUBLIC_INVOKE_STATEMENT, Effect="Deny")
    assert public_url_grant_statements({"Statement": [statement]}) == ()


def test_a_single_statement_object_is_read_as_well_as_a_list() -> None:
    """IAM permits a bare object where a list is usual; both must be read."""
    assert public_url_grant_statements({"Statement": _D333_PUBLIC_INVOKE_STATEMENT}) == (
        _D333_PUBLIC_INVOKE_STATEMENT,
    )


def test_the_auth_type_none_condition_is_recognised_only_on_the_right_key() -> None:
    assert conditioned_on_auth_type_none(_D333_PUBLIC_INVOKE_STATEMENT)
    other_key = dict(
        _D333_PUBLIC_INVOKE_STATEMENT,
        Condition={"StringEquals": {"aws:SourceVpc": "NONE"}},
    )
    assert not conditioned_on_auth_type_none(other_key)


# ---------------------------------------------------------------------------
# The two checks are independent
# ---------------------------------------------------------------------------


def test_the_policy_check_reds_while_the_url_auth_type_is_correct() -> None:
    """An over-broad `Allow` is an exposure even at `AWS_IAM`."""
    client = FakeLambda(
        url_configs={_RELAY: {"AuthType": "AWS_IAM"}},
        policies={_RELAY: {"Statement": [_D333_PUBLIC_INVOKE_STATEMENT]}},
    )
    ctx = _ctx(lambda_client=client)
    assert check_function_url_auth(ctx, _RELAY).status == "pass"
    assert check_no_public_invoke_grant(ctx, _RELAY).status == "fail"


def test_the_url_check_reds_while_the_policy_is_clean() -> None:
    """`AuthType: NONE` with no policy fails the auth-type check."""
    client = FakeLambda(url_configs={_RELAY: {"AuthType": "NONE"}}, policies={})
    ctx = _ctx(lambda_client=client)
    assert check_function_url_auth(ctx, _RELAY).status == "fail"
    assert check_no_public_invoke_grant(ctx, _RELAY).status == "pass"


# ---------------------------------------------------------------------------
# Property 3: VpcConfig
# ---------------------------------------------------------------------------


def test_a_vpc_attached_function_passes() -> None:
    configuration = {
        "VpcConfig": {"SubnetIds": list(_SUBNET_IDS), "SecurityGroupIds": [_SECURITY_GROUP_ID]}
    }
    ctx = _ctx(lambda_client=FakeLambda(configurations={_RELAY: configuration}))
    assert check_vpc_attached(ctx, _RELAY).status == "pass"


@pytest.mark.parametrize(
    "vpc_config",
    [
        None,
        {},
        {"SubnetIds": [], "SecurityGroupIds": []},
        {"SubnetIds": [_SUBNET_IDS[0]], "SecurityGroupIds": []},
        {"SubnetIds": [], "SecurityGroupIds": [_SECURITY_GROUP_ID]},
    ],
    ids=["absent", "empty_object", "both_empty", "no_group", "no_subnet"],
)
def test_an_unattached_function_fails_in_every_shape_lambda_reports(vpc_config: Any) -> None:
    """An unattached function fails for every `VpcConfig` shape, including present-but-empty."""
    configuration: dict[str, Any] = {} if vpc_config is None else {"VpcConfig": vpc_config}
    assert not vpc_config_attached(configuration)
    ctx = _ctx(lambda_client=FakeLambda(configurations={_RELAY: configuration}))
    assert check_vpc_attached(ctx, _RELAY).status == "fail"


# ---------------------------------------------------------------------------
# Property 4: the REST (v1) API is private and VPC-endpoint-only
# ---------------------------------------------------------------------------


def _rest_ctx(
    *,
    api: dict[str, Any] | None = None,
    error: Exception | None = None,
    outputs_api_id: bool = True,
) -> Any:
    outputs: dict[str, Any] = {public_exposure.KNOWN_PRESENT_OUTPUT: _RELAY}
    if outputs_api_id:
        outputs["api_id"] = _API_ID
    # The private-invoke hostname is also present; the id must not be taken from it.
    outputs["api_endpoint"] = f"https://{_VPCE_HOSTNAME}"
    apis = {_API_ID: api} if api is not None else {}
    return _ctx(apigw=FakeApiGateway(apis=apis, error=error), outputs=outputs)


def test_a_private_vpce_only_rest_api_passes() -> None:
    results = list(rest_api_private_vpce_only(_rest_ctx(api=_private_rest_api())))
    assert [r.status for r in results] == ["pass"]
    assert results[0].check == REST_API_CHECK


def _aws_double_escaped(policy_json: str) -> str:
    """Reproduce the shape `get_rest_api` returns for `policy`.

    AWS returns the policy as the body of a JSON string literal: quotes are backslash-escaped and
    slashes are ``\\/`` (`{\\"Version\\":...}`), so the raw value is not valid JSON on its own.
    """
    return json.dumps(policy_json)[1:-1].replace("/", "\\/")


def test_a_private_rest_api_whose_policy_is_double_escaped_as_aws_returns_it_passes() -> None:
    """A backslash-escaped `policy` as AWS returns it is decoded and its SourceVpce Deny read."""
    api = _private_rest_api()
    api["policy"] = _aws_double_escaped(api["policy"])
    results = list(rest_api_private_vpce_only(_rest_ctx(api=api)))
    assert [r.status for r in results] == ["pass"]
    assert results[0].check == REST_API_CHECK


def test_the_api_id_comes_from_the_api_id_output_not_the_vpce_hostname() -> None:
    """The REST API id comes from the `api_id` output, never the vpce hostname's first label."""
    seen: list[str] = []

    class RecordingRestApiGateway(FakeApiGateway):
        def get_rest_api(self, *, restApiId: str) -> dict[str, Any]:  # noqa: N803
            seen.append(restApiId)
            return _private_rest_api()

    ctx = _ctx(
        apigw=RecordingRestApiGateway(),
        outputs={
            public_exposure.KNOWN_PRESENT_OUTPUT: _RELAY,
            "api_id": _API_ID,
            "api_endpoint": f"https://{_VPCE_HOSTNAME}",
        },
    )
    list(rest_api_private_vpce_only(ctx))
    assert seen == [_API_ID]
    assert f"{_API_ID}-vpce-{_HEX_8_ALT}" not in seen


def test_a_regional_rest_api_fails() -> None:
    """A non-PRIVATE endpoint type fails."""
    api = _private_rest_api()
    api["endpointConfiguration"]["types"] = ["REGIONAL"]
    results = list(rest_api_private_vpce_only(_rest_ctx(api=api)))
    assert [r.status for r in results] == ["fail"]
    assert "REGIONAL" in results[0].detail


def test_a_rest_api_with_no_source_vpce_deny_fails() -> None:
    """A policy without the SourceVpce Deny fails."""
    api = _private_rest_api()
    api["policy"] = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "execute-api:Invoke",
                    "Resource": "execute-api:/*",
                }
            ],
        }
    )
    results = list(rest_api_private_vpce_only(_rest_ctx(api=api)))
    assert [r.status for r in results] == ["fail"]
    assert "aws:SourceVpce" in results[0].detail


def test_a_source_vpce_deny_naming_other_endpoints_fails() -> None:
    """The Deny must name exactly the attached endpoint set, not some other endpoint."""
    api = _private_rest_api()
    other = _resource_id("vpce", _HEX_8_SG)
    document = json.loads(api["policy"])
    document["Statement"][1]["Condition"]["StringNotEquals"]["aws:sourceVpce"] = other
    api["policy"] = json.dumps(document)
    results = list(rest_api_private_vpce_only(_rest_ctx(api=api)))
    assert [r.status for r in results] == ["fail"]


def test_the_source_vpce_condition_key_is_matched_case_insensitively() -> None:
    """Both `aws:sourceVpce` and `aws:SourceVpce` condition keys satisfy the check."""
    api = _private_rest_api()
    document = json.loads(api["policy"])
    condition = document["Statement"][1]["Condition"]["StringNotEquals"]
    condition["aws:SourceVpce"] = condition.pop("aws:sourceVpce")
    api["policy"] = json.dumps(document)
    results = list(rest_api_private_vpce_only(_rest_ctx(api=api)))
    assert [r.status for r in results] == ["pass"]


def test_a_get_rest_api_error_yields_a_fail_row_not_a_generator_abort() -> None:
    """A `get_rest_api` lookup failure emits a fail row rather than raising."""
    results = list(rest_api_private_vpce_only(_rest_ctx(error=_NotFoundError("no such REST API"))))
    assert [r.status for r in results] == ["fail"]
    assert "get_rest_api" in results[0].detail


def test_an_absent_api_id_output_fails_rather_than_using_the_hostname() -> None:
    """An absent `api_id` output fails, without falling back to the vpce hostname."""
    results = list(rest_api_private_vpce_only(_rest_ctx(outputs_api_id=False)))
    assert [r.status for r in results] == ["fail"]
    assert "api_id" in results[0].detail


def test_the_rest_check_and_tls_defer_both_emit_even_when_the_lookup_fails() -> None:
    """A failure in the REST check does not suppress the TLS defer that follows it in `checks`."""
    names = source_declared_function_names()
    client = FakeLambda(
        functions=list(names),
        url_configs={name: {"AuthType": "AWS_IAM"} for name in names},
        configurations={
            name: {"VpcConfig": {"SubnetIds": ["subnet-a"], "SecurityGroupIds": ["sg-a"]}}
            for name in names
        },
    )
    ctx = _ctx(
        lambda_client=client,
        apigw=FakeApiGateway(error=_NotFoundError("no such REST API")),
        outputs={public_exposure.KNOWN_PRESENT_OUTPUT: _RELAY, "api_id": _API_ID},
    )
    results = list(checks(ctx))
    rest_rows = _by_check(results, REST_API_CHECK)
    tls_rows = _by_check(results, "tls_minimum_protocol_version")
    assert [r.status for r in rest_rows] == ["fail"]
    assert [r.status for r in tls_rows] == ["defer"]


# ---------------------------------------------------------------------------
# CloudFront TLS: deferred to the CloudFront smoke check it cites
# ---------------------------------------------------------------------------


def test_the_tls_floor_is_recorded_as_a_defer_never_a_pass() -> None:
    result = check_tls_floor_is_owned_elsewhere()
    assert result.status == "defer"
    assert result.defer_reason


def test_the_cited_cloudfront_check_still_exists() -> None:
    """The cited CloudFront TLS check name exists in `scripts/smoke/cloudfront.py`."""
    source = _CLOUDFRONT_SMOKE.read_text(encoding="utf-8")
    assert TLS_OWNER_CHECK in source, (
        f"{TLS_OWNER_CHECK!r} is no longer present in {_CLOUDFRONT_SMOKE.name}; the TLS defer in "
        "public_exposure.py cites a check that no longer exists"
    )


# ---------------------------------------------------------------------------
# Discovery, parity, and non-vacuity
# ---------------------------------------------------------------------------


def test_discovery_reads_every_page_not_just_the_first() -> None:
    """Discovery follows `list_functions` pagination across every page."""
    ctx = _ctx(
        lambda_client=FakeLambda(
            pages=[
                ["crm-agent-eval-get_account", "unrelated-function-a"],
                ["crm-agent-eval-agent-relay", "unrelated-function-b"],
            ]
        )
    )
    assert discover_stack_functions(ctx) == (
        "crm-agent-eval-agent-relay",
        "crm-agent-eval-get_account",
    )


def test_discovery_excludes_functions_outside_this_stack() -> None:
    """Functions outside this stack's name prefix are ignored."""
    ctx = _ctx(lambda_client=FakeLambda(functions=["some-other-teams-function", _RELAY]))
    assert discover_stack_functions(ctx) == (_RELAY,)


def test_an_empty_discovery_skips_and_makes_the_run_vacuous() -> None:
    """Zero functions is a broken discovery step, not a clean account."""
    results = list(checks(_ctx(lambda_client=FakeLambda(functions=[]))))
    discovery = _by_check(results, "discovery")
    assert [result.status for result in discovery] == ["skip"]
    assert vacuity_reason(results) is not None
    assert _by_check(results, "function_url_auth_type") == []


def test_discovery_fails_when_the_known_present_function_is_absent() -> None:
    """Discovery fails, naming the relay, when the known-present relay function is not listed."""
    ctx = _ctx(lambda_client=FakeLambda(functions=["crm-agent-eval-get_account"]))
    results = list(checks(ctx))
    discovery = _by_check(results, "discovery")
    assert [result.status for result in discovery] == ["fail"]
    assert _RELAY in discovery[0].detail


def test_a_run_that_found_real_violations_is_not_vacuous() -> None:
    """Fails mean the account *was* read. Only an unreachable account is vacuous."""
    results = [
        CheckResult(resource="x", check="discovery", status="pass", detail="found"),
        CheckResult(resource="x", check="vpc_attached", status="fail", detail="not attached"),
    ]
    assert vacuity_reason(results) is None


def test_source_parity_fails_on_a_live_function_the_repository_does_not_declare() -> None:
    """A function live in the account but absent from source fails."""
    live = [*source_declared_function_names(), "crm-agent-eval-rogue-function"]
    ctx = _ctx(lambda_client=FakeLambda(functions=live))
    parity = _by_check(list(checks(ctx)), "source_parity")
    assert [result.status for result in parity] == ["fail"]
    assert "crm-agent-eval-rogue-function" in parity[0].detail


def test_source_parity_fails_when_a_declared_function_is_absent_live() -> None:
    """A function declared in source but absent live fails."""
    live = [name for name in source_declared_function_names() if name != _RELAY]
    ctx = _ctx(lambda_client=FakeLambda(functions=[*live, _RELAY]))
    trimmed = _ctx(lambda_client=FakeLambda(functions=live), outputs={})
    assert [r.status for r in _by_check(list(checks(ctx)), "source_parity")] == ["pass"]
    assert [r.status for r in _by_check(list(checks(trimmed)), "source_parity")] == ["fail"]


def test_the_source_declared_set_is_the_tools_plus_the_non_tool_functions() -> None:
    assert len(source_declared_function_names()) == len(ALL_TOOLS) + len(NON_TOOL_FUNCTION_SUFFIXES)


def _infra_non_tool_suffixes(root_files: list[Path]) -> set[str]:
    """The non-tool Lambda name suffixes the `function_name` literals in `root_files` declare."""
    declared: set[str] = set()
    for path in root_files:
        text = blank_comments(path.read_text(encoding="utf-8"))
        declared |= set(re.findall(r'function_name\s*=\s*"([^"$]*\$?\{?[^"]*)"', text))
    literal_names = {
        name.replace("${local.name_prefix}", "crm-agent-eval")
        for name in declared
        if "each.key" not in name
    }
    return {
        name.removeprefix("crm-agent-eval-")
        for name in literal_names
        if name.startswith("crm-agent-eval-")
    }


def test_the_non_tool_function_list_matches_what_every_root_file_declares() -> None:
    """`NON_TOOL_FUNCTION_SUFFIXES` matches the non-tool Lambdas declared in every root `infra/*.tf`."""
    root_files = sorted(_MAIN_TF.parent.glob("*.tf"))
    assert len(root_files) > 1, root_files
    assert _infra_non_tool_suffixes(root_files) == set(NON_TOOL_FUNCTION_SUFFIXES)


def test_a_function_declared_outside_main_tf_is_seen(tmp_path: Path) -> None:
    """A non-tool function in a non-main root file is part of the set the guard compares."""
    (tmp_path / "main.tf").write_text('  function_name = "crm-agent-eval-capabilities"\n')
    (tmp_path / "other.tf").write_text('  function_name = "${local.name_prefix}-sweep-task"\n')
    assert _infra_non_tool_suffixes(sorted(tmp_path.glob("*.tf"))) == {"capabilities", "sweep-task"}


def test_the_expected_check_count_is_derived_from_the_source_set() -> None:
    """The expected check count is derived from the source-declared function set."""
    expectation = expected_check_count(_ctx())
    assert expectation.minimum == len(source_declared_function_names()) * 3 + 4
    assert "source-declared" in expectation.basis


def test_a_full_clean_run_emits_at_least_the_expected_floor() -> None:
    """A compliant run meets the floor."""
    names = source_declared_function_names()
    client = FakeLambda(
        functions=list(names),
        url_configs={name: {"AuthType": "AWS_IAM"} for name in names},
        configurations={
            name: {"VpcConfig": {"SubnetIds": ["subnet-a"], "SecurityGroupIds": ["sg-a"]}}
            for name in names
        },
    )
    ctx = _ctx(
        lambda_client=client,
        apigw=FakeApiGateway(apis={_API_ID: _private_rest_api()}),
        outputs={
            public_exposure.KNOWN_PRESENT_OUTPUT: _RELAY,
            "api_id": _API_ID,
        },
    )
    results = list(checks(ctx))
    assert len(results) >= expected_check_count(ctx).minimum
    assert [result.status for result in results if result.status == "fail"] == []
    assert vacuity_reason(results) is None


def test_the_vacuous_exit_code_is_three_and_distinct_from_pass_and_fail() -> None:
    """The verified-nothing exit code is 3, distinct from pass (0) and fail (1)."""
    assert EXIT_VERIFIED_NOTHING == 3
    assert EXIT_VERIFIED_NOTHING not in (0, 1)


# ---------------------------------------------------------------------------
# The other three ways a run can verify nothing: credentials, absent outputs, stale outputs
# ---------------------------------------------------------------------------


def test_a_missing_credential_skips_rather_than_passing_or_failing() -> None:
    """A missing credential yields a single `credentials` skip and a vacuous run."""

    class NoCredentialsError(Exception):
        pass

    class UncredentialedLambda(FakeLambda):
        def get_paginator(self, operation: str) -> Any:
            raise NoCredentialsError("Unable to locate credentials")

    results = list(checks(_ctx(lambda_client=UncredentialedLambda())))
    assert [(r.check, r.status) for r in results] == [("credentials", "skip")]
    assert vacuity_reason(results) is not None


def test_an_authenticated_but_denied_call_is_a_finding_not_a_skip() -> None:
    """An access denial is not a credential failure and propagates out of `checks`."""

    class ClientError(Exception):
        def __init__(self) -> None:
            super().__init__("AccessDenied")
            self.response = {"Error": {"Code": "AccessDeniedException"}}

    assert credential_failure_reason(ClientError()) is None

    class DeniedLambda(FakeLambda):
        def get_paginator(self, operation: str) -> Any:
            raise ClientError()

    with pytest.raises(ClientError):
        list(checks(_ctx(lambda_client=DeniedLambda())))


def test_an_expired_token_is_recognised_as_a_credential_failure() -> None:
    class ClientError(Exception):
        def __init__(self) -> None:
            super().__init__("token expired")
            self.response = {"Error": {"Code": "ExpiredToken"}}

    assert credential_failure_reason(ClientError()) is not None


def test_an_absent_outputs_snapshot_is_refused_by_name(tmp_path: Path) -> None:
    reason = outputs_staleness_reason(tmp_path / "tf-outputs.json")
    assert reason is not None
    assert "does not exist" in reason


def test_a_stale_outputs_snapshot_is_refused_and_says_so(tmp_path: Path) -> None:
    """A stale outputs snapshot is refused."""
    snapshot = tmp_path / "tf-outputs.json"
    snapshot.write_text("{}", encoding="utf-8")
    os.utime(snapshot, (0, 0))
    reason = outputs_staleness_reason(snapshot)
    assert reason is not None
    assert "STALE" in reason


def test_a_current_outputs_snapshot_is_accepted(tmp_path: Path) -> None:
    """A fresh outputs snapshot is accepted."""
    snapshot = tmp_path / "tf-outputs.json"
    snapshot.write_text("{}", encoding="utf-8")
    assert outputs_staleness_reason(snapshot) is None


def test_a_stale_snapshot_stops_the_run_before_any_aws_call(tmp_path: Path) -> None:
    """A refused snapshot stops the run before any AWS client is used."""
    snapshot = tmp_path / "tf-outputs.json"
    snapshot.write_text("{}", encoding="utf-8")
    os.utime(snapshot, (0, 0))

    class ExplodingLambda(FakeLambda):
        def get_paginator(self, operation: str) -> Any:
            raise AssertionError("no AWS call may happen once the snapshot is known stale")

    results = list(checks(_ctx(lambda_client=ExplodingLambda()), snapshot=snapshot))
    assert [(r.check, r.status) for r in results] == [("outputs_freshness", "skip")]
    assert vacuity_reason(results) is not None


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--outputs", "{tmp}/a.json"], "{tmp}/a.json"),
        (["--outputs={tmp}/b.json"], "{tmp}/b.json"),
        (["--json"], None),
        (["--outputs"], None),
    ],
    ids=["separate", "equals", "absent", "dangling"],
)
def test_the_outputs_flag_is_pre_scanned_in_both_spellings(
    argv: list[str], expected: str | None, tmp_path: Path
) -> None:
    resolved_argv = [arg.format(tmp=tmp_path) for arg in argv]
    resolved_expected = None if expected is None else expected.format(tmp=tmp_path)
    assert public_exposure._outputs_flag(resolved_argv) == resolved_expected


def test_every_vacuity_check_name_is_actually_emitted_somewhere() -> None:
    """Every name in `VACUITY_CHECK_NAMES` is emitted by some code path."""
    source = (_REPO_ROOT / "scripts" / "smoke" / "public_exposure.py").read_text(encoding="utf-8")
    for name in public_exposure.VACUITY_CHECK_NAMES:
        assert f'check="{name}"' in source, f"{name!r} is in VACUITY_CHECK_NAMES but never emitted"


# ---------------------------------------------------------------------------
# The mtime fallback is declared in the run's own output, never silent
# ---------------------------------------------------------------------------


def test_an_unstamped_snapshot_is_accepted_but_the_weaker_form_is_disclosed(tmp_path: Path) -> None:
    """A snapshot accepted on the mtime comparison emits a `defer` naming that comparison."""
    snapshot = tmp_path / "tf-outputs.json"
    snapshot.write_text("{}", encoding="utf-8")
    disclosure = public_exposure.freshness_disclosure(snapshot)
    assert disclosure is not None
    assert disclosure.status == "defer"
    assert disclosure.check == "outputs_freshness_form"
    assert "MTIME" in disclosure.detail


def test_a_commit_stamped_snapshot_needs_no_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No defer is emitted when freshness is established by commit."""
    import scripts.outputs_provenance as prov

    snapshot = tmp_path / "tf-outputs.json"
    snapshot.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        prov,
        "assess_freshness",
        lambda _path: prov.Freshness(refusal=None, form="commit", basis="compared by COMMIT"),
    )
    monkeypatch.setattr(public_exposure, "assess_freshness", prov.assess_freshness)
    assert public_exposure.freshness_disclosure(snapshot) is None


def test_a_stale_snapshot_gets_a_refusal_not_a_disclosure(tmp_path: Path) -> None:
    """A stale snapshot is refused and gets no mtime-fallback disclosure."""
    snapshot = tmp_path / "tf-outputs.json"
    snapshot.write_text("{}", encoding="utf-8")
    os.utime(snapshot, (0, 0))
    assert public_exposure.outputs_staleness_reason(snapshot) is not None
    assert public_exposure.freshness_disclosure(snapshot) is None


def test_this_module_holds_no_second_copy_of_the_freshness_comparison() -> None:
    """`public_exposure.py` compares no mtimes itself and uses `assess_freshness`."""
    source = (_REPO_ROOT / "scripts" / "smoke" / "public_exposure.py").read_text(encoding="utf-8")
    assert "st_mtime" not in source, (
        "public_exposure.py compares mtimes again; freshness belongs to "
        "scripts/outputs_provenance.py so the repository has one notion of it"
    )
    assert "assess_freshness" in source
