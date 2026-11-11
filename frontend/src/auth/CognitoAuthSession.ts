// SPDX-License-Identifier: MIT
/**
 * Cognito SRP authentication session, wrapping `amazon-cognito-identity-js`.
 *
 * Cognito is the sole identity provider for human users. This module obtains and refreshes
 * tokens via the SRP flow and talks to no other credential store.
 *
 * Callers consume the {@link AuthSession} interface via `useAuth` (`AuthProvider.tsx`), never this
 * class directly.
 */
import {
  AuthenticationDetails,
  CognitoUser,
  CognitoUserPool,
  CognitoUserSession,
} from "amazon-cognito-identity-js";
import { jwtDecode } from "jwt-decode";

import type { RuntimeConfig } from "../config/runtimeConfig";
import { parseIdentityClaims, type Identity, type RawIdentityClaims } from "./identity";

/** Thrown by {@link CognitoAuthSession.signIn} when Cognito requires an additional challenge. */
export class AuthChallengeError extends Error {
  constructor(public readonly challengeName: string) {
    super(`unsupported Cognito challenge: ${challengeName}`);
    this.name = "AuthChallengeError";
  }
}

/**
 * A live authenticated session. `getAccessToken` is the single call site every API client and
 * the agent-stream hook use to attach `Authorization: Bearer <token>`; it refreshes silently when
 * the cached session is expired.
 */
export interface AuthSession {
  /** Resolves the current caller identity, decoded from the cached ID token's claims. */
  getIdentity(): Identity;
  /** Returns a valid, non-expired Cognito ID token, refreshing first if necessary. */
  getAccessToken(): Promise<string>;
  /**
   * Signs the user out: revokes the refresh token, waiting at most {@link REVOKE_TIMEOUT_MS}, then
   * clears all cached Cognito state for this pool. Never rejects.
   */
  signOut(): Promise<void>;
}

/** The longest sign-out waits for refresh-token revocation before clearing local state anyway. */
export const REVOKE_TIMEOUT_MS = 3_000;

/** `CognitoUser.revokeTokens`, present at runtime but missing from the library's type declarations. */
interface RevocableUser {
  revokeTokens(callback: (err?: Error) => void): void;
}

export class CognitoAuthSession implements AuthSession {
  private user: CognitoUser;
  private session: CognitoUserSession;

  private constructor(user: CognitoUser, session: CognitoUserSession) {
    this.user = user;
    this.session = session;
  }

  /** SRP username/password sign-in. Rejects on any challenge Cognito may request. */
  static signIn(config: RuntimeConfig, username: string, password: string): Promise<AuthSession> {
    const pool = new CognitoUserPool({
      UserPoolId: config.cognitoUserPoolId,
      ClientId: config.cognitoClientId,
    });
    const user = new CognitoUser({ Username: username, Pool: pool });
    const authDetails = new AuthenticationDetails({ Username: username, Password: password });

    return new Promise<AuthSession>((resolve, reject) => {
      user.authenticateUser(authDetails, {
        onSuccess: (session) => resolve(new CognitoAuthSession(user, session)),
        onFailure: (err) => reject(err instanceof Error ? err : new Error(String(err))),
        newPasswordRequired: () => reject(new AuthChallengeError("NEW_PASSWORD_REQUIRED")),
        mfaRequired: () => reject(new AuthChallengeError("SMS_MFA")),
        totpRequired: () => reject(new AuthChallengeError("SOFTWARE_TOKEN_MFA")),
        customChallenge: () => reject(new AuthChallengeError("CUSTOM_CHALLENGE")),
      });
    });
  }

  /**
   * Restore a session from Cognito's own local cache (`CognitoUserPool.getCurrentUser()`), used
   * on app load so a page refresh does not force a re-login. Resolves `undefined` when no cached
   * user exists or the refresh token itself has expired.
   */
  static async restore(config: RuntimeConfig): Promise<AuthSession | undefined> {
    const pool = new CognitoUserPool({
      UserPoolId: config.cognitoUserPoolId,
      ClientId: config.cognitoClientId,
    });
    const user = pool.getCurrentUser();
    if (!user) return undefined;

    return new Promise<AuthSession | undefined>((resolve) => {
      user.getSession((err: Error | null, session: CognitoUserSession | null) => {
        if (err || !session) {
          resolve(undefined);
          return;
        }
        resolve(new CognitoAuthSession(user, session));
      });
    });
  }

  getIdentity(): Identity {
    const decoded = jwtDecode<RawIdentityClaims>(this.session.getIdToken().getJwtToken());
    return parseIdentityClaims(decoded);
  }

  /**
   * The Cognito **ID** token, despite this method's name.
   *
   * An API Gateway `COGNITO_USER_POOLS` authorizer with no authorization scopes accepts an ID token
   * and rejects an access token. Both token kinds carry the `custom:` claims `getIdentity` reads,
   * and the relay (`src/relay/src/jwt.ts`) accepts either kind.
   */
  async getAccessToken(): Promise<string> {
    if (this.session.isValid()) {
      return this.session.getIdToken().getJwtToken();
    }
    // `getSession` on the underlying CognitoUser transparently refreshes using the stored
    // refresh token when the cached session has expired.
    this.session = await new Promise<CognitoUserSession>((resolve, reject) => {
      this.user.getSession((err: Error | null, session: CognitoUserSession | null) => {
        if (err || !session) {
          reject(err ?? new Error("token refresh: no session returned"));
          return;
        }
        resolve(session);
      });
    });
    return this.session.getIdToken().getJwtToken();
  }

  /**
   * Revoke the refresh token, then clear every cached Cognito token. Revocation is bounded at
   * {@link REVOKE_TIMEOUT_MS}; a token without `origin_jti` is not revocable and makes no network
   * call. A failed or timed-out revocation is reported to the console only, and the cache is
   * cleared regardless.
   */
  async signOut(): Promise<void> {
    try {
      await this.revokeRefreshToken();
    } finally {
      this.user.signOut();
    }
  }

  /** Resolves when revocation settles or times out; reports at most one warning, never rejects. */
  private revokeRefreshToken(): Promise<void> {
    return new Promise<void>((resolve) => {
      let settled = false;
      const settle = (warning?: string): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (warning !== undefined) console.warn(warning);
        resolve();
      };
      const timer = setTimeout(
        () => settle("Refresh-token revocation did not finish in time; signed out locally"),
        REVOKE_TIMEOUT_MS,
      );
      try {
        (this.user as unknown as RevocableUser).revokeTokens((err?: Error) => {
          settle(err ? `Could not revoke the refresh token at sign-out: ${err.name}` : undefined);
        });
      } catch (err) {
        settle(
          `Could not revoke the refresh token at sign-out: ${err instanceof Error ? err.name : "unknown"}`,
        );
      }
    });
  }
}
