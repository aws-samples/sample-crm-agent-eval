# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.api_routes`: the Layer 4 per-API-route smoke script.

Exercises `checks()` against a fake `apigateway` (v1) client and an injected HTTP-call seam: route
enumeration, the toolspec cross-check, unauthenticated 401, authorizers, throttling, request
validators, the authenticated call, the outputs and client the module needs, and the
private-endpoint posture.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import boto3
import pytest
from moto import mock_aws
from scripts.smoke import api_routes, verified_claims
from scripts.smoke.api_routes import HttpResult, checks
from scripts.smoke.cognito_claims import LiveSmokeUserProvisioner
from scripts.smoke.harness import (
    MissingTerraformOutputError,
    UninjectedAwsClientError,
    context_for_test,
)

from crm.auth import pre_token_generation
from crm.authz import ADMIN_GROUP
from crm.envelope import API_ALLOWED_CLIENT_IDS_PARAMETER_ENV
from crm.handlers import eval_trigger as eval_trigger_handler
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.toolspec.definitions import ALL_TOOLS
from eval.trigger import EVAL_ADMIN_SCOPE
from eval.verification import CheckResult
from tests.conftest import TEST_API_CLIENT_ID
from tests.unit import _jwks_test_key as test_key

API_ENDPOINT = "https://abc123.execute-api.us-east-1.amazonaws.com"
#: The base the module calls: the distribution's `/api/*` behaviour, not `api_stage_invoke_url`.
CLOUDFRONT_DOMAIN = "d111111abcdef8.cloudfront.net"
API_BASE_URL = f"https://{CLOUDFRONT_DOMAIN}/api"
API_ID = "abc123"
AUTHORIZER_ID = "auth-1"

#: A disposable `eval-` smoke tenant, the only shape the authenticated pass will write into.
SMOKE_TENANT = "eval-smoke-0123456789abcdef0123456789abcdef"

TOOL_NAMES = [tool.name for tool in ALL_TOOLS]


#: The Cognito outputs `api_routes.token_claims` verifies a token against.
COGNITO_OUTPUTS: dict[str, str] = {
    "cognito_user_pool_id": test_key.USER_POOL_ID,
    "cognito_spa_client_id": test_key.CLIENT_ID,
}


@pytest.fixture(autouse=True)
def _loopback_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tokens verify against the loopback test JWKS instead of the pool's public endpoint."""
    monkeypatch.setattr(verified_claims, "cognito_jwks_url", lambda _r, _p: test_key.jwks_url())


def _fake_token(tenant: str) -> str:
    """A signed ID token for the test pool carrying `custom:tenant` = ``tenant``."""
    return test_key.id_token(**{"custom:tenant": tenant})


class _RecordingLifecycle:
    """Records seed/teardown calls so a test can assert the tenant lifecycle without AWS."""

    def __init__(self) -> None:
        self.seeded: list[str] = []
        self.torn_down: list[str] = []

    def seed(self, tenant: str) -> None:
        self.seeded.append(tenant)

    def teardown(self, tenant: str) -> dict[str, int]:
        self.torn_down.append(tenant)
        return {"accounts": 0}


class FakeApiGatewayV2Client:
    """Minimal stand-in for a boto3 **`apigateway` (v1)** client, keyed to what `checks()` calls.

    Despite the class name it serves the v1 surface (`get_resources` + `get_method`). As in the real
    API, `authorizationType` comes from `get_method`, not from `get_resources`.
    """

    def __init__(
        self,
        *,
        route_keys: list[str],
        authorizer_ids: Mapping[str, str] | None = None,
        stages: list[dict[str, Any]] | None = None,
        endpoint_types: list[str] | None = None,
        vpc_endpoint_ids: list[str] | None = None,
        request_validator_present: bool = True,
        methods_missing_validator: set[str] | None = None,
        posts_missing_model: set[str] | None = None,
    ) -> None:
        self._route_keys = route_keys
        self._authorizer_ids = dict(authorizer_ids or {})
        # Defaults: one validator, referenced on every method, and the object model on every POST.
        self._request_validator_present = request_validator_present
        self._methods_missing_validator = set(methods_missing_validator or set())
        self._posts_missing_model = set(posts_missing_model or set())
        self._stages = stages if stages is not None else [_default_stage()]
        #: Defaults are the correct private-endpoint posture. The `vpce-` placeholder is assembled
        #: so no id literal appears in tracked content.
        self._endpoint_types = ["PRIVATE"] if endpoint_types is None else endpoint_types
        self._vpc_endpoint_ids = (
            ["vpce-" + "0" * 17] if vpc_endpoint_ids is None else vpc_endpoint_ids
        )

    def get_rest_api(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "id": API_ID,
            "endpointConfiguration": {
                "types": list(self._endpoint_types),
                "vpcEndpointIds": list(self._vpc_endpoint_ids),
            },
        }

    def get_resources(self, **_kwargs: Any) -> dict[str, Any]:
        items = []
        for index, route_key in enumerate(self._route_keys):
            http_method, path = route_key.split(" ", 1)
            items.append(
                {
                    "id": f"res{index}",
                    "path": path,
                    "resourceMethods": {http_method: {}},
                }
            )
        return {"items": items}

    def get_request_validators(self, **_kwargs: Any) -> dict[str, Any]:
        """Return the body-and-params validator, or none if `request_validator_present=False`."""
        if not self._request_validator_present:
            return {"items": []}
        return {"items": [{"id": "rv1", "name": "body-and-params"}]}

    def get_method(self, **kwargs: Any) -> dict[str, Any]:
        resource_id = str(kwargs["resourceId"])
        index = int(resource_id.removeprefix("res"))
        route_key = self._route_keys[index]
        http_method = route_key.split(" ", 1)[0]
        method: dict[str, Any] = {
            "authorizationType": "COGNITO_USER_POOLS",
            "authorizerId": self._authorizer_ids.get(route_key, AUTHORIZER_ID),
        }
        if self._request_validator_present and route_key not in self._methods_missing_validator:
            method["requestValidatorId"] = "rv1"
        if http_method.upper() == "POST" and route_key not in self._posts_missing_model:
            method["requestModels"] = {"application/json": "JsonObject"}
        return method

    def get_stages(self, **_kwargs: Any) -> dict[str, Any]:
        # The v1 response key is `item`, not `Items`.
        return {"item": self._stages}


def _default_stage() -> dict[str, Any]:
    """A v1 stage, with stage-level throttling in `methodSettings["*/*"]`."""
    return {
        "stageName": "prod",
        "methodSettings": {
            "*/*": {"throttlingBurstLimit": 200, "throttlingRateLimit": 100},
        },
    }


def _all_route_keys() -> list[str]:
    return [f"POST /{name}" for name in TOOL_NAMES]


def _make_ctx(client: FakeApiGatewayV2Client) -> Any:
    return context_for_test(
        outputs={"api_id": API_ID, "cloudfront_domain_name": CLOUDFRONT_DOMAIN, **COGNITO_OUTPUTS},
        clients={"apigateway": client},
    )


def _always_401(_method: str, _url: str, _headers: Mapping[str, str], _body: str) -> HttpResult:
    return 401, ""


def test_enumerates_routes_from_deployed_api_not_static_list() -> None:
    """A deployed route not in the toolspec still produces checks."""
    undeclared_route = "POST /some_undeclared_tool"
    client = FakeApiGatewayV2Client(route_keys=[*_all_route_keys(), undeclared_route])
    ctx = _make_ctx(client)

    results = list(checks(ctx, http_caller=_always_401, token=None))

    resources = {r.resource for r in results}
    assert "route:POST /some_undeclared_tool" in resources
    undeclared_findings = [
        r for r in results if r.check == "route_has_tool" and "some_undeclared_tool" in r.detail
    ]
    assert len(undeclared_findings) == 1
    assert undeclared_findings[0].status == "fail"


def test_toolspec_tool_with_no_route_is_reported() -> None:
    """A toolspec tool missing its deployed route is reported as a distinct failing check,
    not conflated with route enumeration."""
    missing_tool = TOOL_NAMES[0]
    route_keys = [f"POST /{name}" for name in TOOL_NAMES if name != missing_tool]
    client = FakeApiGatewayV2Client(route_keys=route_keys)
    ctx = _make_ctx(client)

    results = list(checks(ctx, http_caller=_always_401, token=None))

    findings = [r for r in results if r.check == "toolspec_has_route" and missing_tool in r.detail]
    assert len(findings) == 1
    assert findings[0].status == "fail"


def test_enumeration_and_cross_check_are_separate_checks() -> None:
    """The enumeration check and the toolspec cross-check are reported under different `check`
    names."""
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys())
    ctx = _make_ctx(client)

    results = list(checks(ctx, http_caller=_always_401, token=None))

    enumerate_checks = [r for r in results if r.check == "enumerate_routes"]
    assert len(enumerate_checks) == 1
    assert enumerate_checks[0].status == "pass"
    cross_check_names = {r.check for r in results} & {"toolspec_has_route", "route_has_tool"}
    assert cross_check_names == set() or all(
        r.check != "enumerate_routes" for r in results if r.check in cross_check_names
    )


def test_unauthenticated_401_assertion_is_non_vacuous() -> None:
    """A route that returns 200 unauthenticated is reported as a failing check."""
    route_keys = _all_route_keys()
    client = FakeApiGatewayV2Client(route_keys=route_keys)
    ctx = _make_ctx(client)
    broken_route_key = route_keys[0]
    broken_path = broken_route_key.split(" ", 1)[1]

    def caller(_method: str, url: str, _headers: Mapping[str, str], _body: str) -> HttpResult:
        if url.endswith(broken_path):
            return 200, "{}"
        return 401, ""

    results = list(checks(ctx, http_caller=caller, token=None))

    broken_resource = f"route:{broken_route_key}"
    broken_checks = [
        r
        for r in results
        if r.resource == broken_resource and r.check == "unauthenticated_rejected"
    ]
    assert len(broken_checks) == 1
    assert broken_checks[0].status == "fail"
    assert "401" in broken_checks[0].detail or "got 200" in broken_checks[0].detail


def test_unauthenticated_403_is_reported_as_distinct_failure_from_401() -> None:
    """An unauthenticated 403 is a failure distinct from the 401 case."""
    route_keys = _all_route_keys()
    client = FakeApiGatewayV2Client(route_keys=route_keys)
    ctx = _make_ctx(client)
    target_route_key = route_keys[0]
    target_path = target_route_key.split(" ", 1)[1]

    def caller(_method: str, url: str, _headers: Mapping[str, str], _body: str) -> HttpResult:
        if url.endswith(target_path):
            return 403, ""
        return 401, ""

    results = list(checks(ctx, http_caller=caller, token=None))

    target_resource = f"route:{target_route_key}"
    target_checks = [
        r
        for r in results
        if r.resource == target_resource and r.check == "unauthenticated_rejected"
    ]
    assert len(target_checks) == 1
    assert target_checks[0].status == "fail"
    assert "403" in target_checks[0].detail


def test_all_routes_still_checked_when_first_route_fails() -> None:
    """With N routes and the first failing, all N still appear in the inventory."""
    route_keys = _all_route_keys()
    client = FakeApiGatewayV2Client(route_keys=route_keys)
    ctx = _make_ctx(client)
    first_path = route_keys[0].split(" ", 1)[1]

    def caller(_method: str, url: str, _headers: Mapping[str, str], _body: str) -> HttpResult:
        if url.endswith(first_path):
            return 200, "{}"  # wrongly succeeds unauthenticated
        return 401, ""

    results = list(checks(ctx, http_caller=caller, token=None))

    checked_resources = {r.resource for r in results if r.check == "unauthenticated_rejected"}
    expected_resources = {f"route:{route_key}" for route_key in route_keys}
    assert checked_resources == expected_resources
    assert len(route_keys) == len(TOOL_NAMES)


def test_authorizer_missing_on_a_route_is_detected() -> None:
    """A route with no authorizer attached must be reported as a failing check."""
    route_keys = _all_route_keys()
    unauthorized_route = route_keys[0]
    client = FakeApiGatewayV2Client(route_keys=route_keys, authorizer_ids={unauthorized_route: ""})
    ctx = _make_ctx(client)

    results = list(checks(ctx, http_caller=_always_401, token=None))

    findings = [
        r
        for r in results
        if r.resource == f"route:{unauthorized_route}" and r.check == "authorizer_attached"
    ]
    assert len(findings) == 1
    assert findings[0].status == "fail"


def test_authorizer_present_on_all_routes_passes() -> None:
    """All routes carrying an authorizer id pass the authorizer-attached check."""
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys())
    ctx = _make_ctx(client)

    results = list(checks(ctx, http_caller=_always_401, token=None))

    authorizer_checks = [r for r in results if r.check == "authorizer_attached"]
    assert len(authorizer_checks) == len(TOOL_NAMES)
    assert all(r.status == "pass" for r in authorizer_checks)


def test_throttle_and_burst_configured_passes_when_present() -> None:
    """Per-stage throttle and burst limits both present -> pass."""
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys())
    ctx = _make_ctx(client)

    results = list(checks(ctx, http_caller=_always_401, token=None))

    stage_checks = [r for r in results if r.check == "throttle_and_burst_configured"]
    assert len(stage_checks) == 1
    assert stage_checks[0].status == "pass"


def test_throttle_and_burst_missing_fails() -> None:
    """A stage missing throttle/burst settings must fail, not silently pass."""
    broken_stage = {"StageName": "prod", "DefaultRouteSettings": {}}
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys(), stages=[broken_stage])
    ctx = _make_ctx(client)

    results = list(checks(ctx, http_caller=_always_401, token=None))

    stage_checks = [r for r in results if r.check == "throttle_and_burst_configured"]
    assert len(stage_checks) == 1
    assert stage_checks[0].status == "fail"


def test_request_validators_present_and_referenced_by_every_method() -> None:
    """One validator referenced by every method, and the object model on every POST, passes both
    validator checks."""
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys())
    ctx = _make_ctx(client)

    results = list(checks(ctx, http_caller=_always_401, token=None))

    present = [r for r in results if r.check == "request_validators_present"]
    per_method = [r for r in results if r.check == "request_validator_on_every_method"]
    assert len(present) == 1 and present[0].status == "pass", present
    assert len(per_method) == 1 and per_method[0].status == "pass", per_method


def test_request_validators_absent_is_a_fail_not_a_skip() -> None:
    """A deployment that declares no validator fails `request_validators_present`, never skips."""
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys(), request_validator_present=False)
    results = list(checks(_make_ctx(client), http_caller=_always_401, token=None))
    present = [r for r in results if r.check == "request_validators_present"]
    assert len(present) == 1 and present[0].status == "fail", present
    assert "no request validator" in present[0].detail


def test_a_method_missing_its_validator_id_fails_the_per_method_check() -> None:
    """A method with no `requestValidatorId` fails `request_validator_on_every_method` even though
    the API-level validator exists."""
    route_keys = _all_route_keys()
    client = FakeApiGatewayV2Client(
        route_keys=route_keys, methods_missing_validator={route_keys[0]}
    )
    results = list(checks(_make_ctx(client), http_caller=_always_401, token=None))
    per_method = [r for r in results if r.check == "request_validator_on_every_method"]
    assert len(per_method) == 1 and per_method[0].status == "fail", per_method
    assert route_keys[0] in per_method[0].detail


def test_a_post_missing_its_object_model_fails_the_per_method_check() -> None:
    """A POST without the application/json object model fails the per-method check."""
    post_route = next(rk for rk in _all_route_keys() if rk.startswith("POST "))
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys(), posts_missing_model={post_route})
    results = list(checks(_make_ctx(client), http_caller=_always_401, token=None))
    per_method = [r for r in results if r.check == "request_validator_on_every_method"]
    assert len(per_method) == 1 and per_method[0].status == "fail", per_method
    assert post_route in per_method[0].detail


def test_authenticated_call_fails_without_token() -> None:
    """Without a supplied token, the authenticated-call check is `fail`."""
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys())
    ctx = _make_ctx(client)

    results = list(checks(ctx, http_caller=_always_401, token=None))

    auth_checks = [r for r in results if r.check == "authenticated_non_error"]
    assert len(auth_checks) == len(TOOL_NAMES)
    assert all(r.status == "fail" for r in auth_checks)


def test_authenticated_call_with_token_passes_on_non_error_response() -> None:
    """With a supplied token whose tenant claim names a disposable smoke tenant, a non-error
    authenticated response passes, and the tenant is seeded before and torn down after."""
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys())
    ctx = _make_ctx(client)

    def caller(_method: str, _url: str, headers: Mapping[str, str], _body: str) -> HttpResult:
        if "Authorization" in headers:
            return 200, "{}"
        return 401, ""

    lifecycle = _RecordingLifecycle()
    results = list(
        checks(
            ctx,
            http_caller=caller,
            token=_fake_token(SMOKE_TENANT),
            seeder=lifecycle.seed,
            teardown=lifecycle.teardown,
        )
    )

    auth_checks = [r for r in results if r.check == "authenticated_non_error"]
    assert len(auth_checks) == len(TOOL_NAMES)
    assert {r.status for r in auth_checks} == {"pass", "skip"}
    assert [r for r in auth_checks if r.status == "skip"] != []
    assert lifecycle.seeded == [SMOKE_TENANT]
    assert lifecycle.torn_down == [SMOKE_TENANT]
    tenant_checks = [r for r in results if r.check == "tenant_left_clean"]
    assert len(tenant_checks) == 1
    assert tenant_checks[0].status == "pass"


def test_authenticated_call_sends_the_tools_own_required_arguments() -> None:
    """The authenticated call sends a body satisfying the route tool's input schema."""
    tool = next(t for t in ALL_TOOLS if t.input_schema.get("required"))
    client = FakeApiGatewayV2Client(route_keys=[f"POST /{tool.name}"])
    ctx = _make_ctx(client)
    bodies: dict[str, str] = {}

    def caller(_method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
        if "Authorization" in headers:
            bodies[url] = body
            return 200, "{}"
        return 401, ""

    lifecycle = _RecordingLifecycle()
    list(
        checks(
            ctx,
            http_caller=caller,
            token=_fake_token(SMOKE_TENANT),
            seeder=lifecycle.seed,
            teardown=lifecycle.teardown,
        )
    )

    assert len(bodies) == 1
    sent = json.loads(next(iter(bodies.values())))
    for required in tool.input_schema["required"]:
        assert required in sent, f"required property {required!r} missing from request body"
    assert sent != {}


def test_authenticated_call_refused_for_a_demo_scoped_token() -> None:
    """A token scoped to the `demo` tenant produces a failing check and no authenticated call."""
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys())
    ctx = _make_ctx(client)
    calls: list[str] = []

    def caller(_method: str, url: str, headers: Mapping[str, str], _body: str) -> HttpResult:
        if "Authorization" in headers:
            calls.append(url)
            return 200, "{}"
        return 401, ""

    lifecycle = _RecordingLifecycle()
    results = list(
        checks(
            ctx,
            http_caller=caller,
            token=_fake_token("demo"),
            seeder=lifecycle.seed,
            teardown=lifecycle.teardown,
        )
    )

    assert calls == []
    assert lifecycle.seeded == []
    tenant_checks = [r for r in results if r.check == "token_tenant_is_disposable_smoke_tenant"]
    assert len(tenant_checks) == 1
    assert tenant_checks[0].status == "fail"
    assert "demo" in tenant_checks[0].detail
    auth_checks = [r for r in results if r.check == "authenticated_non_error"]
    assert len(auth_checks) == len(TOOL_NAMES)
    assert all(r.status == "fail" for r in auth_checks)


@pytest.mark.parametrize("ctx_tenant", [None, SMOKE_TENANT])
def test_authenticated_call_refused_for_a_forged_token(ctx_tenant: str | None) -> None:
    """A smoke-tenant token signed by a key the pool never published makes no authenticated call,
    including when `--tenant` already names the tenant, so the token's claim is never consulted."""
    forged = test_key.TestKey().sign(test_key.id_token_claims(**{"custom:tenant": SMOKE_TENANT}))
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys())
    ctx = context_for_test(
        outputs={"api_id": API_ID, "cloudfront_domain_name": CLOUDFRONT_DOMAIN, **COGNITO_OUTPUTS},
        clients={"apigateway": client},
        tenant=ctx_tenant,
    )
    calls: list[str] = []

    def caller(_method: str, url: str, headers: Mapping[str, str], _body: str) -> HttpResult:
        if "Authorization" in headers:
            calls.append(url)
            return 200, "{}"
        return 401, ""

    lifecycle = _RecordingLifecycle()
    results = list(
        checks(
            ctx,
            http_caller=caller,
            token=forged,
            seeder=lifecycle.seed,
            teardown=lifecycle.teardown,
        )
    )

    assert calls == []
    assert lifecycle.seeded == []
    (tenant_check,) = [r for r in results if r.check == "token_tenant_is_disposable_smoke_tenant"]
    assert tenant_check.status == "fail"
    assert "failed verification" in tenant_check.detail and "signature" in tenant_check.detail
    auth_checks = [r for r in results if r.check == "authenticated_non_error"]
    assert len(auth_checks) == len(TOOL_NAMES)
    assert all(r.status == "fail" for r in auth_checks)


def test_token_claims_returns_a_verified_tokens_claims() -> None:
    ctx = context_for_test(outputs=COGNITO_OUTPUTS)
    claims = api_routes.token_claims(ctx, test_key.id_token(**{"custom:tenant": SMOKE_TENANT}))
    assert claims["custom:tenant"] == SMOKE_TENANT


def test_token_claims_refuses_an_access_token_and_a_foreign_clients_token() -> None:
    ctx = context_for_test(outputs=COGNITO_OUTPUTS)
    for token, check in (
        (test_key.access_token(aud=test_key.CLIENT_ID), "token_use"),
        (test_key.id_token(aud="another-client"), "aud"),
    ):
        with pytest.raises(verified_claims.ClaimsVerificationError) as info:
            api_routes.token_claims(ctx, token)
        assert info.value.check == check


def test_authenticated_call_refused_for_a_token_with_no_tenant_claim() -> None:
    """A token with no tenant claim makes no authenticated call and is reported as a failure."""
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys())
    ctx = _make_ctx(client)
    calls: list[str] = []

    def caller(_method: str, url: str, headers: Mapping[str, str], _body: str) -> HttpResult:
        if "Authorization" in headers:
            calls.append(url)
            return 200, "{}"
        return 401, ""

    results = list(checks(ctx, http_caller=caller, token="not-a-jwt"))  # noqa: S106  # nosec B106  # malformed-token fixture, not a credential

    assert calls == []
    tenant_checks = [r for r in results if r.check == "token_tenant_is_disposable_smoke_tenant"]
    assert len(tenant_checks) == 1
    assert tenant_checks[0].status == "fail"


def test_authenticated_call_with_token_fails_on_error_response() -> None:
    """With a supplied token, an error authenticated response is a failing check, not a
    silent pass."""
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys())
    ctx = _make_ctx(client)

    def caller(_method: str, _url: str, headers: Mapping[str, str], _body: str) -> HttpResult:
        if "Authorization" in headers:
            return 500, "internal error"
        return 401, ""

    lifecycle = _RecordingLifecycle()
    results = list(
        checks(
            ctx,
            http_caller=caller,
            token=_fake_token(SMOKE_TENANT),
            seeder=lifecycle.seed,
            teardown=lifecycle.teardown,
        )
    )

    auth_checks = [
        r for r in results if r.check == "authenticated_non_error" and r.status != "skip"
    ]
    assert auth_checks != []
    assert all(r.status == "fail" for r in auth_checks)
    assert all("500" in r.detail for r in auth_checks)


EVAL_TRIGGER_ROUTE = "POST /eval_trigger"


class _AbsentJobStore:
    """A `Store` stand-in whose every `get_item` finds nothing, recording each key it was asked for.

    Any other store method is recorded in ``other_calls`` and then fails, so a write cannot pass.
    """

    def __init__(self) -> None:
        self.reads: list[tuple[str, dict[str, str]]] = []
        self.other_calls: list[str] = []

    def get_item(self, table: str, key: Mapping[str, str]) -> None:
        self.reads.append((table, dict(key)))

    def __getattr__(self, name: str) -> Any:
        self.other_calls.append(name)
        raise AttributeError(name)


def _token_with_claims(claims: Mapping[str, Any]) -> str:
    """A signed ID token for the test pool carrying ``claims``."""
    return test_key.id_token(**claims)


def _authorizer_claims(token: str) -> dict[str, Any]:
    """The claims the deployed Cognito authorizer delivers for ``token``, once it verifies."""
    return verified_claims.verified_claims(
        token,
        region=test_key.REGION,
        user_pool_id=test_key.USER_POOL_ID,
        client_id=test_key.CLIENT_ID,
        token_use=verified_claims.USE_ID,
    )


def _eval_trigger_v1_event(body: str, claims: Mapping[str, Any]) -> dict[str, Any]:
    """A REST (payload v1) proxy event as the deployed Cognito-authorized route delivers it."""
    return {
        "resource": "/eval_trigger",
        "path": "/eval_trigger",
        "httpMethod": "POST",
        "headers": {"content-type": "application/json"},
        "requestContext": {
            "authorizer": {"claims": {**claims, "token_use": "id", "aud": TEST_API_CLIENT_ID}}
        },
        "body": body,
        "isBase64Encoded": False,
    }


def _real_eval_trigger_caller(store: _AbsentJobStore) -> api_routes.HttpCaller:
    """An HTTP seam routing an authenticated `/eval_trigger` call into the real handler.

    The authorizer's claims are the presented bearer token's own payload, as Cognito delivers them.
    """

    def caller(_method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
        authorization = headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return 401, ""
        assert url == f"{API_BASE_URL}/eval_trigger", url
        claims = _authorizer_claims(authorization.removeprefix("Bearer "))
        response = eval_trigger_handler.handler(
            _eval_trigger_v1_event(body, claims), None, store=cast("Store", store)
        )
        return int(response["statusCode"]), str(response["body"])

    return caller


def _eval_trigger_row(caller: api_routes.HttpCaller, token: str) -> CheckResult:
    client = FakeApiGatewayV2Client(route_keys=[EVAL_TRIGGER_ROUTE])
    lifecycle = _RecordingLifecycle()
    results = list(
        checks(
            _make_ctx(client),
            http_caller=caller,
            token=token,
            seeder=lifecycle.seed,
            teardown=lifecycle.teardown,
        )
    )
    rows = [
        r
        for r in results
        if r.check == "authenticated_non_error" and r.resource == f"route:{EVAL_TRIGGER_ROUTE}"
    ]
    assert len(rows) == 1, rows
    return rows[0]


def _claims(full_visibility: str) -> dict[str, str]:
    return {
        "custom:rep_id": "rep-smoke",
        "custom:tenant": SMOKE_TENANT,
        "custom:book": "",
        "custom:full_visibility": full_visibility,
    }


def _admin_claims(full_visibility: str = "false") -> dict[str, str]:
    return {**_claims(full_visibility), "cognito:groups": ADMIN_GROUP}


_ADMIN_DENIED_BODY = json.dumps(
    {"error": {"code": "scope_denied", "message": "x", "missing_scope": EVAL_ADMIN_SCOPE}}
)
_NOT_FOUND_BODY = json.dumps({"error": {"code": "not_found", "message": "x"}})


class _CreateOnlyIdp:
    """The two `cognito-idp` calls `LiveSmokeUserProvisioner.provision` makes before authenticating."""

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
        return {"User": {"Username": kwargs["Username"]}}

    def admin_set_user_password(self, **_kwargs: Any) -> dict[str, Any]:
        return {}


def _producer_claims(
    monkeypatch: pytest.MonkeyPatch, *, grant_full_visibility: bool
) -> dict[str, str]:
    """Return the ID-token claims the real producers stamp for the orchestrator's smoke user.

    `LiveSmokeUserProvisioner.provision` writes the `reps` row (into moto DynamoDB), and the real
    `pre_token_generation.lambda_handler` turns that row into claims. ``grant_full_visibility``
    rewrites the row's flag to ``True`` first, which the provisioner itself never does.
    """
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(dynamodb)
        ctx = context_for_test(
            outputs={
                "cognito_user_pool_id": "us-east-1_TestPool",
                "cognito_spa_client_id": "test-spa-client",
                "dynamodb_table_names": {"eval_runs": "eval_runs"},
            },
            clients={"cognito-idp": _CreateOnlyIdp(), "dynamodb": dynamodb},
        )
        provisioner = LiveSmokeUserProvisioner(ctx)
        monkeypatch.setattr(
            provisioner, "_authenticate_with_mfa", lambda **_kwargs: ("unused", "MFA-completed")
        )
        provisioned = provisioner.provision()
        store = Store(client=dynamodb)
        row = store.get_item("reps", {"rep_id": provisioned.rep_id})
        assert row is not None, "the provisioner wrote no reps row"
        if grant_full_visibility:
            store.put_item("reps", {**row, "full_visibility": True})
        event = pre_token_generation.lambda_handler(
            {"userName": provisioned.rep_id, "request": {}, "response": {}}, None, store=store
        )
    overrides = event["response"]["claimsAndScopeOverrideDetails"]["idTokenGeneration"]
    return dict(overrides["claimsToAddOrOverride"])


class TestEvalTriggerProbeProducerContract:
    """The probe's expectation agrees with the claims the real producers put in the smoke token."""

    @pytest.mark.parametrize("grant_full_visibility", [False, True])
    def test_the_orchestrator_smoke_user_is_refused_eval_admin_reading_nothing(
        self, monkeypatch: pytest.MonkeyPatch, grant_full_visibility: bool
    ) -> None:
        claims = _producer_claims(monkeypatch, grant_full_visibility=grant_full_visibility)
        assert not api_routes.token_is_admin(claims)
        store = _AbsentJobStore()

        row = _eval_trigger_row(_real_eval_trigger_caller(store), _token_with_claims(claims))

        assert row.status == "pass", row.detail
        assert "status=403" in row.detail and "error.code=scope_denied" in row.detail
        assert f"error.missing_scope={EVAL_ADMIN_SCOPE}" in row.detail
        assert store.reads == [] and store.other_calls == [], "the admin gate precedes the store"

    def test_an_admin_is_not_refused_there_and_finds_no_run(self) -> None:
        """Falsifying cell: the refusal the probe expects is the admin gate's, not any refusal."""
        store = _AbsentJobStore()

        row = _eval_trigger_row(
            _real_eval_trigger_caller(store), _token_with_claims(_admin_claims())
        )

        assert row.status == "pass", row.detail
        assert "status=404" in row.detail and "error.code=not_found" in row.detail
        expected_run_id = api_routes.eval_trigger_probe_body(SMOKE_TENANT)["run_id"]
        assert store.reads == [("eval_runs", {"run_id": f"job#{expected_run_id}"})]
        assert store.other_calls == [], "a cancel of an absent run writes nothing"


class TestExpectedEvalTriggerStatus:
    """`expected_eval_trigger_status` reads the token's own `cognito:groups` claim."""

    @pytest.mark.parametrize("full_visibility", ["true", "false"])
    def test_a_non_admin_expects_the_admin_gate_refusal(self, full_visibility: str) -> None:
        assert api_routes.expected_eval_trigger_status(
            _claims(full_visibility)
        ) == api_routes.ProbeExpectation(403, "scope_denied", EVAL_ADMIN_SCOPE)

    @pytest.mark.parametrize("groups", ["reps", "[]", '["reps","viewers"]', ["reps"]])
    def test_other_groups_are_not_admin(self, groups: object) -> None:
        claims: dict[str, object] = {**_claims("false"), "cognito:groups": groups}
        assert api_routes.expected_eval_trigger_status(claims).missing_scope == EVAL_ADMIN_SCOPE

    @pytest.mark.parametrize("groups", [ADMIN_GROUP, f"reps,{ADMIN_GROUP}", [ADMIN_GROUP]])
    def test_an_admin_expects_not_found(self, groups: object) -> None:
        claims: dict[str, object] = {**_claims("false"), "cognito:groups": groups}
        assert api_routes.expected_eval_trigger_status(claims) == api_routes.ProbeExpectation(
            404, "not_found"
        )

    @pytest.mark.parametrize("groups", [1, {"admin": True}, "[1]", [1]])
    def test_an_unreadable_groups_claim_raises(self, groups: object) -> None:
        claims: dict[str, object] = {**_claims("false"), "cognito:groups": groups}
        with pytest.raises(api_routes.UnreadableClaimError, match="cognito:groups"):
            api_routes.expected_eval_trigger_status(claims)


class TestTokenHasFullVisibility:
    """`token_has_full_visibility` reads the token's own `custom:full_visibility` claim."""

    @pytest.mark.parametrize("value", ["true", "TRUE", " true ", "1", "yes"])
    def test_a_true_spelling_is_full_visibility(self, value: str) -> None:
        assert api_routes.token_has_full_visibility(_claims(value)) is True

    @pytest.mark.parametrize("value", ["false", "False", "0", "no"])
    def test_a_false_spelling_is_not(self, value: str) -> None:
        assert api_routes.token_has_full_visibility(_claims(value)) is False

    def test_a_missing_claim_raises_naming_the_claim(self) -> None:
        claims = _claims("true")
        del claims["custom:full_visibility"]
        with pytest.raises(api_routes.UnreadableClaimError, match="no custom:full_visibility"):
            api_routes.token_has_full_visibility(claims)

    @pytest.mark.parametrize("value", ["maybe", "", None, True, 1])
    def test_an_unreadable_claim_raises(self, value: object) -> None:
        claims: dict[str, object] = {**_claims("true"), "custom:full_visibility": value}
        with pytest.raises(api_routes.UnreadableClaimError):
            api_routes.token_has_full_visibility(claims)


class TestEvalTriggerProbe:
    """The non-tool `eval_trigger` route passes only on the answer the token's claims call for."""

    def test_a_non_admin_token_answered_with_another_scope_fails(self) -> None:
        """A refusal naming `eval:trigger` is not the admin gate's refusal."""
        wrong_gate = json.dumps(
            {"error": {"code": "scope_denied", "message": "x", "missing_scope": "eval:trigger"}}
        )

        def caller(_method: str, _url: str, headers: Mapping[str, str], _body: str) -> HttpResult:
            if "Authorization" in headers:
                return 403, wrong_gate
            return 401, ""

        row = _eval_trigger_row(caller, _token_with_claims(_claims("false")))

        assert row.status == "fail"
        assert f"error.missing_scope={EVAL_ADMIN_SCOPE}, got" in row.detail

    def test_a_non_admin_token_answered_not_found_fails(self) -> None:
        """A 404 means the admin gate let a non-admin caller through."""

        def caller(_method: str, _url: str, headers: Mapping[str, str], _body: str) -> HttpResult:
            if "Authorization" in headers:
                return 404, _NOT_FOUND_BODY
            return 401, ""

        row = _eval_trigger_row(caller, _token_with_claims(_claims("false")))

        assert row.status == "fail"
        assert "expected status=403 error.code=scope_denied" in row.detail

    def test_a_token_evaluated_as_admin_by_the_real_handler_would_fail(self) -> None:
        """Control: a non-admin token the handler evaluates as admin is not refused, so it fails."""
        store = _AbsentJobStore()

        def elevated(method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
            upgraded = {"Authorization": f"Bearer {_token_with_claims(_admin_claims())}"}
            return _real_eval_trigger_caller(store)(method, url, upgraded, body)

        row = _eval_trigger_row(elevated, _token_with_claims(_claims("false")))

        assert row.status == "fail", row.detail
        assert "got status=404 error.code=not_found" in row.detail

    def test_an_admin_token_evaluated_as_non_admin_by_the_real_handler_would_fail(self) -> None:
        store = _AbsentJobStore()

        def downgraded(method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
            plain = {"Authorization": f"Bearer {_token_with_claims(_claims('false'))}"}
            return _real_eval_trigger_caller(store)(method, url, plain, body)

        row = _eval_trigger_row(downgraded, _token_with_claims(_admin_claims()))

        assert row.status == "fail", row.detail
        assert "got status=403 error.code=scope_denied" in row.detail

    def test_the_probe_body_is_a_cancel_deterministic_per_tenant_and_never_a_job_row_key(
        self,
    ) -> None:
        body = api_routes.eval_trigger_probe_body(SMOKE_TENANT)
        assert body["action"] == eval_trigger_handler.ACTION_CANCEL
        assert set(body) == {"action", "run_id"}
        assert body["run_id"] and "#" not in body["run_id"]
        assert body == api_routes.eval_trigger_probe_body(SMOKE_TENANT)
        assert body != api_routes.eval_trigger_probe_body(f"{SMOKE_TENANT}x")

    @pytest.mark.parametrize(
        ("claims", "status", "response_body"),
        [
            (
                _claims("false"),
                403,
                json.dumps({"error": {"code": "scope_denied", "message": "x"}}),
            ),
            (_claims("false"), 400, json.dumps({"error": {"code": "validation_failed"}})),
            (_claims("false"), 401, json.dumps({"error": {"code": "authentication_failed"}})),
            (_claims("false"), 200, json.dumps({"disposition": "accepted"})),
            (_claims("false"), 403, json.dumps({"message": "Missing Authentication Token"})),
            (_claims("false"), 403, "not json"),
            (_admin_claims(), 403, _ADMIN_DENIED_BODY),
            (_admin_claims(), 200, json.dumps({"disposition": "accepted"})),
            (_admin_claims(), 404, json.dumps({"error": {"code": "something_else"}})),
            (_admin_claims(), 404, "not json"),
        ],
    )
    def test_any_answer_other_than_the_derived_one_fails(
        self, claims: Mapping[str, str], status: int, response_body: str
    ) -> None:
        def caller(_method: str, _url: str, headers: Mapping[str, str], _body: str) -> HttpResult:
            if "Authorization" in headers:
                return status, response_body
            return 401, ""

        row = _eval_trigger_row(caller, _token_with_claims(claims))

        assert row.status == "fail", row.detail

    @pytest.mark.parametrize(
        ("claims", "status", "response_body"),
        [(_claims("false"), 403, _ADMIN_DENIED_BODY), (_admin_claims(), 404, _NOT_FOUND_BODY)],
        ids=["non-admin-403", "admin-404"],
    )
    def test_the_derived_answer_passes(
        self, claims: Mapping[str, str], status: int, response_body: str
    ) -> None:
        def caller(_method: str, _url: str, headers: Mapping[str, str], _body: str) -> HttpResult:
            if "Authorization" in headers:
                return status, response_body
            return 401, ""

        row = _eval_trigger_row(caller, _token_with_claims(claims))

        assert row.status == "pass", row.detail

    def test_an_unreadable_groups_claim_fails_without_calling_the_route(self) -> None:
        calls: list[str] = []

        def caller(_method: str, url: str, headers: Mapping[str, str], _body: str) -> HttpResult:
            if "Authorization" in headers:
                calls.append(url)
                return 403, _ADMIN_DENIED_BODY
            return 401, ""

        claims: dict[str, object] = {**_claims("false"), "cognito:groups": 1}
        row = _eval_trigger_row(caller, _token_with_claims(claims))

        assert row.status == "fail"
        assert "cognito:groups is unreadable" in row.detail
        assert calls == []

    def test_a_transport_failure_fails_the_probe(self) -> None:
        def caller(_method: str, url: str, headers: Mapping[str, str], _body: str) -> HttpResult:
            if "Authorization" in headers:
                return f"transport error calling {url}: connection refused"
            return 401, ""

        row = _eval_trigger_row(caller, _token_with_claims(_claims("false")))

        assert row.status == "fail"
        assert "transport error" in row.detail


USER_SETTINGS_ROUTE = "POST /user_settings"


def _real_user_settings_caller(store: _AbsentJobStore) -> api_routes.HttpCaller:
    """An HTTP seam routing an authenticated `/user_settings` call into the real handler."""
    from crm.handlers import user_settings as user_settings_handler

    def caller(_method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
        authorization = headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return 401, ""
        assert url == f"{API_BASE_URL}/user_settings", url
        claims = _authorizer_claims(authorization.removeprefix("Bearer "))
        event = {
            **_eval_trigger_v1_event(body, claims),
            "resource": "/user_settings",
            "path": "/user_settings",
        }
        response = user_settings_handler.handler(event, None, store=cast("Store", store))
        return int(response["statusCode"]), str(response["body"])

    return caller


def _user_settings_row(caller: api_routes.HttpCaller) -> CheckResult:
    client = FakeApiGatewayV2Client(route_keys=[USER_SETTINGS_ROUTE])
    lifecycle = _RecordingLifecycle()
    results = list(
        checks(
            _make_ctx(client),
            http_caller=caller,
            token=_token_with_claims(_claims("false")),
            seeder=lifecycle.seed,
            teardown=lifecycle.teardown,
        )
    )
    rows = [
        r
        for r in results
        if r.check == "authenticated_non_error" and r.resource == f"route:{USER_SETTINGS_ROUTE}"
    ]
    assert len(rows) == 1, rows
    return rows[0]


class TestUserSettingsProbe:
    """`/user_settings` is probed with a write-free `get` and passes only on the settings
    shape."""

    def test_the_real_handler_answers_the_probe_and_nothing_is_written(self) -> None:
        store = _AbsentJobStore()
        row = _user_settings_row(_real_user_settings_caller(store))
        assert row.status == "pass", row.detail
        assert store.reads, "the get read the caller's settings row"
        assert store.other_calls == []

    def test_the_probe_body_is_a_get(self) -> None:
        from crm.handlers import user_settings as user_settings_handler

        assert api_routes.user_settings_probe_body(SMOKE_TENANT) == {
            "action": user_settings_handler.ACTION_GET
        }

    def test_the_expected_keys_are_the_views_payload_keys(self) -> None:
        from crm.user_settings import SettingsView

        view = SettingsView(tuning={}, is_default=True)
        assert set(view.to_payload()) == api_routes.USER_SETTINGS_BODY_KEYS

    @pytest.mark.parametrize(
        ("status", "response_body"),
        [
            (400, json.dumps({"error": {"code": "validation_failed"}})),
            (200, json.dumps({"error": {"code": "validation_failed"}})),
            (200, json.dumps({"tuning": {}, "is_default": True})),
            (200, "not json"),
            (201, json.dumps(dict.fromkeys(sorted(api_routes.USER_SETTINGS_BODY_KEYS)))),
        ],
        ids=["400-error", "200-error", "200-missing-keys", "200-not-json", "201-settings"],
    )
    def test_any_other_answer_fails(self, status: int, response_body: str) -> None:
        def caller(_method: str, _url: str, headers: Mapping[str, str], _body: str) -> HttpResult:
            if "Authorization" in headers:
                return status, response_body
            return 401, ""

        row = _user_settings_row(caller)
        assert row.status == "fail", row.detail
        assert "body_keys=" in row.detail


def test_transport_failure_reported_as_fail_not_traceback() -> None:
    """A transport-level failure (no HTTP response at all) is reported as a failing check
    with the failure message, never raised out of `checks()`."""
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys())
    ctx = _make_ctx(client)

    def caller(_method: str, url: str, _headers: Mapping[str, str], _body: str) -> HttpResult:
        return f"transport error calling {url}: connection refused"

    results = list(checks(ctx, http_caller=caller, token=None))

    unauth_checks = [r for r in results if r.check == "unauthenticated_rejected"]
    assert len(unauth_checks) == len(TOOL_NAMES)
    assert all(r.status == "fail" for r in unauth_checks)
    assert all("transport error" in r.detail for r in unauth_checks)


def test_an_api_reporting_no_methods_fails_rather_than_passing_over_nothing() -> None:
    """An API whose enumeration returns no methods fails rather than passing over nothing."""

    class NoMethodsClient:
        def get_resources(self, **_kwargs: Any) -> dict[str, Any]:
            return {"items": []}

    ctx = context_for_test(
        outputs={
            "api_id": API_ID,
            "cloudfront_domain_name": CLOUDFRONT_DOMAIN,
        },
        clients={"apigateway": NoMethodsClient()},
    )

    results = list(checks(ctx, http_caller=_always_401, token=None))

    assert len(results) == 1
    assert results[0].check == "resolve_api_routes"
    assert results[0].status == "fail"
    assert API_ID in results[0].detail


def test_a_method_with_authorization_none_fails_even_with_an_authorizer_attached() -> None:
    """A method with `authorizationType` `NONE` fails even with an `authorizerId` attached."""

    class UnauthorizedMethodClient(FakeApiGatewayV2Client):
        def get_method(self, **kwargs: Any) -> dict[str, Any]:
            inherited = super().get_method(**kwargs)
            # Authorizer still attached; authorization switched off.
            return {"authorizationType": "NONE", "authorizerId": inherited["authorizerId"]}

    ctx = context_for_test(
        outputs={"api_id": API_ID, "cloudfront_domain_name": CLOUDFRONT_DOMAIN},
        clients={"apigateway": UnauthorizedMethodClient(route_keys=[f"POST /{TOOL_NAMES[0]}"])},
    )

    results = [
        r
        for r in checks(ctx, http_caller=_always_401, token=None)
        if r.check == "authorizer_attached"
    ]

    assert results, "no authorizer_attached check ran, so this test proves nothing"
    assert all(r.status == "fail" for r in results), [r.detail for r in results]
    assert any("NONE" in r.detail for r in results), [r.detail for r in results]


# ---------------------------------------------------------------------------
# The outputs and the client this module needs: the id comes from `api_id`, and the client is v1
# ---------------------------------------------------------------------------


def test_a_missing_api_id_output_stops_the_run_loudly() -> None:
    """A missing `api_id` output stops the run loudly."""
    ctx = context_for_test(
        outputs={"cloudfront_domain_name": CLOUDFRONT_DOMAIN},
        clients={"apigateway": FakeApiGatewayV2Client(route_keys=_all_route_keys())},
    )
    with pytest.raises(MissingTerraformOutputError):
        list(checks(ctx, http_caller=_always_401))


def test_the_api_endpoint_output_is_not_what_identifies_the_api() -> None:
    """`api_endpoint` present and `api_id` absent still fails."""
    ctx = context_for_test(
        outputs={"api_endpoint": API_ENDPOINT, "cloudfront_domain_name": CLOUDFRONT_DOMAIN},
        clients={"apigateway": FakeApiGatewayV2Client(route_keys=_all_route_keys())},
    )
    with pytest.raises(MissingTerraformOutputError):
        list(checks(ctx, http_caller=_always_401))


def test_the_v1_client_is_the_one_requested_and_a_v2_client_will_not_satisfy_it() -> None:
    """A context injecting only an `apigatewayv2` client fails rather than runs."""
    ctx = context_for_test(
        outputs={"api_id": API_ID, "cloudfront_domain_name": CLOUDFRONT_DOMAIN},
        clients={"apigatewayv2": FakeApiGatewayV2Client(route_keys=_all_route_keys())},
    )
    with pytest.raises(UninjectedAwsClientError, match="apigateway"):
        list(checks(ctx, http_caller=_always_401))


def test_the_operator_facing_description_does_not_still_describe_an_http_api() -> None:
    """The module docstring, printed as the CLI `description`, does not claim the API is an HTTP API
    or that validators do not apply."""
    description = api_routes.__doc__ or ""
    assert description, "the module lost its docstring, which is the script's --help description"
    lowered = " ".join(description.lower().split())
    stale_claims = (
        "the deployed http api",
        "the deployed api is api gateway v2",
        "reported as `skip`",
    )
    for claim in stale_claims:
        assert claim not in lowered, (
            f"the operator-facing description still claims {claim!r}; the deployment is a private "
            "REST API (v1), and the request-validator check is a real fail, not a skip"
        )
    assert "private rest api" in lowered, "the description does not say what the API actually is"


class TestNonToolRouteAllowlist:
    """`NON_TOOL_ROUTES` equals the Terraform `extra_routes` allowlist it mirrors."""

    #: Read as text with a regex, not an HCL parser.
    _VARIABLES_TF = (
        Path(__file__).resolve().parents[2] / "infra" / "modules" / "api" / "variables.tf"
    )

    def _terraform_allowlist(self) -> set[str]:
        text = self._VARIABLES_TF.read_text(encoding="utf-8")
        match = re.search(r"setsubtract\(keys\(var\.extra_routes\),\s*\[([^\]]*)\]\)", text)
        assert match, (
            f"the extra_routes allowlist validation was not found in {self._VARIABLES_TF}. This test "
            "cannot compare two sets when it can only read one, so it fails rather than passing on a "
            "half-measurement."
        )
        return set(re.findall(r'"([^"]+)"', match.group(1)))

    def test_the_two_allowlists_agree(self) -> None:
        terraform_side = self._terraform_allowlist()
        assert terraform_side, "the parsed Terraform allowlist is empty, so this compared nothing"
        assert set(api_routes.NON_TOOL_ROUTES) == terraform_side, (
            "the smoke module's NON_TOOL_ROUTES and the Terraform extra_routes allowlist disagree. "
            "Whichever side gained a route, add it to the other -- and do NOT add it to ALL_TOOLS, "
            "which publishes it to external MCP clients."
        )

    def test_the_parser_discriminates(self) -> None:
        """The `extra_routes` regex captures at least two names."""
        assert self._terraform_allowlist() != {"a-name-no-allowlist-would-contain"}
        assert len(self._terraform_allowlist()) >= 2, (
            "expected at least eval_trigger and capabilities; a smaller set means the regex captured "
            "only part of the list and the comparison above is weaker than it looks"
        )

    def test_no_allowlisted_route_is_a_toolspec_tool(self) -> None:
        """No `NON_TOOL_ROUTES` entry is in `ALL_TOOLS`."""
        overlap = set(api_routes.NON_TOOL_ROUTES) & {tool.name for tool in ALL_TOOLS}
        assert not overlap, (
            f"{sorted(overlap)} are in NON_TOOL_ROUTES and in ALL_TOOLS. If a route became a real "
            "tool, remove it from the allowlist; if it did not, remove it from ALL_TOOLS."
        )


class TestAgentOnlyToolAllowlist:
    """`AGENT_ONLY_TOOLS` equals `local.agent_only_tools` in `infra/main.tf`."""

    _MAIN_TF = Path(__file__).resolve().parents[2] / "infra" / "main.tf"

    def _terraform_agent_only(self) -> set[str]:
        text = self._MAIN_TF.read_text(encoding="utf-8")
        match = re.search(r"agent_only_tools\s*=\s*\[([^\]]*)\]", text)
        assert match, (
            f"local.agent_only_tools was not found in {self._MAIN_TF}. This test compares two sets and "
            "can only read one, so it fails rather than passing on a half-measurement."
        )
        return set(re.findall(r'"([^"]+)"', match.group(1)))

    def test_the_two_allowlists_agree(self) -> None:
        terraform_side = self._terraform_agent_only()
        assert terraform_side, "the parsed agent_only_tools list is empty, so this compared nothing"
        assert set(api_routes.AGENT_ONLY_TOOLS) == terraform_side, (
            "AGENT_ONLY_TOOLS and local.agent_only_tools disagree. Whichever side gained a tool, add "
            "it to the other -- and do NOT give an agent-only tool an HTTP route to satisfy the check."
        )

    def test_the_parser_discriminates(self) -> None:
        """The `agent_only_tools` regex captures `get_page_context`."""
        assert self._terraform_agent_only() != {"a-tool-no-allowlist-would-contain"}
        assert "get_page_context" in self._terraform_agent_only()

    def test_every_agent_only_tool_is_a_real_toolspec_tool(self) -> None:
        """Every `AGENT_ONLY_TOOLS` entry is a real toolspec tool."""
        unknown = set(api_routes.AGENT_ONLY_TOOLS) - {tool.name for tool in ALL_TOOLS}
        assert not unknown, f"{sorted(unknown)} are in AGENT_ONLY_TOOLS but not in ALL_TOOLS"

    def test_the_two_exemptions_are_disjoint(self) -> None:
        """`NON_TOOL_ROUTES` and `AGENT_ONLY_TOOLS` are disjoint."""
        both = set(api_routes.AGENT_ONLY_TOOLS) & set(api_routes.NON_TOOL_ROUTES)
        assert not both, f"{sorted(both)} appear in BOTH exemption sets, which is contradictory"


# ---------------------------------------------------------------------------
# The private-endpoint posture, asserted from the CONTROL PLANE, and the CloudFront repoint.
# ---------------------------------------------------------------------------


def _private_row(client: FakeApiGatewayV2Client) -> CheckResult:
    results = list(checks(_make_ctx(client), http_caller=_always_401, token=None))
    rows = [r for r in results if r.check == "endpoint_configuration_is_private"]
    assert len(rows) == 1, f"expected exactly one endpoint-posture row, got {len(rows)}"
    return rows[0]


def test_the_private_endpoint_posture_passes_on_the_deployed_shape() -> None:
    """`types == ["PRIVATE"]` with at least one VPC endpoint is the correct posture."""
    assert _private_row(FakeApiGatewayV2Client(route_keys=_all_route_keys())).status == "pass"


def test_a_type_set_that_merely_contains_private_is_a_failure() -> None:
    """An endpoint type set that contains `PRIVATE` alongside another type fails."""
    row = _private_row(
        FakeApiGatewayV2Client(route_keys=_all_route_keys(), endpoint_types=["PRIVATE", "EDGE"])
    )
    assert row.status == "fail"
    assert "expected exactly" in row.detail, row.detail


def test_a_regional_api_is_a_failure() -> None:
    row = _private_row(
        FakeApiGatewayV2Client(route_keys=_all_route_keys(), endpoint_types=["REGIONAL"])
    )
    assert row.status == "fail"


def test_a_private_type_with_no_vpc_endpoint_is_a_failure() -> None:
    """A private endpoint type with no VPC endpoint attached fails."""
    row = _private_row(FakeApiGatewayV2Client(route_keys=_all_route_keys(), vpc_endpoint_ids=[]))
    assert row.status == "fail"
    assert "expected at least 1" in row.detail, row.detail


def test_the_posture_row_counts_vpc_endpoints_and_never_names_them() -> None:
    """The posture row reports the VPC endpoint count and never a `vpce-` id."""
    planted = "vpce-" + "0a1b" * 4 + "0"
    row = _private_row(
        FakeApiGatewayV2Client(route_keys=_all_route_keys(), vpc_endpoint_ids=[planted])
    )
    assert row.status == "pass"
    assert planted not in row.detail, f"a VPC endpoint id leaked into the detail: {row.detail}"
    assert "vpce-" not in row.detail, f"a vpce- prefix leaked into the detail: {row.detail}"
    assert "1 VPC endpoint(s)" in row.detail, row.detail


def test_routes_are_called_through_cloudfront_and_never_the_execute_api_stage() -> None:
    """Route calls go through the CloudFront base URL, never the `execute-api` stage URL."""
    client = FakeApiGatewayV2Client(route_keys=_all_route_keys())
    called: list[str] = []

    def caller(_method: str, url: str, _headers: Mapping[str, str], _body: str) -> HttpResult:
        called.append(url)
        return 401, ""

    list(checks(_make_ctx(client), http_caller=caller, token=None))

    assert called, "no route was called, so this cell asserts nothing about where calls go"
    assert all(url.startswith(f"https://{CLOUDFRONT_DOMAIN}/api/") for url in called), (
        f"a call was not addressed through the CloudFront /api base: {sorted(set(called))[:3]}"
    )
    assert not [url for url in called if "execute-api" in url], (
        f"a call still addresses the unreachable execute-api host: {sorted(set(called))[:3]}"
    )


def test_a_bodyless_method_is_probed_with_no_request_body() -> None:
    """A `GET` route is probed with no request body, while a `POST` still sends `{}`.

    API Gateway answers 403 to a `GET` carrying a body, before the authorizer runs.
    """
    get_route = "GET /capabilities"
    client = FakeApiGatewayV2Client(route_keys=[*_all_route_keys(), get_route])
    sent: list[tuple[str, str]] = []

    def caller(method: str, _url: str, _headers: Mapping[str, str], body: str) -> HttpResult:
        sent.append((method, body))
        return 401, ""

    list(checks(_make_ctx(client), http_caller=caller, token=None))

    get_bodies = [body for method, body in sent if method == "GET"]
    assert get_bodies, "no GET was probed, so this cell asserts nothing about bodyless methods"
    assert get_bodies == [""] * len(get_bodies), (
        f"a GET was probed with a request body, which makes API Gateway answer 403 and destroys the "
        f"401-vs-403 signal: {get_bodies!r}"
    )
    post_bodies = {body for method, body in sent if method == "POST"}
    assert post_bodies == {"{}"}, f"a POST lost its request body: {post_bodies!r}"


def test_a_403_is_never_accepted_as_an_unauthenticated_refusal() -> None:
    """An unauthenticated 403 never passes `unauthenticated_rejected`; its detail names an unmatched
    route rather than a misconfigured authorizer.

    On a private REST API a matched method's authorizer answers 401; an unmatched route answers 403.
    """
    assert api_routes._UNAUTHENTICATED_REFUSAL_STATUS == 401

    def rows_for(status: int) -> list[CheckResult]:
        client = FakeApiGatewayV2Client(route_keys=_all_route_keys())

        def caller(_m: str, _url: str, _h: Mapping[str, str], _b: str) -> HttpResult:
            return status, ""

        return [
            r
            for r in checks(_make_ctx(client), http_caller=caller, token=None)
            if r.check == "unauthenticated_rejected"
        ]

    passing = rows_for(401)
    assert passing, "no unauthenticated_rejected row was emitted, so this cell asserts nothing"
    assert {r.status for r in passing} == {"pass"}, "401 must be the accepted refusal"

    refused = rows_for(403)
    assert refused, "no unauthenticated_rejected row was emitted for 403"
    assert {r.status for r in refused} == {"fail"}, (
        "403 was accepted as an unauthenticated refusal; on a PRIVATE REST API that is how an "
        "UNMATCHED route answers, so accepting it lets a deleted route pass this row"
    )
    for row in refused:
        assert "misconfigured authorizer" not in row.detail, (
            f"the 403 detail blames the authorizer, which the adjacent authorizer_attached row "
            f"disproves for the same route: {row.detail}"
        )
        assert "UNMATCHED route" in row.detail, (
            f"the 403 detail does not name the likely cause (an unmatched route): {row.detail}"
        )


def test_update_contact_body_targets_an_active_seeded_contact_in_the_first_account() -> None:
    """`POST /update_contact` sends `fields: {}`, which A2 refuses on PD-01's departed contact."""
    from fixtures.generate import generate_dataset
    from scripts.smoke.lambda_actions import seeded_in_scope_account_id

    tenant = "eval-smoke-abc"
    tool = next(t for t in ALL_TOOLS if t.name == "update_contact")
    body = api_routes.request_body_for(tool, tenant=tenant)
    contacts = {c["contact_id"]: c for c in generate_dataset(tenant).as_mapping()["contacts"]}

    target = contacts[body["contact_id"]]
    assert target["status"] == "active", target
    assert target["account_id"] == seeded_in_scope_account_id(tenant)
    assert "status" not in body["fields"]


# ---------------------------------------------------------------------------
# The deployed app-client allowlist and the direct-invoke audience probe.
# ---------------------------------------------------------------------------

ALLOWLIST_PARAMETER = "/crm-x/api/allowed-client-ids"
FOREIGN_CLIENT_ID = "mcp-oauth-client-test"
ACCOUNT = "123456789012"
RUNTIME_ROLE = f"arn:aws:iam::{ACCOUNT}:role/crm-x-runtime"
RELAY_ROLE = f"arn:aws:iam::{ACCOUNT}:role/crm-x-relay"
PARAMETER_ARN = f"arn:aws:ssm:us-east-1:{ACCOUNT}:parameter/crm-x/api/allowed-client-ids"
IDENTITY_TOOLS = ["get_account", "search_accounts", "eval_trigger"]


def _function_name(tool: str) -> str:
    return f"crm-x-{tool}"


def _role_of(tool: str) -> str:
    return f"arn:aws:iam::{ACCOUNT}:role/crm-x-{tool}"


class _ApiWithIntegrations(FakeApiGatewayV2Client):
    """The v1 fake plus `get_integration`, one Lambda proxy integration per route."""

    def get_integration(self, **kwargs: Any) -> dict[str, Any]:
        index = int(str(kwargs["resourceId"]).removeprefix("res"))
        tool = self._route_keys[index].split(" ", 1)[1].lstrip("/")
        function_arn = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:{_function_name(tool)}"
        return {
            "type": "AWS_PROXY",
            "uri": "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/"
            f"{function_arn}/invocations",
        }


class _Payload:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body


class _FakeLambda:
    """`get_function_configuration` per tool function, and an `invoke` that applies an allowlist."""

    def __init__(
        self,
        *,
        env_overrides: Mapping[str, Mapping[str, str | None]] | None = None,
        admitted: frozenset[str] = frozenset({test_key.CLIENT_ID}),
        function_error_for: frozenset[str] = frozenset(),
        refusal_code: str = "authentication_failed",
    ) -> None:
        self._env_overrides = dict(env_overrides or {})
        self._admitted = admitted
        self._function_error_for = function_error_for
        self._refusal_code = refusal_code
        self.invoked: list[dict[str, Any]] = []

    def get_function_configuration(self, **kwargs: Any) -> dict[str, Any]:
        name = str(kwargs["FunctionName"])
        tool = name.removeprefix("crm-x-")
        variables: dict[str, str] = {API_ALLOWED_ENV: ALLOWLIST_PARAMETER}
        for key, value in self._env_overrides.get(tool, {}).items():
            if value is None:
                variables.pop(key, None)
            else:
                variables[key] = value
        return {
            "FunctionName": name,
            "FunctionArn": f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:{name}",
            "Role": _role_of(tool),
            "Environment": {"Variables": variables},
        }

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        event = json.loads(kwargs["Payload"])
        self.invoked.append(event)
        claims = event["requestContext"]["authorizer"]["claims"]
        client = claims.get("aud") if claims.get("token_use") == "id" else claims.get("client_id")
        response: dict[str, Any] = {}
        if client in self._admitted:
            body: dict[str, Any] = {"statusCode": 200, "body": json.dumps({"account": {}})}
            if client in self._function_error_for:
                response["FunctionError"] = "Unhandled"
        else:
            body = {
                "statusCode": 401,
                "body": json.dumps({"error": {"code": self._refusal_code, "message": "x"}}),
            }
        response["Payload"] = _Payload(json.dumps(body).encode())
        return response


class _FakeSsm:
    def __init__(
        self,
        *,
        value: str = test_key.CLIENT_ID,
        parameter_type: str = "SecureString",
        tags: list[dict[str, str]] | None = None,
    ) -> None:
        self._value = value
        self._type = parameter_type
        self._tags = [{"Key": "data-classification", "Value": "internal"}] if tags is None else tags

    def get_parameter(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["Name"] == ALLOWLIST_PARAMETER, kwargs
        encrypted = self._type == "SecureString" and not kwargs.get("WithDecryption")
        value = "AQICAHciphertextblob==" if encrypted else self._value
        return {"Parameter": {"Name": ALLOWLIST_PARAMETER, "Value": value, "Type": self._type}}

    def list_tags_for_resource(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"ResourceType": "Parameter", "ResourceId": ALLOWLIST_PARAMETER}, kwargs
        return {"TagList": list(self._tags)}


class _FakeIam:
    """Allows exactly `ssm:GetParameter` on the parameter for the API roles, plus any extra grant."""

    def __init__(
        self,
        *,
        extra_allowed: set[tuple[str, str, str]] | None = None,
        withheld: set[str] | None = None,
        missing_context: bool = False,
    ) -> None:
        self._extra = set(extra_allowed or set())
        self._withheld = set(withheld or set())
        self._missing_context = missing_context
        self.calls: list[dict[str, Any]] = []

    def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        role = str(kwargs["PolicySourceArn"])
        (resource,) = kwargs["ResourceArns"]
        api_role = role in {_role_of(tool) for tool in IDENTITY_TOOLS}
        results = []
        for action in kwargs["ActionNames"]:
            allowed = (
                api_role
                and role not in self._withheld
                and action == "ssm:GetParameter"
                and resource == PARAMETER_ARN
            ) or (role, action, resource) in self._extra
            results.append(
                {
                    "EvalActionName": action,
                    "EvalDecision": "allowed" if allowed else "implicitDeny",
                    "MissingContextValues": ["aws:SourceIp"] if self._missing_context else [],
                }
            )
        return {"EvaluationResults": results}


class _FakeLogs:
    def __init__(self, reasons: list[str] | None = None) -> None:
        self._reasons = (
            ["authentication_failed: token aud is not an admitted app client"]
            if reasons is None
            else reasons
        )
        self.calls = 0

    def filter_log_events(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "events": [
                {
                    "message": "2026-11-06T00:00:00Z\tINFO\t"
                    + json.dumps({"event": "crm_error", "reason": r})
                }
                for r in self._reasons
            ]
        }


API_ALLOWED_ENV = API_ALLOWED_CLIENT_IDS_PARAMETER_ENV


def _identity_ctx(
    *,
    lambda_client: _FakeLambda | None = None,
    ssm: _FakeSsm | None = None,
    iam: _FakeIam | None = None,
    logs: _FakeLogs | None = None,
    route_keys: list[str] | None = None,
) -> Any:
    return context_for_test(
        outputs={
            "api_id": API_ID,
            "cloudfront_domain_name": CLOUDFRONT_DOMAIN,
            **COGNITO_OUTPUTS,
            "cognito_mcp_oauth_client_id": FOREIGN_CLIENT_ID,
            "agentcore_runtime_role_arn": RUNTIME_ROLE,
            "agent_relay_role_arn": RELAY_ROLE,
        },
        clients={
            "apigateway": _ApiWithIntegrations(
                route_keys=route_keys or [f"POST /{tool}" for tool in IDENTITY_TOOLS]
            ),
            "lambda": lambda_client or _FakeLambda(),
            "ssm": ssm or _FakeSsm(),
            "iam": iam or _FakeIam(),
            "logs": logs or _FakeLogs(),
        },
    )


def _allowlist_rows(ctx: Any) -> list[CheckResult]:
    lambda_client = ctx.client("lambda")
    names = api_routes.integrated_function_names(ctx.client("apigateway"), API_ID)
    functions = [
        api_routes._describe_function(lambda_client, name) for name in sorted(set(names.values()))
    ]
    return list(api_routes.allowlist_checks(ctx, functions))


def _failing(rows: list[CheckResult]) -> set[tuple[str, str]]:
    return {(row.resource, row.check) for row in rows if row.status == "fail"}


class TestAllowlistChecks:
    def test_the_deployed_shape_passes_every_row(self) -> None:
        rows = _allowlist_rows(_identity_ctx())
        assert _failing(rows) == set()
        checks_seen = {row.check for row in rows}
        assert checks_seen == {
            "resolve_integrated_functions",
            "allowlist_parameter_env_set",
            "legacy_allowlist_env_absent",
            "allowlist_parameter_env_consistent",
            "allowlist_parameter_value_is_spa_client",
            "allowlist_parameter_type_securestring",
            "allowlist_parameter_tagged",
            "ssm_get_parameter_allowed",
            "ssm_read_grant_is_exact_parameter",
            "ssm_parameter_write_denied",
            "ssm_get_parameter_denied_non_api_role",
        }

    def test_the_integrated_set_comes_from_the_deployed_integrations(self) -> None:
        names = api_routes.integrated_function_names(
            _ApiWithIntegrations(route_keys=["POST /get_account", "POST /eval_trigger"]), API_ID
        )
        assert names == {
            "POST /get_account": _function_name("get_account"),
            "POST /eval_trigger": _function_name("eval_trigger"),
        }

    def test_no_integrated_function_fails_rather_than_passing_over_nothing(self) -> None:
        rows = list(api_routes.allowlist_checks(_identity_ctx(), []))
        assert [(r.check, r.status) for r in rows] == [("resolve_integrated_functions", "fail")]

    def test_a_function_without_the_parameter_variable_fails_its_row_only(self) -> None:
        ctx = _identity_ctx(
            lambda_client=_FakeLambda(env_overrides={"search_accounts": {API_ALLOWED_ENV: None}})
        )
        assert _failing(_allowlist_rows(ctx)) == {
            (f"function:{_function_name('search_accounts')}", "allowlist_parameter_env_set")
        }

    def test_the_legacy_direct_variable_fails_its_row_only(self) -> None:
        legacy = api_routes.LEGACY_ALLOWED_CLIENT_IDS_ENV
        ctx = _identity_ctx(
            lambda_client=_FakeLambda(env_overrides={"eval_trigger": {legacy: "x"}})
        )
        assert _failing(_allowlist_rows(ctx)) == {
            (f"function:{_function_name('eval_trigger')}", "legacy_allowlist_env_absent")
        }

    def test_functions_naming_different_parameters_fail_consistency(self) -> None:
        ctx = _identity_ctx(
            lambda_client=_FakeLambda(env_overrides={"get_account": {API_ALLOWED_ENV: "/other"}})
        )
        assert _failing(_allowlist_rows(ctx)) == {
            (api_routes.RESOURCE_CLASS, "allowlist_parameter_env_consistent")
        }

    @pytest.mark.parametrize(
        "value",
        [f"{test_key.CLIENT_ID},{FOREIGN_CLIENT_ID}", FOREIGN_CLIENT_ID, f" {test_key.CLIENT_ID}"],
    )
    def test_a_parameter_value_other_than_the_spa_client_fails_its_row_only(
        self, value: str
    ) -> None:
        ctx = _identity_ctx(ssm=_FakeSsm(value=value))
        assert _failing(_allowlist_rows(ctx)) == {
            (f"parameter:{ALLOWLIST_PARAMETER}", "allowlist_parameter_value_is_spa_client")
        }

    @pytest.mark.parametrize("parameter_type", ["String", "StringList"])
    def test_a_parameter_that_is_not_a_securestring_fails_its_row_only(
        self, parameter_type: str
    ) -> None:
        ctx = _identity_ctx(ssm=_FakeSsm(parameter_type=parameter_type))
        assert _failing(_allowlist_rows(ctx)) == {
            (f"parameter:{ALLOWLIST_PARAMETER}", "allowlist_parameter_type_securestring")
        }

    def test_the_parameter_is_read_with_decryption(self) -> None:
        class _Recording(_FakeSsm):
            calls: list[dict[str, Any]] = []

            def get_parameter(self, **kwargs: Any) -> dict[str, Any]:
                self.calls.append(kwargs)
                return super().get_parameter(**kwargs)

        ssm = _Recording()
        _allowlist_rows(_identity_ctx(ssm=ssm))
        assert ssm.calls == [{"Name": ALLOWLIST_PARAMETER, "WithDecryption": True}]

    def test_a_read_without_decryption_would_fail_the_value_row(self) -> None:
        class _NoDecrypt(_FakeSsm):
            def get_parameter(self, **kwargs: Any) -> dict[str, Any]:
                return super().get_parameter(**{**kwargs, "WithDecryption": False})

        assert _failing(_allowlist_rows(_identity_ctx(ssm=_NoDecrypt()))) == {
            (f"parameter:{ALLOWLIST_PARAMETER}", "allowlist_parameter_value_is_spa_client")
        }

    def test_an_untagged_parameter_fails_its_row_only(self) -> None:
        ctx = _identity_ctx(ssm=_FakeSsm(tags=[{"Key": "service", "Value": "crm"}]))
        assert _failing(_allowlist_rows(ctx)) == {
            (f"parameter:{ALLOWLIST_PARAMETER}", "allowlist_parameter_tagged")
        }

    def test_an_api_role_without_the_read_grant_fails_its_row_only(self) -> None:
        ctx = _identity_ctx(iam=_FakeIam(withheld={_role_of("get_account")}))
        assert _failing(_allowlist_rows(ctx)) == {
            ("role:crm-x-get_account", "ssm_get_parameter_allowed")
        }

    def test_an_allowed_read_with_missing_context_is_not_a_pass(self) -> None:
        failing = _failing(_allowlist_rows(_identity_ctx(iam=_FakeIam(missing_context=True))))
        assert failing == {
            (f"role:crm-x-{tool}", "ssm_get_parameter_allowed") for tool in IDENTITY_TOOLS
        }

    @pytest.mark.parametrize(
        ("action", "resource"),
        [
            ("ssm:GetParameter", f"arn:aws:ssm:us-east-1:{ACCOUNT}:parameter/crm-x/api/other"),
            ("ssm:GetParameter", f"arn:aws:ssm:us-east-1:{ACCOUNT}:parameter/crm-x/*"),
            ("ssm:GetParameters", PARAMETER_ARN),
            ("ssm:GetParametersByPath", PARAMETER_ARN),
        ],
    )
    def test_a_wider_read_grant_fails_the_exactness_row_only(
        self, action: str, resource: str
    ) -> None:
        iam = _FakeIam(extra_allowed={(_role_of("search_accounts"), action, resource)})
        assert _failing(_allowlist_rows(_identity_ctx(iam=iam))) == {
            ("role:crm-x-search_accounts", "ssm_read_grant_is_exact_parameter")
        }

    @pytest.mark.parametrize("action", api_routes.SSM_WRITE_ACTIONS)
    @pytest.mark.parametrize("role", [RUNTIME_ROLE, _role_of("eval_trigger")])
    def test_a_write_grant_fails_the_write_row_only(self, action: str, role: str) -> None:
        iam = _FakeIam(extra_allowed={(role, action, PARAMETER_ARN)})
        assert _failing(_allowlist_rows(_identity_ctx(iam=iam))) == {
            (f"role:{role.rsplit('/', 1)[-1]}", "ssm_parameter_write_denied")
        }

    def test_a_non_api_role_that_can_read_fails_its_row_only(self) -> None:
        iam = _FakeIam(extra_allowed={(RELAY_ROLE, "ssm:GetParameter", PARAMETER_ARN)})
        assert _failing(_allowlist_rows(_identity_ctx(iam=iam))) == {
            ("role:crm-x-relay", "ssm_get_parameter_denied_non_api_role")
        }

    def test_the_parameter_arn_is_built_from_the_functions_account_and_region(self) -> None:
        iam = _FakeIam()
        _allowlist_rows(_identity_ctx(iam=iam))
        resources = {arn for call in iam.calls for arn in call["ResourceArns"]}
        assert PARAMETER_ARN in resources


def _audience_function() -> api_routes.IntegratedFunction:
    return api_routes._describe_function(_FakeLambda(), _function_name("get_account"))


def _audience_rows(ctx: Any, sleeps: list[float] | None = None) -> list[CheckResult]:
    recorded = sleeps if sleeps is not None else []
    claims = test_key.id_token_claims(
        **{"custom:tenant": SMOKE_TENANT, "custom:rep_id": "rep-1", "custom:book": "rep-1"}
    )
    return list(
        api_routes.audience_probe_checks(
            ctx,
            function=_audience_function(),
            claims=claims,
            tenant=SMOKE_TENANT,
            sleep=recorded.append,
            clock=lambda: 1_700_000_000.0,
        )
    )


AUDIENCE_RESOURCE = f"function:{_function_name('get_account')}"


class TestAudienceProbe:
    def test_the_deployed_shape_passes_every_row(self) -> None:
        lambda_client = _FakeLambda()
        rows = _audience_rows(_identity_ctx(lambda_client=lambda_client))
        assert [(r.check, r.status) for r in rows] == [
            ("audience_foreign_id_token_refused", "pass"),
            ("audience_spa_id_token_admitted", "pass"),
            ("audience_foreign_access_token_refused", "pass"),
            ("audience_refusal_logged_with_reason", "pass"),
        ]

    def test_the_variants_differ_only_in_the_app_client_claims(self) -> None:
        lambda_client = _FakeLambda()
        _audience_rows(_identity_ctx(lambda_client=lambda_client))
        stripped = [
            {
                k: v
                for k, v in event["requestContext"]["authorizer"]["claims"].items()
                if k not in {"aud", "client_id", "token_use"}
            }
            for event in lambda_client.invoked
        ]
        assert len(stripped) == 3 and stripped[0] == stripped[1] == stripped[2]
        bodies = {event["body"] for event in lambda_client.invoked}
        assert len(bodies) == 1

    def test_a_lambda_admitting_any_client_fails_both_refusal_rows(self) -> None:
        lambda_client = _FakeLambda(admitted=frozenset({test_key.CLIENT_ID, FOREIGN_CLIENT_ID}))
        assert _failing(_audience_rows(_identity_ctx(lambda_client=lambda_client))) == {
            (AUDIENCE_RESOURCE, "audience_foreign_id_token_refused"),
            (AUDIENCE_RESOURCE, "audience_foreign_access_token_refused"),
        }

    def test_a_lambda_refusing_the_spa_client_fails_the_admitted_row_only(self) -> None:
        lambda_client = _FakeLambda(admitted=frozenset())
        assert _failing(_audience_rows(_identity_ctx(lambda_client=lambda_client))) == {
            (AUDIENCE_RESOURCE, "audience_spa_id_token_admitted")
        }

    def test_a_function_error_with_a_200_payload_is_not_admitted(self) -> None:
        lambda_client = _FakeLambda(function_error_for=frozenset({test_key.CLIENT_ID}))
        assert _failing(_audience_rows(_identity_ctx(lambda_client=lambda_client))) == {
            (AUDIENCE_RESOURCE, "audience_spa_id_token_admitted")
        }

    def test_a_401_with_another_error_code_is_not_the_audience_refusal(self) -> None:
        lambda_client = _FakeLambda(refusal_code="internal_error")
        assert _failing(_audience_rows(_identity_ctx(lambda_client=lambda_client))) == {
            (AUDIENCE_RESOURCE, "audience_foreign_id_token_refused"),
            (AUDIENCE_RESOURCE, "audience_foreign_access_token_refused"),
        }

    @pytest.mark.parametrize(
        "reasons",
        [
            [],
            ["internal_error: parameter=/crm-x/api/allowed-client-ids throttled"],
            ["authentication_failed: parameter=/x token aud is not an admitted app client"],
        ],
    )
    def test_a_refusal_not_logged_as_the_client_check_fails_the_log_row_only(
        self, reasons: list[str]
    ) -> None:
        sleeps: list[float] = []
        logs = _FakeLogs(reasons)
        rows = _audience_rows(_identity_ctx(logs=logs), sleeps)
        assert _failing(rows) == {(AUDIENCE_RESOURCE, "audience_refusal_logged_with_reason")}
        assert logs.calls == api_routes.LOG_POLL_ATTEMPTS
        assert len(sleeps) == api_routes.LOG_POLL_ATTEMPTS - 1


def test_main_runs_the_deployed_identity_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` passes `deployed_identity_checks=True`, so the allowlist rows are never silently off."""
    seen: dict[str, Any] = {}

    def fake_checks(_ctx: Any, **kwargs: Any) -> list[CheckResult]:
        seen.update(kwargs)
        return []

    def fake_run_main(_argv: Any, **kwargs: Any) -> int:
        list(kwargs["checks"](object()))
        return 0

    monkeypatch.setattr(api_routes, "checks", fake_checks)
    monkeypatch.setattr(api_routes, "run_main", fake_run_main)
    assert api_routes.main([]) == 0
    assert seen.get("deployed_identity_checks") is True


def test_the_audience_probe_runs_inside_the_seeded_tenant_lifecycle() -> None:
    """With the identity checks on, the probe rows land between seed and teardown."""
    events: list[str] = []

    class _Lifecycle(_RecordingLifecycle):
        def seed(self, tenant: str) -> None:
            events.append("seed")
            super().seed(tenant)

        def teardown(self, tenant: str) -> dict[str, int]:
            events.append("teardown")
            return super().teardown(tenant)

    lifecycle = _Lifecycle()

    def caller(_m: str, _u: str, headers: Mapping[str, str], _b: str) -> HttpResult:
        return (200, "{}") if headers.get("Authorization") else (401, "")

    rows = []
    for row in checks(
        _identity_ctx(),
        http_caller=caller,
        token=_fake_token(SMOKE_TENANT),
        seeder=lifecycle.seed,
        teardown=lifecycle.teardown,
        deployed_identity_checks=True,
        sleep=lambda _s: None,
    ):
        if row.check.startswith("audience_"):
            events.append(row.check)
        rows.append(row)
    assert events[0] == "seed" and events[-1] == "teardown"
    assert [e for e in events if e.startswith("audience_")] == [
        "audience_foreign_id_token_refused",
        "audience_spa_id_token_admitted",
        "audience_foreign_access_token_refused",
        "audience_refusal_logged_with_reason",
    ]
    assert {r.check for r in rows} >= {"allowlist_parameter_value_is_spa_client"}
