# SPDX-License-Identifier: MIT
"""``list_activities`` action Lambda. Activities attached to one opportunity, most recent first,
optionally filtered by kind, with cursor pagination.
"""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, require_record_scope
from crm.errors import NotFound, bounded_detail_value
from crm.handlers._common import build_handler
from crm.search import decode_cursor, encode_cursor
from crm.store.keys import tenant_prefixed
from crm.store.store import Store

_DEFAULT_LIMIT = 50


def _list_activities(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    opportunity_id = str(arguments["opportunity_id"])
    opportunity = store.get_item("opportunities", {"opportunity_id": opportunity_id})
    if opportunity is None or opportunity.get("tenant") != identity.tenant:
        raise NotFound(
            "The opportunity does not exist.",
            detail=f"opportunity_id={bounded_detail_value(opportunity_id)}",
        )
    require_record_scope(identity, opportunity, operation="list_activities")

    kind = arguments.get("kind")
    limit = int(arguments.get("limit") or _DEFAULT_LIMIT)
    cursor_token = arguments.get("cursor")
    start_key = decode_cursor(cursor_token) if cursor_token else None

    items, next_key = store.query_gsi(
        "activities",
        "gsi_opportunity",
        partition_value=tenant_prefixed(identity.tenant, opportunity_id),
        limit=limit,
        exclusive_start_key=start_key,
    )
    if kind is not None:
        items = [item for item in items if item.get("kind") == kind]
    items = list(reversed(items))

    return {
        "activities": items,
        "next_cursor": encode_cursor(next_key) if next_key is not None else None,
    }


handler = build_handler(_list_activities)
