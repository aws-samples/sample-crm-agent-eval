// SPDX-License-Identifier: MIT
/**
 * The conversation's running cost for the panel header: the client-side sum of each turn's
 * server-computed `cost_usd`. Nothing is priced here.
 */

import type { JSX } from "react";

import { formatUsd } from "../../lib/format";
import type { ConversationTurn } from "./PanelProvider";
import { turnCost } from "./UsageMeter";

export const CONVERSATION_COST_TESTID = "conversation-cost";

/** Shown instead of a total when any turn's cost was withheld. */
export const CONVERSATION_COST_WITHHELD_TEXT = "Cost withheld";

/** The conversation's cost so far. */
export interface ConversationCost {
  readonly totalUsd: number;
  /** True when some turn's cost was withheld, so `totalUsd` is not the whole conversation. */
  readonly withheld: boolean;
}

/**
 * Sum the cost of every turn that reported one.
 *
 * @returns `undefined` until some turn reports cost information.
 */
export function conversationCost(
  conversation: readonly ConversationTurn[],
): ConversationCost | undefined {
  let reported = false;
  let totalUsd = 0;
  let withheld = false;
  for (const turn of conversation) {
    const cost = turnCost(turn.frames);
    if (cost === undefined) continue;
    reported = true;
    if (cost.withheld) withheld = true;
    else if (cost.totalUsd !== null) totalUsd += cost.totalUsd;
  }
  return reported ? { totalUsd, withheld } : undefined;
}

export function ConversationCostHeader({
  conversation,
}: {
  conversation: readonly ConversationTurn[];
}): JSX.Element | null {
  const cost = conversationCost(conversation);
  if (cost === undefined) return null;

  return (
    <p
      data-testid={CONVERSATION_COST_TESTID}
      aria-label="Cost of this conversation"
      className="text-2xs tabular-nums text-text-secondary"
    >
      {cost.withheld ? CONVERSATION_COST_WITHHELD_TEXT : formatUsd(cost.totalUsd)}
    </p>
  );
}
