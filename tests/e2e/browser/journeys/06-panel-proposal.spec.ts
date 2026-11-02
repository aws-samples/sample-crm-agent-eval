// SPDX-License-Identifier: MIT
/**
 * Journey 6 — a high-risk change requested from the assistant panel yields a proposal card and commits
 * nothing: the transcript names no direct-commit tool, the board shows the record unmoved under a
 * pending lock, the audit trail holds no applied change, and the proposal waits in the approvals queue.
 */
import type { Locator, Page } from "@playwright/test";

import { expect, test, type WriteLedger } from "../fixtures/teardown";

/** The tool a high-risk change must travel through, and the ones that would mean it was committed. */
const PROPOSAL_TOOL = "submit_proposal";
const DIRECT_COMMIT_TOOLS = ["update_opportunity", "create_opportunity", "delete_opportunity"];

/** A stage the board's leftmost column holds, and the one to its right. Mirrors `OPPORTUNITY_STAGES`. */
const FROM_STAGE = "Discovery";
const TO_STAGE = "Qualification";

/** A stream can take a while; a turn involves several model round trips. */
const TURN_TIMEOUT = 60_000;

/** The card's visible name: the text its stretched link is labelled by. */
async function cardNameOf(card: Locator): Promise<string> {
  return (
    await card
      .getByRole("link")
      .evaluate((link) => document.getElementById(link.getAttribute("aria-labelledby") ?? "")?.textContent ?? "")
  ).trim();
}

/** Register the proposal `card` shows for teardown; its heading id carries the proposal id. */
async function registerProposalCard(card: Locator, writes: WriteLedger): Promise<void> {
  const labelledBy = (await card.getAttribute("aria-labelledby")) ?? "";
  const proposalId = labelledBy.replace(/^proposal-heading-/, "");
  expect(proposalId, "the proposal card must name the proposal it shows").not.toMatch(/^(unknown)?$/);
  writes.proposal(proposalId);
}

/** Opens the assistant panel if it is not already open, and leaves it open. */
async function openPanel(page: Page): Promise<void> {
  const panel = page.getByTestId("assistant-panel");
  if (await panel.isHidden()) {
    await page.getByTestId("panel-toggle").click();
  }
  await expect(panel).toBeVisible();
}

/**
 * The record id of the first card in the `FROM_STAGE` column; fails when the column is empty.
 */
async function firstOpportunityId(page: Page): Promise<string> {
  const column = page.getByTestId(`pipeline-column-${FROM_STAGE}`);
  await expect(column).toBeVisible();
  // A card already awaiting approval is locked; request the change for one that is free to move.
  const cards = column
    .locator("[data-opportunity-id]")
    .filter({ hasNot: page.getByTestId("pending-badge") });
  expect(
    await cards.count(),
    `no ${FROM_STAGE} record in this tenant's book, so there is no high-risk change to request. ` +
      "Seed the tenant (scripts/seed_demo_tenant.py) — this journey must not be skipped into a pass.",
  ).toBeGreaterThan(0);

  const id = await cards.first().getAttribute("data-opportunity-id");
  expect(id, "the board must expose the record id it is acting on").toBeTruthy();
  return id ?? "";
}

test("a high-risk request from the panel yields a proposal card and commits nothing", async ({
  signedInPage: page,
  writes,
}) => {
  await page.goto("/pipeline");
  const opportunityId = await firstOpportunityId(page);
  // The record's own name, not the column's text: a pending proposal adds a badge to the card.
  const boardCard = page.locator(`[data-opportunity-id="${opportunityId}"]`);
  const cardName = await cardNameOf(boardCard);
  expect(cardName, "the card must name the record it shows").toBeTruthy();

  await openPanel(page);

  // The prompt names the record and the target stage explicitly.
  await page
    .getByTestId("assistant-prompt")
    .fill(`Move opportunity ${opportunityId} from ${FROM_STAGE} to ${TO_STAGE}.`);
  await page.getByTestId("assistant-send").click();

  // ---- the change arrives as a proposal awaiting a person ----
  const card = page.getByTestId("proposal-card");
  await expect(card, "a high-risk change must surface as a proposal card").toBeVisible({
    timeout: TURN_TIMEOUT,
  });
  await registerProposalCard(card, writes);
  await expect(card).toContainText(/approval needed/i);
  await expect(card.getByTestId("proposal-target")).toContainText(opportunityId);
  // Both approve and reject are offered.
  await expect(card.getByTestId("proposal-approve")).toBeVisible();
  await expect(card.getByTestId("proposal-reject")).toBeVisible();

  // ---- negative half 1: the transcript names the proposal tool and no direct-commit tool ----
  // Each tool call is a `trace-step` naming its tool in `data-tool`; the steps fold away once the
  // turn completes, so they are read from the DOM rather than required to be visible. Read them
  // once the turn has finished, so a commit issued after the proposal would be seen.
  await expect(page.getByTestId("trace-steps").last()).toContainText(/Worked through/, {
    timeout: TURN_TIMEOUT,
  });
  const steps = page.getByTestId("trace-step");
  const toolNames = await steps.evaluateAll((rows) => rows.map((row) => row.getAttribute("data-tool") ?? ""));
  expect(
    toolNames.filter((name) => name === PROPOSAL_TOOL),
    `the high-risk change must travel through ${PROPOSAL_TOOL}; tool calls seen: ${toolNames.join(" | ")}`,
  ).not.toEqual([]);

  for (const committed of DIRECT_COMMIT_TOOLS) {
    expect(
      toolNames.filter((text) => text.includes(committed)),
      `a high-risk stage change must never call ${committed}; tool calls seen: ${toolNames.join(" | ")}`,
    ).toEqual([]);
  }

  // ---- negative half 2: the board shows the record unmoved, and locked pending approval ----
  await page.goto("/pipeline");
  await expect(page.getByTestId(`pipeline-column-${FROM_STAGE}`)).toContainText(cardName);
  const lockedCard = page.locator(`[data-opportunity-id="${opportunityId}"]`);
  await expect(lockedCard).toBeVisible();
  // Locked against both move paths: the keyboard handle is disabled and the card is not draggable.
  await expect(lockedCard.getByTestId("opportunity-move-handle")).toBeDisabled();
  await expect(lockedCard).toHaveAttribute("draggable", "false");
  await expect(lockedCard.getByTestId("pending-badge")).toContainText(TO_STAGE);

  // ---- negative half 3: nothing was applied, so the audit trail holds no change for it ----
  // Scoped to `main` because the header nav renders its links as `<li>`.
  await page.goto("/audit");
  const appliedRows = page
    .getByRole("main")
    .getByRole("listitem")
    .filter({ hasText: opportunityId });
  expect(
    await appliedRows.filter({ hasText: TO_STAGE }).count(),
    "an unapproved proposal must leave no applied record in the audit trail",
  ).toBe(0);
});

test("the proposal the panel produced is the one waiting in the approvals queue", async ({
  signedInPage: page,
  writes,
}) => {
  // A proposal raised through the assistant appears in the approvals queue.
  await page.goto("/pipeline");
  const opportunityId = await firstOpportunityId(page);

  await openPanel(page);
  await page
    .getByTestId("assistant-prompt")
    .fill(`Move opportunity ${opportunityId} from ${FROM_STAGE} to ${TO_STAGE}.`);
  await page.getByTestId("assistant-send").click();
  await expect(page.getByTestId("proposal-card")).toBeVisible({ timeout: TURN_TIMEOUT });
  await registerProposalCard(page.getByTestId("proposal-card"), writes);

  await page.goto("/approvals");
  const row = page.getByRole("main").getByRole("listitem").filter({ hasText: opportunityId });
  await expect(row.first()).toBeVisible({ timeout: 30_000 });
  // Still pending, and described as such.
  await expect(row.first()).toContainText(/proposed/i);
});
