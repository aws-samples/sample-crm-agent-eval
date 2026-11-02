// SPDX-License-Identifier: MIT
/**
 * Smoke test for the auth/config/API-client scaffolding.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import { __resetRuntimeConfigForTests, loadRuntimeConfig } from "../config/runtimeConfig";
import { CrmApiClient, CrmApiError } from "../api/CrmApiClient";

describe("runtimeConfig", () => {
  beforeEach(() => {
    __resetRuntimeConfigForTests();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads and validates /config.json", async () => {
    const body = {
      cognitoUserPoolId: "us-east-1_test",
      cognitoClientId: "client123",
      cognitoRegion: "us-east-1",
      cognitoHostedUiDomain: "",
      apiBaseUrl: "/api",
      agentBaseUrl: "/agent",
      agentRuntimeArn: "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/test",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(body) }),
    );
    const config = await loadRuntimeConfig();
    expect(config.apiBaseUrl).toBe("/api");
  });

  it("throws when a required key is missing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
    );
    await expect(loadRuntimeConfig()).rejects.toThrow(/missing or non-string key/);
  });
});

// `parseIdentityClaims` is covered by `src/auth/identity.test.ts`.

describe("CrmApiClient", () => {
  it("POSTs to {apiBaseUrl}/{tool_name} with a bearer token", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
    vi.stubGlobal("fetch", fetchMock);

    const client = new CrmApiClient("/api", { getAccessToken: () => Promise.resolve("tok-123") });
    await client.getOpportunity({ opportunity_id: "opp-1" });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/get_opportunity",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ Authorization: "Bearer tok-123" }),
      }),
    );
    vi.unstubAllGlobals();
  });

  it("normalises a 403 into a scope_denied CrmApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 403,
        json: () => Promise.resolve({ message: "not in your book", missingScope: "account:acct-1" }),
      }),
    );
    const client = new CrmApiClient("/api", { getAccessToken: () => Promise.resolve("tok") });
    await expect(client.getAccount({ account_id: "acct-1" })).rejects.toMatchObject({
      kind: "scope_denied",
      status: 403,
    } satisfies Partial<CrmApiError>);
    vi.unstubAllGlobals();
  });
});
