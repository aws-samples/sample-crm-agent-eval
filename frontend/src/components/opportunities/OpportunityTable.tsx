// SPDX-License-Identifier: MIT
/**
 * The opportunities table shared by the opportunity list and account detail: name (linked),
 * stage badge, value, owner and close date. `truncated` appends a pointer to the page's
 * truncation notice to the caption.
 */
import type { JSX } from "react";
import { Link } from "react-router-dom";

import { formatCurrency, formatDate } from "../../lib/format";
import { OPEN_STAGES, type Opportunity, type OpportunityStage } from "../../types/domain";
import { Badge } from "../ui/badge";
import { cn, focusRing } from "../ui/utils";

/** Inline text link styling, with the shared `focusRing`. */
const textLink = cn(
  "rounded-control text-text-link underline-offset-4 hover:underline",
  focusRing,
);

/**
 * Badge variant per stage: `Closed Won` and `Closed Lost` (the toolspec's stage names) are
 * distinguished from each other and from the open stages.
 */
function stageBadgeVariant(stage: OpportunityStage): "positive" | "neutral" | "accent" {
  if ((OPEN_STAGES as readonly string[]).includes(stage)) return "accent";
  return stage === "Closed Won" ? "positive" : "neutral";
}

export interface OpportunityTableProps {
  readonly items: readonly Opportunity[];
  readonly caption: string;
  readonly truncated?: boolean;
}

export function OpportunityTable({
  items,
  caption,
  truncated = false,
}: OpportunityTableProps): JSX.Element {
  return (
    <div className="overflow-x-auto rounded-surface border border-hairline border-border-subtle bg-surface-raised shadow-raised">
      <table className="w-full border-collapse text-sm">
        <caption className="px-cell-x py-cell-y text-left text-xs text-text-secondary">
          {caption}
          {truncated ? " (incomplete — see notice above)" : ""}
        </caption>
        <thead>
          <tr className="border-b-hairline border-border-default bg-surface-inset">
            {["Name", "Stage", "Value", "Owner", "Close date"].map((heading) => (
              <th
                key={heading}
                scope="col"
                className={cn(
                  "px-cell-x py-cell-y text-xs font-semibold tracking-wide text-text-secondary",
                  heading === "Value" ? "text-right" : "text-left",
                )}
              >
                {heading}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border-subtle">
          {items.map((opp) => (
            <tr key={opp.opportunity_id} className="hover:bg-surface-hover">
              <th scope="row" className="px-cell-x py-cell-y text-left font-medium">
                <Link to={`/opportunities/${opp.opportunity_id}`} className={textLink}>
                  {opp.name}
                </Link>
              </th>
              <td className="px-cell-x py-cell-y">
                <Badge variant={stageBadgeVariant(opp.stage)}>{opp.stage}</Badge>
              </td>
              {/* Right-aligned and tabular so figures compare down the column. */}
              <td className="px-cell-x py-cell-y text-right font-numeric text-text-primary">
                {formatCurrency(opp.value)}
              </td>
              <td className="px-cell-x py-cell-y font-numeric text-text-secondary">
                {opp.owner_rep_id}
              </td>
              <td className="px-cell-x py-cell-y text-text-secondary">
                {formatDate(opp.close_date)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
