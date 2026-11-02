// SPDX-License-Identifier: MIT
/**
 * The run history table, newest first. Rows render in the server's order with no sort affordance.
 */
import type { JSX } from "react";

import { Button } from "../../components/ui/button";
import type { EvalRunStatusResponse } from "./EvalRunClient";
import { erroredItemsCostNote } from "./types";
import { STATUS_LABEL, describeProgress, describeRunSpend } from "./useRunHistory";

const COLUMN_HEADER =
  "h-row-dense px-cell-x text-left align-middle font-sans text-2xs font-semibold text-text-secondary";
const CELL_NUM =
  "px-cell-x py-cell-y align-middle font-numeric text-xs tabular-nums text-text-primary";
const CELL_WORD = "px-cell-x py-cell-y align-middle font-sans text-xs text-text-primary";
const NOTICE =
  "rounded-control border border-hairline px-5 py-4 font-sans text-sm leading-snug";

export interface RunHistoryProps {
  readonly runs: readonly EvalRunStatusResponse[];
  readonly loading: boolean;
  readonly loadingMore: boolean;
  readonly error: string | undefined;
  readonly hasMore: boolean;
  /** Offer "Retry loading runs", which calls `onRetry`. */
  readonly canRetry: boolean;
  /** The run shown in detail above, marked in its row. */
  readonly focusedRunId: string | undefined;
  readonly onOpen: (run: EvalRunStatusResponse) => void;
  readonly onLoadMore: () => void;
  readonly onRetry: () => void;
}

/** The history section: a table of runs, an empty or error notice, and "Load more runs". */
export function RunHistory({
  runs,
  loading,
  loadingMore,
  error,
  hasMore,
  canRetry,
  focusedRunId,
  onOpen,
  onLoadMore,
  onRetry,
}: RunHistoryProps): JSX.Element {
  return (
    <section
      aria-labelledby="eval-history-heading"
      className="flex flex-col gap-4 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6"
    >
      <h2
        id="eval-history-heading"
        className="font-sans text-lg font-semibold leading-snug text-text-primary"
      >
        Run history
      </h2>

      {loading ? (
        <p role="status" className={`${NOTICE} border-info-border bg-info-surface text-info-text`}>
          Loading runs…
        </p>
      ) : null}

      {/* Not role="alert": a page-level alert is reserved for the focused run. */}
      {error !== undefined ? (
        <p className={`${NOTICE} border-danger-border bg-danger-surface text-danger-text`}>
          History unavailable: {error}
        </p>
      ) : null}
      {canRetry && !loading ? (
        <Button type="button" variant="secondary" className="self-start" onClick={onRetry}>
          Retry loading runs
        </Button>
      ) : null}

      {!loading && error === undefined && runs.length === 0 ? (
        <p className={`${NOTICE} border-empty-border bg-empty-surface text-empty-text`}>
          No sweeps have been run yet. Start one above and it appears here.
        </p>
      ) : null}

      {runs.length > 0 ? (
        <div className="w-full overflow-x-auto border-t-hairline border-border-subtle">
          <table className="w-full min-w-max border-collapse font-sans text-sm text-text-primary">
            <caption className="px-6 py-4 text-left font-sans text-2xs leading-snug text-text-muted">
              Evaluation sweeps, newest first
            </caption>
            <thead className="border-b-hairline border-border-default">
              <tr>
                {["Submitted", "Mode", "Status", "Tasks", "Started by", "Spend", "Details"].map(
                  (label) => (
                    <th key={label} scope="col" className={COLUMN_HEADER}>
                      {label}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => {
                const focused = run.run_id === focusedRunId;
                const erroredItemsNote = erroredItemsCostNote(run.tasks_failed);
                return (
                  <tr
                    key={run.run_id}
                    aria-current={focused ? "true" : undefined}
                    className={`border-b-hairline border-border-subtle${focused ? " bg-surface-inset" : ""}`}
                  >
                    <th scope="row" className={`${CELL_NUM} text-left font-medium`}>
                      {run.submitted_at}
                    </th>
                    <td className={CELL_WORD}>{run.mode}</td>
                    <td className={CELL_WORD}>
                      {STATUS_LABEL[run.status]}
                      {run.cancel_requested && !run.is_terminal ? " (cancel requested)" : ""}
                    </td>
                    <td className={CELL_NUM}>{describeProgress(run)}</td>
                    <td className={CELL_NUM}>{run.started_by_rep_id ?? "not reported"}</td>
                    <td className={CELL_NUM}>
                      {describeRunSpend(run)}
                      {erroredItemsNote !== undefined ? (
                        <span className="block max-w-prose font-sans text-2xs leading-normal text-text-muted">
                          {erroredItemsNote}
                        </span>
                      ) : null}
                    </td>
                    <td className="px-cell-x py-cell-y align-middle">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => onOpen(run)}
                        aria-label={`Show run ${run.run_id}`}
                        aria-pressed={focused}
                      >
                        {focused ? "Shown" : "Show"}
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      {hasMore ? (
        <Button
          type="button"
          variant="secondary"
          className="self-start"
          onClick={onLoadMore}
          disabled={loadingMore}
        >
          {loadingMore ? "Loading more runs…" : "Load more runs"}
        </Button>
      ) : null}
    </section>
  );
}
