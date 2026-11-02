# SPDX-License-Identifier: MIT
"""Tenant isolation tests: concurrent eval-run tenants, scoped teardown, and GSI tenant projection.

Every query-path GSI must project `tenant`, or `crm.authz.filter_to_scope` drops its rows.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from crm.authz import Identity, filter_to_scope
from crm.store import keys
from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLES
from crm.store.store import Store


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _seed_opportunity(
    store: Store, *, tenant: str, opportunity_id: str, account_id: str, owner_rep_id: str
) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": opportunity_id,
            "tenant": tenant,
            "account_id": account_id,
            "tenant_account_id": keys.tenant_prefixed(tenant, account_id),
            "tenant_owner_rep_id": keys.tenant_prefixed(tenant, owner_rep_id),
            "owner_rep_id": owner_rep_id,
            "name": f"Opp {opportunity_id}",
            "name_lower": f"opp {opportunity_id}".lower(),
            "stage": "Discovery",
            "stage_close_date": keys.stage_close_date("Discovery", "2026-12-31"),
            "close_date": "2026-12-31",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )


# -- concurrent eval runs against the same tables -------------------------------------------


def test_two_concurrent_eval_tenants_never_see_each_others_items(store: Store) -> None:
    _seed_opportunity(
        store,
        tenant="eval-run-a",
        opportunity_id="opp-a1",
        account_id="acc-1",
        owner_rep_id="rep-1",
    )
    _seed_opportunity(
        store,
        tenant="eval-run-b",
        opportunity_id="opp-b1",
        account_id="acc-1",
        owner_rep_id="rep-1",
    )

    run_a_items, _ = store.query_tenant_scan("opportunities", tenant="eval-run-a")
    run_b_items, _ = store.query_tenant_scan("opportunities", tenant="eval-run-b")

    assert [item["opportunity_id"] for item in run_a_items] == ["opp-a1"]
    assert [item["opportunity_id"] for item in run_b_items] == ["opp-b1"]


def test_same_account_id_in_two_tenants_does_not_collide_on_gsi_account(store: Store) -> None:
    """Identical account IDs seeded in two tenants do not merge on the account GSI."""
    _seed_opportunity(
        store,
        tenant="eval-run-x",
        opportunity_id="opp-x",
        account_id="acc-shared",
        owner_rep_id="rep-1",
    )
    _seed_opportunity(
        store,
        tenant="eval-run-y",
        opportunity_id="opp-y",
        account_id="acc-shared",
        owner_rep_id="rep-1",
    )

    x_items, _ = store.query_gsi(
        "opportunities",
        "gsi_account",
        partition_value=keys.tenant_prefixed("eval-run-x", "acc-shared"),
    )
    y_items, _ = store.query_gsi(
        "opportunities",
        "gsi_account",
        partition_value=keys.tenant_prefixed("eval-run-y", "acc-shared"),
    )
    assert [item["opportunity_id"] for item in x_items] == ["opp-x"]
    assert [item["opportunity_id"] for item in y_items] == ["opp-y"]


def test_demo_tenant_is_never_visible_through_an_eval_tenants_gsi_query(store: Store) -> None:
    _seed_opportunity(
        store, tenant="demo", opportunity_id="opp-demo", account_id="acc-1", owner_rep_id="rep-1"
    )
    _seed_opportunity(
        store, tenant="eval-run-z", opportunity_id="opp-z", account_id="acc-1", owner_rep_id="rep-1"
    )

    eval_items, _ = store.query_gsi(
        "opportunities",
        "gsi_owner_stage",
        partition_value=keys.tenant_prefixed("eval-run-z", "rep-1"),
    )
    assert [item["opportunity_id"] for item in eval_items] == ["opp-z"]


# -- teardown of one failed run leaves siblings untouched ------------------------------------


def test_tearing_down_one_failed_eval_run_leaves_a_concurrent_run_and_demo_untouched(
    store: Store,
) -> None:
    _seed_opportunity(
        store,
        tenant="eval-run-fail",
        opportunity_id="opp-fail",
        account_id="acc-1",
        owner_rep_id="rep-1",
    )
    _seed_opportunity(
        store,
        tenant="eval-run-ok",
        opportunity_id="opp-ok",
        account_id="acc-1",
        owner_rep_id="rep-1",
    )
    _seed_opportunity(
        store, tenant="demo", opportunity_id="opp-demo", account_id="acc-1", owner_rep_id="rep-1"
    )

    deleted = store.delete_tenant("eval-run-fail")

    assert deleted["opportunities"] == 1
    assert store.get_item("opportunities", {"opportunity_id": "opp-fail"}) is None
    assert store.get_item("opportunities", {"opportunity_id": "opp-ok"}) is not None
    assert store.get_item("opportunities", {"opportunity_id": "opp-demo"}) is not None


def test_teardown_is_safe_to_call_twice_on_an_already_torn_down_tenant(store: Store) -> None:
    _seed_opportunity(
        store,
        tenant="eval-run-twice",
        opportunity_id="opp-1",
        account_id="acc-1",
        owner_rep_id="rep-1",
    )
    first = store.delete_tenant("eval-run-twice")
    second = store.delete_tenant("eval-run-twice")
    assert first["opportunities"] == 1
    assert second["opportunities"] == 0


# -- every GSI filter_to_scope's callers use must project tenant -----------------------------


#: The GSIs backing list, search and rollup access patterns, whose rows reach
#: `crm.authz.filter_to_scope`.
_QUERY_PATH_GSIS: tuple[tuple[str, str], ...] = (
    ("accounts", "gsi_owner"),
    ("accounts", "gsi_tenant"),
    ("contacts", "gsi_account"),
    ("contacts", "gsi_tenant"),
    ("opportunities", "gsi_account"),
    ("opportunities", "gsi_owner_stage"),
    ("opportunities", "gsi_stage"),
    ("opportunities", "gsi_tenant"),
    ("activities", "gsi_opportunity"),
    ("activities", "gsi_account"),
    ("activities", "gsi_tenant"),
)


@pytest.mark.parametrize(("table", "gsi_name"), _QUERY_PATH_GSIS)
def test_every_query_path_gsi_projects_tenant(table: str, gsi_name: str) -> None:
    """Every list/search/aggregate GSI projects `tenant`."""
    gsi = TABLES[table].gsi(gsi_name)
    assert "tenant" in gsi.projected_attributes


def test_filter_to_scope_does_not_drop_rows_returned_by_a_tenant_projecting_gsi(
    store: Store,
) -> None:
    """A row queried from a tenant-projecting GSI survives `filter_to_scope`."""
    _seed_opportunity(
        store, tenant="demo", opportunity_id="opp-keep", account_id="acc-1", owner_rep_id="rep-1"
    )
    items, _ = store.query_gsi(
        "opportunities", "gsi_owner_stage", partition_value=keys.tenant_prefixed("demo", "rep-1")
    )
    identity = Identity(rep_id="rep-1", book=frozenset({"acc-1"}), tenant="demo")
    kept = filter_to_scope(identity, items)
    assert [item["opportunity_id"] for item in kept] == ["opp-keep"]


def test_filter_to_scope_drops_a_cross_tenant_row_even_if_one_slipped_through(store: Store) -> None:
    """`filter_to_scope` rejects a row carrying a different tenant value."""
    identity = Identity(rep_id="rep-1", full_visibility=True, tenant="demo")
    foreign_row: dict[str, object] = {
        "opportunity_id": "opp-foreign",
        "tenant": "eval-run-other",
        "account_id": "acc-1",
    }
    assert filter_to_scope(identity, [foreign_row]) == []
