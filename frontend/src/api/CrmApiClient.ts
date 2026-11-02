// SPDX-License-Identifier: MIT
/**
 * Typed CRM API client. Every method corresponds 1:1 to a tool in
 * `src/crm/toolspec/definitions.py`; the request body type is the generated `<ToolName>Input`
 * interface.
 *
 * ## Route convention
 *
 *   POST {apiBaseUrl}/{tool_name}
 *
 * One POST route per tool, path segment equal to the tool's snake_case name as it appears in
 * `ToolName` (e.g. `POST /api/search_opportunities`). `apiBaseUrl` is `/api` (same-origin behind
 * CloudFront, no CORS). The request body is the tool's JSON input as-is; identity is never in the
 * body — it travels as the `Authorization` bearer token. Every page and hook calls through
 * `CrmApiClient`, never `fetch` directly.
 *
 * One non-tool route is also served here: `POST {apiBaseUrl}/user_settings` with
 * `{action: "get" | "put" | "reset", tuning?}`, the caller's own persisted tuning.
 */
import type { AuthContextValue } from "../auth/AuthProvider";
import type {
  ApproveProposalInput,
  CreateAccountInput,
  CreateContactInput,
  CreateNoteInput,
  CreateOpportunityInput,
  DraftMessageInput,
  GetAccountInput,
  GetAuditRecordInput,
  GetContactInput,
  GetOpportunityInput,
  ListActivitiesInput,
  ListAuditRecordsInput,
  ListProposalsInput,
  LogActivityInput,
  PipelineAggregateInput,
  RebaseProposalInput,
  RejectProposalInput,
  RevertAuditRecordInput,
  SearchAccountsInput,
  SearchOpportunitiesInput,
  SubmitProposalInput,
  UpdateAccountInput,
  UpdateActivityInput,
  UpdateContactInput,
  UpdateOpportunityInput,
} from "../types/generated/toolSchemas";

/**
 * The closed error taxonomy. Every non-2xx response is normalised into one of these before it
 * reaches a page component.
 */
export type ApiErrorKind =
  | "authentication_failed"
  | "scope_denied"
  | "not_found"
  | "validation_failed"
  | "conflict"
  | "unknown";

/**
 * The **wire** shape a handler returns, as `crm.errors.CrmError.to_api_response` renders it: the
 * fields nest under `error` and are snake_case. Never handed to a component — {@link normaliseErrorBody}
 * converts it to {@link ApiErrorBody} first.
 */
interface WireErrorEnvelope {
  readonly error?: {
    readonly code?: unknown;
    readonly message?: unknown;
    readonly missing_scope?: unknown;
    readonly source?: unknown;
    readonly reason?: unknown;
    readonly conflicting_fields?: unknown;
    readonly next_step?: unknown;
  };
}

/** One field that moved between a proposal's snapshot and the record's current state. */
export interface ConflictingField {
  readonly field: string;
  readonly snapshot: unknown;
  readonly current: unknown;
  readonly proposed: unknown;
}

/** The **normalised** error a component receives. camelCase, flat, and always has a `message`. */
export interface ApiErrorBody {
  /** User-safe message, distinct from internal diagnostic detail. */
  readonly message: string;
  /** Present on a `scope_denied` error: the missing scope. */
  readonly missingScope?: string;
  /** The taxonomy discriminator the handler emitted, from the envelope's `error.code`. */
  readonly code?: string;
  /**
   * Present on a `validation_failed` error: which input was rejected, from `error.source`.
   */
  readonly source?: string;
  /** Present on a `conflict` the server could explain, from `error.reason`. */
  readonly reason?: string;
  /** Present on a `fields_changed` conflict, from `error.conflicting_fields`. */
  readonly conflictingFields?: readonly ConflictingField[];
  /** What the caller should do next, from `error.next_step` (`crm.errors.NextStep`). */
  readonly nextStep?: string;
  readonly [key: string]: unknown;
}

/** Keep only the well-formed `conflicting_fields` entries; `undefined` when none is an array. */
function parseConflictingFields(raw: unknown): ConflictingField[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  return raw.flatMap((entry: unknown) => {
    if (typeof entry !== "object" || entry === null) return [];
    const row = entry as Record<string, unknown>;
    if (typeof row.field !== "string") return [];
    return [{ field: row.field, snapshot: row.snapshot, current: row.current, proposed: row.proposed }];
  });
}

/**
 * Convert a handler's wire envelope into {@link ApiErrorBody}, reading `error.message`,
 * `error.missing_scope`, `error.code`, `error.source`, `error.reason`,
 * `error.conflicting_fields` and `error.next_step`.
 *
 * Never throws: a body that is not the envelope (an API Gateway 500, an HTML error page, a
 * truncated response) yields `fallbackMessage`.
 */
export function normaliseErrorBody(raw: unknown, fallbackMessage: string): ApiErrorBody {
  const envelope = (typeof raw === "object" && raw !== null ? raw : {}) as WireErrorEnvelope;
  const error = typeof envelope.error === "object" && envelope.error !== null ? envelope.error : {};
  const message = typeof error.message === "string" && error.message !== "" ? error.message : fallbackMessage;
  const conflictingFields = parseConflictingFields(error.conflicting_fields);
  return {
    message,
    ...(typeof error.missing_scope === "string" ? { missingScope: error.missing_scope } : {}),
    ...(typeof error.code === "string" ? { code: error.code } : {}),
    ...(typeof error.source === "string" ? { source: error.source } : {}),
    ...(typeof error.reason === "string" ? { reason: error.reason } : {}),
    ...(conflictingFields !== undefined ? { conflictingFields } : {}),
    ...(typeof error.next_step === "string" ? { nextStep: error.next_step } : {}),
  };
}

export class CrmApiError extends Error {
  constructor(
    public readonly kind: ApiErrorKind,
    public readonly status: number,
    public readonly body: ApiErrorBody,
  ) {
    super(body.message);
    this.name = "CrmApiError";
  }
}

/**
 * Map an HTTP status onto the closed {@link ApiErrorKind} taxonomy. Also used by
 * `pages/evals/EvalRunClient.ts`.
 */
export function errorKindForStatus(status: number): ApiErrorKind {
  switch (status) {
    case 401:
      return "authentication_failed";
    case 403:
      return "scope_denied";
    case 404:
      return "not_found";
    case 400:
      return "validation_failed";
    case 409:
      return "conflict";
    default:
      return "unknown";
  }
}

/** The non-tool route serving the caller's persisted tuning settings. */
export const USER_SETTINGS_ROUTE = "user_settings";

/** The `/api/user_settings` response body, as `crm.user_settings.SettingsView.to_payload` renders it. */
export interface UserSettingsView {
  /** The schema-3 wire tuning block. */
  readonly tuning: Record<string, unknown>;
  readonly is_default: boolean;
  readonly notice: "saved_model_unavailable" | "saved_settings_unreadable" | null;
  readonly updated_at: string | null;
}

/** Injected in tests / storybook in place of a real `AuthProvider` context value. */
export interface TokenSource {
  getAccessToken(): Promise<string>;
}

export class CrmApiClient {
  constructor(
    private readonly apiBaseUrl: string,
    private readonly tokenSource: TokenSource,
  ) {}

  /** Construct a client bound to a live `useAuth()` context value. */
  static fromAuth(apiBaseUrl: string, auth: AuthContextValue): CrmApiClient {
    return new CrmApiClient(apiBaseUrl, { getAccessToken: () => auth.getAccessToken() });
  }

  private async post<TResponse>(toolName: string, input: unknown): Promise<TResponse> {
    const token = await this.tokenSource.getAccessToken();
    const res = await fetch(`${this.apiBaseUrl}/${toolName}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(input),
    });

    if (!res.ok) {
      const fallback = `request to ${toolName} failed with status ${res.status}`;
      let body: ApiErrorBody;
      try {
        body = normaliseErrorBody(await res.json(), fallback);
      } catch {
        // Not JSON at all — an HTML error page from the edge, or a truncated body.
        body = { message: fallback };
      }
      throw new CrmApiError(errorKindForStatus(res.status), res.status, body);
    }

    // A 204 (e.g. some approval actions) has no body to parse.
    if (res.status === 204) {
      return undefined as TResponse;
    }
    return (await res.json()) as TResponse;
  }

  // ---- Reads ----
  searchOpportunities(input: SearchOpportunitiesInput): Promise<unknown> {
    return this.post("search_opportunities", input);
  }
  getOpportunity(input: GetOpportunityInput): Promise<unknown> {
    return this.post("get_opportunity", input);
  }
  searchAccounts(input: SearchAccountsInput): Promise<unknown> {
    return this.post("search_accounts", input);
  }
  getAccount(input: GetAccountInput): Promise<unknown> {
    return this.post("get_account", input);
  }
  getContact(input: GetContactInput): Promise<unknown> {
    return this.post("get_contact", input);
  }
  listActivities(input: ListActivitiesInput): Promise<unknown> {
    return this.post("list_activities", input);
  }
  pipelineAggregate(input: PipelineAggregateInput): Promise<unknown> {
    return this.post("pipeline_aggregate", input);
  }
  listProposals(input: ListProposalsInput): Promise<unknown> {
    return this.post("list_proposals", input);
  }
  listAuditRecords(input: ListAuditRecordsInput): Promise<unknown> {
    return this.post("list_audit_records", input);
  }
  getAuditRecord(input: GetAuditRecordInput): Promise<unknown> {
    return this.post("get_audit_record", input);
  }
  draftMessage(input: DraftMessageInput): Promise<unknown> {
    return this.post("draft_message", input);
  }

  // ---- Low-risk writes ----
  createAccount(input: CreateAccountInput): Promise<unknown> {
    return this.post("create_account", input);
  }
  updateAccount(input: UpdateAccountInput): Promise<unknown> {
    return this.post("update_account", input);
  }
  createContact(input: CreateContactInput): Promise<unknown> {
    return this.post("create_contact", input);
  }
  updateContact(input: UpdateContactInput): Promise<unknown> {
    return this.post("update_contact", input);
  }
  createOpportunity(input: CreateOpportunityInput): Promise<unknown> {
    return this.post("create_opportunity", input);
  }
  updateOpportunity(input: UpdateOpportunityInput): Promise<unknown> {
    return this.post("update_opportunity", input);
  }
  logActivity(input: LogActivityInput): Promise<unknown> {
    return this.post("log_activity", input);
  }
  updateActivity(input: UpdateActivityInput): Promise<unknown> {
    return this.post("update_activity", input);
  }
  createNote(input: CreateNoteInput): Promise<unknown> {
    return this.post("create_note", input);
  }

  // ---- Proposal and approval surface ----
  submitProposal(input: SubmitProposalInput): Promise<unknown> {
    return this.post("submit_proposal", input);
  }
  approveProposal(input: ApproveProposalInput): Promise<unknown> {
    return this.post("approve_proposal", input);
  }
  rejectProposal(input: RejectProposalInput): Promise<unknown> {
    return this.post("reject_proposal", input);
  }
  rebaseProposal(input: RebaseProposalInput): Promise<unknown> {
    return this.post("rebase_proposal", input);
  }
  revertAuditRecord(input: RevertAuditRecordInput): Promise<unknown> {
    return this.post("revert_audit_record", input);
  }

  // ---- The caller's persisted tuning settings (non-tool route) ----
  getUserSettings(): Promise<UserSettingsView> {
    return this.post(USER_SETTINGS_ROUTE, { action: "get" });
  }
  saveUserSettings(input: { readonly tuning: Record<string, unknown> }): Promise<UserSettingsView> {
    return this.post(USER_SETTINGS_ROUTE, { action: "put", tuning: input.tuning });
  }
  resetUserSettings(): Promise<UserSettingsView> {
    return this.post(USER_SETTINGS_ROUTE, { action: "reset" });
  }
}
