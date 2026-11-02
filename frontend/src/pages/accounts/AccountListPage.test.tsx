// SPDX-License-Identifier: MIT
/**
 * Tests for the accounts list page: search/owner-filter interaction, cursor pagination, truncation
 * disclosure, scope-denial rendering distinct from empty results, empty-state text, and the view
 * descriptor reported to the assistant panel.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CrmApiError } from "../../api/CrmApiClient";
import { AccountListPage } from "./AccountListPage";

const searchAccounts = vi.fn();
const setViewSnapshot = vi.fn();

vi.mock("../../api/useCrmApi", () => ({
  useCrmApi: () => ({
    searchAccounts: (...args: unknown[]) => searchAccounts(...args),
  }),
}));

// Capture what the page publishes to the assistant panel without pulling in the whole snapshot store.
vi.mock("../../components/AssistantPanel/viewReport", () => ({
  setViewSnapshot: (...args: unknown[]) => setViewSnapshot(...args),
  clearViewSnapshot: () => {},
}));

function renderPage(): void {
  render(
    <MemoryRouter>
      <AccountListPage />
    </MemoryRouter>,
  );
}

describe("AccountListPage", () => {
  afterEach(() => {
    cleanup();
    searchAccounts.mockReset();
    setViewSnapshot.mockReset();
  });

  it("renders account rows from the client", async () => {
    searchAccounts.mockResolvedValue({
      items: [{ account_id: "acct-1", name: "Acme Corp", tenant: "demo", owner_rep_id: "rep-1" }],
      truncated: false,
      examined_count: 1,
      next_cursor: null,
    });

    renderPage();

    const link = await screen.findByRole("link", { name: "Acme Corp" });
    expect(link).toHaveAttribute("href", "/accounts/acct-1");
    expect(screen.getByText("rep-1")).toBeInTheDocument();
    expect(screen.queryByTestId("truncation-notice")).not.toBeInTheDocument();
  });

  it("renders an em dash for an owner-less row (owner not projected on the tenant path)", async () => {
    searchAccounts.mockResolvedValue({
      items: [{ account_id: "acct-2", name: "Beta Inc", tenant: "demo" }],
      truncated: false,
      examined_count: 1,
      next_cursor: null,
    });

    renderPage();

    expect(await screen.findByRole("link", { name: "Beta Inc" })).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("re-runs the search with the name and owner filters on submit", async () => {
    searchAccounts.mockResolvedValue({
      items: [],
      truncated: false,
      examined_count: 0,
      next_cursor: null,
    });

    renderPage();
    await waitFor(() => expect(searchAccounts).toHaveBeenCalledTimes(1));

    await userEvent.type(screen.getByLabelText(/search by name/i), "acme");
    await userEvent.type(screen.getByLabelText(/owner rep id/i), "rep-7");
    await userEvent.click(screen.getByRole("button", { name: /^search$/i }));

    await waitFor(() => expect(searchAccounts).toHaveBeenCalledTimes(2));
    expect(searchAccounts).toHaveBeenLastCalledWith(
      expect.objectContaining({ query: "acme", owner_rep_id: "rep-7" }),
    );
  });

  it("advances the cursor when loading more truncated results", async () => {
    searchAccounts
      .mockResolvedValueOnce({
        items: [{ account_id: "acct-1", name: "Acme Corp", tenant: "demo" }],
        truncated: true,
        examined_count: 2000,
        next_cursor: "cursor-abc",
      })
      .mockResolvedValueOnce({
        items: [{ account_id: "acct-2", name: "Beta Inc", tenant: "demo" }],
        truncated: false,
        examined_count: 1,
        next_cursor: null,
      });

    renderPage();

    await screen.findByRole("link", { name: "Acme Corp" });
    await userEvent.click(screen.getByRole("button", { name: /load more/i }));

    await waitFor(() => expect(searchAccounts).toHaveBeenCalledTimes(2));
    expect(searchAccounts).toHaveBeenLastCalledWith(
      expect.objectContaining({ cursor: "cursor-abc" }),
    );
    // Both pages are shown, appended in order.
    expect(await screen.findByRole("link", { name: "Beta Inc" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Acme Corp" })).toBeInTheDocument();
  });

  it("surfaces truncation explicitly rather than silently capping results", async () => {
    searchAccounts.mockResolvedValue({
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

  it("renders an empty-state message distinct from an error when there are no matches", async () => {
    searchAccounts.mockResolvedValue({
      items: [],
      truncated: false,
      examined_count: 0,
      next_cursor: null,
    });

    renderPage();

    expect(await screen.findByText(/no accounts matched/i)).toBeInTheDocument();
    expect(screen.queryByTestId("error-banner")).not.toBeInTheDocument();
    expect(screen.queryByTestId("scope-denied-banner")).not.toBeInTheDocument();
  });

  it("renders a scope-denial distinctly from an empty result", async () => {
    searchAccounts.mockRejectedValue(
      new CrmApiError("scope_denied", 403, { message: "not in your book", missingScope: "account:acct-9" }),
    );

    renderPage();

    const banner = await screen.findByTestId("scope-denied-banner");
    expect(banner).toHaveTextContent(/not permitted/i);
    expect(banner).toHaveTextContent("account:acct-9");
    expect(screen.queryByText(/no accounts matched/i)).not.toBeInTheDocument();
  });

  it("reports the applied filters and row count to the assistant panel", async () => {
    searchAccounts.mockResolvedValue({
      items: [{ account_id: "acct-1", name: "Acme Corp", tenant: "demo", owner_rep_id: "rep-1" }],
      truncated: false,
      examined_count: 1,
      next_cursor: null,
    });

    renderPage();
    await screen.findByRole("link", { name: "Acme Corp" });

    await waitFor(() =>
      expect(setViewSnapshot).toHaveBeenLastCalledWith(
        expect.objectContaining({
          route: "/",
          report: { query: { filters: {} } },
          rowCount: 1,
          truncated: false,
        }),
      ),
    );
  });
});
