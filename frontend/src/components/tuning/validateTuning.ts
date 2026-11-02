// SPDX-License-Identifier: MIT
/**
 * Client-side pre-flight validation of one tier's selection, mirroring
 * `providers.tier_tuning.resolve_tier` (`src/providers/tier_tuning.py`): the same checks in the same
 * order — family, model, effort. The server still enforces them on save; this reports the
 * offending control before a round trip.
 *
 * Each pre-flight rejection names the offending UI control (`settings.tuning.<field>`).
 * {@link controlForServerSource} maps a server rejection's `tuning.tiers.<tier>.<field>` source onto
 * the same tier and control, or onto the tier as a whole ({@link TUNING_CONTROL_TIER}) when the
 * field has no control here, such as a parameter sent by a page loaded before a deploy removed it.
 */
import type { CapabilityMatrix, ModelCapability, Tier } from "./capabilityMatrix";
import { FAMILIES, TIERS, configuredEffortFor, modelFor } from "./capabilityMatrix";
import type { TierSelection } from "./sessionTuning";

export const TUNING_CONTROL_FAMILY = "settings.tuning.family";
export const TUNING_CONTROL_MODEL = "settings.tuning.model";
export const TUNING_CONTROL_EFFORT = "settings.tuning.effort";
/** A server rejection naming a tier field that has no control of its own. */
export const TUNING_CONTROL_TIER = "settings.tuning.tier";

export type TuningControl =
  | typeof TUNING_CONTROL_FAMILY
  | typeof TUNING_CONTROL_MODEL
  | typeof TUNING_CONTROL_EFFORT
  | typeof TUNING_CONTROL_TIER;

const CONTROL_FOR_FIELD: Readonly<Partial<Record<string, TuningControl>>> = {
  family: TUNING_CONTROL_FAMILY,
  model: TUNING_CONTROL_MODEL,
  effort: TUNING_CONTROL_EFFORT,
};

const SERVER_SOURCE_PATTERN = /^tuning\.tiers\.([a-z]+)\.([a-z]+)$/;

/** A rejected tuning value, naming the offending control. */
export class TuningValidationError extends Error {
  constructor(
    message: string,
    public readonly control: TuningControl,
  ) {
    super(message);
    this.name = "TuningValidationError";
  }
}

export interface ResolvedTuning {
  readonly model: ModelCapability;
  readonly effort: string | null;
}

/** Mirrors the model step of `resolve_tier`: `null` is the family's model for the tier. */
function resolveModel(
  matrix: CapabilityMatrix,
  tier: Tier,
  selection: TierSelection,
): ModelCapability {
  const family = selection.family ?? matrix.defaultFamily;
  if (!FAMILIES.includes(family)) {
    throw new TuningValidationError(`Unknown model family "${family}".`, TUNING_CONTROL_FAMILY);
  }
  if (selection.model === null) return modelFor(matrix, family, tier);
  const model = matrix.models[selection.model];
  if (model === undefined) {
    throw new TuningValidationError(
      `Unknown model "${selection.model}" for this tier.`,
      TUNING_CONTROL_MODEL,
    );
  }
  if (model.family !== family) {
    throw new TuningValidationError(
      `${model.modelId} is a ${model.family} model; this tier runs the ${family} family.`,
      TUNING_CONTROL_MODEL,
    );
  }
  return model;
}

/** Mirrors `providers.base._validate_effort`. */
function validateEffort(model: ModelCapability, requested: string | null): string | null {
  if (requested === null) {
    return model.effort.supported ? model.effort.default : null;
  }
  if (!model.effort.supported) {
    throw new TuningValidationError(
      `${model.modelId} does not accept an effort parameter.`,
      TUNING_CONTROL_EFFORT,
    );
  }
  if (!model.effort.values.includes(requested)) {
    throw new TuningValidationError(
      `${model.modelId} does not accept effort="${requested}"; accepted values are ${JSON.stringify(model.effort.values)}.`,
      TUNING_CONTROL_EFFORT,
    );
  }
  return requested;
}

/**
 * Validate one tier's selection against the matrix. Throws {@link TuningValidationError} naming the
 * offending control on the first violation, in the order the server checks.
 */
export function validateTierSelection(
  matrix: CapabilityMatrix,
  tier: Tier,
  selection: TierSelection,
): ResolvedTuning {
  const model = resolveModel(matrix, tier, selection);
  const family = selection.family ?? matrix.defaultFamily;
  const requested = selection.effort ?? configuredEffortFor(matrix, family, tier, model);
  const effort = validateEffort(model, requested);
  return { model, effort };
}

/**
 * Map a server rejection's `source` (`tuning.tiers.<tier>.<field>`) onto the tier and the control it
 * names, falling back to {@link TUNING_CONTROL_TIER} for a field with no control. `undefined` when
 * the source names no tier.
 */
export function controlForServerSource(
  source: string | undefined,
): { readonly tier: Tier; readonly control: TuningControl } | undefined {
  const match = (source ?? "").match(SERVER_SOURCE_PATTERN);
  if (match === null) return undefined;
  const tier = TIERS.find((candidate) => candidate === match[1]);
  if (tier === undefined) return undefined;
  return { tier, control: CONTROL_FOR_FIELD[match[2]] ?? TUNING_CONTROL_TIER };
}
