# SPDX-License-Identifier: MIT
"""Integration tests for rich, idempotent ``demo`` seeding, against DynamoDB Local.

Every assertion reads rows back through ``crm.store.store.Store`` and the tenant-prefixed GSIs.
"""

from __future__ import annotations

from typing import Any

import pytest
from fixtures.demo_seed import (
    HIGH_TOUCH_ACTIVITY_COUNT,
    DemoSeedSummary,
    assert_activity_set_truncates,
    seed_demo,
)
from fixtures.generate import CLOSED_STAGES, STAGES

from crm.authz import DEMO_TENANT, Identity
from crm.handlers.list_activities import _DEFAULT_LIMIT as ACTIVITY_PAGE_SIZE
from crm.handlers.list_activities import _list_activities
from crm.store.keys import tenant_prefixed
from crm.store.schema import TENANT_SCOPED_TABLES
from crm.store.store import Store
from eval.tenancy import CANARY_TENANT, seed_demo_tenant

#: The tenant these tests seed; each test gets its own freshly-created table prefix.
_TENANT = DEMO_TENANT


def _count_rows(store: Store, table: str, tenant: str) -> int:
    """Count every row under ``tenant`` in ``table``, paginating the tenant-wide index."""
    total = 0
    cursor: dict[str, object] | None = None
    while True:
        items, cursor = store.query_tenant_scan(table, tenant=tenant, exclusive_start_key=cursor)
        total += len(items)
        if cursor is None:
            return total


def _row_counts(store: Store, tenant: str) -> dict[str, int]:
    """Row counts per tenant-scoped table, the shape an idempotency comparison needs."""
    return {table: _count_rows(store, table, tenant) for table in sorted(TENANT_SCOPED_TABLES)}


@pytest.fixture
def seeded(dynamodb_local_store: tuple[Store, str]) -> tuple[Store, DemoSeedSummary]:
    """A store with ``demo`` seeded once, and the summary of that seed."""
    store, _prefix = dynamodb_local_store
    return store, seed_demo(store, tenant=_TENANT)


# -- idempotency: re-running produces no duplicates ----------------------------------------------


def test_seeding_twice_leaves_every_row_count_unchanged(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    store, _prefix = dynamodb_local_store

    seed_demo(store, tenant=_TENANT)
    after_first = _row_counts(store, _TENANT)
    seed_demo(store, tenant=_TENANT)
    after_second = _row_counts(store, _TENANT)

    assert after_first == after_second
    assert sum(after_first.values()) > 0, "a vacuous zero-row seed would make this comparison empty"


def test_seeding_twice_leaves_the_same_row_identifiers(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """Seeding twice returns identical summaries, row identifiers included."""
    store, _prefix = dynamodb_local_store

    first = seed_demo(store, tenant=_TENANT)
    second = seed_demo(store, tenant=_TENANT)

    assert first == second


def test_reseeding_demo_never_writes_the_canary_tenant(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    store, _prefix = dynamodb_local_store

    seed_demo(store, tenant=_TENANT)
    seed_demo(store, tenant=_TENANT)

    assert _row_counts(store, CANARY_TENANT) == dict.fromkeys(sorted(TENANT_SCOPED_TABLES), 0)


# -- richness: every stage, a departed contact, a truncating result set ---------------------------


def test_every_stage_including_the_closed_ones_has_a_seeded_opportunity(
    seeded: tuple[Store, DemoSeedSummary],
) -> None:
    store, summary = seeded

    for stage in STAGES:
        items, _ = store.query_gsi(
            "opportunities",
            "gsi_stage",
            partition_value=tenant_prefixed(_TENANT, stage),
        )
        assert items, f"stage {stage!r} has no seeded opportunity"

    assert set(CLOSED_STAGES) <= set(summary.stages_present)


def test_a_departed_contact_is_readable_from_the_seeded_tenant(
    seeded: tuple[Store, DemoSeedSummary],
) -> None:
    store, summary = seeded

    assert summary.departed_contact_ids

    for contact_id in summary.departed_contact_ids:
        contact = store.get_item("contacts", {"contact_id": contact_id})
        assert contact is not None
        assert contact["status"] == "departed"
        assert contact["tenant"] == _TENANT


def test_the_high_touch_opportunity_truncates_a_default_activity_list(
    seeded: tuple[Store, DemoSeedSummary],
) -> None:
    """A default activity list for the high-touch opportunity, through the handler, is truncated."""
    store, summary = seeded
    identity = Identity(rep_id="rep-ashford", full_visibility=True, tenant=_TENANT)

    result = _list_activities(
        store, identity, {"opportunity_id": summary.high_touch_opportunity_id}
    )

    assert len(result["activities"]) == ACTIVITY_PAGE_SIZE
    assert result["next_cursor"], "a truncated activity list must carry a resume cursor"
    assert HIGH_TOUCH_ACTIVITY_COUNT > ACTIVITY_PAGE_SIZE


def test_the_full_high_touch_activity_set_is_reachable_by_paging(
    seeded: tuple[Store, DemoSeedSummary],
) -> None:
    """Every seeded high-touch activity is readable through the opportunity index."""
    store, summary = seeded

    items, _ = store.query_gsi(
        "activities",
        "gsi_opportunity",
        partition_value=tenant_prefixed(_TENANT, summary.high_touch_opportunity_id),
    )
    seeded_ids = {str(item["activity_id"]) for item in items}

    assert set(summary.high_touch_activity_ids) <= seeded_ids


def test_pending_proposals_are_readable_through_the_status_index(
    seeded: tuple[Store, DemoSeedSummary],
) -> None:
    store, summary = seeded

    items, _ = store.query_gsi(
        "proposals", "gsi_status", partition_value=tenant_prefixed(_TENANT, "pending")
    )
    pending_ids = {str(item["proposal_id"]) for item in items}

    assert summary.pending_proposal_ids
    assert set(summary.pending_proposal_ids) <= pending_ids
    # The status index does not project the snapshots, so they are read from the base table.
    for proposal_id in summary.pending_proposal_ids:
        proposal = store.get_item("proposals", {"proposal_id": proposal_id})
        assert proposal is not None
        assert proposal["status"] == "pending"
        assert proposal["before_snapshot"], "a seeded proposal must carry a real before-snapshot"
        assert proposal["after_payload"], "a seeded update proposal must carry an after-payload"


def test_audit_history_is_readable_through_the_tenant_index(
    seeded: tuple[Store, DemoSeedSummary],
) -> None:
    store, summary = seeded

    items, _ = store.query_tenant_scan("audit_records", tenant=_TENANT)
    audit_ids = {str(item["audit_id"]) for item in items}

    assert summary.audit_record_ids
    assert set(summary.audit_record_ids) <= audit_ids


def test_the_empty_account_really_has_no_contacts_or_opportunities(
    seeded: tuple[Store, DemoSeedSummary],
) -> None:
    """The seeded empty account has no contacts, opportunities or activities beneath it."""
    store, summary = seeded

    account = store.get_item("accounts", {"account_id": summary.empty_account_id})
    assert account is not None
    assert account["tenant"] == _TENANT

    partition = tenant_prefixed(_TENANT, summary.empty_account_id)
    for table, gsi in (
        ("contacts", "gsi_account"),
        ("opportunities", "gsi_account"),
        ("activities", "gsi_account"),
    ):
        items, _ = store.query_gsi(table, gsi, partition_value=partition)
        assert items == [], f"the deliberately empty account has {table} beneath it"


# -- the eval-tenancy entry point is the only sanctioned writer of demo ---------------------------


def test_seed_demo_tenant_writes_demo_through_the_eval_tenancy_entry_point(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """`eval.tenancy.seed_demo_tenant` seeds the ``demo`` tenant."""
    store, _prefix = dynamodb_local_store

    summary = seed_demo_tenant(store)

    assert isinstance(summary, DemoSeedSummary)
    assert summary.tenant == DEMO_TENANT
    assert _count_rows(store, "opportunities", DEMO_TENANT) > 0


def test_the_truncation_precondition_is_checked_against_the_handlers_own_page_size() -> None:
    """The seeded activity set exceeds the handler's own default page size."""
    assert_activity_set_truncates()


def test_seed_demo_refuses_the_canary_bystander(dynamodb_local_store: tuple[Store, str]) -> None:
    """`seed_demo` refuses the `canary` tenant and writes no row under it."""
    store, _prefix = dynamodb_local_store

    with pytest.raises(ValueError, match=CANARY_TENANT):
        seed_demo(store, tenant=CANARY_TENANT)

    assert _row_counts(store, CANARY_TENANT) == dict.fromkeys(sorted(TENANT_SCOPED_TABLES), 0)


def test_the_canary_refusal_precedes_every_write(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """The canary refusal happens before any row is written to any tenant."""
    store, _prefix = dynamodb_local_store

    with pytest.raises(ValueError):
        seed_demo(store, tenant=CANARY_TENANT)

    assert sum(_row_counts(store, CANARY_TENANT).values()) == 0
    assert sum(_row_counts(store, DEMO_TENANT).values()) == 0, "nor any other tenant"


def test_seed_demo_still_accepts_an_ordinary_isolated_tenant(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """A non-demo tenant is seedable for tests, and the rows carry that tenant, not ``demo``."""
    store, _prefix = dynamodb_local_store

    summary = seed_demo(store, tenant="eval-demo-seed-test")

    assert summary.tenant == "eval-demo-seed-test"
    assert _count_rows(store, "opportunities", DEMO_TENANT) == 0


def test_the_stub_seeder_seam_is_not_what_the_product_path_uses(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """`seed_demo_tenant`'s injectable seam exists, and it always passes the ``demo`` tenant."""
    store, _prefix = dynamodb_local_store
    seen: list[str] = []

    def _record(_store: Any, *, tenant: str) -> str:
        seen.append(tenant)
        return "stub"

    assert seed_demo_tenant(store, seeder=_record) == "stub"
    assert seen == [DEMO_TENANT]
