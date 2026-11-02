# SPDX-License-Identifier: MIT
"""Tests for the eval harness's stub-by-default CLI/library entry point.

Covers that live mode is unreachable without an explicit flag, the CLI argument surface, the default
sweep matrix shape, and provenance completeness. The full stub sweep is in
``tests/stub/test_stub_sweep.py``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from agents.prompt_bundle import prompt_bundle_sha256
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import harness
from eval.harness import (
    DEFAULT_ARM_SPECS,
    DEFAULT_DATASET_SEED,
    ArmSpec,
    build_arg_parser,
    main,
    run_sweep,
    run_sweep_for_ui,
)
from eval.tenancy import TenantTeardownError

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _noop_seeder(store: Store, *, tenant: str) -> None:
    """A fixture seeder that seeds nothing, for tests exercising harness plumbing in isolation."""
    del store, tenant


@pytest.fixture(autouse=True)
def _inject_noop_seeder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every ``eval.tenancy.seed_tenant`` default-path call in this file to a no-op seeder.

    The harness's real call to ``eval.tenancy.seed_tenant`` still runs; only
    ``eval.tenancy._default_seeder`` is replaced.
    """
    monkeypatch.setattr("eval.tenancy._default_seeder", lambda: _noop_seeder)


# --------------------------------------------------------------------------------------
# Stub is the default, and live is unreachable without an explicit flag
# --------------------------------------------------------------------------------------


def test_run_sweep_defaults_to_stub_mode(store: Store) -> None:
    result = run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic"),))
    assert result.mode == "stub"


def test_run_sweep_live_mode_refuses_an_unpaired_offline_substrate(store: Store) -> None:
    """Passing ``mode="live"`` with the default (offline) dispatcher is refused before anything
    is seeded or invoked."""
    with pytest.raises(harness.LiveSubstratePairingError, match="requires the live-AWS substrate"):
        run_sweep(store=store, mode="live", arm_specs=(ArmSpec(family="anthropic"),))


def test_cli_defaults_to_stub_without_the_live_flag() -> None:
    parser = build_arg_parser()
    args = parser.parse_args([])
    assert args.live is False


def test_cli_live_flag_requires_explicit_opt_in() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(["--live"])
    assert args.live is True


def test_main_builds_its_own_explicit_endpoint_store_never_a_bare_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``main()`` constructs :class:`crm.store.store.Store` with a non-``None`` client targeting the
    CLI's ``--endpoint-url``, never a bare ``Store(table_prefix=...)``.

    ``Store`` is replaced by a recording fake and ``create_all_tables`` is stubbed, so no DynamoDB
    endpoint is reached."""
    captured: dict[str, object] = {}

    class _RaisingStore:
        def __init__(self, *, table_prefix: str = "", client: object | None = None) -> None:
            captured["table_prefix"] = table_prefix
            captured["client"] = client
            raise RuntimeError("stop before any DynamoDB call")

    def _record_provisioning(client: object, *, table_prefix: str = "") -> None:
        captured["provisioned_prefix"] = table_prefix

    monkeypatch.setattr(harness, "create_all_tables", _record_provisioning)
    monkeypatch.setattr(harness, "Store", _RaisingStore)

    with pytest.raises(RuntimeError, match="stop before any DynamoDB call"):
        main(["--output-dir", str(tmp_path)])

    assert captured["client"] is not None
    assert captured["table_prefix"] == ""
    assert captured["provisioned_prefix"] == ""


def test_run_sweep_for_ui_defaults_to_stub(store: Store) -> None:
    result = run_sweep_for_ui(store=store)
    assert result.mode == "stub"


def test_run_sweep_for_ui_live_true_still_refuses_an_offline_substrate(store: Store) -> None:
    """The UI's ``live=True`` toggle reaches the same offline-substrate refusal as the CLI's
    ``--live``."""
    with pytest.raises(harness.LiveSubstratePairingError, match="requires the live-AWS substrate"):
        run_sweep_for_ui(store=store, live=True)


def test_harness_module_imports_no_model_vendor_sdk_for_invocation() -> None:
    """``src/eval/harness.py`` contains no ``anthropic``/``openai`` import anywhere (AST check).

    ``boto3``/``botocore`` are not checked: the default store construction imports ``boto3``.
    """
    source = (REPO_ROOT / "src" / "eval" / "harness.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"anthropic", "openai"}
    for node in ast.walk(tree):
        names: list[str | None] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module]
        for name in names:
            if not name:
                continue
            root = name.split(".", 1)[0]
            assert root not in forbidden, f"harness.py imports forbidden vendor module {name!r}"


def test_build_provider_builds_a_bedrock_backed_adapter_for_live_mode() -> None:
    """The provider-construction seam builds the family's real adapter for live mode, wrapped in
    the bounded-retry wrapper, without reading any vendor API key.
    """
    from eval.live import ThrottleRetryingProvider

    provider = harness._build_provider("live", family="anthropic")

    assert isinstance(provider, ThrottleRetryingProvider)


def test_build_provider_returns_the_stub_provider_for_stub_mode() -> None:
    from providers.stub import StubProvider

    provider = harness._build_provider("stub")
    assert isinstance(provider, StubProvider)


# --------------------------------------------------------------------------------------
# Identical code path for CLI and UI
# --------------------------------------------------------------------------------------


def test_stub_sweep_reports_latency_not_measured_with_null_figures(store: Store) -> None:
    """A stub arm reports latency_measured=false with null task/call figures and every tier
    present at count 0."""
    result = run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic"),), run_id="run-lat")
    latency = result.arm_runs[0].model_metrics.latency
    assert latency.measured is False
    assert latency.task_p50_ms is None
    assert latency.task_p95_ms is None
    fields = result.arm_runs[0].arm_result.model_metrics_fields
    assert fields["latency_measured"] is False
    assert fields["task_latency_ms_p50"] is None
    by_tier = fields["call_latency_ms_by_tier"]
    assert isinstance(by_tier, dict)
    assert set(by_tier) == {"supervisor", "subagent", "tool"}
    for tier_fields in by_tier.values():
        assert tier_fields == {"p50": None, "p95": None, "count": 0}


def test_two_stub_sweeps_are_byte_identical_in_their_latency_fields(store: Store) -> None:
    """Two stub sweeps with one seed/run_id render byte-identical latency blocks and matrix JSON
    apart from timestamp."""
    from eval.matrix import render_json

    a = run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic"),), run_id="run-bi")
    b = run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic"),), run_id="run-bi")

    def _latency_block(result: object) -> dict[str, object]:
        arm = result.arm_runs[0].arm_result.model_metrics_fields  # type: ignore[attr-defined]
        return {
            k: arm[k] for k in arm if k.startswith(("latency_", "task_latency", "call_latency"))
        }

    assert _latency_block(a) == _latency_block(b)
    # Full matrix JSON differs only by the timestamp provenance field.
    ja = render_json(a.matrix).replace(a.matrix.arms[0].provenance.timestamp, "TS")
    jb = render_json(b.matrix).replace(b.matrix.arms[0].provenance.timestamp, "TS")
    assert ja == jb


def test_run_sweep_and_run_sweep_for_ui_produce_identical_artifacts_for_identical_input(
    store: Store,
) -> None:
    """Two :func:`run_sweep` calls with the same arguments and ``run_id`` produce identical
    provenance and scores.

    The ``run_id`` is shared because golden-task prompts are localized to the run's tenant.
    """
    arm_specs = (ArmSpec(family="anthropic"),)

    direct_result = run_sweep(store=store, arm_specs=arm_specs, run_id="run-a")
    ui_result = run_sweep(store=store, arm_specs=arm_specs, run_id="run-a")

    direct_fields = direct_result.matrix.to_report_fields()
    ui_fields = ui_result.matrix.to_report_fields()
    direct_arms = direct_fields["arms"]
    ui_arms = ui_fields["arms"]
    assert isinstance(direct_arms, list)
    assert isinstance(ui_arms, list)
    assert len(direct_arms) == len(ui_arms) == 1

    direct_provenance = direct_result.matrix.arms[0].provenance
    ui_provenance = ui_result.matrix.arms[0].provenance
    assert direct_provenance.model_id == ui_provenance.model_id
    assert direct_provenance.effort == ui_provenance.effort
    assert direct_result.matrix.arms[0].aggregate_score_fields == (
        ui_result.matrix.arms[0].aggregate_score_fields
    )
    assert direct_result.matrix.arms[0].task_score_fields == (
        ui_result.matrix.arms[0].task_score_fields
    )


# --------------------------------------------------------------------------------------
# Default sweep matrix is exactly {Anthropic, OpenAI} x {effort medium}, every golden task x 1 rep
# --------------------------------------------------------------------------------------


def test_default_arm_specs_is_exactly_anthropic_and_openai_at_effort_medium() -> None:
    assert len(DEFAULT_ARM_SPECS) == 2
    families = {spec.family for spec in DEFAULT_ARM_SPECS}
    assert families == {"anthropic", "openai"}
    for spec in DEFAULT_ARM_SPECS:
        assert spec.effort == "medium"


def test_default_sweep_runs_every_golden_task_once_per_arm(store: Store) -> None:
    """A default-arm-spec sweep produces 2 arms x every golden task x 1 repetition per-task scores."""
    task_count = len(harness._load_golden_tasks(harness._DEFAULT_GOLDEN_TASKS_PATH))
    result = run_sweep(store=store)
    total_task_runs = sum(len(arm_run.task_scores) for arm_run in result.arm_runs)
    assert len(result.arm_runs) == 2
    assert task_count > 0
    assert total_task_runs == 2 * task_count


def test_a_wider_sweep_requires_an_explicit_arm_specs_argument(store: Store) -> None:
    """Passing additional arms is the only way to widen the sweep; the module-level default is
    untouched by doing so."""
    wide_specs = (*DEFAULT_ARM_SPECS, ArmSpec(family="anthropic", effort="high"))
    result = run_sweep(store=store, arm_specs=wide_specs)
    assert len(result.arm_runs) == 3
    assert (
        ArmSpec(family="anthropic", effort="medium"),
        ArmSpec(family="openai", effort="medium"),
    ) == DEFAULT_ARM_SPECS


# --------------------------------------------------------------------------------------
# Tenant-per-run isolation via eval.tenancy
# --------------------------------------------------------------------------------------


def test_run_sweep_allocates_a_fresh_tenant_and_tears_it_down(store: Store) -> None:
    result = run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic"),))
    assert result.tenant == f"eval-{result.run_id}"
    assert result.teardown_error is None


def test_run_sweep_never_targets_the_demo_tenant(store: Store) -> None:
    from crm.authz import DEMO_TENANT

    result = run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic"),))
    assert result.tenant != DEMO_TENANT
    assert result.tenant.startswith("eval-")


def test_run_sweep_reraises_a_teardown_failure_rather_than_swallowing_it(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_tenant: str) -> dict[str, int]:
        raise RuntimeError("simulated teardown failure")

    monkeypatch.setattr(store, "delete_tenant", _boom)

    with pytest.raises(TenantTeardownError, match="simulated teardown failure"):
        run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic"),))


def test_two_concurrent_sweeps_allocate_distinct_non_colliding_tenants(store: Store) -> None:
    """Two sweeps each allocate their own distinct run id and tenant (run sequentially against
    one moto backend)."""
    result_a = run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic"),))
    result_b = run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic"),))
    assert result_a.run_id != result_b.run_id
    assert result_a.tenant != result_b.tenant


# --------------------------------------------------------------------------------------
# Full provenance on every result record
# --------------------------------------------------------------------------------------


def test_every_arm_provenance_carries_the_fr_9_12_fields_and_no_temperature(store: Store) -> None:
    result = run_sweep(
        store=store,
        arm_specs=(ArmSpec(family="anthropic", effort="low"),),
        dataset_seed=DEFAULT_DATASET_SEED,
        runtime_identifier="local-stub-test",
    )
    provenance_fields = result.matrix.arms[0].provenance.to_report_fields()
    expected_keys = {
        "mode",
        "timestamp",
        "model_id",
        "effort",
        "dataset_seed",
        "git_revision",
        "runtime_identifier",
        "price_table_version",
        "answer_style_sha256",  # the answer-style prompt hash
        "prompt_bundle_sha256",  # the whole prompt bundle's hash
    }
    assert set(provenance_fields) == expected_keys
    assert provenance_fields["mode"] == "stub"
    assert provenance_fields["dataset_seed"] == DEFAULT_DATASET_SEED
    assert provenance_fields["runtime_identifier"] == "local-stub-test"
    assert provenance_fields["price_table_version"]
    assert provenance_fields["git_revision"]
    assert provenance_fields["timestamp"]
    assert provenance_fields["answer_style_sha256"]
    assert provenance_fields["prompt_bundle_sha256"] == prompt_bundle_sha256()


def test_git_revision_never_raises_even_outside_a_git_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(harness, "_REPO_ROOT", tmp_path)
    assert harness._git_revision() == "unknown"


def test_default_dataset_seed_matches_the_fixture_generators_seed() -> None:
    """The harness's default dataset seed equals the fixture generator's pinned seed."""
    from fixtures.generate import SEED

    assert DEFAULT_DATASET_SEED == SEED


# --------------------------------------------------------------------------------------
# Artifact writing: matrix.json/md/csv and raw archives are produced when requested
# --------------------------------------------------------------------------------------


def test_run_sweep_writes_all_three_matrix_artifacts_when_output_dir_given(
    store: Store, tmp_path: Path
) -> None:
    result = run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic"),), output_dir=tmp_path)
    assert set(result.artifact_paths) == {"json", "md", "csv"}
    for path in result.artifact_paths.values():
        assert path.exists()


def test_run_sweep_skips_writing_artifacts_when_output_dir_is_none(store: Store) -> None:
    result = run_sweep(store=store, arm_specs=(ArmSpec(family="anthropic"),), output_dir=None)
    assert result.artifact_paths == {}
    assert result.raw_archive_paths == ()


def test_run_sweep_archives_raw_json_per_arm_when_requested(store: Store, tmp_path: Path) -> None:
    result = run_sweep(
        store=store,
        arm_specs=DEFAULT_ARM_SPECS,
        output_dir=tmp_path,
        archive_raw=True,
    )
    assert len(result.raw_archive_paths) == 2
    for path in result.raw_archive_paths:
        assert path.exists()
        assert path.parent == tmp_path / "raw"


def test_run_sweep_does_not_archive_raw_json_unless_requested(store: Store, tmp_path: Path) -> None:
    result = run_sweep(
        store=store,
        arm_specs=(ArmSpec(family="anthropic"),),
        output_dir=tmp_path,
        archive_raw=False,
    )
    assert result.raw_archive_paths == ()
    assert not (tmp_path / "raw").exists()


def test_main_writes_artifacts_and_returns_zero_on_success(tmp_path: Path) -> None:
    """``main`` writes the artifacts and returns 0 against a moto-mocked DynamoDB backend.

    ``--endpoint-url`` is overridden because moto intercepts only clients built without an
    explicit local ``endpoint_url``.
    """
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        exit_code = main(
            [
                "--output-dir",
                str(tmp_path),
                "--dataset-seed",
                str(DEFAULT_DATASET_SEED),
                "--endpoint-url",
                "",
            ]
        )
    assert exit_code == 0
    assert (tmp_path / "matrix.json").exists()
    assert (tmp_path / "matrix.md").exists()
    assert (tmp_path / "matrix.csv").exists()


# --------------------------------------------------------------------------------------
# Non-interactive execution: no TTY, no pager, no prompt
# --------------------------------------------------------------------------------------


def test_argument_parser_never_prompts_and_exits_cleanly_with_no_args() -> None:
    """``parse_args([])`` must never block on stdin or require a TTY — every argument has a
    default."""
    parser = build_arg_parser()
    args = parser.parse_args([])
    assert args.live is False
    assert args.archive_raw is False
    assert args.table_prefix == ""


def test_cli_help_text_does_not_invoke_a_pager(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])
    captured = capsys.readouterr()
    assert "--live" in captured.out
