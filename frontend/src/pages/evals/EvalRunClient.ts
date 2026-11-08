// SPDX-License-Identifier: MIT
/**
 * Eval-run client: the one code path this dashboard uses to start, watch, list, estimate and cancel
 * sweeps.
 *
 * The API serves each non-tool route as a flat `POST /<name>` (`infra/modules/api/main.tf`), so every
 * operation calls `POST {apiBaseUrl}/eval_trigger` and selects an action with the `action` field the
 * handler dispatches on (`crm/handlers/eval_trigger.py`).
 *
 * Field names are snake_case: `crm.handlers._common.ok` transforms no key, so a run object is
 * `eval.runs.RunRecord.to_payload()`. `tests/unit/test_eval_run_status_contract.py` compares both
 * sides.
 */
import type { AuthContextValue } from "../../auth/AuthProvider";
import {
  CrmApiError,
  errorKindForStatus,
  normaliseErrorBody,
  type ApiErrorBody,
  type TokenSource,
} from "../../api/CrmApiClient";
import { TIER_COMPARISON_TIERS } from "./types";
import type { EvalMatrix, EvalTierComparisonTable } from "./types";

/**
 * The run statuses `RunRecord.to_payload()` renders (`eval.runs.JOB_STATUSES`).
 *
 * `StartEvalRunResponse` and `EvalRunStatusResponse` each declare every key inline, because
 * `tests/unit/test_eval_run_status_contract.py` reads only an interface's own declared properties.
 */
export const JOB_STATUSES = [
  "queued",
  "running",
  "cancelling",
  "completed",
  "cancelled",
  "failed",
] as const;

export type EvalJobStatus = (typeof JOB_STATUSES)[number];

/** The outcomes of a cancel request (`eval.runs.Disposition`). */
export const CANCEL_DISPOSITIONS = ["accepted", "already_requested", "already_terminal"] as const;

export type EvalCancelDisposition = (typeof CANCEL_DISPOSITIONS)[number];

/** The submit response: the new run's `RunRecord.to_payload()` while it is `queued`. */
export interface StartEvalRunResponse {
  readonly run_id: string;
  readonly tenant: string;
  readonly status: EvalJobStatus;
  /** As the backend reported it; classified for display by `runMode.ts`. */
  readonly mode: string;
  readonly submitted_at: string;
  /** The poll-loop exit condition. */
  readonly is_terminal: boolean;
  /** Repeats per arm; absent on a run recorded before repeats existed, read as 1 (`runRepeats`). */
  readonly repeats?: number;
  readonly tasks_total: number;
  readonly tasks_done: number;
  readonly tasks_failed: number;
  readonly tasks_skipped: number;
  readonly cost_spent_usd: number;
  readonly cancel_requested: boolean;
  readonly started_by_rep_id?: string;
  readonly estimate_usd?: number;
  readonly estimate_basis?: string;
  readonly finished_at?: string;
  readonly error?: string;
}

/** One run as `status` and `list` render it. */
export interface EvalRunStatusResponse {
  readonly run_id: string;
  readonly tenant: string;
  readonly status: EvalJobStatus;
  /** As the backend reported it; classified for display by `runMode.ts`. */
  readonly mode: string;
  readonly submitted_at: string;
  /** The poll-loop exit condition. */
  readonly is_terminal: boolean;
  /** Repeats per arm; absent on a run recorded before repeats existed, read as 1 (`runRepeats`). */
  readonly repeats?: number;
  /** The run's `(arm, repeat, task)` item count: arms × repeats × golden tasks. */
  readonly tasks_total: number;
  readonly tasks_done: number;
  readonly tasks_failed: number;
  /** Items recorded `skipped_cancelled` after a cancel request. */
  readonly tasks_skipped: number;
  /** Metered spend so far, in US dollars; zero for a stub sweep. */
  readonly cost_spent_usd: number;
  readonly cancel_requested: boolean;
  readonly cancel_requested_by?: string;
  readonly cancel_requested_at?: string;
  /** The rep id of the user who submitted the run. */
  readonly started_by_rep_id?: string;
  readonly running_at?: string;
  readonly finished_at?: string;
  /** The live-sweep estimate recorded at submission; absent for a stub run. */
  readonly estimate_usd?: number;
  readonly estimate_basis?: string;
  /** `pending`, `ok`, `failed` or `unknown`. */
  readonly teardown_status?: string;
  /** Present when the run failed: a bounded, user-safe failure summary. */
  readonly error?: string;
  /** The parsed `matrix.json` contents, once the run has produced them. */
  readonly matrix?: EvalMatrix;
  /**
   * The tier comparison summary as display cells, present when the run's matrix carries one. A
   * table whose shape cannot be read is dropped at parse time and renders as unavailable.
   */
  readonly tier_comparison_table?: EvalTierComparisonTable;
  /**
   * Paths/URLs to the run artifacts, once produced. `useEvalRun.toArtifactLinks` converts these to
   * the client-side `EvalArtifactLinks`.
   */
  readonly artifacts?: {
    readonly matrix_json?: string;
    readonly matrix_md?: string;
    readonly matrix_csv?: string;
    readonly raw: readonly string[];
  };
}

/** One history page: newest first, and the cursor of the next page (`null` at the end). */
export interface EvalRunListResponse {
  readonly runs: readonly EvalRunStatusResponse[];
  readonly next_cursor: string | null;
}

/** The live-sweep cost estimate (`eval.runs.Estimate.to_payload()`). Shown, never enforced. */
export interface EvalEstimateResponse {
  readonly estimate_usd: number;
  /** `baseline` (from the newest completed live run) or `static` (`config/eval_live_estimate.yaml`). */
  readonly basis: string;
  /** Golden tasks per arm. */
  readonly task_count: number;
  /** The repeats the figures cover; every amount is already multiplied by it. Absent reads as 1. */
  readonly repeats?: number;
  readonly per_arm_usd: Readonly<Record<string, number>>;
  readonly basis_run_id?: string;
  /** Present when the figures were measured at a supervisor effort other than the sweep default. */
  readonly effort_caveat?: string;
}

/** The cancel response: what the request did, and the run as it now stands. */
export interface EvalCancelResponse {
  readonly disposition: EvalCancelDisposition;
  readonly run: EvalRunStatusResponse;
}

/**
 * A short-lived presigned GET for one run artefact (`eval.artefacts.artefact_url`). `key` is one of
 * the run's recorded `artifacts` paths; `url` is valid for `expires_in` seconds (at most 300).
 */
export interface EvalArtefactUrlResponse {
  readonly run_id: string;
  readonly key: string;
  readonly url: string;
  readonly expires_in: number;
}

/** Options of {@link EvalRunClient.getArtefactUrl}; `download` is sent only when `true`. */
export interface ArtefactUrlOptions {
  readonly download?: boolean;
}

/** The most repeats one sweep may run (`eval.runs.MAX_SWEEP_REPEATS`). */
export const MAX_SWEEP_REPEATS = 5;

/** The explicit confirmation a live sweep requires; the handler refuses `live` without it. */
export interface LiveSweepConfirmation {
  readonly confirmed: true;
}

/** The contract this dashboard requires of the backend. */
export interface EvalRunClient {
  /**
   * Starts a stub sweep of `repeats` (default 1) repeats per arm. The request carries no `live` or
   * `mode` field, and `repeats` only when it is not 1.
   */
  startRun(repeats?: number): Promise<StartEvalRunResponse>;
  /** Starts a billable live sweep of `repeats` (default 1). Admin only; the server enforces it. */
  startLiveRun(confirmation: LiveSweepConfirmation, repeats?: number): Promise<StartEvalRunResponse>;
  getRunStatus(runId: string): Promise<EvalRunStatusResponse>;
  /** One history page; omit `cursor` for the newest page. */
  listRuns(cursor?: string): Promise<EvalRunListResponse>;
  /** The live-sweep estimate for `repeats` (default 1) repeats per arm. */
  estimateLiveRun(repeats?: number): Promise<EvalEstimateResponse>;
  /** Requests a cooperative cancel. Admin only; the server enforces it. */
  cancelRun(runId: string): Promise<EvalCancelResponse>;
  /**
   * A presigned GET for one artefact of `runId`; the server checks the caller and the key. With
   * `download`, the URL serves the object as an attachment.
   */
  getArtefactUrl(
    runId: string,
    key: string,
    options?: ArtefactUrlOptions,
  ): Promise<EvalArtefactUrlResponse>;
}

function unreadable(path: string): CrmApiError {
  return new CrmApiError("unknown", 200, {
    message:
      `The evaluation service returned a response this page cannot read (${path}). ` +
      "No run state is shown rather than a guessed one.",
    code: "eval_response_unreadable",
  });
}

/** `{repeats}` when `repeats` is not 1, else nothing: 1 is the server's default. */
function repeatsField(repeats: number): { repeats?: number } {
  return repeats === 1 ? {} : { repeats };
}

function isObject(raw: unknown): raw is Record<string, unknown> {
  return typeof raw === "object" && raw !== null && !Array.isArray(raw);
}

function asRecord(raw: unknown): Record<string, unknown> {
  return isObject(raw) ? raw : {};
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isRepeatCount(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1;
}

/**
 * Whether `raw` has the matrix structure the page indexes into: an `arms` list whose every arm
 * carries `provenance.model_id`, `model_metrics`, `aggregate_score` and a `task_scores` list. The
 * figures inside are not validated here; the renderer names each absent one.
 */
function isRenderableMatrix(raw: unknown): boolean {
  const matrix = asRecord(raw);
  if (!Array.isArray(matrix.arms)) return false;
  return matrix.arms.every((candidate: unknown) => {
    const arm = asRecord(candidate);
    return (
      typeof asRecord(arm.provenance).model_id === "string" &&
      isObject(arm.model_metrics) &&
      isObject(arm.aggregate_score) &&
      Array.isArray(arm.task_scores)
    );
  });
}

/** Whether `raw` is a tier comparison table: string headers, one section per tier in order, each
 * row a list of strings as long as `headers`, and string notes. */
function isTierComparisonTable(raw: unknown): raw is EvalTierComparisonTable {
  const table = asRecord(raw);
  const headers = table.headers;
  const sections = table.sections;
  const notes = table.notes;
  if (!Array.isArray(headers) || headers.length < 2 || !headers.every((h) => typeof h === "string")) {
    return false;
  }
  if (!Array.isArray(notes) || !notes.every((note) => typeof note === "string")) return false;
  if (!Array.isArray(sections) || sections.length !== TIER_COMPARISON_TIERS.length) return false;
  return sections.every((candidate: unknown, index) => {
    const section = asRecord(candidate);
    return (
      section.tier === TIER_COMPARISON_TIERS[index] &&
      typeof section.heading === "string" &&
      Array.isArray(section.rows) &&
      section.rows.every(
        (row: unknown) =>
          Array.isArray(row) &&
          row.length === headers.length &&
          row.every((cell: unknown) => typeof cell === "string"),
      )
    );
  });
}

/**
 * Parse one run, checking the fields the page branches on (`run_id`, `status`, `is_terminal`,
 * `cancel_requested`), the figures it renders (`tasks_*`, `cost_spent_usd`) and, when present,
 * `repeats` and the matrix structure; other display fields are not validated here. An unreadable
 * `tier_comparison_table` is dropped rather than refusing the run.
 *
 * @throws CrmApiError when one of those fields is invalid.
 */
function parseRun<TResponse extends StartEvalRunResponse>(raw: unknown, path: string): TResponse {
  const run = asRecord(raw);
  const counters = [
    run.tasks_total,
    run.tasks_done,
    run.tasks_failed,
    run.tasks_skipped,
    run.cost_spent_usd,
  ];
  if (
    typeof run.run_id !== "string" ||
    run.run_id === "" ||
    typeof run.status !== "string" ||
    !(JOB_STATUSES as readonly string[]).includes(run.status) ||
    typeof run.is_terminal !== "boolean" ||
    typeof run.cancel_requested !== "boolean" ||
    !counters.every(isFiniteNumber) ||
    (run.repeats !== undefined && !isRepeatCount(run.repeats)) ||
    (run.matrix !== undefined && !isRenderableMatrix(run.matrix))
  ) {
    throw unreadable(path);
  }
  if (run.tier_comparison_table !== undefined && !isTierComparisonTable(run.tier_comparison_table)) {
    const { tier_comparison_table: _unreadableTable, ...rest } = run;
    void _unreadableTable;
    return rest as unknown as TResponse;
  }
  return run as unknown as TResponse;
}

/**
 * The one HTTP implementation of {@link EvalRunClient}. Calls `POST {apiBaseUrl}/eval_trigger` for
 * every operation, selecting the operation with the `action` field.
 */
export class HttpEvalRunClient implements EvalRunClient {
  constructor(
    private readonly apiBaseUrl: string,
    private readonly tokenSource: TokenSource,
  ) {}

  static fromAuth(apiBaseUrl: string, auth: AuthContextValue): HttpEvalRunClient {
    return new HttpEvalRunClient(apiBaseUrl, { getAccessToken: () => auth.getAccessToken() });
  }

  private async request<TResponse>(path: string, init: RequestInit): Promise<TResponse> {
    const token = await this.tokenSource.getAccessToken();
    const res = await fetch(`${this.apiBaseUrl}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
        ...init.headers,
      },
    });

    if (!res.ok) {
      // The backend error is the nested envelope `{"error": {"code", "message"}}`;
      // `normaliseErrorBody` flattens it and yields the fallback for a non-envelope body.
      const fallback = `eval request ${path} failed with status ${res.status}`;
      let body: ApiErrorBody;
      try {
        body = normaliseErrorBody(await res.json(), fallback);
      } catch {
        // Not JSON at all.
        body = { message: fallback };
      }
      throw new CrmApiError(errorKindForStatus(res.status), res.status, body);
    }
    try {
      return (await res.json()) as TResponse;
    } catch {
      throw unreadable(path);
    }
  }

  /** One `eval_trigger` call. The path is a literal so the contract test can read it. */
  private action(body: Record<string, unknown>): Promise<unknown> {
    return this.request<unknown>("/eval_trigger", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  startRun(repeats = 1): Promise<StartEvalRunResponse> {
    return this.action({ action: "submit", ...repeatsField(repeats) }).then((raw) =>
      parseRun<StartEvalRunResponse>(raw, "submit"),
    );
  }

  startLiveRun(confirmation: LiveSweepConfirmation, repeats = 1): Promise<StartEvalRunResponse> {
    if (confirmation.confirmed !== true) {
      return Promise.reject(new Error("a live sweep requires explicit confirmation"));
    }
    return this.action({
      action: "submit",
      mode: "live",
      confirmed: true,
      ...repeatsField(repeats),
    }).then((raw) =>
      parseRun<StartEvalRunResponse>(raw, "submit"),
    );
  }

  getRunStatus(runId: string): Promise<EvalRunStatusResponse> {
    return this.action({ action: "status", run_id: runId }).then((raw) =>
      parseRun<EvalRunStatusResponse>(raw, "status"),
    );
  }

  listRuns(cursor?: string): Promise<EvalRunListResponse> {
    const body = cursor === undefined ? { action: "list" } : { action: "list", cursor };
    return this.action(body).then((raw) => {
      const page = asRecord(raw);
      const nextCursor = page.next_cursor;
      if (
        !Array.isArray(page.runs) ||
        !(nextCursor === null || (typeof nextCursor === "string" && nextCursor !== ""))
      ) {
        throw unreadable("list");
      }
      return {
        runs: page.runs.map((run) => parseRun<EvalRunStatusResponse>(run, "list")),
        next_cursor: nextCursor,
      };
    });
  }

  estimateLiveRun(repeats = 1): Promise<EvalEstimateResponse> {
    return this.action({ action: "estimate", ...repeatsField(repeats) }).then((raw) => {
      const estimate = asRecord(raw);
      if (
        !isFiniteNumber(estimate.estimate_usd) ||
        typeof estimate.basis !== "string" ||
        !isFiniteNumber(estimate.task_count) ||
        !isObject(estimate.per_arm_usd) ||
        !Object.values(estimate.per_arm_usd).every(isFiniteNumber) ||
        (estimate.repeats !== undefined && !isRepeatCount(estimate.repeats)) ||
        (estimate.effort_caveat !== undefined && typeof estimate.effort_caveat !== "string")
      ) {
        throw unreadable("estimate");
      }
      return estimate as unknown as EvalEstimateResponse;
    });
  }

  cancelRun(runId: string): Promise<EvalCancelResponse> {
    return this.action({ action: "cancel", run_id: runId }).then((raw) => {
      const response = asRecord(raw);
      const disposition = response.disposition;
      if (
        typeof disposition !== "string" ||
        !(CANCEL_DISPOSITIONS as readonly string[]).includes(disposition)
      ) {
        throw unreadable("cancel");
      }
      return {
        disposition: disposition as EvalCancelDisposition,
        run: parseRun<EvalRunStatusResponse>(response.run, "cancel"),
      };
    });
  }

  getArtefactUrl(
    runId: string,
    key: string,
    options: ArtefactUrlOptions = {},
  ): Promise<EvalArtefactUrlResponse> {
    const download = options.download === true ? { download: true } : {};
    return this.action({ action: "artefact_url", run_id: runId, key, ...download }).then((raw) => {
      const response = asRecord(raw);
      if (
        typeof response.url !== "string" ||
        !response.url.startsWith("https://") ||
        !isFiniteNumber(response.expires_in) ||
        response.key !== key ||
        response.run_id !== runId
      ) {
        throw unreadable("artefact_url");
      }
      return response as unknown as EvalArtefactUrlResponse;
    });
  }
}
