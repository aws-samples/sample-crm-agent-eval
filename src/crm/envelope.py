# SPDX-License-Identifier: MIT
"""Decode a caller identity from one of the three channels that reach an action Lambda.

The three channels are:

``apigw``
    API Gateway with a Cognito authorizer. Claims arrive already validated, either under
    ``requestContext.authorizer.claims`` (REST ``COGNITO_USER_POOLS`` authorizer) or
    ``requestContext.authorizer.jwt.claims`` (HTTP API payload-v2 JWT authorizer).

``gateway``
    AgentCore Gateway with a Lambda target. The identity is the one the Gateway REQUEST
    interceptor injected under the reserved ``_identity`` key.

``agent``
    An in-app Strands agent invoking the Lambda directly, forwarding the claims of the user who
    invoked the agent runtime.

There is no default: a request whose identity cannot be established raises
:class:`~crm.errors.AuthenticationFailed`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from crm.authz import DEMO_TENANT, Identity, IdentitySource, PrincipalType
from crm.errors import AuthenticationFailed

#: The supported public surface of this module; `decode_identity` is the only way to obtain an
#: `Identity` from untrusted input.
__all__ = [
    "Identity",
    "decode_identity",
    "identity_event",
    "split_book",
    "split_groups",
    "strip_identity",
]

#: Reserved key the Gateway REQUEST interceptor writes into a tool call's arguments, after
#: deleting any caller-supplied value.
IDENTITY_KEY: Final[str] = "_identity"

#: Reserved key an in-app agent uses to forward the claims of the user who invoked it, on the
#: direct-invoke path. On the MCP path ``strip_identity`` removes it.
AGENT_IDENTITY_KEY: Final[str] = "_agent_identity"

#: Key under which API Gateway's Cognito authorizer places already-validated claims, on the
#: ``apigw`` path only. On the MCP tool-argument path ``strip_identity`` removes it.
REQUEST_CONTEXT_KEY: Final[str] = "requestContext"

#: Every key :func:`decode_identity` selects an identity channel from; :func:`strip_identity`
#: removes each of them.
IDENTITY_CHANNEL_KEYS: Final[frozenset[str]] = frozenset(
    {IDENTITY_KEY, AGENT_IDENTITY_KEY, REQUEST_CONTEXT_KEY}
)

CLAIM_REP_ID: Final[str] = "custom:rep_id"
CLAIM_BOOK: Final[str] = "custom:book"
CLAIM_FULL_VISIBILITY: Final[str] = "custom:full_visibility"
CLAIM_TENANT: Final[str] = "custom:tenant"
CLAIM_GROUPS: Final[str] = "cognito:groups"

#: Optional top-level key of the agent identity block naming the forwarded principal type.
AGENT_PRINCIPAL_TYPE_KEY: Final[str] = "principal_type"


def split_book(raw: object) -> frozenset[str]:
    """Parse a book claim into a set of account identifiers.

    Accepts the comma-joined string form the Cognito pre-token-generation trigger produces, and a
    list form for callers constructing an identity programmatically. Blank entries are dropped.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, list | tuple | set | frozenset):
        return frozenset(str(item).strip() for item in raw if str(item).strip())
    if isinstance(raw, str):
        return frozenset(part.strip() for part in raw.split(",") if part.strip())
    raise AuthenticationFailed(
        "Your session is not valid. Sign in again.",
        detail=f"book claim has unsupported type {type(raw).__name__}",
    )


def _invalid_groups(detail: str) -> AuthenticationFailed:
    return AuthenticationFailed("Your session is not valid. Sign in again.", detail=detail)


def _group_names(
    items: list[object] | tuple[object, ...] | set[object] | frozenset[object],
) -> frozenset[str]:
    names: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise _invalid_groups(f"groups claim has a {type(item).__name__} member")
        if item.strip():
            names.add(item.strip())
    return frozenset(names)


def split_groups(raw: object) -> frozenset[str]:
    """Parse a ``cognito:groups`` claim into a set of group names.

    Accepts every rendering the authorizer may deliver: a list, a JSON-array string
    (``'["a","b"]'``), a bracketed string (``"[a, b]"``), a comma string (``"a,b"``), or absent.
    Blank entries are dropped.

    Raises:
        AuthenticationFailed: When the claim has an unsupported type or a non-string member.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, list | tuple | set | frozenset):
        return _group_names(raw)
    if not isinstance(raw, str):
        raise _invalid_groups(f"groups claim has unsupported type {type(raw).__name__}")
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            decoded = json.loads(text)
        except ValueError:
            text = text[1:-1]
        else:
            if not isinstance(decoded, list):
                raise _invalid_groups("groups claim JSON is not an array")
            return _group_names(decoded)
    return frozenset(part.strip() for part in text.split(",") if part.strip())


def _truthy(raw: object) -> bool:
    """Interpret a claim value as a boolean.

    Cognito custom claims are strings, so ``"true"`` must be honoured, but anything unrecognised
    resolves to ``False`` rather than ``True`` — the safe direction for a privilege flag.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "1", "yes"}
    return False


def identity_from_claims(
    claims: dict[str, Any],
    *,
    source: IdentitySource,
    session_id: str = "",
    principal_type: PrincipalType = "user",
    groups: frozenset[str] = frozenset(),
) -> Identity:
    """Build an :class:`Identity` from validated JWT claims.

    Group membership is never read from ``claims``; only the caller passes ``groups``.

    Args:
        claims: Claims from an already-validated token.
        source: Which channel delivered these claims.
        session_id: Correlation identifier for the conversation or MCP session.
        principal_type: ``user`` or ``service``.
        groups: Decoded group membership; non-empty only for the ``apigw`` channel.

    Raises:
        AuthenticationFailed: When the rep identifier claim is absent or empty, or the claims do
            not form a valid identity.
    """
    rep_id = str(claims.get(CLAIM_REP_ID) or "").strip()
    if not rep_id:
        raise AuthenticationFailed(
            "Your account is not linked to a CRM user.",
            detail=f"token missing {CLAIM_REP_ID} claim; source={source}",
        )

    tenant = str(claims.get(CLAIM_TENANT) or DEMO_TENANT).strip() or DEMO_TENANT
    full_visibility = _truthy(claims.get(CLAIM_FULL_VISIBILITY))

    try:
        return Identity(
            rep_id=rep_id,
            book=split_book(claims.get(CLAIM_BOOK)),
            full_visibility=full_visibility,
            principal_type=principal_type,
            session_id=session_id,
            source=source,
            tenant=tenant,
            groups=groups,
        )
    except ValueError as exc:
        raise AuthenticationFailed(
            "Your session is not valid. Sign in again.",
            detail=f"identity construction rejected: {exc}",
        ) from exc


def _from_api_gateway(event: dict[str, Any]) -> Identity:
    """Decode claims placed by the API Gateway Cognito authorizer, in either nesting.

    Reads ``authorizer.jwt.claims`` (HTTP API payload v2) or ``authorizer.claims`` (REST), and
    ``cognito:groups`` via :func:`split_groups`. Absent or non-dict claims raise
    :class:`~crm.errors.AuthenticationFailed`.
    """
    request_context = event.get("requestContext")
    if not isinstance(request_context, dict):
        raise AuthenticationFailed(
            "Authentication is required.", detail="event has no requestContext"
        )
    authorizer = request_context.get("authorizer")
    if not isinstance(authorizer, dict):
        raise AuthenticationFailed(
            "Authentication is required.", detail="requestContext has no authorizer block"
        )

    jwt_block = authorizer.get("jwt")
    claims = jwt_block.get("claims") if isinstance(jwt_block, dict) else authorizer.get("claims")
    if not isinstance(claims, dict):
        raise AuthenticationFailed(
            "Authentication is required.", detail="authorizer block carries no claims"
        )

    return identity_from_claims(
        claims,
        source="apigw",
        session_id=str(request_context.get("requestId") or ""),
        groups=split_groups(claims.get(CLAIM_GROUPS)),
    )


def _from_gateway_identity(payload: object) -> Identity:
    """Decode the ``_identity`` structure injected by the Gateway REQUEST interceptor.

    A malformed structure raises :class:`~crm.errors.AuthenticationFailed`.
    """
    if not isinstance(payload, dict):
        raise AuthenticationFailed(
            "Authentication is required.",
            detail=f"{IDENTITY_KEY} is not an object (got {type(payload).__name__})",
        )

    rep_id = str(payload.get("rep_id") or "").strip()
    if not rep_id:
        raise AuthenticationFailed(
            "Authentication is required.", detail=f"{IDENTITY_KEY} carries no rep_id"
        )

    declared_source = str(payload.get("source") or "")
    if declared_source != "gateway":
        raise AuthenticationFailed(
            "Authentication is required.",
            detail=f"{IDENTITY_KEY}.source is {declared_source!r}, expected 'gateway'",
        )

    principal_raw = str(payload.get("principal_type") or "user")
    if principal_raw not in {"user", "service"}:
        raise AuthenticationFailed(
            "Authentication is required.",
            detail=f"{IDENTITY_KEY}.principal_type is {principal_raw!r}",
        )
    principal_type: PrincipalType = "service" if principal_raw == "service" else "user"

    try:
        return Identity(
            rep_id=rep_id,
            book=split_book(payload.get("book")),
            full_visibility=_truthy(payload.get("full_visibility")),
            principal_type=principal_type,
            session_id=str(payload.get("session_id") or ""),
            source="gateway",
            tenant=str(payload.get("tenant") or DEMO_TENANT) or DEMO_TENANT,
        )
    except ValueError as exc:
        raise AuthenticationFailed(
            "Authentication is required.", detail=f"{IDENTITY_KEY} rejected: {exc}"
        ) from exc


def _from_agent_context(payload: object) -> Identity:
    """Decode the claims an agent forwards, with an optional ``principal_type``.

    ``principal_type`` defaults to ``user`` when absent; any value other than ``user`` or
    ``service`` raises :class:`~crm.errors.AuthenticationFailed`. Groups are never decoded here.
    """
    if not isinstance(payload, dict):
        raise AuthenticationFailed(
            "Authentication is required.",
            detail=f"agent identity block is not an object (got {type(payload).__name__})",
        )
    claims = payload.get("claims")
    if not isinstance(claims, dict):
        raise AuthenticationFailed(
            "Authentication is required.", detail="agent identity block carries no claims"
        )
    principal_raw = payload.get(AGENT_PRINCIPAL_TYPE_KEY, "user")
    if principal_raw not in ("user", "service"):
        raise AuthenticationFailed(
            "Authentication is required.",
            detail=f"agent identity {AGENT_PRINCIPAL_TYPE_KEY} is {principal_raw!r}",
        )
    principal_type: PrincipalType = "service" if principal_raw == "service" else "user"
    return identity_from_claims(
        claims,
        source="agent",
        session_id=str(payload.get("session_id") or ""),
        principal_type=principal_type,
    )


def decode_identity(event: dict[str, Any]) -> Identity:
    """Resolve the caller identity from an action Lambda's event.

    The channel is selected by the presence of a channel-specific key, checked in a fixed order:
    ``_identity``, ``_agent_identity``, ``requestContext``.

    Args:
        event: The raw Lambda event.

    Returns:
        The authenticated caller's identity and scope.

    Raises:
        AuthenticationFailed: When no channel is present, or the matching channel's payload is
            absent, malformed, or missing a rep identifier.
    """
    if not isinstance(event, dict):
        raise AuthenticationFailed(
            "Authentication is required.",
            detail=f"event is not an object (got {type(event).__name__})",
        )

    if IDENTITY_KEY in event:
        return _from_gateway_identity(event[IDENTITY_KEY])

    if AGENT_IDENTITY_KEY in event:
        return _from_agent_context(event[AGENT_IDENTITY_KEY])

    if REQUEST_CONTEXT_KEY in event:
        return _from_api_gateway(event)

    raise AuthenticationFailed(
        "Authentication is required.",
        detail=(
            "event carries no recognised identity channel "
            f"(expected one of {sorted(IDENTITY_CHANNEL_KEYS)!r}); "
            f"keys={sorted(k for k in event if not k.startswith('_'))}"
        ),
    )


def strip_identity(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of tool arguments with every caller-controllable identity channel removed.

    Every key in :data:`IDENTITY_CHANNEL_KEYS` is removed unconditionally. The caller re-injects
    the one channel it trusts afterwards.
    """
    cleaned = dict(arguments)
    for key in IDENTITY_CHANNEL_KEYS:
        cleaned.pop(key, None)
    return cleaned


def identity_event(
    channel: str, identity_payload: Mapping[str, Any], arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the raw action-Lambda event for one identity ``channel``, safe against key shadowing.

    ``arguments`` is passed through :func:`strip_identity` first and ``channel`` is written last,
    so no key inside ``arguments`` can shadow the trusted payload.

    Args:
        channel: The channel key to write, a member of :data:`IDENTITY_CHANNEL_KEYS`.
        identity_payload: The trusted payload for that channel — a decoded identity mapping for
            :data:`IDENTITY_KEY`, a ``{"claims": ...}`` context for :data:`AGENT_IDENTITY_KEY`.
        arguments: The tool's own arguments, from an untrusted caller or a model.

    Returns:
        The event :func:`decode_identity` reads, carrying exactly one identity channel.

    Raises:
        ValueError: When ``channel`` is not a recognised identity channel key.
    """
    if channel not in IDENTITY_CHANNEL_KEYS:
        raise ValueError(
            f"{channel!r} is not an identity channel; expected one of {sorted(IDENTITY_CHANNEL_KEYS)!r}"
        )
    event = strip_identity(dict(arguments))
    event[channel] = dict(identity_payload)
    return event
