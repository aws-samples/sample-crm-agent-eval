// SPDX-License-Identifier: MIT
/**
 * Journey 18: an unrelated write to a proposal's record (`log_activity`) does not block its
 * approval; a write to a field the proposal touches is refused at approval with the three-way view
 * (at submit / now / proposed), and "Refresh and review current state" rebases it so the next
 * approval succeeds.
 *
 * User: the demo user (`CRM_DEMO_USER_EMAIL`). The racing writes go through the deployed API from the
 * signed-in page, because the board locks a card that has a pending proposal.
 */
import type { Page } from "@playwright/test";

import { expect, test, type WriteLedger } from "../../fixtures/teardown";
import { field, postAsSignedInUser, transactionKey } from "../../fixtures/pageApi";

const FROM_STAGE = "Discovery";
/** A stage the racing proposal moves the record to, other than the board's next column. */
const RACING_STAGE = "Negotiation";

/** A board card with no pending proposal: its move handle is enabled. */
const MOVABLE_CARD = '[data-opportunity-id]:has([data-testid="opportunity-move-handle"]:not([disabled]))';

/** Propose a one-column stage move on the board; returns the record and the proposal's reason. */
async function proposeOnTheBoard(
  page: Page,
  writes: WriteLedger,
  label: string,
): Promise<{ opportunityId: string; reason: string }> {
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
  const reason = writes.tag(`journey-18 ${label} ${Date.now()}`);
  await dialog.getByLabel("Reason").fill(reason);
  await dialog.getByRole("button", { name: "Submit proposal" }).click();
  await expect(dialog).toBeHidden();
  return { opportunityId, reason };
}

function queuedProposal(page: Page, reason: string) {
  return page.getByRole("main").getByRole("listitem").filter({ hasText: reason });
}

test("an unrelated write to the record does not block the proposal's approval", async ({
  signedInPage: page,
  writes,
}) => {
  const { opportunityId, reason } = await proposeOnTheBoard(page, writes, "unrelated-write");

  const logged = await postAsSignedInUser(page, "log_activity", {
    opportunity_id: opportunityId,
    kind: "note",
    subject: writes.tag(`journey-18 unrelated write ${Date.now()}`),
    transaction_key: transactionKey("j18-activity"),
  });
  expect(logged.status, JSON.stringify(logged.body)).toBe(200);
  writes.created(String(field(logged.body, "activity", "activity_id")));

  await page.goto("/approvals");
  const queued = queuedProposal(page, reason);
  await expect(queued).toBeVisible();
  await queued.getByRole("button", { name: "Approve" }).click();
  await expect(queued).toHaveCount(0, { timeout: 30_000 });
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("a write to a touched field shows the three-way view, and a refresh rebases it for approval", async ({
  signedInPage: page,
  writes,
}) => {
  const { opportunityId, reason } = await proposeOnTheBoard(page, writes, "touched-field");

  // A second proposal on the same field, approved first, moves the record underneath the first.
  const racing = await postAsSignedInUser(page, "submit_proposal", {
    target_table: "opportunities",
    target_id: opportunityId,
    operation: "update",
    after_payload: { stage: RACING_STAGE },
    reason: writes.tag(`journey-18 racing write ${Date.now()}`),
    transaction_key: transactionKey("j18-racing-submit"),
  });
  expect(racing.status, JSON.stringify(racing.body)).toBe(200);
  const racingId = String(field(racing.body, "proposal", "proposal_id"));
  const applied = await postAsSignedInUser(page, "approve_proposal", {
    proposal_id: racingId,
    transaction_key: transactionKey("j18-racing-approve"),
  });
  expect(applied.status, JSON.stringify(applied.body)).toBe(200);

  await page.goto("/approvals");
  const queued = queuedProposal(page, reason);
  await expect(queued).toBeVisible();
  await queued.getByRole("button", { name: "Approve" }).click();

  const conflict = queued.getByRole("alert").filter({ hasText: /Can.t approve/ });
  await expect(conflict).toBeVisible({ timeout: 30_000 });
  await expect(conflict).toHaveAttribute("data-conflict-reason", "fields_changed");
  const threeWay = conflict.getByRole("table", { name: "Fields that changed since this proposal was submitted" });
  await expect(threeWay.getByRole("columnheader")).toHaveText(["Field", "At submit", "Now", "Proposed"]);
  const stageRow = threeWay.getByRole("row").filter({ hasText: "stage" });
  await expect(stageRow).toContainText(FROM_STAGE);
  await expect(stageRow).toContainText(RACING_STAGE);

  await conflict.getByRole("button", { name: "Refresh and review current state" }).click();
  await expect(conflict).toHaveCount(0, { timeout: 30_000 });

  const rebased = queuedProposal(page, reason);
  await expect(rebased).toBeVisible();
  await rebased.getByRole("button", { name: "Approve" }).click();
  await expect(rebased).toHaveCount(0, { timeout: 30_000 });
});
