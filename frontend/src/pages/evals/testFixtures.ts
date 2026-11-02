// SPDX-License-Identifier: MIT
/**
 * The eval run payload as a **compliant backend** renders it, and a fake client serving it, for
 * tests on both sides of the wire.
 *
 * Field names are the wire's own (`RunRecord.to_payload()`, snake_case). `is_terminal` is derived
 * from `status`, never passed in.
 */
import type {
  EvalArtefactUrlResponse,
  EvalCancelResponse,
  EvalEstimateResponse,
  EvalJobStatus,
  EvalRunClient,
  EvalRunListResponse,
  EvalRunStatusResponse,
  LiveSweepConfirmation,
  StartEvalRunResponse,
} from "./EvalRunClient";

/** The statuses from which no further transition happens — mirrors `TERMINAL_JOB_STATUSES`. */
const TERMINAL: readonly EvalJobStatus[] = ["completed", "cancelled", "failed"];

export interface JobPayloadOverrides {
  readonly status?: EvalJobStatus;
  readonly run_id?: string;
  readonly tenant?: string;
  readonly mode?: string;
  readonly submitted_at?: string;
  readonly finished_at?: string;
  readonly error?: string;
  readonly tasks_total?: number;
  readonly tasks_done?: number;
  readonly tasks_failed?: number;
  readonly tasks_skipped?: number;
  readonly cost_spent_usd?: number;
  readonly cancel_requested?: boolean;
  readonly started_by_rep_id?: string;
}

/**
 * One run payload. `status` drives `is_terminal` and, for a terminal status, `finished_at`. A
 * `completed` run defaults to every task done (`tasks_done = tasks_total`).
 *
 * @param overrides - Any field to vary. `matrix`/`artifacts` are not settable here.
 */
export function jobPayload(overrides: JobPayloadOverrides = {}): EvalRunStatusResponse {
  const status = overrides.status ?? "completed";
  const isTerminal = TERMINAL.includes(status);
  const tasksTotal = overrides.tasks_total ?? 40;
  return {
    run_id: "run-1",
    tenant: "eval-run-1",
    status,
    mode: "stub",
    submitted_at: "2026-01-01T00:00:00+00:00",
    is_terminal: isTerminal,
    tasks_total: tasksTotal,
    tasks_done: status === "completed" ? tasksTotal : 0,
    tasks_failed: 0,
    tasks_skipped: 0,
    cost_spent_usd: 0,
    cancel_requested: false,
    started_by_rep_id: "rep-1",
    ...(isTerminal ? { finished_at: "2026-01-01T00:05:00+00:00" } : {}),
    ...overrides,
  };
}

/** A live-sweep estimate as `Estimate.to_payload()` renders it. */
export function estimatePayload(
  overrides: Partial<EvalEstimateResponse> = {},
): EvalEstimateResponse {
  return {
    estimate_usd: 3.1,
    basis: "static",
    task_count: 20,
    per_arm_usd: { "anthropic.claude-opus-5-5": 1.75, "openai.gpt-6-astra": 1.35 },
    ...overrides,
  };
}

export interface FakeClientOptions {
  /** History pages in order; page `i`'s cursor is `page-<i>`. Defaults to one empty page. */
  readonly history?: readonly (readonly EvalRunStatusResponse[])[];
  readonly estimate?: EvalEstimateResponse | Error;
  readonly cancel?: EvalCancelResponse | Error;
  /** When set, every `getArtefactUrl` call rejects with it. */
  readonly artefactError?: Error;
}

/**
 * A fake {@link EvalRunClient}: `startRun` returns a queued stub run, `getRunStatus` serves a
 * queue of reports (throwing once it is exhausted), and every call is recorded.
 */
export class BaseFakeEvalRunClient implements EvalRunClient {
  public startCallCount = 0;
  /** The `repeats` each `startRun` call carried, `undefined` when omitted. */
  public readonly stubStartRepeats: (number | undefined)[] = [];
  public readonly liveStarts: LiveSweepConfirmation[] = [];
  /** The `repeats` each `startLiveRun` call carried, `undefined` when omitted. */
  public readonly liveStartRepeats: (number | undefined)[] = [];
  public readonly listCursors: (string | undefined)[] = [];
  public readonly cancelledRunIds: string[] = [];
  public readonly artefactRequests: { runId: string; key: string }[] = [];
  public estimateCallCount = 0;
  protected readonly statusQueue: EvalRunStatusResponse[];

  constructor(
    statuses: readonly EvalRunStatusResponse[] = [],
    protected readonly options: FakeClientOptions = {},
  ) {
    this.statusQueue = [...statuses];
  }

  startRun(repeats?: number): Promise<StartEvalRunResponse> {
    this.startCallCount += 1;
    this.stubStartRepeats.push(repeats);
    return Promise.resolve(jobPayload({ status: "queued" }));
  }

  startLiveRun(confirmation: LiveSweepConfirmation, repeats?: number): Promise<StartEvalRunResponse> {
    this.liveStarts.push(confirmation);
    this.liveStartRepeats.push(repeats);
    return Promise.resolve(
      jobPayload({ status: "queued", mode: "live", run_id: "run-live" }),
    );
  }

  getRunStatus(runId: string): Promise<EvalRunStatusResponse> {
    void runId;
    const next = this.statusQueue.shift();
    if (!next) throw new Error("FakeEvalRunClient: status queue exhausted");
    return Promise.resolve(next);
  }

  listRuns(cursor?: string): Promise<EvalRunListResponse> {
    this.listCursors.push(cursor);
    const pages = this.options.history ?? [[]];
    const index = cursor === undefined ? 0 : Number(cursor.replace("page-", ""));
    const runs = pages[index] ?? [];
    return Promise.resolve({
      runs,
      next_cursor: index + 1 < pages.length ? `page-${index + 1}` : null,
    });
  }

  estimateLiveRun(): Promise<EvalEstimateResponse> {
    this.estimateCallCount += 1;
    const estimate = this.options.estimate ?? estimatePayload();
    return estimate instanceof Error ? Promise.reject(estimate) : Promise.resolve(estimate);
  }

  cancelRun(runId: string): Promise<EvalCancelResponse> {
    this.cancelledRunIds.push(runId);
    const response = this.options.cancel;
    if (response instanceof Error) return Promise.reject(response);
    return Promise.resolve(
      response ?? {
        disposition: "accepted",
        run: jobPayload({ status: "cancelling", run_id: runId, cancel_requested: true }),
      },
    );
  }

  getArtefactUrl(runId: string, key: string): Promise<EvalArtefactUrlResponse> {
    this.artefactRequests.push({ runId, key });
    if (this.options.artefactError) return Promise.reject(this.options.artefactError);
    return Promise.resolve({
      run_id: runId,
      key,
      url: `https://artefacts.example.test/${runId}/${key}?X-Amz-Expires=300`,
      expires_in: 300,
    });
  }
}
