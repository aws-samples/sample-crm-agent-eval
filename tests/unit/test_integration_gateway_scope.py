# SPDX-License-Identifier: MIT
"""Offline proof for `scripts/smoke/integration_gateway_scope.py`.

No network call is made. `_FakeGateway` is an in-memory model of the path the script asserts: it
advertises the tool under AgentCore's `${target_name}___${tool_name}` namespacing, strips any
caller-supplied `_identity` on `tools/call` (or merges it, for the negative tests), and applies
`crm.authz.require_scope` against the token's verified identity. Denials are rendered from
`crm.errors.ScopeDenied.to_api_response()` as MCP text content.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import scripts.smoke.integration_gateway_scope as smoke
from scripts.smoke.demo_tenant_guard import DEMO_TENANT, WATCHED_TABLES
from scripts.smoke.gateway_tools import ServicePrincipalFixture
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.integration_gateway_scope import (
    DECIDER_TOOLS,
    FILTER_TOOL,
    SERVICE_PRINCIPAL_MISSING_SCOPE,
    McpResult,
    _alternate_channel_forgeries,
    _forged_identity_block,
    _nested_decoy_arguments,
    _nested_forged_arguments,
    checks,
)

from crm.authz import Identity, require_proposal_decider, require_scope
from crm.envelope import IDENTITY_CHANNEL_KEYS, IDENTITY_KEY
from crm.errors import CrmError, NotFound, ScopeDenied, ValidationFailed

_TOKEN = "smoke-token-1"  # nosec B105  # test sentinel, not a credential
_IN_BOOK_ACCOUNT = "acct-in-book"
_OUT_OF_BOOK_ACCOUNT = "acct-out-of-book"
_MCP_TOOL_NAME = "get-account___get_account"

_VERIFIED_IDENTITY = Identity(
    rep_id="rep-verified",
    book=frozenset({_IN_BOOK_ACCOUNT}),
    principal_type="service",
    source="gateway",
)

_DECIDER_MCP_NAMES = {name: f"{name.replace('_', '-')}___{name}" for name, _ in DECIDER_TOOLS}
_DECIDER_CHECK_NAMES = {f"service_principal_refused_{name}" for name, _ in DECIDER_TOOLS}
_FILTER_MCP_NAME = f"{FILTER_TOOL.replace('_', '-')}___{FILTER_TOOL}"
_FILTER_IN_BOOK_CHECK = "in_book_account_filter_succeeds"
_FILTER_OUT_OF_BOOK_CHECK = "out_of_book_account_filter_denied_naming_scope"

_ALL_CHECK_NAMES = {
    "initialize_handshake",
    "mcp_tool_name_resolved",
    "in_book_target_succeeds",
    "out_of_book_target_denied_naming_scope",
    "forged_identity_cannot_widen_scope",
    "forged_identity_cannot_narrow_scope",
    "forged_identity_changes_no_response_bytes_in_book",
    "forged_identity_changes_no_response_bytes_out_of_book",
    "forged_alternate_channel_keys_discarded",
    "undeclared_argument_refused_by_closed_schema",
    "forged_nested_identity_discarded",
    "forged_nested_identity_changes_no_response_bytes",
    _FILTER_IN_BOOK_CHECK,
    _FILTER_OUT_OF_BOOK_CHECK,
} | _DECIDER_CHECK_NAMES

#: The two tenant-boundary assertions the mutating (provisioned) path must publish.
_FR93_CHECK_NAMES = {"demo_tenant_untouched", "writes_only_under_smoke_tenant"}


def _schema_refusal(
    *, extra: Mapping[str, Any] | None = None, envelope_extra: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The action Lambda's closed-schema refusal; `extra` merges into its error block and
    `envelope_extra` into the envelope (its `"error"` member into the error block)."""
    error: dict[str, Any] = {
        "code": "validation_failed",
        "message": "The request arguments did not match this tool's input schema.",
        "source": "arguments",
        "next_step": ValidationFailed.next_step,
    }
    error.update(extra or {})
    envelope: dict[str, Any] = {}
    for key, value in (envelope_extra or {}).items():
        if key == "error":
            error.update(value)
        else:
            envelope[key] = value
    return _content_envelope_response(
        int(envelope.pop("statusCode", 400)), {**envelope, "error": error}
    )


def _content_envelope_response(status_code: int, body: Mapping[str, Any]) -> dict[str, Any]:
    """Render the action Lambda envelope the way a Gateway Lambda target hands it back."""
    envelope = {"statusCode": status_code, **body}
    return {
        "jsonrpc": "2.0",
        "id": "smoke-1",
        "result": {
            "isError": False,
            "content": [{"type": "text", "text": json.dumps(envelope)}],
        },
    }


#: The `initialize` answer every hand-written gateway below gives.
_INITIALIZE_OK: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": "smoke-1",
    "result": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "gw"}},
}


class _FakeGateway:
    """In-memory stand-in for the Gateway REQUEST interceptor + `get_account` action Lambda.

    `merge_identity` controls whether a caller-supplied `_identity` is discarded before scope
    enforcement or (for the negative tests) merged into the verified one. `known_accounts` models
    `get_account`'s `NotFound`, which is raised before scope is evaluated. `filter_mode` models
    `search_opportunities`' explicit `account_id` filter: `"deny"` (the correct scope check before
    any read), `"allow"` (no check), `"not_found"` or `"leak"` (a denial carrying `detail`).
    """

    def __init__(
        self,
        *,
        token_identity: Identity,
        merge_identity: bool = False,
        known_accounts: frozenset[str] = frozenset({_IN_BOOK_ACCOUNT, _OUT_OF_BOOK_ACCOUNT}),
        advertise_tool: bool = True,
        decider_gate: bool = True,
        advertise_deciders: bool = True,
        refuse_initialize: bool = False,
        open_schema: bool = False,
        merge_nested: bool = False,
        merge_alternate: bool = False,
        refusal_echoes_nested_identity: bool = False,
        leak_schema_detail: bool = False,
        refusal_extra: Mapping[str, Any] | None = None,
        advertise_filter_tool: bool = True,
        filter_mode: str = "deny",
    ) -> None:
        self._token_identity = token_identity
        self._merge_identity = merge_identity
        self._known_accounts = known_accounts
        self._advertise_tool = advertise_tool
        self._decider_gate = decider_gate
        self._advertise_deciders = advertise_deciders
        self._refuse_initialize = refuse_initialize
        self._open_schema = open_schema or merge_nested
        self._merge_nested = merge_nested
        self._merge_alternate = merge_alternate
        self._refusal_echoes_nested_identity = refusal_echoes_nested_identity
        self._leak_schema_detail = leak_schema_detail
        self._refusal_extra = dict(refusal_extra or {})
        self._advertise_filter_tool = advertise_filter_tool
        self._filter_mode = filter_mode
        self.calls: list[tuple[str, Mapping[str, Any], str | None]] = []

    def __call__(self, method: str, params: Mapping[str, Any], token: str | None) -> McpResult:
        self.calls.append((method, params, token))
        if token != _TOKEN:
            return {
                "jsonrpc": "2.0",
                "id": "smoke-1",
                "error": {"code": -32001, "message": "Authentication failed. Sign in again."},
            }
        if method == "initialize":
            if self._refuse_initialize:
                return {"jsonrpc": "2.0", "id": "smoke-1", "error": {"message": "Invalid Request"}}
            return {
                "jsonrpc": "2.0",
                "id": "smoke-1",
                "result": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "gw"}},
            }
        if method == "tools/list":
            tools = [{"name": _MCP_TOOL_NAME}] if self._advertise_tool else []
            if self._advertise_deciders:
                tools += [{"name": mcp_name} for mcp_name in _DECIDER_MCP_NAMES.values()]
            if self._advertise_filter_tool:
                tools.append({"name": _FILTER_MCP_NAME})
            return {"jsonrpc": "2.0", "id": "smoke-1", "result": {"tools": tools}}
        if method != "tools/call":
            return {"jsonrpc": "2.0", "id": "smoke-1", "error": {"message": "unsupported"}}
        if params.get("name") in _DECIDER_MCP_NAMES.values():
            return self._decide(str(params.get("name")))
        if params.get("name") == _FILTER_MCP_NAME:
            return self._search(dict(params.get("arguments") or {}))
        if params.get("name") != _MCP_TOOL_NAME:
            return {
                "jsonrpc": "2.0",
                "id": "smoke-1",
                "error": {"code": -32601, "message": "tool not found"},
            }

        arguments = dict(params.get("arguments") or {})
        account_id = str(arguments["account_id"])
        forged = arguments.get("_identity")
        if not self._open_schema:
            undeclared = set(arguments) - set(IDENTITY_CHANNEL_KEYS) - {"account_id"}
            if undeclared:
                extra: dict[str, Any] = {}
                nested = arguments.get("tool_input")
                if (
                    self._refusal_echoes_nested_identity
                    and isinstance(nested, Mapping)
                    and IDENTITY_KEY in nested
                ):
                    extra["message"] = f"Rejected keys: {', '.join(sorted(nested))}."
                if self._leak_schema_detail:
                    extra["detail"] = "jsonschema: Additional properties are not allowed"
                return _schema_refusal(extra=extra, envelope_extra=self._refusal_extra)
        if account_id not in self._known_accounts:
            return _content_envelope_response(
                404, NotFound(f"Account {account_id} was not found.").to_api_response()
            )

        merge = self._merge_identity
        honoured = self._honoured_forgery(arguments)
        if honoured is not None:
            forged, merge = honoured, True

        effective_identity = self._token_identity
        if merge and isinstance(forged, Mapping):
            effective_identity = self._merged(forged)

        try:
            require_scope(effective_identity, account_id, operation="get_account")
        except ScopeDenied as exc:
            return _content_envelope_response(403, exc.to_api_response())

        return _content_envelope_response(
            200, {"account": {"account_id": account_id}, "contacts": []}
        )

    def _honoured_forgery(self, arguments: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """The nested or alternate-channel forgery a broken gateway honours, or `None`."""
        nested = arguments.get("tool_input")
        if self._merge_nested and isinstance(nested, Mapping):
            for key in sorted(IDENTITY_CHANNEL_KEYS):
                value = nested.get(key)
                if isinstance(value, Mapping) and "book" in value:
                    return value
        if self._merge_alternate:
            for key in sorted(set(IDENTITY_CHANNEL_KEYS) - {IDENTITY_KEY}):
                value = arguments.get(key)
                if isinstance(value, Mapping):
                    return value
        return None

    def _search(self, arguments: Mapping[str, Any]) -> McpResult:
        """`search_opportunities` filtered to `account_id`, per `filter_mode`."""
        account_id = str(arguments["account_id"])
        rows = {"opportunities": [{"opportunity_id": "opp-1", "account_id": account_id}]}
        if self._filter_mode == "allow":
            return _content_envelope_response(200, rows)
        try:
            require_scope(self._token_identity, account_id, operation="search_opportunities")
        except ScopeDenied as exc:
            if self._filter_mode == "not_found":
                return _content_envelope_response(
                    404, NotFound(f"Account {account_id} was not found.").to_api_response()
                )
            body = exc.to_api_response()
            if self._filter_mode == "leak":
                body["error"]["detail"] = "operation=search_opportunities rep=rep-verified"
            return _content_envelope_response(403, body)
        return _content_envelope_response(200, rows)

    def _decide(self, mcp_name: str) -> McpResult:
        """The decider handlers' order: refuse a non-person first, then look the target up."""
        if self._decider_gate:
            try:
                require_proposal_decider(self._token_identity, operation=mcp_name)
            except ScopeDenied as exc:
                return _content_envelope_response(403, exc.to_api_response())
        return _content_envelope_response(404, NotFound("No such proposal.").to_api_response())

    def _merged(self, forged: Mapping[str, Any]) -> Identity:
        """Return the (buggy) merge of the verified identity with a caller-supplied one."""
        if forged.get("book") == []:
            # An explicit empty caller-supplied book, under a merge policy, shrinks the effective
            # book to nothing rather than leaving the verified book untouched.
            return Identity(
                rep_id=self._token_identity.rep_id,
                book=frozenset(),
                full_visibility=bool(forged.get("full_visibility"))
                or self._token_identity.full_visibility,
            )
        book = frozenset(str(a) for a in (forged.get("book") or []))
        return Identity(
            rep_id=self._token_identity.rep_id,
            book=self._token_identity.book | book,
            full_visibility=bool(forged.get("full_visibility"))
            or self._token_identity.full_visibility,
        )


def _ctx() -> Any:
    return context_for_test(outputs={"agentcore_gateway_url": "https://gw.example.test"})


def _statuses(results: list[Any]) -> dict[str, str]:
    return {r.check: r.status for r in results}


def _run(gateway: Any, *, token: str | None = _TOKEN) -> dict[str, str]:
    return _statuses(
        checks(
            _ctx(),
            mcp_caller=gateway,
            token=token,
            in_book_account=_IN_BOOK_ACCOUNT,
            out_of_book_account=_OUT_OF_BOOK_ACCOUNT,
        )
    )


# --------------------------------------------------------------------------------------
# The whole suite, on the correct (replace, not merge) behaviour
# --------------------------------------------------------------------------------------


def test_every_check_passes_when_the_interceptor_replaces_identity() -> None:
    statuses = _run(_FakeGateway(token_identity=_VERIFIED_IDENTITY))
    assert statuses == dict.fromkeys(_ALL_CHECK_NAMES, "pass")


# --------------------------------------------------------------------------------------
# Branch: the Gateway's namespaced tool name is resolved from tools/list
# --------------------------------------------------------------------------------------


def test_tool_name_is_resolved_from_the_gateway_namespaced_inventory() -> None:
    gateway = _FakeGateway(token_identity=_VERIFIED_IDENTITY)
    results = checks(
        _ctx(),
        mcp_caller=gateway,
        token=_TOKEN,
        in_book_account=_IN_BOOK_ACCOUNT,
        out_of_book_account=_OUT_OF_BOOK_ACCOUNT,
    )
    resolution = next(r for r in results if r.check == "mcp_tool_name_resolved")
    assert resolution.status == "pass"
    assert _MCP_TOOL_NAME in resolution.detail
    assert all(
        call[1].get("name") == _MCP_TOOL_NAME
        for call in gateway.calls
        if call[0] == "tools/call"
        and call[1].get("name") not in {*_DECIDER_MCP_NAMES.values(), _FILTER_MCP_NAME}
    )


def test_unresolvable_tool_name_fails_its_own_check_and_does_not_skip_the_others() -> None:
    """A Gateway advertising no `get_account` fails the resolution check, and every scope check
    still executes and reports its own outcome rather than being silently skipped.
    """
    gateway = _FakeGateway(token_identity=_VERIFIED_IDENTITY, advertise_tool=False)
    statuses = _run(gateway)
    assert statuses["mcp_tool_name_resolved"] == "fail"
    assert set(statuses) == _ALL_CHECK_NAMES


# --------------------------------------------------------------------------------------
# Branch: in-book target succeeds
# --------------------------------------------------------------------------------------


def test_in_book_target_reports_fail_when_denied() -> None:
    """Non-vacuity: if the in-book account is (wrongly) denied, the check must fail, not pass."""
    gateway = _FakeGateway(token_identity=Identity(rep_id="rep-verified", book=frozenset()))
    assert _run(gateway)["in_book_target_succeeds"] == "fail"


# --------------------------------------------------------------------------------------
# Branch: out-of-book target denied, naming the missing scope
# --------------------------------------------------------------------------------------


def test_out_of_book_target_reports_fail_when_wrongly_allowed() -> None:
    """Non-vacuity: an identity with full visibility must not make this check pass."""
    gateway = _FakeGateway(token_identity=Identity(rep_id="rep-verified", full_visibility=True))
    assert _run(gateway)["out_of_book_target_denied_naming_scope"] == "fail"


def test_out_of_book_target_reports_fail_when_denial_does_not_name_the_scope() -> None:
    """Non-vacuity: a denial that does not name the missing scope must not pass this check."""

    def generic_denial_gateway(
        method: str, params: Mapping[str, Any], token: str | None
    ) -> McpResult:
        if method == "initialize":
            return _INITIALIZE_OK
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": "x", "result": {"tools": [{"name": _MCP_TOOL_NAME}]}}
        return _content_envelope_response(
            403, {"error": {"code": "scope_denied", "message": "Access denied."}}
        )

    assert _run(generic_denial_gateway)["out_of_book_target_denied_naming_scope"] == "fail"


def test_out_of_book_target_reports_fail_on_not_found_rather_than_accepting_it() -> None:
    """A `not_found` means scope was never evaluated; it is never accepted as the denial."""
    gateway = _FakeGateway(
        token_identity=_VERIFIED_IDENTITY, known_accounts=frozenset({_IN_BOOK_ACCOUNT})
    )
    statuses = _run(gateway)
    assert statuses["out_of_book_target_denied_naming_scope"] == "fail"


def test_out_of_book_denial_reports_fail_when_it_leaks_internal_detail() -> None:
    """Non-vacuity: a denial leaking a `detail` key must fail even though it names the scope."""

    def leaking_gateway(method: str, params: Mapping[str, Any], token: str | None) -> McpResult:
        if method == "initialize":
            return _INITIALIZE_OK
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": "x", "result": {"tools": [{"name": _MCP_TOOL_NAME}]}}
        arguments = dict(params.get("arguments") or {})
        if str(arguments["account_id"]) == _IN_BOOK_ACCOUNT:
            return _content_envelope_response(200, {"account": {}, "contacts": []})
        return _content_envelope_response(
            403,
            {
                "error": {
                    "code": "scope_denied",
                    "message": f"denied (missing scope: account:{_OUT_OF_BOOK_ACCOUNT})",
                    "detail": "operation=get_account rep=rep-verified scope=1-accounts",
                }
            },
        )

    assert _run(leaking_gateway)["out_of_book_target_denied_naming_scope"] == "fail"


# --------------------------------------------------------------------------------------
# Branch: caller-supplied _identity does not change the outcome (both directions)
# --------------------------------------------------------------------------------------


def test_forged_identity_widening_scope_is_detected_as_a_failure() -> None:
    """Non-vacuity: if the interceptor merged (widened) the caller-supplied `_identity`, the
    out-of-book account becomes reachable, and the check must fail, not pass.
    """
    gateway = _FakeGateway(token_identity=_VERIFIED_IDENTITY, merge_identity=True)
    assert _run(gateway)["forged_identity_cannot_widen_scope"] == "fail"


def test_forged_identity_narrowing_scope_is_detected_as_a_failure() -> None:
    """Non-vacuity: if the interceptor merged (narrowed via an empty book) the caller-supplied
    `_identity`, the token's own in-book account becomes unreachable, and the check must fail.
    """
    gateway = _FakeGateway(token_identity=_VERIFIED_IDENTITY, merge_identity=True)
    assert _run(gateway)["forged_identity_cannot_narrow_scope"] == "fail"


def test_identity_check_cannot_pass_vacuously_on_an_unobservable_result() -> None:
    """Non-vacuity guard: if the transport returns nothing that reveals allow/deny/which-scope,
    every check must report `fail` ("cannot prove"), never a silent `pass`.
    """

    def unobservable_gateway(
        method: str, params: Mapping[str, Any], token: str | None
    ) -> McpResult:
        if method == "initialize":
            return _INITIALIZE_OK
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": "x", "result": {"tools": [{"name": _MCP_TOOL_NAME}]}}
        return {"jsonrpc": "2.0", "id": "x", "error": {"message": "something went wrong"}}

    statuses = _run(unobservable_gateway)
    assert statuses["forged_identity_cannot_widen_scope"] == "fail"
    assert statuses["forged_identity_cannot_narrow_scope"] == "fail"
    assert statuses["in_book_target_succeeds"] == "fail"
    assert statuses["out_of_book_target_denied_naming_scope"] == "fail"


# --------------------------------------------------------------------------------------
# Branch: byte-identical responses with and without a forged _identity
# --------------------------------------------------------------------------------------


def test_byte_equality_checks_fail_when_a_forged_identity_changes_the_response() -> None:
    """Non-vacuity: a merging interceptor changes the response bytes on both targets."""
    gateway = _FakeGateway(token_identity=_VERIFIED_IDENTITY, merge_identity=True)
    statuses = _run(gateway)
    assert statuses["forged_identity_changes_no_response_bytes_in_book"] == "fail"
    assert statuses["forged_identity_changes_no_response_bytes_out_of_book"] == "fail"


def test_byte_equality_check_fails_on_any_difference_even_within_the_same_outcome() -> None:
    """A forged `_identity` echoed back changes nothing about allow/deny, and must still fail."""

    def echoing_gateway(method: str, params: Mapping[str, Any], token: str | None) -> McpResult:
        if method == "initialize":
            return _INITIALIZE_OK
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": "x", "result": {"tools": [{"name": _MCP_TOOL_NAME}]}}
        arguments = dict(params.get("arguments") or {})
        account_id = str(arguments["account_id"])
        body: dict[str, Any] = {"account": {"account_id": account_id}, "contacts": []}
        if "_identity" in arguments:
            body["echoed_identity"] = arguments["_identity"]
        if account_id == _OUT_OF_BOOK_ACCOUNT:
            return _content_envelope_response(
                403,
                {
                    "error": {
                        "code": "scope_denied",
                        "message": "denied",
                        "missing_scope": f"account:{_OUT_OF_BOOK_ACCOUNT}",
                    },
                    **(
                        {"echoed_identity": arguments["_identity"]}
                        if "_identity" in arguments
                        else {}
                    ),
                },
            )
        return _content_envelope_response(200, body)

    statuses = _run(echoing_gateway)
    assert statuses["out_of_book_target_denied_naming_scope"] == "pass"
    assert statuses["forged_identity_cannot_widen_scope"] == "pass"
    assert statuses["forged_identity_cannot_narrow_scope"] == "pass"
    assert statuses["forged_identity_changes_no_response_bytes_in_book"] == "fail"
    assert statuses["forged_identity_changes_no_response_bytes_out_of_book"] == "fail"


def test_byte_equality_check_ignores_attribute_order_inside_the_embedded_envelope() -> None:
    """Two identical reads whose embedded envelope serializes its attributes in a different order
    are the same response, and must not be reported as a caller-induced change.
    """
    call_count = {"n": 0}

    def reordering_gateway(method: str, params: Mapping[str, Any], token: str | None) -> McpResult:
        if method == "initialize":
            return _INITIALIZE_OK
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": "x", "result": {"tools": [{"name": _MCP_TOOL_NAME}]}}
        call_count["n"] += 1
        account: dict[str, Any] = {
            "account_id": _IN_BOOK_ACCOUNT,
            "name": "Cascadia",
            "region": "NA",
        }
        if call_count["n"] % 2 == 0:
            account = {key: account[key] for key in reversed(list(account))}
        arguments = dict(params.get("arguments") or {})
        if str(arguments["account_id"]) == _OUT_OF_BOOK_ACCOUNT:
            return _content_envelope_response(
                403,
                {
                    "error": {
                        "code": "scope_denied",
                        "message": "denied",
                        "missing_scope": f"account:{_OUT_OF_BOOK_ACCOUNT}",
                    }
                },
            )
        return _content_envelope_response(200, {"account": account, "contacts": []})

    statuses = _run(reordering_gateway)
    assert statuses["forged_identity_changes_no_response_bytes_in_book"] == "pass"


def test_byte_equality_check_fails_when_a_call_did_not_complete() -> None:
    """Two unobserved responses are not evidence of equality."""

    def transport_failure_gateway(
        method: str, params: Mapping[str, Any], token: str | None
    ) -> McpResult:
        if method == "initialize":
            return _INITIALIZE_OK
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": "x", "result": {"tools": [{"name": _MCP_TOOL_NAME}]}}
        return "transport error calling https://gw.example.test/mcp: timed out"

    statuses = _run(transport_failure_gateway)
    assert statuses["forged_identity_changes_no_response_bytes_in_book"] == "fail"
    assert statuses["forged_identity_changes_no_response_bytes_out_of_book"] == "fail"


# --------------------------------------------------------------------------------------
# Inventory completeness, and the no-identity path
# --------------------------------------------------------------------------------------


def test_every_branch_is_reported_even_when_an_earlier_one_fails() -> None:
    gateway = _FakeGateway(
        token_identity=Identity(rep_id="rep-verified", book=frozenset()), merge_identity=True
    )
    results = checks(
        _ctx(),
        mcp_caller=gateway,
        token=_TOKEN,
        in_book_account=_IN_BOOK_ACCOUNT,
        out_of_book_account=_OUT_OF_BOOK_ACCOUNT,
    )
    assert set(_statuses(results)) == _ALL_CHECK_NAMES
    assert len(results) == len(_ALL_CHECK_NAMES)


def test_no_token_supplied_reports_fail_for_every_scope_branch() -> None:
    """An unobserved authorization boundary is not a passing one: a missing identity fails every
    scope branch rather than skipping it, and issues no `tools/call` at all.
    """
    gateway = _FakeGateway(token_identity=_VERIFIED_IDENTITY)
    statuses = _run(gateway, token=None)
    scope_branches = _ALL_CHECK_NAMES - {"mcp_tool_name_resolved"}
    assert all(statuses[name] == "fail" for name in scope_branches)
    assert [call[0] for call in gateway.calls] == ["tools/list"]


# --------------------------------------------------------------------------------------
# Provisioned path: the service principal fixture drives the assertions
# --------------------------------------------------------------------------------------


class _FakeProvisioner:
    """Offline stand-in for `LiveServicePrincipalProvisioner`; writes nothing anywhere."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.cleaned_up = False

    def provision(self) -> ServicePrincipalFixture:
        if self._fail:
            raise RuntimeError("no GATEWAY_SERVICE_PRINCIPAL_REP_ID configured")
        return ServicePrincipalFixture(
            rep_id="svc-mcp-gateway",
            tenant="eval-smoke-deadbeef",
            book=frozenset({_IN_BOOK_ACCOUNT}),
            in_book_account=_IN_BOOK_ACCOUNT,
            out_of_book_account=_OUT_OF_BOOK_ACCOUNT,
            token=_TOKEN,
        )

    def cleanup(self) -> list[CheckResult]:
        self.cleaned_up = True
        return [
            CheckResult(resource="smoke_tenant", check="tenant_left_clean", status="pass"),
        ]


class _FakeRowReader:
    """In-memory stand-in for the tenant-row seam the tenant-boundary assertions read through.

    Models the two tenants that matter: a fixed `demo` corpus that must not move, and this run's
    own disposable tenant. `demo_drift` simulates a run that touched `demo`; `raises` simulates an
    unreadable boundary; `smoke_rows` at zero simulates a run that committed nothing.
    """

    def __init__(
        self,
        *,
        smoke_rows: int = 3,
        demo_rows: int = 7,
        demo_drift: int = 0,
        raises: bool = False,
        smoke_row_tenant: str | None = None,
    ) -> None:
        self._smoke_rows = smoke_rows
        self._demo_rows = demo_rows
        self._demo_drift = demo_drift
        self._raises = raises
        self._smoke_row_tenant = smoke_row_tenant
        self._demo_reads = 0

    def __call__(self, table: str, tenant: str) -> list[Mapping[str, Any]]:
        if self._raises:
            raise RuntimeError("dynamodb unavailable")
        if tenant == DEMO_TENANT:
            self._demo_reads += 1
            second_sweep = self._demo_reads > len(WATCHED_TABLES)
            drifting = second_sweep and table == "accounts"
            rows = self._demo_rows + (self._demo_drift if drifting else 0)
            return [{"tenant": DEMO_TENANT} for _ in range(rows)]
        if table != "accounts":
            return []
        row_tenant = self._smoke_row_tenant if self._smoke_row_tenant is not None else tenant
        return [{"tenant": row_tenant} for _ in range(self._smoke_rows)]


def _fr93(results: list[Any]) -> dict[str, CheckResult]:
    return {r.check: r for r in results if r.check in _FR93_CHECK_NAMES}


def _provisioned(reader: Any) -> dict[str, CheckResult]:
    return _fr93(
        checks(
            _ctx(),
            mcp_caller=_FakeGateway(token_identity=_VERIFIED_IDENTITY),
            provisioner=_FakeProvisioner(),
            row_reader=reader,
        )
    )


def test_the_mutating_path_publishes_both_fr93_assertions() -> None:
    """The mutating path publishes both tenant-boundary assertions in its inventory."""
    published = _provisioned(_FakeRowReader(smoke_rows=3))
    assert set(published) == _FR93_CHECK_NAMES
    assert all(result.status == "pass" for result in published.values()), published
    assert "3 row(s)" in published["writes_only_under_smoke_tenant"].detail


def test_demo_tenant_drift_fails_rather_than_passing() -> None:
    """Non-vacuity: a run that changed a `demo` row count must fail, not pass."""
    published = _provisioned(_FakeRowReader(demo_drift=1))
    assert published["demo_tenant_untouched"].status == "fail"
    assert "changed during this run" in published["demo_tenant_untouched"].detail


def test_an_unreadable_boundary_fails_rather_than_passing() -> None:
    """A re-read that raises is a failure, never a silent pass."""
    published = _provisioned(_FakeRowReader(raises=True))
    assert published["demo_tenant_untouched"].status == "fail"
    assert published["writes_only_under_smoke_tenant"].status == "fail"


def test_a_run_that_wrote_nothing_cannot_pass_vacuously() -> None:
    """Zero observed rows under the run's own tenant is a failure."""
    published = _provisioned(_FakeRowReader(smoke_rows=0))
    assert published["writes_only_under_smoke_tenant"].status == "fail"
    assert "never observed" in published["writes_only_under_smoke_tenant"].detail


def test_a_row_carrying_another_tenant_fails() -> None:
    """The rows are re-checked attribute by attribute, not trusted to the index query."""
    published = _provisioned(_FakeRowReader(smoke_row_tenant="demo"))
    assert published["writes_only_under_smoke_tenant"].status == "fail"


class _PrincipalRowWorld:
    """Rows per `(table, tenant)`, with a provisioner that re-points the principal's `reps` row.

    Models `LiveServicePrincipalProvisioner` against an account whose `svc-mcp-gateway` row already
    lives in `demo`: `provision` moves that row to the smoke tenant and seeds smoke rows; `cleanup`
    restores the row to `demo` (unless `restore` is false) and tears the smoke tenant down.
    `demo_write` adds one `demo` account during the run that nothing reverts.
    """

    _SMOKE_TENANT = "eval-smoke-deadbeef"

    def __init__(self, *, restore: bool = True, demo_write: bool = False) -> None:
        self.rows: dict[tuple[str, str], int] = {
            ("reps", DEMO_TENANT): 2,
            ("accounts", DEMO_TENANT): 7,
        }
        self._restore = restore
        self._demo_write = demo_write

    def reader(self, table: str, tenant: str) -> list[Mapping[str, Any]]:
        return [{"tenant": tenant} for _ in range(self.rows.get((table, tenant), 0))]

    def _move(self, table: str, source: str, target: str) -> None:
        self.rows[(table, source)] = self.rows.get((table, source), 0) - 1
        self.rows[(table, target)] = self.rows.get((table, target), 0) + 1

    def provision(self) -> ServicePrincipalFixture:
        self.rows[("accounts", self._SMOKE_TENANT)] = 3
        self._move("reps", DEMO_TENANT, self._SMOKE_TENANT)
        if self._demo_write:
            self.rows[("accounts", DEMO_TENANT)] += 1
        return ServicePrincipalFixture(
            rep_id="svc-mcp-gateway",
            tenant=self._SMOKE_TENANT,
            book=frozenset({_IN_BOOK_ACCOUNT}),
            in_book_account=_IN_BOOK_ACCOUNT,
            out_of_book_account=_OUT_OF_BOOK_ACCOUNT,
            token=_TOKEN,
        )

    def cleanup(self) -> list[CheckResult]:
        if self._restore:
            self._move("reps", self._SMOKE_TENANT, DEMO_TENANT)
        for key in [key for key in self.rows if key[1] == self._SMOKE_TENANT]:
            del self.rows[key]
        return [CheckResult(resource="smoke_tenant", check="tenant_left_clean", status="pass")]


def _world_run(world: _PrincipalRowWorld) -> dict[str, CheckResult]:
    return _fr93(
        checks(
            _ctx(),
            mcp_caller=_FakeGateway(token_identity=_VERIFIED_IDENTITY),
            provisioner=world,
            row_reader=world.reader,
        )
    )


def test_a_restored_principal_row_move_leaves_the_demo_tenant_untouched() -> None:
    """The run re-points the principal's `demo` row and restores it; the delta is read after."""
    published = _world_run(_PrincipalRowWorld())
    assert published["demo_tenant_untouched"].status == "pass", published
    assert "'reps': 2" in published["demo_tenant_untouched"].detail


def test_non_vacuity_a_genuine_demo_write_alongside_the_move_still_fails() -> None:
    published = _world_run(_PrincipalRowWorld(demo_write=True))
    assert published["demo_tenant_untouched"].status == "fail"
    assert "'accounts': (7, 8)" in published["demo_tenant_untouched"].detail
    assert "reps" not in published["demo_tenant_untouched"].detail


def test_an_unrestored_principal_row_fails_the_demo_delta() -> None:
    published = _world_run(_PrincipalRowWorld(restore=False))
    assert published["demo_tenant_untouched"].status == "fail"
    assert "'reps': (2, 1)" in published["demo_tenant_untouched"].detail


class _RaisingCleanupWorld(_PrincipalRowWorld):
    """Restores the principal's row, then raises before reporting its own removal check."""

    def cleanup(self) -> list[CheckResult]:
        super().cleanup()
        raise RuntimeError("tenant teardown failed")


def test_a_raising_cleanup_is_a_fail_row_and_the_demo_delta_still_runs() -> None:
    world = _RaisingCleanupWorld()
    published = {
        r.check: r
        for r in checks(
            _ctx(),
            mcp_caller=_FakeGateway(token_identity=_VERIFIED_IDENTITY),
            provisioner=world,
            row_reader=world.reader,
        )
    }
    assert published["cleanup_service_principal"].status == "fail"
    assert "RuntimeError: tenant teardown failed" in published["cleanup_service_principal"].detail
    assert published["demo_tenant_untouched"].status == "pass", published
    assert "'reps': 2" in published["demo_tenant_untouched"].detail


def test_the_smoke_tenant_is_read_back_before_its_teardown() -> None:
    published = _world_run(_PrincipalRowWorld())
    assert published["writes_only_under_smoke_tenant"].status == "pass", published
    assert "4 row(s)" in published["writes_only_under_smoke_tenant"].detail


def test_provisioned_path_runs_every_assertion_and_always_cleans_up() -> None:
    provisioner = _FakeProvisioner()
    results = checks(
        _ctx(),
        mcp_caller=_FakeGateway(token_identity=_VERIFIED_IDENTITY),
        provisioner=provisioner,
        row_reader=_FakeRowReader(smoke_rows=3),
    )
    statuses = _statuses(results)
    assert statuses["provision_service_principal"] == "pass"
    assert all(statuses[name] == "pass" for name in _ALL_CHECK_NAMES)
    assert statuses["tenant_left_clean"] == "pass"
    assert provisioner.cleaned_up


def test_failed_provisioning_is_one_actionable_failure_and_still_cleans_up() -> None:
    provisioner = _FakeProvisioner(fail=True)
    results = checks(
        _ctx(),
        mcp_caller=_FakeGateway(token_identity=_VERIFIED_IDENTITY),
        provisioner=provisioner,
        row_reader=_FakeRowReader(smoke_rows=3),
    )
    statuses = _statuses(results)
    assert statuses["provision_service_principal"] == "fail"
    assert provisioner.cleaned_up


def test_an_explicit_token_takes_precedence_over_the_provisioner() -> None:
    provisioner = _FakeProvisioner()
    results = checks(
        _ctx(),
        mcp_caller=_FakeGateway(token_identity=_VERIFIED_IDENTITY),
        token=_TOKEN,
        in_book_account=_IN_BOOK_ACCOUNT,
        out_of_book_account=_OUT_OF_BOOK_ACCOUNT,
        provisioner=provisioner,
    )
    assert "provision_service_principal" not in _statuses(results)
    assert not provisioner.cleaned_up


# --------------------------------------------------------------------------------------
# main() / run_main integration: missing Terraform output fails cleanly
# --------------------------------------------------------------------------------------


def test_missing_gateway_url_output_fails_the_whole_resource_class(tmp_path: Path) -> None:
    from scripts.smoke.integration_gateway_scope import main

    outputs_file = tmp_path / "tf-outputs.json"
    outputs_file.write_text("{}", encoding="utf-8")

    exit_code = main(["--outputs", str(outputs_file)])
    assert exit_code == 1


# --------------------------------------------------------------------------------------
# Sanity: the fake gateway's denial shape really does come from the real taxonomy member
# --------------------------------------------------------------------------------------


def test_fake_gateway_denial_uses_the_real_scope_denied_taxonomy() -> None:
    """Confirms the fixture models the real wire contract, not an invented shape."""
    try:
        require_scope(Identity(rep_id="r", book=frozenset()), _OUT_OF_BOOK_ACCOUNT)
    except CrmError as exc:
        assert isinstance(exc, ScopeDenied)
        assert exc.code == "scope_denied"
        assert exc.missing_scope == f"account:{_OUT_OF_BOOK_ACCOUNT}"
        response = exc.to_api_response()
        assert set(response["error"].keys()) == {"code", "message", "missing_scope", "next_step"}
        assert "detail" not in response["error"]
    else:  # pragma: no cover - defensive; require_scope always raises here
        raise AssertionError("expected ScopeDenied")


# --------------------------------------------------------------------------------------
# main(): the operator override is `--gateway-token`, never the orchestrator's `--token`
# --------------------------------------------------------------------------------------


def _captured_main_kwargs(tmp_path: Path, monkeypatch: Any, argv: list[str]) -> dict[str, Any]:
    """Run `main(argv)` with `checks` replaced by a recorder; nothing provisions or reaches AWS."""
    import scripts.smoke.integration_gateway_scope as module

    outputs_file = tmp_path / "tf-outputs.json"
    outputs_file.write_text(
        json.dumps({"agentcore_gateway_url": {"value": "https://gateway.example.invalid"}}),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    def _record(_ctx: Any, **kwargs: Any) -> list[CheckResult]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(module, "checks", _record)
    module.main([*argv, "--json", "--outputs", str(outputs_file)])
    assert captured, "main never reached checks, so nothing below observed its wiring"
    return captured


def test_crm_smoke_token_env_does_not_disable_provisioning(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An exported end-user `CRM_SMOKE_TOKEN` is not read as the Gateway token, so the service
    principal is still provisioned and its machine token minted.
    """
    from scripts.smoke.gateway_tools import LiveServicePrincipalProvisioner

    monkeypatch.delenv("CRM_SMOKE_GATEWAY_TOKEN", raising=False)
    monkeypatch.setenv("CRM_SMOKE_TOKEN", "spa-id-token")
    captured = _captured_main_kwargs(tmp_path, monkeypatch, [])
    assert captured["token"] is None
    assert isinstance(captured["provisioner"], LiveServicePrincipalProvisioner)


def test_the_gateway_token_env_and_flag_are_the_operator_override(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("CRM_SMOKE_GATEWAY_TOKEN", "from-env")
    captured = _captured_main_kwargs(tmp_path, monkeypatch, [])
    assert captured["token"] == "from-env"
    assert captured["provisioner"] is None
    captured = _captured_main_kwargs(tmp_path, monkeypatch, ["--gateway-token", "from-flag"])
    assert captured["token"] == "from-flag"


def test_main_refuses_a_bare_token_flag(tmp_path: Path) -> None:
    """A bare `--token` is a usage error (exit 2); the module declares no `--token` constant, so
    the orchestrator's AST scan never selects it.
    """
    import pytest
    from scripts.smoke.integration_gateway_scope import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--token", "X", "--outputs", str(tmp_path / "does-not-exist.json")])
    assert exc_info.value.code == 2


# --------------------------------------------------------------------------------------
# The service principal is refused every proposal-deciding tool
# --------------------------------------------------------------------------------------


def _decider_rows(gateway: _FakeGateway) -> dict[str, CheckResult]:
    results = checks(
        _ctx(),
        mcp_caller=gateway,
        token=_TOKEN,
        in_book_account=_IN_BOOK_ACCOUNT,
        out_of_book_account=_OUT_OF_BOOK_ACCOUNT,
    )
    return {r.check: r for r in results if r.check in _DECIDER_CHECK_NAMES}


def test_the_service_principal_is_refused_every_decider_tool_with_403_scope_denied() -> None:
    gateway = _FakeGateway(token_identity=_VERIFIED_IDENTITY)
    rows = _decider_rows(gateway)
    assert set(rows) == _DECIDER_CHECK_NAMES
    assert {r.status for r in rows.values()} == {"pass"}
    called = [p["name"] for m, p, _ in gateway.calls if m == "tools/call"]
    assert set(_DECIDER_MCP_NAMES.values()) <= set(called)


def test_a_decider_that_does_not_gate_the_principal_fails_each_row() -> None:
    """Without the person-only gate the call reaches the lookup and answers not_found (404)."""
    rows = _decider_rows(_FakeGateway(token_identity=_VERIFIED_IDENTITY, decider_gate=False))
    assert {r.status for r in rows.values()} == {"fail"}
    assert all("expected 403" in r.detail for r in rows.values())


def test_a_refusal_naming_another_scope_fails() -> None:
    """A denial that is not the principal refusal (here a channel refusal) does not count."""
    agent_channel = Identity(
        rep_id="rep-verified",
        book=frozenset({_IN_BOOK_ACCOUNT}),
        principal_type="service",
        source="agent",
    )
    rows = _decider_rows(_FakeGateway(token_identity=agent_channel))
    assert {r.status for r in rows.values()} == {"fail"}
    assert all(SERVICE_PRINCIPAL_MISSING_SCOPE in r.detail for r in rows.values())


def test_a_person_token_is_not_refused_so_every_decider_row_fails() -> None:
    person = Identity(rep_id="rep-verified", book=frozenset({_IN_BOOK_ACCOUNT}), source="gateway")
    rows = _decider_rows(_FakeGateway(token_identity=person))
    assert {r.status for r in rows.values()} == {"fail"}


def test_an_unadvertised_decider_tool_fails_its_row() -> None:
    rows = _decider_rows(_FakeGateway(token_identity=_VERIFIED_IDENTITY, advertise_deciders=False))
    assert {r.status for r in rows.values()} == {"fail"}
    assert all("advertises no tool" in r.detail for r in rows.values())


def test_each_decider_call_carries_a_fresh_transaction_key_and_the_tools_id_argument() -> None:
    gateway = _FakeGateway(token_identity=_VERIFIED_IDENTITY)
    _decider_rows(gateway)
    by_name = {p["name"]: p["arguments"] for m, p, _ in gateway.calls if m == "tools/call"}
    keys = set()
    for tool_name, id_argument in DECIDER_TOOLS:
        arguments = by_name[_DECIDER_MCP_NAMES[tool_name]]
        assert set(arguments) == {id_argument, "transaction_key"}
        keys.add(arguments["transaction_key"])
    assert len(keys) == len(DECIDER_TOOLS)


def test_the_decider_tools_are_exactly_the_handlers_that_call_require_proposal_decider() -> None:
    handlers = Path(__file__).resolve().parents[2] / "src" / "crm" / "handlers"
    gated = {
        path.stem
        for path in handlers.glob("*.py")
        if "require_proposal_decider(" in path.read_text("utf-8")
    }
    assert {name for name, _ in DECIDER_TOOLS} == gated


# --------------------------------------------------------------------------------------
# MCP handshake, alternate and nested channel keys, closed schema
# --------------------------------------------------------------------------------------


def _results(gateway: Any) -> dict[str, CheckResult]:
    return {
        r.check: r
        for r in checks(
            _ctx(),
            mcp_caller=gateway,
            token=_TOKEN,
            in_book_account=_IN_BOOK_ACCOUNT,
            out_of_book_account=_OUT_OF_BOOK_ACCOUNT,
        )
    }


def test_the_initialize_handshake_is_the_first_call() -> None:
    gateway = _FakeGateway(token_identity=_VERIFIED_IDENTITY)
    results = _results(gateway)
    assert gateway.calls[0][0] == "initialize"
    assert results["initialize_handshake"].status == "pass"
    assert "2024-11-05" in results["initialize_handshake"].detail


def test_a_refused_initialize_handshake_fails_its_row_only() -> None:
    results = _results(_FakeGateway(token_identity=_VERIFIED_IDENTITY, refuse_initialize=True))
    assert results["initialize_handshake"].status == "fail"
    assert results["in_book_target_succeeds"].status == "pass"


def test_a_gateway_honouring_an_alternate_channel_key_fails_that_row() -> None:
    results = _results(_FakeGateway(token_identity=_VERIFIED_IDENTITY, merge_alternate=True))
    row = results["forged_alternate_channel_keys_discarded"]
    assert row.status == "fail"
    assert "reached out-of-book account" in row.detail
    assert results["forged_identity_cannot_widen_scope"].status == "pass"


def test_the_alternate_channel_forgeries_cover_both_non_identity_channel_keys() -> None:
    channels = {channel for channel, _ in _alternate_channel_forgeries(_OUT_OF_BOOK_ACCOUNT)}
    assert channels == set(IDENTITY_CHANNEL_KEYS) - {IDENTITY_KEY}


def test_the_nested_forgery_buries_every_channel_key_one_level_down() -> None:
    forged = _forged_identity_block(book=[], full_visibility=True, session_id="s")
    arguments = _nested_forged_arguments(_OUT_OF_BOOK_ACCOUNT, forged)
    assert set(IDENTITY_CHANNEL_KEYS) <= set(arguments["tool_input"])
    assert set(IDENTITY_CHANNEL_KEYS) & set(arguments) == set()
    decoy = _nested_decoy_arguments(_OUT_OF_BOOK_ACCOUNT)
    assert set(decoy) == set(arguments)
    assert set(IDENTITY_CHANNEL_KEYS) & set(decoy["tool_input"]) == set()


def test_an_undeclared_argument_refused_by_the_closed_schema_passes_with_its_control() -> None:
    results = _results(_FakeGateway(token_identity=_VERIFIED_IDENTITY))
    row = results["undeclared_argument_refused_by_closed_schema"]
    assert row.status == "pass", row.detail
    assert "in_book_target_succeeds" in row.detail


def test_an_open_schema_fails_the_closed_schema_rows() -> None:
    results = _results(_FakeGateway(token_identity=_VERIFIED_IDENTITY, open_schema=True))
    assert "schema is open" in results["undeclared_argument_refused_by_closed_schema"].detail
    nested = results["forged_nested_identity_discarded"]
    assert nested.status == "fail"
    assert "'scope_denied'" in nested.detail


def test_a_gateway_honouring_a_nested_channel_key_fails_both_nested_rows() -> None:
    results = _results(_FakeGateway(token_identity=_VERIFIED_IDENTITY, merge_nested=True))
    assert results["forged_nested_identity_discarded"].status == "fail"
    assert "nested" in results["forged_nested_identity_discarded"].detail
    assert results["forged_nested_identity_changes_no_response_bytes"].status == "fail"


def test_a_closed_schema_refusal_that_leaks_detail_fails() -> None:
    results = _results(_FakeGateway(token_identity=_VERIFIED_IDENTITY, leak_schema_detail=True))
    row = results["undeclared_argument_refused_by_closed_schema"]
    assert row.status == "fail" and "internal detail" in row.detail
    assert results["forged_nested_identity_discarded"].status == "fail"


def test_a_refusal_varying_with_the_nested_identity_fails_only_the_decoy_row() -> None:
    results = _results(
        _FakeGateway(token_identity=_VERIFIED_IDENTITY, refusal_echoes_nested_identity=True)
    )
    assert results["forged_nested_identity_discarded"].status == "pass"
    row = results["forged_nested_identity_changes_no_response_bytes"]
    assert row.status == "fail" and "changed the response" in row.detail


def test_a_byte_equal_pair_of_successes_fails_the_decoy_row() -> None:
    wide = Identity(
        rep_id="rep-verified",
        book=frozenset({_IN_BOOK_ACCOUNT, _OUT_OF_BOOK_ACCOUNT}),
        principal_type="service",
        source="gateway",
    )
    results = _results(_FakeGateway(token_identity=wide, open_schema=True))
    row = results["forged_nested_identity_changes_no_response_bytes"]
    assert row.status == "fail" and "forged half" in row.detail


def test_the_decoy_row_passes_naming_the_decoy() -> None:
    row = _results(_FakeGateway(token_identity=_VERIFIED_IDENTITY))[
        "forged_nested_identity_changes_no_response_bytes"
    ]
    assert row.status == "pass" and "decoy" in row.detail


def _rewriting_gateway(rewrite: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
    inner = _FakeGateway(token_identity=_VERIFIED_IDENTITY)

    def _call(method: str, params: Mapping[str, Any], token: str | None) -> McpResult:
        result = inner(method, params, token)
        if isinstance(result, dict) and "validation_failed" in json.dumps(result):
            return rewrite(result)
        return result

    return _call


def test_a_refusal_other_than_validation_failed_fails_the_closed_schema_row() -> None:
    def to_not_found(_result: dict[str, Any]) -> dict[str, Any]:
        return _content_envelope_response(404, NotFound("no such record").to_api_response())

    row = _results(_rewriting_gateway(to_not_found))["undeclared_argument_refused_by_closed_schema"]
    assert row.status == "fail" and "'not_found'" in row.detail


def test_the_smoke_refusal_key_set_is_the_real_validation_failed_rendering() -> None:
    """Contract: the smoke's exact key set and next_step are what `crm.errors` renders."""
    block = ValidationFailed("m", source="arguments").to_api_response()["error"]
    assert set(block) == smoke._REFUSAL_ERROR_KEYS
    assert block["next_step"] == smoke._REFUSAL_NEXT_STEP == "fix_arguments"


@pytest.mark.parametrize(
    ("rewrite", "expected"),
    [
        pytest.param(lambda error: error.pop("next_step"), "not exactly", id="next_step-missing"),
        pytest.param(
            lambda error: error.update(next_step="retry_later"),
            "'retry_later'",
            id="next_step-wrong",
        ),
        pytest.param(
            lambda error: error.update(hint="call again"), "not exactly", id="unknown-key"
        ),
    ],
)
def test_a_refusal_error_block_other_than_the_exact_contract_fails(
    rewrite: Callable[[dict[str, Any]], object], expected: str
) -> None:
    """A missing or wrong `next_step` fails, and so does an extra unknown key (control)."""

    def mutate(result: dict[str, Any]) -> dict[str, Any]:
        envelope = json.loads(result["result"]["content"][0]["text"])
        rewrite(envelope["error"])
        result["result"]["content"][0]["text"] = json.dumps(envelope)
        return result

    row = _results(_rewriting_gateway(mutate))["undeclared_argument_refused_by_closed_schema"]
    assert row.status == "fail", row.detail
    assert expected in row.detail


def test_a_validation_failed_naming_another_source_fails() -> None:
    row = _results(
        _rewriting_gateway(lambda _r: _schema_refusal(extra={"source": "page_context"}))
    )["undeclared_argument_refused_by_closed_schema"]
    assert row.status == "fail" and "'page_context'" in row.detail


def test_a_refusal_split_across_two_content_items_fails() -> None:
    def split(result: dict[str, Any]) -> dict[str, Any]:
        result["result"]["content"].append(
            {"type": "text", "text": json.dumps({"account_id": _OUT_OF_BOOK_ACCOUNT})}
        )
        return result

    row = _results(_rewriting_gateway(split))["undeclared_argument_refused_by_closed_schema"]
    assert row.status == "fail" and "exactly one content item" in row.detail


@pytest.mark.parametrize(
    "refusal_extra",
    [
        pytest.param({"body": {"account_id": _OUT_OF_BOOK_ACCOUNT}}, id="envelope-body"),
        pytest.param({"account": {"account_id": _OUT_OF_BOOK_ACCOUNT}}, id="envelope-record"),
        pytest.param(
            {"error": {"record": {"account_id": _OUT_OF_BOOK_ACCOUNT}}}, id="error-record"
        ),
        pytest.param({"statusCode": 200}, id="status-200"),
        pytest.param({"statusCode": 403}, id="status-403"),
    ],
)
def test_a_schema_refusal_carrying_data_or_a_wrong_status_fails_every_closed_schema_row(
    refusal_extra: Mapping[str, Any],
) -> None:
    results = _results(_FakeGateway(token_identity=_VERIFIED_IDENTITY, refusal_extra=refusal_extra))
    for check in (
        "undeclared_argument_refused_by_closed_schema",
        "forged_nested_identity_discarded",
        "forged_nested_identity_changes_no_response_bytes",
    ):
        assert results[check].status == "fail", (check, results[check].detail)


# --------------------------------------------------------------------------------------
# Explicit out-of-book account filter on search_opportunities over MCP
# --------------------------------------------------------------------------------------


def _filter_rows(gateway: Any) -> dict[str, CheckResult]:
    results = checks(
        _ctx(),
        mcp_caller=gateway,
        token=_TOKEN,
        in_book_account=_IN_BOOK_ACCOUNT,
        out_of_book_account=_OUT_OF_BOOK_ACCOUNT,
    )
    return {
        r.check: r for r in results if r.check in {_FILTER_IN_BOOK_CHECK, _FILTER_OUT_OF_BOOK_CHECK}
    }


def test_the_explicit_filter_rows_pass_when_the_out_of_book_filter_is_denied() -> None:
    gateway = _FakeGateway(token_identity=_VERIFIED_IDENTITY)
    rows = _filter_rows(gateway)
    assert {name: row.status for name, row in rows.items()} == {
        _FILTER_IN_BOOK_CHECK: "pass",
        _FILTER_OUT_OF_BOOK_CHECK: "pass",
    }
    assert f"account:{_OUT_OF_BOOK_ACCOUNT}" in rows[_FILTER_OUT_OF_BOOK_CHECK].detail
    filter_calls = [call for call in gateway.calls if call[1].get("name") == _FILTER_MCP_NAME]
    assert [call[1]["arguments"] for call in filter_calls] == [
        {"account_id": _IN_BOOK_ACCOUNT},
        {"account_id": _OUT_OF_BOOK_ACCOUNT},
    ]


def test_an_out_of_book_filter_that_succeeds_fails_the_row() -> None:
    rows = _filter_rows(_FakeGateway(token_identity=_VERIFIED_IDENTITY, filter_mode="allow"))
    assert rows[_FILTER_OUT_OF_BOOK_CHECK].status == "fail"
    assert "reached out-of-book account" in rows[_FILTER_OUT_OF_BOOK_CHECK].detail
    assert rows[_FILTER_IN_BOOK_CHECK].status == "pass"


def test_an_out_of_book_filter_answering_not_found_fails_the_row() -> None:
    rows = _filter_rows(_FakeGateway(token_identity=_VERIFIED_IDENTITY, filter_mode="not_found"))
    assert rows[_FILTER_OUT_OF_BOOK_CHECK].status == "fail"
    assert "scope was never evaluated" in rows[_FILTER_OUT_OF_BOOK_CHECK].detail


def test_an_out_of_book_filter_denial_leaking_detail_fails_the_row() -> None:
    rows = _filter_rows(_FakeGateway(token_identity=_VERIFIED_IDENTITY, filter_mode="leak"))
    assert rows[_FILTER_OUT_OF_BOOK_CHECK].status == "fail"
    assert "leaks internal detail" in rows[_FILTER_OUT_OF_BOOK_CHECK].detail


def test_an_out_of_book_filter_denial_naming_another_scope_fails_the_row() -> None:
    gateway = _FakeGateway(token_identity=_VERIFIED_IDENTITY)

    def renaming(method: str, params: Mapping[str, Any], token: str | None) -> McpResult:
        if params.get("name") == _FILTER_MCP_NAME:
            return _content_envelope_response(
                403,
                ScopeDenied("Access denied.", missing_scope="account:*").to_api_response(),
            )
        return gateway(method, params, token)

    rows = _filter_rows(renaming)
    assert rows[_FILTER_OUT_OF_BOOK_CHECK].status == "fail"
    assert f"account:{_OUT_OF_BOOK_ACCOUNT}" in rows[_FILTER_OUT_OF_BOOK_CHECK].detail


def test_an_in_book_filter_that_is_denied_fails_the_row() -> None:
    rows = _filter_rows(
        _FakeGateway(token_identity=Identity(rep_id="rep-verified", book=frozenset()))
    )
    assert rows[_FILTER_IN_BOOK_CHECK].status == "fail"


def test_an_unadvertised_filter_tool_fails_both_rows() -> None:
    rows = _filter_rows(
        _FakeGateway(token_identity=_VERIFIED_IDENTITY, advertise_filter_tool=False)
    )
    assert {row.status for row in rows.values()} == {"fail"}
    assert all(FILTER_TOOL in row.detail for row in rows.values())
