// SPDX-License-Identifier: MIT
/**
 * Journey 1 — a user signs in through the SPA's sign-in form against the real Cognito pool and lands
 * on a scoped application; before sign-in a protected route renders no CRM data, and sign-out leaves
 * no session behind.
 */
import { expect, test } from "@playwright/test";
import { signInAsDemo } from "../fixtures/auth";

test("a protected route renders no CRM data before sign-in", async ({ page }) => {
  await page.goto("/pipeline");

  // The signed-out surface is the sign-in affordance, not an empty board.
  await expect(page.getByRole("button", { name: /sign in/i })).toBeVisible();
  await expect(page.getByTestId("pipeline-column-Discovery")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /sign out/i })).toHaveCount(0);
});

test("signing in through the sign-in form lands on a scoped, operable application", async ({ page }) => {
  await signInAsDemo(page);

  await expect(page.getByRole("button", { name: /sign out/i })).toBeVisible();

  // The shell discloses whose data this is.
  const header = page.getByRole("banner");
  await expect(header).toContainText(/rep-|full visibility|accounts/i);

  // The skip link is the first focusable element.
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: /skip to main content/i })).toBeFocused();

  await page.goto("/pipeline");
  await expect(page.getByRole("main")).toBeVisible();
  // One main landmark, one h1.
  await expect(page.getByRole("main")).toHaveCount(1);
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
});

test("signing out leaves no session behind", async ({ page }) => {
  await signInAsDemo(page);

  await page.getByRole("button", { name: /sign out/i }).click();
  await expect(page.getByRole("button", { name: /sign in/i })).toBeVisible();

  // A reload does not restore the session.
  await page.reload();
  await expect(page.getByRole("button", { name: /sign in/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /sign out/i })).toHaveCount(0);
});
