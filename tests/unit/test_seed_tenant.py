# SPDX-License-Identifier: MIT
"""Unit tests for ``fixtures.generate.seed_tenant``.

Each test seeds a moto-mocked DynamoDB and asserts the seeded rows are reachable through the GSI
queries the application runs, not merely present in a scan.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from fixtures.generate import generate_dataset, seed_tenant
from moto import mock_aws

from crm.store.keys import tenant_prefixed
from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLE_NAMES
from crm.store.store import Store

_TABLE_PREFIX = "seedtest-"


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    """A moto-mocked DynamoDB client with every entity table created, zero credentials/cost."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client, table_prefix=_TABLE_PREFIX)
        yield client


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    """A prefixed :class:`Store` over the mocked client."""
    return Store(table_prefix=_TABLE_PREFIX, client=dynamodb_client)


def test_seed_tenant_is_exposed_as_the_pinned_entry_point() -> None:
    """``fixtures.generate.seed_tenant`` exists and is callable, which the harness CLI path needs."""
    assert callable(seed_tenant)


def test_seeded_accounts_are_reachable_through_their_owner_gsi(store: Store) -> None:
    """An account is retrievable via ``gsi_owner`` on ``tenant_owner_rep_id``."""
    tenant = "eval-seedtest-001"
    seed_tenant(store, tenant=tenant)

    corpus = generate_dataset(tenant)
    sample_account = corpus.accounts[0]
    owner_rep_id = str(sample_account["owner_rep_id"])
    account_id = str(sample_account["account_id"])

    items, _ = store.query_gsi(
        "accounts",
        "gsi_owner",
        partition_value=tenant_prefixed(tenant, owner_rep_id),
    )
    returned_ids = {str(item["account_id"]) for item in items}
    assert account_id in returned_ids


def test_seeded_contacts_are_reachable_through_their_account_gsi(store: Store) -> None:
    """A contact is retrievable via ``gsi_account`` on ``tenant_account_id``."""
    tenant = "eval-seedtest-002"
    seed_tenant(store, tenant=tenant)

    corpus = generate_dataset(tenant)
    sample_contact = corpus.contacts[0]
    account_id = str(sample_contact["account_id"])
    contact_id = str(sample_contact["contact_id"])

    items, _ = store.query_gsi(
        "contacts",
        "gsi_account",
        partition_value=tenant_prefixed(tenant, account_id),
    )
    returned_ids = {str(item["contact_id"]) for item in items}
    assert contact_id in returned_ids


def test_seeded_opportunities_are_reachable_through_the_stage_gsi(store: Store) -> None:
    """An opportunity is retrievable via ``gsi_stage`` on the derived ``tenant_stage`` attribute."""
    tenant = "eval-seedtest-003"
    seed_tenant(store, tenant=tenant)

    corpus = generate_dataset(tenant)
    sample_opportunity = corpus.opportunities[0]
    stage = str(sample_opportunity["stage"])
    opportunity_id = str(sample_opportunity["opportunity_id"])

    items, _ = store.query_gsi(
        "opportunities",
        "gsi_stage",
        partition_value=tenant_prefixed(tenant, stage),
    )
    returned_ids = {str(item["opportunity_id"]) for item in items}
    assert opportunity_id in returned_ids


def test_seeded_opportunities_are_reachable_through_the_owner_stage_gsi(store: Store) -> None:
    """An opportunity with a ``close_date`` is retrievable via ``gsi_owner_stage``."""
    tenant = "eval-seedtest-004"
    seed_tenant(store, tenant=tenant)

    corpus = generate_dataset(tenant)
    with_close_date = [o for o in corpus.opportunities if o.get("close_date")]
    assert with_close_date, "fixture corpus must contain at least one opportunity with a close_date"
    sample_opportunity = with_close_date[0]
    owner_rep_id = str(sample_opportunity["owner_rep_id"])
    opportunity_id = str(sample_opportunity["opportunity_id"])

    items, _ = store.query_gsi(
        "opportunities",
        "gsi_owner_stage",
        partition_value=tenant_prefixed(tenant, owner_rep_id),
    )
    returned_ids = {str(item["opportunity_id"]) for item in items}
    assert opportunity_id in returned_ids


def test_seeded_activities_are_reachable_through_the_opportunity_gsi(store: Store) -> None:
    """An activity is retrievable via ``gsi_opportunity`` on ``tenant_opportunity_id``."""
    tenant = "eval-seedtest-005"
    seed_tenant(store, tenant=tenant)

    corpus = generate_dataset(tenant)
    assert corpus.activities, "fixture corpus must contain at least one activity"
    sample_activity = corpus.activities[0]
    opportunity_id = str(sample_activity["opportunity_id"])
    activity_id = str(sample_activity["activity_id"])

    items, _ = store.query_gsi(
        "activities",
        "gsi_opportunity",
        partition_value=tenant_prefixed(tenant, opportunity_id),
    )
    returned_ids = {str(item["activity_id"]) for item in items}
    assert activity_id in returned_ids


def test_seeded_notes_are_reachable_through_the_opportunity_gsi(store: Store) -> None:
    """A note is retrievable via ``gsi_opportunity`` on ``tenant_opportunity_id``."""
    tenant = "eval-seedtest-006"
    seed_tenant(store, tenant=tenant)

    corpus = generate_dataset(tenant)
    assert corpus.notes, "fixture corpus must contain at least one note"
    sample_note = corpus.notes[0]
    opportunity_id = str(sample_note["opportunity_id"])
    note_id = str(sample_note["note_id"])

    items, _ = store.query_gsi(
        "notes",
        "gsi_opportunity",
        partition_value=tenant_prefixed(tenant, opportunity_id),
    )
    returned_ids = {str(item["note_id"]) for item in items}
    assert note_id in returned_ids


def test_seeded_corpus_is_reachable_through_the_tenant_wide_gsi_for_every_table(
    store: Store,
) -> None:
    """Every table's ``gsi_tenant`` returns the full seeded count for the tenant."""
    tenant = "eval-seedtest-007"
    seed_tenant(store, tenant=tenant)
    corpus = generate_dataset(tenant)

    for table, items in corpus.as_mapping().items():
        returned, cursor = store.query_tenant_scan(table, tenant=tenant)
        all_returned = list(returned)
        while cursor is not None:
            more, cursor = store.query_tenant_scan(table, tenant=tenant, exclusive_start_key=cursor)
            all_returned.extend(more)
        assert len(all_returned) == len(items), (
            f"table={table!r} expected {len(items)} reachable items via gsi_tenant, "
            f"got {len(all_returned)}"
        )


def test_seeding_is_idempotent_under_a_second_call(store: Store) -> None:
    """Calling ``seed_tenant`` twice for the same tenant leaves item counts unchanged."""
    tenant = "eval-seedtest-008"
    seed_tenant(store, tenant=tenant)
    corpus = generate_dataset(tenant)

    first_pass_counts: dict[str, int] = {}
    for table in corpus.as_mapping():
        items, cursor = store.query_tenant_scan(table, tenant=tenant)
        all_items = list(items)
        while cursor is not None:
            more, cursor = store.query_tenant_scan(table, tenant=tenant, exclusive_start_key=cursor)
            all_items.extend(more)
        first_pass_counts[table] = len(all_items)

    seed_tenant(store, tenant=tenant)

    for table in corpus.as_mapping():
        items, cursor = store.query_tenant_scan(table, tenant=tenant)
        all_items = list(items)
        while cursor is not None:
            more, cursor = store.query_tenant_scan(table, tenant=tenant, exclusive_start_key=cursor)
            all_items.extend(more)
        assert len(all_items) == first_pass_counts[table], (
            f"table={table!r} item count changed after a second seed_tenant call"
        )


def test_seeding_one_tenant_does_not_touch_another_tenant(store: Store) -> None:
    """Two tenants seeded into the same tables never see each other's rows via ``gsi_tenant``."""
    tenant_a = "eval-seedtest-009a"
    tenant_b = "eval-seedtest-009b"
    seed_tenant(store, tenant=tenant_a)
    seed_tenant(store, tenant=tenant_b)

    items_a, _ = store.query_tenant_scan("accounts", tenant=tenant_a)
    items_b, _ = store.query_tenant_scan("accounts", tenant=tenant_b)

    ids_a = {str(item["account_id"]) for item in items_a}
    ids_b = {str(item["account_id"]) for item in items_b}
    assert ids_a.isdisjoint(ids_b)
    for item in items_a:
        assert item["tenant"] == tenant_a
    for item in items_b:
        assert item["tenant"] == tenant_b


def test_seed_tenant_writes_every_table_the_generator_produces(store: Store) -> None:
    """Every table name in ``GeneratedCorpus.as_mapping`` is one this store recognises."""
    tenant = "eval-seedtest-010"
    corpus = generate_dataset(tenant)
    assert set(corpus.as_mapping()).issubset(TABLE_NAMES)
    seed_tenant(store, tenant=tenant)


def test_a_second_seed_leaves_every_seeded_item_identical(
    store: Store, dynamodb_client: object
) -> None:
    """Re-seeding overwrites each item with the same content: no attribute changes or appears."""
    tenant = "eval-seedtest-011"
    tables = sorted(generate_dataset(tenant).as_mapping())

    def snapshot() -> dict[str, list[str]]:
        client: Any = dynamodb_client
        rows: dict[str, list[str]] = {}
        for table in tables:
            items: list[object] = []
            for page in client.get_paginator("scan").paginate(TableName=store.physical_name(table)):
                items.extend(page["Items"])
            rows[table] = sorted(json.dumps(item, sort_keys=True) for item in items)
        return rows

    seed_tenant(store, tenant=tenant)
    first = snapshot()
    seed_tenant(store, tenant=tenant)
    assert snapshot() == first
    assert all(first.values())


def test_seed_tenant_refuses_the_canary_tenant() -> None:
    """``eval.tenancy.seed_tenant`` refuses ``canary`` before any seeder runs."""
    from eval.tenancy import CANARY_TENANT
    from eval.tenancy import seed_tenant as eval_seed_tenant

    calls: list[str] = []

    def seeder(_store: Store, *, tenant: str) -> None:
        calls.append(tenant)

    with pytest.raises(ValueError, match="canary"):
        eval_seed_tenant(Store(client=object()), tenant=CANARY_TENANT, seeder=seeder)
    assert calls == []
