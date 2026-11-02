# SPDX-License-Identifier: MIT
"""A full default stub sweep runs with zero credentials and zero cost.

The default :func:`eval.harness.run_sweep` constructs no live provider, imports no vendor SDK,
resolves no real AWS credential, and writes every report artifact, against a ``moto``-mocked store.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from agents.answer_style import answer_style_prompt_sha256
from crm.authz import DEMO_TENANT
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.harness import DEFAULT_ARM_SPECS, run_sweep


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _noop_seeder(store: Store, *, tenant: str) -> None:
    """A fixture seeder that seeds nothing.

    Substituted for ``fixtures.generate.seed_tenant`` via ``eval.tenancy._default_seeder``.
    """
    del store, tenant


@pytest.fixture(autouse=True)
def _inject_noop_seeder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eval.tenancy._default_seeder", lambda: _noop_seeder)


@pytest.fixture(autouse=True)
def _strip_aws_credential_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every AWS credential environment variable before each test in this file.

    Then sets moto's dummy ``testing`` credentials and ``us-east-1``.
    """
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


# --------------------------------------------------------------------------------------
# A full default sweep, zero credentials, zero cost
# --------------------------------------------------------------------------------------


def test_every_arm_records_the_live_answer_style_prompt_hash(store: Store) -> None:
    """Every arm's provenance carries the current `answer_style_prompt_sha256()`."""
    result = run_sweep(store=store)
    expected = answer_style_prompt_sha256()
    assert result.arm_runs, "sweep produced no arms to check"
    for arm_run in result.arm_runs:
        assert arm_run.arm_result.provenance.answer_style_sha256 == expected


def test_default_stub_sweep_completes_every_run_with_no_output_dir(store: Store) -> None:
    """With no ``mode`` or ``arm_specs`` supplied, the sweep runs 2 arms x every golden task stub
    runs."""
    from eval import harness

    task_count = len(harness._load_golden_tasks(harness._DEFAULT_GOLDEN_TASKS_PATH))
    result = run_sweep(store=store)
    assert result.mode == "stub"
    assert len(result.arm_runs) == len(DEFAULT_ARM_SPECS) == 2
    total_task_runs = sum(len(arm_run.task_scores) for arm_run in result.arm_runs)
    assert task_count > 0
    assert total_task_runs == 2 * task_count


def test_default_stub_sweep_never_touches_a_real_aws_credential(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stub sweep completes with ``botocore``'s credential resolver patched to raise."""
    import botocore.credentials

    def _explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            "a stub sweep must never resolve AWS credentials through botocore's real "
            "credential-resolution chain"
        )

    monkeypatch.setattr(
        botocore.credentials.CredentialResolver, "load_credentials", _explode, raising=False
    )
    result = run_sweep(store=store, arm_specs=DEFAULT_ARM_SPECS)
    assert result.mode == "stub"


def test_default_stub_sweep_imports_no_vendor_sdk_for_model_invocation() -> None:
    """After both are dropped from ``sys.modules``, a stub sweep leaves ``anthropic`` and
    ``openai`` unimported."""
    import sys

    for name in ("anthropic", "openai"):
        sys.modules.pop(name, None)

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        run_sweep(store=Store(client=client), arm_specs=DEFAULT_ARM_SPECS)

    assert "anthropic" not in sys.modules
    assert "openai" not in sys.modules


def test_default_stub_sweep_produces_all_four_fr_9_11_artifact_shapes(
    store: Store, tmp_path: Path
) -> None:
    """One default stub sweep writes ``matrix.json``, ``matrix.md``, ``matrix.csv``, and a per-arm
    raw archive under ``raw/`` in the output directory."""
    result = run_sweep(store=store, output_dir=tmp_path, archive_raw=True)

    matrix_json = tmp_path / "matrix.json"
    matrix_md = tmp_path / "matrix.md"
    matrix_csv = tmp_path / "matrix.csv"
    assert matrix_json.exists()
    assert matrix_md.exists()
    assert matrix_csv.exists()
    assert len(result.raw_archive_paths) == len(DEFAULT_ARM_SPECS)
    for raw_path in result.raw_archive_paths:
        assert raw_path.exists()
        assert raw_path.parent == tmp_path / "raw"

    arms = result.matrix.arms
    assert len(arms) == len(DEFAULT_ARM_SPECS)

    lines = matrix_md.read_text(encoding="utf-8").splitlines()
    assert lines.count("## Tier comparison summary") == 1
    arm_headings = [f"## {arm.provenance.model_id} (stub-mode run)" for arm in arms]
    for heading in arm_headings:
        assert lines.count(heading) == 1, f"matrix.md lacks exactly one {heading!r}"
    assert lines.index("## Tier comparison summary") < min(lines.index(h) for h in arm_headings)

    with matrix_csv.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames is not None
        assert {"model_id", "mode", "task_id"} <= set(reader.fieldnames)
        rows = list(reader)
    expected_rows = sorted(
        (arm.provenance.model_id, str(task["task_id"]))
        for arm in arms
        for task in arm.task_score_fields
    )
    assert expected_rows, "the sweep scored no task to check the CSV against"
    assert sorted((row["model_id"], row["task_id"]) for row in rows) == expected_rows
    assert {row["mode"] for row in rows} == {"stub"}


def test_default_stub_sweep_never_touches_the_demo_tenant(store: Store) -> None:
    """A stub sweep's tenant is never the demo tenant, and a pre-existing ``eval_runs`` marker row
    is left untouched."""
    store.put_item(
        "eval_runs",
        {"run_id": "demo-marker", "status": "untouched", "started_at": "2020-01-01T00:00:00Z"},
    )

    result = run_sweep(store=store, arm_specs=DEFAULT_ARM_SPECS)

    assert result.tenant != DEMO_TENANT
    marker = store.get_item("eval_runs", {"run_id": "demo-marker"})
    assert marker is not None
    assert marker["status"] == "untouched"


def test_default_stub_sweep_reports_provenance_with_no_real_account_identifier(
    store: Store,
) -> None:
    """Every arm's default ``runtime_identifier`` is ``local-stub``, never an ARN."""
    result = run_sweep(store=store, arm_specs=DEFAULT_ARM_SPECS)
    for arm in result.matrix.arms:
        runtime_identifier = arm.provenance.runtime_identifier
        assert "arn:aws" not in runtime_identifier
        assert runtime_identifier == "local-stub"
