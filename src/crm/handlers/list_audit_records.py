# SPDX-License-Identifier: MIT
"""``list_audit_records`` action Lambda. Audited changes within the caller's book, most recent
first, with revert state.

Both indexes are read newest-first by ``created_at``, so each page, and each page a cursor
continues to, is the next run of most recent records. Scope is enforced by
:func:`crm.authz.filter_to_scope` on each record's top-level ``account_id``; a record with no usable
``account_id`` is withheld. Both ``audit_records`` indexes this handler
queries project ``target_id``, ``target_table`` and ``status``.
"""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, filter_to_scope
from crm.errors import ValidationFailed
from crm.handlers._common import build_handler
from crm.search import decode_cursor, encode_cursor
from crm.store.keys import tenant_prefixed
from crm.store.store import Store

_DEFAULT_LIMIT = 50


def _list_audit_records(
    store: Store, identity: Identity, arguments: dict[str, Any]
) -> dict[str, Any]:
    target_id = arguments.get("target_id")
    limit = _DEFAULT_LIMIT
    cursor_token = arguments.get("cursor")
    start_key = decode_cursor(cursor_token) if cursor_token else None

    if target_id == "":
        raise ValidationFailed("target_id must not be empty.", source="target_id")
    if target_id is not None:
        items, next_key = store.query_gsi(
            "audit_records",
            "gsi_target",
            partition_value=tenant_prefixed(identity.tenant, str(target_id)),
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
        "next_cursor": encode_cursor(next_key) if next_key is not None else None,
    }


handler = build_handler(_list_audit_records)
