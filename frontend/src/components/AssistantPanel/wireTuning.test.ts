// SPDX-License-Identifier: MIT
/**
 * `toWireTuning` / `wireTuningBlock`: the user's per-tier selections become the schema-3 block
 * that `providers.tier_tuning.parse_wire_tuning` decodes.
 *
 * The last case writes `tests/integration/fixtures/wire_tuning.json`, which the Python wire-contract
 * test decodes with the real parser, so the two sides are checked against one artefact.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { EMPTY_TIER_SELECTION, type SessionTuning } from "../tuning/sessionTuning";
import { WIRE_SCHEMA_VERSION, WIRE_TUNING_SOURCE, toWireTuning, wireTuningBlock } from "./wireTuning";

const EMPTY: SessionTuning = {
  tiers: {
    supervisor: EMPTY_TIER_SELECTION,
    subagent: EMPTY_TIER_SELECTION,
    tool: EMPTY_TIER_SELECTION,
  },
};

const MIXED: SessionTuning = {
  tiers: {
    supervisor: { family: "openai", model: "openai.gpt-6-astra", effort: "none" },
    subagent: { family: "anthropic", model: "anthropic.claude-sonnet-5-5", effort: "low" },
    tool: { family: "anthropic", model: "anthropic.claude-haiku-5-5", effort: null },
  },
};

const FIXTURE_PATH = resolve(process.cwd(), "..", "tests", "integration", "fixtures", "wire_tuning.json");

describe("toWireTuning", () => {
  it("omits the block when no tier selects anything, so the Runtime applies its defaults", () => {
    expect(toWireTuning(EMPTY)).toBeUndefined();
  });

  it("emits the schema-3 block with every tier and every field once anything is selected", () => {
    const block = toWireTuning({
      tiers: { ...EMPTY.tiers, tool: { ...EMPTY_TIER_SELECTION, effort: "low" } },
    });
    expect(block).toEqual({
      schema: 3,
      source: "settings.tuning",
      tiers: {
        supervisor: { family: null, model: null, effort: null },
        subagent: { family: null, model: null, effort: null },
        tool: { family: null, model: null, effort: "low" },
      },
    });
  });

  it("carries each tier's own family, model and effort, with no flat top-level keys", () => {
    const block = toWireTuning(MIXED) as Record<string, unknown>;
    expect(block.tiers).toEqual(MIXED.tiers);
    for (const key of ["family", "effort", "model"]) {
      expect(Object.keys(block)).not.toContain(key);
    }
  });

  it("sends exactly family, model and effort per tier, even from a selection carrying more", () => {
    const widened = {
      tiers: { ...EMPTY.tiers, tool: { ...EMPTY_TIER_SELECTION, effort: "low", temperature: 0.2 } },
    } as SessionTuning;
    const tiers = (toWireTuning(widened) as { tiers: Record<string, object> }).tiers;
    for (const fields of Object.values(tiers)) {
      expect(Object.keys(fields)).toEqual(["family", "model", "effort"]);
    }
  });
});

describe("wireTuningBlock", () => {
  it("always emits a full block, even when nothing is selected (a save of defaults)", () => {
    expect(wireTuningBlock(EMPTY)).toEqual({
      schema: WIRE_SCHEMA_VERSION,
      source: WIRE_TUNING_SOURCE,
      tiers: EMPTY.tiers,
    });
  });

  it("writes the cross-language fixture the Python decoder reads", () => {
    const fixture = {
      description:
        "Written by frontend/src/components/AssistantPanel/wireTuning.test.ts; decoded by " +
        "providers.tier_tuning.parse_wire_tuning in tests/integration.",
      blocks: { mixed_families: wireTuningBlock(MIXED), all_defaults: wireTuningBlock(EMPTY) },
    };
    mkdirSync(dirname(FIXTURE_PATH), { recursive: true });
    writeFileSync(FIXTURE_PATH, `${JSON.stringify(fixture, null, 2)}\n`, "utf8");
    expect(JSON.parse(readFileSync(FIXTURE_PATH, "utf8"))).toEqual(fixture);
  });
});
