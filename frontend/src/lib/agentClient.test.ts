// SPDX-License-Identifier: MIT
/**
 * Tests for `lib/agentClient.ts` — the two headers a `/agent/*` request owes under CloudFront OAC
 * and the byte-exactness of the payload digest.
 *
 * Every digest is pinned against a fixed string and an independently computed hex, rather than
 * recomputed by the assertion from the code under test.
 */
import { describe, expect, it } from "vitest";

import {
  BODY_DIGEST_HEADER,
  EMPTY_BODY_SHA256,
  JWT_HEADER,
  buildRelayRequest,
  sha256Hex,
} from "./agentClient";

/** Independently computed with `node -e 'crypto.createHash("sha256").update(s,"utf8")'`. */
const VECTORS = {
  empty: { text: "", hex: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" },  // pragma: allowlist secret
  turn: {
    text: '{"prompt":"hello","session_id":"11111111-2222-3333-4444-aaaabbbbcccc","resume_from":0}',
    hex: "1b3195fcaf93bc41604e426d977ad8a1638cc13d567798307c131c3f90a25bca",  // pragma: allowlist secret
  },
  insertionOrder: { text: '{"b":1,"a":2}', hex: "a1d46c3cdb4e5795c8d637f80daeb578ebb1a9a65dc1ed5f11f51794c3c89f3a" },  // pragma: allowlist secret
  keySorted: { text: '{"a":2,"b":1}', hex: "d3626ac30a87e6f7a6428233b3c68299976865fa5508e4267c5415c76af7a772" },  // pragma: allowlist secret
  nonAscii: { text: '{"prompt":"café ☕"}', hex: "bc50660d958fbfc690ec79484020b51b21ecb83f1c23e0a0e8948b3d5766d2fc" },  // pragma: allowlist secret
} as const;

describe("sha256Hex", () => {
  it("matches the pinned hex for the empty string", async () => {
    await expect(sha256Hex(VECTORS.empty.text)).resolves.toBe(VECTORS.empty.hex);
    expect(EMPTY_BODY_SHA256).toBe(VECTORS.empty.hex);
  });

  it("matches the pinned hex for a real turn body", async () => {
    await expect(sha256Hex(VECTORS.turn.text)).resolves.toBe(VECTORS.turn.hex);
  });

  it("hashes UTF-8 bytes, not UTF-16 code units", async () => {
    // A multi-byte body is hashed over its UTF-8 bytes.
    await expect(sha256Hex(VECTORS.nonAscii.text)).resolves.toBe(VECTORS.nonAscii.hex);
  });

  it("is 64 lowercase hex characters", async () => {
    await expect(sha256Hex("anything")).resolves.toMatch(/^[0-9a-f]{64}$/);
  });
});

describe("buildRelayRequest", () => {
  it("sends the JWT in X-Crm-Authorization and never in Authorization", async () => {
    const request = await buildRelayRequest({
      token: "tok-123",
      accept: "text/event-stream",
      body: { prompt: "hi" },
    });
    expect(request.headers[JWT_HEADER]).toBe("Bearer tok-123");
    // OAC's `signing_behavior = "always"` overwrites `Authorization` with CloudFront's SigV4.
    expect(request.headers).not.toHaveProperty("Authorization");
    expect(Object.keys(request.headers).map((k) => k.toLowerCase())).not.toContain("authorization");
  });

  it("digests the exact string it returns as the body, not a re-serialization", async () => {
    // `{b, a}` and `{a, b}` hash differently, so the serialized string is returned with the digest.
    const request = await buildRelayRequest({
      token: "t",
      accept: "application/json",
      body: { b: 1, a: 2 },
    });
    expect(request.body).toBe(VECTORS.insertionOrder.text);
    expect(request.headers[BODY_DIGEST_HEADER]).toBe(VECTORS.insertionOrder.hex);
    expect(request.headers[BODY_DIGEST_HEADER]).not.toBe(VECTORS.keySorted.hex);
    await expect(sha256Hex(request.body!)).resolves.toBe(request.headers[BODY_DIGEST_HEADER]);
  });

  it("pins the digest for a real turn body against a known vector", async () => {
    const request = await buildRelayRequest({
      token: "t",
      accept: "text/event-stream",
      body: {
        prompt: "hello",
        session_id: "11111111-2222-3333-4444-aaaabbbbcccc",
        resume_from: 0,
      },
    });
    expect(request.body).toBe(VECTORS.turn.text);
    expect(request.headers[BODY_DIGEST_HEADER]).toBe(VECTORS.turn.hex);
  });

  it("sends the empty-payload digest and no body when no body is given", async () => {
    const request = await buildRelayRequest({ token: "t", accept: "application/json" });
    expect(request.body).toBeUndefined();
    expect(request.headers[BODY_DIGEST_HEADER]).toBe(EMPTY_BODY_SHA256);
    // A bodyless request still gets a payload hash but no JSON content type.
    expect(request.headers).not.toHaveProperty("Content-Type");
    expect(request.headers[JWT_HEADER]).toBe("Bearer t");
  });

  it("sets Content-Type and the requested Accept for a body-carrying request", async () => {
    const request = await buildRelayRequest({
      token: "t",
      accept: "text/event-stream",
      body: {},
    });
    expect(request.headers["Content-Type"]).toBe("application/json");
    expect(request.headers.Accept).toBe("text/event-stream");
  });
});
