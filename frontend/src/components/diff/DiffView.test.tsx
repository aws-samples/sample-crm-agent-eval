// SPDX-License-Identifier: MIT
import { describe, expect, it, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { DiffView, computeFieldDiffs } from "./DiffView";

afterEach(() => {
  cleanup();
});

describe("computeFieldDiffs", () => {
  it("marks a field present in both with a different value as changed", () => {
    const rows = computeFieldDiffs({ stage: "Discovery" }, { stage: "Negotiation" }, false);
    expect(rows).toEqual([
      { field: "stage", before: "Discovery", after: "Negotiation", status: "changed" },
    ]);
  });

  it("marks a field present only in after as added", () => {
    const rows = computeFieldDiffs({}, { owner_rep_id: "rep-2" }, false);
    expect(rows).toEqual([
      { field: "owner_rep_id", before: undefined, after: "rep-2", status: "added" },
    ]);
  });

  it("marks a field present only in before as removed when partialAfter is false", () => {
    const rows = computeFieldDiffs({ competitor: "Acme" }, {}, false);
    expect(rows).toEqual([
      { field: "competitor", before: "Acme", after: undefined, status: "removed" },
    ]);
  });

  it("omits a field present only in before when partialAfter is true", () => {
    const rows = computeFieldDiffs({ competitor: "Acme", stage: "Discovery" }, { stage: "Negotiation" }, true);
    expect(rows).toEqual([
      { field: "stage", before: "Discovery", after: "Negotiation", status: "changed" },
    ]);
  });

  it("marks an identical field as unchanged, including deep-equal objects", () => {
    const rows = computeFieldDiffs(
      { tags: { a: 1 } },
      { tags: { a: 1 } },
      false,
    );
    expect(rows).toEqual([{ field: "tags", before: { a: 1 }, after: { a: 1 }, status: "unchanged" }]);
  });

  it("sorts changed/added/removed before unchanged", () => {
    const rows = computeFieldDiffs(
      { name: "Acme", stage: "Discovery" },
      { name: "Acme", stage: "Negotiation" },
      false,
    );
    expect(rows.map((r) => r.status)).toEqual(["changed", "unchanged"]);
  });
});

describe("DiffView", () => {
  it("renders a summary count of changed fields", () => {
    render(
      <DiffView
        label="Test diff"
        before={{ stage: "Discovery", value: 1000 }}
        after={{ stage: "Negotiation", value: 1000 }}
      />,
    );
    expect(screen.getByText("1 field changed.")).toBeInTheDocument();
  });

  it("renders 'No field changes.' when before and after are identical", () => {
    render(<DiffView label="Test diff" before={{ stage: "Discovery" }} after={{ stage: "Discovery" }} />);
    expect(screen.getByText("No field changes.")).toBeInTheDocument();
  });

  it("conveys change status via a data attribute and visible text, not colour alone", () => {
    render(
      <DiffView label="Test diff" before={{ stage: "Discovery" }} after={{ stage: "Negotiation" }} />,
    );
    const row = screen.getByText("stage").closest("[data-diff-status]");
    expect(row).toHaveAttribute("data-diff-status", "changed");
    // The visually-hidden status word is present in the accessible text, not conveyed by CSS color.
    expect(row).toHaveTextContent("changed");
  });

  it("labels the whole region for assistive technology", () => {
    render(<DiffView label="Change to opportunity Acme Renewal" before={{}} after={{}} />);
    expect(
      screen.getByRole("region", { name: "Change to opportunity Acme Renewal" }),
    ).toBeInTheDocument();
  });

  it("renders before and after values with accessible before/after labels", () => {
    render(<DiffView label="Test diff" before={{ stage: "Discovery" }} after={{ stage: "Negotiation" }} />);
    expect(screen.getAllByText(/before:/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/after:/).length).toBeGreaterThan(0);
    expect(screen.getByText("Discovery")).toBeInTheDocument();
    expect(screen.getByText("Negotiation")).toBeInTheDocument();
  });

  it("does not render a removed row for a partial after-payload proposal diff", () => {
    render(
      <DiffView
        label="Proposed change"
        before={{ stage: "Discovery", competitor: "Acme" }}
        after={{ stage: "Negotiation" }}
        partialAfter
      />,
    );
    expect(screen.queryByText("competitor")).not.toBeInTheDocument();
    expect(screen.getAllByText("stage").length).toBeGreaterThan(0);
  });

  // `list_audit_records` serves its default call from a GSI whose projection omits before/after, so
  // a list row carries neither; the absent shape is asserted explicitly.
  describe("when a snapshot is absent, as list_audit_records returns it", () => {
    it("renders without throwing when both snapshots are undefined", () => {
      expect(() =>
        render(<DiffView label="Change to opportunities record opp-1" />),
      ).not.toThrow();
    });

    it("says the snapshots are not included rather than claiming no change", () => {
      render(<DiffView label="Change to opportunities record opp-1" />);
      expect(screen.getByText(/snapshots are not included/i)).toBeInTheDocument();
      // An empty diff would assert the change was empty; here it is unknown.
      expect(screen.queryByText("No field changes.")).not.toBeInTheDocument();
    });

    it("keeps the labelled region so the accessible name survives", () => {
      render(<DiffView label="Change to opportunities record opp-1" />);
      expect(
        screen.getByRole("region", { name: "Change to opportunities record opp-1" }),
      ).toBeInTheDocument();
    });

    it.each([
      ["only before is absent", undefined, { stage: "Negotiation" }],
      ["only after is absent", { stage: "Discovery" }, undefined],
    ])("handles the half-absent case: %s", (_name, before, after) => {
      expect(() =>
        render(<DiffView label="Half absent" before={before} after={after} />),
      ).not.toThrow();
      expect(screen.getByText(/snapshots are not included/i)).toBeInTheDocument();
    });
  });
});
