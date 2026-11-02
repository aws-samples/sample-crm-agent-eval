# SPDX-License-Identifier: MIT
"""Submit eval sweeps to the ``eval_sweep`` state machine.

:func:`submit_sweep` is the submission path: it authorizes the caller, takes the single-flight
lock, counts a live sweep against the daily cap, writes the ``queued`` run record
(:mod:`eval.runs`), and only then starts the execution named by the run id, recording its ARN. A
live sweep requires an admin caller and ``confirmed=True``; a stub sweep requires full visibility.
The sweep itself runs in the state machine's two functions, never in this module.

Job state lives in ``eval_runs`` under ``job#<run_id>`` (:func:`job_row_id`) with a sentinel
``status`` of :data:`_JOB_ROW_STATUS`. The harness's tenant-lifecycle row lives at ``<run_id>`` in
the same table. The key spaces are disjoint (``job#`` prefix): ``Store.put_item`` overwrites a whole
item, so a shared key would erase the other row. The sentinel keeps job rows out of
:func:`eval.tenancy.find_orphaned_runs`.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from botocore.exceptions import ClientError

from crm.authz import Identity
from crm.errors import Conflict, CrmError, ScopeDenied, ValidationFailed, wrap_aws_error
from crm.store.store import Store
from eval.harness import (
    EvalMode,
    SweepResult,
)
from eval.runs import EVAL_STATE_MACHINE_ARN_ENV_VAR as _RUNS_STATE_MACHINE_ENV_VAR
from eval.tenancy import tenant_for_run

if TYPE_CHECKING:
    from eval.runs import RunRecord

#: This module's logger. Each call logs a `json.dumps` of a literal dict, with the run id as
#: `correlation_id`.
_logger = logging.getLogger(__name__)


#: The catalog table both row kinds live in — the same one :mod:`eval.tenancy` writes.
_EVAL_RUNS_TABLE: Final[str] = "eval_runs"

#: Primary-key prefix distinguishing a job row from the harness's tenant-lifecycle row.
_JOB_ROW_PREFIX: Final[str] = "job#"

#: Sentinel ``status`` every job row carries. It differs from
#: :data:`eval.tenancy.RUN_STATUS_RUNNING`, so :func:`eval.tenancy.find_orphaned_runs` never returns
#: a job row.
_JOB_ROW_STATUS: Final[str] = "job"

#: Longest logged failure reason.
_MAX_ERROR_LENGTH: Final[int] = 1024


#: How this module's authorization requirement is named in a denial, so a caller can explain it.
EVAL_TRIGGER_SCOPE: Final[str] = "eval:trigger"

#: How the admin requirement for a live sweep, cancel and unlock is named in a denial.
EVAL_ADMIN_SCOPE: Final[str] = "eval:admin"

#: Environment variable naming the ``eval_sweep`` state machine :func:`submit_sweep` starts.
EVAL_STATE_MACHINE_ARN_ENV_VAR: Final[str] = _RUNS_STATE_MACHINE_ENV_VAR


def job_row_id(run_id: str) -> str:
    """Return the ``eval_runs`` primary key of ``run_id``'s job row: ``"job#<run_id>"``.

    Raises:
        ValueError: ``run_id`` is empty, or is already a job row id (which would double-prefix it).
    """
    if not run_id:
        raise ValueError("run_id must not be empty")
    if run_id.startswith(_JOB_ROW_PREFIX):
        raise ValueError(f"already a job row id, refusing to double-prefix: {run_id!r}")
    return f"{_JOB_ROW_PREFIX}{run_id}"


def run_id_of_job_row(row_id: str) -> str:
    """Return the run id embedded in a job row key, the inverse of :func:`job_row_id`.

    Raises:
        ValueError: ``row_id`` does not carry the job row prefix.
    """
    if not row_id.startswith(_JOB_ROW_PREFIX):
        raise ValueError(f"not a job row id: {row_id!r}")
    return row_id[len(_JOB_ROW_PREFIX) :]


def require_eval_trigger_scope(identity: Identity, *, operation: str = "trigger_eval") -> None:
    """Raise :class:`crm.errors.ScopeDenied` unless ``identity`` may start an eval sweep.

    Requires ``full_visibility``, which :class:`crm.authz.Identity` never allows on a service
    principal.

    Raises:
        ScopeDenied: ``identity`` does not hold full visibility.
    """
    if not identity.full_visibility:
        raise ScopeDenied(
            "Starting an evaluation sweep requires full tenant visibility.",
            missing_scope=EVAL_TRIGGER_SCOPE,
            detail=f"operation={operation} {identity.scope_description()}",
        )


def require_eval_admin(identity: Identity, *, operation: str) -> None:
    """Raise :class:`crm.errors.ScopeDenied` unless ``identity`` is an admin user principal.

    Raises:
        ScopeDenied: ``identity`` is not in the ``admin`` group (``missing_scope="eval:admin"``).
    """
    if not identity.is_admin:
        raise ScopeDenied(
            "This evaluation operation requires an administrator.",
            missing_scope=EVAL_ADMIN_SCOPE,
            detail=f"operation={operation} {identity.scope_description()}",
        )


#: ``StartExecution`` error codes meaning the execution was certainly not started.
_DEFINITE_START_REJECTIONS: Final[frozenset[str]] = frozenset(
    {
        "StateMachineDoesNotExist",
        "StateMachineDeleting",
        "InvalidArn",
        "InvalidName",
        "InvalidExecutionInput",
        "ExecutionLimitExceeded",
        "ValidationException",
        "AccessDeniedException",
        "KmsAccessDeniedException",
        "KmsInvalidStateException",
        "KmsThrottlingException",
    }
)


class StateMachineNotConfiguredError(CrmError):
    """Raised when :data:`EVAL_STATE_MACHINE_ARN_ENV_VAR` is unset, before anything is written."""


def sweep_shape() -> tuple[tuple[str, ...], int]:
    """The default sweep's arm families and golden task count per arm."""
    from eval.harness import _DEFAULT_GOLDEN_TASKS_PATH, DEFAULT_ARM_SPECS, _load_golden_tasks

    families = tuple(spec.family for spec in DEFAULT_ARM_SPECS)
    return families, len(_load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH))


def submit_sweep(
    store: Store,
    sfn: Any,
    identity: Identity,
    mode: EvalMode,
    confirmed: bool,
    *,
    repeats: int = 1,
    state_machine_arn: str | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
) -> RunRecord:
    """Start one state-machine sweep and return its ``queued`` record carrying the execution ARN.

    Order: authorize; reconcile and take the lock; for ``live``, count it against the daily cap and
    estimate it; write the record; start the execution ``name=<run_id>`` with input
    ``{run_id, tenant, mode, provider_mode}``; record the ARN. A failure after the lock is taken
    releases it; a failed start also marks the record ``failed``.

    Args:
        store: Where the record, lock and quota rows live.
        sfn: A Step Functions client exposing ``start_execution`` and ``describe_execution``.
        identity: The channel-decoded caller.
        mode: ``"stub"`` or ``"live"``.
        confirmed: For ``live``, whether the caller confirmed the shown estimate.
        repeats: Repeats per arm, 1 to :data:`eval.runs.MAX_SWEEP_REPEATS`; each runs every golden
            task in its own unit tenant, and the live estimate is multiplied by it.
        state_machine_arn: Defaults to :data:`EVAL_STATE_MACHINE_ARN_ENV_VAR`.
        run_id: Defaults to a fresh random id.
        now: Defaults to this instant, UTC.

    Raises:
        ScopeDenied: ``live`` by a non-admin (``eval:admin``) or ``stub`` without full visibility.
        ValidationFailed: ``mode`` is unknown, ``repeats`` is out of range, or ``live`` without
            ``confirmed=True``.
        StateMachineNotConfiguredError: No state machine ARN is configured.
        Conflict: A sweep holds the lock, or the daily live cap is reached.
    """
    from eval.runs import (
        acquire_lock,
        consume_live_quota,
        create_run,
        estimate,
        execution_arn_for,
        read_lock,
        reconcile_lock,
        validate_repeats,
    )
    from eval.tenancy import allocate_run_id

    validate_repeats(repeats)

    if mode == "live":
        require_eval_admin(identity, operation="eval_trigger:submit:live")
        if confirmed is not True:
            raise ValidationFailed(
                "A live sweep spends money; confirm the estimate to start it.",
                source="confirmed",
            )
    elif mode == "stub":
        require_eval_trigger_scope(identity, operation="eval_trigger:submit:stub")
    else:
        raise ValidationFailed(f"Unknown sweep mode {mode!r}.", source="mode")
    arn = state_machine_arn or os.environ.get(EVAL_STATE_MACHINE_ARN_ENV_VAR, "").strip()
    if not arn:
        raise StateMachineNotConfiguredError(
            "Evaluation sweeps are not configured on this deployment.",
            detail=f"{EVAL_STATE_MACHINE_ARN_ENV_VAR} is not set, so no sweep can be started.",
        )

    resolved_now = now if now is not None else datetime.now(UTC)
    resolved_run_id = run_id if run_id is not None else allocate_run_id()
    try:
        execution_arn_for(arn, resolved_run_id)
    except ValueError as exc:
        raise StateMachineNotConfiguredError(
            "Evaluation sweeps are not configured on this deployment.",
            detail=f"{EVAL_STATE_MACHINE_ARN_ENV_VAR} is not a state machine ARN: {exc}",
        ) from exc
    tenant = tenant_for_run(resolved_run_id)
    families, task_count = sweep_shape()

    held = reconcile_lock(store, sfn, now=resolved_now, state_machine_arn=arn)
    if held is not None or not acquire_lock(store, run_id=resolved_run_id, now=resolved_now):
        current = read_lock(store)
        holder = current.holder_run_id if current is not None else "another sweep"
        raise Conflict(
            f"A sweep is already running ({holder}); wait for it to finish or cancel it.",
            detail=f"lock#sweep held by {holder}",
        )
    quota_taken = False
    try:
        sweep_estimate = None
        if mode == "live":
            sweep_estimate = estimate(
                store, task_count=task_count, families=families, repeats=repeats
            )
            consume_live_quota(store, now=resolved_now)
            quota_taken = True
        create_run(
            store,
            run_id=resolved_run_id,
            mode=mode,
            identity=identity,
            tasks_total=len(families) * repeats * task_count,
            repeats=repeats,
            estimate_usd=None if sweep_estimate is None else sweep_estimate.estimate_usd,
            estimate_basis=None if sweep_estimate is None else sweep_estimate.basis,
            now=resolved_now,
        )
    except BaseException:
        _cleanup(
            store,
            resolved_run_id,
            mark_run_failed=False,
            refund_quota=quota_taken,
            now=resolved_now,
        )
        raise

    execution_arn = _start_execution(
        store,
        sfn,
        state_machine_arn=arn,
        run_id=resolved_run_id,
        execution_input={
            "run_id": resolved_run_id,
            "tenant": tenant,
            "mode": mode,
            "provider_mode": mode,
        },
        refund_quota=quota_taken,
        now=resolved_now,
    )
    return _record_execution_arn(store, run_id=resolved_run_id, execution_arn=execution_arn)


def _cleanup(
    store: Store, run_id: str, *, mark_run_failed: bool, refund_quota: bool, now: datetime
) -> None:
    """Undo a submission that never started, each step independently; failures are logged only."""
    from eval.runs import mark_failed, refund_live_quota, release_lock

    steps: list[tuple[str, Callable[[], object]]] = []
    if mark_run_failed:
        steps.append(
            (
                "mark_failed",
                lambda: mark_failed(
                    store,
                    run_id=run_id,
                    failure_summary="The state machine execution could not be started.",
                    now=now,
                ),
            )
        )
    steps.append(("release_lock", lambda: release_lock(store, run_id=run_id)))
    if refund_quota:
        steps.append(("refund_live_quota", lambda: refund_live_quota(store, now=now)))
    for name, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - logged; the caller re-raises the original error
            _logger.error(
                json.dumps(
                    {
                        "event": "eval_sweep_cleanup_failed",
                        "correlation_id": run_id,
                        "reason": _truncate(f"step={name}: {type(exc).__name__}: {exc}"),
                    }
                )
            )


def _start_execution(
    store: Store,
    sfn: Any,
    *,
    state_machine_arn: str,
    run_id: str,
    execution_input: dict[str, str],
    refund_quota: bool,
    now: datetime,
) -> str:
    """Start the execution named ``run_id`` and return its ARN, probing it when the start is unclear.

    A start whose outcome is unknown (a client-side timeout, ``ExecutionAlreadyExists``, a response
    without ``executionArn``) is resolved by describing the execution's deterministic ARN: if it
    exists the sweep is running and its ARN is returned. Only a confirmed absence undoes the
    submission (record ``failed``, lock released, quota refunded). An unresolvable outcome leaves
    the record ``queued`` and the lock held, for :func:`eval.runs.reconcile` to settle.

    Raises:
        CrmError: The execution did not start, or whether it started could not be determined.
    """
    from eval.runs import execution_arn_for

    deterministic = execution_arn_for(state_machine_arn, run_id)
    try:
        response = sfn.start_execution(
            stateMachineArn=state_machine_arn, name=run_id, input=json.dumps(execution_input)
        )
        return str(response["executionArn"])
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in _DEFINITE_START_REJECTIONS:
            _logger.error(
                json.dumps(
                    {
                        "event": "eval_sweep_start_rejected",
                        "correlation_id": run_id,
                        "reason": f"StartExecution error code {code}",
                    }
                )
            )
            _cleanup(store, run_id, mark_run_failed=True, refund_quota=refund_quota, now=now)
            raise wrap_aws_error(exc, operation="start_execution") from exc
        start_error: BaseException = exc
    except Exception as exc:  # noqa: BLE001 - the outcome is probed below, then re-raised
        start_error = exc

    try:
        response = sfn.describe_execution(executionArn=deterministic)
    except ClientError as probe_exc:
        if probe_exc.response.get("Error", {}).get("Code") == "ExecutionDoesNotExist":
            _cleanup(store, run_id, mark_run_failed=True, refund_quota=refund_quota, now=now)
            raise CrmError(
                "The evaluation sweep could not be started.",
                detail=f"start_execution failed ({type(start_error).__name__}) and no execution exists",
            ) from start_error
        _logger.error(
            json.dumps(
                {
                    "event": "eval_sweep_start_unknown",
                    "correlation_id": run_id,
                    "reason": f"start and probe both failed for execution {deterministic}",
                }
            )
        )
        raise CrmError(
            "The evaluation sweep may have started; check its status shortly.",
            detail=f"start and probe both failed for {deterministic}",
        ) from start_error
    _logger.warning(
        json.dumps(
            {
                "event": "eval_sweep_start_recovered",
                "correlation_id": run_id,
                "reason": f"execution {deterministic} exists with status {response.get('status')}",
            }
        )
    )
    return deterministic


def _record_execution_arn(store: Store, *, run_id: str, execution_arn: str) -> RunRecord:
    """Record a started execution's ARN, retrying once; the lock stays held if both attempts fail.

    Raises:
        CrmError: Both writes failed. The sweep is running; reconcile records the ARN later.
    """
    from eval.runs import set_execution_arn

    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            return set_execution_arn(store, run_id=run_id, execution_arn=execution_arn)
        except Exception as exc:  # noqa: BLE001 - retried once, then reported with the ARN
            last_error = exc
    _logger.error(
        json.dumps(
            {
                "event": "eval_sweep_arn_unrecorded",
                "correlation_id": run_id,
                "reason": _truncate(
                    f"execution {execution_arn} started; {type(last_error).__name__}: {last_error}"
                ),
            }
        )
    )
    raise CrmError(
        "The evaluation sweep started, but its record could not be updated; check its status.",
        detail=f"execution {execution_arn} started; set_execution_arn failed twice: {last_error}",
    ) from last_error


def _truncate(message: str) -> str:
    """Bound a logged failure reason to :data:`_MAX_ERROR_LENGTH`, marking it when cut."""
    if len(message) <= _MAX_ERROR_LENGTH:
        return message
    return message[: _MAX_ERROR_LENGTH - 3] + "..."


#: Wire key for each :attr:`eval.harness.SweepResult.artifact_paths` key.
_ARTIFACT_WIRE_KEYS: Final[dict[str, str]] = {
    "json": "matrix_json",
    "md": "matrix_md",
    "csv": "matrix_csv",
}


def _artifact_links(result: SweepResult, *, output_dir: Path | None) -> dict[str, Any] | None:
    """Render where a sweep's artifacts were written, or ``None`` if none were.

    Paths are relative to ``output_dir``. A path outside it is reported as its own string.

    Args:
        result: The completed sweep, read for ``artifact_paths`` and ``raw_archive_paths``.
        output_dir: The directory the sweep wrote into, or ``None`` when it wrote nothing.

    Returns:
        A mapping carrying ``raw`` plus one key per written top-level artifact, or ``None`` when the
        sweep produced no artifacts at all (``output_dir`` was not supplied).

    Raises:
        KeyError: ``artifact_paths`` carries a key :data:`_ARTIFACT_WIRE_KEYS` does not name.
    """
    if output_dir is None or not result.artifact_paths:
        return None

    def relative(path: Path) -> str:
        try:
            return str(path.relative_to(output_dir))
        except ValueError:
            return str(path)

    links: dict[str, Any] = {
        _ARTIFACT_WIRE_KEYS[name]: relative(path)
        for name, path in sorted(result.artifact_paths.items())
    }
    links["raw"] = [relative(path) for path in result.raw_archive_paths]
    return links


__all__ = [
    "EVAL_ADMIN_SCOPE",
    "EVAL_STATE_MACHINE_ARN_ENV_VAR",
    "EVAL_TRIGGER_SCOPE",
    "StateMachineNotConfiguredError",
    "job_row_id",
    "require_eval_admin",
    "require_eval_trigger_scope",
    "run_id_of_job_row",
    "submit_sweep",
    "sweep_shape",
]
