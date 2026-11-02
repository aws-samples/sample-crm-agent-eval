// SPDX-License-Identifier: MIT
/**
 * Tests for the pipeline board:
 *  - A drag never calls `updateOpportunity`/any direct commit — only `submitProposal`.
 *  - The same outcome is reachable by keyboard alone.
 *  - A typed 403 renders distinctly from an empty column.
 *  - Backend-refused transitions (reopen / closed-lateral) are surfaced, while a permitted
 *    regression is allowed through to the proposal dialog.
 */
import type { JSX } from "react";
import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MemoryRouter, useLocation } from "react-router-dom";

import { PipelineBoard } from "./PipelineBoard";
import { RootErrorBoundary } from "../../components/RootErrorBoundary";
import * as useCrmApiModule from "../../api/useCrmApi";
import { CrmApiError } from "../../api/CrmApiClient";
import type { Opportunity } from "../../types/domain";

/**
 * The board inside a router; `usePipelineBoard` stamps its panel report with the current pathname.
 */
/**
 * Reports the router's current pathname so the navigation tests can assert where a card click
 * took the user. `PipelineBoard` renders no `<Routes>`, so a bare probe sibling is enough.
 */
function LocationProbe(): JSX.Element {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

function renderBoard() {
  return render(
    <MemoryRouter initialEntries={["/pipeline"]}>
      <PipelineBoard />
      <LocationProbe />
    </MemoryRouter>,
  );
}

/** A drag event needs a real `dataTransfer`; the board reads the opportunity id back off it. */
function makeDataTransfer() {
  return {
    data: {} as Record<string, string>,
    setData(format: string, value: string) {
      this.data[format] = value;
    },
    getData(format: string) {
      return this.data[format] ?? "";
    },
    effectAllowed: "",
  };
}

function withOpportunity(opportunity: Opportunity, extra: Record<string, unknown> = {}) {
  return {
    searchOpportunities: vi.fn().mockResolvedValue({
      items: [opportunity],
      truncated: false,
      examined_count: 1,
      next_cursor: null,
    }),
    ...extra,
  };
}

function opp(overrides: Partial<Opportunity>): Opportunity {
  return {
    opportunity_id: "opp-1",
    account_id: "acct-1",
    name: "Big Deal",
    value: 5000,
    stage: "Discovery",
    owner_rep_id: "rep-1",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function mockApi(overrides: Partial<Record<string, (...args: unknown[]) => unknown>> = {}) {
  const api = {
    searchOpportunities: vi.fn().mockResolvedValue({
      items: [],
      truncated: false,
      examined_count: 0,
      next_cursor: null,
    }),
    listProposals: vi.fn().mockResolvedValue({ proposals: [], next_cursor: null }),
    submitProposal: vi.fn(),
    updateOpportunity: vi.fn(),
    ...overrides,
  };
  vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue(api as never);
  return api;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("PipelineBoard", () => {
  it("renders empty-state columns when there are no opportunities", async () => {
    mockApi();
    renderBoard();
    expect(await screen.findByTestId("pipeline-column-empty-Discovery")).toBeInTheDocument();
  });

  it("renders a typed 403 scope denial distinctly from an empty column", async () => {
    mockApi({
      searchOpportunities: vi.fn().mockRejectedValue(
        new CrmApiError("scope_denied", 403, { message: "denied", missingScope: "account:acct-9" }),
      ),
    });
    renderBoard();
    const denial = await screen.findByTestId("pipeline-scope-denied");
    expect(denial).toBeInTheDocument();
    expect(screen.queryByTestId("pipeline-column-empty-Discovery")).not.toBeInTheDocument();
    expect(screen.getByText(/account:acct-9/)).toBeInTheDocument();
  });

  it("a drag-drop emits a proposal via submitProposal, never a direct commit", async () => {
    const opportunity = opp({ stage: "Discovery" });
    const api = mockApi({
      searchOpportunities: vi.fn().mockResolvedValue({
        items: [opportunity],
        truncated: false,
        examined_count: 1,
        next_cursor: null,
      }),
      submitProposal: vi.fn().mockResolvedValue({
        proposal: {
          proposal_id: "prop-1",
          target_table: "opportunities",
          target_id: "opp-1",
          operation: "update",
          before_snapshot: {},
          after_payload: { stage: "Qualification" },
          reason: "Moved forward",
          status: "pending",
          created_at: "2026-01-01T00:00:00Z",
        },
      }),
    });

    const user = userEvent.setup();
    renderBoard();
    const card = await screen.findByText(/Big Deal/);
    const cardEl = card.closest("[data-opportunity-id]") as HTMLElement;
    const targetColumn = screen.getByTestId("pipeline-column-Qualification");

    const dataTransfer = {
      data: {} as Record<string, string>,
      setData(format: string, value: string) {
        this.data[format] = value;
      },
      getData(format: string) {
        return this.data[format] ?? "";
      },
      effectAllowed: "",
    };

    fireEvent.dragStart(cardEl, { dataTransfer });
    fireEvent.dragOver(targetColumn, { dataTransfer });
    fireEvent.drop(targetColumn, { dataTransfer });

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeInTheDocument();
    await user.type(screen.getByLabelText("Reason"), "Moved forward");
    await user.click(screen.getByRole("button", { name: "Submit proposal" }));

    await waitFor(() => {
      expect(api.submitProposal).toHaveBeenCalledWith(
        expect.objectContaining({
          target_table: "opportunities",
          target_id: "opp-1",
          operation: "update",
          after_payload: { stage: "Qualification" },
        }),
      );
    });
    // The direct-commit path must never be called for a stage change.
    expect(api.updateOpportunity).not.toHaveBeenCalled();
  });

  it("is operable by keyboard alone: pick up, move with arrows, confirm with Enter", async () => {
    const opportunity = opp({ stage: "Discovery" });
    const api = mockApi({
      searchOpportunities: vi.fn().mockResolvedValue({
        items: [opportunity],
        truncated: false,
        examined_count: 1,
        next_cursor: null,
      }),
      submitProposal: vi.fn().mockResolvedValue({
        proposal: {
          proposal_id: "prop-2",
          target_table: "opportunities",
          target_id: "opp-1",
          operation: "update",
          before_snapshot: {},
          after_payload: { stage: "Qualification" },
          reason: "Advancing",
          status: "pending",
          created_at: "2026-01-01T00:00:00Z",
        },
      }),
    });

    const user = userEvent.setup();
    renderBoard();
    const handle = await screen.findByRole("button", { name: "Move opportunity" });

    handle.focus();
    await user.keyboard("{Enter}"); // pick up
    expect(handle).toHaveAttribute("aria-pressed", "true");

    await user.keyboard("{ArrowRight}"); // move to Qualification
    await user.keyboard("{Enter}"); // confirm drop -> opens dialog

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeInTheDocument();
    await user.type(screen.getByLabelText("Reason"), "Advancing");
    await user.click(screen.getByRole("button", { name: "Submit proposal" }));

    await waitFor(() => {
      expect(api.submitProposal).toHaveBeenCalledWith(
        expect.objectContaining({ after_payload: { stage: "Qualification" } }),
      );
    });
    expect(api.updateOpportunity).not.toHaveBeenCalled();
  });

  it("keyboard Escape cancels the pick-up without submitting a proposal", async () => {
    const opportunity = opp({ stage: "Discovery" });
    const api = mockApi({
      searchOpportunities: vi.fn().mockResolvedValue({
        items: [opportunity],
        truncated: false,
        examined_count: 1,
        next_cursor: null,
      }),
    });

    const user = userEvent.setup();
    renderBoard();
    const handle = await screen.findByRole("button", { name: "Move opportunity" });
    handle.focus();
    await user.keyboard("{Enter}");
    expect(handle).toHaveAttribute("aria-pressed", "true");
    await user.keyboard("{Escape}");
    expect(handle).toHaveAttribute("aria-pressed", "false");
    expect(api.submitProposal).not.toHaveBeenCalled();
  });

  it("surfaces a card with a pending proposal as locked, not directly editable again", async () => {
    const opportunity = opp({ stage: "Discovery" });
    mockApi({
      searchOpportunities: vi.fn().mockResolvedValue({
        items: [opportunity],
        truncated: false,
        examined_count: 1,
        next_cursor: null,
      }),
      listProposals: vi.fn().mockResolvedValue({
        proposals: [
          {
            proposal_id: "prop-3",
            target_table: "opportunities",
            target_id: "opp-1",
            operation: "update",
            before_snapshot: {},
            after_payload: { stage: "Qualification" },
            reason: "Already proposed",
            status: "pending",
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        next_cursor: null,
      }),
    });

    renderBoard();
    const badge = await screen.findByTestId("pending-badge");
    expect(badge).toHaveTextContent("Qualification");
    // The move control is disabled while a proposal is pending; the card is no longer draggable...
    const handle = screen.getByRole("button", { name: "Move opportunity" });
    expect(handle).toBeDisabled();
    const cardEl = badge.closest("[data-opportunity-id]") as HTMLElement;
    expect(cardEl).not.toHaveAttribute("draggable", "true");
    // ...but a locked card must still open on click: its navigational link stays present and enabled.
    expect(screen.getByRole("link", { name: /Big Deal/ })).toBeInTheDocument();
  });

  it("shows a truncation notice explicitly rather than silently dropping records", async () => {
    mockApi({
      searchOpportunities: vi.fn().mockResolvedValue({
        items: [],
        truncated: true,
        examined_count: 2000,
        next_cursor: "abc",
      }),
    });
    renderBoard();
    expect(await screen.findByTestId("pipeline-truncated-notice")).toBeInTheDocument();
  });

  it("refuses reopening a closed stage per the backend's transition classification", async () => {
    const opportunity = opp({ stage: "Closed Won" });
    mockApi({
      searchOpportunities: vi.fn().mockResolvedValue({
        items: [opportunity],
        truncated: false,
        examined_count: 1,
        next_cursor: null,
      }),
    });

    const user = userEvent.setup();
    renderBoard();
    const handle = await screen.findByRole("button", { name: "Move opportunity" });
    handle.focus();
    await user.keyboard("{Enter}"); // pick up in Closed Won
    await user.keyboard("{ArrowLeft}"); // move to Negotiation (reopen)
    await user.keyboard("{Enter}"); // confirm drop -> dialog shows refusal

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/not permitted/i);
    expect(screen.queryByRole("button", { name: "Submit proposal" })).not.toBeInTheDocument();
  });

  it("permits a stage regression (Negotiation back to Discovery) through to the proposal dialog", async () => {
    const opportunity = opp({ stage: "Negotiation" });
    const api = mockApi({
      searchOpportunities: vi.fn().mockResolvedValue({
        items: [opportunity],
        truncated: false,
        examined_count: 1,
        next_cursor: null,
      }),
      submitProposal: vi.fn().mockResolvedValue({
        proposal: {
          proposal_id: "prop-4",
          target_table: "opportunities",
          target_id: "opp-1",
          operation: "update",
          before_snapshot: {},
          after_payload: { stage: "Discovery" },
          reason: "Regressed",
          status: "pending",
          created_at: "2026-01-01T00:00:00Z",
        },
      }),
    });

    const user = userEvent.setup();
    renderBoard();
    const handle = await screen.findByRole("button", { name: "Move opportunity" });
    handle.focus();
    await user.keyboard("{Enter}");
    await user.keyboard("{ArrowLeft}{ArrowLeft}{ArrowLeft}"); // Negotiation -> Discovery
    await user.keyboard("{Enter}");

    const dialog = await screen.findByRole("dialog");
    expect(dialog).not.toHaveTextContent(/not permitted/i);
    await user.type(screen.getByLabelText("Reason"), "Regressed");
    await user.click(screen.getByRole("button", { name: "Submit proposal" }));

    await waitFor(() => {
      expect(api.submitProposal).toHaveBeenCalledWith(
        expect.objectContaining({ after_payload: { stage: "Discovery" } }),
      );
    });
  });

  // ---- Whole-card click opens the opportunity; the move gesture never navigates ----

  it("clicking the card opens the opportunity detail page", async () => {
    mockApi(withOpportunity(opp({ stage: "Discovery" })));
    const user = userEvent.setup();
    renderBoard();
    const link = await screen.findByRole("link", { name: /Big Deal/ });
    await user.click(link);
    expect(screen.getByTestId("location")).toHaveTextContent("/opportunities/opp-1");
  });

  it("Enter on the focused card link opens the opportunity detail page", async () => {
    mockApi(withOpportunity(opp({ stage: "Discovery" })));
    const user = userEvent.setup();
    renderBoard();
    const link = await screen.findByRole("link", { name: /Big Deal/ });
    link.focus();
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("location")).toHaveTextContent("/opportunities/opp-1");
  });

  it("the move handle initiates a keyboard move without navigating", async () => {
    mockApi(withOpportunity(opp({ stage: "Discovery" })));
    const user = userEvent.setup();
    renderBoard();
    const handle = await screen.findByRole("button", { name: "Move opportunity" });
    handle.focus();
    await user.keyboard("{Enter}"); // pick up — this is a move, not an open
    expect(handle).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("location")).toHaveTextContent("/pipeline");
  });

  it("suppresses the click a browser fires after a drag, so a move never also navigates", async () => {
    mockApi(withOpportunity(opp({ stage: "Discovery" })));
    renderBoard();
    const link = await screen.findByRole("link", { name: /Big Deal/ });
    const cardEl = link.closest("[data-opportunity-id]") as HTMLElement;

    // A mouse drag, then the trailing click a browser synthesises on the drag source. The card's
    // drag-occurred guard must swallow that click.
    fireEvent.dragStart(cardEl, { dataTransfer: makeDataTransfer() });
    fireEvent.click(link);

    expect(screen.getByTestId("location")).toHaveTextContent("/pipeline");
    expect(screen.getByTestId("location")).not.toHaveTextContent("/opportunities/opp-1");
  });

  // ---- An unsettled fetch and a non-API failure are both distinguishable from "empty" ----

  it("renders a loading status before the first fetch settles, not bare empty columns", async () => {
    let release: (value: unknown) => void = () => undefined;
    mockApi({
      searchOpportunities: vi.fn().mockReturnValue(
        new Promise((resolve) => {
          release = resolve;
        }),
      ),
    });

    renderBoard();
    expect(screen.getByTestId("pipeline-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("pipeline-column-empty-Discovery")).not.toBeInTheDocument();

    release({ items: [], truncated: false, examined_count: 0, next_cursor: null });
    expect(await screen.findByTestId("pipeline-column-empty-Discovery")).toBeInTheDocument();
  });

  it("surfaces a non-CrmApiError to the error boundary instead of rendering an empty board", async () => {
    mockApi({
      searchOpportunities: vi.fn().mockRejectedValue(new TypeError("value is undefined")),
    });
    // React logs the caught error; silenced so a deliberate throw does not look like a test failure.
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    render(
      <MemoryRouter initialEntries={["/pipeline"]}>
        <RootErrorBoundary>
          <PipelineBoard />
        </RootErrorBoundary>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId("root-error-boundary")).toBeInTheDocument();
    expect(screen.getByTestId("root-error-message")).toHaveTextContent("value is undefined");
    expect(screen.queryByTestId("pipeline-column-empty-Discovery")).not.toBeInTheDocument();
  });
});
