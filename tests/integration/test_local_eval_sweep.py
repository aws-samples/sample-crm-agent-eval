# SPDX-License-Identifier: MIT
"""One full stub eval sweep end to end against DynamoDB Local.

Seeding runs through the production `fixtures.generate.seed_tenant` via `eval.tenancy.seed_tenant`'s
default resolution path.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fixtures.generate import generate_dataset, seed_tenant
from scripts import proc_run

from crm.authz import DEMO_TENANT
from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLE_NAMES
from crm.store.store import Store
from eval.harness import DEFAULT_ARM_SPECS, partial_manifest_path, run_sweep
from eval.matrix import RECORD_SHAPE_VERSION, RECORD_SHAPE_VERSION_KEY
from eval.reprice import PRE_AGGREGATION_DIRNAME
from eval.sweep_units import UNIT_REPEAT_INDEX_KEY, UNIT_TASK_ID_KEY
from eval.tenancy import (
    RUN_STATUS_TORN_DOWN,
    TenantTeardownError,
    teardown_tenant,
    unit_tenants_for_run,
)

#: Unique per process, so concurrent xdist workers never share a table.
_TABLE_PREFIX = f"sweep-{uuid.uuid4().hex[:12]}-"


@pytest.fixture
def sweep_store(dynamodb_local_client: Any) -> Iterator[Store]:
    """A prefixed :class:`Store` against DynamoDB Local, dedicated to this module's own tables.

    Every table this fixture creates is dropped on teardown.
    """
    import contextlib

    create_all_tables(dynamodb_local_client, table_prefix=_TABLE_PREFIX)
    try:
        yield Store(table_prefix=_TABLE_PREFIX, client=dynamodb_local_client)
    finally:
        for table in TABLE_NAMES:
            with contextlib.suppress(dynamodb_local_client.exceptions.ResourceNotFoundException):
                dynamodb_local_client.delete_table(TableName=f"{_TABLE_PREFIX}{table}")


# -- The full stub sweep, against the real engine -------------------------------------------------


def test_a_full_stub_sweep_runs_end_to_end_against_the_real_dynamodb_engine(
    sweep_store: Store, tmp_path: Path
) -> None:
    """A default `run_sweep` allocates a fresh `eval-` tenant, scores every task per arm with no bad
    writes, writes the json/md/csv and raw artifacts, and leaves no rows for its tenant.
    """
    result = run_sweep(
        store=sweep_store,
        mode="stub",
        output_dir=tmp_path,
        archive_raw=True,
    )

    assert result.mode == "stub"
    assert result.tenant != DEMO_TENANT
    assert result.tenant.startswith("eval-")
    assert len(result.arm_runs) == len(DEFAULT_ARM_SPECS)

    for arm_run in result.arm_runs:
        assert len(arm_run.task_scores) > 0
        for score in arm_run.task_scores:
            assert score.bad_write is False, (
                f"{score.task_id} was flagged as a bad write in a clean stub sweep"
            )
        assert arm_run.arm_result.aggregate_score_fields["bad_write_rate"] == 0.0

    # The three report artifacts were written.
    assert set(result.artifact_paths) == {"json", "md", "csv"}
    for path in result.artifact_paths.values():
        assert path.exists()
        assert path.stat().st_size > 0
    assert len(result.raw_archive_paths) == len(DEFAULT_ARM_SPECS)
    for raw_path in result.raw_archive_paths:
        assert raw_path.exists()

    post_teardown_items, _ = sweep_store.query_tenant_scan("accounts", tenant=result.tenant)
    assert post_teardown_items == []
    post_teardown_opps, _ = sweep_store.query_tenant_scan("opportunities", tenant=result.tenant)
    assert post_teardown_opps == []


def test_every_archived_call_names_its_task_and_repeat_on_the_real_engine(
    sweep_store: Store, tmp_path: Path
) -> None:
    """With `--archive-raw` and two repeats, every pre-aggregation invocation and tool-call entry
    names a task the arm scored and a repeat below the repeat count, and every (task, repeat) cell
    is present.
    """
    repeats = 2
    result = run_sweep(
        store=sweep_store,
        mode="stub",
        output_dir=tmp_path,
        archive_raw=True,
        repeats=repeats,
    )
    archives = sorted((tmp_path / "raw" / PRE_AGGREGATION_DIRNAME).glob("*.json"))
    assert len(archives) == len(DEFAULT_ARM_SPECS)
    for arm_run, archive in zip(result.arm_runs, archives, strict=True):
        payload = json.loads(archive.read_text(encoding="utf-8"))
        assert payload[RECORD_SHAPE_VERSION_KEY] == RECORD_SHAPE_VERSION
        task_ids = {score.task_id for score in arm_run.task_scores}
        cells = {(task_id, repeat) for task_id in task_ids for repeat in range(repeats)}
        invocation_cells = set()
        for key in ("invocation_attributions", "tool_call_attributions"):
            entries = payload[key]
            assert entries, (archive.name, key)
            for entry in entries:
                cell = (entry[UNIT_TASK_ID_KEY], entry[UNIT_REPEAT_INDEX_KEY])
                assert cell in cells, (archive.name, key, cell)
                if key == "invocation_attributions":
                    invocation_cells.add(cell)
        assert invocation_cells == cells, archive.name


def test_the_demo_tenant_is_never_written_or_touched_by_a_sweep(
    sweep_store: Store, tmp_path: Path
) -> None:
    """A sweep run beside a seeded `demo` tenant leaves every `demo` account unchanged."""
    seed_tenant(sweep_store, tenant=DEMO_TENANT)
    demo_corpus = generate_dataset(tenant=DEMO_TENANT)
    demo_account_id = str(demo_corpus.accounts[0]["account_id"])
    demo_account_before = sweep_store.get_item("accounts", {"account_id": demo_account_id})
    assert demo_account_before is not None

    result = run_sweep(store=sweep_store, mode="stub", output_dir=tmp_path)
    assert result.tenant != DEMO_TENANT

    demo_account_after = sweep_store.get_item("accounts", {"account_id": demo_account_id})
    assert demo_account_after == demo_account_before

    demo_items_still_present, _ = sweep_store.query_tenant_scan("accounts", tenant=DEMO_TENANT)
    assert len(demo_items_still_present) == len(demo_corpus.accounts)


def test_a_teardown_failure_is_reported_not_swallowed_on_the_real_engine(
    sweep_store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `Store.delete_tenant` failure makes `run_sweep` raise `TenantTeardownError`."""

    def _boom(tenant: str) -> dict[str, int]:
        raise RuntimeError("simulated teardown failure against the real engine")

    monkeypatch.setattr(sweep_store, "delete_tenant", _boom)

    with pytest.raises(TenantTeardownError):
        run_sweep(
            store=sweep_store, mode="stub", output_dir=tmp_path, arm_specs=(DEFAULT_ARM_SPECS[0],)
        )


def test_teardown_tenant_directly_removes_only_the_named_tenants_rows(
    sweep_store: Store,
) -> None:
    """`teardown_tenant` deletes the named tenant's rows and leaves a second tenant's rows intact."""
    tenant_a = "eval-teardown-real-a"
    tenant_b = "eval-teardown-real-b"
    seed_tenant(sweep_store, tenant=tenant_a)
    seed_tenant(sweep_store, tenant=tenant_b)
    corpus_b = generate_dataset(tenant=tenant_b)

    teardown_tenant(sweep_store, tenant=tenant_a)

    remaining_a, _ = sweep_store.query_tenant_scan("accounts", tenant=tenant_a)
    remaining_b, _ = sweep_store.query_tenant_scan("accounts", tenant=tenant_b)
    assert remaining_a == []
    assert len(remaining_b) == len(corpus_b.accounts)


# -- A polite kill mid-sweep orphans no tenant ----------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
#: Bounds the wait for the first unit to finish and for the killed process to exit.
_SIGNAL_TEST_DEADLINE_SECONDS = 240.0


@pytest.mark.parametrize(
    ("signum", "code"), [(signal.SIGTERM, 143), (signal.SIGHUP, 129)], ids=["SIGTERM", "SIGHUP"]
)
def test_a_signal_mid_sweep_tears_every_unit_tenant_down_on_the_real_engine(
    sweep_store: Store, dynamodb_local_client: Any, tmp_path: Path, signum: int, code: int
) -> None:
    """A real CLI process signalled after its first unit finished exits with the signal's code,
    marks its catalog row torn down, leaves no row under any unit tenant, and writes partial
    results instead of a matrix."""
    run_id = uuid.uuid4().hex
    output_dir = tmp_path / "out"
    first_unit = output_dir / "partial" / "units" / run_id / "arm-0-repeat-0.json"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
    environment["PYTHONPATH"] = os.pathsep.join([str(_REPO_ROOT / "src"), str(_REPO_ROOT)])
    command = [
        sys.executable,
        "-m",
        "eval.harness",
        "--endpoint-url",
        str(dynamodb_local_client.meta.endpoint_url),
        "--table-prefix",
        _TABLE_PREFIX,
        "--output-dir",
        str(output_dir),
        "--run-id",
        run_id,
    ]
    process = proc_run.popen(
        command,
        cwd=_REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + _SIGNAL_TEST_DEADLINE_SECONDS
        while not first_unit.exists():
            assert process.poll() is None, f"the sweep exited early: {process.communicate()}"
            assert time.monotonic() < deadline, "the first unit never finished"
            time.sleep(0.2)
        process.send_signal(signum)
        _stdout, stderr = process.communicate(timeout=_SIGNAL_TEST_DEADLINE_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == code, stderr
    assert "Traceback" not in stderr
    row = sweep_store.get_item("eval_runs", {"run_id": run_id}, consistent=True)
    assert row is not None and row["status"] == RUN_STATUS_TORN_DOWN, row
    for unit_tenant in unit_tenants_for_run(run_id, arm_count=len(DEFAULT_ARM_SPECS), repeats=1):
        for table in ("accounts", "opportunities", "contacts"):
            remaining, _ = sweep_store.query_tenant_scan(table, tenant=unit_tenant)
            assert remaining == [], (unit_tenant, table)
    manifest = json.loads(partial_manifest_path(output_dir).read_text(encoding="utf-8"))
    assert manifest["run_id"] == run_id
    assert manifest["reason"] == f"signal:{signal.Signals(signum).name}"
    assert manifest["teardown"] == "ok"
    assert [unit["arm_index"] for unit in manifest["completed_units"]] == [0]
    assert not (output_dir / "matrix.md").exists()
