# SPDX-License-Identifier: MIT
"""Tests for the smoke-test tenant lifecycle (`scripts/smoke/tenant.py`).

Covers tenant-prefixed writes, `demo` refusal, idempotent re-seeding, surfaced teardown failures, and
reapability by `eval.tenancy.find_orphaned_runs`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from moto import mock_aws
from scripts.smoke.tenant import (
    SmokeTenant,
    allocate_smoke_run_id,
    allocate_smoke_tenant,
    is_smoke_run_id,
    seed_smoke_tenant,
    teardown_smoke_tenant,
)

from crm.authz import DEMO_TENANT
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.tenancy import TenantTeardownError, find_orphaned_runs, reap_tenant, tenant_for_run


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _fake_seeder(calls: list[tuple[str]]) -> Any:
    def seeder(seed_store: Store, *, tenant: str) -> None:
        calls.append((tenant,))
        seed_store.put_item(
            "opportunities",
            {
                "opportunity_id": f"opp-{tenant}",
                "tenant": tenant,
                "account_id": "acc-1",
                "tenant_account_id": f"{tenant}#acc-1",
                "tenant_owner_rep_id": f"{tenant}#rep-1",
                "owner_rep_id": "rep-1",
                "name": "Smoke Opp",
                "name_lower": "smoke opp",
                "stage": "Discovery",
                "stage_close_date": f"{tenant}#Discovery#2026-12-31",
                "close_date": "2026-12-31",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

    return seeder


class _SpyStore:
    """Wraps a real `Store`, recording the `tenant` (or key) of every write call."""

    def __init__(self, inner: Store) -> None:
        self._inner = inner
        self.write_tenants: list[str] = []

    def put_item(self, table: str, item: Mapping[str, object]) -> None:
        tenant = item.get("tenant")
        if isinstance(tenant, str):
            self.write_tenants.append(tenant)
        else:
            # eval_runs rows carry no `tenant` attribute; record the run_id-derived tenant instead
            # so catalog writes are still captured by the spy.
            run_id = item.get("run_id")
            if isinstance(run_id, str):
                self.write_tenants.append(tenant_for_run(run_id))
        self._inner.put_item(table, item)

    def delete_tenant(self, tenant: str) -> dict[str, int]:
        self.write_tenants.append(tenant)
        return self._inner.delete_tenant(tenant)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


# -- allocation --------------------------------------------------------------------------------


def test_allocate_smoke_run_id_is_prefixed_and_recognised() -> None:
    run_id = allocate_smoke_run_id()
    assert run_id.startswith("smoke-")
    assert is_smoke_run_id(run_id)


def test_allocate_smoke_run_id_never_collides_across_many_calls() -> None:
    run_ids = {allocate_smoke_run_id() for _ in range(1_000)}
    assert len(run_ids) == 1_000


def test_ordinary_eval_run_id_is_not_a_smoke_run_id() -> None:
    from eval.tenancy import allocate_run_id

    assert is_smoke_run_id(allocate_run_id()) is False


def test_allocate_smoke_tenant_maps_through_the_unchanged_tenant_for_run(store: Store) -> None:
    smoke_tenant = allocate_smoke_tenant(store)
    assert smoke_tenant.tenant == tenant_for_run(smoke_tenant.run_id)
    assert smoke_tenant.tenant.startswith("eval-smoke-")


def test_allocate_smoke_tenant_writes_a_catalog_row_with_running_status(store: Store) -> None:
    smoke_tenant = allocate_smoke_tenant(store)
    row = store.get_item("eval_runs", {"run_id": smoke_tenant.run_id})
    assert row is not None
    assert row["status"] == "running"


# -- every write carries the smoke tenant prefix; demo is never touched ------------------------


def test_full_lifecycle_every_write_carries_the_smoke_tenants_prefix(store: Store) -> None:
    spy = _SpyStore(store)
    calls: list[tuple[str]] = []

    smoke_tenant = allocate_smoke_tenant(spy)  # type: ignore[arg-type]
    seed_smoke_tenant(spy, smoke_tenant, seeder=_fake_seeder(calls))  # type: ignore[arg-type]
    teardown_smoke_tenant(spy, smoke_tenant)  # type: ignore[arg-type]

    assert spy.write_tenants, "expected at least one recorded write"
    for tenant in spy.write_tenants:
        assert tenant == smoke_tenant.tenant, f"write to unexpected tenant {tenant!r}"
        assert tenant != DEMO_TENANT


def test_full_lifecycle_never_writes_to_the_demo_tenant_even_when_demo_has_data(
    store: Store,
) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": "demo-preexisting",
            "tenant": DEMO_TENANT,
            "account_id": "acc-1",
            "tenant_account_id": f"{DEMO_TENANT}#acc-1",
            "tenant_owner_rep_id": f"{DEMO_TENANT}#rep-1",
            "owner_rep_id": "rep-1",
            "name": "Demo Opp",
            "name_lower": "demo opp",
            "stage": "Discovery",
            "stage_close_date": f"{DEMO_TENANT}#Discovery#2026-12-31",
            "close_date": "2026-12-31",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )

    smoke_tenant = allocate_smoke_tenant(store)
    seed_smoke_tenant(store, smoke_tenant, seeder=_fake_seeder([]))
    teardown_smoke_tenant(store, smoke_tenant)

    remaining, _ = store.query_tenant_scan("opportunities", tenant=DEMO_TENANT)
    assert [item["opportunity_id"] for item in remaining] == ["demo-preexisting"]


def test_seed_smoke_tenant_refuses_a_demo_smoke_tenant_explicitly(store: Store) -> None:
    """A `SmokeTenant` pointing at `demo` is refused before the seeder runs."""
    seeder_invoked = False

    def fail_if_called(seed_store: Store, *, tenant: str) -> None:
        nonlocal seeder_invoked
        seeder_invoked = True

    bogus = SmokeTenant(run_id="smoke-bogus", tenant=DEMO_TENANT)
    with pytest.raises(ValueError, match="demo"):
        seed_smoke_tenant(store, bogus, seeder=fail_if_called)
    assert seeder_invoked is False


def test_teardown_smoke_tenant_refuses_a_demo_smoke_tenant_explicitly(store: Store) -> None:
    bogus = SmokeTenant(run_id="smoke-bogus", tenant=DEMO_TENANT)
    with pytest.raises(ValueError, match="demo"):
        teardown_smoke_tenant(store, bogus)


# -- idempotent re-run --------------------------------------------------------------------------


def test_allocate_then_seed_twice_for_the_same_run_id_does_not_double_write_or_raise(
    store: Store,
) -> None:
    calls: list[tuple[str]] = []
    seeder = _fake_seeder(calls)

    smoke_tenant = allocate_smoke_tenant(store)
    seed_smoke_tenant(store, smoke_tenant, seeder=seeder)
    seed_smoke_tenant(store, smoke_tenant, seeder=seeder)

    assert len(calls) == 2  # the seeder itself is called twice...
    items, _ = store.query_tenant_scan("opportunities", tenant=smoke_tenant.tenant)
    assert len(items) == 1  # ...but the resulting state is unchanged (deterministic key overwrite)


def test_reallocating_and_reseeding_the_same_tenant_leaves_catalog_row_unchanged(
    store: Store,
) -> None:
    smoke_tenant = allocate_smoke_tenant(store)
    first_row = store.get_item("eval_runs", {"run_id": smoke_tenant.run_id})
    assert first_row is not None

    # Re-recording the same run id (simulating a retried allocation) must not raise and must
    # leave the row's status unchanged.
    from scripts.smoke.tenant import _put_eval_run_row  # noqa: PLC0415 (internal reuse in test)

    _put_eval_run_row(store, run_id=smoke_tenant.run_id, status="running")
    second_row = store.get_item("eval_runs", {"run_id": smoke_tenant.run_id})
    assert second_row is not None
    assert second_row["status"] == "running"


# -- teardown failure is reported, never swallowed ----------------------------------------------


def test_teardown_smoke_tenant_failure_surfaces_as_tenant_teardown_error(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke_tenant = allocate_smoke_tenant(store)

    def raise_on_delete(tenant: str) -> dict[str, int]:
        raise RuntimeError("simulated DynamoDB outage during smoke teardown")

    monkeypatch.setattr(store, "delete_tenant", raise_on_delete)

    with pytest.raises(TenantTeardownError, match=smoke_tenant.tenant):
        teardown_smoke_tenant(store, smoke_tenant)


def test_teardown_smoke_tenant_failure_leaves_the_catalog_row_running_not_falsely_torn_down(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed teardown leaves the catalog row `running`, not `torn_down`."""
    smoke_tenant = allocate_smoke_tenant(store)

    def raise_on_delete(tenant: str) -> dict[str, int]:
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "delete_tenant", raise_on_delete)

    with pytest.raises(TenantTeardownError):
        teardown_smoke_tenant(store, smoke_tenant)

    row = store.get_item("eval_runs", {"run_id": smoke_tenant.run_id})
    assert row is not None
    assert row["status"] == "running"


# -- reapability by eval.tenancy ------------------------------------------------------------------


def test_an_abandoned_smoke_tenant_is_found_and_reaped_by_the_real_find_orphaned_runs(
    store: Store,
) -> None:
    """A smoke tenant that never tore down is found by `find_orphaned_runs` and reaped."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    smoke_tenant = allocate_smoke_tenant(store)
    # Backdate the catalog row's started_at so it is old enough to be an orphan.
    store.put_item(
        "eval_runs",
        {
            "run_id": smoke_tenant.run_id,
            "status": "running",
            "started_at": "2026-05-01T00:00:00+00:00",
        },
    )
    seed_smoke_tenant(store, smoke_tenant, seeder=_fake_seeder([]))

    orphans = find_orphaned_runs(store, older_than=timedelta(hours=24), now=now)
    matching = [o for o in orphans if o.run_id == smoke_tenant.run_id]
    assert len(matching) == 1
    assert matching[0].tenant == smoke_tenant.tenant
    assert is_smoke_run_id(matching[0].run_id)

    reap_tenant(store, run=matching[0])

    remaining, _ = store.query_tenant_scan("opportunities", tenant=smoke_tenant.tenant)
    assert remaining == []


def test_a_torn_down_smoke_tenant_is_not_reaped_as_an_orphan(store: Store) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    smoke_tenant = allocate_smoke_tenant(store)
    store.put_item(
        "eval_runs",
        {
            "run_id": smoke_tenant.run_id,
            "status": "running",
            "started_at": "2026-05-01T00:00:00+00:00",
        },
    )
    seed_smoke_tenant(store, smoke_tenant, seeder=_fake_seeder([]))
    teardown_smoke_tenant(store, smoke_tenant)

    orphans = find_orphaned_runs(store, older_than=timedelta(hours=24), now=now)
    assert all(o.run_id != smoke_tenant.run_id for o in orphans)
