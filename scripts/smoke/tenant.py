# SPDX-License-Identifier: MIT
"""Smoke-test tenant lifecycle, built on the eval tenancy primitives in :mod:`eval.tenancy`.

A smoke run's id is ``smoke-<uuid4 hex>`` and maps to an ordinary ``eval-<run_id>`` tenant, so an
abandoned smoke tenant is reapable by ``scripts/reap_eval_tenants.py`` like any orphaned eval run.
:func:`is_smoke_run_id` tells a smoke run's catalog row apart from a real eval run's.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from crm.store.store import Store
from eval.tenancy import (
    FixtureSeeder,
    TenantTeardownError,
    seed_tenant,
    teardown_tenant,
    tenant_for_run,
)

#: Marker segment identifying a smoke run's id, distinct from an ordinary eval run's id.
_SMOKE_RUN_ID_PREFIX = "smoke-"

#: The catalog table `eval.tenancy.find_orphaned_runs` scans, matching `eval.tenancy._EVAL_RUNS_TABLE`.
_EVAL_RUNS_TABLE = "eval_runs"


@dataclass(frozen=True, slots=True)
class SmokeTenant:
    """A smoke-test tenant: its run id, the eval tenant it maps to, and the catalog row key.

    Attributes:
        run_id: The smoke run's id, always ``smoke-<hex>`` (see :func:`is_smoke_run_id`).
        tenant: The eval tenant the run id maps to via the unchanged
            :func:`eval.tenancy.tenant_for_run` — a genuine ``eval-<run_id>`` tenant.
    """

    run_id: str
    tenant: str


def allocate_smoke_run_id() -> str:
    """Return a new smoke-run id: ``smoke-<uuid4 hex>``.

    Draws from :func:`uuid.uuid4`, the same random source :func:`eval.tenancy.allocate_run_id` uses.
    """
    return f"{_SMOKE_RUN_ID_PREFIX}{uuid.uuid4().hex}"


def is_smoke_run_id(run_id: str) -> bool:
    """Return whether ``run_id`` was allocated by :func:`allocate_smoke_run_id`."""
    return run_id.startswith(_SMOKE_RUN_ID_PREFIX)


def allocate_smoke_tenant(store: Store) -> SmokeTenant:
    """Allocate a smoke tenant and record its ``eval_runs`` catalog row.

    The row has a real eval run's shape (``run_id``, ``status="running"``, ``started_at``), so
    :func:`eval.tenancy.find_orphaned_runs` discovers an abandoned smoke tenant. The write overwrites
    at the ``run_id`` key, so a retry leaves one row.

    Args:
        store: The store to record the catalog row into, supplied by the caller.

    Returns:
        The newly allocated :class:`SmokeTenant`.
    """
    run_id = allocate_smoke_run_id()
    tenant = tenant_for_run(run_id)
    _put_eval_run_row(store, run_id=run_id, status="running")
    return SmokeTenant(run_id=run_id, tenant=tenant)


def _put_eval_run_row(store: Store, *, run_id: str, status: str) -> None:
    store.put_item(
        _EVAL_RUNS_TABLE,
        {
            "run_id": run_id,
            "status": status,
            "started_at": datetime.now(UTC).isoformat(),
        },
    )


def seed_smoke_tenant(
    store: Store, smoke_tenant: SmokeTenant, *, seeder: FixtureSeeder | None = None
) -> None:
    """Seed the deterministic fixture corpus beneath ``smoke_tenant``.

    Delegates to :func:`eval.tenancy.seed_tenant`, which refuses the ``demo`` tenant and is
    idempotent for a repeated call on the same tenant.

    Args:
        store: The store to seed into.
        smoke_tenant: The tenant to seed, from :func:`allocate_smoke_tenant`.
        seeder: Optional override of the fixture-generator entry point, for tests.
    """
    seed_tenant(store, tenant=smoke_tenant.tenant, seeder=seeder)


def teardown_smoke_tenant(store: Store, smoke_tenant: SmokeTenant) -> dict[str, int]:
    """Tear down ``smoke_tenant`` by scoped delete and mark its catalog row torn down.

    Delegates to :func:`eval.tenancy.teardown_tenant`. On success the ``eval_runs`` catalog row is
    set to ``status="torn_down"``; on failure the error propagates and the row stays
    ``status="running"``, so the tenant remains a reapable orphan.

    Args:
        store: The store to delete from.
        smoke_tenant: The tenant to tear down, from :func:`allocate_smoke_tenant`.

    Returns:
        Per-table item-deletion counts, as returned by :func:`eval.tenancy.teardown_tenant`.

    Raises:
        TenantTeardownError: The underlying deletion raised. Propagated unchanged.
    """
    deleted = teardown_tenant(store, tenant=smoke_tenant.tenant)
    _put_eval_run_row(store, run_id=smoke_tenant.run_id, status="torn_down")
    return deleted


__all__ = [
    "SmokeTenant",
    "TenantTeardownError",
    "allocate_smoke_run_id",
    "allocate_smoke_tenant",
    "is_smoke_run_id",
    "seed_smoke_tenant",
    "teardown_smoke_tenant",
]
