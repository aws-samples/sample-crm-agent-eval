// SPDX-License-Identifier: MIT
/**
 * Response-shape types for the opportunities surface, describing what the opportunities handlers
 * return so pages can narrow `unknown` at the boundary.
 */
import type { Activity, Contact, Note, Opportunity } from "../../types/domain";

/**
 * `search_opportunities` result shape (`src/crm/search.py::SearchResult`). `truncated`,
 * `examined_count` and `next_cursor` are present on every response.
 */
export interface SearchOpportunitiesResult {
  readonly items: readonly Opportunity[];
  readonly truncated: boolean;
  readonly examined_count: number;
  readonly next_cursor: string | null;
}

/** `get_opportunity` result shape: the opportunity plus its notes, activities, and contacts. */
export interface OpportunityDetailResult {
  readonly opportunity: Opportunity;
  readonly notes: readonly Note[];
  readonly activities: readonly Activity[];
  readonly contacts: readonly Contact[];
}

/** Narrow an `unknown` API response into a known shape, or throw a diagnostic error. */
export function asRecord(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null) {
    throw new Error("expected an object response from the API");
  }
  return value as Record<string, unknown>;
}

export function asSearchOpportunitiesResult(value: unknown): SearchOpportunitiesResult {
  const record = asRecord(value);
  return {
    items: Array.isArray(record.items) ? (record.items as Opportunity[]) : [],
    truncated: record.truncated === true,
    examined_count: typeof record.examined_count === "number" ? record.examined_count : 0,
    next_cursor: typeof record.next_cursor === "string" ? record.next_cursor : null,
  };
}

export function asOpportunityDetailResult(value: unknown): OpportunityDetailResult {
  const record = asRecord(value);
  const opportunity = record.opportunity ?? record;
  return {
    opportunity: opportunity as Opportunity,
    notes: Array.isArray(record.notes) ? (record.notes as Note[]) : [],
    activities: Array.isArray(record.activities) ? (record.activities as Activity[]) : [],
    contacts: Array.isArray(record.contacts) ? (record.contacts as Contact[]) : [],
  };
}

