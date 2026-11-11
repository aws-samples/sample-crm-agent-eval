// SPDX-License-Identifier: MIT
/**
 * The SigV4 `InvokeAgentRuntime` call through `@aws-sdk/client-bedrock-agentcore`, returning the
 * response body as an async iterable.
 *
 * - `runtimeUserId` is never set; identity travels in the payload under `_agent_identity`, the key
 *   `crm.envelope.decode_identity` reads on its `agent` branch.
 * - The body is streamed, never buffered (no `transformToString()`).
 * - No retry: a retry would re-run, and re-bill, a turn.
 */
import {
  BedrockAgentCoreClient,
  InvokeAgentRuntimeCommand,
} from "@aws-sdk/client-bedrock-agentcore";

import { validationMessageFromThrownError } from "./errorClassification.js";

/** Environment variable names this module reads. */
export const ENV_RUNTIME_ARN = "AGENT_RUNTIME_ARN";
export const ENV_RUNTIME_QUALIFIER = "AGENT_RUNTIME_QUALIFIER";
export const ENV_REGION = "AWS_REGION";

/** Endpoint qualifier used when the environment names none. */
export const DEFAULT_QUALIFIER = "DEFAULT";

/** MIME type of the payload sent to the Runtime. */
const REQUEST_CONTENT_TYPE = "application/json";

/** MIME type the Runtime answers with — `runtime_server.py` streams `text/event-stream`. */
const RESPONSE_ACCEPT = "text/event-stream";

/** Raised when the Runtime cannot be invoked, or answers without a readable body. */
export class RuntimeInvocationError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "RuntimeInvocationError";
  }
}

/**
 * Raised when the Runtime rejected the turn client-side (a container 4xx); the handler answers 400
 * rather than 502. {@link userMessage} is the user-safe message to send.
 */
export class RuntimeValidationError extends Error {
  readonly userMessage: string;

  constructor(userMessage: string, options?: { cause?: unknown }) {
    super("Runtime rejected the turn as validation_failed", options);
    this.name = "RuntimeValidationError";
    this.userMessage = userMessage;
  }
}

/** Which Runtime to invoke, and where. */
export interface RuntimeConfig {
  readonly runtimeArn: string;
  readonly region: string;
  readonly qualifier: string;
}

/**
 * Read the Runtime configuration from an environment mapping.
 *
 * @throws RuntimeInvocationError when the ARN or the region is absent or blank. An absent qualifier
 *   is not an error: it resolves to {@link DEFAULT_QUALIFIER}.
 */
export function loadRuntimeConfig(env: NodeJS.ProcessEnv = process.env): RuntimeConfig {
  const runtimeArn = (env[ENV_RUNTIME_ARN] ?? "").trim();
  const region = (env[ENV_REGION] ?? "").trim();
  const qualifier = (env[ENV_RUNTIME_QUALIFIER] ?? "").trim() || DEFAULT_QUALIFIER;

  if (!runtimeArn) {
    throw new RuntimeInvocationError(`${ENV_RUNTIME_ARN} is not set`);
  }
  if (!region) {
    throw new RuntimeInvocationError(`${ENV_REGION} is not set`);
  }
  return { runtimeArn, region, qualifier };
}

/** One turn to run, as the browser asked for it plus the claims the relay verified. */
export interface TurnRequest {
  readonly sessionId: string;
  readonly prompt: string;
  readonly claims: Readonly<Record<string, unknown>>;
  readonly pageContext?: unknown;
  readonly tuning?: unknown;
  readonly resumeFrom?: number;
}

/** The principal type the relay declares: it serves only Cognito user tokens. */
export const RELAY_PRINCIPAL_TYPE = "user";

/** The `_agent_identity` block: the verified claims verbatim, the relay's principal type, the session. */
function agentIdentity(
  claims: Readonly<Record<string, unknown>>,
  sessionId: string,
): Record<string, unknown> {
  return { claims, principal_type: RELAY_PRINCIPAL_TYPE, session_id: sessionId };
}

/**
 * Render a turn as the Runtime's invocation payload.
 *
 * The claims are copied in verbatim under `_agent_identity`, the key
 * `crm.envelope.decode_identity` reads on its `agent` branch, beside the required
 * `principal_type`, always {@link RELAY_PRINCIPAL_TYPE}. Optional members are omitted rather
 * than sent as `null`, so `decode_invocation` sees an absent field as absent.
 */
export function buildRuntimePayload(request: TurnRequest): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    prompt: request.prompt,
    session_id: request.sessionId,
    _agent_identity: agentIdentity(request.claims, request.sessionId),
  };
  if (request.pageContext !== undefined) {
    payload["page_context"] = request.pageContext;
  }
  if (request.tuning !== undefined) {
    payload["tuning"] = request.tuning;
  }
  if (request.resumeFrom !== undefined) {
    payload["resume_from"] = request.resumeFrom;
  }
  return payload;
}

/** The response shape this module consumes: a body that can be iterated chunk by chunk. */
export interface RuntimeResponse {
  readonly response?: AsyncIterable<Uint8Array> | undefined;
  readonly statusCode?: number | undefined;
  readonly contentType?: string | undefined;
}

/** What a client must offer this module: one `send` for one command. */
export interface RuntimeClient {
  send(command: InvokeAgentRuntimeCommand): Promise<RuntimeResponse>;
}

/** Build the SigV4 client for `config`, with an explicit region. */
export function createRuntimeClient(config: RuntimeConfig): RuntimeClient {
  return new BedrockAgentCoreClient({ region: config.region }) as unknown as RuntimeClient;
}

let cachedClient: RuntimeClient | undefined;
let cachedConfig: RuntimeConfig | undefined;

/** Return the process-wide client and configuration, building each at most once. */
export function sharedRuntimeClient(env: NodeJS.ProcessEnv = process.env): {
  client: RuntimeClient;
  config: RuntimeConfig;
} {
  if (cachedClient === undefined || cachedConfig === undefined) {
    cachedConfig = loadRuntimeConfig(env);
    cachedClient = createRuntimeClient(cachedConfig);
  }
  return { client: cachedClient, config: cachedConfig };
}

/** Discard the process-wide client, so a test can start from a cold environment. */
export function resetSharedRuntimeClient(): void {
  cachedClient = undefined;
  cachedConfig = undefined;
}

/**
 * Build the command for one turn, with `runtimeUserId` unset.
 *
 * The payload is encoded to UTF-8 bytes because the request member is a blob, not a string.
 */
export function buildInvokeCommand(
  request: TurnRequest,
  config: RuntimeConfig,
): InvokeAgentRuntimeCommand {
  return new InvokeAgentRuntimeCommand({
    agentRuntimeArn: config.runtimeArn,
    qualifier: config.qualifier,
    runtimeSessionId: request.sessionId,
    contentType: REQUEST_CONTENT_TYPE,
    accept: RESPONSE_ACCEPT,
    payload: new TextEncoder().encode(JSON.stringify(buildRuntimePayload(request))),
  });
}

/**
 * Invoke the Runtime for one turn and return its response body, unread.
 *
 * The body is returned rather than consumed so the caller writes each chunk onward as it arrives.
 * Exactly one `send` is issued: a failure is reported, never retried.
 *
 * @param request The turn, carrying the claims the relay verified.
 * @param deps Injection seam for tests; defaults to the process-wide client and configuration.
 * @returns The response's chunks, in order.
 * @throws RuntimeValidationError when the call fails with a Runtime client-side (4xx) rejection.
 * @throws RuntimeInvocationError when the call otherwise fails, when the Runtime answers a non-2xx
 *   status, or when it answers with no body.
 */
export async function invokeRuntimeStream(
  request: TurnRequest,
  deps: { client: RuntimeClient; config: RuntimeConfig } = sharedRuntimeClient(),
): Promise<AsyncIterable<Uint8Array>> {
  const command = buildInvokeCommand(request, deps.config);

  let response: RuntimeResponse;
  try {
    response = await deps.client.send(command);
  } catch (cause) {
    // AgentCore throws on a Runtime non-2xx; a container 4xx becomes a 400, anything else a 502.
    const validationMessage = validationMessageFromThrownError(cause);
    if (validationMessage !== undefined) {
      throw new RuntimeValidationError(validationMessage, { cause });
    }
    throw new RuntimeInvocationError("InvokeAgentRuntime failed", { cause });
  }

  const status = response.statusCode;
  if (status !== undefined && (status < 200 || status > 299)) {
    // Defensive: AgentCore throws on a Runtime non-2xx rather than returning one.
    throw new RuntimeInvocationError(`Runtime answered with status ${status}`);
  }
  if (response.response === undefined) {
    throw new RuntimeInvocationError("Runtime answered with no response body");
  }
  return response.response;
}

/**
 * Payload field and value selecting the Runtime's cooperative-cancellation mode on `/invocations`.
 * Must equal `runtime_server._MODE_KEY` / `runtime_server._CANCEL_MODE`.
 */
export const MODE_KEY = "mode";
export const CANCEL_MODE = "cancel";

/** One cancellation to request, plus the claims the relay verified for the caller asking. */
export interface CancelRequest {
  readonly sessionId: string;
  readonly claims: Readonly<Record<string, unknown>>;
}

/**
 * Render a cancellation as the Runtime's invocation payload.
 *
 * Carries no `prompt`. Identity travels under `_agent_identity` exactly as it does for a turn,
 * `principal_type` included.
 */
export function buildCancelPayload(request: CancelRequest): Record<string, unknown> {
  return {
    [MODE_KEY]: CANCEL_MODE,
    session_id: request.sessionId,
    _agent_identity: agentIdentity(request.claims, request.sessionId),
  };
}

/**
 * Build the cancel command, on the same `runtimeSessionId` as the turn it targets.
 *
 * The Runtime's cancellation store is process-local, so AgentCore session affinity must route the
 * cancel to the instance running the turn. `accept` is JSON: the cancel mode answers with one object.
 */
export function buildCancelCommand(
  request: CancelRequest,
  config: RuntimeConfig,
): InvokeAgentRuntimeCommand {
  return new InvokeAgentRuntimeCommand({
    agentRuntimeArn: config.runtimeArn,
    qualifier: config.qualifier,
    runtimeSessionId: request.sessionId,
    contentType: REQUEST_CONTENT_TYPE,
    accept: REQUEST_CONTENT_TYPE,
    payload: new TextEncoder().encode(JSON.stringify(buildCancelPayload(request))),
  });
}

/** What the Runtime answered a cancellation with: its status and its body, read to the end. */
export interface CancelResult {
  readonly statusCode: number;
  readonly body: string;
}

/**
 * Request one cancellation and read the Runtime's answer.
 *
 * A separate `InvokeAgentRuntime` call, issued on its own Lambda invocation. The body is read to
 * completion, and the Runtime's status is returned uninterpreted.
 *
 * @throws RuntimeInvocationError when the call fails or answers with no body. A non-2xx status is
 *   not an error here.
 */
export async function invokeRuntimeCancel(
  request: CancelRequest,
  deps: { client: RuntimeClient; config: RuntimeConfig } = sharedRuntimeClient(),
): Promise<CancelResult> {
  let response: RuntimeResponse;
  try {
    response = await deps.client.send(buildCancelCommand(request, deps.config));
  } catch (cause) {
    throw new RuntimeInvocationError("InvokeAgentRuntime failed for a cancellation", { cause });
  }
  if (response.response === undefined) {
    throw new RuntimeInvocationError("Runtime answered a cancellation with no response body");
  }
  const decoder = new TextDecoder();
  let body = "";
  for await (const chunk of response.response) {
    body += decoder.decode(chunk, { stream: true });
  }
  body += decoder.decode();
  return { statusCode: response.statusCode ?? 200, body };
}
