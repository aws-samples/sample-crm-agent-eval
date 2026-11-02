# SPDX-License-Identifier: MIT
"""AgentCore Gateway REQUEST interceptor that replaces caller identity with a verified one.

Gateway is configured with a REQUEST interceptor (`interceptionPoints: ["REQUEST"]`,
`inputConfiguration: {passRequestHeaders: true}`), so this handler receives the inbound bearer token
at `mcp.gatewayRequest.headers.Authorization` before the call reaches a Lambda target. It re-verifies
that token against Cognito's JWKS (Gateway's own `CUSTOM_JWT` authorizer has already run) and
rewrites `params.arguments` in `transformedGatewayRequest.body` so the injected `_identity` is the
only identity an action Lambda sees on this path.

`crm.envelope.strip_identity` removes every key in `crm.envelope.IDENTITY_CHANNEL_KEYS` from the
caller's arguments first, unconditionally, and only then is the verified `_identity` written.

`tools/list` is the only method that passes through unmodified. Every other method — `tools/call`
and any unrecognised method name — is treated as a tool call: it must present a verifiable bearer
token, and its `params.arguments` are rewritten.

A token carrying `custom:rep_id` (a human sign-in, stamped by the Cognito pre-token-generation
trigger) yields a `user` principal built from those claims. A token without it (every
`client_credentials` machine token) yields the **service principal**: the configured `rep_id`
(`GATEWAY_SERVICE_PRINCIPAL_REP_ID`) whose book and tenant are read from that rep's `reps` item,
with `principal_type: "service"` and never `full_visibility`. When no service principal is
configured — or its `reps` item is absent, carries no explicit `book` list or `tenant`, or asks for
`full_visibility` — no identity is established and `crm.envelope` refuses the call with
`authentication_failed`.

Verification (`_verify_jwt`) checks the RS256 signature against the JWKS-resolved key and expiry,
that `exp` and `iss` are present, that `iss` equals the issuer derived from the configured discovery
URL, that `token_use` is `access` or `id`, that the client identifier read from the claim `token_use`
names (`client_id` or `aud`) is one of `allowed_client_ids`, and any configured required scopes.

Any verification failure short-circuits with `transformedGatewayResponse`, a JSON-RPC error carrying
one fixed user-safe message for every failure reason. Diagnostic detail goes only to the structured
log line.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any, Final

import jwt
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError, PyJWKClientError

from crm.envelope import IDENTITY_KEY, strip_identity

logger = logging.getLogger(__name__)

#: JSON-RPC method that requires no caller identity. Every other method is treated as a tool call
#: and must carry a verified identity before it reaches a Lambda target.
_TOOLS_LIST_METHOD: Final[str] = "tools/list"

#: Fixed user-safe message for every JWT verification failure, identical for every failure reason.
_AUTH_FAILURE_MESSAGE: Final[str] = "Authentication failed. Sign in again to continue."

#: Default in-process cache lifetime for fetched JWKS keys, in seconds.
_DEFAULT_JWKS_CACHE_SECONDS: Final[int] = 3600

#: Environment variable naming the explicitly configured service principal's ``rep_id``. When
#: unset, no service principal exists and a token carrying no ``custom:rep_id`` claim resolves to an
#: unauthenticable identity.
SERVICE_PRINCIPAL_REP_ID_ENV: Final[str] = "GATEWAY_SERVICE_PRINCIPAL_REP_ID"

#: Environment variable carrying the DynamoDB table-name prefix the ``reps`` lookup needs.
TABLE_PREFIX_ENV: Final[str] = "CRM_TABLE_PREFIX"

#: OIDC discovery suffix. The issuer identifier `iss` is verified against is this suffix removed
#: from the configured discovery URL.
_DISCOVERY_SUFFIX: Final[str] = "/.well-known/openid-configuration"

#: Which claim carries the client identifier, keyed by Cognito's ``token_use`` discriminator. Also
#: the set of accepted ``token_use`` values: a token declaring anything else is refused.
_CLIENT_ID_CLAIM: Final[dict[str, str]] = {"access": "client_id", "id": "aud"}

#: ``reps`` item attributes the service-principal mapping reads, matching ``crm.store.schema``'s
#: ``reps`` table and ``crm.auth.pre_token_generation``'s identical lookup.
_REPS_TABLE: Final[str] = "reps"
_BOOK_ATTR: Final[str] = "book"
_TENANT_ATTR: Final[str] = "tenant"
_FULL_VISIBILITY_ATTR: Final[str] = "full_visibility"

#: Resolves one ``reps`` item by ``rep_id``, returning ``None`` when no such rep exists.
RepLookup = Callable[[str], dict[str, Any] | None]


class InterceptorConfigError(Exception):
    """Raised when the interceptor's own configuration is unusable.

    Distinct from a caller's JWT being invalid: the interceptor cannot verify any token at all.
    """


class GatewayInterceptorConfig:
    """Verification parameters for one Cognito user pool, held for the life of the Lambda process.

    Args:
        discovery_url: The pool's OIDC discovery URL, ending in
            ``/.well-known/openid-configuration``. The issuer every token's ``iss`` claim is
            verified against is this URL with that suffix removed; any other shape is refused.
        jwks_url: The pool's JWKS endpoint. Fetched once from ``discovery_url`` by
            :meth:`from_discovery_url`, or supplied directly for testing.
        allowed_client_ids: Client identifiers permitted to present a token, checked against the
            token's ``client_id`` (access token) or ``aud`` (ID token) claim.
        allowed_scopes: Scopes required to be present on an access token's space-delimited
            ``scope`` claim. Empty means no scope requirement is enforced.
        jwks_cache_seconds: How long a fetched signing key is reused before the client re-fetches
            the JWKS document.
        service_principal_rep_id: The ``rep_id`` a service principal acts as. Empty — the
            default — means no service principal is configured, and a token that carries no
            ``custom:rep_id`` claim yields no usable identity at all.
        table_prefix: DynamoDB table-name prefix used to resolve the service principal's ``reps``
            item. Only read when ``service_principal_rep_id`` is non-empty.
    """

    def __init__(
        self,
        *,
        discovery_url: str,
        jwks_url: str,
        allowed_client_ids: frozenset[str],
        allowed_scopes: frozenset[str] = frozenset(),
        jwks_cache_seconds: int = _DEFAULT_JWKS_CACHE_SECONDS,
        service_principal_rep_id: str = "",
        table_prefix: str = "",
    ) -> None:
        if not discovery_url:
            raise InterceptorConfigError("discovery_url must be non-empty")
        if not jwks_url:
            raise InterceptorConfigError("jwks_url must be non-empty")
        if not allowed_client_ids:
            raise InterceptorConfigError("allowed_client_ids must be non-empty")
        self.discovery_url = discovery_url
        self.issuer = _issuer_from_discovery_url(discovery_url)
        self.jwks_url = jwks_url
        self.allowed_client_ids = allowed_client_ids
        self.allowed_scopes = allowed_scopes
        self.service_principal_rep_id = service_principal_rep_id
        self.table_prefix = table_prefix
        self._jwk_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=jwks_cache_seconds)

    def signing_key_for(self, token: str) -> Any:
        """Return the signing key matching the token's ``kid``.

        Raises:
            PyJWKClientError: When the ``kid`` is absent, malformed, or matches no key currently
                published by the JWKS endpoint.
        """
        return self._jwk_client.get_signing_key_from_jwt(token).key


def _extract_bearer_token(headers: dict[str, Any]) -> str | None:
    """Pull the bearer token out of an `Authorization` header.

    Header names are matched case-insensitively.
    """
    for key, value in headers.items():
        if key.lower() == "authorization":
            raw = str(value or "")
            prefix = "bearer "
            if raw.lower().startswith(prefix):
                return raw[len(prefix) :].strip() or None
            return None
    return None


def _issuer_from_discovery_url(discovery_url: str) -> str:
    """Return the OIDC issuer a discovery URL belongs to.

    The issuer is the discovery URL with its ``/.well-known/openid-configuration`` suffix removed.

    Raises:
        InterceptorConfigError: ``discovery_url`` does not end in the well-known suffix, or names no
            issuer before it.
    """
    if not discovery_url.endswith(_DISCOVERY_SUFFIX):
        raise InterceptorConfigError(f"discovery_url must end in {_DISCOVERY_SUFFIX!r}")
    issuer = discovery_url[: -len(_DISCOVERY_SUFFIX)]
    if not issuer:
        raise InterceptorConfigError("discovery_url names no issuer")
    return issuer


def _client_identifier(claims: dict[str, Any], token_use: str) -> str:
    """Return the client identifier the token's own ``token_use`` says carries it.

    An ``aud`` may be a list per RFC 7519; a single-valued list resolves to that value and a
    multi-valued one resolves to no client at all.
    """
    raw = claims.get(_CLIENT_ID_CLAIM[token_use])
    if isinstance(raw, list | tuple):
        return str(raw[0]) if len(raw) == 1 else ""
    return str(raw or "")


def _verify_jwt(token: str, config: GatewayInterceptorConfig) -> dict[str, Any]:
    """Verify signature and standard claims, returning the decoded claims on success.

    Verifies the signature against the JWKS-resolved key, standard expiry, that ``iss`` is present
    and equals ``config.issuer``, and that the token was issued for one of
    ``config.allowed_client_ids``. Cognito's ``token_use`` claim selects which claim carries the
    client identifier — ``client_id`` on an access token, ``aud`` on an ID token — and must be one of
    those two values. When
    ``config.allowed_scopes`` is non-empty, every required scope must appear in the token's
    space-delimited ``scope`` claim.

    Raises:
        InvalidTokenError: Signature failure, expiry, a missing or mismatched issuer, an absent or
            unrecognised ``token_use``, an unrecognised client, or a missing required scope.
        PyJWKClientError: The token's ``kid`` does not resolve to any published signing key.
    """
    signing_key = config.signing_key_for(token)
    claims: dict[str, Any] = jwt.decode(
        token,
        signing_key,
        algorithms=["RS256"],
        issuer=config.issuer,
        options={"verify_aud": False, "require": ["exp", "iss"]},
    )

    token_use = str(claims.get("token_use") or "")
    if token_use not in _CLIENT_ID_CLAIM:
        raise InvalidTokenError(f"token carries unrecognised token_use {token_use!r}")
    client_id = _client_identifier(claims, token_use)
    if client_id not in config.allowed_client_ids:
        raise InvalidTokenError(f"token issued for unrecognised client {client_id!r}")

    if config.allowed_scopes:
        granted = set(str(claims.get("scope") or "").split())
        missing = config.allowed_scopes - granted
        if missing:
            raise InvalidTokenError(f"token missing required scope(s) {sorted(missing)!r}")

    return claims


def _split_book(raw: object) -> list[str]:
    """Parse a Cognito ``custom:book`` claim into an ordered list of account identifiers.

    Parses the comma-joined form the pre-token-generation trigger produces, into the list shape
    `crm.envelope._from_gateway_identity` expects for the injected ``book`` field.
    """
    if raw is None:
        return []
    if isinstance(raw, list | tuple):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _truthy(raw: object) -> bool:
    """Interpret a Cognito custom claim value as a boolean, defaulting unrecognised input to False."""
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"true", "1", "yes"}


def _unauthenticable_identity(session_id: str) -> dict[str, Any]:
    """Return the identity payload that makes `crm.envelope` refuse with ``authentication_failed``.

    An empty ``rep_id`` is what `crm.envelope._from_gateway_identity` rejects; this is used whenever
    neither a user nor a service identity could be established.
    """
    return {
        "rep_id": "",
        "book": [],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": session_id,
        "source": "gateway",
        "tenant": "",
    }


def _default_rep_lookup(table_prefix: str) -> RepLookup:
    """Return a `RepLookup` reading one ``reps`` item through :class:`crm.store.store.Store`.

    Constructed lazily, per call, so importing this module never builds an AWS client.
    """

    def lookup(rep_id: str) -> dict[str, Any] | None:
        from crm.store.store import Store

        item = Store(table_prefix=table_prefix).get_item(_REPS_TABLE, {"rep_id": rep_id})
        return dict(item) if item is not None else None

    return lookup


def _service_identity(
    config: GatewayInterceptorConfig, *, session_id: str, rep_lookup: RepLookup
) -> dict[str, Any] | None:
    """Return the service principal's `_identity`, or ``None`` when it cannot be established.

    Every one of the following must hold; otherwise ``None`` is returned (which the caller turns
    into ``authentication_failed``):

    * ``config.service_principal_rep_id`` is configured;
    * a ``reps`` item exists at that ``rep_id``;
    * that item carries an explicit ``book`` list (an absent or non-list ``book`` is refused);
    * that item carries an explicit non-empty ``tenant``;
    * that item does not ask for ``full_visibility``.

    An explicitly empty ``book`` (``[]``) is honoured: that is a configured principal with no
    accounts, which every scope check then denies.
    """
    rep_id = config.service_principal_rep_id.strip()
    if not rep_id:
        return None

    try:
        item = rep_lookup(rep_id)
    except Exception:  # noqa: BLE001 - a lookup failure must fail closed, not raise a 500.
        logger.info(json.dumps({"event": "gateway_service_identity_lookup_failed"}))
        return None

    if item is None:
        logger.info(json.dumps({"event": "gateway_service_identity_absent"}))
        return None

    raw_book = item.get(_BOOK_ATTR)
    if not isinstance(raw_book, list | tuple):
        logger.info(json.dumps({"event": "gateway_service_identity_book_not_explicit"}))
        return None

    tenant = str(item.get(_TENANT_ATTR) or "").strip()
    if not tenant:
        logger.info(json.dumps({"event": "gateway_service_identity_tenant_absent"}))
        return None

    if _truthy(item.get(_FULL_VISIBILITY_ATTR)):
        logger.info(json.dumps({"event": "gateway_service_identity_full_visibility_refused"}))
        return None

    return {
        "rep_id": rep_id,
        "book": _split_book(raw_book),
        "full_visibility": False,
        "principal_type": "service",
        "session_id": session_id,
        "source": "gateway",
        "tenant": tenant,
    }


def _build_verified_identity(
    claims: dict[str, Any],
    *,
    session_id: str,
    config: GatewayInterceptorConfig | None = None,
    rep_lookup: RepLookup | None = None,
) -> dict[str, Any]:
    """Build the `_identity` payload `crm.envelope.decode_identity` expects from the gateway channel.

    Shape matches `crm.envelope._from_gateway_identity` exactly: `rep_id`, `book`, `full_visibility`,
    `principal_type`, `session_id`, `source="gateway"`, and `tenant` (the verified token's
    `custom:tenant` claim; `crm.envelope._from_gateway_identity` defaults an absent value to the
    `demo` tenant).

    A token carrying ``custom:rep_id`` is a human sign-in and yields a ``user`` principal. A token
    without it — every ``client_credentials`` machine token — falls through to the service principal
    (:func:`_service_identity`), and to an unauthenticable identity when none is configured.

    Args:
        claims: Claims of an already-verified token.
        session_id: The MCP session id, echoed into the identity for correlation.
        config: Interceptor configuration. Required for the service-principal path; when absent,
            only the user path is available.
        rep_lookup: Resolves the service principal's ``reps`` item. Defaults to a real
            :class:`crm.store.store.Store` read against ``config.table_prefix``.
    """
    rep_id = str(claims.get("custom:rep_id") or "").strip()
    if rep_id:
        return {
            "rep_id": rep_id,
            "book": _split_book(claims.get("custom:book")),
            "full_visibility": _truthy(claims.get("custom:full_visibility")),
            "principal_type": "user",
            "session_id": session_id,
            "source": "gateway",
            "tenant": str(claims.get("custom:tenant") or "").strip(),
        }

    if config is None:
        return _unauthenticable_identity(session_id)

    lookup = rep_lookup if rep_lookup is not None else _default_rep_lookup(config.table_prefix)
    service_identity = _service_identity(config, session_id=session_id, rep_lookup=lookup)
    return (
        service_identity if service_identity is not None else _unauthenticable_identity(session_id)
    )


def _deny(body: dict[str, Any]) -> dict[str, Any]:
    """Build the short-circuiting `transformedGatewayResponse` for a verification failure.

    The JSON-RPC ``id`` is echoed from the inbound request so the caller can correlate the error;
    everything else in the response is the fixed user-safe message, with no token content and no
    internal diagnostic detail.
    """
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "body": {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {"code": -32001, "message": _AUTH_FAILURE_MESSAGE},
                }
            }
        },
    }


def _passthrough(body: dict[str, Any]) -> dict[str, Any]:
    """Return the request unmodified for a method that carries no caller identity to replace."""
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayRequest": {"body": body}},
    }


def _replace_identity(body: dict[str, Any], verified_identity: dict[str, Any]) -> dict[str, Any]:
    """Return a rewritten JSON-RPC body with `params.arguments._identity` unconditionally replaced.

    Strips every key in `crm.envelope.IDENTITY_CHANNEL_KEYS` from the caller-supplied arguments
    first, then writes `verified_identity` under `_identity`. The strip is unconditional.
    """
    params = dict(body.get("params") or {})
    raw_arguments = params.get("arguments")
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}

    cleaned = strip_identity(arguments)
    cleaned[IDENTITY_KEY] = verified_identity

    params["arguments"] = cleaned
    rewritten = dict(body)
    rewritten["params"] = params
    return rewritten


def lambda_handler(event: dict[str, Any], _context: object) -> dict[str, Any]:
    """AgentCore Gateway REQUEST interceptor entry point.

    Args:
        event: Carries `event["mcp"]["gatewayRequest"]` with `headers` (present because
            `passRequestHeaders: true` is configured) and `body`, the full inbound JSON-RPC
            envelope.
        _context: The Lambda context object, unused.

    Returns:
        `transformedGatewayRequest` carrying the rewritten body on success or passthrough, or
        `transformedGatewayResponse` short-circuiting the call on a verification failure.
    """
    config = _load_config()

    gateway_request = event.get("mcp", {}).get("gatewayRequest", {})
    headers = gateway_request.get("headers") or {}
    body = gateway_request.get("body") or {}

    if body.get("method") == _TOOLS_LIST_METHOD:
        return _passthrough(body)

    token = _extract_bearer_token(headers)
    if token is None:
        logger.info(json.dumps({"event": "gateway_auth_denied", "reason": "no_bearer_token"}))
        return _deny(body)

    try:
        claims = _verify_jwt(token, config)
    except (InvalidTokenError, PyJWKClientError) as exc:
        logger.info(
            json.dumps(
                {
                    "event": "gateway_auth_denied",
                    "reason": type(exc).__name__,
                    "ts": int(time.time()),
                }
            )
        )
        return _deny(body)

    session_id = ""
    for key, value in headers.items():
        if key.lower() == "mcp-session-id":
            session_id = str(value or "")
            break

    verified_identity = _build_verified_identity(claims, session_id=session_id, config=config)
    rewritten_body = _replace_identity(body, verified_identity)

    logger.info(
        json.dumps(
            {
                "event": "gateway_auth_verified",
                "rep_id": verified_identity["rep_id"],
                "principal_type": verified_identity["principal_type"],
                "session_id": session_id,
            }
        )
    )

    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayRequest": {"body": rewritten_body}},
    }


_config_cache: GatewayInterceptorConfig | None = None


def _load_config() -> GatewayInterceptorConfig:
    """Return the process-wide interceptor configuration, loading it once per Lambda instance.

    Reads `COGNITO_DISCOVERY_URL`, `COGNITO_JWKS_URL`, `COGNITO_ALLOWED_CLIENT_IDS` (comma-separated),
    the optional `COGNITO_ALLOWED_SCOPES` (comma-separated), and the optional
    `GATEWAY_SERVICE_PRINCIPAL_REP_ID` / `CRM_TABLE_PREFIX` pair that enables the service principal,
    from the process environment. Cached at module scope so a warm invocation reuses the same
    `PyJWKClient` and its key cache.

    An unset `GATEWAY_SERVICE_PRINCIPAL_REP_ID` is not an error: no service principal exists, and a
    machine token then resolves to no usable identity.

    Raises:
        InterceptorConfigError: A required environment variable is absent or empty.
    """
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    import os

    discovery_url = os.environ.get("COGNITO_DISCOVERY_URL", "")
    jwks_url = os.environ.get("COGNITO_JWKS_URL", "")
    client_ids_raw = os.environ.get("COGNITO_ALLOWED_CLIENT_IDS", "")
    scopes_raw = os.environ.get("COGNITO_ALLOWED_SCOPES", "")

    allowed_client_ids = frozenset(
        part.strip() for part in client_ids_raw.split(",") if part.strip()
    )
    allowed_scopes = frozenset(part.strip() for part in scopes_raw.split(",") if part.strip())

    _config_cache = GatewayInterceptorConfig(
        discovery_url=discovery_url,
        jwks_url=jwks_url,
        allowed_client_ids=allowed_client_ids,
        allowed_scopes=allowed_scopes,
        service_principal_rep_id=os.environ.get(SERVICE_PRINCIPAL_REP_ID_ENV, "").strip(),
        table_prefix=os.environ.get(TABLE_PREFIX_ENV, ""),
    )
    return _config_cache
