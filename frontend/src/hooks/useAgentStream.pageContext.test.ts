// SPDX-License-Identifier: MIT
/**
 * The page descriptor reaches the request body as `page_context` on every path that issues a request,
 * including a reconnect, which replays it from `promptRef`.
 */
import { renderHook, waitFor, act } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useAgentStream } from "./useAgentStream";
import { buildTurnRequest } from "../components/AssistantPanel/useAssistantTurn";
import type { PageContext } from "../types/generated/pageContext";

const DESCRIPTOR: PageContext = {
  kind: "list",
  route: "/opportunities",
  entity_type: "opportunities",
  query: { filters: { stage: "Negotiation" } },
};

function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  let index = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index >= frames.length) {
        controller.close();
        return;
      }
      controller.enqueue(encoder.encode(frames[index]));
      index += 1;
    },
  });
  return { ok: true, status: 200, body: stream } as unknown as Response;
}

/** A response whose body errors partway through, forcing the reconnect path. */
function droppingResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  let index = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index >= frames.length) {
        controller.error(new Error("connection dropped"));
        return;
      }
      controller.enqueue(encoder.encode(frames[index]));
      index += 1;
    },
  });
  return { ok: true, status: 200, body: stream } as unknown as Response;
}

function frame(obj: Record<string, unknown>): string {
  return `data: ${JSON.stringify(obj)}\n\n`;
}

const baseOptions = {
  agentBaseUrl: "/agent",
  agentRuntimeArn: "arn:test",
  getAccessToken: () => Promise.resolve("tok-123"),
};

/** The parsed request bodies every `fetch` call was made with, in order. */
function sentBodies(fetchImpl: ReturnType<typeof vi.fn>): Record<string, unknown>[] {
  return fetchImpl.mock.calls.map(
    (call) => JSON.parse((call[1] as RequestInit).body as string) as Record<string, unknown>,
  );
}

describe("the descriptor reaches the wire under the key the relay reads", () => {
  it("sends page_context on the initial request", async () => {
    const fetchImpl = vi
      .fn()
      .mockImplementation(() => Promise.resolve(sseResponse([frame({ kind: "completed" })])));
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("summarise this", { pageContext: DESCRIPTOR });
    });

    const [body] = sentBodies(fetchImpl);
    expect(body["page_context"]).toEqual(DESCRIPTOR);
    // The wire key is snake_case; camelCase never appears as a wire key.
    expect(Object.keys(body)).not.toContain("pageContext");
  });

  it("omits the key entirely when no descriptor is given, matching decodeBody's undefined branch", async () => {
    const fetchImpl = vi
      .fn()
      .mockImplementation(() => Promise.resolve(sseResponse([frame({ kind: "completed" })])));
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    expect("page_context" in sentBodies(fetchImpl)[0]).toBe(false);
  });

  it("sends exactly the body useAssistantTurn composes, so the two cannot drift", async () => {
    // The transport agrees key for key with `useAssistantTurn.buildTurnRequest`.
    const fetchImpl = vi
      .fn()
      .mockImplementation(() => Promise.resolve(sseResponse([frame({ kind: "completed" })])));
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("summarise this", { pageContext: DESCRIPTOR });
    });

    const sent = sentBodies(fetchImpl)[0];
    const composed = buildTurnRequest("summarise this", String(sent["session_id"]), {
      pageContext: DESCRIPTOR,
      resumeFrom: 0,
    });
    expect(sent["prompt"]).toBe(composed.prompt);
    expect(sent["page_context"]).toEqual(composed.page_context);
    expect(Object.keys(sent).filter((k) => k.startsWith("page"))).toEqual(["page_context"]);
  });

  it("carries no record collection anywhere in the sent body", async () => {
    const fetchImpl = vi
      .fn()
      .mockImplementation(() => Promise.resolve(sseResponse([frame({ kind: "completed" })])));
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("summarise this", { pageContext: DESCRIPTOR });
    });

    const paths: string[] = [];
    const walk = (value: unknown, path: string): void => {
      if (Array.isArray(value)) {
        if (value.some((item) => typeof item === "object" && item !== null)) paths.push(path);
        return;
      }
      if (typeof value === "object" && value !== null) {
        for (const [key, nested] of Object.entries(value)) walk(nested, `${path}.${key}`);
      }
    };
    walk(sentBodies(fetchImpl)[0], "$");
    expect(paths).toEqual([]);
  });
});

describe("a resume replays the descriptor (the case a happy-path test misses)", () => {
  it("the automatic reconnect after a mid-stream drop re-sends the same page_context", async () => {
    const fetchImpl = vi
      .fn()
      // First attempt drops after one frame; the retry completes.
      .mockImplementationOnce(() =>
        Promise.resolve(droppingResponse([frame({ kind: "usage", input_tokens: 1 })])),
      )
      .mockImplementation(() => Promise.resolve(sseResponse([frame({ kind: "completed" })])));

    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 2 }),
    );

    await act(async () => {
      await result.current.sendPrompt("summarise this", { pageContext: DESCRIPTOR });
    });
    await waitFor(() => expect(fetchImpl.mock.calls.length).toBeGreaterThan(1), { timeout: 5000 });

    const bodies = sentBodies(fetchImpl);
    expect(bodies.length).toBeGreaterThan(1);
    for (const [index, body] of bodies.entries()) {
      expect(body["page_context"], `request ${index} lost the descriptor`).toEqual(DESCRIPTOR);
    }
    // The second request is a resume, not a fresh turn.
    expect(bodies[1]["session_id"]).toBe(bodies[0]["session_id"]);
    expect(bodies[1]["resume_from"]).not.toBe(0);
  });

  it("the manual reconnect() re-sends the same page_context", async () => {
    const fetchImpl = vi
      .fn()
      .mockImplementationOnce(() =>
        Promise.resolve(droppingResponse([frame({ kind: "usage", input_tokens: 1 })])),
      )
      .mockImplementation(() => Promise.resolve(sseResponse([frame({ kind: "completed" })])));

    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 0 }),
    );

    await act(async () => {
      await result.current.sendPrompt("summarise this", { pageContext: DESCRIPTOR });
    });
    const beforeManual = fetchImpl.mock.calls.length;

    await act(async () => {
      await result.current.reconnect();
    });

    const bodies = sentBodies(fetchImpl);
    expect(bodies.length).toBeGreaterThan(beforeManual);
    expect(bodies[bodies.length - 1]["page_context"]).toEqual(DESCRIPTOR);
  });

  it("a turn started without a descriptor does not acquire one on resume", async () => {
    // Negative control: the replay is not a stale value from an earlier turn.
    const fetchImpl = vi
      .fn()
      .mockImplementationOnce(() =>
        Promise.resolve(droppingResponse([frame({ kind: "usage", input_tokens: 1 })])),
      )
      .mockImplementation(() => Promise.resolve(sseResponse([frame({ kind: "completed" })])));

    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 2 }),
    );

    await act(async () => {
      await result.current.sendPrompt("hello");
    });
    await waitFor(() => expect(fetchImpl.mock.calls.length).toBeGreaterThan(1), { timeout: 5000 });

    for (const body of sentBodies(fetchImpl)) {
      expect("page_context" in body).toBe(false);
    }
  });

  it("a second turn replaces the descriptor rather than merging with the first", async () => {
    const other: PageContext = { kind: "opaque", route: "/approvals", reason: "not queryable" };
    const fetchImpl = vi
      .fn()
      .mockImplementation(() => Promise.resolve(sseResponse([frame({ kind: "completed" })])));
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("first", { pageContext: DESCRIPTOR });
    });
    await act(async () => {
      await result.current.sendPrompt("second", { pageContext: other });
    });

    const bodies = sentBodies(fetchImpl);
    expect(bodies[0]["page_context"]).toEqual(DESCRIPTOR);
    expect(bodies[bodies.length - 1]["page_context"]).toEqual(other);
  });
});
