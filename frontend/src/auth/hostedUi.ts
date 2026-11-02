// SPDX-License-Identifier: MIT
/**
 * Cognito Hosted UI sign-in over the PKCE authorization-code flow.
 *
 * Inactive when `runtimeConfig.cognitoHostedUiDomain` is the empty string — `AuthProvider` then uses
 * SRP exclusively and nothing calls these helpers.
 *
 * The flow is three calls across a full-page redirect:
 *   `beginHostedUiSignIn` → (browser leaves) → `completeHostedUiSignIn` on the callback URL.
 *
 * - A verifier is generated per attempt and consumed exactly once: `takePkceAttempt` deletes it
 *   before it returns, so a replayed callback URL cannot reuse it.
 * - Every failure carries an actionable message; a caller renders `error.message` directly.
 */

import type { RuntimeConfig } from "../config/runtimeConfig";
import { parseIdentityClaims, type Identity, type RawIdentityClaims } from "./identity";

/** Why a Hosted UI sign-in failed. Distinct cases so a caller can tell the user what to do next. */
export type HostedUiFailure =
  | "not_configured"
  | "provider_denied"
  | "state_mismatch"
  | "attempt_missing"
  | "no_code"
  | "exchange_failed"
  | "exchange_unreachable"
  | "malformed_token_response"
  | "claims_undecodable";

/**
 * A Hosted UI failure with a message written for the person looking at the screen.
 *
 * `cause` keeps the underlying detail for the console without putting it on screen.
 */
export class HostedUiError extends Error {
  readonly failure: HostedUiFailure;
  /** Declared explicitly: the build targets ES2020, whose `Error` has no `cause`. */
  readonly cause: unknown;

  constructor(failure: HostedUiFailure, message: string, options?: { cause?: unknown }) {
    super(message);
    this.name = "HostedUiError";
    this.failure = failure;
    this.cause = options?.cause;
  }
}

export function isHostedUiEnabled(config: RuntimeConfig): boolean {
  return config.cognitoHostedUiDomain.length > 0;
}

/* ------------------------------------------------------------------ PKCE primitives */

/** RFC 7636 unreserved characters. */
const VERIFIER_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~";

/** RFC 7636 permits 43–128 characters; 64 is comfortably inside it. */
const VERIFIER_LENGTH = 64;

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * A fresh high-entropy verifier from `crypto.getRandomValues`.
 */
export function createCodeVerifier(): string {
  const bytes = new Uint8Array(VERIFIER_LENGTH);
  crypto.getRandomValues(bytes);
  let verifier = "";
  for (const byte of bytes) verifier += VERIFIER_ALPHABET[byte % VERIFIER_ALPHABET.length];
  return verifier;
}

/** The S256 challenge for a verifier. `plain` is not supported. */
export async function createCodeChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64UrlEncode(new Uint8Array(digest));
}

/** An opaque value tying a callback back to the attempt that started it (CSRF defence). */
export function createState(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return base64UrlEncode(bytes);
}

/* --------------------------------------------------- per-attempt storage (one use) */

/** The in-flight attempt, held across the redirect. */
export interface PkceAttempt {
  readonly verifier: string;
  readonly state: string;
}

export const PKCE_STORAGE_KEY = "crm.pkce";

/**
 * Stored in `sessionStorage`, scoped to the tab that started the attempt. Access is guarded; it
 * throws in a privacy-restricted browser.
 */
function sessionStore(): Storage | undefined {
  try {
    return globalThis.sessionStorage ?? undefined;
  } catch {
    return undefined;
  }
}

function storeAttempt(attempt: PkceAttempt): void {
  try {
    sessionStore()?.setItem(PKCE_STORAGE_KEY, JSON.stringify(attempt));
  } catch {
    // Unavailable storage surfaces later as `attempt_missing`.
  }
}

/**
 * Reads and **deletes** the stored attempt, so a verifier is never available to a second exchange.
 * The delete happens even when the stored value is unparseable.
 */
export function takePkceAttempt(): PkceAttempt | undefined {
  const store = sessionStore();
  if (store === undefined) return undefined;

  let raw: string | null;
  try {
    raw = store.getItem(PKCE_STORAGE_KEY);
    store.removeItem(PKCE_STORAGE_KEY);
  } catch {
    return undefined;
  }
  if (raw === null) return undefined;

  try {
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      typeof (parsed as PkceAttempt).verifier === "string" &&
      typeof (parsed as PkceAttempt).state === "string"
    ) {
      return parsed as PkceAttempt;
    }
    return undefined;
  } catch {
    return undefined;
  }
}

/* ------------------------------------------------------------------------ step one */

/** Where Cognito returns the user; shared by begin and complete. */
export function hostedUiRedirectUri(origin: string): string {
  return `${origin}/`;
}

/**
 * Starts an attempt: mints a verifier and state, stores them for the callback, and returns the
 * Hosted UI URL to navigate to. Every call mints fresh values — never reuse across attempts.
 */
export async function beginHostedUiSignIn(
  config: RuntimeConfig,
  redirectUri: string,
): Promise<string> {
  if (!isHostedUiEnabled(config)) {
    throw new HostedUiError(
      "not_configured",
      "Hosted sign-in is not configured for this deployment. Use your username and password.",
    );
  }

  const verifier = createCodeVerifier();
  const state = createState();
  storeAttempt({ verifier, state });

  const params = new URLSearchParams({
    client_id: config.cognitoClientId,
    response_type: "code",
    scope: "openid email profile",
    redirect_uri: redirectUri,
    code_challenge_method: "S256",
    code_challenge: await createCodeChallenge(verifier),
    state,
  });
  return `https://${config.cognitoHostedUiDomain}/login?${params.toString()}`;
}

/* ------------------------------------------------------------------------ step two */

/** Extract the `code` query parameter from a Hosted UI redirect callback URL, if present. */
export function extractAuthorizationCode(callbackUrl: string): string | undefined {
  return new URL(callbackUrl).searchParams.get("code") ?? undefined;
}

/** True when the URL carries either a `code` or an `error` from the Hosted UI. */
export function isHostedUiCallback(callbackUrl: string): boolean {
  const params = new URL(callbackUrl).searchParams;
  return params.has("code") || params.has("error");
}

/** The token endpoint response fields this app uses. */
export interface HostedUiTokens {
  readonly idToken: string;
  readonly accessToken: string;
  readonly refreshToken: string | undefined;
  readonly expiresIn: number;
}

function decodeJwtClaims(idToken: string): RawIdentityClaims {
  const segments = idToken.split(".");
  if (segments.length !== 3) throw new Error("id token is not a three-part JWT");
  const payload = segments[1].replace(/-/g, "+").replace(/_/g, "/");
  return JSON.parse(atob(payload.padEnd(Math.ceil(payload.length / 4) * 4, "="))) as RawIdentityClaims;
}

/**
 * Exchanges an authorization code for tokens.
 *
 * The client is public and holds no secret; the verifier proves this is the client that started
 * the flow. A non-2xx response is a `HostedUiError`.
 */
export async function exchangeCodeForTokens(
  config: RuntimeConfig,
  code: string,
  redirectUri: string,
  verifier: string,
): Promise<HostedUiTokens> {
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: config.cognitoClientId,
    code,
    redirect_uri: redirectUri,
    code_verifier: verifier,
  });

  let response: Response;
  try {
    response = await fetch(`https://${config.cognitoHostedUiDomain}/oauth2/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
  } catch (cause) {
    throw new HostedUiError(
      "exchange_unreachable",
      "Could not reach the sign-in service. Check your connection and try signing in again.",
      { cause },
    );
  }

  if (!response.ok) {
    throw new HostedUiError(
      "exchange_failed",
      `Sign-in could not be completed (the sign-in service returned ${response.status}). Start sign-in again.`,
      { cause: await response.text().catch(() => undefined) },
    );
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch (cause) {
    throw new HostedUiError(
      "malformed_token_response",
      "The sign-in service returned a response this app could not read. Start sign-in again.",
      { cause },
    );
  }

  /* `JSON.parse("null")` succeeds, so a literal `null` body is checked separately. */
  const record = (typeof payload === "object" && payload !== null ? payload : {}) as Record<
    string,
    unknown
  >;
  if (typeof record.id_token !== "string" || typeof record.access_token !== "string") {
    throw new HostedUiError(
      "malformed_token_response",
      "The sign-in service did not return the expected tokens. Start sign-in again.",
      { cause: Object.keys(record).join(", ") },
    );
  }

  return {
    idToken: record.id_token,
    accessToken: record.access_token,
    refreshToken: typeof record.refresh_token === "string" ? record.refresh_token : undefined,
    expiresIn: typeof record.expires_in === "number" ? record.expires_in : 0,
  };
}

export interface HostedUiSignInResult {
  readonly tokens: HostedUiTokens;
  readonly identity: Identity;
}

/**
 * Completes an attempt from the callback URL: rejects a provider error or a mismatched `state`,
 * consumes the one-use verifier, exchanges the code, and decodes the identity.
 *
 * A successful exchange whose claims cannot be decoded is an authentication failure carrying the
 * decode reason.
 */
export async function completeHostedUiSignIn(
  config: RuntimeConfig,
  callbackUrl: string,
  redirectUri: string,
): Promise<HostedUiSignInResult> {
  const params = new URL(callbackUrl).searchParams;

  const providerError = params.get("error");
  if (providerError !== null) {
    const description = params.get("error_description");
    throw new HostedUiError(
      "provider_denied",
      description !== null && description.length > 0
        ? `Sign-in was not completed: ${description}. Try again.`
        : `Sign-in was not completed (${providerError}). Try again.`,
    );
  }

  const code = params.get("code");
  if (code === null || code.length === 0) {
    throw new HostedUiError(
      "no_code",
      "The sign-in response was missing its authorization code. Start sign-in again.",
    );
  }

  // Consumed before the exchange, and before the state check, so a replayed callback cannot reuse it.
  const attempt = takePkceAttempt();
  if (attempt === undefined) {
    throw new HostedUiError(
      "attempt_missing",
      "This sign-in link has already been used or was opened in a different browser session. Start sign-in again.",
    );
  }

  const returnedState = params.get("state");
  if (returnedState !== attempt.state) {
    throw new HostedUiError(
      "state_mismatch",
      "The sign-in response did not match the request this browser started. Start sign-in again.",
    );
  }

  const tokens = await exchangeCodeForTokens(config, code, redirectUri, attempt.verifier);

  let identity: Identity;
  try {
    identity = parseIdentityClaims(decodeJwtClaims(tokens.idToken));
  } catch (cause) {
    throw new HostedUiError(
      "claims_undecodable",
      `Signed in, but this account is missing information the app needs: ${
        cause instanceof Error ? cause.message : String(cause)
      }. Ask an administrator to check your account.`,
      { cause },
    );
  }

  return { tokens, identity };
}
