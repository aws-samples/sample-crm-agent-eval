// SPDX-License-Identifier: MIT
/**
 * Admin-group membership read from the Cognito ID token's `cognito:groups` claim.
 *
 * Cosmetic only: it decides which controls the UI shows. The server makes the authorization
 * decision (`src/crm/authz.py` `Identity.is_admin`), so a wrong answer here can hide or show a
 * button but never grants an action. Every malformed input resolves to "not an admin".
 *
 * The claim renderings mirror `split_groups` in `src/crm/envelope.py`: a list, a JSON-array string,
 * a bracketed string (`"[a, b]"`), a comma string, or absent.
 */
import { jwtDecode } from "jwt-decode";
import { useEffect, useState } from "react";

import type { AuthContextValue } from "./AuthProvider";

/** The group whose members see admin controls. Mirrors `crm.authz.ADMIN_GROUP`. */
export const ADMIN_GROUP = "admin";

/** The ID-token claim carrying the user's Cognito groups. Mirrors `crm.envelope.CLAIM_GROUPS`. */
export const GROUPS_CLAIM = "cognito:groups";

const NO_GROUPS: ReadonlySet<string> = new Set();

function namesFrom(values: readonly unknown[]): ReadonlySet<string> {
  if (!values.every((value) => typeof value === "string")) return NO_GROUPS;
  return new Set(
    (values as readonly string[]).map((value) => value.trim()).filter((value) => value.length > 0),
  );
}

/**
 * Parse a `cognito:groups` claim value into a set of group names.
 *
 * @param raw - The claim as decoded from the token. An unsupported type or a non-string member
 *   yields the empty set (the server refuses such a token outright).
 */
export function parseGroupsClaim(raw: unknown): ReadonlySet<string> {
  if (raw === undefined || raw === null) return NO_GROUPS;
  if (Array.isArray(raw)) return namesFrom(raw);
  if (typeof raw !== "string") return NO_GROUPS;
  let text = raw.trim();
  if (text.startsWith("[") && text.endsWith("]")) {
    try {
      const decoded: unknown = JSON.parse(text);
      return Array.isArray(decoded) ? namesFrom(decoded) : NO_GROUPS;
    } catch {
      text = text.slice(1, -1);
    }
  }
  return namesFrom(text.split(","));
}

/**
 * The groups named by an ID token, or the empty set when the token cannot be decoded.
 *
 * @param idToken - A compact JWT. Its signature is not checked; the server verifies the token.
 */
export function groupsFromIdToken(idToken: string): ReadonlySet<string> {
  try {
    const claims = jwtDecode<Record<string, unknown>>(idToken);
    return parseGroupsClaim(claims[GROUPS_CLAIM]);
  } catch {
    return NO_GROUPS;
  }
}

/** Whether the ID token names {@link ADMIN_GROUP}. */
export function isAdminIdToken(idToken: string): boolean {
  return groupsFromIdToken(idToken).has(ADMIN_GROUP);
}

/**
 * Whether the signed-in user is in the admin group, for showing admin controls.
 *
 * `false` until the token resolves, when signed out, and when the token cannot be read.
 * `AuthContextValue.getAccessToken` returns the ID token (`CognitoAuthSession.getAccessToken`).
 */
export function useIsAdmin(auth: Pick<AuthContextValue, "status" | "getAccessToken">): boolean {
  const [isAdmin, setIsAdmin] = useState(false);
  const { status, getAccessToken } = auth;
  const [observedStatus, setObservedStatus] = useState(status);
  if (observedStatus !== status) {
    setObservedStatus(status);
    setIsAdmin(false);
  }

  useEffect(() => {
    if (status !== "signed-in") return;
    let active = true;
    getAccessToken().then(
      (token) => {
        if (active) setIsAdmin(isAdminIdToken(token));
      },
      () => {
        if (active) setIsAdmin(false);
      },
    );
    return () => {
      active = false;
    };
  }, [status, getAccessToken]);

  return isAdmin;
}
