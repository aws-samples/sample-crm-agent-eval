# SPDX-License-Identifier: MIT
"""``get_page_context`` action Lambda. Resolves the view in front of the user.

Takes a :data:`crm.toolspec.page_context.PageContext` descriptor of what the user is looking at and
returns scope-checked facts about it. It never receives or returns rows: for an ``entity`` view
only the record's scope-relevant identifiers are returned, and a ``list`` or ``board`` view's query
is echoed for the agent to re-execute through its own read tools, with no row count. Scope is
checked by :func:`crm.authz.require_record_scope`; a denied record raises
:class:`crm.errors.ScopeDenied`.
"""

from __future__ import annotations

from typing import Any, Final

from crm.authz import Identity, account_id_of, require_record_scope
from crm.errors import NotFound, bounded_detail_value
from crm.handlers._common import build_handler
from crm.store.store import Store
from crm.toolspec.page_context import (
    BoardPageContext,
    EntityPageContext,
    ListPageContext,
    OpaquePageContext,
    PageContext,
    parse_page_context,
    to_dict,
)

#: Primary partition-key attribute per entity table, keyed by the plural names
#: :data:`crm.toolspec.page_context.EntityType` accepts.
_KEY_ATTRIBUTE: Final[dict[str, str]] = {
    "accounts": "account_id",
    "contacts": "contact_id",
    "opportunities": "opportunity_id",
    "activities": "activity_id",
}


def _resolve_entity(store: Store, identity: Identity, context: EntityPageContext) -> dict[str, Any]:
    """Confirm the record in view lies within the caller's scope, returning only its identifiers.

    A record in another tenant is reported as :class:`crm.errors.NotFound`, indistinguishable from a
    record that does not exist, so this tool is not an existence oracle across the tenant boundary.
    Within the tenant, the book decision belongs entirely to
    :func:`crm.authz.require_record_scope`, which re-checks the tenant axis too.

    Raises:
        NotFound: No such record in the caller's tenant.
        ScopeDenied: The record exists in the caller's tenant but lies outside its book.
    """
    key_attribute = _KEY_ATTRIBUTE[context.entity_type]
    record = store.get_item(context.entity_type, {key_attribute: context.entity_id})
    if record is None or record.get("tenant") != identity.tenant:
        raise NotFound(
            "The page's record does not exist.",
            detail=(
                f"entity_type={context.entity_type} "
                f"entity_id={bounded_detail_value(context.entity_id)}"
            ),
        )
    require_record_scope(identity, record, operation="get_page_context")
    return {
        "entity_type": context.entity_type,
        "entity_id": context.entity_id,
        "account_id": account_id_of(record),
    }


def _get_page_context(
    store: Store, identity: Identity, arguments: dict[str, Any]
) -> dict[str, Any]:
    context: PageContext = parse_page_context(arguments["page_context"])

    resolved: dict[str, Any] = {
        "kind": context.kind,
        "route": context.route,
        "scope": identity.scope_description(),
    }

    if isinstance(context, OpaquePageContext):
        # An inexpressible view echoes no query descriptor for the agent to run.
        return {**resolved, "summarisable": False, "reason": context.reason}

    if isinstance(context, EntityPageContext):
        return {**resolved, "summarisable": True, **_resolve_entity(store, identity, context)}

    # A list or board descriptor names no single record, so there is nothing to scope-check here;
    # the agent's own read tools enforce scope when it re-executes the query.
    echoed = to_dict(context)
    described: dict[str, Any] = {"query": echoed["query"]}
    if isinstance(context, ListPageContext):
        described["entity_type"] = context.entity_type
    elif not isinstance(context, BoardPageContext):  # pragma: no cover - union is closed
        raise AssertionError(f"unhandled PageContext variant: {type(context).__name__}")
    return {**resolved, "summarisable": True, **described}


handler = build_handler(_get_page_context)
