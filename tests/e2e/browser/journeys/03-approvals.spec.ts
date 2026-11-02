// SPDX-License-Identifier: MIT
/**
 * Journey 3 — an approver reads a before/after diff that differs on more than colour, approves by
 * keyboard, and the change is recorded in the audit trail; a second approval of the same proposal is
 * refused as a conflict. Each test creates its own proposal.
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

/** The actor an audit record's detail view names; empty when it names none. */
async function detailActor(page: Page): Promise<string> {
  const value = page
    .getByRole("main")
    .locator("dt", { hasText: /^Actor$/ })
    .locator("xpath=following-sibling::dd[1]");
  await expect(value).toBeVisible();
  const text = (await value.innerText()).trim();
  return text === "Unknown" ? "" : text;
}

/**
 * Creates one pending stage-change proposal through the pipeline UI and returns its target record id
 * and the reason text, so the approvals queue row can be identified unambiguously.
 */
async function proposeStageChange(
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
  await expect(card).toBeVisible();

  const opportunityId = (await card.getAttribute("data-opportunity-id")) ?? "";
  expect(opportunityId, "the board must expose the record id it is acting on").toBeTruthy();

  await card.getByTestId("opportunity-move-handle").focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("Enter");

  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  const reason = writes.tag(`journey-03 approve-and-audit ${Date.now()}`);
  await dialog.getByLabel("Reason").fill(reason);
  await dialog.getByRole("button", { name: "Submit proposal" }).click();
  await expect(dialog).toBeHidden();

  return { opportunityId, reason };
}

test("approving a proposal by keyboard applies it and records it in the audit trail", async ({
  signedInPage: page,
  writes,
}) => {
  const { opportunityId, reason } = await proposeStageChange(page, writes);

  await page.goto("/approvals");

  // Scoped to `main`: the header nav also renders `<li>` elements.
  const queue = page.getByRole("main").getByRole("listitem");
  const row = queue.filter({ hasText: reason });
  await expect(row).toBeVisible();
  await expect(row).toContainText(opportunityId);

  // Nothing has mutated yet, and the row says so.
  await expect(row).toContainText(/proposed/i);

  /*
   * The diff does not rest on colour. Status values are `changed`/`unchanged` per field, with a changed
   * field carrying both sides; the sides differ in computed `text-decoration-line`.
   */
  const changedField = row.locator('[data-diff-status="changed"]').first();
  await expect(changedField).toBeVisible();
  await expect(changedField).toContainText(FROM_STAGE);
  await expect(changedField).toContainText(TO_STAGE);
  await expect(changedField).toContainText("stage");

  const decorations = await changedField.evaluate((field) =>
    [...field.querySelectorAll("span")]
      .map((span) => getComputedStyle(span).textDecorationLine)
      .filter((line) => line !== "none"),
  );
  expect(
    new Set(decorations).size,
    `before and after must differ on a non-colour channel; decorations seen: ${decorations.join(", ")}`,
  ).toBeGreaterThan(1);

  // ---- approve, by keyboard ----
  const approve = row.getByRole("button", { name: "Approve" });
  await approve.focus();
  await expect(approve).toBeFocused();
  await page.keyboard.press("Enter");

  // The row leaves the pending queue; other proposals may still be waiting.
  await expect(row).toHaveCount(0, { timeout: 30_000 });

  // ---- the change is in the audit trail ----
  await page.goto("/audit");
  const auditRow = page.getByRole("main").getByRole("listitem").filter({ hasText: opportunityId }).first();
  await expect(auditRow).toBeVisible({ timeout: 30_000 });
  // An audit record carries when, not just what: the row's machine timestamp.
  await expect(auditRow.locator("time").first()).toHaveAttribute("datetime", /\d{4}-\d{2}-\d{2}/);

  // The list projection carries no snapshots; the record's detail view shows the applied change.
  await auditRow.getByRole("link").first().click();
  const detail = page.getByRole("main");
  await expect(detail.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(detail).toContainText(opportunityId);
  await expect(detail).toContainText(TO_STAGE);
  await expect(detail.locator("time").first()).toHaveAttribute("datetime", /\d{4}-\d{2}-\d{2}/);
  // And who: the detail view names the acting principal.
  expect(await detailActor(page), "the audit record must name who made the change").toBeTruthy();

  // And the board now shows the applied stage, with the pending lock released.
  await page.goto("/pipeline");
  const movedCard = page.locator(`[data-opportunity-id="${opportunityId}"]`);
  await expect(movedCard).toBeVisible();
  await expect(page.getByTestId(`pipeline-column-${TO_STAGE}`)).toContainText(
    await cardNameOf(movedCard),
  );
  await expect(movedCard.getByTestId("pending-badge")).toHaveCount(0);
});

test("a second approval of the same proposal is refused as a conflict, not applied twice", async ({
  signedInPage: page,
  writes,
}) => {
  const { reason } = await proposeStageChange(page, writes);

  await page.goto("/approvals");
  const row = page.getByRole("main").getByRole("listitem").filter({ hasText: reason });
  await expect(row).toBeVisible();

  // Two approvals from two tabs of the same session; the second comes back as a conflict.
  const second = await page.context().newPage();
  await second.goto("/approvals");
  const secondRow = second.getByRole("main").getByRole("listitem").filter({ hasText: reason });
  await expect(secondRow).toBeVisible();

  await row.getByRole("button", { name: "Approve" }).click();
  await expect(row).toHaveCount(0, { timeout: 30_000 });

  await secondRow.getByRole("button", { name: "Approve" }).click();

  // A conflict, stated as such and offering a way forward.
  const alert = second.getByRole("alert");
  await expect(alert).toBeVisible({ timeout: 30_000 });
  await expect(alert).toContainText(/can.t approve|changed|conflict/i);
  await expect(second.getByRole("button", { name: /refresh and review/i })).toBeVisible();

  await second.close();
});
