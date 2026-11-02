# SPDX-License-Identifier: MIT
"""Tests for eval tenancy allocation, idempotent seeding, scoped teardown, and orphan reaping."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from crm.authz import DEMO_TENANT
from crm.store import keys
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.tenancy import (
    OrphanedRun,
    TenantTeardownError,
    allocate_run_id,
    find_orphaned_runs,
    reap_tenant,
    run_id_for_tenant,
    seed_tenant,
    teardown_tenant,
    tenant_for_run,
)


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _seed_opportunity(store: Store, *, tenant: str, opportunity_id: str) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": opportunity_id,
            "tenant": tenant,
            "account_id": "acc-1",
            "tenant_account_id": keys.tenant_prefixed(tenant, "acc-1"),
            "tenant_owner_rep_id": keys.tenant_prefixed(tenant, "rep-1"),
            "owner_rep_id": "rep-1",
            "name": f"Opp {opportunity_id}",
            "name_lower": f"opp {opportunity_id}".lower(),
            "stage": "Discovery",
            "stage_close_date": keys.stage_close_date("Discovery", "2026-12-31"),
            "close_date": "2026-12-31",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )


def _put_eval_run(store: Store, *, run_id: str, status: str, started_at: str) -> None:
    store.put_item(
        "eval_runs",
        {
            "run_id": run_id,
            "status": status,
            "started_at": started_at,
        },
    )


# -- allocation ---------------------------------------------------------------------------------


def test_allocate_run_id_returns_a_non_empty_string() -> None:
    run_id = allocate_run_id()
    assert isinstance(run_id, str)
    assert run_id


def test_allocate_run_id_never_collides_across_many_calls() -> None:
    run_ids = {allocate_run_id() for _ in range(10_000)}
    assert len(run_ids) == 10_000


def test_allocate_run_id_draws_from_a_cryptographically_random_source() -> None:
    """Run ids are drawn from uuid4, not a counter or a timestamp."""
    run_id = allocate_run_id()
    # A 32-character hex string (uuid4().hex) with no separators and no embedded timestamp prefix.
    assert len(run_id) == 32
    assert all(c in "0123456789abcdef" for c in run_id)


def test_tenant_for_run_is_prefixed_and_invertible() -> None:
    run_id = allocate_run_id()
    tenant = tenant_for_run(run_id)
    assert tenant == f"eval-{run_id}"
    assert run_id_for_tenant(tenant) == run_id


def test_tenant_for_run_rejects_an_empty_run_id() -> None:
    with pytest.raises(ValueError, match="run_id must not be empty"):
        tenant_for_run("")


def test_run_id_for_tenant_rejects_a_non_eval_tenant() -> None:
    with pytest.raises(ValueError, match="not an eval tenant"):
        run_id_for_tenant(DEMO_TENANT)


def test_two_concurrently_allocated_tenants_do_not_collide_on_the_real_store(store: Store) -> None:
    """Two eval tenants sharing account/rep ids stay isolated on the tenant-prefixed GSI."""
    run_id_a = allocate_run_id()
    run_id_b = allocate_run_id()
    tenant_a = tenant_for_run(run_id_a)
    tenant_b = tenant_for_run(run_id_b)
    assert tenant_a != tenant_b

    _seed_opportunity(store, tenant=tenant_a, opportunity_id="opp-a")
    _seed_opportunity(store, tenant=tenant_b, opportunity_id="opp-b")

    items_a, _ = store.query_tenant_scan("opportunities", tenant=tenant_a)
    items_b, _ = store.query_tenant_scan("opportunities", tenant=tenant_b)
    assert [item["tenant"] for item in items_a] == [tenant_a]
    assert [item["tenant"] for item in items_b] == [tenant_b]


# -- seeding: idempotent, scope-parameterised, delegates to the fixture generator ----------------


def test_seed_tenant_calls_the_injected_seeder_with_the_store_and_tenant(store: Store) -> None:
    calls: list[tuple[Store, str]] = []

    def fake_seeder(seed_store: Store, *, tenant: str) -> None:
        calls.append((seed_store, tenant))

    seed_tenant(store, tenant="eval-run-1", seeder=fake_seeder)

    assert calls == [(store, "eval-run-1")]


def test_seed_tenant_calling_the_seeder_twice_is_the_seeders_own_idempotency_contract(
    store: Store,
) -> None:
    """Calling seed_tenant twice calls the seeder twice, with no seeding logic of its own."""
    call_count = 0

    def counting_seeder(seed_store: Store, *, tenant: str) -> None:
        nonlocal call_count
        call_count += 1

    seed_tenant(store, tenant="eval-run-1", seeder=counting_seeder)
    seed_tenant(store, tenant="eval-run-1", seeder=counting_seeder)

    assert call_count == 2


def test_seed_tenant_refuses_the_demo_tenant(store: Store) -> None:
    def fail_if_called(seed_store: Store, *, tenant: str) -> None:
        raise AssertionError("seeder must never be called for the demo tenant")

    with pytest.raises(ValueError, match="demo"):
        seed_tenant(store, tenant=DEMO_TENANT, seeder=fail_if_called)


def test_seed_tenant_without_an_injected_seeder_resolves_the_real_fixtures_generate_seed_tenant(
    store: Store,
) -> None:
    """With no injected seeder, seed_tenant reaches fixtures.generate.seed_tenant and leaves a
    GSI-reachable corpus.
    """
    seed_tenant(store, tenant="eval-run-1")

    scanned, _ = store.query_tenant_scan("opportunities", tenant="eval-run-1")
    assert len(scanned) > 0
    assert all(item["tenant"] == "eval-run-1" for item in scanned)

    # Reachable through the real GSI a search/list path would use, not merely present in a scan.
    scanned_opportunity_id = str(scanned[0]["opportunity_id"])
    full_item = store.get_item("opportunities", {"opportunity_id": scanned_opportunity_id})
    assert full_item is not None
    opportunity_id = str(full_item["opportunity_id"])
    owner_rep_id = str(full_item["owner_rep_id"])
    gsi_items, _ = store.query_gsi(
        "opportunities",
        "gsi_owner_stage",
        partition_value=keys.tenant_prefixed("eval-run-1", owner_rep_id),
    )
    assert any(item["opportunity_id"] == opportunity_id for item in gsi_items)


# -- teardown: scoped delete, exactly one tenant, failure reported ------------------------------


def test_teardown_tenant_deletes_only_the_named_tenants_items(store: Store) -> None:
    _seed_opportunity(store, tenant="eval-run-a", opportunity_id="opp-a1")
    _seed_opportunity(store, tenant="eval-run-b", opportunity_id="opp-b1")

    deleted = teardown_tenant(store, tenant="eval-run-a")

    assert deleted["opportunities"] == 1
    remaining_a, _ = store.query_tenant_scan("opportunities", tenant="eval-run-a")
    remaining_b, _ = store.query_tenant_scan("opportunities", tenant="eval-run-b")
    assert remaining_a == []
    assert [item["opportunity_id"] for item in remaining_b] == ["opp-b1"]


def test_teardown_tenant_never_writes_to_or_deletes_from_demo(store: Store) -> None:
    _seed_opportunity(store, tenant=DEMO_TENANT, opportunity_id="demo-opp-1")

    with pytest.raises(ValueError, match="demo"):
        teardown_tenant(store, tenant=DEMO_TENANT)

    remaining, _ = store.query_tenant_scan("opportunities", tenant=DEMO_TENANT)
    assert [item["opportunity_id"] for item in remaining] == ["demo-opp-1"]


def test_teardown_tenant_failure_is_reported_not_swallowed(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing teardown surfaces as a typed exception, never a silent success."""

    def raise_on_delete(tenant: str) -> dict[str, int]:
        raise RuntimeError("simulated DynamoDB outage during teardown")

    monkeypatch.setattr(store, "delete_tenant", raise_on_delete)

    with pytest.raises(TenantTeardownError, match="eval-run-a") as excinfo:
        teardown_tenant(store, tenant="eval-run-a")

    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_teardown_tenant_error_message_names_the_tenant_and_the_cause(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_on_delete(tenant: str) -> dict[str, int]:
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "delete_tenant", raise_on_delete)

    with pytest.raises(TenantTeardownError) as excinfo:
        teardown_tenant(store, tenant="eval-run-xyz")

    message = str(excinfo.value)
    assert "eval-run-xyz" in message
    assert "boom" in message


# -- orphan reaping ------------------------------------------------------------------------------


def test_find_orphaned_runs_returns_runs_older_than_the_threshold(store: Store) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    old_run_id = "old1234"
    fresh_run_id = "fresh567"
    _put_eval_run(
        store, run_id=old_run_id, status="running", started_at="2026-05-01T00:00:00+00:00"
    )
    _put_eval_run(
        store, run_id=fresh_run_id, status="running", started_at="2026-05-31T23:00:00+00:00"
    )

    orphans = find_orphaned_runs(store, older_than=timedelta(hours=24), now=now)

    assert [o.run_id for o in orphans] == [old_run_id]
    assert orphans[0].tenant == f"eval-{old_run_id}"


def test_find_orphaned_runs_excludes_runs_within_the_threshold(store: Store) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    _put_eval_run(store, run_id="recent1", status="running", started_at="2026-05-31T12:00:00+00:00")

    orphans = find_orphaned_runs(store, older_than=timedelta(hours=24), now=now)

    assert orphans == []


def test_find_orphaned_runs_ignores_a_run_with_a_malformed_timestamp(store: Store) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    _put_eval_run(store, run_id="malformed1", status="running", started_at="not-a-timestamp")

    orphans = find_orphaned_runs(store, older_than=timedelta(hours=24), now=now)

    assert orphans == []


def test_find_orphaned_runs_orders_oldest_first(store: Store) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    _put_eval_run(store, run_id="middle", status="running", started_at="2026-04-15T00:00:00+00:00")
    _put_eval_run(store, run_id="oldest", status="running", started_at="2026-01-01T00:00:00+00:00")
    _put_eval_run(store, run_id="newer", status="running", started_at="2026-05-01T00:00:00+00:00")

    orphans = find_orphaned_runs(store, older_than=timedelta(hours=1), now=now)

    assert [o.run_id for o in orphans] == ["oldest", "middle", "newer"]


def test_reap_tenant_tears_down_the_orphans_tenant(store: Store) -> None:
    run_id = "orphaned1"
    tenant = tenant_for_run(run_id)
    _seed_opportunity(store, tenant=tenant, opportunity_id="opp-1")
    run = OrphanedRun(
        run_id=run_id, tenant=tenant, status="running", started_at="2026-01-01T00:00:00Z"
    )

    deleted = reap_tenant(store, run=run)

    assert deleted["opportunities"] == 1
    remaining, _ = store.query_tenant_scan("opportunities", tenant=tenant)
    assert remaining == []


def test_reap_tenant_reports_failure_not_swallowed(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = OrphanedRun(
        run_id="orphaned2",
        tenant="eval-orphaned2",
        status="running",
        started_at="2026-01-01T00:00:00Z",
    )

    def raise_on_delete(tenant: str) -> dict[str, int]:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(store, "delete_tenant", raise_on_delete)

    with pytest.raises(TenantTeardownError):
        reap_tenant(store, run=run)


def test_reap_tenant_records_the_catalog_row_terminal_so_reaping_is_idempotent(
    store: Store,
) -> None:
    """A reaped run's catalog row is terminal, so it is no longer reported as an orphan."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    run_id = "orphaned-idempotent"
    tenant = tenant_for_run(run_id)
    _put_eval_run(store, run_id=run_id, status="running", started_at="2026-05-01T00:00:00+00:00")
    _seed_opportunity(store, tenant=tenant, opportunity_id="opp-1")

    orphans = find_orphaned_runs(store, older_than=timedelta(hours=1), now=now)
    assert [o.run_id for o in orphans] == [run_id]

    reap_tenant(store, run=orphans[0])

    assert find_orphaned_runs(store, older_than=timedelta(hours=1), now=now) == []


def test_reap_tenant_preserves_the_rows_original_started_at(store: Store) -> None:
    run_id = "orphaned-started-at"
    tenant = tenant_for_run(run_id)
    _put_eval_run(store, run_id=run_id, status="running", started_at="2026-05-01T00:00:00+00:00")
    run = OrphanedRun(
        run_id=run_id, tenant=tenant, status="running", started_at="2026-05-01T00:00:00+00:00"
    )

    reap_tenant(store, run=run)

    row = store.get_item("eval_runs", {"run_id": run_id})
    assert row is not None
    assert row["status"] == "torn_down"
    assert row["started_at"] == "2026-05-01T00:00:00+00:00"


def test_a_failed_teardown_leaves_the_row_running_and_the_tenant_reapable(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal write happens only after deletion succeeds."""
    run_id = "orphaned-failing"
    _put_eval_run(store, run_id=run_id, status="running", started_at="2026-05-01T00:00:00+00:00")
    run = OrphanedRun(
        run_id=run_id,
        tenant=tenant_for_run(run_id),
        status="running",
        started_at="2026-05-01T00:00:00+00:00",
    )

    def raise_on_delete(tenant: str) -> dict[str, int]:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(store, "delete_tenant", raise_on_delete)
    with pytest.raises(TenantTeardownError):
        reap_tenant(store, run=run)

    row = store.get_item("eval_runs", {"run_id": run_id})
    assert row is not None
    assert row["status"] == "running"


def test_end_to_end_orphan_detection_and_reaping(store: Store) -> None:
    """A full lifecycle: a run is recorded as started, its tenant is seeded, the run never tears
    itself down, and the reaper both finds and cleans it up."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    run_id = allocate_run_id()
    tenant = tenant_for_run(run_id)
    _put_eval_run(store, run_id=run_id, status="running", started_at="2026-05-01T00:00:00+00:00")
    _seed_opportunity(store, tenant=tenant, opportunity_id="opp-orphan")

    orphans = find_orphaned_runs(store, older_than=timedelta(hours=24), now=now)
    assert len(orphans) == 1
    assert orphans[0].run_id == run_id

    reap_tenant(store, run=orphans[0])

    remaining, _ = store.query_tenant_scan("opportunities", tenant=tenant)
    assert remaining == []


# -- the demo tenant must never be written by an eval run ------------------------------------


def test_demo_tenant_is_never_written_across_the_full_allocate_seed_teardown_lifecycle(
    store: Store,
) -> None:
    """Allocate -> seed -> teardown with a demo-writing fake seeder leaves demo untouched, and no
    allocated tenant equals demo.
    """
    run_id = allocate_run_id()
    tenant = tenant_for_run(run_id)
    assert tenant != DEMO_TENANT

    _seed_opportunity(store, tenant=DEMO_TENANT, opportunity_id="demo-preexisting")

    def seeder_that_only_touches_its_given_tenant(seed_store: Store, *, tenant: str) -> None:
        _seed_opportunity(seed_store, tenant=tenant, opportunity_id="eval-seeded")

    seed_tenant(store, tenant=tenant, seeder=seeder_that_only_touches_its_given_tenant)
    teardown_tenant(store, tenant=tenant)

    demo_items, _ = store.query_tenant_scan("opportunities", tenant=DEMO_TENANT)
    assert [item["opportunity_id"] for item in demo_items] == ["demo-preexisting"]

    eval_items, _ = store.query_tenant_scan("opportunities", tenant=tenant)
    assert eval_items == []


def test_seed_tenant_structurally_cannot_target_demo_even_with_a_permissive_seeder(
    store: Store,
) -> None:
    """seed_tenant refuses a demo tenant argument before the seeder is called."""
    seeder_invoked = False

    def permissive_seeder(seed_store: Store, *, tenant: str) -> None:
        nonlocal seeder_invoked
        seeder_invoked = True
        _seed_opportunity(seed_store, tenant=tenant, opportunity_id="should-never-exist")

    with pytest.raises(ValueError):
        seed_tenant(store, tenant=DEMO_TENANT, seeder=permissive_seeder)

    assert seeder_invoked is False
    demo_items, _ = store.query_tenant_scan("opportunities", tenant=DEMO_TENANT)
    assert demo_items == []
