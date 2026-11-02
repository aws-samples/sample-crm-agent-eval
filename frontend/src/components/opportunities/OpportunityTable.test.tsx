// SPDX-License-Identifier: MIT
/**
 * Tests for the shared opportunities table: one linked row per opportunity, the five
 * columns, stage badges, and the truncation caption.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import type { Opportunity } from "../../types/domain";
import { OpportunityTable } from "./OpportunityTable";

const BASE = {
  account_id: "acct-1",
  owner_rep_id: "rep-7",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
} as const;

const ITEMS: readonly Opportunity[] = [
  { ...BASE, opportunity_id: "opp-1", name: "Renewal", value: 12000, stage: "Discovery" },
  { ...BASE, opportunity_id: "opp-2", name: "Expansion", value: 5000, stage: "Closed Won" },
];

function renderTable(props: Partial<Parameters<typeof OpportunityTable>[0]> = {}) {
  return render(
    <MemoryRouter>
      <OpportunityTable items={ITEMS} caption="Opportunity results" {...props} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
});

describe("OpportunityTable", () => {
  it("renders the five column headers and one row per opportunity", () => {
    renderTable();
    const table = screen.getByRole("table", { name: "Opportunity results" });
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((cell) => cell.textContent);
    expect(headers).toEqual(["Name", "Stage", "Value", "Owner", "Close date"]);
    expect(within(table).getAllByRole("rowheader")).toHaveLength(2);
  });

  it("links each name to its opportunity detail page", () => {
    renderTable();
    expect(screen.getByRole("link", { name: "Renewal" })).toHaveAttribute(
      "href",
      "/opportunities/opp-1",
    );
    expect(screen.getByRole("link", { name: "Expansion" })).toHaveAttribute(
      "href",
      "/opportunities/opp-2",
    );
  });

  it("shows stage and owner in each row", () => {
    renderTable();
    const row = screen.getByRole("rowheader", { name: "Renewal" }).closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("Discovery")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("rep-7")).toBeInTheDocument();
  });

  it("marks the caption incomplete only when truncated", () => {
    renderTable({ truncated: true });
    expect(
      screen.getByRole("table", { name: "Opportunity results (incomplete — see notice above)" }),
    ).toBeInTheDocument();
    cleanup();
    renderTable();
    expect(screen.getByRole("table", { name: "Opportunity results" })).toBeInTheDocument();
  });
});
