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
from crm.errors import NotFound, bounded_detail_value
from crm.handlers._common import build_handler, now_iso
from crm.handlers.wire import put_operation
from crm.store.keys import derive
from crm.store.store import Store

_TABLE = "accounts"


def _update_account(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    account_id = str(arguments["account_id"])
    fields = dict(arguments["fields"])
    validate_update_fields(_TABLE, fields)  # raises before any read or write on a rejected field

    before = store.get_item(_TABLE, {"account_id": account_id})
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
    store.transact_write(
        [put_operation(_TABLE, after), put_operation("audit_records", audit.to_item())],
        transaction_key=str(arguments["transaction_key"]),
    )
    return {"account": after}


handler = build_handler(_update_account)
