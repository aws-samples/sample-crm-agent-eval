# SPDX-License-Identifier: MIT
"""Raw ``TransactWriteItems`` operations for handlers: attribute-value encoding, ``Put`` builders,
and the optimistic-concurrency condition that pins a write to the item as it was read.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from crm.errors import Conflict, bounded_detail_value


def encode_item(item: Mapping[str, object]) -> dict[str, object]:
    """Encode a plain Python mapping into DynamoDB's typed attribute-value wire format."""
    return {key: encode_value(value) for key, value in item.items()}


def encode_value(value: object) -> dict[str, object]:
    """Encode one plain Python value into its DynamoDB typed attribute-value form."""
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, Mapping):
        return {"M": encode_item(value)}
    if isinstance(value, (list, tuple)):
        return {"L": [encode_value(v) for v in value]}
    raise TypeError(f"cannot encode value of type {type(value).__name__} for DynamoDB")


def put_operation(
    table: str,
    item: Mapping[str, object],
    *,
    condition: str | None = None,
    values: Mapping[str, object] | None = None,
    names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build one raw ``Put`` transact-item operation for ``table``.

    ``table`` is a logical table name (one of :data:`crm.store.schema.TABLE_NAMES`);
    :meth:`crm.store.store.Store.transact_write` resolves it to the physical name.

    Args:
        table: Logical table name.
        item: The full item, as plain Python values.
        condition: Optional ``ConditionExpression``.
        values: ``ExpressionAttributeValues`` already in typed wire form (``{":v": {"S": "x"}}``).
        names: ``ExpressionAttributeNames``.
    """
    put: dict[str, Any] = {"TableName": table, "Item": encode_item(item)}
    if condition is not None:
        put["ConditionExpression"] = condition
    if values:
        put["ExpressionAttributeValues"] = dict(values)
    if names:
        put["ExpressionAttributeNames"] = dict(names)
    return {"Put": put}


def unchanged_since_read(item: Mapping[str, object]) -> str:
    """The condition that ``item``'s ``updated_at`` is still the value that was read.

    A read item with no string ``updated_at`` requires the attribute to be still absent or ``NULL``.
    """
    if isinstance(item.get("updated_at"), str):
        return "updated_at = :read_updated_at"
    return "(attribute_not_exists(updated_at) OR attribute_type(updated_at, :null_type))"


def unchanged_since_read_values(item: Mapping[str, object]) -> dict[str, object]:
    """The typed expression values :func:`unchanged_since_read` refers to."""
    read = item.get("updated_at")
    if isinstance(read, str):
        return {":read_updated_at": {"S": read}}
    return {":null_type": {"S": "NULL"}}


def raise_if_record_changed(
    error: Conflict, *, table: str, record_id: str, operation_index: int = 0
) -> None:
    """Raise a ``fields_changed`` :class:`Conflict` if ``error`` is the record condition failing.

    ``error`` is a :class:`Conflict` from :meth:`crm.store.store.Store.transact_write` whose
    operation at ``operation_index`` is the record's conditional write. Returns normally for any
    other cancellation or an idempotency mismatch, so the caller re-raises ``error`` unchanged.

    Raises:
        Conflict: ``reason="fields_changed"``; the record changed or vanished since it was read.
    """
    if _condition_failed_at(error, index=operation_index):
        raise Conflict(
            "The record changed after it was read. Refresh and try again.",
            reason="fields_changed",
            detail=f"table={table} record_id={bounded_detail_value(record_id)}",
        ) from error


def _condition_failed_at(error: Conflict, *, index: int) -> bool:
    """Whether ``error`` wraps a transaction cancelled by the condition on operation ``index``."""
    response = getattr(error.__cause__, "response", None)
    if not isinstance(response, dict):
        return False
    if response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return False
    reasons = response.get("CancellationReasons")
    if not isinstance(reasons, list) or len(reasons) <= index:
        return False
    reason = reasons[index]
    return isinstance(reason, dict) and reason.get("Code") == "ConditionalCheckFailed"
