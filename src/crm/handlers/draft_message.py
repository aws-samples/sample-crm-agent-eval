# SPDX-License-Identifier: MIT
"""``draft_message`` action Lambda. Drafts a message for a contact the caller may see.

Read-only: persists nothing.
"""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, require_record_scope
from crm.errors import NotFound, bounded_detail_value
from crm.handlers._common import build_handler
from crm.store.store import Store

_DEFAULT_TONE = "neutral"


def _draft_message(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    contact_id = str(arguments["contact_id"])
    contact = store.get_item("contacts", {"contact_id": contact_id})
    if contact is None or contact.get("tenant") != identity.tenant:
        raise NotFound(
            "The contact does not exist.", detail=f"contact_id={bounded_detail_value(contact_id)}"
        )
    require_record_scope(identity, contact, operation="draft_message")

    opportunity_id = arguments.get("opportunity_id")
    if opportunity_id is not None:
        opportunity = store.get_item("opportunities", {"opportunity_id": str(opportunity_id)})
        if opportunity is None or opportunity.get("tenant") != identity.tenant:
            raise NotFound(
                "The opportunity does not exist.",
                detail=f"opportunity_id={bounded_detail_value(opportunity_id)}",
            )
        require_record_scope(identity, opportunity, operation="draft_message")

    intent = str(arguments["intent"])
    tone = str(arguments.get("tone") or _DEFAULT_TONE)
    contact_name = str(contact.get("name", "there"))

    greeting = {"formal": f"Dear {contact_name},", "warm": f"Hi {contact_name},"}.get(
        tone, f"Hello {contact_name},"
    )
    draft = f"{greeting}\n\n{intent}\n\nBest regards"

    return {"draft": draft, "contact_id": contact_id, "tone": tone}


handler = build_handler(_draft_message)
