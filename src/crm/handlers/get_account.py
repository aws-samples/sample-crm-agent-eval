# SPDX-License-Identifier: MIT
"""``get_account`` action Lambda. Retrieves one account with its full contact list.

Every contact under the account is returned regardless of ``status``.
"""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, require_scope
from crm.errors import NotFound, bounded_detail_value
from crm.handlers._common import build_handler
from crm.store.keys import tenant_prefixed
from crm.store.store import Store

_TABLE = "accounts"


def _get_account(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    account_id = str(arguments["account_id"])
    account = store.get_item(_TABLE, {"account_id": account_id})
    if account is None or account.get("tenant") != identity.tenant:
        raise NotFound(
            "The account does not exist.", detail=f"account_id={bounded_detail_value(account_id)}"
        )
    require_scope(identity, account_id, operation="get_account")

    contacts, _cursor = store.query_gsi(
        "contacts",
        "gsi_account",
        partition_value=tenant_prefixed(identity.tenant, account_id),
    )
    return {"account": account, "contacts": contacts}


handler = build_handler(_get_account)
