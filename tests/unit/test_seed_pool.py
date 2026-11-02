# SPDX-License-Identifier: MIT
"""Unit tests for ``fixtures.generate.seed_tenant``'s bounded write pool.

A thread-safe recording store stands in for :class:`crm.store.store.Store`, so each test reads
which chunks were written, by how many concurrent calls, and in what order.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import cast

import fixtures.generate as generate
import pytest
from fixtures.generate import SEED_CHUNK_SIZE, SEED_MAX_WORKERS, generate_dataset, seed_tenant

from crm.store.schema import table_schema
from crm.store.store import Store

_TENANT = "eval-seedpool-0001"
_BARRIER_TIMEOUT_SECONDS = 10.0
#: How long the first wave stays in flight after it has fully started, so an extra worker shows.
_HOLD_SECONDS = 0.5


class _ChunkFailedError(Exception):
    """Raised by the recording store for a chunk selected to fail."""


class _RecordingStore:
    """Records every ``batch_put_items`` call and the peak number of calls in flight at once."""

    def __init__(
        self,
        *,
        fail_calls: frozenset[int] = frozenset(),
        on_call: Callable[[int], None] | None = None,
    ) -> None:
        self.calls: list[tuple[str, list[dict[str, object]]]] = []
        self.peak_in_flight = 0
        self._in_flight = 0
        self._started = 0
        self._lock = threading.Lock()
        self._fail_calls = fail_calls
        self._on_call = on_call

    def batch_put_items(self, table: str, items: Sequence[Mapping[str, object]]) -> int:
        with self._lock:
            index = self._started
            self._started += 1
            self._in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
        try:
            if self._on_call is not None:
                self._on_call(index)
            if index in self._fail_calls:
                raise _ChunkFailedError(f"chunk {index}")
            with self._lock:
                self.calls.append((table, [dict(item) for item in items]))
            return len(items)
        finally:
            with self._lock:
                self._in_flight -= 1


def _key(table: str, item: Mapping[str, object]) -> tuple[str, ...]:
    schema = table_schema(table)
    attrs = [schema.partition_key] + ([schema.sort_key] if schema.sort_key else [])
    return (table, *(str(item[attr]) for attr in attrs))


def _expected_keys() -> Counter[tuple[str, ...]]:
    return Counter(
        _key(table, item)
        for table, items in generate_dataset(_TENANT).as_mapping().items()
        for item in items
    )


def _written_keys(store: _RecordingStore) -> Counter[tuple[str, ...]]:
    return Counter(_key(table, item) for table, chunk in store.calls for item in chunk)


def _sequential_order() -> list[tuple[str, list[tuple[str, ...]]]]:
    """Each chunk's table and item keys, in the order a sequential seed writes them."""
    order: list[tuple[str, list[tuple[str, ...]]]] = []
    for table, items in generate_dataset(_TENANT).as_mapping().items():
        keys = [_key(table, item) for item in items]
        order.extend(
            (table, keys[start : start + SEED_CHUNK_SIZE])
            for start in range(0, len(keys), SEED_CHUNK_SIZE)
        )
    return order


def _seed(store: _RecordingStore) -> None:
    seed_tenant(cast(Store, store), tenant=_TENANT)


def test_every_item_is_written_exactly_once_in_chunks_no_larger_than_the_chunk_size() -> None:
    store = _RecordingStore()
    _seed(store)

    assert _written_keys(store) == _expected_keys()
    assert all(1 <= len(chunk) <= SEED_CHUNK_SIZE for _table, chunk in store.calls)
    assert len(store.calls) == len(_sequential_order())


def test_the_pool_runs_the_bound_concurrently_and_never_more() -> None:
    barrier = threading.Barrier(SEED_MAX_WORKERS, timeout=_BARRIER_TIMEOUT_SECONDS)

    def hold_first_wave(index: int) -> None:
        if index < SEED_MAX_WORKERS:
            barrier.wait()
            time.sleep(_HOLD_SECONDS)

    store = _RecordingStore(on_call=hold_first_wave)
    _seed(store)

    assert store.peak_in_flight == SEED_MAX_WORKERS
    assert _written_keys(store) == _expected_keys()


def test_a_failing_chunk_propagates_after_every_other_chunk_is_written() -> None:
    failing = 1
    store = _RecordingStore(fail_calls=frozenset({failing}))

    with pytest.raises(_ChunkFailedError, match=f"chunk {failing}"):
        _seed(store)

    assert len(store.calls) == len(_sequential_order()) - 1
    assert sum(_written_keys(store).values()) < sum(_expected_keys().values())


def test_the_first_failure_in_submission_order_is_the_one_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generate, "SEED_MAX_WORKERS", 1)
    store = _RecordingStore(fail_calls=frozenset({2, 5}))

    with pytest.raises(_ChunkFailedError, match="chunk 2"):
        _seed(store)

    assert len(store.calls) == len(_sequential_order()) - 2


def test_one_worker_reproduces_the_sequential_call_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(generate, "SEED_MAX_WORKERS", 1)
    store = _RecordingStore()
    _seed(store)

    assert store.peak_in_flight == 1
    assert [
        (table, [_key(table, item) for item in chunk]) for table, chunk in store.calls
    ] == _sequential_order()
