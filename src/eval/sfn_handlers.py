# SPDX-License-Identifier: MIT
"""Lambda handlers for the ``eval_sweep`` state machine.

:func:`sweep_control_handler` dispatches on ``event["step"]``:

* ``start`` -- returns ``refused`` and writes nothing when the run is terminal, the lock does not
  name the run, or another execution owns the run (:func:`eval.runs.claim_execution`). Otherwise it
  captures the ``demo`` baseline to S3; a run cancelled while ``queued`` then returns
  ``cancelled_before_start``, recording every task ``skipped_cancelled`` and seeding nothing, and any
  other is marked ``running`` and gets the ``(arm_index, repeat_index, task_id)`` refs: arm-major,
  then repeat, then task.
* ``seed`` -- records the catalog row with the run's unit tenants (one per ``(arm, repeat)``,
  :func:`eval.tenancy.unit_tenant`) and seeds each.
* ``item_failed`` -- records one item ``failed`` with the state machine's error.
* ``aggregate`` -- assembles each arm from its ``done`` items, writes ``matrix.{json,md,csv}`` and
  the raw archives under ``<EVAL_ARTIFACTS_PREFIX>/<run_id>/``, and stores ``matrix_report`` and
  ``artifact_links``. A run with no ``done`` item writes nothing.
* ``teardown`` -- publishes the ``demo`` delta checks to
  ``<root>/verification-inventory/eval_sweep.json`` (failing when no baseline was captured) and
  records any check not passing on the job row (for a run never seeded, all but
  :data:`UNSEEDED_EXEMPT_CHECK`), once per run; then deletes the run tenant and every unit tenant
  and records the catalog row ``torn_down``. The unit tenants are those the catalog row records,
  which must be exactly the run's; a run never seeded uses its derived set, and only a run whose job
  row predates ``repeats`` may record none. A failure raises, for the state machine's Retry and
  Catch.
* ``finalize`` -- writes the terminal status, ``failed`` when any delta check did not pass, and
  releases the lock on every path but a refusal: it reads no artefacts configuration, an
  unreadable ownership still releases a lock naming this run and execution, and a failure while
  summarising marks the run ``failed`` and releases the lock before raising.

``seed`` and ``aggregate`` raise :class:`NotRunOwner` and write nothing when another execution owns
the run, so the state machine's Catch routes to ``teardown``; ``teardown`` and ``finalize`` return
``refused`` and touch nothing in that case.

:func:`sweep_task_handler` runs one item in its unit tenant: a cancelled run records it
``skipped_cancelled`` without invoking the Runtime; otherwise one ``InvokeAgentRuntime`` in
``eval_turn`` mode is scored and recorded ``done``. A Runtime or transport failure raises, and the
state machine records the item ``failed``. An item already recorded is not run again.

Every event names only the run and, per step, the keys in :data:`STEP_EVENT_KEYS`; mode and repeats
are read from the run record, and an item's tenant is its unit tenant,
``unit_tenant(run_id, arm_index, repeat_index)``. No model adapter is imported: the Runtime calls
the model.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Protocol

from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError

from crm.envelope import (
    AGENT_IDENTITY_KEY,
    AGENT_PRINCIPAL_TYPE_KEY,
    CLAIM_BOOK,
    CLAIM_FULL_VISIBILITY,
    CLAIM_REP_ID,
    CLAIM_TENANT,
)
from crm.errors import (
    Conflict,
    CrmError,
    ProviderUnavailable,
    TurnTimeout,
    ValidationFailed,
    bounded_detail_value,
)
from crm.store.store import Store
from eval.actor import actor_principal_type
from eval.artefacts import artefact_object_key
from eval.harness import (
    _DEFAULT_GOLDEN_TASKS_PATH,
    DEFAULT_ARM_SPECS,
    DEFAULT_DATASET_SEED,
    _assert_no_undisclosed_zero_invocation_render,
    _fr93_delta_checks,
    _git_revision,
    _load_golden_tasks,
    _read_demo_baseline,
    _write_fr93_inventory,
)
from eval.matrix import archive_raw_run, build_matrix_result, write_matrix_artifacts
from eval.pricing import is_cost_provisional, load_price_table_status
from eval.runs import (
    ITEM_STATUSES,
    OWNING_OUTCOMES,
    ItemStatus,
    RunRecord,
    claim_execution,
    finalize,
    job_row_id,
    mark_failed,
    mark_running,
    read_lock,
    record_item,
    release_lock,
    require_run,
    result_id,
)
from eval.scoring import AssertionResult, TaskScore, TierScore, TierScores
from eval.sweep_units import (
    ArmSpec,
    TaskRef,
    TaskUnitResult,
    arm_turn_tuning,
    assemble_arm,
    run_task_unit,
    task_actor,
    task_refs,
    usage_records_for_turn,
)
from eval.task_ids import localize_tasks
from eval.tenancy import (
    record_run_started,
    record_run_torn_down,
    seed_unit_tenants,
    teardown_unit_tenants,
    tenant_for_run,
    unit_tenant,
    unit_tenants_for_run,
)
from eval.tenant_guard import TenantSnapshot
from eval.turn_observation import (
    PROMPT_BUNDLE_SHA256_KEY,
    RESOLVED_TIERS_KEY,
    SERIALISED_TURN_SCHEMA,
    USAGE_LOWER_BOUND_KEY,
    ObservedTurn,
    deserialise_turn,
    serialise_turn,
)
from providers.capabilities import load_capability_matrix
from providers.tier_tuning import to_wire

_logger = logging.getLogger(__name__)

_EVAL_RUNS_TABLE: Final[str] = "eval_runs"
_EVAL_RESULTS_TABLE: Final[str] = "eval_results"

#: The ``eval_results`` attribute a ``failed`` item carries as ``False``: its billed model calls
#: were not observed, so its ``cost_usd`` is not a measurement. A row without it is measured.
COST_MEASURED_ATTRIBUTE: Final[str] = "cost_measured"

#: The ``eval_results`` attribute (``True``) on a ``failed`` item refused because its turn ran under a
#: different prompt bundle than an earlier ``done`` item of the same run. Once one exists, every
#: later item of the run is refused before its turn is sent.
BUNDLE_MISMATCH_ATTRIBUTE: Final[str] = "prompt_bundle_mismatch"

#: The runtime recorded in each arm's provenance.
RUNTIME_IDENTIFIER: Final[str] = "agentcore-runtime-eval-turn"

#: S3 object names beneath a run's artefact root.
DEMO_BASELINE_OBJECT: Final[str] = "demo-baseline.json"
_ARTIFACT_WIRE_KEYS: Final[Mapping[str, str]] = {
    "json": "matrix_json",
    "md": "matrix_md",
    "csv": "matrix_csv",
}

#: The shortest ``runtimeSessionId`` ``InvokeAgentRuntime`` accepts.
_MIN_SESSION_ID_LENGTH: Final[int] = 33
_MAX_ERROR_TEXT: Final[int] = 1024

_STEP_KEY: Final[str] = "step"


@dataclass(frozen=True, slots=True)
class EventKeys:
    """The keys one handler input requires and the further keys it accepts."""

    required: frozenset[str]
    optional: frozenset[str] = frozenset()

    @property
    def accepted(self) -> frozenset[str]:
        """Every key the input may carry."""
        return self.required | self.optional


#: ``sweep_control_handler``'s input keys per ``step``.
STEP_EVENT_KEYS: Final[Mapping[str, EventKeys]] = {
    "start": EventKeys(frozenset({"step", "run_id", "execution_arn"})),
    "seed": EventKeys(frozenset({"step", "run_id", "execution_arn"})),
    "item_failed": EventKeys(
        frozenset({"step", "run_id", "arm_index", "repeat_index", "task_id"}), frozenset({"error"})
    ),
    "aggregate": EventKeys(frozenset({"step", "run_id", "execution_arn"}), frozenset({"error"})),
    "teardown": EventKeys(frozenset({"step", "run_id", "execution_arn"})),
    "finalize": EventKeys(
        frozenset({"step", "run_id", "execution_arn"}), frozenset({"error", "teardown_error"})
    ),
}

#: ``sweep_task_handler``'s input keys.
SWEEP_TASK_EVENT_KEYS: Final[EventKeys] = EventKeys(
    frozenset({"run_id", "arm_index", "repeat_index", "task_id"})
)

#: The keys ``start`` returns, and the keys of each task ref it returns.
START_RESULT_KEYS: Final[frozenset[str]] = frozenset(
    {"refused", "cancelled_before_start", "task_refs"}
)
TASK_REF_KEYS: Final[frozenset[str]] = frozenset({"arm_index", "repeat_index", "task_id"})

#: The keys ``finalize`` returns.
FINALIZE_RESULT_KEYS: Final[frozenset[str]] = frozenset({"run_id", "status", "refused"})

#: The job-row attribute listing the ``demo`` delta checks that did not pass, as ``check=status``.
ISOLATION_FAILURES_ATTRIBUTE: Final[str] = "isolation_failures"

#: The delta check a run that never seeded cannot pass (it wrote no row); it does not fail the run.
UNSEEDED_EXEMPT_CHECK: Final[str] = "writes_only_under_smoke_tenant"

#: The keys ``sweep_task_handler`` returns.
SWEEP_TASK_RESULT_KEYS: Final[frozenset[str]] = frozenset({"item_status"})


class ObjectStore(Protocol):
    """The S3 client surface ``sweep_control`` calls."""

    def put_object(self, **kwargs: Any) -> Mapping[str, Any]:
        """Write one object."""
        ...

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]:
        """Read one object; ``Body`` is a readable byte stream."""
        ...


class RuntimeClient(Protocol):
    """The ``bedrock-agentcore`` client surface ``sweep_task`` calls."""

    def invoke_agent_runtime(self, **kwargs: Any) -> Mapping[str, Any]:
        """Start one Runtime invocation; ``response`` is a readable byte stream."""
        ...


@dataclass(frozen=True, slots=True)
class SweepControlConfig:
    """``sweep_control``'s deployment configuration, read from the Lambda environment.

    Attributes:
        table_prefix: ``CRM_TABLE_PREFIX``; may be empty.
        artifacts_bucket: ``EVAL_ARTIFACTS_BUCKET``.
        artifacts_prefix: ``EVAL_ARTIFACTS_PREFIX``, with no leading or trailing slash.
    """

    table_prefix: str
    artifacts_bucket: str
    artifacts_prefix: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SweepControlConfig:
        """Read the configuration from ``env`` (default ``os.environ``).

        Raises:
            ValueError: ``EVAL_ARTIFACTS_BUCKET`` or ``EVAL_ARTIFACTS_PREFIX`` is unset or empty.
        """
        source = os.environ if env is None else env
        bucket = source.get("EVAL_ARTIFACTS_BUCKET", "").strip()
        prefix = source.get("EVAL_ARTIFACTS_PREFIX", "").strip().strip("/")
        if not bucket or not prefix:
            raise ValueError("EVAL_ARTIFACTS_BUCKET and EVAL_ARTIFACTS_PREFIX must both be set")
        return cls(
            table_prefix=source.get("CRM_TABLE_PREFIX", ""),
            artifacts_bucket=bucket,
            artifacts_prefix=prefix,
        )

    def artifact_root(self, run_id: str) -> str:
        """The S3 key prefix a run's artefacts are written beneath: ``<prefix>/<run_id>/``."""
        return artefact_object_key(self.artifacts_prefix, run_id, "")

    def artifact_key(self, run_id: str, relative: str) -> str:
        """The S3 key of ``relative`` beneath the run's root, by ``artefact_object_key``."""
        return artefact_object_key(self.artifacts_prefix, run_id, relative)


@dataclass(frozen=True, slots=True)
class SweepTaskConfig:
    """``sweep_task``'s deployment configuration, read from the Lambda environment.

    Attributes:
        table_prefix: ``CRM_TABLE_PREFIX``; may be empty.
        runtime_arn: ``AGENT_RUNTIME_ARN``.
        runtime_qualifier: ``AGENT_RUNTIME_QUALIFIER``.
    """

    table_prefix: str
    runtime_arn: str
    runtime_qualifier: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SweepTaskConfig:
        """Read the configuration from ``env`` (default ``os.environ``).

        Raises:
            ValueError: ``AGENT_RUNTIME_ARN`` or ``AGENT_RUNTIME_QUALIFIER`` is unset or empty.
        """
        source = os.environ if env is None else env
        runtime_arn = source.get("AGENT_RUNTIME_ARN", "").strip()
        qualifier = source.get("AGENT_RUNTIME_QUALIFIER", "").strip()
        if not runtime_arn or not qualifier:
            raise ValueError("AGENT_RUNTIME_ARN and AGENT_RUNTIME_QUALIFIER must both be set")
        return cls(
            table_prefix=source.get("CRM_TABLE_PREFIX", ""),
            runtime_arn=runtime_arn,
            runtime_qualifier=qualifier,
        )


# -- event decoding ------------------------------------------------------------------------------


def _check_keys(event: Mapping[str, Any], expected: EventKeys, *, where: str) -> None:
    """Refuse an input missing a required key or carrying one ``expected`` does not accept.

    Raises:
        ValidationFailed: A key is missing or unrecognised.
    """
    present = frozenset(event)
    missing = sorted(expected.required - present)
    unexpected = sorted(present - expected.accepted)
    if missing or unexpected:
        raise ValidationFailed(
            f"The {where} input is not the shape this handler accepts.",
            source=where,
            detail=f"missing={missing} unexpected={unexpected}",
        )


def _required_str(event: Mapping[str, Any], key: str) -> str:
    value = event.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationFailed(f"{key} must be a non-empty string.", source=key, detail=repr(value))
    return value


def _arm_index(event: Mapping[str, Any]) -> int:
    value = event.get("arm_index")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationFailed("arm_index must be an integer.", source="arm_index")
    if not 0 <= value < len(DEFAULT_ARM_SPECS):
        raise ValidationFailed(
            "arm_index names no arm of this sweep.",
            source="arm_index",
            detail=f"{value} outside [0, {len(DEFAULT_ARM_SPECS)})",
        )
    return value


def _repeat_index(event: Mapping[str, Any], record: RunRecord) -> int:
    value = event.get("repeat_index")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationFailed("repeat_index must be an integer.", source="repeat_index")
    if not 0 <= value < record.repeats:
        raise ValidationFailed(
            "repeat_index names no repeat of this run.",
            source="repeat_index",
            detail=f"{value} outside [0, {record.repeats})",
        )
    return value


def _error_text(raw: object) -> str | None:
    """Render a Step Functions error object ``{Error, Cause}`` as bounded text, or ``None``.

    A Lambda error's ``Cause`` is JSON carrying ``errorMessage``; only that message is kept, never
    the stack trace.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return _bounded(f"unrecognised error object {type(raw).__name__}")
    name = str(raw.get("Error") or "UnknownError")
    cause = raw.get("Cause")
    message = ""
    if isinstance(cause, str) and cause:
        try:
            decoded = json.loads(cause)
        except json.JSONDecodeError:
            message = cause
        else:
            message = (
                str(decoded.get("errorMessage") or "") if isinstance(decoded, Mapping) else cause
            )
    return _bounded(f"{name}: {message}" if message else name)


def _bounded(text: str) -> str:
    return text if len(text) <= _MAX_ERROR_TEXT else text[: _MAX_ERROR_TEXT - 3] + "..."


# -- sweep shape ---------------------------------------------------------------------------------


def _record_tenant(record: RunRecord) -> str:
    """The run's tenant, which must be ``eval-<run_id>``.

    Raises:
        ValidationFailed: The record names any other tenant.
    """
    expected = tenant_for_run(record.run_id)
    if record.tenant != expected:
        raise ValidationFailed(
            "This evaluation run's stored tenant is not its own.",
            source="tenant",
            detail=f"run {record.run_id!r} names tenant {record.tenant!r}, expected {expected!r}",
        )
    return expected


def _sweep_tasks() -> list[dict[str, object]]:
    """The tenant-relative golden tasks."""
    return _load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH)


def _unit_tasks(run_id: str, ref: TaskRef) -> list[dict[str, object]]:
    """The golden tasks, localized to ``ref``'s unit tenant."""
    return localize_tasks(_sweep_tasks(), tenant=_ref_tenant(run_id, ref))


def _ref_tenant(run_id: str, ref: TaskRef) -> str:
    return unit_tenant(run_id, arm_index=ref.arm_index, repeat_index=ref.repeat_index)


def _run_unit_tenants(record: RunRecord) -> tuple[str, ...]:
    """Every ``(arm, repeat)`` unit tenant of ``record``'s run, arm-major."""
    return unit_tenants_for_run(
        record.run_id, arm_count=len(DEFAULT_ARM_SPECS), repeats=record.repeats
    )


def _sweep_refs(record: RunRecord) -> tuple[TaskRef, ...]:
    return task_refs(_sweep_tasks(), DEFAULT_ARM_SPECS, record.repeats)


def _result_key(run_id: str, ref: TaskRef) -> dict[str, str]:
    return {
        "run_id": run_id,
        "result_id": result_id(ref.arm_index, ref.repeat_index, ref.task_id),
    }


def _task_by_id(tasks: Sequence[Mapping[str, object]], task_id: str) -> Mapping[str, object]:
    for task in tasks:
        if task.get("id") == task_id:
            return task
    raise ValidationFailed("task_id names no golden task.", source="task_id", detail=task_id)


def session_id_for(run_id: str, arm_index: int, task_id: str, repeat_index: int = 0) -> str:
    """The ``runtimeSessionId`` of one item's eval turn, padded to the minimum length.

    Repeat 0 is ``<run_id>-<arm:02d>-<task_id>``; repeat ``r >= 1`` is
    ``<run_id>-<arm:02d>r<r:02d>-<task_id>``.
    """
    unit = f"{arm_index:02d}" if repeat_index == 0 else f"{arm_index:02d}r{repeat_index:02d}"
    return f"{run_id}-{unit}-{task_id}".ljust(_MIN_SESSION_ID_LENGTH, "0")


# -- S3 ------------------------------------------------------------------------------------------


def _put_object(s3: ObjectStore, config: SweepControlConfig, key: str, body: bytes) -> None:
    """Write ``body`` at ``key`` under SSE-KMS, which the bucket policy requires."""
    content_type = "text/csv" if key.endswith(".csv") else "application/json"
    if key.endswith(".md"):
        content_type = "text/markdown"
    s3.put_object(
        Bucket=config.artifacts_bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        ServerSideEncryption="aws:kms",
    )


def _write_baseline(
    s3: ObjectStore, config: SweepControlConfig, run_id: str, snapshot: TenantSnapshot
) -> None:
    body: dict[str, object] = {
        "counts": None if snapshot.counts is None else dict(snapshot.counts),
        "error": snapshot.error,
    }
    _put_object(
        s3,
        config,
        config.artifact_key(run_id, DEMO_BASELINE_OBJECT),
        json.dumps(body, sort_keys=True).encode("utf-8"),
    )


def _read_baseline(
    s3: ObjectStore, config: SweepControlConfig, run_id: str
) -> TenantSnapshot | None:
    """The ``demo`` baseline ``start`` captured, or ``None`` when none was captured.

    Raises:
        ValidationFailed: The stored baseline is not the shape ``start`` writes.
        ClientError: Any S3 failure other than an absent object.
    """
    key = config.artifact_key(run_id, DEMO_BASELINE_OBJECT)
    try:
        response = s3.get_object(Bucket=config.artifacts_bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return None
        raise
    raw = json.loads(response["Body"].read())
    if not isinstance(raw, Mapping):
        raise ValidationFailed("The stored demo baseline is unreadable.", source="baseline")
    counts = raw.get("counts")
    error = raw.get("error")
    if counts is not None and not (
        isinstance(counts, Mapping)
        and all(isinstance(v, int) and not isinstance(v, bool) for v in counts.values())
    ):
        raise ValidationFailed("The stored demo baseline is unreadable.", source="baseline")
    return TenantSnapshot(
        counts=None if counts is None else {str(k): int(v) for k, v in counts.items()},
        error=str(error or ""),
    )


def _upload_tree(
    s3: ObjectStore, config: SweepControlConfig, run_id: str, directory: Path
) -> list[str]:
    """Upload every file under ``directory`` beneath the run's root; returns the relative paths."""
    relative_paths: list[str] = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        relative = path.relative_to(directory).as_posix()
        _put_object(s3, config, config.artifact_key(run_id, relative), path.read_bytes())
        relative_paths.append(relative)
    return relative_paths


# -- result rows ---------------------------------------------------------------------------------


def _unit_fields(unit: TaskUnitResult) -> dict[str, str | int | float | None]:
    """One task unit's ``eval_results`` attributes, JSON rendered to strings.

    :data:`eval.turn_observation.USAGE_LOWER_BOUND_KEY` is stored (``true``) only for a turn whose
    usage is a lower bound, and ``resolved_efforts`` (the turn's
    :data:`eval.turn_observation.RESOLVED_TIERS_KEY` object) and
    :data:`eval.turn_observation.PROMPT_BUNDLE_SHA256_KEY` only for a turn that recorded one, as
    :func:`eval.turn_observation.serialise_turn` writes them.
    """
    turn = serialise_turn(unit.turn)
    fields: dict[str, str | int | float | None] = {
        "task_score": json.dumps(unit.task_score.to_report_fields(), sort_keys=True),
        "tier_scores": json.dumps(unit.tier_scores.to_report_fields(), sort_keys=True),
        "outcome": json.dumps(turn["outcome"], sort_keys=True),
        "tool_arguments": json.dumps(turn["tool_arguments"], sort_keys=True),
        "attributions": json.dumps(turn["records"]["invocations"], sort_keys=True),
        "tool_calls": json.dumps(turn["records"]["tool_calls"], sort_keys=True),
        "exclusions": json.dumps(turn["exclusions"], sort_keys=True),
        "stream_calls": int(turn["stream_calls"]),
        "turn_latency_ms": turn["turn_latency_ms"],
    }
    if USAGE_LOWER_BOUND_KEY in turn:
        fields[USAGE_LOWER_BOUND_KEY] = bool(turn[USAGE_LOWER_BOUND_KEY])
    if RESOLVED_TIERS_KEY in turn:
        fields["resolved_efforts"] = json.dumps(turn[RESOLVED_TIERS_KEY], sort_keys=True)
    if PROMPT_BUNDLE_SHA256_KEY in turn:
        fields[PROMPT_BUNDLE_SHA256_KEY] = str(turn[PROMPT_BUNDLE_SHA256_KEY])
    return fields


def _json_field(row: Mapping[str, object], name: str) -> Any:
    raw = row.get(name)
    if not isinstance(raw, str):
        raise ValidationFailed(
            "A stored task result is unreadable.", source=name, detail=f"{name}={raw!r}"
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationFailed(
            "A stored task result is unreadable.", source=name, detail=str(exc)
        ) from exc


def _require(condition: bool, field: str) -> None:
    if not condition:
        raise ValidationFailed("A stored task result is unreadable.", source=field)


def task_score_from_fields(fields: Mapping[str, Any]) -> TaskScore:
    """The inverse of :meth:`eval.scoring.TaskScore.to_report_fields`.

    Raises:
        ValidationFailed: A field is absent or of the wrong type.
    """
    correctness = fields.get("correctness")
    _require(isinstance(fields.get("task_id"), str), "task_score.task_id")
    _require(isinstance(fields.get("scoring_mode"), str), "task_score.scoring_mode")
    _require(
        correctness is None
        or (isinstance(correctness, int | float) and not isinstance(correctness, bool)),
        "task_score.correctness",
    )
    _require(
        fields.get("unscored_reason") is None or isinstance(fields.get("unscored_reason"), str),
        "task_score.unscored_reason",
    )
    for flag in ("bad_write", "truncated", "budget_exhausted"):
        _require(isinstance(fields.get(flag), bool), f"task_score.{flag}")
    _require(
        fields.get("tool_call_correct") is None
        or isinstance(fields.get("tool_call_correct"), bool),
        "task_score.tool_call_correct",
    )
    raw_results = fields.get("assertion_results")
    _require(isinstance(raw_results, list), "task_score.assertion_results")
    results: list[AssertionResult] = []
    for entry in raw_results:  # type: ignore[union-attr]
        _require(isinstance(entry, Mapping), "task_score.assertion_results[]")
        _require(isinstance(entry.get("name"), str), "task_score.assertion_results[].name")
        _require(isinstance(entry.get("passed"), bool), "task_score.assertion_results[].passed")
        _require(isinstance(entry.get("detail"), str), "task_score.assertion_results[].detail")
        _require(isinstance(entry.get("unscored"), bool), "task_score.assertion_results[].unscored")
        results.append(
            AssertionResult(
                name=entry["name"],
                passed=entry["passed"],
                detail=entry["detail"],
                unscored=entry["unscored"],
            )
        )
    return TaskScore(
        task_id=str(fields["task_id"]),
        scoring_mode=str(fields["scoring_mode"]),
        correctness=None if correctness is None else float(correctness),
        unscored_reason=fields.get("unscored_reason"),
        bad_write=bool(fields["bad_write"]),
        tool_call_correct=fields.get("tool_call_correct"),
        truncated=bool(fields["truncated"]),
        budget_exhausted=bool(fields["budget_exhausted"]),
        assertion_results=tuple(results),
    )


def tier_scores_from_fields(fields: Mapping[str, Any]) -> TierScores:
    """The inverse of :meth:`eval.scoring.TierScores.to_report_fields`.

    Raises:
        ValidationFailed: A field is absent or of the wrong type, or a tier is inconsistent.
    """
    _require(isinstance(fields.get("task_id"), str), "tier_scores.task_id")
    scores: dict[str, TierScore] = {}
    for tier in ("supervisor", "subagent", "tool"):
        raw = fields.get(tier)
        if not isinstance(raw, Mapping):
            raise ValidationFailed(
                "A stored task result is unreadable.", source=f"tier_scores.{tier}"
            )
        passed = raw.get("passed")
        _require(isinstance(raw.get("applicable"), bool), f"tier_scores.{tier}.applicable")
        _require(passed is None or isinstance(passed, bool), f"tier_scores.{tier}.passed")
        _require(isinstance(raw.get("detail"), str), f"tier_scores.{tier}.detail")
        try:
            scores[tier] = TierScore(
                tier=tier, applicable=raw["applicable"], passed=passed, detail=raw["detail"]
            )
        except ValueError as exc:
            raise ValidationFailed(
                "A stored task result is unreadable.", source=f"tier_scores.{tier}", detail=str(exc)
            ) from exc
    return TierScores(
        task_id=str(fields["task_id"]),
        supervisor=scores["supervisor"],
        subagent=scores["subagent"],
        tool=scores["tool"],
    )


def turn_from_row(row: Mapping[str, object]) -> ObservedTurn:
    """Rebuild a stored item's observed turn through :func:`deserialise_turn`.

    Raises:
        ValidationFailed: An attribute is absent or the turn does not deserialise.
    """
    stream_calls = row.get("stream_calls")
    _require(isinstance(stream_calls, int) and not isinstance(stream_calls, bool), "stream_calls")
    payload = {
        "schema": SERIALISED_TURN_SCHEMA,
        "outcome": _json_field(row, "outcome"),
        "tool_arguments": _json_field(row, "tool_arguments"),
        "records": {
            "invocations": _json_field(row, "attributions"),
            "tool_calls": _json_field(row, "tool_calls"),
        },
        "stream_calls": stream_calls,
        "exclusions": _json_field(row, "exclusions"),
        "turn_latency_ms": row.get("turn_latency_ms"),
    }
    if USAGE_LOWER_BOUND_KEY in row:
        payload[USAGE_LOWER_BOUND_KEY] = row[USAGE_LOWER_BOUND_KEY]
    if "resolved_efforts" in row:
        payload[RESOLVED_TIERS_KEY] = _json_field(row, "resolved_efforts")
    if PROMPT_BUNDLE_SHA256_KEY in row:
        payload[PROMPT_BUNDLE_SHA256_KEY] = row[PROMPT_BUNDLE_SHA256_KEY]
    try:
        return deserialise_turn(payload)
    except ValueError as exc:
        raise ValidationFailed(
            "A stored task result is unreadable.", source="turn", detail=str(exc)
        ) from exc


def unit_from_row(
    row: Mapping[str, object], *, task: Mapping[str, object], arm_spec: ArmSpec
) -> TaskUnitResult:
    """Rebuild one ``done`` item's :class:`eval.sweep_units.TaskUnitResult` from its stored row.

    Usage records are re-derived from the stored invocations, as :func:`run_task_unit` derives them.
    """
    turn = turn_from_row(row)
    task_score = task_score_from_fields(_json_field(row, "task_score"))
    task_id = task_score.task_id
    return TaskUnitResult(
        task_id=task_id,
        task_score=task_score,
        tier_scores=tier_scores_from_fields(_json_field(row, "tier_scores")),
        turn=turn,
        usage_records=usage_records_for_turn(
            turn.invocations,
            task_id=task_id,
            rep_id=str(task_actor(task)["rep_id"]),
            family=arm_spec.family,
        ),
    )


def _item_status(row: Mapping[str, object]) -> ItemStatus:
    status = row.get("item_status")
    for known in ITEM_STATUSES:
        if status == known:
            return known
    raise ValidationFailed(
        "A stored task result is unreadable.", source="item_status", detail=repr(status)
    )


# -- sweep_control steps -------------------------------------------------------------------------


def _start(
    store: Store,
    s3: ObjectStore,
    config: SweepControlConfig,
    event: Mapping[str, Any],
    now: datetime | None,
) -> dict[str, Any]:
    run_id = _required_str(event, "run_id")
    execution_arn = _required_str(event, "execution_arn")
    record = require_run(store, run_id)
    _record_tenant(record)
    if record.is_terminal:
        return _start_refused(run_id, f"the run is already {record.status}")
    lock = read_lock(store)
    if lock is None or lock.holder_run_id != run_id:
        holder = "no run" if lock is None else repr(lock.holder_run_id)
        return _start_refused(run_id, f"the sweep lock names {holder}")
    outcome = claim_execution(store, run_id=run_id, execution_arn=execution_arn)
    if outcome not in OWNING_OUTCOMES:
        return _start_refused(run_id, f"{outcome}: {execution_arn!r} does not own the run")
    refs = _sweep_refs(record)
    if len(refs) != record.tasks_total:
        raise ValidationFailed(
            "This evaluation run's task count does not match the sweep.",
            source="tasks_total",
            detail=f"record carries {record.tasks_total}, the sweep has {len(refs)}",
        )
    _write_baseline(s3, config, run_id, _read_demo_baseline(store))
    if record.cancel_requested:
        return _cancel_before_start(store, run_id, refs)
    if record.status == "queued":
        try:
            mark_running(store, run_id=run_id, now=now)
        except Conflict:
            current = require_run(store, run_id)
            if current.cancel_requested:
                return _cancel_before_start(store, run_id, refs)
            if current.status != "running":
                raise
    return {
        "refused": False,
        "cancelled_before_start": False,
        "task_refs": [
            {"arm_index": ref.arm_index, "repeat_index": ref.repeat_index, "task_id": ref.task_id}
            for ref in refs
        ],
    }


def _start_refused(run_id: str, reason: str) -> dict[str, Any]:
    _log("sweep_start_refused", run_id=run_id, reason=reason)
    return {"refused": True, "cancelled_before_start": False, "task_refs": []}


def _owns_run(store: Store, event: Mapping[str, Any], *, step: str) -> bool:
    """Whether the event's execution owns its run; a refusal is logged."""
    run_id = _required_str(event, "run_id")
    execution_arn = _required_str(event, "execution_arn")
    outcome = claim_execution(store, run_id=run_id, execution_arn=execution_arn)
    if outcome in OWNING_OUTCOMES:
        return True
    _log(
        f"sweep_{step}_refused",
        run_id=run_id,
        reason=f"{outcome}: {execution_arn!r} does not own the run",
    )
    return False


class NotRunOwner(Conflict):
    """A ``seed`` or ``aggregate`` event whose execution does not own its run; nothing was written."""


def _require_ownership(store: Store, event: Mapping[str, Any], *, step: str) -> None:
    """Raise :class:`NotRunOwner` unless the event's execution owns its run."""
    if not _owns_run(store, event, step=step):
        raise NotRunOwner(
            "This execution does not own the evaluation run.",
            detail=f"step={step} run_id={event.get('run_id')!r}",
        )


def _cancel_before_start(store: Store, run_id: str, refs: Sequence[TaskRef]) -> dict[str, Any]:
    """Record every item ``skipped_cancelled`` for a run cancelled before it started."""
    for ref in refs:
        record_item(
            store,
            run_id=run_id,
            arm_index=ref.arm_index,
            repeat_index=ref.repeat_index,
            task_id=ref.task_id,
            arm=DEFAULT_ARM_SPECS[ref.arm_index].family,
            item_status="skipped_cancelled",
        )
    return {"refused": False, "cancelled_before_start": True, "task_refs": []}


def _seed(store: Store, event: Mapping[str, Any], now: datetime | None) -> dict[str, Any]:
    run_id = _required_str(event, "run_id")
    _require_ownership(store, event, step="seed")
    record = require_run(store, run_id)
    tenant = _record_tenant(record)
    record_run_started(
        store,
        run_id=run_id,
        tenant=tenant,
        started_at=now,
        unit_tenants=_run_unit_tenants(record),
    )
    seed_unit_tenants(
        store, run_id=run_id, arm_count=len(DEFAULT_ARM_SPECS), repeats=record.repeats
    )
    return {"run_id": run_id, "seeded": True}


def _item_failed(store: Store, event: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _required_str(event, "run_id")
    arm_index = _arm_index(event)
    task_id = _required_str(event, "task_id")
    repeat_index = _repeat_index(event, require_run(store, run_id))
    recorded = record_item(
        store,
        run_id=run_id,
        arm_index=arm_index,
        repeat_index=repeat_index,
        task_id=task_id,
        arm=DEFAULT_ARM_SPECS[arm_index].family,
        item_status="failed",
        fields={
            "error": _error_text(event.get("error")) or "unknown failure",
            COST_MEASURED_ATTRIBUTE: False,
        },
    )
    return {"run_id": run_id, "recorded": recorded}


def _aggregate(
    store: Store, s3: ObjectStore, config: SweepControlConfig, event: Mapping[str, Any]
) -> dict[str, Any]:
    run_id = _required_str(event, "run_id")
    _require_ownership(store, event, step="aggregate")
    record = require_run(store, run_id)
    _record_tenant(record)
    step_error = _error_text(event.get("error"))
    if step_error is not None:
        _log("sweep_aggregate_after_error", run_id=run_id, reason=step_error)
    units_by_arm: dict[int, list[TaskUnitResult]] = {}
    unrecorded = 0
    for ref in _sweep_refs(record):
        row = store.get_item(_EVAL_RESULTS_TABLE, _result_key(run_id, ref), consistent=True)
        if row is None:
            unrecorded += 1
            continue
        if _item_status(row) != "done":
            continue
        units_by_arm.setdefault(ref.arm_index, []).append(
            unit_from_row(
                row,
                task=_task_by_id(_unit_tasks(run_id, ref), ref.task_id),
                arm_spec=DEFAULT_ARM_SPECS[ref.arm_index],
            )
        )
    if unrecorded:
        _log("sweep_items_unrecorded", run_id=run_id, reason=f"{unrecorded} task(s) have no result")
    if not units_by_arm:
        return {"run_id": run_id, "arms": 0, "items_unrecorded": unrecorded}

    matrix = load_capability_matrix()
    price_status = load_price_table_status()
    cost_withheld = is_cost_provisional()
    git_revision = _git_revision()
    arm_runs = [
        assemble_arm(
            units,
            matrix=matrix,
            arm_spec=DEFAULT_ARM_SPECS[arm_index],
            mode=record.mode,
            dataset_seed=DEFAULT_DATASET_SEED,
            git_revision=git_revision,
            runtime_identifier=RUNTIME_IDENTIFIER,
            price_table_version=price_status.price_table_version,
            cost_withheld=cost_withheld,
            arm_index=arm_index,
        )
        for arm_index, units in sorted(units_by_arm.items())
    ]
    _assert_no_undisclosed_zero_invocation_render(arm_runs)
    matrix_result = build_matrix_result(
        [arm_run.arm_result for arm_run in arm_runs], errored_item_count=record.tasks_failed
    )

    with tempfile.TemporaryDirectory(prefix="eval-sweep-") as scratch:
        directory = Path(scratch)
        written = write_matrix_artifacts(matrix_result, directory)
        raw_paths = [archive_raw_run(arm_run.arm_result, directory / "raw") for arm_run in arm_runs]
        _upload_tree(s3, config, run_id, directory)
        links: dict[str, Any] = {
            _ARTIFACT_WIRE_KEYS[name]: path.relative_to(directory).as_posix()
            for name, path in sorted(written.items())
        }
        links["raw"] = [path.relative_to(directory).as_posix() for path in raw_paths]

    store.update_item(
        _EVAL_RUNS_TABLE,
        {"run_id": job_row_id(run_id)},
        set_values={
            "matrix_report": json.dumps(matrix_result.to_report_fields()),
            "artifact_links": json.dumps(links),
        },
        condition=None,
    )
    return {"run_id": run_id, "arms": len(arm_runs), "items_unrecorded": unrecorded}


def _teardown(
    store: Store, s3: ObjectStore, config: SweepControlConfig, event: Mapping[str, Any]
) -> dict[str, Any]:
    run_id = _required_str(event, "run_id")
    if not _owns_run(store, event, step="teardown"):
        return {"run_id": run_id, "torn_down": False, "refused": True}
    record = require_run(store, run_id)
    tenant = _record_tenant(record)
    job_row = store.get_item(_EVAL_RUNS_TABLE, {"run_id": job_row_id(run_id)}, consistent=True)
    catalog = store.get_item(_EVAL_RUNS_TABLE, {"run_id": run_id}, consistent=True)
    units = (
        _run_unit_tenants(record)
        if catalog is None
        else _catalog_unit_tenants(record, catalog, records_repeats=_records_repeats(job_row))
    )
    already_checked = job_row is not None and ISOLATION_FAILURES_ATTRIBUTE in job_row
    torn_down = catalog is not None and catalog.get("status") == "torn_down"
    if not (already_checked or torn_down):
        _publish_delta_checks(
            store,
            s3,
            config,
            run_id=run_id,
            tenant=tenant,
            unit_tenants=units,
            seeded=catalog is not None,
        )
    teardown_unit_tenants(store, tenants=(*units, tenant))
    if catalog is None:
        _log("sweep_catalog_absent", run_id=run_id, reason="the run was never seeded")
    else:
        started_at = catalog.get("started_at")
        record_run_torn_down(
            store,
            run_id=run_id,
            tenant=tenant,
            started_at=datetime.fromisoformat(started_at) if isinstance(started_at, str) else None,
            unit_tenants=units,
        )
    return {"run_id": run_id, "torn_down": True, "refused": False}


def _records_repeats(job_row: Mapping[str, object] | None) -> bool:
    """Whether the job row records ``repeats``; a row without it predates unit tenants."""
    return job_row is not None and "repeats" in job_row


def _catalog_unit_tenants(
    record: RunRecord, catalog: Mapping[str, object], *, records_repeats: bool
) -> tuple[str, ...]:
    """The unit tenants the run's catalog row records, which must be exactly the run's own.

    A catalog row recording none is accepted, as ``()``, only for a job row that does not record
    ``repeats``.

    Raises:
        ValidationFailed: ``unit_tenants`` is not the run's full unit tenant set, or is absent or
            empty for a run that records ``repeats``.
    """
    raw = catalog.get("unit_tenants")
    if not records_repeats and raw in (None, []):
        return ()
    expected = _run_unit_tenants(record)
    if not isinstance(raw, list) or sorted(raw, key=str) != sorted(expected):
        raise ValidationFailed(
            "This evaluation run's recorded unit tenants are not the run's own.",
            source="unit_tenants",
            detail=f"run {record.run_id!r} records {bounded_detail_value(raw)}",
        )
    return expected


def _publish_delta_checks(
    store: Store,
    s3: ObjectStore,
    config: SweepControlConfig,
    *,
    run_id: str,
    tenant: str,
    unit_tenants: Sequence[str],
    seeded: bool,
) -> None:
    """Publish the ``demo`` delta checks and record those not passing, once; before any tenant is
    deleted. ``writes_only_under_smoke_tenant`` covers each of ``unit_tenants``, or ``tenant``
    alone when ``unit_tenants`` is empty."""
    baseline = _read_baseline(s3, config, run_id)
    if baseline is None:
        _log("sweep_baseline_absent", run_id=run_id, reason="no demo baseline was captured")
        baseline = TenantSnapshot(
            counts=None, error="no demo baseline was captured: the run never passed its start step"
        )
    checks = _fr93_delta_checks(store, tenant=tenant, before=baseline, unit_tenants=unit_tenants)
    with tempfile.TemporaryDirectory(prefix="eval-sweep-") as scratch:
        directory = Path(scratch)
        _write_fr93_inventory(checks, output_dir=directory)
        _upload_tree(s3, config, run_id, directory)
    failures = sorted(
        f"{check.check}={check.status}"
        for check in checks
        if check.status != "pass" and (seeded or check.check != UNSEEDED_EXEMPT_CHECK)
    )
    try:
        store.update_item(
            _EVAL_RUNS_TABLE,
            {"run_id": job_row_id(run_id)},
            set_values={ISOLATION_FAILURES_ATTRIBUTE: json.dumps(failures)},
            condition="attribute_not_exists(#isolation)",
            names={"#isolation": ISOLATION_FAILURES_ATTRIBUTE},
        )
    except Conflict:
        _log("sweep_isolation_already_recorded", run_id=run_id, reason="kept the first record")


def _failed_items_by_family(store: Store, record: RunRecord) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for ref in _sweep_refs(record):
        row = store.get_item(_EVAL_RESULTS_TABLE, _result_key(record.run_id, ref), consistent=True)
        if row is not None and _item_status(row) == "failed":
            counts[DEFAULT_ARM_SPECS[ref.arm_index].family] += 1
    return dict(sorted(counts.items()))


def _isolation_failures(store: Store, run_id: str) -> list[str]:
    """The ``check=status`` entries teardown recorded as not passing; none when unrecorded."""
    row = store.get_item(_EVAL_RUNS_TABLE, {"run_id": job_row_id(run_id)}, consistent=True)
    raw = None if row is None else row.get(ISOLATION_FAILURES_ATTRIBUTE)
    if raw is None:
        return []
    entries = _json_field({ISOLATION_FAILURES_ATTRIBUTE: raw}, ISOLATION_FAILURES_ATTRIBUTE)
    _require(
        isinstance(entries, list) and all(isinstance(entry, str) for entry in entries),
        ISOLATION_FAILURES_ATTRIBUTE,
    )
    return list(entries)


def _failure_summary(
    store: Store, record: RunRecord, *, error: str | None, teardown_error: str | None
) -> str | None:
    """Why the run failed, or ``None`` when it did not: a step error, a teardown failure, a
    ``demo`` delta check that did not pass, or any failed item (counted per family)."""
    parts: list[str] = []
    if error is not None:
        parts.append(f"step failed: {error}")
    if teardown_error is not None:
        parts.append(f"teardown failed: {teardown_error}")
    isolation = _isolation_failures(store, record.run_id)
    if isolation:
        parts.append(f"isolation check(s) not passing: {', '.join(isolation)}")
    if record.tasks_failed > 0:
        by_family = _failed_items_by_family(store, record)
        rendered = ", ".join(f"{family}={count}" for family, count in by_family.items())
        if sum(by_family.values()) != record.tasks_failed:
            _log(
                "sweep_failed_count_mismatch",
                run_id=record.run_id,
                reason=f"counter {record.tasks_failed}, rows {sum(by_family.values())}",
            )
        parts.append(
            f"{record.tasks_failed} task(s) failed ({rendered or 'no failed result row found'})"
        )
    unrecorded = record.tasks_total - (
        record.tasks_done + record.tasks_failed + record.tasks_skipped
    )
    if unrecorded > 0:
        parts.append(f"{unrecorded} task(s) never recorded")
    return _bounded("; ".join(parts)) if parts else None


def _finalize(store: Store, event: Mapping[str, Any], now: datetime | None) -> dict[str, Any]:
    run_id = _required_str(event, "run_id")
    try:
        owned = _owns_run(store, event, step="finalize")
    except Exception:
        _release_if_lock_names_execution(store, run_id=run_id, execution_arn=event["execution_arn"])
        raise
    if not owned:
        return {"run_id": run_id, "status": None, "refused": True}
    error = _error_text(event.get("error"))
    teardown_error = _error_text(event.get("teardown_error"))
    try:
        record = require_run(store, run_id)
        summary = (
            None
            if record.is_terminal
            else _failure_summary(store, record, error=error, teardown_error=teardown_error)
        )
        if summary is not None:
            mark_failed(
                store,
                run_id=run_id,
                failure_summary=summary,
                teardown_status="ok" if teardown_error is None else "failed",
                now=now,
            )
    except Exception as exc:
        _logger.exception(
            json.dumps(
                {
                    "event": "sweep_finalize_failed",
                    "correlation_id": run_id,
                    "reason": type(exc).__name__,
                }
            )
        )
        try:
            mark_failed(
                store,
                run_id=run_id,
                failure_summary=_bounded(
                    f"finalize could not summarise: {type(exc).__name__}: {exc}"
                ),
                teardown_status="ok" if teardown_error is None else "failed",
                now=now,
            )
        except Exception:  # noqa: BLE001 - the lock release below must still run
            _logger.exception(
                json.dumps({"event": "sweep_mark_failed_failed", "correlation_id": run_id})
            )
        finally:
            release_lock(store, run_id=run_id)
        raise
    final = finalize(store, run_id=run_id, teardown_ok=teardown_error is None, now=now)
    return {"run_id": run_id, "status": final.status, "refused": False}


# -- sweep_task ----------------------------------------------------------------------------------


def _eval_turn_request(
    *,
    task: Mapping[str, object],
    tenant: str,
    arm_spec: ArmSpec,
    provider_mode: str,
    session_id: str,
) -> dict[str, Any]:
    """The Runtime's ``eval_turn`` request for one task, sending the actor's declared principal type.

    Raises:
        ValidationFailed: The task has no prompt or id, ``actor.book`` is not a list, or the
            actor's ``principal_type`` is refused by :func:`eval.actor.actor_principal_type`.
    """
    actor = task_actor(task)
    for key in ("prompt", "id"):
        if not isinstance(task.get(key), str) or not task.get(key):
            raise ValidationFailed(f"The golden task carries no {key}.", source=f"task.{key}")
    book = actor.get("book") or []
    if not isinstance(book, list):
        raise ValidationFailed("actor.book must be a list of account ids.", source="actor.book")
    try:
        principal_type = actor_principal_type(actor)
    except ValueError as exc:
        raise ValidationFailed(
            "The golden task's actor declares no usable principal_type.",
            source="actor.principal_type",
        ) from exc
    return {
        "mode": "eval_turn",
        AGENT_IDENTITY_KEY: {
            "claims": {
                CLAIM_REP_ID: str(actor["rep_id"]),
                CLAIM_BOOK: ",".join(str(account_id) for account_id in book),
                CLAIM_FULL_VISIBILITY: "true" if actor.get("full_visibility") else "false",
                CLAIM_TENANT: tenant,
            },
            AGENT_PRINCIPAL_TYPE_KEY: principal_type,
            "session_id": session_id,
        },
        "prompt": str(task.get("prompt") or ""),
        "task_id": str(task.get("id") or ""),
        "provider_mode": provider_mode,
        "tuning": to_wire(arm_turn_tuning(arm_spec)),
    }


#: Runtime failure codes classified as their own taxonomy members.
_RUNTIME_FAILURES: Final[Mapping[str, type[CrmError]]] = {
    TurnTimeout.code: TurnTimeout,
    ProviderUnavailable.code: ProviderUnavailable,
}


def classify_runtime_failure(*, status: int | None, code: str | None, detail: str) -> CrmError:
    """The taxonomy error for a failed eval turn: by the Runtime's ``error.code``, then by status.

    ``turn_timeout`` (504) is :class:`TurnTimeout`, ``provider_unavailable`` (503) is
    :class:`ProviderUnavailable`; any other failure is a generic :class:`CrmError`.
    """
    member = _RUNTIME_FAILURES.get(code or "")
    if member is None:
        member = next(
            (cls for cls in _RUNTIME_FAILURES.values() if status == cls.http_status), CrmError
        )
    return member("The eval turn did not complete.", detail=_bounded(detail))


#: The container status AgentCore embeds in a ``RuntimeClientError`` message, e.g. the ``504`` in
#: ``Received error (504) from runtime. Please check your CloudWatch logs for more information``.
_RUNTIME_ERROR_STATUS: Final[re.Pattern[str]] = re.compile(
    r"Received error \((\d{3})\) from runtime"
)


def _client_error_failure(exc: ClientError) -> CrmError:
    """Classify a refused ``InvokeAgentRuntime`` by the container status its message embeds.

    The Runtime's response body does not cross the AgentCore boundary; only the status in
    :data:`_RUNTIME_ERROR_STATUS` does. A message without it is a generic :class:`CrmError`.
    """
    error = exc.response.get("Error", {})
    message = str(error.get("Message") or "")
    match = _RUNTIME_ERROR_STATUS.search(message)
    return classify_runtime_failure(
        status=int(match.group(1)) if match else None,
        code=None,
        detail=f"InvokeAgentRuntime {error.get('Code') or 'ClientError'}: {message}",
    )


def _invoke_eval_turn(
    runtime: RuntimeClient, config: SweepTaskConfig, request: dict[str, Any]
) -> ObservedTurn:
    """Send one ``eval_turn`` request and return the deserialised observed turn.

    Raises:
        TurnTimeout: The turn or the read timed out.
        ProviderUnavailable: A live provider failed after its retries.
        CrmError: Any other Runtime, transport or decoding failure.
    """
    session_id = str(request[AGENT_IDENTITY_KEY]["session_id"])
    try:
        response = runtime.invoke_agent_runtime(
            agentRuntimeArn=config.runtime_arn,
            qualifier=config.runtime_qualifier,
            runtimeSessionId=session_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(request).encode("utf-8"),
        )
    except ReadTimeoutError as exc:
        raise TurnTimeout("The eval turn did not complete.", detail=str(exc)) from exc
    except ClientError as exc:
        raise _client_error_failure(exc) from exc
    except BotoCoreError as exc:
        raise CrmError("The eval turn did not complete.", detail=type(exc).__name__) from exc
    stream = response.get("response")
    if stream is None:
        raise CrmError("The eval turn did not complete.", detail="no response stream")
    try:
        raw = stream.read()
    except ReadTimeoutError as exc:
        raise TurnTimeout("The eval turn did not complete.", detail=str(exc)) from exc
    except BotoCoreError as exc:
        raise CrmError("The eval turn did not complete.", detail=type(exc).__name__) from exc
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    status = response.get("statusCode")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if isinstance(status, int) and status != 200:
            raise classify_runtime_failure(
                status=status, code=None, detail=f"Runtime status {status}, unparseable body"
            ) from exc
        raise CrmError(
            "The eval turn did not complete.", detail=f"unparseable body: {exc}"
        ) from exc
    error = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(error, Mapping) or (isinstance(status, int) and status != 200):
        code = error.get("code") if isinstance(error, Mapping) else None
        message = error.get("message") if isinstance(error, Mapping) else None
        raise classify_runtime_failure(
            status=status if isinstance(status, int) else None,
            code=code if isinstance(code, str) else None,
            detail=f"Runtime status {status} code {code}: {message}",
        )
    if not isinstance(body, Mapping):
        raise CrmError("The eval turn did not complete.", detail="body is not a JSON object")
    try:
        return deserialise_turn(body)
    except ValueError as exc:
        raise CrmError("The eval turn did not complete.", detail=f"malformed turn: {exc}") from exc


@dataclass(frozen=True, slots=True)
class _RunBundle:
    """What a run's stored items say about its prompt bundle.

    Attributes:
        reference_found: Whether the run has a ``done`` item.
        reference: The first ``done`` item's ``prompt_bundle_sha256`` in sweep order, or ``None``.
        mismatch_recorded: Whether an item was already refused for a different bundle.
    """

    reference_found: bool
    reference: str | None
    mismatch_recorded: bool


def _run_bundle(store: Store, record: RunRecord) -> _RunBundle:
    reference_found = False
    reference: str | None = None
    for ref in _sweep_refs(record):
        row = store.get_item(_EVAL_RESULTS_TABLE, _result_key(record.run_id, ref), consistent=True)
        if row is None:
            continue
        if row.get(BUNDLE_MISMATCH_ATTRIBUTE) is True:
            return _RunBundle(reference_found, reference, mismatch_recorded=True)
        if not reference_found and _item_status(row) == "done":
            reference_found = True
            digest = row.get(PROMPT_BUNDLE_SHA256_KEY)
            reference = digest if isinstance(digest, str) else None
    return _RunBundle(reference_found, reference, mismatch_recorded=False)


def _sweep_task(
    store: Store, runtime: RuntimeClient, config: SweepTaskConfig, event: Mapping[str, Any]
) -> dict[str, Any]:
    _check_keys(event, SWEEP_TASK_EVENT_KEYS, where="sweep_task")
    run_id = _required_str(event, "run_id")
    arm_index = _arm_index(event)
    task_id = _required_str(event, "task_id")
    arm_spec = DEFAULT_ARM_SPECS[arm_index]
    record = require_run(store, run_id)
    _record_tenant(record)
    ref = TaskRef(arm_index=arm_index, repeat_index=_repeat_index(event, record), task_id=task_id)
    tenant = _ref_tenant(run_id, ref)

    existing = store.get_item(_EVAL_RESULTS_TABLE, _result_key(run_id, ref), consistent=True)
    if existing is not None:
        return {"item_status": _item_status(existing)}
    if record.is_terminal:
        raise Conflict(
            "This evaluation run has already finished.", detail=f"run status {record.status}"
        )
    if record.cancel_requested:
        record_item(
            store,
            run_id=run_id,
            arm_index=arm_index,
            repeat_index=ref.repeat_index,
            task_id=task_id,
            arm=arm_spec.family,
            item_status="skipped_cancelled",
        )
        return {"item_status": "skipped_cancelled"}

    task = _task_by_id(_unit_tasks(run_id, ref), task_id)
    session_id = session_id_for(run_id, arm_index, task_id, ref.repeat_index)
    run_bundle = _run_bundle(store, record)
    if run_bundle.mismatch_recorded:
        raise Conflict(
            "This evaluation run's turns ran under more than one prompt bundle.",
            detail="an earlier item was refused for a prompt-bundle mismatch; no turn sent",
        )

    def _turn_runner(unit_task: Mapping[str, object], unit_tenant: str) -> ObservedTurn:
        request = _eval_turn_request(
            task=unit_task,
            tenant=unit_tenant,
            arm_spec=arm_spec,
            provider_mode=record.mode,
            session_id=session_id,
        )
        return _invoke_eval_turn(runtime, config, request)

    unit = run_task_unit(
        store=store, task=task, tenant=tenant, family=arm_spec.family, turn_runner=_turn_runner
    )
    digest = unit.turn.prompt_bundle_sha256
    if run_bundle.reference_found and digest != run_bundle.reference:
        mismatch_fields: dict[str, str | int | float | None] = {
            "error": "prompt-bundle mismatch",
            BUNDLE_MISMATCH_ATTRIBUTE: True,
            PROMPT_BUNDLE_SHA256_KEY: digest,
        }
        record_item(
            store,
            run_id=run_id,
            arm_index=arm_index,
            repeat_index=ref.repeat_index,
            task_id=task_id,
            arm=arm_spec.family,
            item_status="failed",
            cost_usd=sum(usage.cost.total for usage in unit.usage_records),
            fields=mismatch_fields,
        )
        raise Conflict(
            "This evaluation run's turns ran under more than one prompt bundle.",
            detail=f"turn digest {digest} differs from the run's first {run_bundle.reference}",
        )
    record_item(
        store,
        run_id=run_id,
        arm_index=arm_index,
        repeat_index=ref.repeat_index,
        task_id=task_id,
        arm=arm_spec.family,
        item_status="done",
        cost_usd=sum(usage.cost.total for usage in unit.usage_records),
        fields=_unit_fields(unit),
    )
    return {"item_status": "done"}


# -- entry points --------------------------------------------------------------------------------


@functools.cache
def _default_s3() -> ObjectStore:
    import boto3

    client: ObjectStore = boto3.client("s3")
    return client


@functools.cache
def _default_runtime_client() -> RuntimeClient:
    import boto3
    from botocore.config import Config

    client: RuntimeClient = boto3.client(
        "bedrock-agentcore",
        config=Config(
            connect_timeout=10,
            read_timeout=200,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )
    return client


def _failure_suffix(exc: Exception) -> str:
    """`` code=<code> detail=<bounded detail>`` for a taxonomy error, else the empty string."""
    if not isinstance(exc, CrmError):
        return ""
    return f" code={exc.code} detail={_bounded(str(exc.detail or ''))}"


def _log(event_name: str, *, run_id: str, reason: str) -> None:
    _logger.info(json.dumps({"event": event_name, "correlation_id": run_id, "reason": reason}))


def sweep_control_handler(
    event: dict[str, Any],
    context: object = None,
    *,
    store: Store | None = None,
    s3: ObjectStore | None = None,
    config: SweepControlConfig | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """AWS Lambda entry point for every ``sweep_control`` step; see the module docstring.

    Args:
        event: ``{"step", "run_id", ...}`` as :data:`STEP_EVENT_KEYS` declares per step.
        context: The Lambda context, unused.
        store: Injectable persistence layer; built from ``CRM_TABLE_PREFIX`` when absent.
        s3: Injectable S3 client; built when absent.
        config: Injectable configuration; read from the environment when absent.
        now: Injectable clock for the timestamps written; defaults to now, UTC.

    Raises:
        ValidationFailed: The event is not a recognised step's shape.
        Exception: Any step failure, for the state machine's Retry and Catch.
    """
    del context
    step = event.get(_STEP_KEY)
    expected = STEP_EVENT_KEYS.get(step) if isinstance(step, str) else None
    if expected is None:
        raise ValidationFailed("Unknown sweep_control step.", source=_STEP_KEY, detail=repr(step))
    if step == "finalize":
        return _finalize_releasing_lock(event, store=store, config=config, now=now)
    _check_keys(event, expected, where=f"sweep_control:{step}")
    resolved_config = config if config is not None else SweepControlConfig.from_env()
    resolved_store = (
        store if store is not None else Store(table_prefix=resolved_config.table_prefix)
    )
    resolved_s3 = s3 if s3 is not None else _default_s3()
    run_id = _required_str(event, "run_id")
    _log("sweep_control", run_id=run_id, reason=f"step={step}")
    if step == "start":
        return _start(resolved_store, resolved_s3, resolved_config, event, now)
    if step == "seed":
        return _seed(resolved_store, event, now)
    if step == "item_failed":
        return _item_failed(resolved_store, event)
    if step == "aggregate":
        return _aggregate(resolved_store, resolved_s3, resolved_config, event)
    return _teardown(resolved_store, resolved_s3, resolved_config, event)


def _finalize_releasing_lock(
    event: Mapping[str, Any],
    *,
    store: Store | None,
    config: SweepControlConfig | None,
    now: datetime | None,
) -> dict[str, Any]:
    """Run ``finalize`` needing only the table prefix; a malformed event still releases the lock
    when its execution owns the run."""
    table_prefix = (
        config.table_prefix if config is not None else os.environ.get("CRM_TABLE_PREFIX", "")
    )
    resolved_store = store if store is not None else Store(table_prefix=table_prefix)
    run_id = event.get("run_id")
    execution_arn = event.get("execution_arn")
    try:
        _check_keys(event, STEP_EVENT_KEYS["finalize"], where="sweep_control:finalize")
        _log("sweep_control", run_id=_required_str(event, "run_id"), reason="step=finalize")
    except Exception:
        if isinstance(run_id, str) and run_id:
            _release_if_owned(resolved_store, run_id=run_id, execution_arn=execution_arn)
        raise
    return _finalize(resolved_store, event, now)


def _release_if_lock_names_execution(store: Store, *, run_id: str, execution_arn: str) -> None:
    """Release the lock when it names ``run_id`` and ``execution_arn``; log when it is kept."""
    try:
        lock = read_lock(store)
        if (
            lock is not None
            and lock.holder_run_id == run_id
            and lock.execution_arn == execution_arn
        ):
            release_lock(store, run_id=run_id)
            return
        reason = "the lock does not name this run and execution"
    except Exception as exc:  # noqa: BLE001 - the caller re-raises the ownership error
        reason = f"lock unreadable: {type(exc).__name__}"
    _logger.warning(
        json.dumps(
            {"event": "sweep_finalize_lock_retained", "correlation_id": run_id, "reason": reason}
        )
    )


def _release_if_owned(store: Store, *, run_id: str, execution_arn: object) -> None:
    """Release the lock for a malformed finalize whose execution owns the run; log any other case."""
    try:
        owned = (
            isinstance(execution_arn, str)
            and bool(execution_arn)
            and claim_execution(store, run_id=run_id, execution_arn=execution_arn)
            in OWNING_OUTCOMES
        )
        if owned:
            release_lock(store, run_id=run_id)
            return
        reason = "the malformed event's execution does not own the run"
    except Exception as exc:  # noqa: BLE001 - the caller re-raises the event's own error
        reason = f"ownership unreadable: {type(exc).__name__}"
    _logger.warning(
        json.dumps(
            {"event": "sweep_finalize_lock_retained", "correlation_id": run_id, "reason": reason}
        )
    )


def sweep_task_handler(
    event: dict[str, Any],
    context: object = None,
    *,
    store: Store | None = None,
    runtime: RuntimeClient | None = None,
    config: SweepTaskConfig | None = None,
) -> dict[str, Any]:
    """AWS Lambda entry point for one ``(arm_index, repeat_index, task_id)`` item; see the module
    docstring.

    Args:
        event: ``{"run_id", "arm_index", "repeat_index", "task_id"}``.
        context: The Lambda context, unused.
        store: Injectable persistence layer; built from ``CRM_TABLE_PREFIX`` when absent.
        runtime: Injectable ``bedrock-agentcore`` client; built when absent.
        config: Injectable configuration; read from the environment when absent.

    Returns:
        ``{"item_status": "done" | "failed" | "skipped_cancelled"}``.

    Raises:
        TurnTimeout: The eval turn timed out.
        ProviderUnavailable: A live provider failed after its retries.
        CrmError: Any other failure, for the state machine to record the item ``failed``.
    """
    del context
    resolved_config = config if config is not None else SweepTaskConfig.from_env()
    resolved_store = (
        store if store is not None else Store(table_prefix=resolved_config.table_prefix)
    )
    resolved_runtime = runtime if runtime is not None else _default_runtime_client()
    try:
        result = _sweep_task(resolved_store, resolved_runtime, resolved_config, event)
    except Exception as exc:
        _log(
            "sweep_task_failed",
            run_id=str(event.get("run_id")),
            reason=(
                f"arm_index={event.get('arm_index')} repeat_index={event.get('repeat_index')} "
                f"task_id={event.get('task_id')} "
                f"{type(exc).__name__}{_failure_suffix(exc)}"
            ),
        )
        raise
    _log(
        "sweep_task",
        run_id=str(event.get("run_id")),
        reason=f"item_status={result['item_status']}",
    )
    return result


__all__ = [
    "BUNDLE_MISMATCH_ATTRIBUTE",
    "COST_MEASURED_ATTRIBUTE",
    "DEMO_BASELINE_OBJECT",
    "RUNTIME_IDENTIFIER",
    "START_RESULT_KEYS",
    "STEP_EVENT_KEYS",
    "SWEEP_TASK_EVENT_KEYS",
    "SWEEP_TASK_RESULT_KEYS",
    "TASK_REF_KEYS",
    "EventKeys",
    "NotRunOwner",
    "SweepControlConfig",
    "SweepTaskConfig",
    "classify_runtime_failure",
    "session_id_for",
    "sweep_control_handler",
    "sweep_task_handler",
    "task_score_from_fields",
    "tier_scores_from_fields",
    "turn_from_row",
    "unit_from_row",
]
