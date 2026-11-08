# SPDX-License-Identifier: MIT
"""``list_proposals`` action Lambda. Proposals awaiting a decision within the caller's book.

A proposal's scope comes from its ``before_snapshot``, decided by :func:`crm.authz.is_in_scope`. A
proposal whose snapshot is not a mapping, has no determinable scope, or names another tenant is
withheld. A caller with an empty book and no full visibility is denied before any read;
``next_cursor`` is bound to the caller and the ``status`` filter it was issued for
(:mod:`crm.cursor`).
"""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, is_in_scope, require_listing_scope
from crm.cursor import bound_operation, decode_cursor, encode_cursor
from crm.handlers._common import build_handler
from crm.store.keys import tenant_prefixed
from crm.store.store import Store

_DEFAULT_LIMIT = 50
_OPERATION = "list_proposals"


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
    require_listing_scope(identity, operation=_OPERATION)
    limit = _DEFAULT_LIMIT
    if status is not None:
        index_name, partition_value = "gsi_status", tenant_prefixed(identity.tenant, str(status))
    else:
        index_name, partition_value = "gsi_tenant", identity.tenant
    binding = bound_operation(_OPERATION, index_name, partition_value)
    start_key = decode_cursor(arguments.get("cursor"), identity=identity, operation=binding)

    if status is not None:
        items, next_key = store.query_gsi(
            "proposals",
            index_name,
            partition_value=partition_value,
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
        "next_cursor": encode_cursor(next_key, identity=identity, operation=binding),
    }


handler = build_handler(_list_proposals)
