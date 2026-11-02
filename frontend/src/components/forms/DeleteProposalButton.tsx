// SPDX-License-Identifier: MIT
/**
 * The single place a delete action is expressed in the UI. Deletes are high-risk: this submits a
 * `submit_proposal` with `operation: "delete"` and renders the resulting pending-approval state.
 */
import { useId, useState, type JSX } from "react";

import { CrmApiError } from "../../api/CrmApiClient";
import { useCrmApi } from "../../api/useCrmApi";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import type { SubmitProposalInput } from "../../types";
import { ErrorBanner } from "./ErrorBanner";

export interface DeleteProposalButtonProps {
  readonly targetTable: SubmitProposalInput["target_table"];
  readonly targetId: string;
  /** Short label for the record, used in the confirmation prompt and status text. */
  readonly label: string;
  /** Called after a proposal is successfully submitted, e.g. to refresh a cascade-check banner. */
  readonly onSubmitted?: (proposalId: string) => void;
}

interface SubmittedProposal {
  readonly proposalId: string;
}

/**
 * `submit_proposal` returns `{"proposal": {...}}` (`crm.handlers.submit_proposal`) — the id lives
 * nested under `proposal.proposal_id`, never as a top-level field.
 */
function extractProposalId(response: unknown): string | undefined {
  if (typeof response === "object" && response !== null && "proposal" in response) {
    const proposal = (response as { proposal: unknown }).proposal;
    if (typeof proposal === "object" && proposal !== null && "proposal_id" in proposal) {
      const value = (proposal as { proposal_id: unknown }).proposal_id;
      return typeof value === "string" ? value : undefined;
    }
  }
  return undefined;
}

export function DeleteProposalButton({
  targetTable,
  targetId,
  label,
  onSubmitted,
}: DeleteProposalButtonProps): JSX.Element {
  const api = useCrmApi();
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(undefined);
  const [submitted, setSubmitted] = useState<SubmittedProposal | undefined>(undefined);
  const reasonId = useId();
  const [reason, setReason] = useState("");

  if (submitted) {
    return (
      /* `--pending-*` carries `border-style: dashed` and `--pending-hatch` as well as its hue, so
         the pending state is legible without colour. Tests match `/proposal prop-99/` and
         `/Awaiting approval/` in this text. */
      <p
        role="status"
        className="flex max-w-prose flex-wrap items-center gap-2 rounded-surface border border-emphasis border-dashed border-pending-border bg-pending-surface bg-pending-hatch px-5 py-4 text-sm leading-relaxed text-pending-text"
      >
        {/* The badge does not repeat "Awaiting approval": a second copy makes the tests'
            `/Awaiting approval/` query ambiguous ("Found multiple elements"). */}
        <Badge variant="pending">Pending</Badge>
        Delete proposed for {label}. Awaiting approval (proposal {submitted.proposalId}) — nothing
        has been deleted yet.
      </p>
    );
  }

  if (!confirming) {
    return (
      <Button type="button" variant="danger" onClick={() => setConfirming(true)}>
        Delete {label}
      </Button>
    );
  }

  async function handleConfirm(): Promise<void> {
    setSubmitting(true);
    setError(undefined);
    try {
      const response = await api.submitProposal({
        target_table: targetTable,
        target_id: targetId,
        operation: "delete",
        reason: reason.trim() || `Delete ${label} requested from the UI.`,
        transaction_key: `delete-${targetTable}-${targetId}-${Date.now()}`,
      });
      const proposalId = extractProposalId(response) ?? "pending";
      setSubmitted({ proposalId });
      onSubmitted?.(proposalId);
    } catch (err) {
      setError(err);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      role="group"
      aria-label={`Confirm delete of ${label}`}
      /* Destructive *and* gated: `--danger-*` for the consequence, with the approval requirement
         stated in the badge and the copy rather than by reusing the dashed pending treatment — a
         delete should not look like an ordinary proposal. */
      className="max-w-prose space-y-4 rounded-surface border border-hairline border-danger-border bg-danger-surface p-6"
    >
      <div className="flex flex-wrap items-center gap-3">
        <Badge variant="pending">Requires approval</Badge>
      </div>
      <p className="text-sm leading-relaxed text-danger-text">
        Deleting {label} is a high-risk change. This will submit a proposal for approval — it will
        not delete anything immediately.
      </p>
      <div className="flex flex-col gap-2">
        <label
          htmlFor={reasonId}
          className="text-xs font-medium tracking-wide text-text-secondary"
        >
          Reason (optional)
        </label>
        <Input
          id={reasonId}
          type="text"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </div>
      {error ? (
        error instanceof CrmApiError && error.kind === "validation_failed" ? (
          <p
            role="alert"
            className="rounded-surface border border-hairline border-caution-border bg-caution-surface px-5 py-3 text-sm text-caution-text"
          >
            {error.body.message}
          </p>
        ) : (
          <ErrorBanner error={error} />
        )
      ) : null}
      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="button"
          variant="danger"
          onClick={() => void handleConfirm()}
          disabled={submitting}
        >
          {submitting ? "Submitting…" : "Confirm delete proposal"}
        </Button>
        <Button
          type="button"
          variant="ghost"
          onClick={() => setConfirming(false)}
          disabled={submitting}
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}
