# SPDX-License-Identifier: MIT
"""``update_activity`` action Lambda. Commits directly; low-risk. Enforces the activity
update allowlist before any write.
"""

from __future__ import annotations

from typing import Any

from crm.audit import build_audit_record
from crm.authz import Identity, require_record_scope
from crm.domain import validate_update_fields
from crm.errors import NotFound, bounded_detail_value
from crm.handlers._common import build_handler, now_iso
from crm.handlers.wire import put_operation
from crm.store.keys import derive
from crm.store.store import Store

_TABLE = "activities"


def _update_activity(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    activity_id = str(arguments["activity_id"])
    fields = dict(arguments["fields"])
    validate_update_fields(_TABLE, fields)

    before = store.get_item(_TABLE, {"activity_id": activity_id})
    if before is None or before.get("tenant") != identity.tenant:
        raise NotFound(
            "The activity does not exist.",
            detail=f"activity_id={bounded_detail_value(activity_id)}",
        )
    require_record_scope(identity, before, operation="update_activity")

    now = now_iso()
    after = derive(_TABLE, {**before, **fields, "updated_at": now})

    audit = build_audit_record(
        identity=identity,
        target_table=_TABLE,
        target_id=activity_id,
        before_snapshot=before,
        after_snapshot=after,
    )
    store.transact_write(
        [put_operation(_TABLE, after), put_operation("audit_records", audit.to_item())],
        transaction_key=str(arguments["transaction_key"]),
    )
    return {"activity": after}


handler = build_handler(_update_activity)
