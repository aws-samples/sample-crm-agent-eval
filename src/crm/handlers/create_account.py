# SPDX-License-Identifier: MIT
"""``create_account`` action Lambda. Commits directly; low-risk."""

from __future__ import annotations

from typing import Any

from crm.authz import Identity
from crm.handlers._common import build_handler, create_idempotent, idempotent_id, now_iso
from crm.store.keys import derive
from crm.store.store import Store

_TABLE = "accounts"


def _create_account(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    transaction_key = str(arguments["transaction_key"])
    account_id = idempotent_id("acct", transaction_key)
    now = now_iso()
    name = str(arguments["name"])
    item: dict[str, object] = {
        "account_id": account_id,
        "tenant": identity.tenant,
        "owner_rep_id": identity.rep_id,
        "name": name,
        "industry": arguments.get("industry"),
        "region": arguments.get("region"),
        "website": arguments.get("website"),
        "employee_count": arguments.get("employee_count"),
        "created_at": now,
        "updated_at": now,
        "created_by": identity.rep_id,
    }
    persisted = create_idempotent(
        store,
        _TABLE,
        "account_id",
        derive(_TABLE, item),
        transaction_key=transaction_key,
        identity=identity,
    )
    return {"account": persisted}


handler = build_handler(_create_account)
