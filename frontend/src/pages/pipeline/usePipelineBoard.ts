// SPDX-License-Identifier: MIT
/**
 * `usePipelineBoard`: loads opportunities plus pending stage-change proposals, and exposes
 * `proposeStageChange`, the board's only write path, which calls `submit_proposal`.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { useCrmApi } from "../../api/useCrmApi";
import { CrmApiError } from "../../api/CrmApiClient";
import { useReportView } from "../../components/AssistantPanel/useReportView";
import { OPPORTUNITY_STAGES } from "../../types/domain";
import type { Opportunity, OpportunityStage, Proposal } from "../../types/domain";
import type { ListProposalsResponse, SearchOpportunitiesResponse, SubmitProposalResponse } from "./types";

function newTransactionKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `tk-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export interface PendingStageChange {
  readonly proposalId: string;
  readonly toStage: OpportunityStage;
  readonly reason: string;
}

export interface UsePipelineBoardState {
  readonly opportunitiesByStage: ReadonlyMap<OpportunityStage, readonly Opportunity[]>;
  /** Keyed by `opportunity_id`. An opportunity present here has a stage change awaiting approval. */
  readonly pendingChanges: ReadonlyMap<string, PendingStageChange>;
  readonly loading: boolean;
  /**
   * `false` until the first fetch has settled, successfully or not. Distinct from `!loading`, which
   * is also `false` on the first render, before the effect runs.
   */
  readonly loaded: boolean;
  readonly error: CrmApiError | undefined;
  readonly truncated: boolean;
  readonly refetch: () => Promise<void>;
  readonly proposeStageChange: (
    opportunity: Opportunity,
    toStage: OpportunityStage,
    reason: string,
  ) => Promise<Proposal>;
}

export function usePipelineBoard(): UsePipelineBoardState {
  const api = useCrmApi();
  const [opportunities, setOpportunities] = useState<readonly Opportunity[]>([]);
  const [pendingChanges, setPendingChanges] = useState<ReadonlyMap<string, PendingStageChange>>(
    new Map(),
  );
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<CrmApiError | undefined>(undefined);
  const [fatal, setFatal] = useState<unknown>(undefined);
  const [truncated, setTruncated] = useState(false);

  const fetchBoard = useCallback(() => {
    return Promise.all([
      api.searchOpportunities({}) as Promise<SearchOpportunitiesResponse>,
      api.listProposals({ status: "pending" }) as Promise<ListProposalsResponse>,
    ])
      .then(([searchResult, proposalsResult]) => {
        setOpportunities(searchResult.items);
        setTruncated(searchResult.truncated);

        const nextPending = new Map<string, PendingStageChange>();
        for (const proposal of proposalsResult.proposals) {
          if (
            proposal.target_table === "opportunities" &&
            proposal.operation === "update" &&
            proposal.after_payload &&
            typeof proposal.after_payload.stage === "string"
          ) {
            nextPending.set(proposal.target_id, {
              proposalId: proposal.proposal_id,
              toStage: proposal.after_payload.stage as OpportunityStage,
              reason: proposal.reason,
            });
          }
        }
        setPendingChanges(nextPending);
      })
      .catch((err: unknown) => {
        // A non-`CrmApiError` is recorded and re-thrown during the next render (below), since a throw
        // inside this async callback would be an unhandled rejection no error boundary catches.
        if (err instanceof CrmApiError) {
          setError(err);
        } else {
          setFatal(err);
        }
      })
      .finally(() => {
        setLoading(false);
        setLoaded(true);
      });
    // `api` is referentially stable (memoised by `useCrmApi`) and is omitted from the deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void fetchBoard();
  }, [fetchBoard]);

  const refetch = useCallback(async () => {
    setLoading(true);
    setError(undefined);
    await fetchBoard();
  }, [fetchBoard]);

  const proposeStageChange = useCallback(
    async (opportunity: Opportunity, toStage: OpportunityStage, reason: string) => {
      // A stage change is high-risk: this always emits a proposal via `submit_proposal`.
      const result = (await api.submitProposal({
        target_table: "opportunities",
        target_id: opportunity.opportunity_id,
        operation: "update",
        after_payload: { stage: toStage },
        reason,
        transaction_key: newTransactionKey(),
      })) as SubmitProposalResponse;

      setPendingChanges((prev) => {
        const next = new Map(prev);
        next.set(opportunity.opportunity_id, {
          proposalId: result.proposal.proposal_id,
          toStage,
          reason,
        });
        return next;
      });
      return result.proposal;
    },
    [api],
  );

  // Re-thrown during render so the root error boundary catches it (see the `catch` above).
  if (fatal !== undefined) throw fatal;

  const opportunitiesByStage = useMemo(() => {
    const grouped = new Map<OpportunityStage, Opportunity[]>();
    for (const stage of OPPORTUNITY_STAGES) {
      grouped.set(stage, []);
    }
    for (const opportunity of opportunities) {
      grouped.get(opportunity.stage)?.push(opportunity);
    }
    return grouped as ReadonlyMap<OpportunityStage, readonly Opportunity[]>;
  }, [opportunities]);

  /*
   * Reports the board's record count to the assistant panel. The board stops at its own ceiling,
   * so `truncated` is passed with the count, which is then a lower bound.
   */
  useReportView({ rowCount: opportunities.length, truncated });

  return {
    opportunitiesByStage,
    pendingChanges,
    loading,
    loaded,
    error,
    truncated,
    refetch,
    proposeStageChange,
  };
}
