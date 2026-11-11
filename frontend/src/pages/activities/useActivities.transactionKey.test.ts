// SPDX-License-Identifier: MIT
/**
 * Tests for the `transaction_key` that `useActivities` stamps on its mutating calls.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { newTransactionKey } from "./useActivities";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** Hide `randomUUID`, the way an insecure context or an older browser does. */
function withoutRandomUUID<T>(body: () => T): T {
  const original = globalThis.crypto;
  vi.stubGlobal("crypto", { getRandomValues: original.getRandomValues.bind(original) });
  try {
    return body();
  } finally {
    vi.stubGlobal("crypto", original);
  }
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useActivities newTransactionKey", () => {
  it("is a UUID when crypto.randomUUID is available", () => {
    expect(newTransactionKey()).toMatch(UUID);
  });

  it("falls back to a tk-prefixed crypto token when randomUUID is hidden", () => {
    const key = withoutRandomUUID(() => newTransactionKey());

    expect(key).toMatch(/^tk-\d+-[0-9a-z]{11}$/);
  });

  it("draws the fallback from crypto.getRandomValues, never Math.random", () => {
    const randomSpy = vi.spyOn(Math, "random");
    const keys = withoutRandomUUID(() => {
      const cryptoSpy = vi.spyOn(globalThis.crypto, "getRandomValues");
      const drawn = Array.from({ length: 50 }, () => newTransactionKey());
      expect(cryptoSpy).toHaveBeenCalled();
      return drawn;
    });

    expect(randomSpy).not.toHaveBeenCalled();
    expect(new Set(keys).size).toBe(keys.length);
  });
});
