# SPDX-License-Identifier: MIT
"""Decode a caller identity from one of the three channels that reach an action Lambda.

The three channels are:

``apigw``
    API Gateway with a Cognito authorizer. Claims arrive already validated, either under
    ``requestContext.authorizer.claims`` (REST ``COGNITO_USER_POOLS`` authorizer) or
    ``requestContext.authorizer.jwt.claims`` (HTTP API payload-v2 JWT authorizer). The token's
    app client (``aud`` of an ID token, ``client_id`` of an access token) must be listed in the
    comma-separated SSM String parameter named by ``CRM_API_ALLOWED_CLIENT_IDS_PARAMETER``, read
    once per process.

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
import os
from collections.abc import Callable, Mapping
from typing import Any, Final

from crm.authz import DEMO_TENANT, Identity, IdentitySource, PrincipalType
from crm.errors import AuthenticationFailed, CrmError, wrap_aws_error

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

#: Required top-level key of the agent identity block naming the forwarded principal type.
AGENT_PRINCIPAL_TYPE_KEY: Final[str] = "principal_type"

#: Environment variable naming the SSM SecureString parameter that lists, comma-separated, the app
#: client ids admitted on the ``apigw`` channel.
API_ALLOWED_CLIENT_IDS_PARAMETER_ENV: Final[str] = "CRM_API_ALLOWED_CLIENT_IDS_PARAMETER"

#: Reads one SSM SecureString parameter's decrypted value by name; raises on failure.
ParameterReader = Callable[[str], str]

_SSM_OPERATION: Final[str] = "ssm:GetParameter(api_allowed_client_ids)"
_allowed_client_ids_cache: frozenset[str] | None = None

#: The claim naming the token's app client, per Cognito ``token_use``.
_CLIENT_CLAIM_BY_TOKEN_USE: Final[Mapping[str, str]] = {"id": "aud", "access": "client_id"}


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


def _ssm_parameter_reader(name: str) -> str:
    """Return the decrypted value of the SSM SecureString parameter `name`.

    Read with a short-timeout client and ``WithDecryption=True``.

    Raises:
        ValueError: The parameter's type is not ``SecureString``.
    """
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "ssm",
        config=Config(
            connect_timeout=2,
            read_timeout=2,
            retries={"mode": "standard", "total_max_attempts": 3},
        ),
    )
    parameter = client.get_parameter(Name=name, WithDecryption=True)["Parameter"]
    parameter_type = parameter.get("Type")
    if parameter_type != "SecureString":
        raise ValueError(f"parameter {name} has type {parameter_type!r}, expected 'SecureString'")
    return str(parameter["Value"])


_parameter_reader: ParameterReader = _ssm_parameter_reader


def _allowed_client_ids() -> frozenset[str]:
    """Return the admitted ``apigw`` app client ids, read once per process and then cached.

    Raises:
        CrmError: The parameter variable is unset, the read fails (mapped by
            :func:`~crm.errors.wrap_aws_error`, with any 4xx member raised as a 5xx instead), or
            the value lists no client id. Only a non-empty value is cached; a failed read or an
            empty value is read again on the next call.
    """
    global _allowed_client_ids_cache
    if _allowed_client_ids_cache is not None:
        allowed = _allowed_client_ids_cache
    else:
        name = os.environ.get(API_ALLOWED_CLIENT_IDS_PARAMETER_ENV, "").strip()
        if not name:
            raise CrmError(
                "The request could not be completed.",
                detail=f"{API_ALLOWED_CLIENT_IDS_PARAMETER_ENV} is unset",
            )
        try:
            raw = _parameter_reader(name)
        except Exception as exc:
            error = wrap_aws_error(exc, operation=_SSM_OPERATION)
            if error.http_status < 500:
                error = CrmError("The request could not be completed.", detail=error.detail)
            error.detail = f"parameter={name} {error.detail}"
            raise error from exc
        allowed = frozenset(part.strip() for part in raw.split(",") if part.strip())
        if allowed:
            _allowed_client_ids_cache = allowed
    if not allowed:
        raise CrmError(
            "The request could not be completed.",
            detail="the API client allowlist parameter lists no client id",
        )
    return allowed


def _require_allowed_client(claims: Mapping[str, Any]) -> None:
    """Refuse an ``apigw`` token not issued to an admitted app client.

    An ID token (``token_use=id``) is matched on ``aud``, an access token (``token_use=access``) on
    ``client_id``, against :func:`_allowed_client_ids`. An absent or unrecognised ``token_use``,
    and an absent or unlisted client claim, raise :class:`~crm.errors.AuthenticationFailed`; an
    unavailable or empty allowlist raises a 5xx :class:`~crm.errors.CrmError`.
    """
    token_use = claims.get("token_use")
    client_claim = _CLIENT_CLAIM_BY_TOKEN_USE.get(token_use) if isinstance(token_use, str) else None
    if client_claim is None:
        raise AuthenticationFailed(
            "Authentication is required.", detail="token carries no recognised token_use"
        )
    client_id = claims.get(client_claim)
    if not isinstance(client_id, str) or client_id not in _allowed_client_ids():
        raise AuthenticationFailed(
            "Authentication is required.",
            detail=f"token {client_claim} is not an admitted app client",
        )


def _from_api_gateway(event: dict[str, Any]) -> Identity:
    """Decode claims placed by the API Gateway Cognito authorizer, in either nesting.

    Reads ``authorizer.jwt.claims`` (HTTP API payload v2) or ``authorizer.claims`` (REST), and
    ``cognito:groups`` via :func:`split_groups`. Absent or non-dict claims, and a token whose app
    client :func:`_require_allowed_client` refuses, raise :class:`~crm.errors.AuthenticationFailed`.
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

    _require_allowed_client(claims)
    return identity_from_claims(
        claims,
        source="apigw",
        session_id=str(request_context.get("requestId") or ""),
        groups=split_groups(claims.get(CLAIM_GROUPS)),
    )


def _from_gateway_identity(payload: object) -> Identity:
    """Decode the ``_identity`` structure injected by the Gateway REQUEST interceptor.

    A malformed structure, including an absent, empty or unrecognised ``principal_type``, raises
    :class:`~crm.errors.AuthenticationFailed`.
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

    principal_type = _declared_principal_type(
        payload, key="principal_type", channel=f"{IDENTITY_KEY}.principal_type"
    )

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


def _declared_principal_type(
    payload: Mapping[str, object], *, key: str, channel: str
) -> PrincipalType:
    """Return ``payload[key]`` when it is ``user`` or ``service``.

    Raises:
        AuthenticationFailed: The key is absent, or its value is anything else (``None`` and the
            empty string included).
    """
    if key not in payload:
        raise AuthenticationFailed("Authentication is required.", detail=f"{channel} is absent")
    declared = payload[key]
    if declared == "user":
        return "user"
    if declared == "service":
        return "service"
    raise AuthenticationFailed("Authentication is required.", detail=f"{channel} is {declared!r}")


def _from_agent_context(payload: object) -> Identity:
    """Decode the claims an agent forwards, with its required ``principal_type``.

    An absent ``principal_type``, or any value other than ``user`` or ``service``, raises
    :class:`~crm.errors.AuthenticationFailed`. Groups are never decoded here.
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
    principal_type = _declared_principal_type(
        payload, key=AGENT_PRINCIPAL_TYPE_KEY, channel=f"agent identity {AGENT_PRINCIPAL_TYPE_KEY}"
    )
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
