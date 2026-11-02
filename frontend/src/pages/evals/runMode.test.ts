// SPDX-License-Identifier: MIT
/**
 * The mode classifier: an unrecognised mode is classified as neither `stub` nor `live`.
 */
import { describe, expect, it } from "vitest";

import { classifyRunMode } from "./runMode";

describe("classifyRunMode", () => {
  it("recognises exactly the two documented modes", () => {
    expect(classifyRunMode("stub")).toBe("stub");
    expect(classifyRunMode("live")).toBe("live");
  });

  it("classifies everything else as unrecognised rather than defaulting either way", () => {
    for (const value of [undefined, null, "", "STUB", "Live", "replay", 0, 1, true, {}, []]) {
      expect(classifyRunMode(value), `${JSON.stringify(value)} must not be read as a known mode`).toBe(
        "unrecognised",
      );
    }
  });

  it("is case-sensitive, because the wire value is an exact enum member", () => {
    expect(classifyRunMode("Stub")).toBe("unrecognised");
  });
});
