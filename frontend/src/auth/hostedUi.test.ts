// SPDX-License-Identifier: MIT
/**
 * Tests for the Hosted UI PKCE flow.
 *
 * 1. The verifier is generated per attempt and never reused: two attempts differ, reading an
 *    attempt deletes it, and a replayed callback is rejected.
 * 2. Every token-exchange failure path is a `HostedUiError` whose message names what happened and
 *    what to do.
 * 3. Sign-out navigates to the Hosted UI `/logout` with the SPA client and the `/logout` return URI,
 *    and only when Hosted UI is configured.
 */

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import {
  HostedUiError,
  PKCE_STORAGE_KEY,
  beginHostedUiSignIn,
  completeHostedUiSignIn,
  createCodeChallenge,
  createCodeVerifier,
  createState,
  endHostedUiSession,
  exchangeCodeForTokens,
  extractAuthorizationCode,
  hostedUiLogoutRedirectUri,
  hostedUiLogoutUrl,
  hostedUiRedirectUri,
  isHostedUiCallback,
  isHostedUiEnabled,
  takePkceAttempt,
} from "./hostedUi";
import * as AuthProviderModule from "./AuthProvider";
import { SignInForm } from "./SignInForm";
import {
  __resetRuntimeConfigForTests,
  loadRuntimeConfig,
  type RuntimeConfig,
} from "../config/runtimeConfig";

const config: RuntimeConfig = {
  cognitoUserPoolId: "us-east-1_EXAMPLE",
  cognitoClientId: "exampleclientid",
  cognitoRegion: "us-east-1",
  cognitoHostedUiDomain: "crm-example.auth.us-east-1.amazoncognito.com",
  apiBaseUrl: "/api",
  agentBaseUrl: "/agent",
  // Raw, not pre-encoded, as `scripts/render_frontend_config.py` writes it.
  agentRuntimeArn: "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/example",
};

const REDIRECT_URI = "https://crm.example/";

/** An in-memory `sessionStorage`; jsdom provides none in this project's configuration. */
function installSessionStorage(): Map<string, string> {
  const store = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
  });
  return store;
}

function base64UrlDecode(value: string): string {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/");
  return atob(padded.padEnd(Math.ceil(padded.length / 4) * 4, "="));
}

/** A syntactically valid id token carrying the given claims. */
function idTokenFor(claims: Record<string, unknown>): string {
  const encode = (value: unknown) =>
    btoa(JSON.stringify(value)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${encode({ alg: "RS256" })}.${encode(claims)}.signature`;
}

const VALID_CLAIMS = {
  sub: "user-1",
  "custom:rep_id": "rep-7",
  exp: 4_000_000_000,
  "custom:book": "acc-1,acc-2",
  "custom:full_visibility": "false",
};

function tokenResponse(claims: Record<string, unknown> = VALID_CLAIMS) {
  return {
    ok: true,
    status: 200,
    json: () =>
      Promise.resolve({
        id_token: idTokenFor(claims),
        access_token: "access-token",
        refresh_token: "refresh-token",
        expires_in: 3600,
      }),
    text: () => Promise.resolve(""),
  } as unknown as Response;
}

let storage: Map<string, string>;

beforeEach(() => {
  storage = installSessionStorage();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("PKCE verifier and challenge", () => {
  it("mints a verifier inside RFC 7636's length and alphabet", () => {
    const verifier = createCodeVerifier();
    expect(verifier.length).toBeGreaterThanOrEqual(43);
    expect(verifier.length).toBeLessThanOrEqual(128);
    expect(verifier).toMatch(/^[A-Za-z0-9\-._~]+$/);
  });

  it("never mints the same verifier twice", () => {
    const verifiers = new Set(Array.from({ length: 50 }, () => createCodeVerifier()));
    expect(verifiers.size).toBe(50);
  });

  it("never mints the same state twice", () => {
    const states = new Set(Array.from({ length: 50 }, () => createState()));
    expect(states.size).toBe(50);
  });

  it("derives an S256 challenge that is not the verifier and is url-safe", async () => {
    const verifier = createCodeVerifier();
    const challenge = await createCodeChallenge(verifier);
    expect(challenge).not.toBe(verifier);
    expect(challenge).toMatch(/^[A-Za-z0-9\-_]+$/);
    expect(base64UrlDecode(challenge)).toHaveLength(32); // SHA-256 output
  });

  it("derives the challenge deterministically from the verifier", async () => {
    const verifier = createCodeVerifier();
    expect(await createCodeChallenge(verifier)).toBe(await createCodeChallenge(verifier));
    expect(await createCodeChallenge(createCodeVerifier())).not.toBe(
      await createCodeChallenge(verifier),
    );
  });

  it("uses crypto.getRandomValues, not Math.random", () => {
    const randomSpy = vi.spyOn(Math, "random");
    const cryptoSpy = vi.spyOn(crypto, "getRandomValues");
    createCodeVerifier();
    expect(cryptoSpy).toHaveBeenCalled();
    expect(randomSpy).not.toHaveBeenCalled();
  });
});

describe("a verifier is per attempt and never reused", () => {
  it("stores a fresh verifier and state on every begin", async () => {
    const first = await beginHostedUiSignIn(config, REDIRECT_URI);
    const firstAttempt = JSON.parse(storage.get(PKCE_STORAGE_KEY) ?? "{}");

    await beginHostedUiSignIn(config, REDIRECT_URI);
    const secondAttempt = JSON.parse(storage.get(PKCE_STORAGE_KEY) ?? "{}");

    expect(firstAttempt.verifier).not.toBe(secondAttempt.verifier);
    expect(firstAttempt.state).not.toBe(secondAttempt.state);
    expect(new URL(first).searchParams.get("state")).toBe(firstAttempt.state);
  });

  it("sends the challenge, never the verifier, to the authorization endpoint", async () => {
    const url = new URL(await beginHostedUiSignIn(config, REDIRECT_URI));
    const attempt = JSON.parse(storage.get(PKCE_STORAGE_KEY) ?? "{}");

    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("code_challenge")).toBe(
      await createCodeChallenge(attempt.verifier),
    );
    expect(url.search).not.toContain(attempt.verifier);
    expect(url.searchParams.get("code_verifier")).toBeNull();
  });

  it("deletes the attempt as it reads it, so a second read finds nothing", async () => {
    await beginHostedUiSignIn(config, REDIRECT_URI);
    expect(takePkceAttempt()).toBeDefined();
    expect(takePkceAttempt()).toBeUndefined();
    expect(storage.has(PKCE_STORAGE_KEY)).toBe(false);
  });

  it("rejects a replayed callback because the verifier is already consumed", async () => {
    const fetchMock = vi.fn().mockResolvedValue(tokenResponse());
    vi.stubGlobal("fetch", fetchMock);

    const loginUrl = new URL(await beginHostedUiSignIn(config, REDIRECT_URI));
    const callback = `${REDIRECT_URI}?code=auth-code&state=${loginUrl.searchParams.get("state")}`;

    await expect(completeHostedUiSignIn(config, callback, REDIRECT_URI)).resolves.toMatchObject({
      identity: { repId: "rep-7" },
    });

    // The identical URL a second time — a back-button or a copied link.
    await expect(completeHostedUiSignIn(config, callback, REDIRECT_URI)).rejects.toMatchObject({
      failure: "attempt_missing",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("clears a corrupt stored attempt rather than leaving it to be retried", () => {
    storage.set(PKCE_STORAGE_KEY, "{not json");
    expect(takePkceAttempt()).toBeUndefined();
    expect(storage.has(PKCE_STORAGE_KEY)).toBe(false);
  });

  it("ignores a stored attempt missing either field", () => {
    storage.set(PKCE_STORAGE_KEY, JSON.stringify({ verifier: "only-a-verifier" }));
    expect(takePkceAttempt()).toBeUndefined();
  });

  it("sends the verifier in the exchange body, and only there", async () => {
    const fetchMock = vi.fn().mockResolvedValue(tokenResponse());
    vi.stubGlobal("fetch", fetchMock);

    const loginUrl = new URL(await beginHostedUiSignIn(config, REDIRECT_URI));
    const attempt = JSON.parse(storage.get(PKCE_STORAGE_KEY) ?? "{}");
    await completeHostedUiSignIn(
      config,
      `${REDIRECT_URI}?code=auth-code&state=${loginUrl.searchParams.get("state")}`,
      REDIRECT_URI,
    );

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`https://${config.cognitoHostedUiDomain}/oauth2/token`);
    expect(init.method).toBe("POST");
    const body = new URLSearchParams(init.body as string);
    expect(body.get("code_verifier")).toBe(attempt.verifier);
    expect(body.get("grant_type")).toBe("authorization_code");
    expect(body.get("client_id")).toBe(config.cognitoClientId);
    expect(body.get("client_secret")).toBeNull(); // public client, no secret in the browser
  });
});

describe("state binds the callback to the request this browser started", () => {
  it("rejects a mismatched state and does not exchange the code", async () => {
    const fetchMock = vi.fn().mockResolvedValue(tokenResponse());
    vi.stubGlobal("fetch", fetchMock);
    await beginHostedUiSignIn(config, REDIRECT_URI);

    await expect(
      completeHostedUiSignIn(config, `${REDIRECT_URI}?code=c&state=forged`, REDIRECT_URI),
    ).rejects.toMatchObject({ failure: "state_mismatch" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a missing state", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await beginHostedUiSignIn(config, REDIRECT_URI);
    await expect(
      completeHostedUiSignIn(config, `${REDIRECT_URI}?code=c`, REDIRECT_URI),
    ).rejects.toMatchObject({ failure: "state_mismatch" });
  });
});

describe("every failure is actionable, never blank", () => {
  /** Every message must say what happened and what the user can do about it. */
  function expectActionable(error: unknown): HostedUiError {
    expect(error).toBeInstanceOf(HostedUiError);
    const hostedUiError = error as HostedUiError;
    expect(hostedUiError.message.length).toBeGreaterThan(20);
    expect(hostedUiError.message).toMatch(/try|start|check|ask|use your/i);
    return hostedUiError;
  }

  it("surfaces a provider denial with its description", async () => {
    const error = await completeHostedUiSignIn(
      config,
      `${REDIRECT_URI}?error=access_denied&error_description=User%20cancelled`,
      REDIRECT_URI,
    ).catch((e: unknown) => e);
    expect(expectActionable(error).failure).toBe("provider_denied");
    expect((error as HostedUiError).message).toContain("User cancelled");
  });

  it("surfaces a provider denial with no description", async () => {
    const error = await completeHostedUiSignIn(
      config,
      `${REDIRECT_URI}?error=server_error`,
      REDIRECT_URI,
    ).catch((e: unknown) => e);
    expect(expectActionable(error).failure).toBe("provider_denied");
    expect((error as HostedUiError).message).toContain("server_error");
  });

  it("surfaces a callback carrying neither code nor error", async () => {
    const error = await completeHostedUiSignIn(config, REDIRECT_URI, REDIRECT_URI).catch(
      (e: unknown) => e,
    );
    expect(expectActionable(error).failure).toBe("no_code");
  });

  it("surfaces a non-2xx token exchange with its status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 400,
        text: () => Promise.resolve('{"error":"invalid_grant"}'),
      } as unknown as Response),
    );
    const error = await exchangeCodeForTokens(config, "c", REDIRECT_URI, "v").catch(
      (e: unknown) => e,
    );
    expect(expectActionable(error).failure).toBe("exchange_failed");
    expect((error as HostedUiError).message).toContain("400");
  });

  it("surfaces an unreachable token endpoint", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const error = await exchangeCodeForTokens(config, "c", REDIRECT_URI, "v").catch(
      (e: unknown) => e,
    );
    expect(expectActionable(error).failure).toBe("exchange_unreachable");
  });

  it("surfaces a token response that is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.reject(new SyntaxError("Unexpected token <")),
      } as unknown as Response),
    );
    const error = await exchangeCodeForTokens(config, "c", REDIRECT_URI, "v").catch(
      (e: unknown) => e,
    );
    expect(expectActionable(error).failure).toBe("malformed_token_response");
  });

  it("surfaces a token response missing id_token or access_token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ access_token: "a" }),
      } as unknown as Response),
    );
    const error = await exchangeCodeForTokens(config, "c", REDIRECT_URI, "v").catch(
      (e: unknown) => e,
    );
    expect(expectActionable(error).failure).toBe("malformed_token_response");
  });

  it.each([
    ["a literal null body", null],
    ["a scalar body", 5],
    ["an array body", []],
    ["a string body", "ok"],
  ])("surfaces %s as malformed rather than as a raw TypeError", async (_label, payload) => {
    /* `JSON.parse("null")` succeeds, so a literal `null` body needs its own case. */
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve(payload),
      } as unknown as Response),
    );
    const error = await exchangeCodeForTokens(config, "c", REDIRECT_URI, "v").catch(
      (e: unknown) => e,
    );
    const actionable = expectActionable(error);
    expect(actionable.failure).toBe("malformed_token_response");
    // The message is the actionable one, not a TypeError string.
    expect(actionable.message).toContain("Start sign-in again.");
    expect(actionable.message).not.toContain("Cannot read properties");
  });

  it("refuses to begin when Hosted UI is not configured", async () => {
    const error = await beginHostedUiSignIn(
      { ...config, cognitoHostedUiDomain: "" },
      REDIRECT_URI,
    ).catch((e: unknown) => e);
    expect(expectActionable(error).failure).toBe("not_configured");
  });

  /**
   * The exchange succeeds but the claims cannot be decoded into an identity: an authentication
   * failure naming the decode reason.
   */
  it("fails authentication, with the decode reason, when claims are undecodable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(tokenResponse({ sub: "user-1" })));
    const loginUrl = new URL(await beginHostedUiSignIn(config, REDIRECT_URI));

    const error = await completeHostedUiSignIn(
      config,
      `${REDIRECT_URI}?code=c&state=${loginUrl.searchParams.get("state")}`,
      REDIRECT_URI,
    ).catch((e: unknown) => e);

    const hostedUiError = expectActionable(error);
    expect(hostedUiError.failure).toBe("claims_undecodable");
    expect(hostedUiError.message).toContain("custom:rep_id");
  });

  it("fails authentication when the id token is not a JWT", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ id_token: "not-a-jwt", access_token: "a" }),
      } as unknown as Response),
    );
    const loginUrl = new URL(await beginHostedUiSignIn(config, REDIRECT_URI));
    const error = await completeHostedUiSignIn(
      config,
      `${REDIRECT_URI}?code=c&state=${loginUrl.searchParams.get("state")}`,
      REDIRECT_URI,
    ).catch((e: unknown) => e);
    expect(expectActionable(error).failure).toBe("claims_undecodable");
  });

  it("still reports actionably when sessionStorage is unavailable", async () => {
    vi.stubGlobal("sessionStorage", undefined);
    await beginHostedUiSignIn(config, REDIRECT_URI); // must not throw
    const error = await completeHostedUiSignIn(
      config,
      `${REDIRECT_URI}?code=c&state=s`,
      REDIRECT_URI,
    ).catch((e: unknown) => e);
    expect(expectActionable(error).failure).toBe("attempt_missing");
  });

  it("still reports actionably when sessionStorage throws", async () => {
    vi.stubGlobal("sessionStorage", {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("SecurityError");
      },
      removeItem: () => {
        throw new Error("SecurityError");
      },
    });
    await beginHostedUiSignIn(config, REDIRECT_URI);
    const error = await completeHostedUiSignIn(
      config,
      `${REDIRECT_URI}?code=c&state=s`,
      REDIRECT_URI,
    ).catch((e: unknown) => e);
    expect(expectActionable(error).failure).toBe("attempt_missing");
  });
});

describe("Hosted UI logout", () => {
  const ORIGIN = "https://crm.example";

  function fakeLocation(): { origin: string; assign: Mock<(url: string | URL) => void> } {
    return { origin: ORIGIN, assign: vi.fn<(url: string | URL) => void>() };
  }

  it("builds the /logout URL on the Hosted UI domain with the client and the /logout return URI", () => {
    const url = new URL(hostedUiLogoutUrl(config, ORIGIN));

    expect(url.origin).toBe(`https://${config.cognitoHostedUiDomain}`);
    expect(url.pathname).toBe("/logout");
    expect(url.searchParams.get("client_id")).toBe(config.cognitoClientId);
    expect(url.searchParams.get("logout_uri")).toBe(`${ORIGIN}/logout`);
    expect([...url.searchParams.keys()].sort()).toEqual(["client_id", "logout_uri"]);
  });

  it("returns to the SPA's /logout path, not to the sign-in callback", () => {
    expect(hostedUiLogoutRedirectUri(ORIGIN)).toBe(`${ORIGIN}/logout`);
    expect(hostedUiLogoutRedirectUri(ORIGIN)).not.toBe(hostedUiRedirectUri(ORIGIN));
  });

  it("refuses to build a logout URL when Hosted UI is not configured", () => {
    expect(() => hostedUiLogoutUrl({ ...config, cognitoHostedUiDomain: "" }, ORIGIN)).toThrow(
      HostedUiError,
    );
  });

  it("navigates to the logout URL when Hosted UI is configured", () => {
    const location = fakeLocation();

    expect(endHostedUiSession(config, location)).toBe(true);

    expect(location.assign).toHaveBeenCalledTimes(1);
    expect(location.assign).toHaveBeenCalledWith(hostedUiLogoutUrl(config, ORIGIN));
  });

  it("does not navigate when Hosted UI is not configured", () => {
    const location = fakeLocation();

    expect(endHostedUiSession({ ...config, cognitoHostedUiDomain: "" }, location)).toBe(false);

    expect(location.assign).not.toHaveBeenCalled();
  });
});

describe("callback and config helpers", () => {
  it("recognises a callback carrying a code or an error, and nothing else", () => {
    expect(isHostedUiCallback(`${REDIRECT_URI}?code=c&state=s`)).toBe(true);
    expect(isHostedUiCallback(`${REDIRECT_URI}?error=access_denied`)).toBe(true);
    expect(isHostedUiCallback(REDIRECT_URI)).toBe(false);
    expect(isHostedUiCallback(`${REDIRECT_URI}?other=1`)).toBe(false);
  });

  it("extracts the authorization code, or nothing", () => {
    expect(extractAuthorizationCode(`${REDIRECT_URI}?code=abc`)).toBe("abc");
    expect(extractAuthorizationCode(REDIRECT_URI)).toBeUndefined();
  });

  it("reports whether Hosted UI is configured", () => {
    expect(isHostedUiEnabled(config)).toBe(true);
    expect(isHostedUiEnabled({ ...config, cognitoHostedUiDomain: "" })).toBe(false);
  });

  it("derives one redirect uri so begin and complete cannot disagree", () => {
    expect(hostedUiRedirectUri("https://crm.example")).toBe("https://crm.example/");
  });
});

describe("a successful sign-in", () => {
  it("returns the tokens and the decoded identity", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(tokenResponse()));
    const loginUrl = new URL(await beginHostedUiSignIn(config, REDIRECT_URI));

    const result = await completeHostedUiSignIn(
      config,
      `${REDIRECT_URI}?code=c&state=${loginUrl.searchParams.get("state")}`,
      REDIRECT_URI,
    );

    expect(result.tokens.accessToken).toBe("access-token");
    expect(result.tokens.refreshToken).toBe("refresh-token");
    expect(result.tokens.expiresIn).toBe(3600);
    expect(result.identity.repId).toBe("rep-7");
    expect(result.identity.book).toEqual(["acc-1", "acc-2"]);
    expect(result.identity.fullVisibility).toBe(false);
  });
});

/**
 * A token-exchange failure renders an actionable error in `SignInForm`, the component the
 * callback lands on.
 */
describe("SignInForm renders Hosted UI failures actionably", () => {
  async function loadConfig(hostedUiDomain: string): Promise<void> {
    __resetRuntimeConfigForTests();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (url === "/config.json") {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ ...config, cognitoHostedUiDomain: hostedUiDomain }),
          });
        }
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );
    await loadRuntimeConfig();
  }

  function renderSignIn() {
    vi.spyOn(AuthProviderModule, "useAuth").mockReturnValue({
      status: "signed-out",
      identity: undefined,
      signIn: vi.fn(),
      signOut: vi.fn(),
      getAccessToken: vi.fn(),
    } as unknown as ReturnType<typeof AuthProviderModule.useAuth>);
    return render(createElement(SignInForm));
  }

  /** Points the jsdom location at a Hosted UI callback URL. */
  function setLocation(search: string): void {
    window.history.replaceState({}, "", `/${search}`);
  }

  afterEach(() => {
    cleanup();
    __resetRuntimeConfigForTests();
    window.history.replaceState({}, "", "/");
  });

  it("offers single sign-on only when a Hosted UI domain is configured", async () => {
    await loadConfig("crm-example.auth.us-east-1.amazoncognito.com");
    renderSignIn();
    expect(screen.getByTestId("hosted-ui-signin")).toBeInTheDocument();

    cleanup();
    await loadConfig("");
    renderSignIn();
    expect(screen.queryByTestId("hosted-ui-signin")).not.toBeInTheDocument();
    // The password form is still fully usable.
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("renders a token-exchange failure as an alert, never a blank surface", async () => {
    await loadConfig("crm-example.auth.us-east-1.amazoncognito.com");
    const attempt = { verifier: createCodeVerifier(), state: "state-1" };
    storage.set(PKCE_STORAGE_KEY, JSON.stringify(attempt));
    setLocation("?code=auth-code&state=state-1");

    // Config is already cached, so this only serves the token exchange, which fails.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 400,
        text: () => Promise.resolve('{"error":"invalid_grant"}'),
      } as unknown as Response),
    );

    renderSignIn();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveAttribute("data-testid", "signin-error");
    expect(alert.textContent).toContain("400");
    expect(alert.textContent).toMatch(/start sign-in again/i);
    // Not a blank page: the form is still there to retry with.
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("renders a provider denial from the callback", async () => {
    await loadConfig("crm-example.auth.us-east-1.amazoncognito.com");
    setLocation("?error=access_denied&error_description=User%20cancelled");
    vi.stubGlobal("fetch", vi.fn());

    renderSignIn();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("User cancelled");
  });

  it("renders the decode reason when claims are undecodable", async () => {
    await loadConfig("crm-example.auth.us-east-1.amazoncognito.com");
    storage.set(
      PKCE_STORAGE_KEY,
      JSON.stringify({ verifier: createCodeVerifier(), state: "state-2" }),
    );
    setLocation("?code=auth-code&state=state-2");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(tokenResponse({ sub: "user-1" })));

    renderSignIn();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("custom:rep_id");
    expect(alert.textContent).toMatch(/administrator/i);
  });

  it("clears the code from the address bar so a refresh cannot replay it", async () => {
    await loadConfig("crm-example.auth.us-east-1.amazoncognito.com");
    storage.set(
      PKCE_STORAGE_KEY,
      JSON.stringify({ verifier: createCodeVerifier(), state: "state-3" }),
    );
    setLocation("?code=auth-code&state=state-3");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(tokenResponse()));

    renderSignIn();

    await waitFor(() => expect(window.location.search).toBe(""));
  });

  it("renders the password form rather than throwing when the config has not loaded", () => {
    __resetRuntimeConfigForTests();
    renderSignIn();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByTestId("hosted-ui-signin")).not.toBeInTheDocument();
  });
});

describe("error attribution", () => {
  async function loadConfigured(): Promise<void> {
    __resetRuntimeConfigForTests();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) =>
        url === "/config.json"
          ? Promise.resolve({ ok: true, json: () => Promise.resolve(config) })
          : Promise.resolve({
              ok: false,
              status: 400,
              text: () => Promise.resolve(""),
            } as unknown as Response),
      ),
    );
    await loadRuntimeConfig();
  }

  afterEach(() => {
    cleanup();
    __resetRuntimeConfigForTests();
    window.history.replaceState({}, "", "/");
  });

  /**
   * A redirect failure does not mark the password field `aria-invalid`.
   */
  it("does not blame the password field for a redirect failure", async () => {
    await loadConfigured();
    storage.set(
      PKCE_STORAGE_KEY,
      JSON.stringify({ verifier: createCodeVerifier(), state: "state-9" }),
    );
    window.history.replaceState({}, "", "/?code=c&state=state-9");

    vi.spyOn(AuthProviderModule, "useAuth").mockReturnValue({
      status: "signed-out",
      identity: undefined,
      signIn: vi.fn(),
      signOut: vi.fn(),
      getAccessToken: vi.fn(),
    } as unknown as ReturnType<typeof AuthProviderModule.useAuth>);
    render(createElement(SignInForm));

    await screen.findByRole("alert");
    const password = screen.getByLabelText("Password");
    expect(password).not.toHaveAttribute("aria-invalid");
    expect(password).not.toHaveAttribute("aria-describedby");
  });

  it("does blame the password field for a rejected credential", async () => {
    await loadConfigured();
    vi.spyOn(AuthProviderModule, "useAuth").mockReturnValue({
      status: "signed-out",
      identity: undefined,
      signIn: vi.fn().mockRejectedValue(new Error("Incorrect username or password. Try again.")),
      signOut: vi.fn(),
      getAccessToken: vi.fn(),
    } as unknown as ReturnType<typeof AuthProviderModule.useAuth>);
    render(createElement(SignInForm));

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Username"), "rep-7");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await screen.findByRole("alert");
    expect(screen.getByLabelText("Password")).toHaveAttribute("aria-invalid", "true");
  });
});
