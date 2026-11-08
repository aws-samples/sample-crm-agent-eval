# SPDX-License-Identifier: MIT
"""``update_account`` action Lambda. Commits directly; low-risk.

A field outside ``allowlist_for("accounts")`` is rejected by
:func:`crm.domain.validate_update_fields` before any store call.
"""

from __future__ import annotations

from typing import Any

from crm.audit import build_audit_record
from crm.authz import Identity, require_scope
from crm.domain import validate_update_fields
from crm.errors import Conflict, NotFound, bounded_detail_value
from crm.handlers._common import build_handler, now_iso
from crm.handlers.wire import (
    put_operation,
    raise_if_record_changed,
    unchanged_since_read,
    unchanged_since_read_values,
)
from crm.store.keys import derive
from crm.store.store import Store

_TABLE = "accounts"


def _update_account(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    account_id = str(arguments["account_id"])
    fields = dict(arguments["fields"])
    validate_update_fields(_TABLE, fields)  # raises before any read or write on a rejected field

    before = store.get_item(_TABLE, {"account_id": account_id}, consistent=True)
    if before is None or before.get("tenant") != identity.tenant:
        raise NotFound(
            "The account does not exist.", detail=f"account_id={bounded_detail_value(account_id)}"
        )
    require_scope(identity, account_id, operation="update_account")

    now = now_iso()
    after = derive(_TABLE, {**before, **fields, "updated_at": now})

    audit = build_audit_record(
        identity=identity,
        target_table=_TABLE,
        target_id=account_id,
        before_snapshot=before,
        after_snapshot=after,
    )
    record_put = put_operation(
        _TABLE,
        after,
        condition=f"attribute_exists(account_id) AND {unchanged_since_read(before)}",
        values=unchanged_since_read_values(before),
    )
    try:
        store.transact_write(
            [record_put, put_operation("audit_records", audit.to_item())],
            transaction_key=str(arguments["transaction_key"]),
        )
    except Conflict as exc:
        raise_if_record_changed(exc, table=_TABLE, record_id=account_id)
        raise
    return {"account": after}


handler = build_handler(_update_account)
