# SPDX-License-Identifier: MIT
"""Audit-record creation and idempotent revert.

:func:`build_audit_record` builds the before/after record for one committed write; callers include
its ``Put`` in the same ``TransactWriteItems`` call as the write. :func:`get_audit_record` applies the
same record-scope predicate as the list and adds the derived ``action``. :func:`revert`
restores a before-snapshot and raises :class:`crm.errors.Conflict` when the record changed since, the
audit record was already reverted, it records a proposal rebase, it carries no snapshots, or it
records a delete whose restored record's parent no longer exists. Transaction items name logical
tables; :meth:`crm.store.store.Store.transact_write` applies the table prefix.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast, get_args

from crm.authz import Identity, require_record_scope, require_scope, require_tenant
from crm.errors import Conflict, CrmError, NotFound, ScopeDenied, ValidationFailed
from crm.store.keys import derive, tenant_prefixed
from crm.store.store import Store

#: Audit-record lifecycle status. ``active`` until reverted, then terminally ``reversed``.
AuditStatus = str

_STATUS_ACTIVE: Final[AuditStatus] = "active"
_STATUS_REVERSED: Final[AuditStatus] = "reversed"

#: Longest revert justification :func:`revert` will persist.
_MAX_REASON_LENGTH: Final[int] = 1024

_TABLE: Final[str] = "audit_records"

#: What an audit record records. ``proposal_rebased`` is only ever stored explicitly; the
#: other three are derived from the snapshots when no ``action`` is stored.
AuditAction = Literal["create", "update", "delete", "proposal_rebased"]

_EXPLICIT_ACTIONS: Final[frozenset[str]] = frozenset(get_args(AuditAction))

#: The one action :func:`revert` refuses outright.
PROPOSAL_REBASED: Final[AuditAction] = "proposal_rebased"


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One committed write's before/after snapshots, actor, and timestamp.

    Attributes:
        audit_id: Unique identifier for this audit entry.
        tenant: Isolation tenant the audited record belongs to.
        target_table: Table the audited write applied to.
        target_id: Primary key value of the audited record within ``target_table``.
        before_snapshot: The record's full state immediately before the write.
        after_snapshot: The record's full state immediately after the write.
        actor_rep_id: The rep identifier that performed the write.
        created_at: ISO-8601 UTC timestamp the audit entry was written.
        status: ``"active"`` until reverted, then ``"reversed"``.
        reversed_at: ISO-8601 UTC timestamp of a successful revert, or ``None`` while ``active``.
        reversed_by: The rep identifier that performed the revert, or ``None`` while ``active``.
        reversed_reason: The justification recorded by :func:`revert`, or ``None``.
        account_id: The account either snapshot is scoped by, or ``None``. Stored top-level so
            :func:`crm.authz.filter_to_scope` can filter audit records directly.
        action: An explicit :data:`AuditAction`, or ``None`` to have :func:`audit_action` derive it
            from the snapshots. Omitted from the stored item when ``None``.
    """

    audit_id: str
    tenant: str
    target_table: str
    target_id: str
    before_snapshot: dict[str, object]
    after_snapshot: dict[str, object]
    actor_rep_id: str
    created_at: str
    status: AuditStatus = _STATUS_ACTIVE
    reversed_at: str | None = None
    reversed_by: str | None = None
    reversed_reason: str | None = None
    account_id: str | None = None
    action: AuditAction | None = None

    def to_item(self) -> dict[str, object]:
        """Render this record as a plain item, with index keys from :func:`crm.store.keys.derive`."""
        explicit_action: dict[str, object] = {} if self.action is None else {"action": self.action}
        return derive(
            _TABLE,
            {
                "audit_id": self.audit_id,
                "tenant": self.tenant,
                "target_table": self.target_table,
                "target_id": self.target_id,
                "before_snapshot": self.before_snapshot,
                "after_snapshot": self.after_snapshot,
                "actor_rep_id": self.actor_rep_id,
                "created_at": self.created_at,
                "status": self.status,
                "reversed_at": self.reversed_at,
                "reversed_by": self.reversed_by,
                "reversed_reason": self.reversed_reason,
                "account_id": self.account_id,
                **explicit_action,
            },
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _account_scope_of(
    before_snapshot: dict[str, object], after_snapshot: dict[str, object]
) -> str | None:
    """Return the account either snapshot is scoped by, if either carries one.

    Returns ``None`` when neither carries one (a note, scoped through its opportunity).
    """
    for snapshot in (after_snapshot, before_snapshot):
        for key in ("account_id", "accountId"):
            value = snapshot.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def build_audit_record(
    *,
    identity: Identity,
    target_table: str,
    target_id: str,
    before_snapshot: dict[str, object],
    after_snapshot: dict[str, object],
    audit_id: str | None = None,
    created_at: str | None = None,
    action: AuditAction | None = None,
    account_id: str | None = None,
) -> AuditRecord:
    """Construct, without writing, the audit record for one committed write.

    The caller includes :meth:`AuditRecord.to_item` as a ``Put`` in the same ``TransactWriteItems``
    call that commits the mutation.

    Args:
        identity: The caller performing the write; ``identity.rep_id`` becomes ``actor_rep_id`` and
            ``identity.tenant`` becomes the record's tenant.
        target_table: Table the write applies to.
        target_id: Primary key value of the written record.
        before_snapshot: The record's full state immediately before the write. An empty ``dict``
            for a create, where there was no prior state.
        after_snapshot: The record's full state immediately after the write.
        audit_id: Explicit identifier, for deterministic tests. Defaults to a fresh UUID4.
        created_at: Explicit ISO-8601 timestamp, for deterministic tests. Defaults to now (UTC).
        action: Explicit :data:`AuditAction`; ``None`` leaves it to be derived on read.
        account_id: The account the target is scoped by, for a record whose snapshots carry no
            ``account_id`` (a note is scoped through its opportunity). ``None`` derives it from the
            snapshots.

    Returns:
        The constructed, not-yet-persisted :class:`AuditRecord`.
    """
    return AuditRecord(
        audit_id=audit_id if audit_id is not None else str(uuid.uuid4()),
        tenant=identity.tenant,
        target_table=target_table,
        target_id=target_id,
        before_snapshot=dict(before_snapshot),
        after_snapshot=dict(after_snapshot),
        actor_rep_id=identity.rep_id,
        created_at=created_at if created_at is not None else _now(),
        account_id=(
            account_id
            if account_id is not None
            else _account_scope_of(before_snapshot, after_snapshot)
        ),
        action=action,
    )


def audit_action(item: dict[str, object]) -> AuditAction:
    """Return what an audit record records: its stored ``action``, else one derived from snapshots.

    Derivation: ``create`` when ``before_snapshot`` is empty, ``delete`` when ``after_snapshot`` is
    empty, otherwise ``update``. A stored ``None`` counts as absent.

    Raises:
        CrmError: ``internal_error`` when the stored ``action`` is present but not an
            :data:`AuditAction` (a corrupted record).
    """
    stored = item.get("action")
    if stored is not None:
        if isinstance(stored, str) and stored in _EXPLICIT_ACTIONS:
            return cast(AuditAction, stored)
        raise CrmError(
            "The audit record is malformed.",
            detail=f"audit_id={item.get('audit_id')!r} unrecognised action={stored!r}",
        )
    if not item.get("before_snapshot"):
        return "create"
    if not item.get("after_snapshot"):
        return "delete"
    return "update"


def _load_audit_record(store: Store, audit_id: str) -> dict[str, object]:
    item = store.get_item(_TABLE, {"audit_id": audit_id})
    if item is None:
        raise NotFound("No such audit record.")
    return item


def _load_in_tenant(
    identity: Identity, store: Store, audit_id: str, *, operation: str
) -> dict[str, object]:
    item = _load_audit_record(store, audit_id)
    require_tenant(identity, str(item["tenant"]), operation=operation)
    return item


def get_audit_record(identity: Identity, store: Store, audit_id: str) -> dict[str, object]:
    """Return the audit record ``audit_id`` with its :func:`audit_action` as ``action``.

    Authorized by :func:`crm.authz.require_record_scope` on the record's top-level ``account_id``,
    the predicate the list applies, so a record the list withholds is denied here too.

    Raises:
        NotFound: If no audit record exists with this id.
        ScopeDenied: If the record belongs to a different tenant, carries no account scope, or its
            account lies outside the caller's book.
    """
    item = _load_audit_record(store, audit_id)
    require_record_scope(identity, item, operation="get_audit_record")
    return {**item, "action": audit_action(item)}


def revert(
    identity: Identity,
    store: Store,
    *,
    audit_id: str,
    transaction_key: str,
    reason: str | None = None,
) -> dict[str, object]:
    """Reverse an audited change, restoring its before-snapshot.

    The restore and the audit record's flip to ``reversed`` are one ``TransactWriteItems`` call,
    conditioned on the audit record still being ``"active"`` and the target still matching
    ``after_snapshot``.

    Args:
        identity: The caller requesting the revert. Its scope and tenant must cover the audited
            target record.
        store: The persistence layer.
        audit_id: Identifier of the audit record to reverse.
        transaction_key: Idempotency token forwarded as ``ClientRequestToken``. A repeat call
            after a completed revert raises :class:`crm.errors.Conflict`.
        reason: Optional justification, stored as ``reversed_reason``. Blank or non-string values
            are treated as absent.

    Returns:
        The updated audit record item, with ``status="reversed"``.

    Raises:
        NotFound: If no audit record exists with this id.
        ScopeDenied: If the audit record's target lies outside the caller's book, the record
            belongs to a different tenant, or it carries no account scope at all.
        Conflict: If the audit record carries neither snapshot, was already reversed, records a
            proposal rebase (checked after scope), the target record was modified after the audit
            entry was written, the record is a create whose target still has child records (see
            :data:`_CHILD_INDEXES`), or it is a delete whose restored record's parent is absent
            (see :func:`_refuse_while_parent_absent`).
        CrmError: ``internal_error`` when the record carries an unrecognised ``action``.
        ValidationFailed: If ``reason`` is longer than :data:`_MAX_REASON_LENGTH`.
    """
    normalised_reason = _normalise_reason(reason)

    item = _load_in_tenant(identity, store, audit_id, operation="revert_audit_record")

    target_table = str(item["target_table"])
    target_id = str(item["target_id"])
    before_snapshot, after_snapshot = _snapshots_of(item, audit_id)
    require_scope(
        identity,
        _revert_scope(item, audit_id, before_snapshot, after_snapshot),
        operation="revert_audit_record",
    )

    if audit_action(item) == PROPOSAL_REBASED:
        raise Conflict(
            "A proposal rebase is not a change to the record and cannot be reverted.",
            detail=f"audit_id={audit_id} action={PROPOSAL_REBASED}",
        )

    if item.get("status") != _STATUS_ACTIVE:
        raise Conflict(
            "This change has already been reverted.",
            detail=f"audit_id={audit_id} status={item.get('status')}",
        )

    if not before_snapshot:
        _refuse_while_children_exist(store, identity.tenant, target_table, target_id)

    reversed_at = _now()
    operations = _revert_transaction_items(
        target_table=target_table,
        target_id=target_id,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        audit_id=audit_id,
        reversed_at=reversed_at,
        reversed_by=identity.rep_id,
        reversed_reason=normalised_reason,
    )
    if before_snapshot and not after_snapshot:
        _refuse_while_parent_absent(store, identity.tenant, target_table, before_snapshot)
    store.transact_write(operations, transaction_key=transaction_key)

    return {
        **item,
        "status": _STATUS_REVERSED,
        "reversed_at": reversed_at,
        "reversed_by": identity.rep_id,
        "reversed_reason": normalised_reason,
    }


#: For each table a create can be reverted on, the (child table, GSI) pairs whose index partition
#: is ``tenant_prefixed(tenant, <target id>)``. A create revert is refused while any has an item.
_CHILD_INDEXES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "accounts": (
        ("contacts", "gsi_account"),
        ("opportunities", "gsi_account"),
        ("activities", "gsi_account"),
    ),
    "opportunities": (("activities", "gsi_opportunity"), ("notes", "gsi_opportunity")),
}


#: The table whose reverts may only delete a created item; a revert that would ``Put`` is refused.
_DELETE_ONLY_REVERT_TABLE: Final[str] = "notes"


#: Tables whose records link to a parent account through ``account_id``.
_ACCOUNT_CHILD_TABLES: Final[frozenset[str]] = frozenset({"contacts", "opportunities"})

#: Tables whose records link to a parent opportunity through ``opportunity_id``.
_OPPORTUNITY_CHILD_TABLES: Final[frozenset[str]] = frozenset({"activities", "notes"})


def _snapshots_of(
    item: dict[str, object], audit_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    """Return ``item``'s ``(before_snapshot, after_snapshot)``.

    Raises:
        Conflict: The record carries neither snapshot, or one is present but not a mapping.
    """
    if "before_snapshot" not in item and "after_snapshot" not in item:
        raise Conflict(
            "This audit record has no before or after snapshot, so it cannot be reverted.",
            detail=f"audit_id={audit_id} has neither before_snapshot nor after_snapshot",
        )
    return _as_snapshot(item.get("before_snapshot")), _as_snapshot(item.get("after_snapshot"))


def _revert_scope(
    item: dict[str, object],
    audit_id: str,
    before_snapshot: dict[str, object],
    after_snapshot: dict[str, object],
) -> str:
    """Return the account a revert of ``item`` is scope-checked against.

    The record's own ``account_id`` (set by :func:`build_audit_record`) wins, then either snapshot's.

    Raises:
        ScopeDenied: Neither carries one.
    """
    stored = item.get("account_id")
    if isinstance(stored, str) and stored:
        return stored
    derived = _account_scope_of(before_snapshot, after_snapshot)
    if derived is not None:
        return derived
    raise ScopeDenied(
        "This change is not scoped to an account, so it cannot be reverted.",
        missing_scope="account:unknown",
        detail=f"audit_id={audit_id} has no account_id on the record or its snapshots",
    )


def _refuse_while_children_exist(
    store: Store, tenant: str, target_table: str, target_id: str
) -> None:
    """Raise :class:`Conflict` naming every child table that still holds an item of ``target_id``.

    A contact's children are the opportunities naming it as ``primary_contact_id``, found among its
    account's opportunities.
    """
    children = [
        child_table
        for child_table, index in _CHILD_INDEXES.get(target_table, ())
        if store.query_gsi(
            child_table, index, partition_value=tenant_prefixed(tenant, target_id), limit=1
        )[0]
    ]
    if target_table == "contacts" and _names_primary_contact(store, tenant, target_id):
        children.append("opportunities")
    if children:
        raise Conflict(
            f"This record still has {', '.join(children)}; revert or remove them first.",
            detail=f"target_table={target_table} target_id={target_id} children={children}",
        )


def _refuse_while_parent_absent(
    store: Store, tenant: str, target_table: str, before_snapshot: dict[str, object]
) -> None:
    """Raise :class:`Conflict` unless the parent a restored record links to exists in ``tenant``.

    Contacts and opportunities link to an account through ``account_id``; activities and notes to an
    opportunity through ``opportunity_id``. Other tables have no parent. Reads are consistent.
    """
    parent: dict[str, object] | None
    if target_table in _ACCOUNT_CHILD_TABLES:
        link = "account_id"
        parent_id = before_snapshot.get(link)
        parent = (
            store.get_item("accounts", {"account_id": parent_id}, consistent=True)
            if isinstance(parent_id, str) and parent_id
            else None
        )
    elif target_table in _OPPORTUNITY_CHILD_TABLES:
        link = "opportunity_id"
        parent_id = before_snapshot.get(link)
        parent = (
            store.get_item("opportunities", {"opportunity_id": parent_id}, consistent=True)
            if isinstance(parent_id, str) and parent_id
            else None
        )
    else:
        return
    if parent is None or parent.get("tenant") != tenant:
        raise Conflict(
            "The record this change belongs to no longer exists; restore it first.",
            detail=f"target_table={target_table} {link}={parent_id!r} parent absent",
        )


def _names_primary_contact(store: Store, tenant: str, contact_id: str) -> bool:
    """Whether any opportunity names ``contact_id`` as its primary contact.

    Delegates to :func:`crm.handlers.cascade_rules.reject_contact_delete_if_referenced`, the check
    the proposal path's contact delete runs.
    """
    from crm.handlers.cascade_rules import reject_contact_delete_if_referenced

    try:
        reject_contact_delete_if_referenced(store, tenant=tenant, contact_id=contact_id)
    except ValidationFailed:
        return True
    return False


def _normalise_reason(reason: str | None) -> str | None:
    """Return the revert reason to persist, or ``None`` when the caller supplied none.

    Args:
        reason: The caller-supplied value, which reaches here unvalidated.

    Returns:
        The stripped string, or ``None`` when ``reason`` is absent, not a string, or blank.

    Raises:
        ValidationFailed: When the stripped string exceeds :data:`_MAX_REASON_LENGTH`.
    """
    if not isinstance(reason, str):
        return None
    stripped = reason.strip()
    if not stripped:
        return None
    if len(stripped) > _MAX_REASON_LENGTH:
        raise ValidationFailed(
            "The revert reason is too long.",
            source="reason",
            detail=f"length={len(stripped)} max={_MAX_REASON_LENGTH}",
        )
    return stripped


def _as_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise Conflict("The audit record is malformed and cannot be reverted.")
    return value


def _revert_transaction_items(
    *,
    target_table: str,
    target_id: str,
    before_snapshot: dict[str, object],
    after_snapshot: dict[str, object],
    audit_id: str,
    reversed_at: str,
    reversed_by: str,
    reversed_reason: str | None = None,
) -> list[dict[str, Any]]:
    """Build the two-item ``TransactWriteItems`` body for :func:`revert`.

    The target ``Put`` (or ``Delete``, when ``before_snapshot`` is empty) is conditioned on the
    target matching ``after_snapshot``, or, for a delete's record (empty ``after_snapshot``), on the
    target still being absent. The audit ``Update`` is conditioned on ``status = active``.
    """
    target_key_name = _primary_key_name(target_table)
    condition_names = {f"#{k}": k for k in after_snapshot}
    condition_values = {f":{k}": _to_dynamo_value(v) for k, v in after_snapshot.items()}
    condition_expression = (
        " AND ".join(f"#{k} = :{k}" for k in after_snapshot) or "attribute_not_exists(#pk)"
    )
    if not after_snapshot:
        condition_names = {"#pk": target_key_name}
        condition_values = {}

    if target_table == _DELETE_ONLY_REVERT_TABLE and before_snapshot:
        raise Conflict(
            "A note change can only be reverted by deleting a created note.",
            detail=f"audit_id={audit_id} target_table={target_table} has a before_snapshot",
        )

    target_operation: dict[str, Any]
    if before_snapshot:
        target_operation = {
            "Put": {
                "TableName": target_table,
                "Item": _encode_item_for_transaction(before_snapshot),
                "ConditionExpression": condition_expression,
                "ExpressionAttributeNames": condition_names,
                **({"ExpressionAttributeValues": condition_values} if condition_values else {}),
            }
        }
    else:
        target_operation = {
            "Delete": {
                "TableName": target_table,
                "Key": {target_key_name: {"S": str(target_id)}},
                "ConditionExpression": condition_expression,
                "ExpressionAttributeNames": condition_names,
                **({"ExpressionAttributeValues": condition_values} if condition_values else {}),
            }
        }

    audit_update_expression = (
        "SET #status = :reversed, reversed_at = :reversed_at, reversed_by = :reversed_by"
    )
    audit_values: dict[str, Any] = {
        ":active": {"S": _STATUS_ACTIVE},
        ":reversed": {"S": _STATUS_REVERSED},
        ":reversed_at": {"S": reversed_at},
        ":reversed_by": {"S": reversed_by},
    }
    if reversed_reason is not None:
        audit_update_expression += ", reversed_reason = :reversed_reason"
        audit_values[":reversed_reason"] = {"S": reversed_reason}

    audit_operation: dict[str, Any] = {
        "Update": {
            "TableName": _TABLE,
            "Key": {"audit_id": {"S": audit_id}},
            "ConditionExpression": "#status = :active",
            "UpdateExpression": audit_update_expression,
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": audit_values,
        }
    }
    return [target_operation, audit_operation]


def _primary_key_name(target_table: str) -> str:
    """Return the primary partition-key attribute name for a target table."""
    from crm.store.schema import table_schema

    return table_schema(target_table).partition_key


def _encode_item_for_transaction(item: dict[str, object]) -> dict[str, object]:
    return {key: _to_dynamo_value(value) for key, value in item.items()}


def _to_dynamo_value(value: object) -> dict[str, object]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, dict):
        return {"M": _encode_item_for_transaction(value)}
    if isinstance(value, (list, tuple)):
        return {"L": [_to_dynamo_value(v) for v in value]}
    raise TypeError(f"cannot encode value of type {type(value).__name__} for DynamoDB")
