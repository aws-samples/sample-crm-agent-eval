// SPDX-License-Identifier: MIT
/**
 * Tests for the relay's SigV4 `InvokeAgentRuntime` call.
 *
 * Commands are built with the real `InvokeAgentRuntimeCommand`; only the transport is substituted,
 * by a client whose `send` records the command and returns a controllable body.
 */
import { describe, expect, it } from "vitest";
import { InvokeAgentRuntimeCommand } from "@aws-sdk/client-bedrock-agentcore";

import { GENERIC_VALIDATION_MESSAGE } from "../src/errorClassification.js";
import {
  DEFAULT_QUALIFIER,
  ENV_REGION,
  ENV_RUNTIME_ARN,
  ENV_RUNTIME_QUALIFIER,
  RuntimeInvocationError,
  RuntimeValidationError,
  buildInvokeCommand,
  buildRuntimePayload,
  invokeRuntimeStream,
  loadRuntimeConfig,
  resetSharedRuntimeClient,
  type RuntimeClient,
  type RuntimeConfig,
  type RuntimeResponse,
  type TurnRequest,
} from "../src/runtime.js";

const RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/crm-relay";
const config: RuntimeConfig = {
  runtimeArn: RUNTIME_ARN,
  region: "us-east-1",
  qualifier: DEFAULT_QUALIFIER,
};

const CLAIMS = {
  sub: "11111111-2222-3333-4444-5555dddd6666",
  "custom:rep_id": "rep-042",
  "custom:tenant": "demo",
  "custom:book": "acc-1,acc-2",
  "custom:full_visibility": "false",
} as const;

function turn(overrides: Partial<TurnRequest> = {}): TurnRequest {
  return {
    sessionId: "session-11111111-2222-3333-4444-5555dddd6666",
    prompt: "which of my accounts slipped this quarter?",
    claims: CLAIMS,
    ...overrides,
  };
}

/** A client that records every command and answers with a body the test controls. */
class RecordingClient implements RuntimeClient {
  readonly commands: InvokeAgentRuntimeCommand[] = [];

  constructor(private readonly answer: () => Promise<RuntimeResponse>) {}

  send(command: InvokeAgentRuntimeCommand): Promise<RuntimeResponse> {
    this.commands.push(command);
    return this.answer();
  }
}

function frames(...lines: string[]): AsyncIterable<Uint8Array> {
  const encoder = new TextEncoder();
  return {
    async *[Symbol.asyncIterator]() {
      for (const line of lines) {
        yield encoder.encode(line);
      }
    },
  };
}

function decodePayload(command: InvokeAgentRuntimeCommand): Record<string, unknown> {
  const raw = command.input.payload;
  if (!(raw instanceof Uint8Array)) {
    throw new Error(`payload is ${typeof raw}, not the UTF-8 bytes the blob member takes`);
  }
  return JSON.parse(new TextDecoder().decode(raw)) as Record<string, unknown>;
}

describe("the invocation payload carries identity through the retained agent channel", () => {
  it("places the verified claims under _agent_identity, verbatim", () => {
    const payload = buildRuntimePayload(turn());
    expect(payload["_agent_identity"]).toEqual({
      claims: CLAIMS,
      session_id: "session-11111111-2222-3333-4444-5555dddd6666",
    });
  });

  it("carries the prompt and session id the Runtime's decode_invocation reads", () => {
    const payload = buildRuntimePayload(turn());
    expect(payload["prompt"]).toBe("which of my accounts slipped this quarter?");
    expect(payload["session_id"]).toBe("session-11111111-2222-3333-4444-5555dddd6666");
  });

  it("constructs no identity of its own: no rep_id, tenant, book, or scope at any level", () => {
    const payload = buildRuntimePayload(turn({ pageContext: { kind: "opaque", reason: "test" } }));
    const flat = JSON.stringify(payload);
    expect(Object.keys(payload)).not.toContain("identity");
    expect(Object.keys(payload)).not.toContain("requestContext");
    expect(flat).not.toContain('"rep_id"');
    expect(flat).not.toContain('"principal_type"');
    expect(flat).not.toContain('"full_visibility":');
  });

  it("omits optional members rather than sending them as null", () => {
    const payload = buildRuntimePayload(turn());
    expect(Object.keys(payload)).not.toContain("page_context");
    expect(Object.keys(payload)).not.toContain("tuning");
    expect(Object.keys(payload)).not.toContain("resume_from");
  });

  it("forwards page context, tuning, and a resume point when the caller supplies them", () => {
    const payload = buildRuntimePayload(
      turn({
        pageContext: { kind: "list", entity: "opportunities" },
        tuning: { effort: "low" },
        resumeFrom: 7,
      }),
    );
    expect(payload["page_context"]).toEqual({ kind: "list", entity: "opportunities" });
    expect(payload["tuning"]).toEqual({ effort: "low" });
    expect(payload["resume_from"]).toBe(7);
  });
});

describe("the command matches the SDK's own request model", () => {
  it("targets the configured ARN and qualifier and asks for an event stream", () => {
    const input = buildInvokeCommand(turn(), config).input;
    expect(input.agentRuntimeArn).toBe(RUNTIME_ARN);
    expect(input.qualifier).toBe(DEFAULT_QUALIFIER);
    expect(input.contentType).toBe("application/json");
    expect(input.accept).toBe("text/event-stream");
    expect(input.runtimeSessionId).toBe("session-11111111-2222-3333-4444-5555dddd6666");
  });

  it("encodes the payload as UTF-8 bytes, which is what the blob member takes", () => {
    const command = buildInvokeCommand(turn({ prompt: "café ☕" }), config);
    expect(decodePayload(command)["prompt"]).toBe("café ☕");
  });

  it("never sets runtimeUserId, so one action on one ARN suffices", () => {
    const input = buildInvokeCommand(turn(), config).input;
    expect(input.runtimeUserId).toBeUndefined();
    expect(Object.keys(input)).not.toContain("runtimeUserId");
  });

  it("sets no MCP member: the Runtime path is not the Gateway path", () => {
    const input = buildInvokeCommand(turn(), config).input;
    expect(input.mcpSessionId).toBeUndefined();
    expect(input.mcpMethod).toBeUndefined();
    expect(input.mcpProtocolVersion).toBeUndefined();
  });
});

describe("the response body is streamed, not buffered", () => {
  it("returns the body itself so the caller writes each chunk onward", async () => {
    const client = new RecordingClient(() =>
      Promise.resolve({ statusCode: 200, response: frames("data: {}\n\n") }),
    );

    const body = await invokeRuntimeStream(turn(), { client, config });
    const seen: string[] = [];
    for await (const chunk of body) {
      seen.push(new TextDecoder().decode(chunk));
    }
    expect(seen).toEqual(["data: {}\n\n"]);
  });

  it("yields the first chunk before the Runtime has produced the last one", async () => {
    let releaseSecond: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      releaseSecond = resolve;
    });
    const encoder = new TextEncoder();
    const body: AsyncIterable<Uint8Array> = {
      async *[Symbol.asyncIterator]() {
        yield encoder.encode("data: {\"kind\":\"tool_call\"}\n\n");
        await gate;
        yield encoder.encode("data: {\"kind\":\"completed\"}\n\n");
      },
    };
    const client = new RecordingClient(() => Promise.resolve({ statusCode: 200, response: body }));

    const iterator = (await invokeRuntimeStream(turn(), { client, config }))[
      Symbol.asyncIterator
    ]();
    const first = await iterator.next();

    expect(new TextDecoder().decode(first.value)).toContain("tool_call");
    releaseSecond();
    const second = await iterator.next();
    expect(new TextDecoder().decode(second.value)).toContain("completed");
    await expect(iterator.next()).resolves.toMatchObject({ done: true });
  });

  it("does not read the body itself, so nothing is consumed before the caller sees it", async () => {
    let reads = 0;
    const encoder = new TextEncoder();
    const body: AsyncIterable<Uint8Array> = {
      async *[Symbol.asyncIterator]() {
        reads += 1;
        yield encoder.encode("data: {}\n\n");
      },
    };
    const client = new RecordingClient(() => Promise.resolve({ statusCode: 200, response: body }));

    await invokeRuntimeStream(turn(), { client, config });
    expect(reads).toBe(0);
  });
});

describe("failures are reported, never retried and never silent", () => {
  it("wraps a send failure and issues exactly one call", async () => {
    const client = new RecordingClient(() => Promise.reject(new Error("throttled")));

    await expect(invokeRuntimeStream(turn(), { client, config })).rejects.toBeInstanceOf(
      RuntimeInvocationError,
    );
    expect(client.commands).toHaveLength(1);
  });

  it("keeps the underlying cause on the wrapped error", async () => {
    const cause = new Error("throttled");
    const client = new RecordingClient(() => Promise.reject(cause));

    const error = await invokeRuntimeStream(turn(), { client, config }).catch(
      (thrown: unknown) => thrown,
    );
    expect((error as RuntimeInvocationError).cause).toBe(cause);
  });

  it("rejects an answer with no body rather than returning an empty stream", async () => {
    const client = new RecordingClient(() => Promise.resolve({ statusCode: 200 }));

    await expect(invokeRuntimeStream(turn(), { client, config })).rejects.toThrow(
      /no response body/,
    );
  });

  it.each([400, 403, 424, 500])("rejects a non-2xx status (%i)", async (statusCode) => {
    const client = new RecordingClient(() =>
      Promise.resolve({ statusCode, response: frames("data: {}\n\n") }),
    );

    await expect(invokeRuntimeStream(turn(), { client, config })).rejects.toThrow(
      new RegExp(`status ${statusCode}`),
    );
  });

  it("accepts an answer that reports no status at all", async () => {
    const client = new RecordingClient(() => Promise.resolve({ response: frames("data: {}\n\n") }));
    await expect(invokeRuntimeStream(turn(), { client, config })).resolves.toBeTruthy();
  });
});

describe("a thrown RuntimeClientError is mapped by the container status it embeds", () => {
  // AgentCore throws a RuntimeClientError (HTTP 424) whose message embeds the container status.
  const received = (status: number): string =>
    `Received error (${status}) from runtime. Please check your CloudWatch logs for more information`;

  function runtimeClientError(message: string): Error {
    return Object.assign(new Error(message), {
      name: "RuntimeClientError",
      $fault: "client",
      $metadata: { httpStatusCode: 424 },
    });
  }

  it("maps a container 4xx (e.g. 400 validation) to RuntimeValidationError with the generic message", async () => {
    const client = new RecordingClient(() => Promise.reject(runtimeClientError(received(400))));

    const error = await invokeRuntimeStream(turn(), { client, config }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(RuntimeValidationError);
    expect((error as RuntimeValidationError).userMessage).toBe(GENERIC_VALIDATION_MESSAGE);
  });

  it("maps any client-side 4xx (422) to RuntimeValidationError too", async () => {
    const client = new RecordingClient(() => Promise.reject(runtimeClientError(received(422))));

    await expect(invokeRuntimeStream(turn(), { client, config })).rejects.toBeInstanceOf(
      RuntimeValidationError,
    );
  });

  it("keeps a container 5xx (a crash) on the 502 path — not every 424 becomes a 400", async () => {
    const client = new RecordingClient(() => Promise.reject(runtimeClientError(received(500))));

    const error = await invokeRuntimeStream(turn(), { client, config }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(RuntimeInvocationError);
    expect(error).not.toBeInstanceOf(RuntimeValidationError);
  });

  it("keeps a RuntimeClientError whose message is not the documented pattern on the 502 path", async () => {
    // A number merely appearing in some other message must not be read as a container status.
    const client = new RecordingClient(() =>
      Promise.reject(runtimeClientError("some other client error mentioning 400")),
    );

    await expect(invokeRuntimeStream(turn(), { client, config })).rejects.toBeInstanceOf(
      RuntimeInvocationError,
    );
  });

  it("keeps a thrown error with no status/message (a network failure) on the 502 path", async () => {
    const client = new RecordingClient(() => Promise.reject(new Error("socket hang up")));

    const error = await invokeRuntimeStream(turn(), { client, config }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(RuntimeInvocationError);
    expect(error).not.toBeInstanceOf(RuntimeValidationError);
  });
});

describe("configuration fails closed", () => {
  const complete = () =>
    ({
      [ENV_RUNTIME_ARN]: RUNTIME_ARN,
      [ENV_REGION]: "us-east-1",
    }) as NodeJS.ProcessEnv;

  it("reads the ARN and region and defaults the qualifier", () => {
    const loaded = loadRuntimeConfig(complete());
    expect(loaded.runtimeArn).toBe(RUNTIME_ARN);
    expect(loaded.region).toBe("us-east-1");
    expect(loaded.qualifier).toBe(DEFAULT_QUALIFIER);
  });

  it("honours an explicit qualifier", () => {
    const env = complete();
    env[ENV_RUNTIME_QUALIFIER] = "canary";
    expect(loadRuntimeConfig(env).qualifier).toBe("canary");
  });

  it.each([ENV_RUNTIME_ARN, ENV_REGION])("throws when %s is absent", (name) => {
    const env = complete();
    delete env[name];
    expect(() => loadRuntimeConfig(env)).toThrow(RuntimeInvocationError);
  });

  it.each([ENV_RUNTIME_ARN, ENV_REGION])("throws when %s is blank", (name) => {
    const env = complete();
    env[name] = "  ";
    expect(() => loadRuntimeConfig(env)).toThrow(RuntimeInvocationError);
  });

  it("treats a blank qualifier as absent rather than sending an empty endpoint name", () => {
    const env = complete();
    env[ENV_RUNTIME_QUALIFIER] = "   ";
    expect(loadRuntimeConfig(env).qualifier).toBe(DEFAULT_QUALIFIER);
    resetSharedRuntimeClient();
  });
});
