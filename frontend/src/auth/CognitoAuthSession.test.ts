// SPDX-License-Identifier: MIT
/**
 * Tests for sign-out against the real `amazon-cognito-identity-js`: a session restored from seeded
 * browser storage, with `fetch` stubbed so the only observable network effect is a `RevokeToken`
 * request. Asserts the refresh token is revoked when revocable and before the cache is cleared, that
 * revocation is bounded at `REVOKE_TIMEOUT_MS`, that cached tokens are gone once `signOut` resolves,
 * and that a non-revocable token makes no request at all.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RuntimeConfig } from "../config/runtimeConfig";
import { CognitoAuthSession, REVOKE_TIMEOUT_MS } from "./CognitoAuthSession";

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
    fetchMock.mockImplementation(() => Promise.resolve(new Response("{}", { status: 200 })));
    seedCachedSession({ revocable: true });
    const session = await restoredSession();

    await session.signOut();

    const requests = revokeRequests(fetchMock);
    expect(requests).toHaveLength(1);
    expect(JSON.parse(requests[0]?.body as string)).toEqual({
      Token: REFRESH_TOKEN,
      ClientId: CLIENT_ID,
    });
  });

  it("keeps the cache until revocation succeeds, then clears it without waiting for the bound", async () => {
    let respond: (response: Response) => void = () => {};
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          respond = resolve;
        }),
    );
    seedCachedSession({ revocable: true });
    const session = await restoredSession();

    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      let resolved = false;
      const signingOut = session.signOut().then(() => {
        resolved = true;
      });
      await vi.advanceTimersByTimeAsync(REVOKE_TIMEOUT_MS / 2);
      expect(resolved).toBe(false);
      expect(cognitoKeys().length).toBeGreaterThan(0);

      respond(new Response("{}", { status: 200 }));
      await signingOut;
      expect(cognitoKeys()).toEqual([]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("gives up on a revocation that hangs after the bound, warning once, and clears the cache", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    seedCachedSession({ revocable: true });
    const session = await restoredSession();

    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      let resolved = false;
      const signingOut = session.signOut().then(() => {
        resolved = true;
      });
      await vi.advanceTimersByTimeAsync(REVOKE_TIMEOUT_MS - 1);
      expect(resolved).toBe(false);
      expect(cognitoKeys().length).toBeGreaterThan(0);

      await vi.advanceTimersByTimeAsync(1);
      await signingOut;
    } finally {
      vi.useRealTimers();
    }

    expect(revokeRequests(fetchMock)).toHaveLength(1);
    expect(cognitoKeys()).toEqual([]);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(warn.mock.calls)).not.toContain(REFRESH_TOKEN);
  });

  it("makes no network call for a token without origin_jti, and still clears the cache", async () => {
    seedCachedSession({ revocable: false });
    const session = await restoredSession();

    await session.signOut();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(cognitoKeys()).toEqual([]);
  });

  it("reports a failed revocation once, without the token, and clears the cache", async () => {
    fetchMock.mockImplementation(() => Promise.reject(new TypeError("network down")));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    seedCachedSession({ revocable: true });
    const session = await restoredSession();

    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      const signingOut = session.signOut();
      await vi.advanceTimersByTimeAsync(120_000);
      await signingOut;
    } finally {
      vi.useRealTimers();
    }

    expect(warn).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(warn.mock.calls)).not.toContain(REFRESH_TOKEN);
    expect(cognitoKeys()).toEqual([]);
  });

  it("does not navigate away from the page", async () => {
    seedCachedSession({ revocable: false });
    const session = await restoredSession();
    const before = window.location.href;

    await session.signOut();

    expect(window.location.href).toBe(before);
  });
});
