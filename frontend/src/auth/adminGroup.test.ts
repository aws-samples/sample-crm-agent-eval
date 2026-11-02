// SPDX-License-Identifier: MIT
/**
 * Tests for the cosmetic admin-group check: every `cognito:groups` rendering `split_groups`
 * accepts, fail-closed handling of malformed claims and tokens, and the hook's signed-out and
 * token-failure paths.
 */
import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  ADMIN_GROUP,
  GROUPS_CLAIM,
  groupsFromIdToken,
  isAdminIdToken,
  parseGroupsClaim,
  useIsAdmin,
} from "./adminGroup";

function base64Url(value: string): string {
  return btoa(value).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** An unsigned compact JWT carrying `claims`; only the payload is ever decoded here. */
function token(claims: Record<string, unknown>): string {
  return `${base64Url(JSON.stringify({ alg: "none" }))}.${base64Url(JSON.stringify(claims))}.sig`;
}

describe("parseGroupsClaim", () => {
  it.each<[string, unknown]>([
    ["a list", ["admin", "reps"]],
    ["a JSON-array string", '["admin","reps"]'],
    ["a bracketed string", "[admin, reps]"],
    ["a comma string", "admin,reps"],
  ])("reads %s", (_label, raw) => {
    expect([...parseGroupsClaim(raw)].sort()).toEqual(["admin", "reps"]);
  });

  it("drops blank entries and trims names", () => {
    expect([...parseGroupsClaim(" admin , ,reps ")].sort()).toEqual(["admin", "reps"]);
  });

  it.each<[string, unknown]>([
    ["absent", undefined],
    ["null", null],
    ["a number", 7],
    ["an object", { admin: true }],
    ["a list with a non-string member", ["admin", 1]],
    ["a JSON object string", '{"admin": true}'],
    ["a JSON array with a non-string member", '[{"admin": true}]'],
  ])("yields no groups for %s", (_label, raw) => {
    expect(parseGroupsClaim(raw).has(ADMIN_GROUP)).toBe(false);
  });
});

describe("groupsFromIdToken / isAdminIdToken", () => {
  it("reads the groups claim from the token payload", () => {
    expect(isAdminIdToken(token({ [GROUPS_CLAIM]: ["admin"] }))).toBe(true);
    expect([...groupsFromIdToken(token({ [GROUPS_CLAIM]: "reps" }))]).toEqual(["reps"]);
  });

  it("is not an admin without the claim or with another group only", () => {
    expect(isAdminIdToken(token({ sub: "u" }))).toBe(false);
    expect(isAdminIdToken(token({ [GROUPS_CLAIM]: ["administrators"] }))).toBe(false);
  });

  it("is not an admin for a token that cannot be decoded", () => {
    expect(isAdminIdToken("not-a-jwt")).toBe(false);
    expect(isAdminIdToken("")).toBe(false);
  });
});

describe("useIsAdmin", () => {
  it("becomes true once an admin token resolves", async () => {
    const auth = {
      status: "signed-in" as const,
      getAccessToken: vi.fn().mockResolvedValue(token({ [GROUPS_CLAIM]: ["admin"] })),
    };
    const { result } = renderHook(() => useIsAdmin(auth));
    expect(result.current).toBe(false);
    await waitFor(() => expect(result.current).toBe(true));
  });

  it("stays false for a non-admin token", async () => {
    const getAccessToken = vi.fn().mockResolvedValue(token({ [GROUPS_CLAIM]: ["reps"] }));
    const { result } = renderHook(() => useIsAdmin({ status: "signed-in", getAccessToken }));
    await waitFor(() => expect(getAccessToken).toHaveBeenCalled());
    expect(result.current).toBe(false);
  });

  it("stays false when the token cannot be obtained", async () => {
    const getAccessToken = vi.fn().mockRejectedValue(new Error("expired"));
    const { result } = renderHook(() => useIsAdmin({ status: "signed-in", getAccessToken }));
    await waitFor(() => expect(getAccessToken).toHaveBeenCalled());
    expect(result.current).toBe(false);
  });

  it("does not ask for a token while signed out", () => {
    const getAccessToken = vi.fn();
    const { result } = renderHook(() => useIsAdmin({ status: "signed-out", getAccessToken }));
    expect(result.current).toBe(false);
    expect(getAccessToken).not.toHaveBeenCalled();
  });
});
