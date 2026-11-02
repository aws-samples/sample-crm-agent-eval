# SPDX-License-Identifier: MIT
"""Layer 5 smoke script: asserts scope enforcement survives the real Gateway-to-Lambda hop.

Needs a live deployment. Calls `get_account` over MCP against a live AgentCore Gateway: once for an
account inside the acting identity's verified book (must succeed), once for an account outside it
(must be denied with `crm.errors.ScopeDenied`'s `scope_denied` code, naming the missing scope), and
again with a caller-supplied `_identity` in `params.arguments` that first tries to widen scope
(claiming the out-of-book account, or full visibility) and then tries to narrow it (claiming an
empty book) — in both directions the effective scope must match the token's own verified identity,
not the caller-supplied one, proving the Gateway REQUEST interceptor
(`src/gateway/identity_interceptor.py`) replaces rather than merges `_identity` on this real path.
Two further checks compare the **response bytes** of a plain call and a forged call against the same
target: a caller-supplied `_identity` must change nothing observable at all, not merely stay on the
same side of the allow/deny boundary. Every denial is also asserted to carry no internal detail: no
`detail` key, no stack, no exception type, no ARN.

The run opens with the MCP `initialize` handshake a real external client sends. Forged
`_agent_identity` and `requestContext` blocks claiming the out-of-book account are refused
`scope_denied` like `_identity`. An undeclared argument is refused by the tool's closed schema
(`validation_failed`, `source: arguments`, exactly one `{statusCode: 400, error}` content item, no
data or internal detail); a forgery hiding every channel key inside one undeclared argument gets
the same refusal, byte-identical to a same-shaped decoy carrying no channel key. Expired and
underscoped tokens are refused by `scripts.smoke.gateway_tools`.

The acting identity is the Gateway service principal, provisioned by
`scripts/smoke/gateway_tools.LiveServicePrincipalProvisioner` under a disposable `eval-smoke-<hex>`
tenant and removed again; `demo` is never written. An operator may instead supply
`--gateway-token` (env `CRM_SMOKE_GATEWAY_TOKEN`) with their own
`--in-book-account`/`--out-of-book-account`, in which case nothing is provisioned.

The service principal is also refused each proposal-deciding tool (`approve_proposal`,
`reject_proposal`, `revert_audit_record`): each call must return an HTTP 403 envelope carrying
`scope_denied` with `missing_scope` `principal:service` (`crm.authz.require_proposal_decider`).
The target id names nothing, and the refusal precedes any read, so these calls write nothing.

`search_opportunities` with an explicit `account_id` filter is also called over MCP: the in-book
account must succeed, and the out-of-book account must get a user-safe `scope_denied` naming
`account:<id>`, never a success or a `not_found` (`crm.search.search_opportunities`).

A check reports `fail` whenever the result does not unambiguously reveal the outcome (allow vs.
`scope_denied`, the account named as the missing scope, and the response bytes).
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from scripts.smoke.demo_tenant_guard import (
    DEMO_TENANT,
    TenantRowReader,
    demo_tenant_untouched_check,
    read_tenant_snapshot,
    store_row_reader,
    writes_only_under_smoke_tenant_check,
)
from scripts.smoke.gateway_tools import (
    LiveServicePrincipalProvisioner,
    ServicePrincipalFixture,
    ServicePrincipalProvisioner,
    _is_success,
    _live_table_prefix,
    _rpc_error_code,
    _tool_error_code,
    _toolspec_name_of,
)
from scripts.smoke.gateway_tools import _content_envelope as _envelope_of
from scripts.smoke.gateway_tools import default_mcp_caller as _gateway_tools_mcp_caller
from scripts.smoke.harness import CheckResult, SmokeContext, run_main

from crm.envelope import AGENT_IDENTITY_KEY, IDENTITY_KEY, REQUEST_CONTEXT_KEY
from crm.errors import ValidationFailed
from crm.store.store import Store

LAYER = 5
RESOURCE_CLASS = "integration_gateway_scope"

#: One completed MCP JSON-RPC call: the decoded response body (whatever shape the Gateway
#: returned, including a JSON-RPC `error` member) or a `str` transport-failure message when no
#: response body was received at all. Mirrors `scripts/smoke/gateway_tools.py`'s `McpResult`.
McpResult = dict[str, Any] | str

#: `(method, params, token)` -> `McpResult`. The transport seam every check goes through.
McpCaller = Callable[[str, Mapping[str, Any], str | None], McpResult]

_TOOL_NAME = "get_account"

#: Account the smoke-run token's verified identity is expected to own (in-book).
_IN_BOOK_ACCOUNT = "smoke-account-in-book"

#: Account the smoke-run token's verified identity is expected NOT to own (out-of-book).
_OUT_OF_BOOK_ACCOUNT = "smoke-account-out-of-book"

#: `rep_id` every forged, caller-supplied `_identity` in this script claims. Never a real rep.
_FORGED_REP_ID: Final[str] = "attacker-forged-rep"

#: Undeclared argument the nested forgery hides every channel key inside, and the decoy reuses.
_NESTED_ARGUMENT_KEY: Final[str] = "tool_input"

#: Undeclared, non-identity argument the closed-schema probe sends.
_UNDECLARED_PROBE_KEY: Final[str] = "zzz_undeclared_probe"

#: MCP protocol version this client declares in `initialize`, per `docs/mcp-client-guide.md`.
_MCP_PROTOCOL_VERSION: Final[str] = "2024-11-05"

#: The `source` a toolspec input-validation refusal of the top-level arguments names.
_ARGUMENTS_SOURCE: Final[str] = "arguments"

#: The exact keys of the action Lambda's refusal envelope and of its error block.
_REFUSAL_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset({"statusCode", "error"})
_REFUSAL_ERROR_KEYS: Final[frozenset[str]] = frozenset({"code", "message", "source", "next_step"})

#: The `next_step` :mod:`crm.errors` assigns a ``validation_failed`` refusal.
_REFUSAL_NEXT_STEP: Final[str] = ValidationFailed.next_step

#: Each tool only a person may call, and the id argument it refuses a service principal before reading.
DECIDER_TOOLS: Final[tuple[tuple[str, str], ...]] = (
    ("approve_proposal", "proposal_id"),
    ("reject_proposal", "proposal_id"),
    ("revert_audit_record", "audit_id"),
)

#: The `missing_scope` `require_proposal_decider` names for a service principal.
SERVICE_PRINCIPAL_MISSING_SCOPE: Final[str] = "principal:service"

#: The id each decider call targets. It names no record.
_DECIDER_TARGET_ID: Final[str] = "smoke-no-such-decider-target"

#: The listing tool whose explicit `account_id` filter is scope-checked over MCP.
FILTER_TOOL: Final[str] = "search_opportunities"


def default_mcp_caller(
    method: str, params: Mapping[str, Any], token: str | None, *, gateway_url: str = ""
) -> McpResult:
    """Perform one MCP JSON-RPC call over HTTP POST against the live Gateway.

    Delegates to `scripts.smoke.gateway_tools.default_mcp_caller`.

    Args:
        gateway_url: The live Gateway base URL (the pinned `agentcore_gateway_url` Terraform
            output). `checks()` binds this via `functools.partial`. Falls back to
            `CRM_SMOKE_GATEWAY_URL` when not supplied.
    """
    base_url = gateway_url or os.environ.get("CRM_SMOKE_GATEWAY_URL", "")
    return _gateway_tools_mcp_caller(method, params, token, gateway_url=base_url)


def _token_from_env_or_flag(argv: Sequence[str] | None) -> str | None:
    """Resolve the Gateway MACHINE token: `--gateway-token` -> `CRM_SMOKE_GATEWAY_TOKEN` -> `None`.

    Never mints, signs, or fetches a token here; `None` means "provision the service principal and
    mint its own token" (see `checks`). Parsed out of `argv` ahead of `run_main`.
    """
    args = list(argv) if argv is not None else sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--gateway-token" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--gateway-token="):
            return arg.split("=", 1)[1]
    return os.environ.get("CRM_SMOKE_GATEWAY_TOKEN")


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


def _widen_forgeries(out_of_book_account: str) -> tuple[Mapping[str, Any], ...]:
    """Return the caller-supplied `_identity` payloads that attempt to widen scope."""
    return (
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


def _narrow_forgery() -> Mapping[str, Any]:
    """Return the caller-supplied `_identity` payload that attempts to narrow scope."""
    return {
        "rep_id": _FORGED_REP_ID,
        "book": [],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "forged-session-narrow",
        "source": "gateway",
    }


def _call_get_account(
    caller: McpCaller,
    account_id: str,
    token: str | None,
    *,
    forged_identity: Mapping[str, Any] | None,
    mcp_tool_name: str,
) -> McpResult:
    """Invoke `get_account` over MCP, optionally with a caller-supplied `_identity` argument."""
    arguments: dict[str, Any] = {"account_id": account_id}
    if forged_identity is not None:
        arguments["_identity"] = dict(forged_identity)
    return caller("tools/call", {"name": mcp_tool_name, "arguments": arguments}, token)


def _rpc_error(result: McpResult) -> Mapping[str, Any] | None:
    """Return the top-level JSON-RPC `error` member of `result`, or `None` when absent."""
    if isinstance(result, str):
        return None
    error_block = result.get("error")
    return error_block if isinstance(error_block, Mapping) else None


def _tool_error_block(result: McpResult) -> Mapping[str, Any] | None:
    """Return the nested tool-call error block a denied `get_account` carries, or `None`.

    Looks in both shapes the deployed path can produce: `result.error` (`CrmError.to_mcp_error`),
    and the action Lambda's own `{"statusCode", "error"}` envelope carried as MCP text content,
    which is what a Gateway Lambda target actually returns (`gateway_tools._content_envelope`).
    """
    if isinstance(result, str):
        return None
    outer = result.get("result")
    if isinstance(outer, Mapping):
        error_block = outer.get("error")
        if isinstance(error_block, Mapping):
            return error_block
    envelope = _envelope_of(result)
    if envelope is not None:
        envelope_error = envelope.get("error")
        if isinstance(envelope_error, Mapping):
            return envelope_error
    return None


def _denial_message(result: McpResult) -> str | None:
    """Extract the user-facing denial text from any of the shapes the live path can return."""
    rpc_error = _rpc_error(result)
    if rpc_error is not None:
        message = rpc_error.get("message")
        return str(message) if message is not None else None

    tool_error = _tool_error_block(result)
    if tool_error is not None:
        message = tool_error.get("message")
        if message is not None:
            return str(message)

    if not isinstance(result, str):
        outer = result.get("result")
        if isinstance(outer, Mapping) and outer.get("isError"):
            content = outer.get("content")
            if isinstance(content, list):
                for item in content:
                    text = item.get("text") if isinstance(item, Mapping) else None
                    if isinstance(text, str):
                        return text
    return None


def _is_scope_denied(result: McpResult) -> bool:
    """Return whether `result` is a denial specifically carrying `scope_denied`'s code.

    The code is read from the same three places a live denial can carry it (`_tool_error_code`
    covers both `result.error` and the content envelope, `_rpc_error_code` the JSON-RPC error),
    falling back to the `to_mcp_error` message form which embeds `(missing scope: ...)`.
    """
    if _tool_error_code(result) == "scope_denied" or _rpc_error_code(result) == "scope_denied":
        return True
    message = _denial_message(result)
    return message is not None and "missing scope" in message.lower()


def _is_not_found(result: McpResult) -> bool:
    """Return whether `result` is a `not_found` refusal.

    `get_account` raises `NotFound` before it evaluates scope, so a `not_found` in the out-of-book
    position is reported as its own failure, never accepted in place of `scope_denied`.
    """
    return _tool_error_code(result) == "not_found" or _rpc_error_code(result) == "not_found"


def _leaks_internal_detail(result: McpResult) -> bool:
    """Return whether `result` carries any internal-detail signal a user-safe denial must not.

    Checked at the top level, inside a JSON-RPC `error`, inside a nested tool-call error block, and
    inside the action Lambda's content envelope. Beyond a literal `detail` key, also flags a
    `Traceback`/exception-shaped message and an ARN in the user-facing text.
    """
    if isinstance(result, str):
        return False

    blocks: list[Mapping[str, Any]] = [result]
    rpc_error = _rpc_error(result)
    if rpc_error is not None:
        blocks.append(rpc_error)
    tool_error = _tool_error_block(result)
    if tool_error is not None:
        blocks.append(tool_error)
    envelope = _envelope_of(result)
    if envelope is not None:
        blocks.append(envelope)

    for block in blocks:
        if "detail" in block:
            return True

    message = _denial_message(result) or ""
    lowered = message.lower()
    return "traceback" in lowered or "exception" in lowered or "arn:" in lowered


def _missing_scope_names(result: McpResult, account_id: str) -> bool:
    """Return whether the denial for `account_id` names that account as the missing scope.

    Checked via the structured `missing_scope` field when present (`ScopeDenied.to_api_response`),
    falling back to a substring check against the denial message
    (`ScopeDenied.to_mcp_error`'s `f"{message} (missing scope: {self.missing_scope})"` form).
    """
    for block in (_tool_error_block(result), _rpc_error(result)):
        if block is None:
            continue
        missing_scope = block.get("missing_scope")
        if isinstance(missing_scope, str) and account_id in missing_scope:
            return True

    message = _denial_message(result) or ""
    return account_id in message


def _canonicalise(value: Any) -> Any:
    """Return `value` with every mapping key-sorted, recursing into embedded JSON text.

    A Gateway Lambda target returns the action Lambda's envelope as a JSON string inside
    `result.content[0].text`, whose attribute order varies between identical DynamoDB reads. Any
    string that parses to a JSON object or array is replaced by its canonical form; other strings are
    left untouched.
    """
    if isinstance(value, Mapping):
        return {key: _canonicalise(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list):
        return [_canonicalise(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value
        if isinstance(parsed, dict | list):
            return json.dumps(_canonicalise(parsed), sort_keys=True, separators=(",", ":"))
        return value
    return value


def _canonical_bytes(result: McpResult) -> bytes:
    """Return a canonical byte rendering of `result` for an exact-equality comparison.

    Keys are sorted and separators fixed at every nesting level, including inside embedded JSON
    text (see `_canonicalise`).
    """
    if isinstance(result, str):
        return result.encode("utf-8")
    return json.dumps(_canonicalise(result), sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(result: McpResult) -> str:
    """Return a short sha256 digest of `result`'s canonical bytes, for quotable evidence."""
    return hashlib.sha256(_canonical_bytes(result)).hexdigest()[:16]


def _no_token_failure(check_name: str) -> CheckResult:
    """Return the uniform failure for a check that had no token to act with."""
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check_name,
        status="fail",
        detail="no acting identity available (no --gateway-token/CRM_SMOKE_GATEWAY_TOKEN and no "
        "provisioned service principal); an authorization boundary that was never observed is "
        "not a passing one",
    )


def _resolve_mcp_tool_name(caller: McpCaller, token: str | None) -> tuple[str, CheckResult]:
    """Resolve the MCP-visible name for `get_account`, plus the check reporting that resolution.

    AgentCore Gateway namespaces every tool as `${target_name}___${tool_name}`, so the name is read
    from a live `tools/list`. When it cannot be resolved the check `fail`s and the bare toolspec name
    is returned, so the remaining checks still run.
    """
    check_name = "mcp_tool_name_resolved"
    result = caller("tools/list", {}, token)
    if isinstance(result, str):
        return _TOOL_NAME, CheckResult(
            resource=RESOURCE_CLASS, check=check_name, status="fail", detail=result
        )

    outer = result.get("result")
    tools = outer.get("tools") if isinstance(outer, Mapping) else None
    if not isinstance(tools, list):
        return _TOOL_NAME, CheckResult(
            resource=RESOURCE_CLASS,
            check=check_name,
            status="fail",
            detail=f"tools/list returned no tool inventory: {result!r}",
        )

    for tool in tools:
        name = tool.get("name") if isinstance(tool, Mapping) else None
        if isinstance(name, str) and _toolspec_name_of(name) == _TOOL_NAME:
            return name, CheckResult(
                resource=RESOURCE_CLASS,
                check=check_name,
                status="pass",
                detail=f"{_TOOL_NAME} is advertised as {name!r}",
            )

    return _TOOL_NAME, CheckResult(
        resource=RESOURCE_CLASS,
        check=check_name,
        status="fail",
        detail=f"tools/list advertises no tool resolving to {_TOOL_NAME!r}",
    )


def _resolve_tool_names(
    caller: McpCaller, token: str | None, wanted: frozenset[str]
) -> dict[str, str]:
    """Map each toolspec name in `wanted` to its Gateway-namespaced MCP name from `tools/list`.

    A tool the inventory does not advertise is absent from the map; its row then fails.
    """
    result = caller("tools/list", {}, token)
    outer = result.get("result") if isinstance(result, Mapping) else None
    tools = outer.get("tools") if isinstance(outer, Mapping) else None
    names: dict[str, str] = {}
    for tool in tools if isinstance(tools, list) else []:
        mcp_name = tool.get("name") if isinstance(tool, Mapping) else None
        if isinstance(mcp_name, str) and _toolspec_name_of(mcp_name) in wanted:
            names[_toolspec_name_of(mcp_name)] = mcp_name
    return names


def _envelope_status(result: McpResult) -> int | None:
    """The action Lambda envelope's `statusCode`, or `None` when the result carries no envelope."""
    envelope = _envelope_of(result)
    status = envelope.get("statusCode") if envelope is not None else None
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _check_service_principal_refused_decider(
    caller: McpCaller,
    token: str | None,
    *,
    tool_name: str,
    id_argument: str,
    mcp_names: Mapping[str, str],
) -> CheckResult:
    """Assert the service principal gets HTTP 403 `scope_denied` (`principal:service`) on `tool_name`."""
    check_name = f"service_principal_refused_{tool_name}"
    if token is None:
        return _no_token_failure(check_name)
    mcp_name = mcp_names.get(tool_name)
    if mcp_name is None:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check_name,
            status="fail",
            detail=f"tools/list advertises no tool resolving to {tool_name!r}",
        )
    arguments = {
        id_argument: _DECIDER_TARGET_ID,
        "transaction_key": f"smoke-decider-{uuid.uuid4().hex}",
    }
    result = caller("tools/call", {"name": mcp_name, "arguments": arguments}, token)
    if isinstance(result, str):
        return CheckResult(resource=RESOURCE_CLASS, check=check_name, status="fail", detail=result)
    status = _envelope_status(result)
    problems: list[str] = []
    if _is_success(result):
        problems.append("the call succeeded")
    if status != 403:
        problems.append(f"envelope statusCode={status!r}, expected 403")
    if not _is_scope_denied(result):
        problems.append("the denial does not carry scope_denied")
    if not _missing_scope_names(result, SERVICE_PRINCIPAL_MISSING_SCOPE):
        problems.append(f"the denial does not name {SERVICE_PRINCIPAL_MISSING_SCOPE!r}")
    if _leaks_internal_detail(result):
        problems.append("the denial leaks internal detail")
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check_name,
        status="fail" if problems else "pass",
        detail=(
            f"{'; '.join(problems)}: {result!r}"
            if problems
            else f"{mcp_name} refused with 403 scope_denied ({SERVICE_PRINCIPAL_MISSING_SCOPE})"
        ),
    )


def _check_in_book_succeeds(
    caller: McpCaller, token: str | None, in_book_account: str, mcp_tool_name: str
) -> CheckResult:
    """Assert the token's own verified identity can access its in-book account."""
    check_name = "in_book_target_succeeds"
    if token is None:
        return _no_token_failure(check_name)

    result = _call_get_account(
        caller, in_book_account, token, forged_identity=None, mcp_tool_name=mcp_tool_name
    )
    if isinstance(result, str):
        return CheckResult(resource=RESOURCE_CLASS, check=check_name, status="fail", detail=result)
    if not _is_success(result):
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check_name,
            status="fail",
            detail=f"expected success for in-book account {in_book_account!r}, got: {result!r}",
        )
    return CheckResult(resource=RESOURCE_CLASS, check=check_name, status="pass")


def _check_out_of_book_denied_naming_scope(
    caller: McpCaller, token: str | None, out_of_book_account: str, mcp_tool_name: str
) -> CheckResult:
    """Assert an out-of-book target is denied with `scope_denied` naming the missing scope."""
    check_name = "out_of_book_target_denied_naming_scope"
    if token is None:
        return _no_token_failure(check_name)

    result = _call_get_account(
        caller, out_of_book_account, token, forged_identity=None, mcp_tool_name=mcp_tool_name
    )
    if isinstance(result, str):
        return CheckResult(resource=RESOURCE_CLASS, check=check_name, status="fail", detail=result)
    if _is_success(result):
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check_name,
            status="fail",
            detail=(
                f"expected a scope_denied refusal for out-of-book account "
                f"{out_of_book_account!r}, got a success: {result!r}"
            ),
        )
    if _is_not_found(result):
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check_name,
            status="fail",
            detail=(
                f"out-of-book account {out_of_book_account!r} answered not_found, so scope was "
                "never evaluated; a not_found is never accepted in place of a scope_denied"
            ),
        )
    if not _is_scope_denied(result):
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check_name,
            status="fail",
            detail=f"denial does not carry the scope_denied code: {result!r}",
        )
    if not _missing_scope_names(result, out_of_book_account):
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check_name,
            status="fail",
            detail=(
                "denial carries a generic message that does not name the missing scope "
                f"({out_of_book_account!r}): {result!r}"
            ),
        )
    if _leaks_internal_detail(result):
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check_name,
            status="fail",
            detail=f"denial leaks internal detail: {result!r}",
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check_name,
        status="pass",
        detail=(
            "denial body (asserted above to carry no internal detail): "
            f"{json.dumps(_tool_error_block(result) or _rpc_error(result), sort_keys=True)}"
        ),
    )


def _check_forged_identity_cannot_widen_scope(
    caller: McpCaller, token: str | None, out_of_book_account: str, mcp_tool_name: str
) -> CheckResult:
    """Assert a caller-supplied `_identity` claiming the out-of-book account is still denied.

    Two widen attempts are tried: claiming the out-of-book account directly in `book`, and
    claiming `full_visibility`. Either succeeding would mean the interceptor merged rather than
    replaced the caller-supplied `_identity`. A result that does not unambiguously show a denial
    reports `fail`.
    """
    check_name = "forged_identity_cannot_widen_scope"
    if token is None:
        return _no_token_failure(check_name)

    for forged in _widen_forgeries(out_of_book_account):
        result = _call_get_account(
            caller,
            out_of_book_account,
            token,
            forged_identity=forged,
            mcp_tool_name=mcp_tool_name,
        )
        if isinstance(result, str):
            return CheckResult(
                resource=RESOURCE_CLASS, check=check_name, status="fail", detail=result
            )
        if _is_success(result):
            return CheckResult(
                resource=RESOURCE_CLASS,
                check=check_name,
                status="fail",
                detail=(
                    f"a caller-supplied _identity ({forged!r}) widened scope enough to access "
                    f"out-of-book account {out_of_book_account!r} - the interceptor is merging "
                    "rather than replacing _identity"
                ),
            )
        if not _is_scope_denied(result):
            return CheckResult(
                resource=RESOURCE_CLASS,
                check=check_name,
                status="fail",
                detail=(
                    "cannot prove the widen attempt was denied for the expected reason (no "
                    f"scope_denied code observed): {result!r}"
                ),
            )
        if _leaks_internal_detail(result):
            return CheckResult(
                resource=RESOURCE_CLASS,
                check=check_name,
                status="fail",
                detail=f"denial to a widen attempt leaks internal detail: {result!r}",
            )

    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check_name,
        status="pass",
        detail="both widen forgeries (book and full_visibility) still denied scope_denied",
    )


def _check_forged_identity_cannot_narrow_scope(
    caller: McpCaller, token: str | None, in_book_account: str, mcp_tool_name: str
) -> CheckResult:
    """Assert a caller-supplied `_identity` claiming an empty book does not narrow the verified one.

    If the interceptor merged the caller's forged `_identity` instead of replacing it, an empty
    caller-supplied `book` could shrink the effective scope and the in-book account would be
    denied. A denial here is a `fail`.
    """
    check_name = "forged_identity_cannot_narrow_scope"
    if token is None:
        return _no_token_failure(check_name)

    result = _call_get_account(
        caller,
        in_book_account,
        token,
        forged_identity=_narrow_forgery(),
        mcp_tool_name=mcp_tool_name,
    )
    if isinstance(result, str):
        return CheckResult(resource=RESOURCE_CLASS, check=check_name, status="fail", detail=result)
    if not _is_success(result):
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check_name,
            status="fail",
            detail=(
                f"a caller-supplied empty-book _identity narrowed scope enough to deny access to "
                f"the token's own in-book account {in_book_account!r} - the interceptor is merging "
                f"rather than replacing _identity: {result!r}"
            ),
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check_name,
        status="pass",
        detail="an empty-book forgery did not narrow the verified book",
    )


def _check_forged_identity_changes_no_response_bytes(
    caller: McpCaller,
    token: str | None,
    account_id: str,
    *,
    check_name: str,
    forged_identity: Mapping[str, Any],
    mcp_tool_name: str,
) -> CheckResult:
    """Assert a plain and a forged call against the same target return byte-identical responses.

    A caller-supplied `_identity` must change nothing observable in the response. A transport
    failure on either half is a `fail`.
    """
    if token is None:
        return _no_token_failure(check_name)

    plain = _call_get_account(
        caller, account_id, token, forged_identity=None, mcp_tool_name=mcp_tool_name
    )
    forged = _call_get_account(
        caller, account_id, token, forged_identity=forged_identity, mcp_tool_name=mcp_tool_name
    )
    for label, result in (("plain", plain), ("forged", forged)):
        if isinstance(result, str):
            return CheckResult(
                resource=RESOURCE_CLASS,
                check=check_name,
                status="fail",
                detail=f"{label} call did not complete: {result}",
            )

    plain_bytes = _canonical_bytes(plain)
    forged_bytes = _canonical_bytes(forged)
    if plain_bytes != forged_bytes:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check_name,
            status="fail",
            detail=(
                f"a caller-supplied _identity changed the response for {account_id!r}: "
                f"plain={plain!r} forged={forged!r}"
            ),
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check_name,
        status="pass",
        detail=(
            f"{account_id}: {len(plain_bytes)} response bytes identical, "
            f"sha256[:16]={_digest(plain)} both with and without a forged _identity"
        ),
    )


def _call_filtered_search(
    caller: McpCaller, token: str | None, mcp_names: Mapping[str, str], account_id: str
) -> McpResult | None:
    """Call :data:`FILTER_TOOL` filtered to `account_id`, or `None` when it is not advertised."""
    mcp_name = mcp_names.get(FILTER_TOOL)
    if mcp_name is None:
        return None
    return caller("tools/call", {"name": mcp_name, "arguments": {"account_id": account_id}}, token)


def _check_in_book_account_filter_succeeds(
    caller: McpCaller, token: str | None, in_book_account: str, mcp_names: Mapping[str, str]
) -> CheckResult:
    """Assert `search_opportunities` filtered to the in-book account succeeds over MCP."""
    check_name = "in_book_account_filter_succeeds"
    if token is None:
        return _no_token_failure(check_name)
    result = _call_filtered_search(caller, token, mcp_names, in_book_account)
    if result is None:
        return _fail(check_name, f"tools/list advertises no tool resolving to {FILTER_TOOL!r}")
    if isinstance(result, str):
        return _fail(check_name, result)
    if not _is_success(result):
        return _fail(
            check_name,
            f"expected {FILTER_TOOL} filtered to in-book account {in_book_account!r} to succeed, "
            f"got: {result!r}",
        )
    return _passed(check_name, f"{FILTER_TOOL}(account_id={in_book_account!r}) succeeded")


def _check_out_of_book_account_filter_denied(
    caller: McpCaller, token: str | None, out_of_book_account: str, mcp_names: Mapping[str, str]
) -> CheckResult:
    """Assert `search_opportunities` filtered to the out-of-book account is refused `scope_denied`.

    The refusal must name `account:<id>` and carry no internal detail; a success or a `not_found`
    fails.
    """
    check_name = "out_of_book_account_filter_denied_naming_scope"
    if token is None:
        return _no_token_failure(check_name)
    result = _call_filtered_search(caller, token, mcp_names, out_of_book_account)
    if result is None:
        return _fail(check_name, f"tools/list advertises no tool resolving to {FILTER_TOOL!r}")
    problem = _scope_denial_problem(result, out_of_book_account)
    if problem is not None:
        return _fail(check_name, f"{FILTER_TOOL}(account_id={out_of_book_account!r}): {problem}")
    missing_scope = f"account:{out_of_book_account}"
    if not _missing_scope_names(result, missing_scope):
        return _fail(
            check_name, f"the denial does not name the missing scope {missing_scope!r}: {result!r}"
        )
    return _passed(
        check_name,
        f"{FILTER_TOOL}(account_id={out_of_book_account!r}) refused scope_denied ({missing_scope}) "
        "with no internal detail",
    )


# -- MCP client handshake, alternate and nested channel keys, closed schema ---------------------


def _fail(check_name: str, detail: str) -> CheckResult:
    return CheckResult(resource=RESOURCE_CLASS, check=check_name, status="fail", detail=detail)


def _passed(check_name: str, detail: str) -> CheckResult:
    return CheckResult(resource=RESOURCE_CLASS, check=check_name, status="pass", detail=detail)


def _call_tool(
    caller: McpCaller, token: str | None, *, mcp_tool_name: str, arguments: Mapping[str, Any]
) -> McpResult:
    """Invoke the scope-probe tool over MCP with exactly `arguments`."""
    return caller("tools/call", {"name": mcp_tool_name, "arguments": dict(arguments)}, token)


def _forged_identity_block(
    *, book: Sequence[str], full_visibility: bool, session_id: str
) -> dict[str, Any]:
    """Return one caller-supplied, forged identity payload claiming `book`/`full_visibility`."""
    return {
        "rep_id": _FORGED_REP_ID,
        "book": list(book),
        "full_visibility": full_visibility,
        "principal_type": "user",
        "session_id": session_id,
        "source": "gateway",
    }


def _nested_forged_arguments(account_id: str, forged: Mapping[str, Any]) -> dict[str, Any]:
    """Return arguments hiding every identity channel key one level down inside an argument."""
    return {
        "account_id": account_id,
        _NESTED_ARGUMENT_KEY: {
            IDENTITY_KEY: dict(forged),
            AGENT_IDENTITY_KEY: dict(forged),
            REQUEST_CONTEXT_KEY: {"authorizer": {"jwt": {"claims": dict(forged)}}},
            "account_id": account_id,
        },
    }


def _nested_decoy_arguments(account_id: str) -> dict[str, Any]:
    """Return arguments shaped like `_nested_forged_arguments`, carrying no identity channel key."""
    return {
        "account_id": account_id,
        _NESTED_ARGUMENT_KEY: {"probe": True, "account_id": account_id},
    }


def _alternate_channel_forgeries(
    out_of_book_account: str,
) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Return `(channel, arguments)` pairs forging the two non-`_identity` channel keys."""
    forged = _forged_identity_block(
        book=[out_of_book_account], full_visibility=True, session_id="forged-alternate-channel"
    )
    return (
        (AGENT_IDENTITY_KEY, {"account_id": out_of_book_account, AGENT_IDENTITY_KEY: forged}),
        (
            REQUEST_CONTEXT_KEY,
            {
                "account_id": out_of_book_account,
                REQUEST_CONTEXT_KEY: {
                    "authorizer": {
                        "jwt": {
                            "claims": {
                                "custom:rep_id": _FORGED_REP_ID,
                                "custom:book": out_of_book_account,
                                "custom:full_visibility": "true",
                                "custom:tenant": DEMO_TENANT,
                            }
                        }
                    }
                },
            },
        ),
    )


def _check_initialize_handshake(caller: McpCaller, token: str | None) -> CheckResult:
    """Assert the MCP `initialize` handshake a real external client sends first succeeds."""
    check_name = "initialize_handshake"
    if token is None:
        return _no_token_failure(check_name)
    result = caller(
        "initialize",
        {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "crm-eval-demo-smoke-client", "version": "1.0"},
        },
        token,
    )
    if isinstance(result, str):
        return _fail(check_name, result)
    rpc_error = _rpc_error(result)
    if rpc_error is not None:
        return _fail(check_name, f"initialize was refused: {rpc_error!r}")
    outer = result.get("result")
    if not isinstance(outer, Mapping):
        return _fail(check_name, f"initialize returned no result object: {result!r}")
    return _passed(
        check_name,
        f"initialize accepted, server protocolVersion={outer.get('protocolVersion')!r} "
        f"serverInfo={outer.get('serverInfo')!r}",
    )


def _scope_denial_problem(result: McpResult, account_id: str) -> str | None:
    """Why `result` is not a user-safe `scope_denied` refusal for `account_id`, or `None`."""
    if isinstance(result, str):
        return result
    if _is_success(result):
        return f"the call reached out-of-book account {account_id!r}: {result!r}"
    if _is_not_found(result):
        return f"{account_id!r} answered not_found, so scope was never evaluated"
    if not _is_scope_denied(result):
        return f"no scope_denied code observed: {result!r}"
    if _leaks_internal_detail(result):
        return f"the denial leaks internal detail: {result!r}"
    return None


def _check_alternate_channels_discarded(
    caller: McpCaller, token: str | None, out_of_book_account: str, mcp_tool_name: str
) -> CheckResult:
    """Assert forged `_agent_identity` and `requestContext` blocks are discarded like `_identity`."""
    check_name = "forged_alternate_channel_keys_discarded"
    if token is None:
        return _no_token_failure(check_name)
    for channel, arguments in _alternate_channel_forgeries(out_of_book_account):
        result = _call_tool(caller, token, mcp_tool_name=mcp_tool_name, arguments=arguments)
        problem = _scope_denial_problem(result, out_of_book_account)
        if problem is not None:
            return _fail(check_name, f"forged channel {channel!r}: {problem}")
    return _passed(
        check_name,
        f"forged {AGENT_IDENTITY_KEY!r} and {REQUEST_CONTEXT_KEY!r} blocks claiming the "
        "out-of-book account and full visibility were both refused scope_denied",
    )


def _error_code(result: McpResult) -> str | None:
    return _tool_error_code(result) or _rpc_error_code(result)


def _closed_schema_refusal_problem(result: McpResult) -> str | None:
    """Why `result` is not a user-safe closed-schema refusal of the arguments, or `None`.

    A refusal is exactly one MCP content item holding `{"statusCode": 400, "error": {...}}`, whose
    error block carries exactly `code`/`message`/`source`/`next_step`, with `validation_failed`,
    `source == "arguments"`, the `next_step` :mod:`crm.errors` assigns it, and no internal detail.
    """
    if isinstance(result, str):
        return result
    if _is_success(result):
        return f"the call succeeded, so the tool's schema is open: {result!r}"
    code = _error_code(result)
    if code != "validation_failed":
        return f"expected the closed schema's validation_failed, got {code!r}: {result!r}"
    error_block = _tool_error_block(result) or {}
    source = error_block.get("source")
    if source != _ARGUMENTS_SOURCE:
        return f"validation_failed names source {source!r}, not {_ARGUMENTS_SOURCE!r}: {result!r}"
    if _leaks_internal_detail(result):
        return f"the schema refusal leaks internal detail: {result!r}"
    outer = result.get("result")
    content = outer.get("content") if isinstance(outer, Mapping) else None
    if not isinstance(content, list) or len(content) != 1:
        return f"expected exactly one content item holding the refusal envelope: {result!r}"
    envelope = _envelope_of(result)
    if envelope is None or set(envelope) != _REFUSAL_ENVELOPE_KEYS:
        keys = sorted(envelope) if envelope is not None else None
        return f"refusal envelope keys are {keys!r}, not exactly statusCode/error: {result!r}"
    if envelope.get("statusCode") != 400:
        return f"refusal envelope statusCode is {envelope.get('statusCode')!r}, not 400: {result!r}"
    error_keys = set(error_block)
    if error_keys != _REFUSAL_ERROR_KEYS:
        return (
            f"refusal error block keys are {sorted(error_keys)!r}, not exactly "
            f"{sorted(_REFUSAL_ERROR_KEYS)!r}: {result!r}"
        )
    if error_block.get("next_step") != _REFUSAL_NEXT_STEP:
        return (
            f"refusal next_step is {error_block.get('next_step')!r}, not {_REFUSAL_NEXT_STEP!r}: "
            f"{result!r}"
        )
    return None


def _check_undeclared_argument_refused(
    caller: McpCaller, token: str | None, in_book_account: str, mcp_tool_name: str
) -> CheckResult:
    """Assert an undeclared argument is refused by the tool's closed schema.

    The control is `in_book_target_succeeds`: the same account without the extra key succeeds.
    """
    check_name = "undeclared_argument_refused_by_closed_schema"
    if token is None:
        return _no_token_failure(check_name)
    result = _call_tool(
        caller,
        token,
        mcp_tool_name=mcp_tool_name,
        arguments={"account_id": in_book_account, _UNDECLARED_PROBE_KEY: {"probe": True}},
    )
    problem = _closed_schema_refusal_problem(result)
    if problem is not None:
        return _fail(check_name, f"undeclared argument {_UNDECLARED_PROBE_KEY!r}: {problem}")
    return _passed(
        check_name,
        f"undeclared argument {_UNDECLARED_PROBE_KEY!r} refused validation_failed "
        f"(source={_ARGUMENTS_SOURCE!r}) with no data or internal detail; control: "
        "in_book_target_succeeds, the same account without the key",
    )


def _check_nested_forgery_discarded(
    caller: McpCaller, token: str | None, out_of_book_account: str, mcp_tool_name: str
) -> CheckResult:
    """Assert channel keys nested inside an undeclared argument are refused by the closed schema.

    Only `validation_failed` passes; a `scope_denied` would mean the schema carried the argument.
    """
    check_name = "forged_nested_identity_discarded"
    if token is None:
        return _no_token_failure(check_name)
    forged = _forged_identity_block(
        book=[out_of_book_account], full_visibility=True, session_id="forged-nested"
    )
    result = _call_tool(
        caller,
        token,
        mcp_tool_name=mcp_tool_name,
        arguments=_nested_forged_arguments(out_of_book_account, forged),
    )
    if not isinstance(result, str) and _is_success(result):
        return _fail(
            check_name,
            "an identity channel key nested inside a tool argument widened scope enough to reach "
            f"out-of-book account {out_of_book_account!r}: {result!r}",
        )
    problem = _closed_schema_refusal_problem(result)
    if problem is not None:
        return _fail(check_name, f"nested forgery under {_NESTED_ARGUMENT_KEY!r}: {problem}")
    return _passed(
        check_name,
        f"the forgery nested under {_NESTED_ARGUMENT_KEY!r} was refused by the closed schema "
        "(validation_failed, no data); see the decoy byte row for its effect on the response",
    )


def _check_nested_forgery_changes_no_bytes(
    caller: McpCaller, token: str | None, out_of_book_account: str, mcp_tool_name: str
) -> CheckResult:
    """Assert the nested forgery's refusal is byte-identical to a same-shaped decoy's.

    The forged half must itself be a closed-schema refusal, so a byte-equal pair of successes fails.
    """
    check_name = "forged_nested_identity_changes_no_response_bytes"
    if token is None:
        return _no_token_failure(check_name)
    decoy = _call_tool(
        caller,
        token,
        mcp_tool_name=mcp_tool_name,
        arguments=_nested_decoy_arguments(out_of_book_account),
    )
    forged = _call_tool(
        caller,
        token,
        mcp_tool_name=mcp_tool_name,
        arguments=_nested_forged_arguments(
            out_of_book_account,
            _forged_identity_block(
                book=[out_of_book_account], full_visibility=True, session_id="forged-nested-bytes"
            ),
        ),
    )
    for label, result in (("decoy", decoy), ("forged", forged)):
        if isinstance(result, str):
            return _fail(check_name, f"{label} call did not complete: {result}")
    problem = _closed_schema_refusal_problem(forged)
    if problem is not None:
        return _fail(check_name, f"forged half: {problem}")
    if _canonical_bytes(decoy) != _canonical_bytes(forged):
        return _fail(
            check_name,
            f"a nested identity block changed the response for {out_of_book_account!r}: "
            f"decoy sha256[:16]={_digest(decoy)} vs forged sha256[:16]={_digest(forged)}; "
            f"decoy={decoy!r} forged={forged!r}",
        )
    return _passed(
        check_name,
        f"{out_of_book_account}: {len(_canonical_bytes(forged))} canonical response bytes "
        f"identical, sha256[:16]={_digest(forged)}, for the forgery and a same-shaped decoy "
        "carrying no channel key",
    )


def checks(
    ctx: SmokeContext,
    *,
    mcp_caller: McpCaller | None = None,
    token: str | None = None,
    in_book_account: str = _IN_BOOK_ACCOUNT,
    out_of_book_account: str = _OUT_OF_BOOK_ACCOUNT,
    provisioner: ServicePrincipalProvisioner | None = None,
    row_reader: TenantRowReader | None = None,
) -> list[CheckResult]:
    """Yield one `CheckResult` per branch of the end-to-end scope-enforcement assertion.

    Never raises for an individual check failure; every branch is checked even when an earlier one
    already failed. Requires `agentcore_gateway_url` in `ctx.outputs`, which is bound into the
    transport seam here.

    Args:
        ctx: The smoke context.
        mcp_caller: Transport seam override, injected by the offline test.
        token: An already-issued access token. When supplied it takes precedence over
            `provisioner`.
        in_book_account: Account the acting identity is expected to own.
        out_of_book_account: Account the acting identity is expected not to own. Must exist in the
            same tenant.
        provisioner: Seeds a disposable tenant and the service principal's book within it, mints
            that principal's machine token, then removes everything again. The live entry point
            always supplies one.
        row_reader: Seam the tenant-delta assertions read tenant rows through, injected by the
            offline test. Defaults to a reader over the deployed tables. Used only on the
            provisioned (mutating) path.
    """
    gateway_url = ctx.outputs.require_str("agentcore_gateway_url")
    caller = (
        mcp_caller
        if mcp_caller is not None
        else functools.partial(default_mcp_caller, gateway_url=gateway_url)
    )

    if token is not None or provisioner is None:
        return _scope_checks(
            caller,
            token=token,
            in_book_account=in_book_account,
            out_of_book_account=out_of_book_account,
        )
    reader = row_reader if row_reader is not None else _live_row_reader(ctx)
    return _provisioned_checks(caller, provisioner, reader)


def _live_row_reader(ctx: SmokeContext) -> TenantRowReader:
    """Return a `TenantRowReader` over the deployed tables, built lazily per read.

    Lazily, so a missing output or an unavailable client surfaces as a failing tenant-delta check.
    """

    def _read(table: str, tenant: str) -> Sequence[Mapping[str, Any]]:
        store = Store(client=ctx.client("dynamodb"), table_prefix=_live_table_prefix(ctx))
        return store_row_reader(store)(table, tenant)

    return _read


def _provisioned_checks(
    caller: McpCaller, provisioner: ServicePrincipalProvisioner, reader: TenantRowReader
) -> list[CheckResult]:
    """Provision the service principal's fixture, run every check, then always clean up.

    Cleanup runs from a `finally` block, and each removal is reported as its own check. A cleanup
    that raises is a `fail` row, and the `demo` delta is still published.

    This is the script's only mutating path, so it publishes the tenant-delta assertions from the
    same `finally`: the fixture's own rows are read back under its disposable tenant before
    teardown, and the `demo` tenant's row counts, sampled before anything is seeded, are re-read
    only after cleanup has restored the service principal's pre-existing `reps` row.
    """
    results: list[CheckResult] = []
    demo_before = read_tenant_snapshot(reader, DEMO_TENANT)
    fixture: ServicePrincipalFixture | None = None
    try:
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
        results.extend(
            _scope_checks(
                caller,
                token=fixture.token,
                in_book_account=fixture.in_book_account,
                out_of_book_account=fixture.out_of_book_account,
            )
        )
    finally:
        results.append(
            writes_only_under_smoke_tenant_check(
                resource=RESOURCE_CLASS,
                tenant=fixture.tenant if fixture is not None else "",
                reader=reader,
            )
        )
        try:
            results.extend(provisioner.cleanup())
        except Exception as exc:  # noqa: BLE001 - surfaced as a failing check
            results.append(
                CheckResult(
                    resource=RESOURCE_CLASS,
                    check="cleanup_service_principal",
                    status="fail",
                    detail=f"cleanup raised {type(exc).__name__}: {exc}",
                )
            )
        finally:
            results.append(
                demo_tenant_untouched_check(
                    resource=RESOURCE_CLASS, before=demo_before, reader=reader
                )
            )
    return results


def _scope_checks(
    caller: McpCaller,
    *,
    token: str | None,
    in_book_account: str,
    out_of_book_account: str,
) -> list[CheckResult]:
    """Resolve the Gateway's tool name, then run every scope and `_identity`-discard assertion."""
    handshake = _check_initialize_handshake(caller, token)
    mcp_tool_name, name_check = _resolve_mcp_tool_name(caller, token)
    wanted = frozenset({name for name, _ in DECIDER_TOOLS} | {FILTER_TOOL})
    advertised = _resolve_tool_names(caller, token, wanted) if token is not None else {}
    decider_checks = [
        _check_service_principal_refused_decider(
            caller, token, tool_name=tool_name, id_argument=id_argument, mcp_names=advertised
        )
        for tool_name, id_argument in DECIDER_TOOLS
    ]
    return [
        handshake,
        name_check,
        _check_in_book_succeeds(caller, token, in_book_account, mcp_tool_name),
        _check_out_of_book_denied_naming_scope(caller, token, out_of_book_account, mcp_tool_name),
        _check_forged_identity_cannot_widen_scope(
            caller, token, out_of_book_account, mcp_tool_name
        ),
        _check_forged_identity_cannot_narrow_scope(caller, token, in_book_account, mcp_tool_name),
        _check_forged_identity_changes_no_response_bytes(
            caller,
            token,
            in_book_account,
            check_name="forged_identity_changes_no_response_bytes_in_book",
            forged_identity=_narrow_forgery(),
            mcp_tool_name=mcp_tool_name,
        ),
        _check_forged_identity_changes_no_response_bytes(
            caller,
            token,
            out_of_book_account,
            check_name="forged_identity_changes_no_response_bytes_out_of_book",
            forged_identity=_widen_forgeries(out_of_book_account)[0],
            mcp_tool_name=mcp_tool_name,
        ),
        _check_alternate_channels_discarded(caller, token, out_of_book_account, mcp_tool_name),
        _check_undeclared_argument_refused(caller, token, in_book_account, mcp_tool_name),
        _check_nested_forgery_discarded(caller, token, out_of_book_account, mcp_tool_name),
        _check_nested_forgery_changes_no_bytes(caller, token, out_of_book_account, mcp_tool_name),
        _check_in_book_account_filter_succeeds(caller, token, in_book_account, advertised),
        _check_out_of_book_account_filter_denied(caller, token, out_of_book_account, advertised),
        *decider_checks,
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: resolve the token and target accounts from flags/env, run every check.

    When no `--gateway-token`/`CRM_SMOKE_GATEWAY_TOKEN` is supplied, a
    `LiveServicePrincipalProvisioner` seeds a disposable tenant and the service principal's book,
    mints that principal's `client_credentials` token, and removes what it created afterwards.
    """
    token = _token_from_env_or_flag(argv)
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
            in_book_account=in_book_account,
            out_of_book_account=out_of_book_account,
            provisioner=None if token is not None else LiveServicePrincipalProvisioner(ctx),
        ),
        extra_arguments=["--gateway-token", "--in-book-account", "--out-of-book-account"],
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
