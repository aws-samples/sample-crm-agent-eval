# SPDX-License-Identifier: MIT
"""``list_proposals`` action Lambda. Proposals awaiting a decision within the caller's book.

A proposal's scope comes from its ``before_snapshot``, decided by :func:`crm.authz.is_in_scope`. A
proposal whose snapshot is not a mapping, has no determinable scope, or names another tenant is
withheld.
"""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, is_in_scope
from crm.handlers._common import build_handler
from crm.search import decode_cursor, encode_cursor
from crm.store.keys import tenant_prefixed
from crm.store.store import Store

_DEFAULT_LIMIT = 50


def _visible(identity: Identity, proposal: dict[str, object]) -> bool:
    """Return whether ``identity`` may see ``proposal``, deciding on its before-snapshot's scope."""
    if proposal.get("tenant") != identity.tenant:
        return False
    before = proposal.get("before_snapshot")
    if not isinstance(before, dict):
        return False
    return is_in_scope(identity, before)


def _list_proposals(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    status = arguments.get("status")
    limit = _DEFAULT_LIMIT
    cursor_token = arguments.get("cursor")
    start_key = decode_cursor(cursor_token) if cursor_token else None

    if status is not None:
        items, next_key = store.query_gsi(
            "proposals",
            "gsi_status",
            partition_value=tenant_prefixed(identity.tenant, str(status)),
            limit=limit,
            exclusive_start_key=start_key,
        )
    else:
        items, next_key = store.query_tenant_scan(
            "proposals", tenant=identity.tenant, limit=limit, exclusive_start_key=start_key
        )

    # Re-read the base items: neither `gsi_status` nor `gsi_tenant` projects `before_snapshot`,
    # which `_visible` decides on. Order follows the index query; the batch response is unordered.
    schema_key = "proposal_id"
    ordered_ids = [item[schema_key] for item in items if isinstance(item.get(schema_key), str)]
    full_by_id = {
        str(item[schema_key]): item
        for item in store.batch_get_items(
            "proposals", [{schema_key: str(proposal_id)} for proposal_id in ordered_ids]
        )
        if isinstance(item.get(schema_key), str)
    }
    visible = [
        full
        for proposal_id in ordered_ids
        if (full := full_by_id.get(str(proposal_id))) is not None and _visible(identity, full)
    ]

    return {
        "proposals": visible,
        "next_cursor": encode_cursor(next_key) if next_key is not None else None,
    }


handler = build_handler(_list_proposals)
