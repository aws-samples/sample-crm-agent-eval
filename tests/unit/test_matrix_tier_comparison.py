# SPDX-License-Identifier: MIT
"""The tier comparison summary in matrix.md/json/csv: definitions, placement, labels, no verdict.

Fixture arms hand-build the `tier_task_metrics` block in the shape `eval.metrics` emits. Every figure is
compared against a value computed by hand from the fixture, and every other section is shown
equal to the document with the summary projected out.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from eval import harness, matrix
from eval.matrix import (
    TIER_TASK_METRICS_KEY,
    ArmResult,
    MatrixResult,
    RunProvenance,
    archive_raw_run,
    arm_from_report_fields,
    build_matrix_result,
    render_csv,
    render_json,
    render_markdown,
    tier_comparison,
)

OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
HAIKU = "anthropic.claude-haiku-5-5"
ASTRA = "openai.gpt-6-astra"
SOL = "openai.gpt-6-1-sol"

_LEGACY_ARTIFACT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "eval" / "legacy_matrix_artifact.json"
)

_ENTRY_KEYS = [
    "model_ids",
    "efforts",
    "completion",
    "completion_over_repeats",
    "accuracy",
    "cost_withheld_provisional",
    "cost_per_completed_task",
    "cost_per_task",
    "completed_task_latency_ms_p50",
    "completed_task_latency_ms_p95",
    "latency_sample_count",
    "invocation_count",
    "long_context_invocation_count",
    "input_tokens",
    "output_tokens",
    "retry_count",
]

_ROW_LABELS = [
    "Model",
    "Effort",
    "Task completion rate",
    "Task completion over repeats",
    "Task accuracy",
    "Cost per completed task",
    "P95 latency per completed task (ms)",
    "P50 latency per completed task (ms)",
    "Model invocations",
    "Long-context invocations",
    "Input tokens",
    "Output tokens",
    "Cost per task, all tasks",
    "Self-corrected retries",
]


def _provenance(
    model_id: str, *, mode: str = "live", effort: str | None = "medium"
) -> RunProvenance:
    return RunProvenance(
        mode=mode,
        timestamp="2026-10-24T00:00:00Z",
        model_id=model_id,
        effort=effort,
        dataset_seed=42,
        git_revision="not-a-commit",
        runtime_identifier="local-stub-runtime",
        price_table_version="v1",
        answer_style_sha256="0" * 64,
    )


def _task(
    task_id: str, *, budget_exhausted: bool = False, truncated: bool = False
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "scoring_mode": "machine",
        "correctness": 1.0,
        "unscored_reason": None,
        "bad_write": False,
        "tool_call_correct": True,
        "truncated": truncated,
        "budget_exhausted": budget_exhausted,
        "assertion_results": [],
    }


def _aggregate(mean_correctness: float | None) -> dict[str, object]:
    return {
        "scored_task_count": 2,
        "unscored_task_count": 0,
        "mean_correctness": mean_correctness,
        "bad_write_rate": 0.0,
        "tool_call_correct_rate": 1.0,
    }


def _metrics(*, withheld: bool) -> dict[str, object]:
    return {
        "cost_withheld_provisional": withheld,
        "exclusions": {"total_excluded": 0, "by_category": {"cold_start": 0}},
    }


def _summary_entry(
    met: int, applicable: int, cost_total: float | None, tokens: tuple[int, int]
) -> dict[str, object]:
    return {
        "input_tokens": tokens[0],
        "output_tokens": tokens[1],
        "cost_total": cost_total,
        "latency_p50_ms": None,
        "latency_p95_ms": None,
        "success": {
            "passed": met,
            "applicable": applicable,
            "rate": met / applicable if applicable else None,
        },
    }


def _role_row(tier: str, model_id: str, invocations: int) -> dict[str, object]:
    return {
        "tier": tier,
        "role": tier,
        "model_id": model_id,
        "included_invocation_count": invocations,
        "input_tokens": 10 * invocations,
        "output_tokens": invocations,
    }


def _block(
    family: str,
    models: tuple[str, str, str],
    *,
    latencies: dict[str, tuple[int, float | None, float | None]],
    efforts: dict[str, list[str | None]],
    outcomes: tuple[int, int, int, int, int],
    retries: tuple[int, int, int] | None = None,
) -> dict[str, object]:
    """An arm's ``tier_task_metrics`` block; ``retries=None`` omits ``retry_count``."""
    tiers = ("supervisor", "subagent", "tool")
    by_tier: dict[str, dict[str, object]] = {
        tier: {
            "configured_model_id": model_id,
            "efforts": efforts[tier],
            "latency_sample_count": latencies[tier][0],
            "completed_task_latency_ms_p50": latencies[tier][1],
            "completed_task_latency_ms_p95": latencies[tier][2],
        }
        for tier, model_id in zip(tiers, models, strict=True)
    }
    if retries is not None:
        for tier, count in zip(tiers, retries, strict=True):
            by_tier[tier]["retry_count"] = count
    return {
        "family": family,
        "by_tier": by_tier,
        "delegation_outcomes": dict(
            zip(("data", "no_match", "denied", "tool_failure", "none"), outcomes, strict=True)
        ),
    }


def _anthropic_arm() -> ArmResult:
    """Live, cost published; two subagent models; zero tool completions; 5 tasks."""
    return ArmResult(
        provenance=_provenance(OPUS),
        model_metrics_fields=_metrics(withheld=False),
        aggregate_score_fields=_aggregate(0.7),
        task_score_fields=(
            _task("t1", budget_exhausted=True),
            _task("t2", truncated=True),
            _task("t3", truncated=True),
            _task("t4"),
            _task("t5", budget_exhausted=True, truncated=True),
        ),
        tier_role_model_fields=(
            _role_row("supervisor", OPUS, 5),
            _role_row("subagent", SONNET, 3),
            _role_row("subagent", HAIKU, 1),
            _role_row("tool", HAIKU, 4),
        ),
        tier_summary_fields={
            "supervisor": _summary_entry(3, 5, 0.6, (1000, 200)),
            "subagent": _summary_entry(2, 4, 0.2, (400, 80)),
            "tool": _summary_entry(0, 3, 0.09, (120, 30)),
        },
        tier_task_metrics_fields=_block(
            "anthropic",
            (OPUS, SONNET, HAIKU),
            latencies={
                "supervisor": (3, 1200.0, 2500.0),
                "subagent": (1, 800.0, 800.0),
                "tool": (0, None, None),
            },
            efforts={"supervisor": ["medium"], "subagent": ["high"], "tool": [None]},
            outcomes=(2, 1, 1, 0, 1),
            retries=(2, 1, 0),
        ),
    )


def _openai_arm(model_id: str = ASTRA, *, mode: str = "live", effort: str = "medium") -> ArmResult:
    """Cost withheld; no subagent-applicable task; 2 tasks."""
    return ArmResult(
        provenance=_provenance(model_id, mode=mode, effort=effort),
        model_metrics_fields=_metrics(withheld=True),
        aggregate_score_fields=_aggregate(0.5),
        task_score_fields=(_task("t1"), _task("t2")),
        tier_role_model_fields=(_role_row("supervisor", model_id, 2),),
        tier_summary_fields={
            "supervisor": _summary_entry(1, 2, None, (300, 60)),
            "subagent": _summary_entry(0, 0, None, (0, 0)),
            "tool": _summary_entry(1, 1, None, (0, 0)),
        },
        tier_task_metrics_fields=_block(
            "openai",
            (model_id, SOL, SOL),
            latencies={
                "supervisor": (1, 900.0, 900.0) if mode == "live" else (0, None, None),
                "subagent": (0, None, None),
                "tool": (0, None, None),
            },
            efforts={"supervisor": [effort], "subagent": [], "tool": []},
            outcomes=(0, 0, 0, 0, 2),
        ),
    )


def _three_arm_matrix() -> MatrixResult:
    """anthropic live, openai live (withheld), openai stub — a duplicate-family pair."""
    return build_matrix_result(
        [_anthropic_arm(), _openai_arm(), _openai_arm(SOL, mode="stub", effort="low")]
    )


def _entry(summary: dict[str, object], tier: str, column: int) -> dict[str, object]:
    return cast(dict[str, list[dict[str, object]]], summary["tiers"])[tier][column]


def _summary_block(markdown: str) -> str:
    start = markdown.index("## Tier comparison summary")
    end = markdown.index("Measured dimensions per model arm")
    return markdown[start:end]


def _md_table_rows(markdown: str, heading: str) -> dict[str, list[str]]:
    """Map each row label to its cells in the table under ``### <heading>`` of the summary."""
    block = _summary_block(markdown)
    section = block[block.index(f"### {heading}\n") :]
    rows: dict[str, list[str]] = {}
    for line in section.splitlines()[2:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split(" | ")]
        rows[cells[0]] = cells[1:]
    return rows


def _csv_rows(matrix_result: MatrixResult) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(render_csv(matrix_result))))


# ---------------------------------------------------------------------------------------------
# JSON shape and definitions
# ---------------------------------------------------------------------------------------------


def test_matrix_json_leads_with_the_tier_comparison_block() -> None:
    payload = json.loads(render_json(_three_arm_matrix()))
    assert list(payload) == ["tier_comparison", "arms", "figure_semantics"]
    summary = payload["tier_comparison"]
    assert list(summary) == ["columns", "tiers", "notes"]
    assert list(summary["tiers"]) == ["supervisor", "subagent", "tool"]
    assert all(len(entries) == 3 for entries in summary["tiers"].values())


def test_every_entry_carries_the_design_keys_in_order_with_one_tier_specific_key_last() -> None:
    summary = tier_comparison(_three_arm_matrix())
    specific = {
        "supervisor": "budget_exhausted_task_count",
        "subagent": "delegation_outcomes",
        "tool": "truncated_task_count",
    }
    for tier, last_key in specific.items():
        for column in range(3):
            assert list(_entry(summary, tier, column)) == [*_ENTRY_KEYS, last_key]


def test_columns_describe_each_arm_in_arm_order() -> None:
    columns = cast(list[dict[str, object]], tier_comparison(_three_arm_matrix())["columns"])
    assert [column["model_id"] for column in columns] == [OPUS, ASTRA, SOL]
    assert columns[0] == {
        "label": "anthropic",
        "family": "anthropic",
        "model_id": OPUS,
        "mode": "live",
        "effort": "medium",
        "task_count": 5,
    }
    assert [column["mode"] for column in columns] == ["live", "live", "stub"]
    assert [column["task_count"] for column in columns] == [5, 2, 2]


def test_every_figure_equals_its_definition_for_a_published_cost_arm() -> None:
    summary = tier_comparison(_three_arm_matrix())
    supervisor = _entry(summary, "supervisor", 0)
    assert supervisor["model_ids"] == [OPUS]
    assert supervisor["efforts"] == ["medium"]
    assert supervisor["completion"] == {"passed": 3, "applicable": 5, "rate": 0.6}
    assert supervisor["accuracy"] == 0.7
    assert supervisor["cost_withheld_provisional"] is False
    assert supervisor["cost_per_completed_task"] == pytest.approx(0.6 / 3)
    assert supervisor["cost_per_task"] == pytest.approx(0.6 / 5)
    assert supervisor["completed_task_latency_ms_p50"] == 1200.0
    assert supervisor["completed_task_latency_ms_p95"] == 2500.0
    assert supervisor["latency_sample_count"] == 3
    assert supervisor["invocation_count"] == 5
    assert (supervisor["input_tokens"], supervisor["output_tokens"]) == (1000, 200)
    assert supervisor["budget_exhausted_task_count"] == 2

    subagent = _entry(summary, "subagent", 0)
    assert subagent["model_ids"] == sorted([HAIKU, SONNET])
    assert subagent["completion"] == {"passed": 2, "applicable": 4, "rate": 0.5}
    assert subagent["accuracy"] == 0.5
    assert subagent["cost_per_completed_task"] == pytest.approx(0.2 / 2)
    assert subagent["cost_per_task"] == pytest.approx(0.2 / 5)
    assert subagent["invocation_count"] == 4
    assert subagent["delegation_outcomes"] == {
        "data": 2,
        "no_match": 1,
        "denied": 1,
        "tool_failure": 0,
        "none": 1,
    }

    tool = _entry(summary, "tool", 0)
    assert tool["efforts"] == [None]
    assert tool["completion"] == {"passed": 0, "applicable": 3, "rate": 0.0}
    assert tool["cost_per_completed_task"] is None
    assert tool["cost_per_task"] == pytest.approx(0.09 / 5)
    assert tool["latency_sample_count"] == 0
    assert tool["completed_task_latency_ms_p95"] is None
    assert tool["truncated_task_count"] == 3


def test_a_withheld_arm_carries_no_cost_figure() -> None:
    summary = tier_comparison(_three_arm_matrix())
    for tier in ("supervisor", "subagent", "tool"):
        entry = _entry(summary, tier, 1)
        assert entry["cost_withheld_provisional"] is True
        assert entry["cost_per_completed_task"] is None
        assert entry["cost_per_task"] is None


def test_the_model_row_is_the_configured_model_with_every_model_that_spent_tokens() -> None:
    summary = tier_comparison(_three_arm_matrix())
    assert _entry(summary, "subagent", 1)["model_ids"] == [SOL]
    assert _entry(summary, "supervisor", 2)["model_ids"] == [SOL]


# ---------------------------------------------------------------------------------------------
# Column labels
# ---------------------------------------------------------------------------------------------


def test_a_repeated_family_is_labelled_by_model_and_effort() -> None:
    columns = cast(list[dict[str, object]], tier_comparison(_three_arm_matrix())["columns"])
    assert [column["label"] for column in columns] == [
        "anthropic",
        f"openai ({ASTRA}, effort medium)",
        f"openai ({SOL}, effort low)",
    ]


def test_an_otherwise_identical_column_gets_its_arm_number() -> None:
    columns = cast(
        list[dict[str, object]],
        tier_comparison(build_matrix_result([_openai_arm(), _openai_arm()]))["columns"],
    )
    label = f"openai ({ASTRA}, effort medium)"
    assert [column["label"] for column in columns] == [f"{label} [arm 1]", f"{label} [arm 2]"]


def test_an_arm_without_a_recorded_family_is_labelled_by_its_model_id() -> None:
    bare = ArmResult(
        provenance=_provenance(OPUS),
        model_metrics_fields=_metrics(withheld=True),
        aggregate_score_fields=_aggregate(0.7),
    )
    columns = cast(list[dict[str, object]], tier_comparison(build_matrix_result([bare]))["columns"])
    assert columns[0]["label"] == OPUS
    assert columns[0]["family"] is None


# ---------------------------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------------------------


def test_matrix_md_orders_title_summary_intro_reading_then_arms() -> None:
    markdown = render_markdown(_three_arm_matrix())
    assert markdown.startswith("# Model Evaluation Matrix\n\n## Tier comparison summary\n")
    positions = [
        markdown.index(marker)
        for marker in (
            "## Tier comparison summary",
            "### Supervisor",
            "### Subagent",
            "### ToolCall",
            "Notes:",
            "Measured dimensions per model arm",
            "## Reading these figures",
            f"## {OPUS} (live-mode run)",
        )
    ]
    assert positions == sorted(positions)


def test_every_section_carries_the_r1_rows_in_order() -> None:
    markdown = render_markdown(_three_arm_matrix())
    delegation = [f"Delegation outcome: {kind}" for kind in matrix._DELEGATION_OUTCOME_KEYS]
    expected = {
        "Supervisor": [*_ROW_LABELS, "Budget-exhausted tasks"],
        "Subagent": [*_ROW_LABELS, *delegation],
        "ToolCall": [*_ROW_LABELS, "Truncated tasks"],
    }
    for heading, labels in expected.items():
        rows = _md_table_rows(markdown, heading)
        assert list(rows) == ["Metric", "---", *labels]
        assert all(len(cells) == 3 for cells in rows.values())


def test_markdown_cells_render_each_definition() -> None:
    markdown = render_markdown(_three_arm_matrix())
    supervisor = _md_table_rows(markdown, "Supervisor")
    assert supervisor["Metric"] == [
        "anthropic",
        f"openai ({ASTRA}, effort medium)",
        f"openai ({SOL}, effort low)",
    ]
    assert supervisor["Model"] == [OPUS, ASTRA, SOL]
    assert supervisor["Effort"] == ["medium", "medium", "low"]
    assert supervisor["Task completion rate"] == [
        "0.600000 (3/5)",
        "0.500000 (1/2)",
        "0.500000 (1/2)",
    ]
    assert supervisor["Task accuracy"] == ["0.700000", "0.500000", "0.500000"]
    assert supervisor["Cost per completed task"] == ["0.200000", "withheld", "withheld"]
    assert supervisor["Cost per task, all tasks"] == ["0.120000", "withheld", "withheld"]
    assert supervisor["P95 latency per completed task (ms)"] == [
        "2500.000000 (n=3)",
        "900.000000 (n=1)",
        "not measured",
    ]
    assert supervisor["P50 latency per completed task (ms)"][0] == "1200.000000 (n=3)"
    assert supervisor["Model invocations"] == ["5", "2", "2"]
    assert supervisor["Input tokens"] == ["1000", "300", "300"]
    assert supervisor["Budget-exhausted tasks"] == ["2", "0", "0"]

    subagent = _md_table_rows(markdown, "Subagent")
    assert subagent["Model"][0] == f"{HAIKU}, {SONNET}"
    assert subagent["Effort"] == ["high", "—", "—"]
    assert subagent["Task completion rate"] == [
        "0.500000 (2/4)",
        "not applicable",
        "not applicable",
    ]
    assert subagent["Task accuracy"] == ["0.500000", "not applicable", "not applicable"]
    assert subagent["Delegation outcome: none"] == ["1", "2", "2"]

    tool = _md_table_rows(markdown, "ToolCall")
    assert tool["Effort"][0] == "unsupported"
    assert tool["Task completion rate"][0] == "0.000000 (0/3)"
    assert tool["Cost per completed task"][0] == "—"
    assert tool["P95 latency per completed task (ms)"][0] == "— (n=0)"
    assert tool["Truncated tasks"] == ["3", "0", "0"]


def test_every_note_appears_verbatim_in_matrix_md() -> None:
    matrix_result = _three_arm_matrix()
    notes = cast(list[str], tier_comparison(matrix_result)["notes"])
    markdown = render_markdown(matrix_result)
    assert len(notes) >= 4
    for note in notes:
        assert f"- {note}\n" in _summary_block(markdown)


def test_the_fixed_notes_lead_and_conditional_notes_follow_only_when_they_apply() -> None:
    fixed = [
        matrix._TIER_COMPARISON_DEFINITIONS_NOTE,
        matrix._TIER_COMPARISON_COLUMNS_NOTE,
        matrix._TIER_COMPARISON_EFFORT_NOTE,
        matrix._TIER_COMPARISON_TOKENS_NOTE,
    ]
    live_published = build_matrix_result([_anthropic_arm()])
    assert tier_comparison(live_published)["notes"] == fixed

    lower_bound = ArmResult(
        provenance=_provenance(SOL, mode="stub"),
        model_metrics_fields=_metrics(withheld=True),
        aggregate_score_fields=_aggregate(None),
        usage_lower_bound_task_ids=("t1",),
    )
    errored = build_matrix_result([_anthropic_arm(), lower_bound], errored_item_count=2)
    assert tier_comparison(errored)["notes"] == [
        *fixed,
        matrix._TIER_COMPARISON_STUB_NOTE,
        matrix._TIER_COMPARISON_WITHHELD_NOTE,
        matrix.errored_items_cost_note(2),
        f"Usage and cost for {SOL} are a lower bound; that arm's section names the affected "
        "task runs.",
    ]


def test_the_summary_uses_no_verdict_word_bold_or_currency_figure() -> None:
    matrix_result = _three_arm_matrix()
    block = _summary_block(render_markdown(matrix_result))
    assert harness._rendered_verdict_words(block) == []
    assert "**" not in block
    notes = " ".join(cast(list[str], tier_comparison(matrix_result)["notes"]))
    assert harness._rendered_verdict_words(notes) == []
    assert not re.search(r"[$€£]\s*\d|\d\s*(USD|EUR|GBP|dollars?|cents?)\b", notes)


def test_the_provenance_table_omits_git_revision_while_json_keeps_it() -> None:
    matrix_result = _three_arm_matrix()
    assert "git_revision" not in render_markdown(matrix_result)
    payload = json.loads(render_json(matrix_result))
    assert payload["arms"][0]["provenance"]["git_revision"] == "not-a-commit"


# ---------------------------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------------------------


def _expected_summary_columns() -> list[str]:
    suffixes = [
        "model_ids",
        "efforts",
        "completion_passed",
        "completion_applicable",
        "completion_rate",
        "completion_over_repeats_completed",
        "completion_over_repeats_applicable",
        "completion_over_repeats_repeats",
        "completion_over_repeats_rate",
        "accuracy",
        "cost_per_completed_task",
        "cost_per_task",
        "completed_task_latency_ms_p50",
        "completed_task_latency_ms_p95",
        "latency_sample_count",
        "invocation_count",
        "long_context_invocation_count",
        "input_tokens",
        "output_tokens",
    ]
    return [
        "summary_column",
        *(f"summary_{tier}_{s}" for tier in ("supervisor", "subagent", "tool") for s in suffixes),
        "summary_supervisor_budget_exhausted_task_count",
        *(
            f"summary_subagent_delegation_{kind}"
            for kind in ("data", "no_match", "denied", "tool_failure", "none")
        ),
        "summary_tool_truncated_task_count",
        *(f"summary_{tier}_retry_count" for tier in ("supervisor", "subagent", "tool")),
    ]


def test_csv_appends_the_summary_columns_after_every_existing_column() -> None:
    header = render_csv(_three_arm_matrix()).splitlines()[0].split(",")
    summary = _expected_summary_columns()
    assert header[-len(summary) :] == summary
    assert header[: -len(summary)][-1] == "tier_tool_completion_over_repeats_rate"


def test_csv_repeats_each_arms_summary_values_on_its_task_rows() -> None:
    rows = _csv_rows(_three_arm_matrix())
    assert len(rows) == 5 + 2 + 2
    anthropic = rows[:5]
    assert {row["summary_column"] for row in anthropic} == {"anthropic"}
    first = anthropic[0]
    assert first["summary_supervisor_model_ids"] == OPUS
    assert first["summary_subagent_model_ids"] == f"{HAIKU}, {SONNET}"
    assert first["summary_tool_efforts"] == "unsupported"
    assert first["summary_supervisor_completion_rate"] == "0.600000"
    assert first["summary_supervisor_cost_per_completed_task"] == "0.200000"
    assert first["summary_tool_cost_per_completed_task"] == ""
    assert first["summary_supervisor_latency_sample_count"] == "3"
    assert first["summary_subagent_delegation_data"] == "2"
    assert first["summary_tool_truncated_task_count"] == "3"
    withheld = rows[5]
    assert withheld["summary_supervisor_cost_per_task"] == ""
    assert withheld["summary_subagent_efforts"] == ""
    assert withheld["summary_subagent_completion_rate"] == ""


# ---------------------------------------------------------------------------------------------
# The document outside the summary
# ---------------------------------------------------------------------------------------------


def _pre_summary_markdown(matrix_result: MatrixResult) -> str:
    """The pre-summary document: title, intro, errored note, Reading section, arms."""
    lines = [
        "# Model Evaluation Matrix",
        "",
        "Measured dimensions per model arm — direct measurements only; no reference threshold or "
        "conclusion is computed or shown here.",
        "",
    ]
    errored = matrix.errored_items_cost_note(matrix_result.errored_item_count)
    if errored is not None:
        lines.extend([errored, ""])
    lines.append(matrix._render_figure_semantics_markdown(matrix_result))
    lines.extend(matrix._render_arm_markdown(arm) for arm in matrix_result.arms)
    return "\n".join(lines).rstrip("\n") + "\n"


@pytest.mark.parametrize("errored_item_count", [0, 3])
def test_existing_sections_are_unchanged_once_the_summary_is_projected_out(
    errored_item_count: int,
) -> None:
    source = _three_arm_matrix()
    matrix_result = MatrixResult(arms=source.arms, errored_item_count=errored_item_count)
    markdown = render_markdown(matrix_result)
    assert markdown.replace(
        matrix._render_tier_comparison_markdown(matrix_result) + "\n", "", 1
    ) == (_pre_summary_markdown(matrix_result))

    payload = json.loads(render_json(matrix_result))
    payload.pop("tier_comparison")
    assert payload == {
        "arms": [arm.to_report_fields() for arm in matrix_result.arms],
        "figure_semantics": matrix.render_figure_semantics(matrix_result),
    }

    rows = list(csv.reader(io.StringIO(render_csv(matrix_result))))
    width = len(_expected_summary_columns())
    bare = MatrixResult(
        arms=tuple(
            dataclasses.replace(arm, tier_task_metrics_fields={}) for arm in matrix_result.arms
        )
    )
    bare_rows = list(csv.reader(io.StringIO(render_csv(bare))))
    assert [row[:-width] for row in rows] == [row[:-width] for row in bare_rows]


def test_an_arm_without_the_block_emits_no_tier_task_metrics_key() -> None:
    bare = dataclasses.replace(_anthropic_arm(), tier_task_metrics_fields={})
    assert TIER_TASK_METRICS_KEY not in bare.to_report_fields()
    fields = list(_anthropic_arm().to_report_fields())
    assert fields.index(TIER_TASK_METRICS_KEY) == fields.index("tier_summary") + 1


# ---------------------------------------------------------------------------------------------
# Archives and determinism
# ---------------------------------------------------------------------------------------------


def test_a_pre_spec_archive_re_renders_with_absent_rows_as_dashes() -> None:
    payload = json.loads(_LEGACY_ARTIFACT.read_text(encoding="utf-8"))
    arms = [arm_from_report_fields(fields) for fields in payload["arms"]]
    assert all(arm.tier_task_metrics_fields == {} for arm in arms)
    matrix_result = build_matrix_result(arms)
    markdown = render_markdown(matrix_result)
    supervisor = _md_table_rows(markdown, "Supervisor")
    assert supervisor["Effort"] == ["—"] * len(arms)
    assert supervisor["P95 latency per completed task (ms)"] == ["—"] * len(arms)
    assert supervisor["Task completion rate"] == ["—"] * len(arms)
    assert _md_table_rows(markdown, "Subagent")["Delegation outcome: data"] == ["—"] * len(arms)
    render_csv(matrix_result)
    render_json(matrix_result)


def test_an_archived_arm_round_trips_its_block(tmp_path: Path) -> None:
    arm = _anthropic_arm()
    path = archive_raw_run(arm, tmp_path)
    restored = arm_from_report_fields(json.loads(path.read_text(encoding="utf-8")))
    assert restored.tier_task_metrics_fields == arm.tier_task_metrics_fields
    assert render_markdown(build_matrix_result([restored])) == render_markdown(
        build_matrix_result([arm])
    )


def test_a_non_mapping_block_is_rejected_on_load() -> None:
    fields = _anthropic_arm().to_report_fields()
    fields[TIER_TASK_METRICS_KEY] = ["not", "a", "mapping"]
    with pytest.raises(ValueError, match="'tier_task_metrics' is not an object"):
        arm_from_report_fields(fields)


def test_rendering_twice_is_byte_identical() -> None:
    first, second = _three_arm_matrix(), _three_arm_matrix()
    assert render_markdown(first) == render_markdown(second)
    assert render_json(first) == render_json(second)
    assert render_csv(first) == render_csv(second)


# ---------------------------------------------------------------------------------------------
# Self-corrected retries
# ---------------------------------------------------------------------------------------------


def test_the_retry_row_reports_each_tier_and_tool_is_not_applicable() -> None:
    markdown = render_markdown(_three_arm_matrix())
    assert _md_table_rows(markdown, "Supervisor")["Self-corrected retries"] == ["2", "—", "—"]
    assert _md_table_rows(markdown, "Subagent")["Self-corrected retries"] == ["1", "—", "—"]
    assert _md_table_rows(markdown, "ToolCall")["Self-corrected retries"] == ["not applicable"] * 3


def test_the_retry_count_is_in_the_json_entry_and_the_csv_columns() -> None:
    summary = tier_comparison(_three_arm_matrix())
    assert [
        _entry(summary, tier, 0)["retry_count"] for tier in ("supervisor", "subagent", "tool")
    ] == [
        2,
        1,
        0,
    ]
    assert _entry(summary, "supervisor", 1)["retry_count"] is None
    first = _csv_rows(_three_arm_matrix())[0]
    assert first["summary_supervisor_retry_count"] == "2"
    assert first["summary_subagent_retry_count"] == "1"
    assert first["summary_tool_retry_count"] == "0"
    assert _csv_rows(_three_arm_matrix())[5]["summary_supervisor_retry_count"] == ""


def test_a_pre_spec_archive_renders_the_retry_row_as_dashes() -> None:
    payload = json.loads(_LEGACY_ARTIFACT.read_text(encoding="utf-8"))
    arms = [arm_from_report_fields(fields) for fields in payload["arms"]]
    markdown = render_markdown(build_matrix_result(arms))
    assert _md_table_rows(markdown, "Supervisor")["Self-corrected retries"] == ["—"] * len(arms)


# ---------------------------------------------------------------------------------------------
# Display table (tier_comparison_table)
# ---------------------------------------------------------------------------------------------

#: sha256 of each fixture's ``## Tier comparison summary`` block as rendered before the display
#: table was extracted; the Markdown must stay byte-identical.
_SUMMARY_BLOCK_SHA256 = {
    "three_arm": "7b8e1b5660043e6a80f6a2872ab5515d0f97618d4ab48fa0ac04848ea021b243",
    "single_anthropic": "e2e15c22b74b69b68645197b5b3df48f057f8173dea7b1334775b99d4399fb7c",
    "single_openai_stub": "49e873b4c6f086c526a30e08cd97c5602deaaf2330d7c3fbde323cd14a726c96",
    "legacy": "d3b5445014b8fabf050577255414115161ec7532d9d94b79d714a7dc52e23759",
}


def _legacy_matrix() -> MatrixResult:
    payload = json.loads(_LEGACY_ARTIFACT.read_text(encoding="utf-8"))
    return build_matrix_result([arm_from_report_fields(fields) for fields in payload["arms"]])


def _fixture_matrices() -> dict[str, MatrixResult]:
    return {
        "three_arm": _three_arm_matrix(),
        "single_anthropic": build_matrix_result([_anthropic_arm()]),
        "single_openai_stub": build_matrix_result([_openai_arm(SOL, mode="stub", effort="low")]),
        "legacy": _legacy_matrix(),
    }


def _table_as_markdown(table: dict[str, object]) -> str:
    """Format a display table the way ``matrix.md`` lays it out, independently of the module."""
    headers = cast(list[str], table["headers"])

    def _line(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    lines = [
        "## Tier comparison summary",
        "",
        "Per-tier figures for each arm, one column per arm in run order; the notes below the "
        "tables define each row.",
        "",
    ]
    for section in cast(list[dict[str, object]], table["sections"]):
        lines.extend([f"### {section['heading']}", "", _line(headers)])
        lines.append(_line(["---"] * len(headers)))
        lines.extend(_line(row) for row in cast(list[list[str]], section["rows"]))
        lines.append("")
    lines.extend(["Notes:", ""])
    lines.extend(f"- {note}" for note in cast(list[str], table["notes"]))
    lines.append("")
    return "\n".join(lines) + "\n"


def _table(matrix_result: MatrixResult) -> dict[str, object]:
    return matrix.tier_comparison_table(tier_comparison(matrix_result))


def _section_rows(table: dict[str, object], tier: str) -> dict[str, list[str]]:
    for section in cast(list[dict[str, object]], table["sections"]):
        if section["tier"] == tier:
            return {row[0]: row[1:] for row in cast(list[list[str]], section["rows"])}
    raise AssertionError(f"no section for {tier}")


@pytest.mark.parametrize("name", sorted(_SUMMARY_BLOCK_SHA256))
def test_the_summary_markdown_is_byte_identical_to_the_pre_extraction_render(name: str) -> None:
    block = _summary_block(render_markdown(_fixture_matrices()[name]))
    assert hashlib.sha256(block.encode("utf-8")).hexdigest() == _SUMMARY_BLOCK_SHA256[name]


@pytest.mark.parametrize("name", sorted(_SUMMARY_BLOCK_SHA256))
def test_the_json_round_tripped_table_formats_to_the_matrix_md_section(name: str) -> None:
    matrix_result = _fixture_matrices()[name]
    round_tripped = json.loads(render_json(matrix_result))["tier_comparison"]
    table = matrix.tier_comparison_table(round_tripped)
    assert table == _table(matrix_result)
    assert _table_as_markdown(table) == _summary_block(render_markdown(matrix_result))


def test_the_table_has_headers_three_sections_in_order_and_the_notes() -> None:
    matrix_result = _three_arm_matrix()
    table = _table(matrix_result)
    assert list(table) == ["headers", "sections", "notes"]
    assert table["headers"] == [
        "Metric",
        "anthropic",
        f"openai ({ASTRA}, effort medium)",
        f"openai ({SOL}, effort low)",
    ]
    sections = cast(list[dict[str, object]], table["sections"])
    assert [(s["tier"], s["heading"]) for s in sections] == [
        ("supervisor", "Supervisor"),
        ("subagent", "Subagent"),
        ("tool", "ToolCall"),
    ]
    assert [list(s) for s in sections] == [["tier", "heading", "rows"]] * 3
    for section in sections:
        rows = cast(list[list[str]], section["rows"])
        assert [row[0] for row in rows[: len(_ROW_LABELS)]] == _ROW_LABELS
        assert all(len(row) == 4 and all(isinstance(c, str) for c in row) for row in rows)
    assert table["notes"] == tier_comparison(matrix_result)["notes"]


def test_table_cells_render_null_withheld_and_not_applicable() -> None:
    table = _table(_three_arm_matrix())
    supervisor = _section_rows(table, "supervisor")
    assert supervisor["Cost per completed task"] == ["0.200000", "withheld", "withheld"]
    assert supervisor["Self-corrected retries"] == ["2", "—", "—"]
    subagent = _section_rows(table, "subagent")
    assert subagent["Task completion rate"][1:] == ["not applicable", "not applicable"]
    assert subagent["Task accuracy"][1:] == ["not applicable", "not applicable"]
    tool = _section_rows(table, "tool")
    assert tool["Cost per completed task"][0] == "—"
    assert tool["Self-corrected retries"] == ["not applicable"] * 3


def test_a_pre_spec_archive_table_renders_absent_figures_as_dashes() -> None:
    matrix_result = _legacy_matrix()
    supervisor = _section_rows(_table(matrix_result), "supervisor")
    columns = len(matrix_result.arms)
    assert supervisor["Effort"] == ["—"] * columns
    assert supervisor["Task completion rate"] == ["—"] * columns


def test_a_single_arm_table_has_one_cell_per_row() -> None:
    table = _table(build_matrix_result([_anthropic_arm()]))
    assert table["headers"] == ["Metric", "anthropic"]
    for section in cast(list[dict[str, object]], table["sections"]):
        assert all(len(row) == 2 for row in cast(list[list[str]], section["rows"]))


def test_an_unexercised_tier_still_renders_its_section() -> None:
    subagent = _section_rows(_table(build_matrix_result([_openai_arm()])), "subagent")
    assert subagent["Task completion rate"] == ["not applicable"]
    assert subagent["Model"] == [SOL]
    assert subagent["Delegation outcome: none"] == ["2"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.pop("columns"),
        lambda s: s.update(columns="x"),
        lambda s: s.update(columns=["x"]),
        lambda s: s.update(tiers=[]),
        lambda s: s["tiers"].pop("tool"),
        lambda s: s["tiers"].update(subagent=s["tiers"]["subagent"][:1]),
        lambda s: s["tiers"].update(supervisor=["x"] * len(s["columns"])),
        lambda s: s.update(notes=[1]),
        lambda s: s.pop("notes"),
    ],
)
def test_a_malformed_summary_is_refused(mutate: Callable[[dict[str, Any]], object]) -> None:
    summary: dict[str, Any] = json.loads(json.dumps(tier_comparison(_three_arm_matrix())))
    mutate(summary)
    with pytest.raises(ValueError, match="tier comparison summary"):
        matrix.tier_comparison_table(summary)


# ---------------------------------------------------------------------------------------------
# Long-context invocation counts
# ---------------------------------------------------------------------------------------------


def _counted_arm(counts: tuple[int, int, int]) -> ArmResult:
    """The anthropic arm with a long-context count recorded at each tier."""
    arm = _anthropic_arm()
    summary: dict[str, object] = {
        tier: {
            **cast(dict[str, object], arm.tier_summary_fields[tier]),
            "long_context_invocation_count": n,
        }
        for tier, n in zip(("supervisor", "subagent", "tool"), counts, strict=True)
    }
    rows = tuple({**row, "long_context_invocation_count": 0} for row in arm.tier_role_model_fields)
    return dataclasses.replace(
        arm,
        tier_summary_fields=summary,
        tier_role_model_fields=rows,
        model_metrics_fields={**arm.model_metrics_fields, "long_context_invocation_count": 0},
    )


def test_the_entry_carries_the_tiers_long_context_count_or_none_when_unrecorded() -> None:
    summary = tier_comparison(build_matrix_result([_counted_arm((0, 2, 1)), _openai_arm()]))
    assert [
        _entry(summary, tier, 0)["long_context_invocation_count"]
        for tier in ("supervisor", "subagent", "tool")
    ] == [0, 2, 1]
    assert _entry(summary, "tool", 1)["long_context_invocation_count"] is None


def test_the_long_context_row_renders_counts_and_a_dash_for_an_older_record() -> None:
    matrix_result = build_matrix_result([_counted_arm((0, 2, 1)), _openai_arm()])
    table = _table(matrix_result)
    assert _section_rows(table, "supervisor")["Long-context invocations"] == ["0", "—"]
    assert _section_rows(table, "subagent")["Long-context invocations"] == ["2", "—"]
    assert _section_rows(table, "tool")["Long-context invocations"] == ["1", "—"]
    markdown = render_markdown(matrix_result)
    assert _md_table_rows(markdown, "Subagent")["Long-context invocations"] == ["2", "—"]


def test_a_json_round_tripped_record_without_the_key_renders_a_dash_not_zero() -> None:
    summary = json.loads(render_json(build_matrix_result([_anthropic_arm()])))["tier_comparison"]
    for tier in ("supervisor", "subagent", "tool"):
        del summary["tiers"][tier][0]["long_context_invocation_count"]
    table = matrix.tier_comparison_table(summary)
    for tier in ("supervisor", "subagent", "tool"):
        assert _section_rows(table, tier)["Long-context invocations"] == ["—"]


def test_csv_carries_the_long_context_count_per_tier_in_both_column_families() -> None:
    rows = _csv_rows(build_matrix_result([_counted_arm((0, 2, 1)), _openai_arm()]))
    first, withheld = rows[0], rows[5]
    assert [
        first[f"summary_{t}_long_context_invocation_count"]
        for t in ("supervisor", "subagent", "tool")
    ] == ["0", "2", "1"]
    assert [
        first[f"tier_{t}_long_context_invocation_count"] for t in ("supervisor", "subagent", "tool")
    ] == ["0", "2", "1"]
    assert withheld["summary_tool_long_context_invocation_count"] == ""
    assert withheld["tier_tool_long_context_invocation_count"] == ""


def test_json_and_markdown_carry_the_count_per_row_and_per_arm() -> None:
    matrix_result = build_matrix_result([_counted_arm((0, 2, 1))])
    arm = json.loads(render_json(matrix_result))["arms"][0]
    assert all(row["long_context_invocation_count"] == 0 for row in arm["tier_role_model_metrics"])
    assert arm["model_metrics"]["long_context_invocation_count"] == 0
    assert arm["tier_summary"]["subagent"]["long_context_invocation_count"] == 2
    assert "long_context_invocation_count" in matrix._TIER_ROLE_MODEL_FIELDS
    assert "long_context_invocation_count" in matrix._MODEL_METRICS_FIELDS


def test_the_long_context_boundary_is_published_in_json_and_markdown_alike() -> None:
    matrix_result = build_matrix_result([_anthropic_arm()])
    note = matrix.render_figure_semantics(matrix_result)["long_context_invocation_count"]
    assert isinstance(note, str) and "long-context threshold" in note
    assert f"- `long_context_invocation_count` — {note}" in render_markdown(matrix_result)
    definitions = cast(list[str], tier_comparison(matrix_result)["notes"])[0]
    assert "Long-context invocations counts" in definitions


def test_the_run_payload_tier_table_passes_the_count_through_and_dashes_an_older_record() -> None:
    """The eval-runs payload table shows a recorded count and `—` for a stored row without it."""
    from eval.runs import _payload_tier_table

    report = json.loads(render_json(build_matrix_result([_counted_arm((0, 2, 1))])))
    table = _payload_tier_table("run-1", report)
    assert table is not None
    assert [
        _section_rows(table, t)["Long-context invocations"]
        for t in ("supervisor", "subagent", "tool")
    ] == [["0"], ["2"], ["1"]]

    for tier in ("supervisor", "subagent", "tool"):
        del report["tier_comparison"]["tiers"][tier][0]["long_context_invocation_count"]
    older = _payload_tier_table("run-2", report)
    assert older is not None
    assert [
        _section_rows(older, t)["Long-context invocations"]
        for t in ("supervisor", "subagent", "tool")
    ] == [["—"], ["—"], ["—"]]
