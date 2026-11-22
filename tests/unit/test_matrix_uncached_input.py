# SPDX-License-Identifier: MIT
"""Tests for the tier tables' input token rows in :mod:`eval.matrix`.

Total input tokens is the per-call prompt size, Uncached input tokens is derived from it and the
two cache counts, and the identity Total = cache read + cache write + uncached holds exactly.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, cast

import pytest

from agents.tier_attribution import CacheCounts, InvocationAttribution, TokenCounts
from eval import matrix
from eval.matrix import render_csv, render_json, render_markdown, tier_comparison
from eval.metrics import (
    aggregate_tier_role_model,
    aggregate_tier_summary,
    not_measured_latency_metrics,
)
from tests.unit import test_matrix_tier_comparison as fixtures

_ANTHROPIC = "anthropic.claude-sonnet-5-5"
_OPENAI = "openai.gpt-6.2-sol"
_OLD_LABELS = ("Reported input tokens", "Total prompt tokens", "Output tokens")
_TOKEN_LABELS = [
    "Total input tokens",
    "Cache read tokens",
    "Cache write tokens",
    "Uncached input tokens",
    "Total output tokens",
]


def _sections(summary: dict[str, object]) -> list[dict[str, list[str]]]:
    table = matrix.tier_comparison_table(summary)
    return [
        {row[0]: row[1:] for row in cast(list[list[str]], section["rows"])}
        for section in cast(list[dict[str, object]], table["sections"])
    ]


def _supervisor_entry(call: InvocationAttribution) -> dict[str, object]:
    rows = aggregate_tier_role_model((call,), withhold_cost=True)
    summary = aggregate_tier_summary(rows, not_measured_latency_metrics(), (), withhold_cost=True)
    return cast(dict[str, object], summary.to_report_fields()["supervisor"])


def _call(model_id: str, *, input_tokens: int, read: int, write: int) -> InvocationAttribution:
    return InvocationAttribution(
        tier="supervisor",
        role="supervisor",
        model_id=model_id,
        tokens=TokenCounts(input_tokens=input_tokens, output_tokens=7),
        cache=CacheCounts(read_tokens=read, write_tokens=write),
    )


def test_the_token_rows_are_the_five_input_and_output_rows_in_order() -> None:
    for rows in _sections(tier_comparison(fixtures._three_arm_matrix())):
        labels = list(rows)
        start = labels.index("Total input tokens")
        assert labels[start : start + len(_TOKEN_LABELS)] == _TOKEN_LABELS


def test_total_input_is_cache_read_plus_cache_write_plus_uncached_in_every_column() -> None:
    matrix_result = fixtures._three_arm_matrix()
    columns = cast(list[dict[str, object]], tier_comparison(matrix_result)["columns"])
    families = {column["family"] for column in columns}
    assert families == {"anthropic", "openai"}
    checked = 0
    for rows in _sections(tier_comparison(matrix_result)):
        total, read, write, uncached = (
            [int(cell) for cell in rows[label]] for label in _TOKEN_LABELS[:4]
        )
        for column in range(len(total)):
            assert total[column] == read[column] + write[column] + uncached[column]
            checked += 1
    assert checked == 9


def test_the_identity_holds_for_live_shaped_calls_of_both_families() -> None:
    for call in (
        _call(_ANTHROPIC, input_tokens=40, read=50, write=10),
        _call(_OPENAI, input_tokens=100, read=50, write=10),
        _call(_OPENAI, input_tokens=20, read=50, write=10),
    ):
        entry = _supervisor_entry(call)
        uncached = matrix.uncached_input_tokens(entry)
        assert uncached is not None and uncached >= 0
        assert entry["total_prompt_tokens"] == (
            cast(int, entry["cache_read_tokens"])
            + cast(int, entry["cache_write_tokens"])
            + uncached
        )


def test_the_same_prompt_gives_the_same_uncached_input_for_both_families() -> None:
    anthropic = _supervisor_entry(_call(_ANTHROPIC, input_tokens=40, read=50, write=10))
    openai = _supervisor_entry(_call(_OPENAI, input_tokens=100, read=50, write=10))
    assert anthropic["input_tokens"] != openai["input_tokens"]
    assert anthropic["total_prompt_tokens"] == openai["total_prompt_tokens"] == 100
    assert matrix.uncached_input_tokens(anthropic) == matrix.uncached_input_tokens(openai) == 40


@pytest.mark.parametrize("label", _OLD_LABELS)
def test_no_surface_carries_an_old_token_row_label(label: str) -> None:
    matrix_result = fixtures._three_arm_matrix()
    summary = tier_comparison(matrix_result)
    for rows in _sections(summary):
        assert label not in rows
    assert f"| {label} |" not in render_markdown(matrix_result)
    assert not any(label in note for note in cast(list[str], summary["notes"]))


def test_json_entries_carry_the_derived_uncached_figure_beside_total_prompt_tokens() -> None:
    payload = json.loads(render_json(fixtures._three_arm_matrix()))
    for entries in payload["tier_comparison"]["tiers"].values():
        for entry in entries:
            keys = list(entry)
            assert keys.index("uncached_input_tokens") == keys.index("cache_write_tokens") + 1
            assert entry["uncached_input_tokens"] == (
                entry["total_prompt_tokens"]
                - entry["cache_read_tokens"]
                - entry["cache_write_tokens"]
            )
            assert "input_tokens" in entry


def test_csv_carries_the_uncached_column_for_every_tier() -> None:
    reader = csv.DictReader(io.StringIO(render_csv(fixtures._three_arm_matrix())))
    first = next(reader)
    for tier in ("supervisor", "subagent", "tool"):
        assert int(first[f"summary_{tier}_uncached_input_tokens"]) == (
            int(first[f"summary_{tier}_total_prompt_tokens"])
            - int(first[f"summary_{tier}_cache_read_tokens"])
            - int(first[f"summary_{tier}_cache_write_tokens"])
        )
        assert f"summary_{tier}_input_tokens" in first


def test_a_stored_summary_without_the_uncached_key_still_renders_the_derived_row() -> None:
    matrix_result = fixtures._three_arm_matrix()
    stored: dict[str, Any] = json.loads(render_json(matrix_result))["tier_comparison"]
    for entries in stored["tiers"].values():
        for entry in entries:
            del entry["uncached_input_tokens"]
    assert stored[matrix.RECORD_SHAPE_VERSION_KEY] == 4
    assert matrix.tier_comparison_table(stored) == matrix.tier_comparison_table(
        tier_comparison(matrix_result)
    )


def test_an_unrecorded_token_figure_renders_a_dash_for_uncached_input() -> None:
    stored: dict[str, Any] = json.loads(json.dumps(tier_comparison(fixtures._three_arm_matrix())))
    stored["tiers"]["tool"][1]["cache_write_tokens"] = None
    tool = _sections(stored)[2]
    assert tool["Uncached input tokens"][1] == "—"


def test_cache_counts_exceeding_the_total_are_refused() -> None:
    with pytest.raises(ValueError, match="less than cache_read_tokens"):
        matrix.uncached_input_tokens(
            {"total_prompt_tokens": 10, "cache_read_tokens": 8, "cache_write_tokens": 3}
        )


def test_a_boolean_token_figure_is_not_read_as_an_integer() -> None:
    assert (
        matrix.uncached_input_tokens(
            {"total_prompt_tokens": 10, "cache_read_tokens": True, "cache_write_tokens": 0}
        )
        is None
    )
