// SPDX-License-Identifier: MIT
/**
 * Tests for `useAgentStream`: incremental event application, every closed event kind, an
 * unrecognised kind surfacing rather than being dropped, and reconnection by session id after a
 * mid-stream drop.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { renderHook, waitFor, act } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BODY_DIGEST_HEADER, JWT_HEADER, sha256Hex } from "../lib/agentClient";
import { isEventKind } from "../types/generated/agentEvents";
import {
  MALFORMED_FRAME_EXCERPT_LIMIT,
  MALFORMED_FRAME_KIND,
  SESSION_ID_MAX_LENGTH,
  SESSION_ID_MIN_LENGTH,
  newSessionId,
  useAgentStream,
} from "./useAgentStream";

/** Build a `Response`-shaped object whose body streams the given SSE frames one chunk at a time. */
function sseResponse(frames: string[], opts: { ok?: boolean; status?: number } = {}): Response {
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
  return {
    ok: opts.ok ?? true,
    status: opts.status ?? 200,
    body: stream,
  } as unknown as Response;
}

function frame(obj: Record<string, unknown>): string {
  return `data: ${JSON.stringify(obj)}\n\n`;
}

const baseOptions = {
  agentBaseUrl: "/agent",
  agentRuntimeArn: "arn:test",
  getAccessToken: () => Promise.resolve("tok-123"),
};

describe("useAgentStream", () => {
  it("applies tool_call, tool_result, proposal, usage, completed events incrementally", async () => {
    const events = [
      { kind: "tool_call", tool_name: "search_opportunities", tool_input: { q: "x" } },
      { kind: "tool_result", tool_output: { items: [{ id: "opp-1" }] }, truncated: false },
      {
        kind: "proposal",
        proposal_id: "prop-1",
        proposal_summary: { reason: "close deal", before: { stage: "Negotiation" }, after: { stage: "Closed Won" } },
      },
      { kind: "usage", input_tokens: 10, output_tokens: 20 },
      { kind: "completed", text: "Done.", input_tokens: 10, output_tokens: 20 },
    ];
    const fetchImpl = vi.fn().mockImplementation(() => Promise.resolve(sseResponse(events.map(frame))));

    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.turnEnded).toBe(true));
    expect(result.current.transcript).toHaveLength(5);
    expect(result.current.transcript.map((e) => e.event.kind)).toEqual([
      "tool_call",
      "tool_result",
      "proposal",
      "usage",
      "completed",
    ]);
    expect(result.current.transcript.every((e) => !e.unrecognised)).toBe(true);
    expect(result.current.connectionState).toBe("closed");
  });

  it("surfaces an error event distinctly, including budget_exhausted with partial_work", async () => {
    const fetchImpl = vi.fn().mockImplementation(() =>
      Promise.resolve(
        sseResponse([
          frame({
            kind: "error",
            error_kind: "budget_exhausted",
            error_message: "Turn budget exhausted.",
            partial_work: "created 1 proposal",
          }),
        ]),
      ),
    );
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.turnEnded).toBe(true));
    expect(result.current.transcript[0].event.error_kind).toBe("budget_exhausted");
    expect(result.current.transcript[0].event.partial_work).toBe("created 1 proposal");
  });

  it("marks a frame outside the closed event set as unrecognised rather than dropping it", async () => {
    const fetchImpl = vi.fn().mockImplementation(() =>
      Promise.resolve(sseResponse([frame({ kind: "some_future_kind", text: "surprise" })])),
    );
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.transcript).toHaveLength(1));
    expect(result.current.transcript[0].unrecognised).toBe(true);
    expect(result.current.transcript[0].event.kind).toBe("some_future_kind");
    // A clean stream close with no terminal event is not a drop, so the fetch is not retried.
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(result.current.connectionState).toBe("closed");
  });

  it("reconnects by session id with resume_from set to the events already applied, after a mid-stream drop", async () => {
    const sessionIds: string[] = [];
    const resumeFroms: number[] = [];

    const fetchImpl = vi
      .fn()
      .mockImplementationOnce((_url: string, init: RequestInit) => {
        const body = JSON.parse(init.body as string);
        sessionIds.push(body.session_id);
        resumeFroms.push(body.resume_from);
        // First call: emits one event then the stream drops (no terminal event, throws mid-read).
        const encoder = new TextEncoder();
        let delivered = false;
        const stream = new ReadableStream<Uint8Array>({
          pull(controller) {
            if (!delivered) {
              delivered = true;
              controller.enqueue(encoder.encode(frame({ kind: "tool_call", tool_name: "get_account", tool_input: {} })));
              return;
            }
            controller.error(new Error("connection dropped"));
          },
        });
        return Promise.resolve({ ok: true, status: 200, body: stream } as unknown as Response);
      })
      .mockImplementationOnce((_url: string, init: RequestInit) => {
        const body = JSON.parse(init.body as string);
        sessionIds.push(body.session_id);
        resumeFroms.push(body.resume_from);
        return Promise.resolve(sseResponse([frame({ kind: "completed", text: "Recovered." })]));
      });

    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 2 }),
    );

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.turnEnded).toBe(true), { timeout: 5000 });

    expect(sessionIds[0]).toBe(sessionIds[1]);
    expect(resumeFroms[0]).toBe(0);
    expect(resumeFroms[1]).toBe(1);
    expect(result.current.transcript.map((e) => e.event.kind)).toEqual(["tool_call", "completed"]);
  });

  it("reports failed once reconnect attempts are exhausted", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 1 }),
    );

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.connectionState).toBe("failed"), { timeout: 5000 });
  });
});

/** A pre-stream rejection (401, 403, 400) is terminal and never reconnects. */
describe("useAgentStream: an authentication rejection is terminal, not a drop", () => {
  function rejection(status: number): Response {
    return {
      ok: false,
      status,
      body: null,
    } as unknown as Response;
  }

  it.each([401, 403])("does not reconnect after a %i: exactly one request", async (status) => {
    const fetchImpl = vi.fn().mockImplementation(() => Promise.resolve(rejection(status)));
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
    );

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.connectionState).toBe("failed"));
    // Exactly one request: a rejection is never reconnected.
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(result.current.transcript).toEqual([]);
  });

  it.each([
    [401, "unauthenticated"],
    [403, "forbidden"],
  ] as const)("reports %i as %s, because the two need different remedies", async (status, reason) => {
    const fetchImpl = vi.fn().mockImplementation(() => Promise.resolve(rejection(status)));
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.connectionState).toBe("failed"));
    expect(result.current.failureReason).toBe(reason);
  });

  it("keeps 401 and 403 distinct, because a 403 is not a sign-in problem", async () => {
    // Two hooks, so neither result can be the other's leftover state.
    const unauthenticated = renderHook(() =>
      useAgentStream({
        ...baseOptions,
        fetchImpl: vi.fn().mockImplementation(() => Promise.resolve(rejection(401))),
      }),
    );
    const forbidden = renderHook(() =>
      useAgentStream({
        ...baseOptions,
        fetchImpl: vi.fn().mockImplementation(() => Promise.resolve(rejection(403))),
      }),
    );

    await act(async () => {
      await unauthenticated.result.current.sendPrompt("hello");
      await forbidden.result.current.sendPrompt("hello");
    });

    expect(unauthenticated.result.current.failureReason).toBe("unauthenticated");
    expect(forbidden.result.current.failureReason).toBe("forbidden");
    expect(unauthenticated.result.current.failureReason).not.toBe(
      forbidden.result.current.failureReason,
    );
  });

  function rejectionWithMessage(status: number, message: string): Response {
    return {
      ok: false,
      status,
      body: null,
      json: () => Promise.resolve({ message }),
    } as unknown as Response;
  }

  it("does not reconnect after a 400: a validation rejection is terminal", async () => {
    const fetchImpl = vi.fn().mockImplementation(() => Promise.resolve(rejection(400)));
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
    );

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.connectionState).toBe("failed"));
    // A 400 is the relay passing a Runtime `validation_failed` through: exactly one request.
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(result.current.transcript).toEqual([]);
    expect(result.current.failureReason).toBe("invalid_request");
  });

  it("surfaces the relay's user-safe 400 message so the user learns what to change", async () => {
    const message = "Model 'gpt-9' is not available. Choose one of the offered models.";
    const fetchImpl = vi
      .fn()
      .mockImplementation(() => Promise.resolve(rejectionWithMessage(400, message)));
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
    );

    await act(async () => {
      await result.current.sendPrompt("switch to gpt-9");
    });

    await waitFor(() => expect(result.current.connectionState).toBe("failed"));
    // The Runtime's message reaches the user.
    expect(result.current.failureReason).toBe("invalid_request");
    expect(result.current.failureMessage).toBe(message);
    // Still terminal: a validation failure is not a drop to reconnect over.
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(result.current.transcript).toEqual([]);
  });

  it("still reconnects after a non-auth failure, so only 400/401/403 became terminal", async () => {
    const fetchImpl = vi.fn().mockImplementation(() => Promise.resolve(rejection(502)));
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 1 }),
    );

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.connectionState).toBe("failed"));
    expect(result.current.failureReason).toBe("transport");
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("reports an unauthenticated failure when no token can be obtained at all", async () => {
    const fetchImpl = vi.fn();
    const { result } = renderHook(() =>
      useAgentStream({
        ...baseOptions,
        getAccessToken: () => Promise.reject(new Error("no session")),
        fetchImpl,
      }),
    );

    await act(async () => {
      await result.current.sendPrompt("hello").catch(() => undefined);
    });

    await waitFor(() => expect(result.current.connectionState).toBe("failed"));
    expect(result.current.failureReason).toBe("unauthenticated");
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("clears the failure reason when a new prompt is sent", async () => {
    const fetchImpl = vi
      .fn()
      .mockImplementationOnce(() => Promise.resolve(rejection(401)))
      .mockImplementationOnce(() =>
        Promise.resolve(sseResponse([frame({ kind: "completed", text: "ok" })])),
      );
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("first");
    });
    await waitFor(() => expect(result.current.failureReason).toBe("unauthenticated"));

    await act(async () => {
      await result.current.sendPrompt("second");
    });

    await waitFor(() => expect(result.current.turnEnded).toBe(true));
    expect(result.current.failureReason).toBeUndefined();
  });

  it("a clean end-of-body with no terminal event is still not a drop", async () => {
    const fetchImpl = vi
      .fn()
      .mockImplementation(() => Promise.resolve(sseResponse([frame({ kind: "usage" })])));
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
    );

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.connectionState).toBe("closed"));
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(result.current.failureReason).toBeUndefined();
  });
});

// --------------------------------------------------------------------------------------
// The generated session id satisfies the runtimeSessionId bound
//
// These tests drive the fallback path (no `crypto.randomUUID`) explicitly.
// --------------------------------------------------------------------------------------

describe("newSessionId satisfies the runtimeSessionId bound", () => {
  /** Force the fallback by hiding `randomUUID`, the way an insecure context or older browser does. */
  function withoutRandomUUID<T>(body: () => T): T {
    const original = globalThis.crypto;
    // A crypto object present but without `randomUUID`.
    vi.stubGlobal("crypto", { getRandomValues: original?.getRandomValues?.bind(original) });
    try {
      return body();
    } finally {
      vi.stubGlobal("crypto", original);
    }
  }

  it("takes the randomUUID path when it is available, and complies", () => {
    const id = newSessionId();

    expect(id.length).toBeGreaterThanOrEqual(SESSION_ID_MIN_LENGTH);
    expect(id.length).toBeLessThanOrEqual(SESSION_ID_MAX_LENGTH);
  });

  it("actually reaches the fallback when randomUUID is absent", () => {
    // Non-vacuity: the stub really hides `randomUUID`.
    const id = withoutRandomUUID(() => newSessionId());

    expect(id.startsWith("session-")).toBe(true);
    expect(id).not.toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-/);
  });

  it("pads the fallback from crypto.getRandomValues, never Math.random", () => {
    const randomSpy = vi.spyOn(Math, "random");
    try {
      withoutRandomUUID(() => {
        const cryptoSpy = vi.spyOn(globalThis.crypto, "getRandomValues");
        newSessionId();
        expect(cryptoSpy).toHaveBeenCalled();
      });
      expect(randomSpy).not.toHaveBeenCalled();
    } finally {
      randomSpy.mockRestore();
    }
  });

  it("complies on the fallback path across many draws, not just one", () => {
    // The fallback's random suffix varies in length, so the bound is checked over many draws.
    const lengths = new Set<number>();
    withoutRandomUUID(() => {
      for (let i = 0; i < 2000; i += 1) {
        const id = newSessionId();
        lengths.add(id.length);
        expect(id.length).toBeGreaterThanOrEqual(SESSION_ID_MIN_LENGTH);
        expect(id.length).toBeLessThanOrEqual(SESSION_ID_MAX_LENGTH);
      }
    });

    expect(lengths.size).toBeGreaterThan(0);
  });

  it("still yields distinct ids, so the padding did not make them predictable", () => {
    const ids = withoutRandomUUID(() =>
      Array.from({ length: 500 }, () => newSessionId()),
    );

    // The padding is random, so ids minted within one millisecond still differ.
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("does not depend on Date.now() being its current width", () => {
    // Padding is computed against the bound, so a shorter clock value is still compliant.
    const realNow = Date.now;
    Date.now = () => 1;
    try {
      const id = withoutRandomUUID(() => newSessionId());
      expect(id.length).toBeGreaterThanOrEqual(SESSION_ID_MIN_LENGTH);
    } finally {
      Date.now = realNow;
    }
  });

  it("agrees with the relay on both bounds", () => {
    expect(SESSION_ID_MIN_LENGTH).toBe(33);
    expect(SESSION_ID_MAX_LENGTH).toBe(256);
  });
});

/** Read the session-id bounds out of a TypeScript source string. */
function boundsFrom(source: string): { min: number | undefined; max: number | undefined } {
  const read = (name: string): number | undefined => {
    const match = new RegExp(`${name}\\s*=\\s*(\\d+)`).exec(source);
    return match === null ? undefined : Number(match[1]);
  };
  return { min: read("SESSION_ID_MIN_LENGTH"), max: read("SESSION_ID_MAX_LENGTH") };
}

describe("the bound is one constraint declared on two sides", () => {
  const relaySource = readFileSync(
    resolve(process.cwd(), "..", "src", "relay", "src", "requests.ts"),
    "utf8",
  );

  it("declares the same floor and ceiling in the relay as in this generator", () => {
    const relay = boundsFrom(relaySource);

    expect(relay.min).toBe(SESSION_ID_MIN_LENGTH);
    expect(relay.max).toBe(SESSION_ID_MAX_LENGTH);
  });

  it("found real declarations, so the comparison is not between two absences", () => {
    const relay = boundsFrom(relaySource);

    expect(relay.min).toBeDefined();
    expect(relay.max).toBeDefined();
  });

  it("would fail if the relay's floor drifted", () => {
    // A drifted floor, driven through an injected source string.
    const drifted = boundsFrom("export const SESSION_ID_MIN_LENGTH = 41;");

    expect(drifted.min).not.toBe(SESSION_ID_MIN_LENGTH);
  });
});

/**
 * The two headers a `/agent/*` turn needs under CloudFront OAC, asserted at the hook's call site:
 * the JWT in `X-Crm-Authorization` and the body's SHA-256 in `x-amz-content-sha256`.
 */
describe("useAgentStream: the request survives CloudFront OAC", () => {
  function capture(): { calls: { url: string; init: RequestInit }[]; fetchImpl: typeof fetch } {
    const calls: { url: string; init: RequestInit }[] = [];
    const fetchImpl = ((url: string, init: RequestInit) => {
      calls.push({ url, init });
      return Promise.resolve(sseResponse([frame({ kind: "completed", text: "ok" })]));
    }) as unknown as typeof fetch;
    return { calls, fetchImpl };
  }

  it("sends the JWT in X-Crm-Authorization and not in Authorization", async () => {
    const { calls, fetchImpl } = capture();
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    const headers = calls[0]?.init.headers as Record<string, string>;
    expect(headers[JWT_HEADER]).toBe("Bearer tok-123");
    // `signing_behavior = "always"` overwrites `Authorization` with CloudFront's SigV4, so a token
    // left there never reaches the relay and every turn 401s.
    expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain("authorization");
    expect(headers["Accept"]).toBe("text/event-stream");
  });

  it("digests the exact bytes it posts, pinned against a known vector", async () => {
    const { calls, fetchImpl } = capture();
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    const headers = calls[0]?.init.headers as Record<string, string>;
    const sent = String(calls[0]?.init.body);
    // The session id is minted per turn, so the expected body substitutes it in.
    const sessionId = (JSON.parse(sent) as { session_id: string }).session_id;
    expect(sent).toBe(`{"prompt":"hello","session_id":"${sessionId}","resume_from":0}`);
    expect(headers[BODY_DIGEST_HEADER]).toBe(await sha256Hex(sent));
    expect(headers[BODY_DIGEST_HEADER]).toMatch(/^[0-9a-f]{64}$/);
  });

  it("re-digests the resumed body on a reconnect, not the first one", async () => {
    // Drops the stream after one event, so the resumed body (`resume_from` 1) differs from the first.
    const calls: { url: string; init: RequestInit }[] = [];
    const fetchImpl = vi.fn().mockImplementation((url: string, init: RequestInit) => {
      calls.push({ url, init });
      if (calls.length > 1) {
        return Promise.resolve(sseResponse([frame({ kind: "completed", text: "Recovered." })]));
      }
      const encoder = new TextEncoder();
      let delivered = false;
      const stream = new ReadableStream<Uint8Array>({
        pull(controller) {
          if (!delivered) {
            delivered = true;
            controller.enqueue(encoder.encode(frame({ kind: "tool_call", tool_name: "get_account" })));
            return;
          }
          controller.error(new Error("connection dropped"));
        },
      });
      return Promise.resolve({ ok: true, status: 200, body: stream } as unknown as Response);
    }) as unknown as typeof fetch;
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 2 }),
    );

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.turnEnded).toBe(true), { timeout: 5000 });
    expect(calls).toHaveLength(2);
    for (const call of calls) {
      const headers = call.init.headers as Record<string, string>;
      expect(headers[BODY_DIGEST_HEADER]).toBe(await sha256Hex(String(call.init.body)));
    }
    const digests = calls.map((c) => (c.init.headers as Record<string, string>)[BODY_DIGEST_HEADER]);
    // The bodies differ (`resume_from` 0 then 1), so the digests must differ too.
    expect(String(calls[1]?.init.body)).not.toBe(String(calls[0]?.init.body));
    expect(digests[0]).not.toBe(digests[1]);
  });

  it("fails terminally, without a request, when the body cannot be hashed", async () => {
    // `crypto.subtle` is absent outside a secure context.
    const digest = vi
      .spyOn(crypto.subtle, "digest")
      .mockRejectedValue(new Error("SubtleCrypto unavailable"));
    const logged = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const fetchImpl = vi.fn();
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hello").catch(() => undefined);
    });

    await waitFor(() => expect(result.current.connectionState).toBe("failed"));
    expect(result.current.failureReason).toBe("transport");
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(logged).toHaveBeenCalled();
    digest.mockRestore();
    logged.mockRestore();
  });

  it("names the same JWT header the relay reads, so a rename fails here rather than as a 401", () => {
    // The relay lowercases the header name, so the comparison is case-insensitive.
    const relaySource = readFileSync(
      resolve(process.cwd(), "..", "src", "relay", "src", "jwt.ts"),
      "utf8",
    );
    const match = /JWT_HEADER\s*=\s*"([^"]+)"/.exec(relaySource);

    expect(match).not.toBeNull();
    expect(match?.[1]?.toLowerCase()).toBe(JWT_HEADER.toLowerCase());
  });
});

/** An abort this hook issued is not a network drop and issues no further POST. */
describe("useAgentStream: a deliberate abort never reconnects", () => {
  /** A response whose body delivers one frame, then blocks forever unless the request is aborted. */
  function hangingResponse(signal: AbortSignal | undefined): Response {
    const encoder = new TextEncoder();
    let delivered = false;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        signal?.addEventListener("abort", () => {
          controller.error(new DOMException("The operation was aborted.", "AbortError"));
        });
      },
      pull(controller) {
        if (!delivered) {
          delivered = true;
          controller.enqueue(encoder.encode(frame({ kind: "tool_call", tool_name: "get_account" })));
          return;
        }
        // Never settles: the turn is still streaming when the abort arrives.
        return new Promise<void>(() => {});
      },
    });
    return { ok: true, status: 200, body: stream } as unknown as Response;
  }

  it("issues no further request when the hook unmounts mid-stream", async () => {
    const fetchImpl = vi.fn().mockImplementation((_url: string, init: RequestInit) =>
      Promise.resolve(hangingResponse(init.signal ?? undefined)),
    );
    const { result, unmount } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
    );

    await act(async () => {
      void result.current.sendPrompt("hello");
    });
    await waitFor(() => expect(result.current.connectionState).toBe("streaming"));

    unmount();

    // Long enough to cover the first backoff (1000ms) with margin.
    await new Promise((resolve) => setTimeout(resolve, 1400));
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("does not reconnect when a new prompt supersedes an in-flight turn", async () => {
    // `sendPrompt` aborts the previous controller; the abandoned stream must not reconnect.
    const fetchImpl = vi.fn().mockImplementation((_url: string, init: RequestInit) => {
      if (fetchImpl.mock.calls.length === 1) {
        return Promise.resolve(hangingResponse(init.signal ?? undefined));
      }
      return Promise.resolve(sseResponse([frame({ kind: "completed", text: "second turn" })]));
    });
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
    );

    await act(async () => {
      void result.current.sendPrompt("first");
    });
    await waitFor(() => expect(result.current.connectionState).toBe("streaming"));

    await act(async () => {
      await result.current.sendPrompt("second");
    });
    await waitFor(() => expect(result.current.turnEnded).toBe(true));

    await new Promise((resolve) => setTimeout(resolve, 1400));
    // Exactly two: one per prompt.
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("lets the superseding turn own the state, rather than the abandoned one", async () => {
    // The abandoned stream writes no state over the turn that replaced it.
    const fetchImpl = vi.fn().mockImplementation((_url: string, init: RequestInit) => {
      if (fetchImpl.mock.calls.length === 1) {
        return Promise.resolve(hangingResponse(init.signal ?? undefined));
      }
      return Promise.resolve(sseResponse([frame({ kind: "completed", text: "second turn" })]));
    });
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      void result.current.sendPrompt("first");
    });
    await waitFor(() => expect(result.current.connectionState).toBe("streaming"));

    await act(async () => {
      await result.current.sendPrompt("second");
    });

    await waitFor(() => expect(result.current.turnEnded).toBe(true));
    // Waits past the first reconnect backoff, when a reconnect would fire.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1400));
    });
    expect(result.current.connectionState).toBe("closed");
    expect(result.current.failureReason).toBeUndefined();
    expect(result.current.transcript.map((e) => e.event.text)).toEqual(["second turn"]);
  });

  it("still reconnects after a genuine mid-stream drop", async () => {
    // A genuine drop still reconnects.
    const fetchImpl = vi.fn().mockImplementation(() => {
      const encoder = new TextEncoder();
      let delivered = false;
      if (fetchImpl.mock.calls.length > 1) {
        return Promise.resolve(sseResponse([frame({ kind: "completed", text: "Recovered." })]));
      }
      const stream = new ReadableStream<Uint8Array>({
        pull(controller) {
          if (!delivered) {
            delivered = true;
            controller.enqueue(encoder.encode(frame({ kind: "tool_call", tool_name: "get_account" })));
            return;
          }
          controller.error(new Error("connection dropped"));
        },
      });
      return Promise.resolve({ ok: true, status: 200, body: stream } as unknown as Response);
    });
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 2 }),
    );

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.turnEnded).toBe(true), { timeout: 5000 });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(result.current.transcript.map((e) => e.event.kind)).toEqual(["tool_call", "completed"]);
  });
});

/** A frame whose JSON will not parse surfaces as an event rather than vanishing. */
describe("useAgentStream: a malformed frame surfaces", () => {
  it("appends an unrecognised entry instead of dropping the frame", async () => {
    const fetchImpl = vi
      .fn()
      .mockImplementation(() => Promise.resolve(sseResponse(["data: {not json\n\n"])));
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.transcript).toHaveLength(1));
    const entry = result.current.transcript[0];
    expect(entry.unrecognised).toBe(true);
    expect(entry.event.kind).toBe(MALFORMED_FRAME_KIND);
    expect(entry.event.text).toBe("{not json");
  });

  it("keeps the good frames either side of a malformed one", async () => {
    // A parse error does not abort the batch: valid events around it are kept.
    const fetchImpl = vi.fn().mockImplementation(() =>
      Promise.resolve(
        sseResponse([
          frame({ kind: "tool_call", tool_name: "get_account" }),
          "data: {not json\n\n",
          frame({ kind: "completed", text: "Done." }),
        ]),
      ),
    );
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.turnEnded).toBe(true));
    expect(result.current.transcript.map((e) => e.event.kind)).toEqual([
      "tool_call",
      MALFORMED_FRAME_KIND,
      "completed",
    ]);
  });

  it("bounds the excerpt, so a corrupt megabyte does not land in the transcript", async () => {
    const huge = "x".repeat(5000);
    const fetchImpl = vi
      .fn()
      .mockImplementation(() => Promise.resolve(sseResponse([`data: {${huge}\n\n`])));
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.transcript).toHaveLength(1));
    expect(result.current.transcript[0].event.text).toHaveLength(MALFORMED_FRAME_EXCERPT_LIMIT);
  });

  it("is unrecognised by construction: the kind is outside the closed event-kind set", () => {
    // The malformed kind stays outside the declared set, so it renders as unrecognised.
    expect(isEventKind(MALFORMED_FRAME_KIND)).toBe(false);
  });
});

/**
 * A cancelled turn's torn stream is not resumed: the Runtime ending a cancelled turn can tear the
 * stream, which `read()` reports as a thrown error just like a genuine drop.
 */
describe("useAgentStream: a cancelled turn is not resumed", () => {
  /** A response that delivers one frame and then tears the connection, as a cancelled turn can. */
  function dropsAfterOneFrame(): Response {
    const encoder = new TextEncoder();
    let delivered = false;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (!delivered) {
          delivered = true;
          controller.enqueue(encoder.encode(frame({ kind: "tool_call", tool_name: "get_account" })));
          return;
        }
        controller.error(new Error("connection torn by the cancelled turn"));
      },
    });
    return { ok: true, status: 200, body: stream } as unknown as Response;
  }

  it("does not reconnect after the stream tears, once a stop was requested", async () => {
    const fetchImpl = vi.fn().mockImplementation(() => Promise.resolve(dropsAfterOneFrame()));
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
    );

    await act(async () => {
      void result.current.sendPrompt("hello");
    });
    await waitFor(() => expect(result.current.connectionState).toBe("streaming"));
    act(() => {
      result.current.markTurnCancelled();
    });

    await waitFor(() => expect(result.current.connectionState).toBe("closed"), { timeout: 5000 });
    // The cancelled turn is not re-POSTed.
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    // `closed`, not `failed`: the stop was requested.
    expect(result.current.failureReason).toBeUndefined();
  });

  it("still reconnects after the same tear when no stop was requested", async () => {
    // Control: without the cancel flag, the same tear reconnects.
    const fetchImpl = vi.fn().mockImplementation(() => {
      if (fetchImpl.mock.calls.length > 1) {
        return Promise.resolve(sseResponse([frame({ kind: "completed", text: "Recovered." })]));
      }
      return Promise.resolve(dropsAfterOneFrame());
    });
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 2 }),
    );

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.turnEnded).toBe(true), { timeout: 5000 });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("keeps suppressing the resume even when the cancel itself failed", async () => {
    // `markTurnCancelled` takes effect at request time, whatever the cancel's outcome.
    const fetchImpl = vi.fn().mockImplementation(() => Promise.resolve(dropsAfterOneFrame()));
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
    );

    await act(async () => {
      void result.current.sendPrompt("hello");
    });
    await waitFor(() => expect(result.current.connectionState).toBe("streaming"));
    act(() => {
      result.current.markTurnCancelled();
    });

    await waitFor(() => expect(result.current.connectionState).toBe("closed"), { timeout: 5000 });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("clears the suppression on the next prompt, so a later turn still recovers from a drop", async () => {
    // The cancel flag does not outlive its turn.
    const fetchImpl = vi.fn().mockImplementation(() => {
      if (fetchImpl.mock.calls.length > 2) {
        return Promise.resolve(sseResponse([frame({ kind: "completed", text: "Recovered." })]));
      }
      return Promise.resolve(dropsAfterOneFrame());
    });
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 2 }),
    );

    await act(async () => {
      void result.current.sendPrompt("first");
    });
    await waitFor(() => expect(result.current.connectionState).toBe("streaming"));
    act(() => {
      result.current.markTurnCancelled();
    });
    await waitFor(() => expect(result.current.connectionState).toBe("closed"), { timeout: 5000 });
    expect(fetchImpl).toHaveBeenCalledTimes(1);

    await act(async () => {
      await result.current.sendPrompt("second");
    });

    await waitFor(() => expect(result.current.turnEnded).toBe(true), { timeout: 5000 });
    // Second prompt dropped once and recovered: calls 2 and 3.
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });
});

/**
 * A stop already requested when the tear arrives closes the stream at once, via `maybeReconnect`'s
 * entry check, rather than after a full backoff.
 */
describe("useAgentStream: a stop already requested closes without a reconnecting pause", () => {
  it("closes promptly rather than waiting out a backoff it will not use", async () => {
    // The tear is held until the test releases it, so the stop is registered before the tear.
    let announceHolding: () => void = () => {};
    const holding = new Promise<void>((resolve) => {
      announceHolding = resolve;
    });
    let tear: () => void = () => {};
    const encoder = new TextEncoder();
    const fetchImpl = vi.fn().mockImplementation(() => {
      let delivered = false;
      const stream = new ReadableStream<Uint8Array>({
        pull(controller) {
          if (!delivered) {
            delivered = true;
            controller.enqueue(encoder.encode(frame({ kind: "tool_call", tool_name: "get_account" })));
            return;
          }
          return new Promise<void>((resolve) => {
            tear = () => {
              controller.error(new Error("connection torn by the cancelled turn"));
              resolve();
            };
            announceHolding();
          });
        },
      });
      return Promise.resolve({ ok: true, status: 200, body: stream } as unknown as Response);
    });
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
    );

    await act(async () => {
      void result.current.sendPrompt("hello");
    });
    await holding;

    act(() => {
      result.current.markTurnCancelled();
    });
    const startedAt = Date.now();
    act(() => {
      tear();
    });
    await waitFor(() => expect(result.current.connectionState).toBe("closed"), { timeout: 5000 });

    // Well under RECONNECT_BACKOFF_MS (1000).
    expect(Date.now() - startedAt).toBeLessThan(500);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(result.current.failureReason).toBeUndefined();
  });
});

/**
 * A `sendPrompt` during a reconnect backoff stops the superseded turn's pending reconnect. Asserted on
 * POST counts, since the end state looks the same either way.
 */
describe("useAgentStream: a superseded turn issues no request", () => {
  /** Records every POST, so the assertions below are counts rather than end states. */
  function countingFetch(handler: (call: number, init: RequestInit) => Response) {
    const calls: { body: Record<string, unknown> }[] = [];
    const fetchImpl = ((_url: string, init: RequestInit) => {
      calls.push({ body: JSON.parse(String(init.body)) as Record<string, unknown> });
      return Promise.resolve(handler(calls.length, init));
    }) as unknown as typeof fetch;
    return { calls, fetchImpl };
  }

  /** Delivers one frame, then tears — the shape that puts the hook into a reconnect backoff. */
  function tearsAfterOneFrame(): Response {
    const encoder = new TextEncoder();
    let delivered = false;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (!delivered) {
          delivered = true;
          controller.enqueue(encoder.encode(frame({ kind: "tool_call", tool_name: "get_account" })));
          return;
        }
        controller.error(new Error("connection dropped"));
      },
    });
    return { ok: true, status: 200, body: stream } as unknown as Response;
  }

  it("issues exactly two POSTs for two prompts when the first is superseded mid-backoff", async () => {
    const { calls, fetchImpl } = countingFetch((call) =>
      call === 1 ? tearsAfterOneFrame() : sseResponse([frame({ kind: "completed", text: "B done" })]),
    );
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
    );

    await act(async () => {
      void result.current.sendPrompt("turn-A");
    });
    // Turn A has torn and is now inside its backoff.
    await waitFor(() => expect(calls).toHaveLength(1));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 300));
    });
    await act(async () => {
      await result.current.sendPrompt("turn-B");
    });
    await act(async () => {
      // Past turn A's backoff.
      await new Promise((resolve) => setTimeout(resolve, 1400));
    });

    // Two prompts, two POSTs.
    expect(calls).toHaveLength(2);
    expect(calls[0].body.prompt).toBe("turn-A");
    expect(calls[1].body.prompt).toBe("turn-B");
  });

  it("issues no request carrying one turn's session with another turn's prompt", async () => {
    // No POST pairs turn A's session id with turn B's prompt.
    const { calls, fetchImpl } = countingFetch((call) =>
      call === 1 ? tearsAfterOneFrame() : sseResponse([frame({ kind: "completed", text: "B done" })]),
    );
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
    );

    await act(async () => {
      void result.current.sendPrompt("turn-A");
    });
    await waitFor(() => expect(calls).toHaveLength(1));
    const sessionA = calls[0].body.session_id;
    await act(async () => {
      await result.current.sendPrompt("turn-B");
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1400));
    });

    for (const call of calls) {
      const mixed = call.body.session_id === sessionA && call.body.prompt === "turn-B";
      expect(mixed).toBe(false);
    }
  });

  it("lets the live turn keep its transcript and its abort handle", async () => {
    // The superseded turn writes no frames into the live turn.
    const { calls, fetchImpl } = countingFetch((call) =>
      call === 1 ? tearsAfterOneFrame() : sseResponse([frame({ kind: "completed", text: "B done" })]),
    );
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
    );

    await act(async () => {
      void result.current.sendPrompt("turn-A");
    });
    await waitFor(() => expect(calls).toHaveLength(1));
    await act(async () => {
      await result.current.sendPrompt("turn-B");
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1400));
    });

    expect(result.current.transcript.map((e) => e.event.text)).toEqual(["B done"]);
    expect(result.current.turnEnded).toBe(true);
    expect(result.current.failureReason).toBeUndefined();
  });

  /*
   * `runStream`'s entry check stops a superseded reconnect before it sets `connectionState`.
   * Asserts `connectionState`; the POST count above cannot see this.
   */
  it("a superseded reconnect does not touch the live turn's state", async () => {
    const { calls, fetchImpl } = countingFetch((call) =>
      call === 1 ? tearsAfterOneFrame() : sseResponse([frame({ kind: "completed", text: "B done" })]),
    );
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
    );

    await act(async () => {
      void result.current.sendPrompt("turn-A");
    });
    // Turn A has torn and is inside its backoff.
    await waitFor(() => expect(calls).toHaveLength(1));
    await act(async () => {
      await result.current.sendPrompt("turn-B");
    });
    // Turn B is finished and terminal before turn A's backoff expires.
    await waitFor(() => expect(result.current.turnEnded).toBe(true));
    expect(result.current.connectionState).toBe("closed");

    await act(async () => {
      // Past turn A's backoff.
      await new Promise((resolve) => setTimeout(resolve, 1400));
    });

    // The live turn stays closed.
    expect(result.current.connectionState).toBe("closed");
    expect(calls).toHaveLength(2);
  });

  it("still reconnects a turn that was never superseded", async () => {
    const { calls, fetchImpl } = countingFetch((call) =>
      call === 1 ? tearsAfterOneFrame() : sseResponse([frame({ kind: "completed", text: "Recovered." })]),
    );
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 2 }),
    );

    await act(async () => {
      await result.current.sendPrompt("only-turn");
    });

    await waitFor(() => expect(result.current.turnEnded).toBe(true), { timeout: 5000 });
    expect(calls).toHaveLength(2);
    expect(calls[1].body.resume_from).toBe(1);
  });
});

/**
 * `data: null` and other non-object frames surface as `malformed_frame`, with one POST and no
 * reconnect.
 */
describe("useAgentStream: a null frame does not masquerade as a dropped connection", () => {
  function countingFetch(response: () => Response) {
    const calls: number[] = [];
    const fetchImpl = (() => {
      calls.push(calls.length + 1);
      return Promise.resolve(response());
    }) as unknown as typeof fetch;
    return { calls, fetchImpl };
  }

  it("surfaces the null frame and keeps the valid frame after it, in one POST", async () => {
    const { calls, fetchImpl } = countingFetch(() =>
      sseResponse(["data: null\n\n", frame({ kind: "completed", text: "Done." })]),
    );
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
    );

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.turnEnded).toBe(true));
    expect(result.current.transcript.map((e) => e.event.kind)).toEqual([
      MALFORMED_FRAME_KIND,
      "completed",
    ]);
    // No reconnect over a corrupt frame.
    expect(calls).toHaveLength(1);
    expect(result.current.connectionState).toBe("closed");
    expect(result.current.failureReason).toBeUndefined();
  });

  it.each(["data: 5\n\n", "data: []\n\n", 'data: "text"\n\n', "data: true\n\n", "data: {}\n\n"])(
    "treats the non-conforming frame %j as visible, not as a drop",
    async (raw) => {
      // These render as `malformed_frame` with their bytes.
      const { calls, fetchImpl } = countingFetch(() =>
        sseResponse([raw, frame({ kind: "completed", text: "Done." })]),
      );
      const { result } = renderHook(() =>
        useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 3 }),
      );

      await act(async () => {
        await result.current.sendPrompt("hello");
      });

      await waitFor(() => expect(result.current.turnEnded).toBe(true));
      expect(result.current.transcript.map((e) => e.event.kind)).toEqual([
        MALFORMED_FRAME_KIND,
        "completed",
      ]);
      expect(calls).toHaveLength(1);
    },
  );

  it("still passes a well-formed unknown kind through untouched (forward compatibility)", async () => {
    // An unknown kind keeps its own name rather than becoming `malformed_frame`.
    const { calls, fetchImpl } = countingFetch(() =>
      sseResponse([frame({ kind: "some_future_kind", text: "surprise" })]),
    );
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.transcript).toHaveLength(1));
    expect(result.current.transcript[0].event.kind).toBe("some_future_kind");
    expect(result.current.transcript[0].unrecognised).toBe(true);
    expect(calls).toHaveLength(1);
  });
});

/**
 * The manual-retry path, asserted on requests issued. `reconnect()` runs under the current
 * generation: after a supersede it resumes the live turn, never the superseded one.
 */
describe("useAgentStream: a manual retry resumes the live turn, not the superseded one", () => {
  function countingFetch(handler: (call: number) => Response) {
    const calls: { body: Record<string, unknown> }[] = [];
    const fetchImpl = ((_url: string, init: RequestInit) => {
      calls.push({ body: JSON.parse(String(init.body)) as Record<string, unknown> });
      return Promise.resolve(handler(calls.length));
    }) as unknown as typeof fetch;
    return { calls, fetchImpl };
  }

  const rejected = { ok: false, status: 502, body: null } as unknown as Response;

  it("issues exactly one POST for the retry, carrying the live turn's session and prompt", async () => {
    // A fails terminally, B is sent, then the user presses retry: three POSTs, the third about B.
    const { calls, fetchImpl } = countingFetch((call) =>
      call === 1 ? rejected : sseResponse([frame({ kind: "completed", text: "ok" })]),
    );
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 0 }),
    );

    await act(async () => {
      await result.current.sendPrompt("turn-A");
    });
    await waitFor(() => expect(result.current.connectionState).toBe("failed"));
    const sessionA = calls[0].body.session_id;

    await act(async () => {
      await result.current.sendPrompt("turn-B");
    });
    await waitFor(() => expect(result.current.turnEnded).toBe(true));
    const sessionB = calls[1].body.session_id;
    expect(sessionB).not.toBe(sessionA);

    await act(async () => {
      await result.current.reconnect();
    });
    // Past any backoff, so a stray resume would have landed.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1400));
    });

    expect(calls).toHaveLength(3);
    expect(calls[2].body.session_id).toBe(sessionB);
    expect(calls[2].body.prompt).toBe("turn-B");
  });

  it("never issues a request carrying the superseded turn's session", async () => {
    const { calls, fetchImpl } = countingFetch((call) =>
      call === 1 ? rejected : sseResponse([frame({ kind: "completed", text: "ok" })]),
    );
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 0 }),
    );

    await act(async () => {
      await result.current.sendPrompt("turn-A");
    });
    await waitFor(() => expect(result.current.connectionState).toBe("failed"));
    const sessionA = calls[0].body.session_id;

    await act(async () => {
      await result.current.sendPrompt("turn-B");
    });
    await waitFor(() => expect(result.current.turnEnded).toBe(true));

    await act(async () => {
      await result.current.reconnect();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1400));
    });

    for (const call of calls.slice(1)) {
      expect(call.body.session_id).not.toBe(sessionA);
    }
  });

  it("a new prompt during a retry supersedes it without a further POST", async () => {
    // A `sendPrompt` landing while a retry is in flight aborts the retry.
    let releaseRetry: () => void = () => {};
    let announceHolding: () => void = () => {};
    const holding = new Promise<void>((resolve) => {
      announceHolding = resolve;
    });
    const calls: { body: Record<string, unknown> }[] = [];
    const fetchImpl = ((_url: string, init: RequestInit) => {
      calls.push({ body: JSON.parse(String(init.body)) as Record<string, unknown> });
      if (calls.length === 1) return Promise.resolve(rejected);
      if (calls.length === 2) {
        announceHolding();
        // The retry hangs until the test releases it, so the supersede provably lands first.
        return new Promise<Response>((resolve) => {
          releaseRetry = () => resolve(sseResponse([frame({ kind: "completed", text: "late" })]));
        });
      }
      return Promise.resolve(sseResponse([frame({ kind: "completed", text: "C done" })]));
    }) as unknown as typeof fetch;

    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 0 }),
    );

    await act(async () => {
      await result.current.sendPrompt("turn-A");
    });
    await waitFor(() => expect(result.current.connectionState).toBe("failed"));

    act(() => {
      void result.current.reconnect();
    });
    await holding;

    await act(async () => {
      await result.current.sendPrompt("turn-C");
    });
    act(() => {
      releaseRetry();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1400));
    });

    // Three POSTs: A, the retry, and C. The released retry writes nothing into turn C.
    expect(calls).toHaveLength(3);
    expect(calls[2].body.prompt).toBe("turn-C");
    expect(result.current.transcript.map((e) => e.event.text)).toEqual(["C done"]);
  });
});

/**
 * A supersede landing mid-body, after the response resolved and a frame was applied, is caught by
 * the per-chunk check.
 */
describe("useAgentStream: a supersede mid-body stops applying frames", () => {
  it("keeps the superseded stream's later frames out of the live turn", async () => {
    let announceFirstFrame: () => void = () => {};
    const firstFrameDelivered = new Promise<void>((resolve) => {
      announceFirstFrame = resolve;
    });
    let releaseSecondFrame: () => void = () => {};
    const calls: { body: Record<string, unknown> }[] = [];
    const encoder = new TextEncoder();

    const fetchImpl = ((_url: string, init: RequestInit) => {
      calls.push({ body: JSON.parse(String(init.body)) as Record<string, unknown> });
      if (calls.length > 1) {
        return Promise.resolve(sseResponse([frame({ kind: "completed", text: "B done" })]));
      }
      let stage = 0;
      const stream = new ReadableStream<Uint8Array>({
        pull(controller) {
          stage += 1;
          if (stage === 1) {
            controller.enqueue(encoder.encode(frame({ kind: "tool_call", tool_name: "A-first" })));
            announceFirstFrame();
            return;
          }
          if (stage === 2) {
            // Held open until the test has superseded this turn, then delivers a late frame.
            return new Promise<void>((resolve) => {
              releaseSecondFrame = () => {
                controller.enqueue(encoder.encode(frame({ kind: "usage", input_tokens: 999 })));
                controller.close();
                resolve();
              };
            });
          }
          controller.close();
          return;
        },
      });
      return Promise.resolve({ ok: true, status: 200, body: stream } as unknown as Response);
    }) as unknown as typeof fetch;

    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 0 }),
    );

    await act(async () => {
      void result.current.sendPrompt("turn-A");
    });
    await firstFrameDelivered;
    // Turn A's first frame is applied, so its post-response checkpoint has already run.
    await waitFor(() => expect(result.current.transcript).toHaveLength(1));

    await act(async () => {
      await result.current.sendPrompt("turn-B");
    });
    await waitFor(() => expect(result.current.turnEnded).toBe(true));

    act(() => {
      releaseSecondFrame();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 300));
    });

    // Turn B's transcript holds turn B's frame and nothing of turn A's late one.
    expect(result.current.transcript.map((e) => e.event.kind)).toEqual(["completed"]);
    expect(result.current.transcript.some((e) => e.event.input_tokens === 999)).toBe(false);
    // And no extra POST: two prompts, two requests.
    expect(calls).toHaveLength(2);
  });
});

/**
 * Each half of `isSuperseded` reached on its own, and a supersede during the final read:
 *
 * - abort true, generation same — the unmount cleanup aborts without bumping the generation;
 * - generation stale, abort false — `reconnect()` reassigns `abortRef.current`, so a later
 *   `sendPrompt` aborts the retry's controller, not the earlier stream's;
 * - `if (done) break` exits before the per-chunk check, so the post-loop check catches it.
 */
describe("useAgentStream: the supersede check's halves — one pinned, one a stated spare", () => {
  it("half A's scenario — an unmount before any read issues no further POST", async () => {
    /*
     * Reaches the abort half's scenario. A setState after unmount is a no-op on React 19, so this
     * pins the POST count rather than state.
     */
    let announceRequested: () => void = () => {};
    const requested = new Promise<void>((resolve) => {
      announceRequested = resolve;
    });
    let respond: () => void = () => {};
    const calls: number[] = [];
    const fetchImpl = (() => {
      calls.push(calls.length + 1);
      announceRequested();
      // Resolves only after the unmount, so the response arrives with nothing yet read.
      return new Promise<Response>((resolve) => {
        respond = () => resolve(sseResponse([frame({ kind: "completed", text: "late" })]));
      });
    }) as unknown as typeof fetch;

    const { result, unmount } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 0 }),
    );

    await act(async () => {
      void result.current.sendPrompt("turn-A");
    });
    await requested;
    unmount();
    respond();
    await new Promise((resolve) => setTimeout(resolve, 300));

    // One POST, and no second request: the abort half returned before the body was touched.
    expect(calls).toHaveLength(1);
  });

  it("half B — a stream displaced from abortRef by reconnect() is generation-stale with abort false", async () => {
    /*
     * The generation half alone:
     *   1. `sendPrompt("A")` puts ctrlA in `abortRef`; A's body is held open.
     *   2. `reconnect()` mints ctrlR and overwrites `abortRef`; A is still in flight under generation 1.
     *   3. `sendPrompt("B")` aborts `abortRef` (ctrlR, not ctrlA) and bumps the generation to 2.
     *   4. A's body is released: generation stale, `aborted` false.
     */
    let announceAHolding: () => void = () => {};
    const aHolding = new Promise<void>((resolve) => {
      announceAHolding = resolve;
    });
    let releaseA: () => void = () => {};
    const calls: { body: Record<string, unknown> }[] = [];

    const fetchImpl = ((_url: string, init: RequestInit) => {
      calls.push({ body: JSON.parse(String(init.body)) as Record<string, unknown> });
      if (calls.length === 1) {
        announceAHolding();
        return new Promise<Response>((resolve) => {
          releaseA = () => resolve(sseResponse([frame({ kind: "completed", text: "stale-A" })]));
        });
      }
      if (calls.length === 2) {
        // The retry. It never settles; `sendPrompt("B")` aborts it.
        return new Promise<Response>(() => {});
      }
      return Promise.resolve(sseResponse([frame({ kind: "completed", text: "live-B" })]));
    }) as unknown as typeof fetch;

    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 0 }),
    );

    await act(async () => {
      void result.current.sendPrompt("turn-A");
    });
    await aHolding;

    // Step 2: displaces ctrlA from abortRef without touching the generation.
    act(() => {
      void result.current.reconnect();
    });
    await waitFor(() => expect(calls).toHaveLength(2));

    // Step 3: aborts the retry's controller, not A's, and bumps the generation.
    await act(async () => {
      await result.current.sendPrompt("turn-B");
    });
    await waitFor(() => expect(result.current.turnEnded).toBe(true));

    act(() => {
      releaseA();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 300));
    });

    // A's frame must not reach the live turn: it is stale by generation and nothing ever aborted it.
    expect(result.current.transcript.map((e) => e.event.text)).toEqual(["live-B"]);
  });

  it("F2 — a supersede during the FINAL read does not write closed over a live turn", async () => {
    /*
     * A stream superseded during its final read reaches the post-loop check. Turn B is left
     * streaming, so a leak from A would show as `closed`.
     */
    let announceAFirstFrame: () => void = () => {};
    const aFirstFrame = new Promise<void>((resolve) => {
      announceAFirstFrame = resolve;
    });
    let endA: () => void = () => {};
    const calls: number[] = [];
    const encoder = new TextEncoder();

    const fetchImpl = (() => {
      calls.push(calls.length + 1);
      if (calls.length > 1) {
        // Turn B: one frame, then held open — so B stays `streaming` and never reaches `closed`.
        let sent = false;
        return Promise.resolve({
          ok: true,
          status: 200,
          body: new ReadableStream<Uint8Array>({
            pull(controller) {
              if (!sent) {
                sent = true;
                controller.enqueue(encoder.encode(frame({ kind: "tool_call", tool_name: "B" })));
                return;
              }
              return new Promise<void>(() => {});
            },
          }),
        } as unknown as Response);
      }
      let stage = 0;
      return Promise.resolve({
        ok: true,
        status: 200,
        body: new ReadableStream<Uint8Array>({
          pull(controller) {
            stage += 1;
            if (stage === 1) {
              controller.enqueue(encoder.encode(frame({ kind: "tool_call", tool_name: "A" })));
              announceAFirstFrame();
              return;
            }
            // Ends A's body — `done: true` — only once the test has superseded it.
            return new Promise<void>((resolve) => {
              endA = () => {
                controller.close();
                resolve();
              };
            });
          },
        }),
      } as unknown as Response);
    }) as unknown as typeof fetch;

    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 0 }),
    );

    await act(async () => {
      void result.current.sendPrompt("turn-A");
    });
    await aFirstFrame;
    await waitFor(() => expect(result.current.transcript).toHaveLength(1));

    await act(async () => {
      void result.current.sendPrompt("turn-B");
    });
    await waitFor(() => expect(result.current.connectionState).toBe("streaming"));

    act(() => {
      endA();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 300));
    });

    // B is still streaming: A's clean EOF did not write `closed` over it.
    expect(result.current.connectionState).toBe("streaming");
    expect(result.current.transcript.map((e) => e.event.tool_name)).toEqual(["B"]);
    expect(calls).toHaveLength(2);
  });
});

describe("useAgentStream: continuing a session across turns (caller opt-in)", () => {
  it("reuses the session id and resumes from the cumulative event count when the caller continues", async () => {
    const bodies: { session_id: string; resume_from: number }[] = [];
    const responses = [
      sseResponse([
        frame({ kind: "tool_call", tool_name: "get_account", tool_input: {} }),
        frame({ kind: "completed", text: "first" }),
      ]),
      sseResponse([frame({ kind: "completed", text: "second" })]),
    ];
    let call = 0;
    const fetchImpl = vi.fn().mockImplementation((_url: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string);
      bodies.push({ session_id: body.session_id, resume_from: body.resume_from });
      return Promise.resolve(responses[call++]);
    });

    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hello");
    });
    await waitFor(() => expect(result.current.turnEnded).toBe(true));
    const firstSession = result.current.sessionId;
    expect(firstSession).toBeDefined();

    await act(async () => {
      await result.current.sendPrompt("again", { continueSessionId: firstSession });
    });
    await waitFor(() => expect(bodies).toHaveLength(2));

    // The continued turn reused the id and resumed past turn 1's two events.
    expect(bodies[0].session_id).toBe(firstSession);
    expect(bodies[0].resume_from).toBe(0);
    expect(bodies[1].session_id).toBe(firstSession);
    expect(bodies[1].resume_from).toBe(2);
    // The transcript holds the current turn only; the conversation across turns lives above the hook.
    expect(result.current.transcript.map((e) => e.event.kind)).toEqual(["completed"]);
  });

  it("mints a fresh session and resumes from zero by default, so the existing behaviour is unchanged", async () => {
    const ids: string[] = [];
    const resumeFroms: number[] = [];
    const fetchImpl = vi.fn().mockImplementation((_url: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string);
      ids.push(body.session_id);
      resumeFroms.push(body.resume_from);
      return Promise.resolve(sseResponse([frame({ kind: "completed", text: "ok" })]));
    });

    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("one");
    });
    await act(async () => {
      await result.current.sendPrompt("two");
    });
    await waitFor(() => expect(ids).toHaveLength(2));

    expect(ids[0]).not.toBe(ids[1]);
    expect(resumeFroms).toEqual([0, 0]);
  });

  it("treats an empty continueSessionId as a fresh session rather than reusing an empty id", async () => {
    const ids: string[] = [];
    const fetchImpl = vi.fn().mockImplementation((_url: string, init: RequestInit) => {
      ids.push(JSON.parse(init.body as string).session_id);
      return Promise.resolve(sseResponse([frame({ kind: "completed", text: "ok" })]));
    });

    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hi", { continueSessionId: "" });
    });
    await waitFor(() => expect(ids).toHaveLength(1));

    expect(ids[0].length).toBeGreaterThanOrEqual(SESSION_ID_MIN_LENGTH);
    expect(ids[0].length).toBeLessThanOrEqual(SESSION_ID_MAX_LENGTH);
  });
});

describe("useAgentStream: heartbeat frames are liveness only", () => {
  it("never puts a heartbeat in the transcript", async () => {
    const fetchImpl = vi.fn().mockImplementation(() =>
      Promise.resolve(
        sseResponse([
          frame({ kind: "heartbeat" }),
          frame({ kind: "tool_call", tool_name: "get_account", tool_input: {} }),
          frame({ kind: "heartbeat" }),
          frame({ kind: "completed", text: "Done." }),
        ]),
      ),
    );
    const { result } = renderHook(() => useAgentStream({ ...baseOptions, fetchImpl }));

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.turnEnded).toBe(true));
    expect(result.current.transcript.map((e) => e.event.kind)).toEqual(["tool_call", "completed"]);
  });

  it("does not count heartbeats towards resume_from, so a resume replays nothing twice", async () => {
    const resumeFroms: number[] = [];
    const fetchImpl = vi
      .fn()
      .mockImplementationOnce((_url: string, init: RequestInit) => {
        resumeFroms.push(JSON.parse(init.body as string).resume_from);
        const encoder = new TextEncoder();
        const chunks = [
          frame({ kind: "tool_call", tool_name: "get_account", tool_input: {} }),
          frame({ kind: "heartbeat" }),
          frame({ kind: "heartbeat" }),
        ];
        const stream = new ReadableStream<Uint8Array>({
          pull(controller) {
            const next = chunks.shift();
            if (next === undefined) {
              controller.error(new Error("connection dropped"));
              return;
            }
            controller.enqueue(encoder.encode(next));
          },
        });
        return Promise.resolve({ ok: true, status: 200, body: stream } as unknown as Response);
      })
      .mockImplementationOnce((_url: string, init: RequestInit) => {
        resumeFroms.push(JSON.parse(init.body as string).resume_from);
        return Promise.resolve(sseResponse([frame({ kind: "completed", text: "Recovered." })]));
      });
    const { result } = renderHook(() =>
      useAgentStream({ ...baseOptions, fetchImpl, maxReconnectAttempts: 2 }),
    );

    await act(async () => {
      await result.current.sendPrompt("hello");
    });

    await waitFor(() => expect(result.current.turnEnded).toBe(true), { timeout: 5000 });
    expect(resumeFroms).toEqual([0, 1]);
    expect(result.current.transcript.map((e) => e.event.kind)).toEqual(["tool_call", "completed"]);
  });
});
