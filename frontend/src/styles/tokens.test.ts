// SPDX-License-Identifier: MIT
/**
 * Mechanical assertions over the design token layer:
 *   1. The Tailwind theme references CSS variables, never literal values, and every variable it
 *      references exists in `tokens.css`.
 *   2. Every semantic colour pair holds its WCAG contrast floor in both themes.
 *   3. The semantic states are distinct, and pending and diff are distinguished by more than
 *      colour alone.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import tailwindConfig from "../../tailwind.config";
import { screens, themeExtension } from "./theme";

const here = dirname(fileURLToPath(import.meta.url));
const tokensCss = readFileSync(resolve(here, "tokens.css"), "utf8");
const tailwindCss = readFileSync(resolve(here, "tailwind.css"), "utf8");

/** Extracts the custom-property declarations of one rule block, by its selector. */
function tokenBlock(selector: string): Map<string, string> {
  const start = tokensCss.indexOf(`${selector} {`);
  if (start === -1) throw new Error(`tokens.css has no \`${selector}\` block`);
  const end = tokensCss.indexOf("\n}", start);
  const body = tokensCss.slice(start, end);
  const declarations = new Map<string, string>();
  for (const match of body.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
    declarations.set(match[1], match[2].trim());
  }
  return declarations;
}

const lightTokens = tokenBlock(":root");
const darkOverrides = tokenBlock('[data-theme="dark"]');
/** Dark inherits every token it does not override. */
const darkTokens = new Map([...lightTokens, ...darkOverrides]);

const THEMES: ReadonlyArray<readonly [string, Map<string, string>]> = [
  ["light", lightTokens],
  ["dark", darkTokens],
];

function relativeLuminance(hex: string): number {
  const value = hex.replace("#", "");
  const channels = [0, 2, 4].map((offset) => {
    const srgb = parseInt(value.slice(offset, offset + 2), 16) / 255;
    return srgb <= 0.03928 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

/** WCAG 2.1 contrast ratio between two opaque hex colours. */
function contrastRatio(a: string, b: string): number {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function colourOf(tokens: Map<string, string>, name: string): string {
  const value = tokens.get(`--${name}`);
  if (value === undefined) throw new Error(`tokens.css defines no --${name}`);
  if (!/^#[0-9a-f]{6}$/.test(value)) {
    throw new Error(`--${name} is \`${value}\`; contrast assertions need a 6-digit hex`);
  }
  return value;
}

const SURFACES = [
  "surface-sunken",
  "surface-base",
  "surface-raised",
  "surface-overlay",
  "surface-inset",
  "surface-hover",
] as const;

const STATES = ["denied", "danger", "caution", "positive", "info", "empty", "pending"] as const;

/** WCAG 1.4.3 for text; 1.4.11 for control boundaries and chart marks. */
const TEXT_FLOOR = 4.5;
const NON_TEXT_FLOOR = 3;

describe("Tailwind theme maps onto the CSS variables", () => {
  it("references a var() for every scalar theme value, never a literal", () => {
    const literals: string[] = [];
    for (const [group, scaleValue] of Object.entries(themeExtension)) {
      if (group === "screens") continue; // resolved at config time; asserted separately below
      for (const [key, value] of Object.entries(scaleValue as Record<string, unknown>)) {
        const rendered = Array.isArray(value) ? value.join(",") : String(value);
        if (!rendered.includes("var(--")) literals.push(`${group}.${key} = ${rendered}`);
      }
    }
    expect(literals).toEqual([]);
  });

  it("references only variables that tokens.css actually defines", () => {
    const referenced = new Set<string>();
    for (const [group, scaleValue] of Object.entries(themeExtension)) {
      if (group === "screens") continue;
      for (const value of Object.values(scaleValue as Record<string, unknown>)) {
        const rendered = Array.isArray(value) ? value.join(",") : String(value);
        for (const match of rendered.matchAll(/var\((--[a-z0-9-]+)\)/g)) referenced.add(match[1]);
      }
    }
    expect(referenced.size).toBeGreaterThan(80);
    const undefinedTokens = [...referenced].filter((name) => !lightTokens.has(name)).sort();
    expect(undefinedTokens).toEqual([]);
  });

  it("keeps Tailwind screens in step with the breakpoint tokens", () => {
    for (const [name, value] of Object.entries(screens)) {
      expect(colourOrRaw(lightTokens, `breakpoint-${name}`)).toBe(value);
    }
  });

  it("is wired into the config the build consumes", () => {
    expect(tailwindConfig.theme.extend).toBe(themeExtension);
    expect(tailwindConfig.darkMode).toEqual(["class", '[data-theme="dark"]']);
    expect(tailwindCss).toContain('@config "../../tailwind.config.ts";');
    expect(tailwindCss).toContain('@source "../**/*.{ts,tsx,jsx}";');
  });
});

function colourOrRaw(tokens: Map<string, string>, name: string): string {
  const value = tokens.get(`--${name}`);
  if (value === undefined) throw new Error(`tokens.css defines no --${name}`);
  return value;
}

describe.each(THEMES)("%s theme contrast", (themeName, tokens) => {
  const ratio = (a: string, b: string) => contrastRatio(colourOf(tokens, a), colourOf(tokens, b));

  it.each(["text-primary", "text-secondary", "text-muted"])(
    "%s holds AA against every surface",
    (text) => {
      for (const surface of SURFACES) {
        expect(ratio(text, surface), `${text} on ${surface} (${themeName})`).toBeGreaterThanOrEqual(
          TEXT_FLOOR,
        );
      }
    },
  );

  it("holds AA for inverse and on-accent text", () => {
    expect(ratio("text-inverse", "surface-inverse")).toBeGreaterThanOrEqual(TEXT_FLOOR);
    expect(ratio("text-on-accent", "accent-emphasis")).toBeGreaterThanOrEqual(TEXT_FLOOR);
    expect(ratio("text-on-accent", "accent-emphasis-hover")).toBeGreaterThanOrEqual(TEXT_FLOOR);
  });

  it("holds AA for link and accent text on the surfaces they sit on", () => {
    for (const surface of ["surface-base", "surface-raised", "accent-surface"] as const) {
      expect(ratio("text-link", surface)).toBeGreaterThanOrEqual(TEXT_FLOOR);
      expect(ratio("accent-text", surface)).toBeGreaterThanOrEqual(TEXT_FLOOR);
    }
  });

  it.each(STATES)("%s text holds AA on its own surface and on the page surfaces", (state) => {
    for (const surface of [`${state}-surface`, "surface-base", "surface-raised"]) {
      expect(
        ratio(`${state}-text`, surface),
        `${state}-text on ${surface} (${themeName})`,
      ).toBeGreaterThanOrEqual(TEXT_FLOOR);
    }
  });

  it("holds AA for both sides of the approvals diff", () => {
    for (const side of ["removed", "added"] as const) {
      expect(ratio(`diff-${side}-text`, `diff-${side}-surface`)).toBeGreaterThanOrEqual(TEXT_FLOOR);
      expect(ratio(`diff-${side}-text`, "surface-raised")).toBeGreaterThanOrEqual(TEXT_FLOOR);
    }
  });

  it("holds 1.4.11 for control boundaries and the focus ring", () => {
    for (const surface of ["surface-base", "surface-raised", "surface-inset"] as const) {
      expect(ratio("border-control", surface)).toBeGreaterThanOrEqual(NON_TEXT_FLOOR);
      expect(ratio("border-strong", surface)).toBeGreaterThanOrEqual(NON_TEXT_FLOOR);
      expect(ratio("focus-ring-color", surface)).toBeGreaterThanOrEqual(NON_TEXT_FLOOR);
    }
    expect(ratio("pending-border", "pending-surface")).toBeGreaterThanOrEqual(NON_TEXT_FLOOR);
  });

  it("keeps each model-family series mark visible against the page", () => {
    for (const surface of ["surface-base", "surface-raised"] as const) {
      expect(ratio("series-1", surface)).toBeGreaterThanOrEqual(NON_TEXT_FLOOR);
      expect(ratio("series-2", surface)).toBeGreaterThanOrEqual(NON_TEXT_FLOOR);
    }
  });
});

describe.each(THEMES)("%s theme keeps semantic states distinct", (_themeName, tokens) => {
  it("gives denied, danger, caution, positive, empty and pending different surfaces", () => {
    const surfaces = STATES.map((state) => colourOf(tokens, `${state}-surface`));
    expect(new Set(surfaces).size).toBe(STATES.length);
  });

  it("does not render a scope denial as an error", () => {
    expect(colourOf(tokens, "denied-text")).not.toBe(colourOf(tokens, "danger-text"));
    expect(colourOf(tokens, "denied-surface")).not.toBe(colourOf(tokens, "danger-surface"));
  });

  it("does not render an empty result as a denial or a caution", () => {
    expect(colourOf(tokens, "empty-surface")).not.toBe(colourOf(tokens, "denied-surface"));
    expect(colourOf(tokens, "empty-surface")).not.toBe(colourOf(tokens, "caution-surface"));
  });
});

describe("states carry a second channel beyond colour", () => {
  it("marks a pending card with a border style and a hatch, not hue alone", () => {
    expect(lightTokens.get("--pending-border-style")).toBe("dashed");
    expect(lightTokens.get("--pending-hatch")).toContain("repeating-linear-gradient");
    expect(darkOverrides.get("--pending-hatch")).toContain("repeating-linear-gradient");
  });

  /* The two model families are distinguished by a fill and a dash pattern, not by luminance. */
  it("distinguishes the two model-family series by pattern, not by weight", () => {
    expect(colourOf(lightTokens, "series-1")).not.toBe(colourOf(lightTokens, "series-2"));
    expect(lightTokens.get("--series-1-fill")).not.toBe(lightTokens.get("--series-2-fill"));
    expect(lightTokens.get("--series-2-fill")).toContain("repeating-linear-gradient");
    expect(lightTokens.get("--series-1-dash")).not.toBe(lightTokens.get("--series-2-dash"));
  });

  it("distinguishes the diff sides by decoration and marker glyph as well as colour", () => {
    expect(lightTokens.get("--diff-removed-decoration")).toBe("line-through");
    expect(lightTokens.get("--diff-added-decoration")).toBe("underline");
    expect(lightTokens.get("--diff-removed-marker")).not.toBe(
      lightTokens.get("--diff-added-marker"),
    );
  });
});

describe("token vocabulary is semantic and complete", () => {
  it("names no token after a literal colour or a numeric ramp step", () => {
    const literalNames = [...lightTokens.keys()].filter((name) =>
      /--(grey|gray|blue|red|green|yellow|purple|black|white|slate|zinc|neutral)-?\d*$/.test(name),
    );
    expect(literalNames).toEqual([]);
  });

  it("covers all six axes the token layer is required to define", () => {
    const axes: Record<string, RegExp> = {
      colour: /^--surface-/,
      type: /^--text-(3xl|base)$/,
      spacing: /^--space-6$/,
      radius: /^--radius-surface$/,
      elevation: /^--elevation-raised$/,
      motion: /^--duration-normal$/,
    };
    for (const [axis, pattern] of Object.entries(axes)) {
      expect([...lightTokens.keys()].some((name) => pattern.test(name)), axis).toBe(true);
    }
  });

  it("declares no px font size or px spacing value", () => {
    const offenders: string[] = [];
    for (const [name, value] of lightTokens) {
      if (/^--(text|space|radius|row-height|control-height|layout|measure|breakpoint)/.test(name)) {
        if (/\d+px/.test(value)) offenders.push(`${name}: ${value}`);
      }
    }
    // `--radius-pill: 9999px` is the idiomatic full-round sentinel, not a scale value.
    expect(offenders).toEqual(["--radius-pill: 9999px"]);
  });

  it("docks a wide assistant panel as a viewport clamp in rem, never a fixed px width", () => {
    const panelWidth = lightTokens.get("--layout-panel-width");
    expect(panelWidth).toBeDefined();
    expect(panelWidth).toMatch(/^clamp\(/);
    expect(panelWidth).toContain("rem");
    expect(panelWidth).not.toMatch(/\d+px/);
  });

  it("collapses every duration token under a reduced-motion preference", () => {
    const reducedMotion = tokensCss.slice(tokensCss.indexOf("prefers-reduced-motion"));
    for (const name of ["instant", "fast", "normal", "slow"]) {
      expect(reducedMotion).toContain(`--duration-${name}: 1ms;`);
    }
  });
});
