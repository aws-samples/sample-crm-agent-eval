# SPDX-License-Identifier: MIT
"""``create_opportunity`` action Lambda. Commits directly; low-risk.

``stage``, ``value`` and ``primary_contact_id`` are accepted at creation without a proposal; only
later changes to them are high-risk. Index key attributes are computed by
:func:`crm.store.keys.derive`, so an opportunity with no ``close_date`` also has no
``stage_close_date``.
"""

from __future__ import annotations

from typing import Any

from crm.authz import Identity, require_scope
from crm.domain.references import require_primary_contact_in_account
from crm.domain.stages import STAGES
from crm.errors import NotFound, ValidationFailed, bounded_detail_value
from crm.handlers._common import build_handler, create_idempotent, idempotent_id, now_iso
from crm.store.keys import derive
from crm.store.store import Store

_TABLE = "opportunities"
_DEFAULT_STAGE = STAGES[0]


def _create_opportunity(
    store: Store, identity: Identity, arguments: dict[str, Any]
) -> dict[str, Any]:
    account_id = str(arguments["account_id"])
    account = store.get_item("accounts", {"account_id": account_id})
    if account is None or account.get("tenant") != identity.tenant:
        raise NotFound(
            "The account does not exist.", detail=f"account_id={bounded_detail_value(account_id)}"
        )
    require_scope(identity, account_id, operation="create_opportunity")

    stage = str(arguments.get("stage") or _DEFAULT_STAGE)
    if stage not in STAGES:
        raise ValidationFailed(
            f"Unrecognised stage; expected one of {list(STAGES)}.",
            source="stage",
            detail=f"stage={bounded_detail_value(stage)}",
        )

    primary_contact_id = arguments.get("primary_contact_id")
    require_primary_contact_in_account(store, identity, account_id, primary_contact_id)

    opportunity_id = idempotent_id("opp", str(arguments["transaction_key"]))
    now = now_iso()
    close_date = arguments.get("close_date")
    close_date_str = str(close_date) if close_date else None
    name = str(arguments["name"])

    item: dict[str, object] = {
        "opportunity_id": opportunity_id,
        "tenant": identity.tenant,
        "account_id": account_id,
        "owner_rep_id": identity.rep_id,
        "name": name,
        "stage": stage,
        "value": arguments.get("value"),
        "primary_contact_id": primary_contact_id,
        "description": arguments.get("description"),
        "last_touch": None,
        "created_at": now,
        "updated_at": now,
        "created_by": identity.rep_id,
    }
    if close_date_str:
        item["close_date"] = close_date_str
    persisted = create_idempotent(
        store,
        _TABLE,
        "opportunity_id",
        derive(_TABLE, item),
        transaction_key=str(arguments["transaction_key"]),
        identity=identity,
    )
    return {"opportunity": persisted}


handler = build_handler(_create_opportunity)
