# SPDX-License-Identifier: MIT
"""Unit tenants per (arm, repeat): naming, guards, catalog recording and reaping."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.authz import DEMO_TENANT
from crm.store.keys import tenant_prefixed
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.task_ids import LOCAL_ID_KINDS, compose_ids_in_text
from eval.tenancy import (
    CANARY_TENANT,
    TenantTeardownError,
    find_orphaned_runs,
    reap_tenant,
    record_run_started,
    record_run_torn_down,
    run_id_for_tenant,
    seed_unit_tenants,
    teardown_unit_tenants,
    tenant_for_run,
    unit_tenant,
    unit_tenants_for_run,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SWEEP_IAM_TF = _REPO_ROOT / "infra" / "eval_sfn.tf"
_RUN_ID = "0123456789abcdef0123456789abcdef"
_OLD = "2026-05-01T00:00:00+00:00"


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _put_opportunity(store: Store, *, tenant: str, opportunity_id: str) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": opportunity_id,
            "tenant": tenant,
            "tenant_account_id": tenant_prefixed(tenant, "acct-1"),
            "tenant_owner_rep_id": tenant_prefixed(tenant, "rep-1"),
            "tenant_stage": tenant_prefixed(tenant, "Discovery"),
            "account_id": "acct-1",
            "owner_rep_id": "rep-1",
            "name": "Deal",
            "name_lower": "deal",
            "stage": "Discovery",
            "value": 1,
            "created_at": _OLD,
        },
    )


def _count(store: Store, tenant: str) -> int:
    items, _ = store.query_tenant_scan("opportunities", tenant=tenant)
    return len(items)


# --- naming -------------------------------------------------------------------------------


def test_unit_tenant_name_carries_run_arm_and_repeat() -> None:
    """`unit_tenant` renders `eval-<run_id>-a<arm>r<repeat>`."""
    assert unit_tenant(_RUN_ID, arm_index=1, repeat_index=4) == f"eval-{_RUN_ID}-a1r4"


@pytest.mark.parametrize(("arm", "repeat"), [(0, 0), (1, 0), (0, 4), (11, 23)])
def test_run_id_for_tenant_round_trips_a_unit_tenant(arm: int, repeat: int) -> None:
    """`run_id_for_tenant` recovers the run id from a unit tenant."""
    assert run_id_for_tenant(unit_tenant(_RUN_ID, arm_index=arm, repeat_index=repeat)) == _RUN_ID


@pytest.mark.parametrize("run_id", [_RUN_ID, "smoke-scope-probe", "x"])
def test_run_id_for_tenant_still_round_trips_a_run_tenant(run_id: str) -> None:
    """A run-level `eval-<run_id>` tenant round-trips to its run id."""
    assert run_id_for_tenant(tenant_for_run(run_id)) == run_id


def test_run_id_for_tenant_keeps_the_empty_run_id_and_prefix_refusal() -> None:
    """`eval-` alone yields `""`; a non-eval tenant raises."""
    assert run_id_for_tenant("eval-") == ""
    with pytest.raises(ValueError, match="not an eval tenant"):
        run_id_for_tenant(DEMO_TENANT)


def test_a_run_id_that_would_parse_as_a_unit_tenant_is_refused() -> None:
    """A run id ending in `-a<n>r<n>` cannot round-trip, so both constructors refuse it."""
    with pytest.raises(ValueError, match="unit-tenant suffix"):
        tenant_for_run("abc-a0r1")
    with pytest.raises(ValueError, match="unit-tenant suffix"):
        unit_tenant("abc-a0r1", arm_index=0, repeat_index=0)


@pytest.mark.parametrize(("arm", "repeat"), [(-1, 0), (0, -1)])
def test_negative_unit_indices_are_refused(arm: int, repeat: int) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        unit_tenant(_RUN_ID, arm_index=arm, repeat_index=repeat)


def test_unit_tenants_for_run_is_arm_major_and_distinct() -> None:
    """Two arms × three repeats give six distinct tenants, arm-major."""
    tenants = unit_tenants_for_run(_RUN_ID, arm_count=2, repeats=3)
    assert [t.removeprefix(f"eval-{_RUN_ID}-") for t in tenants] == [
        "a0r0",
        "a0r1",
        "a0r2",
        "a1r0",
        "a1r1",
        "a1r2",
    ]
    assert len(set(tenants)) == 6


@pytest.mark.parametrize(("arms", "repeats"), [(0, 1), (1, 0)])
def test_unit_tenants_for_run_refuses_an_empty_matrix(arms: int, repeats: int) -> None:
    with pytest.raises(ValueError, match=">= 1"):
        unit_tenants_for_run(_RUN_ID, arm_count=arms, repeats=repeats)


# --- IAM pattern contract -------------------------------------------------------------------


def _sweep_seed_key_patterns() -> dict[str, str]:
    """Parse `sweep_seed_key_patterns` from the sweep role's Terraform."""
    text = _SWEEP_IAM_TF.read_text(encoding="utf-8")
    block = re.search(r"sweep_seed_key_patterns\s*=\s*\{(?P<body>[^}]*)\}", text)
    assert block is not None, f"sweep_seed_key_patterns not found in {_SWEEP_IAM_TF}"
    return dict(re.findall(r'(\w+)\s*=\s*"([^"]+)"', block.group("body")))


def _sweep_tenant_query_keys() -> list[str]:
    text = _SWEEP_IAM_TF.read_text(encoding="utf-8")
    block = re.search(r"sweep_control_tenant_query_keys\s*=\s*\[(?P<body>[^\]]*)\]", text)
    assert block is not None
    return re.findall(r'"([^"]+)"', block.group("body"))


def test_scoped_ids_under_a_unit_tenant_match_the_sweep_iam_key_patterns() -> None:
    """Every kind's scoped id under a unit tenant matches the IAM `<kind>-eval-*` pattern."""
    patterns = _sweep_seed_key_patterns()
    assert set(patterns.values()) == {f"{kind}-eval-*" for kind in LOCAL_ID_KINDS}
    tenant = unit_tenant(_RUN_ID, arm_index=1, repeat_index=4)
    for kind in LOCAL_ID_KINDS:
        scoped = compose_ids_in_text(f"{kind}-0000", tenant=tenant)
        assert scoped == f"{kind}-{tenant}-0000"
        assert fnmatchcase(scoped, f"{kind}-eval-*"), scoped


def test_a_unit_tenant_matches_the_sweep_tenant_query_pattern() -> None:
    """The unit tenant itself, and a tenant-prefixed GSI key, match the `eval-*` LeadingKeys."""
    assert "eval-*" in _sweep_tenant_query_keys()
    tenant = unit_tenant(_RUN_ID, arm_index=0, repeat_index=0)
    assert fnmatchcase(tenant, "eval-*")
    assert fnmatchcase(tenant_prefixed(tenant, "acct-1"), "eval-*")


# --- guards ---------------------------------------------------------------------------------


@pytest.mark.parametrize("protected", [DEMO_TENANT, CANARY_TENANT])
def test_teardown_unit_tenants_refuses_a_protected_tenant(store: Store, protected: str) -> None:
    with pytest.raises(ValueError, match="refusing"):
        teardown_unit_tenants(store, tenants=[protected])


@pytest.mark.parametrize("protected", [DEMO_TENANT, CANARY_TENANT])
def test_record_run_started_refuses_a_protected_unit_tenant(store: Store, protected: str) -> None:
    with pytest.raises(ValueError, match="refusing"):
        record_run_started(
            store, run_id=_RUN_ID, tenant=tenant_for_run(_RUN_ID), unit_tenants=[protected]
        )
    assert store.get_item("eval_runs", {"run_id": _RUN_ID}) is None


def test_record_run_started_refuses_another_runs_unit_tenant(store: Store) -> None:
    foreign = unit_tenant("f" * 32, arm_index=0, repeat_index=0)
    with pytest.raises(ValueError, match="not a unit tenant of run"):
        record_run_started(
            store, run_id=_RUN_ID, tenant=tenant_for_run(_RUN_ID), unit_tenants=[foreign]
        )


def test_seed_unit_tenants_seeds_each_tenant_once_in_order(store: Store) -> None:
    """The seeder is called once per unit tenant, arm-major, and the tenants are returned."""
    seeded: list[str] = []

    def seeder(_store: Store, *, tenant: str) -> None:
        seeded.append(tenant)

    tenants = seed_unit_tenants(store, run_id=_RUN_ID, arm_count=2, repeats=2, seeder=seeder)
    assert tenants == unit_tenants_for_run(_RUN_ID, arm_count=2, repeats=2)
    assert seeded == list(tenants)


# --- catalog recording ----------------------------------------------------------------------


def test_record_run_started_records_unit_tenants_only_when_given(store: Store) -> None:
    """A call without unit tenants writes no `unit_tenants`; a unit-tenant call adds them."""
    record_run_started(store, run_id="legacy", tenant=tenant_for_run("legacy"))
    legacy = store.get_item("eval_runs", {"run_id": "legacy"})
    assert legacy is not None and "unit_tenants" not in legacy

    units = unit_tenants_for_run(_RUN_ID, arm_count=2, repeats=1)
    record_run_started(store, run_id=_RUN_ID, tenant=tenant_for_run(_RUN_ID), unit_tenants=units)
    row = store.get_item("eval_runs", {"run_id": _RUN_ID})
    assert row is not None
    assert row["unit_tenants"] == list(units)


def test_record_run_torn_down_preserves_unit_tenants(store: Store) -> None:
    units = unit_tenants_for_run(_RUN_ID, arm_count=1, repeats=2)
    record_run_torn_down(store, run_id=_RUN_ID, tenant=tenant_for_run(_RUN_ID), unit_tenants=units)
    row = store.get_item("eval_runs", {"run_id": _RUN_ID})
    assert row is not None
    assert (row["status"], row["unit_tenants"]) == ("torn_down", list(units))


def test_find_orphaned_runs_reads_unit_tenants_from_the_base_row(store: Store) -> None:
    """`gsi_status` does not project `unit_tenants`, so the orphan carries the base row's list."""
    units = unit_tenants_for_run(_RUN_ID, arm_count=2, repeats=2)
    record_run_started(
        store,
        run_id=_RUN_ID,
        tenant=tenant_for_run(_RUN_ID),
        started_at=datetime(2026, 5, 1, tzinfo=UTC),
        unit_tenants=units,
    )
    record_run_started(
        store,
        run_id="legacy",
        tenant=tenant_for_run("legacy"),
        started_at=datetime(2026, 5, 2, tzinfo=UTC),
    )

    orphans = find_orphaned_runs(
        store, older_than=timedelta(hours=1), now=datetime(2026, 6, 1, tzinfo=UTC)
    )

    assert [(o.run_id, o.unit_tenants) for o in orphans] == [(_RUN_ID, units), ("legacy", ())]


def test_find_orphaned_runs_refuses_a_tampered_unit_tenant_list(store: Store) -> None:
    """A row naming a tenant outside its run fails loudly instead of being reaped."""
    store.put_item(
        "eval_runs",
        {
            "run_id": _RUN_ID,
            "tenant": tenant_for_run(_RUN_ID),
            "status": "running",
            "started_at": _OLD,
            "unit_tenants": [DEMO_TENANT],
        },
    )
    with pytest.raises(ValueError, match="refusing"):
        find_orphaned_runs(
            store, older_than=timedelta(hours=1), now=datetime(2026, 6, 1, tzinfo=UTC)
        )


# --- teardown and reaping -------------------------------------------------------------------


def test_teardown_unit_tenants_attempts_every_tenant_then_raises_the_first_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing first tenant does not stop the second from being torn down."""
    first, second = unit_tenants_for_run(_RUN_ID, arm_count=1, repeats=2)
    _put_opportunity(store, tenant=second, opportunity_id="opp-second")
    real_delete = store.delete_tenant
    attempted: list[str] = []

    def delete(tenant: str) -> dict[str, int]:
        attempted.append(tenant)
        if tenant == first:
            raise RuntimeError("simulated failure")
        return real_delete(tenant)

    monkeypatch.setattr(store, "delete_tenant", delete)
    with pytest.raises(TenantTeardownError) as raised:
        teardown_unit_tenants(store, tenants=[first, second])

    assert raised.value.tenant == first
    assert attempted == [first, second]
    assert _count(store, second) == 0


def test_reap_tenant_removes_the_recorded_unit_tenants_and_marks_the_row_terminal(
    store: Store,
) -> None:
    units = unit_tenants_for_run(_RUN_ID, arm_count=2, repeats=1)
    base = tenant_for_run(_RUN_ID)
    record_run_started(
        store,
        run_id=_RUN_ID,
        tenant=base,
        started_at=datetime(2026, 5, 1, tzinfo=UTC),
        unit_tenants=units,
    )
    for index, tenant in enumerate(units):
        _put_opportunity(store, tenant=tenant, opportunity_id=f"opp-unit-{index}")
    (orphan,) = find_orphaned_runs(
        store, older_than=timedelta(hours=1), now=datetime(2026, 6, 1, tzinfo=UTC)
    )

    deleted = reap_tenant(store, run=orphan)

    assert deleted.get("opportunities") == 2
    assert all(_count(store, tenant) == 0 for tenant in units)
    row: dict[str, Any] | None = store.get_item("eval_runs", {"run_id": _RUN_ID})
    assert row is not None
    assert (row["status"], row["unit_tenants"]) == ("torn_down", list(units))


def test_reap_tenant_leaves_the_row_running_when_a_unit_teardown_fails(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    (unit,) = unit_tenants_for_run(_RUN_ID, arm_count=1, repeats=1)
    record_run_started(
        store,
        run_id=_RUN_ID,
        tenant=tenant_for_run(_RUN_ID),
        started_at=datetime(2026, 5, 1, tzinfo=UTC),
        unit_tenants=[unit],
    )
    (orphan,) = find_orphaned_runs(
        store, older_than=timedelta(hours=1), now=datetime(2026, 6, 1, tzinfo=UTC)
    )
    real_delete = store.delete_tenant

    def delete(tenant: str) -> dict[str, int]:
        if tenant == unit:
            raise RuntimeError("simulated failure")
        return real_delete(tenant)

    monkeypatch.setattr(store, "delete_tenant", delete)
    with pytest.raises(TenantTeardownError):
        reap_tenant(store, run=orphan)

    row = store.get_item("eval_runs", {"run_id": _RUN_ID})
    assert row is not None and row["status"] == "running"
