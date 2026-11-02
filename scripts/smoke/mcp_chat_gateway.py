# SPDX-License-Identifier: MIT
"""Layer 5 T3 smoke: Gateway #2 exposes only `chat_with_supervisor`, behind its JWT.

Against the deployed `mcp_chat_gateway_url`:

1. **Authentication**. `tools/list` with no `Authorization` header is answered HTTP 401.
   A user token whose signature is replaced is refused (401 or 403) while the same token unaltered
   is answered 200, so the refusal is the signature check's.
2. **Inventory**. `tools/list` with a real user access token lists exactly one tool, whose
   name after the `<target>___` prefix is `chat_with_supervisor`.
3. **User call**. A disposable user's token carries the chat scope, and a
   `tools/call` returns an answer and a `conv-<32 hex>` conversation id.
4. **Foreign conversation**. A second disposable user passing the first user's
   `conversation_id` is refused with `error_kind: denied`. The target refuses before it calls the
   Runtime, so this call is not model-backed.
5. **Forged identity**. User B passes user A's `conversation_id` plus a forged
   `_agent_identity` naming A, and is refused (`isError`, `invalid_request` or `denied`) before any
   Runtime call.
5a. **Temperature argument**. User A's `tools/call` carrying a `temperature` argument is
   refused by the target's argument validation (`isError`, `invalid_request`) before any Runtime
   call, so it returns no answer and no conversation id and is not model-backed.
6. **Scope separation**. User A's chat-scoped token on Gateway #1's `tools/list`, and a
   `client_credentials` token requested for `gateway/invoke` only on Gateway #2's `tools/list`, are
   each refused, 401 or 403 (RFC 6750 `insufficient_scope` is 403).
7. **Service call**. A `client_credentials` token for the MCP app client, requested with no
   `scope` parameter so it carries every scope the client is granted, carries the chat scope and its
   `tools/call` returns an answer under the default tuning.

The two answered calls are model-backed and are counted against this module's share
(`three_tier_chat.MODEL_TURN_ALLOTMENTS`); the count is printed and a call past the share is not
issued. In a `finally`, every `conversation#` ownership item owned by a smoke principal (each user's
`username`, the service principal's `rep_id`) is found by a consistent scan of `reps`, deleted and
re-read to prove it is gone (the items carry no `tenant`, so tenant teardown never sees them); a
call that returned no conversation id, or a principal whose rep id is unknown, fails the row. Then
users, the service principal's `reps` row and every tenant are removed.

Usage:
    python3 -m scripts.smoke.mcp_chat_gateway [--profile P] [--region R] [--outputs PATH] [--json]
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, Final

from scripts.smoke.gateway_tools import (
    ClaimsVerifier,
    LiveServicePrincipalProvisioner,
    access_token_verifier,
    mcp_client_secret,
)
from scripts.smoke.harness import (
    CheckResult,
    ExpectedCheckCount,
    SmokeContext,
    open_https,
    run_main,
)
from scripts.smoke.three_tier_chat import ModelTurnBudget, budget_refused

from crm.conversations import CONVERSATION_ID_PATTERN, CONVERSATIONS_TABLE
from crm.store.keys import conversation_key
from gateway.chat_target import TOOL_NAME as CHAT_TOOL

LAYER: Final[int] = 5
RESOURCE_CLASS: Final[str] = "mcp_chat_gateway"


def conversation_key_prefix() -> str:
    """The `reps` key prefix every conversation-ownership item carries (`conversation#`)."""
    return conversation_key("conv-" + "0" * 32).removesuffix("conv-" + "0" * 32)


#: AgentCore prefixes every tool with `<target name>___`.
_TARGET_NAME_DELIMITER: Final[str] = "___"

#: Suffix of the resource-server scope Gateway #2's authorizer requires (`supervisor/chat`).
CHAT_SCOPE_SUFFIX: Final[str] = "/chat"

#: The identity block a caller forges; the Gateway interceptor must strip it or chat_target refuse it.
FORGED_IDENTITY_KEY: Final[str] = "_agent_identity"

#: Error kinds that are a refusal of the forged call; anything else, or an answer, fails.
FORGED_REFUSAL_KINDS: Final[frozenset[str]] = frozenset({"invalid_request", "denied"})

#: A prompt that needs no CRM data and a one-word reply.
SMOKE_MESSAGE: Final[str] = "Reply with the single word: ready."

#: An argument the target does not accept, and the refusal it must produce.
REFUSED_ARGUMENT: Final[str] = "temperature"
ARGUMENT_REFUSAL_KIND: Final[str] = "invalid_request"

#: Above the chat target's 660 s timeout, so the Gateway answers before the socket gives up.
_CALL_TIMEOUT_SECONDS: Final[float] = 700.0

#: `(status, body)`: the HTTP status and the decoded JSON body, or `(None, reason)` with no response.
McpResponse = tuple[int | None, dict[str, Any] | str]
#: `(method, params, token)` -> `McpResponse`. The transport seam every check goes through.
McpCaller = Callable[[str, Mapping[str, Any], str | None], McpResponse]
#: `(ctx)` -> a provisioner with `provision() -> str` (an access token) and `cleanup()`.
UserProvisionerFactory = Callable[[SmokeContext], Any]
#: `(ctx)` -> a provisioner with `provision()` returning an object with `.token`, and `cleanup()`.
ServiceProvisionerFactory = Callable[[SmokeContext], Any]
#: `(ctx, scope)` -> a `client_credentials` access token requested for exactly `scope`.
ScopedTokenMinter = Callable[[SmokeContext, str], str]

__all__ = [
    "CHAT_SCOPE_SUFFIX",
    "CHAT_TOOL",
    "LAYER",
    "RESOURCE_CLASS",
    "checks",
    "main",
    "mint_chat_machine_token",
]


def _mcp_url(ctx: SmokeContext, output: str = "mcp_chat_gateway_url") -> str:
    base = ctx.outputs.require_str(output).rstrip("/")
    return base if base.endswith("/mcp") else f"{base}/mcp"


def default_mcp_caller(
    method: str, params: Mapping[str, Any], token: str | None, *, url: str
) -> McpResponse:
    """One MCP JSON-RPC POST, returning the HTTP status with the decoded body."""
    body = json.dumps({"jsonrpc": "2.0", "id": "smoke-1", "method": method, "params": dict(params)})
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310 - URL from a Terraform output
        url, data=body.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with open_https(request, timeout=_CALL_TIMEOUT_SECONDS) as response:
            status, raw = int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status, raw = int(exc.code), exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return None, f"transport error calling {url}: {exc}"
    try:
        decoded = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, f"non-JSON body: {raw[:200]!r}"
    return status, decoded if isinstance(decoded, dict) else f"non-object body: {decoded!r}"


def mint_chat_machine_token(ctx: SmokeContext, scope: str | None = None) -> str:
    """A `client_credentials` access token for the MCP app client.

    With no `scope` Cognito issues every custom scope the client is granted; with one, only that
    scope. The client secret comes from `mcp_client_secret` and is never written anywhere.
    """
    token_endpoint = ctx.outputs.require_str("cognito_token_endpoint")
    client_id = ctx.outputs.require_str("cognito_mcp_client_id")
    client_secret = mcp_client_secret(ctx)
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    request = urllib.request.Request(  # noqa: S310 - endpoint from a Terraform output
        token_endpoint,
        data=urllib.parse.urlencode(
            {"grant_type": "client_credentials", **({"scope": scope} if scope else {})}
        ).encode("utf-8"),
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
            f"token endpoint refused client_credentials with HTTP {exc.code}"
        ) from None
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not token:
        raise RuntimeError("token endpoint returned no access_token")
    return str(token)


class _ChatServicePrincipalProvisioner(LiveServicePrincipalProvisioner):
    """Gateway #1's service-principal fixture, with a token minted for every granted scope."""

    def _mint_machine_token(self) -> str:
        return mint_chat_machine_token(self._ctx)


def _default_user_provisioner(ctx: SmokeContext) -> Any:
    from scripts.smoke_agent_stream import LiveStreamPrincipalProvisioner

    return LiveStreamPrincipalProvisioner(ctx)


def _default_service_provisioner(ctx: SmokeContext) -> Any:
    return _ChatServicePrincipalProvisioner(ctx)


def _token_scopes(token: str, verifier: ClaimsVerifier) -> tuple[list[str], str]:
    """The verified `scope` claim of `token`, split on spaces, and `""`; or `[]` and the reason
    verification failed."""
    try:
        claims = verifier(token)
    except Exception as exc:  # noqa: BLE001 - the reason is reported by the caller's row
        return [], f"the token failed claims verification ({type(exc).__name__}: {exc})"
    return str(claims.get("scope") or "").split(), ""


def _tampered(token: str) -> str:
    """`token` with its signature replaced, so only a verifier that skips the signature accepts it."""
    head, _, _signature = token.rpartition(".")
    return f"{head}.{base64.urlsafe_b64encode(b'not-the-signature').decode('ascii').rstrip('=')}"


def _tool_result(body: dict[str, Any] | str) -> Mapping[str, Any] | None:
    """The chat target's own output from a `tools/call` answer.

    `gateway.chat_target` returns an error as `{isError, content, error_kind}`, which the Gateway
    passes through as the MCP `result` itself; a success (`{answer, conversation_id}`) arrives as
    JSON in the result's text content.
    """
    if isinstance(body, str):
        return None
    outer = body.get("result")
    if not isinstance(outer, Mapping):
        return None
    if outer.get("isError") is True:
        return outer
    for item in outer.get("content") or []:
        if not isinstance(item, Mapping) or not isinstance(item.get("text"), str):
            continue
        try:
            parsed = json.loads(item["text"])
        except ValueError:
            continue
        if isinstance(parsed, Mapping):
            return parsed
    return None


def _scope_row(check: str, token: str, who: str, verifier: ClaimsVerifier) -> CheckResult:
    scopes, error = _token_scopes(token, verifier)
    if error:
        return CheckResult(
            resource=RESOURCE_CLASS, check=check, status="fail", detail=f"{who} token: {error}"
        )
    ok = any(scope.endswith(CHAT_SCOPE_SUFFIX) for scope in scopes)
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check,
        status="pass" if ok else "fail",
        detail=f"{who} token scopes {scopes}"
        + ("" if ok else f"; none ends {CHAT_SCOPE_SUFFIX!r}, so Gateway #2 must refuse it"),
    )


#: Statuses that refuse a credential: 401 for a missing or invalid token, 403 for one lacking scope
#: (RFC 6750 section 3.1). AgentCore answers an invalid signature with 403 `insufficient_scope`.
REFUSED_STATUSES: Final[frozenset[int]] = frozenset({401, 403})


def _unauthenticated_rows(call: McpCaller, user_token: str) -> Iterator[CheckResult]:
    status, body = call("tools/list", {}, None)
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="no_jwt_refused_401",
        status="pass" if status == 401 else "fail",
        detail=f"tools/list -> status={status!r}"
        + ("" if status == 401 else f" body={str(body)[:200]!r}; expected 401"),
    )
    yield _invalid_signature_row(call, user_token)


def _invalid_signature_row(call: McpCaller, user_token: str) -> CheckResult:
    """The same claims are admitted with their signature and refused with it replaced.

    Only the signature differs between the two calls, so the refusal is the signature check's.
    """
    check = "invalid_jwt_refused"
    control, _ = call("tools/list", {}, user_token)
    status, body = call("tools/list", {}, _tampered(user_token))
    if control != 200:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="fail",
            detail=f"control: the untampered token got status={control!r}, not 200, so a refusal "
            "of the tampered copy would not isolate the signature",
        )
    leaked = isinstance(body, Mapping) and "result" in body
    refused = status in REFUSED_STATUSES and not leaked
    finding = (
        "; FINDING: AgentCore answers an invalid signature with 403 insufficient_scope, not RFC "
        "6750's 401 invalid_token. The refusal is still the signature's: the same claims with "
        "their signature were answered 200"
        if status == 403
        else ""
    )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check,
        status="pass" if refused else "fail",
        detail=f"tools/list: valid signature -> {control}, replaced signature -> status={status!r}"
        + (
            finding
            if refused
            else f" body={str(body)[:200]!r}; expected a 401/403 refusal with no tools/list result"
        ),
    )


def _inventory_row(call: McpCaller, token: str) -> tuple[CheckResult, str]:
    """The inventory row, and the chat tool's advertised name (the bare name when unlisted)."""
    status, body = call("tools/list", {}, token)
    outer = body.get("result") if isinstance(body, dict) else None
    tools = outer.get("tools") if isinstance(outer, Mapping) else None
    names = (
        [str(t.get("name")) for t in tools if isinstance(t, Mapping)]
        if isinstance(tools, list)
        else None
    )
    bare = [n.rpartition(_TARGET_NAME_DELIMITER)[2] for n in names] if names is not None else None
    ok = bare == [CHAT_TOOL]
    advertised = next(
        (n for n in names or [] if n.rpartition(_TARGET_NAME_DELIMITER)[2] == CHAT_TOOL), CHAT_TOOL
    )
    row = CheckResult(
        resource=RESOURCE_CLASS,
        check="tools_list_exactly_chat_with_supervisor",
        status="pass" if ok else "fail",
        detail=f"tools/list -> status={status!r} tools={names!r}"
        + ("" if ok else f"; expected exactly [{CHAT_TOOL!r}]"),
    )
    return row, advertised


def _call_chat(
    call: McpCaller, tool_name: str, token: str, arguments: Mapping[str, Any]
) -> tuple[int | None, Mapping[str, Any] | None, str]:
    status, body = call("tools/call", {"name": tool_name, "arguments": dict(arguments)}, token)
    return status, _tool_result(body), str(body)[:300]


def _answer_row(
    check: str, who: str, outcome: tuple[int | None, Mapping[str, Any] | None, str]
) -> CheckResult:
    status, result, raw = outcome
    answer = result.get("answer") if result else None
    conversation = str(result.get("conversation_id") or "") if result else ""
    ok = (
        status == 200
        and isinstance(answer, str)
        and bool(answer.strip())
        and not (result or {}).get("isError")
        and bool(CONVERSATION_ID_PATTERN.match(conversation))
    )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check,
        status="pass" if ok else "fail",
        detail=f"{who} tools/call -> status={status!r} conversation_id={conversation!r} "
        f"answer_chars={len(answer) if isinstance(answer, str) else None}"
        + ("" if ok else f"; result={raw!r}"),
    )


def _forged_identity_row(
    call: McpCaller,
    tool_name: str,
    token: str,
    conversation: str,
    victim_token: str,
    verifier: ClaimsVerifier,
) -> CheckResult:
    check = "forged_agent_identity_refused"
    if not CONVERSATION_ID_PATTERN.match(conversation):
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="fail",
            detail=f"user A's call returned no conversation id ({conversation!r}) to forge onto",
        )
    try:
        victim = verifier(victim_token)
    except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="fail",
            detail=f"user A's token failed claims verification ({type(exc).__name__}: {exc}), "
            "so there is no verified identity to forge",
        )
    forged = {
        "claims": {"sub": victim.get("sub"), "cognito:username": victim.get("username")},
        "principal_type": "user",
        "session_id": conversation,
    }
    status, result, raw = _call_chat(
        call,
        tool_name,
        token,
        {"message": SMOKE_MESSAGE, "conversation_id": conversation, FORGED_IDENTITY_KEY: forged},
    )
    kind = (result or {}).get("error_kind")
    refused = (
        bool((result or {}).get("isError"))
        and kind in FORGED_REFUSAL_KINDS
        and "answer" not in (result or {})
    )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check,
        status="pass" if refused else "fail",
        detail=f"user B with a forged {FORGED_IDENTITY_KEY} naming user A on {conversation} -> "
        f"status={status!r} error_kind={kind!r}"
        + ("" if refused else f"; expected one of {sorted(FORGED_REFUSAL_KINDS)}; result={raw!r}"),
    )


def _temperature_argument_row(call: McpCaller, tool_name: str, token: str) -> CheckResult:
    """A `temperature` argument is refused as `invalid_request` with no answer or conversation."""
    status, result, raw = _call_chat(
        call, tool_name, token, {"message": SMOKE_MESSAGE, REFUSED_ARGUMENT: 0.5}
    )
    outcome = result or {}
    kind = outcome.get("error_kind")
    refused = (
        bool(outcome.get("isError"))
        and kind == ARGUMENT_REFUSAL_KIND
        and "answer" not in outcome
        and not outcome.get("conversation_id")
    )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check="temperature_argument_refused",
        status="pass" if refused else "fail",
        detail=f"user A tools/call with a {REFUSED_ARGUMENT!r} argument -> status={status!r} "
        f"error_kind={kind!r}"
        + (
            "; refused by argument validation before the Runtime call"
            if refused
            else f"; expected isError {ARGUMENT_REFUSAL_KIND!r} with no answer; result={raw!r}"
        ),
    )


def _refused_row(check: str, call: McpCaller, token: str, what: str) -> CheckResult:
    try:
        status, body = call("tools/list", {}, token)
    except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="fail",
            detail=f"{what} tools/list could not be sent: {type(exc).__name__}: {exc}",
        )
    leaked = isinstance(body, Mapping) and "result" in body
    refused = status in REFUSED_STATUSES and not leaked
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check,
        status="pass" if refused else "fail",
        detail=f"{what} tools/list -> status={status!r}"
        + (
            ""
            if refused
            else f" body={str(body)[:200]!r}; expected a 401/403 refusal with no tools/list result"
        ),
    )


def _invoke_only_token_row(
    ctx: SmokeContext, call: McpCaller, minter: ScopedTokenMinter, verifier: ClaimsVerifier
) -> CheckResult:
    check = "gateway2_refuses_invoke_only_token"
    scope = "cognito_gateway_scope"
    try:
        scope = ctx.outputs.require_str("cognito_gateway_scope")
        token = minter(ctx, scope)
    except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="fail",
            detail=f"could not mint a {scope}-only token: {type(exc).__name__}: {exc}",
        )
    scopes, error = _token_scopes(token, verifier)
    if error:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="fail",
            detail=f"the token requested for {scope!r}: {error}",
        )
    if any(s.endswith(CHAT_SCOPE_SUFFIX) for s in scopes) or scope not in scopes:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="fail",
            detail=f"the token requested for {scope!r} carries {scopes}; a refusal of it would "
            "not show that Gateway #2 requires the chat scope",
        )
    return _refused_row(check, call, token, f"Gateway #2 with a {scopes} token")


def _username_claim(token: str, verifier: ClaimsVerifier) -> str:
    """The access token's verified `username` claim, the rep id a provisioned smoke user is seeded
    with; `""` when verification fails, which fails the conversation-removal row."""
    try:
        claims = verifier(token)
    except Exception:  # noqa: BLE001 - surfaced by `_remove_conversations` as an unknown owner
        return ""
    return str(claims.get("username") or "")


def _owned_conversation_keys(dynamodb: Any, table: str, owners: Sequence[str]) -> list[str]:
    """Every `conversation#` key in `table` whose `owner_rep_id` is one of `owners` (consistent)."""
    values: dict[str, dict[str, str]] = {":prefix": {"S": conversation_key_prefix()}}
    placeholders = []
    for index, owner in enumerate(sorted(set(owners))):
        values[f":o{index}"] = {"S": owner}
        placeholders.append(f":o{index}")
    request: dict[str, Any] = {
        "TableName": table,
        "FilterExpression": f"begins_with(rep_id, :prefix) AND owner_rep_id IN "
        f"({', '.join(placeholders)})",
        "ExpressionAttributeValues": values,
        "ProjectionExpression": "rep_id",
        "ConsistentRead": True,
    }
    found: list[str] = []
    while True:
        page = dynamodb.scan(**request)
        found += [str(item["rep_id"]["S"]) for item in page.get("Items") or []]
        if not page.get("LastEvaluatedKey"):
            return found
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _remove_conversations(
    ctx: SmokeContext, issued: Sequence[str], owners: Sequence[str]
) -> CheckResult:
    """Delete every conversation item a smoke principal owns, then re-read each to prove it is gone.

    `issued` holds one entry per chat call that may have created an item: its returned conversation
    id, or `""` when it returned none. `owners` holds each smoke principal's rep id, or `""` when it
    could not be established.
    """
    check = "conversation_items_removed"
    try:
        table = str(ctx.outputs.require_mapping("dynamodb_table_names")[CONVERSATIONS_TABLE])
        dynamodb = ctx.client("dynamodb")
        known = [o for o in owners if o]
        keys = set(_owned_conversation_keys(dynamodb, table, known)) if known else set()
        keys |= {conversation_key(c) for c in issued if CONVERSATION_ID_PATTERN.match(c)}
        left: list[str] = []
        for key in sorted(keys):
            item_key = {"rep_id": {"S": key}}
            dynamodb.delete_item(TableName=table, Key=item_key)
            if "Item" in dynamodb.get_item(TableName=table, Key=item_key, ConsistentRead=True):
                left.append(key)
    except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="fail",
            detail=f"removing the smoke principals' conversation items: {type(exc).__name__}: "
            f"{exc}; items may remain",
        )
    unidentified = sum(1 for c in issued if not CONVERSATION_ID_PATTERN.match(c))
    unknown_owners = sum(1 for o in owners if not o)
    problems = (
        [f"still present: {left}"] * bool(left)
        + [f"{unidentified} call(s) returned no conversation id"] * bool(unidentified)
        + [f"{unknown_owners} principal(s) with no known rep id, so not scanned"]
        * bool(unknown_owners)
    )
    return CheckResult(
        resource=table,
        check=check,
        status="fail" if problems else "pass",
        detail=f"deleted and re-read {len(keys)} conversation item(s) owned by {sorted(known)}"
        + (f"; {'; '.join(problems)}" if problems else ""),
    )


def checks(
    ctx: SmokeContext,
    *,
    mcp_caller: McpCaller | None = None,
    gateway1_caller: McpCaller | None = None,
    user_provisioner_factory: UserProvisionerFactory | None = None,
    service_provisioner_factory: ServiceProvisionerFactory | None = None,
    scoped_token_minter: ScopedTokenMinter | None = None,
    jwks_url: str | None = None,
) -> Iterator[CheckResult]:
    """Provision two users and the service principal, run the checks, and clean up.

    Every token whose claims are read is verified first: user tokens against the SPA client,
    machine tokens against the MCP client, both through `jwks_url` (the pool's by default).
    """
    try:
        url = _mcp_url(ctx)
    except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
        yield CheckResult(
            resource="mcp_chat_gateway_url",
            check="terraform_output_present",
            status="fail",
            detail=f"required Terraform output 'mcp_chat_gateway_url' is absent ({exc})",
        )
        return
    call: McpCaller = mcp_caller or (lambda m, p, t: default_mcp_caller(m, p, t, url=url))
    call1: McpCaller = gateway1_caller or (
        lambda m, p, t: default_mcp_caller(m, p, t, url=_mcp_url(ctx, "agentcore_gateway_url"))
    )
    minter = scoped_token_minter or mint_chat_machine_token
    user_claims = access_token_verifier(
        ctx, client_id_output="cognito_spa_client_id", jwks_url=jwks_url
    )
    machine_claims = access_token_verifier(
        ctx, client_id_output="cognito_mcp_client_id", jwks_url=jwks_url
    )
    conversations: list[str] = []
    owners: list[str] = []
    user_factory = user_provisioner_factory or _default_user_provisioner
    service_factory = service_provisioner_factory or _default_service_provisioner
    budget = ModelTurnBudget(RESOURCE_CLASS)
    provisioners: list[Any] = []
    try:
        user_a = user_factory(ctx)
        provisioners.append(user_a)
        token_a = user_a.provision()
        owners.append(_username_claim(token_a, user_claims))
        yield from _unauthenticated_rows(call, token_a)
        inventory, tool_name = _inventory_row(call, token_a)
        yield inventory

        yield _scope_row("user_token_carries_chat_scope", token_a, "user A", user_claims)
        yield _temperature_argument_row(call, tool_name, token_a)
        if not budget.spend("user A call"):
            yield budget_refused(budget, "user A call")
            return
        first = _call_chat(call, tool_name, token_a, {"message": SMOKE_MESSAGE})
        yield _answer_row("user_call_answered", "user A", first)
        conversation = str((first[1] or {}).get("conversation_id") or "")
        conversations.append(conversation)

        user_b = user_factory(ctx)
        provisioners.append(user_b)
        token_b = user_b.provision()
        owners.append(_username_claim(token_b, user_claims))
        yield _foreign_row(call, tool_name, token_b, conversation)
        yield _forged_identity_row(call, tool_name, token_b, conversation, token_a, user_claims)

        yield _refused_row(
            "gateway1_refuses_chat_user_token", call1, token_a, "Gateway #1 with user A's token"
        )
        yield _invoke_only_token_row(ctx, call, minter, machine_claims)

        service = service_factory(ctx)
        provisioners.append(service)
        fixture = service.provision()
        owners.append(str(getattr(fixture, "rep_id", "") or ""))
        service_token = str(fixture.token)
        yield _scope_row(
            "service_token_carries_chat_scope", service_token, "client_credentials", machine_claims
        )
        if not budget.spend("client_credentials call"):
            yield budget_refused(budget, "client_credentials call")
            return
        service_outcome = _call_chat(call, tool_name, service_token, {"message": SMOKE_MESSAGE})
        conversations.append(str((service_outcome[1] or {}).get("conversation_id") or ""))
        yield _answer_row("service_call_answered", "client_credentials", service_outcome)
    finally:
        yield budget.result()
        yield _remove_conversations(ctx, conversations, owners)
        for provisioner in provisioners:
            yield from provisioner.cleanup()


def _foreign_row(call: McpCaller, tool_name: str, token: str, conversation: str) -> CheckResult:
    check = "foreign_conversation_denied"
    if not CONVERSATION_ID_PATTERN.match(conversation):
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="fail",
            detail=f"user A's call returned no conversation id ({conversation!r}), so there is "
            "nothing for user B to reuse",
        )
    status, result, raw = _call_chat(
        call, tool_name, token, {"message": SMOKE_MESSAGE, "conversation_id": conversation}
    )
    kind = (result or {}).get("error_kind")
    denied = (
        bool((result or {}).get("isError")) and kind == "denied" and "answer" not in (result or {})
    )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check,
        status="pass" if denied else "fail",
        detail=f"user B reusing {conversation} -> status={status!r} error_kind={kind!r}"
        + ("; refused before the Runtime call" if denied else f"; result={raw!r}"),
    )


def expected_check_count(ctx: SmokeContext) -> ExpectedCheckCount:
    """The URL failure alone, or the two rows the `finally` always emits."""
    try:
        _mcp_url(ctx)
    except Exception as exc:  # noqa: BLE001 - the reason belongs in the basis
        return ExpectedCheckCount(
            minimum=1,
            basis=f"mcp_chat_gateway_url is unreadable ({type(exc).__name__}), so `checks` emits "
            "its single terraform_output_present failure",
        )
    return ExpectedCheckCount(
        minimum=2,
        basis="every row after the URL needs a provisioned user, which can legitimately fail "
        "first; the budget row and the conversation_items_removed row are emitted on every path",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the Gateway #2 checks and return the process exit code."""
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
