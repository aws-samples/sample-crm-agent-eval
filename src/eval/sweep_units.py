# SPDX-License-Identifier: MIT
"""One sweep as independent task units, then one assembly per arm.

A sweep is the ``arm × repeat × task`` grid :func:`task_refs` enumerates. :func:`run_task_unit`
runs one cell: it captures the pre-turn values, obtains one :class:`eval.turn_observation.ObservedTurn` from
an injected :data:`TurnRunner` (the in-process harness, or the Runtime's ``eval_turn`` mode via the
sweep-task Lambda), scores it with :func:`eval.scoring.score_task` and
:func:`eval.scoring.score_tiers`, and meters it. :func:`assemble_arm` folds one arm's units into
the :class:`ArmRunResult` the matrix renders. The CLI harness and the state machine both go through
these two functions, so they score and aggregate identically.

Eval arms take their tuning from :class:`ArmSpec` only; no user settings are read.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Final, Literal, cast

from agents.answer_style import answer_style_prompt_sha256
from agents.tier_attribution import InvocationAttribution, ToolCallAttribution
from agents.turn_driver import session_turn_tuning
from crm.metering import TokenUsage, UsageRecord, build_usage_record
from crm.store.store import Store
from eval.matrix import (
    ARM_COST_TOTAL_KEY,
    ARM_LONG_CONTEXT_INVOCATION_COUNT_KEY,
    ArmResult,
    RunProvenance,
)
from eval.metrics import (
    ALL_TIERS,
    TASK_SPREAD_REPORT_KEY,
    TIER_ROLE_MODEL_REPORT_KEY,
    TOOL_METRICS_REPORT_KEY,
    CallLatencySample,
    Exclusion,
    ModelMetrics,
    TaskTierObservation,
    aggregate_latency_metrics,
    aggregate_model_metrics,
    aggregate_task_spread,
    aggregate_tier_role_model,
    aggregate_tier_summary,
    aggregate_tier_task_metrics,
    aggregate_tool_calls,
    build_tier_report_fields,
    require_records_attributed,
)
from eval.pricing import inference_profile_for
from eval.scoring import (
    AggregateScore,
    TaskScore,
    TierScore,
    TierScores,
    aggregate_scores,
    capture_pre_turn_values,
    score_task,
    score_tiers,
)
from eval.turn_observation import ObservedTurn
from providers.base import Tuning, validate_tuning
from providers.capabilities import CapabilityMatrix, Family, Tier
from providers.tier_tuning import TurnTuning

EvalMode = Literal["stub", "live"]

#: The tier whose model names an arm in provenance and the single-model metrics block.
ARM_MODEL_TIER: Final[Tier] = "supervisor"

#: The ``source`` recorded on every arm's tuning.
ARM_TUNING_SOURCE: Final[str] = "eval.harness.default_sweep"

#: The supervisor effort an arm requests when none is given.
DEFAULT_ARM_EFFORT: Final[str] = "medium"


@dataclass(frozen=True, slots=True)
class ArmSpec:
    """One sweep arm's provider family and tuning, before any task is run.

    Attributes:
        family: The provider family this arm exercises.
        effort: The reasoning-effort setting requested for the supervisor tier, or ``None`` to
            use the tier's own configured default.
    """

    family: Family
    effort: str | None = DEFAULT_ARM_EFFORT


@dataclass(frozen=True, slots=True)
class ArmRunResult:
    """One arm's fully assembled result, ready for rendering by :mod:`eval.matrix`.

    Attributes:
        arm_result: The rendered :class:`eval.matrix.ArmResult` for this arm.
        task_scores: Every task's :class:`eval.scoring.TaskScore` for this arm, in task order.
        model_metrics: This arm's aggregated :class:`eval.metrics.ModelMetrics`.
        aggregate_score: This arm's aggregated :class:`eval.scoring.AggregateScore`.
    """

    arm_result: ArmResult
    task_scores: tuple[TaskScore, ...]
    model_metrics: ModelMetrics
    aggregate_score: AggregateScore


@dataclass(frozen=True, slots=True)
class TaskRef:
    """One cell of the sweep grid.

    Attributes:
        arm_index: The arm's position in the sweep's arm specs.
        repeat_index: The repeat's position, from 0.
        task_id: The golden task's ``id``.
    """

    arm_index: int
    repeat_index: int
    task_id: str


@dataclass(frozen=True, slots=True)
class TaskUnitResult:
    """One task unit's scored, metered result.

    Attributes:
        task_id: The golden task's ``id``.
        task_score: The :func:`eval.scoring.score_task` result.
        tier_scores: The :func:`eval.scoring.score_tiers` result.
        turn: The observed turn the scores were computed from.
        usage_records: One priced usage record per invocation attribution of the turn.
    """

    task_id: str
    task_score: TaskScore
    tier_scores: TierScores
    turn: ObservedTurn
    usage_records: tuple[UsageRecord, ...]


#: Produces the observed turn for one golden task in one tenant. The task's pre-turn values have
#: already been captured when it is called.
TurnRunner = Callable[[Mapping[str, object], str], ObservedTurn]

#: Persists one arm's pre-aggregation raw records, keyed by arm index and model id.
RawRecordsWriter = Callable[[int, str, dict[str, object]], None]


def arm_tuning(arm_spec: ArmSpec) -> Tuning:
    """The single-family session tuning an arm runs every task with."""
    return Tuning(
        source=ARM_TUNING_SOURCE,
        family=arm_spec.family,
        effort=arm_spec.effort,
    )


def arm_turn_tuning(arm_spec: ArmSpec) -> TurnTuning:
    """The per-tier tuning an arm's turns run with, via :func:`agents.turn_driver.session_turn_tuning`."""
    return session_turn_tuning(arm_tuning(arm_spec))


def task_refs(
    tasks: Sequence[Mapping[str, object]], arm_specs: Sequence[ArmSpec], repeats: int = 1
) -> tuple[TaskRef, ...]:
    """Every ``(arm, repeat, task)`` cell: arm-major, then repeat, then task order, the order the
    in-process sweep runs.

    Raises:
        ValueError: ``repeats`` is below 1.
    """
    if repeats < 1:
        raise ValueError(f"repeats must be >= 1, got {repeats}")
    return tuple(
        TaskRef(arm_index=arm_index, repeat_index=repeat_index, task_id=_task_id(task))
        for arm_index in range(len(arm_specs))
        for repeat_index in range(repeats)
        for task in tasks
    )


def _task_id(task: Mapping[str, object]) -> str:
    return str(task.get("id") or "unknown-task")


def task_actor(task: Mapping[str, object]) -> dict[str, object]:
    """Return the task's ``actor`` block.

    Raises:
        ValueError: The task carries no usable ``actor`` mapping.
    """
    actor = task.get("actor")
    if not isinstance(actor, dict):
        raise ValueError(f"task {task.get('id')!r} carries no usable 'actor' block")
    return actor


def usage_records_for_turn(
    invocations: Sequence[InvocationAttribution],
    *,
    task_id: str,
    rep_id: str,
    family: Family,
) -> tuple[UsageRecord, ...]:
    """One priced :class:`crm.metering.UsageRecord` per invocation attribution.

    Each call is priced on its own under its model's configured inference profile, so its
    standard or long-context tier follows that call's prompt size. Records are ordered by
    ``(tier, role, model_id)``, attribution order kept within a group.

    Raises:
        ValueError: An attributed model has no configured inference profile.
    """
    ordered = sorted(invocations, key=lambda a: (a.tier, a.role, a.model_id))
    profiles = {a.model_id: inference_profile_for(a.model_id) for a in ordered}
    return tuple(
        build_usage_record(
            correlation_id=task_id,
            turn_id=task_id,
            invocation_id=str(uuid.uuid4()),
            rep_id=rep_id,
            model_id=attribution.model_id,
            family=family,
            tier=attribution.tier,
            usage=TokenUsage(
                input_tokens=attribution.tokens.input_tokens,
                output_tokens=attribution.tokens.output_tokens,
                cache_write_tokens=attribution.cache.write_tokens,
                cache_read_tokens=attribution.cache.read_tokens,
            ),
            inference_profile=profiles[attribution.model_id],
        )
        for attribution in ordered
    )


def arm_total_fields(records: Sequence[UsageRecord], *, withhold_cost: bool) -> dict[str, object]:
    """Return an arm's all-tier totals over every one of its priced usage records.

    ``arm_cost_total`` is the exactly rounded sum of every record's cost, so it does not depend on
    record order, or ``None`` while cost is withheld;
    ``arm_long_context_invocation_count`` counts the records priced at long-context rates.
    """
    return {
        ARM_COST_TOTAL_KEY: None if withhold_cost else math.fsum(r.cost.total for r in records),
        ARM_LONG_CONTEXT_INVOCATION_COUNT_KEY: sum(
            1 for r in records if r.cost.pricing_tier == "long_context"
        ),
    }


def _tier_scores(
    task: Mapping[str, object], turn: ObservedTurn, task_score: TaskScore
) -> TierScores:
    """Score the task's tiers; a task declaring no ``expected_tiers`` is not applicable at any tier.

    Raises:
        ValueError: The task's ``expected_tiers`` block is present but malformed.
    """
    if "expected_tiers" not in task:
        return TierScores(
            task_id=_task_id(task),
            supervisor=TierScore(tier="supervisor", applicable=False, passed=None),
            subagent=TierScore(tier="subagent", applicable=False, passed=None),
            tool=TierScore(tier="tool", applicable=False, passed=None),
        )
    return score_tiers(task, turn.outcome, turn.tool_arguments, task_score=task_score)


def run_task_unit(
    *,
    store: Store,
    task: Mapping[str, object],
    tenant: str,
    family: Family,
    turn_runner: TurnRunner,
) -> TaskUnitResult:
    """Run, score and meter one golden task.

    Args:
        store: The store the task is scored against; read before and after the turn.
        task: The localized golden task.
        tenant: The eval tenant the turn ran in.
        family: The arm's family, recorded on each usage record.
        turn_runner: Produces the task's observed turn.

    Raises:
        ValueError: The task carries no usable ``actor`` block.
    """
    actor = task_actor(task)
    task_id = _task_id(task)
    pre_turn_values = capture_pre_turn_values(store, task=task, tenant=tenant)
    turn = turn_runner(task, tenant)
    task_score = score_task(store, task=task, outcome=turn.outcome, pre_turn_values=pre_turn_values)
    return TaskUnitResult(
        task_id=task_id,
        task_score=task_score,
        tier_scores=_tier_scores(task, turn, task_score),
        turn=turn,
        usage_records=usage_records_for_turn(
            turn.invocations, task_id=task_id, rep_id=str(actor["rep_id"]), family=family
        ),
    )


def _raw_arm_records_payload(
    *,
    model_id: str,
    mode: EvalMode,
    attributions: Sequence[InvocationAttribution],
    tool_calls: Sequence[ToolCallAttribution],
    task_scores: Sequence[TaskScore],
    correctness_by_task: Mapping[str, Sequence[float]],
    task_latencies_ms: Sequence[float],
    call_samples: Sequence[CallLatencySample],
) -> dict[str, object]:
    """A JSON-safe snapshot of one arm's raw per-run records, taken before aggregation."""
    return {
        "model_id": model_id,
        "mode": mode,
        "invocation_attributions": [asdict(a) for a in attributions],
        "tool_call_attributions": [asdict(t) for t in tool_calls],
        "task_scores": [asdict(s) for s in task_scores],
        "correctness_by_task": {
            task_id: list(values) for task_id, values in correctness_by_task.items()
        },
        "task_latencies_ms": list(task_latencies_ms),
        "call_latency_samples": [asdict(c) for c in call_samples],
    }


def assemble_arm(
    units: Sequence[TaskUnitResult],
    *,
    matrix: CapabilityMatrix,
    arm_spec: ArmSpec,
    mode: EvalMode,
    dataset_seed: int,
    git_revision: str,
    runtime_identifier: str,
    price_table_version: str,
    cost_withheld: bool,
    arm_index: int = 0,
    provider_stream_calls: int | None = None,
    residual_exclusions: Sequence[Exclusion] = (),
    raw_records_writer: RawRecordsWriter | None = None,
    timestamp: str | None = None,
) -> ArmRunResult:
    """Fold one arm's task units, in run order, into its scored, metered :class:`ArmRunResult`.

    Args:
        units: The arm's task units in the order they ran.
        matrix: The capability matrix; the arm's supervisor model names it.
        arm_spec: The arm.
        mode: ``"stub"`` or ``"live"``; latency is reported for live arms only.
        dataset_seed: The fixture seed recorded in provenance.
        git_revision: The revision recorded in provenance.
        runtime_identifier: The runtime recorded in provenance.
        price_table_version: The price table version recorded in provenance.
        cost_withheld: Whether currency figures are withheld.
        arm_index: The arm's position, passed to ``raw_records_writer``.
        provider_stream_calls: The arm-level count of ``provider.stream`` calls, when the caller
            owns the provider; must equal the sum the units' drivers reported. ``None`` when the
            turns ran out of process.
        residual_exclusions: Exclusions drained from the arm's provider after its turns, beyond
            those each unit's turn carried.
        raw_records_writer: Persists the pre-aggregation raw records, or ``None``.
        timestamp: The provenance timestamp; defaults to now (UTC, ISO 8601).

    Raises:
        AssertionError: ``provider_stream_calls`` differs from the units' driven stream calls,
            a unit's or the arm's invocation attributions (every tier) outnumber the stream calls
            that could have produced them, ``included_invocation_count`` exceeds those calls, or
            the units carry different prompt-bundle digests.
    """
    tuning = arm_tuning(arm_spec)
    resolved = validate_tuning(matrix.model_for(arm_spec.family, ARM_MODEL_TIER), tuning)
    measured_latency = mode == "live"

    task_scores = [unit.task_score for unit in units]
    all_attributions = [inv for unit in units for inv in unit.turn.invocations]
    all_tool_calls = [call for unit in units for call in unit.turn.tool_calls]
    all_usage_records = [record for unit in units for record in unit.usage_records]
    included_usage = [
        record
        for record in all_usage_records
        if record.tier == ARM_MODEL_TIER and record.model_id == resolved.model.model_id
    ]
    driven_stream_calls = sum(unit.turn.stream_calls for unit in units)
    bundle_digests = {unit.turn.prompt_bundle_sha256 for unit in units}
    if len(bundle_digests) > 1:
        raise AssertionError(
            f"this arm's task units ran under {len(bundle_digests)} different prompt bundles "
            f"({sorted(str(digest) for digest in bundle_digests)}): one arm must measure one "
            "instrument, so its units cannot be folded into one result"
        )
    correctness_by_task: dict[str, list[float]] = {}
    for unit in units:
        if unit.task_score.correctness is not None:
            correctness_by_task.setdefault(unit.task_id, []).append(unit.task_score.correctness)
    task_latencies_ms = (
        [unit.turn.turn_latency_ms for unit in units if unit.turn.turn_latency_ms is not None]
        if measured_latency
        else []
    )
    exclusions = [e for unit in units for e in unit.turn.exclusions] + list(residual_exclusions)

    aggregate_score = aggregate_scores(task_scores)
    call_samples = [
        CallLatencySample(tier=attribution.tier, latency_ms=attribution.latency_ms)
        for attribution in all_attributions
        if attribution.latency_ms is not None
    ]
    latency = aggregate_latency_metrics(
        measured=measured_latency,
        task_latencies_ms=task_latencies_ms,
        call_samples=call_samples,
    )

    require_records_attributed(all_usage_records, all_attributions)

    model_metrics = aggregate_model_metrics(
        resolved.model.model_id,
        included=included_usage,
        exclusions=exclusions,
        withhold_cost=cost_withheld,
        latency=latency,
    )
    if provider_stream_calls is not None and provider_stream_calls != driven_stream_calls:
        raise AssertionError(
            f"provider.stream was called {provider_stream_calls} time(s) for this arm but "
            f"its turn drivers made {driven_stream_calls}: every stream call must originate "
            "inside a turn's driver, so a call made anywhere else -- a second usage-recording pass, "
            "a double-invoke -- shows up as this mismatch and fails the arm rather than silently "
            "doubling spend. (This counts calls, not usage events, so the zero-invocation refusal "
            "path -- a real call that reports no usage -- is unaffected.)"
        )
    stream_calls = (
        provider_stream_calls if provider_stream_calls is not None else driven_stream_calls
    )
    for unit in units:
        if len(unit.turn.invocations) > unit.turn.stream_calls:
            raise AssertionError(
                f"task unit {unit.task_id!r} recorded {len(unit.turn.invocations)} invocation "
                f"attribution(s) across its tiers but its driver made only "
                f"{unit.turn.stream_calls} provider.stream call(s): each call yields at most one "
                "attribution, so a surplus is a double-recorded usage"
            )
    if len(all_attributions) > stream_calls:
        raise AssertionError(
            f"this arm recorded {len(all_attributions)} invocation attribution(s) across its "
            f"tiers but provider.stream was only called {stream_calls} time(s): each call yields "
            "at most one attribution, so a surplus is a double-recorded usage"
        )
    if model_metrics.included_invocation_count > stream_calls:
        raise AssertionError(
            f"included_invocation_count is {model_metrics.included_invocation_count} but "
            f"provider.stream was only called {stream_calls} time(s): a metered "
            "usage record cannot outnumber the calls that could have produced one."
        )

    if raw_records_writer is not None:
        raw_records_writer(
            arm_index,
            resolved.model.model_id,
            _raw_arm_records_payload(
                model_id=resolved.model.model_id,
                mode=mode,
                attributions=all_attributions,
                tool_calls=all_tool_calls,
                task_scores=task_scores,
                correctness_by_task=correctness_by_task,
                task_latencies_ms=task_latencies_ms,
                call_samples=call_samples,
            ),
        )

    tier_role_model = aggregate_tier_role_model(all_attributions, withhold_cost=cost_withheld)
    tool_aggregates = aggregate_tool_calls(all_tool_calls)
    task_spread = aggregate_task_spread(correctness_by_task)
    tier_fields = build_tier_report_fields(tier_role_model, tool_aggregates, task_spread)
    tier_summary = aggregate_tier_summary(
        tier_role_model,
        latency,
        [unit.tier_scores for unit in units],
        withhold_cost=cost_withheld,
    )
    tier_task_metrics = aggregate_tier_task_metrics(
        [
            TaskTierObservation(
                task_id=unit.task_id,
                tier_scores=unit.tier_scores,
                invocations=unit.turn.invocations,
                delegation_outcome=unit.turn.outcome.denial_outcome,
                resolved_efforts=unit.turn.resolved_efforts,
                tool_calls=unit.turn.tool_calls,
            )
            for unit in units
        ],
        measured=measured_latency,
        family=arm_spec.family,
        configured_model_ids={
            tier: matrix.model_for(arm_spec.family, tier).model_id for tier in ALL_TIERS
        },
    )

    provenance = RunProvenance(
        mode=mode,
        timestamp=timestamp if timestamp is not None else datetime.now(UTC).isoformat(),
        model_id=resolved.model.model_id,
        effort=resolved.effort,
        dataset_seed=dataset_seed,
        git_revision=git_revision,
        runtime_identifier=runtime_identifier,
        price_table_version=price_table_version,
        answer_style_sha256=answer_style_prompt_sha256(),
        prompt_bundle_sha256=next(iter(bundle_digests), None),
    )
    arm_result = ArmResult(
        provenance=provenance,
        model_metrics_fields=model_metrics.to_report_fields(),
        aggregate_score_fields=aggregate_score.to_report_fields(),
        task_score_fields=tuple(score.to_report_fields() for score in task_scores),
        tier_role_model_fields=tuple(
            cast("list[dict[str, object]]", tier_fields[TIER_ROLE_MODEL_REPORT_KEY])
        ),
        tool_fields=tuple(cast("list[dict[str, object]]", tier_fields[TOOL_METRICS_REPORT_KEY])),
        task_spread_fields=tuple(
            cast("list[dict[str, object]]", tier_fields[TASK_SPREAD_REPORT_KEY])
        ),
        tier_summary_fields=tier_summary.to_report_fields(),
        usage_lower_bound_task_ids=tuple(
            unit.task_id for unit in units if unit.turn.usage_lower_bound
        ),
        tier_task_metrics_fields=tier_task_metrics.to_report_fields(),
        arm_totals_fields=arm_total_fields(all_usage_records, withhold_cost=cost_withheld),
    )
    return ArmRunResult(
        arm_result=arm_result,
        task_scores=tuple(task_scores),
        model_metrics=model_metrics,
        aggregate_score=aggregate_score,
    )


__all__ = [
    "ARM_MODEL_TIER",
    "ARM_TUNING_SOURCE",
    "DEFAULT_ARM_EFFORT",
    "ArmRunResult",
    "ArmSpec",
    "EvalMode",
    "RawRecordsWriter",
    "TaskRef",
    "TaskUnitResult",
    "TurnRunner",
    "arm_tuning",
    "arm_turn_tuning",
    "assemble_arm",
    "run_task_unit",
    "task_actor",
    "task_refs",
    "arm_total_fields",
    "usage_records_for_turn",
]
