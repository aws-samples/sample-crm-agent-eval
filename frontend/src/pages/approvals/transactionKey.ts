// SPDX-License-Identifier: MIT
/**
 * Generates a caller-supplied idempotency key (`transaction_key`) for a mutating call such as
 * approve, reject or revert: `crypto.randomUUID()`, else `txn-<ms>-<11 base-36>`.
 */
export { newTransactionKey } from "../../lib/randomToken";
