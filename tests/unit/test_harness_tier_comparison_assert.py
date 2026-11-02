# SPDX-License-Identifier: MIT
"""``assert_artifacts`` refuses a published tier comparison summary that falls short.

Artifacts are written by ``eval.matrix.write_matrix_artifacts`` for two arms, then one property is
corrupted on disk per cell; each corruption yields exactly its own failure.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from eval.harness import (
    FR93_INVENTORY_RELATIVE_PATH,
    ArtifactAssertionError,
    assert_artifacts,
)
from eval.matrix import (
    ArmResult,
    RunProvenance,
    archive_raw_run,
    build_matrix_result,
    write_matrix_artifacts,
)

_MODEL_IDS = ("anthropic.claude-opus-5-5", "openai.gpt-6-astra")


def _arm(model_id: str, family: str) -> ArmResult:
    return ArmResult(
        provenance=RunProvenance(
            mode="stub",
            timestamp=f"2026-01-01T00:00:0{_MODEL_IDS.index(model_id)}+00:00",
            model_id=model_id,
            effort="medium",
            dataset_seed=20260101,
            git_revision="not-a-commit",
            runtime_identifier="local-stub-runtime",
            price_table_version="v0-provisional",
            answer_style_sha256="0" * 64,
        ),
        model_metrics_fields={
            "model_id": model_id,
            "cost_withheld_provisional": True,
            "exclusions": {
                "total_excluded": 0,
                "by_category": {"cold_start": 0, "retry": 0, "throttle": 0},
            },
        },
        aggregate_score_fields={
            "scored_task_count": 1,
            "unscored_task_count": 0,
            "mean_correctness": 1.0,
            "bad_write_rate": 0.0,
            "tool_call_correct_rate": 1.0,
        },
        task_score_fields=(
            {
                "task_id": "gt-01-investigation-x",
                "scoring_mode": "machine",
                "correctness": 1.0,
                "unscored_reason": None,
                "bad_write": False,
                "tool_call_correct": True,
                "assertion_results": [],
            },
        ),
        tier_task_metrics_fields={"family": family},
    )


def _arms() -> list[ArmResult]:
    return [_arm(_MODEL_IDS[0], "anthropic"), _arm(_MODEL_IDS[1], "openai")]


def _publish(output_dir: Path) -> tuple[Path, ...]:
    """Write all artifacts plus a passing ``demo``-untouched delta, as a real sweep does."""
    write_matrix_artifacts(build_matrix_result(_arms()), output_dir)
    raw_paths = tuple(archive_raw_run(arm, output_dir / "raw") for arm in _arms())
    delta_path = output_dir / FR93_INVENTORY_RELATIVE_PATH
    delta_path.parent.mkdir(parents=True, exist_ok=True)
    delta_path.write_text(
        json.dumps(
            {
                "checks": [
                    {"check": "demo_tenant_untouched", "status": "pass", "detail": "0 rows"},
                    {"check": "writes_only_under_smoke_tenant", "status": "pass", "detail": "1"},
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return raw_paths


def _failures(output_dir: Path, raw_paths: tuple[Path, ...]) -> list[str]:
    try:
        assert_artifacts(
            output_dir=output_dir,
            expected_arm_count=2,
            expected_task_count=1,
            raw_archive_paths=raw_paths,
        )
    except ArtifactAssertionError as exc:
        return exc.failures
    return []


def _edit_json(output_dir: Path, edit: Callable[[dict[str, object]], None]) -> None:
    path = output_dir / "matrix.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    edit(payload)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _edit_md(output_dir: Path, edit: Callable[[str], str]) -> None:
    path = output_dir / "matrix.md"
    original = path.read_text(encoding="utf-8")
    edited = edit(original)
    assert edited != original
    path.write_text(edited, encoding="utf-8")


def _summary(payload: dict[str, object]) -> dict[str, object]:
    summary = payload["tier_comparison"]
    assert isinstance(summary, dict)
    return summary


def _drop_block(payload: dict[str, object]) -> None:
    del payload["tier_comparison"]


def _drop_column(payload: dict[str, object]) -> None:
    columns = _summary(payload)["columns"]
    assert isinstance(columns, list)
    columns.pop()


def _swap_column_models(payload: dict[str, object]) -> None:
    columns = _summary(payload)["columns"]
    assert isinstance(columns, list)
    columns[0]["model_id"], columns[1]["model_id"] = columns[1]["model_id"], columns[0]["model_id"]


def _drop_tier(payload: dict[str, object]) -> None:
    tiers = _summary(payload)["tiers"]
    assert isinstance(tiers, dict)
    del tiers["tool"]


def _drop_tier_entry(payload: dict[str, object]) -> None:
    tiers = _summary(payload)["tiers"]
    assert isinstance(tiers, dict)
    tiers["subagent"].pop()


def _empty_notes(payload: dict[str, object]) -> None:
    _summary(payload)["notes"] = []


def _second_note(output_dir: Path) -> str:
    payload = json.loads((output_dir / "matrix.json").read_text(encoding="utf-8"))
    note = _summary(payload)["notes"]
    assert isinstance(note, list)
    return str(note[1])


def _move_summary_after_reading(markdown: str) -> str:
    start = markdown.index("## Tier comparison summary")
    end = markdown.index("Measured dimensions per model arm")
    block = markdown[start:end]
    rest = markdown[:start] + markdown[end:]
    return rest + "\n" + block


def test_artifacts_rendered_together_carry_a_complete_summary(tmp_path: Path) -> None:
    assert _failures(tmp_path, _publish(tmp_path)) == []


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (_drop_block, "carries no 'tier_comparison' object"),
        (_drop_column, "carries 1 column(s), expected 2 (one per arm)"),
        (
            _swap_column_models,
            "expected the arms' ['anthropic.claude-opus-5-5', 'openai.gpt-6-astra']",
        ),
        (_drop_tier, "tier_comparison tiers are ['subagent', 'supervisor'], expected"),
        (_drop_tier_entry, "tier_comparison 'subagent' carries 1 entr(ies), expected 2"),
        (_empty_notes, "tier_comparison carries no notes"),
    ],
    ids=["missing-block", "column-count", "column-order", "missing-tier", "tier-entry", "no-notes"],
)
def test_each_json_defect_is_exactly_one_failure(
    tmp_path: Path, edit: Callable[[dict[str, object]], None], expected: str
) -> None:
    raw_paths = _publish(tmp_path)
    _edit_json(tmp_path, edit)
    failures = _failures(tmp_path, raw_paths)
    assert len(failures) == 1, failures
    assert expected in failures[0]


def test_a_note_absent_from_the_markdown_is_exactly_one_failure(tmp_path: Path) -> None:
    raw_paths = _publish(tmp_path)
    note = _second_note(tmp_path)
    _edit_md(tmp_path, lambda markdown: markdown.replace(f"- {note}\n", "", 1))
    failures = _failures(tmp_path, raw_paths)
    assert len(failures) == 1, failures
    assert "tier_comparison note 1 verbatim" in failures[0]


def test_a_summary_after_the_reading_section_is_exactly_one_failure(tmp_path: Path) -> None:
    raw_paths = _publish(tmp_path)
    _edit_md(tmp_path, _move_summary_after_reading)
    failures = _failures(tmp_path, raw_paths)
    assert failures == [
        f"{tmp_path / 'matrix.md'} renders '## Tier comparison summary' after "
        "'## Reading these figures'"
    ]


def test_a_markdown_without_the_summary_heading_is_exactly_one_failure(tmp_path: Path) -> None:
    raw_paths = _publish(tmp_path)
    _edit_md(
        tmp_path,
        lambda markdown: markdown.replace("## Tier comparison summary", "## Summary", 1),
    )
    failures = _failures(tmp_path, raw_paths)
    assert failures == [f"{tmp_path / 'matrix.md'} carries no '## Tier comparison summary' section"]


def test_every_defect_is_collected_rather_than_the_first(tmp_path: Path) -> None:
    raw_paths = _publish(tmp_path)
    _edit_json(tmp_path, _drop_column)
    _edit_json(tmp_path, _drop_tier)
    _edit_md(tmp_path, _move_summary_after_reading)
    failures = _failures(tmp_path, raw_paths)
    assert len(failures) == 3, failures


_REPEATS_SUMMARY: dict[str, object] = {
    "supervisor": {
        "success": {"passed": 1, "applicable": 1, "rate": 1.0},
        "completion_over_repeats": {"completed": 1, "applicable": 1, "repeats": 3, "rate": 1.0},
    }
}
_REPEATS_CELL = "1.000000 (1/1, majority of 3)"


def _publish_with_repeats(output_dir: Path) -> tuple[Path, ...]:
    """Publish arms whose supervisor tier carries a completion-over-repeats figure."""
    arms = [_arm(_MODEL_IDS[0], "anthropic"), _arm(_MODEL_IDS[1], "openai")]
    for arm in arms:
        arm.tier_summary_fields.update(json.loads(json.dumps(_REPEATS_SUMMARY)))
    write_matrix_artifacts(build_matrix_result(arms), output_dir)
    raw_paths = tuple(archive_raw_run(arm, output_dir / "raw") for arm in arms)
    _publish_delta(output_dir)
    return raw_paths


def _publish_delta(output_dir: Path) -> None:
    delta_path = output_dir / FR93_INVENTORY_RELATIVE_PATH
    delta_path.parent.mkdir(parents=True, exist_ok=True)
    delta_path.write_text(
        json.dumps(
            {
                "checks": [
                    {"check": "demo_tenant_untouched", "status": "pass", "detail": "0 rows"},
                    {"check": "writes_only_under_smoke_tenant", "status": "pass", "detail": "1"},
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _edit_csv(output_dir: Path, column: str, old: str, new: str) -> None:
    path = output_dir / "matrix.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    assert rows[0][column] == old
    for row in rows:
        row[column] = new
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_agreeing_repeat_completion_figures_pass(tmp_path: Path) -> None:
    raw_paths = _publish_with_repeats(tmp_path)
    assert _REPEATS_CELL in (tmp_path / "matrix.md").read_text(encoding="utf-8")
    assert _failures(tmp_path, raw_paths) == []


def test_a_markdown_repeat_completion_cell_disagreeing_with_json_is_one_failure(
    tmp_path: Path,
) -> None:
    raw_paths = _publish_with_repeats(tmp_path)
    _edit_md(tmp_path, lambda md: md.replace(_REPEATS_CELL, "0.000000 (0/1, majority of 3)", 1))
    failures = _failures(tmp_path, raw_paths)
    assert len(failures) == 1, failures
    assert "renders supervisor completion over repeats" in failures[0]
    assert "matrix.md" in failures[0]


def test_a_csv_repeat_completion_cell_disagreeing_with_json_is_one_failure(
    tmp_path: Path,
) -> None:
    raw_paths = _publish_with_repeats(tmp_path)
    _edit_csv(tmp_path, "summary_supervisor_completion_over_repeats_completed", "1", "0")
    failures = _failures(tmp_path, raw_paths)
    assert len(failures) == 2, failures
    assert all("matrix.csv records supervisor completion over repeats" in f for f in failures)


def test_a_json_repeat_completion_figure_disagreeing_with_both_renders_fails_each(
    tmp_path: Path,
) -> None:
    raw_paths = _publish_with_repeats(tmp_path)

    def _edit(payload: dict[str, object]) -> None:
        tiers = _summary(payload)["tiers"]
        assert isinstance(tiers, dict)
        tiers["supervisor"][0]["completion_over_repeats"]["completed"] = 0

    _edit_json(tmp_path, _edit)
    failures = _failures(tmp_path, raw_paths)
    assert len(failures) == 2, failures
    assert any("matrix.md renders supervisor" in f for f in failures)
    assert any("matrix.csv records supervisor" in f for f in failures)
