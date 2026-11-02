// SPDX-License-Identifier: MIT
/**
 * Runtime configuration, loaded from `/config.json` at app start rather than baked in at build
 * time. Account identifiers, Cognito ids and endpoints are deploy-time values written into
 * `/config.json` on the S3 origin alongside the built assets; `public/config.example.json` is the
 * committed template.
 *
 * The app is served through CloudFront, so this fetch is same-origin. `apiBaseUrl` and
 * `agentBaseUrl` are CloudFront paths: `/api/*` proxies to API Gateway and `/agent/*` to the
 * streaming relay.
 */

export interface RuntimeConfig {
  /** Cognito user pool id, e.g. `us-east-1_XXXXXXXXX`. Deploy-time value. */
  readonly cognitoUserPoolId: string;
  /** Cognito app client id for the SPA (public client, no secret). Deploy-time value. */
  readonly cognitoClientId: string;
  /** Cognito user pool region, e.g. `us-east-1`. */
  readonly cognitoRegion: string;
  /**
   * Cognito Hosted UI domain, used only for the hosted-UI redirect flow. Empty string when the
   * deployment uses SRP-only auth (see `src/auth/AuthProvider.tsx`).
   */
  readonly cognitoHostedUiDomain: string;
  /** CloudFront-relative path prefix for the CRM REST API. Always `/api`. */
  readonly apiBaseUrl: string;
  /** CloudFront-relative path prefix for the AgentCore SSE stream. Always `/agent`. */
  readonly agentBaseUrl: string;
  /**
   * The AgentCore Runtime ARN, **raw and not URL-encoded**.
   *
   * `useAgentStream` and `useCancelTurn` each call `encodeURIComponent` on it when building the
   * `/agent/<arn>` path; a pre-encoded value would be double-encoded.
   */
  readonly agentRuntimeArn: string;
}

let cached: RuntimeConfig | undefined;
let pending: Promise<RuntimeConfig> | undefined;

const REQUIRED_KEYS: readonly (keyof RuntimeConfig)[] = [
  "cognitoUserPoolId",
  "cognitoClientId",
  "cognitoRegion",
  "cognitoHostedUiDomain",
  "apiBaseUrl",
  "agentBaseUrl",
  "agentRuntimeArn",
];

function assertValid(value: unknown): RuntimeConfig {
  if (typeof value !== "object" || value === null) {
    throw new Error("runtime config: expected a JSON object");
  }
  const record = value as Record<string, unknown>;
  for (const key of REQUIRED_KEYS) {
    if (typeof record[key] !== "string") {
      throw new Error(`runtime config: missing or non-string key "${key}"`);
    }
  }
  return record as unknown as RuntimeConfig;
}

/**
 * Fetch and cache `/config.json`. Same-origin, with no `Authorization` header.
 *
 * Safe to call from multiple modules concurrently — the first call's in-flight fetch is shared.
 */
export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  if (cached) return cached;
  if (!pending) {
    pending = fetch("/config.json", { cache: "no-store" })
      .then((res) => {
        if (!res.ok) {
          throw new Error(`runtime config: /config.json responded ${res.status}`);
        }
        return res.json();
      })
      .then((json: unknown) => {
        cached = assertValid(json);
        return cached;
      })
      .finally(() => {
        pending = undefined;
      });
  }
  return pending;
}

/** Returns the cached config, or throws if `loadRuntimeConfig` has not resolved yet. */
export function getRuntimeConfig(): RuntimeConfig {
  if (!cached) {
    throw new Error("runtime config: loadRuntimeConfig() has not resolved yet");
  }
  return cached;
}

/** Test-only: reset the module cache between test cases. */
export function __resetRuntimeConfigForTests(): void {
  cached = undefined;
  pending = undefined;
}
