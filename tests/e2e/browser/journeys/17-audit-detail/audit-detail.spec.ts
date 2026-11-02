// SPDX-License-Identifier: MIT
/**
 * Journey 17: an audit card opens `/audit/:id` with the record's metadata and its field diff
 * (an update as a before/after change, a create as after-only values, a delete as before-only
 * values), and a revert works from the detail view. A record written without snapshots is covered by
 * the vitest cell in frontend/src/pages/audit/AuditDetailPage.test.tsx, not here.
 *
 * User: the demo user (`CRM_DEMO_USER_EMAIL`), who approves its own proposals here as journey 04 does.
 * Each test creates the audited writes it opens; `fixtures/teardown.ts` undoes them.
 */
import type { Page } from "@playwright/test";

import { expect, test, type WriteLedger } from "../../fixtures/teardown";

const FROM_STAGE = "Discovery";

/** A board card with no pending proposal: its move handle is enabled. */
const MOVABLE_CARD = '[data-opportunity-id]:has([data-testid="opportunity-move-handle"]:not([disabled]))';

/** Propose a stage change on the board, approve it, and return the record it touched. */
async function commitAStageChange(page: Page, writes: WriteLedger): Promise<string> {
  await page.goto("/pipeline");
  const card = page.getByTestId(`pipeline-column-${FROM_STAGE}`).locator(MOVABLE_CARD).first();
  await expect(
    card,
    `no movable record in the ${FROM_STAGE} column; seed the demo tenant with scripts/seed_demo_tenant.py`,
  ).toBeVisible();
  const opportunityId = (await card.getAttribute("data-opportunity-id")) ?? "";
  expect(opportunityId, "the board must expose the record id it is acting on").toBeTruthy();

  await card.getByTestId("opportunity-move-handle").focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog");
  const reason = writes.tag(`journey-17 audit-detail ${Date.now()}`);
  await dialog.getByLabel("Reason").fill(reason);
  await dialog.getByRole("button", { name: "Submit proposal" }).click();
  await expect(dialog).toBeHidden();

  await approveByReason(page, reason);
  return opportunityId;
}

async function approveByReason(page: Page, reason: string): Promise<void> {
  await page.goto("/approvals");
  const queued = page.getByRole("main").getByRole("listitem").filter({ hasText: reason });
  await expect(queued).toBeVisible();
  await queued.getByRole("button", { name: "Approve" }).click();
  await expect(queued).toHaveCount(0, { timeout: 30_000 });
}

/**
 * Open the newest audit card naming `targetId`, and land on its detail page. A card names only its
 * target, so `action` is checked against the detail page's Action, retried until the newest card is
 * the expected write.
 */
async function openAuditDetailFor(page: Page, targetId: string, action?: string): Promise<void> {
  await expect(async () => {
    await page.goto("/audit");
    const card = page.getByRole("main").getByRole("listitem").filter({ hasText: targetId });
    await expect(card.first()).toBeVisible({ timeout: 10_000 });
    await card.first().getByRole("link").first().click();
    await expect(page).toHaveURL(/\/audit\/[^/]+$/);
    if (action !== undefined) await expect(metadata(page, "Action")).toHaveText(action, { timeout: 5_000 });
  }).toPass({ timeout: 60_000 });
}

function metadata(page: Page, term: string) {
  return page.getByRole("main").locator("dt", { hasText: new RegExp(`^${term}$`) }).locator("xpath=following-sibling::dd[1]");
}

test("an update's card opens its detail with metadata and a before/after diff, and reverts there", async ({
  signedInPage: page,
  writes,
}) => {
  const opportunityId = await commitAStageChange(page, writes);
  await openAuditDetailFor(page, opportunityId);

  await expect(page.getByRole("heading", { level: 1 })).toHaveText(`opportunities record ${opportunityId}`);
  for (const term of ["Action", "Actor", "Recorded", "Target", "Status"]) {
    await expect(metadata(page, term)).not.toBeEmpty();
  }
  await expect(metadata(page, "Status")).toHaveText("Active");
  await expect(page.getByLabel(`Change to opportunities record ${opportunityId}`)).toBeVisible();

  await page.getByRole("button", { name: "Revert", exact: true }).click();
  await expect(page.getByRole("button", { name: "Already reverted" })).toBeDisabled({ timeout: 30_000 });
  await expect(page.getByText("Reverted", { exact: true }).first()).toBeVisible();
  await expect(metadata(page, "Status")).toHaveText("Reverted");
});

test("a create shows after-only values and a delete shows before-only values", async ({
  signedInPage: page,
  writes,
}) => {
  const name = writes.tag(`journey-17 account ${Date.now()}`);
  await page.goto("/accounts/new");
  await page.getByLabel("Name").fill(name);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible({ timeout: 30_000 });
  const accountId = decodeURIComponent(new URL(page.url()).pathname.split("/").pop() ?? "");
  expect(accountId).toBeTruthy();
  writes.created(accountId);

  await openAuditDetailFor(page, accountId, "Created");
  await expect(page.getByLabel(`Values accounts record ${accountId} was created with`)).toBeVisible();
  await expect(page.getByLabel(/held when deleted$/)).toHaveCount(0);

  // Delete it through a proposal, approve, and open the delete's audit record.
  await page.goto(`/accounts/${encodeURIComponent(accountId)}`);
  await page.getByRole("button", { name: `Delete ${name}` }).click();
  const reason = writes.tag(`journey-17 delete ${Date.now()}`);
  const confirmGroup = page.getByRole("group", { name: `Confirm delete of ${name}` });
  await confirmGroup.getByLabel("Reason (optional)").fill(reason);
  await confirmGroup.getByRole("button", { name: "Confirm delete proposal" }).click();
  await expect(page.getByText(/Awaiting approval/)).toBeVisible();
  await approveByReason(page, reason);

  await openAuditDetailFor(page, accountId, "Deleted");
  await expect(page.getByLabel(`Values accounts record ${accountId} held when deleted`)).toBeVisible();
  await expect(page.getByLabel(/was created with$/)).toHaveCount(0);
});
