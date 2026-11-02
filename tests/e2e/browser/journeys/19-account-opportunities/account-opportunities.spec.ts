// SPDX-License-Identifier: MIT
/**
 * Journey 19: an account's detail page lists that account's opportunities in the shared
 * opportunity table, and an account with none shows the empty state instead of an empty table.
 *
 * User: the demo user (`CRM_DEMO_USER_EMAIL`). The empty-state test creates its own account.
 */
import { expect, test } from "../../fixtures/teardown";

/** How many accounts to open, newest listing first, looking for one that has opportunities. */
const ACCOUNTS_TO_TRY = 10;

test("an account's detail page lists its opportunities in the shared table", async ({
  signedInPage: page,
}) => {
  await page.goto("/accounts");
  const accountLinks = page.getByRole("main").locator('a[href^="/accounts/"]:not([href="/accounts/new"])');
  await expect(
    accountLinks.first(),
    "the account list shows no account; seed with scripts/seed_demo_tenant.py",
  ).toBeVisible({ timeout: 30_000 });
  const hrefs = (await accountLinks.evaluateAll((nodes) => nodes.map((node) => node.getAttribute("href") ?? "")))
    .filter((href) => /^\/accounts\/[^/]+$/.test(href) && href !== "/accounts/new")
    .slice(0, ACCOUNTS_TO_TRY);
  expect(hrefs.length, "the account list shows no account; seed with scripts/seed_demo_tenant.py").toBeGreaterThan(0);

  let tableFound = false;
  for (const href of hrefs) {
    await page.goto(href);
    const heading = page.getByRole("heading", { name: "Opportunities", exact: true });
    await expect(heading).toBeVisible({ timeout: 30_000 });
    const table = page.getByRole("table", { name: /^Opportunities for / });
    if ((await table.count()) === 0) continue;
    tableFound = true;
    const accountName = (await page.getByRole("heading", { level: 1 }).textContent())?.trim() ?? "";
    await expect(table).toHaveAccessibleName(`Opportunities for ${accountName}`);
    await expect(table.getByRole("columnheader").first()).toBeVisible();
    expect(await table.getByRole("row").count(), "the table has a header but no opportunity row").toBeGreaterThan(1);
    await expect(page.getByText("No opportunities yet.")).toHaveCount(0);
    break;
  }
  expect(
    tableFound,
    `none of the first ${hrefs.length} accounts has an opportunity; seed with scripts/seed_demo_tenant.py`,
  ).toBe(true);
});

test("an account with no opportunities shows the empty state, not an empty table", async ({
  signedInPage: page,
  writes,
}) => {
  const name = writes.tag(`journey-19 empty account ${Date.now()}`);
  await page.goto("/accounts/new");
  await page.getByLabel("Name").fill(name);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible({ timeout: 30_000 });
  writes.created(decodeURIComponent(new URL(page.url()).pathname.split("/").pop() ?? ""));

  await expect(page.getByRole("heading", { name: "Opportunities", exact: true })).toBeVisible();
  await expect(page.getByText("No opportunities yet.")).toBeVisible();
  await expect(page.getByRole("table", { name: /^Opportunities for / })).toHaveCount(0);
});
