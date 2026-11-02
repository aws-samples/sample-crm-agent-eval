// SPDX-License-Identifier: MIT
/**
 * Response-shape types for the accounts surface, describing what `search_accounts` returns so the
 * page can narrow `unknown` at the boundary.
 */

/**
 * A row as `search_accounts` returns it: `account_id`, `name` and `tenant` always; `owner_rep_id`
 * only when the owner filter routes the query to `gsi_owner`, which projects it.
 */
export interface AccountRow {
  readonly account_id: string;
  readonly name: string;
  readonly tenant: string;
  /** Present only when the owner filter routed the query to `gsi_owner`. */
  readonly owner_rep_id?: string;
}

/**
 * `search_accounts` result shape (`src/crm/search.py::SearchResult`). `truncated`,
 * `examined_count` and `next_cursor` are present on every response.
 */
export interface SearchAccountsResult {
  readonly items: readonly AccountRow[];
  readonly truncated: boolean;
  readonly examined_count: number;
  readonly next_cursor: string | null;
}

/** Narrow an `unknown` API response into a known shape, or throw a diagnostic error. */
export function asRecord(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null) {
    throw new Error("expected an object response from the API");
  }
  return value as Record<string, unknown>;
}

export function asSearchAccountsResult(value: unknown): SearchAccountsResult {
  const record = asRecord(value);
  return {
    items: Array.isArray(record.items) ? (record.items as AccountRow[]) : [],
    truncated: record.truncated === true,
    examined_count: typeof record.examined_count === "number" ? record.examined_count : 0,
    next_cursor: typeof record.next_cursor === "string" ? record.next_cursor : null,
  };
}
