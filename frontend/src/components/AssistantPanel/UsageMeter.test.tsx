// SPDX-License-Identifier: MIT
/**
 * The usage meter accumulates every `usage` event, shows the server's per-turn cost and its per-tier
 * lines, and states the withholding only when a frame says `cost_withheld === true`. Also covers the
 * conversation running total.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  FRAME_DELIMITER,
  FRAME_PREFIX,
  type AgentEventFrame,
} from "../../types/generated/agentEvents";
import {
  CONVERSATION_COST_TESTID,
  CONVERSATION_COST_WITHHELD_TEXT,
  ConversationCostHeader,
  conversationCost,
} from "./ConversationCostHeader";
import {
  COST_WITHHELD_TEXT,
  USAGE_METER_TESTID,
  UsageMeter,
  turnCost,
  usageTotals,
} from "./UsageMeter";

afterEach(() => {
  cleanup();
});

type FrameOverrides = Omit<Partial<AgentEventFrame>, "kind"> & { kind: string };

function frameFor(overrides: FrameOverrides): string {
  const payload = {
    cache_read_tokens: null,
    cache_write_tokens: null,
    error_kind: null,
    error_message: null,
    input_tokens: null,
    output_tokens: null,
    partial_work: null,
    proposal_id: null,
    proposal_summary: null,
    text: null,
    tool_input: null,
    tool_name: null,
    tool_output: null,
    truncated: false,
    cost_lines: null,
    cost_usd: null,
    cost_withheld: null,
    resolved_tiers: null,
    ...overrides,
  };
  return `${FRAME_PREFIX}${JSON.stringify(payload)}${FRAME_DELIMITER}`;
}

function eventsFrom(...overrides: (FrameOverrides)[]) {
  return overrides
    .map(frameFor)
    .join("")
    .split(FRAME_DELIMITER)
    .filter((part) => part.startsWith(FRAME_PREFIX))
    .map((part) => JSON.parse(part.slice(FRAME_PREFIX.length)) as AgentEventFrame);
}

const USAGE = { kind: "usage", input_tokens: 1200, output_tokens: 340 } as const;

describe("a turn with no cost information shows tokens only", () => {
  it("renders no currency symbol and no withheld sentence", () => {
    const events = eventsFrom({ ...USAGE, cache_read_tokens: 900, cache_write_tokens: 120 });

    const { container } = render(<UsageMeter events={events} />);
    const text = container.textContent ?? "";

    for (const symbol of ["$", "£", "€", "¥", "USD", "usd"]) {
      expect(text).not.toContain(symbol);
    }
    expect(screen.queryByTestId("usage-cost-withheld")).not.toBeInTheDocument();
    expect(screen.queryByTestId("usage-cost")).not.toBeInTheDocument();
    expect(text).toContain("1,200");
  });
});

const PRICED_LINES = [
  {
    tier: "supervisor",
    role: "supervisor",
    model_id: "us.anthropic.claude-sonnet",
    input_tokens: 1000,
    output_tokens: 200,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    long_context_calls: 0,
    cost_usd: 0.0123,
  },
  {
    tier: "subagent",
    role: "research",
    model_id: "openai.gpt-oss",
    input_tokens: 400,
    output_tokens: 90,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    long_context_calls: 0,
    cost_usd: 0.0007,
  },
] as const;

describe("cost_withheld decides the withheld sentence, both ways", () => {
  it("shows COST_WITHHELD_TEXT and no figure when cost_withheld is true", () => {
    const lines = PRICED_LINES.map((line) => ({ ...line, cost_usd: null }));
    const events = eventsFrom({
      ...USAGE,
      kind: "completed",
      cost_withheld: true,
      cost_usd: null,
      cost_lines: lines,
    });

    const { container } = render(<UsageMeter events={events} />);

    expect(screen.getByTestId("usage-cost-withheld")).toHaveTextContent(COST_WITHHELD_TEXT);
    expect(screen.queryByTestId("usage-cost")).not.toBeInTheDocument();
    expect(container.textContent).not.toContain("$");
    // The per-tier token lines still show; only the money column is withheld.
    expect(screen.getAllByTestId("usage-cost-line")).toHaveLength(2);
  });

  it("shows the figure and no withheld sentence when cost_withheld is false", () => {
    const events = eventsFrom(
      { ...USAGE },
      { kind: "completed", cost_withheld: false, cost_usd: 0.013, cost_lines: [...PRICED_LINES] },
    );

    render(<UsageMeter events={events} />);

    expect(screen.queryByTestId("usage-cost-withheld")).not.toBeInTheDocument();
    expect(screen.getByTestId("usage-cost")).toHaveTextContent("$0.013");
  });
});

describe("per-reply cost and its per-tier lines", () => {
  it("lists one row per cost line, inside a collapsed disclosure", () => {
    const events = eventsFrom({
      kind: "completed",
      cost_withheld: false,
      cost_usd: 0.013,
      cost_lines: [...PRICED_LINES],
    });

    render(<UsageMeter events={events} />);

    const breakdown = screen.getByTestId("usage-cost-breakdown");
    expect(breakdown.tagName).toBe("DETAILS");
    expect(breakdown).not.toHaveAttribute("open");
    const rows = screen.getAllByTestId("usage-cost-line");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("Supervisor"),
      expect.stringContaining("Subagent"),
    ]);
    expect(rows[1]).toHaveTextContent("research");
    expect(rows[1]).toHaveTextContent("openai.gpt-oss");
    expect(rows[1]).toHaveTextContent("$0.0007");
  });

  it("reads the turn cost from the last frame carrying it, never summing frames", () => {
    const events = eventsFrom(
      { kind: "usage", input_tokens: 1, cost_withheld: false, cost_usd: 0.001, cost_lines: [] },
      { kind: "usage", input_tokens: 1, cost_withheld: false, cost_usd: 0.004, cost_lines: [] },
      { kind: "completed", text: "done" },
    );

    expect(turnCost(events)?.totalUsd).toBe(0.004);
  });

  it("renders the cost even for a turn that reported no usage frame", () => {
    render(
      <UsageMeter
        events={eventsFrom({ kind: "completed", cost_withheld: false, cost_usd: 0.5, cost_lines: [] })}
      />,
    );

    expect(screen.getByTestId("usage-cost")).toHaveTextContent("$0.50");
    expect(screen.queryByTestId("usage-input")).not.toBeInTheDocument();
  });
});

describe("calls priced at long-context rates", () => {
  const withLongContext = [
    { ...PRICED_LINES[0], long_context_calls: 2 },
    { ...PRICED_LINES[1], long_context_calls: 1 },
  ];

  it("shows the turn's long-context call count, and which line it came from, when above zero", () => {
    const events = eventsFrom({
      kind: "completed",
      cost_withheld: false,
      cost_usd: 0.013,
      cost_lines: withLongContext,
    });

    render(<UsageMeter events={events} />);

    expect(screen.getByTestId("usage-long-context-calls")).toHaveTextContent("3");
    const notes = screen.getAllByTestId("usage-cost-line-long-context");
    expect(notes.map((note) => note.textContent)).toEqual([
      "2 calls at long-context rates",
      "1 call at long-context rates",
    ]);
    const rows = screen.getAllByTestId("usage-cost-line");
    expect(rows[0]).toContainElement(notes[0] ?? null);
  });

  it("shows nothing about long context when no call used it", () => {
    const events = eventsFrom({
      kind: "completed",
      cost_withheld: false,
      cost_usd: 0.013,
      cost_lines: [...PRICED_LINES],
    });

    render(<UsageMeter events={events} />);

    expect(screen.queryByTestId("usage-long-context-calls")).not.toBeInTheDocument();
    expect(screen.queryByTestId("usage-cost-line-long-context")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("usage-cost-line")).toHaveLength(2);
  });

  it("still shows the count while the cost is withheld", () => {
    const events = eventsFrom({
      kind: "completed",
      cost_withheld: true,
      cost_usd: null,
      cost_lines: withLongContext.map((line) => ({ ...line, cost_usd: null })),
    });

    render(<UsageMeter events={events} />);

    expect(screen.getByTestId("usage-cost-withheld")).toBeInTheDocument();
    expect(screen.getByTestId("usage-long-context-calls")).toHaveTextContent("3");
  });

  it("ignores a line from a frame that predates the field", () => {
    const legacy: Record<string, unknown> = { ...PRICED_LINES[0] };
    delete legacy.long_context_calls;
    const events = eventsFrom({
      kind: "completed",
      cost_withheld: false,
      cost_usd: 0.013,
      cost_lines: [legacy, withLongContext[1]] as unknown as typeof withLongContext,
    });

    render(<UsageMeter events={events} />);

    expect(screen.getByTestId("usage-long-context-calls")).toHaveTextContent("1");
    expect(screen.getAllByTestId("usage-cost-line-long-context")).toHaveLength(1);
  });
});

describe("the conversation running total", () => {
  function turn(...frames: FrameOverrides[]) {
    return { prompt: "q", frames: eventsFrom(...frames), terminalState: null };
  }

  it("sums each turn's server-computed cost", () => {
    const conversation = [
      turn({ kind: "completed", cost_withheld: false, cost_usd: 0.01, cost_lines: [] }),
      turn({ kind: "tool_call", tool_name: "get_account" }),
      turn({ kind: "completed", cost_withheld: false, cost_usd: 0.0025, cost_lines: [] }),
    ];

    expect(conversationCost(conversation)).toEqual({ totalUsd: 0.0125, withheld: false });
    render(<ConversationCostHeader conversation={conversation} />);
    expect(screen.getByTestId(CONVERSATION_COST_TESTID)).toHaveTextContent("$0.0125");
  });

  it("says withheld when any turn's cost was withheld", () => {
    const conversation = [
      turn({ kind: "completed", cost_withheld: false, cost_usd: 0.01, cost_lines: [] }),
      turn({ kind: "completed", cost_withheld: true, cost_usd: null, cost_lines: [] }),
    ];

    render(<ConversationCostHeader conversation={conversation} />);

    expect(screen.getByTestId(CONVERSATION_COST_TESTID)).toHaveTextContent(
      CONVERSATION_COST_WITHHELD_TEXT,
    );
    expect(screen.getByTestId(CONVERSATION_COST_TESTID)).not.toHaveTextContent("$");
  });

  it("renders nothing until some turn reports cost", () => {
    const { container } = render(
      <ConversationCostHeader conversation={[turn({ ...USAGE })]} />,
    );

    expect(container.firstChild).toBeNull();
  });
});

describe("accumulation across a turn", () => {
  it("sums input and output tokens across every usage event", () => {
    const events = eventsFrom(
      { kind: "usage", input_tokens: 1000, output_tokens: 200 },
      { kind: "tool_call", tool_name: "get_account" },
      { kind: "usage", input_tokens: 500, output_tokens: 90 },
    );

    render(<UsageMeter events={events} />);

    // Summed, not replaced.
    expect(screen.getByTestId("usage-input")).toHaveTextContent("1,500");
    expect(screen.getByTestId("usage-output")).toHaveTextContent("290");
  });

  it("ignores events that are not usage events", () => {
    const events = eventsFrom(
      { kind: "completed", input_tokens: 9999, output_tokens: 9999 },
      { kind: "usage", input_tokens: 10, output_tokens: 5 },
    );

    render(<UsageMeter events={events} />);

    expect(screen.getByTestId("usage-input")).toHaveTextContent("10");
  });

  it("renders nothing when the turn reported no usage at all", () => {
    const { container } = render(
      <UsageMeter events={eventsFrom({ kind: "completed", text: "done" })} />,
    );

    expect(container.firstChild).toBeNull();
  });

  it("renders nothing for an empty event list", () => {
    const { container } = render(<UsageMeter events={[]} />);

    expect(container.firstChild).toBeNull();
  });

  it("treats a missing token count as zero rather than omitting the figure", () => {
    render(<UsageMeter events={eventsFrom({ kind: "usage", input_tokens: 40 })} />);

    expect(screen.getByTestId("usage-output")).toHaveTextContent("0");
  });
});

describe("cache counts distinguish `zero` from `not reported`", () => {
  it("shows a reported zero, because a cache finding nothing is a fact", () => {
    render(<UsageMeter events={eventsFrom({ ...USAGE, cache_read_tokens: 0 })} />);

    expect(screen.getByTestId("usage-cache-read")).toHaveTextContent("0");
  });

  it("omits the figure entirely when the provider reported nothing", () => {
    render(<UsageMeter events={eventsFrom(USAGE)} />);

    // A cache count the provider did not report is not rendered as `0`.
    expect(screen.queryByTestId("usage-cache-read")).not.toBeInTheDocument();
    expect(screen.queryByTestId("usage-cache-write")).not.toBeInTheDocument();
  });

  it("keeps `not reported` through summation when no event reported a cache count", () => {
    const totals = usageTotals(
      eventsFrom(
        { kind: "usage", input_tokens: 1 },
        { kind: "usage", input_tokens: 2 },
      ),
    );

    expect(totals?.cacheReadTokens).toBeNull();
  });

  it("sums cache counts once any event reports one", () => {
    const totals = usageTotals(
      eventsFrom(
        { kind: "usage", cache_read_tokens: 30 },
        { kind: "usage", cache_read_tokens: 12 },
      ),
    );

    expect(totals?.cacheReadTokens).toBe(42);
  });
});

describe("presentation", () => {
  it("is a labelled region, so the figures are reachable out of context", () => {
    render(<UsageMeter events={eventsFrom(USAGE)} />);

    expect(screen.getByTestId(USAGE_METER_TESTID)).toHaveAttribute(
      "aria-label",
      "Token usage for this turn",
    );
  });

  it("groups thousands so a large count is readable", () => {
    render(<UsageMeter events={eventsFrom({ kind: "usage", input_tokens: 1234567 })} />);

    expect(screen.getByTestId("usage-input")).toHaveTextContent("1,234,567");
  });
});
