// SPDX-License-Identifier: MIT
/**
 * Generate `src/generated/events.ts` from the Python SSE schema artefact that
 * `python3 -m agents.event_schema` renders from `src/agents/events.py`.
 * `scripts/check_sse_schema_parity.py` fails the build when the generated copy drifts.
 *
 * Usage, from `src/relay/`:
 *
 *     npm run gen:types
 *
 * Exits non-zero when the Python generator is unavailable or emits something this script cannot
 * render.
 */
import { spawnSync } from "node:child_process";
import { mkdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * Write `contents` to `path` so no reader can observe a partial file: write a pid-suffixed sibling
 * temp file, then rename it over the target.
 */
function writeFileAtomic(path, contents, encoding) {
  const tmp = `${path}.${process.pid}.tmp`;
  try {
    writeFileSync(tmp, contents, encoding);
    renameSync(tmp, path);
  } catch (err) {
    // Remove the staging file; gates walk this directory.
    rmSync(tmp, { force: true });
    throw err;
  }
}

const RELAY_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const REPO_ROOT = resolve(RELAY_ROOT, "..", "..");
const OUTPUT = join(RELAY_ROOT, "src", "generated", "events.ts");
const INTERPRETER = join(REPO_ROOT, ".venv", "bin", "python3");

/** Run the Python generator and return the parsed schema. */
function loadSchema() {
  const result = spawnSync(INTERPRETER, ["-m", "agents.event_schema"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    env: { ...process.env, PYTHONPATH: join(REPO_ROOT, "src") },
  });
  if (result.error !== undefined) {
    throw new Error(`cannot run ${INTERPRETER}: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(`agents.event_schema exited ${result.status}: ${result.stderr}`);
  }
  return JSON.parse(result.stdout);
}

/**
 * Render one JSON Schema property as a TypeScript type.
 *
 * An array of titled objects renders as `ReadonlyArray<Title>`, with `Title` registered in
 * `nested` as its own interface.
 */
function propertyType(name, property, kindType, errorKindType, nested) {
  if (Array.isArray(property.enum)) {
    const nullable = property.enum.includes(null);
    const alias = name === "kind" ? kindType : name === "error_kind" ? errorKindType : undefined;
    if (alias === undefined) {
      const members = property.enum.filter((value) => value !== null).map((value) => `"${value}"`);
      return nullable ? `${members.join(" | ")} | null` : members.join(" | ");
    }
    return nullable ? `${alias} | null` : alias;
  }

  const declared = Array.isArray(property.type) ? property.type : [property.type];
  const nullable = declared.includes("null");
  const scalars = declared
    .filter((value) => value !== "null")
    .map((value) => {
      switch (value) {
        case "string":
          return "string";
        case "integer":
        case "number":
          return "number";
        case "boolean":
          return "boolean";
        case "object":
          return "Record<string, unknown>";
        case "array":
          return `ReadonlyArray<${hoistObject(name, property.items, nested)}>`;
        default:
          throw new Error(`no TypeScript mapping for JSON Schema type ${value} on ${name}`);
      }
    });
  if (scalars.length === 0) {
    throw new Error(`property ${name} declares no usable type`);
  }
  return nullable ? `${scalars.join(" | ")} | null` : scalars.join(" | ");
}

/** Register an array's titled element object as an interface in `nested`; return its name. */
function hoistObject(where, items, nested) {
  const title = items?.title;
  if (items?.type !== "object" || typeof title !== "string" || items.properties === undefined) {
    throw new Error(`array property ${where} must have titled object items`);
  }
  if (!nested.has(title)) {
    nested.set(title, "");
    const fields = Object.entries(items.properties)
      .map(([field, sub]) => `  readonly ${field}: ${propertyType(field, sub, undefined, undefined, nested)};`)
      .join("\n");
    nested.set(title, `/** One \`${title}\` element of an event frame array. */\nexport interface ${title} {\n${fields}\n}\n\n`);
  }
  return title;
}

function render(schema) {
  const properties = schema.properties ?? {};
  const kinds = properties.kind?.enum;
  const errorKinds = (properties.error_kind?.enum ?? []).filter((value) => value !== null);
  if (!Array.isArray(kinds) || kinds.length === 0) {
    throw new Error("schema declares no event kinds");
  }

  const nested = new Map();
  const fields = Object.entries(properties)
    .map(([name, property]) => {
      const type = propertyType(name, property, "EventKind", "ErrorKind", nested);
      const quoted = /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name) ? name : `"${name}"`;
      return `  readonly ${quoted}: ${type};`;
    })
    .join("\n");

  const constants = schema["x-constants"] ?? {};

  return `// SPDX-License-Identifier: MIT
// GENERATED FILE — do not edit by hand.
// Regenerate with \`npm run gen:types\` in \`src/relay/\`; the source of truth is
// \`src/agents/events.py\` via \`src/agents/event_schema.py\`, and
// \`scripts/check_sse_schema_parity.py\` fails the build on drift.

/** Version of the frame contract this build was generated against. */
export const SCHEMA_VERSION = "${schema.schema_version}";

/** Prefix and delimiter of one SSE frame, as \`format_sse_line\` emits them. */
export const FRAME_PREFIX = ${JSON.stringify(schema["x-frame-prefix"])};
export const FRAME_DELIMITER = ${JSON.stringify(schema["x-frame-delimiter"])};

/** Marker a truncated tool result carries in place of the records it could not include. */
export const TRUNCATION_MARKER = ${JSON.stringify(constants.truncation_marker ?? "")};

/** Byte ceiling a tool-result payload is truncated at. */
export const TOOL_RESULT_BYTE_CEILING = ${JSON.stringify(constants.tool_result_byte_ceiling ?? 0)};

/** The closed set of event kinds. */
export const EVENT_KINDS = [${kinds.map((kind) => `"${kind}"`).join(", ")}] as const;
export type EventKind = (typeof EVENT_KINDS)[number];

/** The closed set of terminal error kinds, carried inside an \`error\` event. */
export const ERROR_KINDS = [${errorKinds.map((kind) => `"${kind}"`).join(", ")}] as const;
export type ErrorKind = (typeof ERROR_KINDS)[number];

${[...nested.values()].join("")}/** One agent event, as one JSON object on one SSE data frame. */
export interface AgentEventFrame {
${fields}
}

/** Whether \`value\` is one of the kinds this schema version declares. */
export function isEventKind(value: unknown): value is EventKind {
  return typeof value === "string" && (EVENT_KINDS as readonly string[]).includes(value);
}

/** Whether \`value\` is one of the error kinds this schema version declares. */
export function isErrorKind(value: unknown): value is ErrorKind {
  return typeof value === "string" && (ERROR_KINDS as readonly string[]).includes(value);
}
`;
}

function main() {
  const output = render(loadSchema());
  mkdirSync(dirname(OUTPUT), { recursive: true });
  writeFileAtomic(OUTPUT, output, "utf8");
  process.stdout.write(`${OUTPUT}\n`);
}

main();
