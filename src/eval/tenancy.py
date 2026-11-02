# SPDX-License-Identifier: MIT
"""Eval tenancy: per-run allocation, idempotent seeding, scoped teardown, and orphan reaping.

Every eval run executes beneath its own ``eval-<run_id>`` tenant, seeded fresh and torn down by
scoped delete through :mod:`crm.store.store`, which enumerates each tenant-scoped table by its
``gsi_tenant`` index. A sweep may also isolate each (arm, repeat) unit in its own unit tenant
``eval-<run_id>-a<arm>r<repeat>`` (:func:`unit_tenant`), recorded on the run's ``eval_runs`` row so
the reaper finds it.

- :func:`allocate_run_id` returns a random 128-bit run id; allocation needs no network call.
- :func:`seed_tenant` delegates to the idempotent, tenant-scoped :mod:`fixtures.generate` entry
  point. :func:`seed_demo_tenant` is the only function in this module that writes ``demo``.
- :func:`teardown_tenant` wraps :meth:`crm.store.store.Store.delete_tenant` and raises
  :class:`TenantTeardownError` on failure.
- :func:`seed_unit_tenants` and :func:`teardown_unit_tenants` seed and tear down a run's unit
  tenants; teardown attempts every tenant before raising the first failure.
- :func:`find_orphaned_runs` and :func:`reap_tenant` back ``scripts/reap_eval_tenants.py``.

Every function that seeds, records against, or tears down a tenant refuses both
:data:`crm.authz.DEMO_TENANT` and :data:`CANARY_TENANT` with :class:`ValueError`, except
:func:`seed_demo_tenant`, which seeds ``demo``. The read-only :func:`assert_canary_empty` and the
pure :func:`run_id_for_tenant` are not subject to that guard.

``canary`` is never seeded. :func:`teardown_tenant` always asserts it is empty, and
:class:`CanaryTenantNotEmptyError` names the offending records.
``scripts/check_canary_tenant_empty.py`` asserts the same property independently.
"""

from __future__ import annotations

import importlib
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from crm.authz import DEMO_TENANT
from crm.store.schema import TENANT_SCOPED_TABLES, table_schema
from crm.store.store import Store

#: The bystander tenant. Never seeded by any code path; asserted empty by
#: :func:`assert_canary_empty` and, independently, by ``scripts/check_canary_tenant_empty.py``.
CANARY_TENANT: Final[str] = "canary"

#: Tenants no eval run may allocate, seed, or tear down.
PROTECTED_TENANTS: Final[frozenset[str]] = frozenset({DEMO_TENANT, CANARY_TENANT})

#: Prefix applied to a run ID to derive its tenant (``tenant`` = ``eval-<run_id>``).
_EVAL_TENANT_PREFIX = "eval-"

#: The catalog table recording one row per eval run, read by reaping and by the harness.
_EVAL_RUNS_TABLE = "eval_runs"

#: A unit tenant's suffix after its run id: ``-a<arm>r<repeat>``.
_UNIT_SUFFIX: Final[re.Pattern[str]] = re.compile(r"-a(?P<arm>\d+)r(?P<repeat>\d+)")

#: A unit tenant's body after the eval prefix: ``<run_id>-a<arm>r<repeat>``.
_UNIT_TENANT_BODY: Final[re.Pattern[str]] = re.compile(r"(?P<run_id>.+)" + _UNIT_SUFFIX.pattern)


def _reject_protected_tenant(tenant: str) -> None:
    """Raise :class:`ValueError` when ``tenant`` is ``demo`` or ``canary``."""
    if tenant == DEMO_TENANT:
        raise ValueError(
            f"refusing to operate on the demonstration tenant {DEMO_TENANT!r}: "
            "eval tenancy never targets demo data"
        )
    if tenant == CANARY_TENANT:
        raise ValueError(
            f"refusing to operate on the bystander tenant {CANARY_TENANT!r}: it is never seeded, "
            "and its provable emptiness is what makes it evidence of tenant isolation"
        )


def allocate_run_id() -> str:
    """Return a new random eval run identifier (:func:`uuid.uuid4` hex)."""
    return uuid.uuid4().hex


def tenant_for_run(run_id: str) -> str:
    """Return the tenant identifier for ``run_id``: ``"eval-<run_id>"``.

    Raises:
        ValueError: ``run_id`` is empty, or ends in a unit-tenant suffix (``-a0r0``).
    """
    _require_run_id(run_id)
    return f"{_EVAL_TENANT_PREFIX}{run_id}"


def _require_run_id(run_id: str) -> None:
    """Raise :class:`ValueError` for a ``run_id`` that is empty or would parse as a unit tenant."""
    if not run_id:
        raise ValueError("run_id must not be empty")
    if _UNIT_TENANT_BODY.fullmatch(run_id):
        raise ValueError(f"run_id {run_id!r} must not end in a unit-tenant suffix '-a<n>r<n>'")


def unit_tenant(run_id: str, *, arm_index: int, repeat_index: int) -> str:
    """Return the unit tenant for one (arm, repeat) of ``run_id``: ``'eval-<run_id>-a<arm>r<repeat>'``.

    Raises:
        ValueError: ``run_id`` is invalid for :func:`tenant_for_run`, or an index is negative.
    """
    if arm_index < 0 or repeat_index < 0:
        raise ValueError(
            f"unit indices must be non-negative: arm={arm_index} repeat={repeat_index}"
        )
    return f"{tenant_for_run(run_id)}-a{arm_index}r{repeat_index}"


def unit_tenants_for_run(run_id: str, *, arm_count: int, repeats: int) -> tuple[str, ...]:
    """Return every unit tenant of ``run_id``, arm-major (``a0r0``, ``a0r1``, …, ``a1r0``, …).

    Raises:
        ValueError: ``arm_count`` or ``repeats`` is below 1, or ``run_id`` is invalid.
    """
    if arm_count < 1 or repeats < 1:
        raise ValueError(f"arm_count and repeats must be >= 1: {arm_count=} {repeats=}")
    return tuple(
        unit_tenant(run_id, arm_index=arm, repeat_index=repeat)
        for arm in range(arm_count)
        for repeat in range(repeats)
    )


def run_id_for_tenant(tenant: str) -> str:
    """Return the run ID embedded in an eval tenant, the inverse of :func:`tenant_for_run` and of
    :func:`unit_tenant`.

    ``eval-`` alone yields ``""``; callers treat an empty run id as no eval run.

    Raises:
        ValueError: ``tenant`` does not carry the eval tenant prefix.
    """
    if not tenant.startswith(_EVAL_TENANT_PREFIX):
        raise ValueError(f"not an eval tenant: {tenant!r}")
    body = tenant[len(_EVAL_TENANT_PREFIX) :]
    unit = _UNIT_TENANT_BODY.fullmatch(body)
    return unit.group("run_id") if unit else body


def _validated_unit_tenants(run_id: str, unit_tenants: Sequence[str]) -> list[str]:
    """Return ``unit_tenants`` as a list after checking each is a unit tenant of ``run_id``.

    Raises:
        ValueError: An entry is protected, is not a unit tenant, or belongs to another run.
    """
    validated: list[str] = []
    for tenant in unit_tenants:
        _reject_protected_tenant(tenant)
        body = tenant[len(_EVAL_TENANT_PREFIX) :] if tenant.startswith(_EVAL_TENANT_PREFIX) else ""
        unit = _UNIT_TENANT_BODY.fullmatch(body)
        if unit is None or unit.group("run_id") != run_id:
            raise ValueError(f"{tenant!r} is not a unit tenant of run {run_id!r}")
        validated.append(tenant)
    return validated


#: The fixture generator's tenant-scoped, idempotent seeding entry point, consumed by
#: :func:`seed_tenant`.
FixtureSeeder = Callable[..., None]


def _default_seeder() -> FixtureSeeder:
    """Resolve ``fixtures.generate.seed_tenant``, imported lazily.

    Raises:
        ImportError: ``fixtures.generate`` is not importable, or does not expose a callable named
            ``seed_tenant``.
    """
    module = importlib.import_module("fixtures.generate")
    try:
        seeder = module.seed_tenant
    except AttributeError as exc:
        raise ImportError(
            "fixtures.generate must expose a seed_tenant(store, *, tenant) -> None callable"
        ) from exc
    if not callable(seeder):
        raise ImportError("fixtures.generate.seed_tenant is not callable")
    return seeder  # type: ignore[no-any-return]


#: The two statuses an ``eval_runs`` row carries. A ``running`` row is discoverable by
#: :func:`find_orphaned_runs`; ``torn_down`` is terminal. ``scripts/smoke/tenant.py`` writes the
#: same two values.
RUN_STATUS_RUNNING = "running"
RUN_STATUS_TORN_DOWN = "torn_down"


def record_run_started(
    store: Store,
    *,
    run_id: str,
    tenant: str,
    started_at: datetime | None = None,
    unit_tenants: Sequence[str] = (),
) -> None:
    """Record ``run_id``'s ``eval_runs`` row as ``running``, before its tenant is seeded.

    The row makes the run discoverable by :func:`find_orphaned_runs` if the sweep dies before
    teardown.

    Args:
        store: The store holding the catalog table.
        run_id: The run's identifier.
        tenant: The run's tenant, recorded so a reaper need not re-derive it.
        started_at: The recorded start time. Defaults to now, UTC.
        unit_tenants: The run's unit tenants, recorded as ``unit_tenants`` when non-empty so a
            reaper tears them down too. Record them before seeding any of them.

    Raises:
        ValueError: ``tenant`` or a unit tenant is protected, or a unit tenant is not one of
            ``run_id``'s.
    """
    _reject_protected_tenant(tenant)
    units = _validated_unit_tenants(run_id, unit_tenants)
    stamp = started_at if started_at is not None else datetime.now(UTC)
    row: dict[str, object] = {
        "run_id": run_id,
        "tenant": tenant,
        "status": RUN_STATUS_RUNNING,
        "started_at": stamp.isoformat(),
    }
    if units:
        row["unit_tenants"] = units
    store.put_item(_EVAL_RUNS_TABLE, row)


def record_run_torn_down(
    store: Store,
    *,
    run_id: str,
    tenant: str,
    started_at: datetime | None = None,
    torn_down_at: datetime | None = None,
    unit_tenants: Sequence[str] = (),
) -> None:
    """Record ``run_id``'s ``eval_runs`` row as ``torn_down``, after its tenant is deleted.

    Args:
        store: The store holding the catalog table.
        run_id: The run's identifier.
        tenant: The run's tenant.
        started_at: The run's original start time, re-recorded verbatim when supplied.
        torn_down_at: When teardown completed. Defaults to now, UTC.
        unit_tenants: The run's unit tenants, re-recorded when non-empty.

    Raises:
        ValueError: ``tenant`` or a unit tenant is protected, or a unit tenant is not one of
            ``run_id``'s.
    """
    _reject_protected_tenant(tenant)
    units = _validated_unit_tenants(run_id, unit_tenants)
    now = torn_down_at if torn_down_at is not None else datetime.now(UTC)
    started = started_at if started_at is not None else now
    row: dict[str, object] = {
        "run_id": run_id,
        "tenant": tenant,
        "status": RUN_STATUS_TORN_DOWN,
        "started_at": started.isoformat(),
        "torn_down_at": now.isoformat(),
    }
    if units:
        row["unit_tenants"] = units
    store.put_item(_EVAL_RUNS_TABLE, row)


def seed_tenant(store: Store, *, tenant: str, seeder: FixtureSeeder | None = None) -> None:
    """Seed the deterministic fixture corpus beneath ``tenant``, idempotently.

    Delegates to ``fixtures.generate.seed_tenant`` by default, which is itself idempotent.

    Args:
        store: The store to seed into.
        tenant: The eval tenant to seed beneath. Must not be a protected tenant.
        seeder: Override the resolved fixture-generator entry point.

    Raises:
        ValueError: ``tenant`` is a protected tenant.
        ImportError: No ``seeder`` was supplied and ``fixtures.generate`` is not importable or does
            not expose the expected entry point.
    """
    _reject_protected_tenant(tenant)
    resolved_seeder = seeder if seeder is not None else _default_seeder()
    resolved_seeder(store, tenant=tenant)


def seed_unit_tenants(
    store: Store,
    *,
    run_id: str,
    arm_count: int,
    repeats: int,
    seeder: FixtureSeeder | None = None,
) -> tuple[str, ...]:
    """Seed every unit tenant of ``run_id`` with :func:`seed_tenant`, in arm-major order.

    Args:
        store: The store to seed into.
        run_id: The run whose unit tenants are seeded.
        arm_count: Number of arms (at least 1).
        repeats: Number of repeats per arm (at least 1).
        seeder: Override the resolved fixture-generator entry point.

    Returns:
        The seeded unit tenants, as :func:`unit_tenants_for_run` orders them.

    Raises:
        ValueError: As :func:`unit_tenants_for_run`.
        Exception: A seeding failure propagates; tenants seeded before it remain, so callers tear
            down :func:`unit_tenants_for_run`'s full set.
    """
    tenants = unit_tenants_for_run(run_id, arm_count=arm_count, repeats=repeats)
    resolved_seeder = seeder if seeder is not None else _default_seeder()
    for tenant in tenants:
        seed_tenant(store, tenant=tenant, seeder=resolved_seeder)
    return tenants


#: The demo seeder's entry point: ``fixtures.demo_seed.seed_demo(store, *, tenant) -> object``.
#: Resolved by name and lazily, as :data:`FixtureSeeder` is.
DemoSeeder = Callable[..., object]


def _default_demo_seeder() -> DemoSeeder:
    """Resolve ``fixtures.demo_seed.seed_demo`` by name, imported lazily.

    Raises:
        ImportError: ``fixtures.demo_seed`` is not importable, or does not expose a callable named
            ``seed_demo``.
    """
    module = importlib.import_module("fixtures.demo_seed")
    try:
        seeder = module.seed_demo
    except AttributeError as exc:
        raise ImportError(
            "fixtures.demo_seed must expose a seed_demo(store, *, tenant) callable"
        ) from exc
    if not callable(seeder):
        raise ImportError("fixtures.demo_seed.seed_demo is not callable")
    return seeder  # type: ignore[no-any-return]


def seed_demo_tenant(store: Store, *, seeder: DemoSeeder | None = None) -> object:
    """Seed the demonstration tenant idempotently through ``fixtures.demo_seed.seed_demo``.

    The only function in this module that writes ``demo``. Every write lands at a deterministic key
    beneath the ``demo`` tenant, so re-running leaves the store unchanged.

    Args:
        store: The store to seed into.
        seeder: Override the resolved demo-seed entry point.

    Returns:
        Whatever the seeder returns — ``fixtures.demo_seed.DemoSeedSummary`` on the product path.

    Raises:
        ImportError: No ``seeder`` was supplied and ``fixtures.demo_seed`` is not importable or does
            not expose the expected entry point.
    """
    resolved = seeder if seeder is not None else _default_demo_seeder()
    return resolved(store, tenant=DEMO_TENANT)


@dataclass(frozen=True, slots=True)
class CanaryRecord:
    """One row found beneath the ``canary`` tenant.

    Attributes:
        table: Logical table the row was found in.
        key_attribute: The table's primary partition-key attribute name.
        key_value: That attribute's value on the offending row, or ``"<unknown>"`` when the row
            carries no readable key.
    """

    table: str
    key_attribute: str
    key_value: str

    def describe(self) -> str:
        """Render this record as ``table:key_attribute=key_value`` for a failure message."""
        return f"{self.table}:{self.key_attribute}={self.key_value}"


def find_canary_records(store: Store) -> list[CanaryRecord]:
    """Return every row present beneath :data:`CANARY_TENANT`, in a deterministic order.

    Enumerates :meth:`crm.store.store.Store.query_tenant_scan`, fully paginated, for every table in
    :data:`crm.store.schema.TENANT_SCOPED_TABLES`. A store read failure propagates.
    """
    found: list[CanaryRecord] = []
    for table in sorted(TENANT_SCOPED_TABLES):
        key_attribute = table_schema(table).partition_key
        cursor: dict[str, object] | None = None
        while True:
            items, cursor = store.query_tenant_scan(
                table, tenant=CANARY_TENANT, exclusive_start_key=cursor
            )
            for item in items:
                raw = item.get(key_attribute)
                found.append(
                    CanaryRecord(
                        table=table,
                        key_attribute=key_attribute,
                        key_value=str(raw) if raw is not None else "<unknown>",
                    )
                )
            if cursor is None:
                break
    return found


#: How many offending records a :class:`CanaryTenantNotEmptyError` message names inline; the total
#: count is always stated alongside.
_CANARY_RECORDS_NAMED_IN_MESSAGE: Final[int] = 20


class CanaryTenantNotEmptyError(Exception):
    """Raised when the ``canary`` bystander holds any row; the message names the offending records.

    Attributes:
        records: Every offending row found.
        tenant: The tenant the writing run was operating under, when the caller knew it.
        deleted: Per-table deletion counts when raised after a successful teardown; ``None`` when
            raised by a bare :func:`assert_canary_empty`.
    """

    def __init__(
        self,
        records: list[CanaryRecord],
        *,
        tenant: str | None = None,
        deleted: dict[str, int] | None = None,
    ) -> None:
        named = [record.describe() for record in records[:_CANARY_RECORDS_NAMED_IN_MESSAGE]]
        remainder = len(records) - len(named)
        super().__init__(
            f"the {CANARY_TENANT!r} tenant must be empty but holds {len(records)} record(s)"
            + (f", written during a sweep under tenant {tenant!r}" if tenant else "")
            + f": {named}"
            + (f" (+{remainder} more)" if remainder > 0 else "")
            + f". {CANARY_TENANT!r} is seeded by nothing, so a row beneath it means a run wrote "
            "outside its own tenant."
        )
        self.records = records
        self.tenant = tenant
        self.deleted = deleted


def assert_canary_empty(
    store: Store, *, tenant: str | None = None, deleted: dict[str, int] | None = None
) -> None:
    """Assert the ``canary`` bystander still holds no row.

    Args:
        store: The store to read the bystander through.
        tenant: The tenant the calling run was operating under, named in the failure.
        deleted: Per-table deletion counts a caller has already completed, carried onto the
            exception.

    Raises:
        CanaryTenantNotEmptyError: Any row exists beneath :data:`CANARY_TENANT`.
    """
    records = find_canary_records(store)
    if records:
        raise CanaryTenantNotEmptyError(records, tenant=tenant, deleted=deleted)


class TenantTeardownError(Exception):
    """Raised when scoped teardown of an eval tenant fails.

    Attributes:
        tenant: The tenant teardown was attempted against.
        deleted: Per-table deletion counts collected before the failure, when available.
    """

    def __init__(self, tenant: str, *, deleted: dict[str, int] | None, cause: Exception) -> None:
        super().__init__(
            f"teardown of eval tenant {tenant!r} failed: {cause}"
            + (f" (partial deletion counts: {deleted})" if deleted else "")
        )
        self.tenant = tenant
        self.deleted = deleted
        self.__cause__ = cause


def teardown_tenant(store: Store, *, tenant: str) -> dict[str, int]:
    """Delete every item belonging to ``tenant``, then assert the ``canary`` bystander is empty.

    Wraps :meth:`crm.store.store.Store.delete_tenant`. The canary assertion runs after the
    deletion and cannot be disabled.

    Args:
        store: The store to delete from.
        tenant: The eval tenant to tear down. Must not be a protected tenant.

    Returns:
        Per-table item-deletion counts, as returned by :meth:`Store.delete_tenant`.

    Raises:
        ValueError: ``tenant`` is a protected tenant.
        TenantTeardownError: The underlying deletion raised; the original error is its cause.
        CanaryTenantNotEmptyError: Deletion succeeded but the ``canary`` bystander holds a row.
            Not a :class:`TenantTeardownError` subclass.
        Exception: A store read failure while enumerating ``canary`` propagates unwrapped.
    """
    _reject_protected_tenant(tenant)
    try:
        deleted = store.delete_tenant(tenant)
    except Exception as exc:
        raise TenantTeardownError(tenant, deleted=None, cause=exc) from exc
    assert_canary_empty(store, tenant=tenant, deleted=deleted)
    return deleted


def _teardown_each(store: Store, tenants: Sequence[str]) -> tuple[dict[str, int], list[Exception]]:
    """Run :func:`teardown_tenant` on every tenant, returning summed counts and every failure."""
    totals: dict[str, int] = {}
    failures: list[Exception] = []
    for tenant in tenants:
        try:
            deleted = teardown_tenant(store, tenant=tenant)
        except CanaryTenantNotEmptyError as exc:
            deleted = exc.deleted or {}
            failures.append(exc)
        except (TenantTeardownError, ValueError) as exc:
            failures.append(exc)
            continue
        for table, count in deleted.items():
            totals[table] = totals.get(table, 0) + count
    return totals, failures


def teardown_unit_tenants(store: Store, *, tenants: Sequence[str]) -> None:
    """Tear down every tenant in ``tenants`` with :func:`teardown_tenant`, then raise the first failure.

    A failure on one tenant does not stop the others from being attempted.

    Raises:
        ValueError: A tenant is protected (raised for that tenant; the rest are still attempted).
        TenantTeardownError: A deletion failed.
        CanaryTenantNotEmptyError: Deletion succeeded but the ``canary`` bystander holds a row.
    """
    _, failures = _teardown_each(store, tenants)
    if failures:
        raise failures[0]


@dataclass(frozen=True, slots=True)
class OrphanedRun:
    """One eval run whose tenant is old enough to be considered abandoned.

    Attributes:
        run_id: The run's identifier, as recorded in the ``eval_runs`` row.
        tenant: The tenant to tear down, derived from ``run_id``.
        status: The run's last recorded status (for example ``"running"``), for log output only.
        started_at: The run's recorded start timestamp, as an ISO-8601 string.
        unit_tenants: The unit tenants recorded on the run's row, torn down with ``tenant``.
    """

    run_id: str
    tenant: str
    status: str
    started_at: str
    unit_tenants: tuple[str, ...] = ()


def find_orphaned_runs(
    store: Store, *, older_than: timedelta, now: datetime | None = None
) -> list[OrphanedRun]:
    """Return every recorded eval run whose tenant has not been torn down and is stale.

    Scans the ``eval_runs`` catalog for rows whose ``started_at`` is older than ``older_than``
    relative to ``now`` and whose ``status`` is not ``torn_down``.

    Args:
        store: The store to scan.
        older_than: Age threshold. A run started longer ago than this is a reaping candidate.
        now: Reference time for the age comparison. Defaults to the current UTC time.

    Returns:
        Every stale, not-yet-torn-down run, oldest first, with the ``unit_tenants`` read from its
        base row (``gsi_status`` does not project them).

    Raises:
        ValueError: A candidate row's ``unit_tenants`` is malformed or names another run's tenant.
    """
    reference_time = now if now is not None else datetime.now(UTC)
    cutoff = reference_time - older_than

    candidates: list[OrphanedRun] = []
    cursor: dict[str, object] | None = None
    while True:
        items, cursor = store.query_gsi(
            _EVAL_RUNS_TABLE,
            "gsi_status",
            partition_value=RUN_STATUS_RUNNING,
            exclusive_start_key=cursor,
        )
        for item in items:
            started_at_raw = item.get("started_at")
            run_id_raw = item.get("run_id")
            if not isinstance(started_at_raw, str) or not isinstance(run_id_raw, str):
                continue
            started_at = _parse_iso8601(started_at_raw)
            if started_at is None or started_at >= cutoff:
                continue
            candidates.append(
                OrphanedRun(
                    run_id=run_id_raw,
                    tenant=tenant_for_run(run_id_raw),
                    status=str(item.get("status", "unknown")),
                    started_at=started_at_raw,
                    unit_tenants=_recorded_unit_tenants(store, run_id_raw),
                )
            )
        if cursor is None:
            break

    candidates.sort(key=lambda run: run.started_at)
    return candidates


def _recorded_unit_tenants(store: Store, run_id: str) -> tuple[str, ...]:
    """Return the ``unit_tenants`` on ``run_id``'s ``eval_runs`` row, validated; ``()`` if absent.

    Raises:
        ValueError: The attribute is not a list of strings, or names another run's tenant.
    """
    row = store.get_item(_EVAL_RUNS_TABLE, {"run_id": run_id})
    raw = row.get("unit_tenants") if row is not None else None
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(entry, str) for entry in raw):
        raise ValueError(f"eval_runs row {run_id!r} carries a malformed unit_tenants: {raw!r}")
    return tuple(_validated_unit_tenants(run_id, raw))


def _parse_iso8601(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning ``None`` rather than raising on a malformed value."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def reap_tenant(store: Store, *, run: OrphanedRun) -> dict[str, int]:
    """Tear down one orphaned run's unit tenants and tenant, then mark its row terminal.

    Every tenant is attempted. The ``eval_runs`` row is recorded ``torn_down`` (preserving
    ``started_at`` and ``unit_tenants``) only when every deletion succeeded.

    Returns:
        Per-table deletion counts summed over the run's tenants.

    Raises:
        TenantTeardownError: A deletion raised; the row stays ``running``. The first such failure
            is raised.
        CanaryTenantNotEmptyError: Every deletion succeeded but the ``canary`` bystander holds a
            row. The row is made terminal before this propagates.
    """
    deleted, failures = _teardown_each(store, (*run.unit_tenants, run.tenant))
    teardown_failures = [exc for exc in failures if not isinstance(exc, CanaryTenantNotEmptyError)]
    if teardown_failures:
        raise teardown_failures[0]
    record_run_torn_down(
        store,
        run_id=run.run_id,
        tenant=run.tenant,
        started_at=_parse_iso8601(run.started_at),
        unit_tenants=run.unit_tenants,
    )
    if failures:
        raise failures[0]
    return deleted


__all__ = [
    "CANARY_TENANT",
    "PROTECTED_TENANTS",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_TORN_DOWN",
    "CanaryRecord",
    "CanaryTenantNotEmptyError",
    "DemoSeeder",
    "FixtureSeeder",
    "OrphanedRun",
    "TenantTeardownError",
    "allocate_run_id",
    "assert_canary_empty",
    "find_canary_records",
    "find_orphaned_runs",
    "reap_tenant",
    "record_run_started",
    "record_run_torn_down",
    "run_id_for_tenant",
    "seed_demo_tenant",
    "seed_tenant",
    "seed_unit_tenants",
    "teardown_tenant",
    "teardown_unit_tenants",
    "tenant_for_run",
    "unit_tenant",
    "unit_tenants_for_run",
]
