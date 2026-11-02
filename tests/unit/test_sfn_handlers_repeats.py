# SPDX-License-Identifier: MIT
"""`eval.sfn_handlers` over several repeats: one unit tenant per (arm, repeat), every item keyed by
its repeat, and teardown of every unit tenant. Harness: `test_sfn_handlers`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from crm.errors import ValidationFailed
from crm.store.schema import TENANT_SCOPED_TABLES
from eval import sfn_handlers
from eval.harness import DEFAULT_ARM_SPECS
from eval.runs import read_lock, request_cancel, require_run
from eval.sfn_handlers import TASK_REF_KEYS, session_id_for, sweep_task_handler
from eval.tenancy import record_run_started, seed_tenant
from tests.unit.test_sfn_handlers import (
    STARTER,
    TASK_CONFIG,
    Sweep,
    make_sweep,
    mocked_aws,
)

TASKS = 2
REPEATS = 2


@pytest.fixture
def sweep(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sweep]:
    with mocked_aws(monkeypatch):
        yield make_sweep(monkeypatch, tasks=TASKS, repeats=REPEATS)


def _rows(sweep: Sweep, tenant: str) -> list[tuple[str, str]]:
    """Every row under ``tenant``, as ``(table, item)`` pairs, sorted: an identity, not a count."""
    return sorted(
        (table, repr(sorted(item.items())))
        for table in TENANT_SCOPED_TABLES
        for item in sweep.store.query_tenant_scan(table, tenant=tenant)[0]
    )


def _ref_tenant(sweep: Sweep, ref: Mapping[str, Any]) -> str:
    return sweep.unit_tenant(ref["arm_index"], ref["repeat_index"])


def test_start_returns_arm_major_then_repeat_then_task_refs(sweep: Sweep) -> None:
    refs = sweep.control("start")["task_refs"]
    assert all(set(ref) == TASK_REF_KEYS for ref in refs)
    cells = [(ref["arm_index"], ref["repeat_index"]) for ref in refs]
    assert cells == [
        (arm, repeat)
        for arm in range(len(DEFAULT_ARM_SPECS))
        for repeat in range(REPEATS)
        for _task in range(TASKS)
    ]
    assert len({(a, r, ref["task_id"]) for (a, r), ref in zip(cells, refs, strict=True)}) == len(
        refs
    )


def test_seed_records_and_seeds_every_unit_tenant_and_not_the_run_tenant(sweep: Sweep) -> None:
    sweep.control("start")
    sweep.control("seed")
    catalog = sweep.store.get_item("eval_runs", {"run_id": sweep.run_id})
    assert catalog is not None and catalog["unit_tenants"] == list(sweep.unit_tenants)
    assert len(sweep.unit_tenants) == len(DEFAULT_ARM_SPECS) * REPEATS
    assert all(_rows(sweep, tenant) for tenant in sweep.unit_tenants)
    assert _rows(sweep, sweep.tenant) == []


def test_a_full_two_repeat_sweep_runs_every_item_in_its_unit_tenant(sweep: Sweep) -> None:
    final = sweep.drive()
    record = require_run(sweep.store, sweep.run_id)
    expected = len(DEFAULT_ARM_SPECS) * REPEATS * TASKS
    assert final["status"] == "completed"
    assert (record.tasks_total, record.tasks_done) == (expected, expected)
    requests = sweep.runtime.requests
    assert len(requests) == expected
    refs = list(sfn_handlers._sweep_refs(record))
    for request, ref in zip(requests, refs, strict=True):
        claims = request["payload"]["_agent_identity"]["claims"]
        assert claims["custom:tenant"] == sweep.unit_tenant(ref.arm_index, ref.repeat_index)
        assert request["runtimeSessionId"] == session_id_for(
            sweep.run_id, ref.arm_index, ref.task_id, ref.repeat_index
        )
    assert len({request["runtimeSessionId"] for request in requests}) == expected
    rows = [sweep.result_row(ref.arm_index, ref.task_id, ref.repeat_index) for ref in refs]
    assert [row is not None and row["item_status"] for row in rows] == ["done"] * expected
    assert [row is not None and row["repeat_index"] for row in rows] == [
        ref.repeat_index for ref in refs
    ]
    assert sweep.tenant_rows() == 0
    catalog = sweep.store.get_item("eval_runs", {"run_id": sweep.run_id})
    assert catalog is not None and catalog["status"] == "torn_down"
    assert catalog["unit_tenants"] == list(sweep.unit_tenants)
    assert record.matrix_report is not None and len(record.matrix_report["arms"]) == len(
        DEFAULT_ARM_SPECS
    )
    assert read_lock(sweep.store) is None


def test_each_units_writes_land_only_in_its_own_unit_tenant(sweep: Sweep) -> None:
    """Every turn writes one row under the tenant its identity names, as an agent write would; after
    every item ran, each unit tenant holds exactly its own items' rows and no other unit's."""
    original = sweep.runtime.invoke_agent_runtime

    def _writing_turn(**kwargs: Any) -> dict[str, Any]:
        request = json.loads(kwargs["payload"])
        tenant = request["_agent_identity"]["claims"]["custom:tenant"]
        sweep.store.put_item(
            "accounts",
            {
                "account_id": f"acct-{tenant}-written-{kwargs['runtimeSessionId']}",
                "tenant": tenant,
                "name": "written by the turn",
                "name_lower": "written by the turn",
            },
        )
        return original(**kwargs)

    sweep.runtime.invoke_agent_runtime = _writing_turn  # type: ignore[method-assign]
    refs = sweep.control("start")["task_refs"]
    sweep.control("seed")
    for ref in refs:
        sweep.task(ref["arm_index"], ref["task_id"], ref["repeat_index"])
    for tenant in sweep.unit_tenants:
        written = sorted(
            str(item["account_id"]).rsplit("-written-", 1)[1]
            for item in sweep.store.query_tenant_scan("accounts", tenant=tenant)[0]
            if "-written-" in str(item["account_id"])
        )
        assert written == sorted(
            session_id_for(sweep.run_id, ref["arm_index"], ref["task_id"], ref["repeat_index"])
            for ref in refs
            if _ref_tenant(sweep, ref) == tenant
        ), tenant
    assert _rows(sweep, sweep.tenant) == []


@pytest.mark.parametrize(
    ("repeat_index", "match"),
    [(REPEATS, "names no repeat"), (-1, "names no repeat"), (True, "integer"), ("0", "integer")],
)
def test_a_repeat_index_the_run_does_not_have_is_refused_before_the_runtime(
    sweep: Sweep, repeat_index: Any, match: str
) -> None:
    refs = sweep.control("start")["task_refs"]
    event = {
        "run_id": sweep.run_id,
        "arm_index": 0,
        "repeat_index": repeat_index,
        "task_id": refs[0]["task_id"],
    }
    with pytest.raises(ValidationFailed, match=match):
        sweep_task_handler(event, store=sweep.store, runtime=sweep.runtime, config=TASK_CONFIG)
    with pytest.raises(ValidationFailed, match=match):
        sweep.control("item_failed", **{k: v for k, v in event.items() if k != "run_id"})
    assert sweep.runtime.requests == []
    assert require_run(sweep.store, sweep.run_id).tasks_failed == 0


def test_an_item_failure_is_recorded_against_its_own_repeat(sweep: Sweep) -> None:
    refs = sweep.control("start")["task_refs"]
    ref = refs[TASKS]
    sweep.control(
        "item_failed",
        arm_index=ref["arm_index"],
        repeat_index=ref["repeat_index"],
        task_id=ref["task_id"],
        error={"Error": "X", "Cause": ""},
    )
    assert sweep.result_row(ref["arm_index"], ref["task_id"], 0) is None
    row = sweep.result_row(ref["arm_index"], ref["task_id"], ref["repeat_index"])
    assert row is not None and row["item_status"] == "failed" and row["repeat_index"] == 1


def test_a_cancel_mid_sweep_skips_every_remaining_arm_repeat_and_task(sweep: Sweep) -> None:
    def _cancel_after_first(index: int, _ref: Mapping[str, Any]) -> None:
        if index == 1:
            disposition, _ = request_cancel(sweep.store, run_id=sweep.run_id, identity=STARTER)
            assert disposition == "accepted"

    final = sweep.drive(before_item=_cancel_after_first)
    record = require_run(sweep.store, sweep.run_id)
    assert final["status"] == "cancelled"
    assert (record.tasks_done, record.tasks_skipped) == (1, record.tasks_total - 1)
    refs = sfn_handlers._sweep_refs(record)
    statuses = [
        row["item_status"] if row is not None else None
        for row in (sweep.result_row(r.arm_index, r.task_id, r.repeat_index) for r in refs)
    ]
    assert statuses == ["done"] + ["skipped_cancelled"] * (len(refs) - 1)
    assert len(sweep.runtime.requests) == 1
    assert sweep.tenant_rows() == 0


def test_a_run_cancelled_before_start_records_every_item_skipped(sweep: Sweep) -> None:
    request_cancel(sweep.store, run_id=sweep.run_id, identity=STARTER)
    assert sweep.control("start")["cancelled_before_start"] is True
    record = require_run(sweep.store, sweep.run_id)
    refs = sfn_handlers._sweep_refs(record)
    assert record.tasks_skipped == record.tasks_total == len(refs)
    assert all(
        (row := sweep.result_row(r.arm_index, r.task_id, r.repeat_index)) is not None
        and row["item_status"] == "skipped_cancelled"
        for r in refs
    )


def test_teardown_empties_every_unit_tenant_and_one_check_names_each_unit(sweep: Sweep) -> None:
    sweep.control("start")
    sweep.control("seed")
    assert all(_rows(sweep, tenant) for tenant in sweep.unit_tenants)
    assert sweep.control("teardown")["torn_down"] is True
    assert sweep.tenant_rows() == 0
    inventory = json.loads(sweep.objects()["verification-inventory/eval_sweep.json"])
    [writes] = [c for c in inventory["checks"] if c["check"] == "writes_only_under_smoke_tenant"]
    assert writes["status"] == "pass"
    assert all(tenant in writes["detail"] for tenant in sweep.unit_tenants)


def test_teardown_of_a_run_never_seeded_still_tears_down_its_unit_tenants(sweep: Sweep) -> None:
    """With no catalog row, the run's derived unit tenants are torn down; nothing is recorded."""
    sweep.control("start")
    stray = sweep.unit_tenant(1, 1)
    sweep.store.put_item(
        "accounts",
        {"account_id": f"acct-{stray}-x", "tenant": stray, "name": "x", "name_lower": "x"},
    )
    assert sweep.control("teardown")["torn_down"] is True
    assert sweep.tenant_rows() == 0
    assert sweep.store.get_item("eval_runs", {"run_id": sweep.run_id}) is None


def _without_repeats(sweep: Sweep) -> None:
    """Rewrite the job row as one written before runs recorded `repeats`."""
    row = sweep.store.get_item("eval_runs", {"run_id": f"job#{sweep.run_id}"})
    assert row is not None
    sweep.store.put_item("eval_runs", {k: v for k, v in row.items() if k != "repeats"})


def _record_catalog(sweep: Sweep, unit_tenants: object) -> None:
    row: dict[str, object] = {
        "run_id": sweep.run_id,
        "tenant": sweep.tenant,
        "status": "running",
        "started_at": "2026-10-29T00:00:00+00:00",
    }
    if unit_tenants is not None:
        row["unit_tenants"] = unit_tenants
    sweep.store.put_item("eval_runs", row)


@pytest.fixture
def single_repeat(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sweep]:
    with mocked_aws(monkeypatch):
        yield make_sweep(monkeypatch, tasks=TASKS, repeats=1)


def test_a_run_predating_repeats_with_no_unit_tenants_tears_down_its_run_tenant(
    single_repeat: Sweep,
) -> None:
    """A run that predates unit tenants still has its run tenant emptied by teardown."""
    sweep = single_repeat
    sweep.control("start")
    _without_repeats(sweep)
    record_run_started(sweep.store, run_id=sweep.run_id, tenant=sweep.tenant)
    seed_tenant(sweep.store, tenant=sweep.tenant)
    assert _rows(sweep, sweep.tenant)
    assert sweep.control("teardown")["torn_down"] is True
    assert _rows(sweep, sweep.tenant) == []


@pytest.mark.parametrize("recorded", [None, []], ids=["absent", "empty"])
def test_a_run_recording_repeats_refuses_a_catalog_without_its_unit_tenants(
    sweep: Sweep, recorded: object
) -> None:
    """No silent fallback to the run tenant: the unit tenants that may hold rows go unnamed."""
    sweep.control("start")
    sweep.control("seed")
    _record_catalog(sweep, recorded)
    with pytest.raises(ValidationFailed, match="unit tenants"):
        sweep.control("teardown")
    assert all(_rows(sweep, tenant) for tenant in sweep.unit_tenants), "nothing was deleted"


@pytest.mark.parametrize(
    "recorded",
    [["eval-someone-else-a0r0"], "eval-x", [7], "subset", "superset"],
    ids=["foreign", "not-a-list", "not-a-string", "subset", "superset"],
)
def test_teardown_refuses_a_catalog_naming_other_than_the_runs_unit_tenants(
    sweep: Sweep, recorded: object
) -> None:
    sweep.control("start")
    if recorded == "subset":
        recorded = list(sweep.unit_tenants[:-1])
    elif recorded == "superset":
        recorded = [*sweep.unit_tenants, sweep.unit_tenant(0, REPEATS)]
    _record_catalog(sweep, recorded)
    with pytest.raises(ValidationFailed, match="unit tenants"):
        sweep.control("teardown")


def test_teardown_accepts_the_runs_unit_tenants_in_any_order(sweep: Sweep) -> None:
    sweep.control("start")
    sweep.control("seed")
    _record_catalog(sweep, list(reversed(sweep.unit_tenants)))
    assert sweep.control("teardown")["torn_down"] is True
    assert sweep.tenant_rows() == 0


def test_session_ids_keep_repeat_zero_and_differ_per_repeat() -> None:
    assert session_id_for("r" * 32, 1, "gt-01") == session_id_for("r" * 32, 1, "gt-01", 0)
    ids = {session_id_for("r" * 32, arm, "gt-01", repeat) for arm in (0, 1) for repeat in (0, 1, 2)}
    assert len(ids) == 6
