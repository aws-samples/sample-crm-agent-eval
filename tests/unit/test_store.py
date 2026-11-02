# SPDX-License-Identifier: MIT
"""Tests for crm.store: schema declarations, key construction, and the Store class.

Runs against a `moto`-mocked DynamoDB client.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.authz import filter_to_scope
from crm.errors import Conflict, ValidationFailed
from crm.store import keys
from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLE_NAMES, TABLES, TENANT_SCOPED_TABLES, table_schema
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    """A moto-mocked DynamoDB client — no credentials, no network, no cost."""
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    """A Store backed by the mocked client, with every declared table created."""
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


# -- schema declarations -----------------------------------------------------------------------


def test_eleven_tables_declared() -> None:
    """Every declared entity has a table."""
    assert len(TABLE_NAMES) == 11
    assert {
        "accounts",
        "contacts",
        "opportunities",
        "activities",
        "notes",
        "proposals",
        "audit_records",
        "usage_records",
        "reps",
        "eval_runs",
        "eval_results",
    } == TABLE_NAMES


@pytest.mark.parametrize("table", sorted(TENANT_SCOPED_TABLES))
def test_every_tenant_scoped_table_has_a_gsi_tenant_index(table: str) -> None:
    """Full-tenant scan (search, teardown) requires a tenant-wide index on every scoped table."""
    schema = table_schema(table)
    assert "gsi_tenant" in schema.gsis
    assert schema.gsis["gsi_tenant"].partition_key == "tenant"


@pytest.mark.parametrize(
    ("table", "gsi_name"),
    [
        (table, gsi_name)
        for table, schema in TABLES.items()
        for gsi_name in schema.gsis
        if gsi_name != "gsi_tenant" and table in TENANT_SCOPED_TABLES
    ],
)
def test_every_non_tenant_gsi_partition_key_is_tenant_prefixed(table: str, gsi_name: str) -> None:
    """On every tenant-scoped table, every non-scan GSI partition key is a `tenant_<dimension>`.

    ``eval_runs``/``eval_results`` are catalog tables and are exempt.
    """
    gsi = table_schema(table).gsi(gsi_name)
    assert gsi.partition_key.startswith("tenant_") or gsi.partition_key == "tenant"


@pytest.mark.parametrize(
    ("table", "gsi_name"),
    [(table, gsi_name) for table, schema in TABLES.items() for gsi_name in schema.gsis],
)
def test_every_gsi_projects_tenant(table: str, gsi_name: str) -> None:
    """Every GSI a query path uses projects `tenant`."""
    gsi = table_schema(table).gsi(gsi_name)
    assert "tenant" in gsi.projected_attributes


def test_gsi_construction_rejects_a_projection_missing_tenant() -> None:
    """A GSI declaration that does not project `tenant` cannot be constructed."""
    from crm.store.schema import GlobalSecondaryIndex

    with pytest.raises(ValueError, match="must project 'tenant'"):
        GlobalSecondaryIndex(
            name="gsi_bad",
            partition_key="tenant_account_id",
            sort_key="created_at",
            projected_attributes=frozenset({"account_id"}),
        )


def test_table_schema_rejects_unrecognised_name() -> None:
    with pytest.raises(ValueError, match="unrecognised table"):
        table_schema("widgets")


def test_gsi_lookup_rejects_unrecognised_index_name() -> None:
    with pytest.raises(ValueError, match="no GSI named"):
        table_schema("accounts").gsi("gsi_nonexistent")


# -- key construction --------------------------------------------------------------------------


def test_tenant_prefixed_builds_composite_value() -> None:
    assert keys.tenant_prefixed("demo", "rep-1") == "demo#rep-1"
    assert keys.tenant_prefixed("eval-run-42", "acc-1") == "eval-run-42#acc-1"


def test_tenant_prefixed_supports_multiple_dimension_parts() -> None:
    assert keys.tenant_prefixed("demo", "acc-1", "created") == "demo#acc-1#created"


@pytest.mark.parametrize("bad_args", [("",), ()])
def test_tenant_prefixed_rejects_empty_tenant_or_no_parts(bad_args: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        keys.tenant_prefixed("demo" if bad_args else "", *bad_args)


def test_tenant_prefixed_rejects_empty_part() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        keys.tenant_prefixed("demo", "")


def test_stage_close_date_builds_composite_sort_key() -> None:
    assert keys.stage_close_date("Negotiation", "2026-03-01") == "Negotiation#2026-03-01"


def test_run_id_task_id_builds_composite_partition_key() -> None:
    assert keys.run_id_task_id("run-1", "task-7") == "run-1#task-7"


def test_split_tenant_prefixed_reverses_tenant_prefixed() -> None:
    composite = keys.tenant_prefixed("eval-run-1", "acc-9")
    tenant, remainder = keys.split_tenant_prefixed(composite)
    assert tenant == "eval-run-1"
    assert remainder == "acc-9"


def test_split_tenant_prefixed_rejects_a_value_with_no_separator() -> None:
    with pytest.raises(ValueError, match="not a tenant-prefixed"):
        keys.split_tenant_prefixed("no-separator-here")


# -- Store: single-item operations -------------------------------------------------------------


def test_put_then_get_round_trips_an_item(store: Store) -> None:
    item = {
        "account_id": "acc-1",
        "tenant": "demo",
        "tenant_owner_rep_id": keys.tenant_prefixed("demo", "rep-1"),
        "name": "Acme Corp",
        "name_lower": "acme corp",
        "owner_rep_id": "rep-1",
        "created_at": "2026-01-01T00:00:00Z",
    }
    store.put_item("accounts", item)
    fetched = store.get_item("accounts", {"account_id": "acc-1"})
    assert fetched is not None
    assert fetched["name"] == "Acme Corp"
    assert fetched["tenant"] == "demo"


def test_get_item_returns_none_for_absent_key(store: Store) -> None:
    assert store.get_item("accounts", {"account_id": "does-not-exist"}) is None


def test_delete_item_removes_the_item(store: Store) -> None:
    item = {"account_id": "acc-del", "tenant": "demo", "name": "ToDelete", "name_lower": "todelete"}
    store.put_item("accounts", item)
    assert store.get_item("accounts", {"account_id": "acc-del"}) is not None
    store.delete_item("accounts", {"account_id": "acc-del"})
    assert store.get_item("accounts", {"account_id": "acc-del"}) is None


def test_unrecognised_table_name_raises_before_any_call(store: Store) -> None:
    with pytest.raises(ValueError, match="unrecognised table"):
        store.get_item("widgets", {"widget_id": "w-1"})


def test_item_round_trips_nested_and_numeric_and_boolean_and_null_values(store: Store) -> None:
    item = {
        "opportunity_id": "opp-nested",
        "tenant": "demo",
        "value": 12500.50,
        "count": 3,
        "is_open": True,
        "primary_contact_id": None,
        "tags": ["a", "b"],
        "meta": {"source": "referral", "score": 7},
    }
    store.put_item("opportunities", item)
    fetched = store.get_item("opportunities", {"opportunity_id": "opp-nested"})
    assert fetched == item


# -- Store: GSI queries -------------------------------------------------------------------------


def _seed_account(store: Store, *, tenant: str, account_id: str, owner_rep_id: str) -> None:
    store.put_item(
        "accounts",
        {
            "account_id": account_id,
            "tenant": tenant,
            "tenant_owner_rep_id": keys.tenant_prefixed(tenant, owner_rep_id),
            "owner_rep_id": owner_rep_id,
            "name": f"Account {account_id}",
            "name_lower": f"account {account_id}".lower(),
            "created_at": "2026-01-01T00:00:00Z",
        },
    )


def test_query_gsi_returns_only_items_matching_the_partition_value(store: Store) -> None:
    _seed_account(store, tenant="demo", account_id="acc-1", owner_rep_id="rep-1")
    _seed_account(store, tenant="demo", account_id="acc-2", owner_rep_id="rep-2")

    items, cursor = store.query_gsi(
        "accounts", "gsi_owner", partition_value=keys.tenant_prefixed("demo", "rep-1")
    )
    assert cursor is None
    assert [item["account_id"] for item in items] == ["acc-1"]


def test_query_gsi_cannot_cross_a_tenant_boundary_by_construction(store: Store) -> None:
    """Querying one tenant's composite key value never returns another tenant's items."""
    _seed_account(store, tenant="demo", account_id="acc-demo", owner_rep_id="rep-1")
    _seed_account(store, tenant="eval-run-9", account_id="acc-eval", owner_rep_id="rep-1")

    demo_items, _ = store.query_gsi(
        "accounts", "gsi_owner", partition_value=keys.tenant_prefixed("demo", "rep-1")
    )
    eval_items, _ = store.query_gsi(
        "accounts", "gsi_owner", partition_value=keys.tenant_prefixed("eval-run-9", "rep-1")
    )
    assert [item["account_id"] for item in demo_items] == ["acc-demo"]
    assert [item["account_id"] for item in eval_items] == ["acc-eval"]


def test_query_gsi_result_survives_filter_to_scope_because_tenant_is_projected(
    store: Store,
) -> None:
    """`crm.authz.filter_to_scope` keeps a row read straight off a GSI query result."""
    from crm.authz import Identity

    _seed_account(store, tenant="demo", account_id="acc-1", owner_rep_id="rep-1")
    items, _ = store.query_gsi(
        "accounts", "gsi_owner", partition_value=keys.tenant_prefixed("demo", "rep-1")
    )
    identity = Identity(rep_id="rep-1", full_visibility=True, tenant="demo")
    # account_id_of requires an account_id key; accounts are scoped by their own id.
    scoped = [dict(item, account_id=item["account_id"]) for item in items]
    assert filter_to_scope(identity, scoped) == scoped


def test_query_gsi_with_sort_key_prefix_filters_by_begins_with(store: Store) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "stage": "Negotiation",
            "close_date": "2026-05-01",
        },
    )
    store.put_item(
        "opportunities",
        {
            "opportunity_id": "opp-2",
            "tenant": "demo",
            "stage": "Negotiation",
            "close_date": "2026-06-01",
        },
    )
    items, _ = store.query_gsi(
        "opportunities",
        "gsi_stage",
        partition_value=keys.tenant_prefixed("demo", "Negotiation"),
        sort_key_prefix="2026-05",
    )
    assert [item["opportunity_id"] for item in items] == ["opp-1"]


def test_query_gsi_respects_limit_and_returns_a_resumable_cursor(store: Store) -> None:
    for i in range(5):
        _seed_account(store, tenant="demo", account_id=f"acc-{i}", owner_rep_id="rep-x")

    first_page, cursor = store.query_gsi(
        "accounts", "gsi_owner", partition_value=keys.tenant_prefixed("demo", "rep-x"), limit=2
    )
    assert len(first_page) == 2
    assert cursor is not None

    second_page, _ = store.query_gsi(
        "accounts",
        "gsi_owner",
        partition_value=keys.tenant_prefixed("demo", "rep-x"),
        limit=2,
        exclusive_start_key=cursor,
    )
    assert len(second_page) == 2
    first_ids = {item["account_id"] for item in first_page}
    second_ids = {item["account_id"] for item in second_page}
    assert first_ids.isdisjoint(second_ids)


def test_query_gsi_rejects_unrecognised_index(store: Store) -> None:
    with pytest.raises(ValueError, match="no GSI named"):
        store.query_gsi("accounts", "gsi_nonexistent", partition_value="demo#rep-1")


def test_query_tenant_scan_returns_every_item_under_the_tenant(store: Store) -> None:
    _seed_account(store, tenant="demo", account_id="acc-1", owner_rep_id="rep-1")
    _seed_account(store, tenant="demo", account_id="acc-2", owner_rep_id="rep-2")
    _seed_account(store, tenant="eval-run-1", account_id="acc-3", owner_rep_id="rep-1")

    items, _ = store.query_tenant_scan("accounts", tenant="demo")
    assert {item["account_id"] for item in items} == {"acc-1", "acc-2"}


def test_query_tenant_scan_rejects_a_table_with_no_tenant_wide_index(store: Store) -> None:
    with pytest.raises(ValueError, match="no tenant-wide scan index"):
        store.query_tenant_scan("eval_runs", tenant="demo")


# -- Store: idempotent writes ---------------------------------------------------------------------


def test_put_idempotent_first_call_writes_and_returns_true(store: Store) -> None:
    item = {"account_id": "acc-idem", "tenant": "demo", "name": "First"}
    wrote = store.put_idempotent("accounts", item, transaction_key="txn-1")
    assert wrote is True
    fetched = store.get_item("accounts", {"account_id": "acc-idem"})
    assert fetched is not None
    assert fetched["name"] == "First"


def test_put_idempotent_retry_with_identical_payload_is_a_noop(store: Store) -> None:
    item = {"account_id": "acc-idem2", "tenant": "demo", "name": "Same"}
    first = store.put_idempotent("accounts", item, transaction_key="txn-2")
    second = store.put_idempotent("accounts", item, transaction_key="txn-2")
    assert first is True
    assert second is False
    fetched = store.get_item("accounts", {"account_id": "acc-idem2"})
    assert fetched is not None
    assert fetched["name"] == "Same"


def test_put_idempotent_retry_with_different_payload_under_same_key_is_a_conflict(
    store: Store,
) -> None:
    store.put_idempotent(
        "accounts",
        {"account_id": "acc-idem3", "tenant": "demo", "name": "First"},
        transaction_key="txn-3",
    )
    with pytest.raises(Conflict):
        store.put_idempotent(
            "accounts",
            {"account_id": "acc-idem3", "tenant": "demo", "name": "Different"},
            transaction_key="txn-3",
        )


def test_put_idempotent_rejects_an_empty_transaction_key(store: Store) -> None:
    with pytest.raises(ValidationFailed):
        store.put_idempotent(
            "accounts", {"account_id": "acc-idem4", "tenant": "demo"}, transaction_key=""
        )


def test_put_idempotent_different_keys_both_succeed(store: Store) -> None:
    first = store.put_idempotent(
        "accounts",
        {"account_id": "acc-idem5", "tenant": "demo", "name": "A"},
        transaction_key="txn-a",
    )
    # A different transaction key over the same primary key is an ordinary overwrite.
    second = store.put_idempotent(
        "accounts",
        {"account_id": "acc-idem5", "tenant": "demo", "name": "B"},
        transaction_key="txn-b",
    )
    assert first is True
    assert second is True
    fetched = store.get_item("accounts", {"account_id": "acc-idem5"})
    assert fetched is not None
    assert fetched["name"] == "B"


# -- Store: transactional writes -----------------------------------------------------------------


def test_transact_write_applies_multiple_puts_atomically(store: Store) -> None:
    store.transact_write(
        [
            {
                "Put": {
                    "TableName": "opportunities",
                    "Item": {
                        "opportunity_id": {"S": "opp-txn"},
                        "tenant": {"S": "demo"},
                        "last_touch": {"S": "2026-02-01T00:00:00Z"},
                    },
                }
            },
            {
                "Put": {
                    "TableName": "activities",
                    "Item": {
                        "activity_id": {"S": "act-1"},
                        "tenant": {"S": "demo"},
                        "opportunity_id": {"S": "opp-txn"},
                    },
                }
            },
        ]
    )
    opp = store.get_item("opportunities", {"opportunity_id": "opp-txn"})
    act = store.get_item("activities", {"activity_id": "act-1"})
    assert opp is not None and opp["last_touch"] == "2026-02-01T00:00:00Z"
    assert act is not None and act["opportunity_id"] == "opp-txn"


def test_transact_write_rejects_an_empty_operation_list(store: Store) -> None:
    with pytest.raises(ValidationFailed):
        store.transact_write([])


def test_transact_write_condition_failure_raises_conflict(store: Store) -> None:
    store.put_item("proposals", {"proposal_id": "prop-1", "tenant": "demo", "status": "pending"})
    with pytest.raises(Conflict):
        store.transact_write(
            [
                {
                    "Update": {
                        "TableName": "proposals",
                        "Key": {"proposal_id": {"S": "prop-1"}},
                        "UpdateExpression": "SET #s = :new, tenant_status = :ts",
                        "ConditionExpression": "#s = :expected",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {
                            ":new": {"S": "approved"},
                            ":ts": {"S": "demo#approved"},
                            ":expected": {"S": "already-decided"},
                        },
                    }
                }
            ]
        )


def test_transact_write_resolves_every_operations_table_name_to_the_physical_name(
    dynamodb_client: Any,
) -> None:
    """``transact_write`` resolves each operation's logical ``TableName`` to the prefixed name.

    The store has a non-empty ``table_prefix`` and only prefixed tables exist.
    """
    create_all_tables(dynamodb_client, table_prefix="envA-")
    prefixed_store = Store(table_prefix="envA-", client=dynamodb_client)

    prefixed_store.transact_write(
        [
            {
                "Put": {
                    "TableName": "opportunities",
                    "Item": {
                        "opportunity_id": {"S": "opp-prefixed"},
                        "tenant": {"S": "demo"},
                        "last_touch": {"S": "2026-02-01T00:00:00Z"},
                    },
                }
            }
        ]
    )

    # Read back through the Store's own prefix-resolving get_item.
    fetched = prefixed_store.get_item("opportunities", {"opportunity_id": "opp-prefixed"})
    assert fetched is not None
    assert fetched["last_touch"] == "2026-02-01T00:00:00Z"

    # The physical table itself carries the prefix.
    raw = dynamodb_client.get_item(
        TableName="envA-opportunities", Key={"opportunity_id": {"S": "opp-prefixed"}}
    )
    assert "Item" in raw


def test_transact_write_rejects_an_unrecognised_table_name_before_any_network_call(
    store: Store,
) -> None:
    """An operation naming a table outside :data:`crm.store.schema.TABLE_NAMES` raises
    ``ValueError`` before reaching DynamoDB.
    """
    with pytest.raises(ValueError, match="unrecognised table"):
        store.transact_write(
            [{"Put": {"TableName": "not_a_real_table", "Item": {"id": {"S": "x"}}}}]
        )


def test_transact_write_rejects_an_operation_with_no_recognised_action_key(store: Store) -> None:
    """An operation dict carrying none of ``Put``/``Update``/``Delete``/``ConditionCheck`` raises
    before any network call.
    """
    with pytest.raises(ValueError, match="none of"):
        store.transact_write([{"SomeOtherAction": {"TableName": "opportunities"}}])


# -- Store: scoped teardown -----------------------------------------------------------------------


def test_delete_tenant_removes_exactly_that_tenants_items_and_nothing_else(store: Store) -> None:
    _seed_account(store, tenant="eval-run-5", account_id="acc-a", owner_rep_id="rep-1")
    _seed_account(store, tenant="eval-run-5", account_id="acc-b", owner_rep_id="rep-2")
    _seed_account(store, tenant="demo", account_id="acc-demo", owner_rep_id="rep-1")
    store.put_item(
        "contacts",
        {
            "contact_id": "con-1",
            "tenant": "eval-run-5",
            "account_id": "acc-a",
            "name": "Contact",
            "name_lower": "contact",
        },
    )

    deleted = store.delete_tenant("eval-run-5")

    assert deleted["accounts"] == 2
    assert deleted["contacts"] == 1
    assert store.get_item("accounts", {"account_id": "acc-a"}) is None
    assert store.get_item("accounts", {"account_id": "acc-b"}) is None
    assert store.get_item("contacts", {"contact_id": "con-1"}) is None
    # The demo tenant is untouched.
    assert store.get_item("accounts", {"account_id": "acc-demo"}) is not None


def test_delete_tenant_reports_zero_for_tables_with_no_matching_items(store: Store) -> None:
    _seed_account(store, tenant="eval-run-6", account_id="acc-only", owner_rep_id="rep-1")
    deleted = store.delete_tenant("eval-run-6")
    assert deleted["accounts"] == 1
    assert deleted["notes"] == 0
    assert deleted["proposals"] == 0


def test_delete_tenant_covers_every_tenant_scoped_table(store: Store) -> None:
    deleted = store.delete_tenant("eval-run-empty")
    assert set(deleted) == TENANT_SCOPED_TABLES


def test_delete_tenant_handles_more_items_than_one_batch_write_chunk(store: Store) -> None:
    """Teardown chunks deletes into BatchWriteItem calls of at most 25 requests."""
    tenant = "eval-run-bulk"
    for i in range(60):
        _seed_account(store, tenant=tenant, account_id=f"acc-{i}", owner_rep_id="rep-bulk")

    deleted = store.delete_tenant(tenant)

    assert deleted["accounts"] == 60
    remaining, _ = store.query_tenant_scan("accounts", tenant=tenant)
    assert remaining == []


class TestProvisionedProjectionsMatchTheDeclaration:
    """`create_all_tables` builds exactly the projection `schema.py` declares, not a wider one."""

    @staticmethod
    def _projection(client: object, table: str, index: str) -> dict[str, object]:
        described = client.describe_table(TableName=table)["Table"]  # type: ignore[attr-defined]
        for gsi in described.get("GlobalSecondaryIndexes", []):
            if gsi["IndexName"] == index:
                return dict(gsi["Projection"])
        raise AssertionError(f"{table} has no index {index}")

    def test_an_all_keys_projection_is_keys_only_and_not_all(self, dynamodb_client: object) -> None:
        """`proposals.gsi_tenant` declares only key attributes, so its projection is KEYS_ONLY."""
        create_all_tables(dynamodb_client)
        projection = self._projection(dynamodb_client, "proposals", "gsi_tenant")

        assert projection["ProjectionType"] == "KEYS_ONLY"

    def test_an_unprojected_attribute_really_is_absent_from_a_query(self, store: Store) -> None:
        """A query on a KEYS_ONLY index returns no unprojected attribute."""
        store.put_item(
            "proposals",
            {
                "proposal_id": "prop-projection-1",
                "tenant": "t-proj",
                "status": "pending",
                "target_table": "opportunities",
                "target_id": "opp-1",
                "actor_rep_id": "rep-1",
                "operation": "update",
                "reason": "r",
                "created_at": "2026-01-01T00:00:00Z",
                "before_snapshot": {"tenant": "t-proj", "account_id": "acct-1"},
            },
        )

        rows, _ = store.query_tenant_scan("proposals", tenant="t-proj")

        assert len(rows) == 1, (
            "the row must be found — otherwise this asserts nothing about attributes"
        )
        assert "before_snapshot" not in rows[0], (
            "gsi_tenant projects keys only, so the snapshot must be absent here; its presence means "
            "the provisioner widened the projection and every projection-sensitive test is unsound"
        )

    def test_a_declared_include_projection_is_preserved(self, dynamodb_client: object) -> None:
        """An index that declares non-key attributes is INCLUDE, carrying exactly them."""
        create_all_tables(dynamodb_client)
        projection = self._projection(dynamodb_client, "proposals", "gsi_status")

        assert projection["ProjectionType"] == "INCLUDE"
        non_key = projection["NonKeyAttributes"]
        assert isinstance(non_key, list)
        assert "status" in non_key


class TestClientRequestTokenDerivation:
    """`transact_write` sends a deterministic 36-character token derived from the key."""

    @pytest.mark.parametrize("length", [1, 36, 37, 64, 128])
    def test_every_key_length_maps_to_a_36_character_token(self, length: int) -> None:
        from crm.store.store import client_request_token

        token = client_request_token("k" * length)
        assert len(token) == 36

    def test_the_same_key_gives_the_same_token(self) -> None:
        from crm.store.store import client_request_token

        key = "approve-" + "x" * 100
        assert client_request_token(key) == client_request_token(key)

    def test_different_keys_give_different_tokens(self) -> None:
        from crm.store.store import client_request_token

        base = "k" * 127
        tokens = {client_request_token(base + suffix) for suffix in "abcdefgh"}
        assert len(tokens) == 8

    def test_transact_write_sends_the_derived_token_not_the_raw_key(self) -> None:
        from crm.store.store import client_request_token

        sent: dict[str, Any] = {}

        class _Recorder:
            def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
                sent.update(kwargs)
                return {}

        key = "k" * 100
        Store(client=_Recorder()).transact_write(
            [{"Put": {"TableName": "accounts", "Item": {"account_id": {"S": "a"}}}}],
            transaction_key=key,
        )
        assert sent["ClientRequestToken"] == client_request_token(key)
        assert len(sent["ClientRequestToken"]) == 36


class TestClientRequestTokenScope:
    """S4: the token is scoped to (tenant, rep_id, tool), so two callers reusing a key never meet."""

    _SCOPE = ("demo", "rep-1", "update_account")

    def test_same_scope_and_key_give_the_same_token(self) -> None:
        from crm.store.store import client_request_token

        assert client_request_token("k", self._SCOPE) == client_request_token("k", self._SCOPE)
        assert len(client_request_token("k", self._SCOPE)) == 36

    @pytest.mark.parametrize(
        "other",
        [
            ("eval-x", "rep-1", "update_account"),
            ("demo", "rep-2", "update_account"),
            ("demo", "rep-1", "update_contact"),
        ],
        ids=["tenant", "rep", "tool"],
    )
    def test_a_different_tenant_rep_or_tool_gives_a_different_token(
        self, other: tuple[str, str, str]
    ) -> None:
        from crm.store.store import client_request_token

        assert client_request_token("k", self._SCOPE) != client_request_token("k", other)

    def test_a_token_scope_context_scopes_the_token_and_is_reset_after(self) -> None:
        from crm.store.store import client_request_token, token_scope

        sent: list[str] = []

        class _Recorder:
            def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
                sent.append(kwargs["ClientRequestToken"])
                return {}

        store = Store(client=_Recorder())
        put = [{"Put": {"TableName": "accounts", "Item": {"account_id": {"S": "a"}}}}]
        with token_scope(*self._SCOPE):
            store.transact_write(put, transaction_key="k")
        store.transact_write(put, transaction_key="k")
        assert sent == [client_request_token("k", self._SCOPE), client_request_token("k")]


def test_a_real_handler_sends_the_token_scoped_to_its_caller_and_tool(store: Store) -> None:
    """S4 wiring: `build_handler` runs the action in a token scope, so the wire token is the
    (tenant, rep, tool) token, not the key-only one."""
    from crm.envelope import IDENTITY_KEY, identity_event
    from crm.handlers import update_account
    from crm.store.store import client_request_token

    store.put_item(
        "accounts",
        {
            "account_id": "acct-1",
            "tenant": "demo",
            "tenant_owner_rep_id": "demo#rep-1",
            "owner_rep_id": "rep-1",
            "name": "Acme",
            "name_lower": "acme",
        },
    )
    sent: list[str] = []
    real = store._client.transact_write_items

    def record(**kwargs: Any) -> Any:
        sent.append(kwargs.get("ClientRequestToken", ""))
        return real(**kwargs)

    store._client.transact_write_items = record
    identity = {
        "rep_id": "rep-1",
        "book": ["acct-1"],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": "demo",
    }
    args = {"account_id": "acct-1", "fields": {"name": "New"}, "transaction_key": "tx-scope"}
    result = update_account.handler(identity_event(IDENTITY_KEY, identity, args), store=store)

    assert result["statusCode"] == 200, result
    assert sent == [client_request_token("tx-scope", ("demo", "rep-1", "update_account"))]
    assert sent[0] != client_request_token("tx-scope")


def _seed_audit(store: Store, *, audit_id: str, created_at: str) -> None:
    store.put_item(
        "audit_records",
        {
            "audit_id": audit_id,
            "tenant": "demo",
            "target_table": "accounts",
            "target_id": "acc-1",
            "actor_rep_id": "rep-1",
            "created_at": created_at,
            "status": "active",
            "before_snapshot": {},
            "after_snapshot": {"account_id": "acc-1"},
            "account_id": "acc-1",
        },
    )


def test_query_tenant_scan_reads_ascending_by_default_and_descending_when_asked(
    store: Store,
) -> None:
    """`scan_forward` reaches the index query; the default order is unchanged."""
    for n in (2, 0, 1):
        _seed_audit(store, audit_id=f"aud-{n}", created_at=f"2026-10-14T00:00:0{n}+00:00")

    forward, _ = store.query_tenant_scan("audit_records", tenant="demo")
    backward, _ = store.query_tenant_scan("audit_records", tenant="demo", scan_forward=False)

    assert [i["audit_id"] for i in forward] == ["aud-0", "aud-1", "aud-2"]
    assert [i["audit_id"] for i in backward] == ["aud-2", "aud-1", "aud-0"]


def test_query_tenant_scan_cursor_continues_in_descending_order(store: Store) -> None:
    for n in range(5):
        _seed_audit(store, audit_id=f"aud-{n}", created_at=f"2026-10-14T00:00:0{n}+00:00")

    first, cursor = store.query_tenant_scan(
        "audit_records", tenant="demo", limit=3, scan_forward=False
    )
    assert cursor is not None
    rest, _ = store.query_tenant_scan(
        "audit_records", tenant="demo", exclusive_start_key=cursor, scan_forward=False
    )

    assert [i["audit_id"] for i in first + rest] == [f"aud-{n}" for n in (4, 3, 2, 1, 0)]
