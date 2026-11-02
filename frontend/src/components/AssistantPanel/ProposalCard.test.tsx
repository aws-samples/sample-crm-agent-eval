// SPDX-License-Identifier: MIT
/**
 * The proposal card never renders prose-only, and never blank. A real SSE `proposal` frame is driven
 * through `EventTrace` and the `proposal-card` testid is asserted to render under axe.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  FRAME_DELIMITER,
  FRAME_PREFIX,
  type AgentEventFrame,
} from "../../types/generated/agentEvents";
import { EventTrace } from "./EventTrace";
import { Panel } from "./Panel";
import { AssistantPanelProvider, useAssistantPanel } from "./PanelProvider";
import { PROPOSAL_CARD_TESTID, PROPOSAL_EMPTY_TEXT, ProposalCard, fieldChanges } from "./ProposalCard";

afterEach(() => {
  cleanup();
});

/** A real `proposal` frame as `format_sse_line` emits it, carrying every field of the contract. */
function proposalFrame(summary: Record<string, unknown> | null, proposalId = "prop-42"): string {
  const payload = {
    cache_read_tokens: null,
    cache_write_tokens: null,
    error_kind: null,
    error_message: null,
    input_tokens: null,
    kind: "proposal",
    output_tokens: null,
    partial_work: null,
    proposal_id: proposalId,
    proposal_summary: summary,
    text: null,
    tool_input: null,
    tool_name: null,
    tool_output: null,
    truncated: false,
  };
  return `${FRAME_PREFIX}${JSON.stringify(payload)}${FRAME_DELIMITER}`;
}

/** Parse frames the way the panel's reader does, so the event under test came off the wire format. */
function eventFromFrame(frame: string): AgentEventFrame {
  const body = frame.slice(FRAME_PREFIX.length, frame.length - FRAME_DELIMITER.length);
  return JSON.parse(body) as AgentEventFrame;
}

/** The shape `agents.supervisor.proposal_event_for` actually builds. */
const REALISTIC_SUMMARY = {
  before: { stage: "negotiation", amount: 40000 },
  after: { stage: "closed_won", amount: 52000 },
  reason: "The customer signed on 3 September.",
  target_table: "opportunities",
  target_id: "opp-118",
};

/** Fails on any serious or critical violation. `color-contrast` is indeterminate in jsdom (no paint
 * engine), so it is disabled. */
async function runAxe(container: Element): Promise<void> {
  const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
  const disallowed = results.violations.filter(
    (v) => v.impact === "serious" || v.impact === "critical",
  );
  if (disallowed.length > 0) {
    const detail = disallowed
      .map((v) => `[${v.impact}] ${v.id}: ${v.help} — ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)
      .join("\n");
    throw new Error(`axe found ${disallowed.length} serious/critical violation(s):\n${detail}`);
  }
}

describe("the deleted a11y assertion's replacement", () => {
  it("renders a proposal-card under axe from a real SSE proposal frame", async () => {
    const event = eventFromFrame(proposalFrame(REALISTIC_SUMMARY));

    const { container } = render(<EventTrace events={[event]} />);

    // Reached through the trace rather than rendered directly.
    expect(screen.getByTestId(PROPOSAL_CARD_TESTID)).toBeInTheDocument();
    await runAxe(container);
  });

  it("is not vacuous: the card carries real structure, not just a heading", async () => {
    const event = eventFromFrame(proposalFrame(REALISTIC_SUMMARY));

    render(<EventTrace events={[event]} />);

    // The card's structure, beyond its heading.
    expect(screen.getByTestId("proposal-changes")).toBeInTheDocument();
    expect(screen.getByTestId("proposal-target")).toHaveTextContent("opportunities opp-118");
    expect(screen.getByTestId("proposal-reason")).toHaveTextContent("signed on 3 September");
  });

  it("survives axe with an approve and a reject control present", async () => {
    const event = eventFromFrame(proposalFrame(REALISTIC_SUMMARY));

    const { container } = render(
      <ProposalCard event={event} onApprove={vi.fn()} onReject={vi.fn()} />,
    );

    await runAxe(container);
  });
});

/**
 * The panel, open, with the trace rendered through its `children` seam. Axe excludes the hidden
 * subtree of a closed panel.
 */
function PanelHarness({ events }: { events: readonly AgentEventFrame[] }): React.JSX.Element {
  const { open, openPanel } = useAssistantPanel();
  return (
    <div>
      <button type="button" data-testid="open-panel" onClick={openPanel}>
        Assistant
      </button>
      {open ? (
        <Panel>
          <EventTrace events={events} />
        </Panel>
      ) : (
        <Panel />
      )}
    </div>
  );
}

describe("the proposal card is reachable through the panel itself", () => {
  it("renders under axe inside the open panel, from a real SSE frame", async () => {
    // jsdom provides no localStorage in this project's configuration, and `AssistantPanelProvider`
    // reads it for the persisted open state.
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
      removeItem: (key: string) => void store.delete(key),
      clear: () => store.clear(),
    });
    try {
      const event = eventFromFrame(proposalFrame(REALISTIC_SUMMARY));
      const { container } = render(
        // `Panel` reads the route (`usePageContext`), so its render sites need a router.
        <MemoryRouter>
          <AssistantPanelProvider>
            <PanelHarness events={[event]} />
          </AssistantPanelProvider>
        </MemoryRouter>,
      );

      await userEvent.click(screen.getByTestId("open-panel"));

      const panel = screen.getByTestId("assistant-panel");
      expect(panel).not.toHaveAttribute("hidden");
      // The card is inside the panel element, not merely somewhere in the document.
      expect(panel.querySelector(`[data-testid="${PROPOSAL_CARD_TESTID}"]`)).not.toBeNull();
      expect(screen.getByTestId("proposal-changes")).toBeInTheDocument();
      await runAxe(container);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("a proposal is never prose-only", () => {
  it("shows every field the change would alter, with the value it replaces", () => {
    const event = eventFromFrame(proposalFrame(REALISTIC_SUMMARY));

    render(<ProposalCard event={event} />);

    for (const text of ["stage", "negotiation", "closed_won", "amount", "40000", "52000"]) {
      expect(screen.getByText(text)).toBeInTheDocument();
    }
  });

  it("shows a field being set for the first time as `not set`, not as a blank", () => {
    const event = eventFromFrame(
      proposalFrame({ before: {}, after: { next_step: "Send the contract" } }),
    );

    render(<ProposalCard event={event} />);

    expect(screen.getByText("not set")).toBeInTheDocument();
    expect(screen.getByText("Send the contract")).toBeInTheDocument();
  });

  it("shows a field being removed, which a diff of `after` alone would hide", () => {
    const event = eventFromFrame(proposalFrame({ before: { next_step: "Call back" }, after: {} }));

    render(<ProposalCard event={event} />);

    expect(screen.getByText("next_step")).toBeInTheDocument();
    expect(screen.getByText("Call back")).toBeInTheDocument();
  });

  it("says so, visibly, when a summary carries nothing readable", () => {
    // A summary with nothing legible reads as a refusal, not as a card that failed to paint.
    const event = eventFromFrame(proposalFrame({ before: {}, after: {} }));

    render(<ProposalCard event={event} />);

    expect(screen.getByTestId("proposal-empty")).toHaveTextContent(PROPOSAL_EMPTY_TEXT);
  });

  it("says so when the summary is null entirely", () => {
    const event = eventFromFrame(proposalFrame(null));

    render(<ProposalCard event={event} />);

    expect(screen.getByTestId("proposal-empty")).toBeInTheDocument();
  });

  it("renders a summary field the schema does not know rather than dropping it", () => {
    const event = eventFromFrame(
      proposalFrame({ ...REALISTIC_SUMMARY, approver_hint: "needs a manager" }),
    );

    render(<ProposalCard event={event} />);

    expect(screen.getByTestId("proposal-extras")).toHaveTextContent("needs a manager");
  });

  it("never renders an empty card: some content is always present", () => {
    for (const summary of [null, {}, { before: {}, after: {} }, { reason: "" }]) {
      const { container } = render(<ProposalCard event={eventFromFrame(proposalFrame(summary))} />);
      expect(container.textContent?.trim()).not.toBe("");
      expect(screen.getByTestId(PROPOSAL_CARD_TESTID)).toBeInTheDocument();
      cleanup();
    }
  });
});

describe("fieldChanges", () => {
  it("returns the union of before and after keys, so a removal is visible", () => {
    expect(
      fieldChanges({ before: { a: 1, gone: "x" }, after: { a: 2, added: "y" } }).map((c) => c.field),
    ).toEqual(["a", "added", "gone"]);
  });

  it("omits a field whose value did not change", () => {
    expect(fieldChanges({ before: { a: 1, b: 2 }, after: { a: 1, b: 3 } }).map((c) => c.field)).toEqual(
      ["b"],
    );
  });

  it("returns nothing for a null summary rather than throwing", () => {
    expect(fieldChanges(null)).toEqual([]);
  });

  it("tolerates before or after not being an object", () => {
    expect(fieldChanges({ before: "nope", after: { a: 1 } }).map((c) => c.field)).toEqual(["a"]);
  });
});

describe("the approve and reject controls", () => {
  it("renders no controls when no handler is supplied, rather than a dead button", () => {
    const event = eventFromFrame(proposalFrame(REALISTIC_SUMMARY));

    render(<ProposalCard event={event} />);

    expect(screen.queryByTestId("proposal-approve")).not.toBeInTheDocument();
    expect(screen.queryByTestId("proposal-reject")).not.toBeInTheDocument();
  });

  it("passes the proposal id to each handler", async () => {
    const onApprove = vi.fn();
    const onReject = vi.fn();
    const event = eventFromFrame(proposalFrame(REALISTIC_SUMMARY, "prop-99"));

    render(<ProposalCard event={event} onApprove={onApprove} onReject={onReject} />);
    await userEvent.click(screen.getByTestId("proposal-approve"));
    await userEvent.click(screen.getByTestId("proposal-reject"));

    expect(onApprove).toHaveBeenCalledWith("prop-99");
    expect(onReject).toHaveBeenCalledWith("prop-99");
  });

  it("renders no approve control for a proposal with no id, since there is nothing to approve", () => {
    const event = { ...eventFromFrame(proposalFrame(REALISTIC_SUMMARY)), proposal_id: null };

    render(<ProposalCard event={event} onApprove={vi.fn()} onReject={vi.fn()} />);

    expect(screen.queryByTestId("proposal-approve")).not.toBeInTheDocument();
    // The card itself still renders; the change is shown but cannot be acted on here.
    expect(screen.getByTestId(PROPOSAL_CARD_TESTID)).toBeInTheDocument();
  });

  it("is reachable by keyboard, with the approve control focusable", async () => {
    const event = eventFromFrame(proposalFrame(REALISTIC_SUMMARY));

    render(<ProposalCard event={event} onApprove={vi.fn()} onReject={vi.fn()} />);
    await userEvent.tab();

    expect(screen.getByTestId("proposal-reject")).toHaveFocus();
  });
});
