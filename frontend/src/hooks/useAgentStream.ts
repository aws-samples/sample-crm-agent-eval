// SPDX-License-Identifier: MIT
/**
 * `useAgentStream` — drives one chat turn against the agent SSE stream and exposes its events to the
 * chat surface.
 *
 * ## Transport
 *
 * A turn is a POST with custom headers, so this hook uses `fetch` rather than `EventSource` and reads
 * the response body as a stream via `ReadableStream`/`TextDecoder`, splitting on the SSE frame
 * delimiter (`\n\n`) that `agents.runtime_entrypoint.format_sse_line` emits — `data: <json>\n\n`
 * per event, one event per frame.
 *
 * Every request goes through the CloudFront `/agent/*` behaviour (`runtimeConfig.agentBaseUrl`), which
 * fronts the streaming relay; the relay verifies the Cognito token, calls the Runtime with SigV4 and
 * re-emits the same frames. Header construction lives in `lib/agentClient.ts`.
 *
 * ## Reconnection
 *
 * `POST /agent/{arn}` is idempotent per `session_id`: resuming re-POSTs the same `session_id` with
 * `resume_from` set to the count of events already applied, and the server replays only events
 * recorded after that point. A genuine I/O failure — the initial `fetch` rejecting, the response
 * arriving non-OK or bodyless, or `read()` throwing mid-stream — triggers an automatic reconnect, up
 * to `maxReconnectAttempts` times with linear backoff, surfaced via `connectionState`. A stream that
 * ends cleanly (`done: true`) without a terminal `completed`/`error` event is closed, not reconnected.
 *
 * An abort this hook issued (unmount, or a newer `sendPrompt`) is never a drop: such a stream returns
 * without reconnecting and without writing state. `mountedRef` stops a reconnect that is waiting out
 * its backoff when the component unmounts.
 *
 * A 401, 403 or 400 is terminal and never reconnects; each maps to its own `failureReason`.
 *
 * ## Event handling
 *
 * Every event kind in the closed set (`tool_call`, `tool_result`, `proposal`, `usage`, `completed`,
 * `error`) is applied to the transcript. `heartbeat` is the one frame dropped: it is liveness
 * only, so it is neither applied nor counted towards `resume_from`. Nothing else is dropped:
 *
 * 1. a `kind` this build does not know is appended as an `unrecognised` entry, keeping its own name;
 * 2. a frame whose JSON will not parse becomes a {@link MALFORMED_FRAME_KIND} event;
 * 3. a frame that parses but is not an object with a string `kind` becomes
 *    {@link MALFORMED_FRAME_KIND} too.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import type { AuthContextValue } from "../auth/AuthProvider";
import { buildRelayRequest, type RelayRequest } from "../lib/agentClient";
import { randomToken } from "../lib/randomToken";
import type { AgentEventFrame } from "../types/generated/agentEvents";

/** The closed set of event kinds this hook understands natively. */
export type KnownEventKind = "tool_call" | "tool_result" | "proposal" | "usage" | "completed" | "error";

/**
 * One decoded SSE frame. Mirrors `agents.events.AgentEvent`'s field set exactly, as rendered by
 * `agents.runtime_entrypoint.format_sse_line`. All fields besides `kind` are optional/nullable
 * because only the subset matching `kind` is ever populated server-side.
 */
export interface AgentStreamEvent {
  readonly kind: string;
  readonly tool_name?: string | null;
  readonly tool_input?: Record<string, unknown> | null;
  readonly tool_output?: Record<string, unknown> | null;
  readonly truncated?: boolean;
  readonly proposal_id?: string | null;
  readonly proposal_summary?: Record<string, unknown> | null;
  readonly input_tokens?: number | null;
  readonly output_tokens?: number | null;
  readonly cache_read_tokens?: number | null;
  readonly cache_write_tokens?: number | null;
  readonly text?: string | null;
  readonly error_kind?: string | null;
  readonly error_message?: string | null;
  readonly partial_work?: string | null;
  /** Groups the `text_delta` frames of one streamed answer (SSE schema 3.0.0); absent on other kinds. */
  readonly event_id?: string | null;
  /** `true` on every produced `text_delta` (a following `completed`/`error` settles its `event_id`,
   * so no delta arrives non-partial); `false`/absent otherwise. */
  readonly partial?: boolean;
  readonly cost_lines?: AgentEventFrame["cost_lines"];
  readonly cost_usd?: number | null;
  readonly cost_withheld?: boolean | null;
  readonly resolved_tiers?: AgentEventFrame["resolved_tiers"];
}

/** One record in the rendered transcript: a decoded event tagged with a stable render key. */
export interface TranscriptEntry {
  readonly key: string;
  readonly event: AgentStreamEvent;
  /** `true` when `event.kind` fell outside the closed set; rendered visibly. */
  readonly unrecognised: boolean;
}

export type ConnectionState = "idle" | "connecting" | "streaming" | "reconnecting" | "closed" | "failed";

/**
 * Why the stream reached `failed`. None is retryable by reconnecting.
 *
 * - `unauthenticated` — a 401, or no token available; signing in again is the fix.
 * - `forbidden` — a 403: a request that did not arrive through the distribution, an
 *   `x-amz-content-sha256` that disagrees with the body, or CloudFront's OAC signature refused by the
 *   relay's resource policy. Signing in again does not fix it. The first two are rejected before the
 *   relay is invoked, so they leave no relay log.
 * - `transport` — a dropped connection after the reconnect budget is spent, or a request that could
 *   not be built.
 * - `invalid_request` — a 400: the Runtime rejected the turn as `validation_failed` (for example an
 *   unsupported model or tuning selection). Carries the relay's user-safe message in
 *   {@link UseAgentStreamResult.failureMessage} when one is present.
 */
export type StreamFailureReason = "unauthenticated" | "forbidden" | "transport" | "invalid_request";

export interface UseAgentStreamOptions {
  /** CloudFront-relative path prefix for the relay, e.g. `/agent`. Same value `useCancelTurn` uses. */
  readonly agentBaseUrl: string;
  /** URL-encoded AgentCore Runtime ARN forming the invocation path. */
  readonly agentRuntimeArn: string;
  /** Bound `getAccessToken` from `useAuth()`. */
  readonly getAccessToken: AuthContextValue["getAccessToken"];
  /** Maximum automatic reconnect attempts after a mid-stream drop. Defaults to 3. */
  readonly maxReconnectAttempts?: number;
  /** Injectable for tests; defaults to the global `fetch`. */
  readonly fetchImpl?: typeof fetch;
}

export interface UseAgentStreamResult {
  readonly connectionState: ConnectionState;
  /** Set when `connectionState === "failed"`; `undefined` otherwise. */
  readonly failureReason: StreamFailureReason | undefined;
  /**
   * A server-provided user-safe message for the failure — the relay's 400 body for an
   * `invalid_request`. `undefined` when none was sent; a consumer then shows the canned copy.
   */
  readonly failureMessage: string | undefined;
  readonly transcript: readonly TranscriptEntry[];
  readonly sessionId: string | undefined;
  /** Populated once a terminal `completed` or `error` event has been applied. */
  readonly turnEnded: boolean;
  /**
   * Send a new prompt. Starts a fresh session unless one is already in flight.
   *
   * `pageContext` is the descriptor of the view the user is looking at, sent as `page_context` (the
   * key the relay's `decodeBody` reads) and replayed verbatim on a reconnect. It carries a descriptor,
   * never rows.
   */
  sendPrompt(
    prompt: string,
    options?: {
      pageContext?: unknown;
      tuning?: Record<string, unknown>;
      /**
       * Continue an existing conversation instead of starting a fresh one.
       *
       * Absent, `sendPrompt` mints a new `session_id`. Supplied, the turn reuses that id and sends
       * `resume_from` set to the events already seen on the session, so `runtime_entrypoint.run_turn`
       * runs a new turn appended to the conversation rather than replaying the last one. Pass the
       * hook's own current {@link UseAgentStreamResult.sessionId}.
       */
      continueSessionId?: string;
    },
  ): Promise<void>;
  /** Manually retry after `connectionState === "failed"`. */
  reconnect(): Promise<void>;
  /**
   * Record that the user asked this turn to stop, so a subsequent mid-stream drop is not resumed.
   *
   * Call it when the stop is requested, not when it succeeds: the Runtime ending a cancelled turn can
   * tear the socket, which `read()` reports as a thrown error. Suppresses automatic reconnection only;
   * {@link reconnect} stays available. Cleared by the next {@link sendPrompt}.
   */
  markTurnCancelled(): void;
}

const DEFAULT_MAX_RECONNECT_ATTEMPTS = 3;
const RECONNECT_BACKOFF_MS = 1000;

/**
 * Length bounds `InvokeAgentRuntime` documents for `runtimeSessionId`, which the relay passes
 * `session_id` through as. The relay enforces the same bounds in `requests.ts`.
 */
export const SESSION_ID_MIN_LENGTH = 33;
export const SESSION_ID_MAX_LENGTH = 256;

/**
 * A session id within {@link SESSION_ID_MIN_LENGTH} and {@link SESSION_ID_MAX_LENGTH}.
 *
 * Uses `crypto.randomUUID()` (36 characters) when available; otherwise a timestamped id padded with
 * base-36 randomness up to the minimum length.
 */
export function newSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  const prefix = `session-${Date.now()}-`;
  const shortfall = Math.max(0, SESSION_ID_MIN_LENGTH - prefix.length);
  return `${prefix}${randomToken(Math.max(shortfall, 11))}`.slice(0, SESSION_ID_MAX_LENGTH);
}

/**
 * The `kind` a frame gets when it is not a readable event. Outside the closed set, so `applyEvent`
 * marks it `unrecognised` and the trace renders it visibly.
 */
export const MALFORMED_FRAME_KIND = "malformed_frame";

/** The liveness frame. Dropped before being applied: never in the transcript, never counted. */
export const HEARTBEAT_KIND = "heartbeat";

/** How much of an unparseable frame is kept. Enough to identify it; not enough to flood the trace. */
export const MALFORMED_FRAME_EXCERPT_LIMIT = 200;

/**
 * True when this stream no longer owns the turn: either this hook aborted it, or a newer `sendPrompt`
 * replaced it. Checked in `runStream` after the response resolves, after each chunk is read, and after
 * the read loop.
 */
function isSuperseded(
  controller: AbortController,
  generation: number,
  current: number,
): boolean {
  return controller.signal.aborted || generation !== current;
}

/**
 * Parse one buffered SSE chunk into zero or more complete `data: <json>` frames, returning the
 * parsed events and any trailing partial frame to prepend to the next chunk. A frame that is not an
 * object with a string `kind` becomes a {@link MALFORMED_FRAME_KIND} event.
 */
function extractFrames(buffer: string): { events: AgentStreamEvent[]; rest: string } {
  const events: AgentStreamEvent[] = [];
  const frames = buffer.split("\n\n");
  const rest = frames.pop() ?? "";
  for (const frame of frames) {
    const line = frame.trim();
    if (!line) continue;
    if (!line.startsWith("data:")) continue;
    const jsonText = line.slice("data:".length).trim();
    if (!jsonText) continue;
    let parsed: unknown;
    try {
      parsed = JSON.parse(jsonText);
    } catch {
      parsed = undefined;
    }
    // `JSON.parse("null")` succeeds, and reading `kind` off `null` would throw inside the read loop.
    if (typeof parsed === "object" && parsed !== null && typeof (parsed as { kind?: unknown }).kind === "string") {
      events.push(parsed as AgentStreamEvent);
    } else {
      events.push({
        kind: MALFORMED_FRAME_KIND,
        text: jsonText.slice(0, MALFORMED_FRAME_EXCERPT_LIMIT),
        error_message: "The assistant sent a frame this build could not read.",
      });
    }
  }
  return { events, rest };
}

/**
 * Read the relay's user-safe `message` from a rejected pre-stream response (the `{ message }` body
 * `rejectBeforeStream` writes). Returns `undefined` for any body that is absent, not JSON, or has no
 * non-empty string `message`.
 */
async function readRelayMessage(response: Response): Promise<string | undefined> {
  try {
    const data: unknown = await response.json();
    if (typeof data === "object" && data !== null) {
      const message = (data as { message?: unknown }).message;
      if (typeof message === "string" && message.trim() !== "") {
        return message;
      }
    }
  } catch {
    // No JSON body, or not parseable — fall back to the canned copy for the reason.
  }
  return undefined;
}

/**
 * Drive one chat turn's SSE stream, applying every event to a running transcript and
 * transparently reconnecting by session id on a mid-stream drop.
 */
export function useAgentStream(options: UseAgentStreamOptions): UseAgentStreamResult {
  const { agentBaseUrl, agentRuntimeArn, getAccessToken, fetchImpl } = options;
  const maxReconnectAttempts = options.maxReconnectAttempts ?? DEFAULT_MAX_RECONNECT_ATTEMPTS;

  const [connectionState, setConnectionState] = useState<ConnectionState>("idle");
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [sessionId, setSessionId] = useState<string | undefined>(undefined);
  const [turnEnded, setTurnEnded] = useState(false);

  const [failureReason, setFailureReason] = useState<StreamFailureReason | undefined>(undefined);
  const [failureMessage, setFailureMessage] = useState<string | undefined>(undefined);
  const eventCountRef = useRef(0);
  const promptRef = useRef<
    { prompt: string; pageContext?: unknown; tuning?: Record<string, unknown> } | undefined
  >(
    undefined,
  );
  const abortRef = useRef<AbortController | undefined>(undefined);
  /** False once unmounted, so a reconnect waiting out its backoff does not issue a fresh POST. */
  const mountedRef = useRef(true);
  /** True once the user asked this turn to stop. Reset per `sendPrompt`. */
  const cancelRequestedRef = useRef(false);
  /** Monotonic id of the turn the hook is currently serving. Bumped by every `sendPrompt`. */
  const generationRef = useRef(0);
  /** The attempt limit, read through a ref so `maybeReconnect` closes over nothing that changes. */
  const maxReconnectAttemptsRef = useRef(maxReconnectAttempts);
  maxReconnectAttemptsRef.current = maxReconnectAttempts;

  const applyEvent = useCallback((event: AgentStreamEvent) => {
    const knownKinds: readonly KnownEventKind[] = [
      "tool_call",
      "tool_result",
      "proposal",
      "usage",
      "completed",
      "error",
    ];
    const unrecognised = !knownKinds.includes(event.kind as KnownEventKind);
    eventCountRef.current += 1;
    const key = `evt-${eventCountRef.current}-${randomToken(6)}`;
    setTranscript((prev) => [...prev, { key, event, unrecognised }]);
    if (event.kind === "completed" || event.kind === "error") {
      setTurnEnded(true);
    }
  }, []);

  const runStream = useCallback(
    async (
      body: Record<string, unknown>,
      currentSessionId: string,
      attempt: number,
      generation: number,
    ): Promise<void> => {
      if (generation !== generationRef.current) {
        // Superseded before a controller is minted: no POST, and `abortRef.current` stays the live turn's.
        return;
      }
      const doFetch = fetchImpl ?? fetch;
      const controller = new AbortController();
      abortRef.current = controller;

      setConnectionState(attempt === 0 ? "connecting" : "reconnecting");

      let token: string;
      try {
        token = await getAccessToken();
      } catch (err) {
        setFailureReason("unauthenticated");
        setConnectionState("failed");
        throw err;
      }

      // Serialized and hashed in one place, so the digest cannot disagree with the bytes sent.
      let request: RelayRequest;
      try {
        request = await buildRelayRequest({ token, accept: "text/event-stream", body });
      } catch (err) {
        // `crypto.subtle` is absent outside a secure context, or `pageContext` holds a cycle. The body
        // is never logged.
        console.error("Could not build the agent request", err);
        setFailureReason("transport");
        setConnectionState("failed");
        throw err;
      }

      if (generation !== generationRef.current) {
        // Superseded during the token fetch or the body digest.
        return;
      }

      let response: Response;
      try {
        response = await doFetch(`${agentBaseUrl}/${encodeURIComponent(agentRuntimeArn)}`, {
          method: "POST",
          headers: request.headers,
          body: request.body,
          signal: controller.signal,
        });
      } catch {
        if (controller.signal.aborted) return;
        await maybeReconnect(currentSessionId, attempt, generation);
        return;
      }

      if (isSuperseded(controller, generation, generationRef.current)) {
        // Superseded while in flight; a `fetch` aborted after its response resolves does not reject.
        return;
      }

      if (response.status === 401) {
        setFailureReason("unauthenticated");
        setConnectionState("failed");
        return;
      }

      if (response.status === 403) {
        setFailureReason("forbidden");
        setConnectionState("failed");
        return;
      }

      if (response.status === 400) {
        setFailureMessage(await readRelayMessage(response));
        setFailureReason("invalid_request");
        setConnectionState("failed");
        return;
      }

      if (!response.ok || !response.body) {
        await maybeReconnect(currentSessionId, attempt, generation);
        return;
      }

      setConnectionState("streaming");

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let sawTerminalEvent = false;
      let readError: unknown;

      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          if (isSuperseded(controller, generation, generationRef.current)) {
            return;
          }
          buffer += decoder.decode(value, { stream: true });
          const { events, rest } = extractFrames(buffer);
          buffer = rest;
          for (const event of events) {
            // Liveness only: not stored for resume, so it must not advance `resume_from`.
            if (event.kind === HEARTBEAT_KIND) continue;
            applyEvent(event);
            if (event.kind === "completed" || event.kind === "error") {
              sawTerminalEvent = true;
            }
          }
        }
      } catch (err) {
        readError = err;
      }

      if (isSuperseded(controller, generation, generationRef.current)) {
        // `if (done) break` skips the per-chunk check, so a stream superseded during its final read
        // is caught here.
        return;
      }

      if (sawTerminalEvent) {
        // A read failure after a terminal event is not a drop.
        setConnectionState("closed");
        return;
      }

      if (readError !== undefined) {
        await maybeReconnect(currentSessionId, attempt, generation);
        return;
      }

      // Clean EOF with no terminal event: closed, not a drop.
      setConnectionState("closed");
    },
    // `maybeReconnect` is omitted: it and `runStream` are mutually recursive, and it closes over
    // nothing that changes between renders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [agentBaseUrl, agentRuntimeArn, applyEvent, getAccessToken, fetchImpl],
  );

  const maybeReconnect = useCallback(
    async (currentSessionId: string, attempt: number, generation: number) => {
      if (generation !== generationRef.current) {
        // Already superseded before the backoff even started.
        return;
      }
      if (cancelRequestedRef.current) {
        // The user asked this turn to stop, so a drop closes it rather than failing it.
        setConnectionState("closed");
        return;
      }
      if (attempt >= maxReconnectAttemptsRef.current || !promptRef.current) {
        setFailureReason("transport");
        setConnectionState("failed");
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, RECONNECT_BACKOFF_MS * (attempt + 1)));
      if (!mountedRef.current) {
        // Unmounted during the backoff; the unmount's abort targeted the stream that already failed.
        return;
      }
      if (cancelRequestedRef.current) {
        // Stop requested during the backoff.
        setConnectionState("closed");
        return;
      }
      // A supersede during the backoff is stopped by `runStream`'s entry check.
      await runStream(
        {
          prompt: promptRef.current.prompt,
          tuning: promptRef.current.tuning,
          ...(promptRef.current.pageContext === undefined
            ? {}
            : { page_context: promptRef.current.pageContext }),
          session_id: currentSessionId,
          resume_from: eventCountRef.current,
        },
        currentSessionId,
        attempt + 1,
        generation,
      );
    },
    [runStream],
  );

  const sendPrompt = useCallback(
    async (
      prompt: string,
      options?: {
        pageContext?: unknown;
        tuning?: Record<string, unknown>;
        continueSessionId?: string;
      },
    ) => {
      abortRef.current?.abort();
      // A continued turn reuses the id and keeps the event cursor; otherwise a fresh id and a zeroed
      // cursor. The transcript holds only the current turn either way.
      const continueId = options?.continueSessionId;
      const reuseSession = typeof continueId === "string" && continueId !== "";
      const id = reuseSession ? continueId : newSessionId();
      const resumeFrom = reuseSession ? eventCountRef.current : 0;
      if (!reuseSession) eventCountRef.current = 0;
      cancelRequestedRef.current = false;
      // Bumped before any await, so every stream still in flight stops at its next checkpoint.
      const generation = ++generationRef.current;
      // Reconnects replay the prompt, descriptor and tuning from here.
      promptRef.current = { prompt, pageContext: options?.pageContext, tuning: options?.tuning };
      setSessionId(id);
      setTranscript([]);
      setTurnEnded(false);
      setFailureReason(undefined);
      setFailureMessage(undefined);
      await runStream(
        {
          prompt,
          tuning: options?.tuning,
          ...(options?.pageContext === undefined ? {} : { page_context: options.pageContext }),
          session_id: id,
          resume_from: resumeFrom,
        },
        id,
        0,
        generation,
      );
    },
    [runStream],
  );

  const reconnect = useCallback(async () => {
    if (!sessionId || !promptRef.current) return;
    setFailureReason(undefined);
    setFailureMessage(undefined);
    await runStream(
      {
        prompt: promptRef.current.prompt,
        tuning: promptRef.current.tuning,
        ...(promptRef.current.pageContext === undefined
          ? {}
          : { page_context: promptRef.current.pageContext }),
        session_id: sessionId,
        resume_from: eventCountRef.current,
      },
      sessionId,
      0,
      // A manual retry resumes the current turn rather than superseding it.
      generationRef.current,
    );
  }, [runStream, sessionId]);

  const markTurnCancelled = useCallback(() => {
    cancelRequestedRef.current = true;
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  return {
    connectionState,
    failureReason,
    failureMessage,
    transcript,
    sessionId,
    turnEnded,
    sendPrompt,
    reconnect,
    markTurnCancelled,
  };
}
