# SPDX-License-Identifier: MIT
"""Unit tests for the orphan reaper's two failure kinds, against a ``moto``-backed store.

* a **teardown failure** is per-tenant: the tenant remains and its catalog row stays ``running``;
* a **``canary`` violation** is global: the tenant being reaped was torn down, so its row goes
  terminal and the reaper loop continues.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
import scripts.reap_eval_tenants as reaper
from moto import mock_aws
from scripts import proc_run

from crm.config import DEFAULT_REGION
from crm.store import keys
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.tenancy import (
    CANARY_TENANT,
    RUN_STATUS_RUNNING,
    RUN_STATUS_TORN_DOWN,
    CanaryTenantNotEmptyError,
    OrphanedRun,
    TenantTeardownError,
    reap_tenant,
    tenant_for_run,
)


@pytest.fixture
def dynamodb_client() -> Iterator[Any]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: Any) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _seed_orphan(store: Store, *, run_id: str, hours_ago: int = 2) -> OrphanedRun:
    """Record a stale ``running`` catalog row plus one row under its tenant, and return the orphan."""
    tenant = tenant_for_run(run_id)
    started_at = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    store.put_item(
        "eval_runs",
        {
            "run_id": run_id,
            "tenant": tenant,
            "status": RUN_STATUS_RUNNING,
            "started_at": started_at,
        },
    )
    store.put_item(
        "opportunities",
        {
            "opportunity_id": f"opp-{run_id}",
            "tenant": tenant,
            "account_id": "acct-1",
            "owner_rep_id": "rep-1",
            "name": f"Orphan {run_id}",
            "name_lower": f"orphan {run_id}",
            "stage": "Discovery",
            "close_date": "2026-12-31",
            "created_at": started_at,
        },
    )
    return OrphanedRun(
        run_id=run_id, tenant=tenant, status=RUN_STATUS_RUNNING, started_at=started_at
    )


def _plant_canary_row(store: Store, *, account_id: str = "canary-leak-0001") -> str:
    """Write one row under ``canary``, the way a run with a tenant bug would."""
    store.put_item(
        "accounts",
        {
            "account_id": account_id,
            "tenant": CANARY_TENANT,
            "name": "Leaked Account",
            "name_lower": "leaked account",
            "owner_rep_id": "rep-1",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    return account_id


def _row_status(store: Store, run_id: str) -> str:
    row = store.get_item("eval_runs", {"run_id": run_id})
    assert row is not None, f"no catalog row for {run_id!r}"
    return str(row["status"])


def _tenant_row_count(store: Store, tenant: str) -> int:
    items, _ = store.query_tenant_scan("opportunities", tenant=tenant)
    return len(items)


# -- reap_tenant: a canary violation must not strand the catalog row ------------------------------


def test_reap_tenant_makes_the_row_terminal_even_when_the_canary_is_violated(store: Store) -> None:
    """On a canary violation the tenant is deleted and its catalog row goes terminal."""
    orphan = _seed_orphan(store, run_id="run-a")
    _plant_canary_row(store)

    with pytest.raises(CanaryTenantNotEmptyError) as excinfo:
        reap_tenant(store, run=orphan)

    assert _row_status(store, "run-a") == RUN_STATUS_TORN_DOWN
    assert _tenant_row_count(store, orphan.tenant) == 0, "the tenant really was torn down"
    assert excinfo.value.deleted is not None, "the caller needs the counts it did remove"
    assert excinfo.value.deleted["opportunities"] == 1


def test_a_teardown_failure_still_leaves_the_row_running(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a teardown failure the catalog row stays `running`."""
    orphan = _seed_orphan(store, run_id="run-b")

    def _raise(_tenant: str) -> dict[str, int]:
        raise RuntimeError("simulated outage during teardown")

    monkeypatch.setattr(store, "delete_tenant", _raise)

    with pytest.raises(TenantTeardownError):
        reap_tenant(store, run=orphan)

    assert _row_status(store, "run-b") == RUN_STATUS_RUNNING


def test_reaping_a_clean_orphan_is_unchanged(store: Store) -> None:
    """Reaping a clean orphan deletes its rows and makes its row terminal."""
    orphan = _seed_orphan(store, run_id="run-c")

    deleted = reap_tenant(store, run=orphan)

    assert deleted["opportunities"] == 1
    assert _row_status(store, "run-c") == RUN_STATUS_TORN_DOWN


# -- the reaper loop: one violation must not abort the rest ----------------------------------------


def _run_reaper(store: Store, monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    """Drive the real `main`, with only the store construction replaced (no AWS, no credential)."""
    monkeypatch.setattr(reaper, "_build_store", lambda **_kwargs: store)
    return reaper.main(["--older-than", "1h", "--yes", *args])


def test_a_canary_violation_on_the_first_orphan_still_reaps_the_second(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A canary violation surfacing on the first orphan does not stop the second being reaped."""
    first = _seed_orphan(store, run_id="run-first", hours_ago=3)
    second = _seed_orphan(store, run_id="run-second", hours_ago=2)
    _plant_canary_row(store)

    exit_code = _run_reaper(store, monkeypatch)
    captured = capsys.readouterr()

    assert exit_code == 1, "a canary violation must fail the run"
    # Both tenants are torn down, and both catalog rows are terminal.
    assert _tenant_row_count(store, first.tenant) == 0
    assert _tenant_row_count(store, second.tenant) == 0
    assert _row_status(store, "run-first") == RUN_STATUS_TORN_DOWN
    assert _row_status(store, "run-second") == RUN_STATUS_TORN_DOWN
    # Both reaps are reported, and the violation names the offending record.
    assert "Reaped eval-run-first" in captured.out
    assert "Reaped eval-run-second" in captured.out
    assert "canary-leak-0001" in captured.err


def test_the_canary_violation_is_reported_once_not_once_per_orphan(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The canary violation is reported once regardless of how many orphans are reaped."""
    for index in range(3):
        _seed_orphan(store, run_id=f"run-{index}", hours_ago=2 + index)
    _plant_canary_row(store)

    _run_reaper(store, monkeypatch)
    captured = capsys.readouterr()

    assert captured.err.count("FAIL canary tenant not empty") == 1
    assert captured.out.count("Reaped ") == 3


def test_a_second_reaper_run_after_a_violation_finds_nothing_to_reap(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second reaper run after a violation finds nothing to reap and exits zero."""
    _seed_orphan(store, run_id="run-once", hours_ago=2)
    _plant_canary_row(store)

    first_exit = _run_reaper(store, monkeypatch)
    capsys.readouterr()
    second_exit = _run_reaper(store, monkeypatch)
    second = capsys.readouterr()

    assert first_exit == 1
    assert second_exit == 0, "the violated run must not be re-reaped; its row is terminal"
    assert "Reaped " not in second.out
    assert "run-once" not in second.out


def test_a_teardown_failure_and_a_canary_violation_are_reported_distinctly(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A teardown failure and a canary violation produce distinct messages and row outcomes."""
    _seed_orphan(store, run_id="run-doomed", hours_ago=3)
    _seed_orphan(store, run_id="run-ok", hours_ago=2)
    _plant_canary_row(store)
    real_delete = store.delete_tenant

    def _fail_the_first_tenant_only(tenant: str) -> dict[str, int]:
        if tenant == tenant_for_run("run-doomed"):
            raise RuntimeError("simulated outage during teardown")
        return real_delete(tenant)

    monkeypatch.setattr(store, "delete_tenant", _fail_the_first_tenant_only)

    exit_code = _run_reaper(store, monkeypatch)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "FAIL reaping eval-run-doomed" in captured.err
    assert "FAIL canary tenant not empty" in captured.err
    assert _row_status(store, "run-doomed") == RUN_STATUS_RUNNING, "still there, still reapable"
    assert _row_status(store, "run-ok") == RUN_STATUS_TORN_DOWN


def test_a_clean_reaper_run_exits_zero(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A reap with no canary violation exits zero."""
    _seed_orphan(store, run_id="run-clean", hours_ago=2)

    exit_code = _run_reaper(store, monkeypatch)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Reaped eval-run-clean" in captured.out
    assert "canary" not in captured.err
    assert _row_status(store, "run-clean") == RUN_STATUS_TORN_DOWN


def test_a_dry_run_reaps_nothing_even_with_a_canary_violation(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run deletes nothing and exits zero even with a canary row present."""
    orphan = _seed_orphan(store, run_id="run-dry", hours_ago=2)
    _plant_canary_row(store)
    monkeypatch.setattr(reaper, "_build_store", lambda **_kwargs: store)

    exit_code = reaper.main(["--older-than", "1h"])

    assert exit_code == 0
    assert _tenant_row_count(store, orphan.tenant) == 1
    assert _row_status(store, "run-dry") == RUN_STATUS_RUNNING


def test_the_reaper_catches_both_exception_types_from_its_loop() -> None:
    """`main` catches both `CanaryTenantNotEmptyError` and `TenantTeardownError` and continues."""
    import inspect

    source = inspect.getsource(reaper.main)

    assert "except CanaryTenantNotEmptyError" in source
    assert "except TenantTeardownError" in source
    assert "continue" in source


def test_the_planted_canary_row_is_what_makes_these_tests_non_vacuous(store: Store) -> None:
    """`_plant_canary_row` makes `find_canary_records` return the planted record."""
    from eval.tenancy import find_canary_records

    assert find_canary_records(store) == []
    _plant_canary_row(store)
    assert [record.key_value for record in find_canary_records(store)] == ["canary-leak-0001"]
    assert keys.tenant_prefixed(CANARY_TENANT, "x").startswith(CANARY_TENANT)


class TestADestructiveRunRequiresANamedTarget:
    """`--yes` requires a verified target via `--outputs`; a dry run is not gated."""

    def test_yes_without_outputs_refuses_and_names_the_target(self) -> None:
        with pytest.raises(reaper.UnverifiedTargetError) as caught:
            reaper._build_store(
                profile="someone-elses",
                region="us-west-2",
                table_prefix="p",
                destructive=True,
            )
        message = str(caught.value)
        assert "someone-elses" in message and "us-west-2" in message, (
            "the refusal must name the target it was about to act on"
        )
        assert "Nothing has been deleted" in message
        assert "--outputs" in message, "the refusal must name the flag that fixes it"

    def test_a_dry_run_is_not_gated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A dry run builds its store without an outputs file."""
        built: list[object] = []

        class _Session:
            def client(self, _service: str) -> object:
                built.append(_service)
                return object()

        # Patch `boto3.session.Session` on the module `reaper` imports, not `reaper.boto3` — mypy
        # rejects reaching through another module's non-exported import.
        monkeypatch.setattr(boto3, "Session", lambda **_kwargs: _Session())
        monkeypatch.setattr(reaper, "Store", lambda **_kwargs: "store")
        reaper._build_store(profile=None, region="us-east-1", table_prefix="p", destructive=False)
        assert built == ["dynamodb"], "a dry run must still build its store"

    def test_main_reports_the_refusal_as_exit_one(self) -> None:
        """`main` returns 1 when `--yes` is given without `--outputs`."""
        assert reaper.main(["--older-than", "24h", "--yes", "--region", "us-west-2"]) == 1

    def test_the_cli_exposes_outputs(self) -> None:
        """The parser's option strings include `--outputs`."""
        options = {
            option
            for action in reaper.build_arg_parser()._actions
            for option in action.option_strings
        }
        assert "--outputs" in options, sorted(options)


_REAPER_SCRIPT = Path(reaper.__file__).resolve()


def test_path_form_invocation_imports_scripts_smoke_without_pythonpath(tmp_path: Path) -> None:
    """The documented ``python3 scripts/reap_eval_tenants.py ... --yes`` resolves ``scripts.smoke``.

    ``--yes`` with an absent outputs file reaches the deferred ``scripts.smoke.harness`` import and
    then fails on the missing file, before any session or AWS call is made.
    """
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    missing_outputs = tmp_path / "absent-tf-outputs.json"
    completed = proc_run.run(
        [
            sys.executable,
            str(_REAPER_SCRIPT),
            "--older-than",
            "24h",
            "--yes",
            "--outputs",
            str(missing_outputs),
            "--region",
            "us-east-1",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "ModuleNotFoundError" not in completed.stderr, completed.stderr
    assert f"Terraform outputs file not found: {missing_outputs}" in completed.stderr, (
        completed.stderr
    )
    assert completed.returncode != 0


@pytest.mark.parametrize(
    ("argv_region", "env_region", "expected"),
    [
        (["--region", "us-west-2"], "us-east-2", "us-west-2"),
        ([], "us-west-2", "us-west-2"),
        ([], None, DEFAULT_REGION),
    ],
    ids=["flag", "environment", "default"],
)
def test_main_resolves_the_region_as_flag_then_aws_region_then_the_default(
    monkeypatch: pytest.MonkeyPatch,
    argv_region: list[str],
    env_region: str | None,
    expected: str,
) -> None:
    """`main` hands the store builder the flag, else `AWS_REGION`, else `DEFAULT_REGION`."""
    if env_region is None:
        monkeypatch.delenv("AWS_REGION", raising=False)
    else:
        monkeypatch.setenv("AWS_REGION", env_region)
    seen: list[str] = []

    def _capture(**kwargs: Any) -> Store:
        seen.append(kwargs["region"])
        raise reaper.UnverifiedTargetError("stop")

    monkeypatch.setattr(reaper, "_build_store", _capture)
    assert reaper.main(["--older-than", "24h", *argv_region]) == 1
    assert seen == [expected]
