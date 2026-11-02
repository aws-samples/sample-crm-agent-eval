// SPDX-License-Identifier: MIT
/**
 * Contract test between `index.html`'s pre-paint theme script and `AppShell`'s theme resolution.
 *
 * The inline script duplicates the storage key and fallback order. This test runs the real
 * `index.html` in jsdom against stubbed inputs and asserts the applied theme equals
 * `resolveInitialTheme()` given the same inputs.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";

import { THEME_STORAGE_KEY, resolveInitialTheme } from "./AppShell";

const indexHtmlPath = resolve(dirname(fileURLToPath(import.meta.url)), "..", "index.html");
const indexHtml = readFileSync(indexHtmlPath, "utf8");

interface BootInputs {
  readonly stored?: string | null;
  readonly storageThrows?: boolean;
  readonly prefersDark?: boolean;
  readonly matchMediaThrows?: boolean;
}

function getItem(inputs: BootInputs) {
  return (key: string): string | null => {
    if (inputs.storageThrows) throw new Error("SecurityError");
    return key === THEME_STORAGE_KEY ? (inputs.stored ?? null) : null;
  };
}

function matchMedia(inputs: BootInputs) {
  return (query: string) => {
    if (inputs.matchMediaThrows) throw new Error("not supported");
    return { matches: Boolean(inputs.prefersDark) && query.includes("dark") };
  };
}

/** The single inline (non-`src`) script in the document. */
function inlineScriptSource(): string {
  const found = indexHtml.match(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/);
  expect(found, "index.html must carry an inline boot script").not.toBeNull();
  return found?.[1] ?? "";
}

/**
 * Parses the real `index.html` with scripts enabled and returns the `data-theme` the inline script
 * applied. jsdom never fetches the module script.
 */
function runBootScript(inputs: BootInputs): string | null {
  const dom = new JSDOM(indexHtml, {
    runScripts: "dangerously",
    beforeParse(window) {
      Object.defineProperty(window, "localStorage", {
        configurable: true,
        value: { getItem: getItem(inputs) },
      });
      Object.defineProperty(window, "matchMedia", {
        configurable: true,
        value: matchMedia(inputs),
      });
    },
  });
  const applied = dom.window.document.documentElement.getAttribute("data-theme");
  dom.window.close();
  return applied;
}

/** The same inputs, applied to the module the shell uses. */
function runModule(inputs: BootInputs): string {
  const originalStorage = globalThis.localStorage;
  const originalMatchMedia = globalThis.matchMedia;
  const define = (name: string, value: unknown) =>
    Object.defineProperty(globalThis, name, { configurable: true, value });
  try {
    define("localStorage", { getItem: getItem(inputs) });
    define("matchMedia", matchMedia(inputs));
    return resolveInitialTheme();
  } finally {
    define("localStorage", originalStorage);
    define("matchMedia", originalMatchMedia);
  }
}

const SCENARIOS: ReadonlyArray<readonly [string, BootInputs, string]> = [
  ["persisted dark over a light OS", { stored: "dark", prefersDark: false }, "dark"],
  ["persisted light over a dark OS", { stored: "light", prefersDark: true }, "light"],
  ["nothing persisted, dark OS", { stored: null, prefersDark: true }, "dark"],
  ["nothing persisted, light OS", { stored: null, prefersDark: false }, "light"],
  ["a corrupt persisted value ignored", { stored: "solarized", prefersDark: true }, "dark"],
  ["storage throws, dark OS", { storageThrows: true, prefersDark: true }, "dark"],
  ["storage throws, light OS", { storageThrows: true, prefersDark: false }, "light"],
  ["neither storage nor matchMedia works", { storageThrows: true, matchMediaThrows: true }, "light"],
];

describe("index.html applies the theme before first paint", () => {
  it("puts the theme script in head, ahead of the module script", () => {
    const head = indexHtml.slice(indexHtml.indexOf("<head>"), indexHtml.indexOf("</head>"));
    expect(head).toContain("data-theme");
    expect(indexHtml.indexOf("data-theme")).toBeLessThan(indexHtml.indexOf('src="/src/main.tsx"'));
  });

  it("stays dependency-free, since it runs before the bundle", () => {
    expect(inlineScriptSource()).not.toMatch(/\bimport\b|\brequire\(/);
  });

  it("reads the same storage key the shell writes", () => {
    expect(inlineScriptSource()).toContain(THEME_STORAGE_KEY);
  });

  it.each(SCENARIOS)("resolves %s to %s", (_name, inputs, expected) => {
    expect(runBootScript(inputs)).toBe(expected);
  });
});

describe("the inline script and the shell cannot disagree", () => {
  it.each(SCENARIOS)("agrees with resolveInitialTheme for: %s", (_name, inputs) => {
    expect(runBootScript(inputs)).toBe(runModule(inputs));
  });
});
