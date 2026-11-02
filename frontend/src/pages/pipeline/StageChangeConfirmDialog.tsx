// SPDX-License-Identifier: MIT
/**
 * Confirmation dialog collecting the required `reason` before a stage-change proposal is submitted.
 * Shown for both the mouse-drag and keyboard-move paths; it calls only `submitProposal`.
 */
import { useId, useState } from "react";
import type { FormEvent, JSX } from "react";

import type { OpportunityStage } from "../../types/domain";
import { transitionRefusalReason } from "./stageTransitions";
import type { TransitionKind } from "./stageTransitions";
import { Button } from "../../components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../components/ui/dialog";
import { cn, focusRing } from "../../components/ui/utils";

const fieldLabel = "block text-sm font-medium leading-snug text-text-primary";

/** A multi-line control styled to match `components/ui/input.tsx`. Height comes from `rows`. */
const textareaControl = cn(
  "w-full rounded-control border border-hairline border-border-control",
  "bg-surface-raised px-4 py-3 font-sans text-sm leading-relaxed text-text-primary",
  "placeholder:text-text-muted",
  focusRing,
);

export interface StageChangeConfirmDialogProps {
  readonly opportunityName: string;
  readonly fromStage: OpportunityStage;
  readonly toStage: OpportunityStage;
  readonly transitionKind: TransitionKind;
  readonly onConfirm: (reason: string) => Promise<void>;
  readonly onCancel: () => void;
}

export function StageChangeConfirmDialog({
  opportunityName,
  fromStage,
  toStage,
  transitionKind,
  onConfirm,
  onCancel,
}: StageChangeConfirmDialogProps): JSX.Element {
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | undefined>(undefined);
  const reasonId = useId();

  const refusal = transitionRefusalReason(transitionKind);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!reason.trim()) {
      setError("A reason is required to propose this stage change.");
      return;
    }
    setSubmitting(true);
    setError(undefined);
    try {
      await onConfirm(reason.trim());
    } catch {
      setError("Could not submit the proposal. Please try again.");
      setSubmitting(false);
    }
  }

  return (
    /* The `Dialog` primitive supplies the focus trap, `aria-modal` and Escape-to-close. `open` is
       constant because the parent mounts this component only while a move is pending; dismissal is
       routed to `onCancel`. */
    <Dialog open onOpenChange={(next) => !next && onCancel()}>
      {/* Labelling is left to Radix: overriding `DialogTitle`'s generated id detaches its
          title-detection. Interaction outside is prevented so an outside click or focus shift
          cannot discard a typed reason; Escape and Cancel still close. */}
      <DialogContent onInteractOutside={(event) => event.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Propose stage change</DialogTitle>
          <DialogDescription>
            Move <strong className="font-semibold text-text-primary">{opportunityName}</strong> from{" "}
            {fromStage} to {toStage}?
          </DialogDescription>
        </DialogHeader>
        {refusal ? (
          <>
            <p
              role="alert"
              className="rounded-surface border border-hairline border-denied-border bg-denied-surface px-5 py-3 text-sm leading-relaxed text-denied-text"
            >
              {refusal}
            </p>
            <DialogFooter>
              <Button type="button" variant="secondary" onClick={onCancel}>
                Close
              </Button>
            </DialogFooter>
          </>
        ) : (
          /* `noValidate`: the empty-reason check below enforces `required` so its message is
             announced and tied to the field with `aria-invalid`. */
          <form onSubmit={handleSubmit} noValidate className="space-y-5">
            <p className="rounded-surface border border-hairline border-info-border bg-info-surface px-5 py-3 text-sm leading-relaxed text-info-text">
              This is a high-risk change. It will be submitted as a proposal awaiting approval —
              nothing changes until it is approved.
            </p>
            <div className="space-y-3">
              <label htmlFor={reasonId} className={fieldLabel}>
                Reason
              </label>
              <textarea
                id={reasonId}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                required
                rows={3}
                autoFocus
                aria-invalid={error !== undefined}
                className={textareaControl}
              />
            </div>
            {error && (
              <p
                role="alert"
                className="rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-3 text-sm leading-relaxed text-danger-text"
              >
                {error}
              </p>
            )}
            <DialogFooter>
              <Button type="button" variant="secondary" onClick={onCancel} disabled={submitting}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" disabled={submitting}>
                {submitting ? "Submitting…" : "Submit proposal"}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
