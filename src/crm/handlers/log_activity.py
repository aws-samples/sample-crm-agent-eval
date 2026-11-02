# SPDX-License-Identifier: MIT
"""``log_activity`` action Lambda. Commits directly; low-risk.

Inserts the activity and ``SET``s only the parent opportunity's ``last_touch`` and ``updated_at``,
with a ``create`` audit record for the activity and an ``update`` audit record for the
opportunity, in one ``TransactWriteItems`` call. The opportunity write is conditioned on it existing
under the caller's tenant and being unchanged since it was read, so the update record's
``before_snapshot`` is exact and reverting both records undoes the call. The activity id is derived
from ``transaction_key`` and inserted only if absent, so a retry never writes a second activity. Scope is checked by :func:`crm.authz.require_record_scope`, the same
check ``get_opportunity`` uses.
"""

from __future__ import annotations

from typing import Any

from crm.audit import build_audit_record
from crm.authz import Identity, account_id_of, require_record_scope
from crm.errors import NotFound, bounded_detail_value
from crm.handlers._common import build_handler, idempotent_id, now_iso
from crm.handlers.wire import put_operation
from crm.store.keys import derive
from crm.store.store import Store

_ACTIVITY_TABLE = "activities"
_OPPORTUNITY_TABLE = "opportunities"


def _log_activity(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    opportunity_id = str(arguments["opportunity_id"])
    opportunity = store.get_item(
        _OPPORTUNITY_TABLE, {"opportunity_id": opportunity_id}, consistent=True
    )
    if opportunity is None or opportunity.get("tenant") != identity.tenant:
        raise NotFound(
            "The opportunity does not exist.",
            detail=f"opportunity_id={bounded_detail_value(opportunity_id)}",
        )
    require_record_scope(identity, opportunity, operation="log_activity")

    now = now_iso()
    activity_id = idempotent_id("act", str(arguments["transaction_key"]))
    occurred_at = str(arguments.get("occurred_at") or now)
    account_id = account_id_of(opportunity)

    activity: dict[str, object] = derive(
        _ACTIVITY_TABLE,
        {
            "activity_id": activity_id,
            "tenant": identity.tenant,
            "opportunity_id": opportunity_id,
            "account_id": account_id,
            "kind": str(arguments["kind"]),
            "subject": str(arguments["subject"]),
            "body": arguments.get("body"),
            "occurred_at": occurred_at,
            "created_at": now,
            "created_by": identity.rep_id,
        },
    )

    insert_activity = put_operation(_ACTIVITY_TABLE, activity)
    insert_activity["Put"]["ConditionExpression"] = "attribute_not_exists(activity_id)"
    touch_opportunity: dict[str, Any] = {
        "Update": {
            "TableName": _OPPORTUNITY_TABLE,
            "Key": {"opportunity_id": {"S": opportunity_id}},
            "UpdateExpression": "SET last_touch = :last_touch, updated_at = :now",
            "ConditionExpression": (
                "attribute_exists(opportunity_id) AND tenant = :tenant AND "
                + _unchanged_since_read(opportunity)
            ),
            "ExpressionAttributeValues": {
                ":last_touch": {"S": occurred_at},
                ":now": {"S": now},
                ":tenant": {"S": identity.tenant},
                **_unchanged_since_read_values(opportunity),
            },
        }
    }
    touched = {**opportunity, "last_touch": occurred_at, "updated_at": now}
    activity_audit = build_audit_record(
        identity=identity,
        target_table=_ACTIVITY_TABLE,
        target_id=activity_id,
        before_snapshot={},
        after_snapshot=activity,
        action="create",
    )
    opportunity_audit = build_audit_record(
        identity=identity,
        target_table=_OPPORTUNITY_TABLE,
        target_id=opportunity_id,
        before_snapshot=opportunity,
        after_snapshot=touched,
        action="update",
    )
    store.transact_write(
        [
            insert_activity,
            touch_opportunity,
            put_operation("audit_records", activity_audit.to_item()),
            put_operation("audit_records", opportunity_audit.to_item()),
        ],
        transaction_key=str(arguments["transaction_key"]),
    )
    return {"activity": activity, "opportunity": touched}


def _unchanged_since_read(opportunity: dict[str, Any]) -> str:
    """The condition that the opportunity's ``updated_at`` is still the value that was read."""
    if isinstance(opportunity.get("updated_at"), str):
        return "updated_at = :read_updated_at"
    return "(attribute_not_exists(updated_at) OR attribute_type(updated_at, :null_type))"


def _unchanged_since_read_values(opportunity: dict[str, Any]) -> dict[str, Any]:
    """The expression values :func:`_unchanged_since_read` refers to."""
    read = opportunity.get("updated_at")
    if isinstance(read, str):
        return {":read_updated_at": {"S": read}}
    return {":null_type": {"S": "NULL"}}


handler = build_handler(_log_activity)
