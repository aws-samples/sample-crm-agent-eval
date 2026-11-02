# SPDX-License-Identifier: MIT
"""``revert_audit_record`` action Lambda. Thin adapter over :func:`crm.audit.revert`, which
enforces scope itself.

Only a person on the UI or MCP channel may revert; any other caller is refused before anything is
read (:func:`crm.authz.require_proposal_decider`).
"""

from __future__ import annotations

from typing import Any

from crm.audit import revert
from crm.authz import Identity, require_proposal_decider
from crm.handlers._common import build_handler
from crm.store.store import Store


def _revert_audit_record(
    store: Store, identity: Identity, arguments: dict[str, Any]
) -> dict[str, Any]:
    require_proposal_decider(identity, operation="revert_audit_record")
    updated = revert(
        identity,
        store,
        audit_id=str(arguments["audit_id"]),
        transaction_key=str(arguments["transaction_key"]),
        reason=arguments.get("reason"),
    )
    return {"audit_record": updated}


handler = build_handler(_revert_audit_record)
