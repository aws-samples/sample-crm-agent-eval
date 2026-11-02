// SPDX-License-Identifier: MIT
/**
 * Activities page, routed at `/opportunities/:opportunityId/activities`: the timeline with create
 * and inline edit. There is no delete; the toolspec has no `delete_activity` tool.
 */
import { useParams } from "react-router-dom";
import type { JSX } from "react";

import { ActivityTimeline } from "./ActivityTimeline";

export function ActivitiesPage(): JSX.Element {
  const { opportunityId } = useParams<{ opportunityId: string }>();

  if (!opportunityId) {
    return (
      <main className="mx-auto w-full max-w-content px-6 py-8">
        <p
          role="alert"
          className="max-w-prose rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-4 text-sm text-danger-text"
        >
          No opportunity selected.
        </p>
      </main>
    );
  }

  return (
    <main
      aria-label="Opportunity activities"
      className="mx-auto w-full max-w-content space-y-7 px-6 py-8"
    >
      <ActivityTimeline opportunityId={opportunityId} />
    </main>
  );
}
