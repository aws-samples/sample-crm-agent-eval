// SPDX-License-Identifier: MIT
/**
 * Cryptographically random tokens and idempotency keys for the SPA.
 *
 * `randomToken(length)` draws from `globalThis.crypto.getRandomValues`, resolved on every call, and
 * throws when no Web Crypto is present. `newTransactionKey(prefix)` prefers `crypto.randomUUID()`.
 */

const ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz";
const UNBIASED_LIMIT = ALPHABET.length * Math.floor(256 / ALPHABET.length);

/** Lowercase base-36 token of exactly `length` characters from `crypto.getRandomValues`. */
export function randomToken(length: number): string {
  if (!Number.isInteger(length) || length < 1) {
    throw new RangeError(`randomToken length must be a positive integer, got ${length}`);
  }
  const crypto = globalThis.crypto;
  let out = "";
  while (out.length < length) {
    const bytes = crypto.getRandomValues(new Uint8Array(length - out.length + 8));
    for (const byte of bytes) {
      if (byte >= UNBIASED_LIMIT) continue;
      out += ALPHABET[byte % ALPHABET.length];
      if (out.length === length) break;
    }
  }
  return out;
}

/**
 * An idempotency `transaction_key`: `crypto.randomUUID()` when available, otherwise
 * `${prefix}-${Date.now()}-${randomToken(11)}`.
 */
export function newTransactionKey(prefix: string = "txn"): string {
  const crypto = globalThis.crypto;
  if (typeof crypto?.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${prefix}-${Date.now()}-${randomToken(11)}`;
}
