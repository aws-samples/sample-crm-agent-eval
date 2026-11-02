// SPDX-License-Identifier: MIT
/**
 * The measurement-integrity annotations the eval dashboard carries, asserted per arm and attached to
 * the figure each one qualifies:
 *
 * 1. Effort labels are annotated as not comparable across families, inside each arm's own section.
 * 2. The prompt-cache checkpoint minimum is shown per model, and cache-attributable cost is reported
 *    apart from cost excluding cache.
 * 3. A prose outcome renders as `unscored`, including when no reason is stated.
 *
 * Every negative assertion is paired with a positive one on the same section.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { render, screen, waitFor, within, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthContextValue } from "../../auth/AuthProvider";
import { EvalDashboardPage } from "./EvalDashboardPage";
import { HttpEvalRunClient } from "./EvalRunClient";
import type { EvalRunStatusResponse } from "./EvalRunClient";
import { BaseFakeEvalRunClient, jobPayload } from "./testFixtures";
import type { EvalArm, EvalTaskScore } from "./types";

const __dirname = dirname(fileURLToPath(import.meta.url));

/** A short poll interval so a test does not wait the production 3 s. */
const POLL_MS = 5;

afterEach(() => {
  cleanup();
});

const fakeAuth: AuthContextValue = {
  status: "signed-in",
  identity: {
    userId: "user-1",
    repId: "rep-1",
    book: ["acct-1"],
    fullVisibility: false,
    expiresAt: 9_999_999_999,
  },
  signIn: vi.fn(),
  signOut: vi.fn(),
  getAccessToken: vi.fn().mockResolvedValue("tok-123"),
};

interface ArmOptions {
  readonly modelId?: string;
  readonly effort?: string | null;
  /** Set only to build a legacy record; omitted, the provenance carries no `temperature` key. */
  readonly legacyTemperature?: number | null;
  readonly metrics?: Record<string, unknown>;
  readonly taskScores?: readonly EvalTaskScore[];
}

/** An arm as a compliant backend renders it (`ModelMetrics.to_report_fields()`), with one thing
 * overridable per test. */
function buildArm(options: ArmOptions = {}): EvalArm {
  const { modelId = "anthropic.claude-opus-5-5", effort = "low", metrics = {}, taskScores } = options;
  const legacy = "legacyTemperature" in options ? { temperature: options.legacyTemperature } : {};
  return {
    provenance: {
      mode: "stub",
      timestamp: "2026-01-01T00:00:00Z",
      model_id: modelId,
      effort,
      ...legacy,
      dataset_seed: 20260101,
      git_revision: "abc123",
      runtime_identifier: "local-stub",
      price_table_version: "v0",
    },
    model_metrics: {
      model_id: modelId,
      included_invocation_count: 20,
      input_tokens: 8000,
      output_tokens: 1200,
      cache_read_tokens: 3000,
      cache_write_tokens: 900,
      cache_min_checkpoint_tokens: 1024,
      cost_withheld_provisional: true,
      ...metrics,
    },
    aggregate_score: {
      mean_correctness: 0.75,
      scored_task_count: 13,
      unscored_task_count: 7,
      bad_write_rate: 0,
    },
    task_scores: taskScores ?? [
      { task_id: "gt-01", correctness: 1.0, bad_write: false },
      { task_id: "gt-02", correctness: null, bad_write: false, unscored_reason: "prose-only" },
    ],
  };
}

class FakeEvalRunClient extends BaseFakeEvalRunClient {
  constructor(private readonly arms: readonly EvalArm[]) {
    super();
  }

  getRunStatus(): Promise<EvalRunStatusResponse> {
    return Promise.resolve({
      ...jobPayload({ status: "completed" }),
      matrix: { arms: this.arms },
      artifacts: { raw: [] },
    });
  }
}

/** Render the dashboard, trigger a sweep, and wait for the completed run. */
async function renderCompletedRun(arms: readonly EvalArm[]): Promise<void> {
  render(
    <EvalDashboardPage
      apiBaseUrl="/api"
      client={new FakeEvalRunClient(arms)}
      authOverride={fakeAuth}
      pollIntervalMs={POLL_MS}
    />,
  );
  await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));
  await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument());
}

/**
 * One arm's rendered section, located with the browser journey's own selector:
 * `getByRole("region", { name: modelId })`.
 */
function armSection(modelId: string): HTMLElement {
  return screen.getByRole("region", { name: modelId });
}

/**
 * The rendered value paired with the `<dt>` whose text is exactly `label`, within `container`.
 *
 * Reads the `<dd>`'s value element only, excluding any annotation inside the same `<dd>`.
 */
function fieldValue(container: HTMLElement, label: string): string {
  const term = within(container).getByText(label);
  expect(term.tagName, `"${label}" must be a definition term`).toBe("DT");
  const definition = term.nextElementSibling;
  expect(definition?.tagName, `"${label}" must be followed by its value`).toBe("DD");
  const value = definition?.firstElementChild;
  expect(value?.tagName, `"${label}"'s value must be its own element`).toBe("SPAN");
  return value?.textContent ?? "";
}

describe("effort labels are annotated as non-comparable across families", () => {
  const NON_COMPARABILITY = /family-specific label, not a shared scale/i;

  it("annotates the effort label inside every arm's own section, not once for the page", async () => {
    const modelIds = ["anthropic.claude-opus-5-5", "openai.gpt-6-astra"];
    await renderCompletedRun(modelIds.map((modelId) => buildArm({ modelId })));

    for (const modelId of modelIds) {
      const section = armSection(modelId);
      // Positive control: this really is the populated arm section.
      expect(fieldValue(section, "Effort")).toContain("low");
      expect(within(section).getByText(NON_COMPARABILITY)).toBeInTheDocument();
      expect(
        within(section).getByText(/reproducibility, not cross-family comparability/i),
      ).toBeInTheDocument();
    }

    // One annotation per arm, so neither arm is relying on the other's copy.
    expect(screen.getAllByText(NON_COMPARABILITY)).toHaveLength(modelIds.length);
  });

  it("keeps the annotation in the same field as the effort value it qualifies", async () => {
    await renderCompletedRun([buildArm()]);

    const term = screen.getByText("Effort");
    const field = term.closest("div");
    expect(field).not.toBeNull();
    // The annotation and the value share one container.
    expect(within(field as HTMLElement).getByText(NON_COMPARABILITY)).toBeInTheDocument();
    expect((field as HTMLElement).textContent).toContain("low");
  });

  it("still annotates the label when the backend reports no effort at all", async () => {
    // A missing effort keeps the caveat.
    await renderCompletedRun([buildArm({ effort: null })]);

    const section = armSection("anthropic.claude-opus-5-5");
    expect(fieldValue(section, "Effort")).toContain("not set");
    expect(within(section).getByText(NON_COMPARABILITY)).toBeInTheDocument();
  });

  it("states no rung count, so the annotation cannot drift against model_capabilities.yaml", async () => {
    await renderCompletedRun([buildArm()]);

    const annotation = screen.getByText(NON_COMPARABILITY).textContent ?? "";
    expect(annotation).not.toMatch(/\b(?:five|six|seven|\d+)\s+rungs?\b/i);
    expect(annotation.length).toBeGreaterThan(0); // positive control
  });
});

describe("prompt-cache threshold and cache-attributable cost", () => {
  it("shows each arm's own checkpoint minimum, not one value for the matrix", async () => {
    // Differing thresholds, so a hardcoded constant would fail.
    await renderCompletedRun([
      buildArm({ modelId: "model-small-window", metrics: { cache_min_checkpoint_tokens: 512 } }),
      buildArm({ modelId: "model-large-window", metrics: { cache_min_checkpoint_tokens: 4096 } }),
    ]);

    expect(fieldValue(armSection("model-small-window"), "Prompt-cache checkpoint minimum")).toBe(
      "512 tokens",
    );
    expect(fieldValue(armSection("model-large-window"), "Prompt-cache checkpoint minimum")).toBe(
      "4096 tokens",
    );
  });

  it("explains, next to the threshold, that a cost difference can be cache-driven", async () => {
    await renderCompletedRun([buildArm()]);

    const section = armSection("anthropic.claude-opus-5-5");
    expect(fieldValue(section, "Prompt-cache checkpoint minimum")).toBe("1024 tokens");
    expect(
      within(section).getByText(/cost difference can be cache-driven rather than model-driven/i),
    ).toBeInTheDocument();
  });

  it("says a model documents no threshold rather than rendering it as zero or blank", async () => {
    // `null` (no documented minimum) must not render as `0`.
    await renderCompletedRun([buildArm({ metrics: { cache_min_checkpoint_tokens: null } })]);

    const value = fieldValue(armSection("anthropic.claude-opus-5-5"), "Prompt-cache checkpoint minimum");
    expect(value).toBe("no documented threshold");
    expect(value).not.toMatch(/\d/);
  });

  it("keeps 'no documented threshold' distinct from a threshold the wire never carried", async () => {
    // `null` and an absent key render as distinct states.
    await renderCompletedRun([
      buildArm({ modelId: "model-null", metrics: { cache_min_checkpoint_tokens: null } }),
      buildArm({ modelId: "model-absent", metrics: { cache_min_checkpoint_tokens: undefined } }),
    ]);

    const label = "Prompt-cache checkpoint minimum";
    expect(fieldValue(armSection("model-null"), label)).toBe("no documented threshold");
    expect(fieldValue(armSection("model-absent"), label)).toBe("not reported");
    // The absent one is styled as a non-measurement.
    const absentField = within(armSection("model-absent")).getByText(label).closest("div");
    expect(absentField?.className).toContain("border-dashed");
    const nullField = within(armSection("model-null")).getByText(label).closest("div");
    expect(nullField?.className).not.toContain("border-dashed");
  });

  it("reports cache-attributable cost apart from cost excluding cache, with no figure in either", async () => {
    await renderCompletedRun([buildArm()]);

    const section = armSection("anthropic.claude-opus-5-5");
    const cacheCost = fieldValue(section, "Cost, cache-attributable");
    const nonCacheCost = fieldValue(section, "Cost, excluding cache");

    // Two fields, never one summed number.
    expect(cacheCost).toMatch(/withheld — price table is provisional/i);
    expect(nonCacheCost).toMatch(/withheld/i);
    expect(nonCacheCost).toMatch(/never summed into one figure/i);
    // Neither carries a figure while pricing is provisional.
    for (const value of [cacheCost, nonCacheCost]) {
      expect(value).not.toMatch(/\d/);
    }
  });

  it("reports the cache token counts, which are counts and not currency", async () => {
    // Token totals are published even while cost is withheld.
    await renderCompletedRun([buildArm()]);

    const section = armSection("anthropic.claude-opus-5-5");
    expect(fieldValue(section, "Cache read tokens")).toBe("3000 tokens");
    expect(fieldValue(section, "Cache write tokens")).toBe("900 tokens");
  });

  it("says a token count was not reported rather than rendering it as zero", async () => {
    // A missing count is not rendered as `0`, and a real `0` still renders.
    await renderCompletedRun([
      buildArm({ modelId: "model-absent", metrics: { cache_read_tokens: undefined, cache_write_tokens: undefined } }),
      buildArm({ modelId: "model-zero", metrics: { cache_read_tokens: 0, cache_write_tokens: 0 } }),
    ]);

    const absent = armSection("model-absent");
    expect(fieldValue(absent, "Cache read tokens")).toBe("not reported");
    expect(fieldValue(absent, "Cache write tokens")).toBe("not reported");
    // Positive control: a count that *was* sent still renders as a count, including a real zero.
    expect(fieldValue(absent, "Included invocations")).toBe("20");
    expect(fieldValue(armSection("model-zero"), "Cache read tokens")).toBe("0 tokens");
  });

  it("does not render a null token count as the word null", async () => {
    // A `null` count must not render as `"null tokens"`.
    await renderCompletedRun([
      buildArm({ metrics: { cache_read_tokens: null, cache_write_tokens: null } }),
    ]);

    const section = armSection("anthropic.claude-opus-5-5");
    expect(fieldValue(section, "Cache read tokens")).toBe("not reported");
    expect(fieldValue(section, "Cache write tokens")).toBe("not reported");
  });

  it("renders no temperature field for a record that carries no temperature", async () => {
    // A current run records no temperature, so the arm shows neither the label nor a placeholder.
    await renderCompletedRun([buildArm()]);

    const section = armSection("anthropic.claude-opus-5-5");
    expect(within(section).queryByText("Temperature")).toBeNull();
    expect((section.textContent ?? "").toLowerCase()).not.toContain("temperature");
    // Positive control: the neighbouring provenance fields rendered.
    expect(fieldValue(section, "Effort")).toContain("low");
    expect(fieldValue(section, "Included invocations")).toBe("20");
  });

  it("renders a legacy unpinned temperature as recorded, in prose", async () => {
    // A legacy record's `temperature: null` is stated in words and styled as a non-measurement.
    await renderCompletedRun([buildArm({ legacyTemperature: null })]);

    const section = armSection("anthropic.claude-opus-5-5");
    expect(fieldValue(section, "Temperature")).toBe("not set");
    const field = within(section).getByText("Temperature").closest("div");
    expect(field?.className).toContain("border-dashed");
    expect(within(section).getByText(/as recorded by this run/i)).toBeInTheDocument();
  });

  it("renders a legacy pinned temperature as a measurement", async () => {
    // Positive control: a pinned legacy temperature is styled as a figure.
    await renderCompletedRun([buildArm({ legacyTemperature: 0.2 })]);

    const section = armSection("anthropic.claude-opus-5-5");
    expect(fieldValue(section, "Temperature")).toBe("0.2");
    const field = within(section).getByText("Temperature").closest("div");
    expect(field?.className).not.toContain("border-dashed");
  });

  it("discloses that it is not displaying cost when the record omits the withheld flag", async () => {
    // With no withheld flag, the cost field states that cost is not displayed.
    await renderCompletedRun([
      buildArm({ metrics: { cost_withheld_provisional: undefined, cost_total: 12.34 } }),
    ]);

    const section = armSection("anthropic.claude-opus-5-5");
    const value = fieldValue(section, "Cost");
    expect(value).toMatch(/not displayed/i);
    expect(value).toMatch(/does not mark cost as withheld/i);
    expect(value).not.toContain("12.34");
    // The withheld-cost breakdown is absent in this branch.
    expect(within(section).queryByText("Cost, cache-attributable")).toBeNull();
    expect(within(section).queryByText("Cost, excluding cache")).toBeNull();
  });
});

describe("cost figures render only when the arm marks cost not withheld", () => {
  const measuredCost = {
    cost_withheld_provisional: false,
    cost_cache_total: 0.12,
    cost_excluding_cache: 0.35,
  };

  it("shows cache-attributable and excluding-cache cost apart, when not withheld", async () => {
    await renderCompletedRun([buildArm({ metrics: measuredCost })]);

    const section = armSection("anthropic.claude-opus-5-5");
    // The two figures are shown as separate fields, never summed.
    expect(fieldValue(section, "Cost, cache-attributable")).toBe("$0.12");
    expect(fieldValue(section, "Cost, excluding cache")).toBe("$0.35");
    // The cache annotation travels with the cache figure.
    expect(
      within(section).getByText(/never summed into one figure/i),
    ).toBeInTheDocument();
  });

  it("renders a null cost field as 'not reported', never as $0.00", async () => {
    // flag=false with a null cost component renders no figure, not `$0.00`.
    await renderCompletedRun([
      buildArm({ metrics: { ...measuredCost, cost_cache_total: null } }),
    ]);

    const value = fieldValue(armSection("anthropic.claude-opus-5-5"), "Cost, cache-attributable");
    expect(value).toBe("not reported");
    expect(value).not.toContain("$0.00");
    // Positive control: the sibling figure that *was* sent still renders.
    expect(fieldValue(armSection("anthropic.claude-opus-5-5"), "Cost, excluding cache")).toBe("$0.35");
  });

  it("renders an absent (omitted) cost field as 'not reported', never a fabricated figure", async () => {
    // flag=false with an omitted cost component renders no figure.
    await renderCompletedRun([
      buildArm({ metrics: { cost_withheld_provisional: false, cost_excluding_cache: 0.35 } }),
    ]);

    const section = armSection("anthropic.claude-opus-5-5");
    // cost_cache_total was never sent.
    expect(fieldValue(section, "Cost, cache-attributable")).toBe("not reported");
    expect(fieldValue(section, "Cost, excluding cache")).toBe("$0.35"); // positive control
  });

  it("keeps showing the withheld disclosure, not a figure, when the flag is true", async () => {
    // The default fixture withholds; the split disclosure is unchanged.
    await renderCompletedRun([buildArm()]);

    const section = armSection("anthropic.claude-opus-5-5");
    expect(fieldValue(section, "Cost, cache-attributable")).toMatch(/withheld/i);
    expect(fieldValue(section, "Cost, cache-attributable")).not.toMatch(/\$/);
  });
});

describe("P50/P95 latency renders per arm, with 'not measured' for a stub", () => {
  const measuredLatency = {
    latency_measured: true,
    latency_percentile_method: "nearest-rank",
    task_latency_ms_p50: 1200,
    task_latency_ms_p95: 3400,
    call_latency_ms_by_tier: {
      supervisor: { p50: 900, p95: 2000, count: 20 },
      subagent: { p50: 400, p95: 700, count: 12 },
      tool: { p50: 50, p95: 120, count: 40 },
    },
  };

  /** The per-tier latency table within an arm section, located by its caption (the section also
   * holds the per-task table). */
  function callLatencyTable(section: HTMLElement): HTMLElement {
    const caption = within(section).getByText(/per-tier model-call latency/i);
    const table = caption.closest("table");
    expect(table, "the per-tier latency caption must belong to a table").not.toBeNull();
    return table as HTMLElement;
  }

  it("shows task P50 and P95 as millisecond figures for a measured arm", async () => {
    await renderCompletedRun([buildArm({ metrics: measuredLatency })]);

    const section = armSection("anthropic.claude-opus-5-5");
    expect(fieldValue(section, "Task latency (P50)")).toBe("1200 ms");
    expect(fieldValue(section, "Task latency (P95)")).toBe("3400 ms");
  });

  it("renders per-tier call P50/P95 and call counts in an accessible table", async () => {
    await renderCompletedRun([buildArm({ metrics: measuredLatency })]);

    const table = callLatencyTable(armSection("anthropic.claude-opus-5-5"));
    const supervisorRow = within(table).getByRole("rowheader", { name: "Supervisor" }).closest("tr");
    const cells = within(supervisorRow as HTMLElement).getAllByRole("cell");
    expect(cells[0].textContent).toBe("900 ms"); // P50
    expect(cells[1].textContent).toBe("2000 ms"); // P95
    expect(cells[2].textContent).toBe("20"); // Calls
    // A different tier's own figures.
    const toolRow = within(table).getByRole("rowheader", { name: "Tool" }).closest("tr");
    expect(within(toolRow as HTMLElement).getAllByRole("cell")[0].textContent).toBe("50 ms");
  });

  it("offers no sort affordance on the per-tier latency table (measurement, not ranking)", async () => {
    await renderCompletedRun([buildArm({ metrics: measuredLatency })]);

    const table = callLatencyTable(armSection("anthropic.claude-opus-5-5"));
    const headers = within(table).getAllByRole("columnheader");
    expect(headers).toHaveLength(4); // Call tier, P50, P95, Calls
    for (const header of headers) {
      expect(header).not.toHaveAttribute("aria-sort");
      expect(header.querySelector("button")).toBeNull();
    }
  });

  it("says 'not measured' and renders no latency table for a stub arm", async () => {
    // A stub arm reports latency as not measured.
    await renderCompletedRun([buildArm({ metrics: { latency_measured: false } })]);

    const section = armSection("anthropic.claude-opus-5-5");
    expect(fieldValue(section, "Latency")).toMatch(/not measured/i);
    // No task-latency fields and no per-tier table when not measured.
    expect(within(section).queryByText("Task latency (P50)")).toBeNull();
    expect(within(section).queryByText(/per-tier model-call latency/i)).toBeNull();
  });

  it("renders a tier the arm never exercised as a zero-count row, not a dropped one", async () => {
    await renderCompletedRun([
      buildArm({
        metrics: {
          ...measuredLatency,
          call_latency_ms_by_tier: {
            supervisor: { p50: 900, p95: 2000, count: 20 },
            subagent: { p50: 400, p95: 700, count: 12 },
            tool: { p50: null, p95: null, count: 0 },
          },
        },
      }),
    ]);

    const table = callLatencyTable(armSection("anthropic.claude-opus-5-5"));
    const toolRow = within(table).getByRole("rowheader", { name: "Tool" }).closest("tr");
    const cells = within(toolRow as HTMLElement).getAllByRole("cell");
    expect(cells[0].textContent).toBe("not reported"); // P50 — null, not 0 ms
    expect(cells[2].textContent).toBe("0"); // a real zero call count still renders
  });
});

describe("a prose outcome renders as unscored", () => {
  it("renders 'unscored' for a task with no stated reason, never a blank cell", async () => {
    // A reason-less unscored task renders `unscored`, not an empty cell.
    await renderCompletedRun([
      buildArm({
        taskScores: [
          { task_id: "gt-07", correctness: null, bad_write: false },
          { task_id: "gt-08", correctness: 0.5, bad_write: false },
        ],
      }),
    ]);

    const table = screen.getByRole("table", { name: /per-task results/i });
    const unscoredRow = within(table).getByRole("rowheader", { name: "gt-07" }).closest("tr");
    const cells = within(unscoredRow as HTMLElement).getAllByRole("cell");
    expect(cells[0].textContent).toBe("unscored");
    expect(cells[2].textContent).toBe(""); // the reason cell, legitimately empty

      // Positive control: a scored task in the same table still renders its figure.
    const scoredRow = within(table).getByRole("rowheader", { name: "gt-08" }).closest("tr");
    expect(within(scoredRow as HTMLElement).getAllByRole("cell")[0].textContent).toBe("0.5000");
  });
});

describe("the markup satisfies journey 08's selectors", () => {
  // Pins the selectors `tests/e2e/browser/journeys/08-eval-dashboard.spec.ts` uses.
  it("exposes one region per arm named exactly by its model id, plus the artifacts region", async () => {
    const modelIds = ["anthropic.claude-opus-5-5", "openai.gpt-6-astra"];
    await renderCompletedRun(modelIds.map((modelId) => buildArm({ modelId })));

    for (const modelId of modelIds) {
      expect(screen.getAllByRole("region", { name: modelId })).toHaveLength(1);
    }
    expect(screen.getAllByRole("region", { name: /generated artifacts/i })).toHaveLength(1);
    expect(screen.getByRole("heading", { name: /^results$/i })).toBeInTheDocument();
  });

  it("renders one mode-label testid per arm, in the order the backend supplied", async () => {
    // The journey derives rendered arm order from these attributes.
    const modelIds = ["model-weak", "model-strong"];
    await renderCompletedRun(modelIds.map((modelId) => buildArm({ modelId })));

    // Mirrors journey 08's derivation, stripping the `#<effort>` suffix.
    const rendered = Array.from(document.querySelectorAll('[data-testid^="mode-label-"]')).map(
      (node) => (node.getAttribute("data-testid") ?? "").replace("mode-label-", "").split("#")[0],
    );
    expect(rendered).toEqual(modelIds);

    // The attribute is unique per arm.
    const raw = Array.from(document.querySelectorAll('[data-testid^="mode-label-"]')).map(
      (node) => node.getAttribute("data-testid") ?? "",
    );
    expect(new Set(raw).size, `duplicate mode-label testids: ${raw.join(", ")}`).toBe(raw.length);
  });

  it("offers no sort affordance in any column header", async () => {
    await renderCompletedRun([buildArm()]);

    const headers = screen.getAllByRole("columnheader");
    // Positive control: the per-task table's 4 headers plus the run history's 7.
    const perTask = screen.getByRole("table", { name: /per-task results/i });
    const history = screen.getByRole("table", { name: /evaluation sweeps, newest first/i });
    expect(within(perTask).getAllByRole("columnheader")).toHaveLength(4);
    expect(within(history).getAllByRole("columnheader")).toHaveLength(7);
    expect(headers).toHaveLength(11);
    for (const header of headers) {
      expect(header).not.toHaveAttribute("aria-sort");
      expect(header.querySelector("button")).toBeNull();
    }
  });
});

describe("no annotation introduces a comparative claim", () => {
  /** Comparative language an annotation must not contain. */
  const VERDICT_LANGUAGE = [
    "verdict",
    "winner",
    "wins",
    "loser",
    "recommend",
    "best",
    "worst",
    "better",
    "worse",
    "outperform",
    "beats",
    "superior",
    "inferior",
    "leader",
    "runner-up",
    "rank",
  ];

  it("keeps every arm's annotations free of comparative language", async () => {
    // Its positive control asserts the annotation copy rendered into the scanned section.
    const modelIds = ["anthropic.claude-opus-5-5", "openai.gpt-6-astra"];
    await renderCompletedRun(modelIds.map((modelId) => buildArm({ modelId })));

    for (const modelId of modelIds) {
      const text = (armSection(modelId).textContent ?? "").toLowerCase();
      // Positive control: the annotations really did render into this section.
      expect(text).toContain("family-specific label");
      expect(text).toContain("prompt-cache checkpoint minimum");
      for (const forbidden of VERDICT_LANGUAGE) {
        expect(text, `"${forbidden}" appeared in ${modelId}'s annotations`).not.toContain(forbidden);
      }
    }
  });
});

describe("an unrecognised run mode is never read as a spend claim", () => {
  // An unrecognised mode is classified by `classifyRunMode` as neither stub nor live.
  it("discloses an unrecognised mode per arm, claiming neither stub nor billed usage", async () => {
    await renderCompletedRun([buildArm({ metrics: {} , modelId: "model-odd" })].map((arm) => ({
      ...arm,
      provenance: { ...arm.provenance, mode: "replay" },
    })));

    const section = armSection("model-odd");
    const text = (section.textContent ?? "").toLowerCase();

    // Positive control: a populated arm, so the absences below are facts about rendered content.
    expect(text).toContain("mean correctness");
    expect(text).toContain("unrecognised run mode");
    expect(text).toContain("does not establish whether a real model was called");
    // Neither the stub claim nor the billed-usage claim.
    expect(text).not.toContain("made no real model call");
    expect(text).not.toContain("billed usage");
    expect(text).not.toContain("not a validated comparison");
  });

  it("still discloses a recognised stub mode exactly as before", async () => {
    // Positive control: the stub disclosure still renders for a stub run.
    await renderCompletedRun([buildArm()]);

    const text = (armSection("anthropic.claude-opus-5-5").textContent ?? "").toLowerCase();
    expect(text).toContain("stub-mode measurements");
    expect(text).toContain("not a validated comparison");
    expect(text).not.toContain("unrecognised run mode");
  });

  it("gives each arm a unique element id when two arms share a model at different efforts", async () => {
    // Two arms may share a `model_id`; heading ids and React keys are unique per arm.
    await renderCompletedRun([
      buildArm({ modelId: "anthropic.claude-opus-5-5", effort: "low" }),
      buildArm({ modelId: "anthropic.claude-opus-5-5", effort: "high" }),
    ]);

    const ids = Array.from(document.querySelectorAll('[id^="arm-heading-"]')).map((n) => n.id);
    expect(ids).toHaveLength(2);
    expect(new Set(ids).size, `duplicate arm heading ids: ${ids.join(", ")}`).toBe(2);
    // The heading text is the bare model id, the section's accessible name.
    for (const heading of document.querySelectorAll('[id^="arm-heading-"]')) {
      expect(heading.textContent).toBe("anthropic.claude-opus-5-5");
    }
  });
});

describe("a succeeded run with nothing to show says so", () => {
  it("raises an alert instead of a green 'Run complete.' when the matrix is missing", async () => {
    // A success response with no matrix does not render a positive "Run complete." alone.
    const client = new FakeEvalRunClient([]);
    // A succeeded phase with no matrix at all.
    client.getRunStatus = () =>
      Promise.resolve({
      ...jobPayload({ status: "completed" }), artifacts: { raw: [] } });
    render(
      <EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));
    await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument());

    expect(screen.getByRole("alert")).toHaveTextContent(/returned no result matrix/i);
    expect(screen.queryByRole("heading", { name: /^results$/i })).toBeNull();
  });

  it("states that no artifact manifest was reported rather than dropping all four rows", async () => {
    // A missing artifact manifest is stated.
    const client = new FakeEvalRunClient([]);
    client.getRunStatus = () =>
      Promise.resolve({
      ...jobPayload({ status: "completed" }),
        matrix: { arms: [buildArm()] },
      });
    render(
      <EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));
    await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument());

    expect(screen.getByText(/no artifact manifest was reported/i)).toBeInTheDocument();
    // Positive control: the results themselves still rendered, so this is about the manifest only.
    expect(screen.getByRole("heading", { name: /^results$/i })).toBeInTheDocument();
  });
});

describe("a non-conforming payload is reported, never rendered as a measured zero", () => {
  it("renders absent aggregate figures as 'not reported' instead of throwing or fabricating", async () => {
    // Missing numeric keys render as absent without breaking the arm.
    const arm = buildArm();
    await renderCompletedRun([
      {
        ...arm,
        model_metrics: { model_id: "anthropic.claude-opus-5-5" } as EvalArm["model_metrics"],
        aggregate_score: {} as EvalArm["aggregate_score"],
        task_scores: [{ task_id: "gt-01", bad_write: false } as unknown as EvalTaskScore],
      },
    ]);

    const section = armSection("anthropic.claude-opus-5-5");
    expect(fieldValue(section, "Included invocations")).toBe("not reported");
    expect(fieldValue(section, "Mean correctness")).toBe("not reported");
    expect(fieldValue(section, "Bad-write rate")).toBe("not reported");
    expect(fieldValue(section, "Scored / unscored tasks")).toBe("not reported / not reported");
    // The per-task row survived too, rather than the arm failing to render at all.
    expect(within(screen.getByRole("table", { name: /per-task results/i })).getByRole("rowheader", { name: "gt-01" })).toBeInTheDocument();
  });
});

describe("the eval-run contract agrees with the handler", () => {
  // The browser half of `tests/unit/test_eval_run_status_contract.py`.
  it("calls the one deployed route for both operations, with the action as the only body key", async () => {
    // Both operations are `POST /eval_trigger`.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(jobPayload({ status: "queued" })),
    });
    vi.stubGlobal("fetch", fetchMock);
    try {
      const client = new HttpEvalRunClient("/api", {
        getAccessToken: () => Promise.resolve("tok-123"),
      });
      await client.startRun();
      await client.getRunStatus("run-7");

      const calls = fetchMock.mock.calls.map(([url, init]) => ({
        url: String(url),
        method: (init as RequestInit).method,
        body: JSON.parse((init as RequestInit).body as string) as Record<string, unknown>,
      }));
      expect(calls.map((c) => c.url)).toEqual(["/api/eval_trigger", "/api/eval_trigger"]);
      expect(calls.map((c) => c.method)).toEqual(["POST", "POST"]);
      expect(calls[0].body).toEqual({ action: "submit" });
      // The run id travels in the body.
      expect(calls[1].body).toEqual({ action: "status", run_id: "run-7" });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("treats a submitted job as still running, not as a failure", async () => {
    // `submitted` is a running state, not a failure.
    const client = new FakeEvalRunClient([]);
    let polls = 0;
    client.getRunStatus = () => {
      polls += 1;
      return Promise.resolve(
        polls === 1
          ? jobPayload({ status: "queued" })
          : { ...jobPayload({ status: "completed" }), matrix: { arms: [buildArm()] } },
      );
    };
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);
    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));

    // The first poll must not produce an alert.
    await waitFor(() => expect(polls).toBeGreaterThanOrEqual(1));
    expect(screen.queryByRole("alert")).toBeNull();
    await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument(), {
      timeout: 5000,
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("reads is_terminal for the exit condition rather than re-deriving it", async () => {
    // An unrecognised status with `is_terminal: false` keeps polling.
    const client = new FakeEvalRunClient([]);
    let polls = 0;
    client.getRunStatus = () => {
      polls += 1;
      return Promise.resolve(
        polls < 2
          ? { ...jobPayload({ status: "running" }), is_terminal: false }
          : { ...jobPayload({ status: "completed" }), matrix: { arms: [buildArm()] } },
      );
    };
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);
    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));
    await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument(), {
      timeout: 5000,
    });
    expect(polls).toBeGreaterThanOrEqual(2);
  });

  it("reads the mode the backend reported instead of assuming stub", async () => {
    // Asserted with a live value so a hardcoded "stub" would fail.
    const client = new FakeEvalRunClient([]);
    client.getRunStatus = () => Promise.resolve(jobPayload({ status: "running", mode: "live" }));
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);
    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/running live sweep/i));
  });

  it("renders the run provenance the handler emits, so no emitted field is ignored", async () => {
    // `tenant`, `submitted_at` and `finished_at` are rendered as provenance.
    const client = new FakeEvalRunClient([]);
    client.getRunStatus = () =>
      Promise.resolve({
        ...jobPayload({ status: "completed" }),
        tenant: "eval-run-xyz",
        submitted_at: "2026-02-03T04:05:06+00:00",
        finished_at: "2026-02-03T04:09:10+00:00",
        matrix: { arms: [buildArm()] },
      });
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);
    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));
    await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument());

    expect(screen.getByText("Eval tenant").nextElementSibling?.textContent).toBe("eval-run-xyz");
    // Rendered exactly as sent.
    expect(screen.getByText("Submitted at").nextElementSibling?.textContent).toBe(
      "2026-02-03T04:05:06+00:00",
    );
    expect(screen.getByText("Finished at").nextElementSibling?.textContent).toBe(
      "2026-02-03T04:09:10+00:00",
    );
    expect(screen.getByText("Run id").nextElementSibling?.textContent).toBe("run-1");
  });
});

describe("an eval error carries the backend's message, not an empty string", () => {
  // The nested error envelope's message reaches the rendered alert.
  function stubFetch(status: number, body: unknown): void {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status, json: () => Promise.resolve(body) }),
    );
  }

  const client = (): HttpEvalRunClient =>
    new HttpEvalRunClient("/api", { getAccessToken: () => Promise.resolve("tok-123") });

  it("reads the message out of the nested error envelope the handlers actually emit", async () => {
    stubFetch(403, {
      error: { code: "scope_denied", message: "Starting an evaluation sweep requires full tenant visibility." },
    });
    try {
      await expect(client().startRun()).rejects.toMatchObject({
        kind: "scope_denied",
        status: 403,
      });
      // A non-empty message, equal to the backend's own.
      await client()
        .startRun()
        .catch((err: unknown) => {
          expect(err).toBeInstanceOf(Error);
          expect((err as Error).message).toBe(
            "Starting an evaluation sweep requires full tenant visibility.",
          );
          expect((err as Error).message).not.toBe("");
        });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("surfaces missing_scope from the envelope, snake_case on the wire", async () => {
    // The denial names the scope (wire key `missing_scope`, exposed as `missingScope`).
    stubFetch(403, { error: { code: "scope_denied", message: "denied", missing_scope: "eval:trigger" } });
    try {
      await client()
        .getRunStatus("run-1")
        .catch((err: unknown) => {
          expect((err as { body: { missingScope?: string } }).body.missingScope).toBe("eval:trigger");
        });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("falls back to a describable message when the body is not the envelope at all", async () => {
    // A non-envelope error body yields a non-empty fallback and does not throw.
    for (const body of [null, "<html>502</html>", { unexpected: true }, []]) {
      stubFetch(502, body);
      try {
        await client()
          .startRun()
          .catch((err: unknown) => {
            expect((err as Error).message).toMatch(/failed with status 502/);
            expect((err as Error).message).not.toBe("");
          });
      } finally {
        vi.unstubAllGlobals();
      }
    }
  });
});

describe("a job response the page cannot read is refused, not guessed", () => {
  // `status` and `is_terminal` are validated at the boundary; display fields are not.
  function stubOkFetch(body: unknown): void {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve(body) }),
    );
  }

  const client = (): HttpEvalRunClient =>
    new HttpEvalRunClient("/api", { getAccessToken: () => Promise.resolve("tok-123") });

  it.each([
    ["a status outside the known set", { ...jobPayload(), status: "finished" }],
    ["a missing status", { run_id: "r", is_terminal: true }],
    ["a non-boolean is_terminal", { ...jobPayload(), is_terminal: "yes" }],
    ["an absent run_id", { ...jobPayload(), run_id: undefined }],
    ["an empty run_id", { ...jobPayload(), run_id: "" }],
    ["not an object at all", "succeeded"],
  ])("refuses %s rather than deriving a run state from it", async (_label, body) => {
    stubOkFetch(body);
    try {
      await expect(client().getRunStatus("run-1")).rejects.toMatchObject({
        body: { code: "eval_response_unreadable" },
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("accepts a conforming payload, so the refusals above are not refusing everything", async () => {
    // Positive control: a valid payload is accepted.
    stubOkFetch(jobPayload({ status: "running" }));
    try {
      await expect(client().getRunStatus("run-1")).resolves.toMatchObject({
        run_id: "run-1",
        status: "running",
        is_terminal: false,
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("does not refuse a payload whose display fields are the wrong type", async () => {
    // A bad display field (`tenant`) passes the boundary.
    stubOkFetch({ ...jobPayload({ status: "running" }), tenant: 42 });
    try {
      await expect(client().getRunStatus("run-1")).resolves.toMatchObject({ status: "running" });
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("the status-to-kind taxonomy is declared once, not twice", () => {
  /* The client classifies statuses through `CrmApiClient`'s exported `errorKindForStatus`. */
  const clientSource = readFileSync(join(__dirname, "EvalRunClient.ts"), "utf-8");

  it("declares no local status-to-kind map", () => {
    // No local `errorKindForStatus` and no inline respelling of the `ApiErrorKind` union.
    expect(clientSource).not.toMatch(/function\s+errorKindForStatus/);
    expect(clientSource).not.toMatch(/"authentication_failed"\s*\|\s*"scope_denied"/);
    expect(clientSource).not.toMatch(/case\s+40[0-9]\s*:/);
  });

  it("imports the shared map and uses it", () => {
    // Positive control: the shared map is imported and called.
    expect(clientSource).toMatch(/import\s*\{[^}]*\berrorKindForStatus\b[^}]*\}\s*from\s*"\.\.\/\.\.\/api\/CrmApiClient"/s);
    expect(clientSource).toContain("errorKindForStatus(res.status)");
  });

  it("classifies through the shared map at runtime, not merely at import", () => {
    // Driven through the real client.
    const cases = [
      [401, "authentication_failed"],
      [403, "scope_denied"],
      [404, "not_found"],
      [400, "validation_failed"],
      [409, "conflict"],
      [503, "unknown"],
    ] as const;
    return (async () => {
      for (const [status, kind] of cases) {
        vi.stubGlobal(
          "fetch",
          vi.fn().mockResolvedValue({
            ok: false,
            status,
            json: () => Promise.resolve({ error: { code: kind, message: "denied" } }),
          }),
        );
        try {
          const client = new HttpEvalRunClient("/api", {
            getAccessToken: () => Promise.resolve("tok-123"),
          });
          await expect(client.startRun()).rejects.toMatchObject({ kind, status });
        } finally {
          vi.unstubAllGlobals();
        }
      }
    })();
  });
});
