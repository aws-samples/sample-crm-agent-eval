// SPDX-License-Identifier: MIT
/**
 * Classification of a failed `InvokeAgentRuntime` call.
 *
 * On a Runtime non-2xx, AgentCore throws a `RuntimeClientError` (HTTP 424) whose message is a fixed
 * service string, `Received error (<status>) from runtime. ...`; the Runtime's own response body does
 * not cross the boundary, so the only recoverable fact is the container's embedded HTTP status.
 *
 * - {@link validationMessageFromThrownError}: a container 4xx maps to a relay 400 with a generic safe
 *   message; a 5xx, no match, or any other error stays 502.
 * - {@link runtimeClientStatusFromThrownError}: the container 4xx itself, for a pass-through.
 * - {@link causeClassification}: the thrown SDK error's class and transport metadata plus one
 *   content-free message-pattern label, never the message text or body.
 */

/** The generic, user-safe message for a Runtime client-side (4xx) rejection. */
export const GENERIC_VALIDATION_MESSAGE =
  "The assistant could not accept this request. Adjust your selection and try again.";

/**
 * The container HTTP status embedded in AgentCore's `RuntimeClientError` service message, e.g. the
 * `400` in "Received error (400) from runtime. Please check your CloudWatch logs...". Anchored to that
 * phrasing so an unrelated error whose text merely contains a number does not match.
 */
const RUNTIME_ERROR_STATUS = /Received error \((\d{3})\) from runtime/;

/** Parse the container HTTP status from a thrown error's message, or `undefined` when it is not the
 * documented RuntimeClientError shape. */
function runtimeErrorStatus(cause: unknown): number | undefined {
  if (typeof cause !== "object" || cause === null) {
    return undefined;
  }
  const message = (cause as { message?: unknown }).message;
  if (typeof message !== "string") {
    return undefined;
  }
  const match = RUNTIME_ERROR_STATUS.exec(message);
  return match ? Number(match[1]) : undefined;
}

/**
 * Return the generic safe message when a **thrown** SDK error is a Runtime client-side (4xx)
 * rejection, else `undefined` so the caller keeps the 502 path.
 */
export function validationMessageFromThrownError(cause: unknown): string | undefined {
  const status = runtimeErrorStatus(cause);
  return status !== undefined && status >= 400 && status <= 499
    ? GENERIC_VALIDATION_MESSAGE
    : undefined;
}

/**
 * The container's 4xx status when a **thrown** SDK error is a Runtime client-side rejection, else
 * `undefined`. Only the status crosses the AgentCore boundary; the container's body does not.
 */
export function runtimeClientStatusFromThrownError(cause: unknown): number | undefined {
  const status = runtimeErrorStatus(cause);
  return status !== undefined && status >= 400 && status <= 499 ? status : undefined;
}

/** A content-free label of a failure's message shape, for the turn log — never the text itself. */
export type CauseMessagePattern = "runtime_4xx" | "runtime_5xx" | "runtime_other" | "none";

/** Classify the thrown error's message by the container status it embeds. */
function classifyMessagePattern(cause: unknown): CauseMessagePattern {
  const status = runtimeErrorStatus(cause);
  if (status === undefined) {
    return "none";
  }
  if (status >= 400 && status <= 499) {
    return "runtime_4xx";
  }
  if (status >= 500 && status <= 599) {
    return "runtime_5xx";
  }
  return "runtime_other";
}

/**
 * Non-sensitive classification of the SDK error wrapped as a relay failure's `cause`.
 *
 * Reads only `name`, `$metadata.httpStatusCode`, `$fault`, and `code`/`__type`, plus a
 * `cause_message_pattern` label; never the message text, body, headers, or request ids. Returns `{}`
 * when the failure has no object `cause`.
 */
export function causeClassification(failure: unknown): Record<string, string | number> {
  const cause =
    failure instanceof Error ? (failure as { cause?: unknown }).cause : undefined;
  if (typeof cause !== "object" || cause === null) {
    return {};
  }
  const record = cause as Record<string, unknown>;
  const fields: Record<string, string | number> = {};
  const name = record["name"];
  if (typeof name === "string") {
    fields["cause_name"] = name;
  }
  const metadata = record["$metadata"];
  const status =
    typeof metadata === "object" && metadata !== null
      ? (metadata as { httpStatusCode?: unknown })["httpStatusCode"]
      : undefined;
  if (typeof status === "number") {
    fields["cause_http_status"] = status;
  }
  const fault = record["$fault"];
  if (typeof fault === "string") {
    fields["cause_fault"] = fault;
  }
  const code = record["code"] ?? record["__type"];
  if (typeof code === "string") {
    fields["cause_code"] = code;
  }
  fields["cause_message_pattern"] = classifyMessagePattern(cause);
  return fields;
}
