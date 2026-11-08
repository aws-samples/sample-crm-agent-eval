# SPDX-License-Identifier: MIT
"""State-machine sweep run records, the single-flight lock, cancel, reconcile, list and estimate.

A run is one ``eval_runs`` job row ``job#<run_id>`` (:class:`RunRecord`). The lock is the row
``lock#sweep``; every state-machine sweep, stub or live, holds it from submit to finalize.
Per-task results are ``eval_results`` rows keyed by :func:`result_id`.

Every write is conditional: :func:`acquire_lock` creates the lock only if absent,
:func:`release_lock` deletes it only for its holder, :func:`request_cancel` resolves racing callers to
one ``accepted`` and the rest ``already_requested``, and terminal statuses are written only over a
non-terminal record. :func:`reconcile` reads a non-terminal record's Step Functions execution and
marks the record ``failed`` when the execution ended without finalizing. The ``sfn`` argument
is any client exposing ``describe_execution(executionArn=...)``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from botocore.exceptions import ClientError

from crm.authz import Identity
from crm.errors import (
    Conflict,
    CrmError,
    NotFound,
    ValidationFailed,
    bounded_detail_value,
    wrap_aws_error,
)
from crm.store import keys
from crm.store.store import Store
from eval.harness import EvalMode
from eval.matrix import tier_comparison_table
from eval.sweep_units import DEFAULT_ARM_EFFORT

_logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "running", "cancelling", "completed", "cancelled", "failed"]
Disposition = Literal["accepted", "already_requested", "already_terminal"]
TeardownStatus = Literal["pending", "ok", "failed", "unknown"]
ItemStatus = Literal["done", "failed", "skipped_cancelled"]

JOB_MODES: Final[tuple[EvalMode, ...]] = ("stub", "live")
JOB_STATUSES: Final[tuple[JobStatus, ...]] = (
    "queued",
    "running",
    "cancelling",
    "completed",
    "cancelled",
    "failed",
)
TERMINAL_JOB_STATUSES: Final[frozenset[JobStatus]] = frozenset({"completed", "cancelled", "failed"})
TEARDOWN_STATUSES: Final[tuple[TeardownStatus, ...]] = ("pending", "ok", "failed", "unknown")
ITEM_STATUSES: Final[tuple[ItemStatus, ...]] = ("done", "failed", "skipped_cancelled")

#: Stored statuses written by the retired worker path, read as their current equivalents.
LEGACY_STATUS_MAP: Final[Mapping[str, JobStatus]] = {
    "submitted": "queued",
    "succeeded": "completed",
}

#: Step Functions execution statuses that end a run without its Finalize step.
SFN_FAILURE_STATUSES: Final[frozenset[str]] = frozenset({"FAILED", "TIMED_OUT", "ABORTED"})

#: Age after which a ``queued`` record with no execution, or a lock with no record, is stale.
STALE_QUEUED_SECONDS: Final[int] = 300

#: History page size.
LIST_PAGE_SIZE: Final[int] = 20

#: Live sweeps admitted per UTC calendar day. Stub sweeps are not counted.
LIVE_SWEEPS_PER_UTC_DAY: Final[int] = 10

#: The most repeats one state-machine sweep may run.
MAX_SWEEP_REPEATS: Final[int] = 5

EVAL_RUNS_TABLE: Final[str] = "eval_runs"
EVAL_RESULTS_TABLE: Final[str] = "eval_results"

#: Sentinel ``status`` of every job row: the ``gsi_status`` partition the history is read from.
JOB_ROW_STATUS: Final[str] = "job"

_JOB_ROW_PREFIX: Final[str] = "job#"
_LIVE_QUOTA_ROW_PREFIX: Final[str] = "livequota#"
_MAX_FAILURE_SUMMARY: Final[int] = 1024
_CURSOR_KEYS: Final[frozenset[str]] = frozenset({"run_id", "status", "started_at"})

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DEFAULT_ESTIMATE_PATH: Final[Path] = _REPO_ROOT / "config" / "eval_live_estimate.yaml"

#: Environment variable naming the ``eval_sweep`` state machine, from which an execution's ARN is
#: derived when a run has none recorded (the execution's name is the run id).
EVAL_STATE_MACHINE_ARN_ENV_VAR: Final[str] = "EVAL_STATE_MACHINE_ARN"

#: History pages :func:`estimate` reads looking for a completed live run before using the static file.
_ESTIMATE_HISTORY_PAGES: Final[int] = 5


def job_row_id(run_id: str) -> str:
    """Return the ``eval_runs`` key of ``run_id``'s job row: ``"job#<run_id>"``.

    Raises:
        ValueError: ``run_id`` is empty or already a job row id.
    """
    if not run_id:
        raise ValueError("run_id must not be empty")
    if run_id.startswith(_JOB_ROW_PREFIX):
        raise ValueError(f"already a job row id, refusing to double-prefix: {run_id!r}")
    return f"{_JOB_ROW_PREFIX}{run_id}"


def run_id_of_job_row(row_id: str) -> str:
    """Return the run id in a job row key, the inverse of :func:`job_row_id`.

    Raises:
        ValueError: ``row_id`` does not carry the job row prefix.
    """
    if not row_id.startswith(_JOB_ROW_PREFIX):
        raise ValueError(f"not a job row id: {row_id!r}")
    return row_id[len(_JOB_ROW_PREFIX) :]


def validate_repeats(raw: object) -> int:
    """Return a sweep's ``repeats`` argument as an int; ``None`` (absent) is 1.

    Raises:
        ValidationFailed: ``raw`` is a bool, not an int, or outside ``[1, MAX_SWEEP_REPEATS]``.
    """
    if raw is None:
        return 1
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= MAX_SWEEP_REPEATS:
        raise ValidationFailed(
            f"repeats must be an integer from 1 to {MAX_SWEEP_REPEATS}.",
            source="repeats",
            detail=f"received repeats={bounded_detail_value(raw)} ({type(raw).__name__})",
        )
    return raw


def result_id(arm_index: int, repeat_index: int, task_id: str) -> str:
    """The ``eval_results`` sort key of one item.

    Repeat 0 is ``"<arm_index:02d>#<task_id>"`` (:func:`crm.store.keys.eval_result_id`); repeat
    ``r >= 1`` appends ``"#r<r:02d>"``.

    Raises:
        ValueError: An index is out of range or ``task_id`` is empty.
    """
    base = keys.eval_result_id(arm_index, task_id)
    if isinstance(repeat_index, bool) or not isinstance(repeat_index, int):
        raise ValueError(f"repeat_index must be an int, got {type(repeat_index).__name__}")
    if not 0 <= repeat_index < MAX_SWEEP_REPEATS:
        raise ValueError(f"repeat_index must be in [0, {MAX_SWEEP_REPEATS}), got {repeat_index}")
    return base if repeat_index == 0 else f"{base}#r{repeat_index:02d}"


def terminal_status(*, tasks_failed: int, tasks_skipped: int, teardown_ok: bool) -> JobStatus:
    """The terminal status: ``failed`` if teardown failed or any item failed, else
    ``cancelled`` if any item was skipped for cancel, else ``completed``."""
    if not teardown_ok or tasks_failed > 0:
        return "failed"
    if tasks_skipped > 0:
        return "cancelled"
    return "completed"


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One sweep's job row, as read (legacy statuses mapped).

    Attributes mirror the stored row's attributes; ``status`` is ``job_status`` and ``mode`` is ``job_mode``.
    ``matrix_report`` and ``artifact_links`` are the parsed JSON objects, or ``None``.
    """

    run_id: str
    tenant: str
    mode: EvalMode
    status: JobStatus
    started_at: str
    started_by_rep_id: str | None = None
    running_at: str | None = None
    finished_at: str | None = None
    repeats: int = 1
    tasks_total: int = 0
    tasks_done: int = 0
    tasks_failed: int = 0
    tasks_skipped: int = 0
    cost_spent_usd: float = 0.0
    cancel_requested: bool = False
    cancel_requested_by: str | None = None
    cancel_requested_at: str | None = None
    execution_arn: str | None = None
    estimate_usd: float | None = None
    estimate_basis: str | None = None
    teardown_status: TeardownStatus | None = None
    failure_summary: str | None = None
    matrix_report: dict[str, Any] | None = None
    artifact_links: dict[str, Any] | None = None

    @property
    def is_terminal(self) -> bool:
        """Whether the run has reached ``completed``, ``cancelled`` or ``failed``."""
        return self.status in TERMINAL_JOB_STATUSES

    def to_payload(self) -> dict[str, Any]:
        """The wire shape the Evals page reads; absent optional fields are omitted.

        The execution ARN is not emitted. ``tier_comparison_table`` is the
        :func:`eval.matrix.tier_comparison_table` of the matrix's ``tier_comparison`` block when
        the block is a mapping; a block that cannot be formatted is omitted and logged.
        """
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "tenant": self.tenant,
            "status": self.status,
            "mode": self.mode,
            "submitted_at": self.started_at,
            "is_terminal": self.is_terminal,
            "repeats": self.repeats,
            "tasks_total": self.tasks_total,
            "tasks_done": self.tasks_done,
            "tasks_failed": self.tasks_failed,
            "tasks_skipped": self.tasks_skipped,
            "cost_spent_usd": round(self.cost_spent_usd, 6),
            "cancel_requested": self.cancel_requested,
        }
        optional: dict[str, Any] = {
            "started_by_rep_id": self.started_by_rep_id,
            "running_at": self.running_at,
            "finished_at": self.finished_at,
            "cancel_requested_by": self.cancel_requested_by,
            "cancel_requested_at": self.cancel_requested_at,
            "estimate_usd": None if self.estimate_usd is None else round(self.estimate_usd, 2),
            "estimate_basis": self.estimate_basis,
            "teardown_status": self.teardown_status,
            "error": self.failure_summary,
            "matrix": self.matrix_report,
            "tier_comparison_table": _payload_tier_table(self.run_id, self.matrix_report),
            "artifacts": self.artifact_links,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        return payload


#: Longest refusal text an ``eval_tier_table_unavailable`` line carries.
_MAX_TIER_TABLE_REASON: Final[int] = 300


def _payload_tier_table(
    run_id: str, matrix_report: Mapping[str, Any] | None
) -> dict[str, object] | None:
    """The display table of ``matrix_report``'s tier comparison block, or ``None``.

    A block :func:`eval.matrix.tier_comparison_table` refuses as malformed (``ValueError``) is
    omitted and logged with the refusal's text; any other exception propagates.
    """
    summary = matrix_report.get("tier_comparison") if matrix_report is not None else None
    if not isinstance(summary, Mapping):
        return None
    try:
        return tier_comparison_table(summary)
    except ValueError as exc:
        _logger.warning(
            json.dumps(
                {
                    "event": "eval_tier_table_unavailable",
                    "correlation_id": run_id,
                    "reason": str(exc)[:_MAX_TIER_TABLE_REASON],
                }
            )
        )
        return None


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _truncate(message: str) -> str:
    if len(message) <= _MAX_FAILURE_SUMMARY:
        return message
    return message[: _MAX_FAILURE_SUMMARY - 3] + "..."


def _unreadable(row_id: str, field: str, raw: object) -> ValidationFailed:
    return ValidationFailed(
        "This evaluation run's stored state could not be read.",
        source=field,
        detail=f"eval_runs row {row_id!r} carries {field}={raw!r}",
    )


def _read_status(raw: object, row_id: str) -> JobStatus:
    if isinstance(raw, str) and raw in LEGACY_STATUS_MAP:
        return LEGACY_STATUS_MAP[raw]
    for status in JOB_STATUSES:
        if raw == status:
            return status
    raise _unreadable(row_id, "job_status", raw)


def _read_mode(raw: object, row_id: str) -> EvalMode:
    for mode in JOB_MODES:
        if raw == mode:
            return mode
    raise _unreadable(row_id, "job_mode", raw)


def _read_teardown(raw: object, row_id: str) -> TeardownStatus | None:
    if raw is None:
        return None
    for status in TEARDOWN_STATUSES:
        if raw == status:
            return status
    raise _unreadable(row_id, "teardown_status", raw)


def _read_str(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def _read_count(raw: object, row_id: str, field: str) -> int:
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int | float) or raw < 0 or raw != int(raw):
        raise _unreadable(row_id, field, raw)
    return int(raw)


def _read_repeats(raw: object, row_id: str) -> int:
    if raw is None:
        return 1
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int | float)
        or raw != int(raw)
        or not 1 <= raw <= MAX_SWEEP_REPEATS
    ):
        raise _unreadable(row_id, "repeats", raw)
    return int(raw)


def _read_amount(raw: object, row_id: str, field: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise _unreadable(row_id, field, raw)
    return float(raw)


def _read_json_object(raw: object, row_id: str, field: str) -> dict[str, Any] | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise _unreadable(row_id, field, type(raw).__name__)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _unreadable(row_id, field, f"unparseable JSON ({exc})") from exc
    if not isinstance(parsed, dict):
        raise _unreadable(row_id, field, f"JSON {type(parsed).__name__}")
    return parsed


def record_from_item(row: Mapping[str, object]) -> RunRecord:
    """Build a :class:`RunRecord` from a stored job row, mapping legacy statuses.

    Raises:
        ValidationFailed: A status, mode, count, amount or JSON attribute is present but unreadable.
    """
    row_id = str(row.get("run_id", ""))
    run_id = run_id_of_job_row(row_id)
    return RunRecord(
        run_id=run_id,
        tenant=str(row.get("tenant") or f"eval-{run_id}"),
        mode=_read_mode(row.get("job_mode"), row_id),
        status=_read_status(row.get("job_status"), row_id),
        started_at=str(row.get("started_at") or ""),
        started_by_rep_id=_read_str(row.get("started_by_rep_id")),
        running_at=_read_str(row.get("running_at")),
        finished_at=_read_str(row.get("finished_at")),
        repeats=_read_repeats(row.get("repeats"), row_id),
        tasks_total=_read_count(row.get("tasks_total"), row_id, "tasks_total"),
        tasks_done=_read_count(row.get("tasks_done"), row_id, "tasks_done"),
        tasks_failed=_read_count(row.get("tasks_failed"), row_id, "tasks_failed"),
        tasks_skipped=_read_count(row.get("tasks_skipped"), row_id, "tasks_skipped"),
        cost_spent_usd=_read_amount(row.get("cost_spent_usd"), row_id, "cost_spent_usd") or 0.0,
        cancel_requested=row.get("cancel_requested") is True,
        cancel_requested_by=_read_str(row.get("cancel_requested_by")),
        cancel_requested_at=_read_str(row.get("cancel_requested_at")),
        execution_arn=_read_str(row.get("execution_arn")),
        estimate_usd=_read_amount(row.get("estimate_usd"), row_id, "estimate_usd"),
        estimate_basis=_read_str(row.get("estimate_basis")),
        teardown_status=_read_teardown(row.get("teardown_status"), row_id),
        failure_summary=_read_str(row.get("failure_summary")) or _read_str(row.get("error")),
        matrix_report=_read_json_object(row.get("matrix_report"), row_id, "matrix_report"),
        artifact_links=_read_json_object(row.get("artifact_links"), row_id, "artifact_links"),
    )


def read_run(store: Store, run_id: str) -> RunRecord | None:
    """Return ``run_id``'s record, or ``None`` if no job row exists."""
    row = store.get_item(EVAL_RUNS_TABLE, {"run_id": job_row_id(run_id)})
    return None if row is None else record_from_item(row)


def require_run(store: Store, run_id: str) -> RunRecord:
    """Return ``run_id``'s record.

    Raises:
        NotFound: No job row exists for ``run_id``.
    """
    record = read_run(store, run_id)
    if record is None:
        raise NotFound(f"No such evaluation run: {run_id!r}.")
    return record


def create_run(
    store: Store,
    *,
    run_id: str,
    mode: EvalMode,
    identity: Identity,
    tasks_total: int,
    repeats: int = 1,
    estimate_usd: float | None = None,
    estimate_basis: str | None = None,
    now: datetime | None = None,
) -> RunRecord:
    """Write a new ``queued`` job row for ``run_id`` and return it.

    ``tasks_total`` counts every ``(arm, repeat, task)`` item.

    Raises:
        Conflict: A job row already exists for ``run_id``.
        ValidationFailed: ``repeats`` is not accepted by :func:`validate_repeats`.
    """
    validate_repeats(repeats)
    started_at = _now(now).isoformat()
    item: dict[str, object] = {
        "run_id": job_row_id(run_id),
        "status": JOB_ROW_STATUS,
        "started_at": started_at,
        "sweep_run_id": run_id,
        "tenant": f"eval-{run_id}",
        "job_mode": mode,
        "job_status": "queued",
        "started_by_rep_id": identity.rep_id,
        "repeats": repeats,
        "tasks_total": tasks_total,
        "tasks_done": 0,
        "tasks_failed": 0,
        "tasks_skipped": 0,
        "cost_spent_usd": 0,
        "cancel_requested": False,
        "teardown_status": "pending",
    }
    if estimate_usd is not None:
        item["estimate_usd"] = estimate_usd
        item["estimate_basis"] = estimate_basis
    if not store.put_if_absent(EVAL_RUNS_TABLE, item):
        raise Conflict(f"An evaluation run {run_id!r} already exists.")
    return record_from_item(item)


# -- lock ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SweepLock:
    """The ``lock#sweep`` row: which run holds it, since when, and its execution once started."""

    holder_run_id: str
    acquired_at: str
    execution_arn: str | None = None


def read_lock(store: Store) -> SweepLock | None:
    """Return the current lock (a strongly consistent read), or ``None`` when no sweep holds it."""
    row = store.get_item(EVAL_RUNS_TABLE, {"run_id": keys.SWEEP_LOCK_ROW_ID}, consistent=True)
    if row is None:
        return None
    holder = row.get("holder_run_id")
    if not isinstance(holder, str) or not holder:
        raise _unreadable(keys.SWEEP_LOCK_ROW_ID, "holder_run_id", holder)
    return SweepLock(
        holder_run_id=holder,
        acquired_at=str(row.get("acquired_at") or ""),
        execution_arn=_read_str(row.get("execution_arn")),
    )


def acquire_lock(store: Store, *, run_id: str, now: datetime | None = None) -> bool:
    """Take the lock for ``run_id`` if no sweep holds it; ``True`` when this call took it."""
    return store.put_if_absent(
        EVAL_RUNS_TABLE,
        {
            "run_id": keys.SWEEP_LOCK_ROW_ID,
            "holder_run_id": run_id,
            "acquired_at": _now(now).isoformat(),
        },
    )


def release_lock(store: Store, *, run_id: str) -> bool:
    """Delete the lock only if ``run_id`` holds it; ``True`` when this call deleted it.

    A ``DeleteItem`` conditioned on ``holder_run_id``, issued through
    :meth:`~crm.store.store.Store.transact_write`.

    Raises:
        CrmError: The conditional delete failed while the lock still names ``run_id``.
    """
    try:
        store.transact_write(
            [
                {
                    "Delete": {
                        "TableName": EVAL_RUNS_TABLE,
                        "Key": {"run_id": {"S": keys.SWEEP_LOCK_ROW_ID}},
                        "ConditionExpression": "#holder = :run_id",
                        "ExpressionAttributeNames": {"#holder": "holder_run_id"},
                        "ExpressionAttributeValues": {":run_id": {"S": run_id}},
                    }
                }
            ]
        )
    except Conflict as exc:
        current = read_lock(store)
        if current is not None and current.holder_run_id == run_id:
            raise CrmError(
                "The sweep lock could not be released.",
                detail=f"holder {run_id!r} still holds lock#sweep after a failed delete: {exc}",
            ) from exc
        return False
    return True


_ARN_UNSET_OR_EQUAL: Final[str] = "attribute_not_exists(#arn) OR #arn = :arn"


def _write_execution_arn(
    store: Store, *, run_id: str, execution_arn: str, lock: bool
) -> dict[str, object]:
    """Write ``execution_arn`` to the lock (when ``lock``) and then the job row; return the job row.

    Each write is conditioned on the row carrying no ARN or this one, and the lock's also on naming
    ``run_id``, so a refused write leaves the job row unchanged.
    """
    if lock:
        store.update_item(
            EVAL_RUNS_TABLE,
            {"run_id": keys.SWEEP_LOCK_ROW_ID},
            set_values={"execution_arn": execution_arn},
            condition=f"#holder = :run_id AND ({_ARN_UNSET_OR_EQUAL})",
            names={"#holder": "holder_run_id", "#arn": "execution_arn"},
            values={":run_id": run_id, ":arn": execution_arn},
        )
    return store.update_item(
        EVAL_RUNS_TABLE,
        {"run_id": job_row_id(run_id)},
        set_values={"execution_arn": execution_arn},
        condition=_ARN_UNSET_OR_EQUAL,
        names={"#arn": "execution_arn"},
        values={":arn": execution_arn},
    )


def set_execution_arn(
    store: Store, *, run_id: str, execution_arn: str, lock: bool = True
) -> RunRecord:
    """Record the started execution on ``run_id``'s job row and, unless ``lock`` is false, on the lock.

    Raises:
        Conflict: The lock no longer names ``run_id`` (when ``lock``), or either row already
            records another execution. The job row is then unchanged; the lock may already record
            ``execution_arn`` when only the job row refused.
    """
    return record_from_item(
        _write_execution_arn(store, run_id=run_id, execution_arn=execution_arn, lock=lock)
    )


#: Why :func:`claim_execution` did or did not grant ownership.
ClaimOutcome = Literal["owner", "claimed", "owned_by_other", "lock_not_held", "lock_changed"]

#: The outcomes under which the execution owns the run.
OWNING_OUTCOMES: Final[frozenset[str]] = frozenset({"owner", "claimed"})


def claim_execution(store: Store, *, run_id: str, execution_arn: str) -> ClaimOutcome:
    """Whether ``execution_arn`` owns ``run_id``, claiming the run when neither row records one.

    The owner is the execution the job row records, else the one the lock records while it names
    ``run_id`` (copied to the job row when it lacks it). With neither recorded, the run is claimed
    for ``execution_arn`` on both rows when the lock names ``run_id`` and the job row exists. A run
    with no job row is owned, with nothing written, while the lock names it and records no other
    execution.

    Returns:
        ``"owner"`` or ``"claimed"`` (see :data:`OWNING_OUTCOMES`) when ``execution_arn`` owns the
        run; ``"owned_by_other"`` when another execution is recorded; ``"lock_not_held"`` when the
        lock does not name ``run_id``; ``"lock_changed"`` when the conditional claim was refused
        between the read and the write.
    """
    row = store.get_item(EVAL_RUNS_TABLE, {"run_id": job_row_id(run_id)}, consistent=True)
    recorded = None if row is None else _read_str(row.get("execution_arn"))
    if recorded is not None:
        return "owner" if recorded == execution_arn else "owned_by_other"
    lock = read_lock(store)
    if lock is None or lock.holder_run_id != run_id:
        return "lock_not_held"
    if lock.execution_arn is not None:
        if lock.execution_arn != execution_arn:
            return "owned_by_other"
        if row is not None:
            _write_execution_arn(store, run_id=run_id, execution_arn=execution_arn, lock=False)
        return "owner"
    if row is None:
        return "owner"
    try:
        _write_execution_arn(store, run_id=run_id, execution_arn=execution_arn, lock=True)
    except Conflict:
        return "lock_changed"
    return "claimed"


# -- daily live cap ------------------------------------------------------------------------------


def consume_live_quota(store: Store, *, now: datetime | None = None) -> int:
    """Count one live sweep against today's UTC cap and return today's count.

    Raises:
        Conflict: :data:`LIVE_SWEEPS_PER_UTC_DAY` live sweeps were already admitted today.
    """
    day = _now(now).astimezone(UTC).date().isoformat()
    try:
        row = store.update_item(
            EVAL_RUNS_TABLE,
            {"run_id": f"{_LIVE_QUOTA_ROW_PREFIX}{day}"},
            set_values={"quota_day": day},
            add_values={"live_sweeps": 1},
            condition="attribute_not_exists(#count) OR #count < :cap",
            names={"#count": "live_sweeps"},
            values={":cap": LIVE_SWEEPS_PER_UTC_DAY},
            create_if_absent=True,
        )
    except Conflict as exc:
        raise Conflict(
            f"The daily limit of {LIVE_SWEEPS_PER_UTC_DAY} live sweeps per UTC day has been "
            f"reached for {day}. Try again after 00:00 UTC.",
            detail=f"livequota#{day} at cap: {exc}",
        ) from exc
    return int(str(row.get("live_sweeps", 0)))


def refund_live_quota(store: Store, *, now: datetime | None = None) -> None:
    """Return one live sweep to today's UTC count, for a sweep that was admitted but never started."""
    day = _now(now).astimezone(UTC).date().isoformat()
    store.update_item(
        EVAL_RUNS_TABLE,
        {"run_id": f"{_LIVE_QUOTA_ROW_PREFIX}{day}"},
        set_values={"quota_day": day},
        add_values={"live_sweeps": -1},
        condition="#count > :zero",
        names={"#count": "live_sweeps"},
        values={":zero": 0},
    )


def execution_arn_for(state_machine_arn: str, run_id: str) -> str:
    """The ARN of the execution named ``run_id`` on ``state_machine_arn``.

    Raises:
        ValueError: ``state_machine_arn`` is not a Step Functions state machine ARN.
    """
    parts = state_machine_arn.split(":")
    if len(parts) != 7 or parts[:3] != ["arn", parts[1], "states"] or parts[5] != "stateMachine":
        raise ValueError(f"not a state machine ARN: {state_machine_arn!r}")
    return ":".join([*parts[:5], "execution", parts[6], run_id])


# -- run transitions -----------------------------------------------------------------------------

#: Condition placeholders naming every stored terminal value, legacy included.
_NOT_TERMINAL_CONDITION: Final[str] = (
    "NOT (#status IN (:completed, :cancelled, :failed, :succeeded))"
)
_NOT_TERMINAL_VALUES: Final[Mapping[str, object]] = {
    ":completed": "completed",
    ":cancelled": "cancelled",
    ":failed": "failed",
    ":succeeded": "succeeded",
}


def mark_running(store: Store, *, run_id: str, now: datetime | None = None) -> RunRecord:
    """Move a ``queued`` run to ``running``.

    Raises:
        Conflict: The run is not ``queued``.
    """
    row = store.update_item(
        EVAL_RUNS_TABLE,
        {"run_id": job_row_id(run_id)},
        set_values={"job_status": "running", "running_at": _now(now).isoformat()},
        condition="#status IN (:queued, :submitted)",
        names={"#status": "job_status"},
        values={":queued": "queued", ":submitted": "submitted"},
    )
    return record_from_item(row)


def request_cancel(
    store: Store, *, run_id: str, identity: Identity, now: datetime | None = None
) -> tuple[Disposition, RunRecord]:
    """Ask ``run_id`` to stop cooperatively; never stops its execution.

    Exactly one of any number of racing callers gets ``accepted``; the others get
    ``already_requested``. A terminal run gets ``already_terminal`` and is not written.

    Raises:
        NotFound: No job row exists for ``run_id``.
    """
    record = require_run(store, run_id)
    if record.is_terminal:
        return "already_terminal", record
    if record.cancel_requested:
        return "already_requested", record
    try:
        row = store.update_item(
            EVAL_RUNS_TABLE,
            {"run_id": job_row_id(run_id)},
            set_values={
                "cancel_requested": True,
                "cancel_requested_by": identity.rep_id,
                "cancel_requested_at": _now(now).isoformat(),
                "job_status": "cancelling",
            },
            condition=(
                f"(attribute_not_exists(#cancel) OR #cancel = :false) AND {_NOT_TERMINAL_CONDITION}"
            ),
            names={"#cancel": "cancel_requested", "#status": "job_status"},
            values={":false": False, **_NOT_TERMINAL_VALUES},
        )
    except Conflict:
        current = require_run(store, run_id)
        if current.is_terminal:
            return "already_terminal", current
        if current.cancel_requested:
            return "already_requested", current
        raise
    return "accepted", record_from_item(row)


def mark_failed(
    store: Store,
    *,
    run_id: str,
    failure_summary: str,
    teardown_status: TeardownStatus = "unknown",
    now: datetime | None = None,
) -> RunRecord:
    """Mark a non-terminal run ``failed``; a run already terminal is returned unchanged."""
    try:
        row = store.update_item(
            EVAL_RUNS_TABLE,
            {"run_id": job_row_id(run_id)},
            set_values={
                "job_status": "failed",
                "finished_at": _now(now).isoformat(),
                "teardown_status": teardown_status,
                "failure_summary": _truncate(failure_summary),
            },
            condition=_NOT_TERMINAL_CONDITION,
            names={"#status": "job_status"},
            values=dict(_NOT_TERMINAL_VALUES),
        )
    except Conflict:
        return require_run(store, run_id)
    return record_from_item(row)


def _attr(value: object) -> dict[str, object]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int | float):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    raise TypeError(f"unsupported attribute type {type(value).__name__}")


def record_item(
    store: Store,
    *,
    run_id: str,
    arm_index: int,
    task_id: str,
    arm: str,
    item_status: ItemStatus,
    repeat_index: int = 0,
    cost_usd: float = 0.0,
    fields: Mapping[str, str | int | float | None] | None = None,
) -> bool:
    """Write one item's result row and count it on the job row, in one transaction.

    The row is keyed by :func:`result_id` and records ``repeat_index``. ``fields`` are extra result
    attributes (JSON already rendered to strings). The result row is written only if absent, so a
    retried item is not counted twice.

    Returns:
        ``True`` when this call recorded the item, ``False`` when it was already recorded.

    Raises:
        Conflict: The transaction failed while the result row is still absent.
    """
    counter = {"done": "tasks_done", "failed": "tasks_failed", "skipped_cancelled": "tasks_skipped"}
    item_key = result_id(arm_index, repeat_index, task_id)
    item = {
        "run_id": run_id,
        "result_id": item_key,
        "tenant": f"eval-{run_id}",
        "arm": arm,
        "repeat_index": repeat_index,
        "task_id": task_id,
        "item_status": item_status,
        "cost_usd": cost_usd,
        **dict(fields or {}),
    }
    operations: list[dict[str, Any]] = [
        {
            "Put": {
                "TableName": EVAL_RESULTS_TABLE,
                "Item": {name: _attr(value) for name, value in item.items()},
                "ConditionExpression": "attribute_not_exists(#sk)",
                "ExpressionAttributeNames": {"#sk": "result_id"},
            }
        },
        {
            "Update": {
                "TableName": EVAL_RUNS_TABLE,
                "Key": {"run_id": {"S": job_row_id(run_id)}},
                "UpdateExpression": "ADD #count :one, #cost :cost",
                "ConditionExpression": "attribute_exists(#pk)",
                "ExpressionAttributeNames": {
                    "#count": counter[item_status],
                    "#cost": "cost_spent_usd",
                    "#pk": "run_id",
                },
                "ExpressionAttributeValues": {":one": {"N": "1"}, ":cost": _attr(cost_usd)},
            }
        },
    ]
    try:
        store.transact_write(operations)
    except Conflict:
        existing = store.get_item(EVAL_RESULTS_TABLE, {"run_id": run_id, "result_id": item_key})
        if existing is not None:
            return False
        raise
    return True


def finalize(
    store: Store, *, run_id: str, teardown_ok: bool, now: datetime | None = None
) -> RunRecord:
    """Write the terminal status and release the lock, which is released on every path.

    A run already terminal keeps its status.
    """
    try:
        record = require_run(store, run_id)
        if record.is_terminal:
            return record
        status = terminal_status(
            tasks_failed=record.tasks_failed,
            tasks_skipped=record.tasks_skipped,
            teardown_ok=teardown_ok,
        )
        try:
            row = store.update_item(
                EVAL_RUNS_TABLE,
                {"run_id": job_row_id(run_id)},
                set_values={
                    "job_status": status,
                    "finished_at": _now(now).isoformat(),
                    "teardown_status": "ok" if teardown_ok else "failed",
                },
                condition=_NOT_TERMINAL_CONDITION,
                names={"#status": "job_status"},
                values=dict(_NOT_TERMINAL_VALUES),
            )
        except Conflict:
            return require_run(store, run_id)
        return record_from_item(row)
    finally:
        release_lock(store, run_id=run_id)


# -- reconcile -----------------------------------------------------------------------------------


def _age_seconds(stamp: str, now: datetime, *, row_id: str, field: str) -> float:
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError as exc:
        raise _unreadable(row_id, field, stamp) from exc
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (now - then).total_seconds()


def _is_stale(stamp: str, now: datetime, *, row_id: str, field: str) -> bool:
    """Whether ``stamp`` is older than :data:`STALE_QUEUED_SECONDS`.

    Raises:
        ValidationFailed: ``stamp`` is not an ISO-8601 timestamp.
    """
    return _age_seconds(stamp, now, row_id=row_id, field=field) > STALE_QUEUED_SECONDS


def _execution_status(sfn: Any, execution_arn: str) -> str:
    """The execution's status, or ``"MISSING"`` when Step Functions reports no such execution."""
    try:
        response = sfn.describe_execution(executionArn=execution_arn)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ExecutionDoesNotExist":
            return "MISSING"
        raise wrap_aws_error(exc, operation="describe_execution") from exc
    return str(response.get("status", ""))


def _configured_state_machine(state_machine_arn: str | None) -> str | None:
    return state_machine_arn or os.environ.get(EVAL_STATE_MACHINE_ARN_ENV_VAR, "").strip() or None


def reconcile(
    store: Store,
    sfn: Any,
    record: RunRecord,
    *,
    now: datetime | None = None,
    state_machine_arn: str | None = None,
) -> RunRecord:
    """Mark a non-terminal run ``failed`` when its execution ended without finalizing.

    The execution is the recorded ARN, or for a ``queued`` run older than
    :data:`STALE_QUEUED_SECONDS` with none recorded, the one named ``run_id`` on the state machine
    (``state_machine_arn``, else :data:`EVAL_STATE_MACHINE_ARN_ENV_VAR`); a found ARN is recorded.
    An execution that is ``FAILED``, ``TIMED_OUT``, ``ABORTED``, missing, or ``SUCCEEDED`` while the
    record is not terminal makes the run ``failed`` with ``teardown_status = "unknown"``, and the
    lock is released if it still names the run. With no recorded ARN and no state machine
    configured, the run is left unchanged.

    Raises:
        ValidationFailed: The record's ``started_at`` is unreadable.
    """
    if record.is_terminal:
        return record
    resolved_now = _now(now)
    arn = record.execution_arn
    if arn is None:
        if record.status != "queued" or not _is_stale(
            record.started_at, resolved_now, row_id=job_row_id(record.run_id), field="started_at"
        ):
            return record
        machine = _configured_state_machine(state_machine_arn)
        if machine is None:
            _logger.warning(
                json.dumps(
                    {
                        "event": "eval_run_reconcile_skipped",
                        "correlation_id": record.run_id,
                        "reason": f"stale queued run with no execution and no {EVAL_STATE_MACHINE_ARN_ENV_VAR}",
                    }
                )
            )
            return record
        arn = execution_arn_for(machine, record.run_id)
    status = _execution_status(sfn, arn)
    if status != "MISSING" and record.execution_arn is None:
        held = read_lock(store)
        holds = held is not None and held.holder_run_id == record.run_id
        record = set_execution_arn(store, run_id=record.run_id, execution_arn=arn, lock=holds)
    if status in SFN_FAILURE_STATUSES or status == "MISSING":
        reason = f"The sweep's state machine execution ended as {status} without finalizing."
    elif status == "SUCCEEDED":
        reason = "The sweep's state machine execution succeeded but the run was never finalized."
    else:
        return record
    _logger.warning(
        json.dumps(
            {"event": "eval_run_reconciled", "correlation_id": record.run_id, "reason": reason}
        )
    )
    reconciled = mark_failed(store, run_id=record.run_id, failure_summary=reason, now=resolved_now)
    lock = read_lock(store)
    if lock is not None and lock.holder_run_id == record.run_id:
        release_lock(store, run_id=record.run_id)
    return reconciled


def reconcile_lock(
    store: Store, sfn: Any, *, now: datetime | None = None, state_machine_arn: str | None = None
) -> SweepLock | None:
    """Release a lock whose holder has finished or never existed; return the lock still held."""
    resolved_now = _now(now)
    lock = read_lock(store)
    if lock is None:
        return None
    holder = read_run(store, lock.holder_run_id)
    if holder is None:
        if not _is_stale(
            lock.acquired_at, resolved_now, row_id=keys.SWEEP_LOCK_ROW_ID, field="acquired_at"
        ):
            return lock
    elif not reconcile(
        store, sfn, holder, now=resolved_now, state_machine_arn=state_machine_arn
    ).is_terminal:
        return read_lock(store)
    release_lock(store, run_id=lock.holder_run_id)
    return read_lock(store)


def unlock(
    store: Store,
    sfn: Any,
    *,
    identity: Identity,
    now: datetime | None = None,
    state_machine_arn: str | None = None,
) -> dict[str, object]:
    """Release the lock unless its holder's execution is running or about to start (admin action).

    A release by ``identity`` logs one ``eval_run_unlocked`` line naming its ``rep_id``; a holder
    failed by the release carries a failure summary naming that rep.

    Returns:
        ``{"released": bool, "holder_run_id": str | None}``; ``released`` is ``False`` when no lock
        was held or another caller changed it first.

    Raises:
        Conflict: The holder's execution is ``RUNNING``, or it is ``queued`` and not yet stale.
    """
    resolved_now = _now(now)
    lock = read_lock(store)
    if lock is None:
        return {"released": False, "holder_run_id": None}
    remaining = reconcile_lock(store, sfn, now=resolved_now, state_machine_arn=state_machine_arn)
    if remaining is None:
        _log_unlocked(identity, lock.holder_run_id)
        return {"released": True, "holder_run_id": lock.holder_run_id}
    holder = read_run(store, remaining.holder_run_id)
    arn = holder.execution_arn if holder is not None else remaining.execution_arn
    if arn is None:
        raise Conflict(
            f"The sweep {remaining.holder_run_id!r} is starting; try again in a few minutes.",
            detail="lock holder has no execution yet and is not stale",
        )
    if _execution_status(sfn, arn) == "RUNNING":
        raise Conflict(
            f"The sweep {remaining.holder_run_id!r} is still running; cancel it instead.",
            detail=f"execution {arn} is RUNNING",
        )
    released = release_lock(store, run_id=remaining.holder_run_id)
    if released:
        _log_unlocked(identity, remaining.holder_run_id)
    if released and holder is not None and not holder.is_terminal:
        mark_failed(
            store,
            run_id=holder.run_id,
            failure_summary=f"An administrator (rep {identity.rep_id}) released the sweep lock.",
            now=resolved_now,
        )
    return {"released": released, "holder_run_id": remaining.holder_run_id}


def _log_unlocked(identity: Identity, holder_run_id: str) -> None:
    """Log one structured line attributing a sweep-lock release to the releasing rep."""
    _logger.warning(
        json.dumps(
            {
                "event": "eval_run_unlocked",
                "rep_id": identity.rep_id,
                "correlation_id": holder_run_id,
            }
        )
    )


# -- history -------------------------------------------------------------------------------------


def encode_cursor(last_evaluated_key: Mapping[str, object]) -> str:
    """Render a ``gsi_status`` ``LastEvaluatedKey`` as an opaque URL-safe cursor."""
    text = json.dumps(last_evaluated_key, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> dict[str, object]:
    """The ``ExclusiveStartKey`` a cursor from :func:`encode_cursor` names.

    Raises:
        ValidationFailed: The cursor is not one this module issued for the job history.
    """
    invalid = ValidationFailed("The history cursor is not valid.", source="cursor")
    try:
        parsed = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise invalid from exc
    if not isinstance(parsed, dict) or set(parsed) != _CURSOR_KEYS:
        raise invalid
    for value in parsed.values():
        if not isinstance(value, dict) or set(value) != {"S"} or not isinstance(value["S"], str):
            raise invalid
    if parsed["status"]["S"] != JOB_ROW_STATUS or not parsed["run_id"]["S"].startswith(
        _JOB_ROW_PREFIX
    ):
        raise invalid
    return parsed


def list_runs(
    store: Store, *, cursor: str | None = None, limit: int = LIST_PAGE_SIZE
) -> tuple[list[RunRecord], str | None]:
    """One page of runs, newest first, and the cursor of the next page (``None`` at the end)."""
    items, last_key = store.query_gsi(
        EVAL_RUNS_TABLE,
        "gsi_status",
        partition_value=JOB_ROW_STATUS,
        limit=limit,
        exclusive_start_key=None if cursor is None else decode_cursor(cursor),
        scan_forward=False,
    )
    order = [str(item["run_id"]) for item in items]
    rows = store.batch_get_items(EVAL_RUNS_TABLE, [{"run_id": row_id} for row_id in order])
    by_id = {str(row["run_id"]): row for row in rows}
    records = [record_from_item(by_id[row_id]) for row_id in order if row_id in by_id]
    return records, None if last_key is None else encode_cursor(last_key)


# -- estimate ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Estimate:
    """A live-sweep cost estimate, shown and never enforced.

    Attributes:
        estimate_usd: The estimated cost of one live sweep.
        basis: ``"baseline"`` (taken from ``basis_run_id``) or ``"static"`` (the YAML file).
        basis_run_id: The completed live run the figures came from, for ``baseline``.
        task_count: The golden task count per arm and repeat the estimate multiplies by.
        per_arm_usd: Estimated cost per family arm, over every repeat.
        effort_caveat: :data:`ESTIMATE_EFFORT_CAVEAT` filled in when the figures were measured at a
            supervisor effort other than :data:`eval.sweep_units.DEFAULT_ARM_EFFORT`, else ``None``.
        repeats: The repeats per arm the amounts are multiplied by.
    """

    estimate_usd: float
    basis: Literal["baseline", "static"]
    basis_run_id: str | None
    task_count: int
    per_arm_usd: Mapping[str, float]
    effort_caveat: str | None = None
    repeats: int = 1

    def to_payload(self) -> dict[str, object]:
        """The wire shape, amounts rounded to cents; ``effort_caveat`` only when set."""
        payload: dict[str, object] = {
            "estimate_usd": round(self.estimate_usd, 2),
            "basis": self.basis,
            "task_count": self.task_count,
            "repeats": self.repeats,
            "per_arm_usd": {arm: round(cost, 2) for arm, cost in self.per_arm_usd.items()},
        }
        if self.basis_run_id is not None:
            payload["basis_run_id"] = self.basis_run_id
        if self.effort_caveat is not None:
            payload["effort_caveat"] = self.effort_caveat
        return payload


#: The estimate caveat, naming the supervisor effort the figures were measured at and the default.
ESTIMATE_EFFORT_CAVEAT: Final[str] = (
    "These figures were measured at supervisor effort {measured}; sweeps now run at supervisor "
    "effort {default}, so the cost of this sweep may differ from the estimate."
)


def _effort_caveat(measured: Sequence[str]) -> str | None:
    """The caveat for figures measured at ``measured`` efforts, or ``None`` when all are default."""
    if all(effort == DEFAULT_ARM_EFFORT for effort in measured):
        return None
    return ESTIMATE_EFFORT_CAVEAT.format(
        measured=", ".join(sorted(set(measured))), default=DEFAULT_ARM_EFFORT
    )


def _baseline_efforts(matrix_report: Mapping[str, Any]) -> list[str]:
    """Each baseline arm's recorded provenance ``effort``; ``unrecorded`` where it is absent."""
    efforts: list[str] = []
    arms = matrix_report.get("arms")
    for arm in arms if isinstance(arms, list) else []:
        provenance = arm.get("provenance") if isinstance(arm, dict) else None
        effort = provenance.get("effort") if isinstance(provenance, dict) else None
        efforts.append(effort if isinstance(effort, str) else "unrecorded")
    return efforts


def _price(model_id: str, usage: Mapping[str, float]) -> float:
    from crm.metering import TokenUsage, cost_for_usage

    return cost_for_usage(
        TokenUsage(
            input_tokens=round(usage.get("input_tokens", 0.0)),
            output_tokens=round(usage.get("output_tokens", 0.0)),
            cache_write_tokens=round(usage.get("cache_write_tokens", 0.0)),
            cache_read_tokens=round(usage.get("cache_read_tokens", 0.0)),
        ),
        model_id,
    ).total


def _baseline_per_arm(matrix_report: Mapping[str, Any], task_count: int) -> dict[str, float] | None:
    arms = matrix_report.get("arms")
    if not isinstance(arms, list) or not arms:
        return None
    per_arm: dict[str, float] = {}
    for arm in arms:
        if not isinstance(arm, dict):
            return None
        provenance = arm.get("provenance") or {}
        scored = arm.get("task_scores") or []
        rows = arm.get("tier_role_model_metrics") or []
        if not isinstance(scored, list) or not scored or not isinstance(rows, list) or not rows:
            return None
        by_model: dict[str, dict[str, float]] = {}
        for row in rows:
            model_id = row.get("model_id") if isinstance(row, dict) else None
            if not isinstance(model_id, str):
                return None
            totals = by_model.setdefault(model_id, {})
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            ):
                value = row.get(name) or 0
                if isinstance(value, bool) or not isinstance(value, int | float):
                    return None
                totals[name] = totals.get(name, 0.0) + float(value)
        arm_name = str(provenance.get("model_id") or f"arm-{len(per_arm)}")
        per_arm[arm_name] = task_count * sum(
            _price(model_id, {k: v / len(scored) for k, v in totals.items()})
            for model_id, totals in by_model.items()
        )
    return per_arm


def _retired_baseline_models(matrix_report: Mapping[str, Any]) -> list[str]:
    """Model ids a baseline names that no current tier configures, sorted."""
    from providers.capabilities import load_capability_matrix

    matrix = load_capability_matrix()
    current = {
        assignment.model_id for tiers in matrix.tiers.values() for assignment in tiers.values()
    }
    named: set[str] = set()
    arms = matrix_report.get("arms")
    for arm in arms if isinstance(arms, list) else []:
        if not isinstance(arm, dict):
            continue
        provenance = arm.get("provenance")
        if isinstance(provenance, dict) and isinstance(provenance.get("model_id"), str):
            named.add(provenance["model_id"])
        rows = arm.get("tier_role_model_metrics")
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get("model_id"), str):
                named.add(row["model_id"])
    return sorted(named - current)


def load_static_estimate(
    path: Path = DEFAULT_ESTIMATE_PATH,
) -> tuple[dict[str, dict[str, dict[str, Any]]], str]:
    """Parse ``config/eval_live_estimate.yaml``.

    Returns:
        ``(per_task, measured_at_supervisor_effort)``: the ``per_task`` block and the supervisor
        effort its figures were measured at.

    Raises:
        ValueError: The file is not schema 1, ``measured_at_supervisor_effort`` is absent or not a
            non-empty string, or an entry lacks ``model_id`` or a token count.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"{path} is not a schema_version 1 estimate file")
    measured_effort = data.get("measured_at_supervisor_effort")
    if not isinstance(measured_effort, str) or not measured_effort:
        raise ValueError(f"{path}: measured_at_supervisor_effort is {measured_effort!r}")
    per_task = data.get("per_task")
    if not isinstance(per_task, dict) or not per_task:
        raise ValueError(f"{path} has no per_task block")
    for family, tiers in per_task.items():
        if not isinstance(tiers, dict) or not tiers:
            raise ValueError(f"{path}: per_task.{family} must map tiers")
        for tier, entry in tiers.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("model_id"), str):
                raise ValueError(f"{path}: per_task.{family}.{tier} lacks a model_id")
            for name in ("input_tokens", "output_tokens"):
                value = entry.get(name)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{path}: per_task.{family}.{tier}.{name} is {value!r}")
    return per_task, measured_effort


def _newest_completed_live_run(store: Store) -> RunRecord | None:
    cursor: str | None = None
    for _page in range(_ESTIMATE_HISTORY_PAGES):
        records, cursor = list_runs(store, cursor=cursor)
        for record in records:
            if record.mode == "live" and record.status == "completed" and record.matrix_report:
                return record
        if cursor is None:
            return None
    return None


def estimate(
    store: Store,
    *,
    task_count: int,
    families: Sequence[str],
    static_path: Path = DEFAULT_ESTIMATE_PATH,
    repeats: int = 1,
) -> Estimate:
    """Estimate one live sweep: the newest completed live run's per-task tokens, else the static file.

    Args:
        store: Where run history is read.
        task_count: Golden tasks per arm.
        families: The sweep's arm families, for the static path.
        static_path: The static estimate file.
        repeats: Repeats per arm; every amount is multiplied by it.

    Raises:
        CrmError: The static file is needed and is unreadable, invalid or lacks one of
            ``families``, or a model has no price.
        ValidationFailed: ``repeats`` is not accepted by :func:`validate_repeats`.
    """
    validate_repeats(repeats)
    try:
        single = _estimate(store, task_count=task_count, families=families, static_path=static_path)
    except (KeyError, OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise CrmError(
            "The live-sweep estimate could not be computed.",
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    return Estimate(
        estimate_usd=single.estimate_usd * repeats,
        basis=single.basis,
        basis_run_id=single.basis_run_id,
        task_count=single.task_count,
        per_arm_usd={arm: cost * repeats for arm, cost in single.per_arm_usd.items()},
        effort_caveat=single.effort_caveat,
        repeats=repeats,
    )


def _estimate(
    store: Store, *, task_count: int, families: Sequence[str], static_path: Path
) -> Estimate:
    baseline = _newest_completed_live_run(store)
    retired = (
        _retired_baseline_models(baseline.matrix_report)
        if baseline is not None and baseline.matrix_report is not None
        else []
    )
    if baseline is not None and retired:
        _logger.warning(
            json.dumps(
                {
                    "event": "eval_estimate_baseline_unusable",
                    "correlation_id": baseline.run_id,
                    "reason": "baseline_models_retired",
                }
            )
        )
    elif baseline is not None and baseline.matrix_report is not None:
        per_arm = _baseline_per_arm(baseline.matrix_report, task_count)
        if per_arm is None:
            _logger.warning(
                json.dumps(
                    {
                        "event": "eval_estimate_baseline_unusable",
                        "correlation_id": baseline.run_id,
                        "reason": "matrix_report lacks per-arm tasks or tier_role_model_metrics",
                    }
                )
            )
        else:
            return Estimate(
                estimate_usd=sum(per_arm.values()),
                basis="baseline",
                basis_run_id=baseline.run_id,
                task_count=task_count,
                per_arm_usd=per_arm,
                effort_caveat=_effort_caveat(_baseline_efforts(baseline.matrix_report)),
            )
    per_task, measured_effort = load_static_estimate(static_path)
    static_per_arm: dict[str, float] = {}
    for family in families:
        tiers = per_task.get(family)
        if tiers is None:
            raise ValueError(f"{static_path} has no per_task entry for the {family} arm")
        static_per_arm[family] = task_count * sum(
            _price(
                str(entry["model_id"]),
                {"input_tokens": entry["input_tokens"], "output_tokens": entry["output_tokens"]},
            )
            for entry in tiers.values()
        )
    return Estimate(
        estimate_usd=sum(static_per_arm.values()),
        basis="static",
        basis_run_id=None,
        task_count=task_count,
        per_arm_usd=static_per_arm,
        effort_caveat=_effort_caveat([measured_effort]),
    )


__all__ = [
    "DEFAULT_ESTIMATE_PATH",
    "ESTIMATE_EFFORT_CAVEAT",
    "EVAL_RESULTS_TABLE",
    "EVAL_RUNS_TABLE",
    "ITEM_STATUSES",
    "JOB_MODES",
    "JOB_ROW_STATUS",
    "JOB_STATUSES",
    "LEGACY_STATUS_MAP",
    "LIST_PAGE_SIZE",
    "LIVE_SWEEPS_PER_UTC_DAY",
    "MAX_SWEEP_REPEATS",
    "SFN_FAILURE_STATUSES",
    "STALE_QUEUED_SECONDS",
    "TERMINAL_JOB_STATUSES",
    "Disposition",
    "Estimate",
    "EvalMode",
    "ItemStatus",
    "JobStatus",
    "RunRecord",
    "SweepLock",
    "TeardownStatus",
    "acquire_lock",
    "consume_live_quota",
    "EVAL_STATE_MACHINE_ARN_ENV_VAR",
    "execution_arn_for",
    "refund_live_quota",
    "create_run",
    "decode_cursor",
    "ClaimOutcome",
    "OWNING_OUTCOMES",
    "claim_execution",
    "encode_cursor",
    "estimate",
    "finalize",
    "job_row_id",
    "list_runs",
    "load_static_estimate",
    "mark_failed",
    "mark_running",
    "read_lock",
    "read_run",
    "reconcile",
    "reconcile_lock",
    "record_from_item",
    "record_item",
    "release_lock",
    "request_cancel",
    "require_run",
    "result_id",
    "run_id_of_job_row",
    "set_execution_arn",
    "terminal_status",
    "unlock",
    "validate_repeats",
]
