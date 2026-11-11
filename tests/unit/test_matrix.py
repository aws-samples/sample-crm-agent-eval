# SPDX-License-Identifier: MIT
"""Tests for the four-part matrix renderer: determinism, provenance, exclusions, cost, hygiene."""

from __future__ import annotations

import ast
import csv
import dataclasses
import io
import json
import re
from pathlib import Path
from typing import cast

import pytest
from scripts import proc_run

from crm.metering import TOKEN_ACCOUNTING, Family
from eval import matrix
from eval.matrix import (
    ARM_COST_TOTAL_KEY,
    ARM_LONG_CONTEXT_INVOCATION_COUNT_KEY,
    ArmResult,
    MatrixResult,
    RunProvenance,
    archive_raw_run,
    arm_from_report_fields,
    bad_write_rate_basis,
    build_matrix_result,
    correctness_composition,
    render_csv,
    render_figure_semantics,
    render_json,
    render_markdown,
    write_matrix_artifacts,
)

#: Vocabulary a rendered artifact must never contain, matched as whole words so the
#: per-assertion field name `passed` (`eval.scoring.AssertionResult.passed`) does not match "pass".
_VERDICT_VOCABULARY: tuple[str, ...] = (
    "pass",
    "fail",
    "verdict",
    "threshold_crossed",
    "recommend",
)


def _sample_provenance(
    *,
    model_id: str = "us.anthropic.claude-opus-5-v1:0",
    withheld: bool = True,
    runtime_identifier: str = "local-stub-runtime",
) -> RunProvenance:
    return RunProvenance(
        mode="stub",
        timestamp="2026-01-01T00:00:00+00:00",
        model_id=model_id,
        effort="high",
        dataset_seed=20260101,
        git_revision="abc1234",
        runtime_identifier=runtime_identifier,
        price_table_version="v0-provisional",
        answer_style_sha256="0" * 64,
        prompt_bundle_sha256="1" * 64,
    )


def _sample_model_metrics_fields(*, withheld: bool = True) -> dict[str, object]:
    fields: dict[str, object] = {
        "model_id": "us.anthropic.claude-opus-5-v1:0",
        "included_invocation_count": 5,
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_write_tokens": 200,
        "cache_read_tokens": 100,
        "cache_min_checkpoint_tokens": 512,
        "cost_withheld_provisional": withheld,
        "exclusions": {
            "total_excluded": 2,
            "by_category": {"cold_start": 1, "retry": 1, "throttle": 0},
        },
    }
    if not withheld:
        fields.update(
            {
                "cost_input": 0.01,
                "cost_output": 0.02,
                "cost_cache_write": 0.001,
                "cost_cache_read": 0.0005,
                "cost_excluding_cache": 0.03,
                "cost_cache_total": 0.0015,
                "cost_total": 0.0315,
                "price_table_version": "v1-final",
            }
        )
    return fields


def _sample_aggregate_score_fields() -> dict[str, object]:
    return {
        "scored_task_count": 18,
        "unscored_task_count": 2,
        "mean_correctness": 0.85,
        "bad_write_rate": 0.05,
        "tool_call_correct_rate": 0.9,
    }


def _sample_task_score_fields(task_id: str = "gt-01-investigation-x") -> dict[str, object]:
    return {
        "task_id": task_id,
        "scoring_mode": "machine",
        "correctness": 1.0,
        "unscored_reason": None,
        "bad_write": False,
        "tool_call_correct": True,
        "assertion_results": [{"name": "expected_field_changes[0]", "passed": True, "detail": ""}],
    }


def _sample_arm(
    *, withheld: bool = True, runtime_identifier: str = "local-stub-runtime"
) -> ArmResult:
    return ArmResult(
        provenance=_sample_provenance(withheld=withheld, runtime_identifier=runtime_identifier),
        model_metrics_fields=_sample_model_metrics_fields(withheld=withheld),
        aggregate_score_fields=_sample_aggregate_score_fields(),
        task_score_fields=(
            _sample_task_score_fields("gt-01-investigation-x"),
            _sample_task_score_fields("gt-04-enrichment-y"),
        ),
    )


def _sample_matrix(*, withheld: bool = True) -> MatrixResult:
    return build_matrix_result([_sample_arm(withheld=withheld)])


# ---------------------------------------------------------------------------------------------
# Per-arm aggregation fixtures (per-(tier,role,model), per-tool, per-task spread)
# ---------------------------------------------------------------------------------------------


def _sample_tier_role_model_fields(*, withheld: bool = True) -> list[dict[str, object]]:
    """Two `eval.metrics.TierRoleModelMetrics.to_report_fields` rows: one supervisor, one subagent.

    The subagent row carries `call_latency_measured=False` with null figures, exercising a
    renderer's ``—`` handling for a genuine null.
    """
    supervisor: dict[str, object] = {
        "tier": "supervisor",
        "role": "supervisor",
        "model_id": "us.anthropic.claude-opus-5-v1:0",
        "included_invocation_count": 4,
        "input_tokens": 800,
        "output_tokens": 400,
        "cache_read_tokens": 100,
        "cache_write_tokens": 50,
        "call_latency_measured": True,
        "call_latency_ms_p50": 120,
        "call_latency_ms_p95": 240,
        "cost_withheld_provisional": withheld,
        "price_table_version": "v0-provisional",
    }
    subagent: dict[str, object] = {
        "tier": "mid",
        "role": "subagent",
        "model_id": "us.anthropic.claude-sonnet-5-v1:0",
        "included_invocation_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "call_latency_measured": False,
        "call_latency_ms_p50": None,
        "call_latency_ms_p95": None,
        "cost_withheld_provisional": withheld,
        "price_table_version": "v0-provisional",
    }
    if not withheld:
        supervisor.update(
            {
                "cost_input": 0.008,
                "cost_output": 0.016,
                "cost_cache_read": 0.0004,
                "cost_cache_write": 0.0002,
                "cost_excluding_cache": 0.024,
                "cost_cache_total": 0.0006,
                "cost_total": 0.0246,
            }
        )
    return [supervisor, subagent]


def _sample_tool_fields() -> list[dict[str, object]]:
    """Two `eval.metrics.ToolAggregate.to_report_fields` rows with full outcome distributions."""
    return [
        {
            "tool_name": "get_account",
            "role": "tool",
            "count": 6,
            "call_latency_ms_p50": 30,
            "call_latency_ms_p95": 55,
            "truncated_count": 0,
            "outcome_distribution": {
                "data": 5,
                "no_match": 1,
                "denied": 0,
                "tool_failure": 0,
            },
        },
        {
            "tool_name": "search_opportunities",
            "role": "tool",
            "count": 3,
            "call_latency_ms_p50": 42,
            "call_latency_ms_p95": 70,
            "truncated_count": 1,
            "outcome_distribution": {
                "data": 2,
                "no_match": 0,
                "denied": 1,
                "tool_failure": 0,
            },
        },
    ]


def _sample_task_spread_fields() -> list[dict[str, object]]:
    """One `eval.metrics.TaskSpread.to_report_fields` row per task id used by `_sample_arm`."""
    return [
        {
            "task_id": "gt-01-investigation-x",
            "repeats": 3,
            "correctness_min": 0.5,
            "correctness_mean": 0.75,
            "correctness_max": 1.0,
            "success_fraction": 2 / 3,
        },
        {
            "task_id": "gt-04-enrichment-y",
            "repeats": 3,
            "correctness_min": 1.0,
            "correctness_mean": 1.0,
            "correctness_max": 1.0,
            "success_fraction": 1.0,
        },
    ]


def _sample_arm_with_aggregations(*, withheld: bool = True) -> ArmResult:
    """`_sample_arm` extended with the three per-arm aggregations."""
    base = _sample_arm(withheld=withheld)
    return ArmResult(
        provenance=base.provenance,
        model_metrics_fields=base.model_metrics_fields,
        aggregate_score_fields=base.aggregate_score_fields,
        task_score_fields=base.task_score_fields,
        tier_role_model_fields=tuple(_sample_tier_role_model_fields(withheld=withheld)),
        tool_fields=tuple(_sample_tool_fields()),
        task_spread_fields=tuple(_sample_task_spread_fields()),
    )


def _sample_matrix_with_aggregations(*, withheld: bool = True) -> MatrixResult:
    return build_matrix_result([_sample_arm_with_aggregations(withheld=withheld)])


# ---------------------------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------------------------


def test_json_rendering_is_byte_identical_for_identical_input() -> None:
    matrix = _sample_matrix()
    assert render_json(matrix) == render_json(_sample_matrix())


def test_markdown_rendering_is_byte_identical_for_identical_input() -> None:
    matrix = _sample_matrix()
    assert render_markdown(matrix) == render_markdown(_sample_matrix())


def test_csv_rendering_is_byte_identical_for_identical_input() -> None:
    matrix = _sample_matrix()
    assert render_csv(matrix) == render_csv(_sample_matrix())


def test_rendering_is_byte_identical_across_repeated_calls_on_the_same_object() -> None:
    matrix = _sample_matrix()
    first = (render_json(matrix), render_markdown(matrix), render_csv(matrix))
    second = (render_json(matrix), render_markdown(matrix), render_csv(matrix))
    assert first == second


def test_written_artifact_bytes_match_render_function_output(tmp_path: Path) -> None:
    matrix = _sample_matrix()
    paths = write_matrix_artifacts(matrix, tmp_path)
    assert paths["json"].read_text(encoding="utf-8") == render_json(matrix)
    assert paths["md"].read_text(encoding="utf-8") == render_markdown(matrix)
    # Read back as bytes so the csv module's \r\n line endings are compared, not normalised.
    assert paths["csv"].read_bytes() == render_csv(matrix).encode("utf-8")


def test_writing_artifacts_twice_produces_byte_identical_files(tmp_path: Path) -> None:
    matrix = _sample_matrix()
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    write_matrix_artifacts(matrix, first_dir)
    write_matrix_artifacts(_sample_matrix(), second_dir)
    for name in ("matrix.json", "matrix.md", "matrix.csv"):
        assert (first_dir / name).read_bytes() == (second_dir / name).read_bytes()


def test_multi_arm_order_is_preserved_not_resorted() -> None:
    arm_a = _sample_arm()
    arm_b = ArmResult(
        provenance=_sample_provenance(model_id="us.anthropic.claude-haiku-4-5-v1:0"),
        model_metrics_fields=_sample_model_metrics_fields(),
        aggregate_score_fields=_sample_aggregate_score_fields(),
        task_score_fields=(),
    )
    matrix = build_matrix_result([arm_b, arm_a])
    rendered = matrix.to_report_fields()
    arms = rendered["arms"]
    assert isinstance(arms, list)
    model_ids = []
    for arm in arms:
        assert isinstance(arm, dict)
        provenance = arm["provenance"]
        assert isinstance(provenance, dict)
        model_ids.append(provenance["model_id"])
    assert model_ids == ["us.anthropic.claude-haiku-4-5-v1:0", "us.anthropic.claude-opus-5-v1:0"]


# ---------------------------------------------------------------------------------------------
# No pass/fail verdict anywhere
# ---------------------------------------------------------------------------------------------


def _find_verdict_vocabulary(text: str) -> list[str]:
    lowered = text.lower()
    return [word for word in _VERDICT_VOCABULARY if re.search(rf"\b{re.escape(word)}\b", lowered)]


def _verdict_scan_matrix() -> MatrixResult:
    """A sample matrix whose second arm carries the usage lower-bound note."""
    return build_matrix_result(
        [_sample_arm(withheld=False), _lower_bound_arm("gt-04-enrichment-y")]
    )


def test_the_verdict_scan_sample_renders_the_lower_bound_note() -> None:
    """Positive control: the scanned Markdown really carries the lower-bound note text."""
    note = matrix.usage_lower_bound_note(_lower_bound_arm("gt-04-enrichment-y"))
    assert note is not None
    assert note in render_markdown(_verdict_scan_matrix())


def test_rendered_json_contains_no_verdict_vocabulary() -> None:
    assert _find_verdict_vocabulary(render_json(_verdict_scan_matrix())) == []


def test_rendered_markdown_contains_no_verdict_vocabulary() -> None:
    assert _find_verdict_vocabulary(render_markdown(_verdict_scan_matrix())) == []


def test_rendered_csv_contains_no_verdict_vocabulary() -> None:
    assert _find_verdict_vocabulary(render_csv(_verdict_scan_matrix())) == []


def test_the_artifact_verdict_scan_reads_the_lower_bound_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`assert_artifacts`' scan sees the note: a verdict word planted in it is found."""
    from eval.harness import _rendered_verdict_words

    assert _rendered_verdict_words(render_markdown(_verdict_scan_matrix())) == []
    monkeypatch.setattr(
        matrix,
        "usage_lower_bound_note",
        lambda arm: "the best arm" if arm.usage_lower_bound_task_ids else None,
    )
    assert "best" in _rendered_verdict_words(render_markdown(_verdict_scan_matrix()))


def test_module_source_contains_no_verdict_function_or_class_name() -> None:
    source = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath("src", "eval", "matrix.py")
        .read_text(encoding="utf-8")
    )
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
    lowered_names = [name.lower() for name in names]
    offenders = [name for name in lowered_names for word in _VERDICT_VOCABULARY if word in name]
    assert offenders == []


def test_no_vendor_sdk_import_in_matrix_module() -> None:
    source = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath("src", "eval", "matrix.py")
        .read_text(encoding="utf-8")
    )
    tree = ast.parse(source)
    banned = {"anthropic", "openai", "boto3", "botocore"}
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module.split(".")[0])
    assert not (set(found) & banned)


# ---------------------------------------------------------------------------------------------
# Full provenance per record
# ---------------------------------------------------------------------------------------------


def test_provenance_carries_every_required_field() -> None:
    provenance = _sample_provenance()
    fields = provenance.to_report_fields()
    for name in (
        "mode",
        "timestamp",
        "model_id",
        "effort",
        "dataset_seed",
        "git_revision",
        "runtime_identifier",
        "price_table_version",
    ):
        assert name in fields
    assert "temperature" not in fields


def test_rendered_json_includes_provenance_per_arm() -> None:
    matrix = _sample_matrix()
    rendered = json.loads(render_json(matrix))
    assert rendered["arms"][0]["provenance"]["git_revision"] == "abc1234"
    assert rendered["arms"][0]["provenance"]["mode"] == "stub"


def test_rendered_markdown_includes_provenance_per_arm() -> None:
    matrix = _sample_matrix()
    rendered = render_markdown(matrix)
    assert "git_revision" not in rendered
    assert "abc1234" not in rendered
    assert "runtime_identifier" in rendered


def test_raw_archive_carries_full_provenance(tmp_path: Path) -> None:
    arm = _sample_arm()
    path = archive_raw_run(arm, tmp_path)
    archived = json.loads(path.read_text(encoding="utf-8"))
    assert archived["provenance"]["git_revision"] == "abc1234"
    assert archived["provenance"]["runtime_identifier"] == "local-stub-runtime"


# ---------------------------------------------------------------------------------------------
# Exclusions as counted, categorised entries
# ---------------------------------------------------------------------------------------------


def test_rendered_markdown_names_every_exclusion_category_even_at_zero() -> None:
    matrix = _sample_matrix()
    rendered = render_markdown(matrix)
    assert "cold_start: 1" in rendered
    assert "retry: 1" in rendered
    assert "throttle: 0" in rendered


def test_rendered_json_preserves_full_exclusion_tally() -> None:
    matrix = _sample_matrix()
    rendered = json.loads(render_json(matrix))
    exclusions = rendered["arms"][0]["model_metrics"]["exclusions"]
    assert exclusions["total_excluded"] == 2
    assert exclusions["by_category"] == {"cold_start": 1, "retry": 1, "throttle": 0}


# ---------------------------------------------------------------------------------------------
# Cache thresholds accompany cost
# ---------------------------------------------------------------------------------------------


def test_rendered_markdown_reports_cache_threshold_alongside_metrics() -> None:
    matrix = _sample_matrix()
    rendered = render_markdown(matrix)
    assert "cache_min_checkpoint_tokens" in rendered
    assert "512" in rendered


def test_distinct_cache_thresholds_render_for_distinct_models() -> None:
    opus_metrics = _sample_model_metrics_fields()
    opus_metrics["cache_min_checkpoint_tokens"] = 512
    sonnet_metrics = _sample_model_metrics_fields()
    sonnet_metrics["model_id"] = "us.anthropic.claude-sonnet-5-v1:0"
    sonnet_metrics["cache_min_checkpoint_tokens"] = 1024
    haiku_metrics = _sample_model_metrics_fields()
    haiku_metrics["model_id"] = "us.anthropic.claude-haiku-4-5-v1:0"
    haiku_metrics["cache_min_checkpoint_tokens"] = 4096

    arms = [
        ArmResult(
            provenance=_sample_provenance(model_id=str(metrics["model_id"])),
            model_metrics_fields=metrics,
            aggregate_score_fields=_sample_aggregate_score_fields(),
            task_score_fields=(),
        )
        for metrics in (opus_metrics, sonnet_metrics, haiku_metrics)
    ]
    matrix = build_matrix_result(arms)
    rendered = json.loads(render_json(matrix))
    thresholds = [arm["model_metrics"]["cache_min_checkpoint_tokens"] for arm in rendered["arms"]]
    assert thresholds == [512, 1024, 4096]


# ---------------------------------------------------------------------------------------------
# Cost withheld while price table provisional
# ---------------------------------------------------------------------------------------------


def test_withheld_cost_never_appears_as_a_fabricated_figure_in_json() -> None:
    matrix = _sample_matrix(withheld=True)
    rendered = json.loads(render_json(matrix))
    metrics = rendered["arms"][0]["model_metrics"]
    assert metrics["cost_withheld_provisional"] is True
    assert "cost_total" not in metrics
    assert "cost_input" not in metrics


def test_withheld_cost_never_appears_in_markdown() -> None:
    matrix = _sample_matrix(withheld=True)
    rendered = render_markdown(matrix)
    assert "withheld" in rendered.lower()
    assert "cost_total" not in rendered


def test_withheld_cost_never_appears_in_csv() -> None:
    matrix = _sample_matrix(withheld=True)
    rendered = render_csv(matrix)
    assert "cost_total" not in rendered


def test_non_withheld_cost_does_appear_when_price_table_is_final() -> None:
    matrix = _sample_matrix(withheld=False)
    rendered_json = json.loads(render_json(matrix))
    metrics = rendered_json["arms"][0]["model_metrics"]
    assert metrics["cost_withheld_provisional"] is False
    assert metrics["cost_total"] == pytest.approx(0.0315)

    rendered_md = render_markdown(matrix)
    assert "cost_total" in rendered_md
    assert "0.031500" in rendered_md


# ---------------------------------------------------------------------------------------------
# No account identifier reaches a committed artifact
# ---------------------------------------------------------------------------------------------


def test_gitignore_excludes_raw_results_and_generated_top_level_artifacts() -> None:
    gitignore_path = Path(__file__).resolve().parents[2] / ".gitignore"
    gitignore_text = gitignore_path.read_text(encoding="utf-8")
    assert "results/raw/" in gitignore_text
    assert "results/matrix.json" in gitignore_text
    assert "results/matrix.csv" in gitignore_text


def _ignore_decision(path: str) -> tuple[bool, str]:
    """Return `(is_ignored, matching_rule)` for `path`, as git itself decides it.

    The plain `git check-ignore` exit status is the decision (`-v` exits 0 on a matching negation
    too); `-v` supplies the rule. `--no-index` makes tracked paths subject to the rules.
    """
    root = Path(__file__).resolve().parents[2]
    decision = proc_run.run(
        ["git", "check-ignore", "--no-index", path],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert decision.returncode in (0, 1), (
        f"git check-ignore failed for {path}: {decision.stdout}{decision.stderr}"
    )
    verbose = proc_run.run(
        ["git", "check-ignore", "--no-index", "-v", path],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return decision.returncode == 0, verbose.stdout.strip()


def test_matrix_md_is_not_gitignored_since_the_readme_points_at_it() -> None:
    """git's ignore *decision* for `results/matrix.md` is "not ignored"."""
    ignored, rule = _ignore_decision("results/matrix.md")
    assert not ignored, (
        f"results/matrix.md is ignored, but the README points readers at it (rule: {rule})"
    )

    root = Path(__file__).resolve().parents[2]
    tracked = proc_run.run(
        ["git", "ls-files", "--error-unmatch", "results/matrix.md"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode == 0, (
        f"results/matrix.md is not tracked: {tracked.stdout}{tracked.stderr}"
    )

    # Positive control: the same probe reports *ignored* for a sibling artifact.
    sibling_ignored, sibling_rule = _ignore_decision("results/matrix.json")
    assert sibling_ignored, (
        "git reports results/matrix.json as unignored, so this probe is not discriminating"
    )
    assert ".gitignore" in sibling_rule, sibling_rule


def test_arm_built_with_a_non_account_bearing_runtime_identifier_never_renders_an_account_id() -> (
    None
):
    matrix = build_matrix_result([_sample_arm(runtime_identifier="local-stub-runtime")])
    combined = render_json(matrix) + render_markdown(matrix) + render_csv(matrix)
    assert "arn:aws" not in combined


def test_raw_archive_is_written_beneath_the_gitignored_raw_directory(tmp_path: Path) -> None:
    raw_dir = tmp_path / "results" / "raw"
    arm = _sample_arm(
        runtime_identifier="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/example"
    )
    path = archive_raw_run(arm, raw_dir)
    assert path.parent == raw_dir
    assert "raw" in path.parts


def test_raw_archive_file_names_do_not_collide_across_repeated_runs(tmp_path: Path) -> None:
    arm_one = _sample_arm()
    arm_two_provenance = _sample_provenance()
    arm_two = ArmResult(
        provenance=RunProvenance(
            mode=arm_two_provenance.mode,
            timestamp="2026-01-01T00:00:01+00:00",
            model_id=arm_two_provenance.model_id,
            effort=arm_two_provenance.effort,
            dataset_seed=arm_two_provenance.dataset_seed,
            git_revision=arm_two_provenance.git_revision,
            runtime_identifier=arm_two_provenance.runtime_identifier,
            price_table_version=arm_two_provenance.price_table_version,
            answer_style_sha256=arm_two_provenance.answer_style_sha256,
            prompt_bundle_sha256=arm_two_provenance.prompt_bundle_sha256,
        ),
        model_metrics_fields=_sample_model_metrics_fields(),
        aggregate_score_fields=_sample_aggregate_score_fields(),
        task_score_fields=(),
    )
    path_one = archive_raw_run(arm_one, tmp_path)
    path_two = archive_raw_run(arm_two, tmp_path)
    assert path_one != path_two
    assert path_one.exists()
    assert path_two.exists()


# ---------------------------------------------------------------------------------------------
# CSV shape — flat per-task rows
# ---------------------------------------------------------------------------------------------


def test_csv_has_one_row_per_task_across_all_arms() -> None:
    matrix = _sample_matrix()
    rows = list(csv.DictReader(io.StringIO(render_csv(matrix))))
    assert len(rows) == 2
    assert {row["task_id"] for row in rows} == {
        "gt-01-investigation-x",
        "gt-04-enrichment-y",
    }


def test_csv_row_carries_model_id_and_timestamp_for_self_description() -> None:
    matrix = _sample_matrix()
    rows = list(csv.DictReader(io.StringIO(render_csv(matrix))))
    assert rows[0]["model_id"] == "us.anthropic.claude-opus-5-v1:0"
    assert rows[0]["timestamp"] == "2026-01-01T00:00:00+00:00"


def test_csv_renders_unscored_prose_task_reason_without_crashing() -> None:
    prose_task: dict[str, object] = {
        "task_id": "gt-04-enrichment-prose",
        "scoring_mode": "prose_unscored",
        "correctness": None,
        "unscored_reason": "no judge model; free-text reasoning has no committed-state signature",
        "bad_write": False,
        "tool_call_correct": None,
        "assertion_results": [],
    }
    arm = ArmResult(
        provenance=_sample_provenance(),
        model_metrics_fields=_sample_model_metrics_fields(),
        aggregate_score_fields=_sample_aggregate_score_fields(),
        task_score_fields=(prose_task,),
    )
    matrix = build_matrix_result([arm])
    rows = list(csv.DictReader(io.StringIO(render_csv(matrix))))
    assert rows[0]["correctness"] == ""
    assert "no judge model" in rows[0]["unscored_reason"]


# ---------------------------------------------------------------------------------------------
# write_matrix_artifacts / archive_raw_run wiring
# ---------------------------------------------------------------------------------------------


def test_write_matrix_artifacts_creates_output_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "results"
    write_matrix_artifacts(_sample_matrix(), target)
    assert (target / "matrix.json").exists()
    assert (target / "matrix.md").exists()
    assert (target / "matrix.csv").exists()


def test_write_matrix_artifacts_returns_the_three_paths(tmp_path: Path) -> None:
    paths = write_matrix_artifacts(_sample_matrix(), tmp_path)
    assert set(paths) == {"json", "md", "csv"}
    for path in paths.values():
        assert path.exists()


def test_archive_raw_run_creates_raw_directory(tmp_path: Path) -> None:
    raw_dir = tmp_path / "results" / "raw"
    assert not raw_dir.exists()
    archive_raw_run(_sample_arm(), raw_dir)
    assert raw_dir.exists()


def test_json_matches_markdown_and_csv_on_shared_fields() -> None:
    matrix = _sample_matrix(withheld=False)
    json_rendered = json.loads(render_json(matrix))
    md_rendered = render_markdown(matrix)
    csv_rows = list(csv.DictReader(io.StringIO(render_csv(matrix))))

    json_task_ids = {task["task_id"] for task in json_rendered["arms"][0]["task_scores"]}
    csv_task_ids = {row["task_id"] for row in csv_rows}
    assert json_task_ids == csv_task_ids
    for task_id in json_task_ids:
        assert task_id in md_rendered


# ---------------------------------------------------------------------------------------------
# Stub-mode labelling surfaces: the arm heading, the disclosure block, and the CSV `mode` column,
# each asserted present and asserted to fail on a copy of the rendered output with it stripped.
# ---------------------------------------------------------------------------------------------


def test_arm_heading_names_the_run_mode() -> None:
    """The arm heading in `matrix.md` states the run's mode (`## {model_id} ({mode}-mode run)`)."""
    markdown = render_markdown(_sample_matrix())
    assert "## us.anthropic.claude-opus-5-v1:0 (stub-mode run)" in markdown

    mutated_without_mode_suffix = markdown.replace("(stub-mode run)", "")
    assert "## us.anthropic.claude-opus-5-v1:0 (stub-mode run)" not in mutated_without_mode_suffix


def test_arm_heading_names_live_mode_when_provenance_is_live() -> None:
    """A live-mode arm's heading says "live-mode run", derived from `RunProvenance.mode`."""
    live_provenance = _sample_provenance()
    live_arm = ArmResult(
        provenance=RunProvenance(
            mode="live",
            timestamp=live_provenance.timestamp,
            model_id=live_provenance.model_id,
            effort=live_provenance.effort,
            dataset_seed=live_provenance.dataset_seed,
            git_revision=live_provenance.git_revision,
            runtime_identifier=live_provenance.runtime_identifier,
            price_table_version=live_provenance.price_table_version,
            answer_style_sha256=live_provenance.answer_style_sha256,
            prompt_bundle_sha256=live_provenance.prompt_bundle_sha256,
        ),
        model_metrics_fields=_sample_model_metrics_fields(),
        aggregate_score_fields=_sample_aggregate_score_fields(),
        task_score_fields=(_sample_task_score_fields(),),
    )
    markdown = render_markdown(build_matrix_result([live_arm]))
    assert "(live-mode run)" in markdown
    assert "(stub-mode run)" not in markdown


def test_stub_mode_disclosure_precedes_the_aggregate_score_table() -> None:
    """A stub-mode arm carries the stub disclosure sentence immediately above its Aggregate Score
    table; a live-mode arm carries none."""
    markdown = render_markdown(_sample_matrix())
    disclosure = (
        "This is a stub-mode measurement: no live model was invoked. The figures below "
        "reflect the deterministic stub provider, not a validated live-model comparison."
    )
    assert disclosure in markdown

    aggregate_heading_index = markdown.index("### Aggregate Score")
    disclosure_index = markdown.index(disclosure)
    assert aggregate_heading_index < disclosure_index

    mutated_without_disclosure = markdown.replace(disclosure, "")
    assert disclosure not in mutated_without_disclosure

    live_provenance = _sample_provenance()
    live_arm = ArmResult(
        provenance=RunProvenance(
            mode="live",
            timestamp=live_provenance.timestamp,
            model_id=live_provenance.model_id,
            effort=live_provenance.effort,
            dataset_seed=live_provenance.dataset_seed,
            git_revision=live_provenance.git_revision,
            runtime_identifier=live_provenance.runtime_identifier,
            price_table_version=live_provenance.price_table_version,
            answer_style_sha256=live_provenance.answer_style_sha256,
            prompt_bundle_sha256=live_provenance.prompt_bundle_sha256,
        ),
        model_metrics_fields=_sample_model_metrics_fields(),
        aggregate_score_fields=_sample_aggregate_score_fields(),
        task_score_fields=(_sample_task_score_fields(),),
    )
    live_markdown = render_markdown(build_matrix_result([live_arm]))
    assert disclosure not in live_markdown


def test_csv_mode_column_carries_the_run_mode_per_row() -> None:
    """Every row of `matrix.csv` carries the arm's run mode in a `mode` column."""
    csv_text = render_csv(_sample_matrix())
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert rows, "expected at least one CSV row"
    assert "mode" in rows[0]
    for row in rows:
        assert row["mode"] == "stub"

    reader = csv.reader(io.StringIO(csv_text))
    header = next(reader)
    mode_index = header.index("mode")
    mutated_header = header[:mode_index] + header[mode_index + 1 :]
    mutated_rows = [
        dict(
            zip(
                mutated_header,
                row[:mode_index] + row[mode_index + 1 :],
                strict=True,
            )
        )
        for row in reader
    ]
    assert "mode" not in mutated_header
    for mutated_row in mutated_rows:
        assert "mode" not in mutated_row


# ---------------------------------------------------------------------------------------------
# Figure-boundary note: what `mean_correctness`, `bad_write_rate`, and `tool_call_correct_rate`
# do and do not support, generated by the renderer.
# ---------------------------------------------------------------------------------------------


def _must_not_change_result(*, name: str, unscored: bool) -> dict[str, object]:
    return {
        "name": name,
        "passed": True,
        "detail": "no pre-turn baseline was captured" if unscored else "",
        "unscored": unscored,
    }


def _arm_with_must_not_change_assertions(
    results: list[dict[str, object]],
) -> ArmResult:
    task_fields = _sample_task_score_fields("gt-11-mutation-z")
    task_fields["assertion_results"] = [
        {"name": "expected_field_changes[0]", "passed": True, "unscored": False},
        *results,
    ]
    return ArmResult(
        provenance=_sample_provenance(),
        model_metrics_fields=_sample_model_metrics_fields(),
        aggregate_score_fields=_sample_aggregate_score_fields(),
        task_score_fields=(task_fields,),
    )


def test_rendered_markdown_carries_the_figure_boundary_note() -> None:
    """Every rendered `matrix.md` heads its arms with the per-figure boundary section."""
    markdown = render_markdown(_sample_matrix())
    assert "## Reading these figures" in markdown
    assert "Not a measure of end-to-end task success" in markdown
    assert "does not report whether the turn chose the most apt tool for the task" in markdown
    assert "src/eval/matrix.py" in markdown
    assert "review-e2e" not in markdown, (
        "an internal review coordinate is being published into the artifact again"
    )

    section_index = markdown.index("## Reading these figures")
    first_arm_index = markdown.index("## us.anthropic.claude-opus-5-v1:0")
    assert section_index < first_arm_index

    mutated = markdown.replace("## Reading these figures", "")
    assert "## Reading these figures" not in mutated


def test_rendered_markdown_note_carries_no_word_the_harness_verdict_scan_forbids() -> None:
    """The rendered note passes `eval.harness.assert_artifacts`'s verdict-word scan, which still
    catches an injected forbidden word."""
    from eval import harness

    markdown = render_markdown(_sample_matrix())
    assert harness._rendered_verdict_words(markdown) == []

    injected = markdown.replace(
        "## Reading these figures", "## Reading these figures (the better arm wins)"
    )
    assert harness._rendered_verdict_words(injected) == ["better", "wins"]

    two_arms = build_matrix_result(
        [
            _narrowed_arm(model_id="arm-a", narrowed_correctness=2 / 3, mean=0.85),
            _narrowed_arm(model_id="arm-b", narrowed_correctness=2 / 3, mean=0.83),
        ]
    )
    two_arm_markdown = render_markdown(two_arms)
    assert "Retrieval-narrowing magnitude" in two_arm_markdown
    assert harness._rendered_verdict_words(two_arm_markdown) == []


def test_rendered_markdown_states_the_report_quality_boundary() -> None:
    """`matrix.md` states that the gt-04/gt-05 tasks score retrieval instead."""
    markdown = render_markdown(_sample_matrix())

    assert "report quality" in markdown
    assert "Not measured anywhere in this project" in markdown
    for task_id in matrix._RETRIEVAL_NARROWED_TASK_IDS:
        assert task_id in markdown
    # The boundary sits in the block ahead of the first arm.
    assert markdown.index("report quality") < markdown.index("## us.anthropic.claude-opus-5-v1:0")


def test_narrowed_task_ids_match_the_golden_task_file() -> None:
    """The rendered narrowed-task id list equals the set `golden_tasks.yaml`'s notes declare."""
    import yaml

    document = yaml.safe_load(
        (Path(matrix.__file__).resolve().parent / "golden_tasks.yaml").read_text(encoding="utf-8")
    )
    tasks = document["tasks"] if isinstance(document, dict) else document
    derived = {
        str(task["id"])
        for task in tasks
        if all(
            marker in str(task.get("notes") or "").lower()
            for marker in matrix._NARROWED_TASK_NOTE_MARKERS
        )
    }

    assert derived == set(matrix._RETRIEVAL_NARROWED_TASK_IDS)


def test_bad_write_rate_basis_states_a_measurement_when_every_baseline_was_captured() -> None:
    arm = _arm_with_must_not_change_assertions(
        [
            _must_not_change_result(name="must_not_change[contacts.cont-1.email]", unscored=False),
            _must_not_change_result(name="must_not_change[contacts.cont-2.email]", unscored=False),
        ]
    )
    basis = bad_write_rate_basis(arm)
    assert basis["must_not_change_assertions_declared"] == 2
    assert basis["must_not_change_assertions_with_pre_turn_baseline"] == 2
    assert "all 2 declared" in str(basis["note"])
    assert "vacuous" not in str(basis["note"])


def test_bad_write_rate_basis_states_vacuity_when_no_baseline_was_captured() -> None:
    """With `must_not_change` declared but no pre-turn baseline, the basis states vacuity."""
    arm = _arm_with_must_not_change_assertions(
        [
            _must_not_change_result(name="must_not_change[contacts.cont-1.email]", unscored=True),
            _must_not_change_result(name="must_not_change[contacts.cont-2.email]", unscored=True),
        ]
    )
    basis = bad_write_rate_basis(arm)
    assert basis["must_not_change_assertions_declared"] == 2
    assert basis["must_not_change_assertions_with_pre_turn_baseline"] == 0
    assert "none of the 2 declared" in str(basis["note"])
    assert "vacuous" in str(basis["note"])


def test_bad_write_rate_basis_states_partial_coverage_when_only_some_baselines_were_captured() -> (
    None
):
    arm = _arm_with_must_not_change_assertions(
        [
            _must_not_change_result(name="must_not_change[contacts.cont-1.email]", unscored=False),
            _must_not_change_result(name="must_not_change[contacts.cont-2.email]", unscored=True),
        ]
    )
    basis = bad_write_rate_basis(arm)
    assert basis["must_not_change_assertions_with_pre_turn_baseline"] == 1
    assert "1 of 2 declared" in str(basis["note"])
    assert "the other 1 are reported unscored" in str(basis["note"])


def test_bad_write_rate_basis_states_no_signal_when_no_must_not_change_assertion_was_declared() -> (
    None
):
    basis = bad_write_rate_basis(_sample_arm())
    assert basis["must_not_change_assertions_declared"] == 0
    assert "carries no signal" in str(basis["note"])


def test_bad_write_rate_basis_is_rendered_beneath_each_arms_aggregate_table() -> None:
    """Each arm's own `bad_write_rate` basis appears after its Aggregate Score table."""
    vacuous_arm = _arm_with_must_not_change_assertions(
        [_must_not_change_result(name="must_not_change[contacts.cont-1.email]", unscored=True)]
    )
    measured_arm = _arm_with_must_not_change_assertions(
        [_must_not_change_result(name="must_not_change[contacts.cont-1.email]", unscored=False)]
    )
    vacuous_markdown = render_markdown(build_matrix_result([vacuous_arm]))
    measured_markdown = render_markdown(build_matrix_result([measured_arm]))

    assert "vacuous" in vacuous_markdown
    assert "vacuous" not in measured_markdown
    assert "all 1 declared" in measured_markdown

    table_index = vacuous_markdown.index("| bad_write_rate |")
    note_index = vacuous_markdown.index("`bad_write_rate` basis:")
    assert table_index < note_index


def test_rendered_json_carries_the_figure_semantics_block_additively() -> None:
    """`matrix.json` carries the figure-semantics block and leaves `arms` unchanged."""
    matrix = _sample_matrix()
    payload = json.loads(render_json(matrix))
    assert list(payload) == ["tier_comparison", "arms", "figure_semantics"]
    assert len(payload["arms"]) == 1
    semantics = payload["figure_semantics"]
    assert semantics["source"] == "src/eval/matrix.py"
    assert set(semantics) == {
        "source",
        "mean_correctness",
        "bad_write_rate",
        "tool_call_correct_rate",
        "report_quality",
        "retrieval_narrowed_task_ids",
        "retrieval_narrowing_conservatism",
        "retrieval_narrowing_magnitude",
        "effort",
        "token_accounting",
        "long_context_invocation_count",
        "arm_totals",
        "per_arm_bad_write_rate_basis",
        "per_arm_correctness_composition",
        "per_arm_retrieval_conservatism",
    }
    assert [entry["model_id"] for entry in semantics["per_arm_bad_write_rate_basis"]] == [
        "us.anthropic.claude-opus-5-v1:0"
    ]
    assert [entry["model_id"] for entry in semantics["per_arm_correctness_composition"]] == [
        "us.anthropic.claude-opus-5-v1:0"
    ]


def test_csv_carries_no_prose_note_only_per_task_scalars() -> None:
    """`matrix.csv` carries no prose note: a header line, then one line of scalars per task."""
    csv_text = render_csv(_sample_matrix())
    assert "Reading these figures" not in csv_text
    assert "bad_write_rate` basis" not in csv_text
    assert csv_text.splitlines()[0].startswith("model_id,mode,timestamp,")


def test_figure_semantics_note_declares_no_ranking_and_no_currency_figure() -> None:
    """The note states no ranking between arms and no absolute currency figure."""
    per_arm_keys = ("per_arm_bad_write_rate_basis", "per_arm_correctness_composition")
    matrix = build_matrix_result([_sample_arm(), _sample_arm()])
    markdown = render_markdown(matrix)
    semantics = render_figure_semantics(matrix)
    per_arm_notes: list[str] = []
    for key in per_arm_keys:
        entries = semantics[key]
        assert isinstance(entries, list)
        per_arm_notes.extend(str(entry["note"]) for entry in entries)
    note_text = " ".join(
        [
            markdown[: markdown.index("## us.anthropic")],
            *(str(value) for key, value in semantics.items() if key not in per_arm_keys),
            *per_arm_notes,
        ]
    )
    assert _find_verdict_vocabulary(note_text) == []
    assert not re.search(r"[$£€]\s*\d", note_text)
    for ranking_word in ("outperform", "ahead of", "leads", "ranks", "superior"):
        assert ranking_word not in note_text.lower()


# ---------------------------------------------------------------------------------------------
# `mean_correctness` composition disclosure
# ---------------------------------------------------------------------------------------------


def _assertion(name: str, *, passed: bool, unscored: bool = False) -> dict[str, object]:
    return {"name": name, "passed": passed, "detail": "", "unscored": unscored}


def _arm_with_assertions(
    results: list[dict[str, object]], *, correctness: float | None = 0.5
) -> ArmResult:
    task_fields = _sample_task_score_fields("gt-07-mutation-q")
    task_fields["correctness"] = correctness
    task_fields["assertion_results"] = results
    return ArmResult(
        provenance=_sample_provenance(),
        model_metrics_fields=_sample_model_metrics_fields(),
        aggregate_score_fields=_sample_aggregate_score_fields(),
        task_score_fields=(task_fields,),
    )


def test_correctness_composition_counts_positive_assertions_apart_from_negative_guards() -> None:
    """The disclosure partitions the arm's records into positive assertions and negative guards,
    publishing the positive-assertion satisfaction rate."""
    arm = _arm_with_assertions(
        [
            _assertion("expected_field_changes[accounts.acct-1.website]", passed=False),
            _assertion("expected_reads[get_contact:cont-1]", passed=True),
            _assertion("must_not_change[contacts.cont-2.email]", passed=True),
            _assertion("forbidden_outcomes[wrong_contact_mutated]", passed=True),
            _assertion("max_proposal_count", passed=True),
            _assertion("tool_call_correctness", passed=False),
        ]
    )
    composition = correctness_composition(arm)
    assert composition["positive_assertions_scored"] == 2
    assert composition["positive_assertions_satisfied"] == 1
    assert composition["positive_assertion_satisfaction_rate"] == 0.5
    assert composition["negative_guard_assertions_scored"] == 4
    assert composition["negative_guard_assertions_satisfied"] == 3
    assert composition["negative_guard_satisfaction_rate"] == 0.75
    assert composition["negative_guard_share_of_assertion_mass"] == 4 / 6


def test_correctness_composition_classifies_every_positive_assertion_type() -> None:
    """Each positive assertion type `eval.scoring` emits classifies as positive, each
    negative-guard name as a guard."""
    arm = _arm_with_assertions(
        [
            _assertion("expected_field_changes[accounts.acct-1.website]", passed=True),
            _assertion("expected_proposal", passed=True),
            _assertion("expected_denial", passed=True),
            _assertion("expected_reads[get_contact|search_contacts:cont-1]", passed=True),
            _assertion("must_not_change[contacts.cont-2.email]", passed=True),
            _assertion("forbidden_outcomes[fabricated_contact_name]", passed=True),
            _assertion("allowed_proposal_fields[prop-1]", passed=True),
            _assertion("max_proposal_count", passed=True),
            _assertion("tool_call_correctness", passed=True),
        ]
    )
    composition = correctness_composition(arm)
    assert composition["positive_assertions_scored"] == 4
    assert composition["negative_guard_assertions_scored"] == 5


def test_correctness_composition_excludes_unscored_assertions_from_both_sides() -> None:
    """An `unscored` assertion record is excluded from both sides of the composition."""
    arm = _arm_with_assertions(
        [
            _assertion("expected_reads[get_contact:cont-1]", passed=True),
            _assertion("must_not_change[contacts.cont-2.email]", passed=True),
            _assertion("forbidden_outcomes[unhelpful_answer]", passed=True, unscored=True),
            _assertion("expected_proposal", passed=False, unscored=True),
        ]
    )
    composition = correctness_composition(arm)
    assert composition["positive_assertions_scored"] == 1
    assert composition["negative_guard_assertions_scored"] == 1
    assert composition["negative_guard_share_of_assertion_mass"] == 0.5


def test_correctness_composition_counts_only_tasks_the_arm_actually_scored() -> None:
    """Only tasks the arm scored contribute assertions to the composition."""
    scored = _sample_task_score_fields("gt-07-mutation-q")
    scored["correctness"] = 0.5
    scored["assertion_results"] = [
        _assertion("expected_reads[get_contact:cont-1]", passed=True),
        _assertion("must_not_change[contacts.cont-2.email]", passed=True),
    ]
    unscored = _sample_task_score_fields("gt-04-report-y")
    unscored["correctness"] = None
    unscored["assertion_results"] = [
        _assertion("must_not_change[contacts.cont-9.email]", passed=True),
        _assertion("max_proposal_count", passed=True),
        _assertion("tool_call_correctness", passed=True),
    ]
    arm = ArmResult(
        provenance=_sample_provenance(),
        model_metrics_fields=_sample_model_metrics_fields(),
        aggregate_score_fields=_sample_aggregate_score_fields(),
        task_score_fields=(scored, unscored),
    )
    composition = correctness_composition(arm)
    assert composition["positive_assertions_scored"] == 1
    assert composition["negative_guard_assertions_scored"] == 1


def test_correctness_composition_states_no_composition_when_nothing_was_scored() -> None:
    """With nothing scored, the composition states none rather than defaulting a rate."""
    arm = _arm_with_assertions([], correctness=None)
    composition = correctness_composition(arm)
    assert composition["positive_assertion_satisfaction_rate"] is None
    assert composition["negative_guard_share_of_assertion_mass"] is None
    assert "no composition to state" in str(composition["note"])


def test_correctness_composition_share_is_a_mean_over_tasks_matching_the_figure() -> None:
    """The guard share is a mean over scored tasks, (1/2 + 4/6)/2, not pooled (5/8)."""
    light = _sample_task_score_fields("gt-07-mutation-q")
    light["correctness"] = 0.5
    light["assertion_results"] = [
        _assertion("expected_proposal", passed=True),
        _assertion("max_proposal_count", passed=True),
    ]
    heavy = _sample_task_score_fields("gt-09-mutation-r")
    heavy["correctness"] = 0.5
    heavy["assertion_results"] = [
        _assertion("expected_field_changes[accounts.acct-1.website]", passed=True),
        _assertion("expected_reads[get_contact:cont-1]", passed=True),
        _assertion("must_not_change[contacts.cont-1.email]", passed=True),
        _assertion("must_not_change[contacts.cont-2.email]", passed=True),
        _assertion("forbidden_outcomes[wrong_contact_mutated]", passed=True),
        _assertion("tool_call_correctness", passed=True),
    ]
    arm = ArmResult(
        provenance=_sample_provenance(),
        model_metrics_fields=_sample_model_metrics_fields(),
        aggregate_score_fields=_sample_aggregate_score_fields(),
        task_score_fields=(light, heavy),
    )
    share = correctness_composition(arm)["negative_guard_share_of_assertion_mass"]
    assert share == pytest.approx((1 / 2 + 4 / 6) / 2)
    assert share != pytest.approx(5 / 8)


def test_correctness_composition_note_labels_the_share_as_a_mean_and_the_totals_as_pooled() -> None:
    """The note labels the share as a per-task mean (0.5833) and the totals as pooled (5/8)."""
    light = _sample_task_score_fields("gt-07-mutation-q")
    light["correctness"] = 0.5
    light["assertion_results"] = [
        _assertion("expected_proposal", passed=True),
        _assertion("max_proposal_count", passed=True),
    ]
    heavy = _sample_task_score_fields("gt-09-mutation-r")
    heavy["correctness"] = 0.5
    heavy["assertion_results"] = [
        _assertion("expected_field_changes[accounts.acct-1.website]", passed=True),
        _assertion("expected_reads[get_contact:cont-1]", passed=True),
        _assertion("must_not_change[contacts.cont-1.email]", passed=True),
        _assertion("must_not_change[contacts.cont-2.email]", passed=True),
        _assertion("forbidden_outcomes[wrong_contact_mutated]", passed=True),
        _assertion("tool_call_correctness", passed=True),
    ]
    arm = ArmResult(
        provenance=_sample_provenance(),
        model_metrics_fields=_sample_model_metrics_fields(),
        aggregate_score_fields=_sample_aggregate_score_fields(),
        task_score_fields=(light, heavy),
    )
    composition = correctness_composition(arm)
    note = str(composition["note"])

    assert "0.5833 of the assertion mass" in note
    assert "mean over the 2 scored tasks of each task's own guard share" in note
    assert "rather than the ratio pooled over assertions" in note
    assert "pooled totals, for context and not the basis of the figure just quoted" in note
    assert "0.6250" not in note, "the pooled share must not be published as the figure"
    assert composition["negative_guard_share_of_assertion_mass"] == pytest.approx(
        (1 / 2 + 4 / 6) / 2
    ), "the published figure must not move for a labelling fix"


def test_correctness_composition_is_rendered_beneath_each_arms_aggregate_table() -> None:
    """Each arm's own composition disclosure appears below its Aggregate Score table."""
    guard_heavy = _arm_with_assertions(
        [
            _assertion("expected_proposal", passed=False),
            _assertion("must_not_change[contacts.cont-1.email]", passed=True),
            _assertion("must_not_change[contacts.cont-2.email]", passed=True),
            _assertion("tool_call_correctness", passed=True),
        ]
    )
    positive_heavy = _arm_with_assertions(
        [
            _assertion("expected_proposal", passed=True),
            _assertion("expected_reads[get_contact:cont-1]", passed=True),
            _assertion("expected_denial", passed=True),
            _assertion("tool_call_correctness", passed=True),
        ]
    )
    guard_markdown = render_markdown(build_matrix_result([guard_heavy]))
    positive_markdown = render_markdown(build_matrix_result([positive_heavy]))

    assert "0.7500 of the assertion mass" in guard_markdown
    assert "0.2500 of the assertion mass" in positive_markdown
    assert "0 of 1 positive assertions were satisfied (0.0000)" in guard_markdown
    assert "3 of 3 positive assertions were satisfied (1.0000)" in positive_markdown

    table_index = guard_markdown.index("| mean_correctness |")
    note_index = guard_markdown.index("`mean_correctness` composition:")
    assert table_index < note_index


def test_correctness_composition_does_not_alter_any_published_figure() -> None:
    """`arms` in `matrix.json` keeps its aggregate score and carries no composition field."""
    matrix = _sample_matrix()
    payload = json.loads(render_json(matrix))
    arm_payload = payload["arms"][0]
    assert arm_payload["aggregate_score"] == _sample_aggregate_score_fields()
    assert "correctness_composition" not in arm_payload
    assert "correctness_composition" not in arm_payload["aggregate_score"]


# ---------------------------------------------------------------------------------------------
# Cross-family effort-label comparability
# ---------------------------------------------------------------------------------------------


def test_rendered_artifacts_qualify_the_pinned_effort_label() -> None:
    """The artifacts state the `effort` label is pinned for reproducibility and is not a
    cross-family equivalence claim, and name the file declaring both scales."""
    markdown = render_markdown(_sample_matrix())
    semantics = json.loads(render_json(_sample_matrix()))["figure_semantics"]
    for text in (markdown, str(semantics["effort"])):
        assert "not a claim that the arms were given equivalent reasoning" in text
        assert "config/model_capabilities.yaml" in text
        assert "effort_scale_comparability" in text


def test_rendered_artifacts_disclose_each_family_token_accounting() -> None:
    """Both artifacts publish the token-accounting boundary and every family's declaration."""
    markdown = render_markdown(_sample_matrix())
    semantics = json.loads(render_json(_sample_matrix()))["figure_semantics"]["token_accounting"]

    assert semantics["source"] == "src/crm/metering.py"
    assert [entry["family"] for entry in semantics["per_family"]] == sorted(TOKEN_ACCOUNTING)
    assert str(semantics["note"]) in markdown
    assert "not established to be like-for-like totals" in str(semantics["note"])
    assert "No token figure in this artifact is adjusted" in str(semantics["note"])
    for entry in semantics["per_family"]:
        declared = TOKEN_ACCOUNTING[cast(Family, entry["family"])]
        assert entry["cache_read_accounting"] == declared.cache_read_accounting
        assert entry["evidence_direction"] == declared.evidence_direction
        assert str(entry["evidence"]) in markdown
        assert str(entry["cache_read_usage_field"]) in markdown


# ---------------------------------------------------------------------------------------------
# Retrieval-narrowing disclosure
# ---------------------------------------------------------------------------------------------


def _narrowed_arm(*, model_id: str, narrowed_correctness: float, mean: float) -> ArmResult:
    """An arm scoring 20 tasks, one of them a retrieval-narrowed task at ``narrowed_correctness``."""
    tasks: list[dict[str, object]] = []
    for index in range(19):
        tasks.append(_sample_task_score_fields(f"gt-{index:02d}-other"))
    narrowed = _sample_task_score_fields("gt-04-investigation-turn-budget-partial")
    narrowed["correctness"] = narrowed_correctness
    tasks.append(narrowed)
    aggregate = _sample_aggregate_score_fields()
    aggregate["mean_correctness"] = mean
    return ArmResult(
        provenance=_sample_provenance(model_id=model_id),
        model_metrics_fields=_sample_model_metrics_fields(),
        aggregate_score_fields=aggregate,
        task_score_fields=tuple(tasks),
    )


def test_retrieval_conservatism_upper_bound_is_the_shortfall_over_the_scored_denominator() -> None:
    """The published deduction is (1 - task correctness) / scored tasks, from the arm's own records."""
    arm = _narrowed_arm(model_id="m", narrowed_correctness=2 / 3, mean=0.85)
    disclosure = matrix.retrieval_conservatism(arm)
    assert disclosure["narrowed_tasks_scored"] == 1
    assert float(str(disclosure["mean_correctness_deduction_upper_bound"])) == pytest.approx(
        (1 - 2 / 3) / 20
    )
    assert "0.016667" in str(disclosure["note"])
    assert "upper bound" in str(disclosure["note"])


def test_retrieval_conservatism_is_absent_rather_than_zero_when_no_narrowed_task_scored() -> None:
    disclosure = matrix.retrieval_conservatism(_sample_arm())
    assert disclosure["narrowed_tasks_scored"] == 0
    assert disclosure["per_task"] == []
    assert "scored none of the tasks narrowed to retrieval" in str(disclosure["note"])


def test_retrieval_narrowing_magnitude_compares_against_the_absolute_inter_arm_spread() -> None:
    """The deduction is compared with the absolute inter-arm spread, with no arm identified."""
    result = build_matrix_result(
        [
            _narrowed_arm(model_id="arm-a", narrowed_correctness=2 / 3, mean=0.8507936507936508),
            _narrowed_arm(model_id="arm-b", narrowed_correctness=2 / 3, mean=0.8369047619047618),
        ]
    )
    magnitude = matrix.retrieval_conservatism_magnitude(result)
    assert magnitude["deduction_exceeds_inter_arm_difference"] is True
    assert float(
        str(magnitude["largest_absolute_inter_arm_mean_correctness_difference"])
    ) == pytest.approx(0.0138888888, abs=1e-9)
    note = str(magnitude["note"])
    assert "larger than" in note
    assert "arm-a" not in note and "arm-b" not in note


def test_retrieval_narrowing_magnitude_is_none_rather_than_zero_for_a_single_arm() -> None:
    magnitude = matrix.retrieval_conservatism_magnitude(_sample_matrix())
    assert magnitude["largest_absolute_inter_arm_mean_correctness_difference"] is None
    assert magnitude["deduction_exceeds_inter_arm_difference"] is None
    assert "too few arms" in str(magnitude["note"])


def test_markdown_and_json_publish_the_same_retrieval_narrowing_disclosure() -> None:
    """Markdown and JSON publish the same retrieval-narrowing disclosure."""
    result = build_matrix_result(
        [
            _narrowed_arm(model_id="arm-a", narrowed_correctness=2 / 3, mean=0.85),
            _narrowed_arm(model_id="arm-b", narrowed_correctness=2 / 3, mean=0.83),
        ]
    )
    markdown = render_markdown(result)
    semantics = json.loads(render_json(result))["figure_semantics"]
    assert str(semantics["retrieval_narrowing_conservatism"]) in markdown
    assert str(semantics["retrieval_narrowing_magnitude"]["note"]) in markdown
    for entry in semantics["per_arm_retrieval_conservatism"]:
        assert str(entry["note"]) in markdown


def test_tool_call_correct_rate_note_states_expected_reads_is_only_necessary() -> None:
    """The note states `expected_reads` is only a necessary condition for correct tool choice."""
    semantics = render_figure_semantics(_sample_matrix())
    note = str(semantics["tool_call_correct_rate"])
    assert "never a sufficient one" in note
    assert "satisfied incidentally" in note


# ---------------------------------------------------------------------------------------------
# Re-rendering from results/raw/
# ---------------------------------------------------------------------------------------------


def test_arm_round_trips_through_its_raw_archive_fields() -> None:
    arm = _sample_arm()
    assert matrix.arm_from_report_fields(arm.to_report_fields()) == arm


def test_arm_from_report_fields_refuses_an_archive_missing_a_block() -> None:
    fields = _sample_arm().to_report_fields()
    del fields["model_metrics"]
    with pytest.raises(ValueError, match="model_metrics"):
        matrix.arm_from_report_fields(fields)


def test_provenance_records_the_answer_style_prompt_hash() -> None:
    """The answer-style prompt hash is a provenance field, rendered and pinned."""
    fields = _sample_provenance().to_report_fields()
    assert fields["answer_style_sha256"] == "0" * 64
    assert "answer_style_sha256" in matrix._PROVENANCE_FIELDS


def test_provenance_records_the_prompt_bundle_hash_after_the_answer_style_hash() -> None:
    """The prompt-bundle hash is a provenance field, emitted after the answer-style hash."""
    fields = _sample_provenance().to_report_fields()
    assert fields["prompt_bundle_sha256"] == "1" * 64
    names = list(fields)
    assert names.index("prompt_bundle_sha256") == names.index("answer_style_sha256") + 1
    assert list(matrix._PROVENANCE_FIELDS) == [name for name in names if name != "temperature"]


def test_an_archive_without_the_prompt_bundle_hash_reads_none_and_renders_a_dash() -> None:
    fields = _sample_arm().to_report_fields()
    del cast(dict[str, object], fields["provenance"])["prompt_bundle_sha256"]
    arm = matrix.arm_from_report_fields(fields)
    assert arm.provenance.prompt_bundle_sha256 is None
    assert "| prompt_bundle_sha256 | — |" in render_markdown(build_matrix_result([arm]))


def test_arm_from_report_fields_refuses_an_archive_missing_the_answer_style_hash() -> None:
    """A raw archive with no answer-style hash is refused, never defaulted."""
    fields = _sample_arm().to_report_fields()
    provenance = cast(dict[str, object], fields["provenance"])
    del provenance["answer_style_sha256"]
    with pytest.raises(ValueError, match="answer_style_sha256"):
        matrix.arm_from_report_fields(fields)


def test_re_render_from_raw_reproduces_every_artifact(tmp_path: Path) -> None:
    """A sweep's artifacts are reproduced from its raw archives alone."""
    raw_dir = tmp_path / "raw"
    arm = _sample_arm()
    archive_raw_run(arm, raw_dir)
    original = write_matrix_artifacts(build_matrix_result([arm]), tmp_path / "original")
    assert matrix.main(["--raw-dir", str(raw_dir), "--output-dir", str(tmp_path / "again")]) == 0
    for name in ("json", "md", "csv"):
        assert (tmp_path / "again" / f"matrix.{name}").read_bytes() == original[name].read_bytes()


def test_load_raw_archives_selects_one_sweep_by_git_revision(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    published = _sample_arm()
    superseded = ArmResult(
        provenance=RunProvenance(
            mode="stub",
            timestamp="2025-12-31T00:00:00+00:00",
            model_id=published.provenance.model_id,
            effort="high",
            dataset_seed=20260101,
            git_revision="deadbee",
            runtime_identifier="local-stub-runtime",
            price_table_version="v0-provisional",
            answer_style_sha256="0" * 64,
        ),
        model_metrics_fields=_sample_model_metrics_fields(),
        aggregate_score_fields=_sample_aggregate_score_fields(),
        task_score_fields=published.task_score_fields,
    )
    archive_raw_run(published, raw_dir)
    archive_raw_run(superseded, raw_dir)
    selected = matrix.load_raw_archives(raw_dir, git_revision="abc1234")
    assert [arm.provenance.git_revision for arm in selected] == ["abc1234"]
    with pytest.raises(ValueError, match="two archives for model"):
        matrix.load_raw_archives(raw_dir)


def test_load_raw_archives_refuses_an_empty_selection(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    archive_raw_run(_sample_arm(), raw_dir)
    with pytest.raises(ValueError, match="no raw archive"):
        matrix.load_raw_archives(raw_dir, git_revision="0000000")
    assert matrix.main(["--raw-dir", str(raw_dir), "--git-revision", "0000000"]) == 2


# ---------------------------------------------------------------------------------------------
# P50/P95 latency rendering: task percentiles and per-tier call latency reach every artifact; a
# not-measured arm renders nulls.
# ---------------------------------------------------------------------------------------------


def _measured_latency_fields() -> dict[str, object]:
    return {
        "latency_measured": True,
        "latency_percentile_method": "nearest-rank",
        "task_latency_ms_p50": 120.5,
        "task_latency_ms_p95": 340.0,
        "call_latency_ms_by_tier": {
            "supervisor": {"p50": 90.0, "p95": 300.0, "count": 20},
            "subagent": {"p50": None, "p95": None, "count": 0},
            "tool": {"p50": None, "p95": None, "count": 0},
        },
    }


def _not_measured_latency_fields() -> dict[str, object]:
    return {
        "latency_measured": False,
        "latency_percentile_method": "nearest-rank",
        "task_latency_ms_p50": None,
        "task_latency_ms_p95": None,
        "call_latency_ms_by_tier": {
            "supervisor": {"p50": None, "p95": None, "count": 0},
            "subagent": {"p50": None, "p95": None, "count": 0},
            "tool": {"p50": None, "p95": None, "count": 0},
        },
    }


def _arm_with_latency(latency_fields: dict[str, object]) -> ArmResult:
    metrics = _sample_model_metrics_fields()
    metrics.update(latency_fields)
    return ArmResult(
        provenance=_sample_provenance(),
        model_metrics_fields=metrics,
        aggregate_score_fields=_sample_aggregate_score_fields(),
        task_score_fields=(_sample_task_score_fields("gt-01-x"),),
    )


def test_json_carries_latency_fields_verbatim_from_model_metrics() -> None:
    matrix_result = build_matrix_result([_arm_with_latency(_measured_latency_fields())])
    metrics = json.loads(render_json(matrix_result))["arms"][0]["model_metrics"]
    assert metrics["latency_measured"] is True
    assert metrics["task_latency_ms_p50"] == 120.5
    assert metrics["call_latency_ms_by_tier"]["supervisor"] == {
        "p50": 90.0,
        "p95": 300.0,
        "count": 20,
    }


def test_markdown_renders_task_percentiles_and_the_per_tier_call_table() -> None:
    markdown = render_markdown(build_matrix_result([_arm_with_latency(_measured_latency_fields())]))
    assert "latency_measured" in markdown
    assert "task_latency_ms_p50" in markdown
    assert "120.500000" in markdown  # task p50 rendered via _format_scalar
    assert "Call latency by tier (ms):" in markdown
    assert "| tier | p50 | p95 | count |" in markdown
    assert "| supervisor | 90.000000 | 300.000000 | 20 |" in markdown
    # A tier with no sample still renders, with em-dash figures at count 0.
    assert "| tool | — | — | 0 |" in markdown


def test_markdown_per_tier_table_is_absent_for_a_fixture_without_latency() -> None:
    """An arm whose model_metrics carry no latency renders no per-tier table."""
    markdown = render_markdown(_sample_matrix())
    assert "Call latency by tier (ms):" not in markdown


def test_csv_carries_per_arm_latency_columns_repeated_per_task_row() -> None:
    matrix_result = build_matrix_result([_arm_with_latency(_measured_latency_fields())])
    rows = list(csv.DictReader(io.StringIO(render_csv(matrix_result))))
    assert rows[0]["latency_measured"] == "yes"
    assert rows[0]["task_latency_ms_p50"] == "120.500000"
    assert rows[0]["call_latency_ms_supervisor_p50"] == "90.000000"
    assert rows[0]["call_latency_ms_supervisor_count"] == "20"
    # A tier with no sample leaves its percentile cells empty rather than a fabricated zero.
    assert rows[0]["call_latency_ms_tool_p50"] == ""
    assert rows[0]["call_latency_ms_tool_count"] == "0"
    # Header still starts with the pinned per-task prefix the CSV consumers rely on.
    assert render_csv(matrix_result).splitlines()[0].startswith("model_id,mode,timestamp,")


def test_not_measured_latency_renders_as_null_in_every_artifact() -> None:
    matrix_result = build_matrix_result([_arm_with_latency(_not_measured_latency_fields())])
    metrics = json.loads(render_json(matrix_result))["arms"][0]["model_metrics"]
    assert metrics["latency_measured"] is False
    assert metrics["task_latency_ms_p50"] is None

    markdown = render_markdown(matrix_result)
    assert "| latency_measured | no |" in markdown
    assert "| task_latency_ms_p50 | — |" in markdown

    rows = list(csv.DictReader(io.StringIO(render_csv(matrix_result))))
    assert rows[0]["latency_measured"] == "no"
    assert rows[0]["task_latency_ms_p50"] == ""


def test_latency_rendering_stays_byte_identical_for_identical_input() -> None:
    """Latency introduces no nondeterminism into any renderer."""
    a = build_matrix_result([_arm_with_latency(_measured_latency_fields())])
    b = build_matrix_result([_arm_with_latency(_measured_latency_fields())])
    assert render_json(a) == render_json(b)
    assert render_markdown(a) == render_markdown(b)
    assert render_csv(a) == render_csv(b)


def test_latency_columns_carry_no_verdict_vocabulary() -> None:
    matrix_result = build_matrix_result([_arm_with_latency(_measured_latency_fields())])
    for rendered in (
        render_json(matrix_result),
        render_markdown(matrix_result),
        render_csv(matrix_result),
    ):
        assert _find_verdict_vocabulary(rendered) == []


# ---------------------------------------------------------------------------------------------
# cost_excluding_cache (= input + output), rendered beside cost_cache_total; absent while cost is
# withheld.
# ---------------------------------------------------------------------------------------------


def test_cost_excluding_cache_renders_in_every_artifact_when_not_withheld() -> None:
    matrix_result = _sample_matrix(withheld=False)
    metrics = json.loads(render_json(matrix_result))["arms"][0]["model_metrics"]
    assert metrics["cost_excluding_cache"] == pytest.approx(0.03)

    markdown = render_markdown(matrix_result)
    assert "cost_excluding_cache" in markdown
    assert "0.030000" in markdown

    rows = list(csv.DictReader(io.StringIO(render_csv(matrix_result))))
    assert rows[0]["cost_excluding_cache"] == "0.030000"


def test_cost_excluding_cache_is_absent_when_cost_is_withheld() -> None:
    matrix_result = _sample_matrix(withheld=True)
    metrics = json.loads(render_json(matrix_result))["arms"][0]["model_metrics"]
    assert "cost_excluding_cache" not in metrics
    assert "cost_excluding_cache" not in render_markdown(matrix_result)
    rows = list(csv.DictReader(io.StringIO(render_csv(matrix_result))))
    # The column header exists (fixed columns) but the withheld arm's cell is empty.
    assert rows[0]["cost_excluding_cache"] == ""


# ---------------------------------------------------------------------------------------------
# Per-(tier,role,model), per-tool, and per-task spread renderers + CSV + verdict scan
# ---------------------------------------------------------------------------------------------


def test_json_carries_the_three_new_per_arm_aggregations() -> None:
    """`matrix.json` publishes the per-arm aggregations verbatim under their report keys."""
    arm = json.loads(render_json(_sample_matrix_with_aggregations()))["arms"][0]

    assert [r["tier"] for r in arm["tier_role_model_metrics"]] == ["supervisor", "mid"]
    assert [r["tool_name"] for r in arm["tool_metrics"]] == [
        "get_account",
        "search_opportunities",
    ]
    assert [r["task_id"] for r in arm["task_spread"]] == [
        "gt-01-investigation-x",
        "gt-04-enrichment-y",
    ]
    # The outcome taxonomy survives the round-trip into JSON at its full width.
    assert arm["tool_metrics"][0]["outcome_distribution"] == {
        "data": 5,
        "no_match": 1,
        "denied": 0,
        "tool_failure": 0,
    }


def test_json_omits_the_new_aggregations_when_the_arm_carries_none() -> None:
    """An arm without aggregations renders empty lists, not missing keys."""
    arm = json.loads(render_json(_sample_matrix()))["arms"][0]
    assert arm["tier_role_model_metrics"] == []
    assert arm["tool_metrics"] == []
    assert arm["task_spread"] == []


def test_markdown_renders_the_three_new_sub_tables() -> None:
    """Each aggregation renders a labelled Markdown sub-table naming its rows."""
    markdown = render_markdown(_sample_matrix_with_aggregations())

    assert "Per tier / role / model:" in markdown
    assert "Per tool:" in markdown
    assert "Per-task correctness spread across repeats:" in markdown
    for token in ("supervisor", "get_account", "search_opportunities", "success_fraction"):
        assert token in markdown
    # The per-tool outcome taxonomy is flattened into columns, present even at a zero count.
    for column in ("outcome_data", "outcome_no_match", "outcome_denied", "outcome_tool_failure"):
        assert column in markdown


def test_markdown_renders_em_dash_for_a_genuinely_null_latency() -> None:
    """The subagent row's null P50/P95 render as `—`, not `None` or `0`."""
    markdown = render_markdown(_sample_matrix_with_aggregations())
    # The subagent row has call_latency_measured False; its latency cells are em dashes.
    subagent_line = next(
        line for line in markdown.splitlines() if "| subagent |" in line and "sonnet" in line
    )
    assert "| — |" in subagent_line
    assert "None" not in subagent_line


def test_markdown_new_sub_tables_are_absent_for_a_pre_b4_4_arm() -> None:
    """An arm without the aggregations emits no sub-table labels."""
    markdown = render_markdown(_sample_matrix())
    assert "Per tier / role / model:" not in markdown
    assert "Per tool:" not in markdown
    assert "Per-task correctness spread across repeats:" not in markdown


def test_csv_carries_spread_columns_joined_by_task_id() -> None:
    """Each per-task CSV row carries the `TaskSpread` columns for its own task id."""
    rows = list(csv.DictReader(io.StringIO(render_csv(_sample_matrix_with_aggregations()))))
    by_task = {row["task_id"]: row for row in rows}

    assert by_task["gt-01-investigation-x"]["repeats"] == "3"
    assert by_task["gt-01-investigation-x"]["correctness_min"] == "0.500000"
    assert by_task["gt-04-enrichment-y"]["correctness_min"] == "1.000000"
    # The spread cells are populated per-task, never cross-contaminated between task rows.
    assert (
        by_task["gt-01-investigation-x"]["success_fraction"]
        != by_task["gt-04-enrichment-y"]["success_fraction"]
    )


def test_csv_does_not_carry_the_per_tier_or_per_tool_aggregations() -> None:
    """Only spread goes to CSV; per-(tier,role,model) and per-tool are json+md only."""
    reader = csv.DictReader(io.StringIO(render_csv(_sample_matrix_with_aggregations())))
    header = list(reader.fieldnames or [])
    for column in ("tier", "tool_name", "outcome_data", "truncated_count"):
        assert column not in header


def test_csv_spread_cells_are_blank_for_a_pre_b4_4_arm() -> None:
    """The spread columns exist as fixed headers; an arm without spread leaves the cells empty."""
    rows = list(csv.DictReader(io.StringIO(render_csv(_sample_matrix()))))
    assert "repeats" in rows[0]
    assert rows[0]["repeats"] == ""


def test_new_renderers_are_covered_by_the_real_harness_verdict_scan() -> None:
    """`eval.harness.assert_artifacts`'s verdict scan catches a forbidden word injected into an
    aggregation field value rendered into `matrix.md`."""
    from eval import harness

    clean = render_markdown(_sample_matrix_with_aggregations())
    assert harness._rendered_verdict_words(clean) == []

    poisoned_arm = _sample_arm_with_aggregations()
    poisoned_tools = [dict(row) for row in _sample_tool_fields()]
    poisoned_tools[0]["tool_name"] = "recommend_tool"
    poisoned = ArmResult(
        provenance=poisoned_arm.provenance,
        model_metrics_fields=poisoned_arm.model_metrics_fields,
        aggregate_score_fields=poisoned_arm.aggregate_score_fields,
        task_score_fields=poisoned_arm.task_score_fields,
        tier_role_model_fields=poisoned_arm.tier_role_model_fields,
        tool_fields=tuple(poisoned_tools),
        task_spread_fields=poisoned_arm.task_spread_fields,
    )
    poisoned_markdown = render_markdown(build_matrix_result([poisoned]))
    assert harness._rendered_verdict_words(poisoned_markdown) == ["recommend"]


def test_real_assert_artifacts_accepts_a_full_artifact_dir_with_the_new_fields(
    tmp_path: Path,
) -> None:
    """`eval.harness.assert_artifacts` passes a full artifact set whose arms carry all three
    aggregations, and fails when a renderer emits a verdict word.

    The directory is built without a sweep: `write_matrix_artifacts`, `archive_raw_run` per arm,
    and a hand-written delta inventory with both required checks at `pass`.
    """
    from eval import harness

    def _publish(matrix_result: MatrixResult) -> tuple[Path, ...]:
        write_matrix_artifacts(matrix_result, tmp_path)
        raw_dir = tmp_path / "raw"
        raw_paths = tuple(archive_raw_run(arm, raw_dir) for arm in matrix_result.arms)
        inventory = tmp_path / "verification-inventory"
        inventory.mkdir(parents=True, exist_ok=True)
        (inventory / "eval_sweep.json").write_text(
            json.dumps(
                {
                    "resource_class": "eval_sweep",
                    "checks": [
                        {"check": "demo_tenant_untouched", "status": "pass", "detail": ""},
                        {"check": "writes_only_under_smoke_tenant", "status": "pass", "detail": ""},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return raw_paths

    clean = _sample_matrix_with_aggregations()
    raw_paths = _publish(clean)
    harness.assert_artifacts(
        output_dir=tmp_path,
        expected_arm_count=len(clean.arms),
        expected_task_count=20,
        raw_archive_paths=raw_paths,
    )

    # A verdict word in an aggregation field value makes the same check reject the artifact.
    poisoned_tools = [dict(row) for row in _sample_tool_fields()]
    poisoned_tools[0]["tool_name"] = "recommend_this"
    base = _sample_arm_with_aggregations()
    poisoned = build_matrix_result(
        [
            ArmResult(
                provenance=base.provenance,
                model_metrics_fields=base.model_metrics_fields,
                aggregate_score_fields=base.aggregate_score_fields,
                task_score_fields=base.task_score_fields,
                tier_role_model_fields=base.tier_role_model_fields,
                tool_fields=tuple(poisoned_tools),
                task_spread_fields=base.task_spread_fields,
            )
        ]
    )
    poisoned_raw = _publish(poisoned)
    with pytest.raises(harness.ArtifactAssertionError):
        harness.assert_artifacts(
            output_dir=tmp_path,
            expected_arm_count=len(poisoned.arms),
            expected_task_count=2,
            raw_archive_paths=poisoned_raw,
        )


# ---------------------------------------------------------------------------------------------
# Temperature-era archives stay readable
# ---------------------------------------------------------------------------------------------

_LEGACY_ARTIFACT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "eval" / "legacy_matrix_artifact.json"
)


def _legacy_arm_fields() -> list[dict[str, object]]:
    payload = json.loads(_LEGACY_ARTIFACT.read_text(encoding="utf-8"))
    return cast(list[dict[str, object]], payload["arms"])


def test_a_new_provenance_carries_no_temperature_key_or_markdown_row() -> None:
    matrix_result = build_matrix_result([_sample_arm()])

    assert "temperature" not in _sample_provenance().to_report_fields()
    assert "temperature" not in json.loads(render_json(matrix_result))["arms"][0]["provenance"]
    assert "| temperature |" not in render_markdown(matrix_result)


def test_a_legacy_archive_re_renders_its_temperature_as_recorded() -> None:
    """A temperature-era arm, retired tier models included, keeps its provenance key for key."""
    for fields in _legacy_arm_fields():
        recorded = cast(dict[str, object], fields["provenance"])
        assert "temperature" in recorded
        arm = matrix.arm_from_report_fields(fields)
        assert arm.provenance.records_legacy_temperature
        assert "prompt_bundle_sha256" not in recorded
        assert arm.provenance.prompt_bundle_sha256 is None
        assert arm.provenance.to_report_fields() == {**recorded, "prompt_bundle_sha256": None}
        assert list(arm.provenance.to_report_fields()) == [*recorded, "prompt_bundle_sha256"]
        assert arm.tier_role_model_fields == tuple(
            cast(list[dict[str, object]], fields["tier_role_model_metrics"])
        )


def test_a_legacy_numeric_temperature_renders_in_json_and_markdown() -> None:
    fields = _legacy_arm_fields()[0]
    pinned = {**fields, "provenance": {**cast(dict[str, object], fields["provenance"])}}
    cast(dict[str, object], pinned["provenance"])["temperature"] = 0.2
    arm = matrix.arm_from_report_fields(pinned)
    matrix_result = build_matrix_result([arm])

    assert json.loads(render_json(matrix_result))["arms"][0]["provenance"]["temperature"] == 0.2
    assert "| temperature | 0.200000 |" in render_markdown(matrix_result)


def test_a_legacy_artifact_publishes_and_passes_assert_artifacts(tmp_path: Path) -> None:
    from eval.harness import FR93_INVENTORY_RELATIVE_PATH, assert_artifacts

    arms = [matrix.arm_from_report_fields(fields) for fields in _legacy_arm_fields()]
    write_matrix_artifacts(build_matrix_result(arms), tmp_path)
    raw_paths = tuple(archive_raw_run(arm, tmp_path / "raw") for arm in arms)
    inventory = tmp_path / FR93_INVENTORY_RELATIVE_PATH
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        json.dumps(
            {
                "resource_class": "eval_sweep",
                "layer": 6,
                "checks": [
                    {"check": "demo_tenant_untouched", "status": "pass", "detail": "0 rows"},
                    {"check": "writes_only_under_smoke_tenant", "status": "pass", "detail": "1"},
                ],
            }
        ),
        encoding="utf-8",
    )
    aggregate = cast(dict[str, int], _legacy_arm_fields()[0]["aggregate_score"])

    assert_artifacts(
        output_dir=tmp_path,
        expected_arm_count=len(arms),
        expected_task_count=aggregate["scored_task_count"] + aggregate["unscored_task_count"],
        raw_archive_paths=raw_paths,
    )
    assert render_markdown(build_matrix_result(arms)).count("| temperature | — |") == len(arms)


# ---------------------------------------------------------------------------------------------
# Usage completeness
# ---------------------------------------------------------------------------------------------


def _lower_bound_arm(*task_ids: str) -> ArmResult:
    arm = _sample_arm(withheld=False)
    return ArmResult(
        provenance=arm.provenance,
        model_metrics_fields=arm.model_metrics_fields,
        aggregate_score_fields=arm.aggregate_score_fields,
        task_score_fields=arm.task_score_fields,
        usage_lower_bound_task_ids=task_ids,
    )


def test_an_arm_with_an_unmetered_task_run_publishes_the_ids_and_a_lower_bound_note() -> None:
    """The JSON names the task runs; matrix.md says the arm's usage and cost are a lower bound."""
    arm = _lower_bound_arm("gt-04-enrichment-y")
    fields = arm.to_report_fields()
    assert fields[matrix.USAGE_LOWER_BOUND_TASK_IDS_KEY] == ["gt-04-enrichment-y"]
    markdown = render_markdown(build_matrix_result([arm]))
    note = matrix.usage_lower_bound_note(arm)
    assert note is not None
    assert note in markdown
    assert "lower bound" in note
    assert "gt-04-enrichment-y" in note
    assert _find_verdict_vocabulary(markdown) == []


def test_an_arm_whose_usage_is_complete_carries_neither_the_key_nor_the_note() -> None:
    """Without an unmetered run the report is unchanged: no key, no note."""
    arm = _sample_arm(withheld=False)
    assert matrix.USAGE_LOWER_BOUND_TASK_IDS_KEY not in arm.to_report_fields()
    assert matrix.usage_lower_bound_note(arm) is None
    assert "lower bound" not in render_markdown(build_matrix_result([arm]))


def test_the_lower_bound_ids_round_trip_through_a_raw_archive() -> None:
    """A re-rendered archive keeps the ids and so the note."""
    arm = _lower_bound_arm("gt-01-investigation-x", "gt-04-enrichment-y")
    rebuilt = matrix.arm_from_report_fields(arm.to_report_fields())
    assert rebuilt.usage_lower_bound_task_ids == ("gt-01-investigation-x", "gt-04-enrichment-y")


def test_a_legacy_archive_without_the_key_reads_as_complete_usage() -> None:
    """An archive written before the key existed re-renders with no lower-bound note."""
    fields = _sample_arm(withheld=False).to_report_fields()
    assert matrix.USAGE_LOWER_BOUND_TASK_IDS_KEY not in fields
    assert matrix.arm_from_report_fields(fields).usage_lower_bound_task_ids == ()


def test_a_malformed_lower_bound_list_is_refused_on_re_render() -> None:
    """The optional key is still strictly typed."""
    fields = _sample_arm(withheld=False).to_report_fields()
    fields[matrix.USAGE_LOWER_BOUND_TASK_IDS_KEY] = [1]
    with pytest.raises(ValueError, match=matrix.USAGE_LOWER_BOUND_TASK_IDS_KEY):
        matrix.arm_from_report_fields(fields)


def _csv_rows(matrix_result: MatrixResult) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(render_csv(matrix_result))))


def test_the_csv_flags_each_task_row_whose_usage_is_a_lower_bound() -> None:
    """matrix.csv carries the task run's lower-bound flag on its own row."""
    rows = _csv_rows(build_matrix_result([_lower_bound_arm("gt-04-enrichment-y")]))
    by_task = {row["task_id"]: row["usage_lower_bound"] for row in rows}
    assert by_task == {"gt-01-investigation-x": "no", "gt-04-enrichment-y": "yes"}


def test_the_csv_lower_bound_column_reads_no_for_a_complete_or_legacy_arm() -> None:
    """An arm with complete usage, or an archive without the key, renders the column as no."""
    legacy = matrix.arm_from_report_fields(_sample_arm(withheld=False).to_report_fields())
    rows = _csv_rows(build_matrix_result([_sample_arm(withheld=False), legacy]))
    assert rows
    assert {row["usage_lower_bound"] for row in rows} == {"no"}


def test_matrix_md_states_the_errored_items_its_cost_excludes() -> None:
    """A run with items that ended in an error says its cost excludes them."""
    rendered = render_markdown(build_matrix_result([_sample_arm()], errored_item_count=2))
    note = matrix.errored_items_cost_note(2)
    assert note is not None
    assert note in rendered
    assert "2 item(s)" in note


def test_a_run_without_errored_items_carries_no_exclusion_note() -> None:
    """With no errored item, matrix.md and matrix.json are what they were before the qualifier."""
    plain = build_matrix_result([_sample_arm()])
    assert matrix.errored_items_cost_note(0) is None
    assert "Cost excludes" not in render_markdown(plain)
    plain_payload = json.loads(render_json(plain))
    errored_payload = json.loads(
        render_json(build_matrix_result([_sample_arm()], errored_item_count=3))
    )
    assert matrix.errored_items_cost_note(3) in errored_payload.pop("tier_comparison")["notes"]
    plain_payload.pop("tier_comparison")
    assert plain_payload == errored_payload


def test_the_artifact_verdict_scan_finds_nothing_in_the_errored_items_note() -> None:
    """The exclusion note passes the scan `assert_artifacts` runs over matrix.md."""
    from eval.harness import _rendered_verdict_words

    rendered = render_markdown(build_matrix_result([_sample_arm()], errored_item_count=1))
    assert "Cost excludes 1 item(s)" in rendered
    assert _rendered_verdict_words(rendered) == []
    assert _find_verdict_vocabulary(rendered) == []


# ---------------------------------------------------------------------------------------------
# All-tier arm totals
# ---------------------------------------------------------------------------------------------


def _arm_with_totals(*, withheld: bool, cost: float | None = 0.25, count: int = 3) -> ArmResult:
    return dataclasses.replace(
        _sample_arm(withheld=withheld),
        arm_totals_fields={
            ARM_COST_TOTAL_KEY: None if withheld else cost,
            ARM_LONG_CONTEXT_INVOCATION_COUNT_KEY: count,
        },
    )


def test_the_arm_totals_follow_model_metrics_in_the_json() -> None:
    (arm,) = json.loads(render_json(build_matrix_result([_arm_with_totals(withheld=False)])))[
        "arms"
    ]
    keys = list(arm)
    assert keys[keys.index("model_metrics") + 1 : keys.index("model_metrics") + 3] == [
        ARM_COST_TOTAL_KEY,
        ARM_LONG_CONTEXT_INVOCATION_COUNT_KEY,
    ]
    assert arm[ARM_COST_TOTAL_KEY] == 0.25
    assert arm[ARM_LONG_CONTEXT_INVOCATION_COUNT_KEY] == 3


def test_a_withheld_arm_cost_total_is_null_in_json_and_absent_from_markdown_and_csv() -> None:
    matrix_result = build_matrix_result([_arm_with_totals(withheld=True)])
    (arm,) = json.loads(render_json(matrix_result))["arms"]
    assert arm[ARM_COST_TOTAL_KEY] is None
    markdown = render_markdown(matrix_result)
    assert "| arm_long_context_invocation_count | 3 |" in markdown
    assert "cost_total" not in markdown
    rows = list(csv.DictReader(io.StringIO(render_csv(matrix_result))))
    assert rows[0]["arm_cost"] == ""
    assert rows[0]["arm_long_context_invocation_count"] == "3"


def test_markdown_labels_model_metrics_supervisor_only_and_renders_the_arm_totals() -> None:
    markdown = render_markdown(build_matrix_result([_arm_with_totals(withheld=False)]))
    usage = markdown[markdown.index("### Usage and Cost") :]
    assert usage.splitlines()[2].startswith("Supervisor tier only:")
    assert "All tiers (every call the arm made):" in usage
    assert "| arm_cost_total | 0.250000 |" in usage
    assert "| arm_long_context_invocation_count | 3 |" in usage


def test_csv_carries_the_arm_totals_on_every_task_row() -> None:
    rows = list(
        csv.DictReader(
            io.StringIO(render_csv(build_matrix_result([_arm_with_totals(withheld=False)])))
        )
    )
    assert rows and all(row["arm_cost"] == "0.250000" for row in rows)
    assert all(row["arm_long_context_invocation_count"] == "3" for row in rows)


def test_an_older_record_without_arm_totals_emits_no_keys_and_renders_dashes() -> None:
    matrix_result = build_matrix_result([_sample_arm(withheld=False)])
    (arm,) = json.loads(render_json(matrix_result))["arms"]
    assert ARM_COST_TOTAL_KEY not in arm and ARM_LONG_CONTEXT_INVOCATION_COUNT_KEY not in arm
    assert "All tiers (every call the arm made):" not in render_markdown(matrix_result)
    rows = list(csv.DictReader(io.StringIO(render_csv(matrix_result))))
    assert rows[0]["arm_cost"] == "" and rows[0]["arm_long_context_invocation_count"] == ""


def test_the_arm_totals_round_trip_through_a_raw_archive() -> None:
    arm = _arm_with_totals(withheld=False)
    rebuilt = arm_from_report_fields(json.loads(json.dumps(arm.to_report_fields())))
    assert rebuilt.arm_totals_fields == arm.arm_totals_fields
    legacy = arm_from_report_fields(json.loads(json.dumps(_sample_arm().to_report_fields())))
    assert legacy.arm_totals_fields == {}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (ARM_LONG_CONTEXT_INVOCATION_COUNT_KEY, "3"),
        (ARM_LONG_CONTEXT_INVOCATION_COUNT_KEY, True),
        (ARM_COST_TOTAL_KEY, "0.25"),
    ],
)
def test_a_malformed_archived_arm_total_is_refused(key: str, value: object) -> None:
    fields = _arm_with_totals(withheld=False).to_report_fields()
    fields[key] = value
    with pytest.raises(ValueError, match=key):
        arm_from_report_fields(fields)
