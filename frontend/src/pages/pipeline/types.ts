// SPDX-License-Identifier: MIT
/**
 * Response-shape types for the pipeline board, hand-maintained from the handler returns:
 *   - `crm.handlers.search_opportunities` returns `{items, truncated, examined_count,
 *     next_cursor}` (`crm.search.SearchResult`).
 *   - `crm.handlers.submit_proposal` returns `{proposal}`.
 *   - `crm.handlers.list_proposals` returns `{proposals, next_cursor}`.
 */
import type { Opportunity, Proposal } from "../../types/domain";

export interface SearchOpportunitiesResponse {
  readonly items: readonly Opportunity[];
  readonly truncated: boolean;
  readonly examined_count: number;
  readonly next_cursor?: string | null;
}

export interface SubmitProposalResponse {
  readonly proposal: Proposal;
}

export interface ListProposalsResponse {
  readonly proposals: readonly Proposal[];
  readonly next_cursor?: string | null;
}
