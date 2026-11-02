# SPDX-License-Identifier: MIT
"""Shared helpers for smoke modules that reach the AgentCore Runtime or read its caller's token.

* :func:`is_explicit_inbound_deny` recognises the Runtime's resource-policy refusal of a direct
  `InvokeAgentRuntime`.
* :func:`verified_access_claims` returns a Cognito access token's claims once it verifies against
  the stack's user pool.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError
from scripts.smoke.harness import SmokeContext
from scripts.smoke.verified_claims import USE_ACCESS, verified_claims

__all__ = ["is_explicit_inbound_deny", "verified_access_claims"]


def is_explicit_inbound_deny(exc: BaseException, *, runtime_arn: str | None = None) -> bool:
    """Whether `exc` is an `AccessDeniedException` from an explicit deny in a resource-based policy.

    When `runtime_arn` is given, the message must also name it (or its endpoint ARN, which extends
    it). A deny from an identity policy, a service control policy or a permissions boundary, and any
    other error, is `False`.
    """
    if not isinstance(exc, ClientError):
        return False
    error = exc.response.get("Error") or {}
    code = str(error.get("Code") or "")
    message = str(error.get("Message") or "").lower()
    return (
        code == "AccessDeniedException"
        and "explicit deny in a resource-based policy" in message
        and (runtime_arn is None or runtime_arn.lower() in message)
    )


def verified_access_claims(
    ctx: SmokeContext, token: str, *, jwks_url: str | None = None
) -> dict[str, Any]:
    """Return `token`'s claims once it verifies as an access token from the stack's user pool.

    Args:
        ctx: Supplies the region and the `cognito_user_pool_id` / `cognito_spa_client_id` outputs.
        token: The Cognito access token.
        jwks_url: JWKS endpoint to verify against; the pool's by default.

    Raises:
        ClaimsVerificationError: The token failed a check `verified_claims` makes.
    """
    return verified_claims(
        token,
        region=ctx.region,
        user_pool_id=ctx.outputs.require_str("cognito_user_pool_id"),
        client_id=ctx.outputs.require_str("cognito_spa_client_id"),
        token_use=USE_ACCESS,
        jwks_url=jwks_url,
    )
