// SPDX-License-Identifier: MIT
/**
 * Tests for the auth context's sign-out: it clears the saved-settings store, so the next user to
 * sign in on the same page load fetches their own settings and never sees the previous user's.
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

const { signInMock, restoreMock } = vi.hoisted(() => ({
  signInMock: vi.fn(),
  restoreMock: vi.fn(),
}));

vi.mock("../config/runtimeConfig", () => ({
  loadRuntimeConfig: vi.fn().mockResolvedValue({}),
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
    signOut: vi.fn(),
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

beforeEach(() => {
  __resetSessionTuningForTests();
  signInMock.mockReset();
  restoreMock.mockReset().mockResolvedValue(undefined);
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

    act(() => auth.current().signOut());
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

    act(() => auth.current().signOut());

    expect(session.signOut).toHaveBeenCalledTimes(1);
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

      act(() => auth.current().signOut());
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
