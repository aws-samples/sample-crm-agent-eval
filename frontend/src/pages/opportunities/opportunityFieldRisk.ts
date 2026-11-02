// SPDX-License-Identifier: MIT
/**
 * The risk split for opportunity field updates, hand-mirrored from
 * `src/crm/toolspec/definitions.py::UPDATE_ALLOWLISTS["opportunities"]` /
 * `HIGH_RISK_OPPORTUNITY_FIELDS`, which is authoritative. The backend still rejects a disallowed
 * field before any write.
 */

/** Low-risk opportunity fields: editable directly via `update_opportunity`. */
export const OPPORTUNITY_LOW_RISK_FIELDS = ["name", "description", "source", "competitor"] as const;

/**
 * High-risk opportunity fields: stage, value, close date, owner and primary contact. Changes route
 * through `submit_proposal`, never a direct `update_opportunity` call.
 */
export const OPPORTUNITY_HIGH_RISK_FIELDS = [
  "stage",
  "value",
  "close_date",
  "owner_rep_id",
  "primary_contact_id",
] as const;
