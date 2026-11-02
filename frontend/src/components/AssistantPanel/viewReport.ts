// SPDX-License-Identifier: MIT
/**
 * The store a routed page uses to tell the assistant panel about the view it is showing.
 *
 * The panel is mounted as a sibling of the router `<Outlet />`, so a page reaches it only through a
 * `useSyncExternalStore` store. A snapshot carries a count and scalar-only filters, never a row.
 * Each snapshot is stamped with its route, and a reader ignores a snapshot for another route.
 */

import { useSyncExternalStore } from "react";

import type { ViewReport } from "../../hooks/usePageContext";

/** What one page knows about the view it is currently showing. */
export interface ViewSnapshot {
  /** The pathname this snapshot describes. A reader must ignore a snapshot for another route. */
  readonly route: string;
  /** What the page can say about its active query, for `usePageContext`. */
  readonly report?: ViewReport;
  /**
   * How many rows the view is currently showing. Omitted when the view shows no countable
   * collection.
   */
  readonly rowCount?: number;
  /**
   * `true` when the view's own result was cut short, so `rowCount` is a lower bound.
   */
  readonly truncated?: boolean;
}

let snapshot: ViewSnapshot | undefined;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

/** Read the current snapshot, or `undefined` when no page has reported one. */
export function getViewSnapshot(): ViewSnapshot | undefined {
  return snapshot;
}

/**
 * Publish a snapshot, replacing any previous one. A snapshot equal by value (`JSON.stringify`) to
 * the current one is not republished.
 */
export function setViewSnapshot(next: ViewSnapshot): void {
  if (snapshot !== undefined && JSON.stringify(snapshot) === JSON.stringify(next)) return;
  snapshot = next;
  notify();
}

/**
 * Withdraw the snapshot, but only if `route` still owns it: React may mount the next page before
 * unmounting the previous one.
 */
export function clearViewSnapshot(route: string): void {
  if (snapshot?.route !== route) return;
  snapshot = undefined;
  notify();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Bind a component to the current snapshot, re-rendering when a page reports a new one. */
export function useViewSnapshot(): ViewSnapshot | undefined {
  return useSyncExternalStore(subscribe, getViewSnapshot);
}

/** The snapshot for `route`, or `undefined` when the current one describes a different view. */
export function snapshotForRoute(
  current: ViewSnapshot | undefined,
  route: string,
): ViewSnapshot | undefined {
  return current?.route === route ? current : undefined;
}

/** Test-only: restore the module to its initial empty state between test cases. */
export function __resetViewSnapshotForTests(): void {
  snapshot = undefined;
  listeners.clear();
}
