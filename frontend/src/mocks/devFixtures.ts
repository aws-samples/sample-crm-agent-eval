// SPDX-License-Identifier: MIT
/**
 * Dev-only `demo`-tenant dataset, shaped to match `fixtures/demo_seed.py`.
 *
 * Every state a page renders distinctly has a real record behind it: an account outside the
 * signed-in rep's book (a genuine 403), an account with no opportunities, an opportunity with more
 * activities than the page size, a departed contact, and pending proposals carrying before-snapshots.
 *
 * Never imported by application code — see `devServerPlugin.ts` and `mocks.test.ts`.
 */

import type {
  Account,
  Activity,
  AuditRecord,
  Contact,
  Note,
  Opportunity,
  Proposal,
} from "../types/domain";

/** Fixed so a screenshot taken twice is the same screenshot. */
const T0 = "2026-09-01T09:00:00Z";
const T1 = "2026-09-03T14:30:00Z";
const T2 = "2026-09-05T11:15:00Z";

/** `list_activities`' default page size in the handler; the truncating set exceeds it. */
export const ACTIVITY_PAGE_SIZE = 25;

/** `search_opportunities`' default page size; the pipeline set exceeds it. */
export const SEARCH_PAGE_SIZE = 20;

export const DEV_REP_ID = "rep-demo-1";
export const DEV_OTHER_REP_ID = "rep-demo-2";

/** A rep with no accounts assigned, so the designed empty states are reachable in a browser. */
export const DEV_EMPTY_REP_ID = "rep-demo-3";

export const ACCOUNTS: readonly Account[] = [
  {
    account_id: "acc-northwind",
    name: "Northwind Traders",
    industry: "Logistics",
    region: "us-east-1",
    website: "https://northwind.example",
    employee_count: 4200,
    notes: "Renewal cycle closes at end of quarter.",
    created_at: T0,
    updated_at: T2,
  },
  {
    account_id: "acc-contoso",
    name: "Contoso Health",
    industry: "Healthcare",
    region: "us-east-1",
    website: "https://contoso.example",
    employee_count: 18_500,
    notes: "Security review gated on HIPAA questionnaire.",
    created_at: T0,
    updated_at: T1,
  },
  {
    account_id: "acc-fabrikam",
    name: "Fabrikam Logistics",
    industry: "Logistics",
    region: "eu-west-1",
    employee_count: 900,
    created_at: T0,
    updated_at: T1,
  },
  /** In no rep's book below — every read of it is a real `scope_denied`. */
  {
    account_id: "acc-tailspin",
    name: "Tailspin Aerospace",
    industry: "Aerospace",
    region: "us-west-2",
    employee_count: 12_000,
    created_at: T0,
    updated_at: T0,
  },
  /** Has no opportunities and no contacts. */
  {
    account_id: "acc-quiet",
    name: "Quiet Harbour Freight",
    industry: "Shipping",
    region: "us-east-1",
    employee_count: 40,
    created_at: T0,
    updated_at: T0,
  },
];

export const CONTACTS: readonly Contact[] = [
  {
    contact_id: "con-dana",
    account_id: "acc-northwind",
    name: "Dana Whitfield",
    title: "VP Operations",
    email: "dana.whitfield@northwind.example",
    phone: "+1 555 0100",
    status: "active",
    created_at: T0,
    updated_at: T1,
  },
  {
    contact_id: "con-priya",
    account_id: "acc-contoso",
    name: "Priya Raman",
    title: "Director of Security",
    email: "priya.raman@contoso.example",
    status: "active",
    created_at: T0,
    updated_at: T1,
  },
  /** Departed: the UI must show this differently from an active contact. */
  {
    contact_id: "con-marcus",
    account_id: "acc-northwind",
    name: "Marcus Feld",
    title: "Former Procurement Lead",
    email: "marcus.feld@northwind.example",
    status: "departed",
    created_at: T0,
    updated_at: T2,
  },
  {
    contact_id: "con-tailspin",
    account_id: "acc-tailspin",
    name: "Out Of Scope Contact",
    status: "active",
    created_at: T0,
    updated_at: T0,
  },
];

const STAGE_ROTATION = [
  "Discovery",
  "Qualification",
  "Security Review",
  "Negotiation",
  "Closed Won",
  "Closed Lost",
] as const;

function buildOpportunities(): Opportunity[] {
  const named: Opportunity[] = [
    {
      opportunity_id: "opp-northwind-renewal",
      account_id: "acc-northwind",
      name: "Northwind platform renewal",
      value: 482_000,
      close_date: "2026-09-30",
      stage: "Negotiation",
      owner_rep_id: DEV_REP_ID,
      primary_contact_id: "con-dana",
      description: "Three-year renewal with an expanded logistics module.",
      source: "Existing customer",
      competitor: "Adventure Works",
      last_touch: T2,
      created_at: T0,
      updated_at: T2,
    },
    {
      opportunity_id: "opp-contoso-expansion",
      account_id: "acc-contoso",
      name: "Contoso Health expansion",
      value: 1_240_500,
      close_date: "2026-11-14",
      stage: "Security Review",
      owner_rep_id: DEV_REP_ID,
      primary_contact_id: "con-priya",
      description: "Adds 9,000 seats pending the HIPAA questionnaire.",
      source: "Inbound",
      last_touch: T1,
      created_at: T0,
      updated_at: T1,
    },
    {
      opportunity_id: "opp-fabrikam-pilot",
      account_id: "acc-fabrikam",
      name: "Fabrikam pilot",
      value: 96_750,
      close_date: "2026-10-02",
      stage: "Closed Won",
      owner_rep_id: DEV_OTHER_REP_ID,
      description: "Pilot converted after the EU data-residency review.",
      created_at: T0,
      updated_at: T1,
    },
    /** Out of scope for `DEV_REP_ID` — reading it is a real 403. */
    {
      opportunity_id: "opp-tailspin-frame",
      account_id: "acc-tailspin",
      name: "Tailspin airframe programme",
      value: 3_400_000,
      stage: "Discovery",
      owner_rep_id: DEV_OTHER_REP_ID,
      created_at: T0,
      updated_at: T0,
    },
  ];

  /*
   * Filler to push the pipeline past `SEARCH_PAGE_SIZE`, so `truncated: true` is produced by the
   * data rather than asserted by the stub. Every stage is represented, including both closed ones.
   */
  const filler: Opportunity[] = Array.from({ length: 24 }, (_, index) => ({
    opportunity_id: `opp-pipeline-${String(index + 1).padStart(2, "0")}`,
    account_id: index % 2 === 0 ? "acc-northwind" : "acc-contoso",
    name: `${index % 2 === 0 ? "Northwind" : "Contoso"} workstream ${index + 1}`,
    value: 25_000 + index * 7_500,
    close_date: `2026-1${(index % 2) + 1}-0${(index % 9) + 1}`,
    stage: STAGE_ROTATION[index % STAGE_ROTATION.length],
    owner_rep_id: DEV_REP_ID,
    created_at: T0,
    updated_at: T1,
  }));

  return [...named, ...filler];
}

export const OPPORTUNITIES: readonly Opportunity[] = buildOpportunities();

export const NOTES: readonly Note[] = [
  {
    note_id: "note-1",
    opportunity_id: "opp-northwind-renewal",
    body: "Procurement wants the renewal signed before the quarter closes.",
    created_at: T1,
  },
  {
    note_id: "note-2",
    opportunity_id: "opp-northwind-renewal",
    body: "Marcus has left; Dana is now the decision maker.",
    created_at: T2,
  },
];

function buildActivities(): Activity[] {
  const kinds = ["call", "email", "meeting", "note", "stage_change", "finding"] as const;
  /** More than `ACTIVITY_PAGE_SIZE`, so `list_activities` genuinely truncates. */
  const highTouch: Activity[] = Array.from({ length: ACTIVITY_PAGE_SIZE + 7 }, (_, index) => ({
    activity_id: `act-northwind-${String(index + 1).padStart(2, "0")}`,
    opportunity_id: "opp-northwind-renewal",
    kind: kinds[index % kinds.length],
    subject: `Renewal touchpoint ${index + 1}`,
    body: index % 3 === 0 ? "Reviewed commercial terms and open questions." : undefined,
    occurred_at: `2026-08-${String((index % 28) + 1).padStart(2, "0")}T10:00:00Z`,
    created_at: T0,
    updated_at: T0,
  }));

  return [
    ...highTouch,
    {
      activity_id: "act-contoso-1",
      opportunity_id: "opp-contoso-expansion",
      kind: "finding",
      subject: "HIPAA questionnaire returned with two gaps",
      body: "Encryption at rest confirmed; audit-log retention still open.",
      occurred_at: T1,
      created_at: T1,
      updated_at: T1,
    },
  ];
}

export const ACTIVITIES: readonly Activity[] = buildActivities();

/** Four pending proposals with real before-snapshots, so the approvals diff has two sides. */
export const PROPOSALS: readonly Proposal[] = [
  {
    proposal_id: "prop-1",
    target_table: "opportunities",
    target_id: "opp-contoso-expansion",
    operation: "update",
    before_snapshot: { stage: "Security Review", value: 1_240_500 },
    after_payload: { stage: "Negotiation", value: 1_240_500 },
    reason: "Security review signed off; moving to commercial terms.",
    status: "pending",
    created_at: T2,
  },
  {
    proposal_id: "prop-2",
    target_table: "opportunities",
    target_id: "opp-northwind-renewal",
    operation: "update",
    before_snapshot: { value: 482_000, close_date: "2026-09-30" },
    after_payload: { value: 515_000, close_date: "2026-10-15" },
    reason: "Expanded logistics module added; close date slips two weeks.",
    status: "pending",
    created_at: T2,
  },
  {
    proposal_id: "prop-3",
    target_table: "contacts",
    target_id: "con-marcus",
    operation: "update",
    before_snapshot: { status: "active", title: "Procurement Lead" },
    after_payload: { status: "departed", title: "Former Procurement Lead" },
    reason: "Left the company; confirmed by Dana.",
    status: "pending",
    created_at: T1,
  },
  {
    proposal_id: "prop-4",
    target_table: "accounts",
    target_id: "acc-fabrikam",
    operation: "update",
    before_snapshot: { region: "eu-west-1", employee_count: 900 },
    after_payload: { region: "eu-central-1", employee_count: 1_150 },
    reason: "Headquarters moved; headcount refreshed from the annual report.",
    status: "pending",
    created_at: T1,
  },
];

export const AUDIT_RECORDS: readonly AuditRecord[] = [
  {
    audit_id: "aud-1",
    target_table: "opportunities",
    target_id: "opp-fabrikam-pilot",
    before_snapshot: { stage: "Negotiation" },
    after_snapshot: { stage: "Closed Won" },
    actor_rep_id: DEV_OTHER_REP_ID,
    created_at: T1,
  },
  {
    audit_id: "aud-2",
    target_table: "opportunities",
    target_id: "opp-northwind-renewal",
    before_snapshot: { stage: "Qualification" },
    after_snapshot: { stage: "Negotiation" },
    actor_rep_id: DEV_REP_ID,
    created_at: T2,
  },
  /** Already reversed: a second revert is refused. */
  {
    audit_id: "aud-3",
    target_table: "contacts",
    target_id: "con-dana",
    before_snapshot: { title: "Operations Lead" },
    after_snapshot: { title: "VP Operations" },
    actor_rep_id: DEV_REP_ID,
    created_at: T1,
    reversed_at: T2,
  },
];

/** The book each dev rep is scoped to. `acc-tailspin` is in neither. */
export const DEV_BOOKS: Readonly<Record<string, readonly string[]>> = {
  [DEV_REP_ID]: ["acc-northwind", "acc-contoso", "acc-quiet"],
  [DEV_OTHER_REP_ID]: ["acc-fabrikam"],
};
