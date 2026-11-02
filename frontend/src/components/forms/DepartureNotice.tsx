// SPDX-License-Identifier: MIT
/**
 * Informational banner shown when a contact is marked `departed`, computed from opportunities already
 * fetched for the account. It never prevents the save; departure is an ordinary status update.
 */
import type { JSX } from "react";

import type { Opportunity } from "../../types/domain";

export interface DepartureNoticeProps {
  readonly contactId: string;
  /** Opportunities for the contact's account, already loaded by the caller (e.g. account detail). */
  readonly accountOpportunities: readonly Opportunity[];
}

export function DepartureNotice({
  contactId,
  accountOpportunities,
}: DepartureNoticeProps): JSX.Element | null {
  const referencing = accountOpportunities.filter((opp) => opp.primary_contact_id === contactId);
  if (referencing.length === 0) {
    return null;
  }
  return (
    <div
      role="status"
      /* `--caution-*`: this is informational and never blocks the save, so it is neither an error
         nor a denial. The component's own docstring is explicit that departure is allowed. */
      className="max-w-prose rounded-surface border border-hairline border-caution-border bg-caution-surface px-5 py-4 text-caution-text"
    >
      <p className="text-sm leading-relaxed">
        This contact is the primary contact on {referencing.length}{" "}
        {referencing.length === 1 ? "opportunity" : "opportunities"}: {" "}
        {referencing.map((opp) => opp.name).join(", ")}. Marking them departed will not clear or
        reassign that reference — you will need to propose a primary-contact change on each
        opportunity separately.
      </p>
    </div>
  );
}
