// SPDX-License-Identifier: MIT
/**
 * Generate the frontend's two agent-contract consumers from their Python source artefacts:
 *
 *   src/agents/event_schema.py        -> src/types/generated/agentEvents.ts
 *   src/crm/toolspec/page_context.py  -> src/types/generated/pageContext.ts
 *
 * `src/relay/scripts/gen-events.mjs` generates the relay's copy of the same SSE contract from the
 * same artefact. Every event kind, error kind, `PageContext` variant and field name is read from
 * the JSON Schema the Python generators render. `scripts/check_sse_schema_parity.py` and
 * `scripts/check_page_context_no_rows.py` fail the build when a consumer drifts from its schema.
 *
 * Usage, from `frontend/`:
 *
 *     npm run gen:types
 *
 * Event kinds and `PageContext` kinds are emitted as runtime `as const` arrays so an unrecognised
 * kind can be detected at runtime.
 *
 * Exits non-zero when a Python generator is unavailable or emits a construct this script cannot
 * render; an unmapped JSON Schema type is an error, never a skipped property.
 */
import { spawnSync } from "node:child_process";
import { mkdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

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

const FRONTEND_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const REPO_ROOT = resolve(FRONTEND_ROOT, "..");
const OUT_DIR = join(FRONTEND_ROOT, "src", "types", "generated");
const INTERPRETER = join(REPO_ROOT, ".venv", "bin", "python3");

const BANNER = `// SPDX-License-Identifier: MIT
// GENERATED FILE — do not edit by hand.
// Regenerate with \`npm run gen:types\` in \`frontend/\`.`;

/**
 * Run one Python schema generator as a module and return its parsed stdout.
 *
 * @param {string} moduleName Importable module exposing a `__main__` that writes JSON to stdout.
 * @returns {Record<string, unknown>} The parsed JSON Schema.
 */
function loadSchema(moduleName) {
  const result = spawnSync(INTERPRETER, ["-m", moduleName], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    maxBuffer: 10 * 1024 * 1024,
    env: { ...process.env, PYTHONPATH: join(REPO_ROOT, "src") },
  });
  if (result.error !== undefined) {
    throw new Error(`cannot run ${INTERPRETER}: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(`${moduleName} exited ${result.status}: ${result.stderr}`);
  }
  return JSON.parse(result.stdout);
}

/**
 * Map one JSON Schema type name to a TypeScript type.
 *
 * @param {string} declared JSON Schema type name.
 * @param {string} where Property path, for the error message.
 * @returns {string} The TypeScript type.
 */
function scalarType(declared, where) {
  switch (declared) {
    case "string":
      return "string";
    case "integer":
    case "number":
      return "number";
    case "boolean":
      return "boolean";
    default:
      throw new Error(
        `no TypeScript mapping for JSON Schema type "${declared}" at ${where}. ` +
          "Add one deliberately — an unmapped type is a hard failure, never an omitted field.",
      );
  }
}

/** Render `"a" | "b"` from a list of literal values. */
function literalUnion(values) {
  return values.map((value) => JSON.stringify(value)).join(" | ");
}

// ---------------------------------------------------------------------------------------------
// agentEvents.ts — the SSE frame contract
// ---------------------------------------------------------------------------------------------

/**
 * Render one event-frame property as a TypeScript type.
 *
 * An array of titled objects renders as `ReadonlyArray<Title>`, with `Title` registered in
 * `nested` as its own interface.
 *
 * @param {string} name Property name.
 * @param {Record<string, any>} property Its subschema.
 * @param {Map<string, string>} nested Accumulator of `name -> rendered interface`.
 * @param {boolean} topLevel Whether `name` is a frame field, so `kind`/`error_kind` take aliases.
 * @returns {string} The TypeScript type.
 */
function eventPropertyType(name, property, nested, topLevel = true) {
  if (Array.isArray(property.enum)) {
    const nullable = property.enum.includes(null);
    const members = property.enum.filter((value) => value !== null);
    const alias = !topLevel
      ? undefined
      : name === "kind"
        ? "EventKind"
        : name === "error_kind"
          ? "ErrorKind"
          : undefined;
    const rendered = alias ?? literalUnion(members);
    return nullable ? `${rendered} | null` : rendered;
  }

  const declared = Array.isArray(property.type) ? property.type : [property.type];
  const nullable = declared.includes("null");
  const scalars = declared
    .filter((value) => value !== "null")
    .map((value) => {
      if (value === "object") return "Record<string, unknown>";
      if (value === "array") return `ReadonlyArray<${hoistEventElement(name, property.items, nested)}>`;
      return scalarType(value, `agentEvents.${name}`);
    });
  if (scalars.length === 0) {
    throw new Error(`property ${name} declares no usable type`);
  }
  return nullable ? `${scalars.join(" | ")} | null` : scalars.join(" | ");
}

/**
 * Register an event-frame array's titled element object as an interface; return its name.
 *
 * @param {string} where Property path, for error messages.
 * @param {Record<string, any>} items The array's `items` subschema.
 * @param {Map<string, string>} nested Accumulator of `name -> rendered interface`.
 * @returns {string} The interface name to reference.
 */
function hoistEventElement(where, items, nested) {
  const title = items?.title;
  if (items?.type !== "object" || typeof title !== "string" || items.properties === undefined) {
    throw new Error(`agentEvents.${where} is an array without titled object items`);
  }
  if (!nested.has(title)) {
    nested.set(title, "");
    const fields = Object.entries(items.properties)
      .map(([field, sub]) => `  readonly ${field}: ${eventPropertyType(field, sub, nested, false)};`)
      .join("\n");
    nested.set(
      title,
      `/** One \`${title}\` element of an event frame array. */\nexport interface ${title} {\n${fields}\n}\n\n`,
    );
  }
  return title;
}

/**
 * Render `agentEvents.ts` from the SSE schema.
 *
 * @param {Record<string, any>} schema The artefact `agents.event_schema` renders.
 * @returns {string} TypeScript source.
 */
function renderAgentEvents(schema) {
  const properties = schema.properties ?? {};
  const kinds = properties.kind?.enum;
  if (!Array.isArray(kinds) || kinds.length === 0) {
    throw new Error("SSE schema declares no event kinds");
  }
  const errorKinds = (properties.error_kind?.enum ?? []).filter((value) => value !== null);
  const version = schema.schema_version;
  if (typeof version !== "string" || version === "") {
    throw new Error("SSE schema declares no schema_version");
  }

  const nested = new Map();
  const fields = Object.entries(properties)
    .map(([name, property]) => {
      const quoted = /^[A-Za-z_$][\w$]*$/.test(name) ? name : JSON.stringify(name);
      return `  readonly ${quoted}: ${eventPropertyType(name, property, nested)};`;
    })
    .join("\n");

  const constants = schema["x-constants"] ?? {};

  return `${BANNER}
// Source of truth: \`src/agents/events.py\` via \`src/agents/event_schema.py\`.
// \`scripts/check_sse_schema_parity.py\` fails the build on drift. The relay generates its own copy
// of this same contract from this same artefact (\`src/relay/src/generated/events.ts\`).

/** Version of the frame contract this build was generated against. */
export const SCHEMA_VERSION = ${JSON.stringify(version)};

/** Prefix and delimiter of one SSE frame, as \`format_sse_line\` emits them. */
export const FRAME_PREFIX = ${JSON.stringify(schema["x-frame-prefix"] ?? "")};
export const FRAME_DELIMITER = ${JSON.stringify(schema["x-frame-delimiter"] ?? "")};

/** Marker a truncated tool result carries in place of the records it could not include. */
export const TRUNCATION_MARKER = ${JSON.stringify(constants.truncation_marker ?? "")};

/** Byte ceiling a tool-result payload is truncated at. */
export const TOOL_RESULT_BYTE_CEILING = ${JSON.stringify(constants.tool_result_byte_ceiling ?? 0)};

/**
 * The closed set of event kinds, as a runtime array.
 *
 * A runtime set rather than a bare type union so the panel can detect a kind this build does not
 * know and render it visibly instead of dropping it.
 */
export const EVENT_KINDS = [${kinds.map((kind) => JSON.stringify(kind)).join(", ")}] as const;
export type EventKind = (typeof EVENT_KINDS)[number];

/** The closed set of terminal error kinds, carried inside an \`error\` event — never a peer of them. */
export const ERROR_KINDS = [${errorKinds.map((kind) => JSON.stringify(kind)).join(", ")}] as const;
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

// ---------------------------------------------------------------------------------------------
// pageContext.ts — the view descriptor that cannot carry rows
// ---------------------------------------------------------------------------------------------

/**
 * Render one `PageContext` property as a TypeScript type.
 *
 * Rejects any array-typed property, so no member of the union can hold a retrieved row.
 *
 * @param {string} where Property path, for error messages.
 * @param {Record<string, any>} property Its subschema.
 * @returns {string} The TypeScript type.
 */
function pageContextPropertyType(where, property, nested) {
  if (Array.isArray(property.enum)) {
    return literalUnion(property.enum);
  }

  const declared = Array.isArray(property.type) ? property.type : [property.type];
  if (declared.includes("array")) {
    throw new Error(
      `${where} declares an array type. PageContext carries no collections; a row ` +
        "collection must not be representable in it.",
    );
  }
  const optional = declared.includes("null");
  const rendered = declared
    .filter((value) => value !== "null")
    .map((value) => {
      if (value !== "object") {
        return scalarType(value, where);
      }
      if (property.properties !== undefined) {
        // Nested descriptors carry their Python class name as `title` and are hoisted into
        // named interfaces.
        return hoistObject(where, property, nested);
      }
      const additional = property.additionalProperties;
      if (additional === undefined || additional === true) {
        throw new Error(
          `${where} is an unconstrained object. Declare its value type so a record cannot be ` +
            "assigned to it.",
        );
      }
      return `Readonly<Record<string, ${pageContextPropertyType(`${where}[*]`, additional, nested)}>>`;
    });
  // `integer` and `number` both map to TS `number`, so a schema declaring both would otherwise
  // render `number | number`.
  const unique = rendered.filter((value, index, all) => all.indexOf(value) === index);
  if (unique.length === 0) {
    throw new Error(`${where} declares no usable type`);
  }
  return optional ? `${unique.join(" | ")} | null` : unique.join(" | ");
}

/**
 * Register a titled nested object as its own interface and return the interface name.
 *
 * @param {string} where Property path, for error messages.
 * @param {Record<string, any>} property The object subschema.
 * @param {Map<string, string>} nested Accumulator of `name -> rendered interface`.
 * @returns {string} The interface name to reference.
 */
function hoistObject(where, property, nested) {
  const name = property.title;
  if (typeof name !== "string" || name === "") {
    throw new Error(
      `${where} is a nested object with no \`title\`, so it cannot be given a name. Every nested ` +
        "descriptor in the schema is rendered from a dataclass and carries its class name.",
    );
  }
  if (!nested.has(name)) {
    // Reserve the name before recursing so a self-referential schema cannot loop forever.
    nested.set(name, "");
    const required = new Set(property.required ?? []);
    const fields = Object.entries(property.properties)
      .map(([field, sub]) => {
        const type = pageContextPropertyType(`${name}.${field}`, sub, nested);
        const isOptional = !required.has(field) || type.endsWith(" | null");
        return `  readonly ${field}${isOptional ? "?" : ""}: ${type};`;
      })
      .join("\n");
    nested.set(name, `export interface ${name} {\n${fields}\n}`);
  }
  return name;
}

/**
 * Render `pageContext.ts` from the `PageContext` schema.
 *
 * @param {Record<string, any>} schema The artefact `crm.toolspec.page_context` renders.
 * @returns {string} TypeScript source.
 */
function renderPageContext(schema) {
  const variants = schema.oneOf;
  if (!Array.isArray(variants) || variants.length === 0) {
    throw new Error("PageContext schema declares no variants");
  }
  const version = schema["x-schema-version"];
  if (typeof version !== "string" || version === "") {
    throw new Error("PageContext schema declares no x-schema-version");
  }

  const kinds = [];
  const interfaces = [];
  const nested = new Map();
  const entityTypes = [];
  for (const variant of variants) {
    const kind = variant.properties?.kind?.const;
    const name = variant.title;
    if (typeof kind !== "string" || typeof name !== "string") {
      throw new Error("every PageContext variant needs a `kind` const and a title");
    }
    kinds.push(kind);
    for (const value of variant.properties?.entity_type?.enum ?? []) {
      if (!entityTypes.includes(value)) {
        entityTypes.push(value);
      }
    }

    const required = new Set(variant.required ?? []);
    const fields = Object.entries(variant.properties)
      .map(([field, property]) => {
        if (field === "kind") {
          return `  readonly kind: ${JSON.stringify(kind)};`;
        }
        const type =
          field === "entity_type"
            ? "EntityType"
            : pageContextPropertyType(`${name}.${field}`, property, nested);
        const optional = required.has(field) && !type.endsWith(" | null") ? "" : "?";
        return `  readonly ${field}${optional}: ${type};`;
      })
      .join("\n");

    interfaces.push(`export interface ${name} {\n${fields}\n}`);
  }

  if (entityTypes.length === 0) {
    throw new Error("PageContext schema declares no entity_type vocabulary");
  }
  const union = variants.map((variant) => variant.title).join(" | ");
  const nestedBlock = [...nested.values()].join("\n\n");

  return `${BANNER}
// Source of truth: \`src/crm/toolspec/page_context.py\`.
//
// This union has NO field of array-of-record type: the panel tells the agent what the user is
// *looking at*, never what the browser *retrieved*, so no row reaches agent context on the
// browser's read. \`scripts/check_page_context_no_rows.py\` enforces it, and this generator
// refuses to render an array-typed property at all.

/** Version of the descriptor contract this build was generated against. */
export const PAGE_CONTEXT_SCHEMA_VERSION = ${JSON.stringify(version)};

/** Entity tables a descriptor may name. Mirrors \`crm.toolspec.definitions.TABLES\`. */
export const ENTITY_TYPES = [${entityTypes.map((value) => JSON.stringify(value)).join(", ")}] as const;
export type EntityType = (typeof ENTITY_TYPES)[number];

/**
 * The closed set of descriptor kinds, as a runtime array.
 *
 * Runtime rather than type-only for the same reason \`EVENT_KINDS\` is: a kind this build does not
 * know must be detectable, not silently treated as one it does.
 */
export const PAGE_CONTEXT_KINDS = [${kinds.map((kind) => JSON.stringify(kind)).join(", ")}] as const;
export type PageContextKind = (typeof PAGE_CONTEXT_KINDS)[number];

${nestedBlock}

${interfaces.join("\n\n")}

/** What the user is looking at. A descriptor, never a row set. */
export type PageContext = ${union};

/** Whether \`value\` is one of the descriptor kinds this build declares. */
export function isPageContextKind(value: unknown): value is PageContextKind {
  return typeof value === "string" && (PAGE_CONTEXT_KINDS as readonly string[]).includes(value);
}
`;
}

// ---------------------------------------------------------------------------------------------

function main() {
  const outputs = [
    ["agentEvents.ts", renderAgentEvents(loadSchema("agents.event_schema"))],
    ["pageContext.ts", renderPageContext(loadSchema("crm.toolspec.page_context"))],
  ];

  mkdirSync(OUT_DIR, { recursive: true });
  for (const [name, source] of outputs) {
    const path = join(OUT_DIR, name);
    writeFileAtomic(path, source, "utf8");
    process.stdout.write(`${path}\n`);
  }
}

// `main` runs only when this file is the entry point; importing it writes nothing.
export { OUT_DIR, main, renderAgentEvents, renderPageContext };

if (process.argv[1] !== undefined && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
