// SPDX-License-Identifier: MIT
/**
 * Shows that search results were truncated by the backend's 2,000-examined-item budget. Renders
 * whenever `truncated` is true on a `search_opportunities` response (`src/crm/search.py`).
 */
import type { JSX } from "react";

import { Button } from "../../components/ui/button";

export function TruncationNotice({
  examinedCount,
  hasMore,
  onLoadMore,
}: {
  examinedCount: number;
  hasMore: boolean;
  onLoadMore?: () => void;
}): JSX.Element {
  return (
    <div
      role="status"
      data-testid="truncation-notice"
      className="max-w-prose space-y-3 rounded-surface border border-hairline border-caution-border bg-caution-surface px-5 py-4 text-caution-text"
    >
      <p className="text-sm leading-relaxed">
        <strong className="font-semibold">Results are incomplete.</strong> Only{" "}
        <span className="font-numeric">{examinedCount.toLocaleString()}</span> matching record
        {examinedCount === 1 ? "" : "s"} {examinedCount === 1 ? "was" : "were"} examined before the
        search limit was reached. Some opportunities may be missing from this list.
      </p>
      {hasMore && onLoadMore && (
        <Button type="button" variant="secondary" size="sm" onClick={onLoadMore}>
          Load more
        </Button>
      )}
    </div>
  );
}
