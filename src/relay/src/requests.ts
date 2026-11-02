// SPDX-License-Identifier: MIT
/**
 * Decoding the two request shapes the relay accepts, and stripping what no caller may send.
 *
 * The relay's endpoint takes a turn (`{prompt, session_id, …}`) and a cancellation
 * (`{mode: "cancel", session_id}`) on the same POST, as the Runtime does on `/invocations`.
 *
 * Both decoders build their result field by field from an explicit allowlist, never by spreading the
 * parsed body, so a caller-supplied `_agent_identity` cannot reach the Runtime payload.
 */
import type { LambdaFunctionURLEvent } from "aws-lambda";

import { CANCEL_MODE, MODE_KEY } from "./runtime.js";

/**
 * Every identity-channel key `crm.envelope.decode_identity` selects a channel from, plus the Gateway
 * interceptor's own key; a superset of `crm.envelope.IDENTITY_CHANNEL_KEYS`. No caller-supplied copy
 * of these survives into the Runtime payload.
 */
export const IDENTITY_CHANNEL_KEYS: readonly string[] = [
  "_identity",
  "_agent_identity",
  "requestContext",
  "_gateway_identity",
];

/** The turn body the browser sends. Every member is validated before use. */
export interface TurnBody {
  readonly prompt: string;
  readonly sessionId: string;
  readonly pageContext?: unknown;
  readonly tuning?: unknown;
  readonly resumeFrom?: number;
}

/**
 * Remove every identity-channel key from a caller-supplied value, at every depth.
 *
 * Removal is unconditional and is not reported.
 */
export function stripIdentityChannels(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(stripIdentityChannels);
  }
  if (typeof value !== "object" || value === null) {
    return value;
  }
  const cleaned: Record<string, unknown> = {};
  for (const [key, nested] of Object.entries(value)) {
    if (!IDENTITY_CHANNEL_KEYS.includes(key)) {
      cleaned[key] = stripIdentityChannels(nested);
    }
  }
  return cleaned;
}

/**
 * Decode the request body into the closed set of fields the relay forwards, tolerating the base64
 * form a Function URL may deliver. Returns `undefined` when `prompt` or `session_id` is blank.
 */
export function decodeBody(event: LambdaFunctionURLEvent): TurnBody | undefined {
  const body = parseJsonBody(event);
  if (body === undefined) {
    return undefined;
  }
  const prompt = typeof body["prompt"] === "string" ? body["prompt"] : "";
  const sessionId = typeof body["session_id"] === "string" ? body["session_id"] : "";
  if (prompt.trim() === "" || sessionId.trim() === "") {
    return undefined;
  }
  const resumeFrom = typeof body["resume_from"] === "number" ? body["resume_from"] : undefined;
  return {
    prompt,
    sessionId,
    ...(body["page_context"] === undefined
      ? {}
      : { pageContext: stripIdentityChannels(body["page_context"]) }),
    ...(body["tuning"] === undefined ? {} : { tuning: stripIdentityChannels(body["tuning"]) }),
    ...(resumeFrom === undefined ? {} : { resumeFrom }),
  };
}

/** A cancellation body: which conversation to stop, and nothing else. */
export interface CancelBody {
  readonly sessionId: string;
}

/**
 * Parse the request body to a JSON object, tolerating the base64 form a Function URL may deliver.
 *
 * @returns The parsed object, or `undefined` for an absent, unparseable, or non-object body.
 */
export function parseJsonBody(event: LambdaFunctionURLEvent): Record<string, unknown> | undefined {
  if (event.body === undefined || event.body === null || event.body === "") {
    return undefined;
  }
  let parsed: unknown;
  try {
    const raw = event.isBase64Encoded
      ? Buffer.from(event.body, "base64").toString("utf8")
      : event.body;
    parsed = JSON.parse(raw);
  } catch {
    return undefined;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return undefined;
  }
  return parsed as Record<string, unknown>;
}

/**
 * Decode a cancellation, or `undefined` when this body is not one.
 *
 * Only the exact mode value selects cancellation; a near-miss (`"cancel_turn"`, `"Cancel"`) takes the
 * turn path.
 */
export function decodeCancelBody(event: LambdaFunctionURLEvent): CancelBody | undefined {
  const body = parseJsonBody(event);
  if (body === undefined || body[MODE_KEY] !== CANCEL_MODE) {
    return undefined;
  }
  const sessionId = typeof body["session_id"] === "string" ? body["session_id"] : "";
  return sessionId.trim() === "" ? undefined : { sessionId };
}

/**
 * Length bounds `InvokeAgentRuntime` documents for `runtimeSessionId`, to which `session_id` is
 * passed unchanged. Must equal the bounds in `useAgentStream`'s session-id generator.
 */
export const SESSION_ID_MIN_LENGTH = 33;
export const SESSION_ID_MAX_LENGTH = 256;

/**
 * Sent when a `session_id` is outside the documented bounds. Names the constraint, not the value.
 */
export const SESSION_ID_RANGE_MESSAGE = "The session id must be between 33 and 256 characters.";

/** Whether `sessionId` satisfies the documented `runtimeSessionId` bounds. */
export function isSessionIdInRange(sessionId: string): boolean {
  return (
    sessionId.length >= SESSION_ID_MIN_LENGTH && sessionId.length <= SESSION_ID_MAX_LENGTH
  );
}

/**
 * The `session_id` this body declares, or `""` when it carries none usable. Covers both the turn and
 * the cancel shape.
 */
export function bodySessionId(event: LambdaFunctionURLEvent): string {
  const body = parseJsonBody(event);
  const sessionId = body === undefined ? "" : body["session_id"];
  return typeof sessionId === "string" ? sessionId : "";
}

/**
 * The problem with this body's `session_id`, or `undefined` when there is none. A body carrying no id
 * returns `undefined`; the decoders reject it.
 */
export function sessionIdRangeError(event: LambdaFunctionURLEvent): string | undefined {
  const sessionId = bodySessionId(event);
  if (sessionId === "" || isSessionIdInRange(sessionId)) {
    return undefined;
  }
  return SESSION_ID_RANGE_MESSAGE;
}
