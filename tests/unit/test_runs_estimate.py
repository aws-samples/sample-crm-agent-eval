# SPDX-License-Identifier: MIT
"""`eval.runs.estimate`: baseline path, static path, and the price math vs `cost_for_usage`.

The store is a real `moto`-backed `Store`. Expected figures are recomputed independently from
`crm.metering.cost_for_usage`, never copied from the implementation.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
import yaml
from moto import mock_aws

from crm.authz import Identity
from crm.errors import CrmError
from crm.handlers._common import decode
from crm.metering import TokenUsage, cost_for_usage
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import runs
from providers.capabilities import load_capability_matrix

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "events"
_REST_V1 = json.loads((_FIXTURE / "apigw_v1_proxy_post_get_account.json").read_text("utf-8"))
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
_MATRIX = load_capability_matrix()
OPUS = _MATRIX.model_for("anthropic", "supervisor").model_id
HAIKU = _MATRIX.model_for("anthropic", "tool").model_id
ASTRA = _MATRIX.model_for("openai", "supervisor").model_id


def identity() -> Identity:
    event: dict[str, Any] = copy.deepcopy(_REST_V1)
    event["body"] = "{}"
    return decode(event)[0]


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _cost(model_id: str, input_tokens: float, output_tokens: float) -> float:
    return cost_for_usage(TokenUsage(round(input_tokens), round(output_tokens)), model_id).total


def _matrix(arms: list[tuple[str, int, list[tuple[str, int, int]]]]) -> dict[str, Any]:
    return {
        "arms": [
            {
                "provenance": {"model_id": arm_model},
                "task_scores": [{"task_id": f"t{i}"} for i in range(tasks)],
                "tier_role_model_metrics": [
                    {
                        "tier": "supervisor",
                        "model_id": model_id,
                        "input_tokens": inp,
                        "output_tokens": out,
                        "cache_read_tokens": 0,
                        "cache_write_tokens": 0,
                    }
                    for model_id, inp, out in rows
                ],
            }
            for arm_model, tasks, rows in arms
        ]
    }


def _completed_run(
    store: Store, run_id: str, *, mode: str, matrix: dict[str, Any] | None, at: datetime
) -> None:
    runs.create_run(store, run_id=run_id, mode=mode, identity=identity(), tasks_total=4, now=at)  # type: ignore[arg-type]
    values: dict[str, object] = {"job_status": "completed"}
    if matrix is not None:
        values["matrix_report"] = json.dumps(matrix)
    store.update_item("eval_runs", {"run_id": f"job#{run_id}"}, set_values=values, condition=None)


def test_the_static_path_prices_every_tier_of_every_arm(store: Store) -> None:
    per_task = yaml.safe_load(runs.DEFAULT_ESTIMATE_PATH.read_text(encoding="utf-8"))["per_task"]
    result = runs.estimate(store, task_count=20, families=("anthropic", "openai"))
    assert result.basis == "static" and result.basis_run_id is None
    for family in ("anthropic", "openai"):
        expected = 20 * sum(
            _cost(e["model_id"], e["input_tokens"], e["output_tokens"])
            for e in per_task[family].values()
        )
        assert result.per_arm_usd[family] == pytest.approx(expected)
    assert result.estimate_usd == pytest.approx(sum(result.per_arm_usd.values()))
    assert result.estimate_usd > 0


def test_the_estimate_scales_with_the_task_count(store: Store) -> None:
    one = runs.estimate(store, task_count=1, families=("anthropic",))
    twenty = runs.estimate(store, task_count=20, families=("anthropic",))
    assert twenty.estimate_usd == pytest.approx(20 * one.estimate_usd)


def test_the_baseline_path_uses_the_newest_completed_live_run(store: Store) -> None:
    older = _matrix([(OPUS, 2, [(OPUS, 2000, 200)])])
    newer = _matrix(
        [(OPUS, 4, [(OPUS, 4000, 400), (HAIKU, 800, 80)]), (ASTRA, 2, [(ASTRA, 1000, 100)])]
    )
    _completed_run(store, "old", mode="live", matrix=older, at=NOW)
    _completed_run(store, "new", mode="live", matrix=newer, at=NOW + timedelta(hours=1))
    _completed_run(store, "stub", mode="stub", matrix=older, at=NOW + timedelta(hours=2))

    result = runs.estimate(store, task_count=20, families=("anthropic", "openai"))

    assert result.basis == "baseline" and result.basis_run_id == "new"
    anthropic = 20 * (_cost(OPUS, 1000, 100) + _cost(HAIKU, 200, 20))
    openai = 20 * _cost(ASTRA, 500, 50)
    assert result.per_arm_usd == pytest.approx({OPUS: anthropic, ASTRA: openai})
    assert result.estimate_usd == pytest.approx(anthropic + openai)


@pytest.mark.parametrize(
    "matrix",
    [None, {"arms": []}, _matrix([(OPUS, 0, [(OPUS, 1, 1)])]), _matrix([(OPUS, 2, [])])],
    ids=["no-matrix", "no-arms", "no-tasks", "no-tier-metrics"],
)
def test_an_unusable_baseline_falls_back_to_static(
    store: Store, matrix: dict[str, Any] | None
) -> None:
    _completed_run(store, "r", mode="live", matrix=matrix, at=NOW)
    assert runs.estimate(store, task_count=20, families=("anthropic", "openai")).basis == "static"


def test_a_failed_live_run_is_not_a_baseline(store: Store) -> None:
    _completed_run(store, "r", mode="live", matrix=_matrix([(OPUS, 2, [(OPUS, 10, 10)])]), at=NOW)
    store.update_item(
        "eval_runs", {"run_id": "job#r"}, set_values={"job_status": "failed"}, condition=None
    )
    assert runs.estimate(store, task_count=20, families=("anthropic",)).basis == "static"


def test_a_family_missing_from_the_static_file_is_an_error(store: Store, tmp_path: Path) -> None:
    data = yaml.safe_load(runs.DEFAULT_ESTIMATE_PATH.read_text(encoding="utf-8"))
    del data["per_task"]["openai"]
    path = tmp_path / "estimate.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(CrmError) as exc_info:
        runs.estimate(store, task_count=20, families=("anthropic", "openai"), static_path=path)
    assert exc_info.value.detail is not None and "openai" in exc_info.value.detail


@pytest.mark.parametrize("content", ["schema_version: [", "schema_version: 2\n", ""])
def test_an_unreadable_static_file_is_a_crm_error_not_a_crash(
    store: Store, tmp_path: Path, content: str
) -> None:
    """YAML, schema and type errors surface as the taxonomy's internal error."""
    path = tmp_path / "estimate.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(CrmError, match="could not be computed"):
        runs.estimate(store, task_count=20, families=("anthropic",), static_path=path)


def test_an_unusable_baseline_is_logged(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    """Falling back from a malformed baseline is visible."""
    bad = _matrix([(OPUS, 2, [(OPUS, 10, 10)])])
    bad["arms"][0]["tier_role_model_metrics"][0]["input_tokens"] = "many"
    _completed_run(store, "r", mode="live", matrix=bad, at=NOW)
    with caplog.at_level("WARNING", logger="eval.runs"):
        assert runs.estimate(store, task_count=20, families=("anthropic",)).basis == "static"
    assert any("eval_estimate_baseline_unusable" in r.getMessage() for r in caplog.records)


#: A model id no tier configures and no price row names, standing in for a retired model.
_RETIRED = "anthropic.claude-retired-for-test"


def test_a_baseline_naming_a_retired_model_falls_back_to_static(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """A baseline whose tier rows name a retired model is skipped, not priced."""
    retired = _matrix([(OPUS, 2, [(OPUS, 4000, 400), (_RETIRED, 800, 80)])])
    _completed_run(store, "r-retired", mode="live", matrix=retired, at=NOW)

    with caplog.at_level("WARNING", logger="eval.runs"):
        result = runs.estimate(store, task_count=20, families=("anthropic",))

    assert result.basis == "static"
    assert result.basis_run_id is None
    (logged,) = [
        json.loads(r.getMessage())
        for r in caplog.records
        if "eval_estimate_baseline_unusable" in r.getMessage()
    ]
    assert logged["reason"] == "baseline_models_retired"
    assert logged["correlation_id"] == "r-retired"
    assert set(logged) == {"event", "correlation_id", "reason"}


def test_a_baseline_on_current_models_is_still_used(store: Store) -> None:
    """Control: the same baseline shape naming only current tier models keeps the baseline path."""
    current = _matrix([(OPUS, 2, [(OPUS, 4000, 400), (HAIKU, 800, 80)])])
    _completed_run(store, "r-current", mode="live", matrix=current, at=NOW)

    result = runs.estimate(store, task_count=20, families=("anthropic",))

    assert result.basis == "baseline"
    assert result.basis_run_id == "r-current"


def test_the_payload_rounds_to_cents_and_names_the_basis(store: Store) -> None:
    payload = runs.estimate(store, task_count=20, families=("anthropic", "openai")).to_payload()
    assert payload["basis"] == "static"
    assert "basis_run_id" not in payload
    assert payload["task_count"] == 20
    assert payload["estimate_usd"] == round(float(payload["estimate_usd"]), 2)  # type: ignore[arg-type]
