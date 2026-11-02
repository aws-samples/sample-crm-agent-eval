# SPDX-License-Identifier: MIT
"""``get_opportunity`` action Lambda. One opportunity with contacts, notes, and activities.

"Contacts" is the account's full contact list, not only the primary contact.
"""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, account_id_of, require_record_scope
from crm.errors import NotFound, bounded_detail_value
from crm.handlers._common import build_handler
from crm.store.keys import tenant_prefixed
from crm.store.store import Store

_TABLE = "opportunities"


def _get_opportunity(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    opportunity_id = str(arguments["opportunity_id"])
    opportunity = store.get_item(_TABLE, {"opportunity_id": opportunity_id})
    if opportunity is None or opportunity.get("tenant") != identity.tenant:
        raise NotFound(
            "The opportunity does not exist.",
            detail=f"opportunity_id={bounded_detail_value(opportunity_id)}",
        )
    require_record_scope(identity, opportunity, operation="get_opportunity")

    account_id = account_id_of(opportunity)
    contacts, _c1 = store.query_gsi(
        "contacts", "gsi_account", partition_value=tenant_prefixed(identity.tenant, account_id)
    )
    notes, _c2 = store.query_gsi(
        "notes",
        "gsi_opportunity",
        partition_value=tenant_prefixed(identity.tenant, opportunity_id),
    )
    activities, _c3 = store.query_gsi(
        "activities",
        "gsi_opportunity",
        partition_value=tenant_prefixed(identity.tenant, opportunity_id),
    )
    return {
        "opportunity": opportunity,
        "contacts": contacts,
        "notes": notes,
        "activities": activities,
    }


handler = build_handler(_get_opportunity)
