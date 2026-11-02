// SPDX-License-Identifier: MIT
// GENERATED FILE — do not edit by hand.
// Regenerate with `npm run gen:types` in `src/relay/`; the source of truth is
// `src/agents/events.py` via `src/agents/event_schema.py`, and
// `scripts/check_sse_schema_parity.py` fails the build on drift.

/** Version of the frame contract this build was generated against. */
export const SCHEMA_VERSION = "3.0.0";

/** Prefix and delimiter of one SSE frame, as `format_sse_line` emits them. */
export const FRAME_PREFIX = "data: ";
export const FRAME_DELIMITER = "\n\n";

/** Marker a truncated tool result carries in place of the records it could not include. */
export const TRUNCATION_MARKER = "__truncated__";

/** Byte ceiling a tool-result payload is truncated at. */
export const TOOL_RESULT_BYTE_CEILING = 32768;

/** The closed set of event kinds. */
export const EVENT_KINDS = ["tool_call", "tool_result", "proposal", "usage", "completed", "error", "text_delta", "heartbeat"] as const;
export type EventKind = (typeof EVENT_KINDS)[number];

/** The closed set of terminal error kinds, carried inside an `error` event. */
export const ERROR_KINDS = ["budget_exhausted", "tool_failure", "model_failure", "invalid_request"] as const;
export type ErrorKind = (typeof ERROR_KINDS)[number];

/** One `CostLine` element of an event frame array. */
export interface CostLine {
  readonly cache_read_tokens: number;
  readonly cache_write_tokens: number;
  readonly cost_usd: number | null;
  readonly input_tokens: number;
  readonly model_id: string;
  readonly output_tokens: number;
  readonly role: string;
  readonly tier: "supervisor" | "subagent" | "tool";
}

/** One `ResolvedTierInfo` element of an event frame array. */
export interface ResolvedTierInfo {
  readonly effort: string | null;
  readonly family: "anthropic" | "openai";
  readonly model_id: string;
  readonly tier: "supervisor" | "subagent" | "tool";
}

/** One agent event, as one JSON object on one SSE data frame. */
export interface AgentEventFrame {
  readonly cache_read_tokens: number | null;
  readonly cache_write_tokens: number | null;
  readonly cost_lines: ReadonlyArray<CostLine> | null;
  readonly cost_usd: number | null;
  readonly cost_withheld: boolean | null;
  readonly error_kind: ErrorKind | null;
  readonly error_message: string | null;
  readonly event_id: string | null;
  readonly input_tokens: number | null;
  readonly kind: EventKind;
  readonly output_tokens: number | null;
  readonly partial: boolean;
  readonly partial_work: string | null;
  readonly proposal_id: string | null;
  readonly proposal_summary: Record<string, unknown> | null;
  readonly resolved_tiers: ReadonlyArray<ResolvedTierInfo> | null;
  readonly text: string | null;
  readonly tool_input: Record<string, unknown> | null;
  readonly tool_name: string | null;
  readonly tool_output: Record<string, unknown> | null;
  readonly truncated: boolean;
}

/** Whether `value` is one of the kinds this schema version declares. */
export function isEventKind(value: unknown): value is EventKind {
  return typeof value === "string" && (EVENT_KINDS as readonly string[]).includes(value);
}

/** Whether `value` is one of the error kinds this schema version declares. */
export function isErrorKind(value: unknown): value is ErrorKind {
  return typeof value === "string" && (ERROR_KINDS as readonly string[]).includes(value);
}
