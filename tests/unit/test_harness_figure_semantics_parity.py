# SPDX-License-Identifier: MIT
"""Tests that ``--assert-artifacts`` refuses a ``matrix.md``/``matrix.json`` figure-semantics
mismatch.

Each test provokes a divergence on disk: a stale note in the JSON, a note absent from the JSON,
and a JSON that carries no semantics block at all.
"""

from __future__ import annotations

import json
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

_MODEL_ID = "us.anthropic.claude-opus-5-v1:0"


def _arm() -> ArmResult:
    return ArmResult(
        provenance=RunProvenance(
            mode="stub",
            timestamp="2026-01-01T00:00:00+00:00",
            model_id=_MODEL_ID,
            effort="low",
            dataset_seed=20260101,
            git_revision="abc1234",
            runtime_identifier="local-stub-runtime",
            price_table_version="v0-provisional",
            answer_style_sha256="0" * 64,
        ),
        model_metrics_fields={
            "model_id": _MODEL_ID,
            "included_invocation_count": 1,
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_write_tokens": 0,
            "cache_read_tokens": 0,
            "cache_min_checkpoint_tokens": 512,
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
                "assertion_results": [
                    {"name": "expected_field_changes[0]", "passed": True, "detail": ""}
                ],
            },
        ),
    )


def _publish(output_dir: Path) -> tuple[Path, ...]:
    """Write all four artifacts plus a passing ``demo``-untouched delta, as a real sweep does."""
    write_matrix_artifacts(build_matrix_result([_arm()]), output_dir)
    raw_path = archive_raw_run(_arm(), output_dir / "raw")
    delta_path = output_dir / FR93_INVENTORY_RELATIVE_PATH
    delta_path.parent.mkdir(parents=True, exist_ok=True)
    delta_path.write_text(
        json.dumps(
            {
                "resource_class": "eval_sweep",
                "layer": 6,
                "checks": [
                    {"check": "demo_tenant_untouched", "status": "pass", "detail": "0 rows"},
                    {
                        "check": "writes_only_under_smoke_tenant",
                        "status": "pass",
                        "detail": "1 row",
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return (raw_path,)


def _assert(output_dir: Path, raw_paths: tuple[Path, ...]) -> None:
    assert_artifacts(
        output_dir=output_dir,
        expected_arm_count=1,
        expected_task_count=1,
        raw_archive_paths=raw_paths,
    )


def test_artifacts_rendered_together_agree(tmp_path: Path) -> None:
    _assert(tmp_path, _publish(tmp_path))


def test_a_stale_note_left_in_the_json_fails(tmp_path: Path) -> None:
    """A JSON note left at superseded wording after the Markdown re-rendered fails the check."""
    raw_paths = _publish(tmp_path)
    json_path = tmp_path / "matrix.json"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    payload["figure_semantics"]["per_arm_correctness_composition"][0]["note"] = (
        "`mean_correctness` composition: negative guards a do-nothing turn satisfies are 0.8112 of "
        "the assertion mass this figure averages (107 guard assertions against 24 positive ones "
        "over the scored tasks), so the figure is not a task-achievement rate."
    )
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ArtifactAssertionError) as excinfo:
        _assert(tmp_path, raw_paths)
    (failure,) = [f for f in excinfo.value.failures if "figure semantics" in f]
    assert "per_arm_correctness_composition[0].note" in failure
    assert "re-render both from results/raw/" in failure


def test_a_note_the_markdown_never_rendered_fails(tmp_path: Path) -> None:
    """A note present in the JSON but never rendered in the Markdown fails the check."""
    raw_paths = _publish(tmp_path)
    json_path = tmp_path / "matrix.json"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    payload["figure_semantics"]["some_new_boundary"] = (
        "A boundary statement that exists only in the machine-readable artifact and that no reader "
        "of the human-readable one could ever discover."
    )
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ArtifactAssertionError) as excinfo:
        _assert(tmp_path, raw_paths)
    assert any("some_new_boundary" in f for f in excinfo.value.failures)


def test_a_json_with_no_semantics_block_fails(tmp_path: Path) -> None:
    raw_paths = _publish(tmp_path)
    json_path = tmp_path / "matrix.json"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    del payload["figure_semantics"]
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ArtifactAssertionError) as excinfo:
        _assert(tmp_path, raw_paths)
    assert any("carries no 'figure_semantics' object" in f for f in excinfo.value.failures)


def test_an_identifier_is_not_mistaken_for_a_published_note(tmp_path: Path) -> None:
    """A JSON model id or task id is not required to appear in the Markdown prose."""
    raw_paths = _publish(tmp_path)
    json_path = tmp_path / "matrix.json"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    payload["figure_semantics"]["per_arm_correctness_composition"][0]["model_id"] = (
        "a-model-id-that-is-not-in-the-markdown"
    )
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _assert(tmp_path, raw_paths)
