# SPDX-License-Identifier: MIT
"""``rebase_proposal`` action Lambda. Thin adapter over :func:`crm.proposals.rebase_proposal`.

Authorized exactly as ``approve_proposal``: :func:`crm.authz.require_record_scope` on the proposal's
target record, or on its stored before-snapshot when the record is gone. The ``proposal_rebased``
audit record commits in the same transaction, with its id derived from ``transaction_key``. Returns
``{"proposal": <rebased proposal>}``.
"""

from __future__ import annotations

from typing import Any

from crm.audit import PROPOSAL_REBASED, build_audit_record
from crm.authz import Identity, require_record_scope
from crm.handlers._common import build_handler, idempotent_id, now_iso
from crm.handlers.wire import put_operation
from crm.proposals import ApprovalContext, AuditRecorder, proposal_not_found, rebase_proposal
from crm.store.schema import table_schema
from crm.store.store import Store
from crm.toolspec.definitions import TABLES


def _record_audit(identity: Identity, transaction_key: str) -> AuditRecorder:
    def _callback(context: ApprovalContext) -> dict[str, Any]:
        audit = build_audit_record(
            identity=identity,
            target_table=context.target_table,
            target_id=context.target_id,
            before_snapshot=dict(context.before_snapshot),
            after_snapshot=dict(context.after_snapshot) if context.after_snapshot else {},
            action=PROPOSAL_REBASED,
            audit_id=idempotent_id("audit", transaction_key),
        )
        operation = put_operation("audit_records", audit.to_item())
        operation["Put"]["ConditionExpression"] = "attribute_not_exists(audit_id)"
        return operation

    return _callback


def _rebase_proposal(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    proposal_id = str(arguments["proposal_id"])
    proposal = store.get_item("proposals", {"proposal_id": proposal_id})
    if proposal is None or proposal.get("tenant") != identity.tenant:
        raise proposal_not_found(proposal_id)

    target_table = str(proposal["target_table"])
    target = (
        store.get_item(
            target_table,
            {table_schema(target_table).partition_key: str(proposal["target_id"])},
        )
        if target_table in TABLES
        else None
    )
    scope_source = target if target is not None else proposal.get("before_snapshot")
    require_record_scope(identity, scope_source, operation="rebase_proposal")

    transaction_key = str(arguments["transaction_key"])
    updated = rebase_proposal(
        store,
        tenant=identity.tenant,
        proposal_id=proposal_id,
        actor_rep_id=identity.rep_id,
        now=now_iso(),
        record_audit=_record_audit(identity, transaction_key),
        transaction_key=transaction_key,
    )
    return {"proposal": updated}


handler = build_handler(_rebase_proposal)
