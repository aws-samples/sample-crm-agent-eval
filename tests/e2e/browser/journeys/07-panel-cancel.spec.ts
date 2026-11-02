// SPDX-License-Identifier: MIT
/**
 * Journey 7 — pressing Stop ends a running turn with a `[cancelled]`-prefixed completion, and a write
 * whose result frame arrived before Stop stays committed and audited, with no transcript claim that it
 * was undone.
 */
import type { Page } from "@playwright/test";

import { expect, test } from "../fixtures/auth";

/**
 * Copy of `CANCELLATION_TEXT_PREFIX` in `src/agents/supervisor.py`; this package cannot import
 * application code. `tests/unit/test_cancellation_token_parity.py` checks the two agree.
 */
const CANCELLATION_PREFIX = "[cancelled]";

/** The low-risk write this journey commits before cancelling. */
const WRITE_TOOL = "log_activity";

/** Phrases that would mean the transcript is telling the user a committed write was prevented. */
const PREVENTED_CLAIMS =
  /rolled back|reverted|undone|not saved|nothing was (changed|committed)|prevented/i;

const TURN_TIMEOUT = 60_000;

/** The newest turn ended cancelled: its state pill says so and the transcript carries the prefix. */
async function expectTurnCancelled(page: Page): Promise<void> {
  await expect(page.getByTestId("turn-state-pill").last()).toHaveAttribute("data-status", "cancelled", {
    timeout: TURN_TIMEOUT,
  });
  await expect(page.getByTestId("assistant-transcript")).toContainText(CANCELLATION_PREFIX);
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
 * The first record id on the pipeline board; fails when the board is empty.
 */
async function anyOpportunityId(page: Page): Promise<string> {
  await page.goto("/pipeline");
  const cards = page.getByRole("main").locator("[data-opportunity-id]");
  await expect(
    cards.first(),
    "no opportunity in this tenant's book, so there is no write to log and nothing to cancel. " +
      "Seed the tenant (scripts/seed_demo_tenant.py) — this journey must not be skipped into a pass.",
  ).toBeVisible({ timeout: 30_000 });

  const id = await cards.first().getAttribute("data-opportunity-id");
  expect(id, "the board must expose the record id it is acting on").toBeTruthy();
  return id ?? "";
}

test("pressing Stop mid-turn ends the stream and the transcript says the turn was cancelled", async ({
  signedInPage: page,
}) => {
  await page.goto("/pipeline");
  await openPanel(page);

  // A question with several steps in it, so there is a running turn to interrupt.
  await page
    .getByTestId("assistant-prompt")
    .fill("Review every stage of the pipeline and tell me what stands out in each one.");
  await page.getByTestId("assistant-send").click();

  // Stop is offered only while a turn is running.
  const stop = page.getByTestId("assistant-stop");
  await expect(stop, "Stop must be offered while a turn is running").toBeVisible({
    timeout: TURN_TIMEOUT,
  });

  await stop.click();

  // The cancel is acknowledged with any of its three dispositions.
  await expect(page.getByTestId("assistant-cancel-status")).toContainText(
    /stopping after the current step|already stopping|had already finished/i,
    { timeout: 30_000 },
  );

  // The turn terminates: the transcript carries the cancellation terminal event, and the composer is
  // usable again.
  await expectTurnCancelled(page);
  await expect(stop).toHaveCount(0);
  await expect(page.getByTestId("assistant-prompt")).toBeEnabled();
});

test("a write that had already completed when Stop was pressed stays committed and audited", async ({
  signedInPage: page,
}) => {
  const opportunityId = await anyOpportunityId(page);
  const marker = `journey-07 cancel-after-write ${Date.now()}`;

  await openPanel(page);
  await page
    .getByTestId("assistant-prompt")
    .fill(
      `Log a call activity on opportunity ${opportunityId} with the subject exactly "${marker}", ` +
        "then review every stage of the pipeline and tell me what stands out in each one.",
    );
  await page.getByTestId("assistant-send").click();

  // Wait until the turn has moved past the write: a later step means its result frame has arrived.
  const afterWrite = page.locator(
    `[data-testid="trace-step"][data-tool="${WRITE_TOOL}"] ~ [data-testid="trace-step"]`,
  );
  await expect(afterWrite.first(), `the turn must perform the ${WRITE_TOOL} write and go on`).toBeVisible({
    timeout: TURN_TIMEOUT,
  });

  const stop = page.getByTestId("assistant-stop");
  await expect(stop, "the turn must still be running after the write, so there is a turn to cancel")
    .toBeVisible();
  await stop.click();

  await expectTurnCancelled(page);

  /*
   * ---- the transcript does not claim the write was prevented ----
   *
   * Read before navigating: the transcript lives in React state and the navigations below destroy it.
   */
  const transcript = await page.getByTestId("assistant-transcript").innerText();
  expect(
    PREVENTED_CLAIMS.test(transcript),
    `the transcript must not tell the user a committed write was undone; transcript was:\n${transcript}`,
  ).toBe(false);
  // "cancelled" describes the turn, not the write.
  expect(transcript).toContain(CANCELLATION_PREFIX);

  // ---- the write stands ----
  // Scoped to `main` because the header nav renders its links as `<li>`.
  await page.goto(`/opportunities/${opportunityId}/activities`);
  const activity = page.getByRole("main").locator("li[data-activity-id]").filter({ hasText: marker });
  await expect(
    activity.first(),
    "a write that completed before the cancel must still be committed",
  ).toBeVisible({ timeout: 30_000 });
  const activityId = (await activity.first().getAttribute("data-activity-id")) ?? "";
  expect(activityId, "the committed activity must expose its id").toBeTruthy();

  // ---- the write is audited ----
  // An audit card names its target record; its values are on the detail page.
  await page.goto("/audit");
  await expect(
    page.getByRole("main").getByRole("listitem").filter({ hasText: activityId }).first(),
    "a write that completed before the cancel must be in the audit trail",
  ).toBeVisible({ timeout: 30_000 });
});
