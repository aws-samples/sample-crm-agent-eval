# SPDX-License-Identifier: MIT
"""``get_contact`` action Lambda. Retrieves one contact, active or departed."""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, require_record_scope
from crm.errors import NotFound, bounded_detail_value
from crm.handlers._common import build_handler
from crm.store.store import Store

_TABLE = "contacts"


def _get_contact(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    contact_id = str(arguments["contact_id"])
    contact = store.get_item(_TABLE, {"contact_id": contact_id})
    if contact is None or contact.get("tenant") != identity.tenant:
        raise NotFound(
            "The contact does not exist.", detail=f"contact_id={bounded_detail_value(contact_id)}"
        )
    require_record_scope(identity, contact, operation="get_contact")
    return {"contact": contact}


handler = build_handler(_get_contact)
