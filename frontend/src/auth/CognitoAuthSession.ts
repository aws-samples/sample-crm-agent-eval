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
  /** Signs the user out and clears all cached Cognito state for this pool. */
  signOut(): void;
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

  signOut(): void {
    this.user.signOut();
  }
}
