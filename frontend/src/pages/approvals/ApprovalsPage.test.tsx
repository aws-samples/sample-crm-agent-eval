// SPDX-License-Identifier: MIT
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ApprovalsPage } from "./ApprovalsPage";
import { CrmApiError } from "../../api/CrmApiClient";
import type { Proposal } from "../../types/domain";

const listProposals = vi.fn();
const approveProposal = vi.fn();
const rejectProposal = vi.fn();
const rebaseProposal = vi.fn();

vi.mock("../../api/useCrmApi", () => ({
  useCrmApi: () => ({
    listProposals,
    approveProposal,
    rejectProposal,
    rebaseProposal,
  }),
}));

const pendingProposal: Proposal = {
  proposal_id: "prop-1",
  target_table: "opportunities",
  target_id: "opp-1",
  operation: "update",
  before_snapshot: { stage: "Discovery", value: 1000 },
  after_payload: { stage: "Negotiation" },
  reason: "Rep advanced the deal after a signed LOI.",
  status: "pending",
  created_at: "2026-01-01T00:00:00Z",
};

beforeEach(() => {
  listProposals.mockReset();
  approveProposal.mockReset();
  rejectProposal.mockReset();
  rebaseProposal.mockReset();
});

// `globals: false` in vite.config.ts disables RTL's automatic cleanup, so it is registered here.
afterEach(() => {
  cleanup();
});

/**
 * The `list_proposals` envelope, per `crm.handlers.list_proposals`: `{proposals, next_cursor}`.
 */
function listProposalsResponse(proposals: readonly Proposal[]): {
  proposals: readonly Proposal[];
  next_cursor: string | null;
} {
  return { proposals, next_cursor: null };
}

describe("ApprovalsPage", () => {
  it("renders pending proposals as before/after diffs with approve and reject actions", async () => {
    listProposals.mockResolvedValue(listProposalsResponse([pendingProposal]));
    render(<ApprovalsPage />);

    await waitFor(() => expect(screen.getByText(/Update opportunities record opp-1/)).toBeInTheDocument());
    expect(screen.getByText(/Reason:/)).toBeInTheDocument();
    expect(screen.getByText("Discovery")).toBeInTheDocument();
    expect(screen.getByText("Negotiation")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
  });


  /** The page reads the handler's `proposals` key. */
  it("reads the proposals key from the real handler envelope", async () => {
    listProposals.mockResolvedValue(listProposalsResponse([pendingProposal]));
    render(<ApprovalsPage />);
    await waitFor(() =>
      expect(screen.getByText(/Update opportunities record opp-1/)).toBeInTheDocument(),
    );
  });

  it("renders the empty-queue message when the real envelope carries no proposals", async () => {
    listProposals.mockResolvedValue({ proposals: [], next_cursor: null });
    render(<ApprovalsPage />);
    await waitFor(() => expect(screen.getByText("No pending proposals.")).toBeInTheDocument());
  });

  /** The page loads the whole pending queue and offers no "load more" control. */
  it("does not page: a non-null next_cursor adds no pagination control", async () => {
    listProposals.mockResolvedValue({ proposals: [pendingProposal], next_cursor: "cursor-25" });
    render(<ApprovalsPage />);
    await waitFor(() =>
      expect(screen.getByText(/Update opportunities record opp-1/)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: /load more|next page/i })).not.toBeInTheDocument();
  });

  it("renders nothing having mutated: no approve/reject call happens on render alone", async () => {
    listProposals.mockResolvedValue(listProposalsResponse([pendingProposal]));
    render(<ApprovalsPage />);
    await waitFor(() => expect(screen.getByText(/Update opportunities/)).toBeInTheDocument());
    expect(approveProposal).not.toHaveBeenCalled();
    expect(rejectProposal).not.toHaveBeenCalled();
  });

  it("shows an empty-queue message distinct from a denial when there are no pending proposals", async () => {
    listProposals.mockResolvedValue(listProposalsResponse([]));
    render(<ApprovalsPage />);
    await waitFor(() => expect(screen.getByText("No pending proposals.")).toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders a typed 403 scope denial distinctly from an empty queue", async () => {
    listProposals.mockRejectedValue(
      new CrmApiError("scope_denied", 403, { message: "Not permitted.", missingScope: "book:acct-9" }),
    );
    render(<ApprovalsPage />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent("Access denied");
    expect(screen.getByText("book:acct-9")).toBeInTheDocument();
    expect(screen.queryByText("No pending proposals.")).not.toBeInTheDocument();
  });

  it("calls approveProposal with a fresh transaction key when Approve is clicked", async () => {
    listProposals.mockResolvedValue(listProposalsResponse([pendingProposal]));
    approveProposal.mockResolvedValue(undefined);
    render(<ApprovalsPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(approveProposal).toHaveBeenCalledTimes(1));
    const call = approveProposal.mock.calls[0][0];
    expect(call.proposal_id).toBe("prop-1");
    expect(typeof call.transaction_key).toBe("string");
    expect(call.transaction_key.length).toBeGreaterThan(0);
  });

  it("renders a conflict as a clear recoverable message, not a generic error, on approve", async () => {
    listProposals.mockResolvedValue(listProposalsResponse([pendingProposal]));
    approveProposal.mockRejectedValue(
      new CrmApiError("conflict", 409, { message: "The record changed." }),
    );
    render(<ApprovalsPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Can\u2019t approve"));
    expect(screen.getByRole("alert")).toHaveTextContent(/changed underneath/i);
    expect(screen.getByRole("button", { name: /refresh and review/i })).toBeInTheDocument();
  });

  it("rebases the proposal and then refetches the queue when Refresh is used after a conflict", async () => {
    listProposals.mockResolvedValueOnce(listProposalsResponse([pendingProposal]));
    approveProposal.mockRejectedValueOnce(
      new CrmApiError("conflict", 409, { message: "The record changed." }),
    );
    rebaseProposal.mockResolvedValueOnce({ proposal: pendingProposal });
    render(<ApprovalsPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());

    listProposals.mockResolvedValueOnce(listProposalsResponse([]));
    await userEvent.click(screen.getByRole("button", { name: /refresh and review/i }));

    await waitFor(() => expect(screen.getByText("No pending proposals.")).toBeInTheDocument());
    expect(rebaseProposal).toHaveBeenCalledTimes(1);
    const [rebaseArgs] = rebaseProposal.mock.calls[0] as [Record<string, unknown>];
    expect(rebaseArgs.proposal_id).toBe("prop-1");
    expect(typeof rebaseArgs.transaction_key).toBe("string");
    expect(rebaseArgs.transaction_key).not.toBe("");
    expect(rebaseProposal.mock.invocationCallOrder[0]).toBeLessThan(
      listProposals.mock.invocationCallOrder[1],
    );
    expect(listProposals).toHaveBeenCalledTimes(2);
  });

  it("renders the three-way table for a fields_changed conflict", async () => {
    listProposals.mockResolvedValue(listProposalsResponse([pendingProposal]));
    approveProposal.mockRejectedValue(
      new CrmApiError("conflict", 409, {
        message: "The record changed.",
        reason: "fields_changed",
        conflictingFields: [
          { field: "stage", snapshot: "Discovery", current: "Closed Lost", proposed: "Negotiation" },
        ],
      }),
    );
    render(<ApprovalsPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    const table = await screen.findByRole("table", { name: /changed since this proposal/i });
    expect(table).toHaveTextContent("Closed Lost");
    expect(table).toHaveTextContent("Negotiation");
  });

  it.each(["already_decided", "target_missing"])(
    "only refetches, without rebasing, after a %s conflict",
    async (reason) => {
      listProposals.mockResolvedValueOnce(listProposalsResponse([pendingProposal]));
      approveProposal.mockRejectedValueOnce(
        new CrmApiError("conflict", 409, { message: "No.", reason }),
      );
      render(<ApprovalsPage />);
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument(),
      );
      await userEvent.click(screen.getByRole("button", { name: "Approve" }));
      await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());

      listProposals.mockResolvedValueOnce(listProposalsResponse([]));
      await userEvent.click(screen.getByRole("button", { name: /refresh and review/i }));

      await waitFor(() => expect(screen.getByText("No pending proposals.")).toBeInTheDocument());
      expect(rebaseProposal).not.toHaveBeenCalled();
    },
  );

  it("shows a refused rebase as an access denial on the row, keeping the queue", async () => {
    listProposals.mockResolvedValue(listProposalsResponse([pendingProposal]));
    approveProposal.mockRejectedValueOnce(
      new CrmApiError("conflict", 409, { message: "The record changed." }),
    );
    rebaseProposal.mockRejectedValueOnce(
      new CrmApiError("scope_denied", 403, {
        message: "You do not have access to that account.",
        missingScope: "account:acct-2",
      }),
    );
    render(<ApprovalsPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await userEvent.click(await screen.findByRole("button", { name: /refresh and review/i }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Access denied."));
    expect(screen.getByRole("alert")).toHaveTextContent("account:acct-2");
    expect(screen.getByRole("heading", { name: /opp-1/ })).toBeInTheDocument();
    expect(listProposals).toHaveBeenCalledTimes(1);
  });

  it("shows any other rebase failure as an error on the row", async () => {
    listProposals.mockResolvedValue(listProposalsResponse([pendingProposal]));
    approveProposal.mockRejectedValueOnce(
      new CrmApiError("conflict", 409, { message: "The record changed." }),
    );
    rebaseProposal.mockRejectedValueOnce(
      new CrmApiError("conflict", 409, { message: "The proposal changed while refreshing." }),
    );
    render(<ApprovalsPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await userEvent.click(await screen.findByRole("button", { name: /refresh and review/i }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("The proposal changed while refreshing."),
    );
  });

  it("calls rejectProposal when Reject is clicked", async () => {
    listProposals.mockResolvedValue(listProposalsResponse([pendingProposal]));
    rejectProposal.mockResolvedValue(undefined);
    render(<ApprovalsPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Reject" }));

    await waitFor(() => expect(rejectProposal).toHaveBeenCalledTimes(1));
    expect(rejectProposal.mock.calls[0][0].proposal_id).toBe("prop-1");
  });

  /** An `{items, cursor}` envelope renders an empty queue rather than throwing. */
  it("renders an empty queue, not a crash, on the exact shape types.ts used to declare", async () => {
    listProposals.mockResolvedValue({ items: [pendingProposal], cursor: null });
    render(<ApprovalsPage />);

    await waitFor(() => expect(screen.getByText("No pending proposals.")).toBeInTheDocument());
    expect(screen.queryByText(/Update opportunities record opp-1/)).not.toBeInTheDocument();
  });

  /** A bare `Proposal[]` is not accepted; only the `{proposals}` envelope is. */
  it("accepts only the handler envelope: a bare array is not a second accepted shape", async () => {
    listProposals.mockResolvedValue([pendingProposal]);
    render(<ApprovalsPage />);

    await waitFor(() => expect(screen.getByText("No pending proposals.")).toBeInTheDocument());
    expect(screen.queryByText(/Update opportunities record opp-1/)).not.toBeInTheDocument();
  });
});
