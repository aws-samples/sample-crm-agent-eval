# SPDX-License-Identifier: MIT
"""``reject_proposal`` action Lambda. Thin adapter over :mod:`crm.proposals`.

Only a person on the UI or MCP channel may reject; any other caller is refused before anything
is read (:func:`crm.authz.require_proposal_decider`). Scope is checked against the proposal's
recorded target before delegating.
"""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, require_proposal_decider, require_record_scope
from crm.handlers._common import build_handler, now_iso
from crm.proposals import proposal_not_found, reject_proposal
from crm.store.store import Store

_SCOPE_LOOKUP_KEY: dict[str, str] = {
    "accounts": "account_id",
    "contacts": "contact_id",
    "opportunities": "opportunity_id",
    "activities": "activity_id",
}


def _reject_proposal(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    require_proposal_decider(identity, operation="reject_proposal")
    proposal_id = str(arguments["proposal_id"])
    proposal = store.get_item("proposals", {"proposal_id": proposal_id})
    if proposal is None or proposal.get("tenant") != identity.tenant:
        raise proposal_not_found(proposal_id)

    target_table = str(proposal["target_table"])
    target_id = str(proposal["target_id"])
    key_attr = _SCOPE_LOOKUP_KEY[target_table]
    target = store.get_item(target_table, {key_attr: target_id})
    scope_source = target if target is not None else proposal.get("before_snapshot")
    require_record_scope(identity, scope_source, operation="reject_proposal")

    updated = reject_proposal(
        store,
        tenant=identity.tenant,
        proposal_id=proposal_id,
        actor_rep_id=identity.rep_id,
        transaction_key=str(arguments["transaction_key"]),
        now=now_iso(),
        reason=arguments.get("reason"),
    )
    return {"proposal": updated}


handler = build_handler(_reject_proposal)
