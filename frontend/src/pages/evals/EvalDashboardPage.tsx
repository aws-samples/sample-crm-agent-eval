// SPDX-License-Identifier: MIT
/**
 * Eval dashboard: start a sweep, follow the focused run to its end, render its results and
 * artifacts, and list every run newest first.
 *
 * On mount the page loads the first history page and focuses the newest non-terminal run, so a sweep
 * started elsewhere (or before a reload) is followed here. Anyone can start a stub sweep. Admins
 * (ID-token `cognito:groups`, cosmetic only; the server enforces) also see "Run live sweep", which
 * opens the cost estimate before anything is submitted, and "Cancel sweep" on a queued or running
 * live run.
 *
 * This page and its children render numbers only, with no verdict, winner, or recommendation;
 * `evals.test.tsx` scans the source for those words.
 */
import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { JSX } from "react";

import { useAuth, type AuthContextValue } from "../../auth/AuthProvider";
import { useIsAdmin } from "../../auth/adminGroup";
import { Button } from "../../components/ui/button";
import { formatUsd } from "../../lib/format";
import {
  HttpEvalRunClient,
  type EvalCancelDisposition,
  type EvalRunClient,
} from "./EvalRunClient";
import { EvalArmResults } from "./EvalArmResults";
import { EvalArtifactList } from "./EvalArtifactList";
import { LiveSweepDialog, RepeatsSelect } from "./LiveSweepDialog";
import { RunHistory } from "./RunHistory";
import { classifyRunMode } from "./runMode";
import { erroredItemsCostNote } from "./types";
import { toArtifactLinks, useEvalRun } from "./useEvalRun";
import { STATUS_LABEL, describeProgress, describeRunSpend, tasksRan, useRunHistory } from "./useRunHistory";

/** How a run's mode is named in the progress line. An unrecognised value says so (see `runMode.ts`). */
const RUNNING_MODE_LABEL: Readonly<Record<ReturnType<typeof classifyRunMode>, string>> = {
  stub: "stub",
  live: "live",
  unrecognised: "unrecognised-mode",
};

/** What a cancel request did, in the page's words. */
const CANCEL_OUTCOME: Readonly<Record<EvalCancelDisposition, string>> = {
  accepted:
    "Cancel requested. The task in flight finishes and is counted; the remaining tasks are skipped.",
  already_requested: "A cancel had already been requested for this run.",
  already_terminal: "The run had already finished, so nothing was cancelled.",
};

const NOTICE = "rounded-control border border-hairline px-5 py-4 font-sans text-sm leading-snug";
const NOTICE_INFO = `${NOTICE} border-info-border bg-info-surface text-info-text`;
const NOTICE_EMPTY = `${NOTICE} border-empty-border bg-empty-surface text-empty-text`;
const NOTICE_DANGER = `${NOTICE} border-danger-border bg-danger-surface text-danger-text`;
const NOTICE_POSITIVE = `${NOTICE} border-positive-border bg-positive-surface text-positive-text`;
const FIELD = "rounded-control border border-hairline border-border-subtle bg-surface-inset px-5 py-4";
const TERM = "font-sans text-2xs font-semibold leading-snug text-text-secondary";
const VALUE = "mt-2 block font-numeric text-sm leading-snug text-text-primary";
const SECTION =
  "flex flex-col gap-5 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6";
const SECTION_HEADING = "font-sans text-lg font-semibold leading-snug text-text-primary";
const HELP = "max-w-prose font-sans text-2xs leading-normal text-text-muted";

export interface EvalDashboardPageProps {
  /** The API base URL (`RuntimeConfig.apiBaseUrl`, always `/api`). */
  readonly apiBaseUrl: string;
  /** Injected in tests in place of `HttpEvalRunClient.fromAuth(...)`. */
  readonly client?: EvalRunClient;
  /**
   * Injected in tests in place of the real `useAuth()` context value. Production call sites leave
   * it undefined, so `useAuth()` is the source of identity.
   */
  readonly authOverride?: AuthContextValue;
  /** Overrides `POLL_INTERVAL_MS` (`useEvalRun.ts`); tests pass a short one. */
  readonly pollIntervalMs?: number;
}

export function EvalDashboardPage({
  apiBaseUrl,
  client,
  authOverride,
  pollIntervalMs,
}: EvalDashboardPageProps): JSX.Element {
  return authOverride === undefined ? (
    <EvalDashboardPageWithContextAuth
      apiBaseUrl={apiBaseUrl}
      client={client}
      pollIntervalMs={pollIntervalMs}
    />
  ) : (
    <EvalDashboardPageBody
      apiBaseUrl={apiBaseUrl}
      client={client}
      pollIntervalMs={pollIntervalMs}
      auth={authOverride}
    />
  );
}

type BodyProps = Pick<EvalDashboardPageProps, "apiBaseUrl" | "client" | "pollIntervalMs">;

/** Reads `useAuth()` unconditionally (Rules of Hooks) and hands the resolved value to the body.
 * Only rendered when the caller did not inject `authOverride` — i.e. in production. */
function EvalDashboardPageWithContextAuth(props: BodyProps): JSX.Element {
  const auth = useAuth();
  return <EvalDashboardPageBody {...props} auth={auth} />;
}

/** A wire string as sent, or `not reported` when the payload left it out. */
function orNotReported(value: unknown): string {
  return typeof value === "string" && value !== "" ? value : "not reported";
}

function Field({
  label,
  value,
  note,
}: {
  readonly label: string;
  readonly value: string;
  readonly note?: string;
}): JSX.Element {
  return (
    <div className={FIELD}>
      <dt className={TERM}>{label}</dt>
      <dd className={VALUE}>
        {value}
        {note !== undefined ? (
          <span className="mt-1 block max-w-prose font-sans text-2xs leading-normal text-text-muted">
            {note}
          </span>
        ) : null}
      </dd>
    </div>
  );
}

function EvalDashboardPageBody({
  apiBaseUrl,
  client,
  pollIntervalMs,
  auth,
}: BodyProps & { readonly auth: AuthContextValue }): JSX.Element {
  const [runClient] = useState<EvalRunClient>(
    () => client ?? HttpEvalRunClient.fromAuth(apiBaseUrl, auth),
  );
  const isAdmin = useIsAdmin(auth);
  const history = useRunHistory(runClient);
  const { upsert } = history;
  const run = useEvalRun(runClient, { pollIntervalMs, onRunUpdate: upsert });
  const { focus } = run;
  const [liveDialogOpen, setLiveDialogOpen] = useState(false);
  const [stubRepeats, setStubRepeats] = useState(1);

  const stubHelpId = useId();
  const liveHelpId = useId();

  // Follow the newest non-terminal run once, when the first history page arrives.
  const resumed = useRef(false);
  const focusedRun = run.run;
  useEffect(() => {
    if (resumed.current || history.loading) return;
    resumed.current = true;
    if (focusedRun !== undefined) return;
    const active = history.runs.find((candidate) => !candidate.is_terminal);
    if (active !== undefined) focus(active);
  }, [focus, focusedRun, history.loading, history.runs]);

  const { startLive } = run;
  const confirmLive = useCallback(async (repeats: number) => {
    const failure = await startLive(repeats);
    if (failure === undefined) setLiveDialogOpen(false);
    return failure;
  }, [startLive]);

  const focused = run.run;
  const erroredItemsNote =
    focused !== undefined ? erroredItemsCostNote(focused.tasks_failed) : undefined;
  const busy = run.starting || (focused !== undefined && !focused.is_terminal);
  const modeLabel = focused === undefined ? "" : RUNNING_MODE_LABEL[classifyRunMode(focused.mode)];
  const canCancel =
    isAdmin &&
    focused !== undefined &&
    classifyRunMode(focused.mode) === "live" &&
    (focused.status === "queued" || focused.status === "running") &&
    !focused.cancel_requested;
  const showResults =
    focused !== undefined && (focused.status === "completed" || focused.status === "cancelled");
  const matrix = focused?.matrix;
  const artifacts = toArtifactLinks(focused);

  return (
    <main
      aria-labelledby="eval-dashboard-heading"
      className="mx-auto flex w-full max-w-content flex-col gap-9 px-6 py-9 font-sans text-text-primary"
    >
      <h1
        id="eval-dashboard-heading"
        className="font-sans text-xl font-semibold leading-tight tracking-tight text-text-primary"
      >
        Model evaluation dashboard
      </h1>
      <p className="max-w-prose font-sans text-sm leading-relaxed text-text-secondary">
        Runs the default sweep, every golden task on each model family at medium supervisor effort,
        over the same code path as{" "}
        <code className="font-numeric text-xs text-text-primary">python -m eval.harness</code>. A stub
        sweep makes no model call. A sweep keeps running on the server if you leave this page, and is
        picked up again when you return.
      </p>

      <section aria-labelledby="eval-trigger-heading" className={SECTION}>
        <h2 id="eval-trigger-heading" className={SECTION_HEADING}>
          Run a sweep
        </h2>

        <RepeatsSelect
          value={stubRepeats}
          onChange={setStubRepeats}
          disabled={busy}
          help="How many times the stub sweep runs every task on every arm, each time in a fresh tenant."
        />
        <div className="flex flex-wrap items-start gap-4">
          <Button
            type="button"
            variant="primary"
            onClick={() => void run.startStub(stubRepeats)}
            disabled={busy}
            aria-describedby={stubHelpId}
          >
            Run stub sweep (zero cost, no credentials)
          </Button>
          {isAdmin ? (
            <Button
              type="button"
              variant="secondary"
              onClick={() => setLiveDialogOpen(true)}
              disabled={busy}
              aria-describedby={liveHelpId}
            >
              Run live sweep…
            </Button>
          ) : null}
        </div>
        <p id={stubHelpId} className={HELP}>
          The stub provider returns deterministic responses without calling any model, so a stub
          sweep incurs no inference cost.
        </p>
        {isAdmin ? (
          <p id={liveHelpId} className={HELP}>
            Admins only. A live sweep calls real Bedrock models and is billed; the cost estimate is
            shown before anything starts. One sweep runs at a time.
          </p>
        ) : null}
      </section>

      <section aria-live="polite" aria-labelledby="eval-progress-heading" className={SECTION}>
        <h2 id="eval-progress-heading" className={SECTION_HEADING}>
          Progress
        </h2>

        {focused === undefined && !run.starting ? (
          <p className={NOTICE_EMPTY}>
            No run is in progress. Start a sweep, or open one from the run history.
          </p>
        ) : null}
        {focused === undefined && run.starting ? (
          <p role="status" className={NOTICE_INFO}>
            Starting the sweep…
          </p>
        ) : null}

        {focused !== undefined && !focused.is_terminal ? (
          <p role="status" className={NOTICE_INFO}>
            {focused.status === "queued" ? "Queued" : "Running"} {modeLabel} sweep (run_id=
            {focused.run_id})… {describeProgress(focused)}.
            {focused.cancel_requested ? " Cancel requested; the remaining tasks will be skipped." : ""}
          </p>
        ) : null}
        {focused?.status === "completed" ? (
          <p role="status" className={NOTICE_POSITIVE}>
            Run complete.
          </p>
        ) : null}
        {focused?.status === "cancelled" ? (
          <p role="status" className={NOTICE_INFO}>
            Cancelled after {tasksRan(focused)}/{focused.tasks_total} tasks
            {focused.tasks_skipped > 0 ? ` (${focused.tasks_skipped} skipped)` : ""}. Spend:{" "}
            {describeRunSpend(focused)}.
            {erroredItemsNote !== undefined ? ` ${erroredItemsNote}` : ""}
          </p>
        ) : null}
        {focused?.status === "failed" ? (
          <p role="alert" className={NOTICE_DANGER}>
            Run failed: {focused.error ?? "no further detail was reported."}
          </p>
        ) : null}
        {run.pollError !== undefined ? (
          <p role="alert" className={NOTICE_DANGER}>
            {run.pollError}
          </p>
        ) : null}
        {run.actionError !== undefined && !liveDialogOpen ? (
          <p role="alert" className={NOTICE_DANGER}>
            {run.actionError}
          </p>
        ) : null}
        {run.cancelDisposition !== undefined ? (
          <p role="status" className={NOTICE_INFO}>
            {CANCEL_OUTCOME[run.cancelDisposition]}
          </p>
        ) : null}

        {canCancel ? (
          <Button
            type="button"
            variant="danger"
            className="self-start"
            onClick={() => void run.cancel()}
            disabled={run.cancelling}
          >
            {run.cancelling ? "Requesting cancel…" : "Cancel sweep"}
          </Button>
        ) : null}

        {/* The run's own record from `RunRecord.to_payload()`. Timestamps are rendered exactly as
            the backend sent them (ISO-8601, unformatted). */}
        {focused !== undefined ? (
          <dl className="grid items-start gap-4 md:grid-cols-2">
            <Field label="Run id" value={focused.run_id} />
            <Field label="Eval tenant" value={orNotReported(focused.tenant)} />
            <Field label="Status" value={STATUS_LABEL[focused.status]} />
            <Field label="Tasks" value={describeProgress(focused)} />
            <Field
              label="Spend"
              value={describeRunSpend(focused)}
              note={erroredItemsNote}
            />
            <Field label="Started by" value={focused.started_by_rep_id ?? "not reported"} />
            <Field label="Submitted at" value={orNotReported(focused.submitted_at)} />
            <Field label="Running since" value={focused.running_at ?? "not started"} />
            <Field label="Finished at" value={focused.finished_at ?? "not finished"} />
            <Field label="Tenant teardown" value={focused.teardown_status ?? "not reported"} />
            {focused.estimate_usd !== undefined ? (
              <Field
                label="Estimate at submit"
                value={`${formatUsd(focused.estimate_usd)}${
                  focused.estimate_basis ? ` (${focused.estimate_basis} basis)` : ""
                }`}
              />
            ) : null}
            {focused.cancel_requested ? (
              <Field
                label="Cancel requested"
                value={`by ${focused.cancel_requested_by ?? "not reported"} at ${
                  focused.cancel_requested_at ?? "not reported"
                }`}
              />
            ) : null}
          </dl>
        ) : null}
      </section>

      {showResults && !matrix ? (
        <p
          role={focused.status === "completed" ? "alert" : "status"}
          className={focused.status === "completed" ? NOTICE_DANGER : NOTICE_EMPTY}
        >
          {focused.status === "completed"
            ? "The run reported completion but returned no result matrix, so no measurements can be shown. Nothing here should be read as a completed evaluation."
            : "No task finished before the cancel, so this run has no result matrix."}
        </p>
      ) : null}

      {showResults && matrix ? (
        <section aria-labelledby="eval-results-heading" className="flex flex-col gap-6">
          <h2 id="eval-results-heading" className={SECTION_HEADING}>
            Results
          </h2>
          <p className="max-w-prose font-sans text-sm leading-relaxed text-text-secondary">
            Numbers only — this dashboard does not label either model as better. Read the figures
            below alongside the mode label for each arm.
            {focused.status === "cancelled"
              ? " This run was cancelled, so the figures cover only the tasks that finished."
              : ""}
          </p>
          {/* Keyed on model and effort: `model_id` alone is not unique across the matrix. */}
          {matrix.arms.map((arm) => (
            <EvalArmResults
              key={`${arm.provenance.model_id}#${arm.provenance.effort ?? "no-effort"}`}
              arm={arm}
              tasksFailed={focused.tasks_failed}
            />
          ))}
        </section>
      ) : null}

      {/* An absent artifact manifest is stated rather than omitted. */}
      {showResults && (focused.status === "completed" || matrix || artifacts) ? (
        artifacts ? (
          <EvalArtifactList artifacts={artifacts} runId={focused.run_id} client={runClient} />
        ) : (
          <p
            role="status"
            className="rounded-control border border-hairline border-dashed border-empty-border bg-empty-surface px-5 py-4 font-sans text-sm leading-snug text-empty-text"
          >
            No artifact manifest was reported for this run, so none of the run&rsquo;s artifacts can
            be linked.
          </p>
        )
      ) : null}

      <RunHistory
        runs={history.runs}
        loading={history.loading}
        loadingMore={history.loadingMore}
        error={history.error}
        hasMore={history.hasMore}
        canRetry={history.firstPageFailed}
        focusedRunId={focused?.run_id}
        onOpen={focus}
        onLoadMore={() => void history.loadMore()}
        onRetry={() => void history.retry()}
      />

      {liveDialogOpen ? (
        <LiveSweepDialog
          client={runClient}
          onConfirm={confirmLive}
          onClose={() => setLiveDialogOpen(false)}
        />
      ) : null}
    </main>
  );
}
