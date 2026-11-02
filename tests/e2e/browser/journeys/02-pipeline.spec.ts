// SPDX-License-Identifier: MIT
/**
 * Journey 2 — a keyboard stage change on the pipeline board issues exactly one `submit_proposal` call
 * and no `update_opportunity` call, and a refused transition offers no way to submit it.
 */
import type { Locator } from "@playwright/test";

import { expect, test } from "../fixtures/teardown";

/** The tool routes this journey asserts about, by name rather than by full URL. */
const PROPOSAL_TOOL = "submit_proposal";
const DIRECT_COMMIT_TOOL = "update_opportunity";

/** A stage the board's leftmost column holds, and the one to its right. Mirrors `OPPORTUNITY_STAGES`. */
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

test("a keyboard stage change submits a proposal and never a direct commit", async ({
  signedInPage: page,
  writes,
}) => {
  // Tool calls are recorded from the network.
  const toolCalls: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "POST") return;
    const match = new URL(request.url()).pathname.match(/\/api\/([a-z_]+)$/);
    if (match) toolCalls.push(match[1]);
  });

  await page.goto("/pipeline");

  // The board renders stage columns before the first fetch settles, so wait for a card.
  const fromColumn = page.getByTestId(`pipeline-column-${FROM_STAGE}`);
  await expect(fromColumn).toBeVisible();
  // A card already awaiting approval is locked; act on the first one that is free to move.
  const card = fromColumn
    .locator("[data-opportunity-id]")
    .filter({ hasNot: page.getByTestId("pending-badge") })
    .first();
  await expect(card).toBeVisible();
  const cardName = await cardNameOf(card);
  expect(cardName, "the card must name the record it shows").toBeTruthy();
  const opportunityId = await card.getAttribute("data-opportunity-id");
  expect(opportunityId, "the board must expose the record id it is acting on").toBeTruthy();
  const handle = card.getByTestId("opportunity-move-handle");

  // ---- keyboard only, from here to the dialog ----
  await handle.focus();
  await page.keyboard.press("Enter");
  await expect(handle).toHaveAttribute("aria-pressed", "true");

  // The live region announces the pick-up.
  await expect(page.getByTestId("pipeline-live-region")).toContainText(/picked up/i);

  await page.keyboard.press("ArrowRight");
  await expect(page.getByTestId(`pipeline-column-${TO_STAGE}`)).toHaveAttribute(
    "data-keyboard-target",
    "true",
  );

  await page.keyboard.press("Enter");

  // ---- the proposal dialog ----
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute("aria-modal", "true");
  await expect(dialog).toContainText(FROM_STAGE);
  await expect(dialog).toContainText(TO_STAGE);
  // The change is described as awaiting approval, not as applied.
  await expect(dialog).toContainText(/awaiting approval/i);

  const reason = writes.tag(`journey-02 keyboard stage change ${Date.now()}`);
  await dialog.getByLabel("Reason").fill(reason);
  await dialog.getByRole("button", { name: "Submit proposal" }).click();

  await expect(dialog).toBeHidden();

  // ---- one proposal, no direct commit ----
  await expect
    .poll(() => toolCalls.filter((tool) => tool === PROPOSAL_TOOL).length, {
      message: "the stage change must reach the API as a proposal",
    })
    .toBe(1);

  expect(
    toolCalls.filter((tool) => tool === DIRECT_COMMIT_TOOL),
    `a stage change must never issue ${DIRECT_COMMIT_TOOL}; calls seen: ${toolCalls.join(", ")}`,
  ).toEqual([]);

  // The card is still in its original column, locked pending approval.
  await expect(page.getByTestId(`pipeline-column-${FROM_STAGE}`)).toContainText(cardName);
  const lockedCard = page.locator(`[data-opportunity-id="${opportunityId}"]`);
  // Locked against both move paths: the keyboard handle is disabled and the card is not draggable.
  await expect(lockedCard.getByTestId("opportunity-move-handle")).toBeDisabled();
  await expect(lockedCard).toHaveAttribute("draggable", "false");
  await expect(lockedCard.getByTestId("pending-badge")).toContainText(TO_STAGE);
});

test("a refused transition is explained and offers no way to submit it", async ({
  signedInPage: page,
}) => {
  // A closed stage is terminal; the refusal is shown before any round trip and is not submittable.
  const toolCalls: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "POST") return;
    const match = new URL(request.url()).pathname.match(/\/api\/([a-z_]+)$/);
    if (match) toolCalls.push(match[1]);
  });

  await page.goto("/pipeline");

  const closedColumn = page.getByTestId("pipeline-column-Closed Won");
  await expect(closedColumn).toBeVisible();
  const cards = closedColumn.locator("[data-opportunity-id]");

  // Skipped when the seeded book holds no closed-won record.
  test.skip(
    (await cards.count()) === 0,
    "no Closed Won record in this tenant's book, so there is no terminal transition to refuse",
  );

  await cards.first().getByTestId("opportunity-move-handle").focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("Enter");

  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(/not permitted/i);
  await expect(dialog.getByRole("button", { name: "Submit proposal" })).toHaveCount(0);
  await expect(dialog.getByLabel("Reason")).toHaveCount(0);

  expect(
    toolCalls.filter((tool) => tool === PROPOSAL_TOOL || tool === DIRECT_COMMIT_TOOL),
    "a refused transition must reach neither the proposal nor the commit path",
  ).toEqual([]);
});
