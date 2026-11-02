# SPDX-License-Identifier: MIT
"""``update_contact`` action Lambda. Commits directly; low-risk. Enforces the contact
update allowlist before any write. Marking a contact ``departed`` is an ordinary status update; any
other update to a departed contact is refused with ``contact_departed`` unless it moves ``status`` off
``departed``.
"""

from __future__ import annotations

from typing import Any

from crm.audit import build_audit_record
from crm.authz import Identity, require_record_scope
from crm.domain import validate_update_fields
from crm.errors import ContactDeparted, NotFound, bounded_detail_value
from crm.handlers._common import build_handler, now_iso
from crm.handlers.wire import put_operation
from crm.store.keys import derive
from crm.store.store import Store

_TABLE = "contacts"


def _update_contact(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    contact_id = str(arguments["contact_id"])
    fields = dict(arguments["fields"])
    validate_update_fields(_TABLE, fields)

    before = store.get_item(_TABLE, {"contact_id": contact_id})
    if before is None or before.get("tenant") != identity.tenant:
        raise NotFound(
            "The contact does not exist.", detail=f"contact_id={bounded_detail_value(contact_id)}"
        )
    require_record_scope(identity, before, operation="update_contact")
    if before.get("status") == "departed" and fields.get("status", "departed") == "departed":
        raise ContactDeparted(detail=f"contact_id={bounded_detail_value(contact_id)}")

    now = now_iso()
    after = derive(_TABLE, {**before, **fields, "updated_at": now})

    audit = build_audit_record(
        identity=identity,
        target_table=_TABLE,
        target_id=contact_id,
        before_snapshot=before,
        after_snapshot=after,
    )
    store.transact_write(
        [put_operation(_TABLE, after), put_operation("audit_records", audit.to_item())],
        transaction_key=str(arguments["transaction_key"]),
    )
    return {"contact": after}


handler = build_handler(_update_contact)
