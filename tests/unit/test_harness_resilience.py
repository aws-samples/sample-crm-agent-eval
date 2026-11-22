# SPDX-License-Identifier: MIT
"""Tests for sweep resilience: per-task transport failures, partial results, signals, run ids and
the CLI's exit codes.

Every sweep here is a stub sweep against moto with a no-op seeder. Failures are injected by a
provider wrapper that raises on the first model call of a named task.
"""

from __future__ import annotations

import json
import os
import signal
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import harness
from eval.harness import (
    EXIT_CODES,
    MID_STREAM_TRANSPORT_ERROR_CODE,
    ArmSpec,
    SweepSignalled,
    build_arg_parser,
    main,
    partial_manifest_path,
    run_id_is_recorded,
    run_sweep,
)
from eval.live import LiveInvocationError, MidStreamTransportError
from eval.sweep_units import TaskUnitResult
from eval.tenancy import RUN_STATUS_TORN_DOWN, record_run_started, tenant_for_run
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from providers.capabilities import load_capability_matrix
from providers.stub import StubProvider

_ARM = (ArmSpec(family="anthropic"),)
_TASK_IDS: list[str] = [
    str(task["id"]) for task in harness._load_golden_tasks(harness._DEFAULT_GOLDEN_TASKS_PATH)
]


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


@pytest.fixture(autouse=True)
def _noop_seeder(monkeypatch: pytest.MonkeyPatch) -> None:
    def _seed(store: Store, *, tenant: str) -> None:
        del store, tenant

    monkeypatch.setattr("eval.tenancy._default_seeder", lambda: _seed)


class _FailingOnTask(ModelProvider):
    """A stub provider that raises ``make_error()`` on the first model call of each named task."""

    def __init__(self, current: dict[str, str], fail_on: set[str], make_error: Any) -> None:
        self._stub = StubProvider()
        self._current = current
        self._fail_on = fail_on
        self._make_error = make_error
        self.failed: list[str] = []

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        task_id = self._current.get("task_id", "")
        if task_id in self._fail_on and task_id not in self.failed:
            self.failed.append(task_id)
            raise self._make_error()
        async for event in self._stub.stream(invocation, messages, tools):
            yield event


def _inject(
    monkeypatch: pytest.MonkeyPatch, fail_on: set[str], make_error: Callable[[], BaseException]
) -> _FailingOnTask:
    """Route every arm's provider through :class:`_FailingOnTask`, tracking the running task."""
    current: dict[str, str] = {}
    provider = _FailingOnTask(current, fail_on, make_error)
    real_run_task_unit: Callable[..., TaskUnitResult] = vars(harness)["run_task_unit"]

    def _tracking_run_task_unit(**kwargs: Any) -> TaskUnitResult:
        current["task_id"] = str(kwargs["task"]["id"])
        return real_run_task_unit(**kwargs)

    monkeypatch.setattr(harness, "run_task_unit", _tracking_run_task_unit)
    monkeypatch.setattr(harness, "_build_provider", lambda _mode, **_kwargs: provider)
    return provider


def _transport_error() -> MidStreamTransportError:
    return MidStreamTransportError(
        model_id="anthropic.claude-sonnet-5-5",
        error_message="ReadError: The model provider request failed.",
    )


def _manifest(output_dir: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        partial_manifest_path(output_dir).read_text(encoding="utf-8")
    )
    return document


# -- a mid-stream transport failure is one failed task -------------------------------------------


@pytest.mark.parametrize("position", [0, -1], ids=["first-task", "last-task"])
def test_a_mid_stream_failure_is_that_tasks_failure_and_the_sweep_renders(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: int
) -> None:
    target = _TASK_IDS[position]
    provider = _inject(monkeypatch, {target}, _transport_error)

    result = run_sweep(store=store, arm_specs=_ARM, output_dir=tmp_path, archive_raw=True)

    assert provider.failed == [target]
    (arm,) = result.arm_runs
    assert len(arm.task_scores) == len(_TASK_IDS), "the failed task stays in the denominator"
    failed = next(score for score in arm.task_scores if score.task_id == target)
    assert failed.assertion_results[0].name == MID_STREAM_TRANSPORT_ERROR_CODE
    assert failed.assertion_results[0].passed is False
    assert failed.correctness in (0.0, None)
    assert not any(
        result.name == MID_STREAM_TRANSPORT_ERROR_CODE
        for score in arm.task_scores
        if score.task_id != target
        for result in score.assertion_results
    )
    assert result.matrix.errored_item_count == 1
    assert (tmp_path / "matrix.md").exists()
    assert not (tmp_path / "partial").exists(), "a completed sweep removes its per-unit files"


def test_a_failed_tasks_metrics_disclose_the_lower_bound(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _TASK_IDS[0]
    _inject(monkeypatch, {target}, _transport_error)
    recorded: list[TaskUnitResult] = []
    real = harness._failed_task_unit

    def _recording(unit: TaskUnitResult, **kwargs: Any) -> TaskUnitResult:
        failed = real(unit, **kwargs)
        recorded.append(failed)
        return failed

    monkeypatch.setattr(harness, "_failed_task_unit", _recording)

    run_sweep(store=store, arm_specs=_ARM)

    (unit,) = recorded
    assert unit.turn.usage_lower_bound is True
    assert unit.turn.stream_calls == 1
    assert unit.turn.invocations == ()


def test_any_other_live_invocation_error_ends_the_sweep_with_partial_results(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _TASK_IDS[2]
    _inject(monkeypatch, {target}, lambda: LiveInvocationError("ValidationException: malformed."))

    with pytest.raises(LiveInvocationError):
        run_sweep(store=store, arm_specs=_ARM, output_dir=tmp_path, run_id="run-abort")

    manifest = _manifest(tmp_path)
    assert manifest["reason"] == "error:LiveInvocationError"
    assert manifest["teardown"] == "ok"
    assert manifest["in_flight_unit"]["completed_task_ids"] == _TASK_IDS[:2]
    assert not (tmp_path / "matrix.md").exists()
    assert not (tmp_path / "matrix.json").exists()


# -- per-unit persistence -------------------------------------------------------------------------


def test_each_finished_unit_is_on_disk_before_the_next_unit_starts(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    units_dir = tmp_path / "partial" / "units" / "run-persist"
    seen_at_repeat_one: list[list[str]] = []
    real: Callable[..., TaskUnitResult] = vars(harness)["run_task_unit"]

    def _observing(**kwargs: Any) -> TaskUnitResult:
        if kwargs["repeat_index"] == 1 and not seen_at_repeat_one:
            seen_at_repeat_one.append(sorted(path.name for path in units_dir.glob("*.json")))
        return real(**kwargs)

    monkeypatch.setattr(harness, "run_task_unit", _observing)

    run_sweep(store=store, arm_specs=_ARM, repeats=2, output_dir=tmp_path, run_id="run-persist")

    assert seen_at_repeat_one == [["arm-0-repeat-0.json"]]
    assert not units_dir.exists()


def test_an_early_end_keeps_completed_units_and_the_in_flight_units_tasks(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _TASK_IDS[1]
    calls = {"repeat_one": 0}
    real: Callable[..., TaskUnitResult] = vars(harness)["run_task_unit"]

    def _interrupting(**kwargs: Any) -> TaskUnitResult:
        if kwargs["repeat_index"] == 1 and str(kwargs["task"]["id"]) == target:
            calls["repeat_one"] += 1
            raise KeyboardInterrupt
        return real(**kwargs)

    monkeypatch.setattr(harness, "run_task_unit", _interrupting)

    with pytest.raises(KeyboardInterrupt):
        run_sweep(store=store, arm_specs=_ARM, repeats=2, output_dir=tmp_path, run_id="run-int")

    manifest = _manifest(tmp_path)
    assert manifest["reason"] == "signal:SIGINT"
    assert [unit["repeat_index"] for unit in manifest["completed_units"]] == [0]
    assert manifest["completed_units"][0]["task_ids"] == _TASK_IDS
    in_flight = manifest["in_flight_unit"]
    assert (in_flight["repeat_index"], in_flight["completed_task_ids"]) == (1, _TASK_IDS[:1])
    units_dir = tmp_path / "partial" / "units" / "run-int"
    assert (units_dir / manifest["completed_units"][0]["raw"]).exists()
    snapshot = json.loads((units_dir / in_flight["raw"]).read_text(encoding="utf-8"))
    assert [task["task_id"] for task in snapshot["tasks"]] == _TASK_IDS[:1]
    assert not (tmp_path / "matrix.md").exists()


def test_a_failed_task_is_listed_in_the_manifest_of_a_later_early_end(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, abort_on = _TASK_IDS[0], _TASK_IDS[1]
    current: dict[str, str] = {}

    def _error() -> BaseException:
        if current["task_id"] == first:
            return _transport_error()
        return LiveInvocationError("ValidationException: malformed.")

    provider = _inject(monkeypatch, {first, abort_on}, _error)
    real_tracking: Callable[..., TaskUnitResult] = vars(harness)["run_task_unit"]

    def _tracking(**kwargs: Any) -> TaskUnitResult:
        current["task_id"] = str(kwargs["task"]["id"])
        return real_tracking(**kwargs)

    monkeypatch.setattr(harness, "run_task_unit", _tracking)

    with pytest.raises(LiveInvocationError):
        run_sweep(store=store, arm_specs=_ARM, output_dir=tmp_path, run_id="run-failed-then-abort")

    assert provider.failed == [first, abort_on]
    manifest = _manifest(tmp_path)
    assert manifest["failed_tasks"] == [
        {
            "arm_index": 0,
            "repeat_index": 0,
            "task_id": first,
            "error_code": MID_STREAM_TRANSPORT_ERROR_CODE,
        }
    ]


def test_without_an_output_dir_an_early_end_writes_nothing(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inject(monkeypatch, {_TASK_IDS[0]}, lambda: LiveInvocationError("ValidationException: x."))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(LiveInvocationError):
        run_sweep(store=store, arm_specs=_ARM)

    assert list(tmp_path.iterdir()) == []


# -- configuration refusals -----------------------------------------------------------------------


def test_a_task_file_with_a_repeated_id_is_refused(tmp_path: Path) -> None:
    tasks = harness._load_golden_tasks(harness._DEFAULT_GOLDEN_TASKS_PATH)
    path = tmp_path / "tasks.yaml"
    path.write_text(json.dumps({"tasks": [*tasks, dict(tasks[0])]}), encoding="utf-8")

    with pytest.raises(ValueError, match=f"{tasks[0]['id']!r} appears more than once"):
        harness._load_golden_tasks(path)


def test_the_shipped_task_file_has_unique_ids() -> None:
    assert len(_TASK_IDS) == len(set(_TASK_IDS))


def test_run_sweep_for_ui_is_gone() -> None:
    assert not hasattr(harness, "run_sweep_for_ui")
    assert "run_sweep_for_ui" not in harness.__all__


# -- run ids ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["cli", "job"])
def test_run_id_is_recorded_reads_both_catalog_row_shapes(store: Store, key: str) -> None:
    run_id = "run-catalog-probe"
    assert not run_id_is_recorded(store, run_id)
    row_id = run_id if key == "cli" else f"job#{run_id}"
    store.put_item("eval_runs", {"run_id": row_id, "status": "running"})
    assert run_id_is_recorded(store, run_id)


def _cli(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "_build_default_store", lambda **_kwargs: store)
    monkeypatch.setattr(harness, "DEFAULT_ARM_SPECS", _ARM)
    real_run_sweep = harness.run_sweep

    def _one_arm(**kwargs: Any) -> Any:
        return real_run_sweep(arm_specs=_ARM, **kwargs)

    monkeypatch.setattr(harness, "run_sweep", _one_arm)


def test_a_recorded_run_id_exits_five_before_anything_is_seeded(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli(store, monkeypatch)
    run_id = "run-recorded-cli"
    record_run_started(store, run_id=run_id, tenant=tenant_for_run(run_id))

    def _forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("nothing may be seeded for a refused run id")

    monkeypatch.setattr(harness, "seed_unit_tenants", _forbidden)

    assert main(["--output-dir", str(tmp_path), "--run-id", run_id]) == 5
    assert "already recorded" in capsys.readouterr().err


def test_an_unusable_run_id_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _forbidden(**_kwargs: object) -> None:
        raise AssertionError("no store may be built for a refused invocation")

    monkeypatch.setattr(harness, "_build_default_store", _forbidden)
    assert main(["--output-dir", str(tmp_path), "--run-id", "run-a0r0"]) == 2
    assert "--run-id" in capsys.readouterr().err


def test_a_fresh_run_id_is_used_for_the_sweep(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli(store, monkeypatch)
    run_id = "run-fresh-cli"

    assert main(["--output-dir", str(tmp_path), "--run-id", run_id]) == 0

    row = store.get_item("eval_runs", {"run_id": run_id}, consistent=True)
    assert row is not None and row["status"] == RUN_STATUS_TORN_DOWN


# -- exit codes and error reporting ---------------------------------------------------------------


def test_every_exit_code_is_distinct_and_printed_by_help() -> None:
    codes = [code for code, _ in EXIT_CODES]
    assert len(codes) == len(set(codes))
    assert codes == [0, 1, 2, 4, 5, 6, 129, 130, 143]
    rendered = build_arg_parser().format_help()
    assert "exit codes:" in rendered
    for code, meaning in EXIT_CODES:
        assert f"{code:>3}  {meaning}" in rendered


def test_an_unrecoverable_error_after_seeding_exits_six_in_one_line(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli(store, monkeypatch)
    _inject(monkeypatch, {_TASK_IDS[1]}, lambda: LiveInvocationError("ValidationException: x."))
    monkeypatch.delenv(harness.VERBOSE_ERRORS_ENV_VAR, raising=False)

    assert main(["--output-dir", str(tmp_path)]) == 6

    err = capsys.readouterr().err
    failure_lines = [line for line in err.splitlines() if line.startswith("eval sweep failed:")]
    assert len(failure_lines) == 1
    assert "live_invocation_failed: LiveInvocationError" in failure_lines[0]
    assert str(partial_manifest_path(tmp_path)) in failure_lines[0]
    assert "Traceback" not in err


def test_an_error_before_any_unit_exits_one_without_a_traceback(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli(store, monkeypatch)

    def _broken(_path: Path) -> list[dict[str, object]]:
        raise harness.GoldenTaskFileError("tasks.yaml: task id 't-1' appears more than once")

    monkeypatch.setattr(harness, "_load_golden_tasks", _broken)
    monkeypatch.delenv(harness.VERBOSE_ERRORS_ENV_VAR, raising=False)

    assert main(["--output-dir", str(tmp_path)]) == 1

    err = capsys.readouterr().err
    assert "eval sweep failed: invalid_configuration: GoldenTaskFileError:" in err
    assert "Traceback" not in err
    assert not partial_manifest_path(tmp_path).exists()


def test_a_stale_manifest_from_another_run_does_not_turn_an_error_into_exit_six(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli(store, monkeypatch)
    stale = partial_manifest_path(tmp_path)
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps({"run_id": "some-other-run"}), encoding="utf-8")

    def _broken(_path: Path) -> list[dict[str, object]]:
        raise ValueError("unusable task file")

    monkeypatch.setattr(harness, "_load_golden_tasks", _broken)

    assert main(["--output-dir", str(tmp_path)]) == 1


def test_the_verbose_flag_adds_the_traceback(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli(store, monkeypatch)

    def _broken(_path: Path) -> list[dict[str, object]]:
        raise ValueError("unusable task file")

    monkeypatch.setattr(harness, "_load_golden_tasks", _broken)
    monkeypatch.setenv(harness.VERBOSE_ERRORS_ENV_VAR, "1")

    assert main(["--output-dir", str(tmp_path)]) == 1
    assert "Traceback" in capsys.readouterr().err


def test_sigint_mid_sweep_exits_130_after_teardown(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli(store, monkeypatch)
    _inject(monkeypatch, {_TASK_IDS[1]}, KeyboardInterrupt)
    run_id = "run-sigint-cli"

    assert main(["--output-dir", str(tmp_path), "--run-id", run_id]) == 130

    assert _manifest(tmp_path)["reason"] == "signal:SIGINT"
    row = store.get_item("eval_runs", {"run_id": run_id}, consistent=True)
    assert row is not None and row["status"] == RUN_STATUS_TORN_DOWN
    assert "SIGINT" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("signum", "code"), [(signal.SIGTERM, 143), (signal.SIGHUP, 129)], ids=["SIGTERM", "SIGHUP"]
)
def test_a_real_signal_mid_sweep_unwinds_through_teardown(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signum: int, code: int
) -> None:
    """The handler main installs turns the signal into an exit after teardown, then is restored."""
    _cli(store, monkeypatch)

    def _deliver() -> BaseException:
        os.kill(os.getpid(), signum)
        return RuntimeError("the signal handler did not raise")

    _inject(monkeypatch, {_TASK_IDS[1]}, _deliver)
    before = signal.getsignal(signum)
    run_id = "run-real-signal"

    assert main(["--output-dir", str(tmp_path), "--run-id", run_id]) == code

    assert signal.getsignal(signum) is before
    manifest = _manifest(tmp_path)
    assert manifest["reason"] == f"signal:{signal.Signals(signum).name}"
    assert manifest["teardown"] == "ok"
    row = store.get_item("eval_runs", {"run_id": run_id}, consistent=True)
    assert row is not None and row["status"] == RUN_STATUS_TORN_DOWN


def test_a_repeated_signal_during_teardown_does_not_interrupt_it(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli(store, monkeypatch)

    def _deliver() -> BaseException:
        os.kill(os.getpid(), signal.SIGTERM)
        return RuntimeError("the signal handler did not raise")

    _inject(monkeypatch, {_TASK_IDS[1]}, _deliver)
    real_teardown = vars(harness)["teardown_unit_tenants"]

    def _signalled_teardown(store: Store, *, tenants: Any) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        real_teardown(store, tenants=tenants)

    monkeypatch.setattr(harness, "teardown_unit_tenants", _signalled_teardown)
    run_id = "run-repeat-signal"

    assert main(["--output-dir", str(tmp_path), "--run-id", run_id]) == 143

    row = store.get_item("eval_runs", {"run_id": run_id}, consistent=True)
    assert row is not None and row["status"] == RUN_STATUS_TORN_DOWN
    assert _manifest(tmp_path)["teardown"] == "ok"


def test_sweep_signalled_is_a_system_exit_carrying_the_shell_code() -> None:
    raised = SweepSignalled(signal.SIGTERM)
    assert isinstance(raised, SystemExit)
    assert raised.code == 143
    assert raised.signal_name == "SIGTERM"


def test_a_cost_cap_stop_through_the_cli_still_exits_four_with_partial_results(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli(store, monkeypatch)

    assert main(["--output-dir", str(tmp_path), "--max-cost-usd", "0.000000001"]) == 4
    assert _manifest(tmp_path)["reason"] == "cost_cap"


# -- teardown and reporting under failure ------------------------------------------------------


def test_a_delta_read_that_raises_still_tears_down_and_fails_both_checks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raising(*_a: object, **_k: object) -> None:
        raise RuntimeError("delta read failed")

    monkeypatch.setattr(harness, "_fr93_delta_checks", _raising)

    result = run_sweep(store=store, arm_specs=_ARM, run_id="run-delta-raises")

    assert [check.status for check in result.fr93_checks] == ["fail", "fail"]
    assert all("RuntimeError: delta read failed" in check.detail for check in result.fr93_checks)
    row = store.get_item("eval_runs", {"run_id": "run-delta-raises"}, consistent=True)
    assert row is not None and row["status"] == RUN_STATUS_TORN_DOWN


def test_a_non_deletion_teardown_failure_never_replaces_the_body_exception(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inject(monkeypatch, {_TASK_IDS[0]}, lambda: LiveInvocationError("ValidationException: x."))

    def _canary_breach(*_a: object, **_k: object) -> None:
        raise RuntimeError("the canary tenant holds a row")

    monkeypatch.setattr(harness, "teardown_unit_tenants", _canary_breach)

    with pytest.raises(LiveInvocationError) as raised:
        run_sweep(store=store, arm_specs=_ARM, output_dir=tmp_path, run_id="run-body-wins")

    assert any("the canary tenant holds a row" in note for note in raised.value.__notes__)
    assert _manifest(tmp_path)["teardown"] == "failed"


def test_a_non_deletion_teardown_failure_after_a_clean_body_is_raised(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _canary_breach(*_a: object, **_k: object) -> None:
        raise RuntimeError("the canary tenant holds a row")

    monkeypatch.setattr(harness, "teardown_unit_tenants", _canary_breach)

    with pytest.raises(RuntimeError, match="canary tenant holds a row"):
        run_sweep(store=store, arm_specs=_ARM)


def test_a_first_signal_during_teardown_is_held_until_teardown_finishes(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli(store, monkeypatch)
    real_teardown = vars(harness)["teardown_unit_tenants"]

    def _signalled_teardown(store: Store, *, tenants: Any) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        real_teardown(store, tenants=tenants)

    monkeypatch.setattr(harness, "teardown_unit_tenants", _signalled_teardown)
    run_id = "run-held-signal"

    assert main(["--output-dir", str(tmp_path), "--run-id", run_id]) == 143

    row = store.get_item("eval_runs", {"run_id": run_id}, consistent=True)
    assert row is not None and row["status"] == RUN_STATUS_TORN_DOWN
    manifest = _manifest(tmp_path)
    assert (manifest["reason"], manifest["teardown"]) == ("signal:SIGTERM", "ok")
    assert not (tmp_path / "matrix.md").exists()


def test_an_artifact_write_failure_keeps_the_unit_files_and_writes_a_manifest(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _disk_full(*_a: object, **_k: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(harness, "write_matrix_artifacts", _disk_full)

    with pytest.raises(OSError, match="no space left"):
        run_sweep(store=store, arm_specs=_ARM, output_dir=tmp_path, run_id="run-disk-full")

    manifest = _manifest(tmp_path)
    assert (manifest["reason"], manifest["teardown"]) == ("error:OSError", "ok")
    assert [unit["repeat_index"] for unit in manifest["completed_units"]] == [0]
    units_dir = tmp_path / "partial" / "units" / "run-disk-full"
    assert (units_dir / manifest["completed_units"][0]["raw"]).exists()


def test_an_earlier_attempts_manifest_for_the_same_run_id_does_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-stale-attempt"
    stale = partial_manifest_path(tmp_path)
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    os.utime(stale, (1.0, 1.0))

    def _unreachable(**_kwargs: object) -> Store:
        raise RuntimeError("DynamoDB endpoint unreachable")

    monkeypatch.setattr(harness, "_build_default_store", _unreachable)

    assert main(["--output-dir", str(tmp_path), "--run-id", run_id]) == 1


def test_an_unclassified_value_error_is_an_internal_error(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli(store, monkeypatch)

    def _bug(_path: Path) -> list[dict[str, object]]:
        raise ValueError("a programming error")

    monkeypatch.setattr(harness, "_load_golden_tasks", _bug)

    assert main(["--output-dir", str(tmp_path)]) == 1
    assert "eval sweep failed: internal_error: ValueError: a programming error" in (
        capsys.readouterr().err
    )


def test_a_manifest_that_cannot_be_built_never_replaces_the_body_exception(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _inject(monkeypatch, {_TASK_IDS[0]}, lambda: LiveInvocationError("ValidationException: x."))

    def _broken_manifest(self: object, **_kwargs: object) -> dict[str, object]:
        raise KeyError("arm_index")

    monkeypatch.setattr(harness._SweepProgress, "manifest", _broken_manifest)

    with pytest.raises(LiveInvocationError):
        run_sweep(store=store, arm_specs=_ARM, output_dir=tmp_path, run_id="run-bad-manifest")

    assert "could not write the partial-results manifest" in capsys.readouterr().err


# -- dirty-tree provenance -------------------------------------------------------------------------


def test_the_tracked_tree_is_probed_once_at_sweep_start_and_recorded_on_every_arm(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness's one reading reaches every arm; no arm falls back to probing on its own."""
    probes: list[str] = []

    def _probe(git_revision: str) -> bool:
        probes.append(git_revision)
        return False

    def _no_fallback_probe(_repo_root: Path) -> bool:
        raise AssertionError("an arm probed the tree instead of using the sweep-start reading")

    monkeypatch.setattr(harness, "_git_revision", lambda: "a" * 40)
    monkeypatch.setattr(harness, "dirty_tree_for_revision", _probe)
    monkeypatch.setattr("eval.sweep_units.tracked_tree_dirty", _no_fallback_probe)

    result = run_sweep(store=store, repeats=2)

    assert probes == ["a" * 40]
    assert [arm.provenance.dirty_tree for arm in result.matrix.arms] == [False, False]


def test_a_failed_tree_probe_ends_the_sweep_before_anything_is_seeded(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded: list[str] = []

    def _broken_probe(_git_revision: str) -> bool:
        raise RuntimeError("cannot measure dirty_tree: git status exited 128")

    monkeypatch.setattr(harness, "dirty_tree_for_revision", _broken_probe)
    monkeypatch.setattr(harness, "seed_unit_tenants", lambda *_a, **_k: seeded.append("seeded"))

    with pytest.raises(RuntimeError, match="cannot measure dirty_tree"):
        run_sweep(store=store, arm_specs=_ARM)

    assert seeded == []


def test_run_arm_without_a_reading_resolves_it_itself_never_leaving_it_to_assemble_arm(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct `_run_arm` caller that passes no reading still hands `assemble_arm` one."""
    probes: list[str] = []
    passed: list[object] = []

    def _probe(git_revision: str) -> bool:
        probes.append(git_revision)
        return False

    real_assemble = vars(harness)["assemble_arm"]

    def _recording_assemble(*args: Any, **kwargs: Any) -> Any:
        passed.append(kwargs.get("dirty_tree", "<absent>"))
        return real_assemble(*args, **kwargs)

    monkeypatch.setattr(harness, "dirty_tree_for_revision", _probe)
    monkeypatch.setattr(harness, "assemble_arm", _recording_assemble)
    tasks = harness._load_golden_tasks(harness._DEFAULT_GOLDEN_TASKS_PATH)[:1]

    harness._run_arm(
        provider=StubProvider(),
        matrix=load_capability_matrix(),
        arm_spec=_ARM[0],
        tasks=tasks,
        store=store,
        tenant="eval-run-arm-direct",
        mode="stub",
        dataset_seed=1,
        git_revision="b" * 40,
        runtime_identifier="test",
        price_table_version="0.1.0-provisional",
        cost_withheld=True,
    )

    assert probes == ["b" * 40]
    assert passed == [False]
