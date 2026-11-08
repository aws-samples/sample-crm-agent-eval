// SPDX-License-Identifier: MIT
/**
 * Tests for sign-out against the real `amazon-cognito-identity-js`: a session restored from seeded
 * browser storage, with `fetch` stubbed so the only observable network effect is a `RevokeToken`
 * request. Asserts the refresh token is revoked when revocable, that cached tokens are gone before
 * `signOut` returns, and that a non-revocable token makes no request at all.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RuntimeConfig } from "../config/runtimeConfig";
import { CognitoAuthSession } from "./CognitoAuthSession";

const CLIENT_ID = "testclient123";
const USERNAME = "rep-042";
const KEY_PREFIX = `CognitoIdentityServiceProvider.${CLIENT_ID}`;
const REFRESH_TOKEN = "opaque-refresh-token-value"; // gitleaks:allow

const CONFIG = {
  cognitoUserPoolId: "us-east-1_TestPool1",
  cognitoClientId: CLIENT_ID,
  cognitoRegion: "us-east-1",
  cognitoHostedUiDomain: "",
} as RuntimeConfig;

function base64Url(value: object): string {
  return btoa(JSON.stringify(value)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
}

function jwt(claims: Record<string, unknown>): string {
  const now = Math.floor(Date.now() / 1000);
  return `${base64Url({ alg: "none" })}.${base64Url({ iat: now, exp: now + 3600, ...claims })}.sig`;
}

function seedCachedSession({ revocable }: { revocable: boolean }): void {
  const userPrefix = `${KEY_PREFIX}.${USERNAME}`;
  localStorage.setItem(`${KEY_PREFIX}.LastAuthUser`, USERNAME);
  localStorage.setItem(`${userPrefix}.idToken`, jwt({ token_use: "id", sub: "abc" }));
  localStorage.setItem(
    `${userPrefix}.accessToken`,
    jwt({ token_use: "access", ...(revocable ? { origin_jti: "origin-jti-1" } : {}) }),
  );
  localStorage.setItem(`${userPrefix}.refreshToken`, REFRESH_TOKEN);
  localStorage.setItem(`${userPrefix}.clockDrift`, "0");
}

function cognitoKeys(): string[] {
  return Object.keys(localStorage).filter((key) => key.startsWith("CognitoIdentityServiceProvider."));
}

async function restoredSession(): Promise<NonNullable<Awaited<ReturnType<typeof CognitoAuthSession.restore>>>> {
  const session = await CognitoAuthSession.restore(CONFIG);
  if (session === undefined) throw new Error("the seeded session did not restore");
  return session;
}

function revokeRequests(fetchMock: ReturnType<typeof vi.fn>): RequestInit[] {
  return fetchMock.mock.calls
    .map((call) => call[1] as RequestInit)
    .filter((init) => {
      const headers = init.headers as Record<string, string>;
      return headers["X-Amz-Target"] === "AWSCognitoIdentityProviderService.RevokeToken";
    });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  localStorage.clear();
  fetchMock = vi.fn(() => new Promise<Response>(() => {}));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("CognitoAuthSession.signOut", () => {
  it("restores from the seeded cache without a network call (fixture control)", async () => {
    seedCachedSession({ revocable: true });
    await restoredSession();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(cognitoKeys().length).toBeGreaterThan(0);
  });

  it("sends one RevokeToken request carrying the refresh token", async () => {
    seedCachedSession({ revocable: true });
    const session = await restoredSession();

    session.signOut();

    const requests = revokeRequests(fetchMock);
    expect(requests).toHaveLength(1);
    expect(JSON.parse(requests[0]?.body as string)).toEqual({
      Token: REFRESH_TOKEN,
      ClientId: CLIENT_ID,
    });
  });

  it("clears every cached Cognito key before it returns, with the revocation still pending", async () => {
    seedCachedSession({ revocable: true });
    const session = await restoredSession();

    session.signOut();

    expect(cognitoKeys()).toEqual([]);
    expect(revokeRequests(fetchMock)).toHaveLength(1);
  });

  it("makes no network call for a token without origin_jti, and still clears the cache", async () => {
    seedCachedSession({ revocable: false });
    const session = await restoredSession();

    session.signOut();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(cognitoKeys()).toEqual([]);
  });

  it("reports a failed revocation without the token and keeps the cache cleared", async () => {
    fetchMock.mockImplementation(() => Promise.reject(new TypeError("network down")));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    seedCachedSession({ revocable: true });
    const session = await restoredSession();

    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      session.signOut();
      await vi.advanceTimersByTimeAsync(120_000);
    } finally {
      vi.useRealTimers();
    }

    expect(warn).toHaveBeenCalledTimes(1);

    expect(JSON.stringify(warn.mock.calls)).not.toContain(REFRESH_TOKEN);
    expect(cognitoKeys()).toEqual([]);
  });

  it("does not navigate away from the page", async () => {
    seedCachedSession({ revocable: true });
    const session = await restoredSession();
    const before = window.location.href;

    session.signOut();

    expect(window.location.href).toBe(before);
  });
});
