# SPDX-License-Identifier: MIT
"""Layer 4 smoke script: asserts the CloudFront distribution's origin-privacy posture.

Needs a live deployment. Asserts the S3 origin is reached only via Origin Access Control
(OAC) - never a legacy Origin Access Identity (OAI) or public access - and that a direct
request to the S3 origin URL is refused (403). Asserts the distribution's configured
viewer TLS minimum, and separately probes an actual sub-1.2 handshake against the live
domain. Asserts all three cache behaviours (`/*`, `/api/*`, `/agent/*`) route to the
correct origin per the pinned Terraform outputs, with caching disabled and `Authorization`
forwarded on the two identity-bearing behaviours (`infra/modules/frontend_cdn/cloudfront.tf`).
`/api/*` is a VPC origin, so "non-S3 origin" means `CustomOriginConfig` or `VpcOriginConfig`;
`_select_s3_origin` requires exactly one S3-shaped origin. `/agent/*` additionally carries three
assertions about what must be absent from the deployed distribution: the retired AgentCore
data-plane origin, a viewer-request function association, and response compression.

Asserts one custom response-headers policy on every behaviour, carrying an enforced
`Content-Security-Policy` (`Override` on) and `X-Frame-Options: DENY`; that each probed behaviour
serves `Strict-Transport-Security` with max-age=31536000, includeSubDomains and preload; and that the
served CSP on `/`, an unauthenticated `/api/*` response and an unauthenticated `/agent/*` response is
that policy: the designed directive set, Cognito in `connect-src`, and a `sha256-` source for every
inline script the served document carries.

Then drives the distribution over the network: a client-side deep link must return the built SPA
document (and a missing asset path must not), while an unauthenticated `/api/*` request must still
be refused with a 401/403 rather than a `200` page of HTML
(`infra/modules/frontend_cdn/functions/spa_history_fallback.js`).

`minimum_protocol_version` on `viewer_certificate` is only honoured by CloudFront when a
custom ACM certificate is configured; with `cloudfront_default_certificate = true` the
setting is inert, so TLS 1.2 is not enforced on this distribution. This script reports that case
as `defer` with the machine-readable reason `missing_custom_acm_certificate`, never `pass` or
`skip`. See `infra/modules/frontend_cdn/README.md`.
"""

from __future__ import annotations

import base64
import hashlib
import re
import ssl
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from scripts.smoke.harness import DEFER, CheckResult, SmokeContext, open_https, run_main

from eval.verification import Status

LAYER = 4
RESOURCE_CLASS = "cloudfront"

#: Machine-readable `defer_reason` for the inert-`minimum_protocol_version` gap, matched on by
#: readers of the inventory JSON.
_INERT_TLS_DEFER_REASON = "missing_custom_acm_certificate"

# (status_code, body) for a completed HTTP response, or a `str` transport-failure message
# when no response was received at all (mirrors scripts/smoke/api_routes.py's HttpResult).
HttpResult = tuple[int, str] | str
HttpCaller = Callable[[str], HttpResult]

# `(url, method) -> HttpResult`. The negative-auth probe's seam, which chooses its method.
HttpMethodCaller = Callable[[str, str], HttpResult]

# `(status_code, lowercased headers, body)` for a completed response, or a `str` transport failure.
HeaderResult = tuple[int, Mapping[str, str], str] | str

# `(url, method) -> HeaderResult`. The seam for the probes that read response headers.
HeaderCaller = Callable[[str, str], HeaderResult]

# `True` if the sub-1.2 handshake was refused by the server, `False` if it unexpectedly
# succeeded, or a `str` explaining why the local TLS stack could not even attempt the
# probe (e.g. TLS 1.0/1.1 is disabled at the OpenSSL level) - that case is `skip`, never `pass`.
TlsProbeResult = bool | str
TlsProbe = Callable[[str], TlsProbeResult]

#: CloudFront `MinimumProtocolVersion` values that enforce a TLS 1.2-or-better floor.
#: AWS also offers `SSLv3`, `TLSv1`, `TLSv1_2016`, and `TLSv1.1_2016`, all of which permit a
#: sub-1.2 handshake and must never be accepted here.
_TLS_1_2_OR_BETTER_PROTOCOL_VERSIONS: frozenset[str] = frozenset(
    {"TLSv1.2_2018", "TLSv1.2_2019", "TLSv1.2_2021"}
)


def default_http_caller(url: str) -> HttpResult:
    """Perform one unauthenticated GET using only `urllib.request` (no `requests` dep).

    Returns `(status_code, body)` for any received response, including 4xx (captured via
    `HTTPError`, not treated as a transport failure). Returns a `str` only when no HTTP
    response was received at all (e.g. connection refused, DNS failure).
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method="GET")  # noqa: S310 - internal seam
    try:
        with open_https(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return f"transport error calling {url}: {exc}"


def default_http_method_caller(url: str, method: str) -> HttpResult:
    """Perform one unauthenticated request with an explicit method, using only `urllib.request`.

    Sends an empty JSON object as the body for methods that carry one. Returns
    `(status_code, body)` for any received response, including 4xx; a `str` only when no HTTP
    response was received at all.
    """
    import urllib.error
    import urllib.request

    body = b"{}" if method in {"POST", "PUT", "PATCH"} else None
    request = urllib.request.Request(  # noqa: S310 - internal seam
        url,
        method=method,
        data=body,
        headers={"content-type": "application/json"} if body is not None else {},
    )
    try:
        with open_https(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return f"transport error calling {method} {url}: {exc}"


def default_header_caller(url: str, method: str) -> HeaderResult:
    """Perform one unauthenticated request with no body and return its status, headers and body.

    Header names are lowercased. Returns `(status_code, headers, body)` for any received response,
    including 4xx; a `str` only when no HTTP response was received at all.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method=method)  # noqa: S310 - internal seam
    try:
        with open_https(request, timeout=30) as response:
            headers = {name.lower(): value for name, value in response.headers.items()}
            return response.status, headers, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        headers = {name.lower(): value for name, value in exc.headers.items()}
        return exc.code, headers, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return f"transport error calling {method} {url}: {exc}"


def default_tls_probe(domain_name: str) -> TlsProbeResult:
    """Attempt a real TLS 1.0/1.1 handshake against `domain_name:443` and report the outcome.

    Returns `True` if the handshake was refused (the desired, secure outcome), `False` if
    it unexpectedly succeeded, or a `str` reason if the local OpenSSL build cannot offer a
    sub-1.2 protocol at all, which the caller reports as `skip`.
    """
    import socket

    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1
        context.maximum_version = ssl.TLSVersion.TLSv1_1
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    except (ValueError, AttributeError) as exc:
        return f"local TLS stack cannot configure a sub-1.2 ceiling: {exc}"

    try:
        with (
            socket.create_connection((domain_name, 443), timeout=10) as sock,
            context.wrap_socket(sock, server_hostname=domain_name),
        ):
            return False  # handshake succeeded - the server accepted a sub-1.2 protocol
    except ssl.SSLError:
        return True  # handshake was refused - the desired outcome
    except OSError as exc:
        return f"could not reach {domain_name}:443 to attempt the probe: {exc}"


def _origin_by_id(distribution_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return a mapping of `origin_id -> origin config` from a distribution config."""
    origins = distribution_config.get("Origins", {}).get("Items", [])
    return {str(origin["Id"]): origin for origin in origins}


def _behavior_by_path(distribution_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return a mapping of `path_pattern -> behavior config`, `"*"` for the default behavior."""
    behaviors: dict[str, dict[str, Any]] = {
        "*": distribution_config.get("DefaultCacheBehavior", {}),
    }
    for item in distribution_config.get("CacheBehaviors", {}).get("Items", []):
        behaviors[str(item["PathPattern"])] = item
    return behaviors


#: Origin-config keys that positively identify a non-S3 origin. An S3 origin carries neither, so a
#: legacy-OAI origin and a fully public one (no `S3OriginConfig`) are both selected as S3-shaped.
_NON_S3_ORIGIN_CONFIG_KEYS: tuple[str, ...] = ("CustomOriginConfig", "VpcOriginConfig")


def _select_s3_origin(
    origins_by_id: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any]] | str:
    """The distribution's single S3-shaped origin as `(origin_id, origin)`, or why there isn't one.

    Exactly one match is required; the failure names the matching ids.
    """
    matches = {
        origin_id: origin
        for origin_id, origin in origins_by_id.items()
        if not any(key in origin for key in _NON_S3_ORIGIN_CONFIG_KEYS)
    }
    if not matches:
        return (
            "no S3-shaped origin on the distribution: every origin carries one of "
            f"{list(_NON_S3_ORIGIN_CONFIG_KEYS)} (origins: {sorted(origins_by_id)})"
        )
    if len(matches) > 1:
        return (
            f"expected exactly one S3-shaped origin, found {len(matches)}: {sorted(matches)}. "
            "Selecting one of them would test an origin this check was not written for."
        )
    origin_id, origin = next(iter(matches.items()))
    return origin_id, origin


def _check_oac_only_origin(origin_id: str, s3_origin: dict[str, Any]) -> Iterable[CheckResult]:
    """Assert the S3 origin uses OAC and never a legacy OAI, detecting both a legacy-OAI
    origin and a publicly-readable (no OAC, no OAI) origin as failures.

    Takes the origin `_select_s3_origin` selected.
    """
    oac_id = s3_origin.get("OriginAccessControlId") or ""
    legacy_oai = (
        s3_origin.get("S3OriginConfig", {}).get("OriginAccessIdentity", "")
        if "S3OriginConfig" in s3_origin
        else ""
    )

    if legacy_oai:
        yield CheckResult(
            resource="origin:s3",
            check="oac_only_origin_access",
            status="fail",
            detail=f"origin {origin_id!r} uses a legacy Origin Access Identity "
            f"({legacy_oai!r}); OAC required",
        )
    elif not oac_id:
        yield CheckResult(
            resource="origin:s3",
            check="oac_only_origin_access",
            status="fail",
            detail=f"origin {origin_id!r} has neither an OriginAccessControlId nor a legacy OAI - "
            "likely a publicly readable origin",
        )
    else:
        yield CheckResult(
            resource="origin:s3",
            check="oac_only_origin_access",
            status="pass",
            detail=f"origin {origin_id!r} OriginAccessControlId={oac_id}",
        )


def _check_direct_origin_request_refused(
    s3_domain_name: str, http_caller: HttpCaller
) -> Iterable[CheckResult]:
    """Assert a direct GET to the S3 origin's bucket domain is refused with 403.

    A 200 is a failure (publicly readable origin). A 404 is reported as its own distinct
    failure, since it can mean "wrong URL".
    """
    url = f"https://{s3_domain_name}/"
    result = http_caller(url)
    resource = "origin:s3"
    check = "direct_origin_request_refused"

    if isinstance(result, str):
        yield CheckResult(resource=resource, check=check, status="fail", detail=result)
        return

    status_code, _body = result
    if status_code == 403:
        yield CheckResult(resource=resource, check=check, status="pass", detail="403 as expected")
    elif status_code == 200:
        yield CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail="got 200 - S3 origin is publicly readable, OAC is not the only access path",
        )
    elif status_code == 404:
        yield CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail="got 404, not 403 - likely the wrong origin URL rather than a refused request",
        )
    else:
        yield CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"expected 403, got {status_code}",
        )


def _check_viewer_tls(
    distribution_config: dict[str, Any], domain_name: str, tls_probe: TlsProbe
) -> Iterable[CheckResult]:
    """Assert the configured minimum protocol version, and separately probe an actual
    sub-1.2 handshake.

    When the distribution uses the default `cloudfront.net` certificate,
    `MinimumProtocolVersion` is inert: CloudFront does not enforce it, so TLS 1.2 is not
    enforced. That case is reported as `defer` carrying the machine-readable reason
    `missing_custom_acm_certificate`.
    """
    viewer_certificate = distribution_config.get("ViewerCertificate", {})
    configured_minimum = viewer_certificate.get("MinimumProtocolVersion")
    uses_default_certificate = bool(viewer_certificate.get("CloudFrontDefaultCertificate"))

    if uses_default_certificate:
        yield CheckResult(
            resource="distribution",
            check="configured_tls_minimum",
            status=DEFER,
            defer_reason=_INERT_TLS_DEFER_REASON,
            detail=(
                f"NOT VERIFIED and NOT ENFORCED: distribution uses the default cloudfront.net "
                f"certificate, so MinimumProtocolVersion ({configured_minimum!r}) is inert and "
                "TLS 1.2 is not enforced. Unmet prerequisite: a custom ACM certificate "
                "(needs a domain plus DNS validation). Disclosed in README 'Limitations' "
                "and infra/modules/frontend_cdn/README.md 'Viewer TLS'; deferred, not "
                "passed and not skipped"
            ),
        )
    elif configured_minimum in _TLS_1_2_OR_BETTER_PROTOCOL_VERSIONS:
        yield CheckResult(
            resource="distribution",
            check="configured_tls_minimum",
            status="pass",
            detail=f"MinimumProtocolVersion={configured_minimum}, enforced via custom certificate",
        )
    else:
        yield CheckResult(
            resource="distribution",
            check="configured_tls_minimum",
            status="fail",
            detail=f"MinimumProtocolVersion={configured_minimum!r} is below the required TLS 1.2 floor",
        )

    probe_result = tls_probe(domain_name)
    if probe_result is True:
        yield CheckResult(
            resource="distribution",
            check="sub_1_2_handshake_refused",
            status="pass",
            detail="TLS 1.0/1.1 handshake was refused",
        )
    elif probe_result is False:
        yield CheckResult(
            resource="distribution",
            check="sub_1_2_handshake_refused",
            status="fail",
            detail="TLS 1.0/1.1 handshake unexpectedly succeeded",
        )
    else:
        yield CheckResult(
            resource="distribution",
            check="sub_1_2_handshake_refused",
            status="skip",
            detail=(
                f"could not attempt a sub-1.2 handshake from this host: {probe_result} - "
                "this is a local-stack limitation, not evidence the server refuses it"
            ),
        )


#: Both custom-origin behaviours, each checked against a Terraform output rather than a host
#: composed here. `/agent/*` routes to the relay's Function URL (`agent_relay_function_url`).
_CUSTOM_ORIGIN_PATHS: tuple[tuple[str, str, str], ...] = (
    # (path_pattern, outputs key naming the origin domain, human label)
    ("/api/*", "api_endpoint", "API Gateway"),
    ("/agent/*", "agent_relay_function_url", "relay Function URL"),
)

#: The retired AgentCore data-plane host fragment, asserted absent from every origin.
_RETIRED_AGENTCORE_HOST_FRAGMENT = "bedrock-agentcore."

_CACHING_DISABLED_POLICY_ID = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"  # Managed-CachingDisabled


def _origin_domain_for(
    behavior: dict[str, Any], origins_by_id: dict[str, dict[str, Any]]
) -> str | None:
    """Return the `DomainName` of the origin a behavior targets, or `None` if unresolvable."""
    origin_id = behavior.get("TargetOriginId")
    origin = origins_by_id.get(str(origin_id)) if origin_id is not None else None
    return str(origin["DomainName"]) if origin else None


def _domain_matches(expected_output_value: str, actual_origin_domain: str) -> bool:
    """True if `actual_origin_domain` is the host component of (or equals) the pinned output."""
    host = expected_output_value
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    return host == actual_origin_domain


def _check_default_behavior_routes_to_s3(
    behaviors_by_path: dict[str, dict[str, Any]],
    origins_by_id: dict[str, dict[str, Any]],
    s3_domain_name: str,
) -> Iterable[CheckResult]:
    """Assert the default (`/*`) behavior routes to the S3 origin."""
    resource = "behavior:/*"
    behavior = behaviors_by_path.get("*")
    if behavior is None:
        yield CheckResult(
            resource=resource,
            check="routes_to_correct_origin",
            status="fail",
            detail="no default cache behavior found on the distribution",
        )
        return

    actual_domain = _origin_domain_for(behavior, origins_by_id)
    if actual_domain == s3_domain_name:
        yield CheckResult(
            resource=resource,
            check="routes_to_correct_origin",
            status="pass",
            detail=f"routes to S3 origin {actual_domain}",
        )
    else:
        yield CheckResult(
            resource=resource,
            check="routes_to_correct_origin",
            status="fail",
            detail=f"expected S3 origin {s3_domain_name!r}, got {actual_domain!r}",
        )


def _check_custom_origin_behavior(
    path_pattern: str,
    expected_domain: str,
    label: str,
    behaviors_by_path: dict[str, dict[str, Any]],
    origins_by_id: dict[str, dict[str, Any]],
) -> Iterable[CheckResult]:
    """Assert one custom-origin behavior (`/api/*` or `/agent/*`) routes correctly, disables
    caching, and forwards `Authorization`. Each assertion is its own reported check."""
    resource = f"behavior:{path_pattern}"
    behavior = behaviors_by_path.get(path_pattern)
    if behavior is None:
        yield CheckResult(
            resource=resource,
            check="routes_to_correct_origin",
            status="fail",
            detail=f"no cache behavior found for path pattern {path_pattern!r} ({label})",
        )
        yield CheckResult(
            resource=resource,
            check="caching_disabled",
            status="fail",
            detail=f"cannot assert caching - behavior {path_pattern!r} is missing",
        )
        yield CheckResult(
            resource=resource,
            check="authorization_forwarded",
            status="fail",
            detail=f"cannot assert Authorization forwarding - behavior {path_pattern!r} is missing",
        )
        return

    actual_domain = _origin_domain_for(behavior, origins_by_id)
    if actual_domain is not None and _domain_matches(expected_domain, actual_domain):
        yield CheckResult(
            resource=resource,
            check="routes_to_correct_origin",
            status="pass",
            detail=f"routes to {label} origin {actual_domain}",
        )
    else:
        yield CheckResult(
            resource=resource,
            check="routes_to_correct_origin",
            status="fail",
            detail=f"expected {label} origin derived from {expected_domain!r}, got {actual_domain!r}",
        )

    cache_policy_id = behavior.get("CachePolicyId")
    forwarded_values = behavior.get("ForwardedValues")
    if cache_policy_id is not None:
        caching_disabled = cache_policy_id == _CACHING_DISABLED_POLICY_ID
        yield CheckResult(
            resource=resource,
            check="caching_disabled",
            status="pass" if caching_disabled else "fail",
            detail=f"CachePolicyId={cache_policy_id}",
        )
    elif forwarded_values is not None:
        min_ttl = behavior.get("MinTTL", 0)
        max_ttl = behavior.get("MaxTTL", 0)
        default_ttl = behavior.get("DefaultTTL", 0)
        caching_disabled = min_ttl == 0 and max_ttl == 0 and default_ttl == 0
        yield CheckResult(
            resource=resource,
            check="caching_disabled",
            status="pass" if caching_disabled else "fail",
            detail=f"legacy ForwardedValues config: MinTTL={min_ttl} MaxTTL={max_ttl} DefaultTTL={default_ttl}",
        )
    else:
        yield CheckResult(
            resource=resource,
            check="caching_disabled",
            status="fail",
            detail="behavior has neither CachePolicyId nor ForwardedValues configured",
        )

    origin_request_policy_id = behavior.get("OriginRequestPolicyId")
    forwarded_headers = (
        forwarded_values.get("Headers", {}).get("Items", []) if forwarded_values else []
    )
    if origin_request_policy_id is not None:
        # Managed-AllViewerExceptHostHeader forwards every viewer header except Host,
        # including Authorization - a policy id present here is sufficient evidence.
        yield CheckResult(
            resource=resource,
            check="authorization_forwarded",
            status="pass",
            detail=f"OriginRequestPolicyId={origin_request_policy_id}",
        )
    elif "Authorization" in forwarded_headers or "*" in forwarded_headers:
        yield CheckResult(
            resource=resource,
            check="authorization_forwarded",
            status="pass",
            detail=f"legacy ForwardedValues.Headers includes Authorization: {forwarded_headers}",
        )
    else:
        yield CheckResult(
            resource=resource,
            check="authorization_forwarded",
            status="fail",
            detail=(
                "behavior has no OriginRequestPolicyId and does not forward the Authorization "
                f"header via legacy ForwardedValues.Headers: {forwarded_headers}"
            ),
        )


#: Deep link with no corresponding S3 key: `frontend/src/App.tsx` routes it client-side, so
#: only the viewer-request rewrite can make it reach the SPA.
_SPA_DEEP_LINK_PATH = "/accounts/demo-acct-0001"

#: A path whose last segment contains a `.`, which the rewrite must leave alone.
_MISSING_ASSET_PATH = "/assets/definitely-not-a-real-bundle.js"

#: An `/api/*` route reached with no `Authorization` header. It names a deployed route, called with
#: that route's method; `get_account` is read-only.
_UNAUTHENTICATED_API_PATH = "/api/get_account"

#: Method for the probe above; every CRM tool route is `POST`.
_UNAUTHENTICATED_API_METHOD = "POST"

#: Marker proving the response body is the built SPA document rather than CloudFront's own
#: error page. Present in `frontend/index.html` and preserved verbatim by the Vite build.
_SPA_BODY_MARKER = '<div id="root"'


def _check_agent_behavior_is_the_relay(
    behaviors_by_path: dict[str, dict[str, Any]],
    origins_by_id: dict[str, dict[str, Any]],
) -> Iterable[CheckResult]:
    """Assert three `/agent/*` properties of the deployed distribution.

    * No origin anywhere carries the retired AgentCore data-plane host.
    * No viewer-request function on this behaviour.
    * Compression off, since CloudFront compression buffers SSE frames until the turn ends.
    """
    resource = "behavior:/agent/*"

    retired = sorted(
        str(origin.get("DomainName", ""))
        for origin in origins_by_id.values()
        if _RETIRED_AGENTCORE_HOST_FRAGMENT in str(origin.get("DomainName", ""))
    )
    yield CheckResult(
        resource="distribution:origins",
        check="retired_agentcore_origin_absent",
        status="pass" if not retired else "fail",
        detail=(
            "no origin carries the retired AgentCore data-plane host"
            if not retired
            else f"the v1 AgentCore origin(s) survived the apply: {retired}"
        ),
    )

    behavior = behaviors_by_path.get("/agent/*")
    if behavior is None:
        for check in ("path_rewrite_function_absent", "compression_disabled"):
            yield CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail="cannot assert - behavior '/agent/*' is missing from the distribution",
            )
        return

    associations = behavior.get("FunctionAssociations") or {}
    association_count = int(associations.get("Quantity", 0) or 0)
    yield CheckResult(
        resource=resource,
        check="path_rewrite_function_absent",
        status="pass" if association_count == 0 else "fail",
        detail=(
            "no viewer-request function is associated"
            if association_count == 0
            else f"{association_count} function association(s) survive on this behaviour: "
            f"{associations.get('Items')}"
        ),
    )

    compress = behavior.get("Compress")
    yield CheckResult(
        resource=resource,
        check="compression_disabled",
        status="pass" if compress is False else "fail",
        detail=(
            "Compress=False, so CloudFront does not buffer the stream to gzip it"
            if compress is False
            else f"Compress={compress!r} - gzip buffering defeats incremental SSE delivery"
        ),
    )


def _check_spa_history_fallback(domain_name: str, http_caller: HttpCaller) -> Iterable[CheckResult]:
    """Assert the viewer-request rewrite serves the SPA for a client-side route, and that it
    does not do so indiscriminately.

    Two separately reported checks; `/api/*` negative auth is asserted by
    `_check_api_negative_auth_preserved`:

    - `deep_link_serves_the_spa` - a client-side route returns `200` and the body is the SPA
      document.
    - `missing_asset_is_not_rewritten` - a nonexistent `*.js` still gets the origin's `403`.

    A `403` on the deep link is reported as the likely "nothing was published to the origin bucket"
    case.
    """
    deep_link_url = f"https://{domain_name}{_SPA_DEEP_LINK_PATH}"
    resource = "behavior:/*"
    result = http_caller(deep_link_url)

    if isinstance(result, str):
        yield CheckResult(
            resource=resource, check="deep_link_serves_the_spa", status="fail", detail=result
        )
    else:
        status_code, body = result
        if status_code == 200 and _SPA_BODY_MARKER in body:
            yield CheckResult(
                resource=resource,
                check="deep_link_serves_the_spa",
                status="pass",
                detail=f"GET {_SPA_DEEP_LINK_PATH} returned 200 carrying the SPA document",
            )
        elif status_code == 200:
            yield CheckResult(
                resource=resource,
                check="deep_link_serves_the_spa",
                status="fail",
                detail=(
                    f"GET {_SPA_DEEP_LINK_PATH} returned 200 but the body does not contain "
                    f"{_SPA_BODY_MARKER!r} - this is not the built SPA document"
                ),
            )
        elif status_code == 403:
            yield CheckResult(
                resource=resource,
                check="deep_link_serves_the_spa",
                status="fail",
                detail=(
                    f"GET {_SPA_DEEP_LINK_PATH} returned 403: the rewrite reached /index.html "
                    "but the origin bucket has no such key - most likely nothing published the "
                    "built site (see scripts/deploy.sh's frontend publish step)"
                ),
            )
        else:
            yield CheckResult(
                resource=resource,
                check="deep_link_serves_the_spa",
                status="fail",
                detail=f"GET {_SPA_DEEP_LINK_PATH} returned {status_code}, expected 200",
            )

    asset_result = http_caller(f"https://{domain_name}{_MISSING_ASSET_PATH}")
    if isinstance(asset_result, str):
        yield CheckResult(
            resource=resource,
            check="missing_asset_is_not_rewritten",
            status="fail",
            detail=asset_result,
        )
    else:
        asset_status, asset_body = asset_result
        if asset_status == 200 or _SPA_BODY_MARKER in asset_body:
            yield CheckResult(
                resource=resource,
                check="missing_asset_is_not_rewritten",
                status="fail",
                detail=(
                    f"GET {_MISSING_ASSET_PATH} returned {asset_status} carrying the SPA "
                    "document: the rewrite is not guarding asset paths, so a missing script "
                    "silently returns HTML"
                ),
            )
        else:
            yield CheckResult(
                resource=resource,
                check="missing_asset_is_not_rewritten",
                status="pass",
                detail=f"GET {_MISSING_ASSET_PATH} returned {asset_status}, not the SPA document",
            )


def _check_api_negative_auth_preserved(
    domain_name: str, method_caller: HttpMethodCaller
) -> Iterable[CheckResult]:
    """Assert an unauthenticated `/api/*` request through CloudFront is still refused.

    Passes only on 401 or 403 with a body that is not the SPA document; the probe reaches a real
    route with that route's own method (`_UNAUTHENTICATED_API_PATH`, `_UNAUTHENTICATED_API_METHOD`).
    """
    url = f"https://{domain_name}{_UNAUTHENTICATED_API_PATH}"
    resource = "behavior:/api/*"
    check = "unauthenticated_api_still_refused"
    result = method_caller(url, _UNAUTHENTICATED_API_METHOD)

    if isinstance(result, str):
        yield CheckResult(resource=resource, check=check, status="fail", detail=result)
        return

    status_code, body = result
    if _SPA_BODY_MARKER in body:
        yield CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"{_UNAUTHENTICATED_API_METHOD} {_UNAUTHENTICATED_API_PATH} with no Authorization "
                f"header returned {status_code} carrying the SPA document: an API rejection "
                "has been rewritten into the frontend page"
            ),
        )
    elif status_code in (401, 403):
        yield CheckResult(
            resource=resource,
            check=check,
            status="pass",
            detail=(
                f"{_UNAUTHENTICATED_API_METHOD} {_UNAUTHENTICATED_API_PATH} with no Authorization "
                f"header returned {status_code}, and the body is not the SPA document"
            ),
        )
    else:
        yield CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"{_UNAUTHENTICATED_API_METHOD} {_UNAUTHENTICATED_API_PATH} with no Authorization "
                f"header returned {status_code}, expected 401 or 403"
            ),
        )


#: The fixed directives of the served CSP: every directive except `script-src` and `connect-src`,
#: which carry deployment-derived sources after `'self'`.
_CSP_FIXED_DIRECTIVES: dict[str, tuple[str, ...]] = {
    "default-src": ("'self'",),
    "style-src": ("'self'", "'unsafe-inline'"),
    "img-src": ("'self'", "data:"),
    "font-src": ("'self'", "data:"),
    "frame-ancestors": ("'none'",),
    "base-uri": ("'none'",),
    "object-src": ("'none'",),
    "form-action": ("'self'",),
}

#: Every directive the served CSP must carry, and no other.
_CSP_DIRECTIVES: frozenset[str] = frozenset({*_CSP_FIXED_DIRECTIVES, "script-src", "connect-src"})

#: A `script-src` hash source.
_SCRIPT_HASH_SOURCE = re.compile(r"^'sha256-[A-Za-z0-9+/]{43}='$")

#: An inline `<script>` element of any case and attributes, excluding one with a `src` attribute.
_INLINE_SCRIPT = re.compile(
    r"<script\b(?![^>]*\ssrc\s*=)[^>]*>(.*?)</script\b[^>]*>", re.DOTALL | re.IGNORECASE
)

#: An unauthenticated `/agent/*` probe. The relay refuses it before routing, so any path works.
_UNAUTHENTICATED_AGENT_PATH = "/agent/csp-probe"

#: `(resource, path, method, unauthenticated)` for each served-header probe.
_CSP_PROBES: tuple[tuple[str, str, str, bool], ...] = (
    ("behavior:/*", "/", "GET", False),
    ("behavior:/api/*", _UNAUTHENTICATED_API_PATH, _UNAUTHENTICATED_API_METHOD, True),
    ("behavior:/agent/*", _UNAUTHENTICATED_AGENT_PATH, "GET", True),
)

_CSP_HEADER = "content-security-policy"
_CSP_REPORT_ONLY_HEADER = "content-security-policy-report-only"
_HSTS_HEADER = "strict-transport-security"
_HSTS_MAX_AGE_SEC = 31536000


def _parse_csp(policy: str) -> tuple[dict[str, list[str]], list[str]]:
    """Return `{directive: [sources]}` for `policy` and the directives it repeats.

    A repeated directive keeps its first occurrence, as browsers do.
    """
    parsed: dict[str, list[str]] = {}
    repeated: list[str] = []
    for part in policy.split(";"):
        tokens = part.split()
        if not tokens:
            continue
        name = tokens[0].lower()
        if name in parsed:
            repeated.append(name)
            continue
        parsed[name] = tokens[1:]
    return parsed, repeated


def _csp_hash(script_body: str) -> str:
    digest = hashlib.sha256(script_body.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def _csp_design_problems(policy: str) -> list[str]:
    """Every way `policy` departs from the designed directive set; empty when it matches."""
    parsed, repeated = _parse_csp(policy)
    problems: list[str] = [f"directive {name!r} appears more than once" for name in repeated]
    missing = sorted(_CSP_DIRECTIVES - parsed.keys())
    extra = sorted(parsed.keys() - _CSP_DIRECTIVES)
    if missing:
        problems.append(f"missing directives {missing}")
    if extra:
        problems.append(f"unexpected directives {extra}")
    for name, expected in _CSP_FIXED_DIRECTIVES.items():
        if name in parsed and tuple(parsed[name]) != expected:
            problems.append(f"{name} is {parsed[name]}, expected {list(expected)}")
    script_src = parsed.get("script-src", [])
    if script_src[:1] != ["'self'"] or not all(
        _SCRIPT_HASH_SOURCE.match(source) for source in script_src[1:]
    ):
        problems.append(f"script-src must be 'self' then sha256 hash sources only: {script_src}")
    connect_src = parsed.get("connect-src", [])
    if connect_src[:1] != ["'self'"] or not all(
        source.startswith("https://") and "*" not in source for source in connect_src[1:]
    ):
        problems.append(f"connect-src must be 'self' then https origins only: {connect_src}")
    for name, sources in parsed.items():
        if "'unsafe-eval'" in sources:
            problems.append(f"{name} allows 'unsafe-eval'")
        if "'unsafe-inline'" in sources and name != "style-src":
            problems.append(f"{name} allows 'unsafe-inline'")
    return problems


def _hsts_problems(value: str | None) -> list[str]:
    """Return why a served `Strict-Transport-Security` value is not the designed one."""
    if not value:
        return ["no Strict-Transport-Security header"]
    directives = {}
    for part in value.split(";"):
        name, _, argument = part.strip().partition("=")
        if name:
            directives[name.strip().lower()] = argument.strip().strip('"')
    problems = []
    if directives.get("max-age") != str(_HSTS_MAX_AGE_SEC):
        problems.append(f"max-age is {directives.get('max-age')!r}, not {_HSTS_MAX_AGE_SEC}")
    for name in ("includesubdomains", "preload"):
        if name not in directives:
            problems.append(f"{name} is absent")
    return problems


def _check_served_hsts(resource: str, path: str, method: str, value: str | None) -> CheckResult:
    """One `hsts_header_served` row for the response a served-header probe received."""
    problems = _hsts_problems(value)
    return CheckResult(
        resource=resource,
        check="hsts_header_served",
        status="fail" if problems else "pass",
        detail=f"{method} {path}: " + "; ".join(problems)
        if problems
        else f"{method} {path} serves Strict-Transport-Security: {value}",
    )


def _check_response_headers_policy(
    distribution_config: dict[str, Any], cloudfront: Any
) -> tuple[list[CheckResult], str | None]:
    """Assert one custom response-headers policy on every behaviour, enforcing the CSP.

    Returns the results and the policy's CSP value, or `None` when it could not be read.
    """
    resource = "distribution"
    behaviours = [("default", distribution_config.get("DefaultCacheBehavior", {}))]
    for behaviour in distribution_config.get("CacheBehaviors", {}).get("Items", []) or []:
        behaviours.append((str(behaviour.get("PathPattern")), behaviour))
    ids = {label: behaviour.get("ResponseHeadersPolicyId") for label, behaviour in behaviours}
    distinct = set(ids.values())
    results: list[CheckResult] = []
    if None in distinct or "" in distinct or len(distinct) != 1:
        results.append(
            CheckResult(
                resource=resource,
                check="one_response_headers_policy_on_every_behaviour",
                status="fail",
                detail=f"behaviours do not share one ResponseHeadersPolicyId: {ids}",
            )
        )
        results.append(
            CheckResult(
                resource=resource,
                check="response_headers_policy_enforces_csp",
                status="fail",
                detail="no single policy to inspect; see one_response_headers_policy_on_every_behaviour",
            )
        )
        return results, None
    (policy_id,) = distinct
    results.append(
        CheckResult(
            resource=resource,
            check="one_response_headers_policy_on_every_behaviour",
            status="pass",
            detail=f"every behaviour ({', '.join(ids)}) attaches ResponseHeadersPolicyId {policy_id}",
        )
    )

    custom_ids = {
        str(item.get("ResponseHeadersPolicy", {}).get("Id"))
        for item in cloudfront.list_response_headers_policies(Type="custom")
        .get("ResponseHeadersPolicyList", {})
        .get("Items", [])
        or []
    }
    config = (
        cloudfront.get_response_headers_policy(Id=policy_id)
        .get("ResponseHeadersPolicy", {})
        .get("ResponseHeadersPolicyConfig", {})
    )
    security = config.get("SecurityHeadersConfig", {}) or {}
    csp_block = security.get("ContentSecurityPolicy", {}) or {}
    csp_value = csp_block.get("ContentSecurityPolicy")
    frame = (security.get("FrameOptions", {}) or {}).get("FrameOption")
    problems = []
    if policy_id not in custom_ids:
        problems.append(
            f"policy {policy_id} is not a custom policy (an AWS-managed one has no CSP)"
        )
    if not isinstance(csp_value, str) or not csp_value.strip():
        problems.append("the policy carries no Content-Security-Policy")
    if csp_block.get("Override") is not True:
        problems.append(
            f"ContentSecurityPolicy.Override is {csp_block.get('Override')!r}, not True"
        )
    if frame != "DENY":
        problems.append(f"FrameOptions.FrameOption is {frame!r}, not 'DENY'")
    results.append(
        CheckResult(
            resource=resource,
            check="response_headers_policy_enforces_csp",
            status="fail" if problems else "pass",
            detail="; ".join(problems)
            if problems
            else f"custom policy {policy_id}: CSP with Override true, X-Frame-Options DENY",
        )
    )
    return results, csp_value if isinstance(csp_value, str) and csp_value.strip() else None


def _check_served_csp(
    domain_name: str,
    *,
    region: str,
    cognito_domain: str,
    policy_csp: str | None,
    header_caller: HeaderCaller,
) -> Iterable[CheckResult]:
    """Assert the served CSP on `/`, an unauthenticated `/api/*` and an unauthenticated `/agent/*`.

    One `csp_header_enforced` and one `hsts_header_served` row per behaviour (the latter requiring
    max-age=31536000, includeSubDomains and preload), then rows over the `/` response's header: equal on
    every behaviour and to the policy, the designed directive set, Cognito in `connect-src`, and a
    hash source for every inline script in the served document.
    """
    served: dict[str, str] = {}
    root_body: str | None = None
    for resource, path, method, unauthenticated in _CSP_PROBES:
        check = "csp_header_enforced"
        result = header_caller(f"https://{domain_name}{path}", method)
        if isinstance(result, str):
            yield CheckResult(resource=resource, check=check, status="fail", detail=result)
            yield CheckResult(
                resource=resource, check="hsts_header_served", status="fail", detail=result
            )
            continue
        status_code, headers, body = result
        yield _check_served_hsts(resource, path, method, headers.get(_HSTS_HEADER))
        policy = headers.get(_CSP_HEADER)
        if unauthenticated and status_code < 400:
            yield CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail=(
                    f"unauthenticated {method} {path} returned {status_code}; the probe must read "
                    "the header from a refusal"
                ),
            )
        elif not policy:
            report_only = (
                " (only Content-Security-Policy-Report-Only)"
                if headers.get(_CSP_REPORT_ONLY_HEADER)
                else ""
            )
            yield CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail=f"{method} {path} returned {status_code} with no enforced CSP{report_only}",
            )
        else:
            served[resource] = policy
            if path == "/":
                root_body = body
            yield CheckResult(
                resource=resource,
                check=check,
                status="pass",
                detail=f"{method} {path} returned {status_code} carrying Content-Security-Policy",
            )

    resource = "distribution"
    root_policy = served.get("behavior:/*")
    if root_policy is None:
        for check in (
            "served_csp_identical_and_equal_to_policy",
            "served_csp_matches_design",
            "served_csp_connect_src_allows_cognito",
            "served_csp_allows_every_inline_script",
        ):
            yield CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail="no CSP was served on / to inspect; see behavior:/* csp_header_enforced",
            )
        return

    differing = sorted(name for name, value in served.items() if value != root_policy)
    mismatch = []
    if differing:
        mismatch.append(f"behaviours serving a different CSP from /: {differing}")
    if policy_csp is not None and root_policy != policy_csp:
        mismatch.append("the served CSP differs from the response-headers policy's value")
    compared = (
        "and to the policy's value"
        if policy_csp is not None
        else ("(the policy's value was unreadable, so it was not compared)")
    )
    yield CheckResult(
        resource=resource,
        check="served_csp_identical_and_equal_to_policy",
        status="fail" if mismatch else "pass",
        detail="; ".join(mismatch)
        if mismatch
        else f"every behaviour that served a CSP serves the same one, equal to / {compared}",
    )

    problems = _csp_design_problems(root_policy)
    yield CheckResult(
        resource=resource,
        check="served_csp_matches_design",
        status="fail" if problems else "pass",
        detail="; ".join(problems) if problems else f"served CSP matches the design: {root_policy}",
    )

    parsed, _ = _parse_csp(root_policy)
    connect_src = parsed.get("connect-src", [])
    wanted = [
        f"https://cognito-idp.{region}.amazonaws.com",
        f"https://{cognito_domain}.auth.{region}.amazoncognito.com",
    ]
    absent = [origin for origin in wanted if origin not in connect_src]
    yield CheckResult(
        resource=resource,
        check="served_csp_connect_src_allows_cognito",
        status="fail" if absent else "pass",
        detail=f"connect-src lacks {absent}: {connect_src}"
        if absent
        else f"connect-src carries {wanted}",
    )

    script_src = parsed.get("script-src", [])
    scripts = _INLINE_SCRIPT.findall(root_body or "")
    hashes = [_csp_hash(body) for body in scripts]
    not_allowed = [h for h in hashes if h not in script_src]
    status: Status
    if not scripts:
        status, detail = (
            "fail",
            "the served document carries no inline <script>, so the hash check verified nothing",
        )
    elif not_allowed:
        status, detail = (
            "fail",
            f"served inline scripts hash to {not_allowed}, absent from script-src {script_src}: "
            "browsers refuse them",
        )
    else:
        status, detail = "pass", f"every served inline script's hash is in script-src: {hashes}"
    yield CheckResult(
        resource=resource,
        check="served_csp_allows_every_inline_script",
        status=status,
        detail=detail,
    )


def checks(
    ctx: SmokeContext,
    *,
    http_caller: HttpCaller | None = None,
    http_method_caller: HttpMethodCaller | None = None,
    tls_probe: TlsProbe | None = None,
    http_header_caller: HeaderCaller | None = None,
) -> Iterable[CheckResult]:
    """Yield one `CheckResult` per (resource, check) pair for the CloudFront distribution.

    Never raises for an individual check failure; every behaviour/origin is still checked
    even when an earlier one already failed.
    """
    caller = http_caller if http_caller is not None else default_http_caller
    method_caller = (
        http_method_caller if http_method_caller is not None else default_http_method_caller
    )
    probe = tls_probe if tls_probe is not None else default_tls_probe
    header_caller = http_header_caller if http_header_caller is not None else default_header_caller

    domain_name = ctx.outputs.require_str("cloudfront_domain_name")
    cloudfront = ctx.client("cloudfront")

    distributions_page = cloudfront.list_distributions()
    distribution_list = distributions_page.get("DistributionList", {}).get("Items", []) or []
    distribution_summary = next(
        (item for item in distribution_list if item.get("DomainName") == domain_name), None
    )
    if distribution_summary is None:
        yield CheckResult(
            resource="distribution",
            check="resolve_distribution_id",
            status="fail",
            detail=f"no distribution found with DomainName == {domain_name!r}",
        )
        return

    distribution_id = str(distribution_summary["Id"])
    config_response = cloudfront.get_distribution_config(Id=distribution_id)
    distribution_config = config_response.get("DistributionConfig", {})

    origins_by_id = _origin_by_id(distribution_config)
    selection = _select_s3_origin(origins_by_id)
    if isinstance(selection, str):
        yield CheckResult(
            resource="origin:s3",
            check="oac_only_origin_access",
            status="fail",
            detail=selection,
        )
        return
    s3_origin_id, s3_origin = selection
    s3_domain_name = str(s3_origin["DomainName"])

    yield from _check_oac_only_origin(s3_origin_id, s3_origin)
    yield from _check_direct_origin_request_refused(s3_domain_name, caller)
    yield from _check_viewer_tls(distribution_config, domain_name, probe)

    behaviors_by_path = _behavior_by_path(distribution_config)
    yield from _check_default_behavior_routes_to_s3(
        behaviors_by_path, origins_by_id, s3_domain_name
    )
    for path_pattern, output_key, label in _CUSTOM_ORIGIN_PATHS:
        yield from _check_custom_origin_behavior(
            path_pattern,
            ctx.outputs.require_str(output_key),
            label,
            behaviors_by_path,
            origins_by_id,
        )
    yield from _check_agent_behavior_is_the_relay(behaviors_by_path, origins_by_id)

    yield from _check_spa_history_fallback(domain_name, caller)
    yield from _check_api_negative_auth_preserved(domain_name, method_caller)

    policy_results, policy_csp = _check_response_headers_policy(distribution_config, cloudfront)
    yield from policy_results
    yield from _check_served_csp(
        domain_name,
        region=ctx.region,
        cognito_domain=ctx.outputs.require_str("cognito_domain"),
        policy_csp=policy_csp,
        header_caller=header_caller,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the CloudFront/origin-privacy checks and return the exit code."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
