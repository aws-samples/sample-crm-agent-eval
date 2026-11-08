# SPDX-License-Identifier: MIT
"""``submit_proposal`` action Lambda. Thin adapter over :mod:`crm.proposals`.

Before :func:`crm.proposals.submit_proposal` reads or writes anything, this handler checks scope,
applies the cascade checks in :mod:`crm.handlers.cascade_rules` to a delete, and refuses an
opportunity update proposing an impermissible stage transition or a ``primary_contact_id`` outside
the opportunity's account.
"""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, require_record_scope
from crm.domain.references import require_primary_contact_in_account
from crm.domain.stages import PERMITTED_TRANSITION_KINDS, classify_requested_transition
from crm.errors import NotFound, ValidationFailed, bounded_detail_value
from crm.handlers._common import build_handler, idempotent_id, now_iso
from crm.handlers.cascade_rules import (
    reject_account_delete_with_open_opportunities,
    reject_contact_delete_if_referenced,
)
from crm.proposals import submit_proposal
from crm.store.store import Store

_SCOPE_LOOKUP_KEY: dict[str, str] = {
    "accounts": "account_id",
    "contacts": "contact_id",
    "opportunities": "opportunity_id",
    "activities": "activity_id",
}


def _current_target(
    store: Store, identity: Identity, target_table: str, target_id: str
) -> dict[str, object]:
    key_attr = _SCOPE_LOOKUP_KEY[target_table]
    target = store.get_item(target_table, {key_attr: target_id})
    if target is None or target.get("tenant") != identity.tenant:
        raise NotFound(
            "The target record does not exist.",
            detail=f"target_table={target_table} target_id={bounded_detail_value(target_id)}",
        )
    return target


def _submit_proposal(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    target_table = str(arguments["target_table"])
    target_id = str(arguments["target_id"])
    operation = str(arguments["operation"])
    after_payload = arguments.get("after_payload")

    current = _current_target(store, identity, target_table, target_id)
    require_record_scope(identity, current, operation="submit_proposal")

    if operation == "delete":
        if target_table == "accounts":
            reject_account_delete_with_open_opportunities(
                store, tenant=identity.tenant, account_id=target_id
            )
        elif target_table == "contacts":
            reject_contact_delete_if_referenced(store, tenant=identity.tenant, contact_id=target_id)

    if (
        operation == "update"
        and target_table == "opportunities"
        and isinstance(after_payload, dict)
        and "primary_contact_id" in after_payload
    ):
        require_primary_contact_in_account(
            store,
            identity,
            str(current.get("account_id")),
            after_payload["primary_contact_id"],
            source="after_payload.primary_contact_id",
        )

    if (
        operation == "update"
        and target_table == "opportunities"
        and isinstance(after_payload, dict)
        and "stage" in after_payload
    ):
        from_stage = str(current.get("stage"))
        to_stage = str(after_payload["stage"])
        kind = classify_requested_transition(from_stage, to_stage, source="after_payload.stage")
        if kind not in PERMITTED_TRANSITION_KINDS:
            raise ValidationFailed(
                f"Stage transition from {from_stage!r} to {to_stage!r} is not permitted.",
                source="after_payload.stage",
                detail=f"transition_kind={kind.value}",
            )

    proposal = submit_proposal(
        store,
        tenant=identity.tenant,
        proposal_id=idempotent_id("prop", str(arguments["transaction_key"])),
        target_table=target_table,
        target_id=target_id,
        operation=operation,
        actor_rep_id=identity.rep_id,
        reason=str(arguments["reason"]),
        transaction_key=str(arguments["transaction_key"]),
        after_payload=after_payload if isinstance(after_payload, dict) else None,
        now=now_iso(),
    )
    return {"proposal": proposal}


handler = build_handler(_submit_proposal)
