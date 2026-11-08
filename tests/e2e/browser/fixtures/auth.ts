// SPDX-License-Identifier: MIT
/**
 * Signed-in page fixture for the browser journeys: drives the SPA's own SRP sign-in form against
 * the real Cognito pool (no injected token), and waits for the signed-in application shell.
 *
 * Two accounts: `demo` (`CRM_DEMO_USER_EMAIL` / `CRM_DEMO_USER_PASSWORD`, not in the `admin` group)
 * and `admin` (`CRM_E2E_ADMIN_EMAIL` / `CRM_E2E_ADMIN_PASSWORD`, in the `admin` group). A missing
 * variable throws. A refused sign-in throws `SignInRejectedError`, and a Cognito challenge the form
 * does not support throws `SignInChallengeError`, both as soon as the form reports it.
 *
 * Every test built on `test` here (directly, or through `fixtures/teardown.ts`) runs with the
 * Content-Security-Policy violation trap from `fixtures/cspTrap.ts` on its `page`, and fails after its
 * body if any violation was recorded.
 */
import { test as base, expect, type Page } from "@playwright/test";

import { cspViolationsOf, installCspViolationTrap } from "./cspTrap";

export { cspViolationsOf, installCspViolationTrap };

const EMAIL_ENV_VAR = "CRM_DEMO_USER_EMAIL";
const PASSWORD_ENV_VAR = "CRM_DEMO_USER_PASSWORD";  // pragma: allowlist secret

/** The smoke accounts a journey can sign in as, and the variables holding each one's credentials. */
const ACCOUNTS = {
  demo: { email: EMAIL_ENV_VAR, password: PASSWORD_ENV_VAR },
  admin: { email: "CRM_E2E_ADMIN_EMAIL", password: "CRM_E2E_ADMIN_PASSWORD" },  // pragma: allowlist secret gitleaks:allow
} as const;

export type SmokeAccount = keyof typeof ACCOUNTS;

/** How long Cognito's SRP exchange and the first shell render may take. */
const SIGN_IN_TIMEOUT_MS = 30_000;

/** The prefix `CognitoAuthSession` puts on a challenge it does not support. */
const UNSUPPORTED_CHALLENGE_PREFIX = "unsupported Cognito challenge:";

/** The form reported that Cognito refused the credentials (e.g. a wrong password). */
export class SignInRejectedError extends Error {
  constructor(account: SmokeAccount, formMessage: string) {
    super(`sign-in as the ${account} account was rejected: ${formMessage}`);
    this.name = "SignInRejectedError";
  }
}

/** Cognito asked for a challenge (MFA, new password) that the SPA's sign-in form does not render. */
export class SignInChallengeError extends Error {
  constructor(account: SmokeAccount, formMessage: string) {
    super(
      `sign-in as the ${account} account needs a challenge the SPA form does not support ` +
        `(${formMessage}); provision the user without it (scripts/provision_demo_user.py)`,
    );
    this.name = "SignInChallengeError";
  }
}

/** Read a required credential, failing with the variable name rather than a generic assertion. */
function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `${name} is unset. The browser journeys sign in through the SPA's form against the real ` +
        "Cognito pool and cannot substitute an injected token — that would skip the boundary they " +
        "exist to prove. Provision the user with scripts/provision_demo_user.py and export its " +
        `email and password variables (${EMAIL_ENV_VAR}/${PASSWORD_ENV_VAR} for the demo user, ` +
        `${ACCOUNTS.admin.email}/${ACCOUNTS.admin.password} for an admin-group user).`,
    );
  }
  return value;
}

/** Sign `page` in as the demo user, leaving it on the signed-in application. */
export async function signInAsDemo(page: Page): Promise<void> {
  await signInAs(page, "demo");
}

/**
 * Sign `page` in as `account` through the SPA's sign-in form, leaving it on the signed-in
 * application shell.
 *
 * Throws `SignInRejectedError` or `SignInChallengeError` as soon as the form shows its error alert.
 */
export async function signInAs(page: Page, account: SmokeAccount): Promise<void> {
  const email = requiredEnv(ACCOUNTS[account].email);
  const password = requiredEnv(ACCOUNTS[account].password);

  await page.goto("/");

  const form = page.getByRole("form", { name: "Sign in to FirstCRM" });
  await expect(form).toBeVisible();
  await form.getByLabel("Username", { exact: true }).fill(email);
  await form.getByLabel("Password", { exact: true }).fill(password);
  await form.getByRole("button", { name: "Sign in", exact: true }).click();

  const shell = page.getByTestId("app-shell");
  const error = page.getByTestId("signin-error");
  await expect(shell.or(error)).toBeVisible({ timeout: SIGN_IN_TIMEOUT_MS });

  if (await error.isVisible()) {
    const formMessage = (await error.innerText()).trim();
    if (formMessage.startsWith(UNSUPPORTED_CHALLENGE_PREFIX)) {
      throw new SignInChallengeError(account, formMessage);
    }
    throw new SignInRejectedError(account, formMessage);
  }

  await expect(page.getByRole("button", { name: "Sign out", exact: true })).toBeVisible();
}

/**
 * `test` whose `page` carries the CSP violation trap, plus a `signedInPage` fixture: that page
 * signed in as the demo user.
 */
export const test = base.extend<{ signedInPage: Page }>({
  page: async ({ page }, use) => {
    await installCspViolationTrap(page);
    await use(page);
    expect(
      cspViolationsOf(page),
      "the page recorded Content-Security-Policy violations; every journey must run without one",
    ).toEqual([]);
  },
  signedInPage: async ({ page }, use) => {
    await signInAsDemo(page);
    await use(page);
  },
});

export { expect };
