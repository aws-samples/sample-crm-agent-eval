// SPDX-License-Identifier: MIT
/**
 * The hook a routed page calls to describe its own view to the assistant panel:
 *
 * ```ts
 * useReportView({ query: { filters: appliedFilters }, rowCount: rows.length, truncated });
 * ```
 *
 * Report the filters that produced the rows on screen, never unsubmitted form state. `rowCount` is a
 * number and `filters` are scalars. A page with no countable collection omits `rowCount`; one whose
 * query cannot be expressed in the agent's tool vocabulary passes `inexpressible` with a reason.
 */

import { useEffect } from "react";
import { useLocation } from "react-router-dom";

import type { ViewQueryState, ViewReport } from "../../hooks/usePageContext";
import { clearViewSnapshot, setViewSnapshot } from "./viewReport";

export interface ReportViewInput {
  /** The active query, in the shape the descriptor carries it. Omit when unknown. */
  readonly query?: ViewQueryState;
  /** Why this view's query cannot be stated in the agent's tool vocabulary. Wins over `query`. */
  readonly inexpressible?: string;
  /** How many rows the view is showing. Omit when there is no countable collection. */
  readonly rowCount?: number;
  /** `true` when the view's own result was cut short, making `rowCount` a lower bound. */
  readonly truncated?: boolean;
}

/**
 * Publish this view's description for as long as the page is mounted.
 *
 * @param input What the page knows. Re-published whenever any part of it changes, so a filter change
 *   or a "load more" is reflected without the page doing anything else.
 */
export function useReportView(input: ReportViewInput): void {
  const { pathname } = useLocation();
  const { query, inexpressible, rowCount, truncated } = input;

  /*
   * The primitive fields are the dependencies rather than `input`, so a fresh object literal per
   * render does not re-publish. `query` is compared by identity.
   */
  useEffect(() => {
    // `inexpressible` wins over `query`, matching `derivePageContext`'s precedence.
    const report: ViewReport | undefined =
      inexpressible !== undefined
        ? { inexpressible }
        : query !== undefined
          ? { query }
          : undefined;

    setViewSnapshot({
      route: pathname,
      ...(report === undefined ? {} : { report }),
      ...(rowCount === undefined ? {} : { rowCount }),
      ...(truncated === undefined ? {} : { truncated }),
    });
    return () => clearViewSnapshot(pathname);
  }, [pathname, query, inexpressible, rowCount, truncated]);
}
