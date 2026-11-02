// SPDX-License-Identifier: MIT
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { CrmApiError } from "../../api/CrmApiClient";
import { formatDateTime } from "../../lib/format";
import { AuditDetailPage, type AuditRecordDetail } from "./AuditDetailPage";
import { legacyAuditRecordResponse } from "./legacyAuditRecord.fixture";

const getAuditRecord = vi.fn();
const revertAuditRecord = vi.fn();

vi.mock("../../api/useCrmApi", () => ({
  useCrmApi: () => ({ getAuditRecord, revertAuditRecord }),
}));

const update: AuditRecordDetail = {
  audit_id: "audit-1",
  target_table: "opportunities",
  target_id: "opp-1",
  before_snapshot: { stage: "Discovery", name: "Acme" },
  after_snapshot: { stage: "Negotiation", name: "Acme" },
  actor_rep_id: "rep-1",
  created_at: "2026-01-01T00:00:00Z",
  status: "active",
  action: "update",
};

function renderAt(auditId: string): void {
  render(
    <MemoryRouter initialEntries={[`/audit/${auditId}`]}>
      <Routes>
        <Route path="/audit/:auditId" element={<AuditDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  getAuditRecord.mockReset();
  revertAuditRecord.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("AuditDetailPage", () => {
  it("reads the record named in the route and shows its metadata", async () => {
    getAuditRecord.mockResolvedValue({ audit_record: update });
    renderAt("audit-1");

    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
        "opportunities record opp-1",
      ),
    );
    expect(getAuditRecord).toHaveBeenCalledWith({ audit_id: "audit-1" });
    expect(screen.getByText("Updated")).toBeInTheDocument();
    expect(screen.getByText("rep-1")).toBeInTheDocument();
    expect(screen.getByText(formatDateTime("2026-01-01T00:00:00Z"))).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to the audit log" })).toHaveAttribute(
      "href",
      "/audit",
    );
  });

  it("shows an update as a field diff", async () => {
    getAuditRecord.mockResolvedValue({ audit_record: update });
    renderAt("audit-1");

    const diff = await screen.findByRole("region", { name: "Change to opportunities record opp-1" });
    expect(diff).toHaveTextContent("Discovery");
    expect(diff).toHaveTextContent("Negotiation");
  });

  it("shows a create as its new values only", async () => {
    getAuditRecord.mockResolvedValue({
      audit_record: { ...update, action: "create", before_snapshot: {} },
    });
    renderAt("audit-1");

    const region = await screen.findByRole("region", { name: /was created with/ });
    expect(region).toHaveTextContent("Negotiation");
    expect(region).not.toHaveTextContent("Discovery");
  });

  it("shows a delete as its removed values only", async () => {
    getAuditRecord.mockResolvedValue({
      audit_record: { ...update, action: "delete", after_snapshot: {} },
    });
    renderAt("audit-1");

    const region = await screen.findByRole("region", { name: /held when deleted/ });
    expect(region).toHaveTextContent("Discovery");
    expect(region).not.toHaveTextContent("Negotiation");
  });

  it("says so explicitly when a legacy record has no snapshots", async () => {
    const legacy: Record<string, unknown> = { ...update };
    delete legacy.before_snapshot;
    delete legacy.after_snapshot;
    getAuditRecord.mockResolvedValue({ audit_record: legacy });
    renderAt("audit-1");

    expect(await screen.findByText(/written without before and after snapshots/)).toBeInTheDocument();
  });

  it("renders a stored pre-snapshot record with the legacy wording and its revert control", async () => {
    getAuditRecord.mockResolvedValue(legacyAuditRecordResponse);
    renderAt("audit-legacy-1");

    expect(
      await screen.findByText(
        "This audit record was written without before and after snapshots, so its field changes cannot be shown.",
        { normalizer: (text) => text.replace(/\s+/g, " ").trim() },
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revert" })).toBeEnabled();
    expect(screen.queryByText(/was created with/)).not.toBeInTheDocument();
    expect(getAuditRecord).toHaveBeenCalledWith({ audit_id: "audit-legacy-1" });
  });

  it("shows a reverted record's revert time and refuses a second revert", async () => {
    getAuditRecord.mockResolvedValue({
      audit_record: {
        ...update,
        status: "reversed",
        reversed_at: "2026-01-02T00:00:00Z",
        reversed_by: "rep-2",
      },
    });
    renderAt("audit-1");

    const button = await screen.findByRole("button", { name: "Already reverted" });
    expect(button).toBeDisabled();
    expect(screen.getByText(formatDateTime("2026-01-02T00:00:00Z"))).toBeInTheDocument();
    expect(screen.getByText("rep-2")).toBeInTheDocument();
  });

  it("offers no revert for a proposal rebase record", async () => {
    getAuditRecord.mockResolvedValue({ audit_record: { ...update, action: "proposal_rebased" } });
    renderAt("audit-1");

    expect(await screen.findByText("Proposal snapshot re-captured")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /revert/i })).not.toBeInTheDocument();
  });

  it("reverts with a fresh transaction key and re-reads the record", async () => {
    getAuditRecord
      .mockResolvedValueOnce({ audit_record: update })
      .mockResolvedValueOnce({ audit_record: { ...update, status: "reversed" } });
    revertAuditRecord.mockResolvedValue({ audit_record: { ...update, status: "reversed" } });
    renderAt("audit-1");
    await userEvent.click(await screen.findByRole("button", { name: "Revert" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Already reverted" })).toBeInTheDocument());
    const [call] = revertAuditRecord.mock.calls[0] as [Record<string, unknown>];
    expect(call.audit_id).toBe("audit-1");
    expect(typeof call.transaction_key).toBe("string");
    expect(getAuditRecord).toHaveBeenCalledTimes(2);
  });

  it("renders a revert conflict and refreshes the record from it", async () => {
    getAuditRecord.mockResolvedValue({ audit_record: update });
    revertAuditRecord.mockRejectedValue(new CrmApiError("conflict", 409, { message: "Changed." }));
    renderAt("audit-1");
    await userEvent.click(await screen.findByRole("button", { name: "Revert" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Can’t revert"));
    await userEvent.click(screen.getByRole("button", { name: /refresh and review/i }));
    await waitFor(() => expect(getAuditRecord).toHaveBeenCalledTimes(2));
  });

  it("renders an out-of-book record as an access denial", async () => {
    getAuditRecord.mockRejectedValue(
      new CrmApiError("scope_denied", 403, { message: "No access.", missingScope: "account:acct-2" }),
    );
    renderAt("audit-9");

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Access denied"));
    expect(screen.getByRole("alert")).toHaveTextContent("account:acct-2");
  });

  it("renders an unknown record as not found, not as an error", async () => {
    getAuditRecord.mockRejectedValue(new CrmApiError("not_found", 404, { message: "No such." }));
    renderAt("audit-404");

    expect(await screen.findByText(/No audit record audit-404 exists/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
