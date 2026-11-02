// SPDX-License-Identifier: MIT
/**
 * AccountDetailPage error precedence: when both `get_account` and `search_opportunities` fail, the
 * `get_account` outcome is rendered whichever request settles first. `useCrmApi` is mocked at the
 * module boundary.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { act } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CrmApiError } from "../../api/CrmApiClient";
import { AccountDetailPage } from "./AccountDetailPage";

const mockApi = {
  getAccount: vi.fn(),
  searchOpportunities: vi.fn(),
  submitProposal: vi.fn(),
};

vi.mock("../../api/useCrmApi", () => ({
  useCrmApi: () => mockApi,
}));

interface Deferred {
  readonly promise: Promise<unknown>;
  readonly reject: (reason: unknown) => void;
}

function deferred(): Deferred {
  let reject: (reason: unknown) => void = () => undefined;
  const promise = new Promise<unknown>((_resolve, rejectFn) => {
    reject = rejectFn;
  });
  return { promise, reject };
}

const ACCOUNT_ID = "acct-9";

const notFound = (): CrmApiError =>
  new CrmApiError("not_found", 404, { message: "Account acct-9 was not found." });

const accountScopeDenied = (): CrmApiError =>
  new CrmApiError("scope_denied", 403, {
    message: "not in your book",
    missingScope: `account:${ACCOUNT_ID}`,
  });

const searchScopeDenied = (): CrmApiError =>
  new CrmApiError("scope_denied", 403, {
    message: "not in your book",
    missingScope: `account:${ACCOUNT_ID}`,
  });

const searchInternalError = (): CrmApiError =>
  new CrmApiError("unknown", 500, { message: "Opportunity search failed." });

function renderPage(): void {
  render(
    <MemoryRouter initialEntries={[`/accounts/${ACCOUNT_ID}`]}>
      <Routes>
        <Route path="/accounts/:accountId" element={<AccountDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function settle(first: Deferred, firstError: unknown, second: Deferred, secondError: unknown) {
  await act(async () => {
    first.reject(firstError);
    await Promise.resolve();
  });
  await act(async () => {
    second.reject(secondError);
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
});

type Order = "account settles first" | "search settles first";
const ORDERS: readonly Order[] = ["account settles first", "search settles first"];

async function renderWithOrder(order: Order, accountError: unknown, searchError: unknown) {
  const account = deferred();
  const search = deferred();
  mockApi.getAccount.mockReturnValue(account.promise);
  mockApi.searchOpportunities.mockReturnValue(search.promise);
  renderPage();
  if (order === "account settles first") {
    await settle(account, accountError, search, searchError);
  } else {
    await settle(search, searchError, account, accountError);
  }
}

describe("AccountDetailPage error precedence", () => {
  it.each(ORDERS)(
    "a nonexistent account renders get_account's not-found when the %s",
    async (order) => {
      await renderWithOrder(order, notFound(), searchScopeDenied());

      expect(await screen.findByRole("alert")).toHaveTextContent("Account acct-9 was not found.");
      expect(screen.queryByText(/don't have access/)).not.toBeInTheDocument();
    },
  );

  it.each(ORDERS)(
    "an out-of-book account renders get_account's scope denial when the %s",
    async (order) => {
      await renderWithOrder(order, accountScopeDenied(), searchInternalError());

      expect(await screen.findByText(/don't have access/)).toBeInTheDocument();
      expect(screen.getByText(`account:${ACCOUNT_ID}`)).toBeInTheDocument();
      expect(screen.queryByText("Opportunity search failed.")).not.toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    },
  );
});
