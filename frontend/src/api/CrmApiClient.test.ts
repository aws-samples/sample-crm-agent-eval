// SPDX-License-Identifier: MIT
/**
 * Request contracts for the client methods behind the audit-detail, rebase and user-settings
 * features: each method's route, verb, bearer token and exact body, and the response it resolves to.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { CrmApiClient, CrmApiError, USER_SETTINGS_ROUTE, type UserSettingsView } from "./CrmApiClient";

const SETTINGS: UserSettingsView = {
  tuning: { schema: 2, source: "settings.tuning", tiers: {} },
  is_default: true,
  notice: null,
  updated_at: null,
};

interface Captured {
  readonly url: string;
  readonly method: string;
  readonly authorization: string;
  readonly body: unknown;
}

function stubFetch(status: number, responseBody: unknown): Captured[] {
  const calls: Captured[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init: RequestInit) => {
      const headers = init.headers as Record<string, string>;
      calls.push({
        url,
        method: init.method ?? "GET",
        authorization: headers.Authorization,
        body: JSON.parse(init.body as string) as unknown,
      });
      return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        json: () => Promise.resolve(responseBody),
      } as unknown as Response);
    }),
  );
  return calls;
}

function client(): CrmApiClient {
  return new CrmApiClient("/api", { getAccessToken: () => Promise.resolve("tok-1") });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("getAuditRecord", () => {
  it("posts the audit id to the get_audit_record tool route", async () => {
    const calls = stubFetch(200, { audit_record: { audit_id: "aud-1", action: "update" } });
    const result = await client().getAuditRecord({ audit_id: "aud-1" });

    expect(calls).toEqual([
      {
        url: "/api/get_audit_record",
        method: "POST",
        authorization: "Bearer tok-1",
        body: { audit_id: "aud-1" },
      },
    ]);
    expect(result).toEqual({ audit_record: { audit_id: "aud-1", action: "update" } });
  });

  it("rejects an out-of-book record as scope_denied", async () => {
    stubFetch(403, {
      error: { code: "scope_denied", message: "No access.", missing_scope: "account:acct-2" },
    });
    const caught = (await client()
      .getAuditRecord({ audit_id: "aud-2" })
      .catch((err: unknown) => err)) as CrmApiError;

    expect(caught).toBeInstanceOf(CrmApiError);
    expect(caught.kind).toBe("scope_denied");
    expect(caught.body.missingScope).toBe("account:acct-2");
  });
});

describe("rebaseProposal", () => {
  it("posts the proposal id and its transaction key to the rebase_proposal tool route", async () => {
    const calls = stubFetch(200, { proposal: { proposal_id: "prop-1", status: "pending" } });
    await client().rebaseProposal({ proposal_id: "prop-1", transaction_key: "tx-rebase-1" });

    expect(calls).toEqual([
      {
        url: "/api/rebase_proposal",
        method: "POST",
        authorization: "Bearer tok-1",
        body: { proposal_id: "prop-1", transaction_key: "tx-rebase-1" },
      },
    ]);
  });

  it("surfaces an already-decided refusal as a conflict carrying its reason", async () => {
    stubFetch(409, {
      error: { code: "conflict", message: "Already decided.", reason: "already_decided" },
    });
    const caught = (await client()
      .rebaseProposal({ proposal_id: "prop-1", transaction_key: "tx" })
      .catch((err: unknown) => err)) as CrmApiError;

    expect(caught.kind).toBe("conflict");
    expect(caught.body.reason).toBe("already_decided");
    expect(caught.body.conflictingFields).toBeUndefined();
  });
});

describe("user settings", () => {
  it("is served by the user_settings route", () => {
    expect(USER_SETTINGS_ROUTE).toBe("user_settings");
  });

  it.each([
    ["getUserSettings", () => client().getUserSettings(), { action: "get" }],
    [
      "saveUserSettings",
      () => client().saveUserSettings({ tuning: SETTINGS.tuning }),
      { action: "put", tuning: SETTINGS.tuning },
    ],
    ["resetUserSettings", () => client().resetUserSettings(), { action: "reset" }],
  ] as const)("%s posts exactly its action and no rep_id", async (_name, call, expectedBody) => {
    const calls = stubFetch(200, SETTINGS);
    const view = await call();

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/api/user_settings");
    expect(calls[0].method).toBe("POST");
    expect(calls[0].authorization).toBe("Bearer tok-1");
    expect(calls[0].body).toEqual(expectedBody);
    expect(calls[0].body).not.toHaveProperty("rep_id");
    expect(view).toEqual(SETTINGS);
  });

  it.each(["saved_model_unavailable", "saved_settings_unreadable"] as const)(
    "resolves a get carrying the %s notice unchanged",
    async (notice) => {
      const withNotice: UserSettingsView = { ...SETTINGS, notice };
      stubFetch(200, withNotice);
      expect((await client().getUserSettings()).notice).toBe(notice);
    },
  );

  it("surfaces a rejected save's source so the UI can show it at the control", async () => {
    stubFetch(400, {
      error: {
        code: "validation_failed",
        message: "Unknown model 'x' for the tool tier.",
        source: "tuning.tiers.tool.model",
      },
    });
    const caught = (await client()
      .saveUserSettings({ tuning: SETTINGS.tuning })
      .catch((err: unknown) => err)) as CrmApiError;

    expect(caught.kind).toBe("validation_failed");
    expect(caught.body.source).toBe("tuning.tiers.tool.model");
    expect(caught.message).toBe("Unknown model 'x' for the tool tier.");
  });
});
