// SPDX-License-Identifier: MIT
/**
 * The session tuning reaches the request body inside `sendPrompt`'s options wrapper
 * (`{pageContext, tuning}`), and survives a reconnect.
 */
import { renderHook, waitFor, act } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { EMPTY_TIER_SELECTION } from "../components/tuning/sessionTuning";
import { toWireTuning } from "../components/AssistantPanel/wireTuning";
import { useAgentStream } from "./useAgentStream";

/** A schema-3 block as `toWireTuning` builds it, with mixed families across tiers. */
const TUNING = toWireTuning({
  tiers: {
    supervisor: { ...EMPTY_TIER_SELECTION, family: "openai", effort: "none" },
    subagent: { ...EMPTY_TIER_SELECTION, family: "anthropic", model: "anthropic.claude-sonnet-5-5" },
    tool: EMPTY_TIER_SELECTION,
  },
}) as Record<string, unknown>;

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

function sentBodies(fetchImpl: ReturnType<typeof vi.fn>): Record<string, unknown>[] {
  return fetchImpl.mock.calls.map(
    (call) => JSON.parse((call[1] as RequestInit).body as string) as Record<string, unknown>,
  );
}

describe("tuning reaches the wire through the wrapper shape", () => {
  it("builds a schema-3 block for the test, so the wire shape below is the real one", () => {
    expect(TUNING["schema"]).toBe(3);
    expect(Object.keys(TUNING["tiers"] as object)).toEqual(["supervisor", "subagent", "tool"]);
  });

  it("sends the tuning block the wrapper carried", async () => {
    const fetchImpl = vi
      .fn()
      .mockImplementation(() => Promise.resolve(sseResponse([frame({ kind: "completed" })])));
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("summarise this", { tuning: { ...TUNING } });
    });

    expect(sentBodies(fetchImpl)[0]["tuning"]).toEqual(TUNING);
  });

  it("keeps tuning a sibling of prompt, not nested inside another key", async () => {
    // Tuning passed inside the options wrapper reaches the body under `tuning`.
    const fetchImpl = vi
      .fn()
      .mockImplementation(() => Promise.resolve(sseResponse([frame({ kind: "completed" })])));
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("summarise this", { tuning: { ...TUNING } });
    });

    const body = sentBodies(fetchImpl)[0];
    expect(body["tuning"]).toEqual(TUNING);
    // The tuning keys are not at the top level of the body.
    expect(Object.keys(body)).not.toContain("tiers");
    expect(Object.keys(body)).not.toContain("schema");
  });

  it("omits the key entirely when the session overrides nothing", async () => {
    // With no tuning, the body carries no `tuning` key, so the Runtime takes its configured defaults.
    const fetchImpl = vi
      .fn()
      .mockImplementation(() => Promise.resolve(sseResponse([frame({ kind: "completed" })])));
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("summarise this");
    });

    expect("tuning" in sentBodies(fetchImpl)[0]).toBe(false);
  });

  it("replays the same tuning on an automatic reconnect", async () => {
    // The reconnect replays the same tuning.
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
      await result.current.sendPrompt("summarise this", { tuning: { ...TUNING } });
    });
    await waitFor(() => expect(fetchImpl.mock.calls.length).toBeGreaterThan(1), { timeout: 5000 });

    const bodies = sentBodies(fetchImpl);
    for (const [index, body] of bodies.entries()) {
      expect(body["tuning"], `request ${index} lost its tuning`).toEqual(TUNING);
    }
    // The second request is a resume, not a fresh turn.
    expect(bodies[1]["session_id"]).toBe(bodies[0]["session_id"]);
    expect(bodies[1]["resume_from"]).not.toBe(0);
  });
});
