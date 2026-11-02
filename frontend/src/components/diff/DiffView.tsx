// SPDX-License-Identifier: MIT
/**
 * `DiffView`: the shared before/after diff renderer for a proposed or committed change, used by
 * `pages/approvals`, `pages/audit`, and the assistant's proposal cards. Takes two
 * `Record<string, unknown>` snapshots.
 *
 * Change is conveyed through icon, text label, and a `data-diff-status` attribute, never colour
 * alone: each field is a `<dl>` row with a visually-hidden status word, and the list carries an
 * `aria-label`. The removed side carries `--diff-removed-decoration` (`line-through`) and the added
 * side `--diff-added-decoration` (`underline`), plus each side's `--diff-*-marker` glyph.
 *
 * `--diff-*-decoration` and `--diff-*-marker` are not mapped in `src/styles/theme.ts`, so they are
 * referenced as Tailwind arbitrary properties reading the custom property directly.
 */
import type { JSX } from "react";

import { cn } from "../ui/utils";

export type FieldDiffStatus = "unchanged" | "changed" | "added" | "removed";

export interface FieldDiff {
  readonly field: string;
  readonly before: unknown;
  readonly after: unknown;
  readonly status: FieldDiffStatus;
}

export interface DiffViewProps {
  /**
   * State before the change. `undefined`/`null` fields are treated as absent. An `undefined` prop
   * (an audit list row, which carries no snapshots) renders a "not included in this view" note.
   */
  readonly before?: Record<string, unknown>;
  /**
   * State after the change. For a pending proposal this is the `after_payload` (only the
   * changed fields); for a committed audit record this is the full `after_snapshot`. Both shapes
   * render correctly — a key present only in `after` and absent from `before` renders as
   * "added", and vice versa "removed".
   */
  readonly after?: Record<string, unknown>;
  /** Accessible name for the whole diff region, e.g. "Change to opportunity Acme Renewal". */
  readonly label: string;
  /**
   * When true, unchanged fields present in `before` but absent from `after` (typical of a
   * proposal's partial `after_payload`) are hidden rather than rendered as "removed". Set this
   * for proposal diffs; leave false for audit diffs, which always carry a full after-snapshot.
   */
  readonly partialAfter?: boolean;
}

function formatValue(value: unknown): string {
  if (value === undefined || value === null) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function valuesEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a === "object" && typeof b === "object") {
    return JSON.stringify(a) === JSON.stringify(b);
  }
  return false;
}

/** Builds the row-level diff model from two snapshots. Exported for reuse/testing. */
export function computeFieldDiffs(
  before: Record<string, unknown>,
  after: Record<string, unknown>,
  partialAfter: boolean,
): FieldDiff[] {
  const fields = new Set<string>([...Object.keys(before), ...Object.keys(after)]);
  const rows: FieldDiff[] = [];

  for (const field of fields) {
    const hasBefore = Object.prototype.hasOwnProperty.call(before, field);
    const hasAfter = Object.prototype.hasOwnProperty.call(after, field);
    const beforeValue = before[field];
    const afterValue = after[field];

    if (hasBefore && !hasAfter) {
      if (partialAfter) {
        // Not part of this proposal's payload: unchanged, not removed.
        continue;
      }
      rows.push({ field, before: beforeValue, after: undefined, status: "removed" });
      continue;
    }

    if (!hasBefore && hasAfter) {
      rows.push({ field, before: undefined, after: afterValue, status: "added" });
      continue;
    }

    if (valuesEqual(beforeValue, afterValue)) {
      rows.push({ field, before: beforeValue, after: afterValue, status: "unchanged" });
    } else {
      rows.push({ field, before: beforeValue, after: afterValue, status: "changed" });
    }
  }

  // Stable order: changed/added/removed first (the signal), unchanged last (the context).
  const rank: Record<FieldDiffStatus, number> = { changed: 0, added: 1, removed: 2, unchanged: 3 };
  return rows.sort((a, b) => rank[a.status] - rank[b.status] || a.field.localeCompare(b.field));
}

const STATUS_ICON: Record<FieldDiffStatus, string> = {
  changed: "±",
  added: "+",
  removed: "−",
  unchanged: "=",
};

const STATUS_LABEL: Record<FieldDiffStatus, string> = {
  changed: "changed",
  added: "added",
  removed: "removed",
  unchanged: "unchanged",
};

/** Per-status row treatment. Every status differs in surface *and* border, never hue alone. */
const ROW_CLASS: Record<FieldDiffStatus, string> = {
  // No `/40`-style opacity modifier: these colours resolve to `var(--token)`, which Tailwind cannot
  // decompose into channels.
  changed: "border-l-emphasis border-caution-border bg-caution-surface text-caution-text",
  added: "border-l-emphasis border-diff-added-border bg-diff-added-surface text-diff-added-text",
  removed:
    "border-l-emphasis border-diff-removed-border bg-diff-removed-surface text-diff-removed-text",
  unchanged: "border-l-hairline border-border-subtle text-text-muted",
};

/**
 * The removed side: struck through, with its marker glyph. Both come from tokens
 * (`--diff-removed-decoration`, `--diff-removed-marker`), so a token change propagates.
 */
const REMOVED_SIDE =
  "font-numeric text-diff-removed-text [text-decoration-line:var(--diff-removed-decoration)] " +
  "before:mr-1 before:font-semibold before:content-[var(--diff-removed-marker)]";

/** The added side: underlined, with its own marker glyph. */
const ADDED_SIDE =
  "font-numeric font-medium text-diff-added-text [text-decoration-line:var(--diff-added-decoration)] " +
  "before:mr-1 before:font-semibold before:content-[var(--diff-added-marker)]";

export function DiffView({ before, after, label, partialAfter = false }: DiffViewProps): JSX.Element {
  // An absent snapshot means the change is unknown in this view, not empty.
  if (before === undefined || after === undefined) {
    return (
      <section aria-label={label} className="space-y-3">
        <p className="text-xs font-medium tracking-wide text-text-secondary">
          Before/after snapshots are not included in this view.
        </p>
      </section>
    );
  }

  const rows = computeFieldDiffs(before, after, partialAfter);
  const changedCount = rows.filter((r) => r.status !== "unchanged").length;

  return (
    <section aria-label={label} className="space-y-3">
      <p className="text-xs font-medium tracking-wide text-text-secondary">
        {changedCount === 0
          ? "No field changes."
          : `${changedCount} field${changedCount === 1 ? "" : "s"} changed.`}
      </p>
      <dl className="overflow-hidden rounded-surface border border-hairline border-border-subtle">
        {rows.map((row) => (
          <div
            key={row.field}
            className={cn(
              "grid gap-1 px-cell-x py-cell-y sm:grid-cols-[minmax(0,10rem)_1fr] sm:gap-4",
              "border-b-hairline border-border-subtle last:border-b-0",
              ROW_CLASS[row.status],
            )}
            data-diff-status={row.status}
          >
            <dt className="flex items-baseline gap-2 text-xs font-medium">
              <span aria-hidden="true" className="font-numeric font-semibold">
                {STATUS_ICON[row.status]}
              </span>{" "}
              {row.field}
              <span className="visually-hidden"> ({STATUS_LABEL[row.status]})</span>
            </dt>
            <dd className="flex flex-wrap items-baseline gap-2 text-sm">
              {row.status === "unchanged" ? (
                <span className="font-numeric text-text-muted">{formatValue(row.before)}</span>
              ) : (
                <>
                  <span className={REMOVED_SIDE}>
                    <span className="visually-hidden">before: </span>
                    {formatValue(row.before)}
                  </span>
                  <span aria-hidden="true" className="text-text-muted">
                    {" → "}
                  </span>
                  <span className={ADDED_SIDE}>
                    <span className="visually-hidden">after: </span>
                    {formatValue(row.after)}
                  </span>
                </>
              )}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
