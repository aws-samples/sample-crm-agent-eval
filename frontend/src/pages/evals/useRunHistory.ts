// SPDX-License-Identifier: MIT
/**
 * Run history: the server's runs, newest first, one `eval.runs.LIST_PAGE_SIZE` page at a time
 * (`eval_trigger` `list`). The first page loads on mount, and can be retried when it fails;
 * "Load more" follows the opaque cursor. A run this page learns about from a
 * submit or a poll is merged in by id, so the table never shows a stale status for the focused run.
 *
 * Also the run descriptions the history table and the dashboard share: a status label, a
 * task-progress phrase (`N/total`), and the run's spend. Spend is shown only for a live run, as the
 * backend metered it; a stub sweep makes no model call, so it says so rather than rendering a zero
 * figure.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { formatUsd } from "../../lib/format";
import type { EvalJobStatus, EvalRunClient, EvalRunStatusResponse } from "./EvalRunClient";
import { classifyRunMode } from "./runMode";

/** How each run status reads on this page. */
export const STATUS_LABEL: Readonly<Record<EvalJobStatus, string>> = {
  queued: "queued",
  running: "running",
  cancelling: "cancelling",
  completed: "completed",
  cancelled: "cancelled",
  failed: "failed",
};

/** The items that ran, done or failed: the `N` of the `N/total` the page reports. */
export function tasksRan(run: EvalRunStatusResponse): number {
  return run.tasks_done + run.tasks_failed;
}

/** `N/total tasks`, plus the failed and skipped counts when there are any. */
export function describeProgress(run: EvalRunStatusResponse): string {
  const parts = [`${tasksRan(run)}/${run.tasks_total} tasks`];
  if (run.tasks_failed > 0) parts.push(`${run.tasks_failed} failed`);
  if (run.tasks_skipped > 0) parts.push(`${run.tasks_skipped} skipped`);
  return parts.join(", ");
}

/**
 * The run's spend as the backend metered it. A live run shows its figure; a stub run states that it
 * incurs no inference cost; an unrecognised mode shows no figure.
 */
export function describeSpend(run: EvalRunStatusResponse): string {
  switch (classifyRunMode(run.mode)) {
    case "live":
      return formatUsd(run.cost_spent_usd);
    case "stub":
      return "no inference cost (stub)";
    default:
      return "not displayed (unrecognised mode)";
  }
}

/**
 * {@link describeSpend}, except a live run with no measured item (none done, at least one failed)
 * reads "not measured" instead of a figure. The history table and the dashboard both render this.
 */
export function describeRunSpend(run: EvalRunStatusResponse): string {
  const unmeasured =
    classifyRunMode(run.mode) === "live" && run.tasks_done === 0 && run.tasks_failed > 0;
  return unmeasured ? "not measured" : describeSpend(run);
}

export interface UseRunHistoryResult {
  /** Newest first, as the server ordered them. */
  readonly runs: readonly EvalRunStatusResponse[];
  /** True until the first page has loaded or failed. */
  readonly loading: boolean;
  /** True while a further page is loading. */
  readonly loadingMore: boolean;
  /** The last list failure, as the backend worded it. */
  readonly error: string | undefined;
  /** Whether the server reported a further page. */
  readonly hasMore: boolean;
  /** Whether the first page failed, so {@link UseRunHistoryResult.retry} applies. */
  readonly firstPageFailed: boolean;
  loadMore(): Promise<void>;
  /** Load the first page again after it failed. */
  retry(): Promise<void>;
  /** Replace a known run by id, or add a new one at the top. */
  upsert(run: EvalRunStatusResponse): void;
}

function messageOf(err: unknown): string {
  return err instanceof Error && err.message ? err.message : "the run history could not be loaded";
}

/** Merge `page` after `current`, dropping any run already listed. */
function appendPage(
  current: readonly EvalRunStatusResponse[],
  page: readonly EvalRunStatusResponse[],
): EvalRunStatusResponse[] {
  const known = new Set(current.map((run) => run.run_id));
  return [...current, ...page.filter((run) => !known.has(run.run_id))];
}

/** Loads and pages the run history via {@link EvalRunClient.listRuns}. */
export function useRunHistory(client: EvalRunClient): UseRunHistoryResult {
  const [runs, setRuns] = useState<readonly EvalRunStatusResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | undefined>(undefined);
  const [cursor, setCursor] = useState<string | null>(null);
  const [firstPageFailed, setFirstPageFailed] = useState(false);
  const unmounted = useRef(false);

  const fetchFirstPage = useCallback(() => {
    return client
      .listRuns()
      .then((page) => {
        if (unmounted.current) return;
        setRuns((current) => appendPage(current, page.runs));
        setCursor(page.next_cursor);
      })
      .catch((err: unknown) => {
        if (unmounted.current) return;
        setError(messageOf(err));
        setFirstPageFailed(true);
      })
      .finally(() => {
        if (!unmounted.current) setLoading(false);
      });
  }, [client]);

  useEffect(() => {
    unmounted.current = false;
    void fetchFirstPage();
    return () => {
      unmounted.current = true;
    };
  }, [fetchFirstPage]);

  const loadFirstPage = useCallback(async () => {
    setLoading(true);
    setError(undefined);
    setFirstPageFailed(false);
    await fetchFirstPage();
  }, [fetchFirstPage]);

  const loadMore = useCallback(async () => {
    if (cursor === null) return;
    setLoadingMore(true);
    setError(undefined);
    try {
      const page = await client.listRuns(cursor);
      if (unmounted.current) return;
      setRuns((current) => appendPage(current, page.runs));
      setCursor(page.next_cursor);
    } catch (err) {
      if (!unmounted.current) setError(messageOf(err));
    } finally {
      if (!unmounted.current) setLoadingMore(false);
    }
  }, [client, cursor]);

  const upsert = useCallback((run: EvalRunStatusResponse) => {
    setRuns((current) =>
      current.some((known) => known.run_id === run.run_id)
        ? current.map((known) => (known.run_id === run.run_id ? run : known))
        : [run, ...current],
    );
  }, []);

  return {
    runs,
    loading,
    loadingMore,
    error,
    hasMore: cursor !== null,
    firstPageFailed,
    loadMore,
    retry: loadFirstPage,
    upsert,
  };
}
