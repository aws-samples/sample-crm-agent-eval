// SPDX-License-Identifier: MIT
/**
 * Tests for the activities surface: logging an activity, reflecting the server-computed
 * `last_touch`, and the typed 403 vs empty-timeline distinction.
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MemoryRouter } from "react-router-dom";

import { ActivityTimeline } from "./ActivityTimeline";
import { CrmApiError } from "../../api/CrmApiClient";
import * as useCrmApiModule from "../../api/useCrmApi";

function mockApi(overrides: Partial<Record<string, (...args: unknown[]) => unknown>> = {}) {
  const api = {
    listActivities: vi.fn().mockResolvedValue({ activities: [], next_cursor: null }),
    logActivity: vi.fn(),
    updateActivity: vi.fn(),
    ...overrides,
  };
  vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue(api as never);
  return api;
}

/**
 * The timeline inside a router; `useActivities` stamps its panel report with the current pathname.
 */
function renderTimeline(opportunityId = "opp-1") {
  return render(
    <MemoryRouter initialEntries={[`/opportunities/${opportunityId}/activities`]}>
      <ActivityTimeline opportunityId={opportunityId} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ActivityTimeline", () => {
  it("renders an empty state distinctly when there are no activities", async () => {
    mockApi();
    renderTimeline();
    expect(await screen.findByTestId("activities-empty")).toBeInTheDocument();
  });

  it("renders a typed 403 scope denial distinctly from an empty timeline", async () => {
    mockApi({
      listActivities: vi.fn().mockRejectedValue(
        new CrmApiError("scope_denied", 403, {
          message: "not in your book",
          missingScope: "account:acct-9",
        }),
      ),
    });
    renderTimeline();
    const denial = await screen.findByTestId("activities-scope-denied");
    expect(denial).toBeInTheDocument();
    expect(screen.queryByTestId("activities-empty")).not.toBeInTheDocument();
    expect(screen.getByText(/account:acct-9/)).toBeInTheDocument();
  });

  it("logs an activity and reflects the server-returned opportunity's last_touch", async () => {
    const serverOpportunity = {
      opportunity_id: "opp-1",
      account_id: "acct-1",
      name: "Big Deal",
      value: 1000,
      stage: "Discovery",
      owner_rep_id: "rep-1",
      last_touch: "2026-05-01T00:00:00Z",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-05-01T00:00:00Z",
    };
    const serverActivity = {
      activity_id: "act-1",
      opportunity_id: "opp-1",
      kind: "call",
      subject: "Checked in",
      occurred_at: "2026-05-01T00:00:00Z",
      created_at: "2026-05-01T00:00:00Z",
      updated_at: "2026-05-01T00:00:00Z",
    };
    const onOpportunityRefresh = vi.fn();
    mockApi({
      logActivity: vi.fn().mockResolvedValue({
        activity: serverActivity,
        opportunity: serverOpportunity,
      }),
    });

    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/opportunities/opp-1/activities"]}>
        <ActivityTimeline opportunityId="opp-1" onOpportunityRefresh={onOpportunityRefresh} />
      </MemoryRouter>,
    );

    await screen.findByTestId("activities-empty");
    await user.click(screen.getByRole("button", { name: "Log activity" }));
    await user.type(screen.getByLabelText("Subject"), "Checked in");
    await user.click(screen.getByRole("button", { name: "Log activity" }));

    await waitFor(() => {
      expect(onOpportunityRefresh).toHaveBeenCalledWith(serverOpportunity);
    });
    // The client never recomputes last_touch — it only ever renders what the server returned.
    expect(onOpportunityRefresh.mock.calls[0][0].last_touch).toBe("2026-05-01T00:00:00Z");
  });
});
