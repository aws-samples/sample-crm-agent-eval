// SPDX-License-Identifier: MIT
/**
 * The trace's contract: one turn's frames become folded steps plus always-visible asides.
 *
 * - Tool calls are steps inside a `<details>` disclosure, open while the turn streams and collapsed
 *   once it completes. An adjacent `tool_call`+`tool_result` are one step.
 * - `usage` and `completed` render nothing here.
 * - `error`, a `proposal`, a `truncated` result and a kind this build does not declare render outside
 *   the disclosure, always visible.
 *
 * Events are built by parsing real SSE frames through `FRAME_PREFIX`/`FRAME_DELIMITER`.
 */

import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useAgentStream } from "../../hooks/useAgentStream";

import {
  ERROR_KINDS,
  FRAME_DELIMITER,
  FRAME_PREFIX,
  type AgentEventFrame,
  type ErrorKind,
} from "../../types/generated/agentEvents";
import {
  EventTrace,
  TRACE_STEPS_TESTID,
  TRUNCATED_LABEL,
  TRUNCATION_NOTICE_TESTID,
  UNKNOWN_KIND_LABEL,
  errorHeadline,
} from "./EventTrace";
import { PROPOSAL_CARD_TESTID } from "./ProposalCard";
import { toEventFrame } from "./eventFrame";

/** A frame's fields, with `kind` widened to `string` so a fixture can carry an undeclared kind. */
type FrameOverrides = Omit<Partial<AgentEventFrame>, "kind"> & { kind: string };

/** Every field `format_sse_line` emits, so a fixture is never narrower than the real frame. */
function frameFor(overrides: FrameOverrides): string {
  const payload = {
    cache_read_tokens: null,
    cache_write_tokens: null,
    error_kind: null,
    error_message: null,
    event_id: null,
    input_tokens: null,
    output_tokens: null,
    partial: false,
    partial_work: null,
    proposal_id: null,
    proposal_summary: null,
    text: null,
    tool_input: null,
    tool_name: null,
    tool_output: null,
    truncated: false,
    ...overrides,
  };
  return `${FRAME_PREFIX}${JSON.stringify(payload)}${FRAME_DELIMITER}`;
}

/** Parse a run of SSE frames the way the panel's reader does. */
export function parseFrames(raw: string): readonly AgentEventFrame[] {
  return raw
    .split(FRAME_DELIMITER)
    .filter((part) => part.startsWith(FRAME_PREFIX))
    .map((part) => JSON.parse(part.slice(FRAME_PREFIX.length)) as AgentEventFrame);
}

function eventsFrom(...overrides: FrameOverrides[]) {
  return parseFrames(overrides.map(frameFor).join(""));
}

// `globals: false` in the vitest config disables Testing Library's automatic cleanup.
afterEach(() => {
  cleanup();
});

describe("tool calls become folded steps", () => {
  it("labels a step by the tool's display_label, not its wire name", () => {
    const events = eventsFrom({ kind: "tool_call", tool_name: "get_account" });

    render(<EventTrace events={events} />);

    // `get_account` → "Get account" (its display_label), never the raw identifier.
    expect(screen.getByText("Get account")).toBeInTheDocument();
    expect(screen.queryByText("get_account")).not.toBeInTheDocument();
  });

  it("pairs an adjacent tool_call and tool_result into one step, not two", () => {
    const events = eventsFrom(
      { kind: "tool_call", tool_name: "search_opportunities" },
      { kind: "tool_result", tool_name: "search_opportunities" },
    );

    render(<EventTrace events={events} />);

    expect(screen.getAllByTestId("trace-step")).toHaveLength(1);
  });

  it("keeps a tool_result with no preceding call as its own step rather than dropping it", () => {
    const events = eventsFrom({ kind: "tool_result", tool_name: "get_account" });

    render(<EventTrace events={events} />);

    expect(screen.getAllByTestId("trace-step")).toHaveLength(1);
  });

  it("shows a tool this build's toolspec does not know by its raw name", () => {
    const events = eventsFrom({ kind: "tool_call", tool_name: "list_opportunities" });

    render(<EventTrace events={events} />);

    expect(screen.getByText("list_opportunities")).toBeInTheDocument();
  });

  it("summarises a step from a value on the wire, never an invented one", () => {
    const events = eventsFrom({
      kind: "tool_call",
      tool_name: "get_account",
      tool_input: { account_id: "acct-42" },
    });

    render(<EventTrace events={events} />);

    expect(screen.getByText(/acct-42/)).toBeInTheDocument();
  });

  it("marks a truncated tool result rather than presenting a partial answer as whole", () => {
    const events = eventsFrom({ kind: "tool_result", tool_name: "search_opportunities", truncated: true });

    render(<EventTrace events={events} />);

    // Both the per-step badge (inside the disclosure) and the turn-level notice (outside it) mark it.
    expect(screen.getAllByText(TRUNCATED_LABEL).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByTestId(TRUNCATION_NOTICE_TESTID)).toBeInTheDocument();
  });

  it("keeps the truncation notice visible OUTSIDE the collapsed steps on a completed turn", () => {
    // A completed turn collapses the steps disclosure; the turn-level notice stays visible.
    const events = eventsFrom(
      { kind: "tool_call", tool_name: "search_opportunities" },
      { kind: "tool_result", tool_name: "search_opportunities", truncated: true },
      { kind: "completed", text: "Here are the first few." },
    );

    render(<EventTrace events={events} />);

    // Steps are collapsed (turn complete)…
    expect(screen.getByTestId(TRACE_STEPS_TESTID)).not.toHaveAttribute("open");
    // …but the notice is outside the disclosure and remains visible.
    expect(screen.getByTestId(TRUNCATION_NOTICE_TESTID)).toBeVisible();
  });

  it("opens the disclosure while the turn is still streaming", () => {
    const events = eventsFrom({ kind: "tool_call", tool_name: "get_account" });

    render(<EventTrace events={events} />);

    expect(screen.getByTestId(TRACE_STEPS_TESTID)).toHaveAttribute("open");
  });

  it("collapses the disclosure once the turn has completed", () => {
    const events = eventsFrom(
      { kind: "tool_call", tool_name: "get_account" },
      { kind: "completed", text: "Done." },
    );

    render(<EventTrace events={events} />);

    expect(screen.getByTestId(TRACE_STEPS_TESTID)).not.toHaveAttribute("open");
  });
});

describe("usage and completed are not steps", () => {
  it("renders nothing for a turn that is only usage", () => {
    const { container } = render(<EventTrace events={eventsFrom({ kind: "usage", input_tokens: 10 })} />);

    expect(container.firstChild).toBeNull();
  });

  it("does not render the completed text — the answer bubble owns it", () => {
    const events = eventsFrom({ kind: "completed", text: "The pipeline totals $2M." });

    const { container } = render(<EventTrace events={events} />);

    expect(container.firstChild).toBeNull();
    expect(screen.queryByText("The pipeline totals $2M.")).not.toBeInTheDocument();
  });

  it("renders nothing at all for an empty turn rather than an empty container", () => {
    const { container } = render(<EventTrace events={[]} />);

    expect(container.firstChild).toBeNull();
  });

  it("does not render a text_delta as a step or a row — the answer bubble owns the stream", () => {
    const events = eventsFrom(
      { kind: "text_delta", text: "the pipeline ", event_id: "d1", partial: true },
      { kind: "text_delta", text: "totals $2M.", event_id: "d1", partial: true },
    );

    const { container } = render(<EventTrace events={events} />);

    // Nothing is rendered for a stream-only turn.
    expect(container.firstChild).toBeNull();
  });
});

describe("a proposal always shows as its card, outside the folded steps", () => {
  it("renders the proposal card", () => {
    const events = eventsFrom({
      kind: "proposal",
      proposal_id: "prop-1",
      proposal_summary: { after: { stage: "closed_won" }, before: { stage: "negotiation" } },
    });

    render(<EventTrace events={events} />);

    expect(screen.getByTestId(PROPOSAL_CARD_TESTID)).toBeInTheDocument();
  });
});

describe("an ErrorKind is surfaced, not swallowed", () => {
  it.each(ERROR_KINDS)("gives %s its own sentence", (errorKind: ErrorKind) => {
    const events = eventsFrom({ kind: "error", error_kind: errorKind });

    render(<EventTrace events={events} />);

    expect(screen.getByTestId("trace-row-error")).toBeInTheDocument();
    expect(screen.getByText(errorHeadline(events[0]!))).toBeInTheDocument();
  });

  it("gives each ErrorKind a distinct sentence, so the kinds are not interchangeable", () => {
    const headlines = ERROR_KINDS.map((errorKind) =>
      errorHeadline(eventsFrom({ kind: "error", error_kind: errorKind })[0]!),
    );

    expect(new Set(headlines).size).toBe(ERROR_KINDS.length);
  });

  it("says so when an error carries no kind at all", () => {
    render(<EventTrace events={eventsFrom({ kind: "error", error_kind: null })} />);

    expect(screen.getByText(/stopped without saying why/i)).toBeInTheDocument();
  });

  it("shows an error_kind the schema does not declare verbatim rather than dropping it", () => {
    const events = eventsFrom({ kind: "error", error_kind: "quota_exceeded" as ErrorKind });

    render(<EventTrace events={events} />);

    expect(screen.getByText(/quota_exceeded/)).toBeInTheDocument();
  });

  it("shows the model's own message alongside the kind, not instead of it", () => {
    const events = eventsFrom({
      kind: "error",
      error_kind: "budget_exhausted",
      error_message: "Stopped after 12 tool calls.",
    });

    render(<EventTrace events={events} />);

    expect(screen.getByText(/hit its budget/i)).toBeInTheDocument();
    expect(screen.getByText("Stopped after 12 tool calls.")).toBeInTheDocument();
  });

  it("shows partial work a budget-exhausted turn completed before stopping", () => {
    const events = eventsFrom({
      kind: "error",
      error_kind: "budget_exhausted",
      partial_work: "updated 2 of 5 opportunities",
    });

    render(<EventTrace events={events} />);

    expect(screen.getByTestId("trace-partial-work")).toHaveTextContent("updated 2 of 5 opportunities");
  });
});

describe("an unrecognised kind renders visibly", () => {
  it.each(["delegation", "keepalive", "budget_exhausted", "subagent_boundary"])(
    "renders a %s event instead of filtering it",
    (kind) => {
      // `budget_exhausted` is an ErrorKind, not an event kind; `subagent_boundary` is a kind this
      // build's schema does not declare.
      const events = eventsFrom({ kind, text: "an unknown step" });

      render(<EventTrace events={events} />);

      expect(screen.getByTestId(`trace-row-${kind}`)).toBeInTheDocument();
      expect(screen.getByText(UNKNOWN_KIND_LABEL)).toBeInTheDocument();
      expect(screen.getByText(kind)).toBeInTheDocument();
    },
  );

  it("does not rest on colour alone: the unknown badge is the dashed, hatched pending variant", () => {
    render(<EventTrace events={eventsFrom({ kind: "some_future_kind" })} />);

    const badge = screen.getByText(UNKNOWN_KIND_LABEL);
    expect(badge.className).toContain("border-dashed");
    expect(badge.className).toContain("bg-pending-hatch");
  });

  it("keeps unknown asides in the order they arrived", () => {
    const events = eventsFrom(
      { kind: "alpha" },
      { kind: "error", error_kind: "tool_failure" },
      { kind: "omega" },
    );

    const { container } = render(<EventTrace events={events} />);

    const kinds = [...container.querySelectorAll("[data-kind]")].map((node) =>
      node.getAttribute("data-kind"),
    );
    expect(kinds).toEqual(["alpha", "error", "omega"]);
  });
});

describe("no aside renders blank", () => {
  it("never renders a blank aside, even for an event carrying no text", () => {
    const events = eventsFrom({ kind: "error", error_kind: "tool_failure" }, { kind: "mystery" });

    const { container } = render(<EventTrace events={events} />);

    for (const row of container.querySelectorAll("[data-kind]")) {
      expect(row.textContent?.trim()).not.toBe("");
    }
  });
});

describe("heartbeat frames never reach the trace", () => {
  it("would render a raw heartbeat visibly, so the upstream drop is what keeps it out", () => {
    render(<EventTrace events={eventsFrom({ kind: "heartbeat" })} />);

    expect(screen.getByTestId("trace-row-heartbeat")).toBeInTheDocument();
  });

  it("renders no heartbeat row for frames that came through useAgentStream", async () => {
    const body = [
      { kind: "heartbeat" },
      { kind: "tool_call", tool_name: "get_account", tool_input: {} },
      { kind: "heartbeat" },
      { kind: "completed", text: "Done." },
    ]
      .map((frame) => `data: ${JSON.stringify(frame)}\n\n`)
      .join("");
    const fetchImpl = vi.fn().mockImplementation(() =>
      Promise.resolve(new Response(body, { status: 200 })),
    );
    const { result } = renderHook(() =>
      useAgentStream({
        agentBaseUrl: "/agent",
        agentRuntimeArn: "arn:test",
        getAccessToken: () => Promise.resolve("tok"),
        fetchImpl,
      }),
    );
    await act(async () => {
      await result.current.sendPrompt("hello");
    });
    await waitFor(() => expect(result.current.turnEnded).toBe(true));

    render(<EventTrace events={result.current.transcript.map((entry) => toEventFrame(entry.event))} />);

    expect(screen.queryByTestId("trace-row-heartbeat")).not.toBeInTheDocument();
    expect(screen.queryByText(UNKNOWN_KIND_LABEL)).not.toBeInTheDocument();
  });
});
