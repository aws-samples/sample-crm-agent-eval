// SPDX-License-Identifier: MIT
/**
 * Sign-in surface. Accessible SRP form plus the Cognito Hosted UI redirect when the deployment
 * configures a Hosted UI domain.
 *
 * Every failure path, including a failed Hosted UI redirect, renders a `role="alert"` message
 * naming what happened and what to do next.
 */
import { useEffect, useId, useState, type FormEvent, type JSX } from "react";

import { useAuth } from "./AuthProvider";
import {
  beginHostedUiSignIn,
  completeHostedUiSignIn,
  hostedUiRedirectUri,
  isHostedUiCallback,
  isHostedUiEnabled,
} from "./hostedUi";
import { getRuntimeConfig, type RuntimeConfig } from "../config/runtimeConfig";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";

/** The runtime config if it has loaded, otherwise `undefined` — never a throw during render. */
function readRuntimeConfig(): RuntimeConfig | undefined {
  try {
    return getRuntimeConfig();
  } catch {
    return undefined;
  }
}

/** Message for anything thrown by the Hosted UI helpers, which always carry an actionable one. */
function messageFor(error: unknown): string {
  return error instanceof Error && error.message.length > 0
    ? error.message
    : "Sign-in failed. Start sign-in again.";
}

export function SignInForm(): JSX.Element {
  const { signIn } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  /* `source` limits the field-level `aria-invalid` styling to password-form failures. */
  const [error, setError] = useState<{ message: string; source: "credentials" | "redirect" }>();
  const [submitting, setSubmitting] = useState(false);
  const [redirecting, setRedirecting] = useState(false);
  const [callbackSettled, setCallbackSettled] = useState(false);

  const usernameId = useId();
  const passwordId = useId();
  const errorId = useId();

  /* An unresolved config means single sign-on is not offered yet; the password form still works. */
  const config = readRuntimeConfig();
  const hostedUiAvailable = config !== undefined && isHostedUiEnabled(config);
  const completingCallback =
    hostedUiAvailable && !callbackSettled && isHostedUiCallback(window.location.href);
  const contactingSignIn = redirecting || completingCallback;

  /* Completes a Hosted UI redirect, which lands on this surface with `?code=…`. */
  useEffect(() => {
    if (config === undefined || !hostedUiAvailable) return;
    if (!isHostedUiCallback(window.location.href)) return;

    let cancelled = false;
    completeHostedUiSignIn(config, window.location.href, hostedUiRedirectUri(window.location.origin))
      .then(() => {
        // `AuthProvider` observes the established session; nothing to do here on success.
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError({ message: messageFor(caught), source: "redirect" });
      })
      .finally(() => {
        if (!cancelled) setCallbackSettled(true);
        // Drop the code/error from the address bar so a refresh cannot replay a consumed attempt.
        window.history.replaceState({}, "", window.location.pathname);
      });

    return () => {
      cancelled = true;
    };
  }, [config, hostedUiAvailable]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError(undefined);
    setSubmitting(true);
    try {
      await signIn(username, password);
    } catch (err) {
      setError({
        message: err instanceof Error ? err.message : "Sign-in failed. Please try again.",
        source: "credentials",
      });
    } finally {
      setSubmitting(false);
    }
  }

  async function handleHostedUi(): Promise<void> {
    if (config === undefined) return;
    setError(undefined);
    setRedirecting(true);
    try {
      window.location.assign(
        await beginHostedUiSignIn(config, hostedUiRedirectUri(window.location.origin)),
      );
    } catch (caught) {
      setError({ message: messageFor(caught), source: "redirect" });
      setRedirecting(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-surface-base p-6">
      <div className="w-full max-w-narrow rounded-surface border border-hairline border-border-default bg-surface-raised p-9 shadow-raised">
        <form onSubmit={handleSubmit} aria-labelledby={`${usernameId}-heading`}>
          <h1
            id={`${usernameId}-heading`}
            className="pb-8 text-xl font-semibold leading-tight tracking-tight text-text-primary"
          >
            Sign in to FirstCRM
          </h1>

          <div className="flex flex-col gap-2 pb-6">
            <label htmlFor={usernameId} className="text-xs font-medium text-text-secondary">
              Username
            </label>
            <Input
              id={usernameId}
              name="username"
              type="text"
              autoComplete="username"
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </div>

          <div className="flex flex-col gap-2 pb-6">
            <label htmlFor={passwordId} className="text-xs font-medium text-text-secondary">
              Password
            </label>
            <Input
              id={passwordId}
              name="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              aria-describedby={error?.source === "credentials" ? errorId : undefined}
              aria-invalid={error?.source === "credentials" ? true : undefined}
            />
          </div>

          {error && (
            <p
              id={errorId}
              role="alert"
              data-testid="signin-error"
              className="mb-6 rounded-control border border-hairline border-danger-border bg-danger-surface p-5 text-xs text-danger-text"
            >
              {error.message}
            </p>
          )}

          <Button type="submit" variant="primary" className="w-full" disabled={submitting}>
            {submitting ? "Signing in…" : "Sign in"}
          </Button>
        </form>

        {hostedUiAvailable && (
          <div className="pt-6">
            <Button
              type="button"
              variant="secondary"
              className="w-full"
              data-testid="hosted-ui-signin"
              disabled={contactingSignIn}
              onClick={() => void handleHostedUi()}
            >
              {contactingSignIn ? "Contacting sign-in service…" : "Sign in with single sign-on"}
            </Button>
          </div>
        )}

        <p role="status" aria-live="polite" className="sr-only">
          {contactingSignIn ? "Contacting the sign-in service" : ""}
        </p>
      </div>
    </main>
  );
}
