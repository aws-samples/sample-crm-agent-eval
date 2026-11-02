// SPDX-License-Identifier: MIT
/**
 * Journey 16: leaving the Evals page and coming back shows the server's state of a sweep
 * (still pinned while it runs, its result once terminal); history lists runs newest first with the
 * starter; a cancel ends the sweep `cancelled` with N/total tasks and its spend, and a repeated or
 * terminal cancel is a clear no-op. A stub sweep at 2 repeats sizes itself as tasks × arms × repeats,
 * and a cancel ends it `cancelled` with every item it did not run recorded skipped.
 *
 * Users: an admin (`CRM_E2E_ADMIN_EMAIL`), in `admin` and with full visibility. The resume and
 * no-op-cancel tests use stub sweeps. The UI cancel of a live sweep is billable and runs only when
 * `CRM_E2E_ALLOW_LIVE_SWEEP_CANCEL=1` (its own opt-in; each opt-in starts at most one live sweep).
 */
import type { Page } from "@playwright/test";

import { expect, signInAs, test } from "../../fixtures/auth";
import { field, postAsSignedInUser } from "../../fixtures/pageApi";

const LIVE_OPT_IN = "CRM_E2E_ALLOW_LIVE_SWEEP_CANCEL";
const SWEEP_TIMEOUT_MS = 30 * 60_000;
const NO_OP_DISPOSITIONS = ["already_requested", "already_terminal"];
const REPEATS = 2;

async function openEvals(page: Page): Promise<void> {
  await page.goto("/evals");
  await expect(page.getByRole("heading", { name: "Run history" })).toBeVisible();
}

async function focusedRunId(page: Page): Promise<string> {
  const progress = page.getByRole("status").filter({ hasText: /run_id=/ });
  await expect(progress).toBeVisible({ timeout: 60_000 });
  const match = ((await progress.textContent()) ?? "").match(/run_id=([^)\s]+)\)/);
  expect(match, "the progress line names no run_id").not.toBeNull();
  return match?.[1] ?? "";
}

function numberAt(body: unknown, ...path: string[]): number {
  const value = field(body, ...path);
  expect(typeof value, `${path.join(".")} is a number`).toBe("number");
  return value as number;
}

function historyRow(page: Page, runId: string) {
  return page
    .getByRole("table", { name: "Evaluation sweeps, newest first" })
    .getByRole("button", { name: `Show run ${runId}` })
    .locator("xpath=ancestor::tr[1]");
}

test("leaving and returning shows the server's state of a sweep, and history names its starter", async ({
  page,
}) => {
  test.setTimeout(SWEEP_TIMEOUT_MS);
  await signInAs(page, "admin");
  await openEvals(page);

  await page.getByRole("button", { name: /run stub sweep/i }).click();
  const runId = await focusedRunId(page);

  // Away and back: the page resumes the same run from the server, with no click.
  await page.goto("/pipeline");
  await openEvals(page);
  const resumed = page
    .getByRole("status")
    .filter({ hasText: new RegExp(`run_id=${runId}\\)`) })
    .or(page.getByText(/^Run complete\.$/));
  await expect(resumed.first()).toBeVisible({ timeout: 60_000 });

  // Newest first, with the starter.
  const table = page.getByRole("table", { name: "Evaluation sweeps, newest first" });
  await expect(table.getByRole("button", { name: /^Show run / }).first()).toHaveAccessibleName(
    `Show run ${runId}`,
  );
  const row = historyRow(page, runId);
  await expect(row).not.toContainText("not reported");

  // Terminal state also survives the round trip.
  await expect(page.getByText(/^Run complete\.$|^Run failed:/).first()).toBeVisible({
    timeout: SWEEP_TIMEOUT_MS,
  });
  await page.goto("/pipeline");
  await openEvals(page);
  await row.getByRole("button", { name: `Show run ${runId}` }).click();
  await expect(page.getByText(/^Run complete\.$|^Run failed:/).first()).toBeVisible();
});

test("a cancel is accepted once, and a repeated or terminal cancel is a clear no-op", async ({
  page,
}) => {
  test.setTimeout(SWEEP_TIMEOUT_MS);
  await signInAs(page, "admin");
  await openEvals(page);

  await page.getByRole("button", { name: /run stub sweep/i }).click();
  const runId = await focusedRunId(page);

  const first = await postAsSignedInUser(page, "eval_trigger", { action: "cancel", run_id: runId });
  expect(first.status).toBe(200);
  expect(field(first.body, "disposition"), "the first cancel of a running sweep must be accepted").toBe(
    "accepted",
  );

  const repeat = await postAsSignedInUser(page, "eval_trigger", { action: "cancel", run_id: runId });
  expect(repeat.status).toBe(200);
  expect(NO_OP_DISPOSITIONS).toContain(String(field(repeat.body, "disposition")));
  expect(field(repeat.body, "run", "cancel_requested")).toBe(true);

  // The page shows the run's end state as the server reports it.
  await openEvals(page);
  await historyRow(page, runId).getByRole("button", { name: `Show run ${runId}` }).click();
  const end = page.getByText(/^Cancelled after \d+\/\d+ tasks/).first();
  await expect(end).toBeVisible({ timeout: SWEEP_TIMEOUT_MS });

  // Once terminal, a further cancel is still a no-op.
  const terminal = await postAsSignedInUser(page, "eval_trigger", { action: "cancel", run_id: runId });
  expect(field(terminal.body, "disposition")).toBe("already_terminal");
});

test("a cancelled stub sweep at 2 repeats records every item it did not run as skipped", async ({
  page,
}) => {
  test.setTimeout(SWEEP_TIMEOUT_MS);
  await signInAs(page, "admin");
  await openEvals(page);

  const estimate = await postAsSignedInUser(page, "eval_trigger", { action: "estimate", repeats: REPEATS });
  expect(estimate.status).toBe(200);
  const arms = Object.keys((field(estimate.body, "per_arm_usd") ?? {}) as Record<string, unknown>).length;
  expect(arms, "the estimate names at least one arm").toBeGreaterThan(0);
  const expectedTotal = numberAt(estimate.body, "task_count") * arms * REPEATS;

  const submitted = await postAsSignedInUser(page, "eval_trigger", {
    action: "submit",
    mode: "stub",
    repeats: REPEATS,
  });
  expect(submitted.status).toBe(200);
  const runId = String(field(submitted.body, "run_id"));
  expect(field(submitted.body, "repeats")).toBe(REPEATS);
  expect(numberAt(submitted.body, "tasks_total"), "tasks × arms × repeats").toBe(expectedTotal);

  const cancel = await postAsSignedInUser(page, "eval_trigger", { action: "cancel", run_id: runId });
  expect(cancel.status).toBe(200);
  expect(field(cancel.body, "disposition")).toBe("accepted");

  let run: unknown = null;
  await expect
    .poll(
      async () => {
        run = (await postAsSignedInUser(page, "eval_trigger", { action: "status", run_id: runId })).body;
        return field(run, "is_terminal");
      },
      { timeout: SWEEP_TIMEOUT_MS, intervals: [5_000] },
    )
    .toBe(true);
  expect(field(run, "status")).toBe("cancelled");
  expect(field(run, "repeats")).toBe(REPEATS);
  const total = numberAt(run, "tasks_total");
  const skipped = numberAt(run, "tasks_skipped");
  expect(total).toBe(expectedTotal);
  expect(skipped, "a cancel accepted before the sweep ran out skips at least one item").toBeGreaterThan(0);
  expect(
    numberAt(run, "tasks_done") + numberAt(run, "tasks_failed") + skipped,
    "every (arm, repeat, task) item is recorded done, failed or skipped",
  ).toBe(total);

  await openEvals(page);
  await historyRow(page, runId).getByRole("button", { name: `Show run ${runId}` }).click();
  await expect(
    page.getByText(new RegExp(`^Cancelled after \\d+/${total} tasks \\(${skipped} skipped\\)`)).first(),
  ).toBeVisible();
});

test("cancelling a running live sweep from the page ends it cancelled with N/total and its spend", async ({
  page,
}) => {
  test.skip(
    process.env[LIVE_OPT_IN] !== "1",
    `billable: set ${LIVE_OPT_IN}=1 to start (and then cancel) a live sweep`,
  );
  test.setTimeout(SWEEP_TIMEOUT_MS);
  await signInAs(page, "admin");
  await openEvals(page);

  await page.getByRole("button", { name: /run live sweep/i }).click();
  const dialog = page.getByRole("dialog", { name: /start a live sweep/i });
  const confirm = dialog.getByRole("button", { name: "Start live sweep" });
  await expect(confirm).toBeEnabled({ timeout: 30_000 });
  await confirm.click();
  const runId = await focusedRunId(page);

  // Let at least one task finish, so the partial report has something in it.
  await expect(page.getByRole("status").filter({ hasText: /run_id=.*\b[1-9]\d*\/\d+ tasks/ })).toBeVisible({
    timeout: SWEEP_TIMEOUT_MS,
  });
  await page.getByRole("button", { name: "Cancel sweep" }).click();
  await expect(page.getByText(/^Cancel requested\. The task in flight finishes/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel sweep" })).toHaveCount(0);

  const cancelled = page.getByText(/^Cancelled after \d+\/\d+ tasks.*Spend: /);
  await expect(cancelled).toBeVisible({ timeout: SWEEP_TIMEOUT_MS });
  await expect(cancelled).not.toContainText("no inference cost");
  await expect(page.getByRole("table", { name: /^Per-tier summary for / }).first()).toBeVisible();
  const teardown = page.getByRole("main").locator("dt", { hasText: /^Tenant teardown$/ }).locator("xpath=following-sibling::dd[1]");
  await expect(teardown).toHaveText("ok");

  // A cancel after the end is a no-op, and a new sweep may start (the lock was released).
  const again = await postAsSignedInUser(page, "eval_trigger", { action: "cancel", run_id: runId });
  expect(field(again.body, "disposition")).toBe("already_terminal");
  await expect(page.getByRole("button", { name: /run stub sweep/i })).toBeEnabled();
});
