// SPDX-License-Identifier: MIT
/**
 * Journey 1 — a user signs in through the SPA's sign-in form against the real Cognito pool and lands
 * on a scoped application; before sign-in a protected route renders no CRM data, and sign-out leaves
 * no session behind.
 *
 * Sign-out also ends the Cognito Hosted UI session when the deployment configures one: the browser
 * passes through the Hosted UI `/logout` and lands on the SPA's `/logout` sign-in page, and the next
 * Hosted UI sign-in asks for credentials again. Those tests read `cognitoHostedUiDomain` from the
 * deployed `/config.json` and skip, naming the empty value, when Hosted UI is not configured; the
 * no-redirect branch is asserted instead.
 */
import type { Page } from "@playwright/test";

import { cspViolationsOf, expect, signInAsDemo, test } from "../fixtures/auth";

/** The SPA path the Hosted UI returns to after its own logout. */
const LOGOUT_PATH = "/logout";

/** How long a Cognito Hosted UI round trip may take. */
const HOSTED_UI_TIMEOUT_MS = 30_000;

interface HostedUiConfig {
  readonly domain: string;
  readonly clientId: string;
}

/** The deployed SPA's Hosted UI settings; `domain` is empty when Hosted UI is not configured. */
async function hostedUiConfig(page: Page): Promise<HostedUiConfig> {
  const response = await page.request.get("/config.json");
  expect(response.ok(), "the deployed /config.json is readable").toBe(true);
  const config = (await response.json()) as Record<string, unknown>;
  expect(typeof config.cognitoHostedUiDomain, "config.json carries cognitoHostedUiDomain").toBe(
    "string",
  );
  expect(typeof config.cognitoClientId, "config.json carries cognitoClientId").toBe("string");
  return { domain: config.cognitoHostedUiDomain as string, clientId: config.cognitoClientId as string };
}

const HOSTED_UI_DISABLED =
  "the deployed /config.json has an empty cognitoHostedUiDomain, so the SPA offers no Hosted UI " +
  "and sign-out has no Hosted UI session to end";

function spaOrigin(page: Page): string {
  return new URL(page.url()).origin;
}

/** Resolves when the browser navigates back to the SPA carrying a Hosted UI authorization code. */
function callbackNavigation(page: Page, origin: string): Promise<unknown> {
  return page.waitForRequest(
    (request) =>
      request.isNavigationRequest() &&
      new URL(request.url()).origin === origin &&
      new URL(request.url()).searchParams.has("code"),
    { timeout: HOSTED_UI_TIMEOUT_MS },
  );
}

/** The credential fields of the Cognito Hosted UI sign-in page, visible copies only. */
function hostedUiPasswordField(page: Page) {
  return page.locator('input[name="password"]:visible, input[type="password"]:visible').first();
}

/**
 * Sign in through the Hosted UI form so the Cognito domain holds a session cookie. Leaves the page
 * back on the SPA origin.
 */
async function signInThroughHostedUi(page: Page, domain: string): Promise<void> {
  const email = process.env.CRM_DEMO_USER_EMAIL;
  const password = process.env.CRM_DEMO_USER_PASSWORD;
  if (!email || !password) {
    throw new Error("CRM_DEMO_USER_EMAIL and CRM_DEMO_USER_PASSWORD must be set for this journey");
  }
  await page.goto("/");
  const origin = spaOrigin(page);
  await page.getByTestId("hosted-ui-signin").click();
  await page.waitForURL((url) => url.hostname === domain, { timeout: HOSTED_UI_TIMEOUT_MS });
  await page.locator('input[name="username"]:visible').first().fill(email);
  await hostedUiPasswordField(page).fill(password);
  const callback = callbackNavigation(page, origin);
  await page.locator('button[type="submit"]:visible, input[type="submit"]:visible').first().click();
  await callback;
}

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
  const origin = spaOrigin(page);
  const hostedUi = await hostedUiConfig(page);

  const revoke = page.waitForResponse(
    async (response) =>
      new URL(response.url()).hostname.startsWith("cognito-idp.") &&
      ((await response.request().headerValue("x-amz-target")) ?? "").endsWith("RevokeToken"),
    { timeout: 15_000 },
  );
  const hostedUiLogout =
    hostedUi.domain.length > 0
      ? page.waitForRequest(
          (request) =>
            request.isNavigationRequest() &&
            new URL(request.url()).hostname === hostedUi.domain &&
            new URL(request.url()).pathname === "/logout",
          { timeout: HOSTED_UI_TIMEOUT_MS },
        )
      : undefined;
  await page.getByRole("button", { name: /sign out/i }).click();
  expect((await revoke).status(), "sign-out revokes the refresh token at Cognito").toBe(200);
  expect(cspViolationsOf(page), "the revoke call ran without a CSP violation").toEqual([]);

  if (hostedUiLogout !== undefined) {
    const logoutUrl = new URL((await hostedUiLogout).url());
    expect(logoutUrl.searchParams.get("client_id")).toBe(hostedUi.clientId);
    expect(logoutUrl.searchParams.get("logout_uri")).toBe(`${origin}${LOGOUT_PATH}`);
    await page.waitForURL(`${origin}${LOGOUT_PATH}`, { timeout: HOSTED_UI_TIMEOUT_MS });
  } else {
    // No Hosted UI: sign-out stays on the SPA origin.
    expect(new URL(page.url()).origin).toBe(origin);
  }
  await expect(page.getByRole("button", { name: /sign in/i })).toBeVisible();

  // A reload does not restore the session.
  await page.reload();
  await expect(page.getByRole("button", { name: /sign in/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /sign out/i })).toHaveCount(0);
});

test("the /logout route serves the signed-out sign-in page", async ({ page }) => {
  const response = await page.goto(LOGOUT_PATH);
  expect(response?.status(), "the SPA fallback serves /logout").toBe(200);

  await expect(page.getByRole("form", { name: "Sign in to FirstCRM" })).toBeVisible();
  await expect(page.getByRole("button", { name: /sign out/i })).toHaveCount(0);
});

test("after sign-out, a Hosted UI sign-in asks for credentials again", async ({ page }) => {
  const hostedUi = await hostedUiConfig(page);
  test.skip(hostedUi.domain.length === 0, HOSTED_UI_DISABLED);

  await signInThroughHostedUi(page, hostedUi.domain);
  const origin = spaOrigin(page);

  // Control: with the Hosted UI session alive, a second Hosted UI sign-in returns without a prompt.
  await page.goto("/");
  const silentCallback = callbackNavigation(page, origin);
  await page.getByTestId("hosted-ui-signin").click();
  await silentCallback;
  await expect(hostedUiPasswordField(page)).toHaveCount(0);

  await signInAsDemo(page);
  await page.getByRole("button", { name: /sign out/i }).click();
  await page.waitForURL(`${origin}${LOGOUT_PATH}`, { timeout: HOSTED_UI_TIMEOUT_MS });
  await expect(page.getByRole("button", { name: /sign in/i })).toBeVisible();

  await page.getByTestId("hosted-ui-signin").click();
  await page.waitForURL((url) => url.hostname === hostedUi.domain, {
    timeout: HOSTED_UI_TIMEOUT_MS,
  });
  await expect(hostedUiPasswordField(page), "the Hosted UI prompts for credentials").toBeVisible({
    timeout: HOSTED_UI_TIMEOUT_MS,
  });
});

test("a Hosted UI logout with no session still lands on the signed-out page", async ({ page }) => {
  const hostedUi = await hostedUiConfig(page);
  test.skip(hostedUi.domain.length === 0, HOSTED_UI_DISABLED);

  await page.goto("/");
  const origin = spaOrigin(page);
  const params = new URLSearchParams({
    client_id: hostedUi.clientId,
    logout_uri: `${origin}${LOGOUT_PATH}`,
  });
  await page.goto(`https://${hostedUi.domain}/logout?${params.toString()}`);

  await page.waitForURL(`${origin}${LOGOUT_PATH}`, { timeout: HOSTED_UI_TIMEOUT_MS });
  await expect(page.getByRole("form", { name: "Sign in to FirstCRM" })).toBeVisible();
  await expect(page.getByRole("button", { name: /sign out/i })).toHaveCount(0);
});
