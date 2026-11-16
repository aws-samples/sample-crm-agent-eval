# SPDX-License-Identifier: MIT
"""SFN contract: units through `_unit_fields` -> `unit_from_row` assemble the in-process tier_task_metrics."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import Any

import pytest

from crm.errors import ValidationFailed
from eval.sfn_handlers import _unit_fields, unit_from_row
from eval.sweep_units import ArmSpec, TaskUnitResult
from providers.capabilities import Family
from tests.unit.test_sweep_units_tier_task import REP_ID, assemble, units_for

_TASK: dict[str, object] = {"actor": {"rep_id": REP_ID, "principal_type": "user"}}

Row = dict[str, Any]


def _stored_rows(units: list[TaskUnitResult]) -> list[Row]:
    return [json.loads(json.dumps(_unit_fields(unit))) for unit in units]


def _from_rows(rows: list[Row], family: Family) -> list[TaskUnitResult]:
    return [
        unit_from_row(row, task=_TASK, arm_spec=ArmSpec(family=family), repeat_index=0)
        for row in rows
    ]


def _tier_task_metrics(units: list[TaskUnitResult], family: Family, mode: str) -> object:
    return assemble(units, family, mode).arm_result.tier_task_metrics_fields


def _strip_latency(row: Row) -> Row:
    attributions = json.loads(row["attributions"])
    for attribution in attributions:
        attribution["latency_ms"] = None
    return {**row, "attributions": json.dumps(attributions, sort_keys=True)}


def _drop_outcome(row: Row) -> Row:
    outcome = json.loads(row["outcome"])
    outcome["denial_outcome"] = None
    return {**row, "outcome": json.dumps(outcome, sort_keys=True)}


def _drop_efforts(row: Row) -> Row:
    return {key: value for key, value in row.items() if key != "resolved_efforts"}


@pytest.mark.parametrize("family", ["anthropic", "openai"])
@pytest.mark.parametrize("mode", ["live", "stub"])
def test_stored_units_assemble_the_same_tier_task_metrics(family: Family, mode: str) -> None:
    units = units_for(family)
    in_process = _tier_task_metrics(units, family, mode)
    via_rows = _tier_task_metrics(_from_rows(_stored_rows(units), family), family, mode)
    assert via_rows == in_process


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_stored_units_assemble_the_same_arm_report(family: Family) -> None:
    units = units_for(family)
    in_process = assemble(units, family, "live").arm_result.to_report_fields()
    via_rows = assemble(_from_rows(_stored_rows(units), family), family, "live")
    assert via_rows.arm_result.to_report_fields() == in_process


@pytest.mark.parametrize(
    "mutate",
    [_strip_latency, _drop_outcome, _drop_efforts],
    ids=["latency_ms", "denial_outcome", "resolved_efforts"],
)
def test_a_field_lost_in_storage_breaks_the_equality(mutate: Callable[[Row], Row]) -> None:
    units = units_for("anthropic")
    in_process = _tier_task_metrics(units, "anthropic", "live")
    rows = [mutate(row) for row in _stored_rows(units)]
    assert _tier_task_metrics(_from_rows(rows, "anthropic"), "anthropic", "live") != in_process


def _exhausted_at_subagent(units: list[TaskUnitResult]) -> list[TaskUnitResult]:
    first, *rest = units
    outcome = dataclasses.replace(
        first.turn.outcome, budget_exhausted=True, budget_exhausted_tier="subagent"
    )
    return [
        dataclasses.replace(first, turn=dataclasses.replace(first.turn, outcome=outcome)),
        *rest,
    ]


def test_the_exhaustion_tier_survives_storage_into_the_tier_task_metrics() -> None:
    units = _exhausted_at_subagent(units_for("anthropic"))
    via_rows = _tier_task_metrics(_from_rows(_stored_rows(units), "anthropic"), "anthropic", "live")
    assert via_rows == _tier_task_metrics(units, "anthropic", "live")
    assert isinstance(via_rows, dict)
    assert via_rows["by_tier"]["subagent"]["budget_exhausted_task_count"] == 1


def test_a_lost_exhaustion_tier_breaks_the_stored_row() -> None:
    units = _exhausted_at_subagent(units_for("anthropic"))
    row = _stored_rows(units)[0]
    outcome = json.loads(row["outcome"])
    outcome["budget_exhausted_tier"] = None
    with pytest.raises(ValidationFailed, match="unreadable"):
        _from_rows([{**row, "outcome": json.dumps(outcome)}], "anthropic")


@pytest.mark.parametrize("repeat_index", [0, 3])
def test_a_stored_unit_takes_its_repeat_from_the_ref(repeat_index: int) -> None:
    (row, *_rest) = _stored_rows(units_for("anthropic"))
    unit = unit_from_row(
        row, task=_TASK, arm_spec=ArmSpec(family="anthropic"), repeat_index=repeat_index
    )
    assert unit.repeat_index == repeat_index
