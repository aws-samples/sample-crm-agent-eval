// SPDX-License-Identifier: MIT
/**
 * `useCancelTurn`'s contract: all three dispositions are success, a cancel emits no agent event, and
 * the request carries the exact mode value. Assertions are on the composed request and the state
 * reached from a response.
 */

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BODY_DIGEST_HEADER, JWT_HEADER, sha256Hex } from "../../lib/agentClient";
import { CANCEL_MODE, CANCEL_MODE_KEY, useCancelTurn } from "./useCancelTurn";

const BASE_URL = "/agent";
const RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/crm-agent";
const SESSION_ID = "session-4f2c1b8e-6a3d-4e91-b7c5-0d82a1f6e394";

/**
 * SHA-256 of `{"mode":"cancel","session_id":"<SESSION_ID>"}`, computed independently of the code under
 * test with `node -e 'crypto.createHash("sha256")…'`.
 */
const CANCEL_BODY_SHA256 = "89c0b589d2d7baca410de5bdb83c0852f6da3dcb1d49bc8072a1d51b6f829ff7";  // pragma: allowlist secret

interface Stub {
  readonly calls: { url: string; init: RequestInit }[];
  readonly fetchImpl: typeof fetch;
}

/** A fetch that answers once with `status` and `body`, recording what it was asked. */
function stubFetch(status: number, body: unknown, options: { unreadable?: boolean } = {}): Stub {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetchImpl = ((url: string, init: RequestInit) => {
    calls.push({ url, init });
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      json: () =>
        options.unreadable === true
          ? Promise.reject(new Error("not json"))
          : Promise.resolve(body),
    } as unknown as Response);
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

function renderCancel(stub: Stub, getAccessToken = () => Promise.resolve("a-token")) {
  return renderHook(() =>
    useCancelTurn({
      agentBaseUrl: BASE_URL,
      agentRuntimeArn: RUNTIME_ARN,
      getAccessToken,
      fetchImpl: stub.fetchImpl,
    }),
  );
}

/**
 * A fetch that announces when it is entered and settles only when told, so a test observes an
 * in-flight request without timer-based polling.
 */
function gatedFetch(): {
  fetchImpl: typeof fetch;
  entered: Promise<void>;
  release: (value: Response) => void;
} {
  let announceEntered: () => void = () => {};
  const entered = new Promise<void>((resolve) => {
    announceEntered = resolve;
  });
  let release: (value: Response) => void = () => {};
  const fetchImpl = (() => {
    announceEntered();
    return new Promise<Response>((resolve) => {
      release = resolve;
    });
  }) as unknown as typeof fetch;
  return { fetchImpl, entered, release: (value) => release(value) };
}

function bodyOf(stub: Stub): Record<string, unknown> {
  return JSON.parse(String(stub.calls[0]?.init.body)) as Record<string, unknown>;
}

describe("the request the hook composes", () => {
  it("posts the cancel mode and the session id, and nothing else", async () => {
    const stub = stubFetch(200, { session_id: SESSION_ID, disposition: "accepted" });
    const { result } = renderCancel(stub);

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });

    expect(bodyOf(stub)).toEqual({ [CANCEL_MODE_KEY]: CANCEL_MODE, session_id: SESSION_ID });
  });

  it("sends the exact mode value, since only that value cancels", async () => {
    const stub = stubFetch(200, { disposition: "accepted" });
    const { result } = renderCancel(stub);

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });

    expect(bodyOf(stub)["mode"]).toBe("cancel");
  });

  it("carries no prompt: a cancel is not a turn with the prompt left out", async () => {
    const stub = stubFetch(200, { disposition: "accepted" });
    const { result } = renderCancel(stub);

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });

    expect(bodyOf(stub)).not.toHaveProperty("prompt");
  });

  it("posts to the same endpoint a turn goes to, because there is no cancel URL", async () => {
    const stub = stubFetch(200, { disposition: "accepted" });
    const { result } = renderCancel(stub);

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });

    expect(stub.calls[0]?.url).toBe(`${BASE_URL}/${encodeURIComponent(RUNTIME_ARN)}`);
    expect(stub.calls[0]?.init.method).toBe("POST");
  });

  it("sends the bearer token in X-Crm-Authorization and asks for JSON, not an event stream", async () => {
    const stub = stubFetch(200, { disposition: "accepted" });
    const { result } = renderCancel(stub);

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });

    const headers = stub.calls[0]?.init.headers as Record<string, string>;
    expect(headers[JWT_HEADER]).toBe("Bearer a-token");
    expect(headers["Accept"]).toBe("application/json");
    // `Authorization` is overwritten by CloudFront's SigV4 under OAC.
    expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain("authorization");
  });

  it("signs the body it actually sends with x-amz-content-sha256", async () => {
    const stub = stubFetch(200, { disposition: "accepted" });
    const { result } = renderCancel(stub);

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });

    const headers = stub.calls[0]?.init.headers as Record<string, string>;
    const sent = String(stub.calls[0]?.init.body);
    // Pinned vector.
    expect(sent).toBe(`{"mode":"cancel","session_id":"${SESSION_ID}"}`);
    expect(headers[BODY_DIGEST_HEADER]).toBe(CANCEL_BODY_SHA256);
    await expect(sha256Hex(sent)).resolves.toBe(headers[BODY_DIGEST_HEADER]);
  });

  it("fails closed, without a request, when the body cannot be hashed", async () => {
    // An unsigned cancel is never reported as landed.
    const digest = vi
      .spyOn(crypto.subtle, "digest")
      .mockRejectedValue(new Error("SubtleCrypto unavailable"));
    const logged = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const stub = stubFetch(200, { disposition: "accepted" });
    const { result } = renderCancel(stub);

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });

    expect(stub.calls).toHaveLength(0);
    expect(result.current.cancelState).toBe("failed");
    expect(result.current.failureReason).toBe("transport");
    expect(result.current.disposition).toBeUndefined();
    expect(logged).toHaveBeenCalled();
    digest.mockRestore();
    logged.mockRestore();
  });

  it("does not send a request at all for an empty session id", async () => {
    const stub = stubFetch(200, { disposition: "accepted" });
    const { result } = renderCancel(stub);

    await act(async () => {
      await result.current.cancelTurn("   ");
    });

    expect(stub.calls).toHaveLength(0);
    expect(result.current.cancelState).toBe("failed");
  });
});

describe("all three dispositions are success", () => {
  it.each(["accepted", "already_requested", "already_terminal"] as const)(
    "treats %s as a landed cancel, not a failure",
    async (reported) => {
      const stub = stubFetch(200, { session_id: SESSION_ID, disposition: reported });
      const { result } = renderCancel(stub);

      await act(async () => {
        await result.current.cancelTurn(SESSION_ID);
      });

      expect(result.current.cancelState).toBe("requested");
      expect(result.current.disposition).toBe(reported);
      expect(result.current.failureReason).toBeUndefined();
    },
  );

  it("returns the disposition to the caller", async () => {
    const stub = stubFetch(200, { disposition: "already_terminal" });
    const { result } = renderCancel(stub);

    let returned: string | undefined;
    await act(async () => {
      returned = await result.current.cancelTurn(SESSION_ID);
    });

    expect(returned).toBe("already_terminal");
  });

  it("treats a cancel after a terminal event as a no-op, producing no event", async () => {
    // The hook's surface has no member through which an agent event could reach the transcript.
    const stub = stubFetch(200, { disposition: "already_terminal" });
    const { result } = renderCancel(stub);

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });

    expect(Object.keys(result.current).sort()).toEqual([
      "cancelState",
      "cancelTurn",
      "disposition",
      "failureReason",
      "reset",
    ]);
    expect(result.current.disposition).toBe("already_terminal");
  });
});

describe("failures are reported, never retried and never thrown at the caller", () => {
  it.each([
    [401, "unauthenticated"],
    [403, "forbidden"],
    [400, "rejected"],
    [502, "rejected"],
  ] as const)("reports %d as %s", async (status, reason) => {
    const stub = stubFetch(status, { error: { code: "scope_denied" } });
    const { result } = renderCancel(stub);

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });

    expect(result.current.cancelState).toBe("failed");
    expect(result.current.failureReason).toBe(reason);
    expect(result.current.disposition).toBeUndefined();
  });

  it("keeps 401 and 403 distinct, because 403 is not a sign-in problem", async () => {
    // A 403 is not worded as "sign in again".
    const unauthenticated = renderCancel(stubFetch(401, {}));
    const forbidden = renderCancel(stubFetch(403, {}));

    await act(async () => {
      await unauthenticated.result.current.cancelTurn(SESSION_ID);
    });
    await act(async () => {
      await forbidden.result.current.cancelTurn(SESSION_ID);
    });

    expect(unauthenticated.result.current.failureReason).toBe("unauthenticated");
    expect(forbidden.result.current.failureReason).toBe("forbidden");
  });

  it("reports a network failure without retrying", async () => {
    let calls = 0;
    const fetchImpl = (() => {
      calls += 1;
      return Promise.reject(new Error("offline"));
    }) as unknown as typeof fetch;
    const { result } = renderCancel({ calls: [], fetchImpl });

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });

    expect(calls).toBe(1);
    expect(result.current.failureReason).toBe("transport");
  });

  it("does not report a landed cancel when the body cannot be read", async () => {
    // A 200 with an unreadable body does not read as success.
    const stub = stubFetch(200, null, { unreadable: true });
    const { result } = renderCancel(stub);

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });

    expect(result.current.cancelState).toBe("failed");
    expect(result.current.disposition).toBeUndefined();
  });

  it.each([
    ["an unknown disposition", { disposition: "maybe" }],
    ["no disposition at all", { session_id: SESSION_ID }],
    ["a null body", null],
  ])("does not report success for a 200 carrying %s", async (_label, body) => {
    const stub = stubFetch(200, body);
    const { result } = renderCancel(stub);

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });

    expect(result.current.cancelState).toBe("failed");
    expect(result.current.disposition).toBeUndefined();
  });

  it("reports a missing token as unauthenticated without calling fetch", async () => {
    const stub = stubFetch(200, { disposition: "accepted" });
    const { result } = renderCancel(stub, () => Promise.reject(new Error("not signed in")));

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });

    expect(stub.calls).toHaveLength(0);
    expect(result.current.failureReason).toBe("unauthenticated");
  });

  it("resolves rather than rejecting, so a clicked control leaves no unhandled rejection", async () => {
    const { result } = renderCancel(stubFetch(403, {}));

    await act(async () => {
      await expect(result.current.cancelTurn(SESSION_ID)).resolves.toBeUndefined();
    });
  });
});

describe("state transitions", () => {
  it("passes through requesting before reaching requested", async () => {
    // Synchronised on the stub being entered rather than on timer-based `waitFor`.
    const gate = gatedFetch();
    const { result } = renderCancel({ calls: [], fetchImpl: gate.fetchImpl });

    let pending!: Promise<unknown>;
    await act(async () => {
      pending = result.current.cancelTurn(SESSION_ID);
      await gate.entered;
    });

    expect(result.current.cancelState).toBe("requesting");

    await act(async () => {
      gate.release({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ disposition: "accepted" }),
      } as unknown as Response);
      await pending;
    });

    expect(result.current.cancelState).toBe("requested");
  });

  it("ignores a second cancel while the first is still open", async () => {
    /*
     * Synchronised on a signal the code under test raises, not on a microtask flush or a timer-based
     * `waitFor`, so the result does not depend on test ordering.
     */
    let calls = 0;
    let announceEntered: () => void = () => {};
    const entered = new Promise<void>((resolve) => {
      announceEntered = resolve;
    });
    const fetchImpl = (() => {
      calls += 1;
      announceEntered();
      // Never settles, so the first cancel stays in flight.
      return new Promise<Response>(() => {});
    }) as unknown as typeof fetch;
    const { result } = renderCancel({ calls: [], fetchImpl });

    let second: string | undefined = "not-yet-called";
    await act(async () => {
      const first = result.current.cancelTurn(SESSION_ID);
      await entered;
      second = await result.current.cancelTurn(SESSION_ID);
      void first;
    });

    // A second click while one is in flight sends no second request.
    expect(calls).toBe(1);
    expect(second).toBeUndefined();
    expect(result.current.cancelState).toBe("requesting");
  });

  it("allows a second cancel once the first has settled", async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    const fetchImpl = ((url: string, init: RequestInit) => {
      calls.push({ url, init });
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ disposition: "already_requested" }),
      } as unknown as Response);
    }) as unknown as typeof fetch;
    const { result } = renderCancel({ calls, fetchImpl });

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });
    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });

    expect(calls).toHaveLength(2);
    expect(result.current.disposition).toBe("already_requested");
  });

  it("returns to idle on reset, so a later turn starts clean", async () => {
    const { result } = renderCancel(stubFetch(200, { disposition: "accepted" }));

    await act(async () => {
      await result.current.cancelTurn(SESSION_ID);
    });
    act(() => {
      result.current.reset();
    });

    expect(result.current.cancelState).toBe("idle");
    expect(result.current.disposition).toBeUndefined();
    expect(result.current.failureReason).toBeUndefined();
  });

  it("clears a previous failure when a new cancel is attempted", async () => {
    const failing = renderCancel(stubFetch(502, {}));
    await act(async () => {
      await failing.result.current.cancelTurn(SESSION_ID);
    });
    expect(failing.result.current.failureReason).toBe("rejected");

    // A stale failure reason does not survive into a fresh `requesting` state.
    const gate = gatedFetch();
    const retried = renderCancel({ calls: [], fetchImpl: gate.fetchImpl });

    let pending!: Promise<unknown>;
    await act(async () => {
      pending = retried.result.current.cancelTurn(SESSION_ID);
      await gate.entered;
    });

    expect(retried.result.current.cancelState).toBe("requesting");
    expect(retried.result.current.failureReason).toBeUndefined();

    await act(async () => {
      gate.release({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ disposition: "accepted" }),
      } as unknown as Response);
      await pending;
    });
  });
});
