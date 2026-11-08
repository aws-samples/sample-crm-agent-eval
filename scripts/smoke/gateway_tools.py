# SPDX-License-Identifier: MIT
"""Layer 4 smoke script: asserts the deployed AgentCore Gateway's per-tool posture.

Needs a live deployment. Lists tools over MCP (`tools/list`) and asserts the inventory matches
`crm.toolspec.definitions.ALL_TOOLS` exactly — no extra tool, no missing tool, and each tool's
schema matches structurally. Each tool's `description`, and each property's, is byte-identical
to the toolspec's. Invokes every tool individually via `tools/call` with a supplied Cognito token.

Also asserts a caller-supplied `_identity` in `params.arguments` is provably discarded by the
Gateway REQUEST interceptor (`src/gateway/identity_interceptor.py`) in favour of the token's
verified identity. The check calls `get_account` and observes the allow/deny outcome of a forged
`_identity`: one that would widen scope (the out-of-book account, or `full_visibility`) must still
be denied, and one that would narrow scope (an empty book) must still succeed. A result that does
not unambiguously show the expected outcome is a `fail`.

Also posts a raw JSON-RPC array body carrying one well-formed `tools/call` with a valid token, and
asserts every reply member is a refusal (a JSON-RPC error, or a `result` with `isError: true`)
and none carries tool output (`content` or `structuredContent`) without `isError: true`, recording
the reply's shape without its values. That no tool Lambda was invoked cannot
be tied to one request from outside the account's logs and metrics, so that row is reported as
`defer`.

Also asserts an expired token and an underscoped token are both refused with a user-safe
`{"error": {"code", "message"}}` body carrying no internal detail, each preceded by a premise check
read from the token's own signature-verified claims (a past `exp`; a `scope` lacking `*/invoke`). On the provisioned
path (no `--gateway-token`), unless supplied, the expired token is a machine token minted before anything else and aged past its `exp`, and the
underscoped token is a real end-user access token from `scripts.smoke.mfa_user`.

Authenticated checks use the Gateway service principal: the interceptor maps a `client_credentials`
token to a configured `rep_id` whose book and tenant come from that rep's `reps` item.
`LiveServicePrincipalProvisioner` seeds a disposable `eval-smoke-<hex>` tenant and that rep's book,
mints the principal's token, and removes all of it again; it never touches `demo` and never writes
the client secret or the token anywhere. A `reps` row the service principal already had (the
operator's) is read before anything is written and restored exactly in cleanup; only a row
that did not exist is deleted.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import jwt
from boto3.dynamodb.types import TypeDeserializer
from scripts.smoke.harness import DEFER, CheckResult, SmokeContext, open_https, run_main
from scripts.smoke.mfa_user import BareMfaUser, LiveBareMfaUserProvisioner
from scripts.smoke.tenant import (
    SmokeTenant,
    allocate_smoke_tenant,
    seed_smoke_tenant,
    teardown_smoke_tenant,
)
from scripts.smoke.verified_claims import (
    CLOCK_SKEW_SECONDS,
    USE_ACCESS,
    ClaimsVerificationError,
    verified_claims,
)

from crm.authz import DEMO_TENANT
from crm.errors import Conflict, NotFound, ScopeDenied, ValidationFailed
from crm.store.store import Store
from crm.toolspec.definitions import ALL_TOOLS, BY_NAME, ToolDefinition
from gateway.identity_interceptor import (
    SERVICE_PRINCIPAL_REP_ID_ENV as _SERVICE_PRINCIPAL_REP_ID_ENV,
)

LAYER = 4
RESOURCE_CLASS = "gateway_tools"

#: Tenant-name prefixes a seeded smoke tenant may legitimately carry (never `demo`).
_DISPOSABLE_TENANT_PREFIXES: Final[tuple[str, ...]] = ("smoke-", "eval-")

#: How many seeded accounts the service principal's book names.
_SERVICE_BOOK_SIZE: Final[int] = 2

#: Index of the seeded account used as the out-of-book target. Must be >= `_SERVICE_BOOK_SIZE`, so
#: the account genuinely exists in the tenant but is outside the seeded book.
_OUT_OF_BOOK_ACCOUNT_INDEX: Final[int] = 5

#: Fallback resource-server-qualified scope requested when minting the machine token; the Gateway's
#: authorizer requires exactly this scope (`infra/modules/cognito`).
_GATEWAY_SCOPE: Final[str] = "gateway/invoke"

#: Suffix of the resource-server scope the Gateway's authorizer requires (`<server>/invoke`).
_GATEWAY_SCOPE_SUFFIX: Final[str] = "invoke"

#: Extra seconds past a token's own `exp` to wait before presenting it, as a clock-skew margin.
_EXPIRY_MARGIN_SECONDS: Final[float] = 15.0

#: Ceiling on how long the expired-token check waits for a token to age out.
_MAX_EXPIRY_WAIT_SECONDS: Final[float] = 900.0

#: How far past its `exp` a token presented to the expiry check may be while its signature, `iss`,
#: `token_use` and `client_id` are still verified; an older token fails that check.
EXPIRED_TOKEN_MAX_AGE_SECONDS: Final[int] = 7 * 24 * 3600

#: Returns a token's verified claims; raises `ClaimsVerificationError` naming the failed check.
ClaimsVerifier = Callable[[str], Mapping[str, Any]]


def access_token_verifier(
    ctx: SmokeContext,
    *,
    client_id_output: str,
    leeway_seconds: int = CLOCK_SKEW_SECONDS,
    jwks_url: str | None = None,
) -> ClaimsVerifier:
    """A `ClaimsVerifier` for access tokens the deployment's user pool issued to one app client.

    The pool is `cognito_user_pool_id` and the client the `client_id_output` Terraform output,
    both read on each call, so a missing output surfaces as an exception from the verifier.
    `jwks_url` defaults to the pool's own JWKS.
    """

    def verify(token: str) -> Mapping[str, Any]:
        return verified_claims(
            token,
            region=ctx.region,
            user_pool_id=ctx.outputs.require_str("cognito_user_pool_id"),
            client_id=ctx.outputs.require_str(client_id_output),
            token_use=USE_ACCESS,
            jwks_url=jwks_url,
            leeway_seconds=leeway_seconds,
        )

    return verify


@dataclass(frozen=True, slots=True)
class TokenVerifiers:
    """Claims verifiers for the expired machine token and the underscoped user token."""

    expired: ClaimsVerifier
    underscoped: ClaimsVerifier


def token_verifiers(ctx: SmokeContext, *, jwks_url: str | None = None) -> TokenVerifiers:
    """The expired token is the MCP client's, verified with `EXPIRED_TOKEN_MAX_AGE_SECONDS` of
    leeway; the underscoped token is the SPA client's."""
    return TokenVerifiers(
        expired=access_token_verifier(
            ctx,
            client_id_output="cognito_mcp_client_id",
            leeway_seconds=EXPIRED_TOKEN_MAX_AGE_SECONDS,
            jwks_url=jwks_url,
        ),
        underscoped=access_token_verifier(
            ctx, client_id_output="cognito_spa_client_id", jwks_url=jwks_url
        ),
    )


#: One completed MCP JSON-RPC call: the decoded response body (whatever shape the Gateway
#: returned, including a JSON-RPC `error` member) or a `str` transport-failure message when no
#: response body was received at all.
McpResult = dict[str, Any] | str

#: `(method, params, token)` -> `McpResult`. The transport seam every check goes through.
McpCaller = Callable[[str, Mapping[str, Any], str | None], McpResult]

#: A decoded JSON response of any shape (a batch reply is an array), or a `str` transport failure.
RawResult = Any

#: `(raw JSON-RPC body text, token) -> RawResult`. The seam for a request `McpCaller` cannot build.
RawCaller = Callable[[str, str | None], RawResult]

#: `rep_id` a caller-supplied, forged `_identity` claims in the discard check.
_FORGED_REP_ID = "attacker-forged-rep"

#: Account the smoke-run token's verified identity is expected to own (in-book).
_IN_BOOK_ACCOUNT = "smoke-account-in-book"

#: Account the smoke-run token's verified identity is expected NOT to own (out-of-book).
_OUT_OF_BOOK_ACCOUNT = "smoke-account-out-of-book"

_IDENTITY_DISCARD_TOOL_NAME = "get_account"

# AgentCore Gateway advertises every tool as `${target_name}___${tool_name}`
# (docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-tool-naming.html). The inventory
# comparison strips the prefix, and every `tools/call` sends the prefixed name.
_TARGET_NAME_DELIMITER = "___"

# JSON-Schema keywords `crm.toolspec.definitions` declares that AgentCore's `SchemaDefinition`
# structure has no field for, so they are absent from every deployed Gateway tool schema by
# construction. See `_check_unexpressible_constraints`.
_UNEXPRESSIBLE_SCHEMA_KEYWORDS = (
    "additionalProperties",
    "enum",
    "maximum",
    "minLength",
    "minimum",
    "pattern",
)

# The subset of `crm.errors`'s closed taxonomy that represents a tool answering from its own domain
# logic. `authentication_failed` is excluded: identity resolution failed, so the tool was never
# exercised.
_TYPED_DOMAIN_ERROR_CODES = frozenset(
    {
        ScopeDenied.code,
        NotFound.code,
        ValidationFailed.code,
        Conflict.code,
    }
)


def _toolspec_name_of(mcp_name: str) -> str:
    """Return the toolspec tool name behind an MCP-visible, target-prefixed Gateway tool name.

    Splits on the last occurrence of the documented `___` delimiter, so a tool name that itself
    contains no delimiter (an injected offline stub, or a non-Gateway MCP server) is returned
    unchanged rather than mangled.
    """
    _, _, suffix = mcp_name.rpartition(_TARGET_NAME_DELIMITER)
    return suffix or mcp_name


def _mcp_name_for(mcp_names: Mapping[str, str], tool: ToolDefinition) -> str:
    """Return the name to send in `tools/call` for `tool`, falling back to its toolspec name."""
    return mcp_names.get(tool.name, tool.name)


def default_mcp_caller(
    method: str, params: Mapping[str, Any], token: str | None, *, gateway_url: str = ""
) -> McpResult:
    """Perform one MCP JSON-RPC call over HTTP POST using only `urllib.request`.

    Returns the decoded JSON-RPC response body on any HTTP response, including one carrying a
    JSON-RPC `error` member. Returns a `str` describing the failure only when no HTTP response
    was received at all (connection refused, timeout, malformed JSON body).

    Args:
        gateway_url: The live Gateway MCP URL (the bare host from the pinned
            `agentcore_gateway_url` Terraform output; the `/mcp` path is appended here per the
            AgentCore Gateway MCP protocol contract). `checks()` binds this via `functools.partial`.
            Falls back to `CRM_SMOKE_GATEWAY_URL` when not supplied.
    """
    base_url = gateway_url or os.environ.get("CRM_SMOKE_GATEWAY_URL", "")
    url = base_url.rstrip("/") + "/mcp" if base_url else base_url
    body = json.dumps({"jsonrpc": "2.0", "id": "smoke-1", "method": method, "params": dict(params)})
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310 - internal seam, no untrusted URL
        url, data=body.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with open_https(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return f"transport error calling {url}: {exc}"

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return f"response body is not valid JSON: {raw!r}"
    if not isinstance(decoded, dict):
        return f"response body is not a JSON object: {decoded!r}"
    return decoded


def default_raw_caller(body: str, token: str | None, *, gateway_url: str = "") -> RawResult:
    """POST `body` verbatim to the Gateway's `/mcp` endpoint and return the decoded JSON reply.

    Returns the decoded JSON of any shape on any HTTP response, or a `str` when no response was
    received or the body is not JSON. `gateway_url` falls back to `CRM_SMOKE_GATEWAY_URL`.
    """
    base_url = gateway_url or os.environ.get("CRM_SMOKE_GATEWAY_URL", "")
    url = base_url.rstrip("/") + "/mcp" if base_url else base_url
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310 - internal seam, no untrusted URL
        url, data=body.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with open_https(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return f"transport error calling {url}: {exc}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return f"response body is not valid JSON: {raw!r}"


def _token_from_env_or_flag(argv: Sequence[str] | None) -> str | None:
    """Resolve the Gateway MACHINE token: `--gateway-token` -> `CRM_SMOKE_GATEWAY_TOKEN` -> `None`.

    Not spelled `--token`: the Gateway admits only a `client_credentials` access token with the
    `gateway/invoke` scope, and `scripts/verify_deployment.sh` passes its user token to every module
    declaring `--token`.

    Never mints, signs, or fetches a token. Parsed out of `argv` ahead of `run_main`, and declared to
    `run_main` via `extra_arguments`.
    """
    args = list(argv) if argv is not None else sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--gateway-token" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--gateway-token="):
            return arg.split("=", 1)[1]
    return os.environ.get("CRM_SMOKE_GATEWAY_TOKEN")


def _expired_token_from_env_or_flag(argv: Sequence[str] | None) -> str | None:
    """Resolve a deliberately expired token for the expiry-refusal check.

    `--expired-token` flag -> `CRM_SMOKE_EXPIRED_TOKEN` env -> `None` (minted on the provisioned
    path; with `--gateway-token` nothing is minted and the check fails).
    """
    args = list(argv) if argv is not None else sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--expired-token" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--expired-token="):
            return arg.split("=", 1)[1]
    return os.environ.get("CRM_SMOKE_EXPIRED_TOKEN")


def _underscoped_token_from_env_or_flag(argv: Sequence[str] | None) -> str | None:
    """Resolve a token missing a required scope for the underscoped-refusal check.

    `--underscoped-token` flag -> `CRM_SMOKE_UNDERSCOPED_TOKEN` env -> `None` (minted on the
    provisioned path; with `--gateway-token` nothing is minted and the check fails).
    """
    args = list(argv) if argv is not None else sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--underscoped-token" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--underscoped-token="):
            return arg.split("=", 1)[1]
    return os.environ.get("CRM_SMOKE_UNDERSCOPED_TOKEN")


def _in_book_account_from_env_or_flag(argv: Sequence[str] | None) -> str:
    """Resolve the in-book account id: `--in-book-account` flag -> env -> the built-in default."""
    args = list(argv) if argv is not None else sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--in-book-account" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--in-book-account="):
            return arg.split("=", 1)[1]
    return os.environ.get("CRM_SMOKE_IN_BOOK_ACCOUNT") or _IN_BOOK_ACCOUNT


def _out_of_book_account_from_env_or_flag(argv: Sequence[str] | None) -> str:
    """Resolve the out-of-book account id: `--out-of-book-account` flag -> env -> the default."""
    args = list(argv) if argv is not None else sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--out-of-book-account" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--out-of-book-account="):
            return arg.split("=", 1)[1]
    return os.environ.get("CRM_SMOKE_OUT_OF_BOOK_ACCOUNT") or _OUT_OF_BOOK_ACCOUNT


def _placeholder_for(name: str, schema: Mapping[str, Any]) -> Any:
    """Return a schema-satisfying placeholder value for one required tool-input property."""
    if name == "transaction_key":
        return "smoke-txn-key"
    schema_type = schema.get("type")
    enum = schema.get("enum")
    if enum:
        return enum[0]
    if schema_type == "number":
        return 0
    if schema_type == "integer":
        return 1
    if schema_type == "boolean":
        return False
    if schema_type == "object":
        return {}
    return f"smoke-{name}"


def _arguments_for(tool: ToolDefinition) -> dict[str, Any]:
    """Build minimal, schema-satisfying arguments for `tool`, excluding any identity channel key."""
    required = tool.input_schema.get("required", [])
    properties = tool.input_schema.get("properties", {})
    return {name: _placeholder_for(name, properties.get(name, {})) for name in required}


def _rpc_error(result: McpResult) -> Mapping[str, Any] | None:
    """Return the JSON-RPC `error` member of `result`, or `None` when it is not error-shaped."""
    if isinstance(result, str):
        return None
    error_block = result.get("error")
    return error_block if isinstance(error_block, Mapping) else None


def _rpc_error_code(result: McpResult) -> str | None:
    """Return the top-level JSON-RPC `error.code`, or `None` when absent or non-string."""
    rpc_error = _rpc_error(result)
    if rpc_error is None:
        return None
    code = rpc_error.get("code")
    return code if isinstance(code, str) else None


def _content_envelope(result: McpResult) -> Mapping[str, Any] | None:
    """Return the action Lambda's `{"statusCode", ...}` envelope carried in the result's content.

    A Gateway Lambda target's response is handed back as MCP text content, so an action Lambda's
    own `{"statusCode": 401, "error": {...}}` envelope arrives as a JSON string inside
    `result.content[0].text`, with `result.isError` left `false`:
    `{"isError": false, "content": [{"text": "{\\"statusCode\\":401,...}"}]}`.
    """
    if isinstance(result, str):
        return None
    outer = result.get("result")
    if not isinstance(outer, Mapping):
        return None
    content = outer.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, Mapping):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, Mapping) and "statusCode" in parsed:
            return parsed
    return None


def _tool_error_code(result: McpResult) -> str | None:
    """Return a CRM taxonomy `code` embedded in a `tools/call` result's error content, if any.

    A denied tool call surfaces as `{"result": {"isError": true, "content": [...]}}` per
    `crm.errors.CrmError.to_mcp_error`, not as a top-level JSON-RPC `error` — this looks inside
    that shape for the taxonomy code an action Lambda response carries, and, failing that, inside
    the `{"statusCode", "error"}` envelope the Lambda returns as MCP text content
    (`_content_envelope`).
    """
    if isinstance(result, str):
        return None
    outer = result.get("result")
    if not isinstance(outer, Mapping):
        return None
    error_block = outer.get("error")
    if isinstance(error_block, Mapping):
        code = error_block.get("code")
        if code is not None:
            return str(code)
    envelope = _content_envelope(result)
    if envelope is not None:
        envelope_error = envelope.get("error")
        if isinstance(envelope_error, Mapping):
            code = envelope_error.get("code")
            if code is not None:
                return str(code)
    return None


def _leaks_internal_detail(result: McpResult) -> bool:
    """Return whether `result` carries any `detail` key anywhere a user-safe body must not.

    Checked at the top level, inside a JSON-RPC `error`, and inside a nested tool-call error
    block, since a refusal can be shaped as any of the three depending on where it originates.
    """
    if isinstance(result, str):
        return False
    if "detail" in result:
        return True
    for key in ("error", "result"):
        block = result.get(key)
        if isinstance(block, Mapping):
            if "detail" in block:
                return True
            inner_error = block.get("error")
            if isinstance(inner_error, Mapping) and "detail" in inner_error:
                return True
    return False


def _check_inventory(
    tools_list_result: McpResult,
) -> tuple[list[CheckResult], dict[str, str]]:
    """Compare a `tools/list` result against `ALL_TOOLS`: no extra, no missing, schemas match.

    Returns the checks plus a mapping from each toolspec tool name to the MCP-visible name the
    Gateway advertises for it, which every subsequent `tools/call` must use (see
    `_toolspec_name_of`).
    """
    checks: list[CheckResult] = []
    if isinstance(tools_list_result, str):
        checks.append(
            CheckResult(
                resource=RESOURCE_CLASS,
                check="tools_list_reachable",
                status="fail",
                detail=tools_list_result,
            )
        )
        return checks, {}

    outer = tools_list_result.get("result")
    deployed_tools_raw = outer.get("tools") if isinstance(outer, Mapping) else None
    if not isinstance(deployed_tools_raw, list):
        checks.append(
            CheckResult(
                resource=RESOURCE_CLASS,
                check="tools_list_reachable",
                status="fail",
                detail=f"tools/list result carries no tools array: {tools_list_result!r}",
            )
        )
        return checks, {}

    deployed_tools: dict[str, Mapping[str, Any]] = {}
    mcp_names: dict[str, str] = {}
    for entry in deployed_tools_raw:
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
            mcp_name = entry["name"]
            toolspec_name = _toolspec_name_of(mcp_name)
            deployed_tools[toolspec_name] = entry
            mcp_names[toolspec_name] = mcp_name

    checks.append(
        CheckResult(
            resource=RESOURCE_CLASS,
            check="tools_list_reachable",
            status="pass",
            detail=f"{len(deployed_tools)} tool(s) listed",
        )
    )

    toolspec_names = {tool.name for tool in ALL_TOOLS}
    deployed_names = set(deployed_tools)

    missing = sorted(toolspec_names - deployed_names)
    checks.append(
        CheckResult(
            resource=RESOURCE_CLASS,
            check="no_missing_tool",
            status="pass" if not missing else "fail",
            detail=f"missing from deployed inventory: {missing}" if missing else "",
        )
    )

    extra = sorted(deployed_names - toolspec_names)
    checks.append(
        CheckResult(
            resource=RESOURCE_CLASS,
            check="no_extra_tool",
            status="pass" if not extra else "fail",
            detail=f"deployed tool(s) absent from toolspec: {extra}" if extra else "",
        )
    )

    for tool in ALL_TOOLS:
        resource = f"tool:{tool.name}"
        deployed = deployed_tools.get(tool.name)
        checks.append(_check_tool_description(tool, deployed))
        checks.append(_check_property_descriptions(tool, deployed))
        if deployed is None:
            checks.append(
                CheckResult(
                    resource=resource,
                    check="schema_matches",
                    status="fail",
                    detail="tool not present in deployed inventory",
                )
            )
            continue
        deployed_schema = deployed.get("inputSchema")
        divergences = _schema_divergences(deployed_schema, tool.input_schema)
        if divergences:
            checks.append(
                CheckResult(
                    resource=resource,
                    check="schema_matches",
                    status="fail",
                    detail=(
                        f"deployed inputSchema diverges from toolspec: {'; '.join(divergences)} "
                        f"(deployed={deployed_schema!r} toolspec={tool.input_schema!r})"
                    ),
                )
            )
        else:
            checks.append(CheckResult(resource=resource, check="schema_matches", status="pass"))

    checks.append(_check_unexpressible_constraints())

    return checks, mcp_names


def _check_tool_description(
    tool: ToolDefinition, deployed: Mapping[str, Any] | None
) -> CheckResult:
    """Compare the deployed tool's top-level `description` with the toolspec's, byte for byte.

    Deferred, never passed, when the tool is absent from `tools/list`.
    """
    resource = f"tool:{tool.name}"
    check = "description_matches_toolspec"
    if deployed is None:
        return CheckResult(
            resource=resource,
            check=check,
            status="defer",
            defer_reason="deployed_tool_absent",
            detail="the tool is absent from tools/list, so its description cannot be read",
        )
    if "description" not in deployed:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail="the deployed tool carries no description",
        )
    if deployed["description"] != tool.description:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail="the deployed description differs from the toolspec",
        )
    return CheckResult(resource=resource, check=check, status="pass")


def _check_property_descriptions(
    tool: ToolDefinition, deployed: Mapping[str, Any] | None
) -> CheckResult:
    """Compare each deployed property `description` with the toolspec's, byte for byte.

    Covers every property present in both schemas; a description on one side only is a mismatch.
    Deferred, never passed, when the deployed tool or its property map cannot be read, or when no
    property is shared to compare.
    """
    resource = f"tool:{tool.name}"
    check = "property_descriptions_match_toolspec"
    schema = deployed.get("inputSchema") if deployed is not None else None
    deployed_props = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(deployed_props, Mapping):
        return CheckResult(
            resource=resource,
            check=check,
            status="defer",
            defer_reason="deployed_properties_unreadable",
            detail="the deployed tool or its inputSchema.properties is absent from tools/list",
        )
    toolspec_props = tool.input_schema.get("properties") or {}
    shared = sorted(set(deployed_props) & set(toolspec_props))
    if not shared:
        return CheckResult(
            resource=resource,
            check=check,
            status="defer",
            defer_reason="no_shared_property",
            detail="no property is declared by both the deployed schema and the toolspec",
        )
    drifted: list[str] = []
    missing: list[str] = []
    unexpected: list[str] = []
    for name in shared:
        want = toolspec_props[name].get("description")
        got_prop = deployed_props[name]
        got = got_prop.get("description") if isinstance(got_prop, Mapping) else None
        if want == got:
            continue
        if got is None:
            missing.append(name)
        elif want is None:
            unexpected.append(name)
        else:
            drifted.append(name)
    reasons = [
        f"{label}: {names}"
        for label, names in (
            ("description differs from toolspec", drifted),
            ("description missing from deployed property", missing),
            ("deployed description the toolspec does not declare", unexpected),
        )
        if names
    ]
    if reasons:
        return CheckResult(resource=resource, check=check, status="fail", detail="; ".join(reasons))
    described = sum(1 for name in shared if toolspec_props[name].get("description") is not None)
    return CheckResult(
        resource=resource,
        check=check,
        status="pass",
        detail=f"{len(shared)} shared property(ies), {described} with a description",
    )


def _schema_divergences(deployed: Any, toolspec: Mapping[str, Any]) -> list[str]:
    """Return every divergence between a deployed and a toolspec input schema, as prose reasons.

    Compares the whole of what AgentCore's `SchemaDefinition` structure is able to represent —
    the schema `type`, the exact property-name set, each property's `type`, and the exact
    `required` set — and reports each mismatch separately so the failure names what is wrong
    rather than dumping two schemas. Keywords the structure cannot carry
    (`_UNEXPRESSIBLE_SCHEMA_KEYWORDS`) are reported by `_check_unexpressible_constraints`.
    """
    if not isinstance(deployed, Mapping):
        return [f"deployed inputSchema is not an object: {deployed!r}"]

    reasons: list[str] = []
    if deployed.get("type") != toolspec.get("type"):
        reasons.append(f"type {deployed.get('type')!r} != toolspec {toolspec.get('type')!r}")

    deployed_props = deployed.get("properties")
    toolspec_props = toolspec.get("properties") or {}
    deployed_props = deployed_props if isinstance(deployed_props, Mapping) else {}
    if set(deployed_props) != set(toolspec_props):
        missing = sorted(set(toolspec_props) - set(deployed_props))
        extra = sorted(set(deployed_props) - set(toolspec_props))
        if missing:
            reasons.append(f"properties missing from deployed schema: {missing}")
        if extra:
            reasons.append(f"deployed schema declares properties absent from toolspec: {extra}")

    for name in sorted(set(deployed_props) & set(toolspec_props)):
        want = toolspec_props[name]
        got = deployed_props[name]
        want_type = want.get("type") if isinstance(want, Mapping) else None
        got_type = got.get("type") if isinstance(got, Mapping) else None
        if want_type != got_type:
            reasons.append(f"property {name!r} type {got_type!r} != toolspec {want_type!r}")

    deployed_required = deployed.get("required") or []
    toolspec_required = toolspec.get("required") or []
    if set(deployed_required) != set(toolspec_required):
        reasons.append(
            f"required {sorted(set(deployed_required))} != toolspec {sorted(set(toolspec_required))}"
        )
    return reasons


def _check_unexpressible_constraints() -> CheckResult:
    """Report, as an explicit `skip`, the toolspec constraints the Gateway cannot advertise.

    AgentCore's `SchemaDefinition` structure carries only
    `{type, description, properties, required, items}`
    (docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_SchemaDefinition.html),
    so a toolspec `minLength`, `enum`, `minimum`, `maximum` or `additionalProperties` cannot appear
    in the deployed schema. They are named and counted here as a `skip`; the handlers enforce them
    via `crm.toolspec.validation.validate_arguments`, verified offline by
    `check_toolspec_parity.check_handler_validator_coverage`. Counts are of declared instances,
    including permissive nested `additionalProperties: true` on the open `fields`/`after_payload`
    objects.
    """
    counts: dict[str, int] = {}
    for tool in ALL_TOOLS:
        schema = tool.input_schema
        for keyword in _UNEXPRESSIBLE_SCHEMA_KEYWORDS:
            if keyword in schema:
                counts[keyword] = counts.get(keyword, 0) + 1
        for prop in (schema.get("properties") or {}).values():
            if not isinstance(prop, Mapping):
                continue
            for keyword in _UNEXPRESSIBLE_SCHEMA_KEYWORDS:
                if keyword in prop:
                    counts[keyword] = counts.get(keyword, 0) + 1
    summary = ", ".join(f"{k}={counts[k]}" for k in sorted(counts)) or "none declared"
    return CheckResult(
        resource=RESOURCE_CLASS,
        check="schema_constraint_keywords_unexpressible",
        status="skip",
        detail=(
            "AgentCore SchemaDefinition carries only {type, description, properties, required, "
            f"items}}, so these toolspec constraints cannot be advertised over MCP: {summary}. "
            "Not asserted here because no correct Gateway target could satisfy it. Every "
            "restrictive instance is enforced one layer in by "
            "crm.toolspec.validation.validate_arguments, which every action Lambda's source in "
            "this repository applies in run_handler before its handler body runs, and that "
            "enforcement is proved offline against that source by executing one violating payload "
            "per instance in check_toolspec_parity.check_handler_validator_coverage. Read to its "
            "boundary: that is a statement about the code in this repository, not about the code "
            "currently running in the deployed functions -- no layer of this project's "
            "verification has queried a deployed action Lambda's package for it. This check is "
            "therefore recorded skip rather than as a verified control, and the enforcement it "
            "describes must not be read as live. The additionalProperties count "
            "above also includes permissive nested `true` instances, which restrict nothing."
        ),
    )


def _check_tool_invocation(
    caller: McpCaller,
    tool: ToolDefinition,
    token: str | None,
    *,
    mcp_names: Mapping[str, str],
) -> CheckResult:
    """Assert one tool can be individually invoked over MCP with the supplied token."""
    resource = f"tool:{tool.name}"
    if token is None:
        return CheckResult(
            resource=resource,
            check="invoke",
            status="fail",
            detail="no --gateway-token/CRM_SMOKE_GATEWAY_TOKEN supplied; an authorization boundary that was "
            "never observed is not a passing one",
        )

    result = caller(
        "tools/call",
        {"name": _mcp_name_for(mcp_names, tool), "arguments": _arguments_for(tool)},
        token,
    )
    if isinstance(result, str):
        return CheckResult(resource=resource, check="invoke", status="fail", detail=result)

    rpc_error = _rpc_error(result)
    if rpc_error is not None:
        return CheckResult(
            resource=resource,
            check="invoke",
            status="fail",
            detail=f"unexpected JSON-RPC error: {rpc_error!r}",
        )
    # A Gateway tool failure is reported inside a JSON-RPC 200 as `result.isError`, not as a JSON-RPC
    # `error` member. Placeholder arguments name no real record, so a typed domain answer such as
    # `scope_denied` or `not_found` passes; an untyped error or `authentication_failed` fails.
    if not _is_success(result):
        code = _tool_error_code(result) or _rpc_error_code(result)
        if code is None:
            return CheckResult(
                resource=resource,
                check="invoke",
                status="fail",
                detail=(
                    "tools/call failed with an untyped error, so the call never reached the "
                    f"tool's own handler: {result!r}"
                ),
            )
        if code == "authentication_failed":
            return CheckResult(
                resource=resource,
                check="invoke",
                status="fail",
                detail=(
                    "tools/call was refused with authentication_failed, so identity resolution "
                    f"failed and the tool itself was never exercised: {result!r}"
                ),
            )
        if code not in _TYPED_DOMAIN_ERROR_CODES:
            return CheckResult(
                resource=resource,
                check="invoke",
                status="fail",
                detail=(
                    f"tools/call failed with error code {code!r}, which is not in the closed "
                    f"crm.errors taxonomy {sorted(_TYPED_DOMAIN_ERROR_CODES)}: {result!r}"
                ),
            )
    if _leaks_internal_detail(result):
        return CheckResult(
            resource=resource,
            check="invoke",
            status="fail",
            detail=f"response leaks an internal 'detail' key: {result!r}",
        )
    return CheckResult(resource=resource, check="invoke", status="pass")


def _identity_discard_tool() -> ToolDefinition:
    """Return the toolspec tool the identity-discard check invokes: `get_account`.

    A fixed, well-known `read`-risk tool with clear book-of-accounts scope semantics
    (`crm.authz.require_scope`), looked up by name in `ALL_TOOLS`.
    """
    return BY_NAME[_IDENTITY_DISCARD_TOOL_NAME]


def _call_identity_discard_tool(
    caller: McpCaller,
    account_id: str,
    token: str | None,
    *,
    forged_identity: Mapping[str, Any],
    mcp_names: Mapping[str, str],
) -> McpResult:
    """Invoke the identity-discard tool for `account_id` with a caller-supplied `_identity`."""
    tool = _identity_discard_tool()
    arguments = {
        **_arguments_for(tool),
        "account_id": account_id,
        "_identity": dict(forged_identity),
    }
    return caller(
        "tools/call", {"name": _mcp_name_for(mcp_names, tool), "arguments": arguments}, token
    )


#: `result` members that carry a tool call's output.
_TOOL_OUTPUT_KEYS: Final[frozenset[str]] = frozenset({"content", "structuredContent"})

#: `defer_reason` for the no-invocation row: no per-request signal ties a Lambda invocation to the
#: array-body request.
_NO_INVOCATION_SIGNAL_DEFER_REASON: Final[str] = "no_per_request_invocation_signal"


def _array_body(mcp_names: Mapping[str, str], account_id: str) -> str:
    """A JSON-RPC array body holding one well-formed `get_account` `tools/call`."""
    tool = _identity_discard_tool()
    call = {
        "jsonrpc": "2.0",
        "id": "smoke-array-body",
        "method": "tools/call",
        "params": {
            "name": _mcp_name_for(mcp_names, tool),
            "arguments": {**_arguments_for(tool), "account_id": account_id},
        },
    }
    return json.dumps([call])


def _value_shape(value: object) -> str:
    """Name `value`'s JSON type; an object also lists its sorted keys, never its values."""
    if isinstance(value, Mapping):
        return "object{" + ",".join(sorted(str(key) for key in value)) + "}"
    if isinstance(value, list):
        return f"array[{len(value)}]"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    if value is None:
        return "null"
    return "string" if isinstance(value, str) else type(value).__name__


def _reply_shape(reply: RawResult) -> str:
    """Describe `reply`'s structure: per member, its keys, value types, error code and `isError`."""
    if isinstance(reply, str):
        return "no JSON reply"
    members = reply if isinstance(reply, list) else [reply]
    described: list[str] = []
    for member in members:
        if not isinstance(member, Mapping):
            described.append(_value_shape(member))
            continue
        fields = ", ".join(f"{key}:{_value_shape(member[key])}" for key in sorted(member))
        error = member.get("error")
        code = error.get("code") if isinstance(error, Mapping) else None
        suffix = f" error.code={code!r}" if code is not None else ""
        result = member.get("result")
        is_error = result.get("isError") if isinstance(result, Mapping) else None
        if isinstance(is_error, bool):
            suffix += f" result.isError={is_error}"
        described.append("{" + fields + "}" + suffix)
    prefix = f"array[{len(members)}] " if isinstance(reply, list) else ""
    return prefix + "; ".join(described)


def _array_body_problems(reply: RawResult) -> list[str]:
    """Every way `reply` shows a tool call answered, or cannot be judged; empty when neither.

    A member is a refusal when it carries a JSON-RPC `error` or a `result` whose `isError` is
    true; a `result` carrying tool output without `isError: true` is a tool call answered.
    """
    if isinstance(reply, str):
        return [reply]
    members = reply if isinstance(reply, list) else [reply]
    problems: list[str] = []
    for member in members:
        if not isinstance(member, Mapping):
            problems.append(f"reply member is not a JSON object: {_value_shape(member)}")
            continue
        result = member.get("result")
        if not isinstance(result, Mapping) or result.get("isError") is True:
            continue
        output = sorted(_TOOL_OUTPUT_KEYS & set(result))
        if output:
            problems.append(
                f"the reply's `result` carries tool-call output ({', '.join(output)}) without "
                "`isError: true`: a tool call was answered"
            )
    return problems


def _check_array_body_answers_no_tool_call(
    raw_caller: RawCaller, token: str | None, *, account_id: str, mcp_names: Mapping[str, str]
) -> list[CheckResult]:
    """Post a JSON-RPC array body with a valid token; assert no tool call is answered.

    Emits `array_body_answers_no_tool_call` and the deferred `array_body_reaches_no_tool`.
    """
    check = "array_body_answers_no_tool_call"
    if token is None:
        refused = CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="fail",
            detail="no access token, so the array body could not be sent with a valid one",
        )
    else:
        reply = raw_caller(_array_body(mcp_names, account_id), token)
        problems = _array_body_problems(reply)
        shape = f"reply shape: {_reply_shape(reply)}"
        refused = CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="fail" if problems else "pass",
            detail="; ".join([*problems, shape])
            if problems
            else (
                "a JSON-RPC array body carrying a valid get_account tools/call was answered with "
                f"no tool-call output; {shape}"
            ),
        )
    no_tool = CheckResult(
        resource=RESOURCE_CLASS,
        check="array_body_reaches_no_tool",
        status=DEFER,
        defer_reason=_NO_INVOCATION_SIGNAL_DEFER_REASON,
        detail=(
            "NOT VERIFIED: no signal outside the account's logs and metrics ties one Lambda "
            "invocation to this request, and other checks invoke the same tool. The in-band "
            "evidence is array_body_answers_no_tool_call's reply carrying no tool-call output"
        ),
    )
    return [refused, no_tool]


def _is_success(result: McpResult) -> bool:
    """Return whether `result` is a successful (non-error) `tools/call` outcome."""
    if isinstance(result, str):
        return False
    if _rpc_error(result) is not None:
        return False
    outer = result.get("result")
    if isinstance(outer, Mapping) and outer.get("isError"):
        return False
    # Success is read from the Lambda's own envelope, not `isError` alone (see `_content_envelope`).
    envelope = _content_envelope(result)
    if envelope is not None:
        status = envelope.get("statusCode")
        if isinstance(status, bool) or not isinstance(status, int):
            return False
        if status >= 400:
            return False
    return outer is not None


def _check_identity_discard(
    caller: McpCaller,
    token: str | None,
    *,
    in_book_account: str,
    out_of_book_account: str,
    mcp_names: Mapping[str, str],
) -> CheckResult:
    """Assert a caller-supplied `_identity` in `params.arguments` is discarded.

    Observes the allow/deny outcome of three forgeries against the token's own verified identity:
    widening to the out-of-book account directly, widening via `full_visibility`, and narrowing to an
    empty book. The first two must still be denied; the third must still succeed. Any other outcome
    is a `fail`.
    """
    resource = RESOURCE_CLASS
    check_name = "identity_discard"
    if token is None:
        return CheckResult(
            resource=resource,
            check=check_name,
            status="fail",
            detail="no --gateway-token/CRM_SMOKE_GATEWAY_TOKEN supplied; an authorization boundary that was "
            "never observed is not a passing one",
        )

    widen_attempts: tuple[Mapping[str, Any], ...] = (
        {
            "rep_id": _FORGED_REP_ID,
            "book": [out_of_book_account],
            "full_visibility": False,
            "principal_type": "user",
            "session_id": "forged-session-widen-book",
            "source": "gateway",
        },
        {
            "rep_id": _FORGED_REP_ID,
            "book": [],
            "full_visibility": True,
            "principal_type": "user",
            "session_id": "forged-session-widen-visibility",
            "source": "gateway",
        },
    )
    for forged in widen_attempts:
        result = _call_identity_discard_tool(
            caller, out_of_book_account, token, forged_identity=forged, mcp_names=mcp_names
        )
        if isinstance(result, str):
            return CheckResult(resource=resource, check=check_name, status="fail", detail=result)
        if _is_success(result):
            return CheckResult(
                resource=resource,
                check=check_name,
                status="fail",
                detail=(
                    f"a caller-supplied _identity ({forged!r}) widened scope enough to access "
                    f"out-of-book account {out_of_book_account!r} - the Gateway REQUEST "
                    "interceptor is merging rather than replacing _identity"
                ),
            )
        if _tool_error_code(result) != "scope_denied" and _rpc_error_code(result) != "scope_denied":
            return CheckResult(
                resource=resource,
                check=check_name,
                status="fail",
                detail=(
                    "cannot prove the widen attempt was denied for the expected reason (no "
                    f"scope_denied code observed): {result!r}"
                ),
            )
        if _leaks_internal_detail(result):
            return CheckResult(
                resource=resource,
                check=check_name,
                status="fail",
                detail=f"denial to a widen attempt leaks internal detail: {result!r}",
            )

    narrow_forged = {
        "rep_id": _FORGED_REP_ID,
        "book": [],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "forged-session-narrow",
        "source": "gateway",
    }
    narrow_result = _call_identity_discard_tool(
        caller, in_book_account, token, forged_identity=narrow_forged, mcp_names=mcp_names
    )
    if isinstance(narrow_result, str):
        return CheckResult(resource=resource, check=check_name, status="fail", detail=narrow_result)
    if not _is_success(narrow_result):
        return CheckResult(
            resource=resource,
            check=check_name,
            status="fail",
            detail=(
                "a caller-supplied empty-book _identity narrowed scope enough to deny access to "
                f"the token's own in-book account {in_book_account!r} - the Gateway REQUEST "
                f"interceptor is merging rather than replacing _identity: {narrow_result!r}"
            ),
        )
    return CheckResult(resource=resource, check=check_name, status="pass")


def _check_refusal(
    caller: McpCaller,
    tool: ToolDefinition | None,
    token: str | None,
    *,
    check_name: str,
    reason: str,
    mcp_names: Mapping[str, str],
) -> CheckResult:
    """Assert a `tools/call` with `token` is refused with a user-safe `{code, message}` body only.

    `tool` selects which tool to call; falls back to the first toolspec tool when `None`. Used for
    both the expired-token and underscoped-token checks, which share identical assertions and
    differ only in which token is supplied.
    """
    resource = RESOURCE_CLASS
    if token is None:
        return CheckResult(
            resource=resource,
            check=check_name,
            status="fail",
            detail=f"no {reason} supplied; a refusal that was never observed is not a passing one",
        )

    target = tool if tool is not None else ALL_TOOLS[0]
    result = caller(
        "tools/call",
        {"name": _mcp_name_for(mcp_names, target), "arguments": _arguments_for(target)},
        token,
    )
    if isinstance(result, str):
        return CheckResult(resource=resource, check=check_name, status="fail", detail=result)

    rpc_error = _rpc_error(result)
    if rpc_error is None:
        return CheckResult(
            resource=resource,
            check=check_name,
            status="fail",
            detail=f"expected a JSON-RPC error refusing the call, got: {result!r}",
        )

    body_keys = set(rpc_error.keys())
    if body_keys - {"code", "message"}:
        return CheckResult(
            resource=resource,
            check=check_name,
            status="fail",
            detail=f"refusal body carries extra key(s) beyond code/message: {sorted(body_keys)}",
        )
    if _leaks_internal_detail(result):
        return CheckResult(
            resource=resource,
            check=check_name,
            status="fail",
            detail=f"refusal leaks an internal 'detail' key: {rpc_error!r}",
        )
    message = str(rpc_error.get("message", ""))
    if not message:
        return CheckResult(
            resource=resource,
            check=check_name,
            status="fail",
            detail="refusal carries no user-facing message",
        )
    return CheckResult(resource=resource, check=check_name, status="pass", detail=message)


def wait_out_token_lifetime(
    token: str,
    *,
    sleeper: Callable[[float], None],
    clock: Callable[[], float],
    verifier: ClaimsVerifier,
) -> None:
    """Sleep until `token` is past its own `exp` plus a clock-skew margin.

    `exp` is read through `verifier`. Returns immediately when the token has already expired, and
    never sleeps beyond `_MAX_EXPIRY_WAIT_SECONDS`. Never raises; a token failing verification is
    reported by the expiry checks.
    """
    try:
        exp = verifier(token).get("exp")
    except Exception:  # noqa: BLE001 - reported by the checks that read the same claims
        return
    if not isinstance(exp, int):
        return
    remaining = (float(exp) + _EXPIRY_MARGIN_SECONDS) - clock()
    if remaining <= 0:
        return
    sleeper(min(remaining, _MAX_EXPIRY_WAIT_SECONDS))


def token_genuinely_expired_check(
    token: str | None, *, now: float, resource: str, verifier: ClaimsVerifier
) -> CheckResult:
    """Assert the token presented to the expiry check is past its own `exp`.

    Requires a real issued token carrying `iat`/`exp`, whose claims `verifier` verifies with a
    leeway of `EXPIRED_TOKEN_MAX_AGE_SECONDS`; a token older than that fails with reason
    `older_than_verification_leeway`.
    """
    check = "expired_token_genuinely_expired"
    if token is None:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail="no token was minted for the expiry check",
        )
    try:
        claims = verifier(token)
    except ClaimsVerificationError as exc:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                "older_than_verification_leeway: the token is more than "
                f"{EXPIRED_TOKEN_MAX_AGE_SECONDS}s past its exp, so its signature cannot be "
                f"verified within the leeway ({exc})"
                if isinstance(exc.__cause__, jwt.ExpiredSignatureError)
                else f"the token's claims failed verification: {exc}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 - reported, never raised out of a check
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"could not verify the token's claims: {type(exc).__name__}: {exc}",
        )
    exp = claims.get("exp")
    iat = claims.get("iat")
    if not isinstance(exp, int) or not isinstance(iat, int):
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"token carries no integer iat/exp pair: iat={iat!r} exp={exp!r}",
        )
    age_past_exp = now - float(exp)
    if age_past_exp <= 0:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"the token is still valid for {-age_past_exp:.0f}s (exp={exp}); presenting it "
                "would test nothing about expiry"
            ),
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="pass",
        detail=(
            f"token lifetime exp-iat={exp - iat}s (Cognito's 5-minute floor), "
            f"presented {age_past_exp:.0f}s past its own exp; its signature, iss, token_use and "
            "client_id verified against the pool's JWKS"
        ),
    )


def underscoped_token_lacks_gateway_scope_check(
    token: str | None, *, resource: str, verifier: ClaimsVerifier
) -> CheckResult:
    """Assert the token presented to the underscoped check lacks `gateway/invoke`.

    The `scope` claim is read through `verifier`.
    """
    check = "underscoped_token_lacks_gateway_scope"
    if token is None:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail="no token was minted for the underscoped check",
        )
    try:
        claims = verifier(token)
    except Exception as exc:  # noqa: BLE001 - reported, never raised out of a check
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"could not verify the token's claims: {type(exc).__name__}: {exc}",
        )
    scopes = str(claims.get("scope") or "").split()
    if not scopes:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail="token carries no scope claim at all, so it is not a scoped access token",
        )
    offending = [entry for entry in scopes if entry.endswith(f"/{_GATEWAY_SCOPE_SUFFIX}")]
    if offending:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"token already carries the Gateway scope {offending!r}, so a refusal would not be "
                "attributable to an absent scope"
            ),
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="pass",
        detail=(
            f"token scope claim is {scopes!r}, which does not contain any "
            f"*/{_GATEWAY_SCOPE_SUFFIX} scope; token_use={claims.get('token_use')!r}"
        ),
    )


@dataclass(frozen=True, slots=True)
class ServicePrincipalFixture:
    """The Gateway service principal's live scope, and a freshly minted token for it.

    Attributes:
        rep_id: The `rep_id` the deployed interceptor is configured to act as, read from that
            Lambda's own `GATEWAY_SERVICE_PRINCIPAL_REP_ID` environment variable.
        tenant: The disposable `eval-smoke-<hex>` tenant the fixture corpus was seeded into.
        book: Account ids the seeded `reps` row grants, all of which genuinely exist in that tenant.
        in_book_account: One member of `book`.
        out_of_book_account: An account that exists in the same tenant but is not in `book`
            (`get_account` raises `NotFound` before it evaluates scope).
        token: A real `client_credentials` access token for the MCP app client.
    """

    rep_id: str
    tenant: str
    book: frozenset[str]
    in_book_account: str
    out_of_book_account: str
    token: str


class ServicePrincipalProvisioner(Protocol):
    """Seeds the service principal's live scope and mints its token, then removes everything."""

    def provision(self) -> ServicePrincipalFixture:
        """Return a fixture whose `reps` row, tenant, and token all genuinely exist."""

    def cleanup(self) -> Iterable[CheckResult]:
        """Remove everything `provision` created, yielding one check per removal."""


class LiveServicePrincipalProvisioner:
    """Seeds a disposable tenant and the Gateway service principal's book inside it.

    The tenant comes from `scripts.smoke.tenant.allocate_smoke_tenant`, and
    `_require_disposable_tenant` re-checks it before anything is written.

    The minted token is held only in memory and never written to a file, logged, or placed in any
    `CheckResult.detail`.
    """

    def __init__(self, ctx: SmokeContext) -> None:
        self._ctx = ctx
        self._smoke_tenant: SmokeTenant | None = None
        self._rep_id: str | None = None
        #: The principal's `reps` item as DynamoDB returned it before this run wrote, or `None`.
        self._prior_row: dict[str, Any] | None = None
        self._row_written = False

    def _store(self) -> Store:
        return Store(
            client=self._ctx.client("dynamodb"), table_prefix=_live_table_prefix(self._ctx)
        )

    def _configured_rep_id(self) -> str:
        """Return the deployed interceptor's configured service-principal `rep_id`.

        Read from the live Lambda's environment. An unconfigured interceptor raises.
        """
        function_name = f"{_live_table_prefix(self._ctx)}-identity-interceptor"
        configuration = self._ctx.client("lambda").get_function_configuration(
            FunctionName=function_name
        )
        variables = (configuration.get("Environment") or {}).get("Variables") or {}
        rep_id = str(variables.get(_SERVICE_PRINCIPAL_REP_ID_ENV) or "").strip()
        if not rep_id:
            raise RuntimeError(
                f"{function_name} carries no {_SERVICE_PRINCIPAL_REP_ID_ENV}, so no service "
                "principal is configured and every machine token is refused by design; there is "
                "nothing for this smoke run to authenticate as"
            )
        return rep_id

    def provision(self) -> ServicePrincipalFixture:
        """Allocate and seed a tenant, seed the service principal's book, and mint its token.

        Raises:
            RuntimeError: The interceptor has no configured service principal, an account the book
                would name was not actually seeded, or no token was issued.
        """
        rep_id = self._configured_rep_id()
        self._prior_row = self._read_prior_row(rep_id)
        self._rep_id = rep_id

        store = self._store()
        smoke_tenant = allocate_smoke_tenant(store)
        self._smoke_tenant = smoke_tenant
        _require_disposable_tenant(smoke_tenant.tenant)
        seed_smoke_tenant(store, smoke_tenant)

        book = frozenset(
            self._seeded_account(store, smoke_tenant.tenant, index)
            for index in range(_SERVICE_BOOK_SIZE)
        )
        out_of_book_account = self._seeded_account(
            store, smoke_tenant.tenant, _OUT_OF_BOOK_ACCOUNT_INDEX
        )
        if out_of_book_account in book:
            raise RuntimeError(
                f"out-of-book account {out_of_book_account!r} is inside the seeded book "
                f"{sorted(book)!r}; the scope assertion would be vacuous"
            )

        self._row_written = True
        store.put_item(
            "reps",
            {
                "rep_id": rep_id,
                "tenant": smoke_tenant.tenant,
                "book": sorted(book),
                "full_visibility": False,
                "name": "Layer 4 Gateway service principal smoke",
                "status": "active",
            },
        )

        return ServicePrincipalFixture(
            rep_id=rep_id,
            tenant=smoke_tenant.tenant,
            book=book,
            in_book_account=sorted(book)[0],
            out_of_book_account=out_of_book_account,
            token=self._mint_machine_token(),
        )

    def _reps_key(self, rep_id: str) -> dict[str, Any]:
        return {"TableName": self._store().physical_name("reps"), "Key": {"rep_id": {"S": rep_id}}}

    def _read_prior_row(self, rep_id: str) -> dict[str, Any] | None:
        """The principal's existing `reps` item, raw, by a consistent read; `None` when absent.

        Raises:
            RuntimeError: The read failed, so a restore could not be guaranteed; nothing is written.
        """
        try:
            response = self._ctx.client("dynamodb").get_item(
                **self._reps_key(rep_id), ConsistentRead=True
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as the refusal to proceed
            raise RuntimeError(
                f"could not read the existing reps row for {rep_id!r} ({type(exc).__name__}: "
                f"{exc}); refusing to overwrite a row this run could not restore"
            ) from exc
        item = response.get("Item")
        return dict(item) if isinstance(item, Mapping) else None

    def _seeded_account(self, store: Store, tenant: str, index: int) -> str:
        """Return the seeded account id at `index`, having read it back from DynamoDB first.

        The id is the fixture generator's own deterministic `acct-<tenant>-<index>` form, and its
        existence is confirmed before it is used.
        """
        account_id = f"acct-{tenant}-{index:04d}"
        if store.get_item("accounts", {"account_id": account_id}) is None:
            raise RuntimeError(
                f"seeded account {account_id!r} not found after seeding tenant {tenant!r}; "
                "refusing to assert scope against an account that does not exist"
            )
        return account_id

    def _mint_machine_token(self) -> str:
        """Mint a real `client_credentials` access token for the MCP app client.

        Thin instance-level wrapper over `mint_machine_token`.
        """
        return mint_machine_token(self._ctx)

    def cleanup(self) -> Iterable[CheckResult]:
        """Restore or delete the `reps` row and tear the tenant down, proving each by re-read.

        A row that existed before this run is written back exactly; otherwise the row this run
        wrote is deleted. Both steps are attempted even when the first failed, and each is
        reported as its own check.
        """
        yield from self._cleanup_reps_row()
        yield from self._cleanup_tenant()

    def _cleanup_reps_row(self) -> Iterable[CheckResult]:
        rep_id = self._rep_id
        if rep_id is None or not self._row_written:
            return
        if self._prior_row is not None:
            yield self._restore_prior_row(rep_id, self._prior_row)
            return
        try:
            store = self._store()
            store.delete_item("reps", {"rep_id": rep_id})
            remaining = store.get_item("reps", {"rep_id": rep_id})
            yield CheckResult(
                resource="service_principal",
                check="reps_row_deleted",
                status="pass" if remaining is None else "fail",
                detail=(
                    f"reps row for {rep_id!r} absent on read-back"
                    if remaining is None
                    else f"reps row for {rep_id!r} still present after delete_item"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
            yield CheckResult(
                resource="service_principal",
                check="reps_row_deleted",
                status="fail",
                detail=f"could not delete the seeded reps row: {type(exc).__name__}: {exc}",
            )

    def _restore_prior_row(self, rep_id: str, prior: dict[str, Any]) -> CheckResult:
        """Write the pre-run item back and confirm a consistent re-read returns it unchanged."""
        try:
            client = self._ctx.client("dynamodb")
            key = self._reps_key(rep_id)
            client.put_item(TableName=key["TableName"], Item=prior)
            restored = client.get_item(**key, ConsistentRead=True).get("Item")
            error = "" if restored == prior else "a consistent re-read differs from the snapshot"
        except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
            error = f"{type(exc).__name__}: {exc}"
        if not error:
            return CheckResult(
                resource="service_principal",
                check="reps_row_restored",
                status="pass",
                detail=f"the pre-existing reps row for {rep_id!r} was restored byte-identical",
            )
        return CheckResult(
            resource="service_principal",
            check="reps_row_restored",
            status="fail",
            detail=(
                f"could not restore the pre-existing reps row for {rep_id!r} ({error}); an "
                f"operator must rewrite it as: {_plain_item(prior)}"
            ),
        )

    def _cleanup_tenant(self) -> Iterable[CheckResult]:
        smoke_tenant = self._smoke_tenant
        if smoke_tenant is None:
            return
        try:
            deleted = teardown_smoke_tenant(self._store(), smoke_tenant)
            yield CheckResult(
                resource="smoke_tenant",
                check="tenant_left_clean",
                status="pass",
                detail=f"tenant {smoke_tenant.tenant!r} torn down ({sum(deleted.values())} items)",
            )
        except Exception as exc:  # noqa: BLE001 - a failed teardown must never read as clean
            yield CheckResult(
                resource="smoke_tenant",
                check="tenant_left_clean",
                status="fail",
                detail=f"teardown of {smoke_tenant.tenant!r} failed: {type(exc).__name__}: {exc}",
            )


def _plain_item(item: Mapping[str, Any]) -> str:
    """A DynamoDB-typed `reps` item as sorted JSON; the row carries no secret attribute."""
    deserializer = TypeDeserializer()
    plain = {name: deserializer.deserialize(value) for name, value in item.items()}
    return json.dumps(plain, sort_keys=True, default=str)


MCP_CLIENT_SECRET_FILE_ENV: Final[str] = "CRM_MCP_CLIENT_SECRET_FILE"  # noqa: S105 - an environment variable name, not a credential  # nosec B105  # pragma: allowlist secret


def mcp_client_secret(ctx: SmokeContext) -> str:
    """The MCP app client's secret, never logged.

    The stripped contents of the file named by `CRM_MCP_CLIENT_SECRET_FILE` when that variable is
    set, else the `cognito_mcp_client_secret` output.

    Raises:
        RuntimeError: The file is unreadable or the secret is empty or absent. The message names
            `CRM_MCP_CLIENT_SECRET_FILE` and never carries the value.
    """
    path = os.environ.get(MCP_CLIENT_SECRET_FILE_ENV)
    if path:
        try:
            secret = Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(
                f"{MCP_CLIENT_SECRET_FILE_ENV} names a file that cannot be read "
                f"({type(exc).__name__})"
            ) from None
        if not secret:
            raise RuntimeError(f"{MCP_CLIENT_SECRET_FILE_ENV} names an empty file")
        return secret
    value = ctx.outputs.get("cognito_mcp_client_secret")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            "terraform output 'cognito_mcp_client_secret' is absent or empty; set "
            f"{MCP_CLIENT_SECRET_FILE_ENV} to the file the rotation wrote to --secret-out"
        )
    return value


def mint_machine_token(ctx: SmokeContext) -> str:
    """Mint a real `client_credentials` access token for the MCP app client.

    The client secret comes from `mcp_client_secret`, is used for one HTTP Basic credential, and
    is never written anywhere. `urllib` only, and no AWS call.

    Raises:
        RuntimeError: The secret is unavailable (see `mcp_client_secret`) or the token endpoint
            returned no `access_token`.
    """
    token_endpoint = ctx.outputs.require_str("cognito_token_endpoint")
    client_id = ctx.outputs.require_str("cognito_mcp_client_id")
    client_secret = mcp_client_secret(ctx)
    scope = ctx.outputs.get("cognito_gateway_scope") or _GATEWAY_SCOPE

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": str(scope)}).encode(
        "utf-8"
    )
    request = urllib.request.Request(  # noqa: S310 - endpoint comes from Terraform outputs
        token_endpoint,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        method="POST",
    )
    try:
        with open_https(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"token endpoint refused the client_credentials request with HTTP {exc.code}"
        ) from None
    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    if not access_token:
        raise RuntimeError("token endpoint returned no access_token")
    return str(access_token)


def _require_disposable_tenant(tenant: str) -> None:
    """Raise unless `tenant` is a disposable smoke/eval tenant (never `demo`)."""
    if tenant == DEMO_TENANT or not tenant.startswith(_DISPOSABLE_TENANT_PREFIXES):
        raise RuntimeError(
            f"refusing to seed under tenant {tenant!r}: a smoke run must write only beneath a "
            f"disposable tenant named with one of {list(_DISPOSABLE_TENANT_PREFIXES)}"
        )


def _live_table_prefix(ctx: SmokeContext) -> str:
    """Derive the live DynamoDB table-name prefix from the `dynamodb_table_names` output.

    Mirrors `scripts/smoke/cognito_claims.py`, `scripts/smoke/lambda_actions.py`, and
    `scripts/smoke/api_routes.py`: the physical name for the `eval_runs` logical table is
    `<table_prefix>eval_runs`, so stripping that suffix recovers the prefix. The same prefix is
    `local.name_prefix`, so it also yields the interceptor Lambda's function name.
    """
    table_names = ctx.outputs.require_mapping("dynamodb_table_names")
    return str(table_names["eval_runs"]).removesuffix("eval_runs")


#: Refusal-check reasons naming where each token comes from.
_OPERATOR_EXPIRED_REASON: Final[str] = (
    "expired token (--expired-token/CRM_SMOKE_EXPIRED_TOKEN; minted only when no --gateway-token "
    "is supplied)"
)
_OPERATOR_UNDERSCOPED_REASON: Final[str] = (
    "underscoped token (--underscoped-token/CRM_SMOKE_UNDERSCOPED_TOKEN; minted only when no "
    "--gateway-token is supplied)"
)


def checks(
    ctx: SmokeContext,
    *,
    mcp_caller: McpCaller | None = None,
    token: str | None = None,
    expired_token: str | None = None,
    underscoped_token: str | None = None,
    in_book_account: str = _IN_BOOK_ACCOUNT,
    out_of_book_account: str = _OUT_OF_BOOK_ACCOUNT,
    provisioner: ServicePrincipalProvisioner | None = None,
    user_provisioner: LiveBareMfaUserProvisioner | None = None,
    expired_token_minter: Callable[[], str] | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    jwks_url: str | None = None,
    raw_caller: RawCaller | None = None,
) -> Iterable[CheckResult]:
    """Yield one `CheckResult` per (tool-or-resource-class, check) pair.

    Never raises for an individual check failure; every tool is checked even when an earlier
    tool's check already failed. `ctx.outputs` must carry `agentcore_gateway_url`.

    Args:
        ctx: The smoke context.
        mcp_caller: Transport seam override, injected by the offline test.
        token: An already-issued access token. When supplied it takes precedence over
            `provisioner`, and nothing is minted or provisioned.
        expired_token: A genuinely expired token, for `expired_token_refused`. When absent on the
            provisioned path, one is minted via `expired_token_minter` before any other work.
        underscoped_token: A token missing `gateway/invoke`, for `underscoped_token_refused`. When
            absent on the provisioned path, `user_provisioner` mints a real end-user access token.
        in_book_account: Account the acting identity is expected to own.
        out_of_book_account: Account the acting identity is expected not to own. Must exist in the
            same tenant.
        provisioner: Seeds a disposable tenant and the service principal's book within it, mints
            that principal's machine token, then removes everything again. The live entry point
            always supplies one.
        user_provisioner: Mints the underscoped user access token and deletes its user.
        expired_token_minter: Seam that mints the to-be-expired machine token.
        sleeper: Seam the expiry wait sleeps through.
        clock: Seam the expiry assertions read the current time from.
        jwks_url: JWKS endpoint the expired and underscoped tokens are verified against; the
            pool's by default.
        raw_caller: Raw-body transport seam override for the array-body check.
    """
    gateway_url = ctx.outputs.require_str("agentcore_gateway_url")
    caller = (
        mcp_caller
        if mcp_caller is not None
        else functools.partial(default_mcp_caller, gateway_url=gateway_url)
    )
    wait = sleeper if sleeper is not None else time.sleep
    now = clock if clock is not None else time.time
    verifiers = token_verifiers(ctx, jwks_url=jwks_url)
    raw = (
        raw_caller
        if raw_caller is not None
        else functools.partial(default_raw_caller, gateway_url=gateway_url)
    )

    if token is not None or provisioner is None:
        return _gateway_checks(
            caller,
            token=token,
            expired_token=expired_token,
            underscoped_token=underscoped_token,
            in_book_account=in_book_account,
            out_of_book_account=out_of_book_account,
            sleeper=wait,
            clock=now,
            verifiers=verifiers,
            raw_caller=raw,
        )

    # Minted before the tenant is seeded, so the run's own work ages it.
    aging_token = expired_token
    expired_reason = _OPERATOR_EXPIRED_REASON
    if aging_token is None and expired_token_minter is not None:
        try:
            aging_token = expired_token_minter()
        except Exception as exc:  # noqa: BLE001 - surfaced through the refusal check's detail
            expired_reason = (
                "expired token (minting the to-be-expired machine token failed: "
                f"{type(exc).__name__}: {exc})"
            )

    return _provisioned_checks(
        caller,
        provisioner,
        expired_token=aging_token,
        expired_reason=expired_reason,
        underscoped_token=underscoped_token,
        user_provisioner=user_provisioner if underscoped_token is None else None,
        sleeper=wait,
        clock=now,
        verifiers=verifiers,
        raw_caller=raw,
    )


def _provisioned_checks(
    caller: McpCaller,
    provisioner: ServicePrincipalProvisioner,
    *,
    expired_token: str | None,
    expired_reason: str,
    underscoped_token: str | None,
    user_provisioner: LiveBareMfaUserProvisioner | None,
    sleeper: Callable[[float], None],
    clock: Callable[[], float],
    verifiers: TokenVerifiers,
    raw_caller: RawCaller,
) -> list[CheckResult]:
    """Provision the underscoped user and the service principal's fixture, run every check, then
    always clean both up.

    Cleanup runs from a `finally` block, and each removal is reported as its own check.
    """
    results: list[CheckResult] = []
    underscoped_error = ""
    underscoped_auth_factor = ""
    try:
        if user_provisioner is not None:
            try:
                user: BareMfaUser = user_provisioner.provision()
                underscoped_token = user.access_token
                underscoped_auth_factor = user.auth_factor
            except Exception as exc:  # noqa: BLE001 - surfaced as one actionable failing check
                underscoped_error = (
                    "could not mint a real end-user access token for the underscoped-token "
                    f"check: {type(exc).__name__}: {exc}"
                )
        try:
            fixture = provisioner.provision()
        except Exception as exc:  # noqa: BLE001 - surfaced as one actionable failing check
            results.append(
                CheckResult(
                    resource=RESOURCE_CLASS,
                    check="provision_service_principal",
                    status="fail",
                    detail=(
                        "could not seed the service principal's book and mint its token: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
            )
            return results
        results.append(
            CheckResult(
                resource=RESOURCE_CLASS,
                check="provision_service_principal",
                status="pass",
                detail=(
                    f"rep_id={fixture.rep_id!r} tenant={fixture.tenant!r} "
                    f"book={sorted(fixture.book)!r} out_of_book={fixture.out_of_book_account!r}"
                ),
            )
        )
        if underscoped_error:
            results.append(
                CheckResult(
                    resource=RESOURCE_CLASS,
                    check="provision_underscoped_user",
                    status="fail",
                    detail=underscoped_error,
                )
            )
        elif underscoped_auth_factor:
            results.append(
                CheckResult(
                    resource=RESOURCE_CLASS,
                    check="provision_underscoped_user",
                    status="pass",
                    detail=(
                        f"one Cognito user created and signed in, {underscoped_auth_factor} (the "
                        "pool was never weakened to obtain the token); no DynamoDB row written "
                        "for it"
                    ),
                )
            )
        results.extend(
            _gateway_checks(
                caller,
                token=fixture.token,
                expired_token=expired_token,
                underscoped_token=underscoped_token,
                in_book_account=fixture.in_book_account,
                out_of_book_account=fixture.out_of_book_account,
                sleeper=sleeper,
                clock=clock,
                verifiers=verifiers,
                raw_caller=raw_caller,
                expired_reason=expired_reason,
                underscoped_reason=(
                    f"underscoped token ({underscoped_error})"
                    if underscoped_error
                    else _OPERATOR_UNDERSCOPED_REASON
                ),
            )
        )
    finally:
        if user_provisioner is not None:
            results.extend(user_provisioner.cleanup())
        results.extend(provisioner.cleanup())
    return results


def _gateway_checks(
    caller: McpCaller,
    *,
    token: str | None,
    expired_token: str | None,
    underscoped_token: str | None,
    in_book_account: str,
    out_of_book_account: str,
    sleeper: Callable[[float], None],
    clock: Callable[[], float],
    verifiers: TokenVerifiers,
    raw_caller: RawCaller,
    expired_reason: str = _OPERATOR_EXPIRED_REASON,
    underscoped_reason: str = _OPERATOR_UNDERSCOPED_REASON,
) -> list[CheckResult]:
    """Run the inventory, per-tool invoke, identity-discard, array-body and refusal checks, in order.

    The expired token is presented only after it has aged past its own `exp`, and each refusal is
    preceded by a premise check read from the token's own verified claims.
    """
    results: list[CheckResult] = []

    tools_list_result = caller("tools/list", {}, token)
    inventory_checks, mcp_names = _check_inventory(tools_list_result)
    results.extend(inventory_checks)

    for tool in ALL_TOOLS:
        results.append(_check_tool_invocation(caller, tool, token, mcp_names=mcp_names))

    results.append(
        _check_identity_discard(
            caller,
            token,
            in_book_account=in_book_account,
            out_of_book_account=out_of_book_account,
            mcp_names=mcp_names,
        )
    )
    results.extend(
        _check_array_body_answers_no_tool_call(
            raw_caller, token, account_id=in_book_account, mcp_names=mcp_names
        )
    )

    if expired_token is not None:
        wait_out_token_lifetime(
            expired_token, sleeper=sleeper, clock=clock, verifier=verifiers.expired
        )
    results.append(
        token_genuinely_expired_check(
            expired_token, now=clock(), resource=RESOURCE_CLASS, verifier=verifiers.expired
        )
    )
    results.append(
        _check_refusal(
            caller,
            None,
            expired_token,
            check_name="expired_token_refused",
            reason=expired_reason,
            mcp_names=mcp_names,
        )
    )
    results.append(
        underscoped_token_lacks_gateway_scope_check(
            underscoped_token, resource=RESOURCE_CLASS, verifier=verifiers.underscoped
        )
    )
    results.append(
        _check_refusal(
            caller,
            None,
            underscoped_token,
            check_name="underscoped_token_refused",
            reason=underscoped_reason,
            mcp_names=mcp_names,
        )
    )

    return results


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: resolve tokens from flags/env and run every Gateway-tool smoke check.

    When no `--gateway-token`/`CRM_SMOKE_GATEWAY_TOKEN` is supplied, a
    `LiveServicePrincipalProvisioner` seeds a disposable tenant and the service principal's book,
    mints that principal's `client_credentials` token, and removes what it created afterwards. The
    expired token is a second machine token minted first and aged past its `exp`; the underscoped
    token is a real end-user access token from `LiveBareMfaUserProvisioner`, whose user is deleted
    afterwards. Either is taken from its flag/env instead when supplied.
    """
    token = _token_from_env_or_flag(argv)
    expired_token = _expired_token_from_env_or_flag(argv)
    underscoped_token = _underscoped_token_from_env_or_flag(argv)
    in_book_account = _in_book_account_from_env_or_flag(argv)
    out_of_book_account = _out_of_book_account_from_env_or_flag(argv)
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=lambda ctx: checks(
            ctx,
            token=token,
            expired_token=expired_token,
            underscoped_token=underscoped_token,
            in_book_account=in_book_account,
            out_of_book_account=out_of_book_account,
            provisioner=LiveServicePrincipalProvisioner(ctx),
            user_provisioner=(
                None if underscoped_token is not None else LiveBareMfaUserProvisioner(ctx)
            ),
            expired_token_minter=lambda: mint_machine_token(ctx),
        ),
        extra_arguments=[
            "--gateway-token",
            "--expired-token",
            "--underscoped-token",
            "--in-book-account",
            "--out-of-book-account",
        ],
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
