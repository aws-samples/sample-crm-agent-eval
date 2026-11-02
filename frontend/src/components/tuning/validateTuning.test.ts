// SPDX-License-Identifier: MIT
/**
 * Tests for client-side tuning validation: it rejects exactly what
 * `providers.tier_tuning.resolve_tier` rejects server-side, in the same order, naming the offending
 * control; and server rejection sources map onto the same controls.
 */
import { describe, expect, it } from "vitest";

import { parseCapabilityMatrix } from "./capabilityMatrix";
import { EMPTY_TIER_SELECTION, type TierSelection } from "./sessionTuning";
import {
  TUNING_CONTROL_EFFORT,
  TUNING_CONTROL_FAMILY,
  TUNING_CONTROL_MODEL,
  TUNING_CONTROL_TIER,
  TuningValidationError,
  controlForServerSource,
  validateTierSelection,
} from "./validateTuning";

function rawMatrix() {
  return {
    default_family: "anthropic",
    models: {
      "anthropic.claude-opus-5-5": {
        model_id: "anthropic.claude-opus-5-5",
        family: "anthropic",
        tier: "supervisor",
        effort: {
          supported: true,
          values: ["low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: null,
        },
      },
      "anthropic.claude-sonnet-5-5": {
        model_id: "anthropic.claude-sonnet-5-5",
        family: "anthropic",
        tier: "subagent",
        effort: {
          supported: true,
          values: ["low", "medium", "high"],
          default: "high",
          capped_at_when_thinking_disabled: null,
        },
      },
      "anthropic.claude-haiku-5-5": {
        model_id: "anthropic.claude-haiku-5-5",
        family: "anthropic",
        tier: "tool",
        effort: {
          supported: true,
          values: ["low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: "high",
        },
      },
      "anthropic.claude-effortless-test": {
        model_id: "anthropic.claude-effortless-test",
        family: "anthropic",
        tier: "tool",
        effort: { supported: false, values: [], default: null, capped_at_when_thinking_disabled: null },
      },
      "openai.gpt-6-astra": {
        model_id: "openai.gpt-6-astra",
        family: "openai",
        tier: "supervisor",
        effort: {
          supported: true,
          values: ["none", "low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: null,
        },
      },
      "openai.gpt-6.1-sol": {
        model_id: "openai.gpt-6.1-sol",
        family: "openai",
        tier: "subagent",
        effort: {
          supported: true,
          values: ["none", "low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: null,
        },
      },
      "openai.gpt-6-luna": {
        model_id: "openai.gpt-6-luna",
        family: "openai",
        tier: "tool",
        effort: {
          supported: true,
          values: ["none", "low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: null,
        },
      },
    },
    tiers: {
      anthropic: {
        supervisor: { model_id: "anthropic.claude-opus-5-5", tuning: { effort: "low" } },
        subagent: { model_id: "anthropic.claude-sonnet-5-5", tuning: { effort: "medium" } },
        tool: { model_id: "anthropic.claude-haiku-5-5", tuning: { effort: "low" } },
      },
      openai: {
        supervisor: { model_id: "openai.gpt-6-astra", tuning: { effort: "low" } },
        subagent: { model_id: "openai.gpt-6.1-sol", tuning: { effort: "medium" } },
        tool: { model_id: "openai.gpt-6-luna", tuning: { effort: "low" } },
      },
    },
  };
}

const matrix = () => parseCapabilityMatrix(rawMatrix());

function select(overrides: Partial<TierSelection>): TierSelection {
  return { ...EMPTY_TIER_SELECTION, ...overrides };
}

function rejection(tier: "supervisor" | "subagent" | "tool", selection: TierSelection): TuningValidationError {
  try {
    validateTierSelection(matrix(), tier, selection);
  } catch (err) {
    if (err instanceof TuningValidationError) return err;
    throw err;
  }
  throw new Error("expected a rejection");
}

describe("validateTierSelection — family and model", () => {
  it("resolves an empty selection to the default family's model for the tier", () => {
    const resolved = validateTierSelection(matrix(), "subagent", EMPTY_TIER_SELECTION);
    expect(resolved.model.modelId).toBe("anthropic.claude-sonnet-5-5");
    // The tier's configured effort (medium), not Sonnet 5.5's default (high).
    expect(resolved.effort).toBe("medium");
    expect(Object.keys(resolved).sort()).toEqual(["effort", "model"]);
  });

  it("accepts the other family at any tier, with no cross-family switch", () => {
    const resolved = validateTierSelection(matrix(), "tool", select({ family: "openai" }));
    expect(resolved.model.modelId).toBe("openai.gpt-6-luna");
  });

  it("accepts any model of the tier's family, not only the tier's default model", () => {
    const resolved = validateTierSelection(
      matrix(),
      "tool",
      select({ family: "anthropic", model: "anthropic.claude-sonnet-5-5" }),
    );
    expect(resolved.model.modelId).toBe("anthropic.claude-sonnet-5-5");
  });

  it("rejects a model of the other family, naming the model control", () => {
    const err = rejection("supervisor", select({ family: "anthropic", model: "openai.gpt-6-astra" }));
    expect(err.control).toBe(TUNING_CONTROL_MODEL);
    expect(err.message).toMatch(/openai model; this tier runs the anthropic family/);
  });

  it("rejects an unknown model, naming the model control", () => {
    const err = rejection("supervisor", select({ model: "anthropic.claude-nonexistent" }));
    expect(err.control).toBe(TUNING_CONTROL_MODEL);
  });

  it("rejects an unknown family, naming the family control", () => {
    const err = rejection("supervisor", select({ family: "mistral" as never }));
    expect(err.control).toBe(TUNING_CONTROL_FAMILY);
  });
});

describe("validateTierSelection — effort", () => {
  it("rejects xhigh on Sonnet 5.5, naming the effort control", () => {
    const err = rejection("subagent", select({ effort: "xhigh" }));
    expect(err.control).toBe(TUNING_CONTROL_EFFORT);
    expect(err.message).toMatch(/accepted values are/);
  });

  it("accepts xhigh on Opus 5.5", () => {
    expect(validateTierSelection(matrix(), "supervisor", select({ effort: "xhigh" })).effort).toBe(
      "xhigh",
    );
  });

  it("accepts Haiku 5.5's high effort at the tool tier and defaults an unset one to the tier's low", () => {
    expect(validateTierSelection(matrix(), "tool", select({ effort: "high" })).effort).toBe("high");
    expect(validateTierSelection(matrix(), "tool", EMPTY_TIER_SELECTION).effort).toBe("low");
    expect(
      validateTierSelection(matrix(), "tool", select({ model: "anthropic.claude-haiku-5-5" })).effort,
    ).toBe("low");
  });

  it("defaults an unset effort on a model the tier does not configure to that model's default", () => {
    const resolved = validateTierSelection(
      matrix(),
      "tool",
      select({ model: "anthropic.claude-sonnet-5-5" }),
    );
    expect(resolved.effort).toBe("high");
  });

  it("defaults an unset effort on the other family's configured model to that tier's effort", () => {
    // GPT-6 Astra's default is medium; the openai supervisor tier configures low.
    const resolved = validateTierSelection(matrix(), "supervisor", select({ family: "openai" }));
    expect(resolved.model.modelId).toBe("openai.gpt-6-astra");
    expect(resolved.effort).toBe("low");
  });

  it("rejects any effort on a model that accepts none", () => {
    const err = rejection(
      "tool",
      select({ model: "anthropic.claude-effortless-test", effort: "low" }),
    );
    expect(err.control).toBe(TUNING_CONTROL_EFFORT);
    expect(err.message).toMatch(/does not accept an effort parameter/);
  });

  it("checks effort against the selected model, not the tier's default model", () => {
    // Haiku 5.5 accepts xhigh; Sonnet 5.5 chosen at the tool tier does not.
    expect(validateTierSelection(matrix(), "tool", select({ effort: "xhigh" })).effort).toBe(
      "xhigh",
    );
    const err = rejection("tool", select({ model: "anthropic.claude-sonnet-5-5", effort: "xhigh" }));
    expect(err.control).toBe(TUNING_CONTROL_EFFORT);
  });

  it("checks family before model before effort, like the server", () => {
    const err = rejection(
      "supervisor",
      select({ family: "anthropic", model: "openai.gpt-6-astra", effort: "bogus" }),
    );
    expect(err.control).toBe(TUNING_CONTROL_MODEL);
  });
});

describe("controlForServerSource", () => {
  it.each([
    ["tuning.tiers.supervisor.family", "supervisor", TUNING_CONTROL_FAMILY],
    ["tuning.tiers.subagent.model", "subagent", TUNING_CONTROL_MODEL],
    ["tuning.tiers.tool.effort", "tool", TUNING_CONTROL_EFFORT],
  ] as const)("maps %s onto its tier and control", (source, tier, control) => {
    expect(controlForServerSource(source)).toEqual({ tier, control });
  });

  it.each([
    ["tuning.tiers.subagent.temperature", "subagent"],
    ["tuning.tiers.tool.colour", "tool"],
  ] as const)("maps %s, a field with no control, onto its whole tier", (source, tier) => {
    expect(controlForServerSource(source)).toEqual({ tier, control: TUNING_CONTROL_TIER });
  });

  it.each([undefined, "tuning", "tuning.schema", "tuning.tiers.tool", "tuning.tiers.planner.model"])(
    "maps %s onto no control",
    (source) => {
      expect(controlForServerSource(source)).toBeUndefined();
    },
  );
});
