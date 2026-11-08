# SPDX-License-Identifier: MIT
"""Layer 4 smoke script: asserts the deployed private REST API's per-method posture.

Needs a live deployment. Enumerates methods from the deployed API (never from a static
list), cross-checks that enumeration against `crm.toolspec.definitions.ALL_TOOLS`, calls
every route with a supplied Cognito token and asserts a non-error response, calls every
route unauthenticated and asserts 401, and asserts every method both names an authorizer
*and* declares `COGNITO_USER_POOLS` (grant and enforcement are separate fields), per-stage
throttle/burst limits are configured, and request validators are present.

Routes are called through CloudFront's `/api/*` behaviour, not `api_stage_invoke_url`: the API is
`PRIVATE`, so its `execute-api` hostname does not resolve outside the VPC. A 401 here shows the
CloudFront route refuses an unauthenticated caller; the origin's own posture is asserted from the
control plane by `endpoint_configuration_is_private` and `authorizer_attached`.

The API is API Gateway v1 (a private REST API), so every AWS shape below is v1 — the `apigateway`
client, `restApiId`, `get_resources`/`get_method`, `item` rather than `Items`, and
`methodSettings["*/*"]` rather than `DefaultRouteSettings`. A missing request validator fails.

Each authenticated call sends the minimal body satisfying the route tool's `input_schema`,
targeting records the smoke tenant carries, as `scripts/smoke/lambda_actions.py` does.
A non-tool POST route sends its `NON_TOOL_ROUTE_PROBES` body instead and passes only on the status
and `error.code` (and `error.missing_scope`, when the expectation sets one) the presented token's own
claims call for: for `eval_trigger`, a `cancel` of an absent run must be answered `403 scope_denied`
naming `eval:admin` for a non-admin token and `404 not_found` for an admin token. It writes nothing
under either.

Tenant safety: an authenticated call writes under the supplied token's `custom:tenant` claim. This
module decodes that claim (payload only, unverified) and makes no authenticated call unless it
names an ``eval-`` smoke tenant; a ``demo`` token is reported as a failure. With no ``--tenant`` it
seeds that tenant before the authenticated pass and tears it down afterwards; with ``--tenant`` the
orchestrator owns the lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, NamedTuple

from scripts.smoke.harness import (
    CheckResult,
    MissingTerraformOutputError,
    SmokeContext,
    open_https,
    run_main,
)
from scripts.smoke.lambda_actions import (
    _DEPARTED_CONTACT_TOOL,
    _NO_STATIC_SEED_TOOLS,
    _T3_PENDING_TOOLS,
    T3_PENDING_DETAIL,
    seeded_active_contact_id,
    seeded_entity_id,
    seeded_in_scope_account_id,
)
from scripts.smoke.tenant import (
    SmokeTenant,
    TenantTeardownError,
    seed_smoke_tenant,
    teardown_smoke_tenant,
)
from scripts.smoke.verified_claims import USE_ID, ClaimsVerificationError, verified_claims

from crm.authz import ADMIN_GROUP, DEMO_TENANT
from crm.envelope import API_ALLOWED_CLIENT_IDS_PARAMETER_ENV, CLAIM_GROUPS, split_groups
from crm.errors import AuthenticationFailed
from crm.handlers.eval_trigger import ACTION_CANCEL
from crm.store.store import Store
from crm.toolspec.definitions import ALL_TOOLS, ToolDefinition
from eval.tenancy import run_id_for_tenant
from eval.trigger import EVAL_ADMIN_SCOPE

LAYER = 4
RESOURCE_CLASS = "api_routes"

#: Claim the Cognito pre-token-generation trigger stamps with the caller's tenant, and therefore the
#: tenant every authenticated call made by this script writes under.
TENANT_CLAIM: Final[str] = "custom:tenant"

#: Prefix of every tenant `eval.tenancy.tenant_for_run` mints. Only a tenant carrying it is a
#: throwaway tenant this script may seed, write into, and tear down.
EVAL_TENANT_PREFIX: Final[str] = "eval-"

# (status_code, response_body) for a completed HTTP call, or an exception message for a
# transport failure that produced no response at all.
HttpResult = tuple[int, str] | str

HttpCaller = Callable[[str, str, Mapping[str, str], str], HttpResult]

#: Seed one tenant / tear one tenant down. Injectable; the defaults build a `Store` lazily inside
#: the call.
TenantSeeder = Callable[[str], None]
TenantTeardown = Callable[[str], dict[str, int]]


def default_http_caller(method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
    """Perform one HTTP request using only `urllib.request` (no `requests` dependency).

    Returns `(status_code, response_body)` on any response, including 4xx/5xx (those
    raise `urllib.error.HTTPError`, whose `.code`/`.read()` are captured as a normal
    result rather than treated as a transport failure). Returns a `str` describing the
    failure only when no HTTP response was received at all (e.g. connection refused).
    """
    request = urllib.request.Request(  # noqa: S310 - internal seam, no untrusted URL
        url, data=body.encode("utf-8") if body else None, headers=dict(headers), method=method
    )
    try:
        with open_https(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return f"transport error calling {url}: {exc}"


def token_claims(ctx: SmokeContext, token: str) -> dict[str, Any]:
    """Return an ID token's claims once it verifies against the stack's user pool and SPA client.

    Raises:
        ClaimsVerificationError: The token failed verification.
        MissingTerraformOutputError: `cognito_user_pool_id` or `cognito_spa_client_id` is absent.
    """
    return verified_claims(
        token,
        region=ctx.region,
        user_pool_id=ctx.outputs.require_str("cognito_user_pool_id"),
        client_id=ctx.outputs.require_str("cognito_spa_client_id"),
        token_use=USE_ID,
    )


def verified_token_claims(ctx: SmokeContext, token: str) -> tuple[dict[str, Any], str | None]:
    """Return `(claims, None)` for a verified token, or `({}, reason)` naming why it is untrusted."""
    try:
        return token_claims(ctx, token), None
    except (ClaimsVerificationError, MissingTerraformOutputError) as exc:
        return {}, (
            f"supplied token failed verification against the stack's user pool ({exc}), so no "
            "claim in it is trusted and no authenticated call was made"
        )


def request_body_for(tool: ToolDefinition, *, tenant: str) -> dict[str, Any]:
    """Build the minimal schema-satisfying request body for ``tool`` under ``tenant``.

    The body is the tool's ``input_schema`` exactly, with no envelope and no identity key. Every
    required property is filled: a seeded-entity id property gets the real seeded id (via
    `scripts.smoke.lambda_actions.seeded_entity_id`), except `update_contact`'s ``contact_id``, which
    gets an active contact since the departed one refuses a non-status update. The scope argument
    gets the tenant's first seeded account, and ``transaction_key`` gets a digest unique per
    (tenant, tool).
    """
    account_id = seeded_in_scope_account_id(tenant)
    properties: Mapping[str, Any] = tool.input_schema.get("properties", {})
    body: dict[str, Any] = {}
    for name in tool.input_schema.get("required", []):
        body[name] = _placeholder_for(name, properties.get(name, {}), tenant=tenant, tool=tool)
    scope_key = tool.scope_arg or "account_id"
    if scope_key in properties:
        body[scope_key] = account_id
    return body


def _placeholder_for(
    name: str, schema: Mapping[str, Any], *, tenant: str, tool: ToolDefinition
) -> Any:
    """Return a schema-satisfying value for one required property of ``tool``."""
    if name == "transaction_key":
        digest = hashlib.sha256(f"{tenant}:{tool.name}:api".encode()).hexdigest()[:24]
        return f"api-{digest}"
    if name == "account_id":
        return seeded_in_scope_account_id(tenant)
    if tool.name == _DEPARTED_CONTACT_TOOL and name == "contact_id":
        return seeded_active_contact_id(tenant)
    seeded_id = seeded_entity_id(tenant, name)
    if seeded_id is not None:
        return seeded_id
    enum = schema.get("enum")
    if enum:
        return enum[0]
    schema_type = schema.get("type")
    if schema_type == "number":
        return 0
    if schema_type == "integer":
        return 1
    if schema_type == "boolean":
        return False
    if schema_type == "object":
        return {}
    return f"smoke-{name}"


def _token_from_env_or_flag(argv: Sequence[str] | None) -> str | None:
    """Resolve the Cognito access token: `--token` flag -> `CRM_SMOKE_TOKEN` env -> `None`.

    Never mints, signs, or fetches a token; the caller supplies an already-issued one.
    """
    args = list(argv) if argv is not None else sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--token" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--token="):
            return arg.split("=", 1)[1]
    return os.environ.get("CRM_SMOKE_TOKEN")


#: The authorization every method must carry: the `COGNITO_USER_POOLS` authorizer the api module
#: declares.
EXPECTED_AUTHORIZATION_TYPE = "COGNITO_USER_POOLS"


#: Methods this module must call with no request body at all.
#:
#: API Gateway answers 403 to a `GET` carrying a body before the authorizer runs, the same status
#: as an unmatched route. `default_http_caller` sends no body for a falsy `body`, so `""` is used.
_BODYLESS_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "DELETE", "OPTIONS"})


#: The one status that establishes an authorized, existing route on a private REST API: a matched
#: method's authorizer answers 401, while an unmatched route answers 403 `Missing Authentication Token`.
_UNAUTHENTICATED_REFUSAL_STATUS: Final[int] = 401


#: Deployed routes that are not toolspec tools, mirroring the closed allowlist in
#: `infra/modules/api/variables.tf`'s `extra_routes` validation. Exempt from `route_has_tool` only;
#: every other check still applies to them. Adding one to `ALL_TOOLS` would publish it as an MCP tool.
NON_TOOL_ROUTES: Final[frozenset[str]] = frozenset(
    {"eval_trigger", "capabilities", "user_settings"}
)


#: Toolspec tools with no deployed route, mirroring `local.agent_only_tools` in `infra/main.tf`.
#: Reachable only by direct `lambda:InvokeFunction` from the agents; exempt from `toolspec_has_route`.
AGENT_ONLY_TOOLS: Final[frozenset[str]] = frozenset({"get_page_context"})


#: The claim `pre_token_generation` stamps with the caller's full-visibility flag, as `"true"`/`"false"`.
FULL_VISIBILITY_CLAIM: Final[str] = "custom:full_visibility"

#: Claim spellings `crm.envelope` reads as granting / not granting full visibility.
_FULL_VISIBILITY_TRUE: Final[frozenset[str]] = frozenset({"true", "1", "yes"})
_FULL_VISIBILITY_FALSE: Final[frozenset[str]] = frozenset({"false", "0", "no"})


class ProbeExpectation(NamedTuple):
    """The one answer a non-tool probe must receive: an HTTP status, ``error.code`` and, when set,
    ``error.missing_scope``.

    ``error_code=None`` expects a success body: a JSON object with no ``error`` member that carries
    every key in ``body_keys``.
    """

    status: int
    error_code: str | None
    missing_scope: str | None = None
    body_keys: frozenset[str] = frozenset()


class UnreadableClaimError(ValueError):
    """Raised when the claim a probe's expectation depends on is missing or unreadable."""


def token_has_full_visibility(claims: Mapping[str, Any]) -> bool:
    """Return the presented token's ``custom:full_visibility`` flag.

    Raises:
        UnreadableClaimError: The claim is absent, not a string, or not a recognised boolean
            spelling.
    """
    if FULL_VISIBILITY_CLAIM not in claims:
        raise UnreadableClaimError(f"the token carries no {FULL_VISIBILITY_CLAIM} claim")
    raw = claims[FULL_VISIBILITY_CLAIM]
    if not isinstance(raw, str):
        raise UnreadableClaimError(
            f"{FULL_VISIBILITY_CLAIM} is a {type(raw).__name__}, not the string the trigger stamps"
        )
    value = raw.strip().lower()
    if value in _FULL_VISIBILITY_TRUE:
        return True
    if value in _FULL_VISIBILITY_FALSE:
        return False
    raise UnreadableClaimError(f"{FULL_VISIBILITY_CLAIM}={raw!r} is not a recognised boolean")


def token_is_admin(claims: Mapping[str, Any]) -> bool:
    """Return whether the presented token's ``cognito:groups`` names the admin group.

    Reads the claim with the parser the handler's identity decoder uses; an absent claim is no
    groups.

    Raises:
        UnreadableClaimError: The claim has a shape that parser refuses.
    """
    try:
        groups = split_groups(claims.get(CLAIM_GROUPS))
    except AuthenticationFailed as exc:
        raise UnreadableClaimError(f"{CLAIM_GROUPS} is unreadable: {exc.message}") from exc
    return ADMIN_GROUP in groups


def expected_eval_trigger_status(claims: Mapping[str, Any]) -> ProbeExpectation:
    """Return the answer ``eval_trigger`` must give the probe body for a caller holding ``claims``.

    The probe is a ``cancel`` of an absent run. A non-admin caller is refused at the admin gate,
    before any ``eval_runs`` read: ``403 scope_denied`` naming ``eval:admin``. An admin caller
    passes the gate and finds no run: ``404 not_found``.

    Args:
        claims: The presented token's decoded claims.

    Raises:
        UnreadableClaimError: ``cognito:groups`` is unreadable.
    """
    if token_is_admin(claims):
        return ProbeExpectation(status=404, error_code="not_found")
    return ProbeExpectation(status=403, error_code="scope_denied", missing_scope=EVAL_ADMIN_SCOPE)


@dataclass(frozen=True, slots=True)
class NonToolProbe:
    """The authenticated probe of one non-tool POST route, and how its one passing answer is chosen.

    Attributes:
        body: Builds the request body for a smoke tenant.
        expected: Returns the answer the route must give a caller holding the given token claims;
            raises :class:`UnreadableClaimError` when the claims cannot decide it.
    """

    body: Callable[[str], dict[str, Any]]
    expected: Callable[[Mapping[str, Any]], ProbeExpectation]


def eval_trigger_probe_body(tenant: str) -> dict[str, Any]:
    """Return an ``eval_trigger`` ``cancel`` of a run id no sweep was ever submitted under."""
    digest = hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:24]
    return {"action": ACTION_CANCEL, "run_id": f"smoke-absent-{digest}"}


#: The keys `crm.user_settings.SettingsView.to_payload` renders.
USER_SETTINGS_BODY_KEYS: Final[frozenset[str]] = frozenset(
    {"tuning", "is_default", "notice", "updated_at"}
)


def user_settings_probe_body(tenant: str) -> dict[str, Any]:
    """Return a ``user_settings`` ``get``, which reads the caller's settings and writes nothing."""
    del tenant
    return {"action": "get"}


def expected_user_settings_status(claims: Mapping[str, Any]) -> ProbeExpectation:
    """Return the answer ``user_settings`` must give a ``get`` from any authenticated caller."""
    del claims
    return ProbeExpectation(status=200, error_code=None, body_keys=USER_SETTINGS_BODY_KEYS)


#: Authenticated probes of the POST routes in `NON_TOOL_ROUTES`, which have no toolspec body. Every
#: probe is write-free. An answer other than the expected one fails the probe, a 2xx included.
NON_TOOL_ROUTE_PROBES: Final[Mapping[str, NonToolProbe]] = {
    "eval_trigger": NonToolProbe(
        body=eval_trigger_probe_body, expected=expected_eval_trigger_status
    ),
    "user_settings": NonToolProbe(
        body=user_settings_probe_body, expected=expected_user_settings_status
    ),
}


def _deployed_routes(apigw: Any, api_id: str) -> list[dict[str, Any]]:
    """Enumerate a PRIVATE REST API's methods as v2-shaped route dicts.

    The rest of this module uses the field names `RouteKey`, `AuthorizationType` and `AuthorizerId`,
    so one `<METHOD> /<path>` route dict is synthesised per REST method. `authorizationType` is read
    from `get_method` per method, since `get_resources` reports only which methods exist.
    """
    routes: list[dict[str, Any]] = []
    position: str | None = None
    while True:
        kwargs: dict[str, Any] = {"restApiId": api_id}
        if position:
            kwargs["position"] = position
        page = apigw.get_resources(**kwargs)
        for resource in page.get("items", []):
            path = str(resource.get("path", ""))
            for http_method in resource.get("resourceMethods") or {}:
                method = apigw.get_method(
                    restApiId=api_id,
                    resourceId=str(resource["id"]),
                    httpMethod=str(http_method),
                )
                routes.append(
                    {
                        "RouteKey": f"{http_method} {path}",
                        "AuthorizationType": method.get("authorizationType"),
                        "AuthorizerId": method.get("authorizerId"),
                    }
                )
        position = page.get("position")
        if not position:
            return routes


def _invoke_url_for_route(api_base_url: str, route_key: str) -> str:
    """Build the callable invoke URL for one route from the API base URL.

    `route_key` is of the form `"POST /tool_name"`; its single path segment is appended to the base.
    The base is the CloudFront `/api` prefix rather than the stage invoke URL.
    """
    _, path = route_key.split(" ", 1)
    return api_base_url.rstrip("/") + path


def _unauthenticated_probe_body(method: str) -> str:
    """Return the request body the unauthenticated probe must send for ``method``.

    Empty for every method in `_BODYLESS_METHODS`, `"{}"` otherwise.
    """
    return "" if method.upper() in _BODYLESS_METHODS else "{}"


def _live_table_prefix(ctx: SmokeContext) -> str:
    """Derive the live DynamoDB table-name prefix from the `dynamodb_table_names` output.

    Mirrors `scripts/smoke/lambda_actions.py`: the physical name for the `eval_runs` logical table is
    `<table_prefix>eval_runs`, so stripping that suffix recovers the prefix.
    """
    table_names = ctx.outputs.require_mapping("dynamodb_table_names")
    return str(table_names["eval_runs"]).removesuffix("eval_runs")


def _default_seeder(ctx: SmokeContext) -> TenantSeeder:
    """Return a seeder that seeds one tenant against the live store, built lazily."""

    def seed(tenant: str) -> None:
        store = Store(client=ctx.client("dynamodb"), table_prefix=_live_table_prefix(ctx))
        seed_smoke_tenant(store, _smoke_tenant_for(tenant))

    return seed


def _default_teardown(ctx: SmokeContext) -> TenantTeardown:
    """Return a teardown that tears one tenant down against the live store, built lazily."""

    def teardown(tenant: str) -> dict[str, int]:
        store = Store(client=ctx.client("dynamodb"), table_prefix=_live_table_prefix(ctx))
        return teardown_smoke_tenant(store, _smoke_tenant_for(tenant))

    return teardown


def _smoke_tenant_for(tenant: str) -> SmokeTenant:
    """Reconstruct the `SmokeTenant` for an already-allocated ``eval-`` tenant."""
    return SmokeTenant(run_id=run_id_for_tenant(tenant), tenant=tenant)


def _tool_by_name() -> Mapping[str, ToolDefinition]:
    """Return every declared tool keyed by name."""
    return {tool.name: tool for tool in ALL_TOOLS}


def checks(  # noqa: C901, PLR0912, PLR0915 - one cohesive per-route inventory pass
    ctx: SmokeContext,
    *,
    http_caller: HttpCaller | None = None,
    token: str | None = None,
    seeder: TenantSeeder | None = None,
    teardown: TenantTeardown | None = None,
    deployed_identity_checks: bool = False,
    sleep: Sleeper = time.sleep,
) -> Iterable[CheckResult]:
    """Yield one `CheckResult` per (route, check) pair.

    Never raises for an individual check failure; every route in the deployed API is
    still checked even when an earlier route already failed. With `deployed_identity_checks`
    (always set by `main`), the integrated functions' allowlist wiring is asserted
    (`allowlist_checks`) and the audience probe (`audience_probe_checks`) runs inside the seeded
    tenant's lifecycle.
    """
    caller = http_caller if http_caller is not None else default_http_caller
    # The CloudFront `/api` base. Every deployed path is a single segment, so `base + "/<name>"`
    # addresses all of them.
    api_base_url = f"https://{ctx.outputs.require_str('cloudfront_domain_name')}/api"

    # An absent `api_id` stops the run.
    api_id = ctx.outputs.require_str("api_id")

    # The v1 client; `apigatewayv2` cannot see a REST API.
    apigw = ctx.client("apigateway")

    deployed_routes: list[dict[str, Any]] = _deployed_routes(apigw, api_id)
    if not deployed_routes:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="resolve_api_routes",
            status="fail",
            detail=(
                f"the deployed REST API {api_id!r} reported no methods. Every check below iterates "
                "them, so an empty enumeration would report a clean inventory having verified nothing."
            ),
        )
        return

    deployed_route_keys = sorted({str(route["RouteKey"]) for route in deployed_routes})
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="enumerate_routes",
        status="pass" if deployed_route_keys else "fail",
        detail=f"{len(deployed_route_keys)} route(s) found on deployed API {api_id}",
    )

    toolspec_names = {tool.name for tool in ALL_TOOLS}
    route_tool_names = {
        route_key.split(" ", 1)[1].lstrip("/")
        for route_key in deployed_route_keys
        if " " in route_key
    }
    for missing_route_tool in sorted(toolspec_names - route_tool_names - AGENT_ONLY_TOOLS):
        yield CheckResult(
            resource=f"tool:{missing_route_tool}",
            check="toolspec_has_route",
            status="fail",
            detail=(
                f"tool {missing_route_tool!r} in toolspec has no deployed route and is not an "
                f"allowlisted agent-only tool ({sorted(AGENT_ONLY_TOOLS)})"
            ),
        )
    # Each exemption is reported as its own row.
    for agent_only in sorted(AGENT_ONLY_TOOLS & toolspec_names):
        yield CheckResult(
            resource=f"tool:{agent_only}",
            check="tool_is_agent_only_and_unrouted",
            status="pass" if agent_only not in route_tool_names else "fail",
            detail=(
                f"{agent_only!r} is reachable only by direct `lambda:InvokeFunction` from the agents "
                f"and must NOT have an HTTP route; a route would put it inside the API surface"
            ),
        )
    for undeclared_tool in sorted(route_tool_names - toolspec_names - NON_TOOL_ROUTES):
        yield CheckResult(
            resource=f"route:{undeclared_tool}",
            check="route_has_tool",
            status="fail",
            detail=(
                f"deployed route for {undeclared_tool!r} has no matching toolspec tool and is not an "
                f"allowlisted non-tool route ({sorted(NON_TOOL_ROUTES)}). Add it to that allowlist "
                f"deliberately if it is intended -- do NOT add it to ALL_TOOLS, which would publish it "
                f"to external MCP clients"
            ),
        )
    # Each exemption is reported as its own row.
    for allowed in sorted(NON_TOOL_ROUTES & route_tool_names):
        yield CheckResult(
            resource=f"route:{allowed}",
            check="route_is_allowlisted_non_tool",
            status="pass",
            detail=(
                f"{allowed!r} is deliberately absent from the toolspec and exempt from route_has_tool "
                f"only; its authorizer and unauthenticated-rejection checks still run below"
            ),
        )

    for route in deployed_routes:
        route_key = str(route["RouteKey"])
        resource = f"route:{route_key}"
        url = _invoke_url_for_route(api_base_url, route_key)
        method = route_key.split(" ", 1)[0] if " " in route_key else "POST"

        # Both the attached `AuthorizerId` and the `authorizationType` in force are required.
        authorizer_id = route.get("AuthorizerId")
        authorization_type = route.get("AuthorizationType")
        authorized = bool(authorizer_id) and authorization_type == EXPECTED_AUTHORIZATION_TYPE
        yield CheckResult(
            resource=resource,
            check="authorizer_attached",
            status="pass" if authorized else "fail",
            detail=(
                f"authorization_type={authorization_type!r} authorizer_id={authorizer_id!r}"
                if authorized
                else (
                    f"not authorized: authorization_type={authorization_type!r} "
                    f"(expected {EXPECTED_AUTHORIZATION_TYPE!r}), authorizer_id={authorizer_id!r}"
                )
            ),
        )

        unauth_result = caller(method, url, {}, _unauthenticated_probe_body(method))
        if isinstance(unauth_result, str):
            yield CheckResult(
                resource=resource,
                check="unauthenticated_rejected",
                status="fail",
                detail=unauth_result,
            )
        else:
            status_code, _ = unauth_result
            if status_code == _UNAUTHENTICATED_REFUSAL_STATUS:
                yield CheckResult(
                    resource=resource,
                    check="unauthenticated_rejected",
                    status="pass",
                    detail=(
                        f"{_UNAUTHENTICATED_REFUSAL_STATUS} as expected, which establishes both that "
                        "the route exists and that its authorizer refused an unauthenticated caller"
                    ),
                )
            elif status_code == 403:
                yield CheckResult(
                    resource=resource,
                    check="unauthenticated_rejected",
                    status="fail",
                    detail=(
                        f"got 403, expected {_UNAUTHENTICATED_REFUSAL_STATUS}. On a PRIVATE REST API a "
                        "403 is how an UNMATCHED route answers, so the likely cause is that this "
                        f"{method} method is not deployed at this path -- read the adjacent "
                        "`authorizer_attached` row before suspecting the authorizer, since it reads the "
                        "control plane and would already have failed if the authorizer were the cause"
                    ),
                )
            else:
                yield CheckResult(
                    resource=resource,
                    check="unauthenticated_rejected",
                    status="fail",
                    detail=f"expected {_UNAUTHENTICATED_REFUSAL_STATUS}, got {status_code}",
                )

    functions: list[IntegratedFunction] = []
    audience_function: IntegratedFunction | None = None
    if deployed_identity_checks:
        lambda_client = ctx.client("lambda")
        by_route = integrated_function_names(apigw, api_id)
        functions = [
            _describe_function(lambda_client, name) for name in sorted(set(by_route.values()))
        ]
        audience_name = by_route.get(f"POST /{AUDIENCE_PROBE_TOOL}")
        audience_function = next((f for f in functions if f.name == audience_name), None)
        yield from allowlist_checks(ctx, functions)

    yield from _authenticated_pass(
        ctx,
        caller=caller,
        token=token,
        deployed_routes=deployed_routes,
        api_base_url=api_base_url,
        seeder=seeder,
        teardown=teardown,
        audience_function=audience_function,
        audience_probe=deployed_identity_checks,
        sleep=sleep,
    )

    yield from _private_endpoint_checks(apigw, api_id)
    yield from _stage_throttle_checks(apigw, api_id)
    yield from _request_validator_checks(apigw, api_id)


#: The exact endpoint-configuration type set a private REST API must carry, compared as an equal set
#: (`["PRIVATE", "EDGE"]` is publicly reachable).
_PRIVATE_ENDPOINT_TYPES: Final[list[str]] = ["PRIVATE"]


def _private_endpoint_checks(apigw: Any, api_id: str) -> Iterable[CheckResult]:
    """Assert from the CONTROL PLANE that the API is private, and that it has a VPC endpoint.

    Reads `get_rest_api`, not DNS. `vpcEndpointIds` are counted, never named in the detail.
    """
    configuration = apigw.get_rest_api(restApiId=api_id).get("endpointConfiguration") or {}
    types = list(configuration.get("types") or [])
    endpoint_count = len(list(configuration.get("vpcEndpointIds") or []))
    private = types == _PRIVATE_ENDPOINT_TYPES
    attached = endpoint_count > 0
    yield CheckResult(
        resource=f"api:{api_id}",
        check="endpoint_configuration_is_private",
        status="pass" if private and attached else "fail",
        detail=(
            f"endpointConfiguration.types == {_PRIVATE_ENDPOINT_TYPES} and {endpoint_count} VPC "
            "endpoint(s) attached, so the API is reachable only through its VPC endpoint. This is the "
            "control-plane statement of the property; the unauthenticated-refusal rows above are "
            "asserted through CloudFront and speak for the route rather than for the origin"
            if private and attached
            else f"endpointConfiguration.types == {types!r} (expected exactly "
            f"{_PRIVATE_ENDPOINT_TYPES}) with {endpoint_count} VPC endpoint(s) attached (expected at "
            "least 1). A type set that merely CONTAINS 'PRIVATE' is publicly reachable, and a private "
            "type with no VPC endpoint is unreachable by anything"
        ),
    )


#: The v1 `methodSettings` key carrying a stage's default (all-resources, all-verbs) throttle.
_STAGE_DEFAULT_METHOD_SETTINGS_KEY = "*/*"


def _stage_throttle_checks(apigw: Any, api_id: str) -> Iterable[CheckResult]:
    """Assert every deployed stage carries a default throttle rate and burst.

    v1 shapes: `restApiId`, the list is `item`, and stage-level throttling lives in
    `methodSettings["*/*"]` as `throttlingBurstLimit`/`throttlingRateLimit`. A stage with no `*/*`
    entry is a fail.
    """
    for stage in apigw.get_stages(restApiId=api_id).get("item", []):
        stage_name = str(stage.get("stageName", "<unknown>"))
        method_settings = stage.get("methodSettings") or {}
        default_settings = method_settings.get(_STAGE_DEFAULT_METHOD_SETTINGS_KEY) or {}
        burst = default_settings.get("throttlingBurstLimit")
        rate = default_settings.get("throttlingRateLimit")
        configured = burst is not None and rate is not None
        yield CheckResult(
            resource=f"stage:{stage_name}",
            check="throttle_and_burst_configured",
            status="pass" if configured else "fail",
            detail=(
                f"burst={burst} rate={rate}"
                if configured
                else (
                    f"stage {stage_name!r} declares no {_STAGE_DEFAULT_METHOD_SETTINGS_KEY!r} "
                    f"methodSettings entry, so it has no default throttle "
                    f"(keys present: {sorted(method_settings)})"
                )
            ),
        )


def _request_validator_checks(apigw: Any, api_id: str) -> Iterable[CheckResult]:
    """Assert the REST API declares at least one request validator.

    Also asserts, per method, that the validator is referenced and that every POST carries an
    `application/json` model.
    """
    validators = apigw.get_request_validators(restApiId=api_id).get("items", [])
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="request_validators_present",
        status="pass" if validators else "fail",
        detail=(
            f"{len(validators)} request validator(s): "
            f"{sorted(str(v.get('name', '<unnamed>')) for v in validators)}"
            if validators
            else (
                "the deployed REST API declares no request validator, so no route validates its "
                "body or parameters at the edge. The API is a REST API, so the check applies"
            )
        ),
    )

    # Per method: `requestValidatorId` is set, and every POST carries an `application/json` model.
    methods_without_validator: list[str] = []
    posts_without_model: list[str] = []
    saw_method = False
    position: str | None = None
    while True:
        kwargs: dict[str, Any] = {"restApiId": api_id}
        if position:
            kwargs["position"] = position
        page = apigw.get_resources(**kwargs)
        for resource in page.get("items", []):
            path = str(resource.get("path", ""))
            for http_method in resource.get("resourceMethods") or {}:
                saw_method = True
                method = apigw.get_method(
                    restApiId=api_id,
                    resourceId=str(resource["id"]),
                    httpMethod=str(http_method),
                )
                route_key = f"{http_method} {path}"
                if not method.get("requestValidatorId"):
                    methods_without_validator.append(route_key)
                if str(http_method).upper() == "POST" and "application/json" not in (
                    method.get("requestModels") or {}
                ):
                    posts_without_model.append(route_key)
        position = page.get("position")
        if not position:
            break
    ok = saw_method and not methods_without_validator and not posts_without_model
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="request_validator_on_every_method",
        status="pass" if ok else "fail",
        detail=(
            "every method references a request validator and every POST carries an application/json "
            "model"
            if ok
            else (
                "no methods enumerated (a broken enumeration, not a clean pass over zero routes)"
                if not saw_method
                else (
                    f"methods with no requestValidatorId={sorted(methods_without_validator)}; "
                    f"POST methods with no application/json model={sorted(posts_without_model)} -- "
                    "an API-level validator these do not reference enforces nothing"
                )
            )
        ),
    )


def _authenticated_pass(
    ctx: SmokeContext,
    *,
    caller: HttpCaller,
    token: str | None,
    deployed_routes: Sequence[Mapping[str, Any]],
    api_base_url: str,
    seeder: TenantSeeder | None,
    teardown: TenantTeardown | None,
    audience_function: IntegratedFunction | None = None,
    audience_probe: bool = False,
    sleep: Sleeper = time.sleep,
) -> Iterable[CheckResult]:
    """Yield the `authenticated_non_error` check for every route, plus its tenant preamble.

    With `audience_probe`, the direct-invoke audience pair runs against `audience_function` while
    the tenant is seeded; an unresolved function is a failing row.

    Refuses to issue a single authenticated call unless the token's own `custom:tenant` claim
    names an ``eval-`` smoke tenant. A refusal is reported as a failing check per route.
    """
    route_keys = [str(route["RouteKey"]) for route in deployed_routes]
    if token is None:
        for route_key in route_keys:
            yield CheckResult(
                resource=f"route:{route_key}",
                check="authenticated_non_error",
                status="fail",
                detail="no --token/CRM_SMOKE_TOKEN supplied; an authorization boundary that was "
                "never observed is not a passing one",
            )
        return

    claims, unverified = verified_token_claims(ctx, token)
    claimed_tenant = str(claims.get(TENANT_CLAIM) or "")
    tenant = ctx.tenant or claimed_tenant
    refusal = unverified or _tenant_refusal(
        tenant=tenant, claimed_tenant=claimed_tenant, ctx_tenant=ctx.tenant
    )
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="token_tenant_is_disposable_smoke_tenant",
        status="fail" if refusal else "pass",
        detail=refusal
        or f"token {TENANT_CLAIM}={tenant!r}: a disposable eval smoke tenant, never {DEMO_TENANT!r}",
    )
    if refusal:
        for route_key in route_keys:
            yield CheckResult(
                resource=f"route:{route_key}",
                check="authenticated_non_error",
                status="fail",
                detail=f"refused to call authenticated: {refusal}",
            )
        return

    owns_lifecycle = ctx.tenant is None
    do_seed = seeder if seeder is not None else _default_seeder(ctx)
    do_teardown = teardown if teardown is not None else _default_teardown(ctx)
    if owns_lifecycle:
        do_seed(tenant)
    try:
        yield from _authenticated_route_checks(
            caller=caller,
            token=token,
            claims=claims,
            route_keys=route_keys,
            api_base_url=api_base_url,
            tenant=tenant,
        )
        if audience_probe:
            if audience_function is None:
                yield CheckResult(
                    resource=RESOURCE_CLASS,
                    check="audience_probe_function_resolved",
                    status="fail",
                    detail=f"no integrated function for {AUDIENCE_PROBE_TOOL!r} was resolved",
                )
            else:
                yield from audience_probe_checks(
                    ctx, function=audience_function, claims=claims, tenant=tenant, sleep=sleep
                )
    finally:
        if owns_lifecycle:
            yield from _teardown_check(tenant, do_teardown)


def _tenant_refusal(*, tenant: str, claimed_tenant: str, ctx_tenant: str | None) -> str | None:
    """Return why no authenticated call may be made, or `None` when it is safe to proceed."""
    if not tenant:
        return (
            f"supplied token carries no {TENANT_CLAIM} claim, so the tenant its writes would "
            "land in cannot be established"
        )
    if ctx_tenant is not None and claimed_tenant and claimed_tenant != ctx_tenant:
        return (
            f"--tenant {ctx_tenant!r} disagrees with the token's {TENANT_CLAIM}="
            f"{claimed_tenant!r}; the token's claim is what the Lambda enforces"
        )
    if tenant == DEMO_TENANT:
        return (
            f"token is scoped to the {DEMO_TENANT!r} tenant; smoke runs never write there, so no "
            "authenticated call was made"
        )
    if not tenant.startswith(EVAL_TENANT_PREFIX):
        return (
            f"token tenant {tenant!r} is not a disposable {EVAL_TENANT_PREFIX!r} smoke tenant; "
            "refusing to write into a tenant this script cannot seed and tear down"
        )
    return None


def _authenticated_route_checks(
    *,
    caller: HttpCaller,
    token: str,
    claims: Mapping[str, Any],
    route_keys: Sequence[str],
    api_base_url: str,
    tenant: str,
) -> Iterable[CheckResult]:
    """Call every route with ``token`` and a real per-tool body, asserting a non-error answer.

    A route in :data:`NON_TOOL_ROUTE_PROBES` is asserted against the answer its probe derives from
    ``claims``, the token's verified claims, instead.
    """
    tools = _tool_by_name()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for route_key in route_keys:
        resource = f"route:{route_key}"
        method = route_key.split(" ", 1)[0] if " " in route_key else "POST"
        tool_name = route_key.split(" ", 1)[1].lstrip("/") if " " in route_key else ""
        if tool_name in _NO_STATIC_SEED_TOOLS:
            yield CheckResult(
                resource=resource,
                check="authenticated_non_error",
                status="skip",
                detail=(
                    T3_PENDING_DETAIL.format(tool=tool_name)
                    if tool_name in _T3_PENDING_TOOLS
                    else (
                        f"{tool_name} acts on a proposal/audit record fixtures.generate never "
                        "seeds statically, so no body this Layer 4 pass could send would resolve; "
                        "the create-then-act chain is exercised at Layer 5 "
                        "(integration_proposal_chain.py) instead"
                    )
                ),
            )
            continue
        probe = NON_TOOL_ROUTE_PROBES.get(tool_name) if method.upper() == "POST" else None
        if probe is not None:
            yield _non_tool_probe_check(
                caller=caller,
                url=_invoke_url_for_route(api_base_url, route_key),
                headers=headers,
                resource=resource,
                probe=probe,
                tenant=tenant,
                claims=claims,
            )
            continue
        tool = tools.get(tool_name)
        # A bodyless method is called with no entity, as in the unauthenticated probe.
        body = (
            ""
            if method.upper() in _BODYLESS_METHODS
            else json.dumps(request_body_for(tool, tenant=tenant))
            if tool is not None
            else "{}"
        )
        result = caller(method, _invoke_url_for_route(api_base_url, route_key), headers, body)
        if isinstance(result, str):
            yield CheckResult(
                resource=resource, check="authenticated_non_error", status="fail", detail=result
            )
            continue
        status_code, response_body = result
        if status_code < 400:
            yield CheckResult(
                resource=resource,
                check="authenticated_non_error",
                status="pass",
                detail=f"status={status_code}",
            )
        else:
            yield CheckResult(
                resource=resource,
                check="authenticated_non_error",
                status="fail",
                detail=f"status={status_code} body={response_body[:300]!r} request_body={body!r}",
            )


def _error_field_of(response_body: str, field: str) -> str | None:
    """Return the string ``error.<field>`` of a taxonomy-shaped JSON response body, or ``None``."""
    try:
        decoded = json.loads(response_body)
    except json.JSONDecodeError:
        return None
    error = decoded.get("error") if isinstance(decoded, dict) else None
    value = error.get(field) if isinstance(error, dict) else None
    return value if isinstance(value, str) else None


def _error_code_of(response_body: str) -> str | None:
    """Return the ``error.code`` of a taxonomy-shaped JSON response body, or ``None``."""
    return _error_field_of(response_body, "code")


def _error_missing_scope_of(response_body: str) -> str | None:
    """Return the ``error.missing_scope`` of a taxonomy-shaped JSON response body, or ``None``."""
    return _error_field_of(response_body, "missing_scope")


def _success_body_has(response_body: str, keys: frozenset[str]) -> bool:
    """Return whether ``response_body`` is a JSON object with no ``error`` member carrying ``keys``."""
    try:
        decoded = json.loads(response_body)
    except json.JSONDecodeError:
        return False
    return isinstance(decoded, dict) and "error" not in decoded and keys <= set(decoded)


def _matches(expected: ProbeExpectation, status_code: int | None, response_body: str) -> bool:
    """Return whether a response has ``expected``'s status and ``error.code``, and its
    ``error.missing_scope`` when ``expected`` sets one; for a success expectation, whether the
    body carries ``expected.body_keys``."""
    if expected.error_code is None:
        return status_code == expected.status and _success_body_has(
            response_body, expected.body_keys
        )
    return (
        status_code == expected.status
        and _error_code_of(response_body) == expected.error_code
        and (
            expected.missing_scope is None
            or _error_missing_scope_of(response_body) == expected.missing_scope
        )
    )


def _non_tool_probe_check(
    *,
    caller: HttpCaller,
    url: str,
    headers: Mapping[str, str],
    resource: str,
    probe: NonToolProbe,
    tenant: str,
    claims: Mapping[str, Any],
) -> CheckResult:
    """Send ``probe``'s body and pass only on the status, ``error.code`` and, when the expectation
    sets one, ``error.missing_scope`` that ``claims`` call for.

    Claims that cannot decide the expected answer fail the row without a call.
    """
    try:
        expected = probe.expected(claims)
    except UnreadableClaimError as exc:
        return CheckResult(
            resource=resource,
            check="authenticated_non_error",
            status="fail",
            detail=f"cannot derive the expected answer, so no call was made: {exc}",
        )
    body = json.dumps(probe.body(tenant))
    result = caller("POST", url, headers, body)
    if isinstance(result, str):
        return CheckResult(
            resource=resource, check="authenticated_non_error", status="fail", detail=result
        )
    status_code, response_body = result
    error_code = _error_code_of(response_body)
    missing_scope = _error_missing_scope_of(response_body)
    if _matches(expected, status_code, response_body):
        return CheckResult(
            resource=resource,
            check="authenticated_non_error",
            status="pass",
            detail=(
                f"status={status_code} error.code={error_code} "
                f"error.missing_scope={missing_scope} request_body={body!r}"
            ),
        )
    return CheckResult(
        resource=resource,
        check="authenticated_non_error",
        status="fail",
        detail=(
            f"expected status={expected.status} error.code={expected.error_code} "
            f"error.missing_scope={expected.missing_scope}"
            + ("" if expected.error_code else f" body_keys={sorted(expected.body_keys)}")
            + f", got status={status_code} "
            f"error.code={error_code} error.missing_scope={missing_scope} "
            f"body={response_body[:300]!r} request_body={body!r}"
        ),
    )


def _teardown_check(tenant: str, do_teardown: TenantTeardown) -> Iterable[CheckResult]:
    """Tear ``tenant`` down, reporting a failure as its own failing check rather than swallowing it."""
    try:
        deleted = do_teardown(tenant)
    except TenantTeardownError as exc:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="tenant_left_clean",
            status="fail",
            detail=f"teardown of tenant {tenant!r} failed: {exc}",
        )
        return
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="tenant_left_clean",
        status="pass",
        detail=f"tenant {tenant!r} torn down: {deleted!r}",
    )


# ---------------------------------------------------------------------------
# The apigw channel's app-client allowlist, as deployed.
# ---------------------------------------------------------------------------

#: The environment variable an earlier deployment carried the allowlist in directly; read by nothing.
LEGACY_ALLOWED_CLIENT_IDS_ENV: Final[str] = "CRM_API_ALLOWED_CLIENT_IDS"

#: The tool whose deployed Lambda the direct-invoke audience probe calls.
AUDIENCE_PROBE_TOOL: Final[str] = "get_account"

#: The tag every data-bearing resource must carry.
DATA_CLASSIFICATION_TAG: Final[str] = "data-classification"

#: The SSM write actions no project role may hold on the allowlist parameter.
SSM_WRITE_ACTIONS: Final[tuple[str, ...]] = (
    "ssm:PutParameter",
    "ssm:DeleteParameter",
    "ssm:LabelParameterVersion",
)

#: The refusal detail `crm.envelope` gives a token from an unlisted app client, which the handler's
#: `crm_error` log line carries in its `reason`.
AUDIENCE_REFUSAL_REASON: Final[str] = "is not an admitted app client"

#: Bounded poll for the refusal's log line: attempts and the wait between them, in seconds.
LOG_POLL_ATTEMPTS: Final[int] = 6
LOG_POLL_INTERVAL_SECONDS: Final[float] = 5.0

_INTEGRATION_FUNCTION_RE: Final[re.Pattern[str]] = re.compile(
    r"functions/(arn:aws[a-z-]*:lambda:[^/:]+:[0-9]{12}:function:[^/:]+)"
)

Sleeper = Callable[[float], None]
Clock = Callable[[], float]


@dataclass(frozen=True)
class IntegratedFunction:
    """One Lambda function a REST API method is integrated with, as deployed."""

    name: str
    arn: str
    role_arn: str
    environment: Mapping[str, str]
    log_group: str


def integrated_function_names(apigw: Any, api_id: str) -> dict[str, str]:
    """Map each deployed `<METHOD> /<path>` route to the Lambda function name it integrates.

    Read from each method's `get_integration` URI; a method without a Lambda integration is omitted.
    """
    names: dict[str, str] = {}
    position: str | None = None
    while True:
        kwargs: dict[str, Any] = {"restApiId": api_id}
        if position:
            kwargs["position"] = position
        page = apigw.get_resources(**kwargs)
        for resource in page.get("items", []):
            path = str(resource.get("path", ""))
            for http_method in resource.get("resourceMethods") or {}:
                integration = apigw.get_integration(
                    restApiId=api_id, resourceId=str(resource["id"]), httpMethod=str(http_method)
                )
                match = _INTEGRATION_FUNCTION_RE.search(str(integration.get("uri", "")))
                if match is not None:
                    names[f"{http_method} {path}"] = match.group(1).rsplit(":", 1)[-1]
        position = page.get("position")
        if not position:
            return names


def _describe_function(lambda_client: Any, name: str) -> IntegratedFunction:
    configuration = lambda_client.get_function_configuration(FunctionName=name)
    variables = (configuration.get("Environment") or {}).get("Variables") or {}
    logging_config = configuration.get("LoggingConfig") or {}
    return IntegratedFunction(
        name=name,
        arn=str(configuration.get("FunctionArn", "")),
        role_arn=str(configuration.get("Role", "")),
        environment={str(key): str(value) for key, value in variables.items()},
        log_group=str(logging_config.get("LogGroup") or f"/aws/lambda/{name}"),
    )


def _parameter_arn(region: str, function_arn: str, name: str) -> str:
    """The ARN of SSM parameter `name` in the function's own account and `region`."""
    account = function_arn.split(":")[4] if function_arn.count(":") >= 5 else ""
    return f"arn:aws:ssm:{region}:{account}:parameter/{name.lstrip('/')}"


def _simulated_decisions(
    iam: Any, role_arn: str, actions: Sequence[str], resource_arn: str
) -> dict[str, tuple[str, list[str]]]:
    """`action -> (EvalDecision, MissingContextValues)` for one role, actions and resource."""
    response = iam.simulate_principal_policy(
        PolicySourceArn=role_arn, ActionNames=list(actions), ResourceArns=[resource_arn]
    )
    decisions: dict[str, tuple[str, list[str]]] = {}
    for result in response.get("EvaluationResults", []):
        decisions[str(result.get("EvalActionName"))] = (
            str(result.get("EvalDecision")),
            [str(value) for value in result.get("MissingContextValues") or []],
        )
    return decisions


def _not_allowed_row(
    iam: Any,
    *,
    resource: str,
    check: str,
    role_arn: str,
    probes: Sequence[tuple[str, str]],
) -> CheckResult:
    """One row passing only when every `(action, resource_arn)` probe is not `allowed`."""
    allowed: list[str] = []
    for action, resource_arn in probes:
        decision, _ = _simulated_decisions(iam, role_arn, [action], resource_arn).get(
            action, ("missing", [])
        )
        if decision == "missing":
            allowed.append(f"{action} on {resource_arn}: no evaluation result")
        elif decision == "allowed":
            allowed.append(f"{action} on {resource_arn}")
    return CheckResult(
        resource=resource,
        check=check,
        status="fail" if allowed else "pass",
        detail=(
            f"allowed (must not be): {allowed}"
            if allowed
            else f"{len(probes)} probe(s) not allowed for {role_arn.rsplit('/', 1)[-1]}"
        ),
    )


def allowlist_checks(
    ctx: SmokeContext, functions: Sequence[IntegratedFunction]
) -> Iterable[CheckResult]:
    """Assert the deployed allowlist wiring: env on every integrated function, parameter, IAM."""
    if not functions:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="resolve_integrated_functions",
            status="fail",
            detail="no REST API method resolved to a Lambda integration, so nothing was checked",
        )
        return
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="resolve_integrated_functions",
        status="pass",
        detail=f"{len(functions)} integrated function(s) resolved from the deployed API",
    )
    values: set[str] = set()
    for function in functions:
        value = function.environment.get(API_ALLOWED_CLIENT_IDS_PARAMETER_ENV, "")
        if value:
            values.add(value)
        yield CheckResult(
            resource=f"function:{function.name}",
            check="allowlist_parameter_env_set",
            status="pass" if value else "fail",
            detail=f"{API_ALLOWED_CLIENT_IDS_PARAMETER_ENV}={value!r}",
        )
        legacy = LEGACY_ALLOWED_CLIENT_IDS_ENV in function.environment
        yield CheckResult(
            resource=f"function:{function.name}",
            check="legacy_allowlist_env_absent",
            status="fail" if legacy else "pass",
            detail=(
                f"{LEGACY_ALLOWED_CLIENT_IDS_ENV} is set but read by nothing"
                if legacy
                else f"{LEGACY_ALLOWED_CLIENT_IDS_ENV} absent"
            ),
        )
    consistent = len(values) == 1
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="allowlist_parameter_env_consistent",
        status="pass" if consistent else "fail",
        detail=f"distinct non-empty values: {sorted(values)}",
    )
    if not consistent:
        return
    (name,) = values
    spa_client_id = ctx.outputs.require_str("cognito_spa_client_id")
    ssm = ctx.client("ssm")
    parameter = ssm.get_parameter(Name=name)["Parameter"]
    raw_value = str(parameter.get("Value", ""))
    listed = [part.strip() for part in raw_value.split(",") if part.strip()]
    yield CheckResult(
        resource=f"parameter:{name}",
        check="allowlist_parameter_value_is_spa_client",
        status="pass" if raw_value == spa_client_id and listed == [spa_client_id] else "fail",
        detail=f"{len(listed)} client id(s) listed; equals cognito_spa_client_id: "
        f"{raw_value == spa_client_id}",
    )
    parameter_type = str(parameter.get("Type", ""))
    yield CheckResult(
        resource=f"parameter:{name}",
        check="allowlist_parameter_type_string",
        status="pass" if parameter_type == "String" else "fail",
        detail=f"Type={parameter_type!r}",
    )
    tags = ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name).get("TagList", [])
    tagged = any(str(tag.get("Key")) == DATA_CLASSIFICATION_TAG for tag in tags)
    yield CheckResult(
        resource=f"parameter:{name}",
        check="allowlist_parameter_tagged",
        status="pass" if tagged else "fail",
        detail=f"tag keys: {sorted(str(tag.get('Key')) for tag in tags)}",
    )

    iam = ctx.client("iam")
    parameter_arn = _parameter_arn(ctx.region, functions[0].arn, name)
    base = parameter_arn.rsplit("/", 1)[0]
    root = parameter_arn.split(":parameter/", 1)[0] + ":parameter/" + name.strip("/").split("/")[0]
    api_roles = sorted({function.role_arn for function in functions if function.role_arn})
    for role_arn in api_roles:
        role = f"role:{role_arn.rsplit('/', 1)[-1]}"
        decision, missing = _simulated_decisions(
            iam, role_arn, ["ssm:GetParameter"], parameter_arn
        ).get("ssm:GetParameter", ("missing", []))
        yield CheckResult(
            resource=role,
            check="ssm_get_parameter_allowed",
            status="pass" if decision == "allowed" and not missing else "fail",
            detail=f"decision={decision} missing_context={missing}",
        )
        yield _not_allowed_row(
            iam,
            resource=role,
            check="ssm_read_grant_is_exact_parameter",
            role_arn=role_arn,
            probes=[
                ("ssm:GetParameter", f"{base}/other"),
                ("ssm:GetParameter", f"{root}/*"),
                ("ssm:GetParameters", parameter_arn),
                ("ssm:GetParametersByPath", parameter_arn),
            ],
        )
    non_api_roles = sorted(
        {
            ctx.outputs.require_str("agentcore_runtime_role_arn"),
            ctx.outputs.require_str("agent_relay_role_arn"),
        }
    )
    for role_arn in sorted(set(api_roles) | set(non_api_roles)):
        yield _not_allowed_row(
            iam,
            resource=f"role:{role_arn.rsplit('/', 1)[-1]}",
            check="ssm_parameter_write_denied",
            role_arn=role_arn,
            probes=[(action, parameter_arn) for action in SSM_WRITE_ACTIONS],
        )
    for role_arn in non_api_roles:
        yield _not_allowed_row(
            iam,
            resource=f"role:{role_arn.rsplit('/', 1)[-1]}",
            check="ssm_get_parameter_denied_non_api_role",
            role_arn=role_arn,
            probes=[("ssm:GetParameter", parameter_arn)],
        )


def _apigw_event(tool: ToolDefinition, claims: Mapping[str, Any], *, tenant: str) -> dict[str, Any]:
    """A REST (payload v1) proxy event for `tool`, as the Cognito-authorized route delivers it."""
    return {
        "resource": f"/{tool.name}",
        "path": f"/{tool.name}",
        "httpMethod": "POST",
        "headers": {"content-type": "application/json"},
        "requestContext": {
            "requestId": f"smoke-audience-{hashlib.sha256(tenant.encode()).hexdigest()[:12]}",
            "authorizer": {"claims": {key: str(value) for key, value in claims.items()}},
        },
        "body": json.dumps(request_body_for(tool, tenant=tenant)),
        "isBase64Encoded": False,
    }


def _invoke_payload(lambda_client: Any, name: str, event: Mapping[str, Any]) -> tuple[str, Any]:
    """Invoke `name` synchronously; return `(FunctionError or "None", decoded payload)`."""
    response = lambda_client.invoke(
        FunctionName=name, InvocationType="RequestResponse", Payload=json.dumps(event).encode()
    )
    raw = response["Payload"].read()
    try:
        payload: Any = json.loads(raw)
    except (TypeError, ValueError):
        payload = raw
    return str(response.get("FunctionError") or "None"), payload


def _payload_status_and_code(payload: Any) -> tuple[int | None, str | None]:
    if not isinstance(payload, Mapping):
        return None, None
    status = payload.get("statusCode")
    body = payload.get("body")
    code = _error_code_of(body) if isinstance(body, str) else None
    return (int(status) if isinstance(status, int) else None), code


def audience_probe_checks(
    ctx: SmokeContext,
    *,
    function: IntegratedFunction,
    claims: Mapping[str, Any],
    tenant: str,
    sleep: Sleeper = time.sleep,
    clock: Clock = time.time,
) -> Iterable[CheckResult]:
    """Invoke the deployed `get_account` Lambda with claims that differ only in their app client.

    A foreign `aud` on an ID token and a foreign `client_id` on an access token must each be
    answered 401 `authentication_failed`; the SPA client's own `aud` must be admitted. The refusal's
    `crm_error` log line must name the client check, not a parameter read.
    """
    tool = _tool_by_name()[AUDIENCE_PROBE_TOOL]
    lambda_client = ctx.client("lambda")
    spa_client_id = ctx.outputs.require_str("cognito_spa_client_id")
    foreign_client_id = ctx.outputs.require_str("cognito_mcp_oauth_client_id")
    resource = f"function:{function.name}"
    base = {key: value for key, value in claims.items() if key not in {"aud", "client_id"}}
    started_ms = int(clock() * 1000)
    variants = (
        ("audience_foreign_id_token_refused", {"token_use": "id", "aud": foreign_client_id}, 401),
        ("audience_spa_id_token_admitted", {"token_use": "id", "aud": spa_client_id}, None),
        (
            "audience_foreign_access_token_refused",
            {"token_use": "access", "client_id": foreign_client_id},
            401,
        ),
    )
    for check, override, expected in variants:
        event = _apigw_event(tool, {**base, **override}, tenant=tenant)
        function_error, payload = _invoke_payload(lambda_client, function.name, event)
        status, code = _payload_status_and_code(payload)
        if expected is None:
            ok = function_error == "None" and status is not None and status < 400
        else:
            ok = function_error == "None" and status == expected and code == "authentication_failed"
        yield CheckResult(
            resource=resource,
            check=check,
            status="pass" if ok else "fail",
            detail=f"FunctionError={function_error} statusCode={status} error.code={code}",
        )

    logs = ctx.client("logs")
    reasons: list[str] = []
    named: list[str] = []
    for attempt in range(LOG_POLL_ATTEMPTS):
        response = logs.filter_log_events(
            logGroupName=function.log_group,
            startTime=started_ms,
            filterPattern='"crm_error"',
        )
        reasons = [
            str(record.get("reason", ""))
            for record in (_json_object(e.get("message")) for e in response.get("events", []))
            if record.get("event") == "crm_error"
        ]
        named = [r for r in reasons if AUDIENCE_REFUSAL_REASON in r and "parameter=" not in r]
        if named:
            break
        if attempt + 1 < LOG_POLL_ATTEMPTS:
            sleep(LOG_POLL_INTERVAL_SECONDS)
    yield CheckResult(
        resource=resource,
        check="audience_refusal_logged_with_reason",
        status="pass" if named else "fail",
        detail=(
            f"crm_error reason names the client check: {named[0][:200]!r}"
            if named
            else f"no crm_error line naming the client check within the poll; reasons={reasons}"
        ),
    )


def _json_object(message: object) -> dict[str, Any]:
    """The JSON object a log message carries, if any; a Lambda line may prefix it with metadata."""
    if not isinstance(message, str):
        return {}
    start = message.find("{")
    if start < 0:
        return {}
    try:
        value = json.loads(message[start:])
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: resolve the token from `--token`/`CRM_SMOKE_TOKEN` and run the checks."""
    token = _token_from_env_or_flag(argv)
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=lambda ctx: checks(ctx, token=token, deployed_identity_checks=True),
        extra_arguments=["--token"],
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
