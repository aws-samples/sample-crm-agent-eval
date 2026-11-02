// SPDX-License-Identifier: MIT
/**
 * Coverage of the Tailwind 3 sibling-spacing rules in `tailwind.css`: every `space-y-*`,
 * `divide-y` and `divide-border-*` class used in component source must have its previous-sibling
 * rule there, and each its cancelling `:where()` reset, or that class silently takes Tailwind 4's
 * next-sibling spacing.
 */

import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const srcRoot = resolve(here, "..");
const tailwindCss = readFileSync(resolve(here, "tailwind.css"), "utf8");

/** Every non-test `.ts`/`.tsx` file under `src/`, generated types excluded. */
function componentSources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "generated" ? [] : componentSources(path);
    return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

function classesUsed(pattern: RegExp): string[] {
  const found = new Set<string>();
  for (const file of componentSources(srcRoot)) {
    for (const match of readFileSync(file, "utf8").matchAll(pattern)) found.add(match[1]);
  }
  return [...found].sort();
}

const SIBLING_SELECTOR = " > :not([hidden]) ~ :not([hidden]) {";

describe("Tailwind 3 sibling spacing", () => {
  const spaceY = classesUsed(/(?<![\w:-])(space-y-\d+)(?![\w-])/g);
  const divide = classesUsed(/(?<![\w:-])(divide-(?:y|border-[a-z-]+))(?![\w-])/g);

  it("finds the classes it guards", () => {
    expect(spaceY.length).toBeGreaterThan(0);
    expect(divide).toContain("divide-y");
  });

  it.each(spaceY)("%s keeps its previous-sibling margin and its reset", (cls) => {
    expect(tailwindCss).toContain(`.${cls}${SIBLING_SELECTOR}`);
    expect(tailwindCss).toContain(`.${cls} > :not(:last-child)`);
  });

  it.each(divide)("%s keeps its previous-sibling rule and its reset", (cls) => {
    expect(tailwindCss).toContain(`.${cls}${SIBLING_SELECTOR}`);
    expect(tailwindCss).toContain(`:where(.${cls} > :not(:last-child))`);
  });

  it("would flag a class with no rule", () => {
    expect(tailwindCss).not.toContain(`.space-y-12${SIBLING_SELECTOR}`);
  });
});
