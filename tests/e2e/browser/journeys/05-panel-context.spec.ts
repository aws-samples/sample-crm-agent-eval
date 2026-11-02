// SPDX-License-Identifier: MIT
/**
 * Journey 5 — each turn's `page_context` describes the view the user is looking at (route and filter,
 * never rows) and follows navigation; when no disclosure renders, the view's row count is unchanged,
 * and a rendered disclosure names the view's count or explains why no comparison was possible.
 */
import type { Page, Request } from "@playwright/test";

import { expect, test } from "../fixtures/auth";

/** Matches the exported test ids in `frontend/src/components/AssistantPanel/AssistantConversation.tsx`. */
const PROMPT_TESTID = "assistant-prompt";
const SEND_TESTID = "assistant-send";
const DISCLOSURE_TESTID = "assistant-disclosure";

/** The stage this journey filters the list down to, so the descriptor has something to carry. */
const FILTER_STAGE = "Qualification";

/** Opens the panel if it is closed. Panel state persists, so a plain click would sometimes close it. */
async function openPanel(page: Page): Promise<void> {
  const panel = page.getByTestId("assistant-panel");
  if (await panel.isHidden()) {
    await page.getByTestId("panel-toggle").click();
  }
  await expect(panel).toBeVisible();
}

/** Sends one turn and returns the request that carried it, so its body can be inspected. */
async function sendTurn(page: Page, prompt: string): Promise<Request> {
  const pending = page.waitForRequest(
    (request) =>
      request.method() === "POST" && new URL(request.url()).pathname.startsWith("/agent/"),
  );
  await page.getByTestId(PROMPT_TESTID).fill(prompt);
  await page.getByTestId(SEND_TESTID).click();
  return pending;
}

/** The `page_context` object a turn carried, as a plain record. */
function pageContextOf(request: Request): Record<string, unknown> {
  const body = JSON.parse(request.postData() ?? "{}") as Record<string, unknown>;
  const context = body["page_context"];
  expect(context, "the turn carried no page_context, so the panel cannot know what is on screen").toBeTruthy();
  return context as Record<string, unknown>;
}

/**
 * How many *data* rows the opportunities list is showing; scoped to `tbody` to exclude the header row.
 */
async function visibleRowCount(page: Page): Promise<number> {
  return page.getByRole("main").locator("tbody tr").count();
}

test("the turn carries the view the user configured, and carries no rows", async ({
  signedInPage: page,
}) => {
  await page.goto("/opportunities");

  const search = page.getByRole("search", { name: "Filter opportunities" });
  await expect(search).toBeVisible();

  // Precondition: a non-empty opportunity list.
  await expect(
    page.getByText("No opportunities", { exact: false }),
    "the opportunity list is empty, so there is no view to summarise. Seed the demo tenant with " +
      "`scripts/seed_demo_tenant.py` before running G10 — this journey asserts rather than skips.",
  ).toHaveCount(0);

  await search.getByLabel("Stage").selectOption(FILTER_STAGE);
  await search.getByRole("button", { name: /search/i }).click();

  await openPanel(page);
  const request = await sendTurn(page, "summarise what I am looking at");
  const context = pageContextOf(request);

  // The descriptor names this route and this filter.
  expect(JSON.stringify(context)).toContain(FILTER_STAGE);
  expect(String(context["route"] ?? "")).toContain("/opportunities");

  // Rows never travel: no array of objects anywhere in the descriptor.
  const arraysOfObjects = JSON.stringify(context).match(/\[\s*\{/g) ?? [];
  expect(
    arraysOfObjects,
    `page_context carries an array of objects, which is how rows would travel: ${JSON.stringify(context)}`,
  ).toHaveLength(0);
});

test("the descriptor follows the user to a different view rather than repeating the first one", async ({
  signedInPage: page,
}) => {
  await page.goto("/opportunities");
  await openPanel(page);

  const first = pageContextOf(await sendTurn(page, "summarise this list"));
  expect(String(first["route"] ?? "")).toContain("/opportunities");

  // Navigate with the panel open; the panel survives and the next turn describes the new view.
  await page.goto("/pipeline");
  await expect(page.getByTestId("assistant-panel")).toBeVisible();

  const second = pageContextOf(await sendTurn(page, "and now summarise this one"));
  expect(String(second["route"] ?? "")).toContain("/pipeline");
  expect(
    JSON.stringify(second),
    "the second turn repeated the first view's descriptor, so the panel is not tracking the view",
  ).not.toBe(JSON.stringify(first));
});

test("a scope difference is disclosed, and silence is only permitted when the counts matched", async ({
  signedInPage: page,
}) => {
  await page.goto("/opportunities");

  // The list renders a skeleton until its mount search resolves; count only once a data row exists.
  await expect(
    page.getByRole("main").locator("tbody tr").first(),
    "the opportunity list rendered no rows, so the row-count comparison has nothing to compare. Seed the demo tenant " +
      "with `scripts/seed_demo_tenant.py`; this journey asserts rather than skips.",
  ).toBeVisible();
  const rowsBefore = await visibleRowCount(page);
  expect(
    rowsBefore,
    "the opportunity list rendered no rows, so the row-count comparison has nothing to compare. Seed the demo tenant " +
      "with `scripts/seed_demo_tenant.py`; this journey asserts rather than skips.",
  ).toBeGreaterThan(0);

  await openPanel(page);
  await sendTurn(page, "how many of these are worth chasing");

  // The disclosure accompanies an answer, so wait for the turn to finish.
  await expect(page.getByTestId("event-trace")).toBeVisible({ timeout: 60_000 });

  const disclosure = page.getByTestId(DISCLOSURE_TESTID);
  const disclosed = await disclosure.count();

  if (disclosed === 0) {
    // Silence is permitted only when the counts were compared and matched.
    expect(
      await visibleRowCount(page),
      "the panel disclosed nothing, which is only correct when its own count matched this view's. If " +
        "the count changed under it, the comparison it stayed silent about was not the one it made.",
    ).toBe(rowsBefore);
    return;
  }

  // A rendered disclosure is a status announcement, not an alert.
  await expect(disclosure).toBeVisible();
  await expect(disclosure).toHaveAttribute("role", "status");

  const text = (await disclosure.innerText()).trim();
  expect(text.length, "an empty disclosure discloses nothing").toBeGreaterThan(0);

  // It names the view's count, or (`not-comparable` / `opaque`) explains why no comparison was made.
  const namesTheViewsCount = new RegExp(`\\b${rowsBefore}\\b`).test(text);
  const explainsWhyNotComparable =
    /can.t be summarised|may cover a different set|lower bound|could not be read/i.test(text);
  expect(
    namesTheViewsCount || explainsWhyNotComparable,
    `the disclosure neither names this view's count (${rowsBefore}) nor explains why no comparison ` +
      `could be made. Text was: ${text}`,
  ).toBe(true);
});
