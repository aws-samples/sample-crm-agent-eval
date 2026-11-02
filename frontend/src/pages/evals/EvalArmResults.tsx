// SPDX-License-Identifier: MIT
/**
 * Renders one arm's per-task results as an accessible table plus the arm's aggregate figures, its
 * mode-provenance disclosure, and the measurement-integrity annotations that travel with the figures
 * they qualify: the effort label is annotated as family-specific, the per-model prompt-cache
 * checkpoint minimum is shown alongside cache-attributable cost reported apart from cost excluding
 * cache, and an unscored outcome renders the word `unscored`.
 *
 * Renders no pass/fail or ranking language. Arms are styled identically with no per-family colour;
 * identity is the model id alone.
 */
import type { JSX, ReactNode } from "react";

import { formatDurationMs, formatUsd } from "../../lib/format";
import { classifyRunMode, type RunMode } from "./runMode";
import type {
  EvalArm,
  EvalCallLatencyByTier,
  EvalOutcomeKind,
  EvalTaskSpread,
  EvalTierLatency,
  EvalTierRoleModelMetric,
  EvalTierSummary,
  EvalToolMetric,
} from "./types";
import { erroredItemsCostNote, usageLowerBoundNote } from "./types";

const WITHHELD_PROVISIONAL = "withheld — price table is provisional";
/** A value the wire did not carry. Distinct from a measured `0` and from a documented `null`. */
const NOT_REPORTED = "not reported";
/** How the three-tier measurement blocks render a `null` figure: an em dash, never `0`. */
const EM_DASH = "—";

const FIELD = "rounded-control border border-hairline px-5 py-4";
const FIELD_MEASURED = `${FIELD} border-border-subtle bg-surface-inset`;
/* A field absent by policy carries a dashed border as well as its own surface, so the distinction
   does not rely on colour alone. */
const FIELD_ABSENT = `${FIELD} border-dashed border-empty-border bg-empty-surface`;
const TERM = "font-sans text-2xs font-semibold leading-snug text-text-secondary";
const VALUE_FIGURE = "mt-2 block font-numeric text-sm leading-snug text-text-primary";
const VALUE_PROSE = "mt-2 block max-w-prose font-sans text-2xs leading-normal text-empty-text";
const NOTE = "mt-3 max-w-prose font-sans text-2xs leading-normal text-text-muted";
const COLUMN_HEADER =
  "h-row-dense px-cell-x text-left align-middle font-sans text-2xs font-semibold text-text-secondary";

/**
 * The provenance disclosure per mode: `stub`, `live` (a real, billed call), or `unrecognised` (the
 * provenance establishes neither).
 */
const MODE_DISCLOSURE: Readonly<
  Record<RunMode, { readonly heading: string; readonly detail: string; readonly className: string }>
> = {
  stub: {
    heading: "Stub-mode measurements",
    detail: " — this arm made no real model call. Figures below are not a validated comparison.",
    className:
      "border-b-caution-border border-l-caution-emphasis bg-caution-surface text-caution-text",
  },
  live: {
    heading: "Live-mode measurements",
    detail: " — this arm invoked a real Bedrock model and reflects billed usage.",
    className: "border-b-info-border border-l-info-emphasis bg-info-surface text-info-text",
  },
  unrecognised: {
    heading: "Unrecognised run mode",
    detail:
      " — this arm's provenance does not establish whether a real model was called, so these" +
      " figures state nothing about what the run consumed.",
    className:
      "border-b-empty-border border-l-border-strong bg-empty-surface text-empty-text border-dashed",
  },
};

/** A correctness score. `null` is the documented unscored outcome; anything non-numeric is absent. */
function formatCorrectness(value: number | null | undefined): string {
  if (value === null) return "unscored";
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(4) : NOT_REPORTED;
}

/** A whole count. Renders absence as a word, so a missing key is never a blank numeric slot. */
function formatCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : NOT_REPORTED;
}

/** A rate. A value the wire did not carry renders as absent, never as `0`. */
function formatRate(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(4) : NOT_REPORTED;
}

/** A token count. Anything that is not a finite number renders as absent, never as `0`. */
function formatTokens(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `${value} tokens` : NOT_REPORTED;
}

/**
 * A model-spend figure and whether it is a measurement.
 *
 * A finite value is a measured cost (rendered with sub-dollar precision); anything else renders as
 * `not reported`, never a zero currency figure. Only reached when the arm's
 * `cost_withheld_provisional` is explicitly `false`.
 */
function formatCostField(value: number | null | undefined): {
  readonly text: string;
  readonly measured: boolean;
} {
  return typeof value === "number" && Number.isFinite(value)
    ? { text: formatUsd(value), measured: true }
    : { text: NOT_REPORTED, measured: false };
}

/** A latency percentile in ms. Absent (a tier with no calls carries `null`) renders as the word,
 * never `0 ms`. */
function formatLatency(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? formatDurationMs(value) : NOT_REPORTED;
}

/** A latency percentile for the three-tier blocks: `—` for a `null`/absent figure, never `0 ms`,
 * matching the matrix renderer's convention. */
function dashLatency(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? formatDurationMs(value) : EM_DASH;
}

/** A whole count for the three-tier blocks; `—` for a non-finite value, never a blank cell. */
function dashCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : EM_DASH;
}

/** A 0..1 figure (correctness spread, success fraction) at four decimals; `—` when non-finite. */
function dashFixed(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(4) : EM_DASH;
}

/** A backend cost_* figure rendered verbatim (no arithmetic), or `—` for a null/absent value. Only
 * reached for a row whose `cost_withheld_provisional` is explicitly `false`. */
function dashCost(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? formatUsd(value) : EM_DASH;
}

/** The four subagent-outcome kinds and their column labels, in `eval.metrics._OUTCOME_KINDS`
 * order — every kind is always present in a tool's distribution, even at zero. */
const OUTCOME_KINDS: readonly (readonly [EvalOutcomeKind, string])[] = [
  ["data", "Data"],
  ["no_match", "No match"],
  ["denied", "Denied"],
  ["tool_failure", "Tool failure"],
];

/** The three call tiers, in the order the arm's per-tier latency table renders them. */
const CALL_TIERS: readonly (readonly [keyof EvalCallLatencyByTier, string])[] = [
  ["supervisor", "Supervisor"],
  ["subagent", "Subagent"],
  ["tool", "Tool"],
];

/**
 * The prompt-cache checkpoint minimum, as three distinct states: a number, `null` (the model
 * documents no threshold), or an absent key (the writer never rendered the field).
 */
function formatCacheThreshold(value: number | null | undefined): {
  readonly text: string;
  readonly measured: boolean;
} {
  if (value === null) return { text: "no documented threshold", measured: true };
  if (typeof value === "number" && Number.isFinite(value)) {
    return { text: `${value} tokens`, measured: true };
  }
  return { text: NOT_REPORTED, measured: false };
}

interface MetricProps {
  readonly label: string;
  readonly value: ReactNode;
  /** An annotation rendered inside the same `<dd>` as the value it qualifies. */
  readonly note?: ReactNode;
  /** `false` for a value that is prose (a withholding disclosure) rather than a measurement. */
  readonly measured?: boolean;
  readonly span?: boolean;
}

/**
 * One `<dt>`/`<dd>` pair in the arm's measurement list.
 *
 * The `<dd>` holds the value in its own `<span>` and the annotation in a `<p>` beneath it; a `<dl>`
 * may only directly contain `dt`, `dd`, and `div`.
 */
function Metric({ label, value, note, measured = true, span = false }: MetricProps): JSX.Element {
  return (
    <div className={`${measured ? FIELD_MEASURED : FIELD_ABSENT}${span ? " md:col-span-2" : ""}`}>
      <dt className={TERM}>{label}</dt>
      <dd>
        <span className={measured ? VALUE_FIGURE : VALUE_PROSE}>{value}</span>
        {note === undefined ? null : <p className={NOTE}>{note}</p>}
      </dd>
    </div>
  );
}

/**
 * Per-tier model-call latency as an accessible table: one row per call tier, P50/P95 in ms and the
 * number of calls each percentile summarises. Rendered only for a measured (non-stub) arm.
 *
 * A tier the arm never exercised (`count` 0, `null` percentiles) still gets a row. The table has no
 * sort affordance.
 */
function CallLatencyTable({
  byTier,
  modelId,
}: {
  readonly byTier: EvalCallLatencyByTier;
  readonly modelId: string;
}): JSX.Element {
  return (
    <div className="w-full overflow-x-auto border-t-hairline border-border-subtle">
      <table className="w-full min-w-max border-collapse font-sans text-sm text-text-primary">
        <caption className="px-6 py-4 text-left font-sans text-2xs leading-snug text-text-muted">
          Per-tier model-call latency for {modelId}
        </caption>
        <thead className="border-b-hairline border-border-default">
          <tr>
            <th scope="col" className={COLUMN_HEADER}>
              Call tier
            </th>
            <th scope="col" className={COLUMN_HEADER}>
              P50
            </th>
            <th scope="col" className={COLUMN_HEADER}>
              P95
            </th>
            <th scope="col" className={COLUMN_HEADER}>
              Calls
            </th>
          </tr>
        </thead>
        <tbody>
          {CALL_TIERS.map(([key, label]) => {
            const tier: EvalTierLatency | undefined = byTier[key];
            return (
              <tr key={key} className="border-b-hairline border-border-subtle">
                <th
                  scope="row"
                  className="px-cell-x py-cell-y text-left align-middle font-sans text-xs font-medium text-text-primary"
                >
                  {label}
                </th>
                <td className="px-cell-x py-cell-y align-middle font-numeric text-xs tabular-nums text-text-primary">
                  {formatLatency(tier?.p50)}
                </td>
                <td className="px-cell-x py-cell-y align-middle font-numeric text-xs tabular-nums text-text-primary">
                  {formatLatency(tier?.p95)}
                </td>
                <td className="px-cell-x py-cell-y align-middle font-numeric text-xs tabular-nums text-text-primary">
                  {formatCount(tier?.count)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Shared cell classes, matching the per-task and per-tier tables above. */
const ROW_HEADER =
  "px-cell-x py-cell-y text-left align-middle font-sans text-xs font-medium text-text-primary";
const ROW_HEADER_ID = `${ROW_HEADER} font-numeric`;
const CELL_NUM = "px-cell-x py-cell-y align-middle font-numeric text-xs tabular-nums text-text-primary";
const CELL_WORD = "px-cell-x py-cell-y align-middle font-sans text-xs text-text-primary";
const TABLE_WRAP = "w-full overflow-x-auto border-t-hairline border-border-subtle";
const TABLE = "w-full min-w-max border-collapse font-sans text-sm text-text-primary";
const CAPTION = "px-6 py-4 text-left font-sans text-2xs leading-snug text-text-muted";

/**
 * Per-`(tier, role, model_id)` usage, call latency, and cost, as an accessible table. One row per
 * group the arm exercised, in the backend's order. A group with no measured latency shows `—`.
 *
 * Cost is rendered verbatim from the backend's `cost_*` fields, only when the row's
 * `cost_withheld_provisional` is explicitly `false`; otherwise it shows `—`. Cache-attributable cost
 * is shown apart from cost excluding cache and the two are never summed.
 */
function TierRoleModelTable({
  rows,
  modelId,
}: {
  readonly rows: readonly EvalTierRoleModelMetric[];
  readonly modelId: string;
}): JSX.Element {
  return (
    <div className={TABLE_WRAP}>
      <table className={TABLE}>
        <caption className={CAPTION}>Per-tier / role / model usage for {modelId}</caption>
        <thead className="border-b-hairline border-border-default">
          <tr>
            {["Tier", "Role", "Model", "Invocations", "Input tokens", "Output tokens", "Cache read", "Cache write", "Latency P50", "Latency P95", "Cost excl. cache", "Cost, cache"].map(
              (label) => (
                <th key={label} scope="col" className={COLUMN_HEADER}>
                  {label}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            // Only an explicit `false` shows a figure; `true` and an absent flag both render `—`.
            const costShown = row.cost_withheld_provisional === false;
            return (
              <tr key={`${row.tier}#${row.role}#${row.model_id}`} className="border-b-hairline border-border-subtle">
                <th scope="row" className={ROW_HEADER}>
                  {row.tier}
                </th>
                <td className={CELL_WORD}>{row.role}</td>
                <td className={CELL_NUM}>{row.model_id}</td>
                <td className={CELL_NUM}>{dashCount(row.included_invocation_count)}</td>
                <td className={CELL_NUM}>{dashCount(row.input_tokens)}</td>
                <td className={CELL_NUM}>{dashCount(row.output_tokens)}</td>
                <td className={CELL_NUM}>{dashCount(row.cache_read_tokens)}</td>
                <td className={CELL_NUM}>{dashCount(row.cache_write_tokens)}</td>
                <td className={CELL_NUM}>{dashLatency(row.call_latency_ms_p50)}</td>
                <td className={CELL_NUM}>{dashLatency(row.call_latency_ms_p95)}</td>
                <td className={CELL_NUM}>{costShown ? dashCost(row.cost_excluding_cache) : EM_DASH}</td>
                <td className={CELL_NUM}>{costShown ? dashCost(row.cost_cache_total) : EM_DASH}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * The arm's per-tier summary: tokens, cost, call latency and per-tier success, one row per
 * tier in tier order, a tier with no applicable turn included.
 *
 * Cost renders only when the arm's `cost_withheld_provisional` is explicitly `false`; otherwise `—`.
 * A success rate with no applicable turn reads `not applicable`, never `0`.
 */
function TierSummaryTable({
  summary,
  modelId,
  costShown,
}: {
  readonly summary: EvalTierSummary;
  readonly modelId: string;
  readonly costShown: boolean;
}): JSX.Element {
  return (
    <div className={TABLE_WRAP}>
      <table className={TABLE}>
        <caption className={CAPTION}>Per-tier summary for {modelId}</caption>
        <thead className="border-b-hairline border-border-default">
          <tr>
            {["Tier", "Input tokens", "Output tokens", "Cost", "Latency P50", "Latency P95", "Tier success", "Success rate"].map(
              (label) => (
                <th key={label} scope="col" className={COLUMN_HEADER}>
                  {label}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody>
          {CALL_TIERS.map(([key, label]) => {
            const entry = summary[key];
            const success = entry?.success;
            const rate =
              success === undefined
                ? EM_DASH
                : success.applicable === 0
                  ? "not applicable"
                  : dashFixed(success.rate);
            return (
              <tr key={key} className="border-b-hairline border-border-subtle">
                <th scope="row" className={ROW_HEADER}>
                  {label}
                </th>
                <td className={CELL_NUM}>{dashCount(entry?.input_tokens)}</td>
                <td className={CELL_NUM}>{dashCount(entry?.output_tokens)}</td>
                <td className={CELL_NUM}>{costShown ? dashCost(entry?.cost_total) : EM_DASH}</td>
                <td className={CELL_NUM}>{dashLatency(entry?.latency_p50_ms)}</td>
                <td className={CELL_NUM}>{dashLatency(entry?.latency_p95_ms)}</td>
                <td className={CELL_NUM}>
                  {success === undefined
                    ? EM_DASH
                    : `${dashCount(success.passed)} of ${dashCount(success.applicable)}`}
                </td>
                <td className={CELL_NUM}>{rate}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Per-tool aggregation: call count, P50/P95 dispatch latency, truncation count, and the four-way
 * outcome distribution with every kind shown even at zero. Backend order preserved; `—` for an
 * unmeasured latency.
 */
function ToolMetricsTable({
  rows,
  modelId,
}: {
  readonly rows: readonly EvalToolMetric[];
  readonly modelId: string;
}): JSX.Element {
  return (
    <div className={TABLE_WRAP}>
      <table className={TABLE}>
        <caption className={CAPTION}>Per-tool calls and outcomes for {modelId}</caption>
        <thead className="border-b-hairline border-border-default">
          <tr>
            <th scope="col" className={COLUMN_HEADER}>
              Tool
            </th>
            {["Role", "Calls", "Latency P50", "Latency P95", "Truncated"].map((label) => (
              <th key={label} scope="col" className={COLUMN_HEADER}>
                {label}
              </th>
            ))}
            {OUTCOME_KINDS.map(([kind, label]) => (
              <th key={kind} scope="col" className={COLUMN_HEADER}>
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={`${row.tool_name}:${row.role}`}
              className="border-b-hairline border-border-subtle"
            >
              <th scope="row" className={ROW_HEADER_ID}>
                {row.tool_name}
              </th>
              <td className={CELL_WORD}>{row.role}</td>
              <td className={CELL_NUM}>{dashCount(row.count)}</td>
              <td className={CELL_NUM}>{dashLatency(row.call_latency_ms_p50)}</td>
              <td className={CELL_NUM}>{dashLatency(row.call_latency_ms_p95)}</td>
              <td className={CELL_NUM}>{dashCount(row.truncated_count)}</td>
              {OUTCOME_KINDS.map(([kind]) => (
                <td key={kind} className={CELL_NUM}>
                  {dashCount(row.outcome_distribution?.[kind])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Per-task correctness spread across repeats: min / mean / max and the success fraction, with no
 * threshold or ranking. Backend order preserved; `—` for a non-finite figure.
 */
function TaskSpreadTable({
  rows,
  modelId,
}: {
  readonly rows: readonly EvalTaskSpread[];
  readonly modelId: string;
}): JSX.Element {
  return (
    <div className={TABLE_WRAP}>
      <table className={TABLE}>
        <caption className={CAPTION}>Per-task correctness spread across repeats for {modelId}</caption>
        <thead className="border-b-hairline border-border-default">
          <tr>
            <th scope="col" className={COLUMN_HEADER}>
              Task
            </th>
            {["Repeats", "Min", "Mean", "Max", "Success fraction"].map((label) => (
              <th key={label} scope="col" className={COLUMN_HEADER}>
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.task_id} className="border-b-hairline border-border-subtle">
              <th scope="row" className={ROW_HEADER_ID}>
                {row.task_id}
              </th>
              <td className={CELL_NUM}>{dashCount(row.repeats)}</td>
              <td className={CELL_NUM}>{dashFixed(row.correctness_min)}</td>
              <td className={CELL_NUM}>{dashFixed(row.correctness_mean)}</td>
              <td className={CELL_NUM}>{dashFixed(row.correctness_max)}</td>
              <td className={CELL_NUM}>{dashFixed(row.success_fraction)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export interface EvalArmResultsProps {
  readonly arm: EvalArm;
  /** The run's `tasks_failed`; above zero, the arm states that its cost excludes those items. */
  readonly tasksFailed?: number;
}

export function EvalArmResults({ arm, tasksFailed = 0 }: EvalArmResultsProps): JSX.Element {
  const {
    provenance,
    model_metrics: modelMetrics,
    aggregate_score: aggregate,
    task_scores: taskScores,
    tier_role_model_metrics: tierRoleModel = [],
    tool_metrics: toolMetrics = [],
    task_spread: taskSpread = [],
    tier_summary: tierSummary,
  } = arm;
  const mode = classifyRunMode(provenance.mode);
  const provenanceDisclosure = MODE_DISCLOSURE[mode];
  // Unique per arm: two arms can share a model_id. The heading text is the bare model id.
  const armKey = `${provenance.model_id}#${provenance.effort ?? "no-effort"}`;
  const costWithheld = modelMetrics.cost_withheld_provisional === true;
  // Only an explicit `false` shows a figure; an absent flag shows none.
  const costShown = modelMetrics.cost_withheld_provisional === false;
  const latencyMeasured = modelMetrics.latency_measured === true;
  const percentileMethod = modelMetrics.latency_percentile_method;
  const cacheThreshold = formatCacheThreshold(modelMetrics.cache_min_checkpoint_tokens);
  const lowerBoundNote = usageLowerBoundNote(arm);
  const erroredItemsNote = erroredItemsCostNote(tasksFailed);

  return (
    <section
      aria-labelledby={`arm-heading-${armKey}`}
      className="rounded-surface border border-hairline border-border-subtle bg-surface-raised shadow-flush"
    >
      <h3
        id={`arm-heading-${armKey}`}
        className="border-b-hairline border-border-subtle px-6 py-5 font-numeric text-md font-semibold leading-snug text-text-primary"
      >
        {provenance.model_id}
      </h3>

      {/* Mode-provenance disclosure, rendered immediately under the arm heading. An unrecognised
          mode gets its own disclosure (`runMode.ts`). */}
      <p
        role="status"
        data-testid={`mode-label-${armKey}`}
        className={`border-b-hairline border-l-emphasis px-6 py-5 font-sans text-sm leading-snug ${provenanceDisclosure.className}`}
      >
        <strong className="font-semibold">{provenanceDisclosure.heading}</strong>
        {provenanceDisclosure.detail}
      </p>

      <dl className="grid items-start gap-5 p-6 md:grid-cols-2">
        {/* The effort annotation sits in the same field as the label. Each family's effort ladder
            lives in `config/model_capabilities.yaml`. */}
        <Metric
          label="Effort"
          value={provenance.effort ?? "not set"}
          span
          note={
            <>
              Family-specific label, not a shared scale. Each model family defines its own effort
              ladder, so this label does not denote the same setting on another family&rsquo;s arm.
              Pinning effort buys reproducibility, not cross-family comparability.
            </>
          }
        />
        {/* Legacy records only; an unpinned temperature renders as prose, not as a measurement. */}
        {"temperature" in provenance ? (
          <Metric
            label="Temperature"
            value={provenance.temperature ?? "not set"}
            measured={provenance.temperature !== null && provenance.temperature !== undefined}
            note={
              <>
                As recorded by this run. Temperature is no longer a tuning setting, so newer runs do
                not record it.
              </>
            }
          />
        ) : null}
        <Metric
          label="Included invocations"
          value={formatCount(modelMetrics.included_invocation_count)}
        />
        <Metric label="Mean correctness" value={formatCorrectness(aggregate.mean_correctness)} />
        <Metric
          label="Scored / unscored tasks"
          value={`${formatCount(aggregate.scored_task_count)} / ${formatCount(
            aggregate.unscored_task_count,
          )}`}
        />
        <Metric label="Bad-write rate" value={formatRate(aggregate.bad_write_rate)} />

        {/* Prompt-cache attribution. Checkpoint minimums differ by model, so an identical system
            prompt can cache on one arm and not another; the threshold and the cache token counts sit
            beside the two cost fields. */}
        <Metric
          label="Prompt-cache checkpoint minimum"
          value={cacheThreshold.text}
          measured={cacheThreshold.measured}
          note={
            <>
              This model&rsquo;s own minimum. An identical prompt can reach a checkpoint on one arm
              and not on another, so a cost difference can be cache-driven rather than model-driven.
            </>
          }
        />
        <Metric label="Cache read tokens" value={formatTokens(modelMetrics.cache_read_tokens)} />
        <Metric label="Cache write tokens" value={formatTokens(modelMetrics.cache_write_tokens)} />

        {/* Cost is rendered in three states, keyed on the backend's `cost_withheld_provisional`
            flag:
            - `true`  → withheld disclosure, no figure (price table provisional).
            - `false` → the two cost fields the backend emits, cache-attributable apart from
                        excluding-cache, never summed.
            - absent  → "not displayed": no figure is shown. */}
        {costWithheld ? (
          <>
            <Metric label="Cost, cache-attributable" value={WITHHELD_PROVISIONAL} measured={false} />
            <Metric
              label="Cost, excluding cache"
              value="withheld too, and reported apart — the two are never summed into one figure here"
              measured={false}
            />
          </>
        ) : costShown ? (
          <>
            {(() => {
              const cache = formatCostField(modelMetrics.cost_cache_total);
              const excluding = formatCostField(modelMetrics.cost_excluding_cache);
              return (
                <>
                  <Metric
                    label="Cost, cache-attributable"
                    value={cache.text}
                    measured={cache.measured}
                    note={
                      <>
                        Cache-attributable spend, shown apart from the cost excluding cache beside it.
                        The two are never summed into one figure — a cost gap that tracks cache
                        eligibility is not a gap between the models.
                      </>
                    }
                  />
                  <Metric
                    label="Cost, excluding cache"
                    value={excluding.text}
                    measured={excluding.measured}
                  />
                </>
              );
            })()}
          </>
        ) : (
          <Metric
            label="Cost"
            value="not displayed — this run record does not mark cost as withheld, and this page shows no figure it cannot confirm is publishable"
            measured={false}
            span
          />
        )}

        {/* Cost-completeness disclosures, worded as matrix.md words them. */}
        {lowerBoundNote !== undefined ? (
          <Metric label="Usage completeness" value={lowerBoundNote} measured={false} span />
        ) : null}
        {erroredItemsNote !== undefined ? (
          <Metric label="Items not measured" value={erroredItemsNote} measured={false} span />
        ) : null}

        {/* Latency. A stub run reports `latency_measured: false` and null figures, rendered as "not
            measured". Task P50/P95 sit in this list; the per-tier call breakdown is the table below
            the list. */}
        {latencyMeasured ? (
          <>
            <Metric
              label="Task latency (P50)"
              value={formatLatency(modelMetrics.task_latency_ms_p50)}
              note={
                percentileMethod === undefined ? undefined : (
                  <>Percentiles by {percentileMethod}.</>
                )
              }
            />
            <Metric
              label="Task latency (P95)"
              value={formatLatency(modelMetrics.task_latency_ms_p95)}
            />
          </>
        ) : (
          <Metric
            label="Latency"
            value="not measured — this run made no model call, so no wall-clock latency was recorded"
            measured={false}
            span
          />
        )}
      </dl>

      {latencyMeasured ? (
        <CallLatencyTable byTier={modelMetrics.call_latency_ms_by_tier ?? {}} modelId={provenance.model_id} />
      ) : null}

      {tierSummary !== undefined ? (
        <TierSummaryTable summary={tierSummary} modelId={provenance.model_id} costShown={costShown} />
      ) : null}

      {/* Three-tier measurement blocks. Each renders only when the sweep produced its rows, in the
          backend's order. */}
      {tierRoleModel.length > 0 ? (
        <TierRoleModelTable rows={tierRoleModel} modelId={provenance.model_id} />
      ) : null}
      {toolMetrics.length > 0 ? (
        <ToolMetricsTable rows={toolMetrics} modelId={provenance.model_id} />
      ) : null}
      {taskSpread.length > 0 ? (
        <TaskSpreadTable rows={taskSpread} modelId={provenance.model_id} />
      ) : null}

      <div className="w-full overflow-x-auto border-t-hairline border-border-subtle">
        {/* `min-w-max` keeps a task id on one line at a phone width; the wrapper scrolls instead. */}
        <table className="w-full min-w-max border-collapse font-sans text-sm text-text-primary">
          <caption className="px-6 py-4 text-left font-sans text-2xs leading-snug text-text-muted">
            Per-task results for {provenance.model_id}
          </caption>
          <thead className="border-b-hairline border-border-default">
            <tr>
              <th scope="col" className={COLUMN_HEADER}>
                Task
              </th>
              <th scope="col" className={COLUMN_HEADER}>
                Correctness
              </th>
              <th scope="col" className={COLUMN_HEADER}>
                Bad write
              </th>
              <th scope="col" className={COLUMN_HEADER}>
                Notes
              </th>
            </tr>
          </thead>
          <tbody>
            {taskScores.map((task) => (
              <tr key={task.task_id} className="border-b-hairline border-border-subtle">
                <th
                  scope="row"
                  className="px-cell-x py-cell-y text-left align-middle font-numeric text-xs font-medium text-text-primary"
                >
                  {task.task_id}
                </th>
                <td className="px-cell-x py-cell-y align-middle font-numeric text-xs tabular-nums text-text-primary">
                  {formatCorrectness(task.correctness)}
                </td>
                <td className="px-cell-x py-cell-y align-middle font-numeric text-xs text-text-primary">
                  {task.bad_write ? "yes" : "no"}
                </td>
                <td className="px-cell-x py-cell-y align-middle font-sans text-2xs text-text-muted">
                  {task.unscored_reason ?? ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
