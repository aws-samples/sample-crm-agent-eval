// SPDX-License-Identifier: MIT
/**
 * Activity timeline for an opportunity detail view: a semantic `<ol>`, most recent first, with each
 * activity's kind, subject, and timestamp under a per-item heading.
 */
import type { JSX } from "react";

import { Badge } from "../ui/badge";
import { formatDateTime, formatRelativeTime } from "../../lib/format";
import type { Activity } from "../../types/domain";

const KIND_LABELS: Record<Activity["kind"], string> = {
  call: "Call",
  email: "Email",
  meeting: "Meeting",
  note: "Note",
  stage_change: "Stage change",
  finding: "Finding",
};

export function ActivityTimeline({ activities }: { activities: readonly Activity[] }): JSX.Element {
  if (activities.length === 0) {
    /* `--empty-*`: an empty timeline is styled distinct from an error and from a scope denial. */
    return (
      <p className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-5 py-6 text-sm text-empty-text">
        No activity has been logged for this opportunity yet.
      </p>
    );
  }

  const sorted = [...activities].sort((a, b) => b.occurred_at.localeCompare(a.occurred_at));

  return (
    <ol aria-label="Activity timeline" className="pl-1">
      {sorted.map((activity) => (
        <li
          key={activity.activity_id}
          /* A rail plus a marker dot, so chronology is structural rather than carried by spacing. */
          className="relative border-l-hairline border-border-default py-4 pl-6"
        >
          <span
            aria-hidden="true"
            className="absolute -left-1 top-6 h-2 w-2 rounded-pill bg-accent-emphasis"
          />
          <article aria-labelledby={`activity-${activity.activity_id}-subject`} className="space-y-2">
            <h3
              id={`activity-${activity.activity_id}-subject`}
              className="flex flex-wrap items-baseline gap-2 text-sm font-semibold text-text-primary"
            >
              <Badge variant="neutral">{KIND_LABELS[activity.kind]}</Badge>
              {activity.subject}
            </h3>
            <p>
              {/* Relative time on screen, machine value in `dateTime`, absolute in `title`. */}
              <time
                dateTime={activity.occurred_at}
                title={formatDateTime(activity.occurred_at)}
                className="font-numeric text-xs text-text-muted"
              >
                {formatRelativeTime(activity.occurred_at)}
              </time>
            </p>
            {activity.body && (
              <p className="max-w-prose text-sm leading-relaxed text-text-secondary">
                {activity.body}
              </p>
            )}
          </article>
        </li>
      ))}
    </ol>
  );
}
