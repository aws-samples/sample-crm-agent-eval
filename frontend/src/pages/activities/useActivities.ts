// SPDX-License-Identifier: MIT
/**
 * `useActivities`: fetch/create/update activity state for one opportunity. `logActivity` stores the
 * server's returned `opportunity` alongside the new activity, so callers render the server-computed
 * `last_touch`.
 */
import { useCallback, useEffect, useState } from "react";

import { useCrmApi } from "../../api/useCrmApi";
import { CrmApiError } from "../../api/CrmApiClient";
import { useReportView } from "../../components/AssistantPanel/useReportView";
import { newTransactionKey as transactionKeyWithPrefix } from "../../lib/randomToken";
import type { Activity, ActivityKind, Opportunity } from "../../types/domain";
import type { ListActivitiesResponse, LogActivityResponse } from "./types";

function newTransactionKey(): string {
  return transactionKeyWithPrefix("tk");
}

export interface UseActivitiesState {
  readonly activities: readonly Activity[];
  readonly loading: boolean;
  /** Present when the load failed with a typed API error; distinct from an empty list. */
  readonly error: CrmApiError | undefined;
  readonly refetch: () => Promise<void>;
  readonly logActivity: (input: {
    kind: ActivityKind;
    subject: string;
    body?: string;
    occurred_at?: string;
  }) => Promise<{ activity: Activity; opportunity: Opportunity }>;
  readonly updateActivity: (
    activityId: string,
    fields: Record<string, unknown>,
  ) => Promise<Activity>;
}

export function useActivities(opportunityId: string | undefined): UseActivitiesState {
  const api = useCrmApi();
  const [activities, setActivities] = useState<readonly Activity[]>([]);
  const [loading, setLoading] = useState(Boolean(opportunityId));
  const [error, setError] = useState<CrmApiError | undefined>(undefined);
  const [loadedOpportunityId, setLoadedOpportunityId] = useState(opportunityId);
  if (loadedOpportunityId !== opportunityId) {
    setLoadedOpportunityId(opportunityId);
    setLoading(Boolean(opportunityId));
    setError(undefined);
  }

  const fetchActivities = useCallback(() => {
    if (!opportunityId) return Promise.resolve();
    return api
      .listActivities({ opportunity_id: opportunityId })
      .then((result) => {
        setActivities((result as ListActivitiesResponse).activities);
      })
      .catch((err: unknown) => {
        setError(err instanceof CrmApiError ? err : undefined);
      })
      .finally(() => {
        setLoading(false);
      });
    // `api` is referentially stable (memoised by `useCrmApi`) and is omitted from the deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opportunityId]);

  useEffect(() => {
    void fetchActivities();
  }, [fetchActivities]);

  const refetch = useCallback(async () => {
    if (!opportunityId) return;
    setLoading(true);
    setError(undefined);
    await fetchActivities();
  }, [opportunityId, fetchActivities]);

  const logActivity = useCallback(
    async (input: { kind: ActivityKind; subject: string; body?: string; occurred_at?: string }) => {
      if (!opportunityId) {
        throw new Error("logActivity called without an opportunity id");
      }
      const result = (await api.logActivity({
        opportunity_id: opportunityId,
        kind: input.kind,
        subject: input.subject,
        body: input.body,
        occurred_at: input.occurred_at,
        transaction_key: newTransactionKey(),
      })) as LogActivityResponse;
      // Reflect the server-computed last_touch rather than recomputing it here.
      setActivities((prev) => [result.activity, ...prev]);
      return result;
    },
    [api, opportunityId],
  );

  const updateActivity = useCallback(
    async (activityId: string, fields: Record<string, unknown>) => {
      const result = (await api.updateActivity({
        activity_id: activityId,
        fields,
        transaction_key: newTransactionKey(),
      })) as { activity: Activity };
      setActivities((prev) =>
        prev.map((activity) => (activity.activity_id === activityId ? result.activity : activity)),
      );
      return result.activity;
    },
    [api],
  );

  /*
   * Reports the on-screen activity count to the assistant panel. `list_activities` returns the
   * whole collection, so the count is exact; the route already identifies the list.
   */
  useReportView({ rowCount: activities.length });

  return { activities, loading, error, refetch, logActivity, updateActivity };
}

export { newTransactionKey };
