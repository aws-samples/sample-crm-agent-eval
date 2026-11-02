// SPDX-License-Identifier: MIT
/**
 * Journey 15: an admin sees the live-sweep estimate before anything is submitted, with
 * the effort caveat exactly when the server's estimate carries one, and a confirmed live sweep runs to `completed` with per-tier reports; a non-admin sees no live control and
 * is refused 403 by the API; a second sweep while one runs is refused 409, and a new sweep can start
 * once the first is terminal. The dialog offers Repeats from 1 to the server's own bound, defaulting
 * to 1; the confirmed live sweep (1 repeat) scores the multi-step write task on every arm, read from
 * the run's raw artefacts, with no `AccessDenied` in any of its assertion details.
 *
 * Users:
 * - the demo user (`CRM_DEMO_USER_EMAIL`), who must NOT be in the `admin` group;
 * - an admin (`CRM_E2E_ADMIN_EMAIL`), in `admin` and with full visibility, so it may start stub sweeps.
 *
 * Billable: the live run starts only when `CRM_E2E_ALLOW_LIVE_SWEEP=1`; otherwise that test is
 * skipped with that reason. Every other test here uses stub sweeps or starts nothing.
 */
import type { Page } from "@playwright/test";

import { expect, signInAs, test } from "../../fixtures/auth";
import { field, postAsSignedInUser } from "../../fixtures/pageApi";

const LIVE_OPT_IN = "CRM_E2E_ALLOW_LIVE_SWEEP";
/** A stub sweep's 40 items run sequentially; a live one takes about 15 minutes. */
const STUB_SWEEP_TIMEOUT_MS = 20 * 60_000;
const LIVE_SWEEP_TIMEOUT_MS = 60 * 60_000;
/** Text every estimate effort caveat contains (`eval.runs.ESTIMATE_EFFORT_CAVEAT`). */
const EFFORT_CAVEAT_TEXT = /measured at supervisor effort/;
/** The golden task whose two turn-created rows are scored without a `GetItem`. */
const MULTI_STEP_WRITE_TASK = /^gt-24-/;

interface RawTaskScore {
  readonly task_id: string;
  readonly correctness: number | null;
  readonly unscored_reason: string | null;
  readonly assertion_results: ReadonlyArray<{ readonly detail: string }>;
}

async function openEvals(page: Page): Promise<void> {
  await page.goto("/evals");
  await expect(page.getByRole("heading", { name: "Model evaluation dashboard" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Run history" })).toBeVisible();
}

/** The run id the progress line names, once a sweep is focused. */
async function focusedRunId(page: Page): Promise<string> {
  const progress = page.getByRole("status").filter({ hasText: /run_id=/ });
  await expect(progress).toBeVisible({ timeout: 60_000 });
  const match = ((await progress.textContent()) ?? "").match(/run_id=([^)\s]+)\)/);
  expect(match, "the progress line names no run_id").not.toBeNull();
  return match?.[1] ?? "";
}

/** One arm's raw artefact of `runId`, fetched through a presigned `artefact_url`. */
async function rawArtefact(page: Page, runId: string, key: string): Promise<unknown> {
  const presigned = await postAsSignedInUser(page, "eval_trigger", {
    action: "artefact_url",
    run_id: runId,
    key,
  });
  expect(presigned.status, `artefact_url for ${key}`).toBe(200);
  const response = await page.context().request.get(String(field(presigned.body, "url")));
  expect(response.status(), `GET of the presigned ${key}`).toBe(200);
  return response.json();
}

/** Wait for the page to show the focused run as finished, with no reload. */
async function waitForTerminal(page: Page, timeout: number): Promise<string> {
  const terminal = page
    .getByText(/^Run complete\.$|^Cancelled after \d+\/\d+ tasks|^Run failed:/)
    .first();
  await expect(terminal).toBeVisible({ timeout });
  return (await terminal.textContent()) ?? "";
}

test("a non-admin sees no live control and the API refuses a live submit with 403", async ({
  signedInPage: page,
}) => {
  await openEvals(page);
  await expect(page.getByRole("button", { name: /run stub sweep/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /live/i })).toHaveCount(0);
  await expect(page.getByText(/admins only/i)).toHaveCount(0);

  const reply = await postAsSignedInUser(page, "eval_trigger", {
    action: "submit",
    mode: "live",
    confirmed: false,
  });
  expect(reply.status).toBe(403);
  expect(field(reply.body, "error", "code")).toBe("scope_denied");
  expect(field(reply.body, "error", "missing_scope")).toBe("eval:admin");
});

test("an admin sees the estimate, with its basis and size, before anything is submitted", async ({
  page,
}) => {
  await signInAs(page, "admin");
  await openEvals(page);

  await page.getByRole("button", { name: /run live sweep/i }).click();
  const dialog = page.getByRole("dialog", { name: /start a live sweep/i });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("Estimated spend")).toBeVisible({ timeout: 30_000 });
  await expect(dialog.getByText(/\d+ tasks on each of \d+ arms? \(\d+ agent runs\)/)).toBeVisible();
  await expect(dialog.getByText(/Based on /)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Start live sweep" })).toBeEnabled();

  // The caveat is shown exactly when the server's estimate carries one.
  const estimate = await postAsSignedInUser(page, "eval_trigger", { action: "estimate" });
  expect(estimate.status).toBe(200);
  const caveat = field(estimate.body, "effort_caveat");
  if (typeof caveat === "string") {
    await expect(dialog.getByText(caveat, { exact: true })).toBeVisible();
  } else {
    expect(caveat, "effort_caveat is either a string or absent").toBeUndefined();
    await expect(dialog.getByText(EFFORT_CAVEAT_TEXT)).toHaveCount(0);
  }

  // Closing submits nothing.
  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText(/^No run is in progress\./)).toBeVisible();
});

test("the live dialog offers Repeats from 1 to the server's bound, defaulting to 1", async ({ page }) => {
  await signInAs(page, "admin");
  await openEvals(page);

  await page.getByRole("button", { name: /run live sweep/i }).click();
  const dialog = page.getByRole("dialog", { name: /start a live sweep/i });
  const repeats = dialog.getByRole("combobox", { name: "Repeats" });
  await expect(repeats).toBeVisible({ timeout: 30_000 });
  await expect(repeats).toHaveValue("1");
  const offered = (await repeats.locator("option").allTextContents()).map((text) => text.trim());
  const highest = offered.length;
  expect(highest, "the dialog offers more than one repeat count").toBeGreaterThan(1);
  expect(offered).toEqual(Array.from({ length: highest }, (_, index) => String(index + 1)));

  const accepted = await postAsSignedInUser(page, "eval_trigger", {
    action: "estimate",
    repeats: highest,
  });
  expect(accepted.status, `the server accepts the dialog's highest repeat count ${highest}`).toBe(200);
  expect(field(accepted.body, "repeats")).toBe(highest);
  const refused = await postAsSignedInUser(page, "eval_trigger", {
    action: "estimate",
    repeats: highest + 1,
  });
  expect(refused.status, `the server refuses one more than the dialog offers`).toBe(400);
  expect(field(refused.body, "error", "code")).toBe("validation_failed");
  expect(field(refused.body, "error", "source")).toBe("repeats");

  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toBeHidden();
});

test("a second sweep while one runs is refused 409, and a new one starts after it ends", async ({
  page,
}) => {
  test.setTimeout(STUB_SWEEP_TIMEOUT_MS * 2);
  await signInAs(page, "admin");
  await openEvals(page);

  await page.getByRole("button", { name: /run stub sweep/i }).click();
  const first = await focusedRunId(page);
  // While it runs, the page offers no second start ...
  await expect(page.getByRole("button", { name: /run stub sweep/i })).toBeDisabled();
  // ... and the server refuses one.
  const refused = await postAsSignedInUser(page, "eval_trigger", { action: "submit", mode: "stub" });
  expect(refused.status, "a second submit while the first sweep runs must be refused").toBe(409);
  expect(field(refused.body, "error", "code")).toBe("conflict");

  await waitForTerminal(page, STUB_SWEEP_TIMEOUT_MS);
  const start = page.getByRole("button", { name: /run stub sweep/i });
  await expect(start).toBeEnabled();
  await start.click();
  const second = await focusedRunId(page);
  expect(second).not.toBe(first);
  await waitForTerminal(page, STUB_SWEEP_TIMEOUT_MS);
});

test("a confirmed live sweep runs to completed with a per-tier report for each arm", async ({
  page,
}) => {
  test.skip(
    process.env[LIVE_OPT_IN] !== "1",
    `billable: set ${LIVE_OPT_IN}=1 to start a live sweep against real Bedrock models`,
  );
  test.setTimeout(LIVE_SWEEP_TIMEOUT_MS);
  await signInAs(page, "admin");
  await openEvals(page);

  await page.getByRole("button", { name: /run live sweep/i }).click();
  const dialog = page.getByRole("dialog", { name: /start a live sweep/i });
  const confirm = dialog.getByRole("button", { name: "Start live sweep" });
  await expect(confirm).toBeEnabled({ timeout: 30_000 });
  await expect(dialog.getByRole("combobox", { name: "Repeats" })).toHaveValue("1");
  await confirm.click();
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("status").filter({ hasText: /live sweep \(run_id=/ })).toBeVisible();
  const runId = await focusedRunId(page);

  expect(await waitForTerminal(page, LIVE_SWEEP_TIMEOUT_MS)).toBe("Run complete.");
  await expect(page.getByRole("heading", { name: "Results" })).toBeVisible();
  const summaries = page.getByRole("table", { name: /^Per-tier summary for / });
  expect(await summaries.count(), "expected one per-tier summary per arm").toBeGreaterThanOrEqual(2);
  for (const summary of await summaries.all()) {
    await expect(summary.getByRole("rowheader")).toHaveText(["Supervisor", "Subagent", "Tool"]);
  }
  // A live run's spend is metered, never the stub wording.
  const spend = page.getByRole("main").locator("dt", { hasText: /^Spend$/ }).locator("xpath=following-sibling::dd[1]");
  await expect(spend).not.toHaveText(/no inference cost/);

  const status = await postAsSignedInUser(page, "eval_trigger", { action: "status", run_id: runId });
  expect(status.status).toBe(200);
  expect(field(status.body, "repeats")).toBe(1);
  const rawKeys = field(status.body, "artifacts", "raw");
  expect(Array.isArray(rawKeys), "the completed run records its raw artefacts").toBe(true);
  const keys = (rawKeys as unknown[]).map(String);
  expect(keys.length, "expected one raw artefact per arm").toBe(await summaries.count());
  for (const key of keys) {
    const scores = field(await rawArtefact(page, runId, key), "task_scores") as RawTaskScore[];
    const multiStep = scores.filter((score) => MULTI_STEP_WRITE_TASK.test(score.task_id));
    expect(multiStep.length, `${key}: the multi-step write task ran to done exactly once`).toBe(1);
    const [score] = multiStep;
    expect(score.unscored_reason, `${key}: ${score.task_id} is scored`).toBeNull();
    expect(score.correctness, `${key}: ${score.task_id} carries a correctness`).not.toBeNull();
    for (const result of score.assertion_results) {
      expect(result.detail ?? "", `${key}: ${score.task_id} assertion detail`).not.toMatch(/AccessDenied/);
    }
  }
});
