// SPDX-License-Identifier: MIT
/**
 * Stage-transition classification mirroring `src/crm/domain/stages.py`'s `classify_transition` /
 * `PERMITTED_TRANSITION_KINDS`, so the board can mark a refused drop target before calling the
 * server. The server re-classifies in `submit_proposal` and is the authority.
 *
 * Regression (e.g. Negotiation back to Discovery) is permitted; any exit from a closed stage
 * (reopening, or moving between Closed Won and Closed Lost) is refused.
 */
import { OPEN_STAGES } from "../../types/domain";
import type { OpportunityStage } from "../../types/domain";

export type TransitionKind =
  | "same_stage"
  | "advance"
  | "regression"
  | "close"
  | "reopen"
  | "closed_lateral";

const STAGE_INDEX: Readonly<Record<OpportunityStage, number>> = {
  Discovery: 0,
  Qualification: 1,
  "Security Review": 2,
  Negotiation: 3,
  "Closed Won": 4,
  "Closed Lost": 5,
};

const OPEN_STAGE_SET = new Set<string>(OPEN_STAGES);

export function classifyTransition(
  fromStage: OpportunityStage,
  toStage: OpportunityStage,
): TransitionKind {
  if (fromStage === toStage) return "same_stage";

  const fromOpen = OPEN_STAGE_SET.has(fromStage);
  const toOpen = OPEN_STAGE_SET.has(toStage);

  if (fromOpen && toOpen) {
    return STAGE_INDEX[toStage] > STAGE_INDEX[fromStage] ? "advance" : "regression";
  }
  if (fromOpen && !toOpen) return "close";
  if (!fromOpen && toOpen) return "reopen";
  return "closed_lateral";
}

export function transitionRefusalReason(kind: TransitionKind): string | undefined {
  switch (kind) {
    case "reopen":
      return "This opportunity is closed; reopening a closed stage is not permitted.";
    case "closed_lateral":
      return "Moving between two closed stages is not permitted.";
    default:
      return undefined;
  }
}
