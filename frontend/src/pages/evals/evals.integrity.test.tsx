// SPDX-License-Identifier: MIT
/**
 * Measurement-integrity guard for the eval dashboard: adversarial payloads must not make the rendered
 * page show a currency figure it cannot confirm publishable, or any verdict, winner, or ranking.
 *
 * Every negative assertion is paired with a positive one proving the dashboard rendered its figures.
 * Ranking is checked as order preservation and the absence of sort affordances.
 */
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthContextValue } from "../../auth/AuthProvider";
import { EvalArmResults } from "./EvalArmResults";
import { EvalDashboardPage } from "./EvalDashboardPage";
import { BaseFakeEvalRunClient, jobPayload } from "./testFixtures";

/** A short poll interval so a test does not wait the production 3 s. */
const POLL_MS = 5;
import type { EvalArm, EvalTierRoleModelMetric } from "./types";

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

/**
 * An arm with the withholding flag set, as a correct backend renders it. Overrides vary one thing
 * per test.
 */
function buildArm(
  modelId = "anthropic.claude-opus-5-5",
  metricsOverrides: Record<string, unknown> = {},
): EvalArm {
  return {
    provenance: {
      mode: "stub",
      timestamp: "2026-01-01T00:00:00Z",
      model_id: modelId,
      effort: "low",
      dataset_seed: 20260101,
      git_revision: "abc123",
      runtime_identifier: "local-stub",
      price_table_version: "v0",
    },
    model_metrics: {
      model_id: modelId,
      included_invocation_count: 20,
      cost_withheld_provisional: true,
      ...metricsOverrides,
    },
    aggregate_score: {
      mean_correctness: 0.75,
      scored_task_count: 13,
      unscored_task_count: 7,
      bad_write_rate: 0,
    },
    task_scores: [
      { task_id: "gt-01", correctness: 1.0, bad_write: false },
      { task_id: "gt-02", correctness: null, bad_write: false, unscored_reason: "prose-only" },
    ],
  };
}

class FakeEvalRunClient extends BaseFakeEvalRunClient {}

/** Render the dashboard with `arms` and wait for the completed run. Returns the page text. */
async function renderCompletedRun(arms: readonly EvalArm[]): Promise<string> {
  const client = new FakeEvalRunClient([
    {
      ...jobPayload({ status: "completed" }),
      matrix: { arms },
      artifacts: { raw: [] },
    },
  ]);
  render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);
  await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));
  await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument());
  return document.body.textContent ?? "";
}

/**
 * Currency-shaped text: a symbol or code adjacent to a number, in either order. A bare decimal is
 * not matched.
 */
const CURRENCY_SHAPED =
  /(?:[$£€¥₹¢]\s*\d|\d\s*[$£€¥₹¢]|\b(?:USD|EUR|GBP|JPY|CAD|AUD|CHF|INR)\b|\d\s*(?:dollars?|cents?|euros?|pounds?)\b)/i;

describe("dashboard integrity — the negative assertions are not vacuous", () => {
  it("renders real figures, so 'no currency appears' is a fact about a populated page", async () => {
    const text = await renderCompletedRun([buildArm()]);

    // Positive control first.
    expect(text).toContain("anthropic.claude-opus-5-5");
    expect(text).toContain("20"); // included_invocation_count
    expect(text).toContain("0.7500"); // mean_correctness, rendered
    expect(screen.getByRole("table", { name: /per-task results/i })).toBeInTheDocument();
    expect(screen.getByText(/withheld — price table is provisional/i)).toBeInTheDocument();

    expect(text).not.toMatch(CURRENCY_SHAPED);
  });

  it("the currency matcher itself detects a currency figure", () => {
    // The matcher fires on the shapes it covers and not on a bare correctness score.
    for (const sample of ["$12.34", "12.34 USD", "€5", "0.75 dollars", "45¢"]) {
      expect(sample).toMatch(CURRENCY_SHAPED);
    }
    for (const sample of ["0.7500", "20", "2026-01-01T00:00:00Z", "abc123"]) {
      expect(sample).not.toMatch(CURRENCY_SHAPED);
    }
  });
});

describe("dashboard integrity — adversarial backend payloads", () => {
  it("renders no currency figure when the backend omits the withheld flag but sends a cost", async () => {
    // A dropped `cost_withheld_provisional` flag renders no currency figure.
    const text = await renderCompletedRun([
      buildArm("anthropic.claude-opus-5-5", {
        cost_withheld_provisional: undefined,
        cost_total: 12.34,
      }),
    ]);

    expect(text).toContain("anthropic.claude-opus-5-5"); // positive control
    expect(text).not.toContain("12.34");
    expect(text).not.toMatch(CURRENCY_SHAPED);
  });

  it("renders the cost figures when the backend explicitly says not withheld", async () => {
    // An explicit `cost_withheld_provisional: false` shows the two split figures.
    const text = await renderCompletedRun([
      buildArm("anthropic.claude-opus-5-5", {
        cost_withheld_provisional: false,
        cost_cache_total: 0.12,
        cost_excluding_cache: 0.35,
      }),
    ]);

    expect(text).toContain("anthropic.claude-opus-5-5"); // positive control
    // The two cost fields render, apart, as currency.
    expect(text).toContain("$0.12");
    expect(text).toContain("$0.35");
    expect(text).toMatch(CURRENCY_SHAPED);
    // No withheld disclosure appears alongside a shown figure.
    expect(text).not.toMatch(/withheld — price table is provisional/i);
  });

  it("renders a null cost field as absent, never $0.00, even when not withheld", async () => {
    // flag=false with an omitted cost component renders no `$0.00`.
    const text = await renderCompletedRun([
      buildArm("anthropic.claude-opus-5-5", {
        cost_withheld_provisional: false,
        cost_cache_total: null,
        cost_excluding_cache: 0.35,
      }),
    ]);

    expect(text).toContain("$0.35"); // the sibling that was sent still renders (positive control)
    expect(text).not.toContain("$0.00");
  });

  it("renders no currency figure from an unrecognised cost-bearing field", async () => {
    // An unknown cost field name passed through opaquely is not rendered.
    const text = await renderCompletedRun([
      buildArm("anthropic.claude-opus-5-5", {
        cost_usd: 99.99,
        total_spend_usd: 42,
        price_per_1k_tokens: 0.003,
      }),
    ]);

    expect(text).toContain("anthropic.claude-opus-5-5");
    for (const figure of ["99.99", "42", "0.003"]) {
      expect(text).not.toContain(figure);
    }
    expect(text).not.toMatch(CURRENCY_SHAPED);
  });

  it("renders a verdict field the backend invents nowhere on the page", async () => {
    const text = await renderCompletedRun([
      buildArm("anthropic.claude-opus-5-5", {
        verdict: "anthropic wins",
        winner: true,
        rank: 1,
        recommendation: "prefer this family",
      }),
    ]);

    expect(text).toContain("anthropic.claude-opus-5-5");
    expect(text.toLowerCase()).not.toContain("wins");
    expect(text.toLowerCase()).not.toContain("prefer this family");
  });
});

/**
 * Comparative language that constitutes a verdict when applied to a measurement.
 *
 * Scanned against each arm's own rendered section, not the whole page, whose integrity disclosure
 * legitimately says no model is labelled better.
 */
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
];

/** Return each arm's rendered section text, keyed by model id. */
function armSectionTexts(modelIds: readonly string[]): Record<string, string> {
  const texts: Record<string, string> = {};
  for (const modelId of modelIds) {
    const heading = screen.getByRole("heading", { name: modelId });
    const section = heading.closest("section");
    expect(section).not.toBeNull();
    texts[modelId] = (section?.textContent ?? "").toLowerCase();
  }
  return texts;
}

describe("dashboard integrity — verdict and winner language", () => {
  it("renders no verdict or comparative language inside either arm's measurements", async () => {
    const modelIds = ["anthropic.claude-opus-5-5", "openai.gpt-5"];
    await renderCompletedRun([buildArm(modelIds[0]), buildArm(modelIds[1])]);

    const sections = armSectionTexts(modelIds);
    for (const modelId of modelIds) {
      const text = sections[modelId];
      // Positive control: the section rendered its measurements.
      expect(text).toContain("mean correctness");
      expect(text).toContain("included invocations");
      for (const forbidden of VERDICT_LANGUAGE) {
        expect(text, `"${forbidden}" appeared in ${modelId}'s measurements`).not.toContain(
          forbidden,
        );
      }
    }
  });

  it("states that it draws no comparison, rather than merely omitting one", async () => {
    // The page-level disclosure that no comparison is drawn is present.
    const text = (
      await renderCompletedRun([buildArm("anthropic.claude-opus-5-5"), buildArm("openai.gpt-5")])
    ).toLowerCase();
    expect(text).toContain("does not label either model as better");
  });

  it("discloses stub mode per arm, so figures cannot be read as a validated comparison", async () => {
    const modelIds = ["anthropic.claude-opus-5-5", "openai.gpt-5"];
    await renderCompletedRun([buildArm(modelIds[0]), buildArm(modelIds[1])]);

    const sections = armSectionTexts(modelIds);
    for (const modelId of modelIds) {
      expect(sections[modelId]).toContain("stub-mode measurements");
      expect(sections[modelId]).toContain("not a validated comparison");
    }
  });

  it("renders no verdict or comparative language in the cost-completeness notes", () => {
    const arm: EvalArm = { ...buildArm(), usage_lower_bound_task_ids: ["gt-04"] };
    render(<EvalArmResults arm={arm} tasksFailed={2} />);

    const text = (screen.getByRole("region", { name: "anthropic.claude-opus-5-5" }).textContent ?? "")
      .toLowerCase();
    // Positive control: both notes rendered into the scanned section.
    expect(text).toContain("are a lower bound");
    expect(text).toContain("ended in an error");
    for (const forbidden of [...VERDICT_LANGUAGE, "failed", "pass"]) {
      expect(text, `"${forbidden}" appeared in the cost-completeness notes`).not.toContain(forbidden);
    }
  });

  it("renders no verdict or comparative language inside a legacy temperature-era arm", async () => {
    const modelIds = ["anthropic.claude-opus-5", "openai.gpt-5.6-sol"];
    await renderCompletedRun(
      modelIds.map((modelId) => {
        const arm = buildArm(modelId);
        return { ...arm, provenance: { ...arm.provenance, mode: "live", temperature: null } };
      }),
    );

    const sections = armSectionTexts(modelIds);
    for (const modelId of modelIds) {
      const text = sections[modelId];
      // Positive control: the legacy temperature field and its note rendered.
      expect(text).toContain("temperature");
      expect(text).toContain("as recorded by this run");
      for (const forbidden of VERDICT_LANGUAGE) {
        expect(text, `"${forbidden}" appeared in ${modelId}'s measurements`).not.toContain(
          forbidden,
        );
      }
    }
  });
});

describe("three-tier / per-tool / spread blocks uphold the integrity properties", () => {
  /** A stub arm carrying the three-tier blocks; `cost` merges onto the one tier row. */
  function armWithBlocks(
    modelId = "anthropic.claude-opus-5-5",
    cost: Partial<EvalTierRoleModelMetric> = {},
  ): EvalArm {
    return {
      ...buildArm(modelId),
      tier_role_model_metrics: [
        {
          tier: "supervisor",
          role: "supervisor",
          model_id: modelId,
          included_invocation_count: 4,
          input_tokens: 1200,
          output_tokens: 340,
          cache_read_tokens: 0,
          cache_write_tokens: 512,
          call_latency_measured: true,
          call_latency_ms_p50: 810,
          call_latency_ms_p95: 950,
          ...cost,
        },
      ],
      tool_metrics: [
        {
          tool_name: "search_opportunities",
          role: "research",
          count: 3,
          call_latency_ms_p50: 120,
          call_latency_ms_p95: 400,
          truncated_count: 1,
          outcome_distribution: { data: 2, no_match: 1, denied: 0, tool_failure: 0 },
        },
      ],
      task_spread: [
        {
          task_id: "gt-01",
          repeats: 5,
          correctness_min: 0.2,
          correctness_mean: 0.6,
          correctness_max: 1.0,
          success_fraction: 0.4,
        },
      ],
    };
  }

  it("renders token/latency/spread figures but no currency when the tier cost is withheld", async () => {
    const text = await renderCompletedRun([
      armWithBlocks("anthropic.claude-opus-5-5", { cost_withheld_provisional: true }),
    ]);
    // Positive control: the blocks actually rendered their non-currency figures.
    expect(text).toContain("search_opportunities");
    expect(text).toContain("1200"); // a token count
    expect(text).toContain("0.6000"); // spread mean
    // A withheld tier row prints no currency.
    expect(text).not.toMatch(CURRENCY_SHAPED);
  });

  it("renders the two tier cost figures apart when explicitly not withheld, never summed", async () => {
    const text = await renderCompletedRun([
      armWithBlocks("anthropic.claude-opus-5-5", {
        cost_withheld_provisional: false,
        cost_excluding_cache: 0.35,
        cost_cache_total: 0.12,
      }),
    ]);
    expect(text).toContain("$0.35"); // excluding cache
    expect(text).toContain("$0.12"); // cache-attributable
    expect(text).toMatch(CURRENCY_SHAPED);
    // Never combined into one figure.
    expect(text).not.toContain("$0.47");
  });

  it("renders a null tier cost as absent, never $0.00, even when not withheld", async () => {
    const text = await renderCompletedRun([
      armWithBlocks("anthropic.claude-opus-5-5", {
        cost_withheld_provisional: false,
        cost_cache_total: null,
        cost_excluding_cache: 0.35,
      }),
    ]);
    expect(text).toContain("$0.35"); // the sibling that was sent still renders (positive control)
    expect(text).not.toContain("$0.00");
  });

  it("renders no tier cost when the withheld flag is omitted but a cost is sent", async () => {
    // An absent flag renders no cost.
    const text = await renderCompletedRun([
      armWithBlocks("anthropic.claude-opus-5-5", { cost_excluding_cache: 12.34 }),
    ]);
    expect(text).toContain("search_opportunities"); // positive control
    expect(text).not.toContain("12.34");
    expect(text).not.toMatch(CURRENCY_SHAPED);
  });

  it("renders no currency from an unrecognised tier cost field name", async () => {
    const text = await renderCompletedRun([
      armWithBlocks("anthropic.claude-opus-5-5", {
        cost_withheld_provisional: false,
        cost_usd: 99.99,
        total_spend_usd: 42,
      }),
    ]);
    expect(text).toContain("search_opportunities");
    for (const figure of ["99.99", "42"]) {
      expect(text).not.toContain(figure);
    }
  });

  it("introduces no verdict or comparative language in the arm's new blocks", async () => {
    const modelId = "anthropic.claude-opus-5-5";
    await renderCompletedRun([armWithBlocks(modelId, { cost_withheld_provisional: true })]);
    const section = armSectionTexts([modelId])[modelId];
    expect(section).toContain("per-tool calls and outcomes"); // positive control
    for (const forbidden of VERDICT_LANGUAGE) {
      expect(section, `"${forbidden}" appeared in the new blocks`).not.toContain(forbidden);
    }
  });

  it("adds no sortable or sorted column affordance to the new tables", async () => {
    await renderCompletedRun([armWithBlocks("anthropic.claude-opus-5-5", { cost_withheld_provisional: true })]);
    const headers = screen.getAllByRole("columnheader");
    // Positive control: the three-tier tables added their headers.
    expect(headers.length).toBeGreaterThan(4);
    for (const header of headers) {
      expect(header).not.toHaveAttribute("aria-sort");
      expect(header.querySelector("button")).toBeNull();
    }
  });
});

describe("ranking — what is mechanised, and what is not", () => {
  /**
   * Arms render in the order the backend supplied, never sorted by a metric, and no column carries a
   * sort affordance.
   */
  it("renders arms in backend order, not sorted by any score", async () => {
    // Supplied worst-first; a ranking dashboard would reorder these.
    const weak = buildArm("model-weak");
    const strong = buildArm("model-strong");
    const arms: EvalArm[] = [
      { ...weak, aggregate_score: { ...weak.aggregate_score, mean_correctness: 0.1 } },
      { ...strong, aggregate_score: { ...strong.aggregate_score, mean_correctness: 0.9 } },
    ];

    const text = await renderCompletedRun(arms);

    const weakIndex = text.indexOf("model-weak");
    const strongIndex = text.indexOf("model-strong");
    expect(weakIndex).toBeGreaterThanOrEqual(0);
    expect(strongIndex).toBeGreaterThanOrEqual(0);
    expect(weakIndex).toBeLessThan(strongIndex);
  });

  it("renders no sortable or sorted column affordance in the results table", async () => {
    // No `aria-sort` and no sort control.
    await renderCompletedRun([buildArm()]);

    const headers = screen.getAllByRole("columnheader");
    expect(headers.length).toBeGreaterThan(0); // positive control
    for (const header of headers) {
      expect(header).not.toHaveAttribute("aria-sort");
      expect(header.querySelector("button")).toBeNull();
    }
  });

  it("exposes no numeric position, rank, or ordinal for any arm", async () => {
    const text = await renderCompletedRun([
      buildArm("anthropic.claude-opus-5-5"),
      buildArm("openai.gpt-5"),
    ]);
    expect(text).toContain("openai.gpt-5"); // positive control
    expect(text.toLowerCase()).not.toMatch(/\brank(?:ing|ed)?\b/);
    expect(text).not.toMatch(/\b(?:1st|2nd|3rd)\b/);
  });
});
