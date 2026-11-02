// SPDX-License-Identifier: MIT
/**
 * Response-shape types for the activity surfaces, hand-maintained from the handler returns:
 *   - `crm.handlers.log_activity` returns `{activity, opportunity}`;
 *   - `crm.handlers.list_activities` returns `{activities, next_cursor}`.
 */
import type { Activity, Opportunity } from "../../types/domain";

export interface LogActivityResponse {
  readonly activity: Activity;
  readonly opportunity: Opportunity;
}

export interface ListActivitiesResponse {
  readonly activities: readonly Activity[];
  readonly next_cursor?: string | null;
}
