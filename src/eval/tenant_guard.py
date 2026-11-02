# SPDX-License-Identifier: MIT
"""Demo-tenant delta assertions published by every mutating smoke script and every eval sweep.

`eval.harness:run_sweep` and the sweep state machine's teardown step call these, and
`scripts/smoke/demo_tenant_guard.py` re-exports them. No AWS client, no CLI.

* A re-read that raises is reported as `fail`, naming the exception.
* Neither check passes vacuously: `demo_tenant_untouched_check` fails when the *before* snapshot
  was never taken, and `writes_only_under_smoke_tenant_check` fails when the run left no row
  under its own tenant.

Both checks are deltas over the tenant-wide index (`gsi_tenant`). The demo snapshot can be read
with a `TenantCountReader`, which issues count-only queries and returns no row attributes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from crm.authz import DEMO_TENANT
from crm.store.schema import TENANT_SCOPED_TABLES
from eval.verification import CheckResult

__all__ = [
    "DEMO_TENANT",
    "WATCHED_TABLES",
    "TenantCountReader",
    "TenantRowReader",
    "TenantSnapshot",
    "demo_tenant_untouched_check",
    "read_tenant_counts",
    "read_tenant_snapshot",
    "store_count_reader",
    "store_row_reader",
    "writes_only_under_smoke_tenant_check",
]

#: Every table a smoke script could write, in a deterministic order: the set
#: `Store.delete_tenant` walks.
WATCHED_TABLES: Final[tuple[str, ...]] = tuple(sorted(TENANT_SCOPED_TABLES))

#: `(table, tenant) -> the rows under that tenant in that table`. The seam these checks read
#: through.
TenantRowReader = Callable[[str, str], Sequence[Mapping[str, Any]]]

#: `(table, tenant) -> how many rows are under that tenant in that table`.
TenantCountReader = Callable[[str, str], int]


@dataclass(frozen=True, slots=True)
class TenantSnapshot:
    """Per-table row counts for one tenant, or the error that prevented reading them.

    Attributes:
        counts: Rows per table, or `None` when the snapshot could not be taken at all.
        error: The failure that prevented the read. Empty when `counts` is populated.
    """

    counts: Mapping[str, int] | None
    error: str = ""

    @property
    def observed(self) -> bool:
        """Whether the snapshot was genuinely taken."""
        return self.counts is not None


def store_row_reader(store: Any) -> TenantRowReader:
    """Return a `TenantRowReader` over a `crm.store.store.Store`, paginating the tenant index."""

    def _read(table: str, tenant: str) -> Sequence[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        cursor: dict[str, object] | None = None
        while True:
            page, cursor = store.query_tenant_scan(table, tenant=tenant, exclusive_start_key=cursor)
            rows.extend(page)
            if cursor is None:
                return rows

    return _read


def store_count_reader(store: Any) -> TenantCountReader:
    """Return a `TenantCountReader` over a `crm.store.store.Store`'s count-only tenant query."""

    def _count(table: str, tenant: str) -> int:
        return int(store.count_tenant(table, tenant=tenant))

    return _count


def read_tenant_counts(
    counter: TenantCountReader, tenant: str, tables: Sequence[str] = WATCHED_TABLES
) -> TenantSnapshot:
    """Count `tenant`'s rows in every watched table, recording rather than raising on failure."""
    counts: dict[str, int] = {}
    try:
        for table in tables:
            counts[table] = counter(table, tenant)
    except Exception as exc:  # noqa: BLE001 - an unobservable boundary is not a passing one
        return TenantSnapshot(counts=None, error=f"{type(exc).__name__}: {exc}")
    return TenantSnapshot(counts=counts)


def read_tenant_snapshot(
    reader: TenantRowReader, tenant: str, tables: Sequence[str] = WATCHED_TABLES
) -> TenantSnapshot:
    """Count `tenant`'s rows in every watched table by reading them, as `read_tenant_counts`."""
    return read_tenant_counts(_row_counter(reader), tenant, tables)


def _row_counter(reader: TenantRowReader) -> TenantCountReader:
    """A `TenantCountReader` that counts the rows `reader` returns."""
    return lambda table, tenant: len(reader(table, tenant))


def demo_tenant_untouched_check(
    *,
    resource: str,
    before: TenantSnapshot,
    reader: TenantRowReader | None = None,
    counter: TenantCountReader | None = None,
    tables: Sequence[str] = WATCHED_TABLES,
) -> CheckResult:
    """Assert this run changed no `demo`-tenant row count in any table it could write.

    A before/after delta, re-read through exactly one of `reader` (rows) or `counter` (counts).
    Fails when the baseline was never taken and when the re-read raises.

    Raises:
        ValueError: Neither or both of `reader` and `counter` are given.
    """
    if counter is None:
        if reader is None:
            raise ValueError("demo_tenant_untouched_check needs one of reader or counter")
        counter = _row_counter(reader)
    elif reader is not None:
        raise ValueError("demo_tenant_untouched_check takes reader or counter, not both")
    if not before.observed:
        return CheckResult(
            resource=resource,
            check="demo_tenant_untouched",
            status="fail",
            detail=(
                "the demo tenant's baseline row counts were never read, so no delta could be "
                f"computed for this run: {before.error or 'no baseline was taken'}"
            ),
        )
    after = read_tenant_counts(counter, DEMO_TENANT, tables)
    if after.counts is None:
        return CheckResult(
            resource=resource,
            check="demo_tenant_untouched",
            status="fail",
            detail=f"could not re-read the demo tenant's row counts: {after.error}",
        )
    baseline = before.counts or {}
    drift = {
        table: (baseline.get(table), count)
        for table, count in after.counts.items()
        if baseline.get(table) != count
    }
    if drift:
        return CheckResult(
            resource=resource,
            check="demo_tenant_untouched",
            status="fail",
            detail=f"demo-tenant row counts changed during this run: {drift!r}",
        )
    return CheckResult(
        resource=resource,
        check="demo_tenant_untouched",
        status="pass",
        detail=(
            f"demo-tenant row counts unchanged across {len(after.counts)} table(s): "
            f"{dict(after.counts)!r}"
        ),
    )


def writes_only_under_smoke_tenant_check(
    *,
    resource: str,
    tenant: str,
    reader: TenantRowReader,
    tables: Sequence[str] = WATCHED_TABLES,
) -> CheckResult:
    """Assert this run's rows exist under its own disposable tenant and carry that tenant.

    Rows are read back through `gsi_tenant` and each row's `tenant` attribute is re-checked. Fails
    when the tenant is `demo` or empty, when the read raises, and when the run left no row at all.
    """
    if not tenant or tenant == DEMO_TENANT:
        return CheckResult(
            resource=resource,
            check="writes_only_under_smoke_tenant",
            status="fail",
            detail=(
                f"the acting tenant is {tenant!r}; the demo-untouched invariant forbids "
                "writing the demo tenant, and a run with no disposable tenant of its own cannot "
                "prove where its writes landed"
            ),
        )
    observed: dict[str, int] = {}
    violations: list[str] = []
    try:
        for table in tables:
            rows = reader(table, tenant)
            observed[table] = len(rows)
            for row in rows:
                row_tenant = str(row.get("tenant") or "")
                if row_tenant != tenant:
                    violations.append(f"{table}: row carries tenant {row_tenant!r}")
    except Exception as exc:  # noqa: BLE001 - an unobservable boundary is not a passing one
        return CheckResult(
            resource=resource,
            check="writes_only_under_smoke_tenant",
            status="fail",
            detail=(
                f"could not read back this run's own rows under tenant {tenant!r}: "
                f"{type(exc).__name__}: {exc}"
            ),
        )
    if violations:
        return CheckResult(
            resource=resource,
            check="writes_only_under_smoke_tenant",
            status="fail",
            detail="; ".join(sorted(set(violations))[:10]),
        )
    total = sum(observed.values())
    if total == 0:
        return CheckResult(
            resource=resource,
            check="writes_only_under_smoke_tenant",
            status="fail",
            detail=(
                f"this run committed no row under its own tenant {tenant!r}, so the tenant its "
                "writes land in was never observed; an unobserved boundary is not a passing one"
            ),
        )
    written = {table: count for table, count in observed.items() if count}
    return CheckResult(
        resource=resource,
        check="writes_only_under_smoke_tenant",
        status="pass",
        detail=(
            f"{total} row(s) read back under disposable tenant {tenant!r}, every one carrying that "
            f"tenant and none carrying {DEMO_TENANT!r}: {written!r}"
        ),
    )
