// SPDX-License-Identifier: MIT
/**
 * The arm's `model_metrics` figures are labelled as the supervisor tier's, and the arm's all-tier
 * `arm_cost_total` and `arm_long_context_invocation_count` are shown beside them when the run
 * publishes them, withheld while pricing is provisional and absent on an older record.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { EvalArmResults } from "./EvalArmResults";
import type { EvalArm } from "./types";

const MODEL = "anthropic.claude-sonnet-5-5";

afterEach(() => {
  cleanup();
});

function arm(overrides: Partial<EvalArm> = {}, metrics: Partial<EvalArm["model_metrics"]> = {}): EvalArm {
  return {
    provenance: {
      mode: "live",
      timestamp: "2026-11-08T00:00:00Z",
      model_id: MODEL,
      effort: "medium",
      dataset_seed: 20261108,
      git_revision: "not-a-commit",
      runtime_identifier: "agentcore",
      price_table_version: "1.3.0",
    },
    model_metrics: {
      model_id: MODEL,
      included_invocation_count: 12,
      cache_read_tokens: 3000,
      cache_write_tokens: 900,
      cost_withheld_provisional: false,
      cost_cache_total: 0.12,
      cost_excluding_cache: 0.35,
      ...metrics,
    },
    aggregate_score: { mean_correctness: 0.5, scored_task_count: 2, unscored_task_count: 0, bad_write_rate: 0 },
    task_scores: [{ task_id: "gt-01", correctness: 1, bad_write: false }],
    ...overrides,
  };
}

/** The value element of the `<dt>` labelled exactly `label` within `container`. */
function fieldValue(container: HTMLElement, label: string): string {
  const term = within(container).getByText(label, { selector: "dt" });
  return term.nextElementSibling?.firstElementChild?.textContent ?? "";
}

function supervisorGroup(): HTMLElement {
  return screen.getByRole("region", { name: "Supervisor tier" });
}

describe("supervisor-scoped arm figures", () => {
  it("places the cache token and cost figures under a Supervisor tier heading", () => {
    render(<EvalArmResults arm={arm()} />);

    const group = supervisorGroup();
    expect(within(group).getByRole("heading", { level: 4 })).toHaveTextContent("Supervisor tier");
    expect(fieldValue(group, "Cache read tokens")).toBe("3000 tokens");
    expect(fieldValue(group, "Cache write tokens")).toBe("900 tokens");
    expect(fieldValue(group, "Cost, cache-attributable")).toBe("$0.12");
    expect(fieldValue(group, "Cost, excluding cache")).toBe("$0.35");
    expect(fieldValue(group, "Included invocations")).toBe("12");
  });

  it("keeps arm-wide figures outside the supervisor group", () => {
    render(<EvalArmResults arm={arm()} />);

    const group = supervisorGroup();
    expect(within(group).queryByText("Mean correctness")).toBeNull();
    expect(within(group).queryByText("Bad-write rate")).toBeNull();
    expect(within(screen.getByRole("region", { name: MODEL })).getByText("Mean correctness")).toBeInTheDocument();
  });

  it("labels the withheld cost fields as supervisor-tier too", () => {
    render(<EvalArmResults arm={arm({}, { cost_withheld_provisional: true })} />);

    expect(fieldValue(supervisorGroup(), "Cost, cache-attributable")).toMatch(/withheld/);
  });
});

describe("all-tier arm figures", () => {
  it("shows the all-tier arm cost and long-context count outside the supervisor group", () => {
    render(
      <EvalArmResults arm={arm({ arm_cost_total: 1.25, arm_long_context_invocation_count: 3 })} />,
    );

    const section = screen.getByRole("region", { name: MODEL });
    expect(fieldValue(section, "Arm cost, all tiers")).toBe("$1.25");
    expect(fieldValue(section, "Long-context invocations, all tiers")).toBe("3");
    const group = supervisorGroup();
    expect(within(group).queryByText("Arm cost, all tiers")).toBeNull();
    expect(within(group).queryByText("Long-context invocations, all tiers")).toBeNull();
  });

  it("shows a zero long-context count as 0, not as absent", () => {
    render(<EvalArmResults arm={arm({ arm_cost_total: 0.4, arm_long_context_invocation_count: 0 })} />);

    expect(fieldValue(screen.getByRole("region", { name: MODEL }), "Long-context invocations, all tiers")).toBe("0");
  });

  it("withholds the all-tier cost while pricing is provisional, still showing the count", () => {
    render(
      <EvalArmResults
        arm={arm(
          { arm_cost_total: null, arm_long_context_invocation_count: 2 },
          { cost_withheld_provisional: true },
        )}
      />,
    );

    const section = screen.getByRole("region", { name: MODEL });
    const shown = fieldValue(section, "Arm cost, all tiers");
    expect(shown).toMatch(/withheld/);
    expect(shown).not.toMatch(/\$/);
    expect(fieldValue(section, "Long-context invocations, all tiers")).toBe("2");
  });

  it("renders an unpriced all-tier cost as not reported, never as $0", () => {
    render(<EvalArmResults arm={arm({ arm_cost_total: null })} />);

    expect(fieldValue(screen.getByRole("region", { name: MODEL }), "Arm cost, all tiers")).toBe("not reported");
  });

  it("shows neither all-tier figure for a run recorded before they were published", () => {
    render(<EvalArmResults arm={arm()} />);

    const section = screen.getByRole("region", { name: MODEL });
    expect(within(section).queryByText("Arm cost, all tiers")).toBeNull();
    expect(within(section).queryByText("Long-context invocations, all tiers")).toBeNull();
  });
});
