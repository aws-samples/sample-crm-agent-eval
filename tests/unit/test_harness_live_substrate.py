# SPDX-License-Identifier: MIT
"""Tests for the stub sweep's artifact assertion, the ``live-aws`` substrate, and the run catalog.

* ``--assert-artifacts`` reads all four artifacts back and fails on each one's absence or
  malformation.
* the ``live-aws`` substrate dispatches every tool call to the deployed action Lambda on the
  ``_agent_identity`` channel, never in-process.
* a sweep records its own ``eval_runs`` catalog row, so a run that dies before teardown is
  discoverable by ``scripts/reap_eval_tenants.py``.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

import eval.harness as harness
from agents.registry import tool_registry_for
from agents.supervisor import SUPERVISOR_ROLE
from crm.envelope import AGENT_IDENTITY_KEY, IDENTITY_KEY
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.harness import (
    ArtifactAssertionError,
    assert_artifacts,
    live_lambda_dispatch_factory,
    run_sweep,
)
from eval.tenancy import (
    RUN_STATUS_RUNNING,
    RUN_STATUS_TORN_DOWN,
    TenantTeardownError,
    find_orphaned_runs,
    tenant_for_run,
)

#: A zero age threshold, so every non-terminal recorded run counts as an orphan.
_ZERO_THRESHOLD = timedelta(seconds=0)

_ARM_COUNT = 2
#: The packaged golden set's task count.
_TASK_COUNT = len(harness._load_golden_tasks(harness._DEFAULT_GOLDEN_TASKS_PATH))


@pytest.fixture(autouse=True)
def _placeholder_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give ``moto`` a fixed placeholder credential, so no ambient profile is ever consulted."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def _sweep_into(output_dir: Path) -> harness.SweepResult:
    """Run one real stub sweep against a ``moto``-backed store, writing all four artifacts."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        return run_sweep(store=Store(client=client), output_dir=output_dir, archive_raw=True)


def _assert(result: harness.SweepResult, output_dir: Path) -> None:
    assert_artifacts(
        output_dir=output_dir,
        expected_arm_count=_ARM_COUNT,
        expected_task_count=_TASK_COUNT,
        raw_archive_paths=result.raw_archive_paths,
    )


def test_assert_artifacts_passes_on_a_real_sweeps_four_artifacts(tmp_path: Path) -> None:
    """A real sweep's own output satisfies every check."""
    result = _sweep_into(tmp_path)
    _assert(result, tmp_path)
    assert (tmp_path / "raw").is_dir()
    assert len(result.raw_archive_paths) == _ARM_COUNT


@pytest.mark.parametrize(
    "artifact",
    ["matrix.json", "matrix.md", "matrix.csv"],
)
def test_assert_artifacts_fails_when_any_single_file_is_missing(
    tmp_path: Path, artifact: str
) -> None:
    """Deleting any one of the three matrix files fails the check."""
    result = _sweep_into(tmp_path)
    (tmp_path / artifact).unlink()
    with pytest.raises(ArtifactAssertionError) as caught:
        _assert(result, tmp_path)
    assert any(artifact in failure for failure in caught.value.failures)


def test_assert_artifacts_fails_when_the_raw_archive_directory_is_absent(tmp_path: Path) -> None:
    """An absent raw archive directory fails the check."""
    result = _sweep_into(tmp_path)
    shutil.rmtree(tmp_path / "raw")
    with pytest.raises(ArtifactAssertionError) as caught:
        _assert(result, tmp_path)
    assert any("raw" in failure for failure in caught.value.failures)


def test_assert_artifacts_fails_on_a_rendered_verdict_word(tmp_path: Path) -> None:
    """An artifact that renders a pass/fail verdict or names a winner is refused."""
    result = _sweep_into(tmp_path)
    md = tmp_path / "matrix.md"
    md.write_text(md.read_text(encoding="utf-8") + "\nanthropic wins.\n", encoding="utf-8")
    with pytest.raises(ArtifactAssertionError) as caught:
        _assert(result, tmp_path)
    assert any(
        "wins" in failure and "forbidden verdict word" in failure
        for failure in caught.value.failures
    )


def test_assert_artifacts_fails_when_an_arm_accounts_for_the_wrong_task_count(
    tmp_path: Path,
) -> None:
    """An arm accounting for the wrong task count fails the check."""
    result = _sweep_into(tmp_path)
    with pytest.raises(ArtifactAssertionError) as caught:
        assert_artifacts(
            output_dir=tmp_path,
            expected_arm_count=_ARM_COUNT,
            expected_task_count=_TASK_COUNT + 1,
            raw_archive_paths=result.raw_archive_paths,
        )
    assert any("accounts for" in failure for failure in caught.value.failures)


def test_assert_artifacts_fails_when_matrix_json_is_unparseable(tmp_path: Path) -> None:
    """An unparseable ``matrix.json`` fails the check."""
    result = _sweep_into(tmp_path)
    (tmp_path / "matrix.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ArtifactAssertionError) as caught:
        _assert(result, tmp_path)
    assert any("not parseable JSON" in failure for failure in caught.value.failures)


def test_resolve_substrate_prefers_flag_then_environment_then_local() -> None:
    """The substrate resolves from the flag, then the environment, then the local default."""
    assert harness._resolve_substrate(None, {}) == "local"
    assert harness._resolve_substrate(None, {"EVAL_SUBSTRATE": "live-aws"}) == "live-aws"
    assert harness._resolve_substrate("local", {"EVAL_SUBSTRATE": "live-aws"}) == "local"
    assert harness._resolve_substrate("live-aws", {}) == "live-aws"


def test_resolve_substrate_refuses_an_unrecognised_value_rather_than_defaulting() -> None:
    """An unrecognised substrate value is refused rather than defaulted."""
    with pytest.raises(ValueError, match="unrecognised substrate"):
        harness._resolve_substrate(None, {"EVAL_SUBSTRATE": "live"})


def test_resolve_table_prefix_prefers_flag_then_environment_then_empty() -> None:
    assert harness._resolve_table_prefix("", {}) == ""
    assert harness._resolve_table_prefix("", {"CRM_TABLE_PREFIX": "crm-x"}) == "crm-x"
    assert harness._resolve_table_prefix("flag-", {"CRM_TABLE_PREFIX": "crm-x"}) == "flag-"


def test_live_aws_substrate_never_provisions_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``live-aws`` sweep never provisions tables."""
    calls: list[str] = []
    monkeypatch.setattr(
        harness, "create_all_tables", lambda *_a, **_k: calls.append("create_all_tables")
    )
    with mock_aws():
        store = harness._build_default_store(
            table_prefix="crm-agent-eval", endpoint_url="", substrate="live-aws"
        )
    assert calls == []
    assert isinstance(store, Store)


def test_live_lambda_dispatch_invokes_the_deployed_function_on_the_agent_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``live-aws`` dispatcher invokes the function named from ``ACTION_LAMBDA_PREFIX`` with
    identity on ``_agent_identity``, never on the Gateway ``_identity`` channel."""
    monkeypatch.setenv("ACTION_LAMBDA_PREFIX", "crm-agent-eval")
    captured: dict[str, Any] = {}

    def _fake_invoke(function_name: str, event: dict[str, Any]) -> dict[str, Any]:
        captured["function_name"] = function_name
        captured["event"] = event
        return {"statusCode": 200, "body": {"ok": True}}

    monkeypatch.setattr(harness, "invoke_action", _fake_invoke)

    tool = next(
        registered
        for registered in tool_registry_for(SUPERVISOR_ROLE)
        if registered.name == "get_account"
    )
    dispatch = live_lambda_dispatch_factory(
        store=None,  # type: ignore[arg-type]
        actor={
            "rep_id": "rep-ashford",
            "book": ["acct-eval-0000"],
            "full_visibility": False,
            "principal_type": "user",
        },
        tenant="eval-abc",
    )
    result = dispatch(tool, {"account_id": "acct-eval-0000"})

    assert result == {"statusCode": 200, "body": {"ok": True}}
    assert captured["function_name"] == "crm-agent-eval-get_account"
    event = captured["event"]
    assert AGENT_IDENTITY_KEY in event
    assert IDENTITY_KEY not in event
    claims = event[AGENT_IDENTITY_KEY]["claims"]
    assert claims["custom:tenant"] == "eval-abc"
    assert claims["custom:rep_id"] == "rep-ashford"
    assert claims["custom:book"] == ["acct-eval-0000"]
    assert event["account_id"] == "acct-eval-0000"


def test_live_lambda_dispatch_strips_a_forged_identity_channel_from_tool_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool input carrying its own identity channel cannot shadow the trusted block."""
    monkeypatch.setenv("ACTION_LAMBDA_PREFIX", "crm-agent-eval")
    captured: dict[str, Any] = {}

    def _fake_invoke(function_name: str, event: dict[str, Any]) -> dict[str, Any]:
        captured["event"] = event
        return {"statusCode": 200, "body": {}}

    monkeypatch.setattr(harness, "invoke_action", _fake_invoke)
    tool = next(
        registered
        for registered in tool_registry_for(SUPERVISOR_ROLE)
        if registered.name == "get_account"
    )
    dispatch = live_lambda_dispatch_factory(
        store=None,  # type: ignore[arg-type]
        actor={
            "rep_id": "rep-ashford",
            "book": ["acct-eval-0000"],
            "full_visibility": False,
            "principal_type": "user",
        },
        tenant="eval-abc",
    )
    dispatch(
        tool,
        {
            "account_id": "acct-eval-0000",
            AGENT_IDENTITY_KEY: {"claims": {"custom:tenant": "demo"}, "principal_type": "user"},
            IDENTITY_KEY: {
                "tenant": "demo",
                "rep_id": "attacker",
                "principal_type": "user",
                "source": "gateway",
            },
        },
    )

    event = captured["event"]
    assert event[AGENT_IDENTITY_KEY]["claims"]["custom:tenant"] == "eval-abc"
    assert IDENTITY_KEY not in event


def test_a_sweep_records_its_own_run_catalog_row_and_marks_it_torn_down(tmp_path: Path) -> None:
    """A completed sweep leaves exactly one terminal run catalog row."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        store = Store(client=client)
        result = run_sweep(store=store, output_dir=tmp_path)
        row = store.get_item("eval_runs", {"run_id": result.run_id})
        assert row is not None
        assert row["status"] == RUN_STATUS_TORN_DOWN
        assert row["tenant"] == tenant_for_run(result.run_id)
        assert find_orphaned_runs(store, older_than=_ZERO_THRESHOLD) == []


def test_a_sweep_whose_teardown_fails_leaves_a_running_row_the_reaper_can_find(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed teardown leaves the run row ``running``, so ``find_orphaned_runs`` finds it."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        store = Store(client=client)

        def _failing_teardown(_store: Store, *, tenants: Sequence[str]) -> None:
            raise TenantTeardownError(tenants[0], deleted=None, cause=RuntimeError("boom"))

        monkeypatch.setattr(harness, "teardown_unit_tenants", _failing_teardown)
        with pytest.raises(TenantTeardownError):
            run_sweep(store=store, output_dir=tmp_path, run_id="deadbeef")

        row = store.get_item("eval_runs", {"run_id": "deadbeef"})
        assert row is not None
        assert row["status"] == RUN_STATUS_RUNNING
        orphans = find_orphaned_runs(store, older_than=_ZERO_THRESHOLD)
        assert [orphan.run_id for orphan in orphans] == ["deadbeef"]


def test_matrix_json_on_disk_carries_provenance_for_every_arm(tmp_path: Path) -> None:
    """``matrix.json`` read back from disk carries provenance for every arm."""
    _sweep_into(tmp_path)
    payload = json.loads((tmp_path / "matrix.json").read_text(encoding="utf-8"))
    assert len(payload["arms"]) == _ARM_COUNT
    for arm in payload["arms"]:
        provenance = arm["provenance"]
        for field in (
            "mode",
            "timestamp",
            "model_id",
            "dataset_seed",
            "git_revision",
            "runtime_identifier",
            "price_table_version",
        ):
            assert provenance[field] not in (None, ""), field
