// SPDX-License-Identifier: MIT
import { describe, expect, it } from "vitest";

import { diffSnapshots } from "./fieldDiff";

describe("diffSnapshots", () => {
  it("lists only the new values for a create", () => {
    expect(diffSnapshots({}, { stage: "Discovery", name: "Acme" })).toEqual({
      kind: "create",
      fields: [
        { field: "name", value: "Acme" },
        { field: "stage", value: "Discovery" },
      ],
    });
  });

  it("lists only the removed values for a delete", () => {
    expect(diffSnapshots({ stage: "Discovery" }, {})).toEqual({
      kind: "delete",
      fields: [{ field: "stage", value: "Discovery" }],
    });
  });

  it("diffs field by field for an update, changed fields first", () => {
    const diff = diffSnapshots(
      { stage: "Discovery", name: "Acme" },
      { stage: "Negotiation", name: "Acme" },
    );
    expect(diff.kind).toBe("update");
    if (diff.kind !== "update") return;
    expect(diff.rows.map((row) => [row.field, row.status])).toEqual([
      ["stage", "changed"],
      ["name", "unchanged"],
    ]);
  });

  it.each([
    ["no snapshots", undefined, undefined],
    ["only a before snapshot", { stage: "Discovery" }, undefined],
    ["non-object snapshots", "Discovery", ["Negotiation"]],
    ["two empty snapshots", {}, {}],
  ] as const)("is legacy with %s", (_case, before, after) => {
    expect(diffSnapshots(before, after)).toEqual({ kind: "legacy" });
  });
});
