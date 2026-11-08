// SPDX-License-Identifier: MIT
/**
 * Tests for the relay's prompt cap: the limit, its code-point counting, and the body-level check the
 * handler runs before any Runtime call.
 */
import type { LambdaFunctionURLEvent } from "aws-lambda";
import { describe, expect, it } from "vitest";

import {
  PROMPT_MAX_CHARS,
  PROMPT_TOO_LONG_MESSAGE,
  isPromptWithinCap,
  promptLengthError,
} from "../src/requests.js";

function eventWithBody(body: string, isBase64Encoded = false): LambdaFunctionURLEvent {
  return { body, isBase64Encoded } as unknown as LambdaFunctionURLEvent;
}

function turnEvent(prompt: unknown): LambdaFunctionURLEvent {
  return eventWithBody(JSON.stringify({ prompt, session_id: "s".repeat(40) }));
}

describe("PROMPT_MAX_CHARS", () => {
  it("is 8000", () => {
    expect(PROMPT_MAX_CHARS).toBe(8000);
  });

  it("is named in the rejection message", () => {
    expect(PROMPT_TOO_LONG_MESSAGE).toContain("8000");
  });
});

describe("isPromptWithinCap", () => {
  it("accepts a prompt of exactly the cap", () => {
    expect(isPromptWithinCap("x".repeat(PROMPT_MAX_CHARS))).toBe(true);
  });

  it("refuses a prompt one character over the cap", () => {
    expect(isPromptWithinCap("x".repeat(PROMPT_MAX_CHARS + 1))).toBe(false);
  });

  it("counts an astral character once, as the Runtime does", () => {
    const atCap = "\u{1F600}".repeat(PROMPT_MAX_CHARS);
    expect(atCap.length).toBe(2 * PROMPT_MAX_CHARS);
    expect(isPromptWithinCap(atCap)).toBe(true);
    expect(isPromptWithinCap(atCap + "x")).toBe(false);
  });
});

describe("promptLengthError", () => {
  it("names the limit for an over-long prompt", () => {
    expect(promptLengthError(turnEvent("x".repeat(PROMPT_MAX_CHARS + 1)))).toBe(
      PROMPT_TOO_LONG_MESSAGE,
    );
  });

  it("finds an over-long prompt in a base64-encoded body", () => {
    const raw = JSON.stringify({ prompt: "x".repeat(PROMPT_MAX_CHARS + 1) });
    const event = eventWithBody(Buffer.from(raw, "utf8").toString("base64"), true);
    expect(promptLengthError(event)).toBe(PROMPT_TOO_LONG_MESSAGE);
  });

  it.each([
    ["a prompt at the cap", turnEvent("x".repeat(PROMPT_MAX_CHARS))],
    ["a short prompt", turnEvent("hello")],
    ["a non-string prompt", turnEvent(12345)],
    ["no body", eventWithBody("")],
    ["an unparseable body", eventWithBody("not json")],
  ])("is undefined for %s", (_label, event) => {
    expect(promptLengthError(event)).toBeUndefined();
  });
});
