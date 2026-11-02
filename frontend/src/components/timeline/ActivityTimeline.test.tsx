// SPDX-License-Identifier: MIT
/**
 * Tests for the activity timeline component: rendering order and the empty state.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { Activity } from "../../types/domain";
import { ActivityTimeline } from "./ActivityTimeline";

function activity(overrides: Partial<Activity>): Activity {
  return {
    activity_id: "a1",
    opportunity_id: "opp-1",
    kind: "call",
    subject: "Call",
    occurred_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("ActivityTimeline", () => {
  afterEach(() => {
    cleanup();
  });

  it("renders an empty-state message when there are no activities", () => {
    render(<ActivityTimeline activities={[]} />);
    expect(screen.getByText(/no activity has been logged/i)).toBeInTheDocument();
  });

  it("renders most recent activity first", () => {
    render(
      <ActivityTimeline
        activities={[
          activity({ activity_id: "a1", subject: "Older", occurred_at: "2026-01-01T00:00:00Z" }),
          activity({ activity_id: "a2", subject: "Newer", occurred_at: "2026-02-01T00:00:00Z" }),
        ]}
      />,
    );

    const headings = screen.getAllByRole("heading", { level: 3 });
    expect(headings[0]).toHaveTextContent("Newer");
    expect(headings[1]).toHaveTextContent("Older");
  });
});
