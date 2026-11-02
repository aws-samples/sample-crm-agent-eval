// SPDX-License-Identifier: MIT
import { describe, expect, it, vi, afterEach } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConflictNotice } from "./ConflictNotice";

afterEach(() => {
  cleanup();
});

describe("ConflictNotice", () => {
  it("renders as an alert region naming the attempted action", () => {
    render(<ConflictNotice action="approve" onRefresh={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/Can.t approve/);
  });

  it("explains that the record changed underneath, not a generic failure", () => {
    render(<ConflictNotice action="revert" onRefresh={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/changed underneath/i);
    expect(screen.getByRole("alert")).not.toHaveTextContent(/^error$/i);
  });

  it("gives a concrete next step for each action", () => {
    render(<ConflictNotice action="reject" onRefresh={vi.fn()} />);
    expect(screen.getByText(/Refresh the queue/i)).toBeInTheDocument();
  });

  it("invokes onRefresh when the refresh action is used", async () => {
    const onRefresh = vi.fn();
    render(<ConflictNotice action="approve" onRefresh={onRefresh} />);
    await userEvent.click(screen.getByRole("button", { name: /refresh and review/i }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("offers a dismiss action only when onDismiss is provided", () => {
    const { rerender } = render(<ConflictNotice action="approve" onRefresh={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /dismiss/i })).not.toBeInTheDocument();

    rerender(<ConflictNotice action="approve" onRefresh={vi.fn()} onDismiss={vi.fn()} />);
    expect(screen.getByRole("button", { name: /dismiss/i })).toBeInTheDocument();
  });

  const FIELDS = [
    { field: "stage", snapshot: "Discovery", current: "Closed Lost", proposed: "Negotiation" },
    { field: "value", snapshot: 1000, current: null, proposed: 2500 },
  ] as const;

  it("renders the moved fields as a field / at submit / now / proposed table", () => {
    render(
      <ConflictNotice
        action="approve"
        reason="fields_changed"
        conflictingFields={FIELDS}
        onRefresh={vi.fn()}
      />,
    );
    const table = screen.getByRole("table", { name: /changed since this proposal was submitted/i });
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((th) => th.textContent);
    expect(headers).toEqual(["Field", "At submit", "Now", "Proposed"]);
    const stage = within(table).getByRole("row", { name: /stage/ });
    expect(
      within(stage)
        .getAllByRole("cell")
        .map((td) => td.textContent),
    ).toEqual(["Discovery", "Closed Lost", "Negotiation"]);
    const value = within(table).getByRole("row", { name: /value/ });
    expect(within(value).getAllByRole("cell")[1]).toHaveTextContent("—");
  });

  it("shows no table without fields_changed, even when fields are passed", () => {
    render(<ConflictNotice action="approve" conflictingFields={FIELDS} onRefresh={vi.fn()} />);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("explains an already-decided or deleted target instead of the action's default copy", () => {
    const { rerender } = render(
      <ConflictNotice action="approve" reason="already_decided" onRefresh={vi.fn()} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/already approved or rejected/i);
    expect(screen.getByRole("alert")).not.toHaveTextContent(/load the record's current state/i);

    rerender(<ConflictNotice action="approve" reason="target_missing" onRefresh={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/has been deleted/i);
  });

  it("disables the refresh and dismiss actions while refreshing, and says so", async () => {
    const onRefresh = vi.fn();
    render(
      <ConflictNotice action="approve" refreshing onRefresh={onRefresh} onDismiss={vi.fn()} />,
    );
    const button = screen.getByRole("button", { name: "Refreshing…" });
    expect(button).toBeDisabled();
    expect(screen.getByRole("button", { name: /dismiss/i })).toBeDisabled();
    await userEvent.click(button);
    expect(onRefresh).not.toHaveBeenCalled();
  });
});
