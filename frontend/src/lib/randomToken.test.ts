// SPDX-License-Identifier: MIT
// Tests for `lib/randomToken.ts`: exact length, base-36 alphabet, crypto-only randomness with
// rejection of biased bytes, input validation, and both `newTransactionKey` branches.
import { afterEach, describe, expect, it, vi } from "vitest";

import { newTransactionKey, randomToken } from "./randomToken";

const realCrypto = globalThis.crypto;

/** Replace the global `crypto` with one lacking `randomUUID`, keeping `getRandomValues`. */
function hideRandomUUID(): { getRandomValues: ReturnType<typeof vi.fn> } {
  const getRandomValues = vi.fn(<T extends ArrayBufferView<ArrayBuffer>>(array: T): T =>
    realCrypto.getRandomValues(array),
  );
  vi.stubGlobal("crypto", { getRandomValues });
  return { getRandomValues };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("randomToken", () => {
  it.each([1, 6, 11, 32, 200])("returns exactly %i characters", (length) => {
    expect(randomToken(length)).toHaveLength(length);
  });

  it("uses only lowercase base-36 characters", () => {
    expect(randomToken(2000)).toMatch(/^[0-9a-z]+$/);
  });

  it("produces distinct tokens across many draws", () => {
    const tokens = new Set(Array.from({ length: 1000 }, () => randomToken(11)));
    expect(tokens.size).toBe(1000);
  });

  it.each([0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY])(
    "throws RangeError for length %s",
    (length) => {
      expect(() => randomToken(length)).toThrow(RangeError);
    },
  );

  it("uses crypto.getRandomValues, not Math.random", () => {
    const randomSpy = vi.spyOn(Math, "random");
    const cryptoSpy = vi.spyOn(globalThis.crypto, "getRandomValues");
    randomToken(64);
    expect(cryptoSpy).toHaveBeenCalled();
    expect(randomSpy).not.toHaveBeenCalled();
  });

  it("rejects bytes >= 252 instead of mapping them", () => {
    const batches = [
      [252, 255, 253, 254, 0],
      [1, 35, 251],
    ];
    const getRandomValues = vi.fn(<T extends ArrayBufferView>(array: T): T => {
      const view = array as unknown as Uint8Array;
      view.fill(252);
      view.set((batches.shift() ?? []).slice(0, view.length));
      return array;
    });
    vi.stubGlobal("crypto", { getRandomValues });
    expect(randomToken(4)).toBe("01zz");
  });

  it("resolves globalThis.crypto at call time", () => {
    const { getRandomValues } = hideRandomUUID();
    randomToken(8);
    expect(getRandomValues).toHaveBeenCalled();
  });

  it("throws rather than degrading when Web Crypto is absent", () => {
    vi.stubGlobal("crypto", undefined);
    const randomSpy = vi.spyOn(Math, "random");
    expect(() => randomToken(8)).toThrow();
    expect(randomSpy).not.toHaveBeenCalled();
  });
});

describe("newTransactionKey", () => {
  it("returns crypto.randomUUID() when it is available", () => {
    const uuidSpy = vi.spyOn(globalThis.crypto, "randomUUID");
    const key = newTransactionKey();
    expect(uuidSpy).toHaveBeenCalledOnce();
    expect(key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  });

  it("falls back to txn-<ms>-<11 base-36> by default without randomUUID", () => {
    const { getRandomValues } = hideRandomUUID();
    const randomSpy = vi.spyOn(Math, "random");
    expect(newTransactionKey()).toMatch(/^txn-\d+-[0-9a-z]{11}$/);
    expect(getRandomValues).toHaveBeenCalled();
    expect(randomSpy).not.toHaveBeenCalled();
  });

  it("uses an overriding prefix in the fallback", () => {
    hideRandomUUID();
    expect(newTransactionKey("tk")).toMatch(/^tk-\d+-[0-9a-z]{11}$/);
  });
});
