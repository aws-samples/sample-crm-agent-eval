# SPDX-License-Identifier: MIT
"""`eval.sfn_handlers`: a sweep in which every item fails publishes no matrix and no artifact.

Drives the in-process sweep from `tests.unit.test_sfn_handlers` with every Runtime answer replaced
by a failure body, so every item is routed through the state machine's `item_failed` Catch.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from eval.harness import DEFAULT_ARM_SPECS
from eval.runs import read_lock, require_run, terminal_status
from tests.unit.test_sfn_handlers import Sweep, _body_failure, make_sweep, mocked_aws

TASKS_PER_ARM = 2
ITEMS = len(DEFAULT_ARM_SPECS) * TASKS_PER_ARM


@dataclass
class RecordingSweep(Sweep):
    """A `Sweep` that keeps every control step's result, and the object keys each step wrote."""

    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    written: dict[str, set[str]] = field(default_factory=dict)

    def control(self, step: str, **fields: Any) -> dict[str, Any]:
        before = set(self.objects())
        result = super().control(step, **fields)
        self.results[step] = result
        self.written[step] = set(self.objects()) - before
        return result


@pytest.fixture
def all_failed(monkeypatch: pytest.MonkeyPatch) -> Iterator[RecordingSweep]:
    with mocked_aws(monkeypatch):
        base = make_sweep(monkeypatch, tasks=TASKS_PER_ARM)
        sweep = RecordingSweep(
            store=base.store, s3=base.s3, runtime=base.runtime, run_id=base.run_id
        )
        sweep.runtime.fail_with = _body_failure(504, "turn_timeout")
        sweep.drive()
        yield sweep


def test_every_item_failing_aggregates_no_arm(all_failed: RecordingSweep) -> None:
    """The aggregate step reports zero arms and no unrecorded item: every item has a failed row."""
    assert all_failed.results["aggregate"] == {
        "run_id": all_failed.run_id,
        "arms": 0,
        "items_unrecorded": 0,
    }


def test_every_item_failing_writes_no_matrix_and_no_artifact_links(
    all_failed: RecordingSweep,
) -> None:
    record = require_run(all_failed.store, all_failed.run_id)
    assert record.matrix_report is None
    assert record.artifact_links is None
    payload = record.to_payload()
    assert "matrix" not in payload
    assert "artifacts" not in payload


def test_every_item_failing_uploads_no_matrix_or_raw_artifact(all_failed: RecordingSweep) -> None:
    """The aggregate step writes no object; what the bucket holds is the start step's baseline and
    the teardown step's verification inventory only."""
    assert all_failed.written["aggregate"] == set()
    keys = set(all_failed.objects())
    assert keys == all_failed.written["start"] | all_failed.written["teardown"]
    assert not any(key.startswith(("matrix.", "raw/")) for key in keys)


def test_every_item_failing_counts_each_failure_and_ends_terminal(
    all_failed: RecordingSweep,
) -> None:
    record = require_run(all_failed.store, all_failed.run_id)
    assert (record.tasks_done, record.tasks_failed, record.tasks_skipped) == (0, ITEMS, 0)
    expected = terminal_status(tasks_failed=ITEMS, tasks_skipped=0, teardown_ok=True)
    assert expected == "failed"
    assert record.status == expected
    assert all_failed.results["finalize"] == {
        "run_id": all_failed.run_id,
        "status": expected,
        "refused": False,
    }
    assert record.teardown_status == "ok"
    assert read_lock(all_failed.store) is None
    assert len(all_failed.runtime.requests) == ITEMS
