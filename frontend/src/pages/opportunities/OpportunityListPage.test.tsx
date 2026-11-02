// SPDX-License-Identifier: MIT
/**
 * Tests for the opportunities list page: search/filter interaction, truncation disclosure,
 * scope-denial rendering distinct from empty results, and empty-state text.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CrmApiError } from "../../api/CrmApiClient";
import { OpportunityListPage } from "./OpportunityListPage";

const searchOpportunities = vi.fn();

vi.mock("../../api/useCrmApi", () => ({
  useCrmApi: () => ({
    searchOpportunities: (...args: unknown[]) => searchOpportunities(...args),
  }),
}));

function renderPage(): void {
  render(
    <MemoryRouter>
      <OpportunityListPage />
    </MemoryRouter>,
  );
}

describe("OpportunityListPage", () => {
  afterEach(() => {
    cleanup();
    searchOpportunities.mockReset();
  });

  it("renders search results", async () => {
    searchOpportunities.mockResolvedValue({
      items: [
        {
          opportunity_id: "opp-1",
          account_id: "acct-1",
          name: "Acme deal",
          value: 1000,
          stage: "Discovery",
          owner_rep_id: "rep-1",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        },
      ],
      truncated: false,
      examined_count: 1,
      next_cursor: null,
    });

    renderPage();

    expect(await screen.findByText("Acme deal")).toBeInTheDocument();
    expect(screen.queryByTestId("truncation-notice")).not.toBeInTheDocument();
  });

  it("surfaces truncation explicitly rather than silently capping results", async () => {
    searchOpportunities.mockResolvedValue({
      items: [],
      truncated: true,
      examined_count: 2000,
      next_cursor: "cursor-abc",
    });

    renderPage();

    const notice = await screen.findByTestId("truncation-notice");
    expect(notice).toHaveTextContent("2,000");
    expect(notice).toHaveTextContent(/incomplete/i);
    expect(screen.getByRole("button", { name: /load more/i })).toBeInTheDocument();
  });

  it("does not show a load-more control when truncated but no cursor remains", async () => {
    searchOpportunities.mockResolvedValue({
      items: [],
      truncated: true,
      examined_count: 500,
      next_cursor: null,
    });

    renderPage();

    await screen.findByTestId("truncation-notice");
    expect(screen.queryByRole("button", { name: /load more/i })).not.toBeInTheDocument();
  });

  it("renders an empty-state message distinct from an error when there are no matches", async () => {
    searchOpportunities.mockResolvedValue({
      items: [],
      truncated: false,
      examined_count: 0,
      next_cursor: null,
    });

    renderPage();

    expect(await screen.findByText(/no opportunities matched/i)).toBeInTheDocument();
    expect(screen.queryByTestId("error-banner")).not.toBeInTheDocument();
    expect(screen.queryByTestId("scope-denied-banner")).not.toBeInTheDocument();
  });

  it("renders a scope-denial distinctly from an empty result", async () => {
    searchOpportunities.mockRejectedValue(
      new CrmApiError("scope_denied", 403, { message: "not in your book", missingScope: "account:acct-9" }),
    );

    renderPage();

    const banner = await screen.findByTestId("scope-denied-banner");
    expect(banner).toHaveTextContent(/not permitted/i);
    expect(banner).toHaveTextContent("account:acct-9");
    expect(screen.queryByText(/no opportunities matched/i)).not.toBeInTheDocument();
  });

  it("re-runs search with filters on submit", async () => {
    searchOpportunities.mockResolvedValue({
      items: [],
      truncated: false,
      examined_count: 0,
      next_cursor: null,
    });

    renderPage();
    await waitFor(() => expect(searchOpportunities).toHaveBeenCalledTimes(1));

    await userEvent.type(screen.getByLabelText(/search by name/i), "acme");
    await userEvent.selectOptions(screen.getByLabelText(/^stage$/i), "Negotiation");
    await userEvent.click(screen.getByRole("button", { name: /^search$/i }));

    await waitFor(() => expect(searchOpportunities).toHaveBeenCalledTimes(2));
    expect(searchOpportunities).toHaveBeenLastCalledWith(
      expect.objectContaining({ query: "acme", stage: "Negotiation" }),
    );
  });
});
