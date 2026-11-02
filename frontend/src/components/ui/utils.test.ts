// SPDX-License-Identifier: MIT
/** Pins `cn()`'s conflict resolution over the token layer's custom Tailwind scale keys. */

import { describe, expect, it } from "vitest";

import * as theme from "../../styles/theme";
import { cn, focusRing } from "./utils";

describe("cn keeps custom-scale width utilities", () => {
  it.each([
    ["border border-hairline border-border-control", "border-hairline"],
    ["-mb-px border-b-emphasis border-transparent", "border-b-emphasis"],
    ["ring-focus ring-focus-ring", "ring-focus"],
    ["ring-offset-focus ring-offset-surface-base", "ring-offset-focus"],
  ])("does not drop the width from %j", (input, mustKeep) => {
    expect(cn(input).split(" ")).toContain(mustKeep);
  });

  it("keeps a width and a colour of the same property together", () => {
    const result = cn("border border-hairline border-border-control").split(" ");
    expect(result).toContain("border-hairline");
    expect(result).toContain("border-border-control");
  });
});

describe("cn lets the last custom-scale value win", () => {
  it.each([
    ["rounded-control rounded-surface", "rounded-surface", "rounded-control"],
    ["h-control-md h-row-dense", "h-row-dense", "h-control-md"],
    ["max-w-narrow max-w-prose", "max-w-prose", "max-w-narrow"],
    ["duration-fast duration-normal", "duration-normal", "duration-fast"],
    ["shadow-raised shadow-overlay", "shadow-overlay", "shadow-raised"],
    ["ease-standard ease-exit", "ease-exit", "ease-standard"],
    ["z-panel z-overlay", "z-overlay", "z-panel"],
    ["w-nav w-panel", "w-panel", "w-nav"],
    ["text-sm text-md", "text-md", "text-sm"],
  ])("resolves %j to the later value", (input, winner, loser) => {
    const result = cn(input).split(" ");
    expect(result).toContain(winner);
    expect(result).not.toContain(loser);
  });

  it("is what makes a caller's className override a variant default", () => {
    const variantDefault = "h-control-md rounded-control px-5";
    expect(cn(variantDefault, "h-control-lg rounded-surface").split(" ")).toEqual(
      expect.arrayContaining(["h-control-lg", "rounded-surface", "px-5"]),
    );
    expect(cn(variantDefault, "h-control-lg")).not.toContain("h-control-md");
  });
});

describe("cn does not confuse the font scales", () => {
  it("keeps a family and a weight together", () => {
    const result = cn("font-numeric font-medium").split(" ");
    expect(result).toContain("font-numeric");
    expect(result).toContain("font-medium");
  });

  it("resolves two weights and two families independently", () => {
    expect(cn("font-regular font-semibold")).toBe("font-semibold");
    expect(cn("font-sans font-numeric")).toBe("font-numeric");
  });
});

describe("focusRing", () => {
  it("carries a visible ring, its token width, and an offset", () => {
    expect(focusRing).toContain("focus-visible:ring-focus-ring");
    expect(focusRing).toContain("focus-visible:ring-focus");
    expect(focusRing).toContain("focus-visible:ring-offset-focus");
  });

  it("survives cn() intact", () => {
    const merged = cn(focusRing).split(" ");
    expect(merged).toContain("focus-visible:ring-focus");
    expect(merged).toContain("focus-visible:ring-focus-ring");
  });
});

/** `theme.ts` and `utils.ts` agree on the scale-key set, driven by `theme.ts`'s own exports. */
describe("theme.ts and utils.ts agree on the custom scale keys", () => {
  /** Theme scale to the utility prefix Tailwind generates for it. */
  const SCALES: ReadonlyArray<readonly [string, Record<string, unknown>, string]> = [
    ["fontSize", theme.fontSize, "text"],
    ["fontFamily", theme.fontFamily, "font"],
    ["fontWeight", theme.fontWeight, "font"],
    ["lineHeight", theme.lineHeight, "leading"],
    ["borderWidth", theme.borderWidth, "border"],
    ["ringWidth", theme.ringWidth, "ring"],
    ["ringOffsetWidth", theme.ringOffsetWidth, "ring-offset"],
    ["borderRadius", theme.borderRadius, "rounded"],
    ["boxShadow", theme.boxShadow, "shadow"],
    ["height", theme.height, "h"],
    ["width", theme.width, "w"],
    ["maxWidth", theme.maxWidth, "max-w"],
    ["transitionDuration", theme.transitionDuration, "duration"],
    ["transitionTimingFunction", theme.transitionTimingFunction, "ease"],
    ["zIndex", theme.zIndex, "z"],
    ["backgroundImage", theme.backgroundImage, "bg"],
  ];

  const cases = SCALES.flatMap(([scaleName, scale, prefix]) =>
    Object.keys(scale).map((key) => ({ scaleName, prefix, key })),
  );

  it("covers every scale this project customises", () => {
    expect(cases.length).toBeGreaterThan(40);
  });

  it.each(cases)("keeps $prefix-$key ($scaleName) instead of dropping it", ({ prefix, key }) => {
    const utility = `${prefix}-${key}`;
    expect(cn(utility)).toBe(utility);
  });

  it.each(
    SCALES.flatMap(([scaleName, scale, prefix]) => {
      const keys = Object.keys(scale);
      return keys.length < 2 ? [] : [{ scaleName, prefix, first: keys[0], last: keys[1] }];
    }),
  )("lets the later $prefix value win within $scaleName", ({ prefix, first, last }) => {
    const result = cn(`${prefix}-${first}`, `${prefix}-${last}`).split(" ");
    expect(result).toContain(`${prefix}-${last}`);
    expect(result).not.toContain(`${prefix}-${first}`);
  });
});
