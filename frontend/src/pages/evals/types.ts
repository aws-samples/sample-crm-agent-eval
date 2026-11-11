// SPDX-License-Identifier: MIT
/**
 * Types mirroring the eval harness's rendered output (`src/eval/matrix.py`, `src/eval/harness.py`,
 * and `src/eval/metrics.py` for `EvalModelMetrics`).
 *
 * These are the wire shapes `matrix.json` renders (`RunProvenance.to_report_fields()`,
 * `ArmResult.to_report_fields()`, `MatrixResult.to_report_fields()`). They are hand-written, not
 * generated: a field rename in `src/eval/matrix.py` must be mirrored here by hand.
 */

/** One task's rendered score (`eval.scoring.TaskScore.to_report_fields()`). Fields beyond the
 * ones this dashboard renders are passed through opaquely. */
export interface EvalTaskScore {
  readonly task_id: string;
  /** `null` when the task is unscored (prose-only, or no falsifiable assertion). */
  readonly correctness: number | null;
  readonly unscored_reason?: string | null;
  readonly bad_write: boolean;
  readonly [key: string]: unknown;
}

/** `eval.metrics.ModelMetrics.to_report_fields()`. Cost fields are absent (not zeroed) when the
 * backend withholds them; this dashboard displays only currency figures the backend rendered. */
export interface EvalModelMetrics {
  readonly model_id: string;
  readonly included_invocation_count: number;
  /** Invocations priced at the model's long-context rates; absent on a matrix written before the
   * count existed. */
  readonly long_context_invocation_count?: number;
  readonly input_tokens?: number;
  readonly output_tokens?: number;
  /** Tokens written to a new prompt-cache checkpoint; published even while cost is withheld. */
  readonly cache_write_tokens?: number;
  /** Tokens served from an existing prompt-cache checkpoint. */
  readonly cache_read_tokens?: number;
  /**
   * This model's prompt-cache minimum checkpoint size, or `null` when the model documents no
   * threshold. A compliant backend always emits this key; the renderer distinguishes `null` from
   * an absent key.
   */
  readonly cache_min_checkpoint_tokens?: number | null;
  readonly cost_withheld_provisional?: boolean;
  readonly cost_total?: number;
  /**
   * Cost attributable to prompt-cache tokens (`CostTotals.cache_total` = cache write + read), and
   * cost of everything else (`cost_excluding_cache` = input + output), emitted only when cost is not
   * withheld. The two are reported apart and never summed on the dashboard.
   */
  readonly cost_cache_total?: number;
  readonly cost_excluding_cache?: number;
  /**
   * True when this arm measured wall-clock latency around its model calls. `false`/absent for a
   * stub run, which makes no model call — the dashboard shows "not measured" and the figures are null.
   */
  readonly latency_measured?: boolean;
  /** The percentile method the figures were computed with, e.g. `"nearest-rank"`. */
  readonly latency_percentile_method?: string;
  /** Per-golden-task wall-clock latency percentiles, in milliseconds; null when not measured. */
  readonly task_latency_ms_p50?: number | null;
  readonly task_latency_ms_p95?: number | null;
  /** Per-model-call latency percentiles grouped by call tier; null when not measured. */
  readonly call_latency_ms_by_tier?: EvalCallLatencyByTier | null;
  readonly [key: string]: unknown;
}

/** One call tier's latency percentiles and the number of calls they summarise. */
export interface EvalTierLatency {
  readonly p50: number | null;
  readonly p95: number | null;
  readonly count: number;
}

/** Per-tier call latency, keyed by the three tiers the harness groups calls into. */
export interface EvalCallLatencyByTier {
  readonly supervisor?: EvalTierLatency;
  readonly subagent?: EvalTierLatency;
  readonly tool?: EvalTierLatency;
}

/** `eval.scoring.AggregateScore.to_report_fields()`. Carries no verdict or winner field. */
export interface EvalAggregateScore {
  readonly mean_correctness: number | null;
  readonly scored_task_count: number;
  readonly unscored_task_count: number;
  readonly bad_write_rate: number;
  readonly [key: string]: unknown;
}

/** `eval.matrix.RunProvenance.to_report_fields()`. */
export interface EvalRunProvenance {
  /**
   * The mode label this dashboard surfaces prominently. `"stub"` and `"live"` are the recognised
   * values; any other string is reported as unrecognised (`runMode.ts`).
   */
  readonly mode: string;
  readonly timestamp: string;
  readonly model_id: string;
  readonly effort: string | null;
  /**
   * Legacy: present (often `null`) only on records written while temperature was a tuning
   * setting; absent on newer runs. Read for history, never sent.
   */
  readonly temperature?: number | null;
  readonly dataset_seed: number;
  readonly git_revision: string;
  readonly runtime_identifier: string;
  readonly price_table_version: string;
}

/** The four subagent-outcome kinds a tool call resolves to (`agents.tier_attribution.OutcomeKind`),
 * in the order `eval.metrics._OUTCOME_KINDS` renders them. */
export type EvalOutcomeKind = "data" | "no_match" | "denied" | "tool_failure";

/**
 * One `(tier, role, model_id)` group's usage and call latency
 * (`eval.metrics.TierRoleModelMetrics.to_report_fields()`).
 *
 * Cost fields (`cost_*`) appear only when the backend does not withhold them; this dashboard renders
 * no currency from them, so they stay in the opaque passthrough. `call_latency_ms_*` are `null` for
 * a group that recorded no latency (a stub, or a tier with no calls) and render as `—`.
 */
export interface EvalTierRoleModelMetric {
  readonly tier: string;
  readonly role: string;
  readonly model_id: string;
  readonly included_invocation_count: number;
  /** Invocations priced at the model's long-context rates; absent on a matrix written before the
   * count existed. */
  readonly long_context_invocation_count?: number;
  readonly input_tokens: number;
  readonly output_tokens: number;
  readonly cache_read_tokens: number;
  readonly cache_write_tokens: number;
  readonly call_latency_measured: boolean;
  readonly latency_percentile_method?: string;
  readonly call_latency_ms_p50: number | null;
  readonly call_latency_ms_p95: number | null;
  /** `true` while this group's cost is withheld (price table provisional); the `cost_*` keys below
   * are then absent (not zero). The dashboard renders a cost figure only on an explicit `false`. */
  readonly cost_withheld_provisional?: boolean;
  /** Cache-attributable cost (`CostTotals.cache_total`), emitted only when cost is not withheld.
   * Reported apart from `cost_excluding_cache`, never summed with it on the dashboard. `null`
   * renders as `—`. */
  readonly cost_cache_total?: number | null;
  /** Cost excluding cache (input + output), emitted only when cost is not withheld. */
  readonly cost_excluding_cache?: number | null;
  readonly [key: string]: unknown;
}

/** Per-tool aggregation across the sweep (`eval.metrics.ToolAggregate.to_report_fields()`).
 * `call_latency_ms_*` are `null` when no call recorded a latency and render as `—`. */
export interface EvalToolMetric {
  readonly tool_name: string;
  readonly role: string;
  readonly count: number;
  readonly call_latency_ms_p50: number | null;
  readonly call_latency_ms_p95: number | null;
  readonly truncated_count: number;
  /** Count per outcome kind; every kind is present, even at zero. */
  readonly outcome_distribution: Readonly<Record<EvalOutcomeKind, number>>;
  readonly [key: string]: unknown;
}

/** Per-task correctness spread across a task's repeats
 * (`eval.metrics.TaskSpread.to_report_fields()`). `success_fraction` is the fraction of repeats
 * scoring a full `1.0`. */
export interface EvalTaskSpread {
  readonly task_id: string;
  readonly repeats: number;
  readonly correctness_min: number;
  readonly correctness_mean: number;
  readonly correctness_max: number;
  readonly success_fraction: number;
  readonly [key: string]: unknown;
}

/** A tier's per-tier success: turns where the tier passed, of the turns where it applied
 * (`eval.metrics.TierSuccess`). `rate` is `null` when no turn applied, never `0`. */
export interface EvalTierSuccess {
  readonly passed: number;
  readonly applicable: number;
  readonly rate: number | null;
}

/** One tier's roll-up (`eval.metrics.TierSummaryEntry.to_report_fields()`). `cost_total` is
 * `null` while cost is withheld; latency is `null` for a tier with no measured call. */
export interface EvalTierSummaryEntry {
  readonly input_tokens: number;
  readonly output_tokens: number;
  readonly cost_total: number | null;
  readonly latency_p50_ms: number | null;
  readonly latency_p95_ms: number | null;
  readonly success: EvalTierSuccess;
  /** Invocations priced at the model's long-context rates; absent on a matrix written before the
   * count existed. */
  readonly long_context_invocation_count?: number;
}

/** `eval.metrics.TierSummary.to_report_fields()`, keyed by tier. The writer emits every tier; a tier
 * the wire left out renders as `—`. */
export interface EvalTierSummary {
  readonly supervisor?: EvalTierSummaryEntry;
  readonly subagent?: EvalTierSummaryEntry;
  readonly tool?: EvalTierSummaryEntry;
}

/** `eval.matrix.ArmResult.to_report_fields()`. The three-tier measurement blocks are optional;
 * the dashboard renders each only when it carries rows. */
export interface EvalArm {
  readonly provenance: EvalRunProvenance;
  readonly model_metrics: EvalModelMetrics;
  readonly aggregate_score: EvalAggregateScore;
  readonly task_scores: readonly EvalTaskScore[];
  readonly tier_role_model_metrics?: readonly EvalTierRoleModelMetric[];
  readonly tool_metrics?: readonly EvalToolMetric[];
  readonly task_spread?: readonly EvalTaskSpread[];
  /** The per-tier summary; absent on a matrix written before it existed. */
  readonly tier_summary?: EvalTierSummary;
  /** The arm's cost over every tier's calls (`model_metrics` covers the supervisor tier only);
   * absent on a matrix written before it existed, `null` when not priced. */
  readonly arm_cost_total?: number | null;
  /** The arm's long-context invocations over every tier; absent on an older matrix. */
  readonly arm_long_context_invocation_count?: number;
  /**
   * `eval.matrix.USAGE_LOWER_BOUND_TASK_IDS_KEY`: the task runs, in run order, in which a model call
   * ended before reporting token usage. Absent when every run reported its usage, and on a matrix
   * written before the key existed.
   */
  readonly usage_lower_bound_task_ids?: readonly string[];
}

/** The arm's usage-completeness note, worded as `eval.matrix.usage_lower_bound_note`; `undefined`
 * when the arm names no lower-bound task run. */
export function usageLowerBoundNote(arm: EvalArm): string | undefined {
  const taskIds = arm.usage_lower_bound_task_ids ?? [];
  if (taskIds.length === 0) return undefined;
  return (
    `Usage and cost for this arm are a lower bound: in ${taskIds.length} task run(s) ` +
    `(${taskIds.join(", ")}) a model call ended before reporting token usage, so its tokens and ` +
    "cost are not counted in the figures above."
  );
}

/** The run-level cost qualifier for `tasksFailed` items that ended in an error, worded as
 * `eval.matrix.errored_items_cost_note`; `undefined` when there are none. */
export function erroredItemsCostNote(tasksFailed: number): string | undefined {
  if (tasksFailed <= 0) return undefined;
  return (
    `Cost excludes ${tasksFailed} item(s) that ended in an error: their model calls were billed, ` +
    "but their usage was not measured."
  );
}

/** `eval.matrix.MatrixResult.to_report_fields()` — the parsed shape of `matrix.json`. */
export interface EvalMatrix {
  readonly arms: readonly EvalArm[];
}

/** Paths (or URLs) to the four artifacts a completed run produces. `raw` is a list
 * because one raw JSON file is archived per arm under `results/raw/`. */
export interface EvalArtifactLinks {
  readonly matrixJson?: string;
  readonly matrixMd?: string;
  readonly matrixCsv?: string;
  readonly raw: readonly string[];
}

/** A run's repeats per arm, as its payload reports them; a payload without `repeats` reads as 1. */
export function runRepeats(run: { readonly repeats?: number }): number {
  return run.repeats ?? 1;
}

/** The tier sections of {@link EvalTierComparisonTable}, in the order the backend renders them. */
export const TIER_COMPARISON_TIERS = ["supervisor", "subagent", "tool"] as const;

export type EvalComparisonTier = (typeof TIER_COMPARISON_TIERS)[number];

/** One tier's table: `rows` are `[label, ...one cell per arm]`, every cell a display string. */
export interface EvalTierComparisonSection {
  readonly tier: EvalComparisonTier;
  readonly heading: string;
  readonly rows: readonly (readonly string[])[];
}

/**
 * The run payload's `tier_comparison_table` (`eval.matrix.tier_comparison_table`): the
 * `## Tier comparison summary` section of `matrix.md` as cell strings. `headers` is `Metric` then one
 * label per arm in run order. Rendered verbatim; the page formats none of it.
 */
export interface EvalTierComparisonTable {
  readonly headers: readonly string[];
  readonly sections: readonly EvalTierComparisonSection[];
  readonly notes: readonly string[];
}
