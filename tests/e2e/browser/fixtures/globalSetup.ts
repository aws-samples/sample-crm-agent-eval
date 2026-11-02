// SPDX-License-Identifier: MIT
/**
 * Refuses a journey run whose base URL is undiscovered, looks like production, comes from a stale
 * outputs snapshot, or lacks required stack outputs — checked in that order.
 *
 * Runs for `playwright test` but not for `playwright test --list`.
 */
import { missingOutputs, resolveBaseUrl, stalenessRefusal } from "../playwright.config";

/** Substrings that mark a target as production. An ambiguous target is treated as production. */
const PRODUCTION_MARKERS = ["prod", "production", "prd"];

export default function globalSetup(): void {
  const baseUrl = resolveBaseUrl();
  if (!baseUrl) {
    const missing = missingOutputs();
    throw new Error(
      "T4 browser journeys cannot run: the base URL was not discovered from stack outputs, and " +
        "there is deliberately no default — a localhost fallback would let this suite pass against " +
        `nothing. Missing output(s): ${missing.join(", ") || "(no outputs source found)"}.\n` +
        "Provide one of:\n" +
        "  CRM_E2E_BASE_URL=https://<dev-distribution>\n" +
        "  CRM_TF_OUTPUTS_JSON=/path/to/terraform-output.json\n" +
        "  results/tf-outputs.json (written by scripts/deploy.sh)\n" +
        "  a local infra/ state terraform can read.",
    );
  }

  const lowered = baseUrl.toLowerCase();
  const marker = PRODUCTION_MARKERS.find((needle) => lowered.includes(needle));
  if (marker) {
    throw new Error(
      `Refusing to run browser journeys against ${baseUrl}: it contains "${marker}", ` +
        "which marks it as production. These journeys create proposals, approve them, and revert " +
        "audit records — they mutate CRM state. Point them at a dev or ephemeral stack.",
    );
  }

  const stale = stalenessRefusal();
  if (stale) {
    throw new Error(stale);
  }

  const missing = missingOutputs();
  if (missing.length > 0) {
    throw new Error(
      `Base URL resolved to ${baseUrl}, but these stack outputs a journey needs are missing: ` +
        `${missing.join(", ")}. Sign-in drives the SPA's form against the real Cognito pool, so ` +
        "the pool id, SPA client id and Cognito domain must all be discoverable.",
    );
  }
}
