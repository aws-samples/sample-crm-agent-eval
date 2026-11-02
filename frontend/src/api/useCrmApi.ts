// SPDX-License-Identifier: MIT
/**
 * `useCrmApi` — the hook every page/component uses to reach `CrmApiClient`. Binds the client to
 * the live `useAuth()` context so callers never construct `CrmApiClient` by hand.
 */
import { useMemo } from "react";

import { useAuth } from "../auth/AuthProvider";
import { getRuntimeConfig } from "../config/runtimeConfig";
import { CrmApiClient } from "./CrmApiClient";

export function useCrmApi(): CrmApiClient {
  const auth = useAuth();
  return useMemo(
    () => CrmApiClient.fromAuth(getRuntimeConfig().apiBaseUrl, auth),
    [auth],
  );
}
