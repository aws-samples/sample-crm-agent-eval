// SPDX-License-Identifier: MIT
/**
 * Activity timeline for one opportunity: list (most recent first), create (via `ActivityForm`) and
 * inline update. After a log, the server's returned opportunity is passed to `onOpportunityRefresh`
 * so the parent renders the server-computed `last_touch`.
 */
import { useState } from "react";
import type { JSX } from "react";

import { useActivities } from "./useActivities";
import { ActivityForm } from "./ActivityForm";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import { formatDateTime, formatRelativeTime } from "../../lib/format";
import type { Activity, Opportunity } from "../../types/domain";

export interface ActivityTimelineProps {
  readonly opportunityId: string;
  /**
   * Called with the server-updated opportunity immediately after a `log_activity` call commits.
   */
  readonly onOpportunityRefresh?: (opportunity: Opportunity) => void;
}

function ActivityRow({ activity }: { activity: Activity }): JSX.Element {
  return (
    /* The timeline's rail: a left border plus a marker dot. */
    <li
      data-activity-id={activity.activity_id}
      className="relative border-l-hairline border-border-default py-4 pl-6"
    >
      <span
        aria-hidden="true"
        className="absolute -left-1 top-6 h-2 w-2 rounded-pill bg-accent-emphasis"
      />
      <div className="flex flex-wrap items-baseline gap-3">
        <Badge variant="neutral" data-testid="activity-kind">
          {activity.kind}
        </Badge>
        {" — "}
        <strong className="font-semibold text-text-primary">{activity.subject}</strong>
      </div>
      {activity.body && (
        <p className="mt-2 max-w-prose text-sm leading-relaxed text-text-secondary">
          {activity.body}
        </p>
      )}
      {/* Absolute time in the `dateTime` attribute and the title, relative time on screen. */}
      <time
        dateTime={activity.occurred_at}
        title={formatDateTime(activity.occurred_at)}
        className="mt-2 block font-numeric text-xs text-text-muted"
      >
        {formatRelativeTime(activity.occurred_at)}
      </time>
    </li>
  );
}

export function ActivityTimeline({
  opportunityId,
  onOpportunityRefresh,
}: ActivityTimelineProps): JSX.Element {
  const { activities, loading, error, logActivity } = useActivities(opportunityId);
  const [showForm, setShowForm] = useState(false);

  if (error) {
    // A typed 403 scope denial must render distinctly from an empty timeline.
    if (error.kind === "scope_denied") {
      return (
        <div
          role="alert"
          data-testid="activities-scope-denied"
          className="max-w-prose space-y-2 rounded-surface border border-hairline border-denied-border bg-denied-surface px-5 py-4 text-denied-text"
        >
          <p className="text-sm leading-relaxed">
            You do not have access to activities for this opportunity.
          </p>
          {error.body.missingScope && (
            <p className="font-numeric text-xs">Missing scope: {error.body.missingScope}</p>
          )}
        </div>
      );
    }
    return (
      <div
        role="alert"
        className="max-w-prose rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-4 text-sm text-danger-text"
      >
        <p>Could not load activities: {error.body.message}</p>
      </div>
    );
  }

  return (
    <section aria-label="Activity timeline" className="space-y-4">
      <h3 className="text-md font-semibold tracking-tight text-text-primary">Activities</h3>
      {loading && activities.length === 0 ? (
        <div className="space-y-3">
          {[0, 1, 2].map((row) => (
            <Skeleton key={row} label="Loading activities" className="h-row-relaxed w-full" />
          ))}
        </div>
      ) : activities.length === 0 ? (
        <p
          data-testid="activities-empty"
          className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-5 py-6 text-sm text-empty-text"
        >
          No activities logged yet.
        </p>
      ) : (
        <ul className="pl-1">
          {activities.map((activity) => (
            <ActivityRow key={activity.activity_id} activity={activity} />
          ))}
        </ul>
      )}
      {showForm ? (
        <ActivityForm
          onSubmit={async (input) => {
            const result = await logActivity(input);
            onOpportunityRefresh?.(result.opportunity);
            setShowForm(false);
            return result;
          }}
          onCancel={() => setShowForm(false)}
        />
      ) : (
        <Button type="button" variant="secondary" size="sm" onClick={() => setShowForm(true)}>
          Log activity
        </Button>
      )}
    </section>
  );
}
