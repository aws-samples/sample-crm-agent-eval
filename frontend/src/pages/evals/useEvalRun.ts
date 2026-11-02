// SPDX-License-Identifier: MIT
/**
 * The focused run: the one run the dashboard shows in detail. A run becomes focused when this page
 * starts it, when it is the newest non-terminal run on mount, or when it is opened from history. A
 * non-terminal focused run is polled until the backend reports `is_terminal` or a poll fails, then
 * rendered as it stands, with no reload. A cancel that returns a non-terminal run restarts polling.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import type {
  EvalCancelDisposition,
  EvalRunClient,
  EvalRunStatusResponse,
} from "./EvalRunClient";
import type { EvalArtifactLinks } from "./types";

/** How often a non-terminal focused run is polled. */
export const POLL_INTERVAL_MS = 3000;

/** Convert the wire artifact manifest into the client-side links. */
export function toArtifactLinks(
  response: EvalRunStatusResponse | undefined,
): EvalArtifactLinks | undefined {
  if (!response?.artifacts) return undefined;
  return {
    matrixJson: response.artifacts.matrix_json,
    matrixMd: response.artifacts.matrix_md,
    matrixCsv: response.artifacts.matrix_csv,
    raw: response.artifacts.raw,
  };
}

function messageOf(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

/** The poll-failure wording: what failed, that updates stopped, and how to resume them. */
function pollStopped(reason: string): string {
  return `Status updates stopped: ${reason}. Open the run from the run history to resume them.`;
}

export interface UseEvalRunOptions {
  /** Overrides {@link POLL_INTERVAL_MS}; tests pass a short interval. */
  readonly pollIntervalMs?: number;
  /** Called with every run state this hook learns, so history can show it too. */
  readonly onRunUpdate?: (run: EvalRunStatusResponse) => void;
}

export interface UseEvalRunResult {
  /** The focused run as last reported, or `undefined` before any. */
  readonly run: EvalRunStatusResponse | undefined;
  /** True while a submit request is in flight. */
  readonly starting: boolean;
  /** True while a cancel request is in flight. */
  readonly cancelling: boolean;
  /** Why polling stopped, until the run is focused again or a cancel restarts polling. */
  readonly pollError: string | undefined;
  /** The last start or cancel failure, as the backend worded it, until the next action. */
  readonly actionError: string | undefined;
  /** What the last cancel request did, until another run is focused. */
  readonly cancelDisposition: EvalCancelDisposition | undefined;
  /**
   * Start a stub sweep of `repeats` (default 1) repeats per arm and focus it. No parameter can
   * request a live sweep. Resolves to the failure message, or `undefined` when the run started.
   */
  startStub(repeats?: number): Promise<string | undefined>;
  /** Start a confirmed live sweep of `repeats` (default 1) and focus it. Resolves like {@link startStub}. */
  startLive(repeats?: number): Promise<string | undefined>;
  /** Request a cooperative cancel of the focused run. */
  cancel(): Promise<void>;
  /** Focus `run`, polling it when it is not terminal. */
  focus(run: EvalRunStatusResponse): void;
}

/** Drives the focused run via {@link EvalRunClient}: start, poll to terminal, cancel. */
export function useEvalRun(client: EvalRunClient, options: UseEvalRunOptions = {}): UseEvalRunResult {
  const { pollIntervalMs = POLL_INTERVAL_MS, onRunUpdate } = options;
  const [run, setRun] = useState<EvalRunStatusResponse | undefined>(undefined);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [pollError, setPollError] = useState<string | undefined>(undefined);
  const [actionError, setActionError] = useState<string | undefined>(undefined);
  const [cancelDisposition, setCancelDisposition] = useState<EvalCancelDisposition | undefined>(
    undefined,
  );
  const pollTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const focusedId = useRef<string | undefined>(undefined);
  const unmounted = useRef(false);
  const onRunUpdateRef = useRef(onRunUpdate);
  useEffect(() => {
    onRunUpdateRef.current = onRunUpdate;
  }, [onRunUpdate]);

  const stopPolling = useCallback(() => {
    if (pollTimer.current !== undefined) {
      clearTimeout(pollTimer.current);
      pollTimer.current = undefined;
    }
  }, []);

  useEffect(() => {
    unmounted.current = false;
    return () => {
      unmounted.current = true;
      stopPolling();
    };
  }, [stopPolling]);

  /** Record `report` if it is for the focused run and the hook is mounted; returns whether it was. */
  const accept = useCallback((report: EvalRunStatusResponse): boolean => {
    if (unmounted.current || report.run_id !== focusedId.current) return false;
    setRun(report);
    onRunUpdateRef.current?.(report);
    return true;
  }, []);

  const schedulePoll = useCallback(
    (runId: string) => {
      stopPolling();
      const tick = async (): Promise<void> => {
        pollTimer.current = undefined;
        if (unmounted.current || focusedId.current !== runId) return;
        let report: EvalRunStatusResponse;
        try {
          report = await client.getRunStatus(runId);
        } catch (err) {
          if (!unmounted.current && focusedId.current === runId) {
            setPollError(pollStopped(messageOf(err, "the run status check failed")));
          }
          return;
        }
        if (unmounted.current || focusedId.current !== runId) return;
        if (report.run_id !== runId) {
          setPollError(
            pollStopped(`the status check for ${runId} answered for ${report.run_id}`),
          );
          return;
        }
        accept(report);
        setPollError(undefined);
        if (!report.is_terminal) {
          pollTimer.current = setTimeout(() => void tick(), pollIntervalMs);
        }
      };
      pollTimer.current = setTimeout(() => void tick(), pollIntervalMs);
    },
    [accept, client, pollIntervalMs, stopPolling],
  );

  const focus = useCallback(
    (next: EvalRunStatusResponse) => {
      stopPolling();
      focusedId.current = next.run_id;
      setCancelDisposition(undefined);
      setPollError(undefined);
      setActionError(undefined);
      setRun(next);
      if (!next.is_terminal) schedulePoll(next.run_id);
    },
    [schedulePoll, stopPolling],
  );

  const start = useCallback(
    async (
      submit: () => Promise<EvalRunStatusResponse>,
      fallback: string,
    ): Promise<string | undefined> => {
      setStarting(true);
      setActionError(undefined);
      try {
        const started = await submit();
        if (unmounted.current) return undefined;
        focus(started);
        onRunUpdateRef.current?.(started);
        return undefined;
      } catch (err) {
        const message = messageOf(err, fallback);
        if (!unmounted.current) setActionError(message);
        return message;
      } finally {
        if (!unmounted.current) setStarting(false);
      }
    },
    [focus],
  );

  const startStub = useCallback(
    (repeats = 1) => start(() => client.startRun(repeats), "the stub sweep could not be started"),
    [client, start],
  );

  const startLive = useCallback(
    (repeats = 1) =>
      start(
        () => client.startLiveRun({ confirmed: true }, repeats),
        "the live sweep could not be started",
      ),
    [client, start],
  );

  const cancel = useCallback(async () => {
    const runId = focusedId.current;
    if (runId === undefined) return;
    setCancelling(true);
    setActionError(undefined);
    try {
      const response = await client.cancelRun(runId);
      if (!accept(response.run)) return;
      setCancelDisposition(response.disposition);
      if (!response.run.is_terminal) {
        setPollError(undefined);
        schedulePoll(runId);
      }
    } catch (err) {
      if (!unmounted.current) setActionError(messageOf(err, "the cancel request failed"));
    } finally {
      if (!unmounted.current) setCancelling(false);
    }
  }, [accept, client, schedulePoll]);

  return {
    run,
    starting,
    cancelling,
    pollError,
    actionError,
    cancelDisposition,
    startStub,
    startLive,
    cancel,
    focus,
  };
}
