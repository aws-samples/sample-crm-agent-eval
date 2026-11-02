// SPDX-License-Identifier: MIT
/**
 * `viewReport`: a snapshot is ignored on any route but its own, clearing is scoped to the owning
 * route, and an equal-by-value republish does not notify.
 */
import { afterEach, describe, expect, it } from "vitest";

import {
  __resetViewSnapshotForTests,
  clearViewSnapshot,
  getViewSnapshot,
  setViewSnapshot,
  snapshotForRoute,
} from "./viewReport";

afterEach(() => {
  __resetViewSnapshotForTests();
});

describe("a snapshot belongs to the route that made it", () => {
  it("is returned for its own route and withheld for any other", () => {
    const snapshot = { route: "/opportunities", rowCount: 3 };
    setViewSnapshot(snapshot);

    expect(snapshotForRoute(getViewSnapshot(), "/opportunities")).toBe(snapshot);
    // A report outliving its page does not describe the next view.
    expect(snapshotForRoute(getViewSnapshot(), "/pipeline")).toBeUndefined();
  });

  it("withholds nothing when there is nothing to withhold", () => {
    expect(snapshotForRoute(undefined, "/opportunities")).toBeUndefined();
  });
});

describe("clearing is scoped to the route that owns the snapshot", () => {
  it("clears its own", () => {
    setViewSnapshot({ route: "/opportunities", rowCount: 3 });
    clearViewSnapshot("/opportunities");
    expect(getViewSnapshot()).toBeUndefined();
  });

  it("does not clear a newer page's report", () => {
    /*
     * The outgoing page's cleanup may run after the incoming page has published.
     */
    setViewSnapshot({ route: "/pipeline", rowCount: 12 });
    clearViewSnapshot("/opportunities");
    expect(getViewSnapshot()).toEqual({ route: "/pipeline", rowCount: 12 });
  });
});

describe("an equal republish is not a change", () => {
  it("keeps the same snapshot object, so a subscriber does not re-render", () => {
    /*
     * Asserted through the store's own read: `useSyncExternalStore` bails out on an identical
     * reference, so an unchanged reference is the no-re-render property.
     */
    setViewSnapshot({ route: "/opportunities", report: { query: { filters: { stage: "Negotiation" } } } });
    const first = getViewSnapshot();

    setViewSnapshot({ route: "/opportunities", report: { query: { filters: { stage: "Negotiation" } } } });
    expect(getViewSnapshot(), "an equal republish replaced the snapshot").toBe(first);

    // A changed filter, a changed count, and a changed route each land.
    setViewSnapshot({ route: "/opportunities", report: { query: { filters: { stage: "Discovery" } } } });
    expect(getViewSnapshot()).not.toBe(first);

    const second = getViewSnapshot();
    setViewSnapshot({
      route: "/opportunities",
      report: { query: { filters: { stage: "Discovery" } } },
      rowCount: 4,
    });
    expect(getViewSnapshot()).not.toBe(second);

    const third = getViewSnapshot();
    setViewSnapshot({
      route: "/pipeline",
      report: { query: { filters: { stage: "Discovery" } } },
      rowCount: 4,
    });
    expect(getViewSnapshot()).not.toBe(third);
  });
});
