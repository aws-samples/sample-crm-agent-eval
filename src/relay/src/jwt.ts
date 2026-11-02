// SPDX-License-Identifier: MIT
/**
 * Cognito bearer-token verification for the streaming relay.
 *
 * Verifies signature, `iss`, `exp`, and the client the token was issued for, then returns the
 * token's claims verbatim. It builds no `Identity` and derives no scope: the claims travel to the
 * Runtime in the request payload and `crm.envelope.decode_identity` decodes them over the `agent`
 * channel. This check is the single authentication gate on the browser path.
 *
 * The token arrives in {@link JWT_HEADER}, not in `Authorization`: CloudFront OAC signs every origin
 * request and overwrites the viewer's `Authorization` header.
 *
 * Cognito's `token_use` discriminator names which claim carries the client identifier (`client_id`
 * on an access token, `aud` on an ID token), matching `src/gateway/identity_interceptor.py`.
 *
 * Configuration comes from `COGNITO_ISSUER`, `COGNITO_JWKS_URL`, and `COGNITO_AUDIENCE_PARAMETER`
 * (the name of an SSM parameter holding the audience). The parameter is read once per execution
 * environment and cached. All three are required; an unreadable, empty, or absent parameter throws.
 */
import {
  ENV_REGION,
  cachedResolver,
  ssmParameterReader,
  type CachedResolver,
  type ParameterReader,
} from "./parameters.js";
import { JwtVerifier } from "aws-jwt-verify";
import type { JwksCache } from "aws-jwt-verify/jwk";

/** The claims a verified token carries, passed on untouched. */
export type VerifiedClaims = Readonly<Record<string, unknown>>;

/** Which claim carries the client identifier, keyed by Cognito's `token_use` discriminator. */
const CLIENT_ID_CLAIM: Readonly<Record<string, string>> = {
  access: "client_id",
  id: "aud",
};

/**
 * Header the Cognito bearer token travels in. Must equal the SPA's `JWT_HEADER` in
 * `frontend/src/lib/agentClient.ts`.
 */
export const JWT_HEADER = "x-crm-authorization";

/** Pattern the {@link JWT_HEADER} value must match: the bearer scheme and exactly one token. */
const BEARER_HEADER = /^bearer[ \t]+(\S+)$/i;

/** Environment variable names this module reads. */
export const ENV_ISSUER = "COGNITO_ISSUER";
export const ENV_JWKS_URL = "COGNITO_JWKS_URL";
export const ENV_AUDIENCE_PARAMETER = "COGNITO_AUDIENCE_PARAMETER";

export { ENV_REGION, ssmParameterReader };
export type { ParameterReader };

/** Raised for any token the relay refuses, and for a configuration it cannot verify against. */
export class JwtVerificationError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "JwtVerificationError";
  }
}

/** Verified-against values for one Cognito user pool. */
export interface JwtConfig {
  readonly issuer: string;
  readonly jwksUri: string;
  readonly allowedClientIds: readonly string[];
}

/** What the environment supplies directly; the audience is read from the SSM parameter it names. */
export interface JwtEnvConfig {
  readonly issuer: string;
  readonly jwksUri: string;
  readonly audienceParameter: string;
}

/**
 * Read the environment half of the verification configuration.
 *
 * @throws JwtVerificationError when any of the three required variables is absent or blank.
 */
export function loadJwtEnvConfig(env: NodeJS.ProcessEnv = process.env): JwtEnvConfig {
  const issuer = (env[ENV_ISSUER] ?? "").trim();
  const jwksUri = (env[ENV_JWKS_URL] ?? "").trim();
  const audienceParameter = (env[ENV_AUDIENCE_PARAMETER] ?? "").trim();

  if (!issuer) {
    throw new JwtVerificationError(`${ENV_ISSUER} is not set`);
  }
  if (!jwksUri) {
    throw new JwtVerificationError(`${ENV_JWKS_URL} is not set`);
  }
  if (!audienceParameter) {
    throw new JwtVerificationError(`${ENV_AUDIENCE_PARAMETER} is not set`);
  }
  return { issuer, jwksUri, audienceParameter };
}

/**
 * Read the accepted audience from its SSM parameter.
 *
 * The parameter value is one client id, or several comma-separated.
 *
 * @throws JwtVerificationError when the read fails, or resolves to no usable client id.
 */
export async function resolveAudience(
  config: JwtEnvConfig,
  reader: ParameterReader,
): Promise<readonly string[]> {
  let raw: string;
  try {
    raw = await reader.read(config.audienceParameter);
  } catch (cause) {
    throw new JwtVerificationError(
      `cannot read the audience parameter ${JSON.stringify(config.audienceParameter)}`,
      { cause },
    );
  }
  const clientIds = raw
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
  if (clientIds.length === 0) {
    throw new JwtVerificationError(
      `the audience parameter ${JSON.stringify(config.audienceParameter)} names no client id`,
    );
  }
  return clientIds;
}

/**
 * Resolve the full verification configuration, reading the audience parameter at most once.
 *
 * The in-flight promise is cached, so concurrent invocations share one read. A failed read clears
 * the cache so the next attempt retries.
 */
export async function resolveJwtConfig(
  env: NodeJS.ProcessEnv = process.env,
  reader?: ParameterReader,
): Promise<JwtConfig> {
  cachedConfig ??= cachedResolver(async () => {
    const envConfig = loadJwtEnvConfig(env);
    const allowedClientIds = await resolveAudience(envConfig, reader ?? ssmParameterReader(env));
    return { issuer: envConfig.issuer, jwksUri: envConfig.jwksUri, allowedClientIds };
  });
  return cachedConfig.get();
}

/** What a verifier must offer this module: one asynchronous `verify`. */
export interface TokenVerifier {
  verify(token: string): Promise<Record<string, unknown>>;
}

/**
 * Build a verifier for `config`, holding its own JWKS cache.
 *
 * The library's audience check is disabled; {@link assertIssuedToAllowedClient} performs it, since
 * an access token carries its client in `client_id` rather than `aud`.
 *
 * @param config Issuer, JWKS URI, and the client identifiers a token may be issued for.
 * @param jwksCache Cache to resolve signing keys through. Defaults to the library's own HTTPS-backed
 *   cache; supplied by a caller that already holds the key set, or by a test.
 */
export function createVerifier(config: JwtConfig, jwksCache?: JwksCache): TokenVerifier {
  const properties = {
    issuer: config.issuer,
    jwksUri: config.jwksUri,
    audience: null,
  };
  return jwksCache === undefined
    ? JwtVerifier.create(properties)
    : JwtVerifier.create(properties, { jwksCache });
}

let cachedConfig: CachedResolver<JwtConfig> | undefined;
let cachedVerifier: TokenVerifier | undefined;

/** What one verification needs: a verifier and the resolved configuration to check claims against. */
export interface VerificationContext {
  verifier: TokenVerifier;
  config: JwtConfig;
}

/**
 * Return the process-wide verifier and configuration, building each at most once.
 *
 * The JWKS cache lives inside the verifier, so warm invocations reuse the fetched key set.
 */
export async function sharedContext(
  env: NodeJS.ProcessEnv = process.env,
  reader?: ParameterReader,
): Promise<VerificationContext> {
  const config = await resolveJwtConfig(env, reader);
  cachedVerifier ??= createVerifier(config);
  return { verifier: cachedVerifier, config };
}

/** Discard the process-wide verifier and audience, so a test can start from a cold start. */
export function resetSharedVerifier(): void {
  cachedVerifier = undefined;
  cachedConfig?.reset();
  cachedConfig = undefined;
}

/**
 * Extract the bearer token from a {@link JWT_HEADER} value.
 *
 * @throws JwtVerificationError when the header is absent, carries another scheme, or carries no
 *   token after the scheme.
 */
export function extractBearerToken(header: string | undefined): string {
  if (header === undefined || header.trim() === "") {
    throw new JwtVerificationError(`request carries no ${JWT_HEADER} header`);
  }
  const match = header.trim().match(BEARER_HEADER);
  const token = match?.[1];
  if (token === undefined) {
    throw new JwtVerificationError(`${JWT_HEADER} header is not a well-formed bearer token`);
  }
  return token;
}

/**
 * Assert the claims a verified Cognito token must carry beyond signature and expiry.
 *
 * `aws-jwt-verify` treats an absent `exp` as nothing to check, so `exp` is required here.
 *
 * @throws JwtVerificationError when `exp` or `iss` is absent, when `token_use` is absent or is
 *   neither `access` nor `id`, or when the client identifier that `token_use` names is not in
 *   `config.allowedClientIds`.
 */
export function assertIssuedToAllowedClient(
  claims: Record<string, unknown>,
  config: JwtConfig,
): void {
  if (typeof claims["exp"] !== "number") {
    throw new JwtVerificationError("token carries no exp claim");
  }
  if (typeof claims["iss"] !== "string" || claims["iss"] === "") {
    throw new JwtVerificationError("token carries no iss claim");
  }

  const tokenUse = typeof claims["token_use"] === "string" ? claims["token_use"] : "";
  const clientIdClaim = CLIENT_ID_CLAIM[tokenUse];
  if (clientIdClaim === undefined) {
    throw new JwtVerificationError(
      `token carries unrecognised token_use ${JSON.stringify(tokenUse)}`,
    );
  }

  const clientId = singleClientIdentifier(claims[clientIdClaim]);
  if (clientId === "" || !config.allowedClientIds.includes(clientId)) {
    throw new JwtVerificationError(
      `token issued for unrecognised client ${JSON.stringify(clientId)}`,
    );
  }
}

/**
 * Resolve a client identifier claim to one value.
 *
 * An `aud` may be an array per RFC 7519: a single-valued array resolves to that value, and a
 * multi-valued one resolves to no client at all, since no single client issued it.
 */
function singleClientIdentifier(raw: unknown): string {
  if (Array.isArray(raw)) {
    return raw.length === 1 ? String(raw[0]) : "";
  }
  return typeof raw === "string" ? raw : "";
}

/**
 * Verify a {@link JWT_HEADER} value and return the token's claims unchanged.
 *
 * @param header The raw {@link JWT_HEADER} value, or `undefined` when absent.
 * @param deps Injection seam for tests; defaults to the process-wide verifier and configuration.
 * @returns The verified claims, exactly as the token carried them.
 * @throws JwtVerificationError for every rejection — an absent or malformed header, a bad
 *   signature, an expired token, a wrong issuer, an unrecognised `token_use`, or an unrecognised
 *   client.
 */
export async function verifyBearerToken(
  header: string | undefined,
  deps?: VerificationContext,
): Promise<VerifiedClaims> {
  const token = extractBearerToken(header);
  const context = deps ?? (await sharedContext());
  let claims: Record<string, unknown>;
  try {
    claims = await context.verifier.verify(token);
  } catch (cause) {
    throw new JwtVerificationError("token failed verification", { cause });
  }
  assertIssuedToAllowedClient(claims, context.config);
  return claims;
}
