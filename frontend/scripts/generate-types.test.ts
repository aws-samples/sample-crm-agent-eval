// SPDX-License-Identifier: MIT
/**
 * Tests for `generate-types.mjs`, the generator behind `toolSchemas.ts`.
 *
 * Reads the generated file, so `npm run gen:types` must have run first.
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const TOOL_SCHEMAS = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "src",
  "types",
  "generated",
  "toolSchemas.ts",
);

describe("the generated toolSchemas.ts consumer", () => {
  it("carries the SPDX licence header as its first line", () => {
    const [firstLine] = readFileSync(TOOL_SCHEMAS, "utf8").split("\n");

    expect(firstLine).toBe("// SPDX-License-Identifier: MIT");
  });

  it("is marked as generated so nobody hand-edits it", () => {
    const source = readFileSync(TOOL_SCHEMAS, "utf8");

    expect(source).toContain("GENERATED FILE — do not hand-edit.");
    expect(source).toContain("npm run gen:types");
  });
});
