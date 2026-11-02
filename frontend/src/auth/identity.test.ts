// SPDX-License-Identifier: MIT
/**
 * Contract test for the browser half of the Cognito claim contract.
 *
 * The payloads here are the literal output shape of
 * `src/crm/auth/pre_token_generation.py::_claims_to_add_or_override` — `custom:`-prefixed names, a
 * comma-joined `custom:book`, and a `"true"`/`"false"` string `custom:full_visibility`. The Python
 * side is pinned by `tests/unit/test_pre_token_generation_claims.py`.
 */
import { describe, expect, it } from "vitest";

import { parseIdentityClaims } from "./identity";

/** A token payload in exactly the shape the deployed V2 trigger stamps. */
const WIRE_CLAIMS = {
  sub: "8f14e45f-ea6b-4b8f-9c3d-1a2b3c4d5e6f",
  "custom:rep_id": "rep-ashford",
  "custom:book": "demo-acct-0001,demo-acct-0009",
  "custom:full_visibility": "false",
  "custom:tenant": "demo",
  exp: 1_800_000_000,
} as const;

describe("parseIdentityClaims", () => {
  it("decodes a token payload in the trigger's real wire shape", () => {
    expect(parseIdentityClaims(WIRE_CLAIMS)).toEqual({
      userId: WIRE_CLAIMS.sub,
      repId: "rep-ashford",
      book: ["demo-acct-0001", "demo-acct-0009"],
      fullVisibility: false,
      expiresAt: WIRE_CLAIMS.exp,
    });
  });

  it('reads the string "true" as full visibility, matching _truthy', () => {
    const identity = parseIdentityClaims({ ...WIRE_CLAIMS, "custom:full_visibility": "true" });
    expect(identity.fullVisibility).toBe(true);
  });

  it("returns an empty book for the empty comma-joined claim a full-visibility rep carries", () => {
    expect(parseIdentityClaims({ ...WIRE_CLAIMS, "custom:book": "" }).book).toEqual([]);
  });

  it("trims whitespace and drops empty segments in the book claim", () => {
    const identity = parseIdentityClaims({
      ...WIRE_CLAIMS,
      "custom:book": " demo-acct-0001 , ,demo-acct-0009,",
    });
    expect(identity.book).toEqual(["demo-acct-0001", "demo-acct-0009"]);
  });

  it("fails closed on an unrecognised full_visibility value rather than widening access", () => {
    expect(parseIdentityClaims({ ...WIRE_CLAIMS, "custom:full_visibility": "TRUE-ish" })
      .fullVisibility).toBe(false);
    expect(parseIdentityClaims({ ...WIRE_CLAIMS, "custom:full_visibility": undefined })
      .fullVisibility).toBe(false);
  });

  it.each([
    ["sub", { ...WIRE_CLAIMS, sub: undefined }],
    ["custom:rep_id", { ...WIRE_CLAIMS, "custom:rep_id": "" }],
    ["exp", { ...WIRE_CLAIMS, exp: undefined }],
  ])("throws on a missing %s rather than returning a partial identity", (_name, claims) => {
    expect(() => parseIdentityClaims(claims)).toThrow(/identity claims/);
  });

  it("rejects the unprefixed claim names the trigger never emits", () => {
    // An unprefixed `rep_id` is not a valid identity; no real Cognito token carries one.
    expect(() =>
      parseIdentityClaims({
        sub: WIRE_CLAIMS.sub,
        exp: WIRE_CLAIMS.exp,
        rep_id: "rep-ashford",
      } as never),
    ).toThrow(/custom:rep_id/);
  });
});
