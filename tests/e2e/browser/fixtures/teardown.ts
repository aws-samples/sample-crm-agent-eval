// SPDX-License-Identifier: MIT
/**
 * Per-test teardown for the journeys that write. `test` extends `fixtures/auth.ts` with a `writes`
 * ledger: a journey tags every proposal reason and every created record's name with `writes.tag(…)`,
 * which appends a marker unique to this run and this test. A journey also registers what it cannot tag
 * (`writes.proposal(id)` for an assistant-raised proposal, `writes.created(id)` for a direct create).
 * After the test body, the fixture undoes exactly those artefacts through the API as the signed-in
 * demo user:
 *
 * - pending proposals carrying the marker, or registered by id, are rejected;
 * - their approved proposals' audit records are reverted. Each approved proposal is paired with the
 *   one audit record on its target that writes its proposed values (or empties it, for a delete),
 *   nearest its approval time;
 * - a registered created record's audit records are all reverted, which deletes it. Its create
 *   record must carry the marker. For an activity, the update record of its opportunity's touch
 *   (`last_touch` = the activity's `occurred_at`, written in the same transaction) is reverted too;
 * - reverts run newest first across all targets; a record already reverted is left alone.
 *
 * Anything unmatched, unreadable or refused is collected, and the test fails with the whole list once
 * every other artefact has been handled. Nothing without the marker or a registration is touched.
 */
import type { Page, TestInfo } from "@playwright/test";

import { expect, test as signedInTest } from "./auth";
import { postAsSignedInUser, transactionKey } from "./pageApi";
import { randomToken } from "./randomToken";

const RUN_ID_ENV_VAR = "CRM_E2E_RUN_ID";
const RUN_ID_PATTERN = /^[a-z0-9-]{1,32}$/;

/** How far an approval's audit record may sit from the proposal's `updated_at`. */
const PAIRING_WINDOW_MS = 30_000;

/** Upper bound on pages read from one listing, so a cursor that never ends fails instead of looping. */
const MAX_PAGES = 100;

const PROPOSAL_REBASED = "proposal_rebased";

function runId(): string {
  const supplied = process.env[RUN_ID_ENV_VAR];
  if (supplied === undefined || supplied === "") {
    return `${Date.now().toString(36)}${randomToken(4)}`;
  }
  if (!RUN_ID_PATTERN.test(supplied)) {
    throw new Error(`${RUN_ID_ENV_VAR}=${JSON.stringify(supplied)} must match ${RUN_ID_PATTERN}`);
  }
  return supplied;
}

/** This process's run id: one per `playwright test` invocation (`workers: 1`). */
export const RUN_ID = runId();

type Item = Record<string, unknown>;

function isItem(value: unknown): value is Item {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(item: Item, key: string): string {
  const value = item[key];
  return typeof value === "string" ? value : "";
}

function millis(iso: string): number {
  const parsed = Date.parse(iso);
  return Number.isNaN(parsed) ? Number.NaN : parsed;
}

function anyStringContains(snapshot: unknown, needle: string): boolean {
  return isItem(snapshot) && Object.values(snapshot).some((v) => typeof v === "string" && v.includes(needle));
}

function isEmptySnapshot(snapshot: unknown): boolean {
  return !isItem(snapshot) || Object.keys(snapshot).length === 0;
}

/** Whether `audit` is the write `proposal` asked for: its proposed values, or an empty after-image. */
function writesProposal(audit: Item, proposal: Item): boolean {
  if (text(audit, "target_id") !== text(proposal, "target_id")) return false;
  if (text(proposal, "operation") === "delete") return isEmptySnapshot(audit.after_snapshot);
  const after = audit.after_snapshot;
  const payload = proposal.after_payload;
  if (!isItem(after) || !isItem(payload)) return false;
  return Object.entries(payload).every(([key, value]) => JSON.stringify(after[key]) === JSON.stringify(value));
}

/** The artefacts one test wrote, and the marker that identifies them. */
export class WriteLedger {
  readonly marker: string;
  private readonly proposalIds = new Set<string>();
  private readonly createdIds = new Set<string>();

  constructor(marker: string) {
    this.marker = marker;
  }

  /** `label` with this test's marker appended: use it for every proposal reason and created name. */
  tag(label: string): string {
    return `${label} [${this.marker}]`;
  }

  /** Register a proposal this test raised without writing its reason (an assistant proposal). */
  proposal(proposalId: string): void {
    expect(proposalId, "a registered proposal needs its id").toBeTruthy();
    this.proposalIds.add(proposalId);
  }

  /** Register a record this test created directly; its name or subject must carry `tag(…)`. */
  created(recordId: string): void {
    expect(recordId, "a registered record needs its id").toBeTruthy();
    this.createdIds.add(recordId);
  }

  /** Undo this test's artefacts; throws listing every one that could not be undone. */
  async teardown(page: Page): Promise<void> {
    const failures: string[] = [];
    const call = async (route: string, body: Item): Promise<Item | null> => {
      const reply = await postAsSignedInUser(page, route, body);
      if (reply.status !== 200 || !isItem(reply.body)) {
        failures.push(`${route} ${JSON.stringify(body)} answered ${reply.status}: ${JSON.stringify(reply.body)}`);
        return null;
      }
      return reply.body;
    };
    const listAll = async (route: string, key: string, body: Item): Promise<Item[] | null> => {
      const items: Item[] = [];
      let cursor: string | null = null;
      for (let pageIndex = 0; pageIndex < MAX_PAGES; pageIndex += 1) {
        const reply = await call(route, cursor === null ? body : { ...body, cursor });
        if (reply === null) return null;
        const batch = reply[key];
        if (!Array.isArray(batch)) {
          failures.push(`${route} reply has no ${key} array: ${JSON.stringify(reply)}`);
          return null;
        }
        items.push(...batch.filter(isItem));
        const next = reply.next_cursor;
        if (typeof next !== "string" || next === "") return items;
        cursor = next;
      }
      failures.push(`${route} ${JSON.stringify(body)} still had a cursor after ${MAX_PAGES} pages`);
      return null;
    };
    const ours = (proposal: Item): boolean =>
      text(proposal, "reason").includes(this.marker) || this.proposalIds.has(text(proposal, "proposal_id"));

    const pending = ((await listAll("list_proposals", "proposals", { status: "pending" })) ?? []).filter(ours);
    const approved = ((await listAll("list_proposals", "proposals", { status: "approved" })) ?? []).filter(ours);

    const seen = new Set([...pending, ...approved].map((proposal) => text(proposal, "proposal_id")));
    const unseen = [...this.proposalIds].filter((id) => !seen.has(id));
    if (unseen.length > 0) {
      const rejected = new Set(
        ((await listAll("list_proposals", "proposals", { status: "rejected" })) ?? []).map((proposal) =>
          text(proposal, "proposal_id"),
        ),
      );
      for (const id of unseen.filter((candidate) => !rejected.has(candidate))) {
        failures.push(`registered proposal ${id} is not pending, approved or rejected`);
      }
    }

    for (const proposal of pending) {
      await call("reject_proposal", {
        proposal_id: text(proposal, "proposal_id"),
        reason: `teardown ${this.marker}`,
        transaction_key: transactionKey("teardown-reject"),
      });
    }

    const auditsByTarget = new Map<string, Item[]>();
    const auditsOn = async (targetId: string): Promise<Item[]> => {
      const cached = auditsByTarget.get(targetId);
      if (cached !== undefined) return cached;
      const full: Item[] = [];
      for (const row of (await listAll("list_audit_records", "audit_records", { target_id: targetId })) ?? []) {
        const detail = await call("get_audit_record", { audit_id: text(row, "audit_id") });
        const record = detail === null ? undefined : detail.audit_record;
        if (isItem(record) && text(record, "action") !== PROPOSAL_REBASED) full.push(record);
      }
      auditsByTarget.set(targetId, full);
      return full;
    };

    const toRevert = new Map<string, Item>();
    for (const proposal of approved) {
      const approvedAt = millis(text(proposal, "updated_at"));
      const candidates = (await auditsOn(text(proposal, "target_id")))
        .filter((audit) => writesProposal(audit, proposal))
        .map((audit) => ({ audit, distance: Math.abs(millis(text(audit, "created_at")) - approvedAt) }))
        .filter(({ distance }) => distance <= PAIRING_WINDOW_MS)
        .sort((a, b) => a.distance - b.distance);
      const nearest = candidates[0];
      if (nearest === undefined) {
        failures.push(
          `approved proposal ${text(proposal, "proposal_id")} on ${text(proposal, "target_id")} has no audit ` +
            `record writing its values within ${PAIRING_WINDOW_MS} ms of ${text(proposal, "updated_at")}`,
        );
        continue;
      }
      toRevert.set(text(nearest.audit, "audit_id"), nearest.audit);
    }

    for (const recordId of this.createdIds) {
      const audits = await auditsOn(recordId);
      const create = audits.find((audit) => isEmptySnapshot(audit.before_snapshot));
      if (create === undefined) {
        failures.push(`created record ${recordId} has no create audit record, so it cannot be removed`);
        continue;
      }
      if (!anyStringContains(create.after_snapshot, this.marker)) {
        failures.push(`created record ${recordId} does not carry the marker ${this.marker}; left untouched`);
        continue;
      }
      for (const audit of audits) toRevert.set(text(audit, "audit_id"), audit);

      // An activity's create also touches its opportunity, audited in the same transaction.
      const created = create.after_snapshot;
      if (isItem(created) && typeof created.opportunity_id === "string" && typeof created.occurred_at === "string") {
        const createdAt = millis(text(create, "created_at"));
        const touch = (await auditsOn(created.opportunity_id))
          .filter(
            (audit) =>
              !isEmptySnapshot(audit.before_snapshot) &&
              isItem(audit.after_snapshot) &&
              audit.after_snapshot.last_touch === created.occurred_at,
          )
          .map((audit) => ({ audit, distance: Math.abs(millis(text(audit, "created_at")) - createdAt) }))
          .filter(({ distance }) => distance <= PAIRING_WINDOW_MS)
          .sort((a, b) => a.distance - b.distance)[0];
        if (touch === undefined) {
          failures.push(
            `activity ${recordId} has no audit record of its touch on opportunity ${created.opportunity_id}`,
          );
        } else {
          toRevert.set(text(touch.audit, "audit_id"), touch.audit);
        }
      }
    }

    const newestFirst = [...toRevert.values()]
      .filter((audit) => text(audit, "status") === "active")
      .sort((a, b) => millis(text(b, "created_at")) - millis(text(a, "created_at")));
    for (const audit of newestFirst) {
      await call("revert_audit_record", {
        audit_id: text(audit, "audit_id"),
        reason: `teardown ${this.marker}`,
        transaction_key: transactionKey("teardown-revert"),
      });
    }

    expect(failures, `teardown for ${this.marker} could not undo every artefact`).toEqual([]);
  }
}

/** The marker for `testInfo`: this run's id and the test's stable id. */
export function markerFor(testInfo: TestInfo): string {
  return `e2e:${RUN_ID}:${testInfo.testId}`;
}

/** `test` with `signedInPage` and a `writes` ledger whose artefacts are undone after each test. */
export const test = signedInTest.extend<{ writes: WriteLedger }>({
  writes: async ({ signedInPage }, use, testInfo) => {
    const ledger = new WriteLedger(markerFor(testInfo));
    await use(ledger);
    await ledger.teardown(signedInPage);
  },
});

export { expect };
