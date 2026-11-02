// SPDX-License-Identifier: MIT
/**
 * Opportunity list: free-text search plus stage and owner filters, with truncated results surfaced
 * explicitly.
 */
import { useCallback, useEffect, useId, useState, type FormEvent, type JSX } from "react";
import { Link } from "react-router-dom";

import { useCrmApi } from "../../api/useCrmApi";
import { useReportView } from "../../components/AssistantPanel/useReportView";
import { OpportunityTable } from "../../components/opportunities/OpportunityTable";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Skeleton } from "../../components/ui/skeleton";
import { cn, focusRing } from "../../components/ui/utils";
import type { FilterValue } from "../../hooks/usePageContext";
import { OPEN_STAGES, OPPORTUNITY_STAGES, type OpportunityStage } from "../../types/domain";
import { ApiErrorBanner } from "./ApiErrorBanner";
import { TruncationNotice } from "./TruncationNotice";
import { asSearchOpportunitiesResult, type SearchOpportunitiesResult } from "./types";

/** Field label styling, shared by all three filter controls. */
const fieldLabel = "text-xs font-medium tracking-wide text-text-secondary";

export function OpportunityListPage(): JSX.Element {
  const api = useCrmApi();

  const [query, setQuery] = useState("");
  const [stage, setStage] = useState<OpportunityStage | "">("");
  const [ownerRepId, setOwnerRepId] = useState("");

  const [result, setResult] = useState<SearchOpportunitiesResult | undefined>(undefined);
  const [error, setError] = useState<unknown>(undefined);
  const [loading, setLoading] = useState(true);
  const [cursor, setCursor] = useState<string | undefined>(undefined);

  /*
   * The filters that produced the rows currently on screen, captured when the request is issued
   * rather than read from the form controls; this is what the assistant panel is told.
   */
  const [appliedFilters, setAppliedFilters] = useState<Readonly<Record<string, FilterValue>>>({});

  const queryId = useId();
  const stageId = useId();
  const ownerId = useId();

  const fetchPage = useCallback(
    (nextCursor?: string) =>
      api
        .searchOpportunities({
          query: query.trim() === "" ? undefined : query.trim(),
          stage: stage === "" ? undefined : stage,
          owner_rep_id: ownerRepId.trim() === "" ? undefined : ownerRepId.trim(),
          cursor: nextCursor,
        })
        .then((raw) => {
          const parsed = asSearchOpportunitiesResult(raw);
          setResult((prev) =>
            nextCursor && prev
              ? { ...parsed, items: [...prev.items, ...parsed.items] }
              : parsed,
          );
          setCursor(parsed.next_cursor ?? undefined);
        })
        .catch((err: unknown) => {
          setError(err);
        })
        .finally(() => {
          setLoading(false);
        }),
    [api, query, stage, ownerRepId],
  );

  const runSearch = useCallback(
    async (nextCursor?: string) => {
      setLoading(true);
      setError(undefined);
      // Set before the await, so the descriptor never describes the *previous* search while the new
      // rows are already rendering.
      setAppliedFilters({
        ...(query.trim() === "" ? {} : { query: query.trim() }),
        ...(stage === "" ? {} : { stage }),
        ...(ownerRepId.trim() === "" ? {} : { owner_rep_id: ownerRepId.trim() }),
      });
      await fetchPage(nextCursor);
    },
    [fetchPage, query, stage, ownerRepId],
  );

  useEffect(() => {
    void fetchPage();
    // Runs on mount only; later searches are triggered by form submit and load-more.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /*
   * Reports this view to the assistant panel. When `truncated` is set the count is a lower bound,
   * so the panel does not compare it with the agent's own result.
   */
  useReportView({
    query: { filters: appliedFilters },
    ...(result === undefined ? {} : { rowCount: result.items.length, truncated: result.truncated }),
  });

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setCursor(undefined);
    void runSearch();
  }

  return (
    <main aria-labelledby={`${queryId}-heading`} className="mx-auto w-full max-w-content space-y-7 px-6 py-8">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <h1
          id={`${queryId}-heading`}
          className="text-2xl font-semibold tracking-tight text-text-primary"
        >
          Opportunities
        </h1>
        <Button asChild variant="primary" size="sm">
          <Link to="/opportunities/new">Create opportunity</Link>
        </Button>
      </header>

      <form
        onSubmit={handleSubmit}
        role="search"
        aria-label="Filter opportunities"
        className="grid gap-4 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6 shadow-raised md:grid-cols-[2fr_1fr_1fr_auto] md:items-end"
      >
        <div className="flex flex-col gap-2">
          <label htmlFor={queryId} className={fieldLabel}>
            Search by name
          </label>
          <Input id={queryId} type="search" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor={stageId} className={fieldLabel}>
            Stage
          </label>
          {/* A native `<select>` styled to match `Input`; tests drive it with `selectOptions`. */}
          <select
            id={stageId}
            value={stage}
            onChange={(e) => setStage(e.target.value as OpportunityStage | "")}
            className={cn(
              "h-control-md w-full rounded-control border border-hairline border-border-control bg-surface-raised px-4 text-sm text-text-primary",
              focusRing,
            )}
          >
            <option value="">All stages</option>
            {OPPORTUNITY_STAGES.map((s) => (
              <option key={s} value={s}>
                {s} {OPEN_STAGES.includes(s) ? "" : "(closed)"}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor={ownerId} className={fieldLabel}>
            Owner rep ID
          </label>
          <Input
            id={ownerId}
            type="text"
            value={ownerRepId}
            onChange={(e) => setOwnerRepId(e.target.value)}
            className="font-numeric"
          />
        </div>
        <Button type="submit" variant="primary" disabled={loading}>
          {loading ? "Searching…" : "Search"}
        </Button>
      </form>

      {error !== undefined && <ApiErrorBanner error={error} />}

      {/* `result` is undefined only before the mount search resolves, so this skeleton is the
          not-yet-loaded state, distinct from the empty state below. */}
      {result === undefined && error === undefined && (
        <div className="space-y-3 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6">
          <Skeleton label="Loading opportunities" className="h-4 w-1/3" />
          {[0, 1, 2, 3, 4].map((row) => (
            <Skeleton key={row} label="Loading row" className="h-row-default w-full" />
          ))}
        </div>
      )}

      {result && (
        <>
          {result.truncated && (
            <TruncationNotice
              examinedCount={result.examined_count}
              hasMore={Boolean(cursor)}
              onLoadMore={cursor ? () => void runSearch(cursor) : undefined}
            />
          )}

          {result.items.length === 0 && error === undefined ? (
            <p className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-6 py-8 text-sm text-empty-text">
              No opportunities matched your search.
            </p>
          ) : (
            <OpportunityTable
              items={result.items}
              caption="Opportunity results"
              truncated={result.truncated}
            />
          )}
        </>
      )}
    </main>
  );
}
