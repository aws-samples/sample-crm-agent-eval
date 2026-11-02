// SPDX-License-Identifier: MIT
/**
 * Fetches and caches the capability matrix for the page load, exposing loading/error/data state to
 * the settings page.
 */
import { useEffect, useState } from "react";

import type { TokenSource } from "../../api/CrmApiClient";
import { CrmApiError } from "../../api/CrmApiClient";
import { fetchCapabilityMatrix, type CapabilityMatrix } from "./capabilityMatrix";

export type CapabilityMatrixState =
  | { status: "loading" }
  | {
      status: "error";
      error: string;
      /**
       * The denied scope, when the failure was a `scope_denied`; `message` does not carry it.
       * Absent on every other failure.
       */
      missingScope?: string;
    }
  | { status: "ready"; matrix: CapabilityMatrix };

export function useCapabilityMatrix(
  apiBaseUrl: string,
  tokenSource: TokenSource,
): CapabilityMatrixState {
  const [state, setState] = useState<CapabilityMatrixState>({ status: "loading" });
  const [source, setSource] = useState({ apiBaseUrl, tokenSource });
  if (source.apiBaseUrl !== apiBaseUrl || source.tokenSource !== tokenSource) {
    setSource({ apiBaseUrl, tokenSource });
    setState({ status: "loading" });
  }

  useEffect(() => {
    let cancelled = false;
    fetchCapabilityMatrix(apiBaseUrl, tokenSource)
      .then((matrix) => {
        if (!cancelled) setState({ status: "ready", matrix });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState({
            status: "error",
            error: err instanceof Error ? err.message : "Failed to load model capabilities.",
            ...(err instanceof CrmApiError && err.body.missingScope !== undefined
              ? { missingScope: err.body.missingScope }
              : {}),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [apiBaseUrl, tokenSource]);

  return state;
}
