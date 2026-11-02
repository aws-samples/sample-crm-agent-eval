# SPDX-License-Identifier: MIT
"""A real stub sweep against DynamoDB Local publishes the tier comparison summary end to end.

Two identical sweeps run with cost withheld; their artifacts pass ``assert_artifacts`` and the
``check_cost_withheld`` gate under a provisional price table, open with the summary in its fixed
section order with stub columns ``not measured`` and supervisor effort ``medium``, and differ only
in ``timestamp``.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import pytest
import yaml
from scripts import proc_run

from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLE_NAMES
from crm.store.store import Store
from eval.harness import DEFAULT_ARM_SPECS, SweepResult, assert_artifacts, run_sweep
from tests.integration.conftest import _ENDPOINT_URL, _LOCAL_ENGINE_CRED

REPO_ROOT = Path(__file__).resolve().parents[2]
_GOLDEN_TASKS = REPO_ROOT / "src" / "eval" / "golden_tasks.yaml"
_PRICING = REPO_ROOT / "config" / "pricing.yaml"
_GATE = REPO_ROOT / "scripts" / "check_cost_withheld.py"
#: Unique per process, so concurrent xdist workers never share a table.
_TABLE_PREFIX = f"tiersum-{uuid.uuid4().hex[:12]}-"
_RUN_ID = "tiersummary01"

#: An ISO-8601 UTC timestamp as `datetime.now(UTC).isoformat()` renders it.
_ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00")

_SECTIONS = ("Supervisor", "Subagent", "ToolCall")
_COMMON_ROWS = (
    "Model",
    "Effort",
    "Task completion rate",
    "Task completion over repeats",
    "Task accuracy",
    "Cost per completed task",
    "P95 latency per completed task (ms)",
    "P50 latency per completed task (ms)",
    "Model invocations",
    "Input tokens",
    "Output tokens",
    "Cost per task, all tasks",
    "Self-corrected retries",
)
_SPECIFIC_ROWS = {
    "Supervisor": ("Budget-exhausted tasks",),
    "Subagent": tuple(
        f"Delegation outcome: {kind}"
        for kind in ("data", "no_match", "denied", "tool_failure", "none")
    ),
    "ToolCall": ("Truncated tasks",),
}


@dataclass(frozen=True)
class _Sweeps:
    first: SweepResult
    first_dir: Path
    second_dir: Path


@pytest.fixture(scope="module")
def sweeps(
    _dynamodb_local_endpoint: None, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[_Sweeps]:
    """Two identical cost-withheld stub sweeps over this module's own DynamoDB Local tables."""
    client: Any = boto3.client(
        "dynamodb",
        endpoint_url=_ENDPOINT_URL,
        region_name="us-east-1",
        aws_access_key_id=_LOCAL_ENGINE_CRED,
        aws_secret_access_key=_LOCAL_ENGINE_CRED,
    )
    root = tmp_path_factory.mktemp("tier_comparison_sweeps")
    create_all_tables(client, table_prefix=_TABLE_PREFIX)
    try:
        store = Store(table_prefix=_TABLE_PREFIX, client=client)
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr("eval.harness.is_cost_provisional", lambda: True)
            results = [
                run_sweep(store=store, output_dir=root / name, archive_raw=True, run_id=_RUN_ID)
                for name in ("first", "second")
            ]
        yield _Sweeps(first=results[0], first_dir=root / "first", second_dir=root / "second")
    finally:
        for table in TABLE_NAMES:
            with contextlib.suppress(client.exceptions.ResourceNotFoundException):
                client.delete_table(TableName=f"{_TABLE_PREFIX}{table}")


def _golden_task_count() -> int:
    tasks = yaml.safe_load(_GOLDEN_TASKS.read_text(encoding="utf-8"))["tasks"]
    assert isinstance(tasks, list)
    return len(tasks)


def _markdown(sweeps: _Sweeps) -> str:
    return (sweeps.first_dir / "matrix.md").read_text(encoding="utf-8")


def _summary_table(markdown: str, section: str) -> dict[str, list[str]]:
    """Map each row label of one summary section's table to its cells."""
    block = markdown[: markdown.index("Measured dimensions per model arm")]
    body = block[block.index(f"### {section}\n") :]
    rows: dict[str, list[str]] = {}
    for line in body.splitlines()[2:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split(" | ")]
        rows[cells[0]] = cells[1:]
    return rows


def _without_timestamps(value: object) -> object:
    if isinstance(value, dict):
        return {
            k: ("<ts>" if k == "timestamp" else _without_timestamps(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_without_timestamps(item) for item in value]
    return value


def _provisional_pricing(tmp_path: Path) -> Path:
    path = tmp_path / "pricing.yaml"
    forced = re.sub(
        r"(?m)^provisional:\s*false\s*$",
        "provisional: true",
        _PRICING.read_text(encoding="utf-8"),
    )
    assert re.search(r"(?m)^provisional:\s*true\s*$", forced)
    path.write_text(forced, encoding="utf-8")
    return path


def _run_gate(pricing_path: Path, results_dir: Path) -> subprocess.CompletedProcess[Any]:
    return proc_run.run(
        [
            sys.executable,
            str(_GATE),
            "--pricing-file",
            str(pricing_path),
            "--results-dir",
            str(results_dir),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_the_sweep_artifacts_pass_assert_artifacts(sweeps: _Sweeps) -> None:
    assert_artifacts(
        output_dir=sweeps.first_dir,
        expected_arm_count=len(DEFAULT_ARM_SPECS),
        expected_task_count=_golden_task_count(),
        raw_archive_paths=sweeps.first.raw_archive_paths,
    )


def test_matrix_md_opens_with_the_summary_in_the_ac1_order(sweeps: _Sweeps) -> None:
    markdown = _markdown(sweeps)
    assert markdown.startswith("# Model Evaluation Matrix\n\n## Tier comparison summary\n")
    markers = [
        "## Tier comparison summary",
        *(f"### {section}" for section in _SECTIONS),
        "Notes:",
        "Measured dimensions per model arm",
        "## Reading these figures",
    ]
    positions = [markdown.index(marker) for marker in markers]
    assert positions == sorted(positions)
    for section in _SECTIONS:
        rows = _summary_table(markdown, section)
        assert list(rows) == ["Metric", "---", *_COMMON_ROWS, *_SPECIFIC_ROWS[section]]
        assert all(len(cells) == len(DEFAULT_ARM_SPECS) for cells in rows.values())
        assert rows["Metric"] == [spec.family for spec in DEFAULT_ARM_SPECS]


def test_stub_columns_read_not_measured_for_latency(sweeps: _Sweeps) -> None:
    markdown = _markdown(sweeps)
    for section in _SECTIONS:
        rows = _summary_table(markdown, section)
        for label in ("P95 latency per completed task (ms)", "P50 latency per completed task (ms)"):
            assert rows[label] == ["not measured"] * len(DEFAULT_ARM_SPECS)


def test_withheld_cost_cells_read_withheld(sweeps: _Sweeps) -> None:
    markdown = _markdown(sweeps)
    for section in _SECTIONS:
        rows = _summary_table(markdown, section)
        for label in ("Cost per completed task", "Cost per task, all tasks"):
            assert rows[label] == ["withheld"] * len(DEFAULT_ARM_SPECS)


def test_every_arm_ran_its_supervisor_at_effort_medium(sweeps: _Sweeps) -> None:
    payload = json.loads((sweeps.first_dir / "matrix.json").read_text(encoding="utf-8"))
    assert [arm["provenance"]["effort"] for arm in payload["arms"]] == ["medium"] * len(
        DEFAULT_ARM_SPECS
    )
    assert [entry["efforts"] for entry in payload["tier_comparison"]["tiers"]["supervisor"]] == [
        ["medium"]
    ] * len(DEFAULT_ARM_SPECS)
    rows = _summary_table(_markdown(sweeps), "Supervisor")
    assert rows["Effort"] == ["medium"] * len(DEFAULT_ARM_SPECS)


def test_two_identical_sweeps_differ_only_in_timestamp(sweeps: _Sweeps) -> None:
    first_json = json.loads((sweeps.first_dir / "matrix.json").read_text(encoding="utf-8"))
    second_json = json.loads((sweeps.second_dir / "matrix.json").read_text(encoding="utf-8"))
    assert _without_timestamps(first_json) == _without_timestamps(second_json)
    for name in ("matrix.md", "matrix.csv"):
        first = (sweeps.first_dir / name).read_text(encoding="utf-8")
        second = (sweeps.second_dir / name).read_text(encoding="utf-8")
        assert _ISO_TIMESTAMP.sub("<ts>", first) == _ISO_TIMESTAMP.sub("<ts>", second)


def _published_copy(sweeps: _Sweeps, tmp_path: Path) -> Path:
    """Copy the sweep's artifacts, leaving out the ``raw/pre-aggregation/`` working records."""
    results_dir = tmp_path / "results"
    shutil.copytree(sweeps.first_dir, results_dir, ignore=shutil.ignore_patterns("pre-aggregation"))
    assert not (results_dir / "raw" / "pre-aggregation").exists()
    assert sorted(path.name for path in (results_dir / "raw").glob("*.json")) == sorted(
        path.name for path in sweeps.first.raw_archive_paths
    )
    return results_dir


def test_the_withheld_artifacts_pass_check_cost_withheld_under_provisional_pricing(
    sweeps: _Sweeps, tmp_path: Path
) -> None:
    result = _run_gate(_provisional_pricing(tmp_path), _published_copy(sweeps, tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no absolute currency figures published while provisional" in result.stdout


def test_a_planted_cost_per_completed_task_figure_is_reported(
    sweeps: _Sweeps, tmp_path: Path
) -> None:
    results_dir = _published_copy(sweeps, tmp_path)
    matrix_json = results_dir / "matrix.json"
    payload = json.loads(matrix_json.read_text(encoding="utf-8"))
    payload["tier_comparison"]["tiers"]["supervisor"][0]["cost_per_completed_task"] = 0.42
    matrix_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    result = _run_gate(_provisional_pricing(tmp_path), results_dir)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cost_per_completed_task" in result.stdout + result.stderr
