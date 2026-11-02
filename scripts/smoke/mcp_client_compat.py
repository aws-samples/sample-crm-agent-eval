# SPDX-License-Identifier: MIT
"""Layer 5 T3 smoke: both deployed Gateways work from the real `mcp` Python SDK client.

Every MCP session goes through `mcp.client.streamable_http.streamable_http_client` and
`mcp.ClientSession`, so the handshake is the SDK's own: `initialize`, `notifications/initialized`,
then `tools/list` (every page) and, on Gateway #1 only, one `tools/call`.

1. **Gateway #1, machine token.** A `client_credentials` token for the disposable service principal
   (`gateway_tools.LiveServicePrincipalProvisioner`, held in memory only) completes the handshake;
   the advertised names, with their `<target>___` prefix stripped, equal
   `crm.toolspec.definitions.ALL_TOOLS`; a read-only `get_account` on an in-book account answers
   without error and names that account.
2. **Gateway #2, user token.** A disposable user's access token (the
   `scripts.smoke_agent_stream.LiveStreamPrincipalProvisioner` that `mcp_chat_gateway` uses)
   completes the handshake and lists exactly `chat_with_supervisor`. No `tools/call`: that call is
   model-backed and `mcp_chat_gateway` owns it.
3. **Unauthenticated.** An SDK connect with no `Authorization` header is refused HTTP 401 by each
   Gateway.
4. **Gateway #2 OAuth discovery.** An unauthenticated POST is answered 401 with a
   `WWW-Authenticate` `resource_metadata` URL on the Gateway's own host; that RFC 9728 document
   names the pool issuer in `authorization_servers`, the Gateway's `/mcp` URL as `resource`, and
   exactly `supervisor/chat` in `scopes_supported`; the issuer's OpenID configuration is served;
   and an authorization-code request for the `cognito_mcp_oauth_client_id` client with an S256
   PKCE challenge, the Inspector callback and `openid supervisor/chat` is redirected to the login
   page, not back to the callback with an `error`.
5. **Scope ceiling.** The same request for `cognito_gateway_scope` is refused `invalid_scope`.
6. **Scope separation.** The Gateway #2 user token is refused 401/403 by Gateway #1.

Discovery requests are https-only and never follow a redirect. No token, secret, code verifier or
state value is printed or written. In a `finally`, the user, the service principal's `reps` row and
both tenants are removed.

Usage:
    python3 -m scripts.smoke.mcp_client_compat [--profile P] [--region R] [--outputs PATH] [--json]
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import CallToolResult, PaginatedRequestParams, TextContent
from scripts.smoke.gateway_tools import LiveServicePrincipalProvisioner
from scripts.smoke.harness import (
    CheckResult,
    ExpectedCheckCount,
    InsecureSchemeError,
    SmokeContext,
    run_main,
)

from crm.toolspec.definitions import ALL_TOOLS
from gateway.chat_target import TOOL_NAME as CHAT_TOOL

LAYER: Final[int] = 5
RESOURCE_CLASS: Final[str] = "mcp_client_compat"

#: Gateway #2's only scope (`infra/main.tf` `local.chat_scope_identifier`).
CHAT_SCOPE: Final[str] = "supervisor/chat"

#: The MCP Inspector's OAuth callback, one of the `mcp_oauth` client's registered callbacks.
INSPECTOR_CALLBACK: Final[str] = "http://localhost:6274/oauth/callback"

#: The read-only tool Gateway #1's session calls, with an in-book account id.
READ_TOOL: Final[str] = "get_account"

#: 401 for a missing or invalid token, 403 for one lacking scope (RFC 6750 section 3.1).
REFUSED_STATUSES: Final[frozenset[int]] = frozenset({401, 403})

DISCOVERY_CHECKS: Final[tuple[str, ...]] = (
    "g2_unauthenticated_401_names_resource_metadata",
    "g2_protected_resource_metadata_names_pool",
    "issuer_openid_configuration_served",
    "authorize_pkce_chat_scope_redirects_to_login",
    "authorize_gateway_scope_invalid_scope",
)

_TARGET_NAME_DELIMITER: Final[str] = "___"
_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0
_SESSION_ID_HEADER: Final[str] = "mcp-session-id"
#: Bound on one whole SDK session, so a stalled stream fails the row instead of hanging the run.
_SESSION_TIMEOUT_SECONDS: Final[float] = 120.0
_MAX_TOOL_PAGES: Final[int] = 20
_RESOURCE_METADATA = re.compile(r'resource_metadata="([^"]+)"')

#: `(ctx)` -> a provisioner with `provision() -> str` (an access token) and `cleanup()`.
UserProvisionerFactory = Callable[[SmokeContext], Any]
#: `(ctx)` -> a provisioner whose `provision()` returns `.token` and `.in_book_account`.
ServiceProvisionerFactory = Callable[[SmokeContext], Any]

__all__ = [
    "CHAT_SCOPE",
    "CHAT_TOOL",
    "DISCOVERY_CHECKS",
    "INSPECTOR_CALLBACK",
    "LAYER",
    "READ_TOOL",
    "RESOURCE_CLASS",
    "HttpReply",
    "SessionOutcome",
    "checks",
    "classify_authorize",
    "expected_check_count",
    "https_exchange",
    "main",
    "run_session",
]


@dataclass(frozen=True)
class SessionOutcome:
    """What one SDK session observed.

    `status` is the HTTP status that ended the session (`None` when it completed or no HTTP response
    arrived); `tool_names` is `None` unless the handshake and `tools/list` completed.
    """

    status: int | None = None
    error: str = ""
    protocol_version: str = ""
    session_id: str | None = None
    tool_names: tuple[str, ...] | None = None
    called_tool: str = ""
    call_result: CallToolResult | None = None
    call_error: str = ""


class SessionRunner(Protocol):
    """`run_session`'s signature: the seam the checks open every SDK session through."""

    def __call__(
        self,
        url: str,
        token: str | None,
        *,
        call_tool: str = "",
        call_arguments: Mapping[str, Any] | None = None,
    ) -> SessionOutcome: ...


@dataclass(frozen=True)
class HttpReply:
    """One HTTP response, unfollowed: status, lower-cased headers, decoded body and request URL."""

    status: int
    headers: Mapping[str, str]
    body: str
    url: str


#: `(method, url, headers, body)` -> `HttpReply`. The seam every discovery request goes through.
HttpExchange = Callable[[str, str, Mapping[str, str], bytes | None], HttpReply]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _no_redirect_https_opener() -> urllib.request.OpenerDirector:
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        _NoRedirect(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.HTTPSHandler(),
    ):
        opener.add_handler(handler)
    return opener


_OPENER: Final[urllib.request.OpenerDirector] = _no_redirect_https_opener()


def https_exchange(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> HttpReply:
    """Send one https request and return its response without following a redirect.

    Raises:
        InsecureSchemeError: `url` is not https; nothing was sent.
        urllib.error.URLError: No response was received.
    """
    request = urllib.request.Request(  # noqa: S310 - scheme checked below
        url, data=body, headers=dict(headers), method=method
    )
    if request.type != "https":
        raise InsecureSchemeError(f"refusing to open {request.type!r} URL: discovery is https-only")
    try:
        response = _OPENER.open(request, timeout=_HTTP_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return HttpReply(
            status=int(response.status),
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.read().decode("utf-8", errors="replace"),
            url=url,
        )


def _row(check: str, ok: bool, detail: str, resource: str = RESOURCE_CLASS) -> CheckResult:
    return CheckResult(
        resource=resource, check=check, status="pass" if ok else "fail", detail=detail
    )


def _require_https(url: str, what: str) -> str:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError(f"{what} is not an https URL: {url!r}")
    return url


def _with_mcp_path(url: str) -> str:
    base = url.rstrip("/")
    return base if base.endswith("/mcp") else f"{base}/mcp"


def gateway1_url(ctx: SmokeContext) -> str:
    """Gateway #1's MCP URL: `agentcore_gateway_mcp_url`, else `agentcore_gateway_url` + `/mcp`."""
    explicit = ctx.outputs.get("agentcore_gateway_mcp_url")
    url = str(explicit) if explicit else ctx.outputs.require_str("agentcore_gateway_url")
    return _require_https(_with_mcp_path(url), "Gateway #1 URL")


def gateway2_url(ctx: SmokeContext) -> str:
    """Gateway #2's MCP URL: `mcp_chat_gateway_url` with `/mcp` appended when absent."""
    url = ctx.outputs.require_str("mcp_chat_gateway_url")
    return _require_https(_with_mcp_path(url), "Gateway #2 URL")


def _expected_issuer(ctx: SmokeContext) -> str:
    pool = ctx.outputs.require_str("cognito_user_pool_id")
    return f"https://cognito-idp.{ctx.region}.amazonaws.com/{pool}"


def _bare(name: str) -> str:
    return name.rpartition(_TARGET_NAME_DELIMITER)[2] or name


def _http_status_of(exc: BaseException) -> int | None:
    """The status of the first HTTP status error inside `exc` (groups and causes included)."""
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        status = getattr(getattr(current, "response", None), "status_code", None)
        if type(current).__name__ == "HTTPStatusError" and isinstance(status, int):
            return status
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)
    return None


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return _describe(exc.exceptions[0])
    return f"{type(exc).__name__}: {exc}"[:300]


@dataclass
class _HttpObserver:
    """The SDK client's HTTP responses: the first error status of a POST, and the session id.

    Only POSTs carry the JSON-RPC exchange, so the SSE GET stream and the closing DELETE never set
    ``error_status``.
    """

    error_status: int | None = None
    session_id: str | None = None

    async def on_response(self, response: httpx2.Response) -> None:
        failed_post = response.request.method == "POST" and response.status_code >= 400
        if failed_post and self.error_status is None:
            self.error_status = response.status_code
        self.session_id = response.headers.get(_SESSION_ID_HEADER) or self.session_id


async def _session(
    url: str,
    token: str | None,
    call_tool: str,
    call_arguments: Mapping[str, Any],
    observer: _HttpObserver,
) -> SessionOutcome:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else None
    async with (
        create_mcp_http_client(headers=headers) as client,
        streamable_http_client(url, http_client=client) as (read, write),
        ClientSession(read, write, read_timeout_seconds=_HTTP_TIMEOUT_SECONDS) as session,
    ):
        client.event_hooks["response"].append(observer.on_response)
        initialized = await session.initialize()
        names: list[str] = []
        cursor: str | None = None
        for _ in range(_MAX_TOOL_PAGES):
            page = await session.list_tools(
                params=PaginatedRequestParams(cursor=cursor) if cursor else None
            )
            names += [tool.name for tool in page.tools]
            cursor = page.next_cursor
            if not cursor:
                break
        else:
            raise RuntimeError(f"tools/list still paginating after {_MAX_TOOL_PAGES} pages")
        called, result, call_error = "", None, ""
        if call_tool:
            called = next((n for n in names if _bare(n) == call_tool), "")
            if not called:
                call_error = f"{call_tool!r} is not advertised, so it was not called"
            else:
                try:
                    with anyio.fail_after(_HTTP_TIMEOUT_SECONDS):
                        result = await session.call_tool(called, dict(call_arguments))
                except Exception as exc:  # noqa: BLE001 - reported by the call row
                    call_error = _describe(exc)
        return SessionOutcome(
            protocol_version=str(initialized.protocol_version),
            session_id=observer.session_id,
            tool_names=tuple(names),
            called_tool=called,
            call_result=result,
            call_error=call_error,
        )


def run_session(
    url: str,
    token: str | None,
    *,
    call_tool: str = "",
    call_arguments: Mapping[str, Any] | None = None,
) -> SessionOutcome:
    """Run one SDK session against `url` and report what it observed; never raises.

    `token`, when given, is sent as a bearer token. With `call_tool`, the advertised tool whose
    bare name matches is called once with `call_arguments`.
    """

    observer = _HttpObserver()

    async def bounded() -> SessionOutcome:
        with anyio.fail_after(_SESSION_TIMEOUT_SECONDS):
            return await _session(url, token, call_tool, call_arguments or {}, observer)

    try:
        return anyio.run(bounded)
    except Exception as exc:  # noqa: BLE001 - the outcome carries the failure
        status = observer.error_status
        return SessionOutcome(
            status=status if status is not None else _http_status_of(exc), error=_describe(exc)
        )


def _unauthenticated_row(check: str, url: str, runner: SessionRunner) -> CheckResult:
    outcome = runner(url, None)
    ok = outcome.status == 401 and outcome.tool_names is None
    return _row(
        check,
        ok,
        f"SDK connect with no Authorization header -> status={outcome.status!r}"
        + ("" if ok else f" error={outcome.error!r} tools={outcome.tool_names!r}; expected 401"),
    )


def _refused_row(check: str, outcome: SessionOutcome, what: str) -> CheckResult:
    ok = outcome.status in REFUSED_STATUSES and outcome.tool_names is None
    return _row(
        check,
        ok,
        f"{what} -> status={outcome.status!r}"
        + (
            "" if ok else f" error={outcome.error!r} tools={outcome.tool_names!r}; expected 401/403"
        ),
    )


def _handshake_detail(outcome: SessionOutcome) -> str:
    return (
        f"protocolVersion={outcome.protocol_version!r} "
        f"session_id={'issued' if outcome.session_id else 'none'} "
        f"tools={len(outcome.tool_names or ())}"
    )


def _gateway1_inventory_row(outcome: SessionOutcome) -> CheckResult:
    check = "g1_sdk_handshake_tools_list_matches_toolspec"
    if outcome.tool_names is None:
        return _row(check, False, f"SDK session failed: status={outcome.status!r} {outcome.error}")
    deployed = sorted(_bare(n) for n in outcome.tool_names)
    expected = sorted(tool.name for tool in ALL_TOOLS)
    missing = sorted(set(expected) - set(deployed))
    extra = sorted(set(deployed) - set(expected))
    duplicated = sorted({n for n in deployed if deployed.count(n) > 1})
    ok = bool(outcome.protocol_version) and deployed == expected
    return _row(
        check,
        ok,
        _handshake_detail(outcome)
        + ("" if ok else f"; missing={missing} extra={extra} duplicated={duplicated}"),
    )


def _call_text(result: CallToolResult) -> str:
    return "\n".join(item.text for item in result.content if isinstance(item, TextContent))


def _gateway1_call_row(outcome: SessionOutcome, account_id: str) -> CheckResult:
    check = "g1_sdk_read_only_tools_call"
    if outcome.tool_names is None:
        return _row(check, False, "not reached: the Gateway #1 SDK session did not complete")
    result = outcome.call_result
    if result is None:
        return _row(check, False, f"{READ_TOOL} produced no result: {outcome.call_error}")
    text = _call_text(result)
    envelope_error = re.search(r'"statusCode"\s*:\s*[45]\d\d', text)
    named = bool(account_id) and account_id in text
    ok = not result.is_error and envelope_error is None and named
    return _row(
        check,
        ok,
        f"tools/call {outcome.called_tool!r} on an in-book account -> isError={result.is_error} "
        f"names_account={named}" + ("" if ok else f"; content={text[:200]!r}"),
    )


def _gateway2_inventory_row(outcome: SessionOutcome) -> CheckResult:
    check = "g2_sdk_handshake_tools_list_exactly_chat_with_supervisor"
    if outcome.tool_names is None:
        return _row(check, False, f"SDK session failed: status={outcome.status!r} {outcome.error}")
    bare = [_bare(n) for n in outcome.tool_names]
    ok = bool(outcome.protocol_version) and bare == [CHAT_TOOL]
    return _row(
        check,
        ok,
        _handshake_detail(outcome)
        + ("" if ok else f"; tools={list(outcome.tool_names)!r}, expected exactly [{CHAT_TOOL!r}]"),
    )


def _pkce_challenge() -> str:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _authorize_url(endpoint: str, client_id: str, scope: str) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": INSPECTOR_CALLBACK,
            "scope": scope,
            "code_challenge": _pkce_challenge(),
            "code_challenge_method": "S256",
            "state": secrets.token_urlsafe(16),
        }
    )
    return f"{endpoint}?{query}"


def classify_authorize(reply: HttpReply) -> tuple[str, str]:
    """`("login" | "error:<code>" | "callback" | "other", detail)` for an authorize response.

    `detail` names the status and the redirect's scheme, host and path only, never its query.
    Cognito reports a refused scope as `error=invalid_request&error_description=invalid_scope`,
    which is classified `error:invalid_scope`.
    """
    location = reply.headers.get("location", "")
    if 300 <= reply.status < 400 and location:
        target = urllib.parse.urljoin(reply.url, location)
        parts = urllib.parse.urlsplit(target)
        detail = f"status={reply.status} -> {parts.scheme}://{parts.netloc}{parts.path}"
        query = urllib.parse.parse_qs(parts.query)
        error = query.get("error", [""])[0]
        if error == "invalid_request" and query.get("error_description") == ["invalid_scope"]:
            error = "invalid_scope"
        if error:
            return f"error:{error}", detail
        if target.startswith(INSPECTOR_CALLBACK):
            return "callback", detail
        if parts.path.endswith("/login"):
            return "login", detail
        return "other", detail
    detail = f"status={reply.status} (no redirect)"
    body_error = _json_of(reply).get("error")
    if 400 <= reply.status < 500 and isinstance(body_error, str) and body_error:
        return f"error:{body_error}", detail
    return "other", detail


def _json_of(reply: HttpReply) -> Mapping[str, Any]:
    try:
        body = json.loads(reply.body)
    except ValueError:
        return {}
    return body if isinstance(body, Mapping) else {}


def _discovery_rows(ctx: SmokeContext, gateway2: str, http: HttpExchange) -> Iterator[CheckResult]:
    emitted = 0

    def unreached(reason: str) -> Iterator[CheckResult]:
        for check in DISCOVERY_CHECKS[emitted:]:
            yield _row(check, False, f"not reached: {reason}")

    try:
        reply = http(
            "POST",
            gateway2,
            {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            json.dumps({"jsonrpc": "2.0", "id": "smoke-1", "method": "tools/list"}).encode(),
        )
        challenge = reply.headers.get("www-authenticate", "")
        match = _RESOURCE_METADATA.search(challenge)
        metadata_url = match.group(1) if match else ""
        same_host = (
            urllib.parse.urlsplit(metadata_url).netloc == urllib.parse.urlsplit(gateway2).netloc
        )
        ok = (
            reply.status == 401
            and challenge.lower().startswith("bearer")
            and metadata_url.startswith("https://")
            and same_host
        )
        yield _row(
            DISCOVERY_CHECKS[0],
            ok,
            f"POST -> status={reply.status} resource_metadata={metadata_url!r}"
            + (
                ""
                if ok
                else "; expected 401 Bearer naming an https resource_metadata on the Gateway's host"
            ),
        )
        emitted = 1
        if not ok:
            yield from unreached("no usable resource_metadata URL")
            return

        issuer = _expected_issuer(ctx)
        reply = http("GET", metadata_url, {"Accept": "application/json"}, None)
        prm = _json_of(reply)
        servers = list(prm.get("authorization_servers") or [])
        scopes = list(prm.get("scopes_supported") or [])
        resource = str(prm.get("resource") or "")
        ok = (
            reply.status == 200
            and issuer in servers
            and scopes == [CHAT_SCOPE]
            and resource.rstrip("/") == gateway2.rstrip("/")
        )
        yield _row(
            DISCOVERY_CHECKS[1],
            ok,
            f"GET -> status={reply.status} authorization_servers={servers} "
            f"scopes_supported={scopes} resource={resource!r}"
            + ("" if ok else f"; expected {issuer!r}, [{CHAT_SCOPE!r}] and {gateway2!r}"),
        )
        emitted = 2
        if not ok:
            yield from unreached("the protected-resource metadata did not name the pool")
            return

        reply = http(
            "GET",
            f"{issuer}/.well-known/openid-configuration",
            {"Accept": "application/json"},
            None,
        )
        oidc = _json_of(reply)
        endpoint = str(oidc.get("authorization_endpoint") or "")
        ok = (
            reply.status == 200 and oidc.get("issuer") == issuer and endpoint.startswith("https://")
        )
        yield _row(
            DISCOVERY_CHECKS[2],
            ok,
            f"GET -> status={reply.status} issuer={oidc.get('issuer')!r} "
            f"authorization_endpoint={endpoint!r}",
        )
        emitted = 3
        if not ok:
            yield from unreached("no https authorization_endpoint")
            return

        if re.search(r"\s", endpoint) or urllib.parse.urlsplit(endpoint).query:
            yield from unreached(f"authorization_endpoint {endpoint!r} is not a plain https URL")
            return

        client_id = ctx.outputs.get("cognito_mcp_oauth_client_id")
        if not client_id:
            yield from unreached(
                "Terraform output 'cognito_mcp_oauth_client_id' is absent, so no OAuth client "
                "can be exercised"
            )
            return
        url = _authorize_url(endpoint, str(client_id), f"openid {CHAT_SCOPE}")
        kind, detail = classify_authorize(http("GET", url, {}, None))
        yield _row(
            DISCOVERY_CHECKS[3], kind == "login", f"{detail} classified {kind!r}; expected 'login'"
        )
        emitted = 4

        scope = ctx.outputs.require_str("cognito_gateway_scope")
        url = _authorize_url(endpoint, str(client_id), scope)
        kind, detail = classify_authorize(http("GET", url, {}, None))
        yield _row(
            DISCOVERY_CHECKS[4],
            kind == "error:invalid_scope",
            f"scope={scope!r}: {detail} classified {kind!r}; expected 'error:invalid_scope'",
        )
        emitted = 5
    except Exception as exc:  # noqa: BLE001 - failing rows, not a crash
        yield from unreached(_describe(exc))


def _default_user_provisioner(ctx: SmokeContext) -> Any:
    from scripts.smoke_agent_stream import LiveStreamPrincipalProvisioner

    return LiveStreamPrincipalProvisioner(ctx)


def _default_service_provisioner(ctx: SmokeContext) -> Any:
    return LiveServicePrincipalProvisioner(ctx)


def checks(
    ctx: SmokeContext,
    *,
    session_runner: SessionRunner | None = None,
    http_exchange: HttpExchange | None = None,
    user_provisioner_factory: UserProvisionerFactory | None = None,
    service_provisioner_factory: ServiceProvisionerFactory | None = None,
) -> Iterator[CheckResult]:
    """Run rows 1-6 of the module docstring, then clean up every provisioned principal.

    `session_runner` (default `run_session`) and `http_exchange` (default `https_exchange`) are
    the network seams.
    """
    try:
        url1, url2 = gateway1_url(ctx), gateway2_url(ctx)
    except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
        yield _row("terraform_output_present", False, f"a Gateway URL output is unusable ({exc})")
        return
    runner: SessionRunner = session_runner or run_session

    yield _unauthenticated_row("g1_sdk_unauthenticated_refused_401", url1, runner)
    yield _unauthenticated_row("g2_sdk_unauthenticated_refused_401", url2, runner)
    yield from _discovery_rows(ctx, url2, http_exchange or https_exchange)

    provisioners: list[Any] = []
    try:
        try:
            service = (service_provisioner_factory or _default_service_provisioner)(ctx)
            provisioners.append(service)
            fixture = service.provision()
        except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
            yield _row("g1_service_principal_provisioned", False, _describe(exc))
        else:
            account = str(fixture.in_book_account)
            outcome = runner(
                url1,
                str(fixture.token),
                call_tool=READ_TOOL,
                call_arguments={"account_id": account},
            )
            yield _gateway1_inventory_row(outcome)
            yield _gateway1_call_row(outcome, account)

        try:
            user = (user_provisioner_factory or _default_user_provisioner)(ctx)
            provisioners.append(user)
            user_token = str(user.provision())
        except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
            yield _row("g2_user_provisioned", False, _describe(exc))
        else:
            yield _gateway2_inventory_row(runner(url2, user_token))
            yield _refused_row(
                "g1_refuses_g2_user_token",
                runner(url1, user_token),
                "Gateway #1 SDK connect with the Gateway #2 user token",
            )
    finally:
        for provisioner in provisioners:
            try:
                yield from provisioner.cleanup()
            except Exception as exc:  # noqa: BLE001 - every cleanup is attempted
                yield _row("principal_cleanup", False, f"cleanup raised {_describe(exc)}")


def expected_check_count(ctx: SmokeContext) -> ExpectedCheckCount:
    """The URL failure alone, or the rows emitted on every path once both URLs resolve."""
    try:
        gateway1_url(ctx)
        gateway2_url(ctx)
    except Exception as exc:  # noqa: BLE001 - the reason belongs in the basis
        return ExpectedCheckCount(
            minimum=1,
            basis=f"a Gateway URL output is unusable ({type(exc).__name__}), so `checks` emits its "
            "single terraform_output_present failure",
        )
    return ExpectedCheckCount(
        minimum=2 + len(DISCOVERY_CHECKS) + 2,
        basis="two unauthenticated rows, every discovery row (each emitted even when unreached), "
        "and at least one row per principal (its provisioning failure, or its session rows)",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the MCP SDK client checks and return the process exit code."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
