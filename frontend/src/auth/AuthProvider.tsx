// SPDX-License-Identifier: MIT
/**
 * React auth context. Every page and hook that needs the current identity or an access token
 * consumes this via {@link useAuth} — never `CognitoAuthSession` directly.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { JSX, ReactNode } from "react";

import { resetUserSettingsState } from "../components/tuning/sessionTuning";
import { loadRuntimeConfig, type RuntimeConfig } from "../config/runtimeConfig";
import { CognitoAuthSession, type AuthSession } from "./CognitoAuthSession";
import { endHostedUiSession } from "./hostedUi";
import type { Identity } from "./identity";

export type AuthStatus = "loading" | "signed-out" | "signed-in";

export interface AuthContextValue {
  readonly status: AuthStatus;
  /** Present only when `status === "signed-in"`. */
  readonly identity: Identity | undefined;
  signIn(username: string, password: string): Promise<void>;
  /**
   * Revokes and clears the session and resets saved settings, then, when Hosted UI is configured,
   * navigates to its `/logout` so the Cognito domain session ends too. Repeat calls while one is in
   * flight are ignored.
   */
  signOut(): void;
  /** Resolves a fresh, non-expired access token. Throws if not signed in. */
  getAccessToken(): Promise<string>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }): JSX.Element {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [session, setSession] = useState<AuthSession | undefined>(undefined);
  const [identity, setIdentity] = useState<Identity | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const config = await loadRuntimeConfig();
      const restored = await CognitoAuthSession.restore(config);
      if (cancelled) return;
      if (restored) {
        setSession(restored);
        setIdentity(restored.getIdentity());
        setStatus("signed-in");
      } else {
        setStatus("signed-out");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const signIn = useCallback(async (username: string, password: string) => {
    const config = await loadRuntimeConfig();
    const newSession = await CognitoAuthSession.signIn(config, username, password);
    setSession(newSession);
    setIdentity(newSession.getIdentity());
    setStatus("signed-in");
  }, []);

  const signingOut = useRef(false);
  const signOut = useCallback(() => {
    if (signingOut.current) return;
    signingOut.current = true;
    void (async () => {
      try {
        await session?.signOut();
      } catch (err) {
        console.warn("Sign-out did not finish cleanly", err instanceof Error ? err.name : String(err));
      }
      resetUserSettingsState();
      setSession(undefined);
      setIdentity(undefined);
      setStatus("signed-out");
      signingOut.current = false;

      let config: RuntimeConfig;
      try {
        config = await loadRuntimeConfig();
      } catch (err) {
        console.warn(
          "Signed out locally; could not load the runtime config to end the Hosted UI session",
          err instanceof Error ? err.message : String(err),
        );
        return;
      }
      endHostedUiSession(config);
    })();
  }, [session]);

  const getAccessToken = useCallback(async () => {
    if (!session) {
      throw new Error("useAuth().getAccessToken(): not signed in");
    }
    return session.getAccessToken();
  }, [session]);

  const value = useMemo<AuthContextValue>(
    () => ({ status, identity, signIn, signOut, getAccessToken }),
    [status, identity, signIn, signOut, getAccessToken],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** The single hook every page/component uses to reach identity and tokens. */
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth() must be called within an <AuthProvider>");
  }
  return ctx;
}
