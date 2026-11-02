# SPDX-License-Identifier: MIT
"""Four-part deterministic matrix renderer for one sweep's computed scores and metrics.

Renders already-computed :class:`eval.metrics.ModelMetrics`, :class:`eval.scoring.AggregateScore`,
and per-task :class:`eval.scoring.TaskScore` report fields into:

1. ``matrix.json`` — the full :class:`MatrixResult` structure.
2. ``matrix.md`` — the same structure in human-readable Markdown.
3. ``matrix.csv`` — one flat row per scored task.
4. A per-arm raw JSON archive beneath ``results/raw/``, for later re-rendering.

Every ``render_*`` function is a pure function of its input with a fixed key order, so identical
input renders byte-identically. ``matrix.md`` and ``matrix.json`` also carry a generated statement
of what each aggregate figure does and does not support (:func:`render_figure_semantics`).
``matrix.json`` and ``matrix.md`` open with a per-tier comparison of the arms
(:func:`tier_comparison`); ``matrix.csv`` carries it as its trailing ``summary_*`` columns. No
render function computes a verdict, threshold comparison, or recommendation, and cost fields
appear only when the arm's metrics carry them.

:attr:`RunProvenance.runtime_identifier` is rendered unredacted and may be a full runtime ARN;
``results/raw/`` is gitignored. ``python3 -m eval.matrix`` re-renders the three top-level
artifacts from the raw archives (:func:`load_raw_archives`, :func:`arm_from_report_fields`).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, cast

from eval.scoring import POSITIVE_ASSERTION_NAME_PREFIXES

#: The tracked ``<repo>/results`` directory; an all-stub matrix is never written into it.
TRACKED_RESULTS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "results"


class StubMatrixOverwriteRefused(RuntimeError):  # noqa: N818
    """An all-stub matrix was about to be written into :data:`TRACKED_RESULTS_DIR`."""


@dataclass(frozen=True, slots=True)
class RunProvenance:
    """Reproducibility provenance for one sweep arm's results.

    Attributes:
        mode: ``"stub"`` or ``"live"`` — which provider backend produced this arm's results.
        timestamp: ISO-8601 UTC timestamp the caller captured when the arm's run started.
        model_id: The Bedrock model identifier this arm exercised.
        effort: The reasoning-effort setting used, or ``None`` when the model accepts none.
        dataset_seed: The fixture generator's pinned seed the golden tasks were scored against.
        git_revision: The repository's git commit hash at run time.
        runtime_identifier: The AgentCore Runtime (or local stub runtime) identifier that executed
            this arm. May be a full ARN, which embeds an AWS account id; see the module docstring.
        price_table_version: The ``config/pricing.yaml`` version this arm's cost figures (if any)
            were computed against, matching :attr:`eval.metrics.CostTotals.price_table_version`
            when cost was not withheld.
        answer_style_sha256: SHA-256 of the answer-style system prompt text this arm ran with
            (:func:`agents.answer_style.answer_style_prompt_sha256`), which ``turn_driver``
            prepends on every turn.
        prompt_bundle_sha256: SHA-256 of the prompt bundle every task unit of this arm ran with
            (:func:`agents.prompt_bundle.prompt_bundle_sha256`), or ``None`` when the archive
            carries none.
        legacy_temperature: The ``temperature`` a past archive recorded, carried through as read.
            Meaningful only when ``records_legacy_temperature`` is set.
        records_legacy_temperature: Whether the archive this provenance was read from carried a
            ``temperature`` key. New runs never set it.
    """

    mode: str
    timestamp: str
    model_id: str
    effort: str | None
    dataset_seed: int
    git_revision: str
    runtime_identifier: str
    price_table_version: str
    answer_style_sha256: str
    prompt_bundle_sha256: str | None = None
    legacy_temperature: float | None = None
    records_legacy_temperature: bool = False

    def to_report_fields(self) -> dict[str, object]:
        """Render this provenance as a flat, JSON-safe mapping in a fixed key order.

        A legacy archive's ``temperature`` is emitted after ``effort``, where it was recorded.
        """
        fields: dict[str, object] = {
            "mode": self.mode,
            "timestamp": self.timestamp,
            "model_id": self.model_id,
            "effort": self.effort,
        }
        if self.records_legacy_temperature:
            fields["temperature"] = self.legacy_temperature
        fields.update(
            {
                "dataset_seed": self.dataset_seed,
                "git_revision": self.git_revision,
                "runtime_identifier": self.runtime_identifier,
                "price_table_version": self.price_table_version,
                "answer_style_sha256": self.answer_style_sha256,
                "prompt_bundle_sha256": self.prompt_bundle_sha256,
            }
        )
        return fields


#: The optional arm report key listing the task runs whose usage is a lower bound.
USAGE_LOWER_BOUND_TASK_IDS_KEY: Final[str] = "usage_lower_bound_task_ids"

#: The optional arm report key carrying `eval.metrics.TierTaskMetrics.to_report_fields`.
TIER_TASK_METRICS_KEY: Final[str] = "tier_task_metrics"


def errored_items_cost_note(errored_item_count: int) -> str | None:
    """The run-level cost qualifier for items that ended in an error, or ``None`` when none did."""
    if errored_item_count <= 0:
        return None
    return (
        f"Cost excludes {errored_item_count} item(s) that ended in an error: their model calls "
        "were billed, but their usage was not measured."
    )


def usage_lower_bound_note(arm: ArmResult) -> str | None:
    """The arm's usage-completeness note, or ``None`` when every task run reported its usage."""
    task_ids = arm.usage_lower_bound_task_ids
    if not task_ids:
        return None
    return (
        f"Usage and cost for this arm are a lower bound: in {len(task_ids)} task run(s) "
        f"({', '.join(task_ids)}) a model call ended before reporting token usage, so its "
        "tokens and cost are not counted in the figures above."
    )


@dataclass(frozen=True, slots=True)
class ArmResult:
    """One model/configuration arm's full set of measurements for one sweep.

    Attributes:
        provenance: Reproducibility provenance for this arm.
        model_metrics_fields: The arm's :class:`eval.metrics.ModelMetrics.to_report_fields`
            output, carried through unmodified — usage totals, cost (or its withholding flag),
            cache threshold, and categorised exclusions.
        aggregate_score_fields: The arm's :class:`eval.scoring.AggregateScore.to_report_fields`
            output, carried through unmodified.
        task_score_fields: Every scored task's :class:`eval.scoring.TaskScore.to_report_fields`
            output for this arm, in the order the caller scored them.
        tier_role_model_fields: One `eval.metrics.TierRoleModelMetrics.to_report_fields` mapping per
            ``(tier, role, model_id)`` group this arm exercised. Empty when absent.
        tool_fields: One `eval.metrics.ToolAggregate.to_report_fields` mapping per tool this arm
            called. Empty when absent.
        task_spread_fields: One `eval.metrics.TaskSpread.to_report_fields` mapping per golden task,
            carrying the correctness spread across that task's repeats. Empty when absent.
        tier_summary_fields: The arm's `eval.metrics.TierSummary.to_report_fields` mapping, one
            entry per tier. Empty when absent.
        usage_lower_bound_task_ids: The task runs, in run order, in which some model call ended
            before reporting token usage, so the arm's usage and cost under-count them. Written
            to the report only when non-empty; an archive without the key reads as empty.
        tier_task_metrics_fields: The arm's `eval.metrics.TierTaskMetrics.to_report_fields`
            mapping. Written to the report only when non-empty; an archive without the key reads
            as empty.
    """

    provenance: RunProvenance
    model_metrics_fields: dict[str, object]
    aggregate_score_fields: dict[str, object]
    task_score_fields: tuple[dict[str, object], ...] = field(default_factory=tuple)
    tier_role_model_fields: tuple[dict[str, object], ...] = field(default_factory=tuple)
    tool_fields: tuple[dict[str, object], ...] = field(default_factory=tuple)
    task_spread_fields: tuple[dict[str, object], ...] = field(default_factory=tuple)
    tier_summary_fields: dict[str, object] = field(default_factory=dict)
    usage_lower_bound_task_ids: tuple[str, ...] = field(default_factory=tuple)
    tier_task_metrics_fields: dict[str, object] = field(default_factory=dict)

    def to_report_fields(self) -> dict[str, object]:
        """Render this arm as a flat, JSON-safe mapping in a fixed key order."""
        fields: dict[str, object] = {
            "provenance": self.provenance.to_report_fields(),
            "model_metrics": self.model_metrics_fields,
            "aggregate_score": self.aggregate_score_fields,
            "task_scores": list(self.task_score_fields),
            "tier_role_model_metrics": list(self.tier_role_model_fields),
            "tool_metrics": list(self.tool_fields),
            "task_spread": list(self.task_spread_fields),
            "tier_summary": dict(self.tier_summary_fields),
        }
        if self.tier_task_metrics_fields:
            fields[TIER_TASK_METRICS_KEY] = dict(self.tier_task_metrics_fields)
        if self.usage_lower_bound_task_ids:
            fields[USAGE_LOWER_BOUND_TASK_IDS_KEY] = list(self.usage_lower_bound_task_ids)
        return fields


@dataclass(frozen=True, slots=True)
class MatrixResult:
    """The complete per-model-arm comparison matrix for one sweep.

    Attributes:
        arms: Every arm in the sweep, in the order the caller supplies; never resorted.
        errored_item_count: The run's items that ended in an error and so contribute no measured
            usage or cost; ``matrix.md`` states the exclusion when it is above zero.
    """

    arms: tuple[ArmResult, ...] = field(default_factory=tuple)
    errored_item_count: int = 0

    def to_report_fields(self) -> dict[str, object]:
        """Render the full matrix as a flat, JSON-safe mapping in a fixed key order.

        Leads with the ``tier_comparison`` block (:func:`tier_comparison`) and carries a
        ``figure_semantics`` block after ``arms`` — see :func:`render_figure_semantics`.
        """
        return {
            "tier_comparison": tier_comparison(self),
            "arms": [arm.to_report_fields() for arm in self.arms],
            "figure_semantics": render_figure_semantics(self),
        }


#: Source cited by the figure-boundary notes.
_SEMANTICS_SOURCE: Final[str] = "src/eval/matrix.py"

#: Boundary statement for ``mean_correctness``.
_MEAN_CORRECTNESS_SEMANTICS: Final[str] = (
    "The mean fraction of machine-checkable assertions satisfied over the tasks that were actually "
    "scored. Not a measure of end-to-end task success: an assertion naming a record that does not "
    "exist under the tenant an arm ran against lowers this figure whatever the turn did. Most of "
    "what it averages are negative guards a turn that does nothing at all satisfies, so its "
    "dynamic range is compressed well above zero and the figure is not a task-achievement rate; "
    "each arm's own positive-versus-guard composition, including the positive-assertion "
    "satisfaction rate, is stated with its aggregate figures."
)

#: Boundary statement for ``bad_write_rate``; the per-arm basis is :func:`bad_write_rate_basis`.
_BAD_WRITE_RATE_SEMANTICS: Final[str] = (
    "Reports only what a captured pre-turn baseline makes observable: a `must_not_change` assertion "
    "with no baseline is reported unscored and contributes nothing. Each arm's own basis is stated "
    "with its aggregate figures."
)

#: Boundary statement for ``tool_call_correct_rate``, enumerating every condition the underlying
#: assertion can fail on.
_TOOL_CALL_CORRECT_RATE_SEMANTICS: Final[str] = (
    "Reports that each scored turn issued a tool call which dispatched against an in-scope target. "
    "It is derived from exactly three conditions — a turn that issued no tool call at all, a "
    "proposal submitted on a task whose proposal limit is zero, and a proposal targeting a record "
    "other than the expected one — and from nothing else. It therefore does not report whether the "
    "turn chose the most apt tool for the task; where a task names the retrieval its answer "
    "requires, an `expected_reads` assertion checks that at least one call to a named tool reached "
    "the named record, and contributes to `mean_correctness` and deliberately not to this figure. "
    "An entry whose `target_id` is null checks only that one of the "
    "named tools was called at all, never that any particular record was reached, because the tools "
    "it names take no record id. Read to its exact boundary: it is satisfied incidentally by a "
    "turn that calls the named tool on the named record among many other calls, it says nothing "
    "about what the turn then reported, and it is therefore a necessary condition for an apt "
    "retrieval and never a sufficient one (src/eval/scoring.py, `_check_expected_reads`)."
)

#: Boundary statement for report quality, which no figure measures.
_REPORT_QUALITY_SEMANTICS: Final[str] = (
    "Not measured anywhere in this project, by any figure in this artifact. Two tasks score "
    "retrieval in place of report quality: grading a multi-part prose report needs a judge model, "
    "which this project forbids, so their assertions check that the turn read the records the "
    "answer requires and nothing checks what the turn then wrote. A turn that reads exactly the "
    "right records and then reports them incompletely, or wrongly, still satisfies those "
    "assertions and still counts toward `mean_correctness`. The `lookup` tasks' positive signal is "
    "retrieval on the same terms, and for two of the four it is weaker still: their "
    "`expected_reads` entry carries a null `target_id`, so the satisfied assertion evidences only "
    "that the named whole-book tool was called at all, not that any particular record was read. "
    "The narrowing was authorised deliberately and each narrowed task's own `notes` in "
    "golden_tasks.yaml restate the superseded reasoning at length."
)

#: Phrases every retrieval-narrowed golden task's ``notes`` carries. Tests use them to derive the
#: narrowed ids from the task file and compare them to :data:`_RETRIEVAL_NARROWED_TASK_IDS`.
_NARROWED_TASK_NOTE_MARKERS: Final[tuple[str, ...]] = (
    "judge model",
    "deliberate narrowing",
    "retrieval",
    "not the same claim",
)

#: The golden tasks whose correctness figure scores retrieval in place of report quality, named in
#: ``matrix.md``.
_RETRIEVAL_NARROWED_TASK_IDS: Final[tuple[str, ...]] = (
    "gt-04-investigation-turn-budget-partial",
    "gt-05-investigation-large-tool-result",
)

#: Source cited by the retrieval-narrowing notes.
_RETRIEVAL_CONSERVATISM_SOURCE: Final[str] = "src/eval/golden_tasks.yaml"

#: Boundary statement for a retrieval-narrowed task's unsatisfied assertions; per-arm magnitudes
#: are :func:`retrieval_conservatism`.
_RETRIEVAL_CONSERVATISM_SEMANTICS: Final[str] = (
    "A task narrowed to retrieval names the specific retrieval its assertions check, and names only "
    "the retrievals that expose an observable target id. A turn that answers such a task by an "
    "equally apt retrieval which exposes no target id therefore satisfies none of that task's "
    "positive assertions, for a reason unrelated to what the model did. The deduction that "
    "conservatism makes to `mean_correctness` is quantified per arm with that arm's aggregate "
    "figures, as an upper bound derived from the arm's own per-task records, so a reader of this "
    "artifact alone can size it against the figures it appears beside."
)

#: Boundary statement for the ``effort`` label in every arm's provenance.
_EFFORT_LABEL_SEMANTICS: Final[str] = (
    "The reasoning-effort string sent to each arm's supervisor tier, pinned identically across "
    "arms so the sweep is reproducible; the subagent and tool tiers run their config/models.yaml "
    "tier effort. It is not a claim that the arms were given equivalent reasoning "
    "budgets: each family declares its own effort scale with its own rung count and its own "
    "default, so an identical label sits at a different position on each family's scale. The "
    "declared scales, and this non-equivalence, are recorded in "
    "config/model_capabilities.yaml under `effort_scale_comparability.compared_models`, per tier. "
    "Sweeps recorded before tier efforts were configured ran the subagent and tool tiers at "
    "each model's own default "
    "effort (the Anthropic subagent at `high`), so their subagent and tool tiers are not "
    "effort-comparable with this artifact's."
)

#: Assertion-name prefix identifying a ``must_not_change`` assertion in a task's
#: ``assertion_results``, as named by :func:`eval.scoring._check_must_not_change`.
_MUST_NOT_CHANGE_PREFIX: Final[str] = "must_not_change["

#: Source cited by the cached-read accounting note.
_TOKEN_ACCOUNTING_SOURCE: Final[str] = "src/crm/metering.py"  # noqa: S105 - a path, not a credential

#: Boundary statement for the per-arm ``input_tokens`` figures; per-family declarations are
#: rendered by :func:`token_accounting_disclosure`.
_TOKEN_ACCOUNTING_SEMANTICS: Final[str] = (
    "Each arm's `input_tokens` is the count its own provider family reported, and the families "  # noqa: S105 - prose, not a credential
    "report cached prompt reads in differently-shaped usage objects: one as a sibling of "
    "`input_tokens`, the other nested inside its breakdown. Whether a family's `input_tokens` "
    "already contains the `cache_read_tokens` printed beside it is therefore a per-family question, "
    "and this project declares it per family with the evidence behind the declaration rather than "
    "assuming the two figures count the same thing. Two consequences, both exact: the arms' "
    "`input_tokens` are not established to be like-for-like totals and should not be differenced or "
    "ratioed as though they were; and while any family's accounting is declared unconfirmed, "
    "pricing refuses to produce a publishable currency figure for it at all, so no cost comparison "
    "can rest on the ambiguity. No token figure in this artifact is adjusted by this disclosure — "
    "each is the count that was reported."
)


def token_accounting_disclosure() -> dict[str, object]:
    """Publish each provider family's declared cached-read accounting as data.

    Rendered from :data:`crm.metering.TOKEN_ACCOUNTING`, families in sorted order.

    Returns:
        A JSON-safe mapping in a fixed key order carrying the boundary statement and one entry per
        declared family.
    """
    from crm.metering import TOKEN_ACCOUNTING

    return {
        "source": _TOKEN_ACCOUNTING_SOURCE,
        "note": _TOKEN_ACCOUNTING_SEMANTICS,
        "per_family": [
            TOKEN_ACCOUNTING[family].to_report_fields() for family in sorted(TOKEN_ACCOUNTING)
        ],
    }


def _must_not_change_counts(arm: ArmResult) -> tuple[int, int]:
    """Count an arm's ``must_not_change`` assertions, and how many had a pre-turn baseline.

    A ``must_not_change`` assertion is reported ``unscored`` by
    :func:`eval.scoring._check_must_not_change` exactly when no baseline was captured, so the
    ``unscored`` flag is the signal read here.

    Args:
        arm: The arm whose per-task records are inspected.

    Returns:
        ``(declared, with_baseline)`` — how many ``must_not_change`` assertions this arm's tasks
        declared, and how many of those were scored against a captured baseline.
    """
    declared = 0
    with_baseline = 0
    for task_fields in arm.task_score_fields:
        results = task_fields.get("assertion_results")
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict):
                continue
            if not str(result.get("name", "")).startswith(_MUST_NOT_CHANGE_PREFIX):
                continue
            declared += 1
            if not bool(result.get("unscored", False)):
                with_baseline += 1
    return declared, with_baseline


def bad_write_rate_basis(arm: ArmResult) -> dict[str, object]:
    """Describe what ``bad_write_rate`` rests on for one arm, from that arm's own records.

    An arm whose ``must_not_change`` assertions were scored against a captured pre-turn baseline
    is described as a measurement over them; an arm with no baseline at all is described as vacuous.

    Args:
        arm: The arm to describe.

    Returns:
        A JSON-safe mapping in a fixed key order carrying the two counts and the one-sentence
        statement rendered into ``matrix.md``.
    """
    declared, with_baseline = _must_not_change_counts(arm)
    if declared == 0:
        note = (
            "`bad_write_rate` basis: no task scored in this arm declared a `must_not_change` "
            f"assertion, so the figure carries no signal ({_SEMANTICS_SOURCE})."
        )
    elif with_baseline == 0:
        note = (
            f"`bad_write_rate` basis: none of the {declared} declared `must_not_change` "
            "assertion(s) in this arm had a captured pre-turn baseline, so the figure is vacuous — "
            f"entailed by its own input rather than measured ({_SEMANTICS_SOURCE})."
        )
    elif with_baseline == declared:
        note = (
            f"`bad_write_rate` basis: all {declared} declared `must_not_change` assertion(s) in "
            "this arm had a captured pre-turn baseline, so the figure is a measurement over them "
            f"({_SEMANTICS_SOURCE})."
        )
    else:
        note = (
            f"`bad_write_rate` basis: {with_baseline} of {declared} declared `must_not_change` "
            "assertion(s) in this arm had a captured pre-turn baseline; the other "
            f"{declared - with_baseline} are reported unscored and contribute nothing to the "
            f"figure ({_SEMANTICS_SOURCE})."
        )
    return {
        "model_id": arm.provenance.model_id,
        "must_not_change_assertions_declared": declared,
        "must_not_change_assertions_with_pre_turn_baseline": with_baseline,
        "note": note,
    }


def _positive_and_guard_counts(task_fields: dict[str, object]) -> tuple[int, int, int, int]:
    """Partition one task's *scored* assertion records into positive outcomes and negative guards.

    ``unscored`` records are excluded, as :func:`eval.scoring._fraction_passed` excludes them.
    Classification is by assertion name against
    :data:`eval.scoring.POSITIVE_ASSERTION_NAME_PREFIXES`.

    Args:
        task_fields: One task's :meth:`eval.scoring.TaskScore.to_report_fields` output.

    Returns:
        ``(positive, positive_satisfied, guard, guard_satisfied)`` counts for this task.
    """
    positive = positive_satisfied = guard = guard_satisfied = 0
    results = task_fields.get("assertion_results")
    if not isinstance(results, list):
        return 0, 0, 0, 0
    for result in results:
        if not isinstance(result, dict) or bool(result.get("unscored", False)):
            continue
        name = str(result.get("name", ""))
        satisfied = bool(result.get("passed", False))
        if name.startswith(POSITIVE_ASSERTION_NAME_PREFIXES):
            positive += 1
            positive_satisfied += int(satisfied)
        else:
            guard += 1
            guard_satisfied += int(satisfied)
    return positive, positive_satisfied, guard, guard_satisfied


def correctness_composition(arm: ArmResult) -> dict[str, object]:
    """Disclose what one arm's ``mean_correctness`` is composed of.

    Reports the positive and negative-guard assertion counts, the satisfaction rate of each, and
    the guard share of assertion mass as a mean over scored tasks (the same weighting as
    ``mean_correctness``). Reads records only; computes no score.

    Args:
        arm: The arm to describe. Only tasks the arm actually scored (``correctness`` non-null) are
            counted, matching :func:`eval.scoring.aggregate_scores`'s own denominator.

    Returns:
        A JSON-safe mapping in a fixed key order carrying the counts, the two rates, and the
        one-sentence statement rendered into ``matrix.md``. Rates are ``None`` when their
        denominator is zero.
    """
    positive = positive_satisfied = guard = guard_satisfied = 0
    guard_shares: list[float] = []
    for task_fields in arm.task_score_fields:
        if task_fields.get("correctness") is None:
            continue
        task_positive, task_positive_satisfied, task_guard, task_guard_satisfied = (
            _positive_and_guard_counts(task_fields)
        )
        positive += task_positive
        positive_satisfied += task_positive_satisfied
        guard += task_guard
        guard_satisfied += task_guard_satisfied
        scored = task_positive + task_guard
        if scored:
            guard_shares.append(task_guard / scored)
    positive_rate = positive_satisfied / positive if positive else None
    guard_rate = guard_satisfied / guard if guard else None
    guard_share = sum(guard_shares) / len(guard_shares) if guard_shares else None

    if guard_share is None:
        note = (
            "`mean_correctness` composition: no task scored in this arm carried a scored assertion, "
            f"so the figure has no composition to state ({_SEMANTICS_SOURCE})."
        )
    else:
        positive_text = (
            "no positive assertion was scored in this arm"
            if positive_rate is None
            else (
                f"{positive_satisfied} of {positive} positive assertions were satisfied "
                f"({positive_rate:.4f})"
            )
        )
        guard_text = (
            "no negative guard was scored in this arm"
            if guard_rate is None
            else f"the guards alone were satisfied at {guard_rate:.4f}"
        )
        scored_tasks = len(guard_shares)
        note = (
            f"`mean_correctness` composition: negative guards a do-nothing turn satisfies are "
            f"{guard_share:.4f} of the assertion mass this figure averages — the mean over the "
            f"{scored_tasks} scored {'task' if scored_tasks == 1 else 'tasks'} of each task's own "
            f"guard share, weighted exactly as `mean_correctness` itself is, rather than the ratio "
            f"pooled over assertions (pooled totals, for context and not the basis of the figure "
            f"just quoted: {guard} guard assertions and {positive} positive ones across those "
            f"tasks). So the figure is not a task-achievement rate. Taken alone, {positive_text}; "
            f"{guard_text} ({_SEMANTICS_SOURCE})."
        )
    return {
        "model_id": arm.provenance.model_id,
        "positive_assertions_scored": positive,
        "positive_assertions_satisfied": positive_satisfied,
        "positive_assertion_satisfaction_rate": positive_rate,
        "negative_guard_assertions_scored": guard,
        "negative_guard_assertions_satisfied": guard_satisfied,
        "negative_guard_satisfaction_rate": guard_rate,
        "negative_guard_share_of_assertion_mass": guard_share,
        "note": note,
    }


def retrieval_conservatism(arm: ArmResult) -> dict[str, object]:
    """Quantify what one arm's retrieval-narrowed tasks deduct from its ``mean_correctness``.

    The figure is an upper bound: the amount this arm's ``mean_correctness`` would rise if each
    narrowed task's (:data:`_RETRIEVAL_NARROWED_TASK_IDS`) unsatisfied assertions were removed
    entirely. Reads records only; computes no score.

    Args:
        arm: The arm to describe. Only tasks the arm actually scored are counted, matching
            :func:`eval.scoring.aggregate_scores`'s own denominator.

    Returns:
        A JSON-safe mapping in a fixed key order carrying one entry per narrowed task the arm
        scored, the summed upper bound, and the one-sentence statement rendered into ``matrix.md``.
    """
    scored: list[tuple[str, float]] = []
    for task_fields in arm.task_score_fields:
        correctness = task_fields.get("correctness")
        if not isinstance(correctness, (int, float)):
            continue
        scored.append((str(task_fields.get("task_id") or ""), float(correctness)))
    scored_count = len(scored)

    deductions: list[dict[str, object]] = []
    bounds: list[tuple[str, float, float]] = []
    for task_id, correctness in scored:
        if task_id not in _RETRIEVAL_NARROWED_TASK_IDS:
            continue
        bound = (1.0 - correctness) / scored_count
        bounds.append((task_id, correctness, bound))
        deductions.append(
            {
                "task_id": task_id,
                "correctness": correctness,
                "mean_correctness_deduction_upper_bound": bound,
            }
        )
    total = sum(bound for _, _, bound in bounds)

    if not bounds:
        note = (
            "Retrieval-narrowed task deduction: this arm scored none of the tasks narrowed to "
            f"retrieval, so no deduction applies to its `mean_correctness` "
            f"({_RETRIEVAL_CONSERVATISM_SOURCE})."
        )
    else:
        per_task = "; ".join(
            f"`{task_id}` scored {correctness:.6f}, at most {bound:.6f}"
            for task_id, correctness, bound in bounds
        )
        note = (
            "Retrieval-narrowed task deduction: the assertions this arm did not satisfy on the "
            f"tasks narrowed to retrieval account for at most {total:.6f} of its "
            f"`mean_correctness` ({per_task}), an upper bound over the {scored_count} scored "
            f"{'task' if scored_count == 1 else 'tasks'} derived from this arm's own per-task "
            f"records. The figure itself is unchanged by this disclosure "
            f"({_RETRIEVAL_CONSERVATISM_SOURCE})."
        )
    return {
        "model_id": arm.provenance.model_id,
        "narrowed_tasks_scored": len(deductions),
        "per_task": deductions,
        "mean_correctness_deduction_upper_bound": total,
        "note": note,
    }


def retrieval_conservatism_magnitude(matrix: MatrixResult) -> dict[str, object]:
    """Size the retrieval-narrowing deduction against the spread between the arms.

    States the largest deduction any single arm's narrowed tasks can account for, the largest
    absolute difference between any two arms' ``mean_correctness``, and their relation. No arm is
    identified in the comparison.

    Args:
        matrix: The matrix whose arms are compared.

    Returns:
        A JSON-safe mapping in a fixed key order carrying both magnitudes, their relation, and the
        statement rendered into ``matrix.md``. Magnitudes are ``None`` when the data does not
        support them (fewer than two arms; no arm reporting ``mean_correctness``).
    """
    means = [
        float(value)
        for arm in matrix.arms
        if isinstance(value := arm.aggregate_score_fields.get("mean_correctness"), (int, float))
    ]
    spread = max(means) - min(means) if len(means) >= 2 else None
    per_arm = [retrieval_conservatism(arm) for arm in matrix.arms]
    bounds = [
        float(cast(float, entry["mean_correctness_deduction_upper_bound"])) for entry in per_arm
    ]
    largest = max(bounds) if bounds else None

    exceeds: bool | None = None
    if spread is not None and largest is not None:
        exceeds = largest > spread

    if spread is None or largest is None:
        note = (
            "Retrieval-narrowing magnitude: this sweep carries too few arms reporting "
            "`mean_correctness` for a spread between arms to be stated, so the narrowing deduction "
            f"is published per arm only ({_RETRIEVAL_CONSERVATISM_SOURCE})."
        )
    else:
        relation = (
            "larger than"
            if exceeds
            else ("the same size as" if largest == spread else "smaller than")
        )
        note = (
            f"Retrieval-narrowing magnitude: the largest deduction any one arm's retrieval-narrowed "
            f"tasks account for is {largest:.6f} of that arm's `mean_correctness`, which is "
            f"{relation} the largest absolute difference between any two arms' `mean_correctness` "
            f"in this sweep ({spread:.6f}). Which arm sits on which side of that difference is "
            f"deliberately not computed or stated anywhere in this artifact; the "
            f"comparison is published because a disclosed measurement conservatism of this size "
            f"bounds how much the difference between arms can be read as a property of the models "
            f"({_RETRIEVAL_CONSERVATISM_SOURCE})."
        )
    return {
        "largest_single_arm_deduction_upper_bound": largest,
        "largest_absolute_inter_arm_mean_correctness_difference": spread,
        "deduction_exceeds_inter_arm_difference": exceeds,
        "note": note,
    }


def render_figure_semantics(matrix: MatrixResult) -> dict[str, object]:
    """Render the machine-readable boundary of each published figure.

    The per-arm entries are derived from each arm's own records by :func:`bad_write_rate_basis`,
    :func:`correctness_composition` and :func:`retrieval_conservatism`.

    Args:
        matrix: The matrix whose figures are described.

    Returns:
        A JSON-safe mapping in a fixed key order, suitable for embedding in ``matrix.json``.
    """
    return {
        "source": _SEMANTICS_SOURCE,
        "mean_correctness": _MEAN_CORRECTNESS_SEMANTICS,
        "bad_write_rate": _BAD_WRITE_RATE_SEMANTICS,
        "tool_call_correct_rate": _TOOL_CALL_CORRECT_RATE_SEMANTICS,
        "report_quality": _REPORT_QUALITY_SEMANTICS,
        "retrieval_narrowed_task_ids": list(_RETRIEVAL_NARROWED_TASK_IDS),
        "retrieval_narrowing_conservatism": _RETRIEVAL_CONSERVATISM_SEMANTICS,
        "retrieval_narrowing_magnitude": retrieval_conservatism_magnitude(matrix),
        "effort": _EFFORT_LABEL_SEMANTICS,
        "token_accounting": token_accounting_disclosure(),
        "per_arm_bad_write_rate_basis": [bad_write_rate_basis(arm) for arm in matrix.arms],
        "per_arm_correctness_composition": [correctness_composition(arm) for arm in matrix.arms],
        "per_arm_retrieval_conservatism": [retrieval_conservatism(arm) for arm in matrix.arms],
    }


#: Tier order and Markdown section headings of the tier comparison summary.
_TIER_COMPARISON_SECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("supervisor", "Supervisor"),
    ("subagent", "Subagent"),
    ("tool", "ToolCall"),
)

#: Delegation-outcome keys of an arm's ``tier_task_metrics`` block, in report order.
_DELEGATION_OUTCOME_KEYS: Final[tuple[str, ...]] = (
    "data",
    "no_match",
    "denied",
    "tool_failure",
    "none",
)

#: Key of a tier's completion over repeats, in ``tier_summary`` and the tier comparison entry.
REPEAT_COMPLETION_KEY: Final[str] = "completion_over_repeats"

#: Fields of a completion-over-repeats figure (:meth:`eval.metrics.RepeatCompletion.to_report_fields`).
_REPEAT_COMPLETION_FIELDS: Final[tuple[str, ...]] = ("completed", "applicable", "repeats", "rate")

#: Note: definitions of every summary row.
_TIER_COMPARISON_DEFINITIONS_NOTE: Final[str] = (
    "Model lists the tier's configured model and every model that spent tokens at the tier; "
    "Effort lists the efforts the tier's turns ran at, with unsupported for a model that takes no "
    "effort setting. Task completion rate is rate (met/applicable) over the arm's tasks applicable "
    "at the tier, where n is the applicable task count: at Supervisor a task's correctness is 1.0, "
    "at Subagent the observed delegation outcome equals the expected one, and at ToolCall an "
    "expected tool was called with schema-valid arguments. Task accuracy is the arm's "
    "mean_correctness at Supervisor and the completion rate at Subagent and ToolCall. Task "
    "completion over repeats is rate (completed/applicable) over the arm's tasks applicable at the "
    "tier in any repeat, where a task counts when its tier expectation was met in a strict majority "
    "of its applicable repeats, and N is the per-task repeat count. Cost per "
    "completed task divides the tier's total cost by its completed task count, so spend on tasks "
    "that did not complete is included; Cost per task, all tasks divides the same total by every "
    "task the arm ran. Latency per completed task sums the tier's measured call latencies within "
    "each completed task and takes the nearest-rank percentile over those tasks; a completed task "
    "with no measured call at the tier contributes no sample, and n counts the contributing "
    "tasks. Model invocations sums the tier's included invocations. Self-corrected retries counts "
    "the tier's tool calls whose tool_failure result was followed, later in the same task, by a "
    "call to the same tool from the same role that returned data or no_match; such a call does "
    "not set the task's delegation outcome. ToolCall makes one call per invocation, so that row is "
    "not applicable there."
)

#: Note: columns keep run order and are never compared.
_TIER_COMPARISON_COLUMNS_NOTE: Final[str] = (
    "Columns are the sweep's arms in run order, not sorted by any result, and no difference or "
    "ratio between columns is computed."
)

#: Note: effort labels differ in meaning across families.
_TIER_COMPARISON_EFFORT_NOTE: Final[str] = (
    "Effort labels are not equivalent across families: each family declares its own effort scale "
    "(config/model_capabilities.yaml, effort_scale_comparability); see `effort` under "
    '"Reading these figures".'
)

#: Note: input tokens are not like-for-like across families.
_TIER_COMPARISON_TOKENS_NOTE: Final[str] = (
    "Input tokens are each family's own reported count and are not like-for-like across "
    f"families (token_accounting_disclosure, {_SEMANTICS_SOURCE}); see `input_tokens` "
    'comparability under "Reading these figures".'
)

#: Conditional note: at least one column ran the stub provider.
_TIER_COMPARISON_STUB_NOTE: Final[str] = (
    "Stub-mode columns ran the deterministic stub provider: no live model was invoked, so latency "
    "is not measured there."
)

#: Conditional note: at least one column's cost is withheld.
_TIER_COMPARISON_WITHHELD_NOTE: Final[str] = (
    "Cost cells read withheld where the price table is marked provisional."
)


def _as_mapping(value: object) -> Mapping[str, object]:
    """Return ``value`` as a mapping, or an empty mapping when it is not one."""
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _is_number(value: object) -> bool:
    """Whether ``value`` is an int or float figure (not a bool)."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _tier_task_block(arm: ArmResult) -> Mapping[str, object] | None:
    """The arm's ``tier_task_metrics`` block, or ``None`` when the arm carries none."""
    return arm.tier_task_metrics_fields or None


def _effort_text(effort: str | None) -> str:
    """Render one effort value; ``None`` (the model takes no effort) reads ``unsupported``."""
    return "unsupported" if effort is None else effort


def _column_labels(matrix: MatrixResult) -> list[str]:
    """Return each arm's summary column header, in arm order."""
    families: list[str | None] = []
    for arm in matrix.arms:
        block = _tier_task_block(arm)
        family = block.get("family") if block is not None else None
        families.append(family if isinstance(family, str) else None)
    labels: list[str] = []
    for arm, family in zip(matrix.arms, families, strict=True):
        if family is None:
            labels.append(arm.provenance.model_id)
        elif families.count(family) == 1:
            labels.append(family)
        else:
            labels.append(
                f"{family} ({arm.provenance.model_id}, "
                f"effort {_effort_text(arm.provenance.effort)})"
            )
    return [
        f"{label} [arm {index}]" if labels.count(label) > 1 else label
        for index, label in enumerate(labels, start=1)
    ]


def _tier_comparison_column(arm: ArmResult, label: str) -> dict[str, object]:
    """Return one arm's summary column descriptor."""
    block = _tier_task_block(arm)
    family = block.get("family") if block is not None else None
    return {
        "label": label,
        "family": family if isinstance(family, str) else None,
        "model_id": arm.provenance.model_id,
        "mode": arm.provenance.mode,
        "effort": arm.provenance.effort,
        "task_count": len(arm.task_score_fields),
    }


def _tier_comparison_entry(arm: ArmResult, tier: str) -> dict[str, object]:
    """Return one arm's summary entry at ``tier``, in a fixed key order."""
    block = _tier_task_block(arm)
    by_tier = _as_mapping(_as_mapping(block.get("by_tier")).get(tier)) if block else None

    model_ids: set[str] = set()
    configured = by_tier.get("configured_model_id") if by_tier is not None else None
    if isinstance(configured, str) and configured:
        model_ids.add(configured)
    invocation_count = 0
    for row in arm.tier_role_model_fields:
        if row.get("tier") != tier:
            continue
        model_id = row.get("model_id")
        if isinstance(model_id, str) and model_id:
            model_ids.add(model_id)
        count = row.get("included_invocation_count")
        if isinstance(count, int) and not isinstance(count, bool):
            invocation_count += count

    efforts_value = by_tier.get("efforts") if by_tier is not None else None
    efforts = list(cast(list[object], efforts_value)) if isinstance(efforts_value, list) else None

    summary = _as_mapping(arm.tier_summary_fields.get(tier)) if arm.tier_summary_fields else None
    success = _as_mapping(summary.get("success")) if summary is not None else {}
    met = success.get("passed")
    applicable = success.get("applicable")
    rate = success.get("rate")
    over_repeats = summary.get(REPEAT_COMPLETION_KEY) if summary is not None else None
    completion_over_repeats = (
        {key: _as_mapping(over_repeats).get(key) for key in _REPEAT_COMPLETION_FIELDS}
        if isinstance(over_repeats, Mapping)
        else None
    )

    withheld = bool(arm.model_metrics_fields.get("cost_withheld_provisional"))
    cost_total = summary.get("cost_total") if summary is not None else None
    task_count = len(arm.task_score_fields)
    cost_per_completed_task: float | None = None
    cost_per_task: float | None = None
    if not withheld and _is_number(cost_total):
        total = float(cast(float, cost_total))
        if isinstance(met, int) and not isinstance(met, bool) and met >= 1:
            cost_per_completed_task = total / met
        if task_count >= 1:
            cost_per_task = total / task_count

    accuracy = arm.aggregate_score_fields.get("mean_correctness") if tier == "supervisor" else rate

    entry: dict[str, object] = {
        "model_ids": sorted(model_ids),
        "efforts": efforts,
        "completion": {"passed": met, "applicable": applicable, "rate": rate},
        REPEAT_COMPLETION_KEY: completion_over_repeats,
        "accuracy": accuracy,
        "cost_withheld_provisional": withheld,
        "cost_per_completed_task": cost_per_completed_task,
        "cost_per_task": cost_per_task,
        "completed_task_latency_ms_p50": (
            by_tier.get("completed_task_latency_ms_p50") if by_tier is not None else None
        ),
        "completed_task_latency_ms_p95": (
            by_tier.get("completed_task_latency_ms_p95") if by_tier is not None else None
        ),
        "latency_sample_count": (
            by_tier.get("latency_sample_count") if by_tier is not None else None
        ),
        "invocation_count": invocation_count,
        "input_tokens": summary.get("input_tokens") if summary is not None else None,
        "output_tokens": summary.get("output_tokens") if summary is not None else None,
        "retry_count": by_tier.get("retry_count") if by_tier is not None else None,
    }
    if tier == "supervisor":
        entry["budget_exhausted_task_count"] = sum(
            1 for task in arm.task_score_fields if task.get("budget_exhausted") is True
        )
    elif tier == "subagent":
        outcomes = block.get("delegation_outcomes") if block is not None else None
        entry["delegation_outcomes"] = (
            {key: _as_mapping(outcomes).get(key) for key in _DELEGATION_OUTCOME_KEYS}
            if isinstance(outcomes, Mapping)
            else None
        )
    else:
        entry["truncated_task_count"] = sum(
            1 for task in arm.task_score_fields if task.get("truncated") is True
        )
    return entry


def _tier_comparison_notes(matrix: MatrixResult, labels: Sequence[str]) -> list[str]:
    """Return the summary's notes: the fixed four, then the conditional ones in order."""
    notes = [
        _TIER_COMPARISON_DEFINITIONS_NOTE,
        _TIER_COMPARISON_COLUMNS_NOTE,
        _TIER_COMPARISON_EFFORT_NOTE,
        _TIER_COMPARISON_TOKENS_NOTE,
    ]
    if any(arm.provenance.mode == "stub" for arm in matrix.arms):
        notes.append(_TIER_COMPARISON_STUB_NOTE)
    if any(bool(arm.model_metrics_fields.get("cost_withheld_provisional")) for arm in matrix.arms):
        notes.append(_TIER_COMPARISON_WITHHELD_NOTE)
    errored_note = errored_items_cost_note(matrix.errored_item_count)
    if errored_note is not None:
        notes.append(errored_note)
    for arm, label in zip(matrix.arms, labels, strict=True):
        if arm.usage_lower_bound_task_ids:
            notes.append(
                f"Usage and cost for {label} are a lower bound; that arm's section names the "
                "affected task runs."
            )
    return notes


def tier_comparison(matrix: MatrixResult) -> dict[str, object]:
    """Build the per-tier comparison of the matrix's arms, one column per arm in arm order.

    Pure and deterministic. Figures are copied or derived from each arm's existing report blocks;
    a figure whose source block is absent is ``None``. No column is ranked or differenced.

    Args:
        matrix: The matrix to summarise.

    Returns:
        ``{"columns": [...], "tiers": {"supervisor"|"subagent"|"tool": [entry per column]},
        "notes": [...]}`` in a fixed key order.
    """
    labels = _column_labels(matrix)
    return {
        "columns": [
            _tier_comparison_column(arm, label)
            for arm, label in zip(matrix.arms, labels, strict=True)
        ],
        "tiers": {
            tier: [_tier_comparison_entry(arm, tier) for arm in matrix.arms]
            for tier, _ in _TIER_COMPARISON_SECTIONS
        },
        "notes": _tier_comparison_notes(matrix, labels),
    }


def render_json(matrix: MatrixResult) -> str:
    """Render ``matrix`` as pretty-printed, deterministic JSON text.

    Uses each dataclass's ``to_report_fields`` key order and ``sort_keys=False``.

    Args:
        matrix: The matrix to render.

    Returns:
        JSON text, terminated with a trailing newline, suitable for writing directly to
        ``matrix.json``.
    """
    return json.dumps(matrix.to_report_fields(), indent=2, sort_keys=False) + "\n"


def _format_scalar(value: object) -> str:
    """Render one report-field value as Markdown/CSV-safe text.

    ``None`` renders as ``"—"`` (an em dash), booleans as ``yes``/``no``, floats to six places.
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


#: The provenance fields every arm carries, in :meth:`RunProvenance.to_report_fields`'s key order.
#: An archive may omit the fields in :data:`_OPTIONAL_PROVENANCE_FIELDS`.
_PROVENANCE_FIELDS: tuple[str, ...] = (
    "mode",
    "timestamp",
    "model_id",
    "effort",
    "dataset_seed",
    "git_revision",
    "runtime_identifier",
    "price_table_version",
    "answer_style_sha256",
    "prompt_bundle_sha256",
)

#: Optional provenance fields; absent reads as ``None``.
_OPTIONAL_PROVENANCE_FIELDS: frozenset[str] = frozenset({"prompt_bundle_sha256"})

#: Fixed column order for each arm's model-metrics block in ``matrix.md``, cost fields last.
_MODEL_METRICS_FIELDS: tuple[str, ...] = (
    "model_id",
    "included_invocation_count",
    "input_tokens",
    "output_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "cache_min_checkpoint_tokens",
    "latency_measured",
    "latency_percentile_method",
    "task_latency_ms_p50",
    "task_latency_ms_p95",
    "cost_withheld_provisional",
    "cost_input",
    "cost_output",
    "cost_cache_write",
    "cost_cache_read",
    "cost_excluding_cache",
    "cost_cache_total",
    "cost_total",
    "price_table_version",
)

#: Tier order for the per-tier call-latency table in ``matrix.md`` and the flattened per-tier
#: columns in ``matrix.csv``. Matches :data:`eval.metrics._ALL_TIERS`.
_LATENCY_TIERS: tuple[str, ...] = ("supervisor", "subagent", "tool")

#: Fixed column order for each arm's aggregate-score block in ``matrix.md``.
_AGGREGATE_SCORE_FIELDS: tuple[str, ...] = (
    "scored_task_count",
    "unscored_task_count",
    "mean_correctness",
    "bad_write_rate",
    "tool_call_correct_rate",
)

#: Fixed column order for the per-task table in ``matrix.md`` and for every row in ``matrix.csv``.
_TASK_SCORE_FIELDS: tuple[str, ...] = (
    "task_id",
    "scoring_mode",
    "correctness",
    "unscored_reason",
    "bad_write",
    "tool_call_correct",
    "truncated",
    "budget_exhausted",
)


#: Per-tier ``tier_summary`` fields flattened into ``matrix.csv``, after the tier prefix. ``cost``
#: carries ``cost_total``; it is empty while cost is withheld.
_TIER_SUMMARY_CSV_SUFFIXES: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cost",
    "latency_p50_ms",
    "latency_p95_ms",
    "success_passed",
    "success_applicable",
    "success_rate",
    *(f"{REPEAT_COMPLETION_KEY}_{name}" for name in _REPEAT_COMPLETION_FIELDS),
)


def _tier_summary_csv_columns() -> tuple[str, ...]:
    """Return the per-arm ``tier_summary`` columns appended to every ``matrix.csv`` row."""
    return tuple(
        f"tier_{tier}_{suffix}" for tier in _LATENCY_TIERS for suffix in _TIER_SUMMARY_CSV_SUFFIXES
    )


def _tier_summary_entry(summary: Mapping[str, object], tier: str) -> dict[str, object]:
    """Flatten one tier's ``tier_summary`` entry under :data:`_TIER_SUMMARY_CSV_SUFFIXES` names."""
    entry = summary.get(tier)
    entry = entry if isinstance(entry, Mapping) else {}
    success = entry.get("success")
    success = success if isinstance(success, Mapping) else {}
    over_repeats = entry.get(REPEAT_COMPLETION_KEY)
    over_repeats = over_repeats if isinstance(over_repeats, Mapping) else {}
    return {
        "input_tokens": entry.get("input_tokens"),
        "output_tokens": entry.get("output_tokens"),
        "cost": entry.get("cost_total"),
        "latency_p50_ms": entry.get("latency_p50_ms"),
        "latency_p95_ms": entry.get("latency_p95_ms"),
        "success_passed": success.get("passed"),
        "success_applicable": success.get("applicable"),
        "success_rate": success.get("rate"),
        **{
            f"{REPEAT_COMPLETION_KEY}_{name}": over_repeats.get(name)
            for name in _REPEAT_COMPLETION_FIELDS
        },
    }


def _tier_summary_csv_row(summary: Mapping[str, object]) -> dict[str, str]:
    """Extract one arm's ``tier_summary`` as CSV cells; every cell is empty when it is absent."""
    row: dict[str, str] = dict.fromkeys(_tier_summary_csv_columns(), "")
    for tier in _LATENCY_TIERS if summary else ():
        for suffix, value in _tier_summary_entry(summary, tier).items():
            row[f"tier_{tier}_{suffix}"] = _format_scalar(value) if value is not None else ""
    return row


def _latency_csv_columns() -> tuple[str, ...]:
    """Return the per-arm latency columns appended to every ``matrix.csv`` row.

    The arm-level task percentiles plus a flattened ``p50``/``p95``/``count`` triple per tier in
    :data:`_LATENCY_TIERS`.
    """
    columns = [
        "latency_measured",
        "latency_percentile_method",
        "task_latency_ms_p50",
        "task_latency_ms_p95",
    ]
    for tier in _LATENCY_TIERS:
        columns.extend(
            (
                f"call_latency_ms_{tier}_p50",
                f"call_latency_ms_{tier}_p95",
                f"call_latency_ms_{tier}_count",
            )
        )
    return tuple(columns)


def _latency_csv_row(metrics_fields: Mapping[str, object]) -> dict[str, str]:
    """Extract one arm's latency values as CSV cell strings, keyed by :func:`_latency_csv_columns`.

    Every cell is empty when the arm carries no latency block, and a ``None`` figure renders
    empty.
    """
    row: dict[str, str] = dict.fromkeys(_latency_csv_columns(), "")
    if "latency_measured" not in metrics_fields:
        return row
    for name in (
        "latency_measured",
        "latency_percentile_method",
        "task_latency_ms_p50",
        "task_latency_ms_p95",
    ):
        value = metrics_fields.get(name)
        row[name] = _format_scalar(value) if value is not None else ""
    by_tier = metrics_fields.get("call_latency_ms_by_tier")
    if isinstance(by_tier, Mapping):
        for tier in _LATENCY_TIERS:
            tier_fields = by_tier.get(tier)
            tier_fields = tier_fields if isinstance(tier_fields, Mapping) else {}
            for suffix in ("p50", "p95", "count"):
                value = tier_fields.get(suffix)
                row[f"call_latency_ms_{tier}_{suffix}"] = (
                    _format_scalar(value) if value is not None else ""
                )
    return row


def _markdown_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    """Render a GitHub-flavoured Markdown table from a fixed header and row set."""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _render_exclusions_line(exclusions_fields: object) -> str:
    """Render an arm's ``exclusions`` block as one Markdown line naming every category."""
    if not isinstance(exclusions_fields, dict):
        raise TypeError("exclusions report fields must be a mapping")
    by_category = exclusions_fields.get("by_category", {})
    if not isinstance(by_category, dict):
        raise TypeError("exclusions 'by_category' must be a mapping")
    category_parts = ", ".join(f"{category}: {count}" for category, count in by_category.items())
    total = exclusions_fields.get("total_excluded", 0)
    return f"Excluded invocations: {total} total ({category_parts})"


def _render_call_latency_by_tier(metrics_fields: Mapping[str, object]) -> list[str]:
    """Render an arm's ``call_latency_ms_by_tier`` block as a Markdown sub-table.

    Returns an empty list when the arm carries no latency block. Every tier in
    :data:`_LATENCY_TIERS` is rendered even at a zero count.
    """
    by_tier = metrics_fields.get("call_latency_ms_by_tier")
    if not isinstance(by_tier, Mapping):
        return []
    rows: list[tuple[str, ...]] = []
    for tier in _LATENCY_TIERS:
        tier_fields = by_tier.get(tier)
        tier_fields = tier_fields if isinstance(tier_fields, Mapping) else {}
        rows.append(
            (
                tier,
                _format_scalar(tier_fields.get("p50")),
                _format_scalar(tier_fields.get("p95")),
                _format_scalar(tier_fields.get("count")),
            )
        )
    return [
        "Call latency by tier (ms):",
        "",
        _markdown_table(("tier", "p50", "p95", "count"), rows),
        "",
    ]


#: Fixed column order for the per-(tier, role, model) sub-table (`TierRoleModelMetrics`). A
#: withheld arm omits the cost keys, so they render as ``—``.
_TIER_ROLE_MODEL_FIELDS: tuple[str, ...] = (
    "tier",
    "role",
    "model_id",
    "included_invocation_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "call_latency_measured",
    "call_latency_ms_p50",
    "call_latency_ms_p95",
    "cost_withheld_provisional",
    "cost_input",
    "cost_output",
    "cost_cache_read",
    "cost_cache_write",
    "cost_excluding_cache",
    "cost_cache_total",
    "cost_total",
    "price_table_version",
)

#: Fixed leading columns for the per-tool sub-table (`ToolAggregate`); the four outcome-count
#: columns follow, one per :data:`_TOOL_OUTCOMES` member.
_TOOL_FIELDS: tuple[str, ...] = (
    "tool_name",
    "role",
    "count",
    "call_latency_ms_p50",
    "call_latency_ms_p95",
    "truncated_count",
)

#: The four outcome kinds of a `ToolAggregate`'s ``outcome_distribution``, flattened into
#: ``outcome_<kind>`` columns.
_TOOL_OUTCOMES: tuple[str, ...] = ("data", "no_match", "denied", "tool_failure")

#: Fixed column order for the per-task spread block (`TaskSpread`).
_TASK_SPREAD_FIELDS: tuple[str, ...] = (
    "task_id",
    "repeats",
    "correctness_min",
    "correctness_mean",
    "correctness_max",
    "success_fraction",
)

#: The spread columns added to each per-task ``matrix.csv`` row, joined by ``task_id``. The
#: per-(tier, role, model) and per-tool aggregations appear only in ``matrix.json`` and
#: ``matrix.md``.
_TASK_SPREAD_CSV_FIELDS: tuple[str, ...] = tuple(
    name for name in _TASK_SPREAD_FIELDS if name != "task_id"
)


def _render_tier_role_model(fields_list: tuple[dict[str, object], ...]) -> list[str]:
    """Render the per-(tier, role, model) aggregation as a Markdown sub-table; empty when absent."""
    if not fields_list:
        return []
    rows = [tuple(_format_scalar(f.get(k)) for k in _TIER_ROLE_MODEL_FIELDS) for f in fields_list]
    return ["Per tier / role / model:", "", _markdown_table(_TIER_ROLE_MODEL_FIELDS, rows), ""]


#: Column headers of the per-tier summary sub-table in ``matrix.md``.
_TIER_SUMMARY_MD_HEADERS: tuple[str, ...] = (
    "tier",
    "input_tokens",
    "output_tokens",
    "cost",
    "latency_p50_ms",
    "latency_p95_ms",
    "success (met/applicable)",
    "success_rate",
)


def _render_tier_summary(summary: Mapping[str, object]) -> list[str]:
    """Render an arm's ``tier_summary`` as a Markdown sub-table; empty when absent.

    A tier with no applicable turn renders its success as ``not applicable``.
    """
    if not summary:
        return []
    rows: list[tuple[str, ...]] = []
    for tier in _LATENCY_TIERS:
        entry = _tier_summary_entry(summary, tier)
        applicable = entry["success_applicable"]
        success_cell = f"{entry['success_passed']}/{applicable}" if applicable else "not applicable"
        rows.append(
            (
                tier,
                _format_scalar(entry["input_tokens"]),
                _format_scalar(entry["output_tokens"]),
                _format_scalar(entry["cost"]),
                _format_scalar(entry["latency_p50_ms"]),
                _format_scalar(entry["latency_p95_ms"]),
                success_cell,
                _format_scalar(entry["success_rate"]),
            )
        )
    return ["Per-tier summary:", "", _markdown_table(_TIER_SUMMARY_MD_HEADERS, rows), ""]


def _render_tool_metrics(fields_list: tuple[dict[str, object], ...]) -> list[str]:
    """Render the per-tool aggregation as a Markdown sub-table; empty when absent."""
    if not fields_list:
        return []
    headers = _TOOL_FIELDS + tuple(f"outcome_{outcome}" for outcome in _TOOL_OUTCOMES)
    rows: list[tuple[str, ...]] = []
    for f in fields_list:
        distribution = f.get("outcome_distribution")
        distribution = distribution if isinstance(distribution, Mapping) else {}
        rows.append(
            tuple(_format_scalar(f.get(k)) for k in _TOOL_FIELDS)
            + tuple(_format_scalar(distribution.get(outcome)) for outcome in _TOOL_OUTCOMES)
        )
    return ["Per tool:", "", _markdown_table(headers, rows), ""]


def _render_task_spread(fields_list: tuple[dict[str, object], ...]) -> list[str]:
    """Render the per-task correctness spread as a Markdown sub-table; empty when absent."""
    if not fields_list:
        return []
    rows = [tuple(_format_scalar(f.get(k)) for k in _TASK_SPREAD_FIELDS) for f in fields_list]
    return [
        "Per-task correctness spread across repeats:",
        "",
        _markdown_table(_TASK_SPREAD_FIELDS, rows),
        "",
    ]


def _render_arm_markdown(arm: ArmResult) -> str:
    """Render one arm's provenance, metrics, aggregate score, and per-task table as Markdown."""
    metrics_fields = arm.model_metrics_fields
    aggregate_fields = arm.aggregate_score_fields

    sections: list[str] = [f"## {arm.provenance.model_id} ({arm.provenance.mode}-mode run)", ""]

    sections.append("### Provenance")
    sections.append("")
    provenance_fields = arm.provenance.to_report_fields()
    sections.append(
        _markdown_table(
            ("field", "value"),
            [
                (name, _format_scalar(value))
                for name, value in provenance_fields.items()
                if name != "git_revision"
            ],
        )
    )
    sections.append("")

    sections.append("### Usage and Cost")
    sections.append("")
    sections.append(
        _markdown_table(
            ("field", "value"),
            [
                (name, _format_scalar(metrics_fields[name]))
                for name in _MODEL_METRICS_FIELDS
                if name in metrics_fields
            ],
        )
    )
    sections.append("")
    if bool(metrics_fields.get("cost_withheld_provisional")):
        sections.append("Cost figures are withheld: the price table is marked provisional.")
        sections.append("")
    lower_bound_note = usage_lower_bound_note(arm)
    if lower_bound_note is not None:
        sections.append(lower_bound_note)
        sections.append("")

    sections.extend(_render_call_latency_by_tier(metrics_fields))
    sections.extend(_render_tier_role_model(arm.tier_role_model_fields))
    sections.extend(_render_tier_summary(arm.tier_summary_fields))
    sections.extend(_render_tool_metrics(arm.tool_fields))

    sections.append(_render_exclusions_line(metrics_fields["exclusions"]))
    sections.append("")

    sections.append("### Aggregate Score")
    sections.append("")
    if arm.provenance.mode == "stub":
        sections.append(
            "This is a stub-mode measurement: no live model was invoked. The figures below "
            "reflect the deterministic stub provider, not a validated live-model comparison."
        )
        sections.append("")
    sections.append(
        _markdown_table(
            ("field", "value"),
            [(name, _format_scalar(aggregate_fields[name])) for name in _AGGREGATE_SCORE_FIELDS],
        )
    )
    sections.append("")
    sections.append(str(bad_write_rate_basis(arm)["note"]))
    sections.append("")
    sections.append(str(correctness_composition(arm)["note"]))
    sections.append("")
    sections.append(str(retrieval_conservatism(arm)["note"]))
    sections.append("")

    sections.append("### Per-Task Results")
    sections.append("")
    task_rows = [
        tuple(_format_scalar(task_fields.get(name)) for name in _TASK_SCORE_FIELDS)
        for task_fields in arm.task_score_fields
    ]
    sections.append(_markdown_table(_TASK_SCORE_FIELDS, task_rows))
    sections.append("")

    sections.extend(_render_task_spread(arm.task_spread_fields))

    return "\n".join(sections)


def _render_figure_semantics_markdown(matrix: MatrixResult) -> str:
    """Render the per-figure boundary section that heads ``matrix.md``.

    Arm-specific notes are rendered with each arm's aggregate figures instead.
    """
    disclosure = token_accounting_disclosure()
    per_family = cast(list[dict[str, object]], disclosure["per_family"])
    return "\n".join(
        [
            "## Reading these figures",
            "",
            "Each figure below names one narrow measured signal. Its boundary is stated here so "
            "the artifact cannot be read past what it supports. Mechanism and evidence: "
            f"{_SEMANTICS_SOURCE}.",
            "",
            f"- `mean_correctness` — {_MEAN_CORRECTNESS_SEMANTICS}",
            f"- `bad_write_rate` — {_BAD_WRITE_RATE_SEMANTICS}",
            f"- `tool_call_correct_rate` — {_TOOL_CALL_CORRECT_RATE_SEMANTICS}",
            f"- report quality — {_REPORT_QUALITY_SEMANTICS} The tasks narrowed to retrieval are: "
            f"{', '.join('`' + task_id + '`' for task_id in _RETRIEVAL_NARROWED_TASK_IDS)}.",
            f"- retrieval narrowing — {_RETRIEVAL_CONSERVATISM_SEMANTICS}",
            f"- {str(retrieval_conservatism_magnitude(matrix)['note'])}",
            f"- `effort` (provenance) — {_EFFORT_LABEL_SEMANTICS}",
            f"- `input_tokens` comparability — {_TOKEN_ACCOUNTING_SEMANTICS} "
            f"({_TOKEN_ACCOUNTING_SOURCE})",
            *[
                f"  - `{entry['family']}`: cached reads read from "
                f"`{entry['cache_read_usage_field']}`; accounting priced under: "
                f"`{entry['cache_read_accounting']}`; direction the evidence points: "
                f"`{entry['evidence_direction']}`. {entry['evidence']}"
                for entry in per_family
            ],
            "",
        ]
    )


def _completion_cell(completion: Mapping[str, object]) -> str:
    """Render a completion figure as ``rate (passed/applicable)``."""
    applicable = completion.get("applicable")
    if applicable is None:
        return "—"
    if applicable == 0:
        return "not applicable"
    return (
        f"{_format_scalar(completion.get('rate'))} "
        f"({_format_scalar(completion.get('passed'))}/{applicable})"
    )


def _repeat_completion_cell(completion: object) -> str:
    """Render completion over repeats as ``rate (completed/applicable, majority of N)``."""
    if not isinstance(completion, Mapping):
        return "n/a"
    applicable = completion.get("applicable")
    if applicable is None:
        return "n/a"
    if applicable == 0:
        return "not applicable"
    return (
        f"{_format_scalar(completion.get('rate'))} "
        f"({_format_scalar(completion.get('completed'))}/{applicable}, "
        f"majority of {_format_scalar(completion.get('repeats'))})"
    )


def _latency_cell(entry: Mapping[str, object], key: str, *, stub: bool) -> str:
    """Render a per-completed-task latency figure with its sample count."""
    if stub:
        return "not measured"
    count = entry.get("latency_sample_count")
    if count is None:
        return "—"
    return f"{_format_scalar(entry.get(key))} (n={count})"


def _tier_comparison_rows(
    tier: str, entries: Sequence[Mapping[str, object]], columns: Sequence[Mapping[str, object]]
) -> list[tuple[str, ...]]:
    """Return one tier section's Markdown rows, in a fixed order with fixed labels."""
    stubs = [column.get("mode") == "stub" for column in columns]

    def _row(label: str, cells: list[str]) -> tuple[str, ...]:
        return (label, *cells)

    def _model(entry: Mapping[str, object]) -> str:
        model_ids = cast(list[str], entry.get("model_ids") or [])
        return ", ".join(model_ids) or "—"

    def _effort(entry: Mapping[str, object]) -> str:
        efforts = entry.get("efforts")
        if not isinstance(efforts, list) or not efforts:
            return "—"
        return ", ".join(_effort_text(cast(str | None, effort)) for effort in efforts)

    def _accuracy(entry: Mapping[str, object]) -> str:
        if tier != "supervisor" and _as_mapping(entry.get("completion")).get("applicable") == 0:
            return "not applicable"
        return _format_scalar(entry.get("accuracy"))

    def _cost(entry: Mapping[str, object], key: str) -> str:
        if entry.get("cost_withheld_provisional") is True:
            return "withheld"
        return _format_scalar(entry.get(key))

    def _retries(entry: Mapping[str, object]) -> str:
        if tier == "tool":
            return "not applicable"
        return _format_scalar(entry.get("retry_count"))

    rows = [
        _row("Model", [_model(e) for e in entries]),
        _row("Effort", [_effort(e) for e in entries]),
        _row(
            "Task completion rate",
            [_completion_cell(_as_mapping(e.get("completion"))) for e in entries],
        ),
        _row(
            "Task completion over repeats",
            [_repeat_completion_cell(e.get(REPEAT_COMPLETION_KEY)) for e in entries],
        ),
        _row("Task accuracy", [_accuracy(e) for e in entries]),
        _row("Cost per completed task", [_cost(e, "cost_per_completed_task") for e in entries]),
        _row(
            "P95 latency per completed task (ms)",
            [
                _latency_cell(e, "completed_task_latency_ms_p95", stub=stub)
                for e, stub in zip(entries, stubs, strict=True)
            ],
        ),
        _row(
            "P50 latency per completed task (ms)",
            [
                _latency_cell(e, "completed_task_latency_ms_p50", stub=stub)
                for e, stub in zip(entries, stubs, strict=True)
            ],
        ),
        _row("Model invocations", [_format_scalar(e.get("invocation_count")) for e in entries]),
        _row("Input tokens", [_format_scalar(e.get("input_tokens")) for e in entries]),
        _row("Output tokens", [_format_scalar(e.get("output_tokens")) for e in entries]),
        _row("Cost per task, all tasks", [_cost(e, "cost_per_task") for e in entries]),
        _row("Self-corrected retries", [_retries(e) for e in entries]),
    ]
    if tier == "supervisor":
        rows.append(
            _row(
                "Budget-exhausted tasks",
                [_format_scalar(e.get("budget_exhausted_task_count")) for e in entries],
            )
        )
    elif tier == "subagent":
        for kind in _DELEGATION_OUTCOME_KEYS:
            rows.append(
                _row(
                    f"Delegation outcome: {kind}",
                    [
                        _format_scalar(_as_mapping(e.get("delegation_outcomes")).get(kind))
                        for e in entries
                    ],
                )
            )
    else:
        rows.append(
            _row(
                "Truncated tasks",
                [_format_scalar(e.get("truncated_task_count")) for e in entries],
            )
        )
    return rows


def _render_tier_comparison_markdown(matrix: MatrixResult) -> str:
    """Render the ``## Tier comparison summary`` block that follows the ``matrix.md`` title."""
    summary = tier_comparison(matrix)
    columns = cast(list[dict[str, object]], summary["columns"])
    tiers = cast(dict[str, list[dict[str, object]]], summary["tiers"])
    headers = ("Metric", *(str(column["label"]) for column in columns))
    lines = [
        "## Tier comparison summary",
        "",
        "Per-tier figures for each arm, one column per arm in run order; the notes below the "
        "tables define each row.",
        "",
    ]
    for tier, heading in _TIER_COMPARISON_SECTIONS:
        lines.extend(
            [
                f"### {heading}",
                "",
                _markdown_table(headers, _tier_comparison_rows(tier, tiers[tier], columns)),
                "",
            ]
        )
    lines.extend(["Notes:", ""])
    lines.extend(f"- {note}" for note in cast(list[str], summary["notes"]))
    lines.append("")
    return "\n".join(lines)


def render_markdown(matrix: MatrixResult) -> str:
    """Render ``matrix`` as human-readable Markdown.

    The tier comparison summary follows the title, then a "Reading these figures" section, then
    one section per arm. Each
    arm's heading states its run mode, a stub-mode arm carries a stub disclosure above its
    aggregate table, and a withheld arm states that cost is withheld in place of ``cost_*`` fields.
    Each arm's per-arm notes are rendered below its aggregate table.

    Args:
        matrix: The matrix to render.

    Returns:
        Markdown text, terminated with a trailing newline, suitable for writing directly to
        ``matrix.md``.
    """
    lines = ["# Model Evaluation Matrix", ""]
    lines.append(_render_tier_comparison_markdown(matrix))
    lines.append(
        "Measured dimensions per model arm — direct measurements only; no reference threshold or "
        "conclusion is computed or shown here."
    )
    lines.append("")
    errored_note = errored_items_cost_note(matrix.errored_item_count)
    if errored_note is not None:
        lines.append(errored_note)
        lines.append("")
    lines.append(_render_figure_semantics_markdown(matrix))
    for arm in matrix.arms:
        lines.append(_render_arm_markdown(arm))
    return "\n".join(lines).rstrip("\n") + "\n"


#: Per-tier suffixes of the ``summary_<tier>_*`` columns in ``matrix.csv``.
_SUMMARY_CSV_TIER_SUFFIXES: Final[tuple[str, ...]] = (
    "model_ids",
    "efforts",
    "completion_passed",
    "completion_applicable",
    "completion_rate",
    *(f"{REPEAT_COMPLETION_KEY}_{name}" for name in _REPEAT_COMPLETION_FIELDS),
    "accuracy",
    "cost_per_completed_task",
    "cost_per_task",
    "completed_task_latency_ms_p50",
    "completed_task_latency_ms_p95",
    "latency_sample_count",
    "invocation_count",
    "input_tokens",
    "output_tokens",
)


def _summary_csv_columns() -> tuple[str, ...]:
    """Return the tier comparison columns appended after every other ``matrix.csv`` column."""
    return (
        "summary_column",
        *(
            f"summary_{tier}_{suffix}"
            for tier, _ in _TIER_COMPARISON_SECTIONS
            for suffix in _SUMMARY_CSV_TIER_SUFFIXES
        ),
        "summary_supervisor_budget_exhausted_task_count",
        *(f"summary_subagent_delegation_{kind}" for kind in _DELEGATION_OUTCOME_KEYS),
        "summary_tool_truncated_task_count",
        *(f"summary_{tier}_retry_count" for tier, _ in _TIER_COMPARISON_SECTIONS),
    )


def _csv_cell(value: object) -> str:
    """Render one summary value as a CSV cell; ``None`` is empty."""
    return "" if value is None else _format_scalar(value)


def _summary_csv_rows(matrix: MatrixResult) -> list[dict[str, str]]:
    """Return each arm's tier comparison CSV cells, in arm order."""
    summary = tier_comparison(matrix)
    columns = cast(list[dict[str, object]], summary["columns"])
    tiers = cast(dict[str, list[dict[str, object]]], summary["tiers"])
    rows: list[dict[str, str]] = []
    for index, column in enumerate(columns):
        row = {"summary_column": str(column["label"])}
        for tier, _ in _TIER_COMPARISON_SECTIONS:
            entry = tiers[tier][index]
            completion = _as_mapping(entry["completion"])
            efforts = entry["efforts"]
            values: dict[str, object] = dict(entry)
            values["model_ids"] = ", ".join(cast(list[str], entry["model_ids"])) or None
            values["efforts"] = (
                ", ".join(_effort_text(cast(str | None, e)) for e in efforts) or None
                if isinstance(efforts, list)
                else None
            )
            for key in ("passed", "applicable", "rate"):
                values[f"completion_{key}"] = completion.get(key)
            over_repeats = _as_mapping(entry[REPEAT_COMPLETION_KEY])
            for name in _REPEAT_COMPLETION_FIELDS:
                values[f"{REPEAT_COMPLETION_KEY}_{name}"] = over_repeats.get(name)
            for suffix in _SUMMARY_CSV_TIER_SUFFIXES:
                row[f"summary_{tier}_{suffix}"] = _csv_cell(values[suffix])
        supervisor = tiers["supervisor"][index]
        subagent = tiers["subagent"][index]
        tool = tiers["tool"][index]
        row["summary_supervisor_budget_exhausted_task_count"] = _csv_cell(
            supervisor["budget_exhausted_task_count"]
        )
        outcomes = subagent["delegation_outcomes"]
        for kind in _DELEGATION_OUTCOME_KEYS:
            row[f"summary_subagent_delegation_{kind}"] = _csv_cell(
                _as_mapping(outcomes).get(kind) if outcomes is not None else None
            )
        row["summary_tool_truncated_task_count"] = _csv_cell(tool["truncated_task_count"])
        for tier, _ in _TIER_COMPARISON_SECTIONS:
            row[f"summary_{tier}_retry_count"] = _csv_cell(tiers[tier][index]["retry_count"])
        rows.append(row)
    return rows


def render_csv(matrix: MatrixResult) -> str:
    """Render ``matrix`` as flat per-task CSV rows.

    One row per scored task per arm, carrying the arm's ``model_id``, run ``mode``, provenance
    timestamp, the task's own fields, its spread, ``usage_lower_bound`` (whether that task's run is
    one of the arm's :attr:`ArmResult.usage_lower_bound_task_ids`), the arm's latency columns,
    ``cost_excluding_cache``, its ``tier_*`` columns, and last its ``summary_*`` tier comparison
    columns. Carries no figure-boundary prose.

    Args:
        matrix: The matrix to render.

    Returns:
        CSV text (``\\r\\n`` line endings per :mod:`csv`'s default dialect), suitable for writing
        directly to ``matrix.csv``.
    """
    buffer = io.StringIO()
    fieldnames = (
        "model_id",
        "mode",
        "timestamp",
        *_TASK_SCORE_FIELDS,
        *_TASK_SPREAD_CSV_FIELDS,
        "usage_lower_bound",
        *_latency_csv_columns(),
        "cost_excluding_cache",
        *_tier_summary_csv_columns(),
        *_summary_csv_columns(),
    )
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for arm, summary_cells in zip(matrix.arms, _summary_csv_rows(matrix), strict=True):
        spread_by_task = {
            str(f.get("task_id")): f for f in arm.task_spread_fields if f.get("task_id") is not None
        }
        # Arm-level latency and cost cells repeat on every task row; cost is empty when withheld.
        latency_cells = _latency_csv_row(arm.model_metrics_fields)
        tier_summary_cells = _tier_summary_csv_row(arm.tier_summary_fields)
        cost_excluding_cache = arm.model_metrics_fields.get("cost_excluding_cache")
        cost_cell = _format_scalar(cost_excluding_cache) if cost_excluding_cache is not None else ""
        lower_bound_task_ids = set(arm.usage_lower_bound_task_ids)
        for task_fields in arm.task_score_fields:
            row: dict[str, object] = {
                "model_id": arm.provenance.model_id,
                "mode": arm.provenance.mode,
                "timestamp": arm.provenance.timestamp,
                "usage_lower_bound": _format_scalar(
                    str(task_fields.get("task_id")) in lower_bound_task_ids
                ),
                "cost_excluding_cache": cost_cell,
            }
            for name in _TASK_SCORE_FIELDS:
                value = task_fields.get(name)
                row[name] = _format_scalar(value) if value is not None else ""
            spread = spread_by_task.get(str(task_fields.get("task_id")), {})
            for name in _TASK_SPREAD_CSV_FIELDS:
                value = spread.get(name)
                row[name] = _format_scalar(value) if value is not None else ""
            row.update(latency_cells)
            row.update(tier_summary_cells)
            row.update(summary_cells)
            writer.writerow(row)
    return buffer.getvalue()


def write_matrix_artifacts(matrix: MatrixResult, output_dir: Path) -> dict[str, Path]:
    """Write ``matrix.json``, ``matrix.md``, and ``matrix.csv`` beneath ``output_dir``.

    Each file's content is exactly the corresponding ``render_*`` function's output, as UTF-8.

    Args:
        matrix: The matrix to write.
        output_dir: Directory the three files are written into. Created if absent.

    Returns:
        A mapping from artifact name (``"json"``, ``"md"``, ``"csv"``) to the path written.

    Raises:
        StubMatrixOverwriteRefused: Every arm ran in stub mode and ``output_dir`` resolves to
            :data:`TRACKED_RESULTS_DIR`; nothing is written.
    """
    if (
        all(arm.provenance.mode == "stub" for arm in matrix.arms)
        and output_dir.resolve() == TRACKED_RESULTS_DIR
    ):
        raise StubMatrixOverwriteRefused(
            f"refusing to write an all-stub matrix into {TRACKED_RESULTS_DIR}; the tracked "
            "matrix.md holds a live sweep, so write stub output elsewhere (results/stub)"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output_dir / "matrix.json",
        "md": output_dir / "matrix.md",
        "csv": output_dir / "matrix.csv",
    }
    paths["json"].write_text(render_json(matrix), encoding="utf-8")
    paths["md"].write_text(render_markdown(matrix), encoding="utf-8")
    with paths["csv"].open("w", encoding="utf-8", newline="") as csv_file:
        csv_file.write(render_csv(matrix))
    return paths


def archive_raw_run(arm: ArmResult, raw_dir: Path) -> Path:
    """Archive one arm's full result as raw JSON beneath ``results/raw/``.

    The file is named from the arm's ``model_id`` and provenance timestamp, and is read back by
    :func:`arm_from_report_fields`.

    Args:
        arm: The arm to archive.
        raw_dir: Directory the raw JSON file is written into (typically ``results/raw/``).
            Created if absent.

    Returns:
        The path written.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    safe_model_id = arm.provenance.model_id.replace("/", "_").replace(":", "_")
    safe_timestamp = arm.provenance.timestamp.replace(":", "-")
    path = raw_dir / f"{safe_model_id}_{safe_timestamp}.json"
    path.write_text(
        json.dumps(arm.to_report_fields(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


def arm_from_report_fields(fields: Mapping[str, object]) -> ArmResult:
    """Reconstruct an :class:`ArmResult` from its :meth:`ArmResult.to_report_fields` output.

    The inverse of :meth:`ArmResult.to_report_fields`, used to re-render a ``results/raw/``
    archive. Every measurement is carried through untouched.

    Args:
        fields: One arm's archived report fields.

    Returns:
        The reconstructed arm.

    Raises:
        ValueError: When a required block or provenance field is absent, or a block is not an
            object.
    """

    def _block(name: str) -> Mapping[str, object]:
        value = fields.get(name)
        if not isinstance(value, Mapping):
            raise ValueError(f"raw archive carries no {name!r} object, so it cannot be re-rendered")
        return cast(Mapping[str, object], value)

    provenance_fields = _block("provenance")
    missing = [
        name
        for name in _PROVENANCE_FIELDS
        if name not in provenance_fields and name not in _OPTIONAL_PROVENANCE_FIELDS
    ]
    if missing:
        raise ValueError(f"raw archive provenance is missing field(s) {missing}")
    effort = provenance_fields["effort"]
    temperature = provenance_fields.get("temperature")
    bundle_digest = provenance_fields.get("prompt_bundle_sha256")
    provenance = RunProvenance(
        mode=str(provenance_fields["mode"]),
        timestamp=str(provenance_fields["timestamp"]),
        model_id=str(provenance_fields["model_id"]),
        effort=None if effort is None else str(effort),
        dataset_seed=int(cast(int, provenance_fields["dataset_seed"])),
        git_revision=str(provenance_fields["git_revision"]),
        runtime_identifier=str(provenance_fields["runtime_identifier"]),
        price_table_version=str(provenance_fields["price_table_version"]),
        answer_style_sha256=str(provenance_fields["answer_style_sha256"]),
        prompt_bundle_sha256=None if bundle_digest is None else str(bundle_digest),
        legacy_temperature=None if temperature is None else float(cast(float, temperature)),
        records_legacy_temperature="temperature" in provenance_fields,
    )
    task_scores = fields.get("task_scores")
    if not isinstance(task_scores, list):
        raise ValueError("raw archive carries no 'task_scores' list, so it cannot be re-rendered")

    def _aggregation(name: str) -> tuple[dict[str, object], ...]:
        """Reconstruct one per-arm aggregation list: absent is an empty tuple, a non-list raises."""
        value = fields.get(name)
        if value is None:
            return ()
        if not isinstance(value, list):
            raise ValueError(f"raw archive's {name!r} is not a list, so it cannot be re-rendered")
        return tuple(dict(cast(Mapping[str, object], entry)) for entry in value)

    lower_bound_ids = fields.get(USAGE_LOWER_BOUND_TASK_IDS_KEY, [])
    if not isinstance(lower_bound_ids, list) or not all(
        isinstance(task_id, str) for task_id in lower_bound_ids
    ):
        raise ValueError(
            f"raw archive's {USAGE_LOWER_BOUND_TASK_IDS_KEY!r} is not a list of task ids, so it "
            "cannot be re-rendered"
        )

    tier_summary = fields.get("tier_summary")
    if tier_summary is None:
        tier_summary = {}
    if not isinstance(tier_summary, Mapping):
        raise ValueError(
            "raw archive's 'tier_summary' is not an object, so it cannot be re-rendered"
        )

    tier_task_metrics = fields.get(TIER_TASK_METRICS_KEY)
    if tier_task_metrics is None:
        tier_task_metrics = {}
    if not isinstance(tier_task_metrics, Mapping):
        raise ValueError(
            f"raw archive's {TIER_TASK_METRICS_KEY!r} is not an object, so it cannot be re-rendered"
        )

    return ArmResult(
        provenance=provenance,
        model_metrics_fields=dict(_block("model_metrics")),
        aggregate_score_fields=dict(_block("aggregate_score")),
        task_score_fields=tuple(dict(cast(Mapping[str, object], entry)) for entry in task_scores),
        tier_role_model_fields=_aggregation("tier_role_model_metrics"),
        tool_fields=_aggregation("tool_metrics"),
        task_spread_fields=_aggregation("task_spread"),
        tier_summary_fields=dict(cast(Mapping[str, object], tier_summary)),
        usage_lower_bound_task_ids=tuple(cast("list[str]", lower_bound_ids)),
        tier_task_metrics_fields=dict(cast(Mapping[str, object], tier_task_metrics)),
    )


def load_raw_archives(raw_dir: Path, *, git_revision: str | None = None) -> list[ArmResult]:
    """Load archived arms from ``raw_dir``, ordered by provenance timestamp.

    ``git_revision`` selects one sweep; without it every archive in the directory is loaded.

    Args:
        raw_dir: Directory holding the ``*.json`` archives :func:`archive_raw_run` wrote.
        git_revision: When given, keep only arms whose provenance ``git_revision`` starts with this
            prefix. A full hash and an abbreviated one therefore both select the same sweep.

    Returns:
        The selected arms, ordered by provenance timestamp ascending.

    Raises:
        ValueError: When ``raw_dir`` holds no archive matching the selection, or when the selection
            holds more than one arm for a single model id.
    """
    arms: list[ArmResult] = []
    for path in sorted(raw_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{path} does not carry a JSON object")
        arm = arm_from_report_fields(cast(Mapping[str, object], payload))
        if git_revision and not arm.provenance.git_revision.startswith(git_revision):
            continue
        arms.append(arm)
    if not arms:
        raise ValueError(
            f"{raw_dir} holds no raw archive"
            + (f" at git_revision {git_revision!r}" if git_revision else "")
        )
    arms.sort(key=lambda arm: arm.provenance.timestamp)
    seen: dict[str, str] = {}
    for arm in arms:
        previous = seen.get(arm.provenance.model_id)
        if previous is not None:
            raise ValueError(
                f"{raw_dir} holds two archives for model {arm.provenance.model_id!r} in this "
                f"selection ({previous} and {arm.provenance.timestamp}) — narrow it with "
                f"--git-revision so the re-render is unambiguous"
            )
        seen[arm.provenance.model_id] = arm.provenance.timestamp
    return arms


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the raw-archive re-render CLI."""
    parser = argparse.ArgumentParser(
        prog="python3 -m eval.matrix",
        description=(
            "Re-render matrix.json/matrix.md/matrix.csv from archived results/raw/ records, "
            "without re-running the sweep. Renders only; makes no AWS call and no model call."
        ),
    )
    parser.add_argument("--raw-dir", type=Path, default=Path("results/raw"))
    parser.add_argument("--output-dir", type=Path, default=Path("results"))
    parser.add_argument(
        "--git-revision",
        default=None,
        help="Select only archives whose provenance git_revision starts with this prefix.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Re-render the published artifacts from archived raw records.

    Args:
        argv: Command-line arguments, defaulting to :data:`sys.argv`'s tail.

    Returns:
        Process exit code: 0 on success, 2 when the selection could not be resolved.
    """
    args = _build_arg_parser().parse_args(argv)
    try:
        arms = load_raw_archives(args.raw_dir, git_revision=args.git_revision)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    paths = write_matrix_artifacts(build_matrix_result(arms), args.output_dir)
    for arm in arms:
        print(
            f"re-rendered {arm.provenance.model_id} "
            f"({arm.provenance.mode}, {arm.provenance.timestamp}, "
            f"{arm.provenance.git_revision[:8]})"
        )
    for name in ("json", "md", "csv"):
        print(f"wrote {paths[name]}")
    return 0


def build_matrix_result(
    arms: list[ArmResult],
    *,
    errored_item_count: int = 0,
) -> MatrixResult:
    """Construct a :class:`MatrixResult` from a caller-ordered list of arms.

    Order-preserving: the arm list is not resorted or deduplicated.

    Args:
        arms: Every arm in the sweep, in display order.
        errored_item_count: The run's ``tasks_failed``, for the ``matrix.md`` cost qualifier.

    Returns:
        A :class:`MatrixResult` wrapping ``arms`` unchanged.

    Raises:
        ValueError: The arms carry different ``prompt_bundle_sha256`` values (``None`` included),
            so they did not measure one instrument and cannot share a matrix.
    """
    digests = {arm.provenance.prompt_bundle_sha256 for arm in arms}
    if len(digests) > 1:
        per_arm = ", ".join(
            f"{arm.provenance.model_id}={arm.provenance.prompt_bundle_sha256}" for arm in arms
        )
        raise ValueError(
            f"the arms of one matrix ran under {len(digests)} different prompt bundles ({per_arm})"
        )
    return MatrixResult(arms=tuple(arms), errored_item_count=errored_item_count)


if __name__ == "__main__":  # pragma: no cover - exercised via main() in tests
    sys.exit(main())
