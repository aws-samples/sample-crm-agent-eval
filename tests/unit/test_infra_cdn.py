# SPDX-License-Identifier: MIT
"""Source assertions on `infra/modules/frontend_cdn`'s CloudFront behaviours and origins.

`/agent/*` targets the relay's Function URL origin, disables compression and caching, and uses the
module's own origin-request policy, which forwards `X-Crm-Authorization` (the Cognito JWT) and
`x-amz-content-sha256`. `/api/*` targets a VPC origin. The module reads live AWS-managed policies
and cannot be planned offline, so these tests parse the committed HCL;
`scripts/smoke/cloudfront.py` checks the deployed distribution.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOUDFRONT_TF = REPO_ROOT / "infra" / "modules" / "frontend_cdn" / "cloudfront.tf"
VARIABLES_TF = REPO_ROOT / "infra" / "modules" / "frontend_cdn" / "variables.tf"
ROOT_TF = REPO_ROOT / "infra" / "main.tf"
FUNCTIONS_DIR = REPO_ROOT / "infra" / "modules" / "frontend_cdn" / "functions"

RELAY_ORIGIN_ID = "agent-relay-origin"

#: The `/api/*` behaviour's target origin.
API_ORIGIN_ID = "api-gateway-origin"

#: Data-source references for `Managed-CachingDisabled` and `Managed-AllViewerExceptHostHeader`.
CACHING_DISABLED_DATA_SOURCE = "data.aws_cloudfront_cache_policy.caching_disabled.id"
ALL_VIEWER_DATA_SOURCE = "data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id"

#: `/agent/*`'s own origin-request policy, so the forwarded header set is readable from source.
RELAY_ORIGIN_REQUEST_POLICY = "aws_cloudfront_origin_request_policy.agent_relay.id"

#: The lambda-type OAC that SigV4-signs origin requests to the relay's `AWS_IAM` Function URL.
RELAY_OAC = "aws_cloudfront_origin_access_control.agent_relay.id"

#: The Cognito JWT's header; OAC's `always` signing overwrites `Authorization`.
JWT_HEADER = "x-crm-authorization"

#: The body hash OAC requires the viewer to supply on a POST to a Lambda Function URL.
BODY_HASH_HEADER = "x-amz-content-sha256"


def _body_after(text: str, index: int) -> str:
    """The brace-balanced body starting at the `{` at or after `index`."""
    start = text.index("{", index)
    depth = 0
    for position in range(start, len(text)):
        character = text[position]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : position]
    raise AssertionError(f"unbalanced braces from offset {index}")


def _blocks(text: str, header: str) -> list[str]:
    """Every brace-balanced body of a block introduced by `header` (e.g. `ordered_cache_behavior`)."""
    bodies = []
    for match in re.finditer(rf"(?m)^\s*{re.escape(header)}\s*\{{", text):
        bodies.append(_body_after(text, match.start()))
    return bodies


def _resource(text: str, resource_type: str, name: str) -> str:
    match = re.search(rf'(?m)^resource\s+"{resource_type}"\s+"{name}"\s*\{{', text)
    assert match is not None, f'resource "{resource_type}" "{name}" not found'
    return _body_after(text, match.start())


def _arguments(body: str) -> dict[str, str]:
    """Top-level `key = value` arguments of a block body, excluding nested blocks and comments."""
    stripped = body
    while True:
        match = re.search(r"(?m)^\s*[a-z_]+\s*\{", stripped)
        if match is None:
            break
        inner = _body_after(stripped, match.start())
        end = stripped.index(inner, match.start()) + len(inner) + 1
        stripped = stripped[: match.start()] + stripped[end:]

    arguments = {}
    for line in stripped.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if re.fullmatch(r"[a-z_][a-z0-9_]*", key.strip()):
            arguments[key.strip()] = value.strip().strip('"')
    return arguments


def _listed_items(body: str) -> list[str]:
    """The `items = ["a", "b"]` list of a policy sub-block, lowercased. Empty when unset."""
    match = re.search(r"(?ms)^\s*items\s*=\s*\[(.*?)\]", body)
    if match is None:
        return []
    return [item.strip().strip('"').lower() for item in match.group(1).split(",") if item.strip()]


def _relay_headers_config() -> tuple[str, list[str]]:
    """`/agent/*`'s origin-request policy as (`header_behavior`, the headers it names)."""
    policy = _resource(
        CLOUDFRONT_TF.read_text(encoding="utf-8"),
        "aws_cloudfront_origin_request_policy",
        "agent_relay",
    )
    blocks = _blocks(policy, "headers_config")
    assert len(blocks) == 1, "expected exactly one headers_config on the relay policy"
    behaviour = _arguments(blocks[0])["header_behavior"]
    named = [header for block in _blocks(blocks[0], "headers") for header in _listed_items(block)]
    return behaviour, named


def _forwards_to_origin(header: str) -> bool:
    """Whether CloudFront forwards `header` to the relay, under any `header_behavior` in source."""
    behaviour, named = _relay_headers_config()
    header = header.lower()
    if behaviour == "none":
        return False
    if behaviour == "whitelist":
        return header in named
    if behaviour == "allExcept":
        return header not in named
    if behaviour in {"allViewer", "allViewerAndWhitelistCloudFront"}:
        return True
    raise AssertionError(f"unknown header_behavior {behaviour!r}")


def _behaviour(path_pattern: str) -> dict[str, str]:
    text = CLOUDFRONT_TF.read_text(encoding="utf-8")
    for body in _blocks(text, "ordered_cache_behavior"):
        arguments = _arguments(body)
        if arguments.get("path_pattern") == path_pattern:
            return arguments
    raise AssertionError(f"no ordered_cache_behavior for {path_pattern}")


def _behaviour_body(path_pattern: str) -> str:
    text = CLOUDFRONT_TF.read_text(encoding="utf-8")
    for body in _blocks(text, "ordered_cache_behavior"):
        if _arguments(body).get("path_pattern") == path_pattern:
            return body
    raise AssertionError(f"no ordered_cache_behavior for {path_pattern}")


# ---------------------------------------------------------------------------
# The parse itself, asserted before anything is concluded from it.
# ---------------------------------------------------------------------------


def test_the_parser_finds_both_identity_bearing_behaviours() -> None:
    """The parser finds exactly the `/api/*` and `/agent/*` ordered cache behaviours."""
    patterns = {
        _arguments(body).get("path_pattern")
        for body in _blocks(CLOUDFRONT_TF.read_text(encoding="utf-8"), "ordered_cache_behavior")
    }
    assert patterns == {"/api/*", "/agent/*"}


# ---------------------------------------------------------------------------
# /agent/* — the streaming path.
# ---------------------------------------------------------------------------


def test_the_agent_behaviour_targets_the_relay_not_the_agentcore_data_plane() -> None:
    assert _behaviour("/agent/*")["target_origin_id"] == RELAY_ORIGIN_ID


def test_the_agent_behaviour_disables_compression() -> None:
    """`/agent/*` sets `compress = false`, since CloudFront compression buffers the SSE stream."""
    assert _behaviour("/agent/*")["compress"] == "false"


def test_the_agent_behaviour_disables_caching_and_uses_the_modules_own_origin_request_policy() -> (
    None
):
    arguments = _behaviour("/agent/*")
    assert arguments["cache_policy_id"] == CACHING_DISABLED_DATA_SOURCE
    assert arguments["origin_request_policy_id"] == RELAY_ORIGIN_REQUEST_POLICY
    assert arguments["viewer_protocol_policy"] == "https-only"


def test_the_agent_behaviour_accepts_the_methods_a_streaming_post_needs() -> None:
    allowed = _behaviour("/agent/*")["allowed_methods"]
    for method in ("POST", "OPTIONS", "GET", "HEAD"):
        assert method in allowed


def test_the_agent_behaviour_has_no_viewer_request_function() -> None:
    """`/agent/*` has no function association and the old path-rewrite function file is absent."""
    assert "function_association" not in _behaviour_body("/agent/*")
    assert not (FUNCTIONS_DIR / "agent_path_rewrite.js").exists()


# ---------------------------------------------------------------------------
# The relay origin and its OAC lock.
# ---------------------------------------------------------------------------


def _relay_origin() -> str:
    text = CLOUDFRONT_TF.read_text(encoding="utf-8")
    return next(
        body
        for body in _blocks(text, "origin")
        if _arguments(body).get("origin_id") == RELAY_ORIGIN_ID
    )


def test_the_relay_origin_is_locked_by_oac_not_by_a_secret_header() -> None:
    """The relay origin points at the Function URL domain and is locked by the relay OAC."""
    origin = _relay_origin()
    arguments = _arguments(origin)
    assert arguments["domain_name"] == "var.agent_relay_function_url_domain"
    assert arguments["origin_access_control_id"] == RELAY_OAC


def test_the_relay_oac_signs_every_origin_request_with_sigv4() -> None:
    """The relay OAC is lambda-type and signs every origin request (`always`, `sigv4`)."""
    oac = _arguments(
        _resource(
            CLOUDFRONT_TF.read_text(encoding="utf-8"),
            "aws_cloudfront_origin_access_control",
            "agent_relay",
        )
    )
    assert oac["origin_access_control_origin_type"] == "lambda"
    assert oac["signing_behavior"] == "always"
    assert oac["signing_protocol"] == "sigv4"


def test_the_distribution_may_invoke_the_relays_function_url_and_only_this_distribution() -> None:
    """The Function URL permission names CloudFront, this distribution's ARN, and `AWS_IAM`."""
    permission = _arguments(
        _resource(
            CLOUDFRONT_TF.read_text(encoding="utf-8"),
            "aws_lambda_permission",
            "agent_relay_function_url",
        )
    )
    assert permission["action"] == "lambda:InvokeFunctionUrl"
    assert permission["principal"] == "cloudfront.amazonaws.com"
    assert permission["function_name"] == "var.agent_relay_function_name"
    assert permission["source_arn"] == "aws_cloudfront_distribution.frontend.arn"
    assert permission["function_url_auth_type"] == "AWS_IAM"


def test_the_relay_origin_request_policy_forwards_the_jwt_and_the_body_hash() -> None:
    """The relay origin-request policy's forwarded set includes the JWT and body-hash headers."""
    assert _forwards_to_origin(JWT_HEADER)
    assert _forwards_to_origin(BODY_HASH_HEADER)


def test_the_relay_origin_request_policy_does_not_forward_host() -> None:
    """The relay origin-request policy does not forward `Host`."""
    assert not _forwards_to_origin("host")


def test_no_agentcore_data_plane_origin_survives() -> None:
    """The module declares no AgentCore data-plane or Gateway origin."""
    text = CLOUDFRONT_TF.read_text(encoding="utf-8")
    assert "bedrock-agentcore" not in text
    assert "agentcore-gateway-origin" not in text
    assert "agentcore_gateway_domain_name" not in VARIABLES_TF.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# /api/*, and the root's wiring of this module's inputs.
# ---------------------------------------------------------------------------


def test_the_api_behaviour_is_unchanged_by_the_agent_rework() -> None:
    """`/api/*` targets the API origin, compresses, disables caching, and has its function."""
    arguments = _behaviour("/api/*")
    assert arguments["target_origin_id"] == "api-gateway-origin"
    assert arguments["compress"] == "true"
    assert arguments["cache_policy_id"] == CACHING_DISABLED_DATA_SOURCE
    assert "function_association" in _behaviour_body("/api/*")


# ---------------------------------------------------------------------------
# /api/*'s VPC origin to the private REST API.
# ---------------------------------------------------------------------------


def _origin(origin_id: str) -> str:
    """The brace-balanced body of the `origin` block whose `origin_id` is `origin_id`."""
    text = CLOUDFRONT_TF.read_text(encoding="utf-8")
    bodies = [
        body for body in _blocks(text, "origin") if _arguments(body).get("origin_id") == origin_id
    ]
    assert len(bodies) == 1, f"expected exactly one origin with origin_id {origin_id!r}"
    return bodies[0]


def test_the_api_origin_is_a_vpc_origin_and_not_a_custom_origin() -> None:
    """The `/api/*` origin declares `vpc_origin_config` and no `custom_origin_config`."""
    origin = _origin(API_ORIGIN_ID)
    assert _blocks(origin, "vpc_origin_config"), "the /api/* origin declares no vpc_origin_config"
    assert not _blocks(origin, "custom_origin_config"), (
        "the /api/* origin still declares custom_origin_config"
    )


def test_the_api_origin_takes_its_vpc_origin_id_from_the_module_variable() -> None:
    """The `/api/*` origin's `vpc_origin_id` is `var.api_origin_vpc_origin_id`, not a literal."""
    blocks = _blocks(_origin(API_ORIGIN_ID), "vpc_origin_config")
    assert len(blocks) == 1
    assert _arguments(blocks[0])["vpc_origin_id"] == "var.api_origin_vpc_origin_id"


def test_the_api_origins_domain_name_is_still_the_private_api_host_header() -> None:
    """The `/api/*` origin's `domain_name` is the private API hostname, sent as `Host`."""
    assert _arguments(_origin(API_ORIGIN_ID))["domain_name"] == "var.api_gateway_domain_name"


def test_the_api_behaviour_does_not_forward_the_viewers_host_header() -> None:
    """`/api/*` uses `Managed-AllViewerExceptHostHeader`, so the viewer's `Host` is dropped."""
    assert _behaviour("/api/*")["origin_request_policy_id"] == ALL_VIEWER_DATA_SOURCE


def test_the_root_wires_the_vpc_origin_id_from_the_api_origin_module() -> None:
    """The root passes `module.api_origin.vpc_origin_id`, and `modules/api_origin` declares it."""
    root = ROOT_TF.read_text(encoding="utf-8")
    module_block = _body_after(root, root.index('module "frontend_cdn"'))
    assert _arguments(module_block)["api_origin_vpc_origin_id"] == "module.api_origin.vpc_origin_id"

    api_origin_outputs = (REPO_ROOT / "infra" / "modules" / "api_origin" / "outputs.tf").read_text(
        encoding="utf-8"
    )
    assert 'output "vpc_origin_id"' in api_origin_outputs


def test_the_root_wires_the_relay_origin_from_the_relay_module() -> None:
    """The root passes the relay module's `function_url_domain` and `function_name` outputs."""
    module_block = _body_after(
        ROOT_TF.read_text(encoding="utf-8"),
        ROOT_TF.read_text(encoding="utf-8").index('module "frontend_cdn"'),
    )
    arguments = _arguments(module_block)
    assert arguments["agent_relay_function_url_domain"] == "module.agent_relay.function_url_domain"
    assert arguments["agent_relay_function_name"] == "module.agent_relay.function_name"

    relay_outputs = (REPO_ROOT / "infra" / "modules" / "agent_relay" / "outputs.tf").read_text(
        encoding="utf-8"
    )
    assert 'output "function_url_domain"' in relay_outputs
    assert 'output "function_name"' in relay_outputs


def test_every_declared_input_is_wired_by_the_root() -> None:
    """The root passes exactly the variables `frontend_cdn` declares, in both directions."""
    variables = set(re.findall(r'(?m)^variable\s+"([a-z0-9_]+)"', VARIABLES_TF.read_text("utf-8")))
    assert variables, "parsed no variables, so this assertion would be vacuous"

    root = ROOT_TF.read_text(encoding="utf-8")
    module_block = _body_after(root, root.index('module "frontend_cdn"'))
    passed = set(_arguments(module_block)) - {"source"}

    assert variables - passed == set(), (
        f"declared but never passed by the root: {variables - passed}"
    )
    assert passed - variables == set(), f"passed by the root but not declared: {passed - variables}"


#: `Managed-SecurityHeadersPolicy`'s data-source reference.
SECURITY_HEADERS_DATA_SOURCE = "data.aws_cloudfront_response_headers_policy.security_headers.id"


def test_every_behaviour_attaches_the_managed_security_headers_policy() -> None:
    """The default behaviour and every ordered behaviour carry the security-headers policy."""
    text = CLOUDFRONT_TF.read_text(encoding="utf-8")
    behaviours = _blocks(text, "default_cache_behavior") + _blocks(text, "ordered_cache_behavior")
    assert len(behaviours) == 3, (
        f"expected the default, /api/* and /agent/* behaviours: {behaviours}"
    )
    for body in behaviours:
        arguments = _arguments(body)
        assert arguments.get("response_headers_policy_id") == SECURITY_HEADERS_DATA_SOURCE, (
            arguments.get("path_pattern", "default"),
            arguments.get("response_headers_policy_id"),
        )
    data = re.search(
        r'(?ms)^data\s+"aws_cloudfront_response_headers_policy"\s+"security_headers"\s*\{(.*?)^\}',
        text,
    )
    assert data is not None, "the security-headers data source is not declared"
    assert _arguments(data.group(1)).get("name") == "Managed-SecurityHeadersPolicy"
