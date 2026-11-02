// SPDX-License-Identifier: MIT
/**
 * Cognito identity: decoded custom claims stamped by the pre-token-generation V2 trigger. This
 * module only decodes what Cognito put in the token; it makes no authorization decision.
 *
 * ## Wire shape
 *
 * The claim names and value encodings here are the browser half of the contract documented in
 * `infra/modules/cognito/README.md`'s "Claim names matched" section. `src/crm/auth/
 * pre_token_generation.py` stamps four `custom:`-prefixed, **string-typed** claims (Cognito custom
 * attributes cannot be any other type): `custom:book` is comma-joined and
 * `custom:full_visibility` is the lowercase `"true"`/`"false"`. The parsing mirrors
 * `src/gateway/identity_interceptor.py`'s `_split_book` and `_truthy`.
 */
export interface Identity {
  /** Cognito `sub`. */
  readonly userId: string;
  /** Custom claim: the CRM rep id this user maps to. */
  readonly repId: string;
  /** Custom claim: explicit account ids in this rep's book. Empty when `fullVisibility` is true. */
  readonly book: readonly string[];
  /** Custom claim: true when this rep has full visibility across all accounts. */
  readonly fullVisibility: boolean;
  /** Token expiry, seconds since epoch. */
  readonly expiresAt: number;
}

/** Raw shape of the custom claims on the Cognito access token, before validation. */
export interface RawIdentityClaims {
  readonly sub?: unknown;
  readonly "custom:rep_id"?: unknown;
  readonly "custom:book"?: unknown;
  readonly "custom:full_visibility"?: unknown;
  readonly exp?: unknown;
}

/**
 * Parse a `custom:book` claim into an ordered list of account ids.
 *
 * Mirrors `_split_book` in `src/gateway/identity_interceptor.py`: the trigger emits a comma-joined
 * string, and an already-split array is accepted for symmetry with that parser.
 */
function parseBook(raw: unknown): string[] {
  if (raw === undefined || raw === null) return [];
  const parts = Array.isArray(raw) ? raw : String(raw).split(",");
  return parts.map((part) => String(part).trim()).filter((part) => part.length > 0);
}

/**
 * Interpret a custom claim as a boolean.
 *
 * Mirrors `_truthy` in `src/gateway/identity_interceptor.py`; an unrecognised value is `false`.
 */
function parseTruthy(raw: unknown): boolean {
  if (typeof raw === "boolean") return raw;
  return ["true", "1", "yes"].includes(String(raw ?? "").trim().toLowerCase());
}

/**
 * Validate and narrow decoded JWT claims into an {@link Identity}.
 *
 * Throws on a missing `sub`, `custom:rep_id`, or `exp`. `custom:book` and
 * `custom:full_visibility` are parsed fail-closed (empty book, no full visibility).
 */
export function parseIdentityClaims(claims: RawIdentityClaims): Identity {
  const sub = claims.sub;
  const repId = claims["custom:rep_id"];
  const exp = claims.exp;
  if (typeof sub !== "string" || sub.length === 0) {
    throw new Error('identity claims: missing "sub"');
  }
  if (typeof repId !== "string" || repId.length === 0) {
    throw new Error('identity claims: missing "custom:rep_id" custom claim');
  }
  if (typeof exp !== "number") {
    throw new Error('identity claims: missing "exp"');
  }
  return {
    userId: sub,
    repId,
    book: parseBook(claims["custom:book"]),
    fullVisibility: parseTruthy(claims["custom:full_visibility"]),
    expiresAt: exp,
  };
}
