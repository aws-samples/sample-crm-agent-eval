// SPDX-License-Identifier: MIT
/**
 * `ConflictNotice` renders a `CrmApiError` of kind `"conflict"` (a stale proposal on approve/reject,
 * or a modified or already-reverted record on revert) as a recoverable message: what happened, why
 * the action was refused, and what to do next. The `action` prop selects the copy; a server
 * `reason` of `already_decided` or `target_missing` replaces it. When the server names the fields
 * that moved (`reason: "fields_changed"`), they render as a field / at submit / now / proposed
 * table. Styled on `--caution-*`.
 */
import type { JSX } from "react";

import type { ConflictingField } from "../../api/CrmApiClient";
import { Button } from "../ui/button";

export type ConflictAction = "approve" | "reject" | "revert";

export interface ConflictNoticeProps {
  readonly action: ConflictAction;
  /** The server's `error.reason`, when it sent one. */
  readonly reason?: string;
  /** The server's `error.conflicting_fields`, when it sent them. */
  readonly conflictingFields?: readonly ConflictingField[];
  /** While true the refresh action is in flight: its button is disabled and says so. */
  readonly refreshing?: boolean;
  /** Brings the view up to date with the record so the user can decide again. */
  onRefresh(): void;
  /** Optional: dismiss the notice without refreshing. */
  onDismiss?(): void;
}

const EXPLANATION: Record<ConflictAction, string> = {
  approve:
    "This record changed after the proposal was created, so the before/after comparison " +
    "shown is no longer accurate. Approving it now could apply a change on top of information " +
    "that is out of date.",
  reject:
    "This proposal was already decided or the underlying record changed after it was created, " +
    "so this decision can no longer be applied.",
  revert:
    "This record was already reverted, or it was modified again after the audit entry was " +
    "written, so reverting to the recorded snapshot is no longer safe.",
};

const NEXT_STEP: Record<ConflictAction, string> = {
  approve:
    "Refresh to load the record's current state into this proposal, then review the change and " +
    "decide again.",
  reject: "Refresh the queue to see its current status.",
  revert: "Refresh to see the record's current state before deciding on a next step.",
};

/** Copy for the server reasons whose recovery differs from the action's default. */
const REASON_COPY: Readonly<Record<string, { readonly explanation: string; readonly next: string }>> =
  {
    already_decided: {
      explanation: "Someone else already approved or rejected this proposal.",
      next: "Refresh the queue to see its current status.",
    },
    target_missing: {
      explanation: "The record this proposal changes has been deleted.",
      next: "Refresh the queue; this proposal can no longer be applied.",
    },
  };

function formatValue(value: unknown): string {
  if (value === undefined || value === null) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

const cell = "px-cell-x py-cell-y text-left align-top";

function ConflictingFieldsTable({
  fields,
}: {
  readonly fields: readonly ConflictingField[];
}): JSX.Element {
  return (
    <div className="overflow-x-auto rounded-surface border border-hairline border-caution-border bg-surface-raised">
      <table className="w-full border-collapse text-sm text-text-primary">
        <caption className="px-cell-x py-cell-y text-left text-xs text-text-secondary">
          Fields that changed since this proposal was submitted
        </caption>
        <thead>
          <tr className="border-b-hairline border-border-default bg-surface-inset">
            {["Field", "At submit", "Now", "Proposed"].map((heading) => (
              <th
                key={heading}
                scope="col"
                className={`${cell} text-xs font-semibold tracking-wide text-text-secondary`}
              >
                {heading}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border-subtle">
          {fields.map((row) => (
            <tr key={row.field} data-conflicting-field={row.field}>
              <th scope="row" className={`${cell} font-medium`}>
                {row.field}
              </th>
              <td className={`${cell} font-numeric text-text-secondary`}>
                {formatValue(row.snapshot)}
              </td>
              <td className={`${cell} font-numeric font-semibold text-caution-text`}>
                {formatValue(row.current)}
              </td>
              <td className={`${cell} font-numeric text-diff-added-text`}>
                {formatValue(row.proposed)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ConflictNotice({
  action,
  reason,
  conflictingFields,
  refreshing = false,
  onRefresh,
  onDismiss,
}: ConflictNoticeProps): JSX.Element {
  const reasonCopy = reason === undefined ? undefined : REASON_COPY[reason];
  const showFields =
    reason === "fields_changed" && conflictingFields !== undefined && conflictingFields.length > 0;
  return (
    <div
      role="alert"
      data-conflict-action={action}
      data-conflict-reason={reason}
      className="max-w-prose space-y-3 rounded-surface border border-hairline border-caution-border bg-caution-surface px-5 py-4 text-caution-text"
    >
      <p className="text-sm leading-relaxed">
        <strong className="font-semibold">
          Can&rsquo;t {action} — the record changed underneath this action.
        </strong>
      </p>
      <p className="text-sm leading-relaxed">{reasonCopy?.explanation ?? EXPLANATION[action]}</p>
      {showFields && <ConflictingFieldsTable fields={conflictingFields} />}
      <p className="text-sm leading-relaxed">{reasonCopy?.next ?? NEXT_STEP[action]}</p>
      <div className="flex flex-wrap items-center gap-4 pt-1">
        <Button
          type="button"
          variant="secondary"
          size="sm"
          disabled={refreshing}
          onClick={onRefresh}
        >
          {refreshing ? "Refreshing…" : "Refresh and review current state"}
        </Button>
        {onDismiss && (
          <Button type="button" variant="ghost" size="sm" disabled={refreshing} onClick={onDismiss}>
            Dismiss
          </Button>
        )}
      </div>
    </div>
  );
}
