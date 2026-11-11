# SPDX-License-Identifier: MIT
"""Tests for the sweep cost cap: :class:`eval.harness.SweepCostGuard` and ``--max-cost-usd``."""

from __future__ import annotations

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
    """A stand-in carrying only the ``usage_records`` the guard reads."""

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


def _units(*units: _Unit) -> list[TaskUnitResult]:
    return list(units)  # type: ignore[arg-type]


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


def test_the_first_unit_always_starts() -> None:
    """With nothing spent and no unit completed, the projection is zero."""
    SweepCostGuard(1e-12).before_unit()


def test_record_unit_sums_every_record_across_tiers_and_models() -> None:
    guard = SweepCostGuard(100.0)
    unit = _Unit((_HAIKU, 10_000), (_SONNET, 20_000))
    guard.record_unit(_units(unit))
    assert guard.spent_usd == pytest.approx(_record_cost(unit))
    assert guard.spent_usd > 0
    assert guard.largest_unit_usd == guard.spent_usd
    assert guard.completed_units == 1


def test_largest_unit_is_the_maximum_not_the_last() -> None:
    guard = SweepCostGuard(100.0)
    big, small = _Unit((_SONNET, 50_000)), _Unit((_SONNET, 1_000))
    guard.record_unit(_units(big))
    guard.record_unit(_units(small))
    assert guard.largest_unit_usd == pytest.approx(_record_cost(big))
    assert guard.spent_usd == pytest.approx(_record_cost(big) + _record_cost(small))


def test_spent_plus_largest_above_the_cap_refuses_the_next_unit() -> None:
    unit = _Unit((_SONNET, 100_000))
    cost = _record_cost(unit)
    guard = SweepCostGuard(cost * 1.5)
    guard.record_unit(_units(unit))
    with pytest.raises(SweepCostCapExceeded) as raised:
        guard.before_unit()
    assert raised.value.spent_usd == pytest.approx(cost)
    assert raised.value.largest_unit_usd == pytest.approx(cost)
    assert raised.value.max_cost_usd == cost * 1.5
    assert raised.value.completed_units == 1
    assert "--max-cost-usd" in str(raised.value)


def test_spent_plus_largest_equal_to_the_cap_still_starts() -> None:
    unit = _Unit((_SONNET, 100_000))
    guard = SweepCostGuard(100.0)
    guard.record_unit(_units(unit))
    guard.max_cost_usd = guard.spent_usd + guard.largest_unit_usd
    guard.before_unit()


# -- run_sweep -----------------------------------------------------------------------------------


def test_a_breach_stops_before_the_next_unit_tears_down_and_writes_nothing(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a cap below one unit's cost, repeat 0 runs, repeat 1 never starts."""
    started: list[str] = []
    from eval.sweep_units import run_task_unit as real_run_task_unit

    def _recording_run_task_unit(**kwargs: Any) -> TaskUnitResult:
        started.append(str(kwargs["tenant"]))
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

    assert raised.value.completed_units == 1
    assert raised.value.spent_usd > 1e-9
    assert len(set(started)) == 1, "only the first unit tenant may run any task"
    assert len(torn_down) == 1 and len(torn_down[0]) == 2, "both unit tenants are torn down"
    assert not (tmp_path / "matrix.json").exists()
    assert not (tmp_path / "matrix.md").exists()
    assert not (tmp_path / "matrix.csv").exists()
    assert not list((tmp_path / "raw").glob("*.json"))


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
