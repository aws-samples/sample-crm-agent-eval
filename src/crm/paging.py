# SPDX-License-Identifier: MIT
"""Byte-bounded handler pages and the DynamoDB resume key that continues after a kept record.

A handler that returns a cursor-bearing record list passes it through :func:`bound_page`; when the
page is cut, it mints ``next_cursor`` from :func:`resume_key` of the last kept record and marks the
result ``truncated``, so following the cursor reaches every record exactly once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from crm.errors import CrmError
from crm.store.schema import table_schema

#: Largest serialised size, in bytes, of a handler's record list before the page is cut.
HANDLER_PAGE_BYTE_BUDGET: Final[int] = 28_672

_LIST_BRACKETS_BYTES: Final[int] = 2
_LIST_SEPARATOR_BYTES: Final[int] = 2


def _record_size(record: Mapping[str, object]) -> int:
    """The UTF-8 length of ``record``'s ``repr``."""
    return len(repr(record).encode("utf-8"))


def bound_page(
    records: Sequence[Mapping[str, object]], *, byte_budget: int = HANDLER_PAGE_BYTE_BUDGET
) -> tuple[list[dict[str, object]], bool]:
    """Keep the longest prefix of ``records`` whose list ``repr`` fits ``byte_budget`` bytes.

    The size is the UTF-8 length of the ``repr`` of the kept list. At least one record is always
    kept when ``records`` is non-empty, even one larger than the budget.

    Returns:
        ``(kept, cut)``: the kept records, in order, and whether any record was dropped.
    """
    kept: list[dict[str, object]] = []
    size = _LIST_BRACKETS_BYTES
    for record in records:
        added = _record_size(record) + (_LIST_SEPARATOR_BYTES if kept else 0)
        if kept and size + added > byte_budget:
            return kept, True
        kept.append(dict(record))
        size += added
    return kept, False


def resume_key(table: str, index_name: str, item: Mapping[str, object]) -> dict[str, object]:
    """Build the ``ExclusiveStartKey`` that resumes ``index_name`` immediately after ``item``.

    Raises:
        CrmError: If ``item`` lacks one of the table or index key attributes.
    """
    schema = table_schema(table)
    gsi = schema.gsi(index_name)
    names = {schema.partition_key, gsi.partition_key}
    names.update(name for name in (schema.sort_key, gsi.sort_key) if name is not None)
    key: dict[str, object] = {}
    for name in sorted(names):
        value = item.get(name)
        if isinstance(value, str):
            key[name] = {"S": value}
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            key[name] = {"N": str(value)}
        else:
            raise CrmError(
                "The request could not be completed.",
                detail=f"search resume key: {table}.{index_name} item lacks {name}",
            )
    return key
