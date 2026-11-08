# SPDX-License-Identifier: MIT
"""The run payload's tier table and the published ``matrix.md`` agree byte for byte.

For stub-built matrices, ``write_matrix_artifacts`` writes ``matrix.md``; the same matrix's report
fields, round-tripped through JSON as the aggregate step stores them, become a ``RunRecord`` whose
``to_payload()["tier_comparison_table"]``, formatted as Markdown here, must equal that file's
``## Tier comparison summary`` section. The test also writes the payload tables the frontend
renders to ``frontend/src/pages/evals/__fixtures__/tierComparison.json``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

import pytest

from eval.matrix import (
    ArmResult,
    MatrixResult,
    RunProvenance,
    build_matrix_result,
    write_matrix_artifacts,
)
from eval.runs import RunRecord

_REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_FIXTURE = (
    _REPO_ROOT / "frontend" / "src" / "pages" / "evals" / "__fixtures__" / ("tierComparison.json")
)

_OPUS = "anthropic.claude-opus-5-5"
_SONNET = "anthropic.claude-sonnet-5-5"
_HAIKU = "anthropic.claude-haiku-5-5"
_SOL = "openai.gpt-6-1-sol"
_LUNA = "openai.gpt-6-1-luna"
_TIERS = ("supervisor", "subagent", "tool")


def _summary_entry(met: int, applicable: int, tokens: tuple[int, int]) -> dict[str, object]:
    return {
        "input_tokens": tokens[0],
        "output_tokens": tokens[1],
        "cost_total": None,
        "latency_p50_ms": None,
        "latency_p95_ms": None,
        "success": {
            "passed": met,
            "applicable": applicable,
            "rate": met / applicable if applicable else None,
        },
    }


def _stub_arm(
    family: str,
    models: tuple[str, str, str],
    *,
    completions: tuple[tuple[int, int], tuple[int, int], tuple[int, int]],
    outcomes: tuple[int, int, int, int, int],
) -> ArmResult:
    """A stub-mode arm with cost withheld; only an ``anthropic`` arm records retry counts."""
    by_tier = {
        tier: {
            "configured_model_id": model_id,
            "efforts": ["medium"] if tier == "supervisor" else ["low"],
            "latency_sample_count": 0,
            "completed_task_latency_ms_p50": None,
            "completed_task_latency_ms_p95": None,
        }
        for tier, model_id in zip(_TIERS, models, strict=True)
    }
    if family == "anthropic":
        for tier in _TIERS:
            by_tier[tier]["retry_count"] = 0
    tasks: tuple[dict[str, object], ...] = tuple(
        {
            "task_id": f"gt-{index:02d}",
            "scoring_mode": "machine",
            "correctness": 1.0 if index < completions[0][0] else 0.0,
            "unscored_reason": None,
            "bad_write": False,
            "tool_call_correct": True,
            "truncated": False,
            "budget_exhausted": False,
            "assertion_results": [],
        }
        for index in range(completions[0][1])
    )
    return ArmResult(
        provenance=RunProvenance(
            mode="stub",
            timestamp="2026-11-06T00:00:00+00:00",
            model_id=models[0],
            effort="medium",
            dataset_seed=20261106,
            git_revision="not-a-commit",
            runtime_identifier="local-stub-runtime",
            price_table_version="v1",
            answer_style_sha256="0" * 64,
        ),
        model_metrics_fields={
            "cost_withheld_provisional": True,
            "exclusions": {"total_excluded": 0, "by_category": {"cold_start": 0}},
        },
        aggregate_score_fields={
            "scored_task_count": len(tasks),
            "unscored_task_count": 0,
            "mean_correctness": completions[0][0] / completions[0][1],
            "bad_write_rate": 0.0,
            "tool_call_correct_rate": 1.0,
        },
        task_score_fields=tasks,
        tier_role_model_fields=tuple(
            {
                "tier": tier,
                "role": tier,
                "model_id": model_id,
                "included_invocation_count": 2,
                "input_tokens": 20,
                "output_tokens": 2,
            }
            for tier, model_id in zip(_TIERS, models, strict=True)
        ),
        tier_summary_fields={
            tier: _summary_entry(met, applicable, (100 * (i + 1), 10 * (i + 1)))
            for i, (tier, (met, applicable)) in enumerate(zip(_TIERS, completions, strict=True))
        },
        tier_task_metrics_fields={
            "family": family,
            "by_tier": by_tier,
            "delegation_outcomes": dict(
                zip(("data", "no_match", "denied", "tool_failure", "none"), outcomes, strict=True)
            ),
        },
    )


def _matrices() -> dict[str, MatrixResult]:
    anthropic = _stub_arm(
        "anthropic",
        (_SONNET, _HAIKU, _HAIKU),
        completions=((3, 4), (2, 3), (1, 2)),
        outcomes=(2, 0, 1, 0, 1),
    )
    openai = _stub_arm(
        "openai",
        (_SOL, _LUNA, _LUNA),
        completions=((2, 4), (0, 0), (2, 2)),
        outcomes=(0, 0, 0, 0, 4),
    )
    return {
        "multi_arm": build_matrix_result([anthropic, openai]),
        "single_arm": build_matrix_result(
            [
                _stub_arm(
                    "anthropic",
                    (_OPUS, _SONNET, _HAIKU),
                    completions=((1, 1), (1, 1), (0, 1)),
                    outcomes=(1, 0, 0, 0, 0),
                )
            ]
        ),
    }


def _matrix_md_section(markdown: str) -> str:
    start = markdown.index("## Tier comparison summary")
    end = markdown.index("Measured dimensions per model arm")
    return markdown[start:end]


def _table_markdown(table: dict[str, Any]) -> str:
    """Format a payload table the way ``matrix.md`` lays the summary out."""

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    headers = cast(list[str], table["headers"])
    lines = [
        "## Tier comparison summary",
        "",
        "Per-tier figures for each arm, one column per arm in run order; the notes below the "
        "tables define each row.",
        "",
    ]
    for section in table["sections"]:
        lines.extend([f"### {section['heading']}", "", line(headers), line(["---"] * len(headers))])
        lines.extend(line(row) for row in section["rows"])
        lines.append("")
    lines.extend(["Notes:", "", *(f"- {note}" for note in table["notes"]), ""])
    return "\n".join(lines) + "\n"


def _payload_table(matrix: MatrixResult, run_id: str) -> dict[str, Any]:
    stored = json.loads(json.dumps(matrix.to_report_fields()))
    record = RunRecord(
        run_id=run_id,
        tenant=f"eval-{run_id}",
        mode="stub",
        status="completed",
        started_at="2026-11-06T00:00:00+00:00",
        matrix_report=stored,
    )
    wire = json.loads(json.dumps(record.to_payload()))
    return cast(dict[str, Any], wire["tier_comparison_table"])


@pytest.mark.parametrize("case", ["multi_arm", "single_arm"])
def test_the_payload_table_formats_to_the_published_matrix_md_section(
    case: str, tmp_path: Path
) -> None:
    matrix = _matrices()[case]
    paths = write_matrix_artifacts(matrix, tmp_path)
    published = _matrix_md_section(paths["md"].read_text(encoding="utf-8"))
    table = _payload_table(matrix, f"run-{case}")
    assert _table_markdown(table) == published
    assert len(table["headers"]) == 1 + len(matrix.arms)


def test_a_reordered_cell_is_detected_control(tmp_path: Path) -> None:
    """Control: the byte comparison above sees a single changed cell."""
    matrix = _matrices()["multi_arm"]
    published = _matrix_md_section(
        write_matrix_artifacts(matrix, tmp_path)["md"].read_text("utf-8")
    )
    table = _payload_table(matrix, "run-control")
    row = table["sections"][0]["rows"][0]
    row[1], row[2] = row[2], row[1]
    assert _table_markdown(table) != published


def _fixture_document() -> str:
    cases: dict[str, object] = {}
    for case, matrix in _matrices().items():
        cases[case] = {"tier_comparison_table": _payload_table(matrix, f"run-{case}")}
    return json.dumps(cases, indent=2, ensure_ascii=False) + "\n"


def test_the_frontend_fixture_is_the_payload_tables(tmp_path: Path) -> None:
    document = _fixture_document()
    FRONTEND_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    staging = FRONTEND_FIXTURE.with_name(f".{FRONTEND_FIXTURE.name}.{os.getpid()}.tmp")
    staging.write_text(document, encoding="utf-8")
    staging.replace(FRONTEND_FIXTURE)
    written = json.loads(FRONTEND_FIXTURE.read_text(encoding="utf-8"))
    for case, matrix in _matrices().items():
        table = written[case]["tier_comparison_table"]
        published = _matrix_md_section(
            write_matrix_artifacts(matrix, tmp_path / case)["md"].read_text("utf-8")
        )
        assert _table_markdown(table) == published
