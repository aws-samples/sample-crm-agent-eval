// SPDX-License-Identifier: MIT
/**
 * Mints unsigned dev JWTs and seeds a Cognito session from them.
 *
 * The tokens carry no signature and are accepted only by `devApi.ts`, which the dev server mounts.
 * The session is seeded into the cache `amazon-cognito-identity-js` reads, so the unmodified
 * `CognitoAuthSession.restore()` establishes it. The sign-in handshake itself is not exercised.
 *
 * Never imported by application code.
 */

import { DEV_BOOKS, DEV_EMPTY_REP_ID, DEV_OTHER_REP_ID, DEV_REP_ID } from "./devFixtures";

function base64Url(value: string): string {
  return btoa(value).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export interface DevRep {
  readonly repId: string;
  readonly username: string;
  readonly book: readonly string[];
  readonly fullVisibility: boolean;
}

/** The dev reps. The scoped one is the default. */
export const DEV_REPS: Readonly<Record<string, DevRep>> = {
  scoped: {
    repId: DEV_REP_ID,
    username: "dev-scoped",
    book: DEV_BOOKS[DEV_REP_ID] ?? [],
    fullVisibility: false,
  },
  manager: {
    repId: DEV_OTHER_REP_ID,
    username: "dev-manager",
    book: DEV_BOOKS[DEV_OTHER_REP_ID] ?? [],
    fullVisibility: true,
  },
  /**
   * A rep with an empty book (no accounts assigned), so the designed empty states are reachable in a
   * browser.
   */
  empty: {
    repId: DEV_EMPTY_REP_ID,
    username: "dev-empty",
    book: [],
    fullVisibility: false,
  },
};

/** An unsigned JWT carrying the claims `parseIdentityClaims` requires. */
export function mintDevToken(rep: DevRep, lifetimeSeconds = 3600): string {
  const now = Math.floor(Date.now() / 1000);
  const header = base64Url(JSON.stringify({ alg: "none", typ: "JWT" }));
  const payload = base64Url(
    JSON.stringify({
      sub: `dev-${rep.repId}`,
      "custom:rep_id": rep.repId,
      "custom:book": rep.book.join(","),
      "custom:full_visibility": String(rep.fullVisibility),
      "cognito:username": rep.username,
      token_use: "access",
      iat: now,
      exp: now + lifetimeSeconds,
    }),
  );
  return `${header}.${payload}.dev-not-a-signature`;
}

/**
 * Writes the token cache `amazon-cognito-identity-js` reads, so `CognitoUserPool.getCurrentUser()`
 * finds a user and `restore()` returns a session. Key layout is that library's own convention.
 */
export function seedDevCognitoSession(clientId: string, rep: DevRep): void {
  const token = mintDevToken(rep);
  const prefix = `CognitoIdentityServiceProvider.${clientId}`;
  const entries: Record<string, string> = {
    [`${prefix}.LastAuthUser`]: rep.username,
    [`${prefix}.${rep.username}.idToken`]: token,
    [`${prefix}.${rep.username}.accessToken`]: token,
    [`${prefix}.${rep.username}.refreshToken`]: "dev-refresh-token",
    [`${prefix}.${rep.username}.clockDrift`]: "0",
  };
  for (const [key, value] of Object.entries(entries)) {
    window.localStorage.setItem(key, value);
  }
}

/** Removes the seeded session, so the signed-out surface can be reviewed too. */
export function clearDevCognitoSession(clientId: string, rep: DevRep): void {
  const prefix = `CognitoIdentityServiceProvider.${clientId}`;
  for (const suffix of ["idToken", "accessToken", "refreshToken", "clockDrift"]) {
    window.localStorage.removeItem(`${prefix}.${rep.username}.${suffix}`);
  }
  window.localStorage.removeItem(`${prefix}.LastAuthUser`);
}
