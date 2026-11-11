// SPDX-License-Identifier: MIT
/**
 * What the turn spent: token totals, the turn's cost, and that cost broken down per tier.
 *
 * Cost comes from the server (`cost_usd`, `cost_lines` on `usage`/`completed` frames); nothing
 * is priced here. {@link COST_WITHHELD_TEXT} replaces the figure only when the frame says
 * `cost_withheld === true`.
 *
 * Cache counts are shown only when the model reported them, so a reported `0` is distinct from
 * "not reported". Calls priced at long-context rates are counted only when there is at least one,
 * whether or not the cost is withheld.
 */

import type { JSX } from "react";

import { formatUsd } from "../../lib/format";
import type { AgentEventFrame, CostLine } from "../../types/generated/agentEvents";

export const USAGE_METER_TESTID = "usage-meter";

/** Shared class lists for the token figures, so the markup repeats ids and not styling. */
const FIGURE_CLASSES = "flex flex-col gap-1";
const FIGURE_LABEL_CLASSES = "text-2xs text-text-secondary";
const FIGURE_VALUE_CLASSES = "text-xs font-medium tabular-nums text-text-primary";
const CELL_CLASSES = "py-1 pr-4 text-2xs text-text-primary";
const NUMBER_CELL_CLASSES = "py-1 pr-4 text-right text-2xs tabular-nums text-text-primary";
const HEADER_CELL_CLASSES = "py-1 pr-4 text-left text-2xs font-medium text-text-secondary";

/** Shown in place of a cost figure. Names the reason, so it is not mistaken for a missing value. */
export const COST_WITHHELD_TEXT = "Cost is withheld while pricing is provisional.";

/** How each tier is named to the user. */
const TIER_LABEL: Readonly<Record<CostLine["tier"], string>> = {
  supervisor: "Supervisor",
  subagent: "Subagent",
  tool: "Tool",
};

/** One turn's token totals, accumulated across every `usage` event the turn emitted. */
export interface UsageTotals {
  readonly inputTokens: number;
  readonly outputTokens: number;
  readonly cacheReadTokens: number | null;
  readonly cacheWriteTokens: number | null;
}

/** One turn's cost as the server reported it. */
export interface TurnCost {
  /** The turn total, or `null` when withheld or not priced. */
  readonly totalUsd: number | null;
  readonly withheld: boolean;
  readonly lines: readonly CostLine[];
}

/**
 * Sum every `usage` event in `events` into one set of totals. Each event's counts are added. A
 * `null` cache count stays `null` unless some event reported a number.
 */
export function usageTotals(events: readonly AgentEventFrame[]): UsageTotals | undefined {
  const usage = events.filter((event) => event.kind === "usage");
  if (usage.length === 0) return undefined;

  let cacheRead: number | null = null;
  let cacheWrite: number | null = null;
  for (const event of usage) {
    if (event.cache_read_tokens !== null) cacheRead = (cacheRead ?? 0) + event.cache_read_tokens;
    if (event.cache_write_tokens !== null) cacheWrite = (cacheWrite ?? 0) + event.cache_write_tokens;
  }

  return {
    inputTokens: usage.reduce((total, event) => total + (event.input_tokens ?? 0), 0),
    outputTokens: usage.reduce((total, event) => total + (event.output_tokens ?? 0), 0),
    cacheReadTokens: cacheRead,
    cacheWriteTokens: cacheWrite,
  };
}

/**
 * The turn's cost, read from the last `usage` or `completed` frame that carries any cost field.
 * Each such frame holds the turn's figures so far, so the last one is the turn's; they are not
 * summed.
 *
 * @returns `undefined` when no frame carried cost information.
 */
export function turnCost(events: readonly AgentEventFrame[]): TurnCost | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event === undefined || (event.kind !== "usage" && event.kind !== "completed")) continue;
    if (event.cost_lines === null && event.cost_usd === null && event.cost_withheld === null) {
      continue;
    }
    const withheld = event.cost_withheld === true;
    return {
      totalUsd: withheld ? null : event.cost_usd,
      withheld,
      lines: event.cost_lines ?? [],
    };
  }
  return undefined;
}

/** How many of the turn's calls were priced at long-context rates, summed over its cost lines. */
function longContextCalls(lines: readonly CostLine[]): number {
  return lines.reduce(
    (total, line) => (line.long_context_calls > 0 ? total + line.long_context_calls : total),
    0,
  );
}

function longContextNote(count: number): string {
  return count === 1 ? "1 call at long-context rates" : `${count} calls at long-context rates`;
}

function CostBreakdown({ lines, withheld }: { lines: readonly CostLine[]; withheld: boolean }) {
  return (
    <details data-testid="usage-cost-breakdown" className="flex flex-col gap-2">
      <summary className="cursor-pointer text-2xs text-text-secondary">Cost by tier</summary>
      <table className="mt-2 w-full border-collapse">
        <thead>
          <tr>
            <th scope="col" className={HEADER_CELL_CLASSES}>Tier</th>
            <th scope="col" className={HEADER_CELL_CLASSES}>Role</th>
            <th scope="col" className={HEADER_CELL_CLASSES}>Model</th>
            <th scope="col" className={`${HEADER_CELL_CLASSES} text-right`}>Tokens in</th>
            <th scope="col" className={`${HEADER_CELL_CLASSES} text-right`}>Tokens out</th>
            {withheld ? null : (
              <th scope="col" className={`${HEADER_CELL_CLASSES} text-right`}>Cost</th>
            )}
          </tr>
        </thead>
        <tbody>
          {lines.map((line) => (
            <tr
              key={`${line.tier}|${line.role}|${line.model_id}`}
              data-testid="usage-cost-line"
              className="border-t-hairline border-border-subtle"
            >
              <td className={CELL_CLASSES}>{TIER_LABEL[line.tier] ?? line.tier}</td>
              <td className={CELL_CLASSES}>{line.role}</td>
              <td className={`${CELL_CLASSES} break-all`}>
                {line.model_id}
                {line.long_context_calls > 0 ? (
                  <span
                    data-testid="usage-cost-line-long-context"
                    className="block text-text-secondary"
                  >
                    {longContextNote(line.long_context_calls)}
                  </span>
                ) : null}
              </td>
              <td className={NUMBER_CELL_CLASSES}>{line.input_tokens.toLocaleString()}</td>
              <td className={NUMBER_CELL_CLASSES}>{line.output_tokens.toLocaleString()}</td>
              {withheld ? null : (
                <td className={NUMBER_CELL_CLASSES}>{formatUsd(line.cost_usd)}</td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

export function UsageMeter({ events }: { events: readonly AgentEventFrame[] }): JSX.Element | null {
  const totals = usageTotals(events);
  const cost = turnCost(events);
  if (totals === undefined && cost === undefined) return null;
  const longContext = cost === undefined ? 0 : longContextCalls(cost.lines);

  return (
    <section
      data-testid={USAGE_METER_TESTID}
      aria-label="Token usage for this turn"
      className="flex flex-col gap-3 border-t-hairline border-border-subtle pt-4"
    >
      <dl className="flex flex-wrap gap-x-6 gap-y-3">
        {/*
         * The `data-testid` values are literals so `check_testid_preservation` can trace each one
         * from its test to this markup; a prop-mediated id is reported as unresolved.
         */}
        {totals === undefined ? null : (
          <>
            <div className={FIGURE_CLASSES}>
              <dt className={FIGURE_LABEL_CLASSES}>Tokens in</dt>
              <dd data-testid="usage-input" className={FIGURE_VALUE_CLASSES}>
                {totals.inputTokens.toLocaleString()}
              </dd>
            </div>
            <div className={FIGURE_CLASSES}>
              <dt className={FIGURE_LABEL_CLASSES}>Tokens out</dt>
              <dd data-testid="usage-output" className={FIGURE_VALUE_CLASSES}>
                {totals.outputTokens.toLocaleString()}
              </dd>
            </div>
            {totals.cacheReadTokens === null ? null : (
              <div className={FIGURE_CLASSES}>
                <dt className={FIGURE_LABEL_CLASSES}>Read from cache</dt>
                <dd data-testid="usage-cache-read" className={FIGURE_VALUE_CLASSES}>
                  {totals.cacheReadTokens.toLocaleString()}
                </dd>
              </div>
            )}
            {totals.cacheWriteTokens === null ? null : (
              <div className={FIGURE_CLASSES}>
                <dt className={FIGURE_LABEL_CLASSES}>Written to cache</dt>
                <dd data-testid="usage-cache-write" className={FIGURE_VALUE_CLASSES}>
                  {totals.cacheWriteTokens.toLocaleString()}
                </dd>
              </div>
            )}
          </>
        )}
        {longContext === 0 ? null : (
          <div className={FIGURE_CLASSES}>
            <dt className={FIGURE_LABEL_CLASSES}>Long-context calls</dt>
            <dd data-testid="usage-long-context-calls" className={FIGURE_VALUE_CLASSES}>
              {longContext.toLocaleString()}
            </dd>
          </div>
        )}
        {cost === undefined || cost.totalUsd === null ? null : (
          <div className={FIGURE_CLASSES}>
            <dt className={FIGURE_LABEL_CLASSES}>Cost</dt>
            <dd data-testid="usage-cost" className={FIGURE_VALUE_CLASSES}>
              {formatUsd(cost.totalUsd)}
            </dd>
          </div>
        )}
      </dl>
      {cost?.withheld === true ? (
        <p data-testid="usage-cost-withheld" className="text-2xs text-text-secondary">
          {COST_WITHHELD_TEXT}
        </p>
      ) : null}
      {cost === undefined || cost.lines.length === 0 ? null : (
        <CostBreakdown lines={cost.lines} withheld={cost.withheld} />
      )}
    </section>
  );
}
