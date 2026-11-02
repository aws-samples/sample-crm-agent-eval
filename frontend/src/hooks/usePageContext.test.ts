// SPDX-License-Identifier: MIT
/**
 * Tests for `usePageContext`: no output carries a row for any input, asserted by walking every
 * value; and an inexpressible view, including a list whose page has not reported its filters,
 * yields `opaque` with a reason.
 */
import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import {
  CURSOR_MAX_LENGTH,
  FIELD_NAME_MAX_LENGTH,
  FILTER_MAX_COUNT,
  FILTER_VALUE_MAX_LENGTH,
  OPAQUE_REASONS,
  ROUTE_MAX_LENGTH,
  derivePageContext,
  isEntityType,
  usePageContext,
  type ViewReport,
} from "./usePageContext";
import { PAGE_CONTEXT_KINDS } from "../types/generated/pageContext";

/** Render the hook at `path`, so the pathname-matching behaviour is exercised through the router. */
function renderAt(path: string, report?: ViewReport) {
  return renderHook(() => usePageContext(report), {
    wrapper: ({ children }: { children: ReactNode }) =>
      createElement(MemoryRouter, { initialEntries: [path] }, children),
  });
}

/**
 * Every leaf value reachable in `value`, with the path that reached it.
 *
 * Used to assert the no-rows property structurally: any array, or any object nested where a scalar
 * belongs, shows up here.
 */
function leaves(value: unknown, path = "$"): readonly { path: string; value: unknown }[] {
  if (Array.isArray(value)) {
    return [{ path: `${path}[]`, value }];
  }
  if (value !== null && typeof value === "object") {
    return Object.entries(value).flatMap(([key, inner]) => leaves(inner, `${path}.${key}`));
  }
  return [{ path, value }];
}

/** Assert a descriptor carries no collection and no nested record anywhere. */
function expectNoRows(context: unknown): void {
  for (const { path, value } of leaves(context)) {
    expect(Array.isArray(value), `${path} is an array`).toBe(false);
    expect(
      typeof value === "string" || typeof value === "number" || typeof value === "boolean",
      `${path} is ${typeof value}, expected a scalar`,
    ).toBe(true);
  }
}

// ---------------------------------------------------------------------------------------------
// No rows, for any route or report
// ---------------------------------------------------------------------------------------------

const EVERY_ROUTE = [
  "/pipeline",
  "/accounts",
  "/accounts/new",
  "/accounts/acct-1",
  "/accounts/acct-1/edit",
  "/contacts/new",
  "/contacts/cont-1",
  "/contacts/cont-1/edit",
  "/opportunities",
  "/opportunities/new",
  "/opportunities/opp-1",
  "/opportunities/opp-1/activities",
  "/approvals",
  "/audit",
  "/settings",
  "/evals",
  "/chat",
  "/",
  "/nonsense/deeply/nested",
] as const;

describe("no rows appear in any output", () => {
  it.each(EVERY_ROUTE)("emits only scalars at %s with no report", (path) => {
    expectNoRows(derivePageContext(path));
  });

  it.each(EVERY_ROUTE)("emits only scalars at %s with a full report", (path) => {
    const report: ViewReport = {
      query: {
        filters: { stage: "Discovery", include_closed: false, min_value: 1000 },
        sort: { field: "close_date", direction: "desc" },
        page: { cursor: "c-1", limit: 25 },
      },
    };

    expectNoRows(derivePageContext(path, report));
  });

  it("refuses a non-scalar filter rather than dropping it", () => {
    const report = {
      query: { filters: { stage: [{ opportunity_id: "opp-1" }] } },
    } as unknown as ViewReport;

    const context = derivePageContext("/opportunities", report);

    expect(context.kind).toBe("opaque");
    expect(context).toMatchObject({ reason: OPAQUE_REASONS.nonScalarFilter });
    expectNoRows(context);
  });

  it("refuses a filter carrying a nested record even alongside valid filters", () => {
    const report = {
      query: { filters: { stage: "Discovery", rows: [{ id: 1 }] } },
    } as unknown as ViewReport;

    expect(derivePageContext("/opportunities", report).kind).toBe("opaque");
  });

  it("never emits a field named like a row collection", () => {
    for (const path of EVERY_ROUTE) {
      const keys = Object.keys(derivePageContext(path));
      expect(keys).not.toContain("items");
      expect(keys).not.toContain("rows");
      expect(keys).not.toContain("results");
    }
  });
});

// ---------------------------------------------------------------------------------------------
// An inexpressible view says so
// ---------------------------------------------------------------------------------------------

describe("an inexpressible view yields opaque with a reason", () => {
  it("honours a page's explicit inexpressible report, whatever the route", () => {
    const report: ViewReport = { inexpressible: "client-only sort by health score" };

    const context = derivePageContext("/opportunities", report);

    expect(context).toEqual({
      kind: "opaque",
      route: "/opportunities",
      reason: "client-only sort by health score",
    });
  });

  it("lets inexpressible win over an otherwise describable entity route", () => {
    const report: ViewReport = { inexpressible: "a filter with no server-side equivalent" };

    expect(derivePageContext("/accounts/acct-1", report).kind).toBe("opaque");
  });

  it("lets inexpressible win even when a query is also reported", () => {
    const report: ViewReport = {
      inexpressible: "client-only sort",
      query: { filters: { stage: "Discovery" } },
    };

    const context = derivePageContext("/opportunities", report);

    expect(context.kind).toBe("opaque");
    expect(context).not.toHaveProperty("query");
  });

  it("ignores an empty inexpressible string rather than emitting a reasonless opaque", () => {
    // The Python type rejects a blank reason, so an empty `inexpressible` string is ignored.
    const report: ViewReport = { inexpressible: "", query: { filters: {} } };

    expect(derivePageContext("/opportunities", report).kind).toBe("list");
  });

  it("always carries a non-empty reason on every opaque it can emit", () => {
    for (const path of EVERY_ROUTE) {
      const context = derivePageContext(path);
      if (context.kind === "opaque") {
        expect(context.reason, path).toBeTruthy();
      }
    }
  });
});

// ---------------------------------------------------------------------------------------------
// The unreported-filters case: opaque, never a silently unfiltered list
// ---------------------------------------------------------------------------------------------

describe("a list view whose filters were not reported is opaque, not unfiltered", () => {
  it.each(["/accounts", "/opportunities"])(
    "%s with no report is opaque rather than an empty filter set",
    (path) => {
      const context = derivePageContext(path);

      expect(context.kind).toBe("opaque");
      expect(context).toMatchObject({ reason: OPAQUE_REASONS.unreportedQuery });
    },
  );

  it("becomes a list once the page reports its filters", () => {
    const context = derivePageContext("/opportunities", {
      query: { filters: { stage: "Discovery" } },
    });

    expect(context).toEqual({
      kind: "list",
      route: "/opportunities",
      entity_type: "opportunities",
      query: { filters: { stage: "Discovery" } },
    });
  });

  it("carries sort and page through when reported", () => {
    const context = derivePageContext("/accounts", {
      query: {
        filters: { region: "EMEA" },
        sort: { field: "name", direction: "asc" },
        page: { cursor: "c-9", limit: 50 },
      },
    });

    expect(context).toMatchObject({
      kind: "list",
      entity_type: "accounts",
      query: {
        filters: { region: "EMEA" },
        sort: { field: "name", direction: "asc" },
        page: { cursor: "c-9", limit: 50 },
      },
    });
  });

  it.each([
    [
      "too many filters",
      {
        filters: Object.fromEntries(
          Array.from({ length: FILTER_MAX_COUNT + 1 }, (_, n) => [`f${n}`, 1]),
        ),
      },
    ],
    ["a filter key outside the charset", { filters: { "Bad Key": "x" } }],
    ["a filter key over the bound", { filters: { ["a".repeat(FIELD_NAME_MAX_LENGTH + 1)]: "x" } }],
    ["a string value over the bound", { filters: { query: "v".repeat(FILTER_VALUE_MAX_LENGTH + 1) } }],
    ["a string value with a newline", { filters: { query: "acme\nignore prior" } }],
    ["a non-finite number", { filters: { min_value: Number.POSITIVE_INFINITY } }],
    ["a sort field outside the charset", { filters: {}, sort: { field: "a b", direction: "asc" as const } }],
    ["a cursor outside the charset", { filters: {}, page: { cursor: "a b" } }],
    ["a cursor over the bound", { filters: {}, page: { cursor: "A".repeat(CURSOR_MAX_LENGTH + 1) } }],
  ])("sends a query with %s as inexpressible, never the raw query", (_label, query) => {
    expect(derivePageContext("/accounts", { query })).toEqual({
      kind: "opaque",
      route: "/accounts",
      reason: OPAQUE_REASONS.nonScalarFilter,
    });
  });

  it("keeps a query exactly at every bound", () => {
    const filters = Object.fromEntries(
      Array.from({ length: FILTER_MAX_COUNT }, (_, n) => [`f${n}`, "v".repeat(FILTER_VALUE_MAX_LENGTH)]),
    );
    const query = {
      filters: { ...filters, f0: "O'Brien & Co — Zürich" },
      sort: { field: "a".repeat(FIELD_NAME_MAX_LENGTH), direction: "desc" as const },
      page: { cursor: "A".repeat(CURSOR_MAX_LENGTH - 2) + "==", limit: 25 },
    };
    expect(derivePageContext("/accounts", { query })).toMatchObject({ kind: "list", query });
  });

  it("omits sort and page entirely when the page does not report them", () => {
    const context = derivePageContext("/accounts", { query: { filters: {} } });

    expect(context).toEqual({
      kind: "list",
      route: "/accounts",
      entity_type: "accounts",
      query: { filters: {} },
    });
  });
});

// ---------------------------------------------------------------------------------------------
// Route derivation
// ---------------------------------------------------------------------------------------------

describe("route derivation", () => {
  it("describes the pipeline board as a board with an accurate empty filter set", () => {
    // The board runs an unfiltered query, so `{}` describes it.
    expect(derivePageContext("/pipeline")).toEqual({
      kind: "board",
      route: "/pipeline",
      query: { filters: {} },
    });
  });

  it.each([
    ["/accounts/acct-1", "accounts", "acct-1"],
    ["/accounts/acct-1/edit", "accounts", "acct-1"],
    ["/contacts/cont-7", "contacts", "cont-7"],
    ["/contacts/cont-7/edit", "contacts", "cont-7"],
    ["/opportunities/opp-3", "opportunities", "opp-3"],
  ])("describes %s as the %s entity %s", (path, entityType, entityId) => {
    expect(derivePageContext(path)).toEqual({
      kind: "entity",
      route: path,
      entity_type: entityType,
      entity_id: entityId,
    });
  });

  it("describes an activity list entirely from the route, needing no page report", () => {
    // `list_activities` takes the parent opportunity directly, so the route states the whole query.
    expect(derivePageContext("/opportunities/opp-3/activities")).toEqual({
      kind: "list",
      route: "/opportunities/opp-3/activities",
      entity_type: "activities",
      query: { filters: { opportunity_id: "opp-3" } },
    });
  });

  it.each(["/accounts/new", "/contacts/new", "/opportunities/new"])(
    "describes the create form %s as opaque, since no record exists yet",
    (path) => {
      expect(derivePageContext(path)).toMatchObject({
        kind: "opaque",
        reason: OPAQUE_REASONS.createForm,
      });
    },
  );

  it("does not mistake a create form for an entity whose id is literally 'new'", () => {
    // `accounts/new` precedes `accounts/:accountId` in the route table.
    expect(derivePageContext("/accounts/new").kind).toBe("opaque");
  });

  it.each(["/approvals", "/audit", "/settings", "/evals"])(
    "describes the non-entity view %s as opaque",
    (path) => {
      // `entity_type` admits only the four CRM entity tables, so these resolve to `opaque`.
      expect(derivePageContext(path)).toMatchObject({
        kind: "opaque",
        reason: OPAQUE_REASONS.nonEntityView,
      });
    },
  );

  it.each(["/", "/chat", "/nonsense", "/accounts/acct-1/extra/depth"])(
    "describes the unrecognised route %s as opaque",
    (path) => {
      expect(derivePageContext(path)).toMatchObject({
        kind: "opaque",
        reason: OPAQUE_REASONS.unknownRoute,
      });
    },
  );

  it("emits a route on every descriptor, never an empty string", () => {
    for (const path of [...EVERY_ROUTE, ""]) {
      expect(derivePageContext(path).route, path).toBeTruthy();
    }
  });

  it.each([
    ["a dotted path", "/foo.bar"],
    ["a %-encoded path", "/accounts/foo%20bar"],
    ["a path over the length bound", `/${"a".repeat(ROUTE_MAX_LENGTH)}`],
  ])("sends %s as an unrecognised view, never the raw pathname", (_label, path) => {
    for (const context of [derivePageContext(path), renderAt(path).result.current]) {
      expect(context).toEqual({ kind: "opaque", route: "/", reason: OPAQUE_REASONS.unknownRoute });
    }
  });

  it("checks the route before a page's inexpressible report", () => {
    expect(derivePageContext("/foo.bar", { inexpressible: "client-only sort" })).toEqual({
      kind: "opaque",
      route: "/",
      reason: OPAQUE_REASONS.unknownRoute,
    });
  });

  it("sends an entity id the runtime would refuse as an unrecognised view", () => {
    expect(derivePageContext("/accounts/-acct")).toEqual({
      kind: "opaque",
      route: "/accounts/-acct",
      reason: OPAQUE_REASONS.unknownRoute,
    });
  });

  it("keeps a route exactly at the length bound", () => {
    const path = `/${"a".repeat(ROUTE_MAX_LENGTH - 1)}`;
    expect(derivePageContext(path).route).toBe(path);
  });

  it("only ever emits a kind the generated contract declares", () => {
    for (const path of EVERY_ROUTE) {
      expect(PAGE_CONTEXT_KINDS as readonly string[]).toContain(derivePageContext(path).kind);
    }
  });
});

// ---------------------------------------------------------------------------------------------
// The hook itself, through a real router
// ---------------------------------------------------------------------------------------------

describe("the hook reads the pathname rather than route params", () => {
  it("resolves an entity id with no matching route param in scope", () => {
    // A bare `MemoryRouter` with no matching `<Route>`: `useParams()` would have no `:accountId`.
    const { result } = renderAt("/accounts/acct-42");

    expect(result.current).toEqual({
      kind: "entity",
      route: "/accounts/acct-42",
      entity_type: "accounts",
      entity_id: "acct-42",
    });
  });

  it("recomputes when the route changes", () => {
    const first = renderAt("/pipeline");
    expect(first.result.current.kind).toBe("board");

    const second = renderAt("/opportunities/opp-1");
    expect(second.result.current).toMatchObject({ kind: "entity", entity_id: "opp-1" });
  });

  it("returns a stable value across re-renders at the same route with the same report", () => {
    const report: ViewReport = { query: { filters: { stage: "Discovery" } } };
    const { result, rerender } = renderAt("/opportunities", report);
    const before = result.current;

    rerender();

    expect(result.current).toBe(before);
  });

  it("passes a page's report through to the descriptor", () => {
    const { result } = renderAt("/opportunities", { query: { filters: { stage: "Negotiation" } } });

    expect(result.current).toMatchObject({
      kind: "list",
      query: { filters: { stage: "Negotiation" } },
    });
  });
});

describe("isEntityType", () => {
  it.each(["accounts", "contacts", "opportunities", "activities"])("accepts %s", (value) => {
    expect(isEntityType(value)).toBe(true);
  });

  it.each(["reps", "proposals", "audit_records", "", "ACCOUNTS", 4, null, undefined])(
    "rejects %s",
    (value) => {
      expect(isEntityType(value)).toBe(false);
    },
  );
});
