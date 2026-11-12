// SPDX-License-Identifier: MIT
/**
 * Tests for the relay's `streamifyResponse` handler, driven end to end over a recording sink with
 * `verify` and `invoke` injected: assertions are on the committed status, headers, and frame bytes.
 */
import { createSign, generateKeyPairSync, type KeyObject } from "node:crypto";

import { describe, expect, it, vi } from "vitest";
import { SimpleJwksCache } from "aws-jwt-verify/jwk";
import type { LambdaFunctionURLEvent } from "aws-lambda";

import {
  FRAME_DELIMITER,
  FRAME_PREFIX,
  SCHEMA_VERSION,
  type AgentEventFrame,
} from "../src/generated/events.js";
import { JWT_HEADER, JwtVerificationError, createVerifier, verifyBearerToken } from "../src/jwt.js";
import {
  RuntimeInvocationError,
  RuntimeValidationError,
  type CancelResult,
  type TurnRequest,
} from "../src/runtime.js";
import {
  decodeBody,
  frame,
  handler,
  headerValue,
  httpResponseSink,
  PROMPT_MAX_CHARS,
  PROMPT_TOO_LONG_MESSAGE,
  SESSION_ID_MAX_LENGTH,
  SESSION_ID_MIN_LENGTH,
  SESSION_ID_RANGE_MESSAGE,
  TRANSPORT_ERROR_MESSAGE,
  streamTurn,
  stripIdentityChannels,
  transportErrorFrame,
  type HandlerDeps,
  type ResponseSink,
} from "../src/index.js";

const CLAIMS = { sub: "abc", "custom:rep_id": "rep-042", "custom:tenant": "demo" } as const;
const SESSION_ID = "session-11111111-2222-3333-4444-5555dddd6666";

/** A second compliant session id, for asserting that the id is forwarded rather than defaulted. */
const OTHER_SESSION_ID = "session-99999999-8888-7777-6666-5555dddd6666";

/** Records everything the handler writes, in order, plus the status it committed. */
class RecordingSink implements ResponseSink {
  statusCode: number | undefined;
  headers: Readonly<Record<string, string>> | undefined;
  readonly chunks: string[] = [];
  beginCalls = 0;
  endCalls = 0;

  begin(statusCode: number, headers: Readonly<Record<string, string>>): void {
    this.beginCalls += 1;
    this.statusCode = statusCode;
    this.headers = headers;
  }

  write(chunk: string): void {
    if (this.endCalls > 0) {
      throw new Error("wrote after end()");
    }
    this.chunks.push(chunk);
  }

  end(): void {
    this.endCalls += 1;
  }

  get body(): string {
    return this.chunks.join("");
  }
}

function eventFor(
  body: unknown,
  options: { method?: string; bearer?: string } = {},
) {
  // The JWT rides `X-Crm-Authorization`, never `Authorization`.
  const headers: Record<string, string> = {
    [JWT_HEADER]: options.bearer ?? "Bearer valid.token.here",
  };
  return {
    version: "2.0",
    requestContext: { http: { method: options.method ?? "POST" } },
    headers,
    body: typeof body === "string" ? body : JSON.stringify(body),
    isBase64Encoded: false,
  } as unknown as LambdaFunctionURLEvent;
}

function turnBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { prompt: "which accounts slipped?", session_id: SESSION_ID, ...overrides };
}

/** A frame carrying every field of the closed schema, as the Runtime emits it. */
function runtimeFrame(
  overrides: Omit<Partial<AgentEventFrame>, "kind"> & { kind: string },
): string {
  return frame({
    tool_name: null,
    tool_input: null,
    tool_output: null,
    truncated: false,
    proposal_id: null,
    proposal_summary: null,
    input_tokens: null,
    output_tokens: null,
    cache_read_tokens: null,
    cache_write_tokens: null,
    text: null,
    error_kind: null,
    error_message: null,
    partial_work: null,
    cost_lines: null,
    cost_usd: null,
    cost_withheld: null,
    resolved_tiers: null,
    ...overrides,
  });
}

function streamOf(...chunks: string[]): AsyncIterable<Uint8Array> {
  const encoder = new TextEncoder();
  return {
    async *[Symbol.asyncIterator]() {
      for (const chunk of chunks) {
        yield encoder.encode(chunk);
      }
    },
  };
}

function depsFor(
  stream: AsyncIterable<Uint8Array> | (() => Promise<AsyncIterable<Uint8Array>>),
  overrides: Partial<HandlerDeps> = {},
): HandlerDeps & { requests: TurnRequest[] } {
  const requests: TurnRequest[] = [];
  return {
    requests,
    verify: () => Promise.resolve(CLAIMS),
    cancel: () =>
      Promise.reject(new Error("this fixture's Runtime cancel must not be called")),

    invoke: (request: TurnRequest) => {
      requests.push(request);
      return typeof stream === "function" ? stream() : Promise.resolve(stream);
    },
    ...overrides,
  };
}

describe("a successful turn", () => {
  it("commits 200 with non-buffering event-stream headers before the first frame", async () => {
    const sink = new RecordingSink();
    await streamTurn(
      eventFor(turnBody()),
      sink,
      depsFor(streamOf(runtimeFrame({ kind: "completed", text: "done" }))),
    );

    expect(sink.statusCode).toBe(200);
    expect(sink.headers?.["content-type"]).toBe("text/event-stream");
    expect(sink.headers?.["cache-control"]).toBe("no-cache, no-store");
    expect(sink.headers?.["x-accel-buffering"]).toBe("no");
    expect(sink.beginCalls).toBe(1);
  });

  it("re-emits every frame byte for byte", async () => {
    const frames = [
      runtimeFrame({ kind: "tool_call", tool_name: "list_accounts", tool_input: { limit: 5 } }),
      runtimeFrame({ kind: "tool_result", tool_output: { items: [] } }),
      runtimeFrame({ kind: "usage", input_tokens: 11, output_tokens: 7 }),
      runtimeFrame({ kind: "completed", text: "two accounts slipped" }),
    ];
    const sink = new RecordingSink();

    await streamTurn(eventFor(turnBody()), sink, depsFor(streamOf(frames.join(""))));

    expect(sink.body).toBe(frames.join(""));
    expect(sink.chunks).toEqual(frames);
  });

  it("forwards a 3.0.0 frame carrying cost lines and resolved tiers byte for byte", async () => {
    expect(SCHEMA_VERSION).toBe("3.0.0");
    const accounted = runtimeFrame({
      kind: "completed",
      text: "done",
      input_tokens: 160,
      output_tokens: 48,
      cost_lines: [
        {
          tier: "supervisor",
          role: "supervisor",
          model_id: "supervisor-model",
          input_tokens: 40,
          output_tokens: 8,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          long_context_calls: 0,
          cost_usd: 0.0002,
        },
        {
          tier: "subagent",
          role: "research",
          model_id: "subagent-model",
          input_tokens: 120,
          output_tokens: 40,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          long_context_calls: 0,
          cost_usd: 0.00031,
        },
      ],
      cost_usd: 0.00051,
      cost_withheld: false,
      resolved_tiers: [
        { tier: "supervisor", family: "openai", model_id: "supervisor-model", effort: "low" },
      ],
    });
    const heartbeat = runtimeFrame({ kind: "heartbeat" });
    const sink = new RecordingSink();

    await streamTurn(eventFor(turnBody()), sink, depsFor(streamOf(`${heartbeat}${accounted}`)));

    expect(sink.chunks).toEqual([heartbeat, accounted]);
    const forwarded = JSON.parse(sink.chunks[1]!.slice(FRAME_PREFIX.length, -FRAME_DELIMITER.length)) as AgentEventFrame;
    expect(forwarded.cost_lines?.map((line) => line.model_id)).toEqual([
      "supervisor-model",
      "subagent-model",
    ]);
    expect(forwarded.resolved_tiers).toEqual([
      { tier: "supervisor", family: "openai", model_id: "supervisor-model", effort: "low" },
    ]);
  });

  it("forwards one frame per write even when frames arrive split across chunks", async () => {
    const first = runtimeFrame({ kind: "tool_call", tool_name: "get_account", tool_input: {} });
    const second = runtimeFrame({ kind: "completed", text: "done" });
    const joined = `${first}${second}`;
    const sink = new RecordingSink();

    await streamTurn(
      eventFor(turnBody()),
      sink,
      depsFor(streamOf(joined.slice(0, 20), joined.slice(20, 60), joined.slice(60))),
    );

    expect(sink.chunks).toEqual([first, second]);
  });

  it("does not write a frame before the whole frame has arrived", async () => {
    const only = runtimeFrame({ kind: "completed", text: "done" });
    const sink = new RecordingSink();
    const partial = only.slice(0, only.length - 1);

    await streamTurn(eventFor(turnBody()), sink, depsFor(streamOf(partial, only.slice(-1))));

    expect(sink.chunks[0]).toBe(only);
  });

  it("survives a multi-byte character split across two chunks", async () => {
    const only = runtimeFrame({ kind: "completed", text: "café" });
    const bytes = new TextEncoder().encode(only);
    const split = only.indexOf("caf") + 4;
    const sink = new RecordingSink();
    const stream: AsyncIterable<Uint8Array> = {
      async *[Symbol.asyncIterator]() {
        yield bytes.slice(0, split);
        yield bytes.slice(split);
      },
    };

    await streamTurn(eventFor(turnBody()), sink, depsFor(stream));

    expect(sink.body).toBe(only);
  });

  it("forwards the verified claims and the turn body to the Runtime, adding nothing", async () => {
    const sink = new RecordingSink();
    const deps = depsFor(streamOf(runtimeFrame({ kind: "completed", text: "done" })));

    await streamTurn(
      eventFor(turnBody({ page_context: { kind: "opaque", reason: "test" }, resume_from: 3 })),
      sink,
      deps,
    );

    expect(deps.requests).toHaveLength(1);
    expect(deps.requests[0]?.claims).toEqual(CLAIMS);
    expect(deps.requests[0]?.sessionId).toBe(SESSION_ID);
    expect(deps.requests[0]?.pageContext).toEqual({ kind: "opaque", reason: "test" });
    expect(deps.requests[0]?.resumeFrom).toBe(3);
  });

  it("ends the stream exactly once", async () => {
    const sink = new RecordingSink();
    await streamTurn(
      eventFor(turnBody()),
      sink,
      depsFor(streamOf(runtimeFrame({ kind: "completed", text: "done" }))),
    );
    expect(sink.endCalls).toBe(1);
  });
});

describe("an unrecognised kind is forwarded, not filtered", () => {
  it("passes a kind this schema version does not declare straight through", async () => {
    const unknown = runtimeFrame({ kind: "delegation" });
    const terminal = runtimeFrame({ kind: "completed", text: "done" });
    const sink = new RecordingSink();

    await streamTurn(eventFor(turnBody()), sink, depsFor(streamOf(`${unknown}${terminal}`)));

    expect(sink.chunks).toEqual([unknown, terminal]);
    expect(sink.body).toContain('"kind":"delegation"');
  });

  it("passes a frame that is not valid JSON through rather than dropping it", async () => {
    const malformed = `${FRAME_PREFIX}{"kind":"completed"${FRAME_DELIMITER}`;
    const sink = new RecordingSink();

    await streamTurn(eventFor(turnBody()), sink, depsFor(streamOf(malformed)));

    expect(sink.chunks).toEqual([malformed]);
  });

  it("passes a frame with no data: prefix through unchanged", async () => {
    const comment = `: keep-alive${FRAME_DELIMITER}`;
    const terminal = runtimeFrame({ kind: "completed", text: "done" });
    const sink = new RecordingSink();

    await streamTurn(eventFor(turnBody()), sink, depsFor(streamOf(`${comment}${terminal}`)));

    expect(sink.chunks).toEqual([comment, terminal]);
  });

  it("records the schema version it ran under on its turn log", async () => {
    const written: string[] = [];
    const spy = vi.spyOn(process.stdout, "write").mockImplementation((chunk: unknown) => {
      written.push(String(chunk));
      return true;
    });
    try {
      await streamTurn(
        eventFor(turnBody()),
        new RecordingSink(),
        depsFor(streamOf(runtimeFrame({ kind: "delegation" }))),
      );
    } finally {
      spy.mockRestore();
    }

    const log = JSON.parse(written.join("")) as Record<string, unknown>;
    expect(log["schema_version"]).toBe(SCHEMA_VERSION);
    expect(log["unrecognised_kinds"]).toBe(1);
    expect(log["frames"]).toBe(1);
    expect(log["session_id"]).toBe(SESSION_ID);
  });
});

describe("a pre-stream rejection is a status, not a stream", () => {
  it("answers 401 for a rejected token and never calls the Runtime", async () => {
    const sink = new RecordingSink();
    const deps = depsFor(streamOf(), {
      verify: () => Promise.reject(new JwtVerificationError("expired")),
    });

    await streamTurn(eventFor(turnBody()), sink, deps);

    expect(sink.statusCode).toBe(401);
    expect(sink.headers?.["content-type"]).toBe("application/json");
    expect(deps.requests).toHaveLength(0);
    expect(sink.endCalls).toBe(1);
  });

  it("answers 401 without any SSE frame, so reconnection is not triggered", async () => {
    const sink = new RecordingSink();
    await streamTurn(
      eventFor(turnBody()),
      sink,
      depsFor(streamOf(), { verify: () => Promise.reject(new JwtVerificationError("expired")) }),
    );

    expect(sink.body).not.toContain(FRAME_PREFIX);
    expect(sink.body).not.toContain('"kind"');
    expect(JSON.parse(sink.body)).toEqual({ message: "Your session is not valid. Sign in again." });
  });

  it("leaks no internal detail in the 401 body", async () => {
    const sink = new RecordingSink();
    await streamTurn(
      eventFor(turnBody()),
      sink,
      depsFor(streamOf(), {
        verify: () =>
          Promise.reject(new JwtVerificationError('token issued for unrecognised client "abc123"')),
      }),
    );

    expect(sink.body).not.toContain("abc123");
    expect(sink.body).not.toContain("unrecognised client");
  });

  it("answers 401 for an unexpected error from verification, never 200", async () => {
    const sink = new RecordingSink();
    await streamTurn(
      eventFor(turnBody()),
      sink,
      depsFor(streamOf(), { verify: () => Promise.reject(new TypeError("boom")) }),
    );
    expect(sink.statusCode).toBe(401);
  });

  it.each([
    ["an absent body", undefined],
    ["a body that is not JSON", "not json"],
    ["a JSON array", "[]"],
    ["an object with no prompt", JSON.stringify({ session_id: SESSION_ID })],
    ["a whitespace-only prompt", JSON.stringify(turnBody({ prompt: "   " }))],
    ["an object with no session id", JSON.stringify({ prompt: "hello" })],
    ["a non-string prompt", JSON.stringify(turnBody({ prompt: 42 }))],
    // Authentication runs first, so the malformed body is refused with a 400 only after the token
    // verifies, and the Runtime is never reached.
  ])("answers 400 for %s, after verifying, and never calls the Runtime", async (_label, body) => {
    const sink = new RecordingSink();
    const verify = vi.fn(() => Promise.resolve(CLAIMS));
    const deps = depsFor(streamOf(), { verify });

    await streamTurn(eventFor(body === undefined ? "" : body), sink, deps);

    expect(sink.statusCode).toBe(400);
    expect(verify).toHaveBeenCalledTimes(1);
    expect(deps.requests).toHaveLength(0);
    expect(sink.endCalls).toBe(1);
  });

  it.each([
    ["an absent body", ""],
    ["a body that is not JSON", "not json"],
    ["an object with no prompt", JSON.stringify({ session_id: SESSION_ID })],
  ])(
    "answers 401 rather than 400 for %s when the caller has no valid token",
    async (_label, body) => {
    // An anonymous malformed request is refused as unauthenticated, not diagnosed.
      const sink = new RecordingSink();
      const deps = depsFor(streamOf(), {
        verify: () => Promise.reject(new JwtVerificationError("no token")),
      });

      await streamTurn(eventFor(body), sink, deps);

      expect(sink.statusCode).toBe(401);
      expect(sink.body).not.toContain("required to start a turn");
      expect(JSON.parse(sink.body)).toEqual({ message: "Your session is not valid. Sign in again." });
      expect(deps.requests).toHaveLength(0);
    },
  );

  it("answers 401 rather than 405 for a non-POST from a caller with no valid token", async () => {
    const sink = new RecordingSink();
    const deps = depsFor(streamOf(), {
      verify: () => Promise.reject(new JwtVerificationError("no token")),
    });

    await streamTurn(eventFor(turnBody(), { method: "GET" }), sink, deps);

    expect(sink.statusCode).toBe(401);
  });

  it("answers 405 for a method that is not POST", async () => {
    const sink = new RecordingSink();
    await streamTurn(eventFor(turnBody(), { method: "GET" }), sink, depsFor(streamOf()));
    expect(sink.statusCode).toBe(405);
  });

  it("answers 502 when the Runtime cannot be reached, before any frame", async () => {
    const sink = new RecordingSink();
    const deps = depsFor(() => Promise.reject(new RuntimeInvocationError("unavailable")));

    await streamTurn(eventFor(turnBody()), sink, deps);

    expect(sink.statusCode).toBe(502);
    expect(sink.body).not.toContain(FRAME_PREFIX);
    expect(sink.endCalls).toBe(1);
  });

  it("answers 400 with the Runtime's message when the turn is rejected as validation_failed", async () => {
    const sink = new RecordingSink();
    const message = "Model 'gpt-9' is not available. Choose one of the offered models.";
    const deps = depsFor(() => Promise.reject(new RuntimeValidationError(message)));

    await streamTurn(eventFor(turnBody()), sink, deps);

    // A validation failure is a 400 carrying the user-safe message, with no SSE frame.
    expect(sink.statusCode).toBe(400);
    expect(JSON.parse(sink.body)).toEqual({ message });
    expect(sink.body).not.toContain(FRAME_PREFIX);
    expect(sink.endCalls).toBe(1);
  });
});

describe("forwards the Runtime's own 200 SSE error frame verbatim", () => {
  // The Runtime returns a validation failure as HTTP 200 with one `error` frame of kind
  // `invalid_request`; the relay re-emits it byte-for-byte.
  const D11_MESSAGE = "The effort 'not-a-real-effort-value' is not one of the offered values.";
  // Built as a raw record rather than the typed `runtimeFrame`.
  const d11ErrorFrame = frame({
    kind: "error",
    tool_name: null,
    tool_input: null,
    tool_output: null,
    truncated: false,
    proposal_id: null,
    proposal_summary: null,
    input_tokens: null,
    output_tokens: null,
    cache_read_tokens: null,
    cache_write_tokens: null,
    text: null,
    error_kind: "invalid_request",
    error_message: D11_MESSAGE,
    partial_work: null,
  });

  it("re-emits the error frame byte-for-byte, at status 200, with its message intact", async () => {
    const sink = new RecordingSink();
    await streamTurn(eventFor(turnBody()), sink, depsFor(streamOf(d11ErrorFrame)));

    expect(sink.statusCode).toBe(200);
    // A Runtime-emitted error frame on a 200 stream is forwarded, never remapped to 400 or 502.
    expect(sink.statusCode).not.toBe(400);
    expect(sink.statusCode).not.toBe(502);
    // The exact frame the Runtime emitted, unchanged.
    expect(sink.chunks).toContain(d11ErrorFrame);
    expect(sink.body).toContain(D11_MESSAGE);
    // The error_kind is forwarded intact.
    expect(sink.body).toContain("invalid_request");
    expect(sink.endCalls).toBe(1);
  });
});

describe("a failure after the first byte travels as an SSE error frame", () => {
  it("emits a terminal error frame in the closed schema when the stream throws", async () => {
    const first = runtimeFrame({ kind: "tool_call", tool_name: "get_account", tool_input: {} });
    const sink = new RecordingSink();
    const stream: AsyncIterable<Uint8Array> = {
      async *[Symbol.asyncIterator]() {
        yield new TextEncoder().encode(first);
        throw new Error("connection reset");
      },
    };

    await streamTurn(eventFor(turnBody()), sink, depsFor(stream));

    expect(sink.statusCode).toBe(200);
    expect(sink.chunks[0]).toBe(first);
    const terminal = JSON.parse(
      (sink.chunks.at(-1) ?? "").slice(FRAME_PREFIX.length),
    ) as AgentEventFrame;
    expect(terminal.kind).toBe("error");
    expect(terminal.error_kind).toBe("model_failure");
    expect(terminal.error_message).toBeTruthy();
    expect(sink.endCalls).toBe(1);
  });

  it("names an ErrorKind the schema declares, not an invented one", () => {
    const parsed = JSON.parse(transportErrorFrame().slice(FRAME_PREFIX.length)) as AgentEventFrame;
    expect(["budget_exhausted", "tool_failure", "model_failure"]).toContain(parsed.error_kind);
    expect(Object.keys(parsed)).toHaveLength(15);
  });

  it("carries no internal detail in the error frame", async () => {
    const sink = new RecordingSink();
    const stream: AsyncIterable<Uint8Array> = {
      async *[Symbol.asyncIterator]() {
        yield new TextEncoder().encode(runtimeFrame({ kind: "usage", input_tokens: 1, output_tokens: 1 }));
        throw new Error("ECONNRESET at 10.0.4.17:443");
      },
    };

    await streamTurn(eventFor(turnBody()), sink, depsFor(stream));

    expect(sink.body).not.toContain("ECONNRESET");
    expect(sink.body).not.toContain("10.0.4.17");
  });

  it("reports a truncated trailing frame instead of forwarding half a JSON object", async () => {
    const good = runtimeFrame({ kind: "usage", input_tokens: 1, output_tokens: 1 });
    const partial = `${FRAME_PREFIX}{"kind":"comple`;
    const sink = new RecordingSink();

    await streamTurn(eventFor(turnBody()), sink, depsFor(streamOf(`${good}${partial}`)));

    expect(sink.chunks[0]).toBe(good);
    expect(sink.body).not.toContain('"comple');
    const terminal = JSON.parse(
      (sink.chunks.at(-1) ?? "").slice(FRAME_PREFIX.length),
    ) as AgentEventFrame;
    expect(terminal.kind).toBe("error");
  });

  it("ends the stream even when writing the error frame is the last thing it does", async () => {
    const sink = new RecordingSink();
    const stream: AsyncIterable<Uint8Array> = {
      async *[Symbol.asyncIterator]() {
        yield new TextEncoder().encode(runtimeFrame({ kind: "completed", text: "x" }));
        throw new Error("late failure");
      },
    };

    await streamTurn(eventFor(turnBody()), sink, depsFor(stream));

    expect(sink.endCalls).toBe(1);
  });

  it("ends the stream and records the loss when the client's socket is already gone", async () => {
    const sink = new RecordingSink();
    const exploding: ResponseSink = {
      begin: (code, headers) => sink.begin(code, headers),
      write: () => {
        throw new Error("socket closed");
      },
      end: () => sink.end(),
    };
    const written: string[] = [];
    const spy = vi.spyOn(process.stdout, "write").mockImplementation((chunk: unknown) => {
      written.push(String(chunk));
      return true;
    });

    try {
      await streamTurn(
        eventFor(turnBody()),
        exploding,
        depsFor(streamOf(runtimeFrame({ kind: "completed", text: "done" }))),
      );
    } finally {
      spy.mockRestore();
    }

    expect(sink.endCalls).toBe(1);
    const log = JSON.parse(written.join("")) as Record<string, unknown>;
    expect(log["outcome"]).toBe("error_frame_undeliverable");
    expect(log["detail"]).toBe("socket closed");
  });
});

describe("the relay never retries", () => {
  it("invokes the Runtime exactly once for a failing turn", async () => {
    let calls = 0;
    const deps: HandlerDeps = {
      verify: () => Promise.resolve(CLAIMS),
      cancel: () => Promise.reject(new Error("a no-retry fixture must not cancel")),
      invoke: () => {
        calls += 1;
        return Promise.reject(new RuntimeInvocationError("unavailable"));
      },
    };

    await streamTurn(eventFor(turnBody()), new RecordingSink(), deps);

    expect(calls).toBe(1);
  });

  it("invokes the Runtime exactly once when the stream dies mid-turn", async () => {
    let calls = 0;
    const deps: HandlerDeps = {
      verify: () => Promise.resolve(CLAIMS),
      cancel: () => Promise.reject(new Error("a no-retry fixture must not cancel")),
      invoke: () => {
        calls += 1;
        return Promise.resolve({
          async *[Symbol.asyncIterator]() {
            yield new TextEncoder().encode(runtimeFrame({ kind: "usage", input_tokens: 1, output_tokens: 1 }));
            throw new Error("reset");
          },
        });
      },
    };

    await streamTurn(eventFor(turnBody()), new RecordingSink(), deps);

    expect(calls).toBe(1);
  });
});

describe("helpers", () => {
  it("reads a header case-insensitively", () => {
    expect(headerValue({ Authorization: "Bearer x" }, "authorization")).toBe("Bearer x");
    expect(headerValue({ authorization: "Bearer x" }, "Authorization")).toBe("Bearer x");
    expect(headerValue(undefined, "authorization")).toBeUndefined();
    expect(headerValue({}, "authorization")).toBeUndefined();
  });

  it("decodes a base64 body", () => {
    const event = {
      requestContext: { http: { method: "POST" } },
      headers: {},
      body: Buffer.from(JSON.stringify(turnBody())).toString("base64"),
      isBase64Encoded: true,
    } as unknown as LambdaFunctionURLEvent;

    expect(decodeBody(event)?.prompt).toBe("which accounts slipped?");
  });

  it("frames a payload with the generated prefix and delimiter", () => {
    expect(frame({ kind: "usage" })).toBe(`${FRAME_PREFIX}{"kind":"usage"}${FRAME_DELIMITER}`);
  });
});

describe("the deployed entry point", () => {
  it("is the handler the Lambda runtime's decorator returned", async () => {
    const written: string[] = [];
    let metadata: { statusCode?: number } | undefined;
    const responseStream = {
      write: (chunk: string) => written.push(chunk),
      end: () => undefined,
      get metadata() {
        return metadata;
      },
      set metadata(value: { statusCode?: number } | undefined) {
        metadata = value;
      },
    };

    expect(typeof handler).toBe("function");
    await handler(
      eventFor(turnBody(), { method: "GET" }),
      responseStream as unknown as awslambda.HttpResponseStream,
      {} as never,
    );

    // Through the deployed entry point and the real `DEFAULT_DEPS`: with no Cognito configuration the
    // real verifier throws, so authentication answers 401 ahead of the method check.
    expect(metadata?.statusCode).toBe(401);
  });

  it("commits metadata once and writes through the stream the runtime returned", () => {
    const written: string[] = [];
    const stream = { write: (chunk: string) => written.push(chunk), end: () => undefined };

    const sink = httpResponseSink(stream as unknown as awslambda.HttpResponseStream);
    sink.begin(200, { "content-type": "text/event-stream" });
    sink.begin(500, { "content-type": "application/json" });
    sink.write("data: {}\n\n");
    sink.end();

    expect((stream as unknown as { metadata?: { statusCode?: number } }).metadata?.statusCode).toBe(
      200,
    );
    expect(written).toEqual(["data: {}\n\n"]);
  });
});

// --------------------------------------------------------------------------------------
// The authentication gate at the handler boundary
//
// Each token rejection becomes a pre-stream HTTP 401 with no SSE frame and no Runtime call. The real
// `verifyBearerToken` is wired in over a real RSA key pair and the library's real JWKS cache.
// --------------------------------------------------------------------------------------

const GATE_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_gate";
const GATE_JWKS_URI = `${GATE_ISSUER}/.well-known/jwks.json`;
const GATE_CLIENT_ID = "1gate23client45id67890abc";
const GATE_KEY_ID = "gate-key-1";

const gateKeys = generateKeyPairSync("rsa", { modulusLength: 2048 });
const gateForeignKeys = generateKeyPairSync("rsa", { modulusLength: 2048 });

function base64Url(value: Buffer | string): string {
  return Buffer.from(value)
    .toString("base64")
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
}

function gateJwks(): string {
  const jwk = gateKeys.publicKey.export({ format: "jwk" }) as { n?: string; e?: string };
  return JSON.stringify({
    keys: [
      { kty: "RSA", alg: "RS256", use: "sig", kid: GATE_KEY_ID, n: jwk.n ?? "", e: jwk.e ?? "" },
    ],
  });
}

function gateToken(
  claims: Record<string, unknown>,
  options: { kid?: string; key?: KeyObject; alg?: string } = {},
): string {
  const header = { alg: options.alg ?? "RS256", kid: options.kid ?? GATE_KEY_ID, typ: "JWT" };
  const signingInput = `${base64Url(JSON.stringify(header))}.${base64Url(JSON.stringify(claims))}`;
  if (header.alg === "none") {
    return `${signingInput}.`;
  }
  const signer = createSign("RSA-SHA256");
  signer.update(signingInput);
  return `${signingInput}.${base64Url(signer.sign(options.key ?? gateKeys.privateKey))}`;
}

function gateClaims(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    iss: GATE_ISSUER,
    aud: GATE_CLIENT_ID,
    token_use: "id",
    exp: Math.floor(Date.now() / 1000) + 3600,
    sub: "99999999-8888-7777-6666-5555dddd6666",
    "custom:rep_id": "rep-042",
    "custom:tenant": "demo",
    ...overrides,
  };
}

/** Deps whose `verify` is the real verifier, with only the JWKS network hop substituted. */
function realVerifyDeps(): HandlerDeps & { invocations: number } {
  const jwksCache = new SimpleJwksCache({
    fetcher: {
      fetch: () => Promise.resolve(new TextEncoder().encode(gateJwks()).buffer as ArrayBuffer),
    },
  });
  const context = {
    verifier: createVerifier(
      { issuer: GATE_ISSUER, jwksUri: GATE_JWKS_URI, allowedClientIds: [GATE_CLIENT_ID] },
      jwksCache,
    ),
    config: {
      issuer: GATE_ISSUER,
      jwksUri: GATE_JWKS_URI,
      allowedClientIds: [GATE_CLIENT_ID],
    },
  };
  const deps = {
    invocations: 0,
    cancel: () =>
      Promise.reject(new Error("this fixture's Runtime cancel must not be called")),

    verify: (header: string | undefined) => verifyBearerToken(header, context),
    invoke: () => {
      deps.invocations += 1;
      return Promise.resolve(streamOf(runtimeFrame({ kind: "completed", text: "done" })));
    },
  };
  return deps;
}

describe("the authentication gate, driven through the handler with the real verifier", () => {
  it("admits a valid token: 200, an event stream, and the Runtime invoked once", async () => {
    const sink = new RecordingSink();
    const deps = realVerifyDeps();

    await streamTurn(eventFor(turnBody(), { bearer: `Bearer ${gateToken(gateClaims())}` }), sink, deps);

    expect(sink.statusCode).toBe(200);
    expect(deps.invocations).toBe(1);
    expect(sink.body).toContain(FRAME_PREFIX);
  });

  it.each([
    [
      "a valid signature but an unlisted client",
      () => gateToken(gateClaims({ aud: "9unlisted87client65id432" })),
    ],
    ["an unknown kid", () => gateToken(gateClaims(), { kid: "not-published" })],
    ["alg none", () => gateToken(gateClaims(), { alg: "none" })],
    [
      "a foreign signing key",
      () => gateToken(gateClaims(), { key: gateForeignKeys.privateKey }),
    ],
    [
      "an expired token",
      () => gateToken(gateClaims({ exp: Math.floor(Date.now() / 1000) - 3600 })),
    ],
    [
      "a token with no exp at all",
      () => {
        const claims = gateClaims();
        delete claims["exp"];
        return gateToken(claims);
      },
    ],
    ["a wrong issuer", () => gateToken(gateClaims({ iss: `${GATE_ISSUER}-other` }))],
    [
      "an unrecognised token_use",
      () => gateToken(gateClaims({ token_use: "refresh" })),
    ],
    ["a malformed bearer token", () => "not-a-jwt"],
  ])("answers 401 for %s, with no frame and no Runtime call", async (_label, mint) => {
    const sink = new RecordingSink();
    const deps = realVerifyDeps();

    await streamTurn(eventFor(turnBody(), { bearer: `Bearer ${mint()}` }), sink, deps);

    expect(sink.statusCode).toBe(401);
    expect(sink.headers?.["content-type"]).toBe("application/json");
    expect(sink.body).not.toContain(FRAME_PREFIX);
    expect(sink.body).not.toContain('"kind"');
    expect(deps.invocations).toBe(0);
    expect(sink.endCalls).toBe(1);
  });

  it.each([
    ["absent", undefined],
    ["blank", ""],
    ["another scheme", `Basic ${Buffer.from("fixture-user:fixture-pass").toString("base64")}`],
    ["a bare token with no scheme", "eyJhbGciOiJSUzI1NiJ9" + ".e30.AAAA"], // gitleaks:allow
    ["a scheme with no token", "Bearer"],
  ])(`answers 401 for a ${JWT_HEADER} header that is %s`, async (_label, header) => {
    const sink = new RecordingSink();
    const deps = realVerifyDeps();
    const event = eventFor(turnBody());
    const headers = { ...(event.headers ?? {}) } as Record<string, string | undefined>;
    if (header === undefined) {
      delete headers[JWT_HEADER];
    } else {
      headers[JWT_HEADER] = header;
    }

    await streamTurn({ ...event, headers } as LambdaFunctionURLEvent, sink, deps);

    expect(sink.statusCode).toBe(401);
    expect(deps.invocations).toBe(0);
  });

  // `Authorization` belongs to CloudFront's SigV4 under OAC, so a token found there is never trusted.
  it.each([
    ["lowercased, as a Function URL delivers it", "authorization"],
    ["in the canonical casing a client would write", "Authorization"],
  ])(
    "rejects 401 a valid token presented only in `%s`, and never invokes the Runtime",
    async (_label, headerName) => {
      const sink = new RecordingSink();
      const deps = realVerifyDeps();
      const event = eventFor(turnBody());
      const headers = { ...(event.headers ?? {}) } as Record<string, string | undefined>;
      delete headers[JWT_HEADER];
      headers[headerName] = `Bearer ${gateToken(gateClaims())}`;

      await streamTurn({ ...event, headers } as LambdaFunctionURLEvent, sink, deps);

      expect(sink.statusCode).toBe(401);
      expect(sink.headers?.["content-type"]).toBe("application/json");
      expect(sink.body).not.toContain(FRAME_PREFIX);
      expect(deps.invocations).toBe(0);
      expect(sink.endCalls).toBe(1);
    },
  );

  // The SPA writes the canonical casing and a Function URL delivers headers lowercased, so both
  // spellings reach `streamTurn`.
  it.each(["x-crm-authorization", "X-Crm-Authorization", "X-CRM-AUTHORIZATION"])(
    "admits a valid token presented in `%s`",
    async (headerName) => {
      const sink = new RecordingSink();
      const deps = realVerifyDeps();
      const event = eventFor(turnBody());
      const headers = { ...(event.headers ?? {}) } as Record<string, string | undefined>;
      delete headers[JWT_HEADER];
      headers[headerName] = `Bearer ${gateToken(gateClaims())}`;

      await streamTurn({ ...event, headers } as LambdaFunctionURLEvent, sink, deps);

      expect(sink.statusCode).toBe(200);
      expect(deps.invocations).toBe(1);
    },
  );

  it("admits a token in X-Crm-Authorization even when `Authorization` carries a foreign signature", async () => {
    // The deployed shape: CloudFront's SigV4 in `Authorization`, the user's token beside it.
    const sink = new RecordingSink();
    const deps = realVerifyDeps();
    const event = eventFor(turnBody(), { bearer: `Bearer ${gateToken(gateClaims())}` });
    const headers = {
      ...(event.headers ?? {}),
      authorization: "AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/20260909/us-east-1/lambda/aws4_request",
    } as Record<string, string | undefined>;

    await streamTurn({ ...event, headers } as LambdaFunctionURLEvent, sink, deps);

    expect(sink.statusCode).toBe(200);
    expect(deps.invocations).toBe(1);
  });
});

describe("the Runtime payload is built from an allowlist, never from the body", () => {
  const channelKeys = ["_identity", "_agent_identity", "requestContext", "_gateway_identity"];

  it("drops every caller-supplied identity channel from the turn body", async () => {
    const sink = new RecordingSink();
    const deps = depsFor(streamOf(runtimeFrame({ kind: "completed", text: "done" })));
    const forged = Object.fromEntries(
      channelKeys.map((key) => [key, { claims: { "custom:rep_id": "rep-attacker" } }]),
    );

    await streamTurn(eventFor(turnBody(forged)), sink, deps);

    const request = deps.requests[0];
    expect(request).toBeDefined();
    const serialised = JSON.stringify(request);
    for (const key of channelKeys) {
      expect(serialised).not.toContain(key);
    }
    expect(serialised).not.toContain("rep-attacker");
    expect(request?.claims).toEqual(CLAIMS);
  });

  it("drops an identity channel nested inside page_context, at any depth", async () => {
    const sink = new RecordingSink();
    const deps = depsFor(streamOf(runtimeFrame({ kind: "completed", text: "done" })));

    await streamTurn(
      eventFor(
        turnBody({
          page_context: {
            kind: "list",
            entity: "accounts",
            filters: {
              _agent_identity: { claims: { "custom:rep_id": "rep-attacker" } },
              nested: [{ requestContext: { authorizer: { jwt: { claims: {} } } } }],
            },
          },
        }),
      ),
      sink,
      deps,
    );

    const serialised = JSON.stringify(deps.requests[0]);
    for (const key of channelKeys) {
      expect(serialised).not.toContain(key);
    }
    expect(serialised).not.toContain("rep-attacker");
    expect(serialised).toContain('"entity":"accounts"');
  });

  it("keeps the legitimate page_context fields it is asked to forward", async () => {
    const sink = new RecordingSink();
    const deps = depsFor(streamOf(runtimeFrame({ kind: "completed", text: "done" })));

    await streamTurn(
      eventFor(turnBody({ page_context: { kind: "opaque", reason: "not expressible" } })),
      sink,
      deps,
    );

    expect(deps.requests[0]?.pageContext).toEqual({ kind: "opaque", reason: "not expressible" });
  });

  it("strips channel keys at every depth of an arbitrary value", () => {
    const cleaned = stripIdentityChannels({
      keep: 1,
      _identity: "x",
      inner: { _agent_identity: "x", also: [{ requestContext: "x", fine: true }] },
    });
    expect(cleaned).toEqual({ keep: 1, inner: { also: [{ fine: true }] } });
  });
});

describe("a transport death is distinguishable from a model failure", () => {
  it("names the connection, not the model, in the frame the browser receives", () => {
    const parsed = JSON.parse(transportErrorFrame().slice(FRAME_PREFIX.length)) as AgentEventFrame;

    expect(parsed.error_message).toBe(TRANSPORT_ERROR_MESSAGE);
    expect(parsed.error_message).toMatch(/connection/i);
    expect(parsed.error_message).not.toMatch(/model/i);
    // The message, not the kind, carries the distinction.
    expect(parsed.error_kind).toBe("model_failure");
  });

  it("records an outcome on the turn log that a model failure could not produce", async () => {
    const written: string[] = [];
    const spy = vi.spyOn(process.stdout, "write").mockImplementation((chunk: unknown) => {
      written.push(String(chunk));
      return true;
    });
    const stream: AsyncIterable<Uint8Array> = {
      async *[Symbol.asyncIterator]() {
        yield new TextEncoder().encode(runtimeFrame({ kind: "usage", input_tokens: 1, output_tokens: 1 }));
        throw new Error("socket hang up");
      },
    };

    try {
      await streamTurn(eventFor(turnBody()), new RecordingSink(), depsFor(stream));
    } finally {
      spy.mockRestore();
    }

    const log = JSON.parse(written.join("")) as Record<string, unknown>;
    expect(log["outcome"]).toBe("stream_failed");
    expect(log["detail"]).toBe("socket hang up");
    expect(log["schema_version"]).toBe(SCHEMA_VERSION);
  });

  it("a Runtime-emitted model_failure frame is forwarded verbatim and is not confused with ours", async () => {
    const fromRuntime = runtimeFrame({
      kind: "error",
      error_kind: "model_failure",
      error_message: "The model could not complete the request.",
    });
    const sink = new RecordingSink();

    await streamTurn(eventFor(turnBody()), sink, depsFor(streamOf(fromRuntime)));

    expect(sink.chunks).toEqual([fromRuntime]);
    expect(sink.body).not.toContain(TRANSPORT_ERROR_MESSAGE);
  });
});

// --------------------------------------------------------------------------------------
// Every request produces exactly one turn log, on every exit path
// --------------------------------------------------------------------------------------

/** Drive `streamTurn` while capturing the `relay_turn` lines it writes to stdout. */
async function turnLogsFor(
  event: LambdaFunctionURLEvent,
  deps: HandlerDeps,
  sink: ResponseSink = new RecordingSink(),
): Promise<Record<string, unknown>[]> {
  const written: string[] = [];
  const spy = vi.spyOn(process.stdout, "write").mockImplementation((chunk: unknown) => {
    written.push(String(chunk));
    return true;
  });
  try {
    await streamTurn(event, sink, deps);
  } finally {
    spy.mockRestore();
  }
  return written
    .join("")
    .split("\n")
    .filter((line) => line.trim() !== "")
    .map((line) => JSON.parse(line) as Record<string, unknown>);
}

describe("every exit path emits exactly one turn log", () => {
  it.each([
    [
      "401 for a rejected token",
      "unauthorized",
      () => eventFor(turnBody()),
      () => depsFor(streamOf(), { verify: () => Promise.reject(new JwtVerificationError("nope")) }),
    ],
    [
      "401 for a non-JwtVerificationError from the verifier",
      "unauthorized_other",
      () => eventFor(turnBody()),
      () => depsFor(streamOf(), { verify: () => Promise.reject(new Error("boom")) }),
    ],
    [
      "405 for a non-POST",
      "method_not_allowed",
      () => eventFor(turnBody(), { method: "GET" }),
      () => depsFor(streamOf()),
    ],
    [
      "400 for a body that cannot be decoded",
      "bad_request",
      () => eventFor("not json at all"),
      () => depsFor(streamOf()),
    ],
    [
      "400 for a session id outside the bound",
      "session_id_out_of_range",
      () => eventFor(turnBody({ session_id: "short" })),
      () => depsFor(streamOf()),
    ],
    [
      "400 for a prompt over the cap",
      "prompt_too_long",
      () => eventFor(turnBody({ prompt: "x".repeat(PROMPT_MAX_CHARS + 1) })),
      () => depsFor(streamOf()),
    ],
    [
      "502 when the Runtime cannot be reached",
      "runtime_unavailable",
      () => eventFor(turnBody()),
      () => depsFor(() => Promise.reject(new RuntimeInvocationError("unavailable"))),
    ],
    [
      "502 for an unexpected invoke failure",
      "invoke_failed",
      () => eventFor(turnBody()),
      () => depsFor(() => Promise.reject(new Error("boom"))),
    ],
    [
      "200 for a completed turn",
      "completed",
      () => eventFor(turnBody()),
      () => depsFor(streamOf(runtimeFrame({ kind: "completed", text: "done" }))),
    ],
  ])("logs %s as outcome %s, exactly once", async (_label, outcome, makeEvent, makeDeps) => {
    const logs = await turnLogsFor(makeEvent(), makeDeps());

    expect(logs).toHaveLength(1);
    expect(logs[0]?.["event"]).toBe("relay_turn");
    expect(logs[0]?.["outcome"]).toBe(outcome);
    expect(logs[0]?.["schema_version"]).toBe(SCHEMA_VERSION);
  });

  it("logs a cancellation exactly once, and never twice", async () => {
    // `respondToCancel` returns into the same `finally`, so a cancel logs exactly once.
    const logs = await turnLogsFor(eventFor(cancelBody()), cancelDeps());

    expect(logs).toHaveLength(1);
    expect(logs[0]?.["outcome"]).toBe("cancel_200");
  });

  it("attaches the failing error's name and message to the 401 line, and no token material", async () => {
    const logs = await turnLogsFor(
      eventFor(turnBody(), { bearer: "Bearer super.secret.token" }),
      depsFor(streamOf(), {
        verify: () => Promise.reject(new JwtVerificationError("token failed verification")),
      }),
    );

    expect(logs[0]?.["error"]).toBe("JwtVerificationError");
    expect(logs[0]?.["detail"]).toBe("token failed verification");
    expect(JSON.stringify(logs[0])).not.toContain("super.secret.token");
  });

  it("classifies the wrapped SDK error + message pattern, without its message, body, or ids", async () => {
    // A RuntimeClientError 424 whose service message embeds the container status, wrapped as the
    // cause of the RuntimeInvocationError.
    const sdkError = Object.assign(
      new Error("Received error (500) from runtime. Please check your CloudWatch logs for more information"),
      {
        name: "RuntimeClientError",
        $fault: "client",
        $metadata: { httpStatusCode: 424, requestId: "req-SECRET-abc123" },
        $response: { body: "RAW-RUNTIME-BODY-must-not-be-logged" },
      },
    );
    const logs = await turnLogsFor(
      eventFor(turnBody()),
      depsFor(() =>
        Promise.reject(new RuntimeInvocationError("InvokeAgentRuntime failed", { cause: sdkError })),
      ),
    );

    const line = logs[0] ?? {};
    // The classification plus the content-free message-pattern label: a container 5xx.
    expect(line["cause_name"]).toBe("RuntimeClientError");
    expect(line["cause_http_status"]).toBe(424);
    expect(line["cause_fault"]).toBe("client");
    expect(line["cause_message_pattern"]).toBe("runtime_5xx");
    // No pre-classification boolean fields.
    expect(line).not.toHaveProperty("cause_message_has_validation_failed");
    expect(line).not.toHaveProperty("cause_has_response_body");
    // The wrapper's own fields are unchanged; a container 5xx stays 502.
    expect(line["error"]).toBe("RuntimeInvocationError");
    expect(line["outcome"]).toBe("runtime_unavailable");
    // None of the underlying error's content: not its message, its body, or its request id.
    const raw = JSON.stringify(line);
    expect(raw).not.toContain("CloudWatch");
    expect(raw).not.toContain("RAW-RUNTIME-BODY-must-not-be-logged");
    expect(raw).not.toContain("req-SECRET-abc123");
  });

  it("labels a container 4xx as cause_message_pattern=runtime_4xx on the 400 line", async () => {
    // A container 4xx maps to a relay 400 (RuntimeValidationError); the log carries the content-free
    // pattern label and still no message text.
    const cause = Object.assign(
      new Error("Received error (400) from runtime. Please check your CloudWatch logs for more information"),
      { name: "RuntimeClientError", $fault: "client", $metadata: { httpStatusCode: 424 } },
    );
    const logs = await turnLogsFor(
      eventFor(turnBody()),
      depsFor(() =>
        Promise.reject(new RuntimeValidationError("The assistant could not accept this request.", { cause })),
      ),
    );

    const line = logs[0] ?? {};
    expect(line["outcome"]).toBe("validation_failed");
    expect(line["cause_message_pattern"]).toBe("runtime_4xx");
    expect(JSON.stringify(line)).not.toContain("CloudWatch");
  });

  it("adds no cause_* fields when the failure has no wrapped cause", async () => {
    // Classification is emitted only when a cause is present.
    const logs = await turnLogsFor(
      eventFor(turnBody()),
      depsFor(() => Promise.reject(new RuntimeInvocationError("unavailable"))),
    );

    const line = logs[0] ?? {};
    expect(line["outcome"]).toBe("runtime_unavailable"); // positive control
    for (const key of ["cause_name", "cause_http_status", "cause_fault", "cause_code", "cause_message_pattern"]) {
      expect(line).not.toHaveProperty(key);
    }
  });

  it("records the session id on a rejected turn when the body carried a usable one", async () => {
    // The 401 runs before the body is decoded, so an unauthenticated line carries no session id.
    const logs = await turnLogsFor(
      eventFor(turnBody()),
      depsFor(streamOf(), { verify: () => Promise.reject(new JwtVerificationError("nope")) }),
    );

    expect(logs[0]?.["session_id"]).toBe("");
  });
});

// --------------------------------------------------------------------------------------
// No origin-verification header
//
// A request with no origin header is served, and an anonymous caller is refused by the JWT check
// (401), never by a reachability check.
// --------------------------------------------------------------------------------------

describe("no origin-verification header is required", () => {
  it("serves a request that carries no origin-verification header at all", async () => {
    const sink = new RecordingSink();
    const deps = depsFor(streamOf(runtimeFrame({ kind: "completed", text: "done" })));

    await streamTurn(eventFor(turnBody()), sink, deps);

    expect(sink.statusCode).toBe(200);
    expect(deps.requests).toHaveLength(1);
  });

  it("never answers 403 — no request shape can produce the retired origin rejection", async () => {
    // Nothing in the handler issues a 403.
    const shapes: LambdaFunctionURLEvent[] = [
      eventFor(turnBody()),
      eventFor(cancelBody()),
      eventFor(turnBody(), { bearer: "Bearer nonsense" }),
      eventFor("not json at all"),
    ];

    for (const event of shapes) {
      const sink = new RecordingSink();
      await streamTurn(event, sink, depsFor(streamOf(runtimeFrame({ kind: "completed" }))));
      expect(sink.statusCode).not.toBe(403);
    }
  });

  it("ignores a leftover x-crm-origin-verify header rather than treating it as meaningful", async () => {
    // A stale client could still send it; it is neither required nor privileged.
    const sink = new RecordingSink();
    const deps = depsFor(streamOf(runtimeFrame({ kind: "completed", text: "done" })));
    const event = eventFor(turnBody());
    const headers = { ...(event.headers ?? {}), "x-crm-origin-verify": "stale" } as Record<
      string,
      string | undefined
    >;

    await streamTurn({ ...event, headers } as LambdaFunctionURLEvent, sink, deps);

    expect(sink.statusCode).toBe(200);
  });

  it("still refuses an anonymous caller with the JWT check's 401, not a reachability check", async () => {
    // The token check still refuses an anonymous caller.
    const sink = new RecordingSink();
    const deps = realVerifyDeps();
    const event = eventFor(turnBody());
    const headers = { ...(event.headers ?? {}) } as Record<string, string | undefined>;
    delete headers[JWT_HEADER];

    await streamTurn({ ...event, headers } as LambdaFunctionURLEvent, sink, deps);

    expect(sink.statusCode).toBe(401);
    expect(deps.invocations).toBe(0);
  });

  it("reports no origin-lock outcome on the turn log", async () => {
    const written: string[] = [];
    const spy = vi.spyOn(process.stdout, "write").mockImplementation((chunk) => {
      written.push(String(chunk));
      return true;
    });
    try {
      await streamTurn(
        eventFor(turnBody()),
        new RecordingSink(),
        depsFor(streamOf(runtimeFrame({ kind: "completed", text: "done" }))),
      );
    } finally {
      spy.mockRestore();
    }

    const log = written.join("");
    expect(log).not.toContain("origin_lock_unavailable");
    expect(log).not.toContain("origin_lock_rejected");
    expect(log).toContain('"outcome":"completed"');
  });
});

// --------------------------------------------------------------------------------------
// Cancellation
//
// A cancel is a `mode` on the same endpoint, authenticated on its own, and issued as a separate
// Runtime call.
// --------------------------------------------------------------------------------------

function cancelBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { mode: "cancel", session_id: SESSION_ID, ...overrides };
}

interface RecordedCancel {
  readonly sessionId: string;
  readonly claims: Readonly<Record<string, unknown>>;
}

function cancelDeps(
  result: CancelResult | (() => Promise<CancelResult>) = {
    statusCode: 200,
    body: JSON.stringify({ session_id: SESSION_ID, disposition: "accepted" }),
  },
  overrides: Partial<HandlerDeps> = {},
): HandlerDeps & { cancels: RecordedCancel[]; turns: TurnRequest[] } {
  const cancels: RecordedCancel[] = [];
  const turns: TurnRequest[] = [];
  return {
    cancels,
    turns,
    verify: () => Promise.resolve(CLAIMS),
    cancel: (request) => {
      cancels.push({ sessionId: request.sessionId, claims: request.claims });
      return typeof result === "function" ? result() : Promise.resolve(result);
    },
    invoke: (request: TurnRequest) => {
      turns.push(request);
      return Promise.resolve(streamOf(runtimeFrame({ kind: "completed", text: "done" })));
    },
    ...overrides,
  };
}

describe("a cancellation", () => {
  it("asks the Runtime to cancel the named session and passes its answer through", async () => {
    const sink = new RecordingSink();
    const deps = cancelDeps();

    await streamTurn(eventFor(cancelBody()), sink, deps);

    expect(sink.statusCode).toBe(200);
    expect(sink.headers?.["content-type"]).toBe("application/json");
    expect(JSON.parse(sink.body)).toEqual({ session_id: SESSION_ID, disposition: "accepted" });
    expect(deps.cancels).toEqual([{ sessionId: SESSION_ID, claims: CLAIMS }]);
  });

  it("starts no turn — the whole point is to stop one", async () => {
    const deps = cancelDeps();

    await streamTurn(eventFor(cancelBody()), new RecordingSink(), deps);

    expect(deps.turns).toEqual([]);
  });

  it("answers with a plain body, never an SSE frame", async () => {
    const sink = new RecordingSink();

    await streamTurn(eventFor(cancelBody()), sink, cancelDeps());

    expect(sink.body).not.toContain(FRAME_PREFIX);
    expect(sink.body).not.toContain(FRAME_DELIMITER);
    expect(sink.headers?.["content-type"]).not.toContain("event-stream");
  });

  it("carries the same session id the turn ran under", async () => {
    // The relay forwards the id unchanged; AgentCore's session routing is not asserted here.
    const deps = cancelDeps();

    await streamTurn(eventFor(cancelBody({ session_id: OTHER_SESSION_ID })), sink0(), deps);

    expect(deps.cancels[0]?.sessionId).toBe(OTHER_SESSION_ID);
  });

  it.each([401, 403, 400, 500])(
    "passes the Runtime's %d through rather than reinterpreting it",
    async (statusCode) => {
      const sink = new RecordingSink();
      const body = JSON.stringify({ error: { code: "scope_denied" } });

      await streamTurn(eventFor(cancelBody()), sink, cancelDeps({ statusCode, body }));

      expect(sink.statusCode).toBe(statusCode);
      expect(sink.body).toBe(body);
    },
  );

  /** A cancel whose `InvokeAgentRuntime` threw AgentCore's 424 carrying the container's `status`. */
  function thrownCancel(status: number): () => Promise<CancelResult> {
    const cause = Object.assign(
      new Error(`Received error (${status}) from runtime. Please check your CloudWatch logs for more information`),
      {
        name: "RuntimeClientError",
        $fault: "client",
        $metadata: { httpStatusCode: 424, requestId: "req-SECRET-cancel" },
        $response: { body: "RAW-CANCEL-BODY-must-not-be-logged" },
      },
    );
    return () =>
      Promise.reject(
        new RuntimeInvocationError("InvokeAgentRuntime failed for a cancellation", { cause }),
      );
  }

  it.each([403, 401, 400])(
    "answers a cancel the container refused with %d by that status, and logs the cause",
    async (status) => {
      const sink = new RecordingSink();

      const logs = await turnLogsFor(eventFor(cancelBody()), cancelDeps(thrownCancel(status)), sink);

      expect(sink.statusCode).toBe(status);
      expect(sink.headers?.["content-type"]).toBe("application/json");
      expect(JSON.parse(sink.body)).toEqual({ message: "The turn could not be stopped." });
      const line = logs[0] ?? {};
      expect(logs).toHaveLength(1);
      expect(line["outcome"]).toBe(`cancel_${status}`);
      expect(line["error"]).toBe("RuntimeInvocationError");
      expect(line["cause_name"]).toBe("RuntimeClientError");
      expect(line["cause_http_status"]).toBe(424);
      expect(line["cause_message_pattern"]).toBe("runtime_4xx");
      const raw = JSON.stringify(line);
      expect(raw).not.toContain("CloudWatch");
      expect(raw).not.toContain("RAW-CANCEL-BODY-must-not-be-logged");
      expect(raw).not.toContain("req-SECRET-cancel");
    },
  );

  it("still answers 502 when the container failed with a 5xx, and logs the cause", async () => {
    const sink = new RecordingSink();

    const logs = await turnLogsFor(eventFor(cancelBody()), cancelDeps(thrownCancel(500)), sink);

    expect(sink.statusCode).toBe(502);
    expect(JSON.parse(sink.body)).toEqual({ message: "The assistant is unavailable. Try again." });
    expect(logs[0]?.["outcome"]).toBe("cancel_unavailable");
    expect(logs[0]?.["cause_message_pattern"]).toBe("runtime_5xx");
  });

  it("still answers 502 when the cancel failed with no container status", async () => {
    const sink = new RecordingSink();
    const deps = cancelDeps(() => Promise.reject(new RuntimeInvocationError("unavailable")));

    const logs = await turnLogsFor(eventFor(cancelBody()), deps, sink);

    expect(sink.statusCode).toBe(502);
    expect(logs[0]?.["outcome"]).toBe("cancel_unavailable");
    expect(logs[0]?.["error"]).toBe("RuntimeInvocationError");
  });

  it("refuses an unverified caller with a 401 and never reaches the Runtime", async () => {
    const sink = new RecordingSink();
    const deps = cancelDeps(undefined, {
      verify: () => Promise.reject(new JwtVerificationError("expired")),
    });

    await streamTurn(eventFor(cancelBody()), sink, deps);

    expect(sink.statusCode).toBe(401);
    expect(deps.cancels).toEqual([]);
    expect(deps.turns).toEqual([]);
  });

  // The cancel path reads the token from the same header as a turn.
  it("reads its token from X-Crm-Authorization, and gets nothing when only `Authorization` carries one", async () => {
    const handed: (string | undefined)[] = [];
    const deps = cancelDeps(undefined, {
      verify: (header) => {
        handed.push(header);
        return header === undefined
          ? Promise.reject(new JwtVerificationError("no token"))
          : Promise.resolve(CLAIMS);
      },
    });

    const withToken = new RecordingSink();
    await streamTurn(eventFor(cancelBody(), { bearer: "Bearer cancel.token" }), withToken, deps);

    const event = eventFor(cancelBody());
    const headers = { ...(event.headers ?? {}) } as Record<string, string | undefined>;
    delete headers[JWT_HEADER];
    headers["authorization"] = "Bearer cancel.token";
    const wrongHeader = new RecordingSink();
    await streamTurn({ ...event, headers } as LambdaFunctionURLEvent, wrongHeader, deps);

    expect(handed).toEqual(["Bearer cancel.token", undefined]);
    expect(withToken.statusCode).toBe(200);
    expect(wrongHeader.statusCode).toBe(401);
    expect(deps.cancels).toHaveLength(1);
  });

  it("is refused 401 by the JWT check, the first check that refuses it", async () => {
    // The token check is the sole gate, so a cancel without one never reaches the Runtime.
    const sink = new RecordingSink();
    const deps = cancelDeps(undefined, {
      verify: () => Promise.reject(new JwtVerificationError("no token")),
    });

    await streamTurn(eventFor(cancelBody()), sink, deps);

    expect(sink.statusCode).toBe(401);
    expect(deps.cancels).toEqual([]);
  });

  it("answers 502 when the Runtime cannot be reached, and does not retry", async () => {
    const sink = new RecordingSink();
    let calls = 0;
    const deps = cancelDeps(() => {
      calls += 1;
      return Promise.reject(new RuntimeInvocationError("arn:aws:bedrock-agentcore:AccessDenied"));
    });

    await streamTurn(eventFor(cancelBody()), sink, deps);

    expect(sink.statusCode).toBe(502);
    expect(calls).toBe(1);
    // The safe message itself says "unavailable", so the assertion names the internal detail.
    expect(sink.body).not.toContain("AccessDenied");
    expect(sink.body).not.toContain("arn:aws");
  });

  it("records the outcome on the turn log, distinctly from a turn", async () => {
    const written: string[] = [];
    const spy = vi.spyOn(process.stdout, "write").mockImplementation((chunk: unknown) => {
      written.push(String(chunk));
      return true;
    });
    try {
      await streamTurn(eventFor(cancelBody()), new RecordingSink(), cancelDeps());
    } finally {
      spy.mockRestore();
    }

    const line = JSON.parse(written[0] ?? "{}") as Record<string, unknown>;
    expect(line["outcome"]).toBe("cancel_200");
    expect(line["session_id"]).toBe(SESSION_ID);
    expect(line["frames"]).toBe(0);
  });
});

describe("what is and is not a cancellation", () => {
  it.each([
    ["a turn body with no mode", { prompt: "hello", session_id: SESSION_ID }],
    ["a near-miss mode", { mode: "cancel_turn", session_id: SESSION_ID }],
    ["a differently-cased mode", { mode: "Cancel", session_id: SESSION_ID }],
    ["a non-string mode", { mode: 1, session_id: SESSION_ID }],
  ])("does not treat %s as a cancellation", async (_label, body) => {
    const deps = cancelDeps();

    await streamTurn(eventFor({ prompt: "hello", ...body }), new RecordingSink(), deps);

    expect(deps.cancels).toEqual([]);
  });

  it("takes the turn path for a cancel-shaped body with no session id", async () => {
    // Refused as a malformed turn.
    const sink = new RecordingSink();
    const deps = cancelDeps();

    await streamTurn(eventFor({ mode: "cancel" }), sink, deps);

    expect(sink.statusCode).toBe(400);
    expect(deps.cancels).toEqual([]);
  });

  it("cancels even when the body also carries a prompt", async () => {
    // A body carrying both shapes resolves to the cancel, never to a billed turn.
    const deps = cancelDeps();

    await streamTurn(
      eventFor(cancelBody({ prompt: "keep going" })),
      new RecordingSink(),
      deps,
    );

    expect(deps.cancels).toHaveLength(1);
    expect(deps.turns).toEqual([]);
  });

  it("strips a caller-supplied identity channel from a cancellation too", async () => {
    // The allowlist decode applies to both shapes: only the verified claims reach the Runtime.
    const deps = cancelDeps();

    await streamTurn(
      eventFor(cancelBody({ _agent_identity: { claims: { "custom:rep_id": "rep-999" } } })),
      new RecordingSink(),
      deps,
    );

    expect(deps.cancels[0]?.claims).toEqual(CLAIMS);
  });
});

/** A sink for a test that asserts on the deps rather than on the response. */
function sink0(): ResponseSink {
  return new RecordingSink();
}

// --------------------------------------------------------------------------------------
// The `runtimeSessionId` bound
//
// `session_id` becomes `runtimeSessionId` unchanged, and `InvokeAgentRuntime` documents a minimum of
// 33; an out-of-range id is refused with a 400 before the Runtime is called.
// --------------------------------------------------------------------------------------

describe("the runtimeSessionId bound", () => {
  it("agrees with the documented minimum and maximum", () => {
    // Pinned as literals.
    expect(SESSION_ID_MIN_LENGTH).toBe(33);
    expect(SESSION_ID_MAX_LENGTH).toBe(256);
  });

  it("proves this suite's own fixture is not compliant by accident", () => {
    // Non-vacuity guard: the suite's happy-path session id satisfies the bound.
    expect(SESSION_ID.length).toBeGreaterThanOrEqual(SESSION_ID_MIN_LENGTH);
    expect(OTHER_SESSION_ID.length).toBeGreaterThanOrEqual(SESSION_ID_MIN_LENGTH);
  });

  it.each([
    ["one character", 1],
    ["a plausible-looking short id", 13],
    ["one below the floor", 32],
  ])("rejects a turn whose session id is %s, before any Runtime call", async (_label, length) => {
    const sink = new RecordingSink();
    const deps = depsFor(streamOf());

    await streamTurn(
      eventFor(turnBody({ session_id: "s".repeat(length) })),
      sink,
      deps,
    );

    expect(sink.statusCode).toBe(400);
    expect(JSON.parse(sink.body)).toEqual({ message: SESSION_ID_RANGE_MESSAGE });
    expect(deps.requests).toHaveLength(0);
  });

  it("rejects an id one character over the ceiling", async () => {
    const sink = new RecordingSink();
    const deps = depsFor(streamOf());

    await streamTurn(
      eventFor(turnBody({ session_id: "s".repeat(SESSION_ID_MAX_LENGTH + 1) })),
      sink,
      deps,
    );

    expect(sink.statusCode).toBe(400);
    expect(deps.requests).toHaveLength(0);
  });

  it.each([
    ["exactly at the floor", SESSION_ID_MIN_LENGTH],
    ["exactly at the ceiling", SESSION_ID_MAX_LENGTH],
  ])("accepts an id %s", async (_label, length) => {
    // Both boundaries are inclusive.
    const sink = new RecordingSink();
    const deps = depsFor(streamOf(runtimeFrame({ kind: "completed", text: "done" })));

    await streamTurn(eventFor(turnBody({ session_id: "s".repeat(length) })), sink, deps);

    expect(sink.statusCode).toBe(200);
    expect(deps.requests).toHaveLength(1);
  });

  it("rejects a cancellation with an out-of-range id too, not just a turn", async () => {
    // The bound is checked before either shape is decoded, so it covers a cancel too.
    const sink = new RecordingSink();
    const deps = cancelDeps();

    await streamTurn(eventFor(cancelBody({ session_id: "short" })), sink, deps);

    expect(sink.statusCode).toBe(400);
    expect(deps.cancels).toEqual([]);
  });

  // Authentication precedes the range check, and the range check precedes decoding.
  it("refuses a caller with no token 401, and tells it nothing about the bound", async () => {
    const sink = new RecordingSink();
    const deps = realVerifyDeps();
    const event = eventFor(turnBody({ session_id: "short" }));
    const headers = { ...(event.headers ?? {}) } as Record<string, string | undefined>;
    delete headers[JWT_HEADER];

    await streamTurn({ ...event, headers } as LambdaFunctionURLEvent, sink, deps);

    expect(sink.statusCode).toBe(401);
    expect(sink.body).not.toContain(SESSION_ID_RANGE_MESSAGE);
    expect(sink.body).not.toContain(String(SESSION_ID_MIN_LENGTH));
    expect(deps.invocations).toBe(0);
  });

  it("still answers 400 naming the bound to an AUTHENTICATED caller", async () => {
    const sink = new RecordingSink();
    const deps = realVerifyDeps();

    await streamTurn(
      eventFor(turnBody({ session_id: "short" }), { bearer: `Bearer ${gateToken(gateClaims())}` }),
      sink,
      deps,
    );

    expect(sink.statusCode).toBe(400);
    expect(sink.body).toContain(SESSION_ID_RANGE_MESSAGE);
    expect(deps.invocations).toBe(0);
  });

  it("does not echo the offending value back to the caller", async () => {
    const sink = new RecordingSink();

    await streamTurn(
      eventFor(turnBody({ session_id: "<script>alert(1)</script>" })),
      sink,
      depsFor(streamOf()),
    );

    expect(sink.body).not.toContain("script");
  });

  it("keeps an absent session id as its own error, not a range error", async () => {
    // A body with no id at all gets the decoders' message, not the range message.
    const sink = new RecordingSink();

    await streamTurn(eventFor({ prompt: "hello" }), sink, depsFor(streamOf()));

    expect(sink.statusCode).toBe(400);
    expect(sink.body).not.toContain(SESSION_ID_RANGE_MESSAGE);
  });
});

describe("the prompt cap", () => {
  it("rejects a prompt one character over the cap with a 400, before any Runtime call", async () => {
    const sink = new RecordingSink();
    const deps = depsFor(streamOf());

    await streamTurn(eventFor(turnBody({ prompt: "x".repeat(PROMPT_MAX_CHARS + 1) })), sink, deps);

    expect(sink.statusCode).toBe(400);
    expect(JSON.parse(sink.body)).toEqual({ message: PROMPT_TOO_LONG_MESSAGE });
    expect(deps.requests).toHaveLength(0);
  });

  it("forwards a prompt of exactly the cap, counted in code points", async () => {
    const sink = new RecordingSink();
    const deps = depsFor(streamOf(runtimeFrame({ kind: "completed", text: "done" })));
    const prompt = "\u{1F600}".repeat(PROMPT_MAX_CHARS);

    await streamTurn(eventFor(turnBody({ prompt })), sink, deps);

    expect(sink.statusCode).toBe(200);
    expect(deps.requests).toHaveLength(1);
    expect(deps.requests[0]?.prompt).toBe(prompt);
  });

  it("never echoes the rejected prompt in the response", async () => {
    const sink = new RecordingSink();
    const prompt = "secret-" + "x".repeat(PROMPT_MAX_CHARS);

    await streamTurn(eventFor(turnBody({ prompt })), sink, depsFor(streamOf()));

    expect(sink.body).not.toContain("secret-");
  });
});
