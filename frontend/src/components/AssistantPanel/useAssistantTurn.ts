// SPDX-License-Identifier: MIT
/**
 * Compose one assistant turn's request body from the page descriptor, and derive the disclosure the
 * panel renders alongside the reply.
 *
 * {@link buildTurnRequest} is the one place the turn body is composed. It writes the descriptor under
 * `page_context` (read by the relay's `decodeBody` and `agents.runtime_entrypoint.PAGE_CONTEXT_KEY`)
 * and has no parameter through which a row could be passed; the agent re-executes the described
 * query under its own identity.
 *
 * {@link deriveDisclosure} compares the view's own row count with the count in the agent's
 * `tool_result` frames and reports a divergence for the panel to show. A truncated result on either
 * side, or a tool payload with no recognised row-bearing key, yields `not-comparable`, never `none`.
 */
import { useMemo } from "react";

import type { PageContext } from "../../types/generated/pageContext";
import type { AgentStreamEvent, TranscriptEntry } from "../../hooks/useAgentStream";

/** The wire key the descriptor travels under (snake_case, as on the wire). */
export const PAGE_CONTEXT_WIRE_KEY = "page_context";

/** Tool-result payload keys that carry a collection of records, per tool. */
const ROW_BEARING_KEYS: readonly string[] = [
  "items",
  "activities",
  "records",
  "proposals",
  "opportunities",
];

/** Marker `agents.events.TRUNCATION_MARKER` appends in place of records it could not include. */
export const TRUNCATION_MARKER = "__truncated__";

/** The turn body posted to the relay. Has no member a row could be assigned to. */
export interface TurnRequestBody {
  readonly prompt: string;
  readonly session_id: string;
  readonly resume_from?: number;
  readonly page_context?: PageContext;
  readonly tuning?: Readonly<Record<string, unknown>>;
}

/**
 * What the panel must tell the user about the relationship between their view and the agent's answer.
 * `none` means the counts were compared and matched; an impossible comparison is `not-comparable`.
 */
export type ContextDisclosure =
  | { readonly kind: "none" }
  | { readonly kind: "opaque"; readonly reason: string; readonly message: string }
  | {
      readonly kind: "row-count-divergence";
      readonly viewRowCount: number;
      readonly agentRowCount: number;
      readonly message: string;
    }
  | { readonly kind: "not-comparable"; readonly reason: string; readonly message: string };

/** The sentence shown when the view cannot be summarised. Exported so the panel and its tests name it once. */
export const OPAQUE_MESSAGE = "This view can't be summarised";

/** Reasons a comparison could not be made. Exported so tests name them once. */
export const NOT_COMPARABLE_REASONS = {
  truncated:
    "the assistant's own query stopped early, so its row count is a lower bound and cannot be " +
    "compared with what this view shows",
  unrecognisedPayload:
    "the assistant's tool result did not carry a row collection this build recognises, so its " +
    "row count could not be read",
  viewTruncated:
    "this view's own result stopped early, so the number of records on screen is a lower bound " +
    "and cannot be compared with what the assistant's query returned",
} as const;

/**
 * Compose the turn body.
 *
 * @param prompt The user's message.
 * @param sessionId The turn's session id.
 * @param options `pageContext` is omitted from the body entirely when absent; `resumeFrom` is set only
 *   on a resume.
 * @returns The body to POST.
 */
export function buildTurnRequest(
  prompt: string,
  sessionId: string,
  options?: {
    readonly pageContext?: PageContext;
    readonly tuning?: Readonly<Record<string, unknown>>;
    readonly resumeFrom?: number;
  },
): TurnRequestBody {
  return {
    prompt,
    session_id: sessionId,
    ...(options?.resumeFrom === undefined ? {} : { resume_from: options.resumeFrom }),
    ...(options?.pageContext === undefined ? {} : { page_context: options.pageContext }),
    ...(options?.tuning === undefined ? {} : { tuning: options.tuning }),
  };
}

/** The agent's row count read off one `tool_result` payload, or why it could not be read. */
type AgentRowCount =
  | { readonly count: number }
  | { readonly unreadable: keyof typeof NOT_COMPARABLE_REASONS };

/**
 * Read the row count out of one `tool_result` event. A `truncated` event reports as truncated,
 * checked before the payload is inspected.
 */
export function agentRowCount(event: AgentStreamEvent): AgentRowCount {
  if (event.truncated === true) return { unreadable: "truncated" };
  const raw = event.tool_output;
  if (raw === undefined || raw === null) return { unreadable: "unrecognisedPayload" };
  // Off the proxy path the action Lambdas answer `{"statusCode", "body": {...}}`, so the collection
  // is nested under `body`. A `body` that is not a plain object falls back to the payload itself.
  const body = raw.body;
  const payload =
    typeof body === "object" && body !== null && !Array.isArray(body)
      ? (body as Record<string, unknown>)
      : raw;
  for (const key of ROW_BEARING_KEYS) {
    const value = payload[key];
    if (!Array.isArray(value)) continue;
    if (value.includes(TRUNCATION_MARKER)) return { unreadable: "truncated" };
    return { count: value.length };
  }
  return { unreadable: "unrecognisedPayload" };
}

/**
 * The last readable row count across a transcript, or why none was readable.
 *
 * Uses the last row-bearing `tool_result`: a `tool_result` with no recognised
 * {@link ROW_BEARING_KEYS} collection (e.g. `get_page_context`) and non-`tool_result` frames are
 * skipped. A declared truncation counts as row-bearing. When no frame carries a recognised
 * collection, the last `tool_result`'s unreadable reason is returned; a transcript with no
 * `tool_result` yields `undefined`.
 */
export function transcriptRowCount(
  transcript: readonly TranscriptEntry[],
): AgentRowCount | undefined {
  let lastToolResult: AgentRowCount | undefined;
  let lastRowBearing: AgentRowCount | undefined;
  for (const entry of transcript) {
    if (entry.event.kind !== "tool_result") continue;
    const count = agentRowCount(entry.event);
    lastToolResult = count;
    if (!("unreadable" in count) || count.unreadable === "truncated") {
      lastRowBearing = count;
    }
  }
  return lastRowBearing ?? lastToolResult;
}

/** What the panel knows about the view it is attached to. */
export interface AssistantTurnInput {
  /** The descriptor for the current view, from `usePageContext`. */
  readonly pageContext: PageContext;
  /**
   * How many rows the view is currently showing. Omitted when the view shows no countable
   * collection (a detail page), in which case there is no disclosure.
   */
  readonly viewRowCount?: number;
  /** `true` when the view's own result was cut short, making {@link viewRowCount} a lower bound. */
  readonly viewTruncated?: boolean;
  /** The turn's transcript so far, from `useAgentStream`. */
  readonly transcript: readonly TranscriptEntry[];
}

/**
 * Derive the disclosure for the current turn. An `opaque` context short-circuits before any count
 * comparison.
 */
export function deriveDisclosure(input: AssistantTurnInput): ContextDisclosure {
  const { pageContext, viewRowCount, viewTruncated, transcript } = input;

  if (pageContext.kind === "opaque") {
    return {
      kind: "opaque",
      reason: pageContext.reason,
      message: `${OPAQUE_MESSAGE} — ${pageContext.reason}.`,
    };
  }

  // A detail page shows one record, not a collection.
  if (viewRowCount === undefined) return { kind: "none" };

  const agent = transcriptRowCount(transcript);
  if (agent === undefined) return { kind: "none" };

  /*
   * A truncated view has a lower-bound count, so it is `not-comparable`.
   */
  if (viewTruncated === true) {
    return {
      kind: "not-comparable",
      reason: "viewTruncated",
      message:
        "The assistant's answer may cover a different set of records than this view shows: " +
        `${NOT_COMPARABLE_REASONS.viewTruncated}.`,
    };
  }

  if ("unreadable" in agent) {
    return {
      kind: "not-comparable",
      reason: agent.unreadable,
      message:
        "The assistant's answer may cover a different set of records than this view shows: " +
        `${NOT_COMPARABLE_REASONS[agent.unreadable]}.`,
    };
  }

  if (agent.count === viewRowCount) return { kind: "none" };

  return {
    kind: "row-count-divergence",
    viewRowCount,
    agentRowCount: agent.count,
    message:
      `This view shows ${viewRowCount} record${viewRowCount === 1 ? "" : "s"}, but the assistant's ` +
      `own query returned ${agent.count}. You and the assistant may have access to different ` +
      "records, so its answer describes its own result set, not this view.",
  };
}

/** What the panel gets back from {@link useAssistantTurn}. */
export interface UseAssistantTurnResult {
  /** The disclosure to render for this turn. `kind: "none"` renders nothing. */
  readonly disclosure: ContextDisclosure;
  /** Compose the body for a new turn, carrying the current descriptor. */
  buildRequest(
    prompt: string,
    sessionId: string,
    options?: { readonly tuning?: Readonly<Record<string, unknown>>; readonly resumeFrom?: number },
  ): TurnRequestBody;
}

/**
 * Bind the current page descriptor to a turn and expose the disclosure the panel must render.
 *
 * @param input The current view's descriptor, its row count if it shows a collection, and the
 *   turn transcript so far.
 * @returns The disclosure, and a request builder that carries the descriptor without rows.
 */
export function useAssistantTurn(input: AssistantTurnInput): UseAssistantTurnResult {
  const { pageContext, viewRowCount, viewTruncated, transcript } = input;

  const disclosure = useMemo(
    () => deriveDisclosure({ pageContext, viewRowCount, viewTruncated, transcript }),
    [pageContext, viewRowCount, viewTruncated, transcript],
  );

  const buildRequest = useMemo(
    () =>
      (
        prompt: string,
        sessionId: string,
        options?: {
          readonly tuning?: Readonly<Record<string, unknown>>;
          readonly resumeFrom?: number;
        },
      ): TurnRequestBody =>
        buildTurnRequest(prompt, sessionId, {
          pageContext,
          ...(options?.tuning === undefined ? {} : { tuning: options.tuning }),
          ...(options?.resumeFrom === undefined ? {} : { resumeFrom: options.resumeFrom }),
        }),
    [pageContext],
  );

  return { disclosure, buildRequest };
}
