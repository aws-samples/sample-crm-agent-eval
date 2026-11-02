// SPDX-License-Identifier: MIT
/**
 * The one classifier for a run's reported mode, shared by the page and the per-arm panel.
 *
 * `mode` arrives from the wire as an open string, so any value other than `"stub"` or `"live"` is
 * classified as unrecognised rather than defaulted to either mode.
 */

/** The recognised modes, and the third outcome for anything else. */
export type RunMode = "stub" | "live" | "unrecognised";

/**
 * Classify a wire `mode` value.
 *
 * @param value - The `mode` a run reported. Absent, null, and any unrecognised string all classify as
 *   `"unrecognised"`, never as a recognised mode.
 */
export function classifyRunMode(value: unknown): RunMode {
  if (value === "stub") return "stub";
  if (value === "live") return "live";
  return "unrecognised";
}
