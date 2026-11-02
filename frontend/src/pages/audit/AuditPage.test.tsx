// SPDX-License-Identifier: MIT
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { formatDateTime } from "../../lib/format";
import { AuditPage } from "./AuditPage";
import { CrmApiError } from "../../api/CrmApiClient";
import type { AuditRecord } from "../../types/domain";

const listAuditRecords = vi.fn();
const revertAuditRecord = vi.fn();

vi.mock("../../api/useCrmApi", () => ({
  useCrmApi: () => ({
    listAuditRecords,
    revertAuditRecord,
  }),
}));

const record: AuditRecord = {
  audit_id: "audit-1",
  target_table: "opportunities",
  target_id: "opp-1",
  before_snapshot: { stage: "Discovery" },
  after_snapshot: { stage: "Negotiation" },
  actor_rep_id: "rep-1",
  created_at: "2026-01-01T00:00:00Z",
};

beforeEach(() => {
  listAuditRecords.mockReset();
  revertAuditRecord.mockReset();
});

// `globals: false` in vite.config.ts disables RTL's automatic cleanup, so it is registered here.
afterEach(() => {
  cleanup();
});

function renderPage(): void {
  render(
    <MemoryRouter>
      <AuditPage />
    </MemoryRouter>,
  );
}

describe("AuditPage", () => {
  it("renders audit entries as before/after diffs with a revert action", async () => {
    listAuditRecords.mockResolvedValue([record]);
    renderPage();

    await waitFor(() => expect(screen.getByText(/opportunities record opp-1/)).toBeInTheDocument());
    expect(screen.getByText("Discovery")).toBeInTheDocument();
    expect(screen.getByText("Negotiation")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revert" })).toBeInTheDocument();
  });

  it("renders audit entries from the real `list_audit_records` envelope ({audit_records, next_cursor})", async () => {
    // `list_audit_records` returns `{audit_records: [...], next_cursor}`.
    listAuditRecords.mockResolvedValue({ audit_records: [record], next_cursor: null });
    renderPage();

    await waitFor(() => expect(screen.getByText(/opportunities record opp-1/)).toBeInTheDocument());
    expect(screen.queryByText("No audit records.")).not.toBeInTheDocument();
  });

  it("shows an empty-log message distinct from a denial", async () => {
    listAuditRecords.mockResolvedValue([]);
    renderPage();
    await waitFor(() => expect(screen.getByText("No audit records.")).toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders a typed 403 scope denial distinctly from an empty audit log", async () => {
    listAuditRecords.mockRejectedValue(
      new CrmApiError("scope_denied", 403, { message: "Not permitted.", missingScope: "book:acct-9" }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent("Access denied");
    expect(screen.queryByText("No audit records.")).not.toBeInTheDocument();
  });

  it("calls revertAuditRecord with a fresh transaction key when Revert is clicked", async () => {
    listAuditRecords.mockResolvedValue([record]);
    revertAuditRecord.mockResolvedValue(undefined);
    renderPage();
    await waitFor(() => expect(screen.getByRole("button", { name: "Revert" })).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Revert" }));

    await waitFor(() => expect(revertAuditRecord).toHaveBeenCalledTimes(1));
    const call = revertAuditRecord.mock.calls[0][0];
    expect(call.audit_id).toBe("audit-1");
    expect(typeof call.transaction_key).toBe("string");
  });

  it("renders a conflict as a clear recoverable message on a second revert attempt", async () => {
    listAuditRecords.mockResolvedValue([record]);
    revertAuditRecord.mockRejectedValue(
      new CrmApiError("conflict", 409, { message: "Already reverted." }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByRole("button", { name: "Revert" })).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Revert" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Can\u2019t revert"));
    expect(screen.getByRole("alert")).toHaveTextContent(/changed underneath/i);
  });

  it("disables Revert and shows the reverted badge for an already-reverted record", async () => {
    listAuditRecords.mockResolvedValue([
      { ...record, status: "reversed", reversed_at: "2026-01-02T00:00:00Z" },
    ]);
    renderPage();

    await waitFor(() => expect(screen.getByRole("button", { name: "Already reverted" })).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Already reverted" })).toBeDisabled();
    // Compared against the real `formatDateTime`, whose output is locale- and timezone-dependent.
    // The raw value must not be on screen, and the machine-readable value stays in `dateTime`.
    const revertedAt = "2026-01-02T00:00:00Z";
    expect(
      screen.getByText(`Reverted at ${formatDateTime(revertedAt)}`),
    ).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(`Reverted at ${revertedAt}`))).not.toBeInTheDocument();
    expect(screen.getByText(/^Reverted at /).getAttribute("dateTime")).toBe(revertedAt);
  });

  it("refetches the audit log when Refresh is used after a conflict", async () => {
    listAuditRecords.mockResolvedValueOnce([record]);
    revertAuditRecord.mockRejectedValueOnce(new CrmApiError("conflict", 409, { message: "Conflict." }));
    renderPage();
    await waitFor(() => expect(screen.getByRole("button", { name: "Revert" })).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Revert" }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());

    listAuditRecords.mockResolvedValueOnce([{ ...record, status: "reversed" }]);
    await userEvent.click(screen.getByRole("button", { name: /refresh and review/i }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Already reverted" })).toBeInTheDocument(),
    );
    expect(listAuditRecords).toHaveBeenCalledTimes(2);
  });

  it("makes each card a keyboard-reachable link to its detail view", async () => {
    listAuditRecords.mockResolvedValue({ audit_records: [record], next_cursor: null });
    renderPage();

    const link = await screen.findByRole("link", { name: "opportunities record opp-1" });
    expect(link).toHaveAttribute("href", "/audit/audit-1");
    await userEvent.tab();
    expect(link).toHaveFocus();
    expect(link.className).toContain("focus-visible:ring-focus");
  });

  it("reads the Reverted state from the projected status, which the default list carries", async () => {
    // `gsi_tenant` projects `status` but not `reversed_at`.
    listAuditRecords.mockResolvedValue({
      audit_records: [{ ...record, status: "reversed" }],
      next_cursor: null,
    });
    renderPage();

    expect(await screen.findByText("Reverted")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Already reverted" })).toBeDisabled();
    expect(screen.queryByText(/^Reverted at /)).not.toBeInTheDocument();
  });

  it("does not treat a stray reversed_at as reverted while the status is active", async () => {
    listAuditRecords.mockResolvedValue([
      { ...record, status: "active", reversed_at: "2026-01-02T00:00:00Z" },
    ]);
    renderPage();

    expect(await screen.findByRole("button", { name: "Revert" })).toBeEnabled();
  });
});
