// SPDX-License-Identifier: MIT
/**
 * Generates a caller-supplied idempotency key (`transaction_key`) for a mutating call such as
 * approve, reject or revert.
 */
export function newTransactionKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  // Fallback for a test/JSDOM environment lacking `crypto.randomUUID`.
  return `txn-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
