// SPDX-License-Identifier: MIT
/**
 * `toEventFrame`: `truncated` defaults to `false`, a reported cache count of `0` survives as `0`
 * rather than `null`, and unknown or missing kinds are carried visibly.
 */
import { describe, expect, it } from "vitest";

import type { AgentStreamEvent } from "../../hooks/useAgentStream";
import { EVENT_KINDS } from "../../types/generated/agentEvents";
import { MALFORMED_KIND, toEventFrame } from "./eventFrame";

describe("toEventFrame", () => {
  it("fills every contract field a minimal frame omitted", () => {
    const frame = toEventFrame({ kind: "completed" });

    expect(frame).toEqual({
      kind: "completed",
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
      event_id: null,
      partial: false,
      cost_lines: null,
      cost_usd: null,
      cost_withheld: null,
      resolved_tiers: null,
    });
  });

  it("defaults truncated to false rather than leaving it undefined", () => {
    // `EventTrace` badges a truthy `truncated`.
    expect(toEventFrame({ kind: "tool_result" }).truncated).toBe(false);
    expect(toEventFrame({ kind: "tool_result", truncated: true }).truncated).toBe(true);
  });

  it("preserves a reported zero instead of collapsing it to null", () => {
    const frame = toEventFrame({
      kind: "usage",
      input_tokens: 0,
      output_tokens: 0,
      cache_read_tokens: 0,
    });

    expect(frame.input_tokens).toBe(0);
    expect(frame.output_tokens).toBe(0);
    expect(frame.cache_read_tokens).toBe(0);
    // Not reported at all stays null.
    expect(frame.cache_write_tokens).toBeNull();
  });

  it("carries an undeclared kind through unchanged, so it can render visibly", () => {
    // A kind this build's schema does not declare is carried through, not dropped.
    const kind = "subagent_boundary";
    expect((EVENT_KINDS as readonly string[]).includes(kind)).toBe(false);
    expect(toEventFrame({ kind }).kind).toBe(kind);
  });

  it("names a frame that carried no kind at all, rather than rendering the word undefined", () => {
    // A frame with no `kind` gets the named stand-in rather than `undefined`.
    expect(toEventFrame({} as AgentStreamEvent).kind).toBe(MALFORMED_KIND);
    expect(toEventFrame({ kind: "" }).kind).toBe(MALFORMED_KIND);
    expect(MALFORMED_KIND).not.toBe("undefined");
    // The stand-in never shadows a real kind, declared or not.
    expect((EVENT_KINDS as readonly string[]).includes(MALFORMED_KIND)).toBe(false);
  });

  it("carries an undeclared error_kind through unchanged", () => {
    // `errorHeadline` shows an unrecognised `error_kind` verbatim; that only works if it arrives.
    const frame = toEventFrame({ kind: "error", error_kind: "quota_exceeded" });
    expect(frame.error_kind).toBe("quota_exceeded");
  });

  it("passes every declared field through when the producer sent them all", () => {
    const event: AgentStreamEvent = {
      kind: "proposal",
      tool_name: "submit_proposal",
      tool_input: { field: "amount" },
      tool_output: { proposals: [] },
      truncated: true,
      proposal_id: "prop-1",
      proposal_summary: { reason: "renewal" },
      input_tokens: 11,
      output_tokens: 22,
      cache_read_tokens: 33,
      cache_write_tokens: 44,
      text: "a line",
      error_kind: "tool_failure",
      error_message: "refused",
      partial_work: "one write landed",
      event_id: "delta-1",
      partial: true,
      cost_lines: [
        {
          tier: "subagent",
          role: "research",
          model_id: "model-a",
          input_tokens: 11,
          output_tokens: 22,
          cache_read_tokens: 33,
          cache_write_tokens: 44,
          long_context_calls: 0,
          cost_usd: 0.5,
        },
      ],
      cost_usd: 0.5,
      cost_withheld: false,
      resolved_tiers: [
        { tier: "subagent", family: "openai", model_id: "model-a", effort: "low" },
      ],
    };

    expect(toEventFrame(event)).toEqual(event);
  });
});
