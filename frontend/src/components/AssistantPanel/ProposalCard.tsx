// SPDX-License-Identifier: MIT
/**
 * A high-risk change the assistant is asking for, rendered as structure rather than as prose.
 *
 * High-risk changes go through `submit_proposal` and an explicit approve or reject, so this card is
 * where a user sees one. The target, the reason, and every field the change would alter are rendered
 * from `proposal_summary`; a summary with no legible field says so rather than rendering blank.
 */

import type { JSX } from "react";

import type { AgentEventFrame } from "../../types/generated/agentEvents";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";

/** Test hook for the card as a whole. The browser journeys and the axe gate both key on this. */
export const PROPOSAL_CARD_TESTID = "proposal-card";

/** Shown when a proposal carries a summary with nothing legible in it. Never rendered as silence. */
export const PROPOSAL_EMPTY_TEXT = "This proposal arrived with no readable changes. Reject it.";

/** Keys of `proposal_summary` that are metadata about the change rather than the change itself. */
const META_KEYS = ["before", "after", "reason", "target_table", "target_id"] as const;

/** One field the change would alter, with the value it replaces. */
interface FieldChange {
  readonly field: string;
  readonly before: unknown;
  readonly after: unknown;
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function asText(value: unknown): string | undefined {
  if (typeof value === "string") return value.trim() === "" ? undefined : value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return undefined;
}

/**
 * Render one value for display, or `not set` for its absence.
 */
function displayValue(value: unknown): string {
  const text = asText(value);
  if (text !== undefined) return text;
  if (value === null || value === undefined) return "not set";
  return JSON.stringify(value);
}

/**
 * Derive the field-level changes from a proposal summary, over the union of `before` and `after`
 * keys so a removed field is shown.
 */
export function fieldChanges(summary: Record<string, unknown> | null): readonly FieldChange[] {
  if (summary === null) return [];
  const before = asRecord(summary["before"]) ?? {};
  const after = asRecord(summary["after"]) ?? {};
  const fields = [...new Set([...Object.keys(before), ...Object.keys(after)])].sort();
  return fields
    .filter((field) => !Object.is(before[field], after[field]))
    .map((field) => ({ field, before: before[field], after: after[field] }));
}

/**
 * How far a decision on this proposal has got. Supplied from outside, since the card renders from an
 * immutable event frame.
 */
export type ProposalDecision = "deciding" | "approved" | "rejected";

/** What the badge says for each settled decision. Exported so the tests name them once. */
export const DECISION_BADGE_TEXT = {
  deciding: "Sending your decision",
  approved: "Approved",
  rejected: "Rejected",
} as const;

export interface ProposalCardProps {
  readonly event: AgentEventFrame;
  /** Rendered only when supplied, so the card never shows a control that is wired to nothing. */
  readonly onApprove?: (proposalId: string) => void;
  readonly onReject?: (proposalId: string) => void;
  /** Absent while the proposal is still pending. A settled value withdraws the controls. */
  readonly decision?: ProposalDecision;
}

export function ProposalCard({
  event,
  onApprove,
  onReject,
  decision,
}: ProposalCardProps): JSX.Element {
  const summary = event.proposal_summary;
  const changes = fieldChanges(summary);
  const reason = asText(summary?.["reason"]);
  const targetTable = asText(summary?.["target_table"]);
  const targetId = asText(summary?.["target_id"]);
  const proposalId = event.proposal_id;

  /*
   * Anything the summary carries beyond the five known keys is still shown.
   */
  const extras = Object.entries(summary ?? {}).filter(
    ([key, value]) =>
      !(META_KEYS as readonly string[]).includes(key) && asText(value) !== undefined,
  );

  const hasContent = changes.length > 0 || reason !== undefined || extras.length > 0;

  return (
    <article
      data-testid={PROPOSAL_CARD_TESTID}
      aria-labelledby={`proposal-heading-${proposalId ?? "unknown"}`}
      className="flex flex-col gap-4 rounded-surface border border-hairline border-caution-border bg-surface-raised p-5 shadow-overlay"
    >
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h3
          id={`proposal-heading-${proposalId ?? "unknown"}`}
          className="text-sm font-semibold text-text-primary"
        >
          Approval needed
        </h3>
        {decision === undefined ? (
          <Badge variant="caution">Waiting for you</Badge>
        ) : (
          <Badge
            data-testid="proposal-decision"
            variant={
              decision === "approved" ? "positive" : decision === "rejected" ? "neutral" : "pending"
            }
          >
            {DECISION_BADGE_TEXT[decision]}
          </Badge>
        )}
      </header>

      {targetTable !== undefined || targetId !== undefined ? (
        <p data-testid="proposal-target" className="text-xs text-text-secondary">
          Changes {targetTable ?? "a record"}
          {targetId === undefined ? "" : ` ${targetId}`}
        </p>
      ) : null}

      {reason === undefined ? null : (
        <p data-testid="proposal-reason" className="text-xs leading-relaxed text-text-secondary">
          {reason}
        </p>
      )}

      {changes.length > 0 ? (
        <dl data-testid="proposal-changes" className="flex flex-col gap-3">
          {changes.map((change) => (
            <div key={change.field} className="flex flex-col gap-1">
              <dt className="text-2xs font-medium text-text-secondary">{change.field}</dt>
              <dd className="flex flex-wrap items-baseline gap-2 text-xs">
                <span className="rounded-surface bg-diff-removed-surface px-2 py-1 text-text-secondary line-through">
                  {displayValue(change.before)}
                </span>
                <span aria-hidden="true" className="text-text-secondary">
                  becomes
                </span>
                <span className="rounded-surface bg-accent-surface px-2 py-1 font-medium text-accent-text">
                  {displayValue(change.after)}
                </span>
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      {extras.length > 0 ? (
        <dl data-testid="proposal-extras" className="flex flex-col gap-1 text-2xs">
          {extras.map(([key, value]) => (
            <div key={key} className="flex gap-2">
              <dt className="text-text-secondary">{key}</dt>
              <dd className="text-text-primary">{String(value)}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      {hasContent ? null : (
        <p
          data-testid="proposal-empty"
          className="rounded-surface bg-denied-surface p-3 text-xs text-denied-text"
        >
          {PROPOSAL_EMPTY_TEXT}
        </p>
      )}

      {/*
        A settled decision withdraws both controls. While a decision is in flight they stay rendered
        but disabled, so a second press cannot race the first.
      */}
      {decision === "approved" || decision === "rejected" ? null : onApprove === undefined &&
        onReject === undefined ? null : (
        <div className="flex flex-wrap gap-2">
          {onReject === undefined || proposalId === null ? null : (
            <Button
              variant="ghost"
              size="sm"
              data-testid="proposal-reject"
              disabled={decision === "deciding"}
              onClick={() => onReject(proposalId)}
            >
              Reject
            </Button>
          )}
          {onApprove === undefined || proposalId === null ? null : (
            <Button
              size="sm"
              data-testid="proposal-approve"
              disabled={decision === "deciding"}
              onClick={() => onApprove(proposalId)}
            >
              Approve change
            </Button>
          )}
        </div>
      )}
    </article>
  );
}
