# SPDX-License-Identifier: MIT
"""Signature-verified claims of a Cognito-issued JWT, for smoke scripts that read token claims.

`verified_claims` accepts RS256 only, fetches the signing key from the pool's JWKS through
`jwt.PyJWKClient`, and verifies the signature, `exp`, `iss`, `token_use`, and the audience (`aud`
for an ID token, the `client_id` claim for an access token). Any failure raises
`ClaimsVerificationError` whose `check` attribute names the check that failed.
"""

from __future__ import annotations

import functools
import urllib.parse
from typing import Any, Final, Literal

import jwt

#: The only signing algorithm Cognito user pools use, and the only one accepted here.
_ALGORITHM: Final = "RS256"

#: The two `token_use` values Cognito issues.
USE_ID: Final = "id"
USE_ACCESS: Final = "access"

#: Default allowance, in seconds, between the host clock and Cognito's for `iat`, `nbf` and `exp`.
CLOCK_SKEW_SECONDS: Final = 60


class ClaimsVerificationError(Exception):
    """A token failed verification; `check` names which check (e.g. `signature`, `iss`)."""

    def __init__(self, check: str, detail: str) -> None:
        super().__init__(f"{check}: {detail}")
        self.check = check


def cognito_issuer(region: str, user_pool_id: str) -> str:
    """Return the `iss` value Cognito stamps on tokens from `user_pool_id` in `region`."""
    return f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"


def cognito_jwks_url(region: str, user_pool_id: str) -> str:
    """Return the pool's public JWKS endpoint."""
    return f"{cognito_issuer(region, user_pool_id)}/.well-known/jwks.json"


#: Hosts a plain-http `jwks_url` may name: the loopback test JWKS server.
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost"})


def require_trusted_jwks_url(url: str) -> None:
    """Accept an `https` URL with a host, or `http` to a loopback host; else raise.

    Raises:
        ClaimsVerificationError: `check` is `jwks_url`.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS:
        return
    raise ClaimsVerificationError(
        "jwks_url", f"{url!r} is neither https nor a loopback http URL; refusing to fetch keys"
    )


@functools.lru_cache(maxsize=16)
def _jwk_client(jwks_url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(jwks_url)


def verified_claims(
    token: str,
    *,
    region: str,
    user_pool_id: str,
    client_id: str,
    token_use: Literal["id", "access"],
    jwks_url: str | None = None,
    leeway_seconds: int = CLOCK_SKEW_SECONDS,
) -> dict[str, Any]:
    """Return `token`'s claims once every check passes.

    Args:
        token: The raw JWT.
        region, user_pool_id: Identify the issuing pool; `iss` must equal `cognito_issuer(...)`.
        client_id: The app client; must equal `aud` (ID token) or `client_id` (access token).
        token_use: The expected `token_use` claim.
        jwks_url: JWKS endpoint; defaults to `cognito_jwks_url(region, user_pool_id)`.
        leeway_seconds: Clock-skew allowance applied to `iat`, `nbf` and `exp`.

    Raises:
        ClaimsVerificationError: `check` is one of `header`, `alg`, `jwks_url`, `signing_key`,
            `signature`, `exp`, `iat`, `nbf`, `iss`, `aud`, `claims`, `token_use`, `client_id`.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise ClaimsVerificationError("header", f"unreadable JWT header: {exc}") from exc
    alg = header.get("alg")
    if alg != _ALGORITHM:
        raise ClaimsVerificationError("alg", f"alg {alg!r} is not {_ALGORITHM}")

    url = jwks_url if jwks_url is not None else cognito_jwks_url(region, user_pool_id)
    require_trusted_jwks_url(url)
    try:
        signing_key = _jwk_client(url).get_signing_key_from_jwt(token).key
    except jwt.PyJWTError as exc:
        raise ClaimsVerificationError(
            "signing_key", f"no signing key for kid {header.get('kid')!r} at {url}: {exc}"
        ) from exc

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=[_ALGORITHM],
            issuer=cognito_issuer(region, user_pool_id),
            audience=client_id if token_use == USE_ID else None,
            leeway=leeway_seconds,
            options={
                "require": ["exp", "iss"],
                "verify_aud": token_use == USE_ID,
            },
        )
    except jwt.InvalidSignatureError as exc:
        raise ClaimsVerificationError("signature", str(exc)) from exc
    except jwt.ExpiredSignatureError as exc:
        raise ClaimsVerificationError("exp", str(exc)) from exc
    except jwt.ImmatureSignatureError as exc:
        raise ClaimsVerificationError("iat" if "(iat)" in str(exc) else "nbf", str(exc)) from exc
    except jwt.InvalidIssuerError as exc:
        raise ClaimsVerificationError("iss", str(exc)) from exc
    except jwt.InvalidAudienceError as exc:
        raise ClaimsVerificationError("aud", str(exc)) from exc
    except jwt.MissingRequiredClaimError as exc:
        raise ClaimsVerificationError(exc.claim, str(exc)) from exc
    except jwt.PyJWTError as exc:
        raise ClaimsVerificationError("claims", str(exc)) from exc

    if claims.get("token_use") != token_use:
        raise ClaimsVerificationError(
            "token_use", f"token_use {claims.get('token_use')!r}, expected {token_use!r}"
        )
    if token_use == USE_ACCESS and claims.get("client_id") != client_id:
        raise ClaimsVerificationError(
            "client_id", f"client_id {claims.get('client_id')!r}, expected {client_id!r}"
        )
    return claims
