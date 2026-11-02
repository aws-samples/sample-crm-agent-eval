// SPDX-License-Identifier: MIT
/**
 * Account list page: free-text name search plus an owner filter against `search_accounts`, with
 * truncated results surfaced explicitly. Row names link to the account detail page.
 */
import { useCallback, useEffect, useId, useState, type FormEvent, type JSX } from "react";
import { Link } from "react-router-dom";

import { useCrmApi } from "../../api/useCrmApi";
import { useReportView } from "../../components/AssistantPanel/useReportView";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Skeleton } from "../../components/ui/skeleton";
import { cn, focusRing } from "../../components/ui/utils";
import type { FilterValue } from "../../hooks/usePageContext";
import { ApiErrorBanner } from "../opportunities/ApiErrorBanner";
import { TruncationNotice } from "../opportunities/TruncationNotice";
import { asSearchAccountsResult, type SearchAccountsResult } from "./types";

/** Inline text link styling, with the shared `focusRing`. */
const textLink = cn(
  "rounded-control text-text-link underline-offset-4 hover:underline",
  focusRing,
);

/** Field label styling, shared by both filter controls. */
const fieldLabel = "text-xs font-medium tracking-wide text-text-secondary";

export function AccountListPage(): JSX.Element {
  const api = useCrmApi();

  const [query, setQuery] = useState("");
  const [ownerRepId, setOwnerRepId] = useState("");

  const [result, setResult] = useState<SearchAccountsResult | undefined>(undefined);
  const [error, setError] = useState<unknown>(undefined);
  const [loading, setLoading] = useState(true);
  const [cursor, setCursor] = useState<string | undefined>(undefined);

  /*
   * The filters that produced the rows currently on screen, captured when the request is issued
   * rather than read from the form controls; this is what the assistant panel is told.
   */
  const [appliedFilters, setAppliedFilters] = useState<Readonly<Record<string, FilterValue>>>({});

  const queryId = useId();
  const ownerId = useId();

  const fetchPage = useCallback(
    (nextCursor?: string) =>
      api
        .searchAccounts({
          query: query.trim() === "" ? undefined : query.trim(),
          owner_rep_id: ownerRepId.trim() === "" ? undefined : ownerRepId.trim(),
          cursor: nextCursor,
        })
        .then((raw) => {
          const parsed = asSearchAccountsResult(raw);
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
    [api, query, ownerRepId],
  );

  const runSearch = useCallback(
    async (nextCursor?: string) => {
      setLoading(true);
      setError(undefined);
      // Set before the await, so the descriptor never describes the *previous* search while the new
      // rows are already rendering.
      setAppliedFilters({
        ...(query.trim() === "" ? {} : { query: query.trim() }),
        ...(ownerRepId.trim() === "" ? {} : { owner_rep_id: ownerRepId.trim() }),
      });
      await fetchPage(nextCursor);
    },
    [fetchPage, query, ownerRepId],
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
          Accounts
        </h1>
        <Button asChild variant="primary" size="sm">
          <Link to="/accounts/new">Create account</Link>
        </Button>
      </header>

      <form
        onSubmit={handleSubmit}
        role="search"
        aria-label="Filter accounts"
        className="grid gap-4 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6 shadow-raised md:grid-cols-[2fr_1fr_auto] md:items-end"
      >
        <div className="flex flex-col gap-2">
          <label htmlFor={queryId} className={fieldLabel}>
            Search by name
          </label>
          <Input id={queryId} type="search" value={query} onChange={(e) => setQuery(e.target.value)} />
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
          <Skeleton label="Loading accounts" className="h-4 w-1/3" />
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
              No accounts matched your search.
            </p>
          ) : (
            <div className="overflow-x-auto rounded-surface border border-hairline border-border-subtle bg-surface-raised shadow-raised">
              <table className="w-full border-collapse text-sm">
                <caption className="px-cell-x py-cell-y text-left text-xs text-text-secondary">
                  Account results{result.truncated ? " (incomplete — see notice above)" : ""}
                </caption>
                <thead>
                  <tr className="border-b-hairline border-border-default bg-surface-inset">
                    {["Name", "Owner"].map((heading) => (
                      <th
                        key={heading}
                        scope="col"
                        className="px-cell-x py-cell-y text-left text-xs font-semibold tracking-wide text-text-secondary"
                      >
                        {heading}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-border-subtle">
                  {result.items.map((account) => (
                    <tr key={account.account_id} className="hover:bg-surface-hover">
                      <th scope="row" className="px-cell-x py-cell-y text-left font-medium">
                        <Link to={`/accounts/${account.account_id}`} className={textLink}>
                          {account.name}
                        </Link>
                      </th>
                      {/* `owner_rep_id` is only projected on the owner-filtered path; an em dash
                          means "not shown here", not an empty owner. */}
                      <td className="px-cell-x py-cell-y font-numeric text-text-secondary">
                        {account.owner_rep_id ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </main>
  );
}
