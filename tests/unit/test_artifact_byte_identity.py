# SPDX-License-Identifier: MIT
"""Byte-identity of matrix artifacts re-rendered from the raw archive.

Covers the per-tier, per-tool and per-task aggregations, stub not-measured latency rows, and the
``timestamp`` field as the only run-to-run difference.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.matrix import (
    ArmResult,
    MatrixResult,
    RunProvenance,
    archive_raw_run,
    arm_from_report_fields,
    build_matrix_result,
    load_raw_archives,
    render_csv,
    render_json,
    render_markdown,
)

# Shared aggregation fixtures.
from tests.unit.test_matrix import (  # noqa: E402
    _sample_arm_with_aggregations,
    _sample_matrix_with_aggregations,
)


def _round_trip(matrix: MatrixResult, raw_dir: Path) -> MatrixResult:
    """Archive every arm, reload from the raw directory, and rebuild the matrix."""
    for arm in matrix.arms:
        archive_raw_run(arm, raw_dir)
    reloaded = load_raw_archives(raw_dir)
    return build_matrix_result(reloaded)


def test_raw_archive_round_trip_is_byte_identical_with_the_new_fields(tmp_path: Path) -> None:
    """A re-render from the raw archive reproduces the JSON, Markdown and CSV artifacts exactly."""
    original = _sample_matrix_with_aggregations()
    reloaded = _round_trip(original, tmp_path / "raw")

    assert render_json(reloaded) == render_json(original)
    assert render_markdown(reloaded) == render_markdown(original)
    assert render_csv(reloaded) == render_csv(original)


def test_arm_from_report_fields_reconstructs_the_three_aggregations() -> None:
    """The inverse rebuilds each aggregation verbatim from ``to_report_fields`` output."""
    arm = _sample_arm_with_aggregations()
    rebuilt = arm_from_report_fields(arm.to_report_fields())

    assert rebuilt.tier_role_model_fields == arm.tier_role_model_fields
    assert rebuilt.tool_fields == arm.tool_fields
    assert rebuilt.task_spread_fields == arm.task_spread_fields


def test_arm_from_report_fields_defaults_a_pre_b4_4_archive_to_empty() -> None:
    """An archive without the aggregation fields reconstructs to empty tuples."""
    fields = dict(_sample_arm_with_aggregations().to_report_fields())
    for key in ("tier_role_model_metrics", "tool_metrics", "task_spread"):
        fields.pop(key)
    rebuilt = arm_from_report_fields(fields)

    assert rebuilt.tier_role_model_fields == ()
    assert rebuilt.tool_fields == ()
    assert rebuilt.task_spread_fields == ()


def test_arm_from_report_fields_rejects_a_non_list_aggregation() -> None:
    """A present but non-list aggregation raises ``ValueError``."""
    fields = dict(_sample_arm_with_aggregations().to_report_fields())
    fields["tool_metrics"] = {"not": "a list"}
    with pytest.raises(ValueError, match="tool_metrics"):
        arm_from_report_fields(fields)


def test_stub_latency_measured_false_rows_round_trip_unchanged(tmp_path: Path) -> None:
    """A ``call_latency_measured: false`` row with null figures survives the archive round-trip."""
    original = _sample_matrix_with_aggregations()
    # The fixture carries a not-measured row.
    tier_rows = original.arms[0].tier_role_model_fields
    assert any(row.get("call_latency_measured") is False for row in tier_rows)

    reloaded = _round_trip(original, tmp_path / "raw")
    reloaded_rows = reloaded.arms[0].tier_role_model_fields
    not_measured = [r for r in reloaded_rows if r.get("call_latency_measured") is False]
    assert not_measured
    assert not_measured[0]["call_latency_ms_p50"] is None
    assert not_measured[0]["call_latency_ms_p95"] is None
    assert render_json(reloaded) == render_json(original)


def _arm_at_timestamp(timestamp: str) -> ArmResult:
    base = _sample_arm_with_aggregations()
    provenance = base.provenance
    return ArmResult(
        provenance=RunProvenance(
            mode=provenance.mode,
            timestamp=timestamp,
            model_id=provenance.model_id,
            effort=provenance.effort,
            dataset_seed=provenance.dataset_seed,
            git_revision=provenance.git_revision,
            runtime_identifier=provenance.runtime_identifier,
            price_table_version=provenance.price_table_version,
            answer_style_sha256=provenance.answer_style_sha256,
            prompt_bundle_sha256=provenance.prompt_bundle_sha256,
        ),
        model_metrics_fields=base.model_metrics_fields,
        aggregate_score_fields=base.aggregate_score_fields,
        task_score_fields=base.task_score_fields,
        tier_role_model_fields=base.tier_role_model_fields,
        tool_fields=base.tool_fields,
        task_spread_fields=base.task_spread_fields,
    )


def test_timestamp_is_the_only_field_that_differs_across_two_otherwise_identical_runs() -> None:
    """Two arms differing only in ``timestamp`` render identically once it is normalised."""
    early = "2026-01-01T00:00:00+00:00"
    late = "2026-02-02T12:34:56+00:00"
    matrix_early = build_matrix_result([_arm_at_timestamp(early)])
    matrix_late = build_matrix_result([_arm_at_timestamp(late)])

    def _normalise(text: str) -> str:
        return text.replace(early, "TS").replace(late, "TS")

    assert _normalise(render_json(matrix_early)) == _normalise(render_json(matrix_late))
    assert _normalise(render_markdown(matrix_early)) == _normalise(render_markdown(matrix_late))
    assert _normalise(render_csv(matrix_early)) == _normalise(render_csv(matrix_late))
    # Unnormalised, the renders differ.
    assert render_json(matrix_early) != render_json(matrix_late)
