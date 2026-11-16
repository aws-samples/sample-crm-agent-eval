# SPDX-License-Identifier: MIT
"""The SFN aggregate step hands `assemble_arm` each unit under the repeat its item ran in.

`eval.sfn_handlers._aggregate` rebuilds every `done` item's unit from its stored row and must carry
the item's `TaskRef.repeat_index` onto it. A spy over `assemble_arm` records the
`(arm_index, repeat_index, task_id)` of every unit it receives over a full two-repeat stub sweep,
and that set must equal the sweep's own refs. Harness: `test_sfn_handlers`.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from eval import sfn_handlers
from eval.harness import DEFAULT_ARM_SPECS
from eval.runs import require_run
from eval.sweep_units import ArmRunResult, TaskUnitResult, assemble_arm
from tests.unit.test_sfn_handlers import Sweep, make_sweep, mocked_aws

TASKS = 2
REPEATS = 2


@pytest.fixture
def sweep(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sweep]:
    with mocked_aws(monkeypatch):
        yield make_sweep(monkeypatch, tasks=TASKS, repeats=REPEATS)


def _spy_on_assemble_arm(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, str]]:
    """Wrap `assemble_arm` as `_aggregate` calls it; return the list each unit is recorded into."""
    observed: list[tuple[int, int, str]] = []

    def spy(units: Sequence[TaskUnitResult], **kwargs: Any) -> ArmRunResult:
        observed.extend((kwargs["arm_index"], unit.repeat_index, unit.task_id) for unit in units)
        return assemble_arm(units, **kwargs)

    monkeypatch.setattr(sfn_handlers, "assemble_arm", spy)
    return observed


def test_aggregate_passes_each_units_own_repeat_index_to_assemble_arm(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = _spy_on_assemble_arm(monkeypatch)
    final = sweep.drive()
    assert final["status"] == "completed"
    refs = sfn_handlers._sweep_refs(require_run(sweep.store, sweep.run_id))
    expected = sorted((ref.arm_index, ref.repeat_index, ref.task_id) for ref in refs)
    assert sorted(observed) == expected
    assert {repeat for _arm, repeat, _task in observed} == set(range(REPEATS))
    assert len(observed) == len(DEFAULT_ARM_SPECS) * REPEATS * TASKS
