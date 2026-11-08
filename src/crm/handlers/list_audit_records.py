# SPDX-License-Identifier: MIT
"""``list_audit_records`` action Lambda. Audited changes within the caller's book, most recent
first, with revert state.

Both indexes are read newest-first by ``created_at``, so each page, and each page a cursor
continues to, is the next run of most recent records. Scope is enforced by
:func:`crm.authz.filter_to_scope` on each record's top-level ``account_id``; a record with no usable
``account_id`` is withheld. Both ``audit_records`` indexes this handler
queries project ``target_id``, ``target_table`` and ``status``. A caller with an empty book and no
full visibility is denied before any read; ``next_cursor`` is bound to the caller and the
``target_id`` it was issued for (:mod:`crm.cursor`).
"""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, filter_to_scope, require_listing_scope
from crm.cursor import bound_operation, decode_cursor, encode_cursor
from crm.errors import ValidationFailed
from crm.handlers._common import build_handler
from crm.store.keys import tenant_prefixed
from crm.store.store import Store

_DEFAULT_LIMIT = 50
_OPERATION = "list_audit_records"


def _list_audit_records(
    store: Store, identity: Identity, arguments: dict[str, Any]
) -> dict[str, Any]:
    target_id = arguments.get("target_id")
    require_listing_scope(identity, operation=_OPERATION)
    limit = _DEFAULT_LIMIT
    if target_id == "":
        raise ValidationFailed("target_id must not be empty.", source="target_id")
    if target_id is not None:
        index_name, partition_value = "gsi_target", tenant_prefixed(identity.tenant, str(target_id))
    else:
        index_name, partition_value = "gsi_tenant", identity.tenant
    binding = bound_operation(_OPERATION, index_name, partition_value)
    start_key = decode_cursor(arguments.get("cursor"), identity=identity, operation=binding)

    if target_id is not None:
        items, next_key = store.query_gsi(
            "audit_records",
            index_name,
            partition_value=partition_value,
            limit=limit,
            exclusive_start_key=start_key,
            scan_forward=False,
        )
    else:
        items, next_key = store.query_tenant_scan(
            "audit_records",
            tenant=identity.tenant,
            limit=limit,
            exclusive_start_key=start_key,
            scan_forward=False,
        )

    visible = filter_to_scope(identity, items)

    return {
        "audit_records": visible,
        "next_cursor": encode_cursor(next_key, identity=identity, operation=binding),
    }


handler = build_handler(_list_audit_records)
