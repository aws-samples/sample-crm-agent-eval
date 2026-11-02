// SPDX-License-Identifier: MIT
/**
 * The relay's `streamifyResponse` handler: verify, invoke, re-emit.
 *
 * 1. Verify the Cognito bearer token from `X-Crm-Authorization` before every other check; a
 *    rejection is a plain HTTP 401. This is the only authentication on the browser path.
 * 2. Reject anything that is not a `POST` with a usable body, before the stream opens.
 * 3. Invoke the Runtime over SigV4 and re-emit its frames verbatim, one complete
 *    `data: <json>\n\n` frame at a time. A frame whose `kind` this schema version does not know is
 *    forwarded, not filtered.
 * 4. A failure discovered after the first byte travels as an SSE `error` frame.
 * 5. `end()` runs in a `finally`.
 *
 * It holds no business logic and builds no `Identity`: verified claims travel in the Runtime payload
 * over the `agent` channel. It sets no timeout of its own and never retries.
 */

import type { LambdaFunctionURLEvent } from "aws-lambda";

import {
  FRAME_DELIMITER,
  FRAME_PREFIX,
  SCHEMA_VERSION,
  isEventKind,
  type ErrorKind,
} from "./generated/events.js";
import { causeClassification, runtimeClientStatusFromThrownError } from "./errorClassification.js";
import { JWT_HEADER, JwtVerificationError, verifyBearerToken, type VerifiedClaims } from "./jwt.js";
import { decodeBody, decodeCancelBody, sessionIdRangeError } from "./requests.js";
import {
  RuntimeInvocationError,
  RuntimeValidationError,
  invokeRuntimeCancel,
  invokeRuntimeStream,
  type CancelRequest,
  type CancelResult,
  type TurnRequest,
} from "./runtime.js";

/**
 * `ErrorKind` used when the Runtime or its transport dies after the stream has opened. A transport
 * death is distinguished by {@link TRANSPORT_ERROR_MESSAGE} on the wire and by the turn log's `outcome`.
 */
const TRANSPORT_ERROR_KIND: ErrorKind = "model_failure";

/**
 * Message sent to the browser when the turn dies mid-stream. Names no socket, host, or underlying
 * error.
 */
export const TRANSPORT_ERROR_MESSAGE =
  "The connection to the assistant ended before the turn finished. Try again.";

/** Response headers for the event stream, chosen so no hop buffers it. */
const STREAM_HEADERS: Readonly<Record<string, string>> = {
  "content-type": "text/event-stream",
  "cache-control": "no-cache, no-store",
  "x-accel-buffering": "no",
};


// Request decoding lives in `./requests.js`; re-exported as part of the relay's surface.
export {
  IDENTITY_CHANNEL_KEYS,
  decodeBody,
  decodeCancelBody,
  parseJsonBody,
  SESSION_ID_MAX_LENGTH,
  SESSION_ID_MIN_LENGTH,
  SESSION_ID_RANGE_MESSAGE,
  stripIdentityChannels,
} from "./requests.js";

/** Where the handler writes its response. One implementation wraps the Lambda response stream. */
export interface ResponseSink {
  /** Commit the status line and headers. Called at most once, before any `write`. */
  begin(statusCode: number, headers: Readonly<Record<string, string>>): void;
  write(chunk: string): void;
  end(): void;
}


/** Injection seam: the three collaborators the handler drives. */
export interface HandlerDeps {
  verify: (header: string | undefined) => Promise<VerifiedClaims>;
  invoke: (request: TurnRequest) => Promise<AsyncIterable<Uint8Array>>;
  /** Requests one cancellation, as its own Runtime call. */
  cancel: (request: CancelRequest) => Promise<CancelResult>;
}

const DEFAULT_DEPS: HandlerDeps = {
  verify: (header) => verifyBearerToken(header),
  invoke: (request) => invokeRuntimeStream(request),
  cancel: (request) => invokeRuntimeCancel(request),
};

/** Read a header case-insensitively; Function URLs lowercase them, but callers do not. */
export function headerValue(
  headers: Readonly<Record<string, string | undefined>> | undefined,
  name: string,
): string | undefined {
  if (headers === undefined) {
    return undefined;
  }
  const wanted = name.toLowerCase();
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() === wanted) {
      return value;
    }
  }
  return undefined;
}

/** Render one SSE frame carrying `payload`, using the generated framing. */
export function frame(payload: Record<string, unknown>): string {
  return `${FRAME_PREFIX}${JSON.stringify(payload)}${FRAME_DELIMITER}`;
}

/**
 * Render the terminal `error` frame the relay emits when a turn dies mid-stream.
 *
 * Every field of the closed schema is present, matching what the Runtime itself emits.
 */
export function transportErrorFrame(): string {
  return frame({
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
    error_kind: TRANSPORT_ERROR_KIND,
    error_message: TRANSPORT_ERROR_MESSAGE,
    partial_work: null,
  });
}

/** A pre-stream rejection: a status and a JSON body, never a stack trace. */
function rejectBeforeStream(sink: ResponseSink, statusCode: number, message: string): void {
  sink.begin(statusCode, { "content-type": "application/json" });
  sink.write(JSON.stringify({ message }));
}


/** One turn's frame accounting, emitted as a single structured log line. */
interface TurnLog {
  schema_version: string;
  session_id: string;
  frames: number;
  unrecognised_kinds: number;
  malformed_frames: number;
  outcome: string;
}

/**
 * Split `buffered` into complete frames, forwarding each **verbatim** and counting what it saw.
 *
 * Returns the trailing partial frame, which stays buffered until the rest of it arrives. A frame that
 * does not parse, or whose `kind` this schema version does not declare, is still forwarded — only its
 * counter moves.
 */
function forwardCompleteFrames(buffered: string, sink: ResponseSink, log: TurnLog): string {
  const parts = buffered.split(FRAME_DELIMITER);
  const remainder = parts.pop() ?? "";
  for (const part of parts) {
    const raw = `${part}${FRAME_DELIMITER}`;
    log.frames += 1;
    if (part.startsWith(FRAME_PREFIX)) {
      try {
        const payload = JSON.parse(part.slice(FRAME_PREFIX.length)) as Record<string, unknown>;
        if (!isEventKind(payload["kind"])) {
          log.unrecognised_kinds += 1;
        }
      } catch {
        log.malformed_frames += 1;
      }
    } else {
      log.malformed_frames += 1;
    }
    sink.write(raw);
  }
  return remainder;
}

/**
 * Handle one turn: validate, verify, invoke, re-emit.
 *
 * @param event The Function URL invocation event.
 * @param sink Where the response is written.
 * @param deps Collaborators; defaults to the real verifier and the real SigV4 client.
 */
export async function streamTurn(
  event: LambdaFunctionURLEvent,
  sink: ResponseSink,
  deps: HandlerDeps = DEFAULT_DEPS,
): Promise<void> {
  const log: TurnLog = {
    schema_version: SCHEMA_VERSION,
    session_id: "",
    frames: 0,
    unrecognised_kinds: 0,
    malformed_frames: 0,
    outcome: "unknown",
  };

  // Attached to the single log line emitted in the `finally` below.
  let failure: unknown;

  try {
    // Authentication before every other check, so an anonymous caller learns nothing but 401.
    let claims: VerifiedClaims;
    try {
      claims = await deps.verify(headerValue(event.headers, JWT_HEADER));
    } catch (error) {
      log.outcome = error instanceof JwtVerificationError ? "unauthorized" : "unauthorized_other";
      failure = error;
      rejectBeforeStream(sink, 401, "Your session is not valid. Sign in again.");
      return;
    }

    const method = event.requestContext?.http?.method;
    if (method !== undefined && method.toUpperCase() !== "POST") {
      log.outcome = "method_not_allowed";
      rejectBeforeStream(sink, 405, "Use POST to start a turn.");
      return;
    }

    // Checked for both shapes before either is decoded.
    const sessionIdProblem = sessionIdRangeError(event);
    if (sessionIdProblem !== undefined) {
      log.outcome = "session_id_out_of_range";
      rejectBeforeStream(sink, 400, sessionIdProblem);
      return;
    }

    // A cancellation carries no prompt, so it is decoded before a turn.
    const cancelBody = decodeCancelBody(event);
    if (cancelBody !== undefined) {
      log.session_id = cancelBody.sessionId;
      failure = await respondToCancel(cancelBody.sessionId, claims, sink, deps, log);
      return;
    }

    const body = decodeBody(event);
    if (body === undefined) {
      log.outcome = "bad_request";
      rejectBeforeStream(sink, 400, "A message and a session id are required to start a turn.");
      return;
    }
    log.session_id = body.sessionId;

    let body_stream: AsyncIterable<Uint8Array>;
    try {
      body_stream = await deps.invoke({
        sessionId: body.sessionId,
        prompt: body.prompt,
        claims,
        ...(body.pageContext === undefined ? {} : { pageContext: body.pageContext }),
        ...(body.tuning === undefined ? {} : { tuning: body.tuning }),
        ...(body.resumeFrom === undefined ? {} : { resumeFrom: body.resumeFrom }),
      });
    } catch (error) {
      // A Runtime client-side rejection is a 400; every other pre-stream failure is a 502.
      if (error instanceof RuntimeValidationError) {
        log.outcome = "validation_failed";
        failure = error;
        rejectBeforeStream(sink, 400, error.userMessage);
        return;
      }
      log.outcome = error instanceof RuntimeInvocationError ? "runtime_unavailable" : "invoke_failed";
      failure = error;
      rejectBeforeStream(sink, 502, "The assistant is unavailable. Try again.");
      return;
    }

    sink.begin(200, STREAM_HEADERS);
    let buffered = "";
    const decoder = new TextDecoder();
    try {
      for await (const chunk of body_stream) {
        buffered += decoder.decode(chunk, { stream: true });
        buffered = forwardCompleteFrames(buffered, sink, log);
      }
      buffered += decoder.decode();
      buffered = forwardCompleteFrames(buffered, sink, log);
      if (buffered.trim() !== "") {
        // A trailing partial frame is a truncated stream and is not forwarded.
        log.outcome = "truncated";
        log.malformed_frames += 1;
        sink.write(transportErrorFrame());
      } else {
        log.outcome = "completed";
      }
    } catch (error) {
      log.outcome = "stream_failed";
      try {
        sink.write(transportErrorFrame());
      } catch {
        // The client's connection is gone; recorded under a distinct outcome.
        log.outcome = "error_frame_undeliverable";
      }
      failure = error;
      return;
    }
  } finally {
    // Exactly one log line per request, on every exit path including the early `return`s.
    logTurn(log, failure);
    sink.end();
  }
}

/**
 * Ask the Runtime to cancel, and answer with what it said.
 *
 * Takes `claims` already verified by {@link streamTurn}. Never streams: the Runtime's status and JSON
 * body are passed through unchanged. A container 4xx that AgentCore raised as an error is answered
 * with that status and a generic message, since its body does not cross the boundary.
 *
 * @returns The caught failure, for the turn log; `undefined` when the Runtime answered.
 */
async function respondToCancel(
  sessionId: string,
  claims: VerifiedClaims,
  sink: ResponseSink,
  deps: HandlerDeps,
  log: TurnLog,
): Promise<unknown> {
  let result: CancelResult;
  try {
    result = await deps.cancel({ sessionId, claims });
  } catch (error) {
    const clientStatus =
      error instanceof RuntimeInvocationError
        ? runtimeClientStatusFromThrownError(error.cause)
        : undefined;
    if (clientStatus !== undefined) {
      log.outcome = `cancel_${clientStatus}`;
      rejectBeforeStream(sink, clientStatus, "The turn could not be stopped.");
      return error;
    }
    log.outcome =
      error instanceof RuntimeInvocationError ? "cancel_unavailable" : "cancel_failed";
    rejectBeforeStream(sink, 502, "The assistant is unavailable. Try again.");
    return error;
  }

  log.outcome = `cancel_${result.statusCode}`;
  sink.begin(result.statusCode, { "content-type": "application/json" });
  sink.write(result.body);
  return undefined;
}

/** Emit one structured line per turn, naming the schema version the relay ran under. */
function logTurn(log: TurnLog, error?: unknown): void {
  const detail = error instanceof Error ? { error: error.name, detail: error.message } : {};
  process.stdout.write(
    `${JSON.stringify({ event: "relay_turn", ...log, ...detail, ...causeClassification(error) })}\n`,
  );
}

/** Adapt a Lambda response stream to {@link ResponseSink}. */
export function httpResponseSink(responseStream: awslambda.HttpResponseStream): ResponseSink {
  let target: { write(chunk: string): void; end(): void } = responseStream;
  let begun = false;
  return {
    begin(statusCode, headers) {
      if (begun) {
        return;
      }
      begun = true;
      target = awslambda.HttpResponseStream.from(responseStream, {
        statusCode,
        headers: { ...headers },
      });
    },
    write(chunk) {
      target.write(chunk);
    },
    end() {
      target.end();
    },
  };
}

/** The deployed entry point. */
export const handler = awslambda.streamifyResponse(
  async (event: LambdaFunctionURLEvent, responseStream: awslambda.HttpResponseStream) => {
    await streamTurn(event, httpResponseSink(responseStream));
  },
);
