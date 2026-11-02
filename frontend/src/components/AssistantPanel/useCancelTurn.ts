// SPDX-License-Identifier: MIT
/**
 * Cooperative cancellation for an in-flight turn.
 *
 * The cancel is posted to the same endpoint a turn goes to, with a `{"mode": "cancel"}` body, as
 * its own request; closing the SSE stream does not stop a turn.
 *
 * - All three dispositions are HTTP 200 and all are success; `already_terminal` is a no-op.
 * - This hook never emits an agent event; the transcript is written only by `useAgentStream`.
 * - It does not use `useAgentStream`'s reconnection path, so a 401 or 403 cannot drive a retry.
 *
 * Headers come from `lib/agentClient.ts`, the same builder the turn stream uses: the JWT in
 * `X-Crm-Authorization` and the body's SHA-256 in `x-amz-content-sha256`.
 */

import { useCallback, useRef, useState } from "react";

import type { AuthContextValue } from "../../auth/AuthProvider";
import { buildRelayRequest, type RelayRequest } from "../../lib/agentClient";

/**
 * The payload field and value the Runtime dispatches on. Only the exact value cancels. Must equal
 * the relay's and the Runtime's constants.
 */
export const CANCEL_MODE_KEY = "mode";
export const CANCEL_MODE = "cancel";

/** The Runtime's answer, all three of which are success. */
export type CancelDisposition = "accepted" | "already_requested" | "already_terminal";

/**
 * Why a cancel did not land. `forbidden` is any 403: a session the caller does not own, or a
 * request refused before it reached the relay. It is not worded as a sign-in problem.
 */
export type CancelFailureReason = "unauthenticated" | "forbidden" | "rejected" | "transport";

export type CancelState = "idle" | "requesting" | "requested" | "failed";

export interface UseCancelTurnOptions {
  /** CloudFront-relative path prefix for the relay, e.g. `/agent`. Same value `useAgentStream` uses. */
  readonly agentBaseUrl: string;
  /** URL-encoded AgentCore Runtime ARN forming the invocation path. */
  readonly agentRuntimeArn: string;
  /** Bound `getAccessToken` from `useAuth()`, injected so this hook needs no real `AuthProvider`. */
  readonly getAccessToken: AuthContextValue["getAccessToken"];
  /** Injectable for tests; defaults to the global `fetch`. */
  readonly fetchImpl?: typeof fetch;
}

export interface UseCancelTurnResult {
  readonly cancelState: CancelState;
  /** Set once the Runtime answered. `already_terminal` is a successful no-op, not a failure. */
  readonly disposition: CancelDisposition | undefined;
  /** Set only when `cancelState === "failed"`. */
  readonly failureReason: CancelFailureReason | undefined;
  /**
   * Ask the Runtime to stop `sessionId`.
   *
   * Resolves rather than rejecting on a refused cancel; the outcome is reported through
   * `cancelState`/`failureReason`. Returns the disposition when one was reached, `undefined`
   * otherwise — which also covers a call made while a cancel was already in flight, so read
   * `cancelState`, not this return value.
   */
  cancelTurn(sessionId: string): Promise<CancelDisposition | undefined>;
  /** Return to `idle`, so a later turn starts from a clean state. */
  reset(): void;
}

const DISPOSITIONS: readonly string[] = ["accepted", "already_requested", "already_terminal"];

function isDisposition(value: unknown): value is CancelDisposition {
  return typeof value === "string" && DISPOSITIONS.includes(value);
}

export function useCancelTurn(options: UseCancelTurnOptions): UseCancelTurnResult {
  const { agentBaseUrl, agentRuntimeArn, getAccessToken, fetchImpl } = options;
  const [cancelState, setCancelState] = useState<CancelState>("idle");
  const [disposition, setDisposition] = useState<CancelDisposition | undefined>(undefined);
  const [failureReason, setFailureReason] = useState<CancelFailureReason | undefined>(undefined);

  /*
   * Guards a second click while the first request is still open, so two requests cannot race to set
   * the state.
   */
  const inFlightRef = useRef(false);

  const reset = useCallback(() => {
    setCancelState("idle");
    setDisposition(undefined);
    setFailureReason(undefined);
  }, []);

  const cancelTurn = useCallback(
    async (sessionId: string): Promise<CancelDisposition | undefined> => {
      if (sessionId.trim() === "") {
        // Nothing to cancel.
        setFailureReason("rejected");
        setCancelState("failed");
        return undefined;
      }
      if (inFlightRef.current) {
        return undefined;
      }
      inFlightRef.current = true;
      setCancelState("requesting");
      setFailureReason(undefined);

      try {
        let token: string;
        try {
          token = await getAccessToken();
        } catch {
          // No usable token: the same state a 401 produces.
          setFailureReason("unauthenticated");
          setCancelState("failed");
          return undefined;
        }

          // Serialized and hashed in one place, so the digest matches the bytes sent.
        let request: RelayRequest;
        try {
          request = await buildRelayRequest({
            token,
            accept: "application/json",
            body: { [CANCEL_MODE_KEY]: CANCEL_MODE, session_id: sessionId },
          });
        } catch (err) {
          // `crypto.subtle` is absent outside a secure context, so the body cannot be hashed; fail
          // closed and log.
          console.error("Could not build the cancel request", err);
          setFailureReason("transport");
          setCancelState("failed");
          return undefined;
        }

        const doFetch = fetchImpl ?? fetch;
        let response: Response;
        try {
          response = await doFetch(`${agentBaseUrl}/${encodeURIComponent(agentRuntimeArn)}`, {
            method: "POST",
            headers: request.headers,
            body: request.body,
          });
        } catch {
          // No retry.
          setFailureReason("transport");
          setCancelState("failed");
          return undefined;
        }

        if (response.status === 401) {
          setFailureReason("unauthenticated");
          setCancelState("failed");
          return undefined;
        }
        if (response.status === 403) {
          setFailureReason("forbidden");
          setCancelState("failed");
          return undefined;
        }
        if (!response.ok) {
          setFailureReason("rejected");
          setCancelState("failed");
          return undefined;
        }

        let body: unknown;
        try {
          body = await response.json();
        } catch {
          // A 200 with an unreadable body is not reported as a landed cancel.
          setFailureReason("transport");
          setCancelState("failed");
          return undefined;
        }

        const reported = (body as { disposition?: unknown } | null)?.disposition;
        if (!isDisposition(reported)) {
          setFailureReason("transport");
          setCancelState("failed");
          return undefined;
        }

        setDisposition(reported);
        setCancelState("requested");
        return reported;
      } finally {
        inFlightRef.current = false;
      }
    },
    [agentBaseUrl, agentRuntimeArn, getAccessToken, fetchImpl],
  );

  return { cancelState, disposition, failureReason, cancelTurn, reset };
}
