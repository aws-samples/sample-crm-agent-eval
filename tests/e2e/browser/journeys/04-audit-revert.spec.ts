// SPDX-License-Identifier: MIT
/**
 * Journey 4 — an audited write is reverted once, and a second revert is refused: in the same tab by a
 * disabled `Already reverted` control and a `Reverted` badge, and from a stale tab by the server.
 * Each test creates the audited write it reverts, and asserts its seeded-data precondition. The
 * list asserts the reverted status; the revert time is read from the record's detail view.
 * Writes are tagged and undone by `fixtures/teardown.ts`.
 */
// `Page` comes from Playwright directly: `fixtures/auth.ts` re-exports only `test` and `expect`.
import type { Locator, Page } from "@playwright/test";

import { expect, test, type WriteLedger } from "../fixtures/teardown";

const FROM_STAGE = "Discovery";
const TO_STAGE = "Qualification";

/** The card's visible name: the text its stretched link is labelled by. */
async function cardNameOf(card: Locator): Promise<string> {
  return (
    await card
      .getByRole("link")
      .evaluate((link) => document.getElementById(link.getAttribute("aria-labelledby") ?? "")?.textContent ?? "")
  ).trim();
}

/**
 * Produces one committed, audited write through the UI (propose, then approve) and returns the
 * record it touched.
 */
async function commitAnAuditedWrite(
  page: Page,
  writes: WriteLedger,
): Promise<{ opportunityId: string; reason: string }> {
  await page.goto("/pipeline");

  const column = page.getByTestId(`pipeline-column-${FROM_STAGE}`);
  await expect(column).toBeVisible();

  // A card already awaiting approval is locked; act on the first one that is free to move.
  const card = column
    .locator("[data-opportunity-id]")
    .filter({ hasNot: page.getByTestId("pending-badge") })
    .first();
  await expect(
    card,
    `no record in the ${FROM_STAGE} column, so there is nothing to propose a change to. This journey ` +
      "asserts rather than skips: seed the demo tenant with `scripts/seed_demo_tenant.py` before " +
      "running G10.",
  ).toBeVisible();

  const opportunityId = (await card.getAttribute("data-opportunity-id")) ?? "";
  expect(opportunityId, "the board must expose the record id it is acting on").toBeTruthy();

  await card.getByTestId("opportunity-move-handle").focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("Enter");

  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  const reason = writes.tag(`journey-04 revert-refusal ${Date.now()}`);
  await dialog.getByLabel("Reason").fill(reason);
  await dialog.getByRole("button", { name: "Submit proposal" }).click();
  await expect(dialog).toBeHidden();

  // Approve it, so the change is committed and therefore audited.
  await page.goto("/approvals");
  const queued = page.getByRole("main").getByRole("listitem").filter({ hasText: reason });
  await expect(queued).toBeVisible();
  await queued.getByRole("button", { name: "Approve" }).click();
  await expect(queued).toHaveCount(0, { timeout: 30_000 });

  return { opportunityId, reason };
}

/** The value an audit record's detail view gives for `term`. */
function detailValue(page: Page, term: string): Locator {
  return page
    .getByRole("main")
    .locator("dt", { hasText: new RegExp(`^${term}$`) })
    .locator("xpath=following-sibling::dd[1]");
}

/** The actor an audit record's detail view names; empty when it names none. */
async function detailActor(page: Page): Promise<string> {
  const value = detailValue(page, "Actor");
  await expect(value).toBeVisible();
  const text = (await value.innerText()).trim();
  return text === "Unknown" ? "" : text;
}

/** The audit row for `opportunityId`, newest first — the one this journey just created. */
function auditRowFor(page: Page, opportunityId: string) {
  return page.getByRole("main").getByRole("listitem").filter({ hasText: opportunityId }).first();
}

test("a reverted audit record refuses a second revert, and says so three ways", async ({
  signedInPage: page,
  writes,
}) => {
  const { opportunityId } = await commitAnAuditedWrite(page, writes);

  await page.goto("/audit");
  const row = auditRowFor(page, opportunityId);
  await expect(row).toBeVisible({ timeout: 30_000 });

  // Before the revert: the record offers an undo.
  const revert = row.getByRole("button", { name: "Revert", exact: true });
  await expect(revert).toBeEnabled();
  await expect(row.getByText("Reverted", { exact: true })).toHaveCount(0);

  // The trail attributes the write — when, not just what: the row's machine timestamp.
  await expect(row.locator("time").first()).toHaveAttribute("datetime", /\d{4}-\d{2}-\d{2}/);
  // And who: the list projection carries no actor, so read it from the record's detail view.
  await row.getByRole("link").first().click();
  expect(await detailActor(page), "the audit record must name who made the change").toBeTruthy();
  await page.goBack();
  await expect(row).toBeVisible({ timeout: 30_000 });

  await revert.click();

  // All three refusal signals, each named so a failure says which one regressed.
  await expect(
    row.getByRole("button", { name: "Already reverted" }),
    "signal 1 of 3: the control's label must change, not merely its enabled state",
  ).toBeVisible({ timeout: 30_000 });
  await expect(
    row.getByRole("button", { name: "Already reverted" }),
    "signal 2 of 3: the control must be disabled, so there is no way to submit a second revert",
  ).toBeDisabled();
  await expect(
    row.getByText("Reverted", { exact: true }),
    "signal 3 of 3: the row must carry a status badge, so the state is legible without reading the button",
  ).toBeVisible();

  // The original `Revert` control is gone, not merely restyled.
  await expect(row.getByRole("button", { name: "Revert", exact: true })).toHaveCount(0);

  // The revert is itself recorded with its own timestamp, on the record's detail view.
  await row.getByRole("link").first().click();
  await expect(detailValue(page, "Status")).toHaveText("Reverted");
  await expect(detailValue(page, "Reverted").locator("time")).toHaveAttribute(
    "datetime",
    /\d{4}-\d{2}-\d{2}T/,
  );

  // The board reflects the undo.
  await page.goto("/pipeline");
  const card = page.locator(`[data-opportunity-id="${opportunityId}"]`);
  await expect(card).toBeVisible();
  await expect(page.getByTestId(`pipeline-column-${FROM_STAGE}`)).toContainText(await cardNameOf(card));
});

test("a second revert from a stale tab is refused by the server, not silently applied", async ({
  signedInPage: page,
  writes,
}) => {
  const { opportunityId } = await commitAnAuditedWrite(page, writes);

  await page.goto("/audit");
  const row = auditRowFor(page, opportunityId);
  await expect(row).toBeVisible({ timeout: 30_000 });

  // A second tab opened before the first revert still holds an enabled `Revert` button.
  const stale = await page.context().newPage();
  await stale.goto("/audit");
  const staleRow = auditRowFor(stale, opportunityId);
  await expect(staleRow).toBeVisible({ timeout: 30_000 });
  await expect(staleRow.getByRole("button", { name: "Revert", exact: true })).toBeEnabled();

  await row.getByRole("button", { name: "Revert", exact: true }).click();
  await expect(row.getByRole("button", { name: "Already reverted" })).toBeVisible({ timeout: 30_000 });

  // The stale tab still believes it can revert. It submits, and must be refused.
  await staleRow.getByRole("button", { name: "Revert", exact: true }).click();

  const notice = staleRow.getByRole("alert");
  await expect(
    notice,
    "a refused second revert must be stated; silence would leave the user believing it applied",
  ).toBeVisible({ timeout: 30_000 });
  await expect(notice).toContainText(/can.t revert|changed|conflict/i);
  // The user is offered a refresh.
  await expect(stale.getByRole("button", { name: /refresh and review/i })).toBeVisible();

  // The change was undone once: the record still sits in the pre-change stage.
  await stale.close();
  await page.goto("/pipeline");
  const card = page.locator(`[data-opportunity-id="${opportunityId}"]`);
  await expect(card).toBeVisible();
  const cardName = await cardNameOf(card);
  await expect(page.getByTestId(`pipeline-column-${FROM_STAGE}`)).toContainText(cardName);
  await expect(page.getByTestId(`pipeline-column-${TO_STAGE}`)).not.toContainText(cardName);
});
