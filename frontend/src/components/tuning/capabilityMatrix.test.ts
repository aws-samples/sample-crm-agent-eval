// SPDX-License-Identifier: MIT
/**
 * Tests for the pure capability-matrix parse/query helpers, over matrix shapes mirroring
 * `config/model_capabilities.yaml`'s entries (Opus 5.5, Sonnet 5.5, Haiku 5.5, Astra, GPT-6.1 Sol,
 * Luna).
 */
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it, vi, afterEach } from "vitest";

import { CrmApiError, errorKindForStatus } from "../../api/CrmApiClient";

/** The removed tuning parameter, which neither the producer nor this parser may carry. */
const REMOVED_TUNING_PARAMETER = "temperature";
import {
  effortOptionsFor,
  FAMILIES,
  fetchCapabilityMatrix,
  modelFor,
  modelsOfFamily,
  TIERS,
  parseCapabilityMatrix,
  resolvedEffort,
  type ModelCapability,
} from "./capabilityMatrix";

/** `rawMatrix()` with Opus 5.5's effort marked unsupported, for the no-effort-parameter paths. */
function rawMatrixWithoutOpusEffort() {
  const raw = rawMatrix();
  (raw.models["anthropic.claude-opus-5-5"] as Record<string, unknown>).effort = {
    supported: false,
    values: [],
    default: null,
    capped_at_when_thinking_disabled: null,
  };
  return raw;
}

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

describe("parseCapabilityMatrix", () => {
  it("parses a full matrix into the camelCase TypeScript shape", () => {
    const matrix = parseCapabilityMatrix(rawMatrix());
    expect(matrix.defaultFamily).toBe("anthropic");
    expect("allowCrossFamilyOverride" in matrix).toBe(false);
    expect(matrix.models["anthropic.claude-opus-5-5"]?.effort.values).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
    expect(matrix.tiers.anthropic.supervisor.modelId).toBe("anthropic.claude-opus-5-5");
  });

  it("rejects a non-object payload", () => {
    expect(() => parseCapabilityMatrix(null)).toThrow(/expected a JSON object/);
  });

  it("rejects a payload missing a required top-level key", () => {
    expect(() => parseCapabilityMatrix({ default_family: "anthropic" })).toThrow(
      /missing key/,
    );
  });
});

describe("modelFor", () => {
  it("resolves the model assigned to a family's tier", () => {
    const matrix = parseCapabilityMatrix(rawMatrix());
    expect(modelFor(matrix, "anthropic", "tool").modelId).toBe("anthropic.claude-haiku-5-5");
    expect(modelFor(matrix, "openai", "supervisor").modelId).toBe("openai.gpt-6-astra");
  });
});

describe("effortOptionsFor", () => {
  it("returns the model's accepted values when effort is supported", () => {
    const matrix = parseCapabilityMatrix(rawMatrix());
    expect(effortOptionsFor(modelFor(matrix, "anthropic", "supervisor"))).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
  });

  it("returns undefined for a model that accepts no effort parameter at all", () => {
    const matrix = parseCapabilityMatrix(rawMatrixWithoutOpusEffort());
    expect(effortOptionsFor(modelFor(matrix, "anthropic", "supervisor"))).toBeUndefined();
  });

  it("offers Haiku 5.5's effort rungs on the tool tier", () => {
    const matrix = parseCapabilityMatrix(rawMatrix());
    expect(effortOptionsFor(modelFor(matrix, "anthropic", "tool"))).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
  });

  it("caps Sonnet 5.5 below Opus 5.5 — no xhigh or max in Sonnet 5.5's option list", () => {
    const matrix = parseCapabilityMatrix(rawMatrix());
    const sonnetOptions = effortOptionsFor(modelFor(matrix, "anthropic", "subagent"));
    expect(sonnetOptions).toEqual(["low", "medium", "high"]);
    expect(sonnetOptions).not.toContain("xhigh");
    expect(sonnetOptions).not.toContain("max");
  });

  it("includes max on every GPT-6 tier", () => {
    // All three GPT-6 tiers list max; Sonnet 5.5 is the remaining effort exclusion (asserted above).
    const matrix = parseCapabilityMatrix(rawMatrix());
    expect(effortOptionsFor(modelFor(matrix, "openai", "supervisor"))).toContain("max");
    expect(effortOptionsFor(modelFor(matrix, "openai", "subagent"))).toContain("max");
    expect(effortOptionsFor(modelFor(matrix, "openai", "tool"))).toContain("max");
  });
});

describe("resolvedEffort", () => {
  it("resolves an unset effort on the tier's configured model to the tier's configured effort", () => {
    // Opus 5.5's default is medium; the anthropic supervisor tier configures low.
    const matrix = parseCapabilityMatrix(rawMatrix());
    const opus = modelFor(matrix, "anthropic", "supervisor");
    expect(opus.effort.default).toBe("medium");
    expect(resolvedEffort(matrix, "anthropic", "supervisor", opus, null)).toBe("low");
  });

  it("resolves an unset effort on a model the tier does not configure to that model's default", () => {
    const matrix = parseCapabilityMatrix(rawMatrix());
    const sonnet = modelFor(matrix, "anthropic", "subagent");
    expect(resolvedEffort(matrix, "anthropic", "supervisor", sonnet, null)).toBe("high");
    expect(resolvedEffort(matrix, "anthropic", "subagent", sonnet, null)).toBe("medium");
  });

  it("falls back to the model's default when the configured tier sets no effort", () => {
    const raw = rawMatrix();
    raw.tiers.anthropic.supervisor = { model_id: "anthropic.claude-opus-5-5", tuning: {} } as never;
    const matrix = parseCapabilityMatrix(raw);
    const opus = modelFor(matrix, "anthropic", "supervisor");
    expect(resolvedEffort(matrix, "anthropic", "supervisor", opus, null)).toBe("medium");
  });

  it("returns null when the model accepts no effort parameter and none is requested or configured", () => {
    const matrix = parseCapabilityMatrix(rawMatrixWithoutOpusEffort());
    const opus = modelFor(matrix, "anthropic", "supervisor");
    expect(resolvedEffort(matrix, "anthropic", "subagent", opus, null)).toBeNull();
  });

  it("passes through an explicit request unchanged", () => {
    const matrix = parseCapabilityMatrix(rawMatrix());
    const model: ModelCapability = modelFor(matrix, "anthropic", "supervisor");
    expect(resolvedEffort(matrix, "anthropic", "supervisor", model, "xhigh")).toBe("xhigh");
  });
});

/**
 * Contract with `providers.tier_tuning.resolve_tier`: the fixture is written by
 * `tests/integration/test_effort_resolver_contract.py` from the server resolver and the capability
 * payload the handler serves; this resolver must give the same effort for every cell.
 */
describe("resolvedEffort agrees with the server resolver on every cell", () => {
  interface ResolvedCell {
    readonly family: (typeof FAMILIES)[number];
    readonly tier: (typeof TIERS)[number];
    readonly model: string | null;
    readonly requested: string | null;
    readonly resolved_model: string;
    readonly resolved_effort: string | null;
  }
  const fixture = JSON.parse(
    readFileSync(
      resolve(process.cwd(), "..", "tests", "integration", "fixtures", "resolved_efforts.json"),
      "utf8",
    ),
  ) as { capabilities: unknown; cells: readonly ResolvedCell[] };
  const matrix = parseCapabilityMatrix(fixture.capabilities);

  it("parses the served default family, openai", () => {
    expect(matrix.defaultFamily).toBe("openai");
  });

  it("read cells covering every family and tier, so the comparison below is not vacuous", () => {
    for (const family of FAMILIES) {
      for (const tier of TIERS) {
        expect(
          fixture.cells.filter((cell) => cell.family === family && cell.tier === tier).length,
          `${family}/${tier}`,
        ).toBeGreaterThan(1);
      }
    }
  });

  it.each(
    fixture.cells.map((cell) => [
      `${cell.family}/${cell.tier} model=${cell.model ?? "default"} effort=${cell.requested ?? "unset"}`,
      cell,
    ]),
  )("%s", (_label, cell) => {
    const model =
      cell.model === null ? modelFor(matrix, cell.family, cell.tier) : matrix.models[cell.model];
    expect(model?.modelId).toBe(cell.resolved_model);
    expect(
      resolvedEffort(matrix, cell.family, cell.tier, model as ModelCapability, cell.requested),
    ).toBe(cell.resolved_effort);
  });
});

describe("fetchCapabilityMatrix", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const tokenSource = { getAccessToken: () => Promise.resolve("tok-123") };

  it("GETs {apiBaseUrl}/capabilities with the caller's Authorization bearer token and parses the body", async () => {
    // The request carries the bearer token, as every other /api/* call does.
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: () => Promise.resolve(rawMatrix()) });
    vi.stubGlobal("fetch", fetchMock);

    const matrix = await fetchCapabilityMatrix("/api", tokenSource);

    expect(fetchMock).toHaveBeenCalledWith("/api/capabilities", {
      method: "GET",
      headers: { Authorization: "Bearer tok-123" },
    });
    expect(matrix.defaultFamily).toBe("anthropic");
  });

  it("throws on a non-2xx response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 503 }));
    await expect(fetchCapabilityMatrix("/api", tokenSource)).rejects.toThrow(/503/);
  });
});

/**
 * The error envelope on this route, read from both sides: the nested snake_case field names are
 * parsed out of `src/crm/errors.py`, and a `scope_denied` names the missing scope.
 */
describe("fetchCapabilityMatrix surfaces the envelope src/crm/errors.py actually emits", () => {
  const tokenSource = { getAccessToken: () => Promise.resolve("tok-123") };

  const errorsPy = readFileSync(resolve(process.cwd(), "..", "src", "crm", "errors.py"), "utf8");

  function respondWith(status: number, json: () => Promise<unknown>): void {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status, json } as unknown as Response));
  }

  /** Reject, and return the reason, so a resolved promise cannot pass as a caught error. */
  async function rejectionOf(promise: Promise<unknown>): Promise<unknown> {
    const sentinel = Symbol("resolved");
    const outcome = await promise.then(() => sentinel).catch((err: unknown) => err);
    expect(outcome).not.toBe(sentinel);
    return outcome;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("found errors.py, so the assertions below are not comparing two absences", () => {
    expect(errorsPy).toContain("class ScopeDenied");
    expect(errorsPy).toContain("def to_api_response");
  });

  it("reads the authority: message nests under `error`, and the scope field is `missing_scope`", () => {
    expect(errorsPy).toMatch(/"error":\s*\{"code":\s*self\.code,\s*"message":\s*self\.message\}/);
    expect(errorsPy).toMatch(/payload\["error"\]\["missing_scope"\]\s*=\s*self\.missing_scope/);
    // No camelCase spelling of the field appears in the authority.
    expect(errorsPy).not.toContain("missingScope");
  });

  it("recovers the handler's message and missing scope from a real ScopeDenied envelope", async () => {
    // The exact bytes a `ScopeDenied` renders through API Gateway.
    const wire = JSON.parse(
      '{"statusCode": 403, "error": {"code": "scope_denied", "message": "Model tuning is outside your book.", "missing_scope": "tuning:write"}}',
    ) as unknown;
    respondWith(403, () => Promise.resolve(wire));

    const caught = await rejectionOf(fetchCapabilityMatrix("/api", tokenSource));

    expect(caught).toBeInstanceOf(CrmApiError);
    const error = caught as CrmApiError;
    expect(error.kind).toBe("scope_denied");
    expect(error.status).toBe(403);
    expect(error.body.message).toBe("Model tuning is outside your book.");
    expect(error.body.missingScope).toBe("tuning:write");
    expect(error.body.code).toBe("scope_denied");
    // `useCapabilityMatrix` renders `err.message`.
    expect(error.message).toBe("Model tuning is outside your book.");
    // A status code alone is not the missing scope.
    expect(error.message).not.toMatch(/responded 403/);
  });

  it("classifies status onto the same taxonomy CrmApiClient uses, not a second copy", async () => {
    for (const status of [401, 403, 404, 400, 409, 503]) {
      respondWith(status, () => Promise.resolve({ error: { code: "x", message: "m" } }));
      const error = (await rejectionOf(fetchCapabilityMatrix("/api", tokenSource))) as CrmApiError;
      expect(error.kind).toBe(errorKindForStatus(status));
      vi.unstubAllGlobals();
    }
  });

  it.each([
    ["an HTML error page from the edge", () => Promise.reject(new SyntaxError("Unexpected token <"))],
    ["a null body", () => Promise.resolve(null)],
    ["a body that is not the envelope", () => Promise.resolve({ oops: true })],
    ["the dev stub's flat shape, whose `error` is a string", () =>
      Promise.resolve({ error: "capability_matrix_unavailable", message: "no interpreter" })],
  ])("does not throw out of the error path for %s", async (_label, json) => {
    // The error path itself never throws.
    respondWith(502, json);
    const error = (await rejectionOf(fetchCapabilityMatrix("/api", tokenSource))) as CrmApiError;
    expect(error).toBeInstanceOf(CrmApiError);
    expect(error.body.message).toContain("responded 502");
    expect(error.body.missingScope).toBeUndefined();
  });
});

/**
 * `parseCapabilityMatrix` validates every field it reads, rather than casting the wire body. The
 * first block asserts that the real payload still parses.
 */
describe("parseCapabilityMatrix accepts the real payload", () => {
  const repoRoot = resolve(process.cwd(), "..");

  let realMatrixJson = "";
  try {
    realMatrixJson = execFileSync(
      join(repoRoot, ".venv", "bin", "python3"),
      [
        "-c",
        "import dataclasses, json; from providers.capabilities import load_capability_matrix; " +
          "print(json.dumps(dataclasses.asdict(load_capability_matrix())))",
      ],
      { cwd: repoRoot, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
    );
  } catch {
    realMatrixJson = "";
  }

  it("could read the real matrix at all — otherwise the assertion below verifies nothing", () => {
    expect(
      realMatrixJson.length,
      "load_capability_matrix() produced nothing; the .venv may be missing",
    ).toBeGreaterThan(0);
  });

  it("parses what providers.capabilities actually serialises, so validation is not over-strict", () => {
    // The payload of `load_capability_matrix`, the one Python reader of `config/model_capabilities.yaml`.
    const matrix = parseCapabilityMatrix(JSON.parse(realMatrixJson));
    for (const family of FAMILIES) {
      for (const tier of TIERS) {
        expect(modelFor(matrix, family, tier).modelId).toEqual(expect.any(String));
      }
    }
    // The real producer carries no cross-family key, and no model entry has a field this parser
    // drops: every key it serialises per model is one `ModelCapability` mirrors or ignores by name.
    const raw = JSON.parse(realMatrixJson) as { models: Record<string, Record<string, unknown>> };
    expect("allow_cross_family_override" in raw).toBe(false);
    for (const [modelId, entry] of Object.entries(raw.models)) {
      expect(Object.keys(entry), modelId).not.toContain(REMOVED_TUNING_PARAMETER);
      expect(Object.keys(matrix.models[modelId] ?? {}), modelId).not.toContain(
        REMOVED_TUNING_PARAMETER,
      );
    }
    // The key/`model_id` reconciliation holds for the real producer.
    for (const [key, model] of Object.entries(matrix.models)) {
      expect(model.modelId).toBe(key);
    }
  });
});

describe("parseCapabilityMatrix rejects a malformed payload, naming the field", () => {
  /** `rawMatrix()` with one field replaced at `path`, so each case differs in exactly one read. */
  function withField(path: readonly string[], value: unknown): unknown {
    const root = JSON.parse(JSON.stringify(rawMatrix())) as Record<string, unknown>;
    let cursor: Record<string, unknown> = root;
    for (const key of path.slice(0, -1)) {
      cursor = cursor[key] as Record<string, unknown>;
    }
    cursor[path[path.length - 1]] = value;
    return root;
  }

  const OPUS = ["models", "anthropic.claude-opus-5-5"] as const;

  it.each([
    ["a non-boolean effort.supported", [...OPUS, "effort", "supported"], "yes", /effort\.supported must be a boolean/],
    ["a non-array effort.values", [...OPUS, "effort", "values"], "low", /effort\.values must be an array/],
    ["a non-string inside effort.values", [...OPUS, "effort", "values"], ["low", 7], /effort\.values\[1\] must be a string/],
    ["an undefined effort.default", [...OPUS, "effort", "default"], undefined, /effort\.default must be a string/],
    ["a non-object effort", [...OPUS, "effort"], "high", /effort must be a JSON object/],
    ["a non-string model_id", [...OPUS, "model_id"], 5, /model_id must be a string/],
    ["an unknown family", [...OPUS, "family"], "mistral", /family must be one of anthropic, openai, got "mistral"/],
    ["an unknown tier", [...OPUS, "tier"], "orchestrator", /tier must be one of supervisor, subagent, tool/],
    ["an unknown default_family", ["default_family"], "cohere", /default_family must be one of/],
    ["a non-object models", ["models"], [], /models must be a JSON object/],
    ["an array where a tier block belongs", ["tiers", "anthropic"], [], /tiers\.anthropic must be a JSON object/],
    ["a non-string tier model_id", ["tiers", "anthropic", "supervisor", "model_id"], null, /tiers\.anthropic\.supervisor\.model_id must be a string/],
    ["a non-string tuning value", ["tiers", "anthropic", "supervisor", "tuning"], { effort: true }, /tuning\.effort must be a string/],
    ["a numeric tuning value", ["tiers", "anthropic", "supervisor", "tuning"], { effort: 0.2 }, /tuning\.effort must be a string/],
  ] as const)("rejects %s", (_label, path, value, expected) => {
    expect(() => parseCapabilityMatrix(withField(path, value))).toThrow(expected);
  });

  it("requires tiers to cover every family x tier, because modelFor indexes it as a total record", () => {
    // `modelFor` reads `matrix.tiers[family][tier]` unguarded, so every block is required.
    const raw = JSON.parse(JSON.stringify(rawMatrix())) as {
      tiers: { anthropic: Record<string, unknown> };
    };
    delete raw.tiers.anthropic.subagent;
    expect(() => parseCapabilityMatrix(raw)).toThrow(/tiers\.anthropic\.subagent must be a JSON object/);
  });

  it("no longer requires allow_cross_family_override, and ignores it when an old producer sends it", () => {
    const raw = rawMatrix() as unknown as Record<string, unknown>;
    expect("allow_cross_family_override" in raw).toBe(false);
    expect(() => parseCapabilityMatrix(raw)).not.toThrow();
    expect(() => parseCapabilityMatrix({ ...raw, allow_cross_family_override: false })).not.toThrow();
  });

  it("still accepts null where null is meaningful — no default, no cap", () => {
    // `requireNullableString` accepts null for `default` and `capped_at_when_thinking_disabled`.
    const matrix = parseCapabilityMatrix(rawMatrixWithoutOpusEffort());
    const opus = modelFor(matrix, "anthropic", "supervisor");
    expect(opus.effort.default).toBeNull();
    expect(opus.effort.cappedAtWhenThinkingDisabled).toBeNull();
    expect(modelFor(matrix, "anthropic", "tool").effort.cappedAtWhenThinkingDisabled).toBe("high");
  });

  it("drops a model field it does not mirror, so a stale producer's extra field never reaches the UI", () => {
    const raw = JSON.parse(JSON.stringify(rawMatrix())) as {
      models: Record<string, Record<string, unknown>>;
    };
    raw.models["openai.gpt-6-astra"][REMOVED_TUNING_PARAMETER] = { supported: true };
    const model = parseCapabilityMatrix(raw).models["openai.gpt-6-astra"];
    expect(Object.keys(model ?? {})).not.toContain(REMOVED_TUNING_PARAMETER);
  });

  /** What is not checked. */
  it("ignores unknown fields rather than rejecting them, so the Python side may add any", () => {
    // Fields this client does not model (`inference_profile`, `context_window_tokens`, …) are ignored.
    const raw = rawMatrix() as unknown as Record<string, unknown>;
    raw.some_future_field = { nested: true };
    expect(() => parseCapabilityMatrix(raw)).not.toThrow();
  });

  /**
   * A `models` key and its entry's `model_id` must agree: `load_capability_matrix` builds the map as
   * `models[model_id] = ModelCapability(model_id=model_id, …)`, so a payload where they differ did
   * not come from that producer.
   */
  it("rejects a models key that disagrees with its own model_id, naming the path", () => {
    const mismatched = JSON.parse(JSON.stringify(rawMatrix())) as {
      models: Record<string, { model_id: string }>;
    };
    mismatched.models["anthropic.claude-opus-5-5"].model_id = "anthropic.something-else";
    expect(() => parseCapabilityMatrix(mismatched)).toThrow(
      /models\["anthropic\.claude-opus-5-5"\]\.model_id is "anthropic\.something-else", which does not match its key/,
    );
  });

  it("accepts every key that does agree, so the reconciliation is not rejecting the normal case", () => {
    // Negative control: `rawMatrix()` has six agreeing entries.
    const matrix = parseCapabilityMatrix(rawMatrix());
    for (const [key, model] of Object.entries(matrix.models)) {
      expect(model.modelId).toBe(key);
    }
    expect(Object.keys(matrix.models)).toHaveLength(6);
  });
});

describe("modelsOfFamily", () => {
  it("lists every model of the family, in tier order, and none of the other family", () => {
    const matrix = parseCapabilityMatrix(rawMatrix());
    expect(modelsOfFamily(matrix, "anthropic").map((m) => m.modelId)).toEqual([
      "anthropic.claude-opus-5-5",
      "anthropic.claude-sonnet-5-5",
      "anthropic.claude-haiku-5-5",
    ]);
    expect(modelsOfFamily(matrix, "openai").every((m) => m.family === "openai")).toBe(true);
    expect(modelsOfFamily(matrix, "openai")).toHaveLength(3);
  });
});
