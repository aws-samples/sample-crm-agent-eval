// SPDX-License-Identifier: MIT
/**
 * Tests for opportunity creation: a low-risk direct write with no proposal involved.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OpportunityCreatePage } from "./OpportunityCreatePage";

const createOpportunity = vi.fn();
const navigate = vi.fn();

vi.mock("../../api/useCrmApi", () => ({
  useCrmApi: () => ({
    createOpportunity: (...args: unknown[]) => createOpportunity(...args),
  }),
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => navigate };
});

describe("OpportunityCreatePage", () => {
  afterEach(() => {
    cleanup();
    createOpportunity.mockReset();
    navigate.mockReset();
  });

  it("submits a low-risk create request directly", async () => {
    createOpportunity.mockResolvedValue({ opportunity: { opportunity_id: "opp-new" } });

    render(
      <MemoryRouter>
        <OpportunityCreatePage />
      </MemoryRouter>,
    );

    await userEvent.type(screen.getByLabelText(/^name$/i), "New deal");
    await userEvent.type(screen.getByLabelText(/account id/i), "acct-1");
    await userEvent.click(screen.getByRole("button", { name: /create opportunity/i }));

    await waitFor(() => expect(createOpportunity).toHaveBeenCalledTimes(1));
    expect(createOpportunity).toHaveBeenCalledWith(
      expect.objectContaining({ name: "New deal", account_id: "acct-1", stage: "Discovery" }),
    );
    // `create_opportunity` returns `{opportunity: {...}}`; navigation uses the nested id.
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/opportunities/opp-new"));
  });
});
