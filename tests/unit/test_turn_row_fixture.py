# SPDX-License-Identifier: MIT
"""`eval.sfn_handlers.turn_from_row` on a frozen schema-1 `eval_results` row.

`tests/fixtures/eval/turn_row_schema1.json` is a row stored while `SERIALISED_TURN_SCHEMA` was 1.
It must stay readable, and losslessly so, under every later schema.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from eval.sfn_handlers import turn_from_row
from eval.turn_observation import SERIALISED_TURN_SCHEMA, serialise_turn

SCHEMA_1_ROW: Final[Path] = (
    Path(__file__).resolve().parents[1] / "fixtures" / "eval" / "turn_row_schema1.json"
)


def _row() -> dict[str, Any]:
    row = json.loads(SCHEMA_1_ROW.read_text(encoding="utf-8"))
    assert isinstance(row, dict)
    return row


def test_the_frozen_row_stores_no_schema_attribute() -> None:
    assert "schema" not in _row()


def test_the_schema_1_row_rebuilds_under_the_current_schema() -> None:
    turn = serialise_turn(turn_from_row(_row()))
    assert turn["schema"] == SERIALISED_TURN_SCHEMA


def test_the_schema_1_row_rebuilds_losslessly() -> None:
    row = _row()
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
