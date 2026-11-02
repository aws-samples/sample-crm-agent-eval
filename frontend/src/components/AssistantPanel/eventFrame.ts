// SPDX-License-Identifier: MIT
/**
 * Normalise a decoded SSE event into the generated `AgentEventFrame` the panel's components read.
 *
 * `useAgentStream`'s `AgentStreamEvent` is the parse shape, with every field but `kind` optional;
 * `AgentEventFrame` is the contract shape generated from `src/agents/events.py`, with every field
 * present and `null` when unused. {@link toEventFrame} is the one crossing between them; `truncated`
 * defaults to `false` here.
 */

import type { AgentStreamEvent } from "../../hooks/useAgentStream";
import type { AgentEventFrame, ErrorKind, EventKind } from "../../types/generated/agentEvents";

/** Stands in for a `kind` the producer did not send at all. */
export const MALFORMED_KIND = "malformed_frame";

/**
 * Widen one parsed frame into the contract shape.
 *
 * `kind` and `error_kind` are carried through unvalidated, so a kind this build's schema does not
 * declare still reaches `EventTrace`, which renders it visibly.
 *
 * @param event One frame as `useAgentStream` parsed it off the wire.
 * @returns The same frame with every contract field present.
 */
export function toEventFrame(event: AgentStreamEvent): AgentEventFrame {
  return {
    kind: (typeof event.kind === "string" && event.kind !== "" ? event.kind : MALFORMED_KIND) as EventKind,
    tool_name: event.tool_name ?? null,
    tool_input: event.tool_input ?? null,
    tool_output: event.tool_output ?? null,
    truncated: event.truncated ?? false,
    proposal_id: event.proposal_id ?? null,
    proposal_summary: event.proposal_summary ?? null,
    input_tokens: event.input_tokens ?? null,
    output_tokens: event.output_tokens ?? null,
    cache_read_tokens: event.cache_read_tokens ?? null,
    cache_write_tokens: event.cache_write_tokens ?? null,
    text: event.text ?? null,
    error_kind: (event.error_kind ?? null) as ErrorKind | null,
    error_message: event.error_message ?? null,
    partial_work: event.partial_work ?? null,
    event_id: event.event_id ?? null,
    // Only a `text_delta` sets it; every other kind is a complete frame.
    partial: event.partial ?? false,
    cost_lines: event.cost_lines ?? null,
    cost_usd: event.cost_usd ?? null,
    cost_withheld: event.cost_withheld ?? null,
    resolved_tiers: event.resolved_tiers ?? null,
  };
}
