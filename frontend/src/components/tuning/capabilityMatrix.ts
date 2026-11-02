// SPDX-License-Identifier: MIT
/**
 * Client-side mirror of `providers.capabilities.CapabilityMatrix` (`src/providers/capabilities.py`):
 * types, a fetch of `GET /api/capabilities`, and pure query helpers (`effortOptionsFor`,
 * `resolvedEffort`) that read fields of the fetched matrix and encode no capability rule of their
 * own. `config/model_capabilities.yaml` is the source of truth.
 *
 * `GET {apiBaseUrl}/capabilities` is not a tool route: it is an `extra_routes` entry on `module.api`,
 * served by `crm.handlers.capabilities.handler`, which returns
 * `dataclasses.asdict(load_capability_matrix())`. The request carries the caller's
 * `Authorization: Bearer` token via an injected {@link TokenSource}.
 */
import type { ApiErrorBody, TokenSource } from "../../api/CrmApiClient";
import { CrmApiError, errorKindForStatus, normaliseErrorBody } from "../../api/CrmApiClient";

/**
 * The families and tiers, as runtime tuples with the exported unions derived from them, so the
 * validator checks a wire value against the same list the type admits.
 */
export const FAMILIES = ["anthropic", "openai"] as const;
export const TIERS = ["supervisor", "subagent", "tool"] as const;

export type Family = (typeof FAMILIES)[number];
export type Tier = (typeof TIERS)[number];

/** Mirrors `providers.capabilities.EffortCapability`. */
export interface EffortCapability {
  readonly supported: boolean;
  readonly values: readonly string[];
  readonly default: string | null;
  readonly cappedAtWhenThinkingDisabled: string | null;
}

/** Mirrors `providers.capabilities.ModelCapability`. */
export interface ModelCapability {
  readonly modelId: string;
  readonly family: Family;
  readonly tier: Tier;
  readonly effort: EffortCapability;
}

/** Mirrors `providers.capabilities.TierAssignment`. */
export interface TierAssignment {
  readonly modelId: string;
  readonly tuning: Readonly<Record<string, string>>;
}

/** Mirrors `providers.capabilities.CapabilityMatrix`. */
export interface CapabilityMatrix {
  readonly defaultFamily: Family;
  readonly models: Readonly<Record<string, ModelCapability>>;
  readonly tiers: Readonly<Record<Family, Readonly<Record<Tier, TierAssignment>>>>;
}

/**
 * Raw wire shape expected from `GET /api/capabilities`, in the YAML/Python snake_case field names.
 * Every field is `?: unknown`, so nothing is read before {@link requireString} and friends check it.
 */
interface RawEffortCapability {
  readonly supported?: unknown;
  readonly values?: unknown;
  readonly default?: unknown;
  readonly capped_at_when_thinking_disabled?: unknown;
}
interface RawModelCapability {
  readonly model_id?: unknown;
  readonly family?: unknown;
  readonly tier?: unknown;
  readonly effort?: unknown;
}
interface RawTierAssignment {
  readonly model_id?: unknown;
  readonly tuning?: unknown;
}
interface RawCapabilityMatrix {
  readonly default_family?: unknown;
  readonly models?: unknown;
  readonly tiers?: unknown;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/* ------- field validators. Each names the path it rejected, so a bad payload is diagnosable. ------- */

function requireRecord(value: unknown, at: string): Record<string, unknown> {
  if (!isRecord(value) || Array.isArray(value)) {
    throw new Error(`capability matrix: ${at} must be a JSON object`);
  }
  return value;
}

function requireBoolean(value: unknown, at: string): boolean {
  if (typeof value !== "boolean") throw new Error(`capability matrix: ${at} must be a boolean`);
  return value;
}

function requireString(value: unknown, at: string): string {
  if (typeof value !== "string") throw new Error(`capability matrix: ${at} must be a string`);
  return value;
}

/** `null` is meaningful here — "no default", "no cap" — so it is accepted and `undefined` is not. */
function requireNullableString(value: unknown, at: string): string | null {
  if (value === null) return null;
  return requireString(value, at);
}

function requireStringArray(value: unknown, at: string): string[] {
  if (!Array.isArray(value)) throw new Error(`capability matrix: ${at} must be an array`);
  return value.map((entry, index) => requireString(entry, `${at}[${index}]`));
}

function requireMember<T extends string>(value: unknown, allowed: readonly T[], at: string): T {
  const text = requireString(value, at);
  const match = allowed.find((candidate) => candidate === text);
  if (match === undefined) {
    throw new Error(`capability matrix: ${at} must be one of ${allowed.join(", ")}, got "${text}"`);
  }
  return match;
}

/** Tuning values travel as strings. */
function requireTuning(value: unknown, at: string): Record<string, string> {
  const raw = requireRecord(value, at);
  const tuning: Record<string, string> = {};
  for (const [key, entry] of Object.entries(raw)) {
    tuning[key] = requireString(entry, `${at}.${key}`);
  }
  return tuning;
}

/**
 * Structural validation only — no capability rule is asserted here, only shape. The returned value's
 * fields are all still `unknown`; the `parse*` functions below are what check them.
 */
function assertRawMatrix(value: unknown): RawCapabilityMatrix {
  if (!isRecord(value)) {
    throw new Error("capability matrix: expected a JSON object");
  }
  for (const key of ["default_family", "models", "tiers"]) {
    if (!(key in value)) {
      throw new Error(`capability matrix: missing key "${key}"`);
    }
  }
  return value as RawCapabilityMatrix;
}

function parseEffort(value: unknown, at: string): EffortCapability {
  const raw = requireRecord(value, at) as RawEffortCapability;
  return {
    supported: requireBoolean(raw.supported, `${at}.supported`),
    values: requireStringArray(raw.values, `${at}.values`),
    default: requireNullableString(raw.default, `${at}.default`),
    cappedAtWhenThinkingDisabled: requireNullableString(
      raw.capped_at_when_thinking_disabled,
      `${at}.capped_at_when_thinking_disabled`,
    ),
  };
}

function parseModel(value: unknown, at: string): ModelCapability {
  const raw = requireRecord(value, at) as RawModelCapability;
  return {
    modelId: requireString(raw.model_id, `${at}.model_id`),
    family: requireMember(raw.family, FAMILIES, `${at}.family`),
    tier: requireMember(raw.tier, TIERS, `${at}.tier`),
    effort: parseEffort(raw.effort, `${at}.effort`),
  };
}

function parseTierAssignment(value: unknown, at: string): TierAssignment {
  const raw = requireRecord(value, at) as RawTierAssignment;
  return {
    modelId: requireString(raw.model_id, `${at}.model_id`),
    tuning: requireTuning(raw.tuning, `${at}.tuning`),
  };
}

/**
 * Parse a `GET /api/capabilities` response body into a {@link CapabilityMatrix}.
 *
 * Throws an `Error` naming the offending field path on any payload that does not match, including a
 * `tiers` record that does not cover all of {@link FAMILIES} × {@link TIERS}, or a `models` key that
 * differs from its entry's `model_id`. Unknown fields are ignored.
 */
export function parseCapabilityMatrix(value: unknown): CapabilityMatrix {
  const raw = assertRawMatrix(value);

  const models: Record<string, ModelCapability> = {};
  for (const [modelId, entry] of Object.entries(requireRecord(raw.models, "models"))) {
    const model = parseModel(entry, `models["${modelId}"]`);
      // The producer derives each entry's `model_id` from its key, so a mismatch is a malformed payload.
    if (model.modelId !== modelId) {
      throw new Error(
        `capability matrix: models["${modelId}"].model_id is "${model.modelId}", which does not match its key`,
      );
    }
    models[modelId] = model;
  }

  const rawTiers = requireRecord(raw.tiers, "tiers");
  const tiers = {} as Record<Family, Record<Tier, TierAssignment>>;
  for (const family of FAMILIES) {
    const tierBlock = requireRecord(rawTiers[family], `tiers.${family}`);
    const parsedTierBlock = {} as Record<Tier, TierAssignment>;
    for (const tier of TIERS) {
      parsedTierBlock[tier] = parseTierAssignment(tierBlock[tier], `tiers.${family}.${tier}`);
    }
    tiers[family] = parsedTierBlock;
  }

  return {
    defaultFamily: requireMember(raw.default_family, FAMILIES, "default_family"),
    models,
    tiers,
  };
}

/**
 * Fetch and parse the capability matrix from `GET /api/capabilities` (same-origin).
 *
 * A non-2xx rejects with {@link CrmApiError}, built by {@link normaliseErrorBody} and
 * {@link errorKindForStatus}, so the message and `missing_scope` are recovered from the nested
 * `error` body.
 */
export async function fetchCapabilityMatrix(
  apiBaseUrl: string,
  tokenSource: TokenSource,
): Promise<CapabilityMatrix> {
  const token = await tokenSource.getAccessToken();
  const res = await fetch(`${apiBaseUrl}/capabilities`, {
    method: "GET",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    const fallback = `GET ${apiBaseUrl}/capabilities responded ${res.status}`;
    let body: ApiErrorBody;
    try {
      body = normaliseErrorBody(await res.json(), fallback);
    } catch {
      // Not JSON at all — an HTML error page from the edge, or a body-less stubbed response.
      body = { message: fallback };
    }
    throw new CrmApiError(errorKindForStatus(res.status), res.status, body);
  }
  return parseCapabilityMatrix(await res.json());
}

/** The model assigned to `family`'s `tier`, resolved through `matrix.tiers` then `matrix.models`. */
export function modelFor(matrix: CapabilityMatrix, family: Family, tier: Tier): ModelCapability {
  const modelId = matrix.tiers[family][tier].modelId;
  const model = matrix.models[modelId];
  if (!model) {
    throw new Error(`capability matrix: tier ${family}/${tier} names unknown model ${modelId}`);
  }
  return model;
}

/**
 * Every model of `family`, in tier order then model id. A tier may run any model of its own family.
 */
export function modelsOfFamily(matrix: CapabilityMatrix, family: Family): ModelCapability[] {
  return Object.values(matrix.models)
    .filter((model) => model.family === family)
    .sort(
      (a, b) => TIERS.indexOf(a.tier) - TIERS.indexOf(b.tier) || a.modelId.localeCompare(b.modelId),
    );
}

/**
 * The effort values the UI may offer for `model`, or `undefined` when the model accepts no effort
 * parameter. A pure read of `model.effort`.
 */
export function effortOptionsFor(model: ModelCapability): readonly string[] | undefined {
  return model.effort.supported ? model.effort.values : undefined;
}

/**
 * The configured effort an unset request takes for `model` at `family`'s `tier`: the tier's
 * `config/models.yaml` effort when `model` is that tier's configured model, else `null`.
 */
export function configuredEffortFor(
  matrix: CapabilityMatrix,
  family: Family,
  tier: Tier,
  model: ModelCapability,
): string | null {
  const configured = matrix.tiers[family][tier];
  return model.modelId === configured.modelId ? (configured.tuning.effort ?? null) : null;
}

/**
 * The effort that will run for `model` at `family`'s `tier` given a caller's requested value,
 * mirroring `providers.tier_tuning.resolve_tier`: the request, else {@link configuredEffortFor},
 * else the model's default.
 */
export function resolvedEffort(
  matrix: CapabilityMatrix,
  family: Family,
  tier: Tier,
  model: ModelCapability,
  requested: string | null,
): string | null {
  const effort = requested ?? configuredEffortFor(matrix, family, tier, model);
  if (effort !== null) return effort;
  return model.effort.supported ? model.effort.default : null;
}
