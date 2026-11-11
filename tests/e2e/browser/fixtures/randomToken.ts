// SPDX-License-Identifier: MIT
/** Unpredictable tokens for run ids and idempotency keys, drawn from `crypto.getRandomValues`. */

const ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz";
const UNBIASED_BYTE_LIMIT = 252;

/**
 * A lowercase base-36 token of exactly `length` characters. Bytes >= 252 are discarded so every
 * character is equally likely. Throws `RangeError` when `length` is not a positive integer.
 */
export function randomToken(length: number): string {
  if (!Number.isInteger(length) || length < 1) {
    throw new RangeError(`randomToken length must be a positive integer, got ${length}`);
  }
  let token = "";
  while (token.length < length) {
    const bytes = globalThis.crypto.getRandomValues(new Uint8Array(length - token.length + 8));
    for (const byte of bytes) {
      if (byte >= UNBIASED_BYTE_LIMIT) continue;
      token += ALPHABET[byte % ALPHABET.length];
      if (token.length === length) break;
    }
  }
  return token;
}
