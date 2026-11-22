# SPDX-License-Identifier: MIT
"""Tests for the sweep cost cap: :class:`eval.harness.SweepCostGuard` and ``--max-cost-usd``."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.metering import TokenUsage, build_usage_record
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import harness
from eval.harness import (
    ArmSpec,
    SweepCostCapExceeded,
    SweepCostGuard,
    build_arg_parser,
    main,
    run_sweep,
)
from eval.sweep_units import TaskUnitResult

_ARM = (ArmSpec(family="anthropic"),)


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


@pytest.fixture(autouse=True)
def _noop_seeder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed nothing: these tests exercise the cap, not the scored fixture corpus."""

    def _seed(store: Store, *, tenant: str) -> None:
        del store, tenant

    monkeypatch.setattr("eval.tenancy._default_seeder", lambda: _seed)


class _Unit:
    """A stand-in task unit carrying only the ``usage_records`` the guard reads."""

    def __init__(self, *tokens: tuple[str, int]) -> None:
        self.usage_records = tuple(
            build_usage_record(
                correlation_id="c",
                turn_id="t",
                invocation_id=f"i{index}",
                rep_id="rep-1",
                model_id=model_id,
                family="anthropic",
                tier="supervisor",
                usage=TokenUsage(input_tokens=input_tokens, output_tokens=0),
                inference_profile=f"global.{model_id}",
            )
            for index, (model_id, input_tokens) in enumerate(tokens)
        )


def _task(unit: _Unit) -> TaskUnitResult:
    return unit  # type: ignore[return-value]


_HAIKU = "anthropic.claude-haiku-5-5"
_SONNET = "anthropic.claude-sonnet-5-5"


def _record_cost(unit: _Unit) -> float:
    return sum(
        r.cost.input + r.cost.output + r.cost.cache_write + r.cost.cache_read
        for r in unit.usage_records
    )


# -- SweepCostGuard ------------------------------------------------------------------------------


@pytest.mark.parametrize("cap", [0.0, -1.0, math.nan, math.inf])
def test_a_non_positive_or_non_finite_cap_is_refused(cap: float) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        SweepCostGuard(cap)


def test_the_first_task_always_starts() -> None:
    """With nothing spent and no task completed, the projection is zero."""
    SweepCostGuard(1e-12).before_task()


def test_record_task_sums_every_record_across_tiers_and_models() -> None:
    guard = SweepCostGuard(100.0)
    unit = _Unit((_HAIKU, 10_000), (_SONNET, 20_000))
    guard.record_task(_task(unit))
    assert guard.spent_usd == pytest.approx(_record_cost(unit))
    assert guard.spent_usd > 0
    assert guard.largest_task_usd == guard.spent_usd
    assert guard.completed_tasks == 1


def test_largest_task_is_the_maximum_not_the_last() -> None:
    guard = SweepCostGuard(100.0)
    big, small = _Unit((_SONNET, 50_000)), _Unit((_SONNET, 1_000))
    guard.record_task(_task(big))
    guard.record_task(_task(small))
    assert guard.largest_task_usd == pytest.approx(_record_cost(big))
    assert guard.spent_usd == pytest.approx(_record_cost(big) + _record_cost(small))


def test_spent_plus_largest_above_the_cap_refuses_the_next_task() -> None:
    unit = _Unit((_SONNET, 100_000))
    cost = _record_cost(unit)
    guard = SweepCostGuard(cost * 1.5)
    guard.record_task(_task(unit))
    with pytest.raises(SweepCostCapExceeded) as raised:
        guard.before_task()
    assert raised.value.spent_usd == pytest.approx(cost)
    assert raised.value.largest_task_usd == pytest.approx(cost)
    assert raised.value.max_cost_usd == cost * 1.5
    assert raised.value.completed_tasks == 1
    assert "before task 2" in str(raised.value)
    assert "--max-cost-usd" in str(raised.value)


def test_spent_plus_largest_equal_to_the_cap_still_starts() -> None:
    unit = _Unit((_SONNET, 100_000))
    guard = SweepCostGuard(100.0)
    guard.record_task(_task(unit))
    guard.max_cost_usd = guard.spent_usd + guard.largest_task_usd
    guard.before_task()


# -- run_sweep -----------------------------------------------------------------------------------


def test_a_breach_stops_before_the_next_task_tears_down_and_writes_no_matrix(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a cap below one task's cost, exactly one task runs; the rest never start."""
    started: list[tuple[str, str]] = []
    from eval.sweep_units import run_task_unit as real_run_task_unit

    def _recording_run_task_unit(**kwargs: Any) -> TaskUnitResult:
        started.append((str(kwargs["tenant"]), str(kwargs["task"]["id"])))
        return real_run_task_unit(**kwargs)

    torn_down: list[tuple[str, ...]] = []
    from eval.tenancy import teardown_unit_tenants as real_teardown

    def _recording_teardown(store: Store, *, tenants: Any) -> None:
        torn_down.append(tuple(tenants))
        real_teardown(store, tenants=tenants)

    monkeypatch.setattr(harness, "run_task_unit", _recording_run_task_unit)
    monkeypatch.setattr(harness, "teardown_unit_tenants", _recording_teardown)

    with pytest.raises(SweepCostCapExceeded) as raised:
        run_sweep(
            store=store,
            arm_specs=_ARM,
            repeats=2,
            output_dir=tmp_path,
            archive_raw=True,
            run_id="run-cap-breach",
            max_cost_usd=1e-9,
        )

    assert raised.value.completed_tasks == 1
    assert raised.value.spent_usd > 1e-9
    assert len(started) == 1, "only the first task may run"
    assert len(torn_down) == 1 and len(torn_down[0]) == 2, "both unit tenants are torn down"
    assert not (tmp_path / "matrix.json").exists()
    assert not (tmp_path / "matrix.md").exists()
    assert not (tmp_path / "matrix.csv").exists()
    assert not list((tmp_path / "raw").glob("*.json"))
    manifest = json.loads((tmp_path / "partial" / "partial-run.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == "run-cap-breach"
    assert manifest["reason"] == "cost_cap"
    assert manifest["completed_units"] == []
    assert manifest["in_flight_unit"]["completed_task_ids"] == [started[0][1]]


def test_a_mid_unit_breach_keeps_the_in_flight_units_completed_tasks(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cap that trips inside a unit persists that unit's finished tasks beside the manifest."""
    from eval.sweep_units import run_task_unit as real_run_task_unit

    probed: list[TaskUnitResult] = []

    def _recording_run_task_unit(**kwargs: Any) -> TaskUnitResult:
        unit = real_run_task_unit(**kwargs)
        probed.append(unit)
        return unit

    monkeypatch.setattr(harness, "run_task_unit", _recording_run_task_unit)
    run_sweep(store=store, arm_specs=_ARM, run_id="run-cap-probe")
    monkeypatch.setattr(harness, "run_task_unit", real_run_task_unit)
    costs = [
        sum(
            r.cost.input + r.cost.output + r.cost.cache_write + r.cost.cache_read
            for r in unit.usage_records
        )
        for unit in probed
    ]
    assert len(costs) >= 3 and min(costs) > 0
    cap = (costs[0] + costs[1] + max(costs[0], costs[1])) * 0.999

    with pytest.raises(SweepCostCapExceeded) as raised:
        run_sweep(
            store=store,
            arm_specs=_ARM,
            output_dir=tmp_path,
            run_id="run-cap-mid-unit",
            max_cost_usd=cap,
        )

    assert raised.value.completed_tasks == 2
    partial = tmp_path / "partial"
    manifest = json.loads((partial / "partial-run.json").read_text(encoding="utf-8"))
    in_flight = manifest["in_flight_unit"]
    assert len(in_flight["completed_task_ids"]) == 2
    snapshot = json.loads(
        (partial / "units" / "run-cap-mid-unit" / in_flight["raw"]).read_text(encoding="utf-8")
    )
    assert [task["task_id"] for task in snapshot["tasks"]] == in_flight["completed_task_ids"]
    assert all(task["invocation_attributions"] for task in snapshot["tasks"])


def test_a_cap_above_the_spend_completes_every_unit(store: Store) -> None:
    result = run_sweep(store=store, arm_specs=_ARM, repeats=2, max_cost_usd=1_000.0)
    assert len(result.arm_runs) == 1
    assert len(result.unit_tenants) == 2


def test_no_cap_means_no_guard(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("no guard may be built without max_cost_usd")

    monkeypatch.setattr(harness, "SweepCostGuard", _forbidden)
    run_sweep(store=store, arm_specs=_ARM)


# -- CLI -----------------------------------------------------------------------------------------


def test_the_parser_accepts_max_cost_usd_as_a_float() -> None:
    args = build_arg_parser().parse_args(["--max-cost-usd", "100"])
    assert args.max_cost_usd == 100.0
    assert build_arg_parser().parse_args([]).max_cost_usd is None


def test_live_without_max_cost_usd_exits_two(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _forbidden(**_kwargs: object) -> None:
        raise AssertionError("no store may be built for a refused invocation")

    monkeypatch.setattr(harness, "_build_default_store", _forbidden)
    assert main(["--live", "--substrate", "live-aws"]) == 2
    assert "--max-cost-usd" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf"])
def test_an_unusable_max_cost_usd_exits_two(
    value: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _forbidden(**_kwargs: object) -> None:
        raise AssertionError("no store may be built for a refused invocation")

    monkeypatch.setattr(harness, "_build_default_store", _forbidden)
    assert main(["--live", "--substrate", "live-aws", "--max-cost-usd", value]) == 2
    assert "positive finite" in capsys.readouterr().err


def test_a_breach_through_the_cli_exits_four_naming_spent_and_cap(
    store: Store,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harness, "_build_default_store", lambda **_kwargs: store)
    monkeypatch.setattr(harness, "DEFAULT_ARM_SPECS", _ARM)
    real_run_sweep = harness.run_sweep

    def _one_arm(**kwargs: Any) -> Any:
        return real_run_sweep(arm_specs=_ARM, **kwargs)

    monkeypatch.setattr(harness, "run_sweep", _one_arm)

    code = main(["--output-dir", str(tmp_path), "--repeats", "2", "--max-cost-usd", "0.000000001"])

    assert code == 4
    err = capsys.readouterr().err
    assert "spent $" in err and "--max-cost-usd $0.0000" in err
    assert "no matrix artifact written" in err
    assert not (tmp_path / "matrix.json").exists()
    assert (tmp_path / "partial" / "partial-run.json").exists()
