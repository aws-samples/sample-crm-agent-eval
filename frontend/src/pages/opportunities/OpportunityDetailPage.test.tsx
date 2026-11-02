// SPDX-License-Identifier: MIT
/**
 * Tests for the opportunity detail page: low-risk fields commit via `update_opportunity`, while
 * high-risk fields and delete route exclusively through `submit_proposal`.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CrmApiError } from "../../api/CrmApiClient";
import { OpportunityDetailPage } from "./OpportunityDetailPage";

const getOpportunity = vi.fn();
const updateOpportunity = vi.fn();
const submitProposal = vi.fn();
const logActivity = vi.fn();

vi.mock("../../api/useCrmApi", () => ({
  useCrmApi: () => ({
    getOpportunity: (...args: unknown[]) => getOpportunity(...args),
    updateOpportunity: (...args: unknown[]) => updateOpportunity(...args),
    submitProposal: (...args: unknown[]) => submitProposal(...args),
    logActivity: (...args: unknown[]) => logActivity(...args),
  }),
}));

const baseOpportunity = {
  opportunity_id: "opp-1",
  account_id: "acct-1",
  name: "Acme deal",
  value: 1000,
  stage: "Discovery" as const,
  owner_rep_id: "rep-1",
  close_date: "2026-06-01",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function renderDetail(): void {
  render(
    <MemoryRouter initialEntries={["/opportunities/opp-1"]}>
      <Routes>
        <Route path="/opportunities/:opportunityId" element={<OpportunityDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("OpportunityDetailPage", () => {
  afterEach(() => {
    cleanup();
    getOpportunity.mockReset();
    updateOpportunity.mockReset();
    submitProposal.mockReset();
    logActivity.mockReset();
  });

  it("renders notes, timeline, and contacts", async () => {
    getOpportunity.mockResolvedValue({
      opportunity: baseOpportunity,
      notes: [{ note_id: "n1", opportunity_id: "opp-1", body: "Great call", created_at: "2026-01-01T00:00:00Z" }],
      activities: [
        {
          activity_id: "a1",
          opportunity_id: "opp-1",
          kind: "call",
          subject: "Intro call",
          occurred_at: "2026-01-02T00:00:00Z",
          created_at: "2026-01-02T00:00:00Z",
          updated_at: "2026-01-02T00:00:00Z",
        },
      ],
      contacts: [
        { contact_id: "c1", account_id: "acct-1", name: "Jane Doe", status: "active", created_at: "x", updated_at: "x" },
      ],
    });

    renderDetail();

    expect(await screen.findByRole("heading", { name: "Acme deal" })).toBeInTheDocument();
    expect(screen.getByText("Great call")).toBeInTheDocument();
    expect(screen.getByText(/Intro call/)).toBeInTheDocument();
    const contactsSection = screen.getByRole("region", { name: /related contacts/i });
    expect(within(contactsSection).getByText(/Jane Doe/)).toBeInTheDocument();
  });

  it("commits a low-risk field edit directly via update_opportunity, never submit_proposal", async () => {
    getOpportunity.mockResolvedValue({
      opportunity: baseOpportunity,
      notes: [],
      activities: [],
      contacts: [],
    });
    updateOpportunity.mockResolvedValue({});

    renderDetail();
    await screen.findByRole("heading", { name: "Acme deal" });

    const nameInput = screen.getByLabelText(/^name$/i);
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, "Acme deal (renamed)");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(updateOpportunity).toHaveBeenCalledTimes(1));
    expect(updateOpportunity).toHaveBeenCalledWith(
      expect.objectContaining({
        opportunity_id: "opp-1",
        fields: { name: "Acme deal (renamed)" },
      }),
    );
    expect(submitProposal).not.toHaveBeenCalled();
  });

  it("routes a high-risk stage change through submit_proposal, never update_opportunity", async () => {
    getOpportunity.mockResolvedValue({
      opportunity: baseOpportunity,
      notes: [],
      activities: [],
      contacts: [],
    });
    submitProposal.mockResolvedValue({ proposal_id: "prop-1" });

    renderDetail();
    await screen.findByRole("heading", { name: "Acme deal" });

    await userEvent.selectOptions(screen.getByLabelText(/^stage$/i), "Negotiation");
    await userEvent.type(screen.getByLabelText(/^reason$/i), "Customer confirmed budget");
    await userEvent.click(screen.getByRole("button", { name: /submit proposal/i }));

    await waitFor(() => expect(submitProposal).toHaveBeenCalledTimes(1));
    expect(submitProposal).toHaveBeenCalledWith(
      expect.objectContaining({
        target_table: "opportunities",
        target_id: "opp-1",
        operation: "update",
        after_payload: { stage: "Negotiation" },
      }),
    );
    expect(updateOpportunity).not.toHaveBeenCalled();
    expect(await screen.findByText(/pending approval/i)).toBeInTheDocument();
  });

  it("routes delete through submit_proposal with operation delete", async () => {
    getOpportunity.mockResolvedValue({
      opportunity: baseOpportunity,
      notes: [],
      activities: [],
      contacts: [],
    });
    submitProposal.mockResolvedValue({ proposal_id: "prop-2" });

    renderDetail();
    await screen.findByRole("heading", { name: "Acme deal" });

    await userEvent.type(screen.getByLabelText(/reason for deletion/i), "Duplicate record");
    await userEvent.click(screen.getByRole("button", { name: /propose delete/i }));

    await waitFor(() => expect(submitProposal).toHaveBeenCalledTimes(1));
    expect(submitProposal).toHaveBeenCalledWith(
      expect.objectContaining({
        target_table: "opportunities",
        target_id: "opp-1",
        operation: "delete",
      }),
    );
  });

  it("renders a scope-denial banner distinctly from a not-found state", async () => {
    getOpportunity.mockRejectedValue(
      new CrmApiError("scope_denied", 403, { message: "denied", missingScope: "account:acct-1" }),
    );

    renderDetail();

    const banner = await screen.findByTestId("scope-denied-banner");
    expect(banner).toHaveTextContent(/not permitted/i);
    expect(screen.queryByText(/could not be found/i)).not.toBeInTheDocument();
  });
});
