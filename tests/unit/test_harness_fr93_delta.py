# SPDX-License-Identifier: MIT
"""Tests that a sweep publishes a non-vacuous ``demo``-untouched delta.

The delta uses the ``scripts/smoke/demo_tenant_guard.py`` checks and is written to
``<output_dir>/verification-inventory/eval_sweep.json``. Each failure mode is provoked:

* a re-read of the ``demo`` tenant that raises fails the delta,
* a baseline that was never taken fails the delta,
* a sweep that wrote no row at all under its own tenant fails the delta.

``--assert-artifacts`` refuses artifacts published without a passing delta, and the CLI refuses a
``live-aws`` sweep whose delta did not hold whether or not that flag was passed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from scripts.smoke.demo_tenant_guard import WATCHED_TABLES

import eval.harness as harness
from crm.authz import DEMO_TENANT
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.harness import (
    _DEFAULT_GOLDEN_TASKS_PATH,
    FR93_INVENTORY_RELATIVE_PATH,
    ArmSpec,
    ArtifactAssertionError,
    _load_golden_tasks,
    assert_artifacts,
    run_sweep,
)

_ONE_ARM = (ArmSpec(family="anthropic"),)
_TASK_COUNT = len(_load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH))


@pytest.fixture(autouse=True)
def _placeholder_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give ``moto`` a fixed placeholder credential, so no ambient profile is ever consulted."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


class _DemoReadFailingStore(Store):
    """A store whose ``demo``-tenant reads start raising after ``allowed_demo_reads`` of them.

    ``allowed_demo_reads=0`` makes the baseline unreadable; one full watched-table sweep's worth
    makes the baseline succeed and the *re-read* raise. Every non-``demo`` read is untouched, so the
    sweep itself runs normally either way.
    """

    def __init__(self, *, client: Any, allowed_demo_reads: int) -> None:
        super().__init__(client=client)
        self._allowed_demo_reads = allowed_demo_reads
        self.demo_reads = 0

    def query_tenant_scan(
        self,
        table: str,
        *,
        tenant: str,
        limit: int | None = None,
        exclusive_start_key: Mapping[str, object] | None = None,
        scan_forward: bool = True,
    ) -> tuple[list[dict[str, object]], dict[str, object] | None]:
        self._refuse_demo_read_past_allowance(tenant)
        return super().query_tenant_scan(
            table,
            tenant=tenant,
            limit=limit,
            exclusive_start_key=exclusive_start_key,
            scan_forward=scan_forward,
        )

    def count_tenant(self, table: str, *, tenant: str) -> int:
        self._refuse_demo_read_past_allowance(tenant)
        return super().count_tenant(table, tenant=tenant)

    def _refuse_demo_read_past_allowance(self, tenant: str) -> None:
        if tenant == DEMO_TENANT:
            self.demo_reads += 1
            if self.demo_reads > self._allowed_demo_reads:
                raise RuntimeError("demo-tenant read refused by this test's store")


@pytest.fixture
def dynamodb_client() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield client


def _check(result: harness.SweepResult, name: str) -> Any:
    """Return the sweep's published check named ``name``, failing the test if it is absent."""
    for check in result.fr93_checks:
        if check.check == name:
            return check
    raise AssertionError(f"the sweep published no {name!r} check: {result.fr93_checks!r}")


def _noop_seeder(store: Store, *, tenant: str) -> None:
    """A seeder that seeds nothing, so the sweep writes no tenant-scoped row at all."""
    del store, tenant


# --------------------------------------------------------------------------------------
# The positive case: a real sweep publishes both checks, passing
# --------------------------------------------------------------------------------------


def test_a_real_sweep_publishes_both_checks_passing(dynamodb_client: Any, tmp_path: Path) -> None:
    result = run_sweep(
        store=Store(client=dynamodb_client),
        arm_specs=_ONE_ARM,
        output_dir=tmp_path,
        archive_raw=True,
    )
    assert [check.status for check in result.fr93_checks] == ["pass", "pass"]
    assert result.fr93_inventory_path == tmp_path / FR93_INVENTORY_RELATIVE_PATH
    payload = json.loads(result.fr93_inventory_path.read_text(encoding="utf-8"))
    assert payload["layer"] == 6
    assert payload["resource_class"] == "eval_sweep"
    assert {entry["check"] for entry in payload["checks"]} == {
        "demo_tenant_untouched",
        "writes_only_under_smoke_tenant",
    }
    assert payload["provenance"]["region"]
    # The inventory carries no AWS account id.
    assert re.search(r"\b\d{12}\b", json.dumps(payload)) is None


def test_the_writes_check_is_non_vacuous_because_it_runs_before_teardown(
    dynamodb_client: Any,
) -> None:
    """The writes check records a non-zero row count taken before teardown deleted the tenant."""
    result = run_sweep(store=Store(client=dynamodb_client), arm_specs=_ONE_ARM)
    writes = _check(result, "writes_only_under_smoke_tenant")
    assert writes.status == "pass"
    assert " 0 row(s)" not in writes.detail
    # The tenant is gone by the time the sweep returns.
    rows, _ = Store(client=dynamodb_client).query_tenant_scan("accounts", tenant=result.tenant)
    assert rows == []


def test_the_inventory_is_not_written_when_there_is_nowhere_to_write_it(
    dynamodb_client: Any,
) -> None:
    result = run_sweep(store=Store(client=dynamodb_client), arm_specs=_ONE_ARM)
    assert result.fr93_inventory_path is None
    assert [check.status for check in result.fr93_checks] == ["pass", "pass"]


# --------------------------------------------------------------------------------------
# Trap 1: a re-read that raises. Trap 2: a baseline that was never taken.
# --------------------------------------------------------------------------------------


def test_a_raising_demo_re_read_fails_the_delta(dynamodb_client: Any) -> None:
    store = _DemoReadFailingStore(client=dynamodb_client, allowed_demo_reads=len(WATCHED_TABLES))
    result = run_sweep(store=store, arm_specs=_ONE_ARM)
    demo = _check(result, "demo_tenant_untouched")
    assert demo.status == "fail"
    assert "could not re-read" in demo.detail
    assert "RuntimeError" in demo.detail


def test_a_missing_baseline_fails_the_delta(dynamodb_client: Any) -> None:
    store = _DemoReadFailingStore(client=dynamodb_client, allowed_demo_reads=0)
    result = run_sweep(store=store, arm_specs=_ONE_ARM)
    demo = _check(result, "demo_tenant_untouched")
    assert demo.status == "fail"
    assert "never read" in demo.detail


# --------------------------------------------------------------------------------------
# Trap 3: a sweep that wrote nothing cannot pass vacuously.
# --------------------------------------------------------------------------------------


def test_a_sweep_that_wrote_no_row_fails_the_delta(
    dynamodb_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("eval.tenancy._default_seeder", lambda: _noop_seeder)
    result = run_sweep(store=Store(client=dynamodb_client), arm_specs=_ONE_ARM)
    writes = _check(result, "writes_only_under_smoke_tenant")
    assert writes.status == "fail"
    assert "committed no row" in writes.detail


# --------------------------------------------------------------------------------------
# --assert-artifacts refuses artifacts published without a passing delta
# --------------------------------------------------------------------------------------


def _assert_artifacts(result: harness.SweepResult, output_dir: Path) -> None:
    assert_artifacts(
        output_dir=output_dir,
        expected_arm_count=len(result.arm_runs),
        expected_task_count=_TASK_COUNT,
        raw_archive_paths=result.raw_archive_paths,
    )


def _sweep_with_artifacts(client: Any, output_dir: Path) -> harness.SweepResult:
    return run_sweep(
        store=Store(client=client), arm_specs=_ONE_ARM, output_dir=output_dir, archive_raw=True
    )


def test_assert_artifacts_accepts_a_real_sweeps_published_delta(
    dynamodb_client: Any, tmp_path: Path
) -> None:
    _assert_artifacts(_sweep_with_artifacts(dynamodb_client, tmp_path), tmp_path)


def test_assert_artifacts_fails_when_the_delta_inventory_is_absent(
    dynamodb_client: Any, tmp_path: Path
) -> None:
    result = _sweep_with_artifacts(dynamodb_client, tmp_path)
    (tmp_path / FR93_INVENTORY_RELATIVE_PATH).unlink()
    with pytest.raises(ArtifactAssertionError) as caught:
        _assert_artifacts(result, tmp_path)
    assert any("no demo-untouched delta" in failure for failure in caught.value.failures)


@pytest.mark.parametrize("check_name", ["demo_tenant_untouched", "writes_only_under_smoke_tenant"])
def test_assert_artifacts_fails_when_either_check_is_not_pass(
    dynamodb_client: Any, tmp_path: Path, check_name: str
) -> None:
    result = _sweep_with_artifacts(dynamodb_client, tmp_path)
    path = tmp_path / FR93_INVENTORY_RELATIVE_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    for entry in payload["checks"]:
        if entry["check"] == check_name:
            entry["status"] = "fail"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactAssertionError) as caught:
        _assert_artifacts(result, tmp_path)
    assert any(check_name in failure and "'fail'" in failure for failure in caught.value.failures)


@pytest.mark.parametrize("check_name", ["demo_tenant_untouched", "writes_only_under_smoke_tenant"])
def test_assert_artifacts_fails_when_either_check_is_missing_entirely(
    dynamodb_client: Any, tmp_path: Path, check_name: str
) -> None:
    result = _sweep_with_artifacts(dynamodb_client, tmp_path)
    path = tmp_path / FR93_INVENTORY_RELATIVE_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["checks"] = [entry for entry in payload["checks"] if entry["check"] != check_name]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactAssertionError) as caught:
        _assert_artifacts(result, tmp_path)
    assert any(f"no {check_name!r} check" in failure for failure in caught.value.failures)


def test_assert_artifacts_fails_on_an_unparseable_delta_inventory(
    dynamodb_client: Any, tmp_path: Path
) -> None:
    result = _sweep_with_artifacts(dynamodb_client, tmp_path)
    (tmp_path / FR93_INVENTORY_RELATIVE_PATH).write_text("{not json", encoding="utf-8")
    with pytest.raises(ArtifactAssertionError) as caught:
        _assert_artifacts(result, tmp_path)
    assert any("not parseable JSON" in failure for failure in caught.value.failures)


# --------------------------------------------------------------------------------------
# The CLI refuses a live-aws sweep whose delta did not hold, flag or no flag
# --------------------------------------------------------------------------------------


def _stub_sweep_result(*statuses: str) -> harness.SweepResult:
    """Build a SweepResult carrying only the delta checks a CLI test needs to react to."""
    from scripts.smoke.harness import CheckResult

    from eval.matrix import build_matrix_result

    return harness.SweepResult(
        run_id="run",
        tenant="eval-run",
        mode="stub",
        matrix=build_matrix_result([]),
        fr93_checks=tuple(
            CheckResult(
                resource="eval_sweep",
                check=name,
                status=status,  # type: ignore[arg-type]
                detail="synthesised for this test",
            )
            for name, status in zip(harness._FR93_REQUIRED_CHECKS, statuses, strict=True)
        ),
    )


@pytest.mark.parametrize(
    ("statuses", "expected_exit"),
    [(("pass", "pass"), 0), (("pass", "fail"), 1), (("fail", "pass"), 1)],
)
def test_main_refuses_a_live_aws_sweep_whose_delta_did_not_hold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    statuses: tuple[str, str],
    expected_exit: int,
) -> None:
    monkeypatch.setenv("EVAL_SUBSTRATE", "live-aws")
    monkeypatch.setattr(harness, "_build_default_store", lambda **_: object())
    monkeypatch.setattr(harness, "run_sweep", lambda **_: _stub_sweep_result(*statuses))
    assert harness.main(["--output-dir", str(tmp_path)]) == expected_exit
