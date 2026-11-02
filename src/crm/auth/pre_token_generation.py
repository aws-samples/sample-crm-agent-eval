# SPDX-License-Identifier: MIT
"""Cognito pre-token-generation V2 Lambda trigger that stamps a rep's scope into its tokens.

Writes ``custom:rep_id``, ``custom:book`` (comma-joined account ids), ``custom:full_visibility`` and
``custom:tenant`` into both the ID and access tokens. The book, flag and tenant come from the
``reps`` table item keyed by ``event["userName"]``. A missing or malformed item, or a failed lookup,
yields the least-privilege defaults (empty book, no full visibility, ``demo`` tenant); the handler
never raises.

The access token also gains the Gateway #2 chat scope named by env ``CHAT_SCOPE`` through
``scopesToAdd``. An unset, blank or malformed ``CHAT_SCOPE``, or one naming
``gateway/invoke``, adds no scope. Groups (``cognito:groups``) are never added, overridden or
suppressed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: reps table item attribute names, matching crm.store.schema's "reps" TableSchema.
_BOOK_ATTR = "book"
_FULL_VISIBILITY_ATTR = "full_visibility"
_TENANT_ATTR = "tenant"

#: Least-privilege defaults applied when no reps item exists, or an attribute is absent or the
#: wrong shape.
_DEFAULT_BOOK: tuple[str, ...] = ()
_DEFAULT_FULL_VISIBILITY = False
_DEFAULT_TENANT = "demo"

#: Env var naming the scope added to every user access token.
CHAT_SCOPE_ENV = "CHAT_SCOPE"

#: The machine-client scope Gateway #1 requires; never issued to a user token.
_GATEWAY_INVOKE_SCOPE = "gateway/invoke"


def _coerce_book(raw: object) -> tuple[str, ...]:
    """Return ``raw`` (a list, tuple or set) as a tuple of account ids, or the fail-closed default."""
    if isinstance(raw, list | tuple | set | frozenset):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    return _DEFAULT_BOOK


def _coerce_full_visibility(raw: object) -> bool:
    """Return ``raw`` as a bool, defaulting anything not already a real bool to False."""
    return raw is True


def _rep_scope(rep_item: dict[str, Any] | None) -> tuple[tuple[str, ...], bool, str]:
    """Return ``(book, full_visibility, tenant)`` for ``rep_item``, or the fail-closed defaults."""
    if rep_item is None:
        return _DEFAULT_BOOK, _DEFAULT_FULL_VISIBILITY, _DEFAULT_TENANT
    book = _coerce_book(rep_item.get(_BOOK_ATTR))
    full_visibility = _coerce_full_visibility(rep_item.get(_FULL_VISIBILITY_ATTR))
    tenant_raw = rep_item.get(_TENANT_ATTR)
    tenant = str(tenant_raw).strip() if tenant_raw else _DEFAULT_TENANT
    return book, full_visibility, tenant


def _claims_to_add_or_override(
    rep_id: str, book: tuple[str, ...], full_visibility: bool, tenant: str
) -> dict[str, str]:
    """Render the four scope claims as strings: ``custom:book`` comma-joined and
    ``custom:full_visibility`` as ``"true"``/``"false"``.
    """
    return {
        "custom:rep_id": rep_id,
        "custom:book": ",".join(book),
        "custom:full_visibility": "true" if full_visibility else "false",
        "custom:tenant": tenant,
    }


def _scopes_to_add() -> list[str]:
    """Return ``[CHAT_SCOPE]``, or ``[]`` when it is unset, blank, contains whitespace, or is the
    machine-client scope ``gateway/invoke``.
    """
    import os

    scope = os.environ.get(CHAT_SCOPE_ENV, "").strip()
    if not scope or any(ch.isspace() for ch in scope) or scope == _GATEWAY_INVOKE_SCOPE:
        if scope:
            logger.info(json.dumps({"event": "pre_token_generation_chat_scope_refused"}))
        return []
    return [scope]


def _default_store() -> Any:
    """Construct a :class:`crm.store.store.Store` using ``CRM_TABLE_PREFIX`` from the environment."""
    import os

    from crm.store.store import Store

    return Store(table_prefix=os.environ.get("CRM_TABLE_PREFIX", ""))


def lambda_handler(
    event: dict[str, Any], context: object = None, *, store: Any | None = None
) -> dict[str, Any]:
    """Cognito V2 pre-token-generation entry point. Never raises; a lookup failure yields the
    least-privilege defaults.

    Args:
        event: The Cognito V2 pre-token-generation request event.
        context: Unused; accepted to match the Lambda handler signature.
        store: Injectable :class:`crm.store.store.Store` for tests; constructed lazily otherwise.

    Returns:
        ``event`` with ``response.claimsAndScopeOverrideDetails`` populated.
    """
    del context
    rep_id = str(event.get("userName") or "").strip()

    book: tuple[str, ...] = _DEFAULT_BOOK
    full_visibility = _DEFAULT_FULL_VISIBILITY
    tenant = _DEFAULT_TENANT

    if rep_id:
        try:
            resolved_store = store if store is not None else _default_store()
            rep_item = resolved_store.get_item("reps", {"rep_id": rep_id})
            book, full_visibility, tenant = _rep_scope(rep_item)
        except Exception:  # noqa: BLE001 - a lookup failure must fail closed, never fail sign-in.
            logger.info(
                json.dumps({"event": "pre_token_generation_lookup_failed", "rep_id": rep_id})
            )
            book, full_visibility, tenant = _DEFAULT_BOOK, _DEFAULT_FULL_VISIBILITY, _DEFAULT_TENANT
    else:
        logger.info(json.dumps({"event": "pre_token_generation_missing_username"}))

    claims = _claims_to_add_or_override(rep_id, book, full_visibility, tenant)

    event["response"] = {
        "claimsAndScopeOverrideDetails": {
            "idTokenGeneration": {"claimsToAddOrOverride": claims},
            "accessTokenGeneration": {
                "claimsToAddOrOverride": claims,
                "scopesToAdd": _scopes_to_add(),
            },
        }
    }
    return event
