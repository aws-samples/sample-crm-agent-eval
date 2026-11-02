// SPDX-License-Identifier: MIT
/**
 * Asserts the body text/background pair (`--text-primary` on `--surface-base` in
 * `styles/tokens.css`) meets the WCAG AA body-text contrast ratio (>= 4.5:1) in both themes,
 * computed from the declared hex values because jsdom has no paint engine. Also asserts every rule
 * in `index.css` sits in `@layer base`, so Tailwind utilities override the global focus ring.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import postcss, { type AtRule } from "postcss";
import { describe, expect, it } from "vitest";

const __dirname = dirname(fileURLToPath(import.meta.url));

function relativeLuminance(hex: string): number {
  const value = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => Number.parseInt(value.slice(i, i + 2), 16) / 255);
  const channel = (c: number): number => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrastRatio(hexA: string, hexB: string): number {
  const lumA = relativeLuminance(hexA);
  const lumB = relativeLuminance(hexB);
  const lighter = Math.max(lumA, lumB);
  const darker = Math.min(lumA, lumB);
  return (lighter + 0.05) / (darker + 0.05);
}

/**
 * Return the block of declarations for `selector`.
 */
function themeBlock(css: string, selector: string): string {
  const start = css.indexOf(`${selector} {`);
  if (start === -1) {
    throw new Error(`${selector} not found in tokens.css`);
  }
  const end = css.indexOf("\n}", start);
  return css.slice(start, end === -1 ? undefined : end);
}

function extractDeclaredColor(css: string, customProperty: string): string {
  const match = css.match(new RegExp(`${customProperty}:\\s*(#[0-9a-fA-F]{6})`));
  if (!match) {
    throw new Error(`${customProperty} not found as a literal hex value in the block searched`);
  }
  return match[1];
}

describe("design tokens meet WCAG AA contrast", () => {
  const tokensCss = readFileSync(join(__dirname, "styles", "tokens.css"), "utf-8");

  it.each([
    ["light", ":root"],
    ["dark", '[data-theme="dark"]'],
  ])("%s theme: --text-primary on --surface-base is >= 4.5:1", (_theme, selector) => {
    const block = themeBlock(tokensCss, selector);
    const textColor = extractDeclaredColor(block, "--text-primary");
    const backgroundColor = extractDeclaredColor(block, "--surface-base");

    const ratio = contrastRatio(textColor, backgroundColor);

    expect(ratio).toBeGreaterThanOrEqual(4.5);
  });
});

describe("index.css stays beneath Tailwind's utilities", () => {
  const root = postcss.parse(readFileSync(join(__dirname, "index.css"), "utf-8"));
  const nodes = root.nodes.filter((node) => node.type !== "comment");

  it("holds nothing outside `@layer base`", () => {
    expect(nodes.length).toBeGreaterThan(0);
    for (const node of nodes) {
      expect(node.type === "atrule" && `@${node.name} ${node.params}`).toBe("@layer base");
    }
  });

  it("keeps the global focus ring and the visually-hidden utility inside it", () => {
    const selectors: string[] = [];
    for (const node of nodes) {
      (node as AtRule).walkRules((rule) => {
        selectors.push(rule.selector);
      });
    }
    expect(selectors).toEqual([":focus-visible", ".visually-hidden"]);
  });
});
