// SPDX-License-Identifier: MIT
/**
 * Calls the deployed API from inside a signed-in page, with the ID token the Hosted UI sign-in left
 * in the Cognito localStorage cache. For server answers a journey must observe but the UI does not
 * offer a control for (a refused second start, a repeated cancel, a racing write). The request goes
 * through the same CloudFront `/api` route and Cognito authorizer as the SPA's own calls.
 */
import { expect, type Page } from "@playwright/test";

import { randomToken } from "./randomToken";

/** A route's HTTP status and parsed JSON body (`null` when the body is not JSON). */
export interface ApiReply {
  readonly status: number;
  readonly body: unknown;
}

/** Whether `body` would start a billable live sweep. Only the opt-in-guarded UI step may. */
export function startsALiveSweep(body: Record<string, unknown>): boolean {
  return body.mode === "live" && body.confirmed === true;
}

/**
 * POST `body` to `/api/<route>` from `page`'s origin as the signed-in user.
 *
 * @throws When `body` would start a live sweep, or when no ID token is in the Cognito cache, i.e.
 *   the page is not signed in.
 */
export async function postAsSignedInUser(
  page: Page,
  route: string,
  body: Record<string, unknown>,
): Promise<ApiReply> {
  if (startsALiveSweep(body)) {
    throw new Error(
      `refusing to POST {mode: "live", confirmed: true} to /api/${route}: an API probe must never ` +
        "start a billable sweep; use confirmed: false or a stub run",
    );
  }
  const reply = await page.evaluate(
    async ({ path, payload }) => {
      const tokenKey = Object.keys(window.localStorage).find((key) =>
        /^CognitoIdentityServiceProvider\..+\.idToken$/.test(key),
      );
      const token = tokenKey === undefined ? null : window.localStorage.getItem(tokenKey);
      if (token === null) return { status: -1, body: null };
      const response = await fetch(`/api/${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify(payload),
      });
      let parsed: unknown = null;
      try {
        parsed = await response.json();
      } catch {
        parsed = null;
      }
      return { status: response.status, body: parsed };
    },
    { path: route, payload: body },
  );
  expect(
    reply.status,
    "no Cognito ID token in localStorage, so the page is not signed in; sign in before calling the API",
  ).not.toBe(-1);
  return reply;
}

/** The value at `path` inside a parsed JSON body, or `undefined`. */
export function field(body: unknown, ...path: string[]): unknown {
  let current: unknown = body;
  for (const key of path) {
    if (typeof current !== "object" || current === null) return undefined;
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

/** A unique idempotency key for one write. */
export function transactionKey(label: string): string {
  return `e2e-${label}-${Date.now()}-${randomToken(8)}`;
}
