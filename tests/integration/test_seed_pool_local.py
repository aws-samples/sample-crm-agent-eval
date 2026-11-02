# SPDX-License-Identifier: MIT
"""Pooled ``seed_tenant`` against DynamoDB Local writes the same rows as a sequential seed.

Two table sets are seeded with the same tenant: one with the default write pool, one with
``SEED_MAX_WORKERS`` forced to ``1``. Every seeded table's base-table rows must match. Each seed's
wall time is reported through ``record_property``; neither is asserted.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any, Final

import fixtures.generate as generate
import pytest
from fixtures.generate import generate_dataset, seed_tenant

from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLE_NAMES
from crm.store.store import Store

_TENANT: Final[str] = "eval-seedpool-0001"


@pytest.fixture
def sequential_store(dynamodb_local_client: Any) -> Iterator[Store]:
    """A second, freshly-created table set, left empty for the sequential seed."""
    prefix = f"s{uuid.uuid4().hex[:12]}_"
    create_all_tables(dynamodb_local_client, table_prefix=prefix)
    try:
        yield Store(table_prefix=prefix, client=dynamodb_local_client)
    finally:
        for table in TABLE_NAMES:
            with contextlib.suppress(dynamodb_local_client.exceptions.ResourceNotFoundException):
                dynamodb_local_client.delete_table(TableName=f"{prefix}{table}")


def _scan(client: Any, physical: str) -> list[str]:
    """Every raw item in ``physical``, canonicalised and sorted."""
    items: list[dict[str, object]] = []
    for page in client.get_paginator("scan").paginate(TableName=physical):
        items.extend(page["Items"])
    return sorted(json.dumps(item, sort_keys=True) for item in items)


def _timed(seed: Callable[[], None]) -> float:
    started = time.perf_counter()
    seed()
    return time.perf_counter() - started


def test_a_pooled_seed_writes_the_same_rows_as_a_sequential_seed(
    dynamodb_local_store: tuple[Store, str],
    sequential_store: Store,
    dynamodb_local_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    pooled, _prefix = dynamodb_local_store
    pooled_seconds = _timed(lambda: seed_tenant(pooled, tenant=_TENANT))
    with monkeypatch.context() as patch:
        patch.setattr(generate, "SEED_MAX_WORKERS", 1)
        sequential_seconds = _timed(lambda: seed_tenant(sequential_store, tenant=_TENANT))
    record_property("pooled_seed_seconds", round(pooled_seconds, 3))
    record_property("sequential_seed_seconds", round(sequential_seconds, 3))

    corpus = generate_dataset(_TENANT).as_mapping()
    for table, items in corpus.items():
        expected = _scan(dynamodb_local_client, sequential_store.physical_name(table))
        assert len(expected) == len(items), f"{table}: the sequential seed wrote a partial set"
        assert _scan(dynamodb_local_client, pooled.physical_name(table)) == expected, table
