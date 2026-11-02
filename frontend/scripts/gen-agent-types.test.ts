// SPDX-License-Identifier: MIT
/**
 * Tests for `gen-agent-types.mjs`, the generator behind `agentEvents.ts` and `pageContext.ts`.
 *
 * Asserts that an unmapped JSON Schema type fails rather than omitting a field, that an
 * array-typed `PageContext` property is refused, and that kind sets are emitted as runtime
 * `as const` arrays.
 */
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// @ts-expect-error — plain ESM generator with no type declarations; this test is its contract.
import { renderAgentEvents, renderPageContext } from "./gen-agent-types.mjs";

const GENERATED_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..", "src", "types", "generated");

/** A minimal but well-formed SSE schema, matching what `agents.event_schema` renders. */
function eventSchema(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: "1.0.0",
    "x-frame-prefix": "data: ",
    "x-frame-delimiter": "\n\n",
    properties: {
      kind: { enum: ["tool_call", "completed", "error"] },
      error_kind: { enum: ["budget_exhausted", null] },
      session_id: { type: ["string", "null"] },
    },
    ...overrides,
  };
}

/**
 * A minimal but well-formed `PageContext` schema, matching what `page_context` renders.
 *
 * The default includes an `entity` variant; the generator refuses a schema with no `entity_type`
 * vocabulary.
 */
function pageContextSchema(variants?: unknown[]): Record<string, unknown> {
  return {
    "x-schema-version": "1",
    oneOf: variants ?? [
      {
        title: "EntityPageContext",
        required: ["kind", "route", "entity_type", "entity_id"],
        properties: {
          kind: { type: "string", const: "entity" },
          route: { type: "string" },
          entity_type: { type: "string", enum: ["accounts", "contacts"] },
          entity_id: { type: "string" },
        },
      },
      {
        title: "BoardPageContext",
        required: ["kind", "route"],
        properties: {
          kind: { type: "string", const: "board" },
          route: { type: "string" },
        },
      },
    ],
  };
}

describe("an unmapped JSON Schema type fails loudly", () => {
  it("throws rather than omitting an event field whose type has no TypeScript mapping", () => {
    const schema = eventSchema({
      properties: {
        kind: { enum: ["completed"] },
        weird: { type: ["date-time"] },
      },
    });

    expect(() => renderAgentEvents(schema)).toThrowError(/no TypeScript mapping/);
  });

  it("names the offending property, so the failure is actionable", () => {
    const schema = eventSchema({
      properties: { kind: { enum: ["completed"] }, weird: { type: ["date-time"] } },
    });

    expect(() => renderAgentEvents(schema)).toThrowError(/weird/);
  });

  it("throws rather than omitting a PageContext field with no mapping", () => {
    const schema = pageContextSchema([
      {
        title: "BadPageContext",
        required: ["kind"],
        properties: {
          kind: { type: "string", const: "bad" },
          when: { type: ["date-time"] },
        },
      },
    ]);

    expect(() => renderPageContext(schema)).toThrowError(/no TypeScript mapping/);
  });
});

describe("the generator refuses to render a row collection", () => {
  it("throws on an array-typed PageContext property", () => {
    const schema = pageContextSchema([
      {
        title: "RowCarryingContext",
        required: ["kind", "route"],
        properties: {
          kind: { type: "string", const: "rows" },
          route: { type: "string" },
          items: { type: "array", items: { type: "object" } },
        },
      },
    ]);

    expect(() => renderPageContext(schema)).toThrowError(/carries no collections/);
  });

  it("throws on an unconstrained object property, which admits a record", () => {
    const schema = pageContextSchema([
      {
        title: "OpenContext",
        required: ["kind", "route"],
        properties: {
          kind: { type: "string", const: "open" },
          route: { type: "string" },
          payload: { type: "object", additionalProperties: true },
        },
      },
    ]);

    expect(() => renderPageContext(schema)).toThrowError(/unconstrained object/);
  });
});

describe("a malformed schema is refused rather than half-rendered", () => {
  it("rejects an SSE schema declaring no event kinds", () => {
    expect(() => renderAgentEvents(eventSchema({ properties: {} }))).toThrowError(
      /no event kinds/,
    );
  });

  it("rejects an SSE schema with no version, which a consumer could not be checked against", () => {
    const schema = eventSchema();
    delete (schema as Record<string, unknown>).schema_version;

    expect(() => renderAgentEvents(schema)).toThrowError(/no schema_version/);
  });

  it("rejects a PageContext schema with no variants", () => {
    expect(() => renderPageContext(pageContextSchema([]))).toThrowError(/no variants/);
  });

  it("rejects a PageContext variant with no kind discriminator", () => {
    const schema = pageContextSchema([
      { title: "Untitled", properties: { route: { type: "string" } } },
    ]);

    expect(() => renderPageContext(schema)).toThrowError(/needs a `kind` const and a title/);
  });

  it("rejects a nested object with no title, which could not be given a name", () => {
    const schema = pageContextSchema([
      {
        title: "NestedContext",
        required: ["kind"],
        properties: {
          kind: { type: "string", const: "nested" },
          query: { type: "object", properties: { field: { type: "string" } } },
        },
      },
    ]);

    expect(() => renderPageContext(schema)).toThrowError(/no `title`/);
  });
});

describe("the kind sets exist at runtime, not only in the type system", () => {
  it("emits EVENT_KINDS as an `as const` array so an unknown kind is detectable", () => {
    const source = renderAgentEvents(eventSchema());

    expect(source).toContain('export const EVENT_KINDS = ["tool_call", "completed", "error"] as const');
    expect(source).toContain("export type EventKind = (typeof EVENT_KINDS)[number]");
    expect(source).toContain("export function isEventKind");
  });

  it("emits ERROR_KINDS without the schema's null placeholder", () => {
    const source = renderAgentEvents(eventSchema());

    expect(source).toContain('export const ERROR_KINDS = ["budget_exhausted"] as const');
    expect(source).not.toContain('"budget_exhausted", null');
  });

  it("emits PAGE_CONTEXT_KINDS as an `as const` array", () => {
    const source = renderPageContext(pageContextSchema());

    expect(source).toContain('export const PAGE_CONTEXT_KINDS = ["entity", "board"] as const');
    expect(source).toContain("export function isPageContextKind");
  });

  it("emits ENTITY_TYPES as a runtime array and references it from the variants", () => {
    const source = renderPageContext(pageContextSchema());

    expect(source).toContain('export const ENTITY_TYPES = ["accounts", "contacts"] as const');
    expect(source).toContain("readonly entity_type: EntityType;");
  });

  it("refuses a schema with no entity vocabulary rather than emitting an empty const array", () => {
    const boardOnly = pageContextSchema([
      {
        title: "BoardPageContext",
        required: ["kind", "route"],
        properties: { kind: { type: "string", const: "board" }, route: { type: "string" } },
      },
    ]);

    expect(() => renderPageContext(boardOnly)).toThrowError(/no entity_type vocabulary/);
  });

  it("aliases the kind and error_kind fields to their named types rather than re-inlining them", () => {
    const source = renderAgentEvents(eventSchema());

    expect(source).toContain("readonly kind: EventKind;");
    expect(source).toContain("readonly error_kind: ErrorKind | null;");
  });
});

describe("the committed consumers match what the generator produces", () => {
  it("emits the SSE consumer at the exact path check_sse_schema_parity looks for", () => {
    // `check_sse_schema_parity`'s `DEFAULT_CONSUMERS` names this literal path.
    const source = readFileSync(join(GENERATED_DIR, "agentEvents.ts"), "utf8");

    expect(source).toContain("export const EVENT_KINDS = [");
    expect(source).toContain("export const SCHEMA_VERSION = ");
  });

  it("emits the PageContext consumer with no array type anywhere in it", () => {
    const source = readFileSync(join(GENERATED_DIR, "pageContext.ts"), "utf8");

    expect(source).toContain("export type PageContext = ");
    // No `T[]` and no `Array<...>`.
    expect(source).not.toMatch(/:\s*\w+\[\]/);
    expect(source).not.toContain("Array<");
  });

  it("marks both consumers as generated so nobody hand-edits them", () => {
    for (const name of ["agentEvents.ts", "pageContext.ts"]) {
      const source = readFileSync(join(GENERATED_DIR, name), "utf8");
      expect(source, name).toContain("GENERATED FILE — do not edit by hand");
      expect(source, name).toContain("npm run gen:types");
    }
  });
});
