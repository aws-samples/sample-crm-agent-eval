# SPDX-License-Identifier: MIT
"""``approve_proposal`` action Lambda. Thin adapter over :mod:`crm.proposals`.

Only a person on the UI or MCP channel may approve; any other caller is refused before anything
is read (:func:`crm.authz.require_proposal_decider`). Scope is checked against the proposal's
recorded target with :func:`crm.authz.require_record_scope` before delegating to
:func:`crm.proposals.approve_proposal`. The audit record is written in the same transaction through
its ``record_audit`` callback, with its id derived from ``transaction_key``.
"""

from __future__ import annotations

from typing import Any

from crm.audit import build_audit_record
from crm.authz import Identity, require_proposal_decider, require_record_scope
from crm.handlers._common import build_handler, idempotent_id, now_iso
from crm.handlers.wire import put_operation
from crm.proposals import ApprovalContext, AuditRecorder, approve_proposal, proposal_not_found
from crm.store.store import Store

_SCOPE_LOOKUP_KEY: dict[str, str] = {
    "accounts": "account_id",
    "contacts": "contact_id",
    "opportunities": "opportunity_id",
    "activities": "activity_id",
}


def _record_audit(identity: Identity, transaction_key: str) -> AuditRecorder:
    def _callback(context: ApprovalContext) -> dict[str, Any]:
        audit = build_audit_record(
            identity=identity,
            target_table=context.target_table,
            target_id=context.target_id,
            before_snapshot=dict(context.before_snapshot),
            after_snapshot=dict(context.after_snapshot) if context.after_snapshot else {},
            audit_id=idempotent_id("audit", transaction_key),
        )
        operation = put_operation("audit_records", audit.to_item())
        operation["Put"]["ConditionExpression"] = "attribute_not_exists(audit_id)"
        return operation

    return _callback


def _approve_proposal(
    store: Store, identity: Identity, arguments: dict[str, Any]
) -> dict[str, Any]:
    require_proposal_decider(identity, operation="approve_proposal")
    proposal_id = str(arguments["proposal_id"])
    proposal = store.get_item("proposals", {"proposal_id": proposal_id})
    if proposal is None or proposal.get("tenant") != identity.tenant:
        raise proposal_not_found(proposal_id)

    target_table = str(proposal["target_table"])
    target_id = str(proposal["target_id"])
    key_attr = _SCOPE_LOOKUP_KEY[target_table]
    target = store.get_item(target_table, {key_attr: target_id})
    scope_source = target if target is not None else proposal.get("before_snapshot")
    require_record_scope(identity, scope_source, operation="approve_proposal")

    transaction_key = str(arguments["transaction_key"])
    updated = approve_proposal(
        store,
        tenant=identity.tenant,
        proposal_id=proposal_id,
        actor_rep_id=identity.rep_id,
        transaction_key=transaction_key,
        now=now_iso(),
        record_audit=_record_audit(identity, transaction_key),
    )
    return {"proposal": updated}


handler = build_handler(_approve_proposal)
