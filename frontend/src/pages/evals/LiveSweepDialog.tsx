// SPDX-License-Identifier: MIT
/**
 * The live-sweep confirmation: a Repeats select, the server's cost estimate for the selected repeats
 * with its basis, and only then the button that submits `{mode: "live", confirmed: true}` with
 * those repeats.
 *
 * The estimate is shown, not enforced. The confirm button stays disabled until the estimate for the
 * selected repeats has loaded, so a live sweep is never started without its expected spend on screen.
 */
import { useEffect, useId, useState } from "react";
import type { JSX } from "react";

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
import { formatUsd } from "../../lib/format";
import { MAX_SWEEP_REPEATS } from "./EvalRunClient";
import type { EvalEstimateResponse, EvalRunClient } from "./EvalRunClient";

const NOTICE = "rounded-surface border border-hairline px-5 py-3 text-sm leading-relaxed";

const REPEAT_OPTIONS = Array.from({ length: MAX_SWEEP_REPEATS }, (_, index) => index + 1);

const SELECT = cn(
  "h-control-md w-full rounded-control border border-hairline border-border-control bg-surface-raised px-4 text-sm text-text-primary disabled:cursor-not-allowed disabled:opacity-60",
  focusRing,
);

/** How the estimate's basis is described. */
function describeBasis(estimate: EvalEstimateResponse): string {
  if (estimate.basis === "baseline") {
    return estimate.basis_run_id === undefined
      ? "Based on the newest completed live sweep."
      : `Based on the newest completed live sweep (${estimate.basis_run_id}).`;
  }
  if (estimate.basis === "static") {
    return "Based on the static per-task token figures in config/eval_live_estimate.yaml.";
  }
  return `Basis reported as ${estimate.basis}.`;
}

/** The sweep size, derived from the estimate: tasks per arm, arms, repeats, and their product. */
function describeSize(estimate: EvalEstimateResponse): string {
  const arms = Object.keys(estimate.per_arm_usd).length;
  const repeats = estimate.repeats ?? 1;
  const repeatsPart = repeats === 1 ? "" : `, ${repeats} times over`;
  return (
    `${estimate.task_count} tasks on each of ${arms} ${arms === 1 ? "arm" : "arms"}${repeatsPart} ` +
    `(${estimate.task_count * arms * repeats} agent runs).`
  );
}

/** The outcome of the estimate request for one repeats value. */
interface LoadedEstimate {
  readonly repeats: number;
  readonly estimate?: EvalEstimateResponse;
  readonly error?: string;
}

export interface RepeatsSelectProps {
  readonly value: number;
  readonly onChange: (repeats: number) => void;
  readonly disabled?: boolean;
  /** The accessible description shown under the select. */
  readonly help: string;
}

/** The labelled "Repeats" select, 1 to {@link MAX_SWEEP_REPEATS}; used by the dialog and the page. */
export function RepeatsSelect({ value, onChange, disabled, help }: RepeatsSelectProps): JSX.Element {
  const selectId = useId();
  const helpId = useId();
  return (
    <div className="flex flex-col gap-2">
      <label htmlFor={selectId} className="text-xs font-medium tracking-wide text-text-secondary">
        Repeats
      </label>
      <select
        id={selectId}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        disabled={disabled}
        aria-describedby={helpId}
        className={SELECT}
      >
        {REPEAT_OPTIONS.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
      <p id={helpId} className="font-sans text-2xs leading-normal text-text-muted">
        {help}
      </p>
    </div>
  );
}

export interface LiveSweepDialogProps {
  readonly client: EvalRunClient;
  /**
   * Submits the confirmed live sweep of `repeats` repeats per arm. Resolves to the failure message,
   * or `undefined` when the run started; the parent closes the dialog on success.
   */
  readonly onConfirm: (repeats: number) => Promise<string | undefined>;
  readonly onClose: () => void;
}

/** Mounted only while open; dismissal is routed to `onClose`. */
export function LiveSweepDialog({ client, onConfirm, onClose }: LiveSweepDialogProps): JSX.Element {
  const [repeats, setRepeats] = useState(1);
  const [loaded, setLoaded] = useState<LoadedEstimate | undefined>(undefined);
  const [submitting, setSubmitting] = useState(false);
  const [startError, setStartError] = useState<string | undefined>(undefined);
  const current = loaded?.repeats === repeats ? loaded : undefined;
  const estimate = current?.estimate;
  const estimateError = current?.error;

  useEffect(() => {
    let active = true;
    client.estimateLiveRun(repeats).then(
      (response) => {
        if (active) setLoaded({ repeats, estimate: response });
      },
      (err: unknown) => {
        if (active) {
          setLoaded({
            repeats,
            error:
              err instanceof Error && err.message ? err.message : "the estimate could not be loaded",
          });
        }
      },
    );
    return () => {
      active = false;
    };
  }, [client, repeats]);

  async function confirm(): Promise<void> {
    setSubmitting(true);
    setStartError(undefined);
    let failure: string | undefined;
    try {
      failure = await onConfirm(repeats);
    } catch (err) {
      failure = err instanceof Error && err.message ? err.message : "the live sweep could not be started";
    }
    setSubmitting(false);
    setStartError(failure);
  }

  return (
    <Dialog open onOpenChange={(next) => !next && !submitting && onClose()}>
      <DialogContent onInteractOutside={(event) => event.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Start a live sweep?</DialogTitle>
          <DialogDescription>
            A live sweep runs every golden task on each arm against real Bedrock models and is
            billed to this account.
          </DialogDescription>
        </DialogHeader>

        <RepeatsSelect
          value={repeats}
          onChange={setRepeats}
          disabled={submitting}
          help="Each repeat runs every task on every arm again, in its own fresh tenant."
        />

        {estimate === undefined && estimateError === undefined ? (
          <p role="status" className={`${NOTICE} border-info-border bg-info-surface text-info-text`}>
            Loading the cost estimate…
          </p>
        ) : null}

        {estimateError !== undefined ? (
          <p
            role="alert"
            className={`${NOTICE} border-danger-border bg-danger-surface text-danger-text`}
          >
            The cost estimate is unavailable ({estimateError}), so a live sweep cannot be started
            from here.
          </p>
        ) : null}

        {estimate !== undefined ? (
          <dl className="grid gap-4">
            <div className="rounded-control border border-hairline border-border-subtle bg-surface-inset px-5 py-4">
              <dt className="font-sans text-2xs font-semibold leading-snug text-text-secondary">
                Estimated spend
              </dt>
              <dd className="mt-2 font-numeric text-xl font-semibold leading-tight text-text-primary">
                {formatUsd(estimate.estimate_usd)}
              </dd>
              <dd className="mt-2 max-w-prose font-sans text-2xs leading-normal text-text-muted">
                {describeSize(estimate)} {describeBasis(estimate)} An estimate, not a limit: the
                sweep is not stopped if it costs more.
              </dd>
              {estimate.effort_caveat !== undefined ? (
                <dd className="mt-2 max-w-prose font-sans text-2xs leading-normal text-text-secondary">
                  {estimate.effort_caveat}
                </dd>
              ) : null}
            </div>
            {Object.entries(estimate.per_arm_usd).map(([arm, cost]) => (
              <div
                key={arm}
                className="flex items-baseline justify-between gap-4 border-b-hairline border-border-subtle pb-3"
              >
                <dt className="font-numeric text-xs text-text-secondary">{arm}</dt>
                <dd className="font-numeric text-xs tabular-nums text-text-primary">
                  {formatUsd(cost)}
                </dd>
              </div>
            ))}
          </dl>
        ) : null}

        {startError !== undefined ? (
          <p
            role="alert"
            className={`${NOTICE} mt-5 border-danger-border bg-danger-surface text-danger-text`}
          >
            The live sweep was not started: {startError}
          </p>
        ) : null}

        <DialogFooter>
          <Button type="button" variant="secondary" onClick={onClose} disabled={submitting}>
            Close
          </Button>
          <Button
            type="button"
            variant="primary"
            onClick={() => void confirm()}
            disabled={estimate === undefined || submitting}
          >
            {submitting ? "Starting live sweep…" : "Start live sweep"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
