// SPDX-License-Identifier: MIT
/**
 * Tests for the auth context's sign-out: it clears the saved-settings store, so the next user to
 * sign in on the same page load fetches their own settings and never sees the previous user's; and
 * it waits for the session's revocation before clearing local state and ending the Hosted UI
 * session.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { UserSettingsView } from "../api/CrmApiClient";
import {
  EMPTY_TIER_SELECTION,
  __resetSessionTuningForTests,
  getSessionTuning,
  loadUserSettings,
  resetUserSettings,
  resetUserSettingsState,
  saveUserSettings,
  useSettingsStatus,
  type SettingsClient,
} from "../components/tuning/sessionTuning";
import { AuthProvider, useAuth, type AuthContextValue } from "./AuthProvider";
import type { AuthSession } from "./CognitoAuthSession";
import type { Identity } from "./identity";

const { signInMock, restoreMock, loadRuntimeConfigMock, endHostedUiSessionMock } = vi.hoisted(
  () => ({
    signInMock: vi.fn(),
    restoreMock: vi.fn(),
    loadRuntimeConfigMock: vi.fn(),
    endHostedUiSessionMock: vi.fn(),
  }),
);

const RUNTIME_CONFIG = { cognitoHostedUiDomain: "crm-example.auth.us-east-1.amazoncognito.com" };

vi.mock("../config/runtimeConfig", () => ({
  loadRuntimeConfig: loadRuntimeConfigMock,
}));

vi.mock("./hostedUi", () => ({
  endHostedUiSession: endHostedUiSessionMock,
}));

vi.mock("./CognitoAuthSession", () => ({
  CognitoAuthSession: { signIn: signInMock, restore: restoreMock },
}));

function fakeSession(userId: string): AuthSession {
  const identity: Identity = {
    userId,
    repId: `rep-${userId}`,
    book: [],
    fullVisibility: false,
    expiresAt: Number.MAX_SAFE_INTEGER,
  };
  return {
    getIdentity: () => identity,
    getAccessToken: () => Promise.resolve(`token-${userId}`),
    signOut: vi.fn(() => Promise.resolve()),
  };
}

function viewFor(model: string): UserSettingsView {
  return {
    tuning: {
      schema: 3,
      source: "settings.tuning",
      tiers: { supervisor: { family: "anthropic", model, effort: "low" } },
    },
    is_default: false,
    notice: null,
    updated_at: "2026-10-08T00:00:00Z",
  };
}

function settingsClient(
  getUserSettings: () => Promise<UserSettingsView>,
): SettingsClient & { getUserSettings: ReturnType<typeof vi.fn> } {
  return {
    getUserSettings: vi.fn(getUserSettings),
    saveUserSettings: vi.fn(),
    resetUserSettings: vi.fn(),
  } as never;
}

async function renderAuth(): Promise<{ current: () => AuthContextValue }> {
  const { result } = renderHook(() => useAuth(), { wrapper: AuthProvider });
  await act(async () => {});
  return { current: () => result.current };
}

async function signInAs(auth: { current: () => AuthContextValue }, userId: string): Promise<void> {
  signInMock.mockResolvedValueOnce(fakeSession(userId));
  await act(async () => {
    await auth.current().signIn(userId, "pw");
  });
}

/** Sign out and let the asynchronous revoke → clear → redirect sequence finish. */
async function signOutAndSettle(auth: { current: () => AuthContextValue }): Promise<void> {
  await act(async () => {
    auth.current().signOut();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

beforeEach(() => {
  __resetSessionTuningForTests();
  signInMock.mockReset();
  restoreMock.mockReset().mockResolvedValue(undefined);
  loadRuntimeConfigMock.mockReset().mockResolvedValue(RUNTIME_CONFIG);
  endHostedUiSessionMock.mockReset().mockReturnValue(true);
});

afterEach(() => {
  __resetSessionTuningForTests();
});

describe("AuthProvider sign-out", () => {
  it("makes the next user's settings load fetch afresh instead of reusing the previous user's", async () => {
    const auth = await renderAuth();
    const clientA = settingsClient(() => Promise.resolve(viewFor("model-of-a")));
    const clientB = settingsClient(() => Promise.resolve(viewFor("model-of-b")));

    await signInAs(auth, "user-a");
    await act(async () => {
      await loadUserSettings(clientA);
    });
    expect(getSessionTuning().tiers.supervisor.model).toBe("model-of-a");

    await signOutAndSettle(auth);
    expect(auth.current().status).toBe("signed-out");
    expect(getSessionTuning().tiers.supervisor).toEqual(EMPTY_TIER_SELECTION);

    await signInAs(auth, "user-b");
    await act(async () => {
      await loadUserSettings(clientB);
    });

    expect(clientB.getUserSettings).toHaveBeenCalledTimes(1);
    expect(getSessionTuning().tiers.supervisor.model).toBe("model-of-b");
  });

  it("calls the session's own sign-out", async () => {
    const auth = await renderAuth();
    const session = fakeSession("user-a");
    signInMock.mockResolvedValueOnce(session);
    await act(async () => {
      await auth.current().signIn("user-a", "pw");
    });

    await signOutAndSettle(auth);

    expect(session.signOut).toHaveBeenCalledTimes(1);
  });

  it("waits for the session's revocation before clearing state, then ends the Hosted UI session", async () => {
    const auth = await renderAuth();
    const revoked = deferred<undefined>();
    const session = { ...fakeSession("user-a"), signOut: vi.fn(() => revoked.promise) };
    signInMock.mockResolvedValueOnce(session);
    await act(async () => {
      await auth.current().signIn("user-a", "pw");
    });
    await act(async () => {
      await loadUserSettings(settingsClient(() => Promise.resolve(viewFor("model-of-a"))));
    });

    await act(async () => {
      auth.current().signOut();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(session.signOut).toHaveBeenCalledTimes(1);
    expect(auth.current().status).toBe("signed-in");
    expect(getSessionTuning().tiers.supervisor.model).toBe("model-of-a");
    expect(endHostedUiSessionMock).not.toHaveBeenCalled();

    await act(async () => {
      revoked.resolve(undefined);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(auth.current().status).toBe("signed-out");
    expect(getSessionTuning().tiers.supervisor).toEqual(EMPTY_TIER_SELECTION);
    expect(endHostedUiSessionMock).toHaveBeenCalledTimes(1);
    expect(endHostedUiSessionMock).toHaveBeenCalledWith(RUNTIME_CONFIG);
  });

  it("ignores a second sign-out while the first is still revoking", async () => {
    const auth = await renderAuth();
    const revoked = deferred<undefined>();
    const session = { ...fakeSession("user-a"), signOut: vi.fn(() => revoked.promise) };
    signInMock.mockResolvedValueOnce(session);
    await act(async () => {
      await auth.current().signIn("user-a", "pw");
    });

    await act(async () => {
      auth.current().signOut();
      auth.current().signOut();
      revoked.resolve(undefined);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(session.signOut).toHaveBeenCalledTimes(1);
    expect(endHostedUiSessionMock).toHaveBeenCalledTimes(1);
  });

  it("still signs out locally and ends the Hosted UI session when the session's sign-out rejects", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const auth = await renderAuth();
    const session = {
      ...fakeSession("user-a"),
      signOut: vi.fn(() => Promise.reject(new Error("revoke exploded"))),
    };
    signInMock.mockResolvedValueOnce(session);
    await act(async () => {
      await auth.current().signIn("user-a", "pw");
    });

    await signOutAndSettle(auth);

    expect(auth.current().status).toBe("signed-out");
    expect(endHostedUiSessionMock).toHaveBeenCalledTimes(1);
    expect(warn).toHaveBeenCalledTimes(1);
    warn.mockRestore();
  });

  it("signs out locally without redirecting when the runtime config cannot load", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const auth = await renderAuth();
    await signInAs(auth, "user-a");
    loadRuntimeConfigMock.mockRejectedValueOnce(new Error("runtime config: /config.json responded 503"));

    await signOutAndSettle(auth);

    expect(auth.current().status).toBe("signed-out");
    expect(endHostedUiSessionMock).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledTimes(1);
    warn.mockRestore();
  });

  it("ends the Hosted UI session even when no local session exists", async () => {
    const auth = await renderAuth();
    expect(auth.current().status).toBe("signed-out");

    await signOutAndSettle(auth);

    expect(auth.current().status).toBe("signed-out");
    expect(endHostedUiSessionMock).toHaveBeenCalledWith(RUNTIME_CONFIG);
  });
});

describe("resetUserSettingsState", () => {
  it("returns a loaded store to idle and notifies subscribers", async () => {
    const { result } = renderHook(() => useSettingsStatus());
    await act(async () => {
      await loadUserSettings(settingsClient(() => Promise.resolve(viewFor("model-of-a"))));
    });
    expect(result.current.status).toBe("ready");

    act(() => resetUserSettingsState());

    expect(result.current).toEqual({ status: "idle" });
  });

  it("discards a load that was in flight when the store was reset", async () => {
    let resolveA: (view: UserSettingsView) => void = () => {};
    const clientA = settingsClient(
      () =>
        new Promise<UserSettingsView>((resolve) => {
          resolveA = resolve;
        }),
    );
    const pendingA = loadUserSettings(clientA);

    resetUserSettingsState();
    const clientB = settingsClient(() => Promise.resolve(viewFor("model-of-b")));
    await loadUserSettings(clientB);
    resolveA(viewFor("model-of-a"));
    await pendingA;

    expect(clientB.getUserSettings).toHaveBeenCalledTimes(1);
    expect(getSessionTuning().tiers.supervisor.model).toBe("model-of-b");
  });

  it("discards the failure of a load that was in flight when the store was reset", async () => {
    let rejectA: (err: Error) => void = () => {};
    const pendingA = loadUserSettings(
      settingsClient(
        () =>
          new Promise<UserSettingsView>((_resolve, reject) => {
            rejectA = reject;
          }),
      ),
    );

    resetUserSettingsState();
    rejectA(new Error("previous user's load failed"));
    await pendingA;

    const { result } = renderHook(() => useSettingsStatus());
    expect(result.current).toEqual({ status: "idle" });
  });
});

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("a save or reset in flight at sign-out", () => {
  it.each([
    ["save", (client: SettingsClient) => saveUserSettings(client, getSessionTuning())],
    ["reset", (client: SettingsClient) => resetUserSettings(client)],
  ] as const)(
    "discards user A's %s result, so user B's sign-in fetches B's settings",
    async (_label, write) => {
      const auth = await renderAuth();
      const pendingA = deferred<UserSettingsView>();
      const clientA = {
        ...settingsClient(() => Promise.resolve(viewFor("model-of-a"))),
        saveUserSettings: vi.fn(() => pendingA.promise),
        resetUserSettings: vi.fn(() => pendingA.promise),
      };
      const clientB = settingsClient(() => Promise.resolve(viewFor("model-of-b")));

      await signInAs(auth, "user-a");
      await act(async () => {
        await loadUserSettings(clientA);
      });
      const writeA = write(clientA);

      await signOutAndSettle(auth);
      await act(async () => {
        pendingA.resolve(viewFor("model-of-a-written"));
        await writeA;
      });
      expect(getSessionTuning().tiers.supervisor).toEqual(EMPTY_TIER_SELECTION);

      await signInAs(auth, "user-b");
      await act(async () => {
        await loadUserSettings(clientB);
      });

      expect(clientB.getUserSettings).toHaveBeenCalledTimes(1);
      expect(getSessionTuning().tiers.supervisor.model).toBe("model-of-b");
    },
  );

  it("still adopts a save that completes before sign-out (control)", async () => {
    const client = {
      ...settingsClient(() => Promise.resolve(viewFor("model-of-a"))),
      saveUserSettings: vi.fn(() => Promise.resolve(viewFor("model-of-a-written"))),
    };

    await saveUserSettings(client, getSessionTuning());

    expect(getSessionTuning().tiers.supervisor.model).toBe("model-of-a-written");
  });
});
