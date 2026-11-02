# SPDX-License-Identifier: MIT
"""``create_note`` action Lambda. Commits directly; low-risk."""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, account_id_of, require_record_scope
from crm.errors import NotFound, bounded_detail_value
from crm.handlers._common import build_handler, create_idempotent, idempotent_id, now_iso
from crm.store.keys import derive
from crm.store.store import Store

_TABLE = "notes"


def _create_note(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    opportunity_id = str(arguments["opportunity_id"])
    opportunity = store.get_item("opportunities", {"opportunity_id": opportunity_id})
    if opportunity is None or opportunity.get("tenant") != identity.tenant:
        raise NotFound(
            "The opportunity does not exist.",
            detail=f"opportunity_id={bounded_detail_value(opportunity_id)}",
        )
    require_record_scope(identity, opportunity, operation="create_note")

    note_id = idempotent_id("note", str(arguments["transaction_key"]))
    now = now_iso()
    item: dict[str, object] = {
        "note_id": note_id,
        "tenant": identity.tenant,
        "opportunity_id": opportunity_id,
        "body": str(arguments["body"]),
        "created_at": now,
        "created_by": identity.rep_id,
    }
    persisted = create_idempotent(
        store,
        _TABLE,
        "note_id",
        derive(_TABLE, item),
        transaction_key=str(arguments["transaction_key"]),
        identity=identity,
        audit_account_id=account_id_of(opportunity),
    )
    return {"note": persisted}


handler = build_handler(_create_note)
