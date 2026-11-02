# SPDX-License-Identifier: MIT
"""Batched ``seed_tenant`` against DynamoDB Local equals a per-item ``put_item`` seed.

Two table sets are seeded with the same tenant: one by :func:`fixtures.generate.seed_tenant`
(``BatchWriteItem``), one by an item-at-a-time ``Store.put_item`` loop applying the same GSI
derivers. Their base-table item sets, their full ``gsi_tenant`` results and a sample of every
other GSI's partitions must match, and a re-seed must leave the batched set unchanged.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest
from fixtures.generate import _GSI_DERIVERS, generate_dataset, seed_tenant

from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLE_NAMES, table_schema
from crm.store.store import Store

_TENANT: Final[str] = "eval-batchseed-0001"
#: Distinct partition values queried per non-tenant GSI.
_PARTITIONS_PER_GSI: Final[int] = 5


@pytest.fixture
def per_item_store(dynamodb_local_client: Any) -> Iterator[Store]:
    """A second, freshly-created table set seeded item by item through ``Store.put_item``."""
    prefix = f"p{uuid.uuid4().hex[:12]}_"
    create_all_tables(dynamodb_local_client, table_prefix=prefix)
    store = Store(table_prefix=prefix, client=dynamodb_local_client)
    try:
        for table, items in generate_dataset(_TENANT).as_mapping().items():
            deriver = _GSI_DERIVERS[table]
            for item in items:
                store.put_item(table, {**item, **deriver(_TENANT, item)})
        yield store
    finally:
        for table in TABLE_NAMES:
            with contextlib.suppress(dynamodb_local_client.exceptions.ResourceNotFoundException):
                dynamodb_local_client.delete_table(TableName=f"{prefix}{table}")


def _canonical(items: list[dict[str, object]]) -> list[str]:
    return sorted(json.dumps(item, sort_keys=True, default=str) for item in items)


def _scan(client: Any, physical: str) -> list[str]:
    """Every raw item in ``physical``, canonicalised."""
    items: list[dict[str, object]] = []
    for page in client.get_paginator("scan").paginate(TableName=physical):
        items.extend(page["Items"])
    return _canonical(items)


def _query_all(store: Store, table: str, gsi: str, value: str) -> list[str]:
    items: list[dict[str, object]] = []
    cursor: dict[str, object] | None = None
    while True:
        page, cursor = store.query_gsi(
            table, gsi, partition_value=value, exclusive_start_key=cursor
        )
        items.extend(page)
        if cursor is None:
            return _canonical(items)


def _seeded_tables() -> list[str]:
    return sorted(generate_dataset(_TENANT).as_mapping())


def test_batched_seed_matches_a_per_item_seed_in_every_table_and_index(
    dynamodb_local_store: tuple[Store, str], per_item_store: Store, dynamodb_local_client: Any
) -> None:
    batched, _prefix = dynamodb_local_store
    seed_tenant(batched, tenant=_TENANT)

    for table in _seeded_tables():
        expected = _scan(dynamodb_local_client, per_item_store.physical_name(table))
        assert expected, f"{table}: the per-item seed wrote nothing"
        assert _scan(dynamodb_local_client, batched.physical_name(table)) == expected, table

        schema = table_schema(table)
        for gsi in schema.gsis.values():
            if gsi.partition_key == "tenant":
                values = [_TENANT]
            else:
                scanned = [json.loads(row) for row in expected]
                distinct = sorted(
                    {row[gsi.partition_key]["S"] for row in scanned if gsi.partition_key in row}
                )
                assert distinct, f"{table}.{gsi.name}: no seeded item carries its partition key"
                values = distinct[:_PARTITIONS_PER_GSI]
            for value in values:
                reference = _query_all(per_item_store, table, gsi.name, value)
                assert reference, f"{table}.{gsi.name}={value!r} returned nothing"
                assert _query_all(batched, table, gsi.name, value) == reference, (
                    table,
                    gsi.name,
                    value,
                )


def test_a_second_batched_seed_leaves_every_table_unchanged(
    dynamodb_local_store: tuple[Store, str], dynamodb_local_client: Any
) -> None:
    store, _prefix = dynamodb_local_store
    seed_tenant(store, tenant=_TENANT)
    first = {t: _scan(dynamodb_local_client, store.physical_name(t)) for t in _seeded_tables()}

    seed_tenant(store, tenant=_TENANT)

    second = {t: _scan(dynamodb_local_client, store.physical_name(t)) for t in _seeded_tables()}
    assert second == first
    assert all(first.values())
