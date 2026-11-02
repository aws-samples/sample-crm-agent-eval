// SPDX-License-Identifier: MIT
/**
 * Guards the Tailwind 3 defaults `tailwind.css` restores on Tailwind 4. The real entry point is
 * compiled over a fixed class list and, for each guarded selector and property, the declaration
 * that wins (last in the latest cascade layer) must be the restored value.
 * Removing a rule, moving it ahead of preflight, or putting it in the wrong layer fails here.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import tailwind from "@tailwindcss/postcss";
import postcss, { type AtRule, type Declaration, type Rule } from "postcss";
import { beforeAll, describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const entryPath = resolve(here, "tailwind.css");

/** Classes compiled for the utility checks; every guarded utility appears here. */
const CANDIDATES = "outline-none focus-visible:outline-none max-w-prose text-sm text-2xl flex";

/** Cascade order of Tailwind's layers; a later layer wins regardless of source order. */
const LAYER_ORDER = ["properties", "theme", "base", "components", "utilities"];

/** Longhand property to the declarations that set it, shorthands included. */
const SETTERS: Record<string, string[]> = {
  "border-color": ["border", "border-color"],
  "background-color": ["background", "background-color"],
  "outline-style": ["outline", "outline-style"],
  "outline-offset": ["outline-offset"],
  padding: ["padding"],
};

interface Winner {
  layer: string;
  value: string;
}

let compiled: postcss.Root;

/** The entry point with its content sources replaced by `CANDIDATES`, everything else intact. */
function entryOverCandidates(): string {
  const entry = readFileSync(entryPath, "utf8");
  const withoutSources = entry.replace(/^@source\s+["'][^"']*["']\s*;\s*$/gm, "");
  return `${withoutSources}\n@source inline(${JSON.stringify(CANDIDATES)});\n`;
}

function layerOf(rule: Rule): string {
  for (let node: postcss.Node | undefined = rule.parent; node; node = node.parent) {
    if (node.type === "atrule" && (node as AtRule).name === "layer") return (node as AtRule).params;
  }
  return "unlayered";
}

/** The winning declaration of `property` for a rule whose selector list contains `selector`. */
function winner(selector: string, property: string): Winner | undefined {
  const setters = SETTERS[property] ?? [property];
  let best: (Winner & { rank: number }) | undefined;
  compiled.walkRules((rule) => {
    if (!rule.selectors.map((s) => s.replace(/\s+/g, " ").trim()).includes(selector)) return;
    rule.walkDecls((decl: Declaration) => {
      if (decl.parent !== rule || !setters.includes(decl.prop)) return;
      const layer = layerOf(rule);
      const rank = layer === "unlayered" ? LAYER_ORDER.length : LAYER_ORDER.indexOf(layer);
      if (!best || rank >= best.rank) best = { layer, value: decl.value, rank };
    });
  });
  return best && { layer: best.layer, value: best.value };
}

beforeAll(async () => {
  const { css } = await postcss([tailwind()]).process(entryOverCandidates(), { from: entryPath });
  compiled = postcss.parse(css);
}, 30_000);

describe("preflight defaults restored in the base layer", () => {
  it.each([
    ["*", "border-color", "var(--color-gray-200, currentcolor)"],
    ["::file-selector-button", "border-color", "var(--color-gray-200, currentcolor)"],
    ["input::placeholder", "color", "var(--color-gray-400)"],
    ["textarea::placeholder", "color", "var(--color-gray-400)"],
    ['input:where(:not([type="button"], [type="reset"], [type="submit"]))', "background-color", "revert"],
    ["select", "background-color", "revert"],
    ["textarea", "background-color", "revert"],
    ["option", "padding", "revert"],
    ['[type="search"]', "appearance", "textfield"],
    ['[type="search"]', "outline-offset", "-2px"],
    ["button:not(:disabled)", "cursor", "pointer"],
    ['[role="button"]:not(:disabled)', "cursor", "pointer"],
  ])("%s wins %s: %s", (selector, property, value) => {
    expect(winner(selector, property)).toEqual({ layer: "base", value });
  });

  it("keeps the Tailwind 3 greys the defaults read", () => {
    const declared = new Map<string, string>();
    compiled.walkDecls(/^--color-gray-(200|400)$/, (decl) => {
      declared.set(decl.prop, decl.value);
    });
    expect(Object.fromEntries(declared)).toEqual({
      "--color-gray-200": "#e5e7eb",
      "--color-gray-400": "#9ca3af",
    });
  });
});

describe("utilities restored in the utilities layer", () => {
  it.each([
    [".outline-none", "outline-style", "2px solid transparent"],
    [".outline-none", "outline-offset", "2px"],
    [".focus-visible\\:outline-none:focus-visible", "outline-style", "2px solid transparent"],
    [".focus-visible\\:outline-none:focus-visible", "outline-offset", "2px"],
    [".max-w-prose", "max-width", "var(--measure-prose)"],
  ])("%s wins %s: %s", (selector, property, value) => {
    expect(winner(selector, property)).toEqual({ layer: "utilities", value });
  });

  it.each([".text-sm", ".text-2xl"])("%s sets a font size and no line height", (selector) => {
    expect(winner(selector, "font-size")?.value).toMatch(/^var\(--text-/);
    expect(winner(selector, "line-height")).toBeUndefined();
  });

  it("compiled the candidate list at all", () => {
    expect(winner(".flex", "display")).toEqual({ layer: "utilities", value: "flex" });
  });
});
