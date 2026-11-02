# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.cloudfront`: the Layer 4 CloudFront/origin-privacy smoke script.

Exercises `checks()` against a fake `cloudfront` client and injected HTTP/TLS-probe seams, with no
credentials and no network: origin access, direct-origin refusal, the TLS minimum, each cache
behaviour, the SPA history fallback, and what must be absent from the distribution.
"""

from __future__ import annotations

from typing import Any

import pytest
from scripts.smoke.cloudfront import (
    _UNAUTHENTICATED_API_METHOD,
    _UNAUTHENTICATED_API_PATH,
    HttpCaller,
    HttpMethodCaller,
    HttpResult,
    TlsProbeResult,
    checks,
)
from scripts.smoke.harness import CheckResult, ResourceClassResult, context_for_test

DOMAIN_NAME = "d111111abcdef8.cloudfront.net"
DISTRIBUTION_ID = "E1ABCDEF23456"
S3_DOMAIN_NAME = "frontend-cdn-origin-123456789012.s3.us-east-1.amazonaws.com"
#: The `/api/*` origin's host and the `api_endpoint` output derived from it: a PRIVATE REST API's
#: endpoint-associated hostname (`<api-id>-<vpce-id>.execute-api.<region>.amazonaws.com`). The
#: `vpce` segment is synthetic.
API_ORIGIN_HOST = "abc123-vpce0123456789abcdef.execute-api.us-east-1.amazonaws.com"
API_ENDPOINT = f"https://{API_ORIGIN_HOST}"
AGENTCORE_URL = "https://bedrock-agentcore.us-east-1.amazonaws.com"

#: The relay's Function URL as `agent_relay_function_url` reports it, scheme and trailing slash
#: included; `_domain_matches` reduces it to a host.
RELAY_FUNCTION_URL = "https://abcdefghij1234567890.lambda-url.us-east-1.on.aws/"
RELAY_FUNCTION_HOST = "abcdefghij1234567890.lambda-url.us-east-1.on.aws"
OAC_ID = "E2QWERTYUIOP"

CACHING_DISABLED_POLICY_ID = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
CACHING_OPTIMIZED_POLICY_ID = "658327ea-f89d-4fab-a63d-7e88639e58f6"
ALL_VIEWER_EXCEPT_HOST_POLICY_ID = "b689b0a8-53d0-40ab-baf2-68738e2966ac"


def _s3_origin(
    *, oac: bool = True, legacy_oai: str = "", none_at_all: bool = False
) -> dict[str, Any]:
    origin: dict[str, Any] = {"Id": "s3-frontend-origin", "DomainName": S3_DOMAIN_NAME}
    if none_at_all:
        return origin
    if oac:
        origin["OriginAccessControlId"] = OAC_ID
    origin["S3OriginConfig"] = {"OriginAccessIdentity": legacy_oai}
    return origin


def _custom_origin(origin_id: str, domain_name: str) -> dict[str, Any]:
    return {
        "Id": origin_id,
        "DomainName": domain_name,
        "CustomOriginConfig": {
            "HTTPPort": 80,
            "HTTPSPort": 443,
            "OriginProtocolPolicy": "https-only",
        },
    }


def _api_origin() -> dict[str, Any]:
    """The `/api/*` origin as deployed: a VPC origin, with no `CustomOriginConfig`."""
    return {
        "Id": "api-gateway-origin",
        "DomainName": API_ORIGIN_HOST,
        "VpcOriginConfig": {"VpcOriginId": "vo-0123456789abcdef0"},
    }


def _api_custom_origin() -> dict[str, Any]:
    """The `/api/*` origin in its custom-origin shape."""
    return _custom_origin("api-gateway-origin", API_ORIGIN_HOST)


def _relay_origin() -> dict[str, Any]:
    origin = _custom_origin("agent-relay-origin", RELAY_FUNCTION_HOST)
    origin["OriginAccessControlId"] = OAC_ID
    return origin


def _retired_agentcore_origin() -> dict[str, Any]:
    """The retired `/agent/*` AgentCore origin, for tests that detect a surviving one."""
    return _custom_origin("agentcore-gateway-origin", "bedrock-agentcore.us-east-1.amazonaws.com")


def _default_behavior(target_origin_id: str = "s3-frontend-origin") -> dict[str, Any]:
    return {
        "TargetOriginId": target_origin_id,
        "CachePolicyId": CACHING_OPTIMIZED_POLICY_ID,
    }


def _custom_behavior(
    path_pattern: str,
    target_origin_id: str,
    *,
    cache_policy_id: str = CACHING_DISABLED_POLICY_ID,
    origin_request_policy_id: str | None = ALL_VIEWER_EXCEPT_HOST_POLICY_ID,
    compress: bool | None = None,
    function_associations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    behavior: dict[str, Any] = {
        "PathPattern": path_pattern,
        "TargetOriginId": target_origin_id,
        "CachePolicyId": cache_policy_id,
    }
    if origin_request_policy_id is not None:
        behavior["OriginRequestPolicyId"] = origin_request_policy_id
    if compress is not None:
        behavior["Compress"] = compress
    if function_associations is not None:
        behavior["FunctionAssociations"] = function_associations
    return behavior


def _distribution_config(
    *,
    s3_origin: dict[str, Any] | None = None,
    default_behavior: dict[str, Any] | None = None,
    api_behavior: dict[str, Any] | None = None,
    agent_behavior: dict[str, Any] | None = None,
    uses_default_certificate: bool = True,
    minimum_protocol_version: str | None = None,
    include_api_behavior: bool = True,
    include_agent_behavior: bool = True,
    extra_origins: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    origins = [
        s3_origin if s3_origin is not None else _s3_origin(),
        _api_origin(),
        _relay_origin(),
    ]
    if extra_origins:
        origins.extend(extra_origins)
    behaviors = []
    if include_api_behavior:
        behaviors.append(
            api_behavior
            if api_behavior is not None
            else _custom_behavior("/api/*", "api-gateway-origin")
        )
    if include_agent_behavior:
        behaviors.append(
            agent_behavior
            if agent_behavior is not None
            else _custom_behavior("/agent/*", "agent-relay-origin", compress=False)
        )

    viewer_certificate: dict[str, Any] = {"CloudFrontDefaultCertificate": uses_default_certificate}
    if minimum_protocol_version is not None:
        viewer_certificate["MinimumProtocolVersion"] = minimum_protocol_version
    if not uses_default_certificate:
        viewer_certificate["CloudFrontDefaultCertificate"] = False

    return {
        "Origins": {"Items": origins},
        "DefaultCacheBehavior": default_behavior
        if default_behavior is not None
        else _default_behavior(),
        "CacheBehaviors": {"Items": behaviors},
        "ViewerCertificate": viewer_certificate,
    }


class FakeCloudFrontClient:
    """Minimal stand-in for a boto3 `cloudfront` client, keyed to what `checks()` calls."""

    def __init__(
        self, *, distribution_config: dict[str, Any], domain_name: str = DOMAIN_NAME
    ) -> None:
        self._distribution_config = distribution_config
        self._domain_name = domain_name

    def list_distributions(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "DistributionList": {
                "Items": [{"Id": DISTRIBUTION_ID, "DomainName": self._domain_name}]
            }
        }

    def get_distribution_config(self, **_kwargs: Any) -> dict[str, Any]:
        return {"DistributionConfig": self._distribution_config, "ETag": "etag-1"}


def _outputs() -> dict[str, str]:
    return {
        "cloudfront_domain_name": DOMAIN_NAME,
        "api_endpoint": API_ENDPOINT,
        "agentcore_gateway_url": AGENTCORE_URL,
        "agent_relay_function_url": RELAY_FUNCTION_URL,
    }


def _one(results: list[CheckResult], resource: str, check: str) -> CheckResult:
    """Return the single result for `(resource, check)`, asserting there is exactly one."""
    matches = [r for r in results if r.resource == resource and r.check == check]
    assert len(matches) == 1, (
        f"expected exactly one {check!r} result for {resource!r}, got {len(matches)}: "
        f"{[(r.resource, r.check, r.status) for r in results]}"
    )
    return matches[0]


def _as_method_caller(caller: HttpCaller) -> HttpMethodCaller:
    """Adapt a url-only fake into an `HttpMethodCaller` that asserts the method is `POST`."""

    def method_caller(url: str, method: str) -> HttpResult:
        assert method == "POST", f"negative-auth probe used {method}, but CRM routes are POST-only"
        return caller(url)

    return method_caller


def _make_ctx(distribution_config: dict[str, Any]) -> Any:
    return context_for_test(
        outputs=_outputs(),
        clients={"cloudfront": FakeCloudFrontClient(distribution_config=distribution_config)},
    )


#: The built SPA document: the marker `frontend/index.html` carries through the Vite build.
SPA_DOCUMENT = '<!doctype html><html><body><div id="root"></div></body></html>'


def _healthy_caller(url: str) -> HttpResult:
    """Route by URL to the responses a correctly configured distribution gives."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.netloc == S3_DOMAIN_NAME:
        return 403, ""  # OAC-only origin refuses a direct request
    if parts.path.startswith("/api/"):
        return 401, '{"message":"Unauthorized"}'
    last_segment = parts.path.rsplit("/", 1)[-1]
    if "." in last_segment:
        return 403, ""  # a missing asset key, left alone by the viewer-request rewrite
    return 200, SPA_DOCUMENT  # a client-side route, rewritten to /index.html


def _refused_probe(_domain_name: str) -> TlsProbeResult:
    return True


def test_oac_only_origin_passes_with_oac_configured() -> None:
    ctx = _make_ctx(_distribution_config())

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [r for r in results if r.check == "oac_only_origin_access"]
    assert len(findings) == 1
    assert findings[0].status == "pass"
    assert OAC_ID in findings[0].detail


def test_legacy_oai_origin_is_detected() -> None:
    """A distribution still using the legacy OAI fails the OAC-only check."""
    config = _distribution_config(
        s3_origin=_s3_origin(oac=False, legacy_oai="origin-access-identity/cloudfront/E1XXXXX")
    )
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [r for r in results if r.check == "oac_only_origin_access"]
    assert len(findings) == 1
    assert findings[0].status == "fail"
    # The public-origin verdict also contains "legacy", so the OAI's own value is asserted.
    assert "origin-access-identity/cloudfront/E1XXXXX" in findings[0].detail


def test_public_origin_with_no_oac_and_no_oai_is_detected() -> None:
    """A publicly readable origin (no OAC, no OAI) fails the OAC-only check."""
    config = _distribution_config(s3_origin=_s3_origin(none_at_all=True))
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [r for r in results if r.check == "oac_only_origin_access"]
    assert len(findings) == 1
    assert findings[0].status == "fail"
    assert "publicly readable" in findings[0].detail.lower()


# ---------------------------------------------------------------------------
# Which origin the S3 checks run against. `/api/*` is a VPC origin and also carries no
# `CustomOriginConfig`, so the S3 origin must be selected uniquely, not by first match.
# ---------------------------------------------------------------------------


def _recording_caller(inner: HttpCaller) -> tuple[HttpCaller, list[str]]:
    """Wrap `inner`, returning the wrapper and the list of URLs it was called with."""
    seen: list[str] = []

    def caller(url: str) -> HttpResult:
        seen.append(url)
        return inner(url)

    return caller, seen


def _config_with_origins_ordered(
    vpc_api_origin_first: bool, s3_origin: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the standard config with the `Origins.Items` order pinned explicitly.

    `get_distribution_config` documents no order for `Origins.Items`, so tests exercise both orders.
    """
    config = _distribution_config(s3_origin=s3_origin)
    items = config["Origins"]["Items"]
    s3 = next(item for item in items if item["Id"] == "s3-frontend-origin")
    others = [item for item in items if item["Id"] != "s3-frontend-origin"]
    config["Origins"]["Items"] = [*others, s3] if vpc_api_origin_first else [s3, *others]
    return config


@pytest.mark.parametrize("vpc_api_origin_first", [True, False])
def test_the_s3_checks_run_against_the_s3_origin_not_the_vpc_api_origin(
    vpc_api_origin_first: bool,
) -> None:
    """The direct-origin probe addresses the S3 bucket host in either origin order."""
    caller, seen = _recording_caller(_healthy_caller)
    ctx = _make_ctx(_config_with_origins_ordered(vpc_api_origin_first))

    results = list(
        checks(
            ctx,
            http_caller=caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    probed_hosts = {url.split("/")[2] for url in seen if url.startswith("https://")}
    assert S3_DOMAIN_NAME in probed_hosts, (
        f"the direct-origin probe never addressed the S3 origin; probed {sorted(probed_hosts)}"
    )
    assert API_ORIGIN_HOST not in probed_hosts, (
        "the direct-origin probe addressed the private API host, so the S3 origin's public-access "
        f"property was never tested; probed {sorted(probed_hosts)}"
    )

    refused = _one(results, "origin:s3", "direct_origin_request_refused")
    assert refused.status == "pass"
    oac = _one(results, "origin:s3", "oac_only_origin_access")
    assert oac.status == "pass"
    assert "s3-frontend-origin" in oac.detail, (
        f"the OAC verdict does not name the origin it judged: {oac.detail!r}"
    )


def test_a_legacy_oai_origin_is_still_selected_and_detected_alongside_a_vpc_origin() -> None:
    """A legacy-OAI S3 origin is still selected as the S3 origin and fails the OAC check."""
    config = _config_with_origins_ordered(
        vpc_api_origin_first=True,
        s3_origin=_s3_origin(oac=False, legacy_oai="origin-access-identity/cloudfront/E1XXXXX"),
    )
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    oac = _one(results, "origin:s3", "oac_only_origin_access")
    assert oac.status == "fail"
    # The public-origin verdict also contains "legacy", so the OAI's own value is asserted.
    assert "origin-access-identity/cloudfront/E1XXXXX" in oac.detail
    assert "s3-frontend-origin" in oac.detail


def test_a_fully_public_origin_is_still_selected_and_detected_alongside_a_vpc_origin() -> None:
    """A fully public origin (no `S3OriginConfig`, OAC or OAI) is selected as the S3 origin."""
    config = _config_with_origins_ordered(
        vpc_api_origin_first=True, s3_origin=_s3_origin(none_at_all=True)
    )
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    oac = _one(results, "origin:s3", "oac_only_origin_access")
    assert oac.status == "fail"
    assert "publicly readable" in oac.detail.lower()
    assert "s3-frontend-origin" in oac.detail


def test_two_s3_shaped_origins_fail_loudly_instead_of_silently_picking_the_first() -> None:
    """Two S3-shaped origins fail the selection and the detail names both candidates."""
    second = {"Id": "another-bucket-origin", "DomainName": "other.s3.us-east-1.amazonaws.com"}
    config = _distribution_config(extra_origins=[second])
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    oac = _one(results, "origin:s3", "oac_only_origin_access")
    assert oac.status == "fail"
    assert "exactly one" in oac.detail
    # Both candidates are named.
    assert "s3-frontend-origin" in oac.detail
    assert "another-bucket-origin" in oac.detail


def test_no_s3_shaped_origin_at_all_is_reported_with_the_origins_it_saw() -> None:
    """No S3-shaped origin fails the selection and the detail names what was found."""
    config = _distribution_config(s3_origin=_api_custom_origin())
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    oac = _one(results, "origin:s3", "oac_only_origin_access")
    assert oac.status == "fail"
    assert "no S3-shaped origin" in oac.detail
    assert "agent-relay-origin" in oac.detail


def test_direct_origin_request_refused_passes_on_403() -> None:
    ctx = _make_ctx(_distribution_config())

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [r for r in results if r.check == "direct_origin_request_refused"]
    assert len(findings) == 1
    assert findings[0].status == "pass"


def test_direct_origin_request_200_is_detected_as_failure() -> None:
    """A direct S3-origin request that returns 200 fails."""
    ctx = _make_ctx(_distribution_config())

    def caller(_url: str) -> HttpResult:
        return 200, "<html>public bucket listing</html>"

    results = list(
        checks(
            ctx,
            http_caller=caller,
            http_method_caller=_as_method_caller(caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [r for r in results if r.check == "direct_origin_request_refused"]
    assert len(findings) == 1
    assert findings[0].status == "fail"
    assert "200" in findings[0].detail


def test_direct_origin_request_404_is_a_distinct_failure_from_expected_403() -> None:
    """A direct S3-origin 404 fails as distinct from the expected 403 refusal."""
    ctx = _make_ctx(_distribution_config())

    def caller(_url: str) -> HttpResult:
        return 404, ""

    results = list(
        checks(
            ctx,
            http_caller=caller,
            http_method_caller=_as_method_caller(caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [r for r in results if r.check == "direct_origin_request_refused"]
    assert len(findings) == 1
    assert findings[0].status == "fail"
    assert "404" in findings[0].detail
    assert "wrong" in findings[0].detail.lower()


def test_direct_origin_request_transport_failure_is_reported_not_raised() -> None:
    ctx = _make_ctx(_distribution_config())

    def caller(url: str) -> HttpResult:
        return f"transport error calling {url}: connection refused"

    results = list(
        checks(
            ctx,
            http_caller=caller,
            http_method_caller=_as_method_caller(caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [r for r in results if r.check == "direct_origin_request_refused"]
    assert len(findings) == 1
    assert findings[0].status == "fail"
    assert "transport error" in findings[0].detail


def test_configured_tls_minimum_is_defer_not_pass_or_skip_when_using_default_certificate() -> None:
    """With the default cloudfront.net certificate the TLS minimum is `defer` with reason
    `missing_custom_acm_certificate`, never `pass` or `skip`."""
    config = _distribution_config(uses_default_certificate=True, minimum_protocol_version="TLSv1")
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [r for r in results if r.check == "configured_tls_minimum"]
    assert len(findings) == 1
    # Typed `str` so the `!=` assertions are not statically narrowed away.
    status: str = findings[0].status
    assert status == "defer"
    assert status != "pass"
    assert status != "skip"
    assert findings[0].defer_reason == "missing_custom_acm_certificate"
    assert "inert" in findings[0].detail.lower()
    assert "not enforced" in findings[0].detail.lower()
    assert "custom acm certificate" in findings[0].detail.lower()


def test_a_deferred_tls_check_does_not_mask_a_real_failure_of_another_cloudfront_check() -> None:
    """The deferred TLS check does not mask another check's failure in the exit code."""
    config = _distribution_config(uses_default_certificate=True, minimum_protocol_version="TLSv1")
    ctx = _make_ctx(config)

    def publicly_readable_origin(url: str) -> HttpResult:
        # Only the bucket domain misbehaves.
        if S3_DOMAIN_NAME in url:
            return 200, "<html>bucket listing</html>"
        return _healthy_caller(url)

    results = tuple(
        checks(
            ctx,
            http_caller=publicly_readable_origin,
            http_method_caller=_as_method_caller(publicly_readable_origin),
            tls_probe=_refused_probe,
        )
    )
    result = ResourceClassResult(layer=4, resource_class="cloudfront", checks=results)

    assert [r.check for r in result.deferred] == ["configured_tls_minimum"]
    assert [r.check for r in result.failed] == ["direct_origin_request_refused"]
    assert result.exit_code() == 1


def test_configured_tls_minimum_passes_with_custom_certificate_and_correct_floor() -> None:
    """With a custom ACM certificate and a TLS 1.2+ floor the TLS-minimum check passes."""
    config = _distribution_config(
        uses_default_certificate=False, minimum_protocol_version="TLSv1.2_2021"
    )
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [r for r in results if r.check == "configured_tls_minimum"]
    assert len(findings) == 1
    assert findings[0].status == "pass"


def test_configured_tls_minimum_fails_with_custom_certificate_but_weak_floor() -> None:
    """A custom certificate with a sub-1.2 floor fails the TLS-minimum check."""
    config = _distribution_config(uses_default_certificate=False, minimum_protocol_version="TLSv1")
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [r for r in results if r.check == "configured_tls_minimum"]
    assert len(findings) == 1
    assert findings[0].status == "fail"


def test_configured_tls_minimum_fails_for_tlsv1_1_2016_even_with_custom_certificate() -> None:
    """`TLSv1.1_2016` fails the TLS-minimum check even with a custom ACM certificate."""
    config = _distribution_config(
        uses_default_certificate=False, minimum_protocol_version="TLSv1.1_2016"
    )
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [r for r in results if r.check == "configured_tls_minimum"]
    assert len(findings) == 1
    assert findings[0].status == "fail"
    assert "below the required TLS 1.2 floor" in findings[0].detail


@pytest.mark.parametrize(
    ("minimum_protocol_version", "expected_status"),
    [
        ("SSLv3", "fail"),
        ("TLSv1", "fail"),
        ("TLSv1_2016", "fail"),
        ("TLSv1.1_2016", "fail"),
        ("TLSv1.2_2018", "pass"),
        ("TLSv1.2_2019", "pass"),
        ("TLSv1.2_2021", "pass"),
    ],
)
def test_configured_tls_minimum_accepted_set_matches_every_aws_protocol_version(
    minimum_protocol_version: str, expected_status: str
) -> None:
    """Every `MinimumProtocolVersion` CloudFront offers gets its correct verdict with a custom
    certificate."""
    config = _distribution_config(
        uses_default_certificate=False, minimum_protocol_version=minimum_protocol_version
    )
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [r for r in results if r.check == "configured_tls_minimum"]
    assert len(findings) == 1
    assert findings[0].status == expected_status


def test_sub_1_2_handshake_refused_passes_when_probe_reports_refused() -> None:
    ctx = _make_ctx(_distribution_config())

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [r for r in results if r.check == "sub_1_2_handshake_refused"]
    assert len(findings) == 1
    assert findings[0].status == "pass"


def test_sub_1_2_handshake_unexpectedly_succeeding_is_a_failure() -> None:
    """A sub-1.2 handshake that succeeds fails the TLS probe check."""
    ctx = _make_ctx(_distribution_config())

    def probe(_domain_name: str) -> TlsProbeResult:
        return False

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=probe,
        )
    )

    findings = [r for r in results if r.check == "sub_1_2_handshake_refused"]
    assert len(findings) == 1
    assert findings[0].status == "fail"
    assert "unexpectedly succeeded" in findings[0].detail


def test_sub_1_2_handshake_local_stack_limitation_is_skip_not_pass() -> None:
    """A local OpenSSL that cannot offer TLS 1.0/1.1 makes the probe check `skip`, never `pass`."""
    ctx = _make_ctx(_distribution_config())

    def probe(_domain_name: str) -> TlsProbeResult:
        return "local TLS stack cannot configure a sub-1.2 ceiling: unsupported protocol"

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=probe,
        )
    )

    findings = [r for r in results if r.check == "sub_1_2_handshake_refused"]
    assert len(findings) == 1
    assert findings[0].status == "skip"
    assert "local-stack limitation" in findings[0].detail


def test_default_behavior_routes_to_s3_passes() -> None:
    ctx = _make_ctx(_distribution_config())

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [
        r for r in results if r.resource == "behavior:/*" and r.check == "routes_to_correct_origin"
    ]
    assert len(findings) == 1
    assert findings[0].status == "pass"


def test_default_behavior_routed_to_wrong_origin_is_detected() -> None:
    """The default behaviour wired to the wrong origin fails."""
    config = _distribution_config(
        default_behavior=_default_behavior(target_origin_id="api-gateway-origin")
    )
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [
        r for r in results if r.resource == "behavior:/*" and r.check == "routes_to_correct_origin"
    ]
    assert len(findings) == 1
    assert findings[0].status == "fail"


def test_api_behavior_routes_caches_and_forwards_correctly() -> None:
    ctx = _make_ctx(_distribution_config())

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    resource_checks = {
        (r.resource, r.check): r.status for r in results if r.resource == "behavior:/api/*"
    }
    assert resource_checks[("behavior:/api/*", "routes_to_correct_origin")] == "pass"
    assert resource_checks[("behavior:/api/*", "caching_disabled")] == "pass"
    assert resource_checks[("behavior:/api/*", "authorization_forwarded")] == "pass"


def test_agent_behavior_routes_caches_and_forwards_correctly() -> None:
    ctx = _make_ctx(_distribution_config())

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    resource_checks = {
        (r.resource, r.check): r.status for r in results if r.resource == "behavior:/agent/*"
    }
    assert resource_checks[("behavior:/agent/*", "routes_to_correct_origin")] == "pass"
    assert resource_checks[("behavior:/agent/*", "caching_disabled")] == "pass"
    assert resource_checks[("behavior:/agent/*", "authorization_forwarded")] == "pass"


def test_api_behavior_routed_to_wrong_origin_is_detected() -> None:
    config = _distribution_config(
        api_behavior=_custom_behavior("/api/*", "agentcore-gateway-origin")
    )
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [
        r
        for r in results
        if r.resource == "behavior:/api/*" and r.check == "routes_to_correct_origin"
    ]
    assert len(findings) == 1
    assert findings[0].status == "fail"


def test_missing_api_behavior_fails_naming_the_path_pattern() -> None:
    """A missing behaviour must fail, naming the path pattern, across all three checks."""
    config = _distribution_config(include_api_behavior=False)
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    # `unauthenticated_api_still_refused` reads the live response, not the config.
    behavior_checks = [
        r
        for r in results
        if r.resource == "behavior:/api/*" and r.check != "unauthenticated_api_still_refused"
    ]
    assert len(behavior_checks) == 3
    assert all(r.status == "fail" for r in behavior_checks)
    assert all("/api/*" in r.detail for r in behavior_checks)


def test_missing_agent_behavior_fails_naming_the_path_pattern() -> None:
    config = _distribution_config(include_agent_behavior=False)
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    behavior_checks = [r for r in results if r.resource == "behavior:/agent/*"]
    # Origin, caching, Authorization, no path-rewrite function, and compression off.
    assert len(behavior_checks) == 5
    assert all(r.status == "fail" for r in behavior_checks)
    assert all("/agent/*" in r.detail for r in behavior_checks)


def test_caching_enabled_on_api_behavior_is_detected() -> None:
    """A caching-enabled `/api/*` behaviour (wrong cache policy) fails."""
    config = _distribution_config(
        api_behavior=_custom_behavior(
            "/api/*", "api-gateway-origin", cache_policy_id=CACHING_OPTIMIZED_POLICY_ID
        )
    )
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [
        r for r in results if r.resource == "behavior:/api/*" and r.check == "caching_disabled"
    ]
    assert len(findings) == 1
    assert findings[0].status == "fail"


def test_caching_enabled_via_legacy_forwarded_values_ttl_is_detected() -> None:
    """Nonzero legacy `ForwardedValues` TTLs fail the caching-disabled check."""
    legacy_behavior = {
        "PathPattern": "/api/*",
        "TargetOriginId": "api-gateway-origin",
        "ForwardedValues": {"Headers": {"Items": ["Authorization"]}},
        "MinTTL": 0,
        "DefaultTTL": 300,
        "MaxTTL": 3600,
    }
    config = _distribution_config(api_behavior=legacy_behavior)
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [
        r for r in results if r.resource == "behavior:/api/*" and r.check == "caching_disabled"
    ]
    assert len(findings) == 1
    assert findings[0].status == "fail"


def test_stripped_authorization_header_on_agent_behavior_is_detected() -> None:
    """A behaviour with no `OriginRequestPolicyId` and no legacy `Authorization` header fails."""
    stripped_behavior = {
        "PathPattern": "/agent/*",
        "TargetOriginId": "agentcore-gateway-origin",
        "CachePolicyId": CACHING_DISABLED_POLICY_ID,
    }
    config = _distribution_config(agent_behavior=stripped_behavior)
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [
        r
        for r in results
        if r.resource == "behavior:/agent/*" and r.check == "authorization_forwarded"
    ]
    assert len(findings) == 1
    assert findings[0].status == "fail"


def test_authorization_forwarded_via_legacy_forwarded_values_headers_passes() -> None:
    """Legacy `ForwardedValues.Headers` listing `Authorization` (or `*`) passes."""
    legacy_behavior = {
        "PathPattern": "/agent/*",
        "TargetOriginId": "agentcore-gateway-origin",
        "ForwardedValues": {"Headers": {"Items": ["Authorization"]}},
        "MinTTL": 0,
        "DefaultTTL": 0,
        "MaxTTL": 0,
    }
    config = _distribution_config(agent_behavior=legacy_behavior)
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    findings = [
        r
        for r in results
        if r.resource == "behavior:/agent/*" and r.check == "authorization_forwarded"
    ]
    assert len(findings) == 1
    assert findings[0].status == "pass"


def test_no_matching_distribution_fails_with_actionable_detail() -> None:
    """No distribution matching `cloudfront_domain_name` fails with a detail naming the domain."""

    class EmptyCloudFrontClient:
        def list_distributions(self, **_kwargs: Any) -> dict[str, Any]:
            return {"DistributionList": {"Items": []}}

    ctx = context_for_test(outputs=_outputs(), clients={"cloudfront": EmptyCloudFrontClient()})

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    assert len(results) == 1
    assert results[0].check == "resolve_distribution_id"
    assert results[0].status == "fail"
    assert DOMAIN_NAME in results[0].detail


def test_all_resource_classes_still_checked_when_oac_check_fails() -> None:
    """Every behaviour and check still runs after an earlier check has failed."""
    config = _distribution_config(s3_origin=_s3_origin(none_at_all=True))
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    check_names = {r.check for r in results}
    assert "direct_origin_request_refused" in check_names
    assert "configured_tls_minimum" in check_names
    assert "sub_1_2_handshake_refused" in check_names
    assert any(r.resource == "behavior:/*" for r in results)
    assert any(r.resource == "behavior:/api/*" for r in results)
    assert any(r.resource == "behavior:/agent/*" for r in results)


def _spa_checks(results: list[Any], check: str) -> Any:
    findings = [r for r in results if r.check == check]
    assert len(findings) == 1, f"expected exactly one {check!r} result, got {len(findings)}"
    return findings[0]


def test_spa_deep_link_serving_the_document_and_api_still_refused_both_pass() -> None:
    """A client-side route returns the SPA and an unauthenticated `/api/*` call is refused."""
    ctx = _make_ctx(_distribution_config())

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    assert _spa_checks(results, "deep_link_serves_the_spa").status == "pass"
    assert _spa_checks(results, "missing_asset_is_not_rewritten").status == "pass"
    assert _spa_checks(results, "unauthenticated_api_still_refused").status == "pass"


def test_a_200_that_is_not_the_spa_document_fails_the_deep_link_check() -> None:
    """A deep link returning 200 with a body that is not the SPA document fails."""
    ctx = _make_ctx(_distribution_config())

    def caller(url: str) -> HttpResult:
        if "/accounts/" in url:
            return 200, "<html><body>ERROR: The request could not be satisfied.</body></html>"
        return _healthy_caller(url)

    results = list(
        checks(
            ctx,
            http_caller=caller,
            http_method_caller=_as_method_caller(caller),
            tls_probe=_refused_probe,
        )
    )

    finding = _spa_checks(results, "deep_link_serves_the_spa")
    assert finding.status == "fail"
    assert "not the built SPA document" in finding.detail


def test_an_unpublished_origin_bucket_fails_the_deep_link_check_naming_the_cause() -> None:
    """A 403 on the deep link is reported as `/index.html` missing from the bucket."""
    ctx = _make_ctx(_distribution_config())

    def caller(url: str) -> HttpResult:
        if S3_DOMAIN_NAME in url:
            return 403, ""
        return 403, "<?xml version='1.0'?><Error><Code>AccessDenied</Code></Error>"

    results = list(
        checks(
            ctx,
            http_caller=caller,
            http_method_caller=_as_method_caller(caller),
            tls_probe=_refused_probe,
        )
    )

    finding = _spa_checks(results, "deep_link_serves_the_spa")
    assert finding.status == "fail"
    assert "published" in finding.detail


def test_a_blanket_rewrite_that_also_swallows_missing_assets_is_detected() -> None:
    """A rewrite that also serves the SPA for a missing `*.js` fails."""
    ctx = _make_ctx(_distribution_config())

    def caller(url: str) -> HttpResult:
        if S3_DOMAIN_NAME in url:
            return 403, ""
        if "/api/" in url:
            return 401, ""
        return 200, SPA_DOCUMENT  # every non-API path, assets included

    results = list(
        checks(
            ctx,
            http_caller=caller,
            http_method_caller=_as_method_caller(caller),
            tls_probe=_refused_probe,
        )
    )

    assert _spa_checks(results, "deep_link_serves_the_spa").status == "pass"
    finding = _spa_checks(results, "missing_asset_is_not_rewritten")
    assert finding.status == "fail"
    assert "not guarding asset paths" in finding.detail


def test_an_api_rejection_rewritten_into_the_spa_page_is_detected() -> None:
    """An unauthenticated `/api/*` rejection rewritten into the SPA page fails."""
    ctx = _make_ctx(_distribution_config())

    def caller(url: str) -> HttpResult:
        if S3_DOMAIN_NAME in url:
            return 403, ""
        return 200, SPA_DOCUMENT  # 403 -> 200 /index.html, applied distribution-wide

    results = list(
        checks(
            ctx,
            http_caller=caller,
            http_method_caller=_as_method_caller(caller),
            tls_probe=_refused_probe,
        )
    )

    finding = _spa_checks(results, "unauthenticated_api_still_refused")
    assert finding.status == "fail"
    assert finding.resource == "behavior:/api/*"
    assert "rewritten into the frontend page" in finding.detail


@pytest.mark.parametrize("status_code", [200, 302, 500])
def test_any_api_status_other_than_401_or_403_fails(status_code: int) -> None:
    ctx = _make_ctx(_distribution_config())

    def caller(url: str) -> HttpResult:
        if "/api/" in url:
            return status_code, '{"accounts": []}'
        return _healthy_caller(url)

    results = list(
        checks(
            ctx,
            http_caller=caller,
            http_method_caller=_as_method_caller(caller),
            tls_probe=_refused_probe,
        )
    )

    finding = _spa_checks(results, "unauthenticated_api_still_refused")
    assert finding.status == "fail"
    assert str(status_code) in finding.detail


def test_transport_failures_on_the_spa_and_api_probes_are_reported_not_raised() -> None:
    ctx = _make_ctx(_distribution_config())

    def caller(url: str) -> HttpResult:
        if S3_DOMAIN_NAME in url:
            return 403, ""
        return f"transport error calling {url}: connection reset"

    results = list(
        checks(
            ctx,
            http_caller=caller,
            http_method_caller=_as_method_caller(caller),
            tls_probe=_refused_probe,
        )
    )

    for check in (
        "deep_link_serves_the_spa",
        "missing_asset_is_not_rewritten",
        "unauthenticated_api_still_refused",
    ):
        finding = _spa_checks(results, check)
        assert finding.status == "fail"
        assert "transport error" in finding.detail


def test_the_negative_auth_probe_names_a_route_the_api_actually_declares() -> None:
    """The probe targets a declared `POST /<tool_name>` route named in the toolspec."""
    from crm.toolspec.definitions import ALL_TOOLS

    assert _UNAUTHENTICATED_API_METHOD == "POST"
    assert _UNAUTHENTICATED_API_PATH.startswith("/api/")
    tool_name = _UNAUTHENTICATED_API_PATH.removeprefix("/api/")
    assert tool_name in {tool.name for tool in ALL_TOOLS}


def test_a_404_from_route_matching_fails_rather_than_reading_as_a_refusal() -> None:
    """A `404` from the unauthenticated API probe fails rather than counting as a refusal."""
    ctx = _make_ctx(_distribution_config())

    def caller(url: str) -> HttpResult:
        if "/api/" in url:
            return 404, '{"message":"Not Found"}'
        return _healthy_caller(url)

    results = list(
        checks(
            ctx,
            http_caller=caller,
            http_method_caller=_as_method_caller(caller),
            tls_probe=_refused_probe,
        )
    )

    finding = _spa_checks(results, "unauthenticated_api_still_refused")
    assert finding.status == "fail"
    assert "404" in finding.detail


# ---------------------------------------------------------------------------
# What must be ABSENT from the deployed distribution.
# ---------------------------------------------------------------------------


def test_the_agent_behavior_is_checked_against_the_relay_function_url_output() -> None:
    """The relay origin is compared against the host of the `agent_relay_function_url` output."""
    ctx = _make_ctx(_distribution_config())

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    routed = _one(results, "behavior:/agent/*", "routes_to_correct_origin")
    assert routed.status == "pass", routed.detail
    assert RELAY_FUNCTION_HOST in routed.detail


def test_a_surviving_agentcore_origin_is_detected() -> None:
    """A surviving retired AgentCore origin in the deployed distribution fails."""
    config = _distribution_config(extra_origins=[_retired_agentcore_origin()])
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    retired = _one(results, "distribution:origins", "retired_agentcore_origin_absent")
    assert retired.status == "fail"
    assert "bedrock-agentcore" in retired.detail


def test_a_clean_distribution_reports_the_retired_origin_absent() -> None:
    """A distribution with no retired origin passes the absence check."""
    ctx = _make_ctx(_distribution_config())

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    assert _one(results, "distribution:origins", "retired_agentcore_origin_absent").status == "pass"


def test_a_surviving_path_rewrite_function_is_detected() -> None:
    """A viewer-request function surviving on the `/agent/*` behaviour fails."""
    config = _distribution_config(
        agent_behavior=_custom_behavior(
            "/agent/*",
            "agent-relay-origin",
            compress=False,
            function_associations={
                "Quantity": 1,
                "Items": [
                    {
                        "FunctionARN": "arn:aws:cloudfront::123456789012:function/agent-path-rewrite",
                        "EventType": "viewer-request",
                    }
                ],
            },
        )
    )
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    association = _one(results, "behavior:/agent/*", "path_rewrite_function_absent")
    assert association.status == "fail"
    assert "agent-path-rewrite" in association.detail


def test_compression_enabled_on_the_agent_behavior_is_detected() -> None:
    """`Compress: true` on `/agent/*` fails: CloudFront buffers SSE frames to gzip them."""
    config = _distribution_config(
        agent_behavior=_custom_behavior("/agent/*", "agent-relay-origin", compress=True)
    )
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    compression = _one(results, "behavior:/agent/*", "compression_disabled")
    assert compression.status == "fail"
    assert "buffering" in compression.detail


def test_an_absent_compress_field_fails_rather_than_reading_as_disabled() -> None:
    """An absent `Compress` field on the `/agent/*` behaviour fails rather than reading as off."""
    config = _distribution_config(agent_behavior=_custom_behavior("/agent/*", "agent-relay-origin"))
    ctx = _make_ctx(config)

    results = list(
        checks(
            ctx,
            http_caller=_healthy_caller,
            http_method_caller=_as_method_caller(_healthy_caller),
            tls_probe=_refused_probe,
        )
    )

    assert _one(results, "behavior:/agent/*", "compression_disabled").status == "fail"
