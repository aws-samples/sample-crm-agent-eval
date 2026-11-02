// SPDX-License-Identifier: MIT
/**
 * Compiles Tailwind utilities for a markup fragment through the project's own `tailwind.config.ts`,
 * for tests asserting that a class emits a rule.
 *
 * `compileTailwindUtilities(markup)` returns the utilities layer only (no preflight, no theme) for
 * the classes found in `markup`; an unknown class contributes nothing.
 */

import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import tailwind from "@tailwindcss/postcss";
import postcss from "postcss";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const configPath = join(frontendRoot, "tailwind.config.ts");

export async function compileTailwindUtilities(markup: string): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "crm-tailwind-"));
  try {
    const source = join(dir, "markup.html");
    await writeFile(source, markup, "utf8");
    const entry = [
      '@import "tailwindcss/utilities" source(none);',
      `@config ${JSON.stringify(configPath)};`,
      `@source ${JSON.stringify(source)};`,
    ].join("\n");
    const { css } = await postcss([tailwind()]).process(entry, {
      from: join(frontendRoot, "src", "test", "compileTailwind.css"),
    });
    return css;
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}
