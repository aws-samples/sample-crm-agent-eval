# SPDX-License-Identifier: MIT
"""DynamoDB attribute-value encoding for raw ``TransactWriteItems`` operations built by handlers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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


def put_operation(table: str, item: Mapping[str, object]) -> dict[str, Any]:
    """Build one raw ``Put`` transact-item operation for ``table``.

    ``table`` is a logical table name (one of :data:`crm.store.schema.TABLE_NAMES`);
    :meth:`crm.store.store.Store.transact_write` resolves it to the physical name.
    """
    return {"Put": {"TableName": table, "Item": encode_item(item)}}
