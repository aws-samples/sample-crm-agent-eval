#!/usr/bin/env node
// SPDX-License-Identifier: MIT
/**
 * Generate `src/types/generated/toolSchemas.ts` from the Python single source of truth
 * (`src/crm/toolspec/definitions.py`) via `frontend/scripts/export_tool_schemas.py`.
 *
 * Pipeline: Python renders every `ToolDefinition.input_schema` as JSON Schema + a metadata
 * sidecar -> `json-schema-to-typescript` compiles the schemas to TS interfaces -> this script
 * appends the generated `ToolName` union and `TOOL_METADATA` const, then writes one file.
 *
 * The generated file is never hand-edited — regenerate with `npm run gen:types`, which
 * `npm run typecheck` and `npm run test` both need to have run first.
 */
import { compile } from "json-schema-to-typescript";
import { execFileSync } from "node:child_process";
import { mkdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, "..", "..");
const outDir = resolve(__dirname, "..", "src", "types", "generated");
const outFile = resolve(outDir, "toolSchemas.ts");

/**
 * Write `contents` to `path` so no reader can observe a partial file.
 *
 * Writes a pid-suffixed sibling temp file and renames it over the target, which is atomic within
 * one filesystem.
 */
function writeFileAtomic(path, contents, encoding) {
  const tmp = `${path}.${process.pid}.tmp`;
  try {
    writeFileSync(tmp, contents, encoding);
    renameSync(tmp, path);
  } catch (err) {
    // Never leave staging debris next to a generated file; gates walk this directory.
    rmSync(tmp, { force: true });
    throw err;
  }
}

function runPythonExporter() {
  const venvPython = resolve(repoRoot, ".venv", "bin", "python3");
  const pythonBin = existsExecutable(venvPython) ? venvPython : "python3";
  const raw = execFileSync(pythonBin, [resolve(__dirname, "export_tool_schemas.py")], {
    cwd: repoRoot,
    encoding: "utf-8",
    maxBuffer: 10 * 1024 * 1024,
  });
  return JSON.parse(raw);
}

function existsExecutable(path) {
  try {
    execFileSync("test", ["-x", path]);
    return true;
  } catch {
    return false;
  }
}

async function main() {
  const { schemas, meta } = runPythonExporter();

  const compiledParts = [];
  for (const [typeName, schema] of Object.entries(schemas)) {
    const ts = await compile(schema, typeName, {
      bannerComment: "",
      additionalProperties: false,
      style: { semi: true, singleQuote: false },
    });
    compiledParts.push(ts.trim());
  }

  const toolNames = Object.keys(meta).sort();
  const toolNameUnion = toolNames.map((name) => `  | "${name}"`).join("\n");

  const metaEntries = toolNames
    .map((name) => {
      const m = meta[name];
      const agents = m.agents.map((a) => `"${a}"`).join(", ");
      return `  "${name}": {
    toolName: "${m.toolName}",
    display_label: ${JSON.stringify(m.display_label)},
    typeName: "${m.typeName}",
    risk: "${m.risk}",
    agents: [${agents}] as const,
    mutates: ${m.mutates},
  },`;
    })
    .join("\n");

  const banner = `// SPDX-License-Identifier: MIT
/**
 * GENERATED FILE — do not hand-edit.
 *
 * Source of truth: src/crm/toolspec/definitions.py.
 * Regenerate with: npm run gen:types
 */
`;

  const unionBlock = `export type ToolName =\n${toolNameUnion};\n`;

  const riskUnion = `export type RiskClass = "read" | "low_risk_write" | "proposal" | "approval";\n`;

  const metaInterface = `export interface ToolMetadata {
  readonly toolName: ToolName;
  readonly display_label: string;
  readonly typeName: string;
  readonly risk: RiskClass;
  readonly agents: readonly string[];
  readonly mutates: boolean;
}
`;

  const metaBlock = `export const TOOL_METADATA: Readonly<Record<ToolName, ToolMetadata>> = {\n${metaEntries}\n};\n`;

  const output = [
    banner,
    riskUnion,
    unionBlock,
    metaInterface,
    metaBlock,
    ...compiledParts,
    "",
  ].join("\n");

  mkdirSync(outDir, { recursive: true });
  writeFileAtomic(outFile, output, "utf-8");
  console.log(`generated ${outFile} (${toolNames.length} tools)`);
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
