// SPDX-License-Identifier: MIT
/**
 * `diffSnapshots(before, after)`: the audit detail view's change model, from a record's two
 * snapshots. A create (empty `before`) lists the new values alone, a delete (empty `after`) the
 * removed values alone, an update the field-level diff; a record without both snapshots as objects
 * is `legacy` and has no diff to show.
 */
import { computeFieldDiffs, type FieldDiff } from "../../components/diff";

/** One field of a created or deleted record. */
export interface SnapshotField {
  readonly field: string;
  readonly value: unknown;
}

export type SnapshotDiff =
  | { readonly kind: "create"; readonly fields: readonly SnapshotField[] }
  | { readonly kind: "delete"; readonly fields: readonly SnapshotField[] }
  | { readonly kind: "update"; readonly rows: readonly FieldDiff[] }
  | { readonly kind: "legacy" };

function isSnapshot(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function fieldsOf(snapshot: Record<string, unknown>): SnapshotField[] {
  return Object.keys(snapshot)
    .sort((a, b) => a.localeCompare(b))
    .map((field) => ({ field, value: snapshot[field] }));
}

export function diffSnapshots(before: unknown, after: unknown): SnapshotDiff {
  if (!isSnapshot(before) || !isSnapshot(after)) return { kind: "legacy" };
  const hasBefore = Object.keys(before).length > 0;
  const hasAfter = Object.keys(after).length > 0;
  if (!hasBefore && !hasAfter) return { kind: "legacy" };
  if (!hasBefore) return { kind: "create", fields: fieldsOf(after) };
  if (!hasAfter) return { kind: "delete", fields: fieldsOf(before) };
  return { kind: "update", rows: computeFieldDiffs(before, after, false) };
}
