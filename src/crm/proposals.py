# SPDX-License-Identifier: MIT
"""Proposal lifecycle: submit, approve, reject, and rebase for high-risk writes.

Every high-risk write reaches persisted state only through :func:`submit_proposal` followed by
:func:`approve_proposal`. Approval re-reads the target and refuses with
:class:`crm.errors.Conflict` when a field the proposal changes no longer matches the stored
``before_snapshot``; it writes only those fields. Approval and
rejection both condition the status flip on ``status == pending``, so exactly one decision wins.
``after_payload`` is checked by :func:`crm.domain.allowlists.validate_proposal_payload` at submit
and again at approve. :func:`rebase_proposal` re-captures a pending proposal's ``before_snapshot``
from current state. Proposal ``Update`` expressions set ``tenant_status`` alongside ``status``;
:meth:`crm.store.store.Store.transact_write` applies the table prefix.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from crm.domain.allowlists import validate_proposal_payload
from crm.errors import Conflict, CrmError, NotFound, ValidationFailed
from crm.store.keys import derive, derived_attributes, tenant_prefixed
from crm.store.store import Store
from crm.toolspec.definitions import TABLES

logger = logging.getLogger(__name__)

_PENDING = "pending"
_APPROVED = "approved"
_REJECTED = "rejected"

#: Operations a proposal may carry out on approval (matches ``submit_proposal``'s tool schema).
_OPERATIONS = frozenset({"update", "delete"})

#: Callback :func:`approve_proposal` invokes to build one audit-record transact-item operation,
#: committed in the same transaction as the change.
AuditRecorder = Callable[["ApprovalContext"], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ApprovalContext:
    """Everything an :data:`AuditRecorder` needs to build one audit-record write operation.

    Attributes:
        tenant: Tenant the proposal and target record belong to.
        proposal_id: The proposal being approved.
        target_table: Table the change applies to.
        target_id: Primary key of the affected record.
        before_snapshot: The record's state immediately before this approval, as re-read from the
            store.
        after_snapshot: The record's state immediately after applying the proposal's
            ``after_payload`` (a delete yields ``None``).
        actor_rep_id: Rep approving the proposal.
        reason: The proposal's recorded reason.
    """

    tenant: str
    proposal_id: str
    target_table: str
    target_id: str
    before_snapshot: Mapping[str, object]
    after_snapshot: Mapping[str, object] | None
    actor_rep_id: str
    reason: str


#: The longest caller-supplied value an error ``detail`` carries.
_DETAIL_VALUE_LIMIT: Final[int] = 64


def _bounded(value: object) -> str:
    """Return ``value``'s repr cut to :data:`_DETAIL_VALUE_LIMIT` characters, for ``detail`` only."""
    return repr(str(value)[:_DETAIL_VALUE_LIMIT])


def proposal_not_found(proposal_id: str) -> NotFound:
    """Return the ``NotFound`` for an absent or other-tenant proposal; the id goes to ``detail``."""
    return NotFound("The proposal does not exist.", detail=f"proposal_id={_bounded(proposal_id)}")


def _require_table(target_table: str) -> None:
    if target_table not in TABLES:
        raise ValidationFailed(
            "Unrecognised proposal target table.",
            source="target_table",
            detail=f"target_table={_bounded(target_table)}",
        )


def _table_schema_keys(target_table: str) -> tuple[str, str | None]:
    """Return ``(partition_key_attr, sort_key_attr)`` for a target table."""
    from crm.store.schema import table_schema

    schema = table_schema(target_table)
    return schema.partition_key, schema.sort_key


def submit_proposal(
    store: Store,
    *,
    tenant: str,
    proposal_id: str,
    target_table: str,
    target_id: str,
    operation: str,
    actor_rep_id: str,
    reason: str,
    transaction_key: str,
    after_payload: Mapping[str, object] | None = None,
    now: str,
) -> dict[str, object]:
    """Create a pending proposal holding a before-snapshot, an after-payload, and a reason.

    The target is only read, to capture ``before_snapshot``; the proposal is the only item written.

    Args:
        store: The persistence layer.
        tenant: Tenant the proposal and target belong to.
        proposal_id: Caller-supplied stable identifier for the new proposal.
        target_table: One of :data:`crm.toolspec.definitions.TABLES`.
        target_id: Primary key of the record the proposal targets.
        operation: ``"update"`` or ``"delete"``.
        actor_rep_id: Rep submitting the proposal.
        reason: Why this change is being requested. Recorded on the proposal and, on approval, on
            the audit record.
        transaction_key: Caller-supplied idempotency key.
        after_payload: Intended field values for an ``"update"``. Validated whatever the operation;
            a ``"delete"`` proposal stores ``None``.
        now: Caller-supplied current timestamp, ISO 8601.

    Returns:
        The persisted proposal item.

    Raises:
        ValidationFailed: ``target_table`` is not recognised, ``operation`` is not one of
            ``update``/``delete``, ``reason`` is empty, an ``update`` proposal carries no
            ``after_payload``, or ``after_payload`` carries a field outside
            :func:`crm.domain.allowlists.proposal_payload_allowlist`.
        NotFound: The target record does not exist in ``target_table`` under ``tenant``.
    """
    _require_table(target_table)
    if operation not in _OPERATIONS:
        raise ValidationFailed(
            f"Unrecognised proposal operation; expected one of {sorted(_OPERATIONS)}.",
            source="operation",
            detail=f"operation={_bounded(operation)}",
        )
    if not reason:
        raise ValidationFailed("A reason is required to submit a proposal.", source="reason")
    if operation == "update" and not after_payload:
        raise ValidationFailed(
            "An update proposal requires a non-empty after_payload.", source="after_payload"
        )
    validate_proposal_payload(target_table, after_payload)

    partition_key, sort_key = _table_schema_keys(target_table)
    lookup_key: dict[str, str] = {partition_key: target_id}
    before_snapshot = store.get_item(target_table, lookup_key)
    if before_snapshot is None or before_snapshot.get("tenant") != tenant:
        raise NotFound(
            "The record targeted by this proposal does not exist.",
            detail=f"target_id={_bounded(target_id)}",
        )

    proposal_item: dict[str, object] = derive(
        "proposals",
        {
            "proposal_id": proposal_id,
            "tenant": tenant,
            "status": _PENDING,
            "target_table": target_table,
            "target_id": target_id,
            "operation": operation,
            "before_snapshot": dict(before_snapshot),
            "after_payload": (
                dict(after_payload) if operation != "delete" and after_payload else None
            ),
            "reason": reason,
            "actor_rep_id": actor_rep_id,
            "created_at": now,
            "updated_at": now,
            "sort_key_attr": sort_key,
        },
    )
    store.put_idempotent(
        "proposals",
        proposal_item,
        transaction_key=transaction_key,
    )
    return proposal_item


def _apply_operation(
    target_table: str,
    current: Mapping[str, object],
    operation: str,
    after_payload: Mapping[str, object] | None,
    *,
    now: str,
) -> dict[str, object] | None:
    """Compute the record's post-approval state, never writing it.

    A ``delete`` yields ``None``; an ``update`` yields ``current`` merged with ``after_payload``,
    stamped with ``now``, with index keys recomputed by :func:`crm.store.keys.derive`.
    """
    if operation == "delete":
        return None
    merged = {**current, **(after_payload or {}), "updated_at": now}
    return derive(target_table, merged)


def _require_payload_targets_declared_record(
    payload: Mapping[str, object],
    *,
    partition_key: str,
    target_table: str,
    proposal_id: str,
) -> None:
    """Refuse an ``after_payload`` that would rewrite the target's key or move its tenant.

    Raises:
        ValidationFailed: ``payload`` assigns the partition key or ``tenant``.
    """
    if partition_key in payload:
        raise ValidationFailed(
            "This proposal's after_payload would write to a different record than it declared.",
            source="after_payload",
            detail=f"proposal_id={proposal_id} target_table={target_table} assigns {partition_key}",
        )
    if "tenant" in payload:
        raise ValidationFailed(
            "This proposal's after_payload would move the record to a different tenant.",
            source="after_payload",
            detail=f"proposal_id={proposal_id} target_table={target_table} assigns tenant",
        )


def _changed_fields(
    touched: Mapping[str, object],
    snapshot: Mapping[str, object],
    current: Mapping[str, object],
) -> list[dict[str, object]]:
    """Return one ``{field, snapshot, current, proposed}`` entry per touched field that moved."""
    return [
        {
            "field": field,
            "snapshot": snapshot.get(field),
            "current": current.get(field),
            "proposed": proposed,
        }
        for field, proposed in touched.items()
        if (field in snapshot, snapshot.get(field)) != (field in current, current.get(field))
    ]


def _fields_changed_conflict(changed: list[dict[str, object]], *, proposal_id: str) -> Conflict:
    return Conflict(
        "Fields this proposal changes were modified since it was raised; approval refused.",
        reason="fields_changed",
        conflicting_fields=changed,
        detail=f"proposal_id={proposal_id} fields={[c['field'] for c in changed]}",
    )


def _target_missing_conflict(*, proposal_id: str, target_table: str, target_id: str) -> Conflict:
    return Conflict(
        "The record this proposal targets no longer exists.",
        reason="target_missing",
        detail=f"proposal_id={proposal_id} target_table={target_table} target_id={target_id}",
    )


def _guarded_derived_inputs(target_table: str, writable: Mapping[str, object]) -> list[str]:
    """Untouched sources (other than ``tenant``) of derived attributes ``writable`` feeds, in order."""
    guarded: list[str] = []
    for spec in derived_attributes(target_table):
        if not any(source in writable for source in spec.sources):
            continue
        for source in spec.sources:
            if source not in writable and source != "tenant" and source not in guarded:
                guarded.append(source)
    return guarded


def _conditional_update_operation(
    target_table: str,
    partition_key: str,
    target_id: str,
    tenant: str,
    touched: Mapping[str, object],
    snapshot: Mapping[str, object],
    current: Mapping[str, object],
    now: str,
) -> dict[str, Any]:
    """Build the target ``Update`` that applies only ``touched``.

    ``SET``s each touched field, ``updated_at`` and every derived attribute a touched field feeds
    (``REMOVE`` when its sources no longer build it). Conditioned on the record existing under
    ``tenant``, each touched field still equal to ``snapshot`` (``attribute_not_exists`` when
    absent there), and each untouched source of an affected derived attribute equal to
    ``current``.
    """
    names: dict[str, str] = {"#pk": partition_key, "#tenant": "tenant"}
    values: dict[str, object] = {":tenant": {"S": tenant}}
    assignments: list[str] = []
    removals: list[str] = []
    conditions = ["attribute_exists(#pk)", "#tenant = :tenant"]

    writable = {k: v for k, v in touched.items() if k != "updated_at"}
    for index, (field, value) in enumerate(writable.items()):
        names[f"#f{index}"] = field
        values[f":f{index}"] = _encode_value(value)
        assignments.append(f"#f{index} = :f{index}")
        if field in snapshot:
            values[f":s{index}"] = _encode_value(snapshot[field])
            conditions.append(f"#f{index} = :s{index}")
        else:
            conditions.append(f"attribute_not_exists(#f{index})")
    names["#updated_at"] = "updated_at"
    values[":now"] = {"S": now}
    assignments.append("#updated_at = :now")

    rebuilt = derive(target_table, {**current, **writable})
    for index, spec in enumerate(derived_attributes(target_table)):
        if not any(source in writable for source in spec.sources):
            continue
        names[f"#d{index}"] = spec.name
        if spec.name in rebuilt:
            values[f":d{index}"] = {"S": str(rebuilt[spec.name])}
            assignments.append(f"#d{index} = :d{index}")
        else:
            removals.append(f"#d{index}")
    for index, source in enumerate(_guarded_derived_inputs(target_table, writable)):
        names[f"#u{index}"] = source
        if source in current:
            values[f":u{index}"] = _encode_value(current[source])
            conditions.append(f"#u{index} = :u{index}")
        else:
            conditions.append(f"attribute_not_exists(#u{index})")

    update_expression = "SET " + ", ".join(assignments)
    if removals:
        update_expression += " REMOVE " + ", ".join(removals)
    return {
        "Update": {
            "TableName": target_table,
            "Key": {partition_key: {"S": target_id}},
            "UpdateExpression": update_expression,
            "ConditionExpression": " AND ".join(conditions),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
    }


def _explain_cancelled_approval(
    store: Store,
    original: Conflict,
    *,
    tenant: str,
    proposal_id: str,
    target_table: str,
    partition_key: str,
    target_id: str,
    operation: str,
    touched: Mapping[str, object],
    snapshot: Mapping[str, object],
    read: Mapping[str, object],
) -> CrmError:
    """Re-read after a cancelled approval and return the error that explains it.

    Reports a moved touched field, or a moved untouched input of a rewritten derived attribute
    (``proposed`` is ``None`` for those), as ``fields_changed``. Returns ``original`` when the
    re-read shows nothing that would have failed a condition, or when the re-read itself fails.
    """
    try:
        proposal = store.get_item("proposals", {"proposal_id": proposal_id}, consistent=True)
        current = store.get_item(target_table, {partition_key: target_id}, consistent=True)
    except Exception as exc:  # noqa: BLE001 - the original Conflict is the truthful fallback.
        logger.warning(
            json.dumps({"event": "approve_conflict_explain_failed", "reason": type(exc).__name__})
        )
        return original
    if proposal is None or proposal.get("tenant") != tenant:
        return proposal_not_found(proposal_id)
    if proposal.get("status") != _PENDING:
        return Conflict(
            "This proposal has already been decided and cannot be approved again.",
            reason="already_decided",
            detail=f"proposal_id={proposal_id} decided concurrently",
        )
    if current is None or current.get("tenant") != tenant:
        return _target_missing_conflict(
            proposal_id=proposal_id, target_table=target_table, target_id=target_id
        )
    if operation == "delete":
        return original
    changed = _changed_fields(touched, snapshot, current)
    writable = {k: v for k, v in touched.items() if k != "updated_at"}
    for field in _guarded_derived_inputs(target_table, writable):
        if (field in read, read.get(field)) != (field in current, current.get(field)):
            changed.append(
                {
                    "field": field,
                    "snapshot": snapshot.get(field),
                    "current": current.get(field),
                    "proposed": None,
                }
            )
    if changed:
        return _fields_changed_conflict(changed, proposal_id=proposal_id)
    return original


def _approval_key(transaction_key: str) -> str:
    """The stable marker an approval writes onto its proposal, derived from ``transaction_key``."""
    return f"approval-{uuid.uuid5(uuid.NAMESPACE_OID, f'approval:{transaction_key}')}"


def _approved_by(proposal: Mapping[str, object] | None, transaction_key: str) -> bool:
    """Whether ``proposal`` was approved by an earlier call carrying ``transaction_key``."""
    return (
        proposal is not None
        and proposal.get("status") == _APPROVED
        and proposal.get("approval_key") == _approval_key(transaction_key)
    )


def _replayed_approval(
    store: Store, *, tenant: str, proposal_id: str, transaction_key: str
) -> dict[str, object] | None:
    """The approved proposal if ``transaction_key`` already approved it, else ``None``."""
    try:
        proposal = store.get_item("proposals", {"proposal_id": proposal_id}, consistent=True)
    except Exception as exc:  # noqa: BLE001 - absence of a replay falls back to the conflict.
        logger.warning(
            json.dumps({"event": "approve_replay_check_failed", "reason": type(exc).__name__})
        )
        return None
    if proposal is None or proposal.get("tenant") != tenant:
        return None
    return proposal if _approved_by(proposal, transaction_key) else None


def approve_proposal(
    store: Store,
    *,
    tenant: str,
    proposal_id: str,
    actor_rep_id: str,
    transaction_key: str,
    now: str,
    record_audit: AuditRecorder | None = None,
) -> dict[str, object]:
    """Approve a pending proposal: verify, apply, and flip status in one atomic transaction.

    Only the fields in ``after_payload`` are compared and written: the target ``Update``
    sets them, ``updated_at`` and the derived attributes they feed, conditioned on each still
    holding its ``before_snapshot`` value. Writes to other fields do not block approval. The
    proposal ``Update`` is conditioned on ``status == pending`` and stamps an ``approval_key``
    derived from ``transaction_key``, so a retry of a committed approval returns the approved
    proposal instead of a conflict.

    Args:
        store: The persistence layer.
        tenant: Tenant the proposal belongs to.
        proposal_id: The proposal to approve.
        actor_rep_id: Rep approving the proposal.
        transaction_key: Caller-supplied idempotency key forwarded as the transaction's
            ``ClientRequestToken``.
        now: Caller-supplied current timestamp, ISO 8601.
        record_audit: Optional callback producing one additional transact-item operation that
            records an audit entry in the same transaction. Its context carries the record as
            re-read (``before_snapshot``) and that record merged with ``after_payload``
            (``after_snapshot``).

    Returns:
        The proposal item as it stood immediately before approval, with ``status`` updated to
        ``"approved"`` in the returned copy.

    Raises:
        NotFound: The proposal does not exist under ``tenant``.
        Conflict: ``reason="already_decided"`` when the proposal is not ``pending``;
            ``reason="target_missing"`` when the target no longer exists under ``tenant``;
            ``reason="fields_changed"`` with ``conflicting_fields`` when a field the proposal
            changes moved since it was raised; no reason when the before-snapshot is malformed
            or the transaction was cancelled for another cause.
        ValidationFailed: The stored ``after_payload`` carries a field outside
            :func:`crm.domain.allowlists.proposal_payload_allowlist`, or assigns the target's
            partition key or ``tenant``.
        RuntimeError: A non-``delete`` operation produced no after-snapshot (internal invariant).
    """
    proposal = store.get_item("proposals", {"proposal_id": proposal_id})
    if proposal is None or proposal.get("tenant") != tenant:
        raise proposal_not_found(proposal_id)
    if _approved_by(proposal, transaction_key):
        return proposal
    if proposal.get("status") != _PENDING:
        raise Conflict(
            "This proposal has already been decided and cannot be approved again.",
            reason="already_decided",
            detail=f"proposal_id={proposal_id} status={proposal.get('status')!r}",
        )

    target_table = str(proposal["target_table"])
    target_id = str(proposal["target_id"])
    operation = str(proposal["operation"])
    stored_before = proposal.get("before_snapshot")
    if not isinstance(stored_before, Mapping):
        raise Conflict(
            "This proposal's before-snapshot is malformed and cannot be verified.",
            detail=f"proposal_id={proposal_id}",
        )

    after_payload = proposal.get("after_payload")
    stored_payload = after_payload if isinstance(after_payload, Mapping) else None
    validate_proposal_payload(target_table, stored_payload)
    touched: Mapping[str, object] = (stored_payload or {}) if operation != "delete" else {}

    partition_key, _sort_key = _table_schema_keys(target_table)
    _require_payload_targets_declared_record(
        touched, partition_key=partition_key, target_table=target_table, proposal_id=proposal_id
    )
    current = store.get_item(target_table, {partition_key: target_id})
    if current is None or current.get("tenant") != tenant:
        raise _target_missing_conflict(
            proposal_id=proposal_id, target_table=target_table, target_id=target_id
        )
    changed = _changed_fields(touched, stored_before, current)
    if changed:
        raise _fields_changed_conflict(changed, proposal_id=proposal_id)

    after_snapshot = _apply_operation(target_table, current, operation, touched, now=now)
    if operation != "delete" and after_snapshot is None:
        raise RuntimeError(f"operation {operation!r} produced no after-snapshot")

    # Logical table names; Store.transact_write resolves the physical prefixed names.
    operations: list[dict[str, Any]] = [
        {
            "Update": {
                "TableName": "proposals",
                "Key": {"proposal_id": {"S": proposal_id}},
                "UpdateExpression": (
                    "SET #status = :approved, tenant_status = :tenant_status, updated_at = :now, "
                    "approval_key = :approval_key"
                ),
                "ConditionExpression": "#status = :pending",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {
                    ":approved": {"S": _APPROVED},
                    ":tenant_status": {"S": tenant_prefixed(tenant, _APPROVED)},
                    ":pending": {"S": _PENDING},
                    ":now": {"S": now},
                    ":approval_key": {"S": _approval_key(transaction_key)},
                },
            }
        }
    ]

    if operation == "delete":
        operations.append(
            {
                "Delete": {
                    "TableName": target_table,
                    "Key": {partition_key: {"S": target_id}},
                    "ConditionExpression": "attribute_exists(#pk) AND #tenant = :tenant",
                    "ExpressionAttributeNames": {"#pk": partition_key, "#tenant": "tenant"},
                    "ExpressionAttributeValues": {":tenant": {"S": tenant}},
                }
            }
        )
    else:
        operations.append(
            _conditional_update_operation(
                target_table,
                partition_key,
                target_id,
                tenant,
                touched,
                stored_before,
                current,
                now,
            )
        )

    if record_audit is not None:
        context = ApprovalContext(
            tenant=tenant,
            proposal_id=proposal_id,
            target_table=target_table,
            target_id=target_id,
            before_snapshot=current,
            after_snapshot=after_snapshot,
            actor_rep_id=actor_rep_id,
            reason=str(proposal.get("reason", "")),
        )
        operations.append(record_audit(context))

    try:
        store.transact_write(operations, transaction_key=transaction_key)
    except Conflict as exc:
        replayed = _replayed_approval(
            store, tenant=tenant, proposal_id=proposal_id, transaction_key=transaction_key
        )
        if replayed is not None:
            return replayed
        raise _explain_cancelled_approval(
            store,
            exc,
            tenant=tenant,
            proposal_id=proposal_id,
            target_table=target_table,
            partition_key=partition_key,
            target_id=target_id,
            operation=operation,
            touched=touched,
            snapshot=stored_before,
            read=current,
        ) from exc

    return {
        **proposal,
        "status": _APPROVED,
        "tenant_status": tenant_prefixed(tenant, _APPROVED),
        "updated_at": now,
        "approval_key": _approval_key(transaction_key),
    }


def reject_proposal(
    store: Store,
    *,
    tenant: str,
    proposal_id: str,
    actor_rep_id: str,
    transaction_key: str,
    now: str,
    reason: str | None = None,
) -> dict[str, object]:
    """Reject a pending proposal. Terminal: a rejected proposal can never be approved.

    Conditions the status flip on ``status == pending``, the same guard :func:`approve_proposal`
    uses.

    Args:
        store: The persistence layer.
        tenant: Tenant the proposal belongs to.
        proposal_id: The proposal to reject.
        actor_rep_id: Rep rejecting the proposal.
        transaction_key: Caller-supplied idempotency key.
        now: Caller-supplied current timestamp, ISO 8601.
        reason: Optional rejection reason, distinct from the proposal's original submission reason.

    Returns:
        The proposal item with ``status`` updated to ``"rejected"`` in the returned copy.

    Raises:
        NotFound: The proposal does not exist under ``tenant``.
        Conflict: The proposal is not ``pending`` (already approved or already rejected).
    """
    proposal = store.get_item("proposals", {"proposal_id": proposal_id})
    if proposal is None or proposal.get("tenant") != tenant:
        raise proposal_not_found(proposal_id)
    if proposal.get("status") != _PENDING:
        raise Conflict(
            "This proposal has already been decided and cannot be rejected again.",
            detail=f"proposal_id={proposal_id} status={proposal.get('status')!r}",
        )

    values: dict[str, object] = {
        ":rejected": {"S": _REJECTED},
        ":tenant_status": {"S": tenant_prefixed(tenant, _REJECTED)},
        ":pending": {"S": _PENDING},
        ":now": {"S": now},
        ":actor": {"S": actor_rep_id},
    }
    update_expression = (
        "SET #status = :rejected, tenant_status = :tenant_status, updated_at = :now, "
        "rejected_by = :actor"
    )
    if reason:
        update_expression += ", rejection_reason = :reason"
        values[":reason"] = {"S": reason}

    operations: list[dict[str, Any]] = [
        {
            "Update": {
                "TableName": "proposals",
                "Key": {"proposal_id": {"S": proposal_id}},
                "UpdateExpression": update_expression,
                "ConditionExpression": "#status = :pending",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": values,
            }
        }
    ]
    store.transact_write(operations, transaction_key=transaction_key)

    result = {
        **proposal,
        "status": _REJECTED,
        "tenant_status": tenant_prefixed(tenant, _REJECTED),
        "updated_at": now,
        "rejected_by": actor_rep_id,
    }
    if reason:
        result["rejection_reason"] = reason
    return result


def rebase_proposal(
    store: Store,
    *,
    tenant: str,
    proposal_id: str,
    actor_rep_id: str,
    now: str,
    record_audit: AuditRecorder,
    transaction_key: str | None = None,
) -> dict[str, object]:
    """Replace a pending proposal's ``before_snapshot`` with the target's current state.

    The proposal ``Update`` is conditioned on ``status == pending`` and on ``updated_at`` still
    holding the value just read, and commits in one transaction with the audit operation
    ``record_audit`` builds. That callback receives the old snapshot as ``before_snapshot`` and the
    current record as ``after_snapshot``. ``after_payload`` is unchanged.

    Args:
        store: The persistence layer.
        tenant: Tenant the proposal belongs to.
        proposal_id: The proposal to rebase.
        actor_rep_id: Rep requesting the rebase.
        now: Caller-supplied current timestamp, ISO 8601; becomes the proposal's ``updated_at``.
        record_audit: Builds the audit-record transact-item operation committed alongside.
        transaction_key: Optional idempotency key forwarded as ``ClientRequestToken``.

    Returns:
        The proposal item with the new ``before_snapshot`` and ``updated_at``.

    Raises:
        NotFound: The proposal does not exist under ``tenant``.
        Conflict: ``reason="already_decided"`` when the proposal is not ``pending``;
            ``reason="target_missing"`` when the target record no longer exists under ``tenant``;
            no reason when the proposal changed concurrently or is malformed.
    """
    proposal = store.get_item("proposals", {"proposal_id": proposal_id})
    if proposal is None or proposal.get("tenant") != tenant:
        raise proposal_not_found(proposal_id)
    if proposal.get("status") != _PENDING:
        raise Conflict(
            "This proposal has already been decided and cannot be rebased.",
            reason="already_decided",
            detail=f"proposal_id={proposal_id} status={proposal.get('status')!r}",
        )

    target_table = str(proposal["target_table"])
    target_id = str(proposal["target_id"])
    stored_before = proposal.get("before_snapshot")
    if target_table not in TABLES or not isinstance(stored_before, Mapping):
        raise Conflict(
            "This proposal is malformed and cannot be rebased.",
            detail=f"proposal_id={proposal_id} target_table={target_table!r}",
        )

    partition_key, _sort_key = _table_schema_keys(target_table)
    current = store.get_item(target_table, {partition_key: target_id})
    if current is None or current.get("tenant") != tenant:
        raise Conflict(
            "The record this proposal targets no longer exists.",
            reason="target_missing",
            detail=f"proposal_id={proposal_id} target_table={target_table} target_id={target_id}",
        )

    read_updated_at = proposal.get("updated_at")
    values: dict[str, object] = {
        ":pending": {"S": _PENDING},
        ":current": _encode_value(current),
        ":now": {"S": now},
    }
    if isinstance(read_updated_at, str):
        updated_at_condition = "updated_at = :read_updated_at"
        values[":read_updated_at"] = {"S": read_updated_at}
    else:
        updated_at_condition = "attribute_not_exists(updated_at)"

    context = ApprovalContext(
        tenant=tenant,
        proposal_id=proposal_id,
        target_table=target_table,
        target_id=target_id,
        before_snapshot=stored_before,
        after_snapshot=current,
        actor_rep_id=actor_rep_id,
        reason=str(proposal.get("reason", "")),
    )
    operations: list[dict[str, Any]] = [
        {
            "Update": {
                "TableName": "proposals",
                "Key": {"proposal_id": {"S": proposal_id}},
                "UpdateExpression": "SET before_snapshot = :current, updated_at = :now",
                "ConditionExpression": f"#status = :pending AND {updated_at_condition}",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": values,
            }
        },
        record_audit(context),
    ]
    store.transact_write(operations, transaction_key=transaction_key)

    return {**proposal, "before_snapshot": dict(current), "updated_at": now}


def _encode_put_item(item: Mapping[str, object]) -> dict[str, object]:
    """Encode a plain mapping into DynamoDB's typed attribute-value wire format for a raw ``Put``.

    Uses the same encoding rules as ``crm.store.store``'s private item encoder.
    """
    return {key: _encode_value(value) for key, value in item.items()}


def _encode_value(value: object) -> dict[str, object]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, Mapping):
        return {"M": _encode_put_item(value)}
    if isinstance(value, (list, tuple)):
        return {"L": [_encode_value(v) for v in value]}
    raise TypeError(f"cannot encode value of type {type(value).__name__} for DynamoDB")
