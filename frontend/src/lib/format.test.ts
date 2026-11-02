// SPDX-License-Identifier: MIT
// Tests for the shared formatters, including the contract every formatter shares: an absent
// value renders the em dash, a real zero renders as a measured zero, and a non-finite input never
// leaks "NaN"/"Invalid" into a cell.
import { describe, expect, it } from "vitest";

import { ABSENT, formatDurationMs, formatUsd } from "./format";

describe("formatDurationMs", () => {
  it("renders a whole-millisecond figure", () => {
    expect(formatDurationMs(123)).toBe("123 ms");
  });

  it("rounds a fractional millisecond to the nearest whole ms", () => {
    expect(formatDurationMs(123.4)).toBe("123 ms");
    expect(formatDurationMs(123.6)).toBe("124 ms");
  });

  it("renders a real zero as a measured 0 ms, not as absent", () => {
    // A measured zero is a measurement; only a value the wire did not carry is absent.
    expect(formatDurationMs(0)).toBe("0 ms");
  });

  it.each([null, undefined, NaN, Infinity, -Infinity])(
    "renders %s as the absent marker, never 'NaN ms'",
    (value) => {
      expect(formatDurationMs(value as number | null | undefined)).toBe(ABSENT);
    },
  );
});

describe("formatUsd", () => {
  it("keeps cents on a sub-dollar model-spend figure, where formatCurrency would round to $0", () => {
    // $0.47 must not render as "$0".
    expect(formatUsd(0.47)).toBe("$0.47");
  });

  it("keeps sub-cent precision for a tiny figure", () => {
    expect(formatUsd(0.0023)).toBe("$0.0023");
  });

  it("renders a real zero as $0.00, a measured zero rather than absent", () => {
    expect(formatUsd(0)).toBe("$0.00");
  });

  it.each([null, undefined, NaN, Infinity])(
    "renders %s as the absent marker, never $0.00",
    (value) => {
      const rendered = formatUsd(value as number | null | undefined);
      expect(rendered).toBe(ABSENT);
      expect(rendered).not.toBe("$0.00");
    },
  );
});
