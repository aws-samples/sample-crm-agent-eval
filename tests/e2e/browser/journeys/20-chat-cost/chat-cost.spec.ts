// SPDX-License-Identifier: MIT
/**
 * Journey 20: each assistant reply shows its cost with a per-tier breakdown, and the panel
 * header shows the conversation total; the "withheld" wording appears only while pricing is
 * provisional, and no figure appears while it is.
 *
 * Which branch applies is read from `config/pricing.yaml`'s `provisional:` line, the same file the
 * deployed stack prices from. This assumes the stack was deployed from this checkout's pricing file.
 *
 * User: the demo user (`CRM_DEMO_USER_EMAIL`). Each turn calls a real model and is billed as any
 * chat turn is.
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { Page } from "@playwright/test";

import { expect, test } from "../../fixtures/auth";

const PRICING_FILE = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../../config/pricing.yaml");

/** Mirrors `UsageMeter.COST_WITHHELD_TEXT` and `ConversationCostHeader.CONVERSATION_COST_WITHHELD_TEXT`. */
const TURN_WITHHELD_TEXT = "Cost is withheld while pricing is provisional.";
const CONVERSATION_WITHHELD_TEXT = "Cost withheld";
const CURRENCY = /\$\s?\d/;

function pricingIsProvisional(): boolean {
  const match = readFileSync(PRICING_FILE, "utf-8").match(/^provisional:\s*(true|false)\b/m);
  expect(match, `${PRICING_FILE} declares no top-level provisional: true|false`).not.toBeNull();
  return match?.[1] === "true";
}

async function sendTurn(page: Page, prompt: string): Promise<void> {
  const answers = page.getByTestId("markdown-answer");
  const before = await answers.count();
  await page.getByTestId("assistant-prompt").fill(prompt);
  await page.getByTestId("assistant-send").click();
  await expect(answers).toHaveCount(before + 1, { timeout: 90_000 });
  await expect(page.getByTestId("turn-state-pill").last()).toHaveAttribute("data-status", "completed");
}

/** A currency string as a number of dollars. */
function dollars(text: string): number {
  return Number(text.replace(/[^0-9.]/g, ""));
}

test("each reply shows its cost by tier, and the header totals the conversation", async ({
  signedInPage: page,
}) => {
  const provisional = pricingIsProvisional();
  await page.goto("/pipeline");
  const panel = page.getByTestId("assistant-panel");
  if (await panel.isHidden()) await page.getByTestId("panel-toggle").click();
  await expect(panel).toBeVisible();

  await sendTurn(page, "How many open opportunities do I have?");
  await sendTurn(page, "And which one closes soonest?");

  const meters = panel.getByTestId("usage-meter");
  await expect(meters).toHaveCount(2);
  const header = panel.getByTestId("conversation-cost");
  await expect(header).toBeVisible();

  if (provisional) {
    for (const meter of await meters.all()) {
      await expect(meter.getByTestId("usage-cost-withheld")).toHaveText(TURN_WITHHELD_TEXT);
      await expect(meter).not.toContainText(CURRENCY);
    }
    await expect(header).toHaveText(CONVERSATION_WITHHELD_TEXT);
    return;
  }

  // Not provisional: the withheld wording must be absent, and every figure present.
  await expect(panel.getByTestId("usage-cost-withheld")).toHaveCount(0);
  await expect(header).not.toHaveText(CONVERSATION_WITHHELD_TEXT);
  let sum = 0;
  for (const meter of await meters.all()) {
    const cost = meter.getByTestId("usage-cost");
    await expect(cost).toHaveText(CURRENCY);
    sum += dollars((await cost.textContent()) ?? "");

    const breakdown = meter.getByTestId("usage-cost-breakdown");
    await breakdown.locator("summary").click();
    const lines = breakdown.getByTestId("usage-cost-line");
    expect(await lines.count(), "a priced turn shows at least one per-tier cost line").toBeGreaterThan(0);
    for (const line of await lines.all()) {
      await expect(line).toContainText(/Supervisor|Subagent|Tool/);
      await expect(line).toContainText(CURRENCY);
    }
  }
  await expect(header).toHaveText(CURRENCY);
  expect(dollars((await header.textContent()) ?? "")).toBeCloseTo(sum, 4);
});
