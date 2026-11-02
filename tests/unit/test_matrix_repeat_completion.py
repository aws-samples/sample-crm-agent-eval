# SPDX-License-Identifier: MIT
"""Completion over repeats in matrix.json, matrix.md and matrix.csv: the
tier comparison carries it beside ``completion``, the Markdown renders it as one row, both summary
CSV column sets carry it, and archives without it still load and render ``n/a``.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import json
from pathlib import Path
from typing import cast

from eval import harness, matrix
from eval.matrix import (
    ArmResult,
    MatrixResult,
    arm_from_report_fields,
    build_matrix_result,
    render_csv,
    render_json,
    render_markdown,
    tier_comparison,
)
from eval.metrics import aggregate_tier_summary, not_measured_latency_metrics
from eval.scoring import SCORED_TIERS, TierScore, TierScores
from tests.unit.test_matrix import _sample_arm

_LEGACY_ARTIFACT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "eval" / "legacy_matrix_artifact.json"
)

_DEFINITION = (
    "a task counts when its tier expectation was met in a strict majority of its applicable repeats"
)


def _scores(task_id: str, **passed: bool | None) -> TierScores:
    by_tier = {
        tier: TierScore(tier=tier, applicable=passed.get(tier) is not None, passed=passed.get(tier))
        for tier in SCORED_TIERS
    }
    return TierScores(task_id=task_id, **by_tier)


def _three_repeat_turns() -> list[TierScores]:
    """Supervisor: t1 2/3 (completes), t2 1/3 (does not). Subagent: t1 3/3. Tool: never."""
    return [
        _scores("t1", supervisor=True, subagent=True),
        _scores("t2", supervisor=False),
        _scores("t1", supervisor=True, subagent=True),
        _scores("t2", supervisor=True),
        _scores("t1", supervisor=False, subagent=True),
        _scores("t2", supervisor=False),
    ]


def _arm() -> ArmResult:
    summary = aggregate_tier_summary(
        (), not_measured_latency_metrics(), _three_repeat_turns(), withhold_cost=True
    )
    return dataclasses.replace(_sample_arm(), tier_summary_fields=summary.to_report_fields())


def _matrix() -> MatrixResult:
    return build_matrix_result([_arm()])


def _entry(summary: dict[str, object], tier: str) -> dict[str, object]:
    return cast(dict[str, list[dict[str, object]]], summary["tiers"])[tier][0]


def _md_rows(markdown: str, heading: str) -> dict[str, list[str]]:
    section = markdown[markdown.index(f"### {heading}\n") :]
    rows: dict[str, list[str]] = {}
    for line in section.splitlines()[2:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split(" | ")]
        rows[cells[0]] = cells[1:]
    return rows


def _csv_rows(matrix_result: MatrixResult) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(render_csv(matrix_result))))


# ------------------------------------------------------------------------------------------- JSON


def test_tier_comparison_entry_carries_completion_over_repeats_beside_completion() -> None:
    summary = tier_comparison(_matrix())
    supervisor = _entry(summary, "supervisor")
    keys = list(supervisor)
    assert keys.index("completion_over_repeats") == keys.index("completion") + 1
    assert supervisor["completion_over_repeats"] == {
        "completed": 1,
        "applicable": 2,
        "repeats": 3,
        "rate": 0.5,
    }
    assert supervisor["completion"] == {"passed": 3, "applicable": 6, "rate": 0.5}
    assert _entry(summary, "subagent")["completion_over_repeats"] == {
        "completed": 1,
        "applicable": 1,
        "repeats": 3,
        "rate": 1.0,
    }


def test_matrix_json_carries_the_figure_in_both_the_comparison_and_the_arm_summary() -> None:
    payload = json.loads(render_json(_matrix()))
    comparison = payload["tier_comparison"]["tiers"]["tool"][0]["completion_over_repeats"]
    assert comparison == {"completed": 0, "applicable": 0, "repeats": 3, "rate": None}
    arm_summary = payload["arms"][0]["tier_summary"]["supervisor"]["completion_over_repeats"]
    assert arm_summary == {"completed": 1, "applicable": 2, "repeats": 3, "rate": 0.5}


# --------------------------------------------------------------------------------------- Markdown


def test_markdown_renders_one_row_per_tier_after_the_completion_rate_row() -> None:
    markdown = render_markdown(_matrix())
    for heading in ("Supervisor", "Subagent", "ToolCall"):
        labels = list(_md_rows(markdown, heading))
        assert labels.index("Task completion over repeats") == (
            labels.index("Task completion rate") + 1
        )
    assert _md_rows(markdown, "Supervisor")["Task completion over repeats"] == [
        "0.500000 (1/2, majority of 3)"
    ]
    assert _md_rows(markdown, "Subagent")["Task completion over repeats"] == [
        "1.000000 (1/1, majority of 3)"
    ]
    assert _md_rows(markdown, "ToolCall")["Task completion over repeats"] == ["not applicable"]


def test_the_definitions_note_states_the_strict_majority_rule_with_no_threshold() -> None:
    notes = cast(list[str], tier_comparison(_matrix())["notes"])
    assert _DEFINITION in notes[0]
    assert notes[0] == matrix._TIER_COMPARISON_DEFINITIONS_NOTE
    assert f"- {notes[0]}\n" in render_markdown(_matrix())
    assert "85" not in notes[0]
    assert "%" not in notes[0]


def test_the_rendered_markdown_carries_no_verdict_word() -> None:
    markdown = render_markdown(_matrix())
    assert "Task completion over repeats" in markdown
    assert harness._rendered_verdict_words(markdown) == []


# -------------------------------------------------------------------------------------------- CSV


def test_both_summary_csv_column_sets_carry_the_figure() -> None:
    rows = _csv_rows(_matrix())
    assert len(rows) == 2
    for row in rows:
        assert row["tier_supervisor_completion_over_repeats_completed"] == "1"
        assert row["tier_supervisor_completion_over_repeats_applicable"] == "2"
        assert row["tier_supervisor_completion_over_repeats_repeats"] == "3"
        assert row["tier_supervisor_completion_over_repeats_rate"] == "0.500000"
        assert row["tier_tool_completion_over_repeats_rate"] == ""
        assert row["summary_supervisor_completion_over_repeats_completed"] == "1"
        assert row["summary_supervisor_completion_over_repeats_applicable"] == "2"
        assert row["summary_supervisor_completion_over_repeats_repeats"] == "3"
        assert row["summary_supervisor_completion_over_repeats_rate"] == "0.500000"
        assert row["summary_subagent_completion_over_repeats_rate"] == "1.000000"
        assert row["summary_tool_completion_over_repeats_applicable"] == "0"


def test_the_csv_column_order_places_the_figure_after_the_completion_columns() -> None:
    header = render_csv(_matrix()).splitlines()[0].split(",")
    for prefix, after in (("tier", "success_rate"), ("summary", "completion_rate")):
        for tier in SCORED_TIERS:
            position = header.index(f"{prefix}_{tier}_{after}")
            assert header[position + 1 : position + 5] == [
                f"{prefix}_{tier}_completion_over_repeats_{suffix}"
                for suffix in ("completed", "applicable", "repeats", "rate")
            ]


# --------------------------------------------------------------------------------------- Archives


def _strip_repeat_completion(fields: dict[str, object]) -> dict[str, object]:
    summary = cast(dict[str, dict[str, object]], fields["tier_summary"])
    for entry in summary.values():
        entry.pop("completion_over_repeats")
    return fields


def test_an_archive_lacking_the_key_loads_and_renders_n_a() -> None:
    fields = _strip_repeat_completion(json.loads(json.dumps(_arm().to_report_fields())))
    restored = arm_from_report_fields(fields)
    matrix_result = build_matrix_result([restored])
    summary = tier_comparison(matrix_result)
    assert _entry(summary, "supervisor")["completion_over_repeats"] is None
    markdown = render_markdown(matrix_result)
    assert _md_rows(markdown, "Supervisor")["Task completion over repeats"] == ["n/a"]
    assert _md_rows(markdown, "Supervisor")["Task completion rate"] == ["0.500000 (3/6)"]
    row = _csv_rows(matrix_result)[0]
    assert row["summary_supervisor_completion_over_repeats_rate"] == ""
    assert row["tier_supervisor_completion_over_repeats_completed"] == ""
    assert (
        json.loads(render_json(matrix_result))["tier_comparison"]["tiers"]["supervisor"][0][
            "completion_over_repeats"
        ]
        is None
    )


def test_the_pre_spec_legacy_archive_still_loads_and_renders() -> None:
    payload = json.loads(_LEGACY_ARTIFACT.read_text(encoding="utf-8"))
    arms = [arm_from_report_fields(fields) for fields in payload["arms"]]
    matrix_result = build_matrix_result(arms)
    markdown = render_markdown(matrix_result)
    assert _md_rows(markdown, "Supervisor")["Task completion over repeats"] == ["n/a"] * len(arms)
    render_csv(matrix_result)
    render_json(matrix_result)


def test_an_archive_carrying_the_key_round_trips_it() -> None:
    arm = _arm()
    restored = arm_from_report_fields(json.loads(json.dumps(arm.to_report_fields())))
    assert render_markdown(build_matrix_result([restored])) == render_markdown(
        build_matrix_result([arm])
    )
