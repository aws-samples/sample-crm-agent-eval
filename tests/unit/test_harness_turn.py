# SPDX-License-Identifier: MIT
"""Tests that the eval harness turn adapter drives ``agents.turn_driver.drive_turn``.

Covers delegation to the shared driver, non-empty per-``(tier, role, model_id)``, per-tool and
per-task-spread arrays in a stub sweep's ``matrix.json``, and ``run_sweep(repeats=...)``. The stub
calls only supervisor-direct tools, so the subagent tiers stay at count 0.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import boto3
import pytest
from moto import mock_aws

from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.harness import ArmSpec, _observe_provider_turn, run_sweep
from eval.turn_observation import observe_turn


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


@pytest.fixture(autouse=True)
def _inject_noop_seeder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the default-path seeder to a no-op, through the seam every harness test file uses."""

    def _noop_seeder(store: Store, *, tenant: str) -> None:
        del store, tenant

    monkeypatch.setattr("eval.tenancy._default_seeder", lambda: _noop_seeder)


def test_the_observed_turn_delegates_to_the_shared_driver_rather_than_mirroring_it() -> None:
    """The harness turn reaches ``drive_turn`` through ``observe_turn``, not a mirrored loop."""
    assert "observe_turn(" in inspect.getsource(_observe_provider_turn)
    assert "drive_turn(" in inspect.getsource(observe_turn), (
        "eval.turn_observation.observe_turn must delegate to agents.turn_driver.drive_turn"
    )


def _arm_json(store: Store, tmp_path: Path, *, repeats: int = 1) -> dict[str, object]:
    """Run a one-arm stub sweep writing artifacts, and return its single arm's matrix.json object."""
    run_sweep(
        store=store,
        arm_specs=(ArmSpec(family="anthropic", effort="low"),),
        output_dir=tmp_path,
        repeats=repeats,
    )
    payload = json.loads((tmp_path / "matrix.json").read_text(encoding="utf-8"))
    arms = payload["arms"]
    assert len(arms) == 1
    arm = arms[0]
    assert isinstance(arm, dict)
    return arm


def _rows(arm: dict[str, object], key: str) -> list[dict[str, object]]:
    """The list-of-row-objects published under ``key`` (a JSON array of report-field mappings)."""
    return cast("list[dict[str, object]]", arm[key])


def test_stub_sweep_matrix_json_carries_the_three_aggregations_non_empty(
    store: Store, tmp_path: Path
) -> None:
    """The per-tier, per-tool and per-task aggregations are published non-empty for a stub sweep."""
    arm = _arm_json(store, tmp_path)

    tier_rows = _rows(arm, "tier_role_model_metrics")
    tool_rows = _rows(arm, "tool_metrics")
    spread_rows = _rows(arm, "task_spread")
    assert isinstance(tier_rows, list) and tier_rows, "per-(tier,role,model) array is empty"
    assert isinstance(tool_rows, list) and tool_rows, "per-tool array is empty"
    assert isinstance(spread_rows, list) and spread_rows, "per-task spread array is empty"

    # A stub sweep never delegates, so the supervisor row is the one always present.
    supervisor_rows = [r for r in tier_rows if r.get("tier") == "supervisor"]
    assert supervisor_rows, "no supervisor-tier row in the per-tier aggregation"
    assert supervisor_rows[0]["role"] == "supervisor"
    model_metrics = cast("dict[str, object]", arm["model_metrics"])
    assert supervisor_rows[0]["model_id"] == model_metrics["model_id"]
    assert cast("int", supervisor_rows[0]["included_invocation_count"]) >= 1


def test_stub_sweep_per_tool_rows_carry_the_full_four_way_outcome_taxonomy(
    store: Store, tmp_path: Path
) -> None:
    """Each per-tool row publishes the whole outcome distribution, present even at a zero count."""
    arm = _arm_json(store, tmp_path)
    tool_rows = _rows(arm, "tool_metrics")
    assert tool_rows
    for row in tool_rows:
        distribution = cast("dict[str, object]", row["outcome_distribution"])
        assert set(distribution) == {"data", "no_match", "denied", "tool_failure"}
        assert row["role"] == "supervisor"  # a stub only ever calls supervisor-direct tools


def test_stub_sweep_task_spread_has_one_row_per_scored_task(store: Store, tmp_path: Path) -> None:
    """The per-task spread carries one row per task that produced a correctness verdict."""
    arm = _arm_json(store, tmp_path)
    spread_rows = _rows(arm, "task_spread")
    task_ids = [r["task_id"] for r in spread_rows]
    assert len(task_ids) == len(set(task_ids)), "a task appears twice in the spread"
    # With a single repeat, each scored task's min/mean/max collapse to its one correctness value.
    for row in spread_rows:
        assert row["repeats"] == 1
        assert row["correctness_min"] == row["correctness_max"] == row["correctness_mean"]


def test_repeats_runs_each_task_the_requested_number_of_times(store: Store, tmp_path: Path) -> None:
    """``run_sweep(repeats=N)`` runs each golden task N times and the spread reports N repeats."""
    one = _arm_json(store, tmp_path / "r1", repeats=1)
    three = _arm_json(store, tmp_path / "r3", repeats=3)

    def _total(arm: dict[str, object]) -> int:
        agg = arm["aggregate_score"]
        assert isinstance(agg, dict)
        return int(agg["scored_task_count"]) + int(agg["unscored_task_count"])

    assert _total(three) == 3 * _total(one)
    for row in _rows(three, "task_spread"):
        assert row["repeats"] == 3


def test_run_sweep_rejects_a_repeats_below_one(store: Store) -> None:
    """``run_sweep`` rejects a ``repeats`` below one with ``ValueError``."""
    with pytest.raises(ValueError, match="repeats must be >= 1"):
        run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic", effort="low"),), repeats=0)


def test_default_stub_sweep_arms_all_publish_non_empty_aggregations(
    store: Store, tmp_path: Path
) -> None:
    """Both default-matrix arms (anthropic, openai) publish the three arrays non-empty."""
    run_sweep(store=store, output_dir=tmp_path)
    payload = json.loads((tmp_path / "matrix.json").read_text(encoding="utf-8"))
    arms = payload["arms"]
    assert len(arms) == 2
    for arm in arms:
        assert isinstance(arm, dict)
        assert arm["tier_role_model_metrics"]
        assert arm["tool_metrics"]
        assert arm["task_spread"]
