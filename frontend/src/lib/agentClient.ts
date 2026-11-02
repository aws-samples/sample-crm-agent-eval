// SPDX-License-Identifier: MIT
/**
 * The one place a `/agent/*` relay request's headers and body bytes are built.
 *
 * Consumed by `hooks/useAgentStream.ts` (the turn stream) and
 * `components/AssistantPanel/useCancelTurn.ts` (the cancel).
 *
 * Two headers:
 *
 * - the Cognito token in {@link JWT_HEADER}, **not** `Authorization` — CloudFront's SigV4 occupies that;
 * - the body's SHA-256 in {@link BODY_DIGEST_HEADER}, which OAC requires the viewer to supply for a
 *   Lambda Function URL origin.
 *
 * {@link buildRelayRequest} serializes the body once and returns the exact string it hashed. Pass that
 * string to `fetch` unmodified: a digest that disagrees with the bytes sent is a CloudFront 403.
 */

/** The header the Cognito access token travels in; `Authorization` carries CloudFront's SigV4. */
export const JWT_HEADER = "X-Crm-Authorization";

/** The SigV4 payload-hash header OAC requires the viewer to supply for a Function URL origin. */
export const BODY_DIGEST_HEADER = "x-amz-content-sha256";

/** SHA-256 of the empty string — the payload hash of a request that sends no body. */
export const EMPTY_BODY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";  // pragma: allowlist secret

/** Lowercase hex SHA-256 of `payload`'s UTF-8 bytes, as SigV4 spells a payload hash. */
export async function sha256Hex(payload: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(payload));
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

export interface BuildRelayRequestOptions {
  /** The Cognito access token. Sent in {@link JWT_HEADER}, prefixed with `Bearer `. */
  readonly token: string;
  /** The response media type this call wants, e.g. `text/event-stream` or `application/json`. */
  readonly accept: string;
  /** The JSON body to post. Omit for a request that sends no body. */
  readonly body?: Record<string, unknown>;
}

export interface RelayRequest {
  readonly headers: Record<string, string>;
  /** The exact string {@link BODY_DIGEST_HEADER} was computed over. Absent when no body was given. */
  readonly body?: string;
}

/**
 * Build the headers and body bytes for one `/agent/*` request.
 *
 * Returning the serialized body alongside its digest is the contract: hand `body` straight to `fetch`.
 */
export async function buildRelayRequest(
  options: BuildRelayRequestOptions,
): Promise<RelayRequest> {
  const { token, accept, body } = options;
  if (body === undefined) {
    return {
      headers: {
        Accept: accept,
        [JWT_HEADER]: `Bearer ${token}`,
        [BODY_DIGEST_HEADER]: EMPTY_BODY_SHA256,
      },
    };
  }
  const serialized = JSON.stringify(body);
  return {
    headers: {
      "Content-Type": "application/json",
      Accept: accept,
      [JWT_HEADER]: `Bearer ${token}`,
      [BODY_DIGEST_HEADER]: await sha256Hex(serialized),
    },
    body: serialized,
  };
}
