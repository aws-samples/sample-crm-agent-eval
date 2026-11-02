# SPDX-License-Identifier: MIT
"""The state-machine-shaped sweep path scores exactly what the in-process harness scores.

The SFN shape runs each ``(arm, task)`` cell as an independent :func:`eval.sweep_units.run_task_unit`
whose turn crosses a process boundary as strict JSON (:func:`eval.turn_observation.serialise_turn`
then :func:`~eval.turn_observation.deserialise_turn`), then folds each arm with
:func:`eval.sweep_units.assemble_arm` and no arm-level stream counter. Every published arm field must
equal the in-process :func:`eval.harness._run_arm` result, except ``timestamp`` and
``runtime_identifier``. That comparison runs every arm in ``TENANT`` on both sides.

The real state-machine handlers (``eval.sfn_handlers``, driven step by step with the in-process
Runtime of ``tests.unit.test_sfn_handlers``) then run one sweep at each of 1 and 2 repeats, each
(arm, repeat) in its own unit tenant; the matrix they publish equals
:func:`eval.harness.run_sweep`'s for the same run id and repeats, ``timestamp`` and
``runtime_identifier`` aside.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import harness, sfn_handlers
from eval.runs import require_run
from eval.sweep_units import (
    TaskUnitResult,
    arm_tuning,
    assemble_arm,
    run_task_unit,
    task_actor,
    task_refs,
)
from eval.task_ids import localize_tasks
from eval.turn_observation import ObservedTurn, deserialise_turn, serialise_turn
from providers.capabilities import load_capability_matrix
from providers.stub import StubProvider
from tests.unit.test_sfn_handlers import make_sweep, mocked_aws

MATRIX = load_capability_matrix()
TENANT = "eval-sfnequiv"
_COMMON: dict[str, Any] = {
    "mode": "stub",
    "dataset_seed": 1,
    "git_revision": "rev",
    "price_table_version": "1.0.0",
    "cost_withheld": True,
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def _fresh_store() -> Store:
    client = boto3.client("dynamodb", region_name="us-east-1")
    create_all_tables(client)
    return Store(client=client)


def _isolated(run: Any) -> list[dict[str, Any]]:
    """Run ``run`` against its own empty moto account, so the two paths share no state."""
    with mock_aws():
        result: list[dict[str, Any]] = run(_fresh_store())
    return result


def _tasks() -> list[dict[str, object]]:
    return localize_tasks(
        harness._load_golden_tasks(harness._DEFAULT_GOLDEN_TASKS_PATH), tenant=TENANT
    )


def _in_process(store: Store) -> list[dict[str, Any]]:
    return [
        harness._run_arm(
            provider=StubProvider(),
            matrix=MATRIX,
            arm_spec=spec,
            tasks=harness._load_golden_tasks(harness._DEFAULT_GOLDEN_TASKS_PATH),
            store=store,
            unit_tenants=[TENANT],
            runtime_identifier="in-process",
            arm_index=index,
            **_COMMON,
        ).arm_result.to_report_fields()
        for index, spec in enumerate(harness.DEFAULT_ARM_SPECS)
    ]


def _across_the_wire(turn: ObservedTurn) -> ObservedTurn:
    return deserialise_turn(json.loads(json.dumps(serialise_turn(turn), allow_nan=False)))


def _sfn_shaped(store: Store) -> list[dict[str, Any]]:
    tasks = {str(task["id"]): task for task in _tasks()}
    specs = harness.DEFAULT_ARM_SPECS
    units: dict[int, list[TaskUnitResult]] = {index: [] for index in range(len(specs))}
    for ref in task_refs(list(tasks.values()), specs):
        spec = specs[ref.arm_index]

        def runner(task: Mapping[str, object], tenant: str, spec: Any = spec) -> ObservedTurn:
            actor = task_actor(task)
            turn = harness._observe_provider_turn(
                StubProvider(),
                matrix=MATRIX,
                tuning=arm_tuning(spec),
                prompt=str(task.get("prompt") or ""),
                store=store,
                tenant=tenant,
                identity_payload=harness._identity_payload_for_actor(actor, tenant=tenant),
                task_id=str(task["id"]),
            )
            crossed = _across_the_wire(turn)
            assert crossed == turn, f"{task['id']}: the turn changed crossing the wire"
            return crossed

        units[ref.arm_index].append(
            run_task_unit(
                store=store,
                task=tasks[ref.task_id],
                tenant=TENANT,
                family=spec.family,
                turn_runner=runner,
            )
        )
    return [
        assemble_arm(
            units[index],
            matrix=MATRIX,
            arm_spec=spec,
            runtime_identifier="state-machine",
            arm_index=index,
            **_COMMON,
        ).arm_result.to_report_fields()
        for index, spec in enumerate(specs)
    ]


def _comparable(arm: dict[str, Any]) -> dict[str, Any]:
    provenance = {
        k: v for k, v in arm["provenance"].items() if k not in ("timestamp", "runtime_identifier")
    }
    return {**arm, "provenance": provenance}


def test_the_state_machine_shape_equals_the_in_process_sweep() -> None:
    in_process = _isolated(_in_process)
    sfn = _isolated(_sfn_shaped)
    assert len(in_process) == len(sfn) == 2
    assert [_comparable(arm) for arm in sfn] == [_comparable(arm) for arm in in_process]
    assert {arm["provenance"]["runtime_identifier"] for arm in sfn} == {"state-machine"}


def test_the_comparison_discriminates() -> None:
    """The equality above is not vacuous: one perturbed field changes the comparison."""
    baseline = [_comparable(arm) for arm in _isolated(_in_process)]
    perturbed = [_comparable(arm) for arm in _isolated(_sfn_shaped)]
    perturbed[0]["aggregate_score"] = {**perturbed[0]["aggregate_score"], "scored_task_count": -1}
    assert perturbed != baseline


#: Golden tasks per arm and repeat in the real-handler comparison.
_HANDLER_TASKS = 3


def _comparable_matrix(report: dict[str, Any]) -> dict[str, Any]:
    return {**report, "arms": [_comparable(arm) for arm in report["arms"]]}


def _limit_golden_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    load = harness._load_golden_tasks

    def _first(path: Any) -> list[dict[str, object]]:
        return load(path)[:_HANDLER_TASKS]

    monkeypatch.setattr(harness, "_load_golden_tasks", _first)
    monkeypatch.setattr(sfn_handlers, "_load_golden_tasks", _first)


def _state_machine_matrix(monkeypatch: pytest.MonkeyPatch, repeats: int) -> tuple[str, Any]:
    """Drive one sweep through every handler step; return its run id and published matrix."""
    with mocked_aws(monkeypatch):
        sweep = make_sweep(monkeypatch, tasks=_HANDLER_TASKS, repeats=repeats)
        _limit_golden_tasks(monkeypatch)
        final = sweep.drive()
        assert final["status"] == "completed", final
        assert sweep.tenant_rows() == 0
        record = require_run(sweep.store, sweep.run_id)
        assert record.repeats == repeats
        return sweep.run_id, record.matrix_report


def _in_process_matrix(monkeypatch: pytest.MonkeyPatch, run_id: str, repeats: int) -> Any:
    with mocked_aws(monkeypatch):
        _limit_golden_tasks(monkeypatch)
        result = harness.run_sweep(store=_fresh_store(), run_id=run_id, repeats=repeats)
        assert result.unit_tenants and len(result.unit_tenants) == 2 * repeats
        return json.loads(json.dumps(result.matrix.to_report_fields()))


@pytest.mark.parametrize("repeats", [1, 2])
def test_the_state_machine_handlers_publish_the_in_process_matrix(
    monkeypatch: pytest.MonkeyPatch, repeats: int
) -> None:
    run_id, published = _state_machine_matrix(monkeypatch, repeats)
    in_process = _in_process_matrix(monkeypatch, run_id, repeats)
    assert len(published["arms"]) == len(in_process["arms"]) == 2
    scored = [len(arm["task_scores"]) for arm in published["arms"]]
    assert scored == [_HANDLER_TASKS * repeats] * 2
    assert _comparable_matrix(published) == _comparable_matrix(in_process)


def test_the_matrix_comparison_discriminates_between_repeat_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A two-repeat state-machine matrix differs from a one-repeat in-process one for the same
    run."""
    run_id, published = _state_machine_matrix(monkeypatch, 2)
    assert _comparable_matrix(published) != _comparable_matrix(
        _in_process_matrix(monkeypatch, run_id, 1)
    )
