# SPDX-License-Identifier: MIT
"""`eval.sfn_handlers.turn_from_row` on a frozen schema-1 `eval_results` row.

`tests/fixtures/eval/turn_row_schema1.json` is a row stored while `SERIALISED_TURN_SCHEMA` was 1.
Its outcome carries no `budget_exhausted_tier` and no `output_truncated`, and its attributions carry
no `output_truncated`; the current schema requires all three, so the row is refused loudly rather
than read with invented values. With those keys supplied it rebuilds losslessly, which pins that
nothing else about the stored shape has drifted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from crm.errors import ValidationFailed
from eval.sfn_handlers import turn_from_row
from eval.turn_observation import SERIALISED_TURN_SCHEMA, serialise_turn

SCHEMA_1_ROW: Final[Path] = (
    Path(__file__).resolve().parents[1] / "fixtures" / "eval" / "turn_row_schema1.json"
)


def _row() -> dict[str, Any]:
    row = json.loads(SCHEMA_1_ROW.read_text(encoding="utf-8"))
    assert isinstance(row, dict)
    return row


def _row_with_tier() -> dict[str, Any]:
    row = _row()
    outcome = json.loads(row["outcome"])
    outcome["budget_exhausted_tier"] = None
    row["outcome"] = json.dumps(outcome)
    return row


def _row_with_current_keys() -> dict[str, Any]:
    row = _row_with_tier()
    outcome = json.loads(row["outcome"])
    outcome["output_truncated"] = False
    row["outcome"] = json.dumps(outcome)
    attributions = json.loads(row["attributions"])
    for attribution in attributions:
        attribution["output_truncated"] = False
    row["attributions"] = json.dumps(attributions)
    return row


def test_the_frozen_row_stores_no_schema_attribute() -> None:
    assert "schema" not in _row()


def test_the_frozen_row_stores_no_exhaustion_tier() -> None:
    outcome = json.loads(_row()["outcome"])
    assert outcome["budget_exhausted"] is False
    assert "budget_exhausted_tier" not in outcome


def test_the_schema_1_row_is_refused_naming_the_missing_tier() -> None:
    with pytest.raises(ValidationFailed) as refused:
        turn_from_row(_row())
    assert "budget_exhausted_tier" in str(refused.value.detail)


def test_the_frozen_row_stores_no_output_truncation() -> None:
    row = _row()
    assert "output_truncated" not in json.loads(row["outcome"])
    assert all("output_truncated" not in a for a in json.loads(row["attributions"]))


def test_the_row_with_only_the_tier_supplied_is_refused_naming_output_truncated() -> None:
    with pytest.raises(ValidationFailed) as refused:
        turn_from_row(_row_with_tier())
    assert "output_truncated" in str(refused.value.detail)


def test_the_row_with_the_current_keys_supplied_rebuilds_under_the_current_schema() -> None:
    turn = serialise_turn(turn_from_row(_row_with_current_keys()))
    assert turn["schema"] == SERIALISED_TURN_SCHEMA


def test_the_row_with_the_current_keys_supplied_rebuilds_losslessly() -> None:
    row = _row_with_current_keys()
    turn = serialise_turn(turn_from_row(row))
    assert turn["outcome"] == json.loads(row["outcome"])
    assert turn["tool_arguments"] == json.loads(row["tool_arguments"])
    assert turn["records"]["invocations"] == json.loads(row["attributions"])
    assert turn["records"]["tool_calls"] == json.loads(row["tool_calls"])
    assert turn["exclusions"] == json.loads(row["exclusions"])
    assert turn["stream_calls"] == row["stream_calls"] == 1
    assert turn["turn_latency_ms"] == row["turn_latency_ms"]
    [invocation] = turn["records"]["invocations"]
    assert (invocation["tier"], invocation["tokens"]) == (
        "supervisor",
        {"input_tokens": 128, "output_tokens": 32},
    )
