# SPDX-License-Identifier: MIT
"""`eval.runs.Estimate.effort_caveat`: set exactly when the figures' supervisor effort differs
from `eval.sweep_units.DEFAULT_ARM_EFFORT`, on the static and the baseline basis.

The store is a real `moto`-backed `Store`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest
import yaml
from moto import mock_aws

from crm.authz import Identity
from crm.errors import CrmError
from crm.handlers._common import decode
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import runs
from eval.sweep_units import DEFAULT_ARM_EFFORT
from providers.capabilities import load_capability_matrix

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "events"
_REST_V1 = json.loads((_FIXTURE / "apigw_v1_proxy_post_get_account.json").read_text("utf-8"))
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
_MATRIX = load_capability_matrix()
OPUS = _MATRIX.model_for("anthropic", "supervisor").model_id
ASTRA = _MATRIX.model_for("openai", "supervisor").model_id
FAMILIES = ("anthropic", "openai")


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _identity() -> Identity:
    event: dict[str, Any] = copy.deepcopy(_REST_V1)
    event["body"] = "{}"
    return decode(event)[0]


def _static_file(tmp_path: Path, measured: str) -> Path:
    data = yaml.safe_load(runs.DEFAULT_ESTIMATE_PATH.read_text(encoding="utf-8"))
    data["measured_at_supervisor_effort"] = measured
    path = tmp_path / "estimate.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _baseline(store: Store, efforts: list[str | None]) -> None:
    arms = []
    for model_id, effort in zip((OPUS, ASTRA), efforts, strict=True):
        provenance: dict[str, object] = {"model_id": model_id}
        if effort is not None:
            provenance["effort"] = effort
        arms.append(
            {
                "provenance": provenance,
                "task_scores": [{"task_id": "t0"}],
                "tier_role_model_metrics": [
                    {"tier": "supervisor", "model_id": model_id, "input_tokens": 100}
                ],
            }
        )
    runs.create_run(
        store, run_id="live-1", mode="live", identity=_identity(), tasks_total=2, now=NOW
    )
    store.update_item(
        "eval_runs",
        {"run_id": "job#live-1"},
        set_values={"job_status": "completed", "matrix_report": json.dumps({"arms": arms})},
        condition=None,
    )


def test_the_default_supervisor_effort_is_medium() -> None:
    """The caveat cells below assume a `low` figure differs from the default."""
    assert DEFAULT_ARM_EFFORT == "medium"


def test_the_shipped_static_file_carries_no_caveat(store: Store) -> None:
    result = runs.estimate(store, task_count=20, families=FAMILIES)
    assert result.basis == "static"
    assert result.effort_caveat is None
    assert "effort_caveat" not in result.to_payload()


def test_a_static_file_measured_at_low_carries_the_caveat(store: Store, tmp_path: Path) -> None:
    path = _static_file(tmp_path, "low")
    result = runs.estimate(store, task_count=20, families=FAMILIES, static_path=path)
    expected = runs.ESTIMATE_EFFORT_CAVEAT.format(measured="low", default=DEFAULT_ARM_EFFORT)
    assert result.effort_caveat == expected
    assert result.to_payload()["effort_caveat"] == expected
    assert "low" in expected and "medium" in expected


def test_a_static_file_measured_at_the_default_carries_no_caveat(
    store: Store, tmp_path: Path
) -> None:
    path = _static_file(tmp_path, DEFAULT_ARM_EFFORT)
    result = runs.estimate(store, task_count=20, families=FAMILIES, static_path=path)
    assert result.effort_caveat is None
    assert "effort_caveat" not in result.to_payload()


def test_a_static_file_without_the_measured_effort_is_an_error(
    store: Store, tmp_path: Path
) -> None:
    data = yaml.safe_load(runs.DEFAULT_ESTIMATE_PATH.read_text(encoding="utf-8"))
    del data["measured_at_supervisor_effort"]
    path = tmp_path / "estimate.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="measured_at_supervisor_effort"):
        runs.load_static_estimate(path)
    with pytest.raises(CrmError) as raised:
        runs.estimate(store, task_count=20, families=FAMILIES, static_path=path)
    assert "measured_at_supervisor_effort" in str(raised.value.detail)


def test_a_baseline_with_an_arm_at_low_carries_the_caveat(store: Store) -> None:
    _baseline(store, ["low", DEFAULT_ARM_EFFORT])
    result = runs.estimate(store, task_count=20, families=FAMILIES)
    assert result.basis == "baseline"
    assert result.effort_caveat == runs.ESTIMATE_EFFORT_CAVEAT.format(
        measured=f"low, {DEFAULT_ARM_EFFORT}", default=DEFAULT_ARM_EFFORT
    )
    assert "effort_caveat" in result.to_payload()


def test_a_baseline_with_every_arm_at_the_default_carries_no_caveat(store: Store) -> None:
    _baseline(store, [DEFAULT_ARM_EFFORT, DEFAULT_ARM_EFFORT])
    result = runs.estimate(store, task_count=20, families=FAMILIES)
    assert result.basis == "baseline"
    assert result.effort_caveat is None
    assert "effort_caveat" not in result.to_payload()


def test_a_baseline_arm_without_a_recorded_effort_carries_the_caveat(store: Store) -> None:
    _baseline(store, [DEFAULT_ARM_EFFORT, None])
    result = runs.estimate(store, task_count=20, families=FAMILIES)
    assert result.basis == "baseline"
    assert result.effort_caveat is not None and "unrecorded" in result.effort_caveat
