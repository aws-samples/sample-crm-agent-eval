// SPDX-License-Identifier: MIT
/**
 * CRM domain entity types: persisted record shapes returned by the read tools, distinct from the
 * generated `*Input` request types in `generated/toolSchemas.ts`. Hand-maintained;
 * `src/test/responseShapes.test.ts` pins the response keys the frontend reads.
 */

/** Opportunity lifecycle stages, ordered. The first four are open stages. */
export const OPPORTUNITY_STAGES = [
  "Discovery",
  "Qualification",
  "Security Review",
  "Negotiation",
  "Closed Won",
  "Closed Lost",
] as const;
export type OpportunityStage = (typeof OPPORTUNITY_STAGES)[number];

export const OPEN_STAGES: readonly OpportunityStage[] = [
  "Discovery",
  "Qualification",
  "Security Review",
  "Negotiation",
];

export const ACTIVITY_KINDS = [
  "call",
  "email",
  "meeting",
  "note",
  "stage_change",
  "finding",
] as const;
export type ActivityKind = (typeof ACTIVITY_KINDS)[number];

export type ContactStatus = "active" | "departed";
export type ProposalStatus = "pending" | "approved" | "rejected";

export interface Account {
  readonly account_id: string;
  readonly name: string;
  readonly industry?: string;
  readonly region?: string;
  readonly website?: string;
  readonly employee_count?: number;
  readonly notes?: string;
  readonly created_at: string;
  readonly updated_at: string;
}

export interface Contact {
  readonly contact_id: string;
  readonly account_id: string;
  readonly name: string;
  readonly title?: string;
  readonly email?: string;
  readonly phone?: string;
  readonly status: ContactStatus;
  readonly created_at: string;
  readonly updated_at: string;
}

export interface Opportunity {
  readonly opportunity_id: string;
  readonly account_id: string;
  readonly name: string;
  readonly value: number;
  readonly close_date?: string;
  readonly stage: OpportunityStage;
  readonly owner_rep_id: string;
  readonly primary_contact_id?: string;
  readonly description?: string;
  readonly source?: string;
  readonly competitor?: string;
  readonly last_touch?: string;
  readonly created_at: string;
  readonly updated_at: string;
}

export interface Activity {
  readonly activity_id: string;
  readonly opportunity_id: string;
  readonly kind: ActivityKind;
  readonly subject: string;
  readonly body?: string;
  readonly occurred_at: string;
  readonly created_at: string;
  readonly updated_at: string;
}

export interface Note {
  readonly note_id: string;
  readonly opportunity_id: string;
  readonly body: string;
  readonly created_at: string;
}

export interface Proposal {
  readonly proposal_id: string;
  readonly target_table: "accounts" | "contacts" | "opportunities" | "activities";
  readonly target_id: string;
  readonly operation: "update" | "delete";
  readonly before_snapshot: Record<string, unknown>;
  readonly after_payload?: Record<string, unknown>;
  readonly reason: string;
  readonly status: ProposalStatus;
  readonly created_at: string;
  readonly decided_at?: string;
}

/**
 * A row as `list_audit_records` actually returns it.
 *
 * The snapshot and actor fields are optional: `list_audit_records` serves its default call from
 * `audit_records.gsi_tenant`, whose `INCLUDE` projection is
 * `tenant, audit_id, target_id, account_id, target_table, status` plus the keys. The base table
 * item carries all of them, so a detail read by `audit_id` can populate them.
 */
export interface AuditRecord {
  readonly audit_id: string;
  readonly target_table: string;
  readonly target_id: string;
  readonly created_at: string;
  /** Present on a detail read; absent from `list_audit_records` rows. */
  readonly before_snapshot?: Record<string, unknown>;
  /** Present on a detail read; absent from `list_audit_records` rows. */
  readonly after_snapshot?: Record<string, unknown>;
  /** Projected by `gsi_actor`/`gsi_target`, not by `gsi_tenant` — so absent from the default list. */
  readonly actor_rep_id?: string;
  /** Revert state, projected by `gsi_tenant`. */
  readonly status?: string;
  readonly reversed_at?: string;
}
