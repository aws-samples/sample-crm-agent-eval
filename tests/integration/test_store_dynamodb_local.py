# SPDX-License-Identifier: MIT
"""`crm.store.store.Store` against DynamoDB Local.

Covers GSI projections, tenant-prefixed GSI isolation, `ConditionExpression` validity, and
`TransactWriteItems` acceptance and cancellation translation.
"""

from __future__ import annotations

import pytest

from crm.errors import Conflict
from crm.store.keys import (
    derive,
    derived_attribute_names,
    derived_attributes,
    tenant_prefixed,
)
from crm.store.schema import TABLES, TENANT_SCOPED_TABLES
from crm.store.store import Store


def _typed_attribute(value: object) -> dict[str, object]:
    """Encode one plain Python value into DynamoDB's typed attribute-value wire format.

    Supports ``bool``, numbers and ``str`` only.
    """
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    raise TypeError(f"unsupported sample value type: {type(value).__name__}")


def _typed_item(item: dict[str, object]) -> dict[str, dict[str, object]]:
    """Encode every attribute of ``item`` into DynamoDB's typed attribute-value wire format."""
    return {key: _typed_attribute(value) for key, value in item.items()}


def _sample_item(table: str, tenant: str, *, suffix: str = "1") -> dict[str, object]:
    """Build a minimal item for ``table`` carrying every attribute its GSIs project.

    Derived index keys are not set; their source attributes are populated and ``Store.put_item``
    derives the keys at write time.
    """
    schema = TABLES[table]
    derived = derived_attribute_names(table)
    item: dict[str, object] = {schema.partition_key: f"{table}-{suffix}", "tenant": tenant}
    if schema.sort_key is not None:
        item[schema.sort_key] = f"sort-{suffix}"
    for gsi in schema.gsis.values():
        if gsi.partition_key not in derived:
            item[gsi.partition_key] = _partition_value_for(gsi.partition_key, tenant, suffix)
        if gsi.sort_key is not None and gsi.sort_key not in item:
            item[gsi.sort_key] = f"sk-{suffix}"
        for attr in gsi.projected_attributes:
            item.setdefault(attr, f"{attr}-{suffix}")
    for spec in derived_attributes(table):
        for source in spec.sources:
            item.setdefault(source, f"{source}-{suffix}")
    return item


def _partition_value_for(attr: str, tenant: str, suffix: str) -> str:
    """Build the composite value a tenant-prefixed GSI partition key attribute holds."""
    if attr == "tenant":
        return tenant
    dimension = attr.removeprefix("tenant_")
    return tenant_prefixed(tenant, f"{dimension}-{suffix}")


# -- GSI projection proof, per table and per index ------------------------------------------------


@pytest.mark.parametrize(
    ("table", "gsi_name"),
    [(table, gsi_name) for table, schema in TABLES.items() for gsi_name in schema.gsis],
)
def test_every_declared_gsi_projects_its_declared_attributes(
    dynamodb_local_store: tuple[Store, str], table: str, gsi_name: str
) -> None:
    """A query against every declared GSI returns `tenant` and every attribute it declares as
    projected."""
    store, _ = dynamodb_local_store
    schema = TABLES[table]
    gsi = schema.gsi(gsi_name)
    tenant = "demo"
    item = _sample_item(table, tenant)
    store.put_item(table, item)

    # `put_item` recomputes derived index keys, so read the key from the derived item.
    partition_value = derive(table, item)[gsi.partition_key]
    assert isinstance(partition_value, str)
    items, _ = store.query_gsi(table, gsi_name, partition_value=partition_value)

    assert len(items) == 1
    returned = items[0]
    assert "tenant" in returned, f"{table}.{gsi_name} did not project 'tenant'"
    for attr in gsi.projected_attributes:
        assert attr in returned, f"{table}.{gsi_name} did not project declared attribute {attr!r}"


# -- Tenant isolation against the real engine -----------------------------------------------------


@pytest.mark.parametrize("table", sorted(TENANT_SCOPED_TABLES))
def test_tenant_prefixed_gsi_query_never_returns_another_tenants_items(
    dynamodb_local_store: tuple[Store, str], table: str
) -> None:
    """A `gsi_tenant` query for one tenant's composite key never surfaces another tenant's item."""
    store, _ = dynamodb_local_store
    demo_item = _sample_item(table, "demo", suffix="demo-1")
    eval_item = _sample_item(table, "eval-run-42", suffix="eval-1")
    store.put_item(table, demo_item)
    store.put_item(table, eval_item)

    demo_items, _ = store.query_tenant_scan(table, tenant="demo")
    eval_items, _ = store.query_tenant_scan(table, tenant="eval-run-42")

    schema = TABLES[table]
    assert {row[schema.partition_key] for row in demo_items} == {demo_item[schema.partition_key]}
    assert {row[schema.partition_key] for row in eval_items} == {eval_item[schema.partition_key]}


# -- ConditionExpression validity against the real engine -----------------------------------------


def test_put_idempotent_first_write_and_identical_replay(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """First call writes; an identical-payload replay under the same key is a no-op, not an error."""
    store, _ = dynamodb_local_store
    item = _sample_item("accounts", "demo")
    wrote_first = store.put_idempotent("accounts", item, transaction_key="txn-1")
    wrote_second = store.put_idempotent("accounts", item, transaction_key="txn-1")

    assert wrote_first is True
    assert wrote_second is False


def test_put_idempotent_conflict_uses_a_real_condition_check(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """A retry with the same key and a different payload is refused with `Conflict`."""
    store, _ = dynamodb_local_store
    account_id = "accounts-conflict-1"
    schema = TABLES["accounts"]
    base = {
        schema.partition_key: account_id,
        "tenant": "demo",
        "tenant_owner_rep_id": tenant_prefixed("demo", "rep-1"),
        "created_at": "2026-01-01T00:00:00Z",
        "owner_rep_id": "rep-1",
        "name": "Acme",
        "tenant_name_lower": "acme",
        "name_lower": "acme",
    }
    store.put_idempotent("accounts", base, transaction_key="txn-conflict")

    different_payload = {**base, "name": "Acme Renamed"}
    with pytest.raises(Conflict):
        store.put_idempotent("accounts", different_payload, transaction_key="txn-conflict")


# -- TransactWriteItems against the real engine ----------------------------------------------------


def test_transact_write_accepts_a_real_multi_table_body(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """A `TransactWriteItems` body spanning two tables, with logical table names, writes both rows.

    `transact_write` resolves each logical ``TableName`` to the store's prefixed physical name.
    """
    store, _prefix = dynamodb_local_store
    account = _sample_item("accounts", "demo", suffix="txn-a")
    contact = _sample_item("contacts", "demo", suffix="txn-c")

    store.transact_write(
        [
            {"Put": {"TableName": "accounts", "Item": _typed_item(account)}},
            {"Put": {"TableName": "contacts", "Item": _typed_item(contact)}},
        ]
    )

    assert store.get_item("accounts", {"account_id": str(account["account_id"])})
    assert store.get_item("contacts", {"contact_id": str(contact["contact_id"])})


def test_transact_write_translates_a_real_condition_check_cancellation(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """A transaction cancelled by a failed `ConditionCheck` raises the taxonomy `Conflict`."""
    store, _prefix = dynamodb_local_store
    account = _sample_item("accounts", "demo", suffix="txn-cancel")
    store.put_item("accounts", account)

    with pytest.raises(Conflict):
        store.transact_write(
            [
                {
                    "ConditionCheck": {
                        "TableName": "accounts",
                        "Key": {"account_id": {"S": str(account["account_id"])}},
                        "ConditionExpression": "attribute_not_exists(account_id)",
                    }
                },
                {
                    "Put": {
                        "TableName": "accounts",
                        "Item": _typed_item(
                            _sample_item("accounts", "demo", suffix="txn-cancel-2")
                        ),
                    }
                },
            ]
        )


def test_transact_write_accepts_a_long_transaction_key_and_a_replay_is_idempotent(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """A 37-128 character key is accepted, and replaying it does not apply the update twice.

    The update increments a counter, so a second application would leave it at 2.
    """
    import uuid

    store, _prefix = dynamodb_local_store
    account = _sample_item("accounts", "demo", suffix="txn-long")
    store.put_item("accounts", account)
    key = f"long-key-{uuid.uuid4().hex}-".ljust(120, "x")
    assert 37 <= len(key) <= 128
    increment = [
        {
            "Update": {
                "TableName": "accounts",
                "Key": {"account_id": {"S": str(account["account_id"])}},
                "UpdateExpression": "ADD replay_count :one",
                "ExpressionAttributeValues": {":one": {"N": "1"}},
            }
        }
    ]

    store.transact_write(increment, transaction_key=key)
    store.transact_write(increment, transaction_key=key)
    once = store.get_item("accounts", {"account_id": str(account["account_id"])})
    assert once is not None and int(str(once["replay_count"])) == 1

    store.transact_write(increment, transaction_key=key[:-1] + "y")
    twice = store.get_item("accounts", {"account_id": str(account["account_id"])})
    assert twice is not None and int(str(twice["replay_count"])) == 2
