# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.mcp_client_compat`: the real `mcp` SDK client against fake Gateways.

`_FakeDeployment` serves both Gateways' streamable-HTTP MCP endpoints (stateless, as deployed: no
`Mcp-Session-Id`), Gateway #2's RFC 9728 metadata, the issuer's OpenID configuration and Cognito's
authorize endpoint. SDK sessions reach it over a loopback HTTP server, so the SDK's own transport
and `ClientSession` run unmodified; discovery requests reach it in-process through the
`http_exchange` seam. No AWS, and nothing leaves 127.0.0.1.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from collections.abc import Iterable, Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from scripts import proc_run
from scripts.smoke import mcp_client_compat as module
from scripts.smoke.harness import CheckResult, InsecureSchemeError, context_for_test

from crm.toolspec.definitions import ALL_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[2]
_REGION = "us-east-1"
_POOL = "us-east-1_TestPool"
_ISSUER = f"https://cognito-idp.{_REGION}.amazonaws.com/{_POOL}"
_G1_HOST = "gw1.gateway.bedrock-agentcore.us-east-1.amazonaws.com"
_G2_HOST = "gw2.gateway.bedrock-agentcore.us-east-1.amazonaws.com"
_AUTH_HOST = "crm-test.auth.us-east-1.amazoncognito.com"
_OAUTH_CLIENT = "test-mcp-oauth-client"
_MACHINE = "machine-token"
_USER = "user-token"
_ACCOUNT = "acct-eval-smoke-0000"
_OUTPUTS: dict[str, Any] = {
    "agentcore_gateway_url": f"https://{_G1_HOST}",
    "mcp_chat_gateway_url": f"https://{_G2_HOST}",
    "cognito_user_pool_id": _POOL,
    "cognito_gateway_scope": "gateway/invoke",
    "cognito_mcp_oauth_client_id": _OAUTH_CLIENT,
}
_G1_TOOLS = [f"crm-actions___{tool.name}" for tool in ALL_TOOLS]
_G2_TOOLS = [f"chat-with-supervisor___{module.CHAT_TOOL}"]

#: `(status, headers, body)` as the fake answers one request.
_Answer = tuple[int, dict[str, str], bytes]


def _json(status: int, body: Any, headers: Mapping[str, str] | None = None) -> _Answer:
    return (
        status,
        {"content-type": "application/json", **(headers or {})},
        json.dumps(body).encode(),
    )


class _FakeDeployment:
    """Both Gateways, their protected-resource metadata, the issuer and Cognito's authorize."""

    def __init__(self) -> None:
        self.tokens: dict[str, set[str | None]] = {_G1_HOST: {_MACHINE}, _G2_HOST: {_USER}}
        self.tools = {_G1_HOST: list(_G1_TOOLS), _G2_HOST: list(_G2_TOOLS)}
        self.page_size = 10
        self.call_is_error = False
        self.call_names_account = True
        self.challenge_metadata = True
        self.metadata_host = _G2_HOST
        self.scopes_supported = [module.CHAT_SCOPE]
        self.authorization_servers = [_ISSUER]
        self.oidc_status = 200
        self.login_path = "/login"
        #: Cognito's live reply to a scope the client is not granted.
        self.invalid_scope_query = "error=invalid_request&error_description=invalid_scope"
        self.refused_status = 401
        self.unauthenticated_status = 401
        self.session_id: str | None = None
        self.lock = threading.Lock()
        self.urls: list[str] = []
        self.methods: list[tuple[str, str]] = []
        self.calls: list[dict[str, Any]] = []

    def handle(self, method: str, url: str, headers: Mapping[str, str], body: bytes) -> _Answer:
        with self.lock:
            self.urls.append(url)
        parts = urllib.parse.urlsplit(url)
        host, path = parts.hostname or "", parts.path
        if host in self.tokens and path == "/mcp" and method == "POST":
            return self._mcp(host, headers, body)
        if host in self.tokens and path == "/mcp" and method == "DELETE":
            return 200, {}, b""
        if host == _G2_HOST and path == "/.well-known/oauth-protected-resource":
            return _json(
                200,
                {
                    "authorization_servers": self.authorization_servers,
                    "resource": f"https://{_G2_HOST}/mcp",
                    "scopes_supported": self.scopes_supported,
                },
            )
        if url == f"{_ISSUER}/.well-known/openid-configuration":
            return _json(
                self.oidc_status,
                {
                    "issuer": _ISSUER,
                    "authorization_endpoint": f"https://{_AUTH_HOST}/oauth2/authorize",
                },
            )
        if host == _AUTH_HOST and path == "/oauth2/authorize":
            return self._authorize(dict(urllib.parse.parse_qsl(parts.query)))
        return 404, {}, b""

    def _authorize(self, query: Mapping[str, str]) -> _Answer:
        redirect = query.get("redirect_uri", "")
        if query.get("client_id") != _OAUTH_CLIENT or query.get("code_challenge_method") != "S256":
            return 302, {"location": "/error?error=unauthorized_client"}, b""
        if "gateway/invoke" in query.get("scope", "").split():
            target = f"{redirect}?{self.invalid_scope_query}&state={query.get('state')}"
            return 302, {"location": target}, b""
        login = f"https://{_AUTH_HOST}{self.login_path}?{urllib.parse.urlencode(query)}"
        return 302, {"location": login}, b""

    def _mcp(self, host: str, headers: Mapping[str, str], body: bytes) -> _Answer:
        auth = headers.get("authorization", "")
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else None
        if token not in self.tokens[host]:
            challenge = {}
            if self.challenge_metadata:
                metadata = f"https://{self.metadata_host}/.well-known/oauth-protected-resource"
                challenge["www-authenticate"] = (
                    f'Bearer error="invalid_token", resource_metadata="{metadata}"'
                )
            status = self.unauthenticated_status if token is None else self.refused_status
            return _json(status, {"message": "Unauthorized"}, challenge)
        message = json.loads(body)
        with self.lock:
            self.methods.append((host, str(message.get("method"))))
        if "id" not in message:
            return 202, {}, b""
        issued = (
            {"mcp-session-id": self.session_id}
            if self.session_id and message.get("method") == "initialize"
            else None
        )
        return _json(
            200,
            {"jsonrpc": "2.0", "id": message["id"], "result": self._result(host, message)},
            issued,
        )

    def _result(self, host: str, message: dict[str, Any]) -> dict[str, Any]:
        method, params = message["method"], message.get("params") or {}
        if method == "initialize":
            return {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fake-gateway", "version": "1"},
            }
        if method == "tools/list":
            start = int(params.get("cursor") or 0)
            names = self.tools[host][start : start + self.page_size]
            more = start + self.page_size < len(self.tools[host])
            return {
                "tools": [{"name": n, "inputSchema": {"type": "object"}} for n in names],
                **({"nextCursor": str(start + self.page_size)} if more else {}),
            }
        if method == "tools/call":
            with self.lock:
                self.calls.append({"host": host, **params})
            record = {"account_id": _ACCOUNT if self.call_names_account else "other"}
            return {
                "isError": self.call_is_error,
                "content": [{"type": "text", "text": json.dumps(record)}],
            }
        raise AssertionError(f"unexpected method {method}")

    def exchange(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> module.HttpReply:
        """The `http_exchange` seam, answered in-process."""
        lowered = {k.lower(): v for k, v in headers.items()}
        status, answer_headers, answer = self.handle(method, url, lowered, body or b"")
        return module.HttpReply(status, answer_headers, answer.decode(), url)


@pytest.fixture
def loopback() -> Iterator[tuple[_FakeDeployment, str]]:
    """A `_FakeDeployment` behind `http://127.0.0.1:<port>/<host>/...`, and that base URL."""
    fake = _FakeDeployment()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            host, _, rest = self.path.lstrip("/").partition("/")
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, answer_headers, answer = fake.handle(
                self.command, f"https://{host}/{rest}", headers, body
            )
            self.send_response(status)
            for key, value in answer_headers.items():
                self.send_header(key, value)
            self.send_header("content-length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        do_GET = do_POST = do_DELETE = _serve  # noqa: N815

        def log_message(self, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield fake, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")


def _loopback_runner(base: str) -> module.SessionRunner:
    """`run_session` with each https Gateway URL rewritten onto the loopback server."""

    def runner(
        url: str,
        token: str | None,
        *,
        call_tool: str = "",
        call_arguments: Mapping[str, Any] | None = None,
    ) -> module.SessionOutcome:
        parts = urllib.parse.urlsplit(url)
        assert parts.scheme == "https", url
        return module.run_session(
            f"{base}/{parts.netloc}{parts.path}",
            token,
            call_tool=call_tool,
            call_arguments=call_arguments,
        )

    return runner


class _FakeService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.cleaned = False

    def provision(self) -> Any:
        if self.fail:
            raise RuntimeError("interceptor carries no service principal")
        return type("Fixture", (), {"token": _MACHINE, "in_book_account": _ACCOUNT})()

    def cleanup(self) -> Iterable[CheckResult]:
        self.cleaned = True
        yield CheckResult(resource="service_principal", check="reps_row_deleted", status="pass")


class _FakeUser:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.cleaned = False

    def provision(self) -> str:
        if self.fail:
            raise RuntimeError("admin_create_user refused")
        return _USER

    def cleanup(self) -> Iterable[CheckResult]:
        self.cleaned = True
        yield CheckResult(resource="smoke_user", check="user_removed", status="pass")


def _run(
    deployment: tuple[_FakeDeployment, str],
    *,
    outputs: dict[str, Any] | None = None,
    service: _FakeService | None = None,
    user: _FakeUser | None = None,
    runner: module.SessionRunner | None = None,
) -> dict[str, CheckResult]:
    fake, base = deployment
    ctx = context_for_test(outputs=_OUTPUTS if outputs is None else outputs, region=_REGION)
    service = service or _FakeService()
    user = user or _FakeUser()
    rows = list(
        module.checks(
            ctx,
            session_runner=runner or _loopback_runner(base),
            http_exchange=fake.exchange,
            service_provisioner_factory=lambda _ctx: service,
            user_provisioner_factory=lambda _ctx: user,
        )
    )
    by_name = {row.check: row for row in rows}
    assert len(by_name) == len(rows), f"duplicate check names: {[r.check for r in rows]}"
    return by_name


def _failed(rows: Mapping[str, CheckResult]) -> set[str]:
    return {name for name, row in rows.items() if row.status != "pass"}


_ALL_ROWS = {
    "g1_sdk_unauthenticated_refused_401",
    "g2_sdk_unauthenticated_refused_401",
    *module.DISCOVERY_CHECKS,
    "g1_sdk_handshake_tools_list_matches_toolspec",
    "g1_sdk_read_only_tools_call",
    "g2_sdk_handshake_tools_list_exactly_chat_with_supervisor",
    "g1_refuses_g2_user_token",
    "reps_row_deleted",
    "user_removed",
}


def test_a_conforming_deployment_passes_every_row(
    loopback: tuple[_FakeDeployment, str],
) -> None:
    """Control: every row passes against a deployment that behaves as the live probe recorded."""
    rows = _run(loopback)
    assert set(rows) == _ALL_ROWS
    assert _failed(rows) == set(), {n: rows[n].detail for n in _failed(rows)}
    floor = module.expected_check_count(context_for_test(outputs=_OUTPUTS)).minimum
    assert floor <= len(rows)


def test_the_sdk_performs_the_full_handshake_and_one_read_call_on_gateway1_only(
    loopback: tuple[_FakeDeployment, str],
) -> None:
    """initialize -> notifications/initialized -> every tools/list page; tools/call on G1 only."""
    fake, _ = loopback
    fake.page_size = 7
    _run(loopback)
    g1 = [m for h, m in fake.methods if h == _G1_HOST]
    g2 = [m for h, m in fake.methods if h == _G2_HOST]
    assert g1[:2] == ["initialize", "notifications/initialized"]
    assert g1.count("tools/list") == -(-len(_G1_TOOLS) // 7)
    assert g2 == ["initialize", "notifications/initialized", "tools/list"]
    assert fake.calls == [
        {
            "host": _G1_HOST,
            "name": f"crm-actions___{module.READ_TOOL}",
            "arguments": {"account_id": _ACCOUNT},
        }
    ]


def test_the_session_id_the_gateway_issues_is_reported(
    loopback: tuple[_FakeDeployment, str],
) -> None:
    """The `mcp-session-id` header on the initialize response becomes the outcome's session id."""
    fake, base = loopback
    fake.session_id = "sess-0001"
    outcome = _loopback_runner(base)(f"https://{_G1_HOST}/mcp", _MACHINE)
    assert outcome.tool_names is not None
    assert outcome.session_id == "sess-0001"


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_only_a_failed_post_sets_the_error_status(method: str) -> None:
    """A 405 on the SSE GET or the closing DELETE is not the status that ended the session."""
    observer = module._HttpObserver()
    url = f"https://{_G1_HOST}/mcp"

    async def feed() -> None:
        await observer.on_response(httpx2.Response(405, request=httpx2.Request(method, url)))
        assert observer.error_status is None
        await observer.on_response(httpx2.Response(401, request=httpx2.Request("POST", url)))
        await observer.on_response(httpx2.Response(500, request=httpx2.Request("POST", url)))

    anyio.run(feed)
    assert observer.error_status == 401


def test_no_session_id_header_reports_none(loopback: tuple[_FakeDeployment, str]) -> None:
    fake, base = loopback
    outcome = _loopback_runner(base)(f"https://{_G1_HOST}/mcp", _MACHINE)
    assert outcome.tool_names is not None
    assert outcome.session_id is None


def test_no_token_reaches_a_check_detail(loopback: tuple[_FakeDeployment, str]) -> None:
    fake, _ = loopback
    fake.tools[_G1_HOST].append("crm-actions___extra")
    for row in _run(loopback).values():
        assert _MACHINE not in row.detail and _USER not in row.detail


def test_redirects_are_never_followed(loopback: tuple[_FakeDeployment, str]) -> None:
    fake, _ = loopback
    _run(loopback)
    assert not any(urllib.parse.urlsplit(u).path == "/login" for u in fake.urls)


@pytest.mark.parametrize(
    ("mutate", "failing"),
    [
        (lambda f: f.tools[_G1_HOST].pop(), ("g1_sdk_handshake_tools_list_matches_toolspec",)),
        (
            lambda f: f.tools[_G1_HOST].append("crm-actions___high_risk_write"),
            ("g1_sdk_handshake_tools_list_matches_toolspec",),
        ),
        (
            lambda f: f.tools[_G1_HOST].append(_G1_TOOLS[0]),
            ("g1_sdk_handshake_tools_list_matches_toolspec",),
        ),
        (lambda f: setattr(f, "call_is_error", True), ("g1_sdk_read_only_tools_call",)),
        (lambda f: setattr(f, "call_names_account", False), ("g1_sdk_read_only_tools_call",)),
        (
            lambda f: f.tools[_G1_HOST].remove(f"crm-actions___{module.READ_TOOL}"),
            ("g1_sdk_handshake_tools_list_matches_toolspec", "g1_sdk_read_only_tools_call"),
        ),
        (
            lambda f: f.tools[_G2_HOST].append("chat-with-supervisor___extra"),
            ("g2_sdk_handshake_tools_list_exactly_chat_with_supervisor",),
        ),
        (lambda f: f.tokens[_G1_HOST].add(_USER), ("g1_refuses_g2_user_token",)),
    ],
    ids=[
        "g1-missing-tool",
        "g1-extra-tool",
        "g1-duplicate-tool",
        "g1-call-is-error",
        "g1-call-wrong-record",
        "g1-read-tool-unadvertised",
        "g2-extra-tool",
        "g1-admits-user-token",
    ],
)
def test_each_session_row_fails_on_its_own_defect(
    loopback: tuple[_FakeDeployment, str], mutate: Any, failing: tuple[str, ...]
) -> None:
    """Each defect reds exactly the rows that own it; an unadvertised read tool is both an
    inventory defect and an uncallable tool."""
    mutate(loopback[0])
    assert _failed(_run(loopback)) == set(failing)


def test_a_403_refusal_of_the_user_token_on_gateway1_passes(
    loopback: tuple[_FakeDeployment, str],
) -> None:
    loopback[0].refused_status = 403
    row = _run(loopback)["g1_refuses_g2_user_token"]
    assert row.status == "pass"
    assert "status=403" in row.detail


def test_a_gateway1_server_error_on_the_user_token_is_not_a_refusal(
    loopback: tuple[_FakeDeployment, str],
) -> None:
    """Row 6 needs a 401/403: a 5xx lists no tools either, but refuses nothing."""
    loopback[0].refused_status = 500
    rows = _run(loopback)
    assert _failed(rows) == {"g1_refuses_g2_user_token"}
    assert "status=500" in rows["g1_refuses_g2_user_token"].detail


@pytest.mark.parametrize(
    ("host", "row"),
    [
        (_G1_HOST, "g1_sdk_unauthenticated_refused_401"),
        (_G2_HOST, "g2_sdk_unauthenticated_refused_401"),
    ],
)
def test_an_unauthenticated_connect_that_is_admitted_fails(
    loopback: tuple[_FakeDeployment, str], host: str, row: str
) -> None:
    loopback[0].tokens[host].add(None)
    rows = _run(loopback)
    assert row in _failed(rows)
    assert "tools=(" in rows[row].detail


def test_an_unauthenticated_403_is_not_a_401(loopback: tuple[_FakeDeployment, str]) -> None:
    """Row 3 requires 401 exactly: a 403 means a credential was judged, not that one is missing."""
    loopback[0].unauthenticated_status = 403
    failed = _failed(_run(loopback))
    assert {"g1_sdk_unauthenticated_refused_401", "g2_sdk_unauthenticated_refused_401"} <= failed


@pytest.mark.parametrize(
    ("mutate", "first_failing"),
    [
        (lambda f: setattr(f, "challenge_metadata", False), 0),
        (lambda f: setattr(f, "metadata_host", "attacker.example.com"), 0),
        (lambda f: setattr(f, "authorization_servers", ["https://other-issuer"]), 1),
        (lambda f: setattr(f, "scopes_supported", ["supervisor/chat", "gateway/invoke"]), 1),
        (lambda f: setattr(f, "oidc_status", 500), 2),
        (lambda f: setattr(f, "login_path", "/oauth2/idpresponse"), 3),
    ],
    ids=[
        "no-resource-metadata",
        "metadata-on-foreign-host",
        "prm-names-other-issuer",
        "prm-advertises-gateway-scope",
        "oidc-not-served",
        "authorize-not-to-login",
    ],
)
def test_a_broken_discovery_step_fails_it_and_every_later_step(
    loopback: tuple[_FakeDeployment, str], mutate: Any, first_failing: int
) -> None:
    """A step that fails leaves every step after it `not reached`, except the two independent
    authorize requests, which are both sent once the endpoint is known."""
    mutate(loopback[0])
    rows = _run(loopback)
    steps = module.DISCOVERY_CHECKS
    later = set(steps[first_failing + 1 :]) if first_failing < 3 else set()
    assert _failed(rows) == {steps[first_failing]} | later
    for name in later:
        assert rows[name].detail.startswith("not reached:")


def test_an_error_redirect_fails_the_login_row(loopback: tuple[_FakeDeployment, str]) -> None:
    """A redirect carrying `error=` is a refusal, never the login page."""
    rows = _run(
        loopback, outputs={**_OUTPUTS, "cognito_mcp_oauth_client_id": "unregistered-client"}
    )
    row = rows["authorize_pkce_chat_scope_redirects_to_login"]
    assert row.status == "fail"
    assert "error:unauthorized_client" in row.detail


def test_a_gateway_scope_not_refused_invalid_scope_fails_the_ceiling_row(
    loopback: tuple[_FakeDeployment, str],
) -> None:
    loopback[0].invalid_scope_query = "error=access_denied"
    assert _failed(_run(loopback)) == {"authorize_gateway_scope_invalid_scope"}


def test_a_granted_gateway_scope_redirected_to_login_fails_the_ceiling_row(
    loopback: tuple[_FakeDeployment, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A login redirect for `gateway/invoke` means the client may obtain it: a failure."""
    fake = loopback[0]
    real = fake._authorize

    def grants_everything(query: Mapping[str, str]) -> _Answer:
        widened = {**query, "scope": f"openid {module.CHAT_SCOPE}"}
        return real(widened)

    monkeypatch.setattr(fake, "_authorize", grants_everything)
    row = _run(loopback)["authorize_gateway_scope_invalid_scope"]
    assert row.status == "fail"
    assert "classified 'login'" in row.detail


@pytest.mark.parametrize(
    ("query", "passes"),
    [
        ("error=invalid_request&error_description=invalid_scope", True),
        ("error=invalid_scope", True),
        ("error=invalid_request&error_description=redirect_mismatch", False),
        ("error=invalid_request", False),
        ("error_description=invalid_scope", False),
        ("code=granted", False),
    ],
    ids=[
        "cognito-form",
        "rfc-form",
        "other-description",
        "bare-invalid-request",
        "description-only",
        "callback-without-error",
    ],
)
def test_the_ceiling_row_accepts_only_an_invalid_scope_refusal(
    loopback: tuple[_FakeDeployment, str], query: str, passes: bool
) -> None:
    """Cognito's `invalid_request` + `invalid_scope` description and RFC 6749's `invalid_scope`
    both count; any other error, or none, does not."""
    loopback[0].invalid_scope_query = query
    row = _run(loopback)["authorize_gateway_scope_invalid_scope"]
    assert (row.status == "pass") is passes, row.detail


def test_an_absent_oauth_client_output_fails_loudly_and_never_passes(
    loopback: tuple[_FakeDeployment, str],
) -> None:
    fake, _ = loopback
    outputs = {k: v for k, v in _OUTPUTS.items() if k != "cognito_mcp_oauth_client_id"}
    rows = _run(loopback, outputs=outputs)
    assert _failed(rows) == set(module.DISCOVERY_CHECKS[3:])
    for name in module.DISCOVERY_CHECKS[3:]:
        assert "cognito_mcp_oauth_client_id" in rows[name].detail
    assert not any(urllib.parse.urlsplit(u).hostname == _AUTH_HOST for u in fake.urls)


def test_the_authorize_request_carries_an_s256_pkce_challenge_and_the_inspector_callback(
    loopback: tuple[_FakeDeployment, str],
) -> None:
    fake, _ = loopback
    _run(loopback)
    authorizes = [
        dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query))
        for u in fake.urls
        if urllib.parse.urlsplit(u).hostname == _AUTH_HOST
    ]
    assert [a["scope"] for a in authorizes] == [f"openid {module.CHAT_SCOPE}", "gateway/invoke"]
    for query in authorizes:
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", query["code_challenge"])
        assert query["redirect_uri"] == module.INSPECTOR_CALLBACK
        assert query["client_id"] == _OAUTH_CLIENT
        assert query["state"]
    assert authorizes[0]["code_challenge"] != authorizes[1]["code_challenge"]


def test_classify_authorize_reads_a_non_redirect_error_body() -> None:
    url = f"https://{_AUTH_HOST}/oauth2/authorize"
    refused = module.HttpReply(400, {}, json.dumps({"error": "invalid_scope"}), url)
    assert module.classify_authorize(refused)[0] == "error:invalid_scope"
    assert module.classify_authorize(module.HttpReply(400, {}, "nope", url))[0] == "other"


def test_classify_authorize_needs_a_4xx_json_error_without_a_redirect() -> None:
    """A login page that merely mentions `invalid_scope` refuses nothing."""
    url = f"https://{_AUTH_HOST}/oauth2/authorize"
    page = module.HttpReply(200, {}, "<p>errors such as invalid_scope</p>", url)
    assert module.classify_authorize(page)[0] == "other"
    text = module.HttpReply(400, {}, "invalid_scope", url)
    assert module.classify_authorize(text)[0] == "other"
    ok_json = module.HttpReply(200, {}, json.dumps({"error": "invalid_scope"}), url)
    assert module.classify_authorize(ok_json)[0] == "other"


def test_an_empty_in_book_account_fails_the_call_row(
    loopback: tuple[_FakeDeployment, str],
) -> None:
    class EmptyAccount(_FakeService):
        def provision(self) -> Any:
            return type("Fixture", (), {"token": _MACHINE, "in_book_account": ""})()

    rows = _run(loopback, service=EmptyAccount())
    assert _failed(rows) == {"g1_sdk_read_only_tools_call"}


def test_an_authorization_endpoint_with_whitespace_is_not_requested(
    loopback: tuple[_FakeDeployment, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, _ = loopback
    real = fake.handle

    def bad_endpoint(method: str, url: str, headers: Mapping[str, str], body: bytes) -> Any:
        status, answer_headers, answer = real(method, url, headers, body)
        if url.endswith("/openid-configuration"):
            document = json.loads(answer)
            document["authorization_endpoint"] = f"https://{_AUTH_HOST}/oauth2/authorize x"
            answer = json.dumps(document).encode()
        return status, answer_headers, answer

    monkeypatch.setattr(fake, "handle", bad_endpoint)
    rows = _run(loopback)
    assert _failed(rows) == set(module.DISCOVERY_CHECKS[3:])
    assert not any(urllib.parse.urlsplit(u).hostname == _AUTH_HOST for u in fake.urls)
    for name in module.DISCOVERY_CHECKS[3:]:
        assert "state=" not in rows[name].detail


def test_a_raising_cleanup_does_not_skip_the_next_one(
    loopback: tuple[_FakeDeployment, str],
) -> None:
    class Raising(_FakeService):
        def cleanup(self) -> Iterable[CheckResult]:
            raise RuntimeError("delete_item refused")

    user = _FakeUser()
    rows = _run(loopback, service=Raising(), user=user)
    assert user.cleaned
    assert rows["principal_cleanup"].status == "fail"
    assert "delete_item refused" in rows["principal_cleanup"].detail


def test_classify_authorize_never_puts_the_query_in_its_detail() -> None:
    url = f"https://{_AUTH_HOST}/oauth2/authorize"
    reply = module.HttpReply(302, {"location": "/login?state=secret-state&x=1"}, "", url)
    kind, detail = module.classify_authorize(reply)
    assert kind == "login"
    assert "secret-state" not in detail


def test_https_exchange_refuses_plain_http_before_sending() -> None:
    with pytest.raises(InsecureSchemeError):
        module.https_exchange("GET", "http://127.0.0.1:9/x", {}, None)


def test_gateway1_url_prefers_the_mcp_output_and_falls_back_to_the_bare_host() -> None:
    explicit = context_for_test(
        outputs={**_OUTPUTS, "agentcore_gateway_mcp_url": "https://explicit.example.com/mcp"}
    )
    assert module.gateway1_url(explicit) == "https://explicit.example.com/mcp"
    assert module.gateway1_url(context_for_test(outputs=_OUTPUTS)) == f"https://{_G1_HOST}/mcp"


def test_a_non_https_gateway_url_is_refused_before_any_request(
    loopback: tuple[_FakeDeployment, str],
) -> None:
    fake, _ = loopback
    rows = _run(loopback, outputs={**_OUTPUTS, "mcp_chat_gateway_url": f"http://{_G2_HOST}"})
    assert list(rows) == ["terraform_output_present"]
    assert rows["terraform_output_present"].status == "fail"
    assert fake.urls == []


def test_a_missing_gateway_url_output_fails_and_the_floor_is_one(
    loopback: tuple[_FakeDeployment, str],
) -> None:
    outputs = {k: v for k, v in _OUTPUTS.items() if k != "agentcore_gateway_url"}
    rows = _run(loopback, outputs=outputs)
    assert [r.status for r in rows.values()] == ["fail"]
    assert module.expected_check_count(context_for_test(outputs=outputs)).minimum == 1


@pytest.mark.parametrize("who", ["service", "user"])
def test_a_provisioning_failure_fails_its_row_and_still_cleans_up(
    loopback: tuple[_FakeDeployment, str], who: str
) -> None:
    service, user = _FakeService(fail=who == "service"), _FakeUser(fail=who == "user")
    rows = _run(loopback, service=service, user=user)
    expected = "g1_service_principal_provisioned" if who == "service" else "g2_user_provisioned"
    assert _failed(rows) == {expected}
    assert service.cleaned and user.cleaned


def test_cleanup_runs_when_a_session_raises(loopback: tuple[_FakeDeployment, str]) -> None:
    """A crash after both principals are provisioned still yields every cleanup row."""
    service, user = _FakeService(), _FakeUser()
    real = _loopback_runner(loopback[1])

    def crash_on_user(
        url: str,
        token: str | None,
        *,
        call_tool: str = "",
        call_arguments: Mapping[str, Any] | None = None,
    ) -> module.SessionOutcome:
        if token == _USER:
            raise RuntimeError("simulated crash")
        return real(url, token, call_tool=call_tool, call_arguments=call_arguments)

    ctx = context_for_test(outputs=_OUTPUTS, region=_REGION)
    rows: list[CheckResult] = []
    with pytest.raises(RuntimeError, match="simulated crash"):
        for row in module.checks(
            ctx,
            session_runner=crash_on_user,
            http_exchange=loopback[0].exchange,
            service_provisioner_factory=lambda _c: service,
            user_provisioner_factory=lambda _c: user,
        ):
            rows.append(row)
    assert service.cleaned and user.cleaned
    assert {"reps_row_deleted", "user_removed"} <= {r.check for r in rows}


def test_a_refused_connection_is_a_failing_outcome_not_an_exception() -> None:
    with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as server:
        port = server.server_address[1]
    outcome = module.run_session(f"http://127.0.0.1:{port}/mcp", None)
    assert outcome.status is None and outcome.tool_names is None
    assert outcome.error


def test_the_chat_scope_constant_matches_the_infra_local() -> None:
    """Contract: the scope this module expects in `scopes_supported` is the one Terraform sets."""
    main_tf = (REPO_ROOT / "infra" / "main.tf").read_text(encoding="utf-8")
    assert re.search(
        rf'^\s*chat_scope_identifier\s*=\s*"{re.escape(module.CHAT_SCOPE)}"', main_tf, re.MULTILINE
    )


def test_the_module_is_registered_in_layer_5() -> None:
    script = (REPO_ROOT / "scripts" / "verify_deployment.sh").read_text(encoding="utf-8")
    block = re.search(r"^LAYER_5_MODULES=\((.*?)^\)", script, re.MULTILINE | re.DOTALL)
    assert block is not None
    assert '"scripts.smoke.mcp_client_compat"' in block.group(1).split()
    assert module.LAYER == 5


def test_the_sdk_is_a_pinned_dev_dependency() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert '"mcp==2.3.0"' in pyproject
    assert re.search(r"^mcp==2\.3\.0$", lock, re.MULTILINE)


def test_the_cli_fails_loudly_on_missing_outputs(tmp_path: Path) -> None:
    python = REPO_ROOT / ".venv" / "bin" / "python3"
    if not python.exists():
        pytest.skip(".venv not provisioned in this environment")
    result = proc_run.run(
        [
            str(python),
            "-m",
            "scripts.smoke.mcp_client_compat",
            "--outputs",
            str(tmp_path / "absent.json"),
            "--json",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_EC2_METADATA_DISABLED": "true",
        },
        check=False,
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["resource_class"] == module.RESOURCE_CLASS
    assert [c["status"] for c in payload["checks"]] == ["fail"]


class HTTPStatusError(Exception):
    """Named to match the HTTP client's status error, which is how the walk recognises it."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.response = type("_Response", (), {"status_code": status_code})()


def _raised_from(outer: Exception, inner: BaseException, *, explicit: bool) -> Exception:
    try:
        try:
            raise inner
        except BaseException as exc:
            if explicit:
                raise outer from exc
            raise outer  # noqa: B904
    except Exception as raised:
        return raised


def test_http_status_of_follows_an_explicit_cause() -> None:
    assert (
        module._http_status_of(_raised_from(RuntimeError("x"), HTTPStatusError(403), explicit=True))
        == 403
    )


def test_http_status_of_traverses_exception_group_members_and_their_causes() -> None:
    member = _raised_from(RuntimeError("member"), HTTPStatusError(401), explicit=True)
    group = ExceptionGroup("task group", [ValueError("other"), member])
    assert module._http_status_of(group) == 401
    assert module._http_status_of(ExceptionGroup("g", [HTTPStatusError(429)])) == 429


def test_http_status_of_does_not_follow_a_context_only_chain() -> None:
    """A status error that is only the implicit ``__context__`` is not the failure's cause."""
    raised = _raised_from(RuntimeError("x"), HTTPStatusError(403), explicit=False)
    assert raised.__cause__ is None
    assert isinstance(raised.__context__, HTTPStatusError)
    assert module._http_status_of(raised) is None
    group = ExceptionGroup("task group", [raised])
    assert module._http_status_of(group) is None
