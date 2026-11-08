// SPDX-License-Identifier: MIT
/**
 * Derive the `PageContext` descriptor the assistant panel sends with a turn.
 *
 * The descriptor says what the user is looking at — route, entity reference, and a description of
 * the active query. It never carries retrieved rows; the agent re-executes the described query
 * through its own scoped tools. The `PageContext` type (`src/types/generated/pageContext.ts`) has no
 * member a row could be assigned to.
 *
 * Routes are matched on the pathname rather than with `useParams`, because the panel is mounted
 * outside the router outlet, where `useParams()` has no route parameters.
 *
 * A view whose active query this hook cannot state resolves to `opaque`, never to an empty filter
 * set. A list view is `opaque` until its page reports its filter state through `report`.
 */
import { useMemo } from "react";
import { useLocation } from "react-router-dom";

import type {
  EntityType,
  PageContext,
  QueryDescriptor,
} from "../types/generated/pageContext";
import { ENTITY_TYPES } from "../types/generated/pageContext";

/** A filter value the descriptor can carry. Scalars only — a collection could hold a row. */
export type FilterValue = string | number | boolean;

/** The active query a page reports, in the shape the descriptor carries it. */
export interface ViewQueryState {
  readonly filters?: Readonly<Record<string, FilterValue>>;
  readonly sort?: { readonly field: string; readonly direction: "asc" | "desc" };
  readonly page?: { readonly cursor?: string; readonly limit?: number };
}

/**
 * What a page tells the hook about its own view: `query` when it can state its active query in terms
 * the agent's tools accept, or `inexpressible` with a reason when it cannot. Reporting neither leaves
 * a list view `opaque`.
 */
export interface ViewReport {
  readonly query?: ViewQueryState;
  readonly inexpressible?: string;
}

/** Reasons this hook emits on the `opaque` variant. Exported so tests name them once. */
export const OPAQUE_REASONS = {
  unreportedQuery:
    "this view's active filters were not reported to the assistant, so re-running the query " +
    "could return a different set than you are looking at",
  createForm: "this is a create form, so there is no saved record to summarise yet",
  nonEntityView:
    "this view is not one of the CRM entity collections the assistant can query directly",
  unknownRoute: "the assistant does not recognise this route",
  nonScalarFilter:
    "this view has a filter the assistant's tools cannot express, so its query cannot be re-run " +
    "faithfully",
} as const;

/** One route this hook can describe, as an exact segment pattern. */
interface RoutePattern {
  /** Path segments; a segment starting with `:` captures. */
  readonly segments: readonly string[];
  /** Build the descriptor for a matched route. */
  readonly describe: (captures: Readonly<Record<string, string>>) => RouteShape;
}

/** What a matched route resolves to, before a page's own report is applied. */
type RouteShape =
  | { readonly kind: "entity"; readonly entityType: EntityType; readonly entityId: string }
  | { readonly kind: "board" }
  | { readonly kind: "list"; readonly entityType: EntityType; readonly fixedFilters?: Readonly<Record<string, FilterValue>> }
  | { readonly kind: "opaque"; readonly reason: string };

/**
 * The routes this hook understands, matched in order against `App.tsx`'s own route table.
 * `accounts/new` precedes `accounts/:accountId`; segment counts are compared exactly.
 */
const ROUTES: readonly RoutePattern[] = [
  { segments: ["pipeline"], describe: () => ({ kind: "board" }) },

  { segments: ["accounts"], describe: () => ({ kind: "list", entityType: "accounts" }) },
  { segments: ["accounts", "new"], describe: () => ({ kind: "opaque", reason: OPAQUE_REASONS.createForm }) },
  {
    segments: ["accounts", ":accountId"],
    describe: (c) => ({ kind: "entity", entityType: "accounts", entityId: c.accountId }),
  },
  {
    segments: ["accounts", ":accountId", "edit"],
    describe: (c) => ({ kind: "entity", entityType: "accounts", entityId: c.accountId }),
  },

  { segments: ["contacts", "new"], describe: () => ({ kind: "opaque", reason: OPAQUE_REASONS.createForm }) },
  {
    segments: ["contacts", ":contactId"],
    describe: (c) => ({ kind: "entity", entityType: "contacts", entityId: c.contactId }),
  },
  {
    segments: ["contacts", ":contactId", "edit"],
    describe: (c) => ({ kind: "entity", entityType: "contacts", entityId: c.contactId }),
  },

  { segments: ["opportunities"], describe: () => ({ kind: "list", entityType: "opportunities" }) },
  { segments: ["opportunities", "new"], describe: () => ({ kind: "opaque", reason: OPAQUE_REASONS.createForm }) },
  {
    segments: ["opportunities", ":opportunityId"],
    describe: (c) => ({ kind: "entity", entityType: "opportunities", entityId: c.opportunityId }),
  },
  // An activity list's only filter is the parent opportunity, which `list_activities` takes directly.
  {
    segments: ["opportunities", ":opportunityId", "activities"],
    describe: (c) => ({
      kind: "list",
      entityType: "activities",
      fixedFilters: { opportunity_id: c.opportunityId },
    }),
  },

  // Proposals, audit records, settings and the eval dashboard are not entity collections.
  { segments: ["approvals"], describe: () => ({ kind: "opaque", reason: OPAQUE_REASONS.nonEntityView }) },
  { segments: ["audit"], describe: () => ({ kind: "opaque", reason: OPAQUE_REASONS.nonEntityView }) },
  { segments: ["settings"], describe: () => ({ kind: "opaque", reason: OPAQUE_REASONS.nonEntityView }) },
  { segments: ["evals"], describe: () => ({ kind: "opaque", reason: OPAQUE_REASONS.nonEntityView }) },
];

/** The longest route the runtime accepts, in characters (`ROUTE_MAX_LENGTH` server-side). */
export const ROUTE_MAX_LENGTH = 256;

/** The route charset the runtime accepts: an absolute path of letters, digits, `/`, `_`, `-`. */
export const ROUTE_PATTERN = /^\/[A-Za-z0-9/_-]*$/;

/** The longest record id the runtime accepts (`ENTITY_ID_MAX_LENGTH` server-side). */
export const ENTITY_ID_MAX_LENGTH = 128;

/** The record-id shape the runtime accepts. */
export const ENTITY_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

/** The most filters a query may carry (`FILTER_MAX_COUNT` server-side). */
export const FILTER_MAX_COUNT = 16;

/** The longest filter key or sort field (`FIELD_NAME_MAX_LENGTH` server-side). */
export const FIELD_NAME_MAX_LENGTH = 64;

/** A filter key or sort field: a lower-case snake-case attribute name. */
export const FIELD_NAME_PATTERN = /^[a-z][a-z0-9_]*$/;

/** The longest string filter value (`FILTER_VALUE_MAX_LENGTH` server-side). */
export const FILTER_VALUE_MAX_LENGTH = 256;

/** Code-point ranges a string filter value may not carry: C0/C1 controls and the line separators. */
export const FORBIDDEN_VALUE_RANGES = [
  [0x00, 0x1f],
  [0x7f, 0x9f],
  [0x2028, 0x2029],
] as const;

/** Whether `value` carries a code point in {@link FORBIDDEN_VALUE_RANGES}. */
function hasForbiddenCharacter(value: string): boolean {
  for (const character of value) {
    const code = character.codePointAt(0) ?? 0;
    if (FORBIDDEN_VALUE_RANGES.some(([low, high]) => code >= low && code <= high)) return true;
  }
  return false;
}

/** The longest pagination cursor (`CURSOR_MAX_LENGTH` server-side). */
export const CURSOR_MAX_LENGTH = 4000;

/** A pagination cursor: URL-safe base64. */
export const CURSOR_PATTERN = /^[A-Za-z0-9_-]*=*$/;

/** The route sent in place of a pathname the runtime would refuse. */
const FALLBACK_ROUTE = "/";

/** Whether the runtime would accept `route` as a page-context route. */
function isSendableRoute(route: string): boolean {
  return route.length <= ROUTE_MAX_LENGTH && ROUTE_PATTERN.test(route);
}

/** Whether the runtime would accept `entityId` as a page-context record id. */
function isSendableEntityId(entityId: string): boolean {
  return entityId.length <= ENTITY_ID_MAX_LENGTH && ENTITY_ID_PATTERN.test(entityId);
}

/** Whether the runtime would accept `name` as a filter key or sort field. */
function isSendableFieldName(name: string): boolean {
  return name.length <= FIELD_NAME_MAX_LENGTH && FIELD_NAME_PATTERN.test(name);
}

/** Whether the runtime would accept `value` as a filter value. */
function isSendableFilterValue(value: FilterValue): boolean {
  if (typeof value === "string") {
    return value.length <= FILTER_VALUE_MAX_LENGTH && !hasForbiddenCharacter(value);
  }
  return typeof value === "boolean" || Number.isFinite(value);
}

/** Whether the runtime would accept every part of `query`. */
function isSendableQuery(query: QueryDescriptor): boolean {
  const filters = Object.entries(query.filters ?? {});
  if (filters.length > FILTER_MAX_COUNT) return false;
  if (!filters.every(([key, value]) => isSendableFieldName(key) && isSendableFilterValue(value))) {
    return false;
  }
  if (query.sort !== undefined && query.sort !== null && !isSendableFieldName(query.sort.field)) {
    return false;
  }
  const cursor = query.page?.cursor;
  return (
    cursor === undefined ||
    cursor === null ||
    (cursor.length <= CURSOR_MAX_LENGTH && CURSOR_PATTERN.test(cursor))
  );
}

/** Split a pathname into non-empty segments. */
function pathSegments(pathname: string): readonly string[] {
  return pathname.split("/").filter((segment) => segment !== "");
}

/** Match `segments` against the route table, returning the shape or `undefined`. */
function matchRoute(segments: readonly string[]): RouteShape | undefined {
  for (const pattern of ROUTES) {
    if (pattern.segments.length !== segments.length) continue;
    const captures: Record<string, string> = {};
    let matched = true;
    for (let index = 0; index < pattern.segments.length; index += 1) {
      const expected = pattern.segments[index];
      const actual = segments[index];
      if (expected.startsWith(":")) {
        if (actual === "") {
          matched = false;
          break;
        }
        captures[expected.slice(1)] = actual;
      } else if (expected !== actual) {
        matched = false;
        break;
      }
    }
    if (matched) return pattern.describe(captures);
  }
  return undefined;
}

/** Whether every filter value is a scalar the descriptor may carry. */
function filtersAreScalar(filters: Readonly<Record<string, unknown>>): boolean {
  return Object.values(filters).every(
    (value) =>
      typeof value === "string" || typeof value === "number" || typeof value === "boolean",
  );
}

/**
 * Build the `QueryDescriptor` for a list or board view, or `undefined` when the query is not
 * faithfully expressible and the view must be `opaque` instead.
 */
function buildQuery(
  report: ViewReport | undefined,
  fixedFilters: Readonly<Record<string, FilterValue>> | undefined,
): { readonly query: QueryDescriptor } | { readonly opaqueReason: string } {
  const reported = report?.query;
  if (reported === undefined) {
    if (fixedFilters !== undefined) return { query: { filters: fixedFilters } };
    return { opaqueReason: OPAQUE_REASONS.unreportedQuery };
  }

  const merged = { ...fixedFilters, ...(reported.filters ?? {}) };
  if (!filtersAreScalar(merged)) {
    // A non-scalar filter makes the query inexpressible rather than being dropped.
    return { opaqueReason: OPAQUE_REASONS.nonScalarFilter };
  }

  const query: QueryDescriptor = {
    filters: merged,
    ...(reported.sort === undefined
      ? {}
      : { sort: { field: reported.sort.field, direction: reported.sort.direction } }),
    ...(reported.page === undefined
      ? {}
      : {
          page: {
            ...(reported.page.cursor === undefined ? {} : { cursor: reported.page.cursor }),
            ...(reported.page.limit === undefined ? {} : { limit: reported.page.limit }),
          },
        }),
  };
  // A query the runtime would refuse is described as inexpressible rather than sent.
  if (!isSendableQuery(query)) return { opaqueReason: OPAQUE_REASONS.nonScalarFilter };
  return { query };
}

/**
 * Derive the descriptor for the current route.
 *
 * @param report What the active page knows about its own query, if anything.
 * @returns The `PageContext` to send with the next turn. Never contains a row.
 */
export function usePageContext(report?: ViewReport): PageContext {
  const { pathname } = useLocation();

  return useMemo(
    () => derivePageContext(pathname, report),
    // `report` is compared by identity.
    [pathname, report],
  );
}

/**
 * The pure function behind {@link usePageContext}, exported for direct testing.
 *
 * @param pathname The current location's pathname.
 * @param report What the active page knows about its own query, if anything.
 */
export function derivePageContext(pathname: string, report?: ViewReport): PageContext {
  const route = pathname === "" ? "/" : pathname;

  // A pathname the runtime would refuse is never sent; the view is described as unrecognised.
  if (!isSendableRoute(route)) {
    return { kind: "opaque", route: FALLBACK_ROUTE, reason: OPAQUE_REASONS.unknownRoute };
  }

  // A page's `inexpressible` report wins over the route.
  if (report?.inexpressible !== undefined && report.inexpressible !== "") {
    return { kind: "opaque", route, reason: report.inexpressible };
  }

  const shape = matchRoute(pathSegments(pathname));
  if (shape === undefined) {
    return { kind: "opaque", route, reason: OPAQUE_REASONS.unknownRoute };
  }

  switch (shape.kind) {
    case "opaque":
      return { kind: "opaque", route, reason: shape.reason };

    case "entity":
      // A captured id is a path segment, so it is always a non-empty string here.
      if (!isSendableEntityId(shape.entityId)) {
        return { kind: "opaque", route, reason: OPAQUE_REASONS.unknownRoute };
      }
      return {
        kind: "entity",
        route,
        entity_type: shape.entityType,
        entity_id: shape.entityId,
      };

    case "board": {
      // The board runs an unfiltered query (`searchOpportunities({})`), so `{}` is accurate.
      const built = buildQuery(report, {});
      if ("opaqueReason" in built) {
        return { kind: "opaque", route, reason: built.opaqueReason };
      }
      return { kind: "board", route, query: built.query };
    }

    case "list": {
      const built = buildQuery(report, shape.fixedFilters);
      if ("opaqueReason" in built) {
        return { kind: "opaque", route, reason: built.opaqueReason };
      }
      return { kind: "list", route, entity_type: shape.entityType, query: built.query };
    }
  }
}

/** Whether `value` is one of the four entity tables. Re-exported for callers building a report. */
export function isEntityType(value: unknown): value is EntityType {
  return typeof value === "string" && (ENTITY_TYPES as readonly string[]).includes(value);
}
