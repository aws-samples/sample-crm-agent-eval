// SPDX-License-Identifier: MIT
/**
 * Tests for the accounts and contacts CRUD surfaces, including the delete-routes-to-proposal path
 * and cascade visibility. `useCrmApi` is mocked at the module boundary.
 */
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import { CrmApiError } from "../../api/CrmApiClient";
import { AccountDetailPage } from "./AccountDetailPage";
import { AccountCreatePage } from "./AccountCreatePage";
import { AccountEditPage } from "./AccountEditPage";

const mockApi = {
  getAccount: vi.fn(),
  searchOpportunities: vi.fn(),
  createAccount: vi.fn(),
  updateAccount: vi.fn(),
  submitProposal: vi.fn(),
};

vi.mock("../../api/useCrmApi", () => ({
  useCrmApi: () => mockApi,
}));

function renderAtAccount(accountId: string) {
  return render(
    <MemoryRouter initialEntries={[`/accounts/${accountId}`]}>
      <Routes>
        <Route path="/accounts/:accountId" element={<AccountDetailPage />} />
        <Route path="/accounts/:accountId/edit" element={<AccountEditPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

const ACCOUNT = {
  account_id: "acct-1",
  name: "Acme Corp",
  industry: "Software",
  region: "NA",
  website: "acme.example",
  employee_count: 500,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const CONTACT = {
  contact_id: "contact-1",
  account_id: "acct-1",
  name: "Jane Doe",
  status: "active" as const,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
});

// `globals: false` in vite.config.ts disables RTL's automatic DOM cleanup, so it is registered here.
afterEach(() => {
  cleanup();
});

describe("AccountDetailPage", () => {
  it("renders account details and its contacts", async () => {
    mockApi.getAccount.mockResolvedValue({ account: ACCOUNT, contacts: [CONTACT] });
    mockApi.searchOpportunities.mockResolvedValue({ items: [] });

    renderAtAccount("acct-1");

    expect(await screen.findByRole("heading", { name: "Acme Corp" })).toBeInTheDocument();
    expect(screen.getByText("Jane Doe")).toBeInTheDocument();
  });

  it("surfaces open opportunities and withholds the delete action", async () => {
    // `search_opportunities` returns `{items: [...]}`; the open-opportunity banner reads `items`.
    mockApi.getAccount.mockResolvedValue({ account: ACCOUNT, contacts: [] });
    mockApi.searchOpportunities.mockResolvedValue({
      items: [
        {
          opportunity_id: "opp-1",
          account_id: "acct-1",
          name: "Big Deal",
          value: 1000,
          stage: "Discovery",
          owner_rep_id: "rep-1",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        },
      ],
    });

    renderAtAccount("acct-1");

    expect(await screen.findByText(/has 1 open opportunity/)).toBeInTheDocument();
    expect(screen.getByText(/Big Deal/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete Acme Corp/ })).not.toBeInTheDocument();
  });

  it("lists the account's opportunities in the shared table", async () => {
    mockApi.getAccount.mockResolvedValue({ account: ACCOUNT, contacts: [] });
    mockApi.searchOpportunities.mockResolvedValue({
      items: [
        {
          opportunity_id: "opp-1",
          account_id: "acct-1",
          name: "Big Deal",
          value: 1000,
          stage: "Discovery",
          owner_rep_id: "rep-1",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        },
        {
          opportunity_id: "opp-2",
          account_id: "acct-1",
          name: "Old Deal",
          value: 500,
          stage: "Closed Lost",
          owner_rep_id: "rep-2",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        },
      ],
      truncated: false,
      examined_count: 2,
      next_cursor: null,
    });

    renderAtAccount("acct-1");

    expect(await screen.findByRole("heading", { name: "Opportunities" })).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Opportunities for Acme Corp" });
    expect(table).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Big Deal" })).toHaveAttribute(
      "href",
      "/opportunities/opp-1",
    );
    expect(screen.getByRole("link", { name: "Old Deal" })).toBeInTheDocument();
    expect(mockApi.searchOpportunities).toHaveBeenCalledWith({ account_id: "acct-1" });
  });

  it("shows an empty state when the account has no opportunities", async () => {
    mockApi.getAccount.mockResolvedValue({ account: ACCOUNT, contacts: [] });
    mockApi.searchOpportunities.mockResolvedValue({ items: [] });

    renderAtAccount("acct-1");

    expect(await screen.findByText("No opportunities yet.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("keeps the open-opportunity count in the cascade warning but not the names", async () => {
    mockApi.getAccount.mockResolvedValue({ account: ACCOUNT, contacts: [] });
    mockApi.searchOpportunities.mockResolvedValue({
      items: ["opp-1", "opp-2"].map((id) => ({
        opportunity_id: id,
        account_id: "acct-1",
        name: `Deal ${id}`,
        value: 1000,
        stage: "Discovery",
        owner_rep_id: "rep-1",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      })),
    });

    renderAtAccount("acct-1");

    const warning = await screen.findByText(/has 2 open opportunities/);
    expect(warning).not.toHaveTextContent("Deal opp-1");
    expect(warning).not.toHaveTextContent("Deal opp-2");
  });

  it("surfaces a truncated opportunity search on the account page", async () => {
    mockApi.getAccount.mockResolvedValue({ account: ACCOUNT, contacts: [] });
    mockApi.searchOpportunities.mockResolvedValue({
      items: [
        {
          opportunity_id: "opp-1",
          account_id: "acct-1",
          name: "Big Deal",
          value: 1000,
          stage: "Closed Won",
          owner_rep_id: "rep-1",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        },
      ],
      truncated: true,
      examined_count: 2000,
      next_cursor: "c1",
    });

    renderAtAccount("acct-1");

    expect(await screen.findByTestId("truncation-notice")).toBeInTheDocument();
    expect(
      screen.getByRole("table", {
        name: "Opportunities for Acme Corp (incomplete — see notice above)",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("offers delete (as a proposal) when there are no open opportunities", async () => {
    mockApi.getAccount.mockResolvedValue({ account: ACCOUNT, contacts: [] });
    mockApi.searchOpportunities.mockResolvedValue({ items: [] });

    renderAtAccount("acct-1");

    const deleteButton = await screen.findByRole("button", { name: /Delete Acme Corp/ });
    expect(deleteButton).toBeInTheDocument();
  });

  it("submits a delete proposal rather than deleting directly", async () => {
    mockApi.getAccount.mockResolvedValue({ account: ACCOUNT, contacts: [] });
    mockApi.searchOpportunities.mockResolvedValue({ items: [] });
    // `submit_proposal` returns `{proposal: {...}}`; the rendered id is read from the nested proposal.
    mockApi.submitProposal.mockResolvedValue({ proposal: { proposal_id: "prop-99" } });

    const user = userEvent.setup();
    renderAtAccount("acct-1");

    const deleteButton = await screen.findByRole("button", { name: /Delete Acme Corp/ });
    await user.click(deleteButton);
    const confirmButton = await screen.findByRole("button", { name: /Confirm delete proposal/ });
    await user.click(confirmButton);

    await waitFor(() => {
      expect(mockApi.submitProposal).toHaveBeenCalledWith(
        expect.objectContaining({
          target_table: "accounts",
          target_id: "acct-1",
          operation: "delete",
        }),
      );
    });
    expect(await screen.findByText(/proposal prop-99/)).toBeInTheDocument();
    expect(await screen.findByText(/Awaiting approval/)).toBeInTheDocument();
  });

  it("renders a scope-denial clearly, distinct from an empty state", async () => {
    mockApi.getAccount.mockRejectedValue(
      new CrmApiError("scope_denied", 403, {
        message: "not in your book",
        missingScope: "account:acct-1",
      }),
    );
    mockApi.searchOpportunities.mockResolvedValue({ items: [] });

    renderAtAccount("acct-1");

    expect(await screen.findByText(/don't have access/)).toBeInTheDocument();
    expect(screen.getByText(/account:acct-1/)).toBeInTheDocument();
  });
});

describe("AccountCreatePage", () => {
  it("creates an account and navigates to the new record's own page", async () => {
    // `create_account` returns `{account: {...}}`; navigation uses the nested account id.
    mockApi.createAccount.mockResolvedValue({ account: { account_id: "acct-new" } });
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/accounts/new"]}>
        <Routes>
          <Route path="/accounts/new" element={<AccountCreatePage />} />
          <Route path="/accounts/:accountId" element={<p>Account detail: acct-new</p>} />
        </Routes>
      </MemoryRouter>,
    );

    const nameInput = screen.getByLabelText("Name");
    await user.type(nameInput, "New Co");
    await user.click(screen.getByRole("button", { name: /Create account/ }));

    await waitFor(() => {
      expect(mockApi.createAccount).toHaveBeenCalledWith(
        expect.objectContaining({ name: "New Co" }),
      );
    });
    expect(await screen.findByText("Account detail: acct-new")).toBeInTheDocument();
  });
});

describe("AccountEditPage", () => {
  it("loads the existing account into the form and saves an update", async () => {
    mockApi.getAccount.mockResolvedValue({ account: ACCOUNT, contacts: [] });
    mockApi.updateAccount.mockResolvedValue({});
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/accounts/acct-1/edit"]}>
        <Routes>
          <Route path="/accounts/:accountId/edit" element={<AccountEditPage />} />
        </Routes>
      </MemoryRouter>,
    );

    const nameInput = await screen.findByLabelText("Name");
    expect(nameInput).toHaveValue("Acme Corp");

    await user.clear(nameInput);
    await user.type(nameInput, "Acme Corp Renamed");
    await user.click(screen.getByRole("button", { name: /Save changes/ }));

    await waitFor(() => {
      expect(mockApi.updateAccount).toHaveBeenCalledWith(
        expect.objectContaining({
          account_id: "acct-1",
          fields: expect.objectContaining({ name: "Acme Corp Renamed" }),
        }),
      );
    });
  });
});
