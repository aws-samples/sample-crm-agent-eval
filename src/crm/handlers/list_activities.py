# SPDX-License-Identifier: MIT
"""``list_activities`` action Lambda. Activities attached to one opportunity, most recent first
within each page, optionally filtered by kind, with cursor pagination; ``next_cursor`` is bound to
the caller and the opportunity it was issued for (:mod:`crm.cursor`).

A page over :data:`crm.paging.HANDLER_PAGE_BYTE_BUDGET` is cut in query order, marked
``truncated: True``, and its ``next_cursor`` resumes after the last activity returned.
"""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, require_record_scope
from crm.cursor import bound_operation, decode_cursor, encode_cursor
from crm.errors import NotFound, bounded_detail_value
from crm.handlers._common import build_handler
from crm.paging import bound_page, resume_key
from crm.store.keys import tenant_prefixed
from crm.store.store import Store

_DEFAULT_LIMIT = 50
_OPERATION = "list_activities"
_TABLE = "activities"
_INDEX = "gsi_opportunity"


def _list_activities(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    opportunity_id = str(arguments["opportunity_id"])
    opportunity = store.get_item("opportunities", {"opportunity_id": opportunity_id})
    if opportunity is None or opportunity.get("tenant") != identity.tenant:
        raise NotFound(
            "The opportunity does not exist.",
            detail=f"opportunity_id={bounded_detail_value(opportunity_id)}",
        )
    require_record_scope(identity, opportunity, operation=_OPERATION)

    kind = arguments.get("kind")
    limit = int(arguments.get("limit") or _DEFAULT_LIMIT)
    partition_value = tenant_prefixed(identity.tenant, opportunity_id)
    binding = bound_operation(_OPERATION, _INDEX, partition_value)
    start_key = decode_cursor(arguments.get("cursor"), identity=identity, operation=binding)

    items, next_key = store.query_gsi(
        _TABLE,
        _INDEX,
        partition_value=partition_value,
        limit=limit,
        exclusive_start_key=start_key,
    )
    if kind is not None:
        items = [item for item in items if item.get("kind") == kind]
    items, cut = bound_page(items)
    if cut:
        next_key = resume_key(_TABLE, _INDEX, items[-1])
    items.reverse()

    result: dict[str, Any] = {
        "activities": items,
        "next_cursor": encode_cursor(next_key, identity=identity, operation=binding),
    }
    if cut:
        result["truncated"] = True
    return result


handler = build_handler(_list_activities)
