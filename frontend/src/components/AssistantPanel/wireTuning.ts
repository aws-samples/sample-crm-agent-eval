// SPDX-License-Identifier: MIT
/**
 * Render the user's per-tier tuning as the schema-3 `tuning` block a turn and a settings save
 * carry, decoded server-side by `providers.tier_tuning.parse_wire_tuning`:
 *
 * `{schema: 3, source: "settings.tuning", tiers: {<tier>: {family, model, effort}}}`
 *
 * Every tier travels, each with all three fields; `null` means the tier's default.
 */

import { TIERS } from "../tuning/capabilityMatrix";
import type { SessionTuning } from "../tuning/sessionTuning";

/** The wire schema version `parse_wire_tuning` accepts. Mirrors `WIRE_SCHEMA_VERSION`. */
export const WIRE_SCHEMA_VERSION = 3;

/** The `source` the browser stamps on its blocks; named in every server rejection's detail. */
export const WIRE_TUNING_SOURCE = "settings.tuning";

/** Build the full schema-3 block for `tuning`, every tier present. */
export function wireTuningBlock(tuning: SessionTuning): Record<string, unknown> {
  const tiers: Record<string, unknown> = {};
  for (const tier of TIERS) {
    const { family, model, effort } = tuning.tiers[tier];
    tiers[tier] = { family, model, effort };
  }
  return { schema: WIRE_SCHEMA_VERSION, source: WIRE_TUNING_SOURCE, tiers };
}

/**
 * Build the `tuning` block for a turn, or `undefined` when no tier selects anything, so the Runtime
 * applies its configured defaults.
 *
 * @param tuning The current tuning, from `getSessionTuning()`.
 */
export function toWireTuning(tuning: SessionTuning): Record<string, unknown> | undefined {
  const selectsAnything = TIERS.some((tier) =>
    Object.values(tuning.tiers[tier]).some((value) => value !== null),
  );
  return selectsAnything ? wireTuningBlock(tuning) : undefined;
}
