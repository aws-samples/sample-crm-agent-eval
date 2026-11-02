# SPDX-License-Identifier: MIT
"""Unit tests for `scripts/smoke/demo_tenant_guard.py`.

An unread baseline, an unreadable re-read, a run that wrote nothing, and a row carrying the wrong
tenant must each fail.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from scripts.smoke.demo_tenant_guard import (
    DEMO_TENANT,
    WATCHED_TABLES,
    TenantSnapshot,
    demo_tenant_untouched_check,
    read_tenant_snapshot,
    store_row_reader,
    writes_only_under_smoke_tenant_check,
)

_RESOURCE = "integration_probe"
_SMOKE_TENANT = "eval-smoke-c0ffee"


class _Reader:
    """A tenant-row reader over an in-memory `{tenant: {table: rows}}` corpus."""

    def __init__(
        self, corpus: Mapping[str, Mapping[str, int]], *, raise_after: int | None = None
    ) -> None:
        self._corpus = corpus
        self._raise_after = raise_after
        self.calls = 0

    def __call__(self, table: str, tenant: str) -> Sequence[Mapping[str, Any]]:
        self.calls += 1
        if self._raise_after is not None and self.calls > self._raise_after:
            raise RuntimeError("dynamodb unavailable")
        count = self._corpus.get(tenant, {}).get(table, 0)
        return [{"tenant": tenant, "n": index} for index in range(count)]


def _demo(counts: Mapping[str, int]) -> dict[str, Mapping[str, int]]:
    return {DEMO_TENANT: counts, _SMOKE_TENANT: {"accounts": 2}}


def test_every_tenant_scoped_table_is_watched() -> None:
    """The watch list is the set `Store.delete_tenant` walks, so no writable table is unwatched."""
    from crm.store.schema import TENANT_SCOPED_TABLES

    assert set(WATCHED_TABLES) == set(TENANT_SCOPED_TABLES)
    assert tuple(sorted(WATCHED_TABLES)) == WATCHED_TABLES, "order must be deterministic"


def test_an_unchanged_demo_tenant_passes_as_a_delta() -> None:
    reader = _Reader(_demo({"accounts": 5, "contacts": 9}))
    before = read_tenant_snapshot(reader, DEMO_TENANT)
    assert before.observed

    result = demo_tenant_untouched_check(resource=_RESOURCE, before=before, reader=reader)

    assert result.status == "pass"
    assert "unchanged" in result.detail


def test_a_changed_demo_row_count_fails() -> None:
    """The assertion is falsifiable: a `demo` write during the run is a failure."""
    corpus: dict[str, dict[str, int]] = {DEMO_TENANT: {"accounts": 5}, _SMOKE_TENANT: {}}
    reader = _Reader(corpus)
    before = read_tenant_snapshot(reader, DEMO_TENANT)
    corpus[DEMO_TENANT]["accounts"] = 6

    result = demo_tenant_untouched_check(resource=_RESOURCE, before=before, reader=reader)

    assert result.status == "fail"
    assert "changed during this run" in result.detail


def test_a_baseline_that_was_never_taken_fails_rather_than_passing() -> None:
    """A check with no before-snapshot has no delta and does not report a pass."""
    reader = _Reader(_demo({"accounts": 5}))

    result = demo_tenant_untouched_check(
        resource=_RESOURCE, before=TenantSnapshot(counts=None, error="boom"), reader=reader
    )

    assert result.status == "fail"
    assert "never read" in result.detail


def test_a_baseline_read_that_raises_is_recorded_not_swallowed() -> None:
    reader = _Reader(_demo({"accounts": 5}), raise_after=0)

    snapshot = read_tenant_snapshot(reader, DEMO_TENANT)

    assert not snapshot.observed
    assert "RuntimeError" in snapshot.error


def test_a_re_read_that_raises_fails_rather_than_passing() -> None:
    """An unreadable re-read of the boundary fails the check."""
    reader = _Reader(_demo({"accounts": 5}), raise_after=len(WATCHED_TABLES))
    before = read_tenant_snapshot(reader, DEMO_TENANT)
    assert before.observed

    result = demo_tenant_untouched_check(resource=_RESOURCE, before=before, reader=reader)

    assert result.status == "fail"
    assert "could not re-read" in result.detail


def test_rows_under_the_smoke_tenant_pass_and_are_counted() -> None:
    reader = _Reader({_SMOKE_TENANT: {"accounts": 4, "contacts": 3}})

    result = writes_only_under_smoke_tenant_check(
        resource=_RESOURCE, tenant=_SMOKE_TENANT, reader=reader
    )

    assert result.status == "pass"
    assert "7 row(s)" in result.detail


def test_a_run_that_wrote_nothing_fails() -> None:
    """A run that wrote zero rows under its own tenant fails the check."""
    reader = _Reader({_SMOKE_TENANT: {}})

    result = writes_only_under_smoke_tenant_check(
        resource=_RESOURCE, tenant=_SMOKE_TENANT, reader=reader
    )

    assert result.status == "fail"
    assert "never observed" in result.detail


def test_the_demo_tenant_as_the_acting_tenant_fails_outright() -> None:
    reader = _Reader({DEMO_TENANT: {"accounts": 4}})

    result = writes_only_under_smoke_tenant_check(
        resource=_RESOURCE, tenant=DEMO_TENANT, reader=reader
    )

    assert result.status == "fail"
    assert "forbids writing the demo tenant" in result.detail


def test_an_absent_tenant_fails_rather_than_passing() -> None:
    reader = _Reader({_SMOKE_TENANT: {"accounts": 4}})

    result = writes_only_under_smoke_tenant_check(resource=_RESOURCE, tenant="", reader=reader)

    assert result.status == "fail"


def test_a_row_whose_own_tenant_attribute_disagrees_fails() -> None:
    """The rows are re-checked rather than trusted to the index query that returned them."""

    def _mislabelled(_table: str, _tenant: str) -> Sequence[Mapping[str, Any]]:
        return [{"tenant": DEMO_TENANT}]

    result = writes_only_under_smoke_tenant_check(
        resource=_RESOURCE, tenant=_SMOKE_TENANT, reader=_mislabelled
    )

    assert result.status == "fail"
    assert "carries tenant" in result.detail


def test_a_write_read_that_raises_fails_rather_than_passing() -> None:
    reader = _Reader({_SMOKE_TENANT: {"accounts": 4}}, raise_after=0)

    result = writes_only_under_smoke_tenant_check(
        resource=_RESOURCE, tenant=_SMOKE_TENANT, reader=reader
    )

    assert result.status == "fail"
    assert "could not read back" in result.detail


def test_the_store_reader_paginates_every_page() -> None:
    """Every page of a large tenant is read."""

    class _PagingStore:
        def __init__(self) -> None:
            self.pages = 0

        def query_tenant_scan(
            self, _table: str, *, tenant: str, exclusive_start_key: Any = None
        ) -> tuple[list[dict[str, object]], dict[str, object] | None]:
            self.pages += 1
            if exclusive_start_key is None:
                return [{"tenant": tenant}], {"cursor": 1}
            return [{"tenant": tenant}], None

    store = _PagingStore()
    rows = store_row_reader(store)("accounts", _SMOKE_TENANT)

    assert len(rows) == 2
    assert store.pages == 2
