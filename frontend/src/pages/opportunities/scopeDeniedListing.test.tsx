// SPDX-License-Identifier: MIT
/**
 * The listing pages render a 403 `scope_denied` / `account:*` body as a denial, never as the empty
 * state. Each page is driven through the real `CrmApiClient`, with `fetch` returning the handler's
 * wire envelope.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CrmApiClient } from "../../api/CrmApiClient";
import { AccountListPage } from "../accounts/AccountListPage";
import { PipelineBoard } from "../pipeline/PipelineBoard";
import { OpportunityListPage } from "./OpportunityListPage";

vi.mock("../../api/useCrmApi", () => {
  const client = new CrmApiClient("/api", { getAccessToken: () => Promise.resolve("token") });
  return { useCrmApi: () => client };
});

/** `crm.authz.require_listing_scope`'s denial, as `ScopeDenied.to_api_response` renders it. */
const EMPTY_BOOK_DENIAL = {
  statusCode: 403,
  error: {
    code: "scope_denied",
    message: "You do not have access to any accounts.",
    next_step: "tell_user",
    missing_scope: "account:*",
  },
};

const DENIED_TOOLS = new Set(["search_opportunities", "search_accounts", "pipeline_aggregate"]);
const calledTools: string[] = [];

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  calledTools.length = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      const tool = url.split("/").pop() ?? "";
      calledTools.push(tool);
      if (DENIED_TOOLS.has(tool)) return Promise.resolve(jsonResponse(403, EMPTY_BOOK_DENIAL));
      if (tool === "list_proposals") return Promise.resolve(jsonResponse(200, { proposals: [] }));
      return Promise.resolve(jsonResponse(500, {}));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe.each([
  { page: "OpportunityListPage", Page: OpportunityListPage, empty: /no opportunities matched/i },
  { page: "AccountListPage", Page: AccountListPage, empty: /no accounts matched/i },
])("$page with an empty book", ({ Page, empty }) => {
  it("renders ApiErrorBanner naming account:*, not the empty state", async () => {
    render(
      <MemoryRouter>
        <Page />
      </MemoryRouter>,
    );

    const banner = await screen.findByTestId("scope-denied-banner");
    expect(banner).toHaveAttribute("role", "alert");
    expect(banner).toHaveTextContent('"account:*"');
    expect(screen.queryByTestId("error-banner")).not.toBeInTheDocument();
    expect(screen.queryByText(empty)).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});

describe("PipelineBoard with an empty book", () => {
  it("renders its scope-denied panel naming account:*, with no empty columns", async () => {
    render(
      <MemoryRouter>
        <PipelineBoard />
      </MemoryRouter>,
    );

    const panel = await screen.findByTestId("pipeline-scope-denied");
    expect(panel).toHaveAttribute("role", "alert");
    expect(panel).toHaveTextContent("account:*");
    expect(calledTools).toContain("search_opportunities");
    expect(screen.queryAllByTestId(/^pipeline-column-empty-/)).toHaveLength(0);
    expect(screen.queryByRole("region", { name: "Pipeline board" })).not.toBeInTheDocument();
  });
});
