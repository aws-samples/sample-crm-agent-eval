# SPDX-License-Identifier: MIT
"""Integration: unit tenants on DynamoDB Local are isolated, torn down, and reaped.

Seeds two unit tenants with the real fixture corpus, writes through the real `update_contact`
handler in one, and checks the other is untouched. Teardown and the real reaper `main` each leave
zero rows, and `canary` stays empty.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import scripts.reap_eval_tenants as reaper
from fixtures.generate import scoped_id

from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import get_contact, update_contact
from crm.store.schema import TENANT_SCOPED_TABLES
from crm.store.store import Store
from eval.tenancy import (
    RUN_STATUS_TORN_DOWN,
    allocate_run_id,
    find_canary_records,
    record_run_started,
    seed_unit_tenants,
    teardown_unit_tenants,
    tenant_for_run,
    unit_tenants_for_run,
)

_CONTACT = "cont-0001"
_NEW_PHONE = "+1-555-010-4242"


def _identity_event(tenant: str, args: dict[str, Any]) -> dict[str, Any]:
    identity = {
        "rep_id": "rep-unit-tenant-test",
        "book": [],
        "full_visibility": True,
        "principal_type": "user",
        "session_id": "sess-unit-tenant",
        "source": "gateway",
        "tenant": tenant,
    }
    return identity_event(IDENTITY_KEY, identity, args)


def _row_counts(store: Store, tenant: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in sorted(TENANT_SCOPED_TABLES):
        total = 0
        cursor: dict[str, object] | None = None
        while True:
            items, cursor = store.query_tenant_scan(
                table, tenant=tenant, exclusive_start_key=cursor
            )
            total += len(items)
            if cursor is None:
                break
        counts[table] = total
    return counts


def _phone(store: Store, tenant: str) -> object:
    result = get_contact.handler(
        _identity_event(tenant, {"contact_id": scoped_id(tenant, _CONTACT)}), store=store
    )
    assert result["statusCode"] == 200, result
    return result["body"]["contact"].get("phone")


def test_unit_tenants_are_isolated_and_teardown_leaves_zero_rows(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """A write in one unit tenant is invisible in the other; teardown empties both."""
    store, _ = dynamodb_local_store
    run_id = allocate_run_id()
    first, second = seed_unit_tenants(store, run_id=run_id, arm_count=2, repeats=1)
    try:
        first_counts = _row_counts(store, first)
        assert sum(first_counts.values()) > 0
        assert _row_counts(store, second) == first_counts
        second_phone_before = _phone(store, second)
        assert second_phone_before != _NEW_PHONE

        written = update_contact.handler(
            _identity_event(
                first,
                {
                    "contact_id": scoped_id(first, _CONTACT),
                    "fields": {"phone": _NEW_PHONE},
                    "transaction_key": uuid.uuid4().hex + uuid.uuid4().hex[:4],
                },
            ),
            store=store,
        )
        assert written["statusCode"] == 200, written

        assert _phone(store, first) == _NEW_PHONE
        assert _phone(store, second) == second_phone_before
        cross = get_contact.handler(
            _identity_event(second, {"contact_id": scoped_id(first, _CONTACT)}), store=store
        )
        assert cross["statusCode"] == 404
    finally:
        teardown_unit_tenants(store, tenants=[first, second])

    assert sum(_row_counts(store, first).values()) == 0
    assert sum(_row_counts(store, second).values()) == 0
    assert find_canary_records(store) == []


def test_the_reaper_removes_unit_tenants_recorded_on_an_orphaned_run_row(
    dynamodb_local_store: tuple[Store, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An orphaned run's recorded unit tenants are reaped by the real reaper `main`."""
    store, _ = dynamodb_local_store
    run_id = allocate_run_id()
    units = unit_tenants_for_run(run_id, arm_count=1, repeats=2)
    record_run_started(
        store,
        run_id=run_id,
        tenant=tenant_for_run(run_id),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        unit_tenants=units,
    )
    seed_unit_tenants(store, run_id=run_id, arm_count=1, repeats=2)
    assert all(sum(_row_counts(store, tenant).values()) > 0 for tenant in units)

    monkeypatch.setattr(reaper, "_build_store", lambda **_kwargs: store)
    exit_code = reaper.main(["--older-than", "1h", "--yes"])
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert f"unit_tenants={','.join(units)}" in captured.out
    assert all(sum(_row_counts(store, tenant).values()) == 0 for tenant in units)
    row = store.get_item("eval_runs", {"run_id": run_id})
    assert row is not None and row["status"] == RUN_STATUS_TORN_DOWN
    assert find_canary_records(store) == []
