# SPDX-License-Identifier: MIT
"""Integration tests for the harness-side ``canary`` tenant guard, against DynamoDB Local.

A row under ``canary`` must fail the sweep and be named; a teardown failure must propagate as
``TenantTeardownError`` and stay distinct from a canary violation.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLE_NAMES, TENANT_SCOPED_TABLES
from crm.store.store import Store
from eval.harness import DEFAULT_ARM_SPECS, run_sweep
from eval.tenancy import (
    CANARY_TENANT,
    CanaryTenantNotEmptyError,
    TenantTeardownError,
    assert_canary_empty,
    find_canary_records,
    teardown_tenant,
)

#: Unique per process, so concurrent xdist workers never share a table.
_TABLE_PREFIX = f"canary-{uuid.uuid4().hex[:12]}-"

#: More planted rows than a failure message names inline.
_MANY_PLANTED = 25


@pytest.fixture
def guard_store(dynamodb_local_client: Any) -> Iterator[Store]:
    """A prefixed :class:`Store` against DynamoDB Local with its own tables, dropped on teardown."""
    create_all_tables(dynamodb_local_client, table_prefix=_TABLE_PREFIX)
    try:
        yield Store(table_prefix=_TABLE_PREFIX, client=dynamodb_local_client)
    finally:
        for table in TABLE_NAMES:
            with contextlib.suppress(dynamodb_local_client.exceptions.ResourceNotFoundException):
                dynamodb_local_client.delete_table(TableName=f"{_TABLE_PREFIX}{table}")


def _plant_canary_account(store: Store, *, account_id: str) -> str:
    """Write one account under ``canary`` through ``Store.put_item``, bypassing ``eval.tenancy``."""
    store.put_item(
        "accounts",
        {
            "account_id": account_id,
            "tenant": CANARY_TENANT,
            "name": "Leaked Account",
            "name_lower": "leaked account",
            "owner_rep_id": "rep-ashford",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    return account_id


def _plant_eval_opportunity(store: Store, *, tenant: str, opportunity_id: str) -> None:
    """Write one opportunity under an eval tenant, so a teardown has something real to remove."""
    store.put_item(
        "opportunities",
        {
            "opportunity_id": opportunity_id,
            "tenant": tenant,
            "account_id": "acct-1",
            "owner_rep_id": "rep-ashford",
            "name": "Doomed Deal",
            "name_lower": "doomed deal",
            "stage": "Discovery",
            "close_date": "2026-12-31",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )


# -- the clean case: the guard passes, and does not pass vacuously --------------------------------


def test_a_clean_canary_tenant_yields_no_records(guard_store: Store) -> None:
    assert find_canary_records(guard_store) == []
    assert_canary_empty(guard_store)


def test_the_guard_watches_every_table_a_sweep_could_write(guard_store: Store) -> None:
    """A guard that only looked at some tables would pass on a violation in the others."""
    planted = {table: _plant_row_in(guard_store, table) for table in sorted(TENANT_SCOPED_TABLES)}

    found = find_canary_records(guard_store)

    assert {record.table for record in found} == set(planted)


def _plant_row_in(store: Store, table: str) -> str:
    """Write one minimal ``canary`` row into ``table``, returning its primary key value.

    ``name`` is set because the derived ``name_lower`` is the sparse tenant index's sort key;
    without it the row is absent from that index.
    """
    from crm.store.schema import table_schema

    schema = table_schema(table)
    key_value = f"canary-leak-{table}"
    item: dict[str, object] = {
        schema.partition_key: key_value,
        "tenant": CANARY_TENANT,
        "created_at": "2026-01-01T00:00:00Z",
        "name": "Canary Leak",
        "rep_id": key_value,
        "account_id": "acct-1",
        "opportunity_id": "opp-1",
        "owner_rep_id": "rep-ashford",
        "actor_rep_id": "rep-ashford",
        "target_id": "opp-1",
        "correlation_id": "corr-1",
        "run_id": "run-1",
        "status": "pending",
        "stage": "Discovery",
        "close_date": "2026-12-31",
    }
    if schema.sort_key is not None:
        item[schema.sort_key] = key_value
    store.put_item(table, item)
    return key_value


# -- the violation is named, not merely counted --------------------------------------------------


def test_a_planted_canary_record_is_named_in_the_failure(guard_store: Store) -> None:
    account_id = _plant_canary_account(guard_store, account_id="canary-leak-0001")

    with pytest.raises(CanaryTenantNotEmptyError) as excinfo:
        assert_canary_empty(guard_store, tenant="eval-suspect-run")

    message = str(excinfo.value)
    assert account_id in message, "a guard that says only 'non-empty' costs an hour of bisection"
    assert "accounts" in message
    assert "eval-suspect-run" in message, "the failure must also name the suspect sweep"
    assert [record.key_value for record in excinfo.value.records] == [account_id]


def test_a_mass_violation_names_a_bounded_sample_and_states_the_total(guard_store: Store) -> None:
    for index in range(_MANY_PLANTED):
        _plant_canary_account(guard_store, account_id=f"canary-leak-{index:04d}")

    with pytest.raises(CanaryTenantNotEmptyError) as excinfo:
        assert_canary_empty(guard_store)

    message = str(excinfo.value)
    assert len(excinfo.value.records) == _MANY_PLANTED
    assert f"{_MANY_PLANTED} record(s)" in message
    assert "more" in message, "a truncated list must disclose that it is truncated"
    assert "canary-leak-0000" in message


def test_an_unreadable_canary_tenant_is_not_reported_as_an_empty_one(
    guard_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read failure on the canary tenant propagates rather than reading as empty."""

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated DynamoDB outage while reading the canary tenant")

    monkeypatch.setattr(guard_store, "query_tenant_scan", _raise)

    with pytest.raises(RuntimeError, match="simulated DynamoDB outage"):
        assert_canary_empty(guard_store)


# -- teardown: the guard is unskippable, and it does not orphan the run's own tenant --------------


def test_teardown_asserts_the_canary_tenant_and_still_removes_the_runs_own_tenant(
    guard_store: Store,
) -> None:
    tenant = "eval-run-with-a-leak"
    _plant_eval_opportunity(guard_store, tenant=tenant, opportunity_id="opp-doomed")
    _plant_canary_account(guard_store, account_id="canary-leak-0001")

    with pytest.raises(CanaryTenantNotEmptyError) as excinfo:
        teardown_tenant(guard_store, tenant=tenant)

    assert "canary-leak-0001" in str(excinfo.value)
    remaining, _ = guard_store.query_tenant_scan("opportunities", tenant=tenant)
    assert remaining == [], "a canary violation must not leave the run's own tenant orphaned"


def test_teardown_passes_the_canary_check_on_a_clean_store(guard_store: Store) -> None:
    tenant = "eval-clean-run"
    _plant_eval_opportunity(guard_store, tenant=tenant, opportunity_id="opp-clean")

    deleted = teardown_tenant(guard_store, tenant=tenant)

    assert deleted["opportunities"] == 1


def test_teardown_refuses_the_canary_tenant_itself(guard_store: Store) -> None:
    """`canary` is refused as a teardown target."""
    with pytest.raises(ValueError, match=CANARY_TENANT):
        teardown_tenant(guard_store, tenant=CANARY_TENANT)


# -- a teardown failure propagates, distinct from a canary violation -----------------------------


def test_a_teardown_failure_propagates_and_is_not_a_canary_violation(
    guard_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise_on_delete(_tenant: str) -> dict[str, int]:
        raise RuntimeError("simulated DynamoDB outage during teardown")

    monkeypatch.setattr(guard_store, "delete_tenant", _raise_on_delete)

    with pytest.raises(TenantTeardownError) as excinfo:
        teardown_tenant(guard_store, tenant="eval-doomed-teardown")

    assert not issubclass(TenantTeardownError, CanaryTenantNotEmptyError)
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "eval-doomed-teardown" in str(excinfo.value)


def test_a_canary_violation_is_not_a_teardown_error(guard_store: Store) -> None:
    """A canary violation raises ``CanaryTenantNotEmptyError``, not ``TenantTeardownError``."""
    _plant_canary_account(guard_store, account_id="canary-leak-0001")

    with pytest.raises(CanaryTenantNotEmptyError):
        teardown_tenant(guard_store, tenant="eval-some-run")

    assert not issubclass(CanaryTenantNotEmptyError, TenantTeardownError)


# -- the sweep itself: no sweep completes without the check ---------------------------------------


def test_a_planted_canary_record_fails_the_whole_sweep_and_is_named(
    guard_store: Store, tmp_path: Path
) -> None:
    """A planted canary record fails ``run_sweep`` with an error naming the offending record."""
    _plant_canary_account(guard_store, account_id="canary-leak-mid-sweep")

    with pytest.raises(CanaryTenantNotEmptyError) as excinfo:
        run_sweep(
            store=guard_store,
            mode="stub",
            arm_specs=(DEFAULT_ARM_SPECS[0],),
            output_dir=tmp_path,
        )

    message = str(excinfo.value)
    assert "canary-leak-mid-sweep" in message
    assert "accounts" in message
    assert excinfo.value.tenant is not None
    assert excinfo.value.tenant.startswith("eval-"), "the failure names the sweep's own tenant"


def test_the_failing_sweep_still_tore_down_its_own_tenant(
    guard_store: Store, tmp_path: Path
) -> None:
    """A sweep failing on a canary violation still removes every row of its own tenant."""
    _plant_canary_account(guard_store, account_id="canary-leak-mid-sweep")

    with pytest.raises(CanaryTenantNotEmptyError) as excinfo:
        run_sweep(
            store=guard_store,
            mode="stub",
            arm_specs=(DEFAULT_ARM_SPECS[0],),
            output_dir=tmp_path,
        )

    tenant = excinfo.value.tenant
    assert tenant is not None
    for table in sorted(TENANT_SCOPED_TABLES):
        rows, _ = guard_store.query_tenant_scan(table, tenant=tenant)
        assert rows == [], f"{table} still holds rows for the torn-down sweep tenant {tenant!r}"


def test_a_clean_sweep_completes_with_the_canary_tenant_still_empty(
    guard_store: Store, tmp_path: Path
) -> None:
    """A clean sweep completes without a teardown error and leaves ``canary`` empty."""
    result = run_sweep(
        store=guard_store,
        mode="stub",
        arm_specs=(DEFAULT_ARM_SPECS[0],),
        output_dir=tmp_path,
    )

    assert result.tenant.startswith("eval-")
    assert result.teardown_error is None
    assert find_canary_records(guard_store) == []
