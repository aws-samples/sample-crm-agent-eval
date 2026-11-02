// SPDX-License-Identifier: MIT
/**
 * Tests for `useAssistantTurn`: the request body carries a descriptor and no record collection
 * (checked structurally, by walking the serialised body for any array of objects); a row-count
 * divergence renders a disclosure; an `opaque` context renders "this view can't be summarised".
 */
import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { PageContext } from "../../types/generated/pageContext";
import type { AgentStreamEvent, TranscriptEntry } from "../../hooks/useAgentStream";
import {
  NOT_COMPARABLE_REASONS,
  OPAQUE_MESSAGE,
  PAGE_CONTEXT_WIRE_KEY,
  TRUNCATION_MARKER,
  agentRowCount,
  buildTurnRequest,
  deriveDisclosure,
  transcriptRowCount,
  useAssistantTurn,
} from "./useAssistantTurn";

const LIST_CONTEXT: PageContext = {
  kind: "list",
  route: "/opportunities",
  entity_type: "opportunities",
  query: { filters: { stage: "Negotiation" }, sort: { field: "amount", direction: "desc" } },
};

const OPAQUE_CONTEXT: PageContext = {
  kind: "opaque",
  route: "/approvals",
  reason: "this view is not one of the CRM entity collections the assistant can query directly",
};

const ENTITY_CONTEXT: PageContext = {
  kind: "entity",
  route: "/accounts/acct-1",
  entity_type: "accounts",
  entity_id: "acct-1",
};

function entry(event: AgentStreamEvent, key = "k"): TranscriptEntry {
  return { key, event, unrecognised: false };
}

function toolResult(
  payload: Record<string, unknown> | null,
  overrides: Partial<AgentStreamEvent> = {},
): AgentStreamEvent {
  return { kind: "tool_result", tool_output: payload, ...overrides };
}

/** Every array-of-object found anywhere in `value`, as JSON paths. A record collection is a row set. */
function recordCollectionPaths(value: unknown, path = "$"): string[] {
  if (Array.isArray(value)) {
    const found = value.some((item) => typeof item === "object" && item !== null);
    return found ? [path] : [];
  }
  if (typeof value === "object" && value !== null) {
    return Object.entries(value).flatMap(([key, nested]) =>
      recordCollectionPaths(nested, `${path}.${key}`),
    );
  }
  return [];
}

describe("the request body carries a descriptor and no rows", () => {
  it("writes the descriptor under the snake_case wire key the relay reads", () => {
    const body = buildTurnRequest("summarise this", "sess-1", { pageContext: LIST_CONTEXT });
    expect(PAGE_CONTEXT_WIRE_KEY).toBe("page_context");
    expect(body.page_context).toEqual(LIST_CONTEXT);
    // The camelCase spelling does not reach the wire.
    expect(Object.keys(body)).not.toContain("pageContext");
  });

  it("omits the key entirely when there is no descriptor, matching decodeBody's undefined branch", () => {
    const body = buildTurnRequest("hello", "sess-1");
    expect(PAGE_CONTEXT_WIRE_KEY in body).toBe(false);
  });

  it("carries no record collection anywhere in the serialised body", () => {
    const body = buildTurnRequest("summarise this", "sess-1", {
      pageContext: LIST_CONTEXT,
      tuning: { effort: "low" },
    });
    expect(recordCollectionPaths(JSON.parse(JSON.stringify(body)))).toEqual([]);
  });

  it("the row-absence check is not vacuous", () => {
    // Non-vacuity: the same walk finds a planted row set.
    const planted = { page_context: { kind: "list", rows: [{ id: "opp-1" }] } };
    expect(recordCollectionPaths(planted)).toEqual(["$.page_context.rows"]);
  });

  it("has no parameter through which a caller could pass rows", () => {
    // `buildTurnRequest`'s options admit only `pageContext`, `tuning`, and `resumeFrom`.
    const body = buildTurnRequest("x", "sess-1", { pageContext: LIST_CONTEXT });
    expect(Object.keys(body).sort()).toEqual(["page_context", "prompt", "session_id"]);
  });

  it("sets resume_from only on a resume, and keeps the descriptor across one", () => {
    expect("resume_from" in buildTurnRequest("x", "s", { pageContext: LIST_CONTEXT })).toBe(false);
    const resumed = buildTurnRequest("x", "s", { pageContext: LIST_CONTEXT, resumeFrom: 4 });
    expect(resumed.resume_from).toBe(4);
    expect(resumed.page_context).toEqual(LIST_CONTEXT);
  });

  it("the hook's builder carries the current descriptor without the caller restating it", () => {
    const { result } = renderHook(() =>
      useAssistantTurn({ pageContext: LIST_CONTEXT, transcript: [] }),
    );
    expect(result.current.buildRequest("summarise this", "sess-1").page_context).toEqual(
      LIST_CONTEXT,
    );
  });
});

describe("a row-count divergence renders a disclosure", () => {
  it("discloses when the agent's count differs from the view's", () => {
    const disclosure = deriveDisclosure({
      pageContext: LIST_CONTEXT,
      viewRowCount: 12,
      transcript: [entry(toolResult({ items: [{ id: "a" }, { id: "b" }] }))],
    });
    expect(disclosure.kind).toBe("row-count-divergence");
    if (disclosure.kind !== "row-count-divergence") throw new Error("unreachable");
    expect(disclosure.viewRowCount).toBe(12);
    expect(disclosure.agentRowCount).toBe(2);
    expect(disclosure.message).toContain("12");
    expect(disclosure.message).toContain("2");
    // The disclosure must not present the agent's set as the user's.
    expect(disclosure.message).toMatch(/different records|not this view/i);
  });

  it("stays silent when the counts match", () => {
    expect(
      deriveDisclosure({
        pageContext: LIST_CONTEXT,
        viewRowCount: 2,
        transcript: [entry(toolResult({ items: [{ id: "a" }, { id: "b" }] }))],
      }),
    ).toEqual({ kind: "none" });
  });

  it("says nothing on a detail page, which shows no collection to compare", () => {
    expect(
      deriveDisclosure({
        pageContext: ENTITY_CONTEXT,
        transcript: [entry(toolResult({ items: [{ id: "a" }] }))],
      }).kind,
    ).toBe("none");
  });

  it("says nothing when the agent never queried, so there is nothing to diverge from", () => {
    expect(
      deriveDisclosure({
        pageContext: LIST_CONTEXT,
        viewRowCount: 5,
        transcript: [entry({ kind: "completed", text: "Here is a summary." })],
      }).kind,
    ).toBe("none");
  });

  it("compares against the LAST tool result, which is the one the agent answered from", () => {
    const disclosure = deriveDisclosure({
      pageContext: LIST_CONTEXT,
      viewRowCount: 3,
      transcript: [
        entry(toolResult({ items: [{ id: "a" }, { id: "b" }, { id: "c" }] }), "first"),
        entry(toolResult({ items: [{ id: "a" }] }), "second"),
      ],
    });
    expect(disclosure.kind).toBe("row-count-divergence");
  });

  it("reads back past a get_page_context result that carries no rows to the countable one (F4)", () => {
    // A later `get_page_context` result, which carries no row collection, does not shadow the
    // earlier search count.
    const disclosure = deriveDisclosure({
      pageContext: LIST_CONTEXT,
      viewRowCount: 32,
      transcript: [
        entry(toolResult({ items: Array.from({ length: 32 }, (_, i) => ({ id: `opp-${i}` })) }), "search"),
        entry(toolResult({ view: "board", columns: [] }), "page-context"),
      ],
    });
    expect(disclosure.kind).toBe("none");
    expect(transcriptRowCount([
      entry(toolResult({ items: Array.from({ length: 32 }, (_, i) => ({ id: `opp-${i}` })) }), "search"),
      entry(toolResult({ view: "board", columns: [] }), "page-context"),
    ])).toEqual({ count: 32 });
  });

  it("reads back past a trailing error frame to the countable tool result (F4)", () => {
    const disclosure = deriveDisclosure({
      pageContext: LIST_CONTEXT,
      viewRowCount: 32,
      transcript: [
        entry(toolResult({ items: Array.from({ length: 32 }, (_, i) => ({ id: `opp-${i}` })) }), "search"),
        entry({ kind: "error", error_kind: "validation_failed" }, "validation"),
      ],
    });
    expect(disclosure.kind).toBe("none");
  });

  it("still reports unreadable when NO frame carries a recognised collection", () => {
    const disclosure = deriveDisclosure({
      pageContext: LIST_CONTEXT,
      viewRowCount: 7,
      transcript: [
        entry(toolResult({ view: "board", columns: [] }), "page-context-a"),
        entry(toolResult({ some_future_key: [{ id: "a" }] }), "page-context-b"),
      ],
    });
    expect(disclosure.kind).toBe("not-comparable");
    if (disclosure.kind !== "not-comparable") throw new Error("unreachable");
    expect(disclosure.reason).toBe("unrecognisedPayload");
  });

  it("reads each tool's own row-bearing key", () => {
    expect(agentRowCount(toolResult({ activities: [{ id: "act-1" }] }))).toEqual({ count: 1 });
    expect(agentRowCount(toolResult({ items: [] }))).toEqual({ count: 0 });
  });

  it("unwraps the off-proxy {statusCode, body} envelope the action Lambdas actually send (F7)", () => {
    // Off the proxy path the collection is nested under `body`.
    expect(
      agentRowCount(toolResult({ statusCode: 200, body: { items: Array(32).fill({ id: "opp" }) } })),
    ).toEqual({ count: 32 });
    // The truncation marker is honoured inside the body envelope too.
    expect(
      agentRowCount(toolResult({ statusCode: 200, body: { items: [{ id: "a" }, TRUNCATION_MARKER] } })),
    ).toEqual({ unreadable: "truncated" });
    // A body-wrapped payload with no recognised collection is still unreadable, not a false count.
    expect(agentRowCount(toolResult({ statusCode: 200, body: { kind: "board" } }))).toEqual({
      unreadable: "unrecognisedPayload",
    });
  });

  it("still reads the flat shape the stub and error path use", () => {
    // A flat tool_output (the stub, the error path) falls back to the payload itself.
    expect(agentRowCount(toolResult({ items: [{ id: "a" }, { id: "b" }] }))).toEqual({ count: 2 });
    expect(agentRowCount(toolResult({ statusCode: 400, error: "nope" }))).toEqual({
      unreadable: "unrecognisedPayload",
    });
  });

  describe("truncation is not divergence, and is never silence", () => {
    it("a declared truncation is not comparable rather than a smaller count", () => {
      const disclosure = deriveDisclosure({
        pageContext: LIST_CONTEXT,
        viewRowCount: 40,
        transcript: [entry(toolResult({ items: [{ id: "a" }] }, { truncated: true }))],
      });
      expect(disclosure.kind).toBe("not-comparable");
      if (disclosure.kind !== "not-comparable") throw new Error("unreachable");
      expect(disclosure.reason).toBe("truncated");
      expect(disclosure.message).toContain("lower bound");
    });

    it("the truncation marker in the payload is honoured even without the flag", () => {
      expect(agentRowCount(toolResult({ items: [{ id: "a" }, TRUNCATION_MARKER] }))).toEqual({
        unreadable: "truncated",
      });
    });

    it("the flag is checked before the payload, so a flagged result with no marker still reports truncated", () => {
      expect(agentRowCount(toolResult({ items: [{ id: "a" }] }, { truncated: true }))).toEqual({
        unreadable: "truncated",
      });
    });

    it("an unrecognised payload shape is not comparable rather than zero rows", () => {
      // An unrecognised payload is not read as 0 rows.
      const disclosure = deriveDisclosure({
        pageContext: LIST_CONTEXT,
        viewRowCount: 7,
        transcript: [entry(toolResult({ some_future_key: [{ id: "a" }] }))],
      });
      expect(disclosure.kind).toBe("not-comparable");
      if (disclosure.kind !== "not-comparable") throw new Error("unreachable");
      expect(disclosure.reason).toBe("unrecognisedPayload");
    });

    it("a null payload is not comparable", () => {
      expect(agentRowCount(toolResult(null))).toEqual({ unreadable: "unrecognisedPayload" });
    });

    it("this view's own truncation is not comparable either, and is not a divergence", () => {
      /*
       * A view that stopped at its own ceiling has a lower-bound count, so it is not compared.
       */
      const diverging = {
        pageContext: LIST_CONTEXT,
        viewRowCount: 3,
        transcript: [entry(toolResult({ opportunities: [{ id: "a" }, { id: "b" }] }))],
      };

      // Non-vacuity: the same inputs without the flag produce a divergence.
      expect(deriveDisclosure(diverging).kind).toBe("row-count-divergence");

      const disclosure = deriveDisclosure({ ...diverging, viewTruncated: true });
      expect(disclosure.kind).toBe("not-comparable");
      if (disclosure.kind !== "not-comparable") throw new Error("unreachable");
      expect(disclosure.reason).toBe("viewTruncated");
      expect(disclosure.message).toContain("this view's own result stopped early");
    });

    it("every not-comparable reason has a message that names the consequence", () => {
      for (const [reason, text] of Object.entries(NOT_COMPARABLE_REASONS)) {
        expect(text, reason).toMatch(/compare|row count/i);
      }
    });
  });

  it("transcriptRowCount returns undefined only when no tool_result exists at all", () => {
    expect(transcriptRowCount([])).toBeUndefined();
    expect(transcriptRowCount([entry({ kind: "usage", input_tokens: 5 })])).toBeUndefined();
    expect(transcriptRowCount([entry(toolResult({ items: [] }))])).toEqual({ count: 0 });
  });
});

describe("an opaque context says the view can't be summarised", () => {
  it("renders the sentence and the reason, not a silent empty state", () => {
    const disclosure = deriveDisclosure({ pageContext: OPAQUE_CONTEXT, transcript: [] });
    expect(disclosure.kind).toBe("opaque");
    if (disclosure.kind !== "opaque") throw new Error("unreachable");
    expect(disclosure.message).toContain(OPAQUE_MESSAGE);
    expect(disclosure.message).toContain(OPAQUE_CONTEXT.reason);
  });

  it("wins over a count comparison, because there was no described query to re-execute", () => {
    const disclosure = deriveDisclosure({
      pageContext: OPAQUE_CONTEXT,
      viewRowCount: 9,
      transcript: [entry(toolResult({ items: [{ id: "a" }] }))],
    });
    expect(disclosure.kind).toBe("opaque");
  });

  it("never presents an approximation as the same thing", () => {
    const disclosure = deriveDisclosure({ pageContext: OPAQUE_CONTEXT, transcript: [] });
    if (disclosure.kind !== "opaque") throw new Error("unreachable");
    expect(disclosure.message).not.toMatch(/approximat|roughly|about the same/i);
  });

  it("the hook surfaces it", () => {
    const { result } = renderHook(() =>
      useAssistantTurn({ pageContext: OPAQUE_CONTEXT, transcript: [] }),
    );
    expect(result.current.disclosure.kind).toBe("opaque");
  });
});

describe("the disclosure is stable across re-renders with unchanged input", () => {
  it("memoises on the descriptor, count, and transcript", () => {
    const transcript = [entry(toolResult({ items: [{ id: "a" }] }))];
    const { result, rerender } = renderHook(
      (props: Parameters<typeof useAssistantTurn>[0]) => useAssistantTurn(props),
      { initialProps: { pageContext: LIST_CONTEXT, viewRowCount: 4, transcript } },
    );
    const first = result.current.disclosure;
    rerender({ pageContext: LIST_CONTEXT, viewRowCount: 4, transcript });
    expect(result.current.disclosure).toBe(first);
  });
});
