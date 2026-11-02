# SPDX-License-Identifier: MIT
"""``create_contact`` action Lambda. Commits directly; low-risk."""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, require_scope
from crm.errors import NotFound, bounded_detail_value
from crm.handlers._common import build_handler, create_idempotent, idempotent_id, now_iso
from crm.store.keys import derive
from crm.store.store import Store

_TABLE = "contacts"


def _create_contact(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    account_id = str(arguments["account_id"])
    account = store.get_item("accounts", {"account_id": account_id})
    if account is None or account.get("tenant") != identity.tenant:
        raise NotFound(
            "The account does not exist.", detail=f"account_id={bounded_detail_value(account_id)}"
        )
    require_scope(identity, account_id, operation="create_contact")

    contact_id = idempotent_id("cont", str(arguments["transaction_key"]))
    now = now_iso()
    name = str(arguments["name"])
    item: dict[str, object] = {
        "contact_id": contact_id,
        "tenant": identity.tenant,
        "account_id": account_id,
        "name": name,
        "title": arguments.get("title"),
        "email": arguments.get("email"),
        "phone": arguments.get("phone"),
        "status": arguments.get("status") or "active",
        "created_at": now,
        "updated_at": now,
        "created_by": identity.rep_id,
    }
    persisted = create_idempotent(
        store,
        _TABLE,
        "contact_id",
        derive(_TABLE, item),
        transaction_key=str(arguments["transaction_key"]),
        identity=identity,
    )
    return {"contact": persisted}


handler = build_handler(_create_contact)
