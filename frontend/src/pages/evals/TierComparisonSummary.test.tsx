// SPDX-License-Identifier: MIT
/**
 * The tier comparison summary renders the backend's `tier_comparison_table` cell for cell. The
 * fixture is written by `tests/integration/test_tier_comparison_ui_contract.py` from the same
 * function that renders `matrix.md`, so a reformatted cell here diverges from the file.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthContextValue } from "../../auth/AuthProvider";
import { EvalDashboardPage } from "./EvalDashboardPage";
import { HttpEvalRunClient } from "./EvalRunClient";
import { BaseFakeEvalRunClient, jobPayload } from "./testFixtures";
import { TIER_COMPARISON_UNAVAILABLE, TierComparisonSummary } from "./TierComparisonSummary";
import type { EvalArm, EvalTierComparisonTable } from "./types";

type FixtureCase = "multi_arm" | "single_arm";

const FIXTURE = JSON.parse(
  readFileSync(join(__dirname, "__fixtures__", "tierComparison.json"), "utf-8"),
) as Record<FixtureCase, { tier_comparison_table: EvalTierComparisonTable }>;

function fixtureTable(name: FixtureCase): EvalTierComparisonTable {
  return FIXTURE[name].tier_comparison_table;
}

const POLL_MS = 5;

const fakeAuth: AuthContextValue = {
  status: "signed-in",
  identity: {
    userId: "user-1",
    repId: "rep-1",
    book: [],
    fullVisibility: true,
    expiresAt: 9_999_999_999,
  },
  signIn: vi.fn(),
  signOut: vi.fn(),
  getAccessToken: vi.fn().mockResolvedValue("tok-123"),
};

function arm(modelId: string): EvalArm {
  return {
    provenance: {
      mode: "stub",
      timestamp: "2026-11-06T00:00:00Z",
      model_id: modelId,
      effort: "medium",
      dataset_seed: 20261106,
      git_revision: "not-a-commit",
      runtime_identifier: "local-stub",
      price_table_version: "v1",
    },
    model_metrics: { model_id: modelId, included_invocation_count: 2, cost_withheld_provisional: true },
    aggregate_score: { mean_correctness: 0.5, scored_task_count: 2, unscored_task_count: 0, bad_write_rate: 0 },
    task_scores: [{ task_id: "gt-01", correctness: 1.0, bad_write: false }],
  };
}

/** Header cells, then every body row's cells, as the DOM renders them. */
function renderedTable(table: HTMLElement): { headers: string[]; rows: string[][] } {
  const headers = within(table)
    .getAllByRole("columnheader")
    .map((cell) => cell.textContent ?? "");
  const bodyRows = within(table).getAllByRole("row").slice(1);
  const rows = bodyRows.map((row) =>
    Array.from(row.querySelectorAll("th, td")).map((cell) => cell.textContent ?? ""),
  );
  return { headers, rows };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** A note's text as rendered: the markdown code-span markers of a balanced note removed. */
function withoutCodeSpanMarkers(note: string): string {
  return note.split("`").length % 2 === 1 ? note.split("`").join("") : note;
}

describe("TierComparisonSummary — verbatim cells", () => {
  it.each<FixtureCase>(["multi_arm", "single_arm"])(
    "renders every header, cell and note of the %s fixture exactly as sent",
    (name) => {
      const table = fixtureTable(name);
      render(<TierComparisonSummary table={table} />);

      const summary = screen.getByTestId("tier-comparison-summary");
      for (const section of table.sections) {
        const element = within(summary).getByTestId(`tier-comparison-table-${section.tier}`);
        expect(within(element).getByText(section.heading, { selector: "caption" })).toBeInTheDocument();
        expect(renderedTable(element)).toEqual({
          headers: [...table.headers],
          rows: section.rows.map((row) => [...row]),
        });
      }
      const notes = within(summary).getByRole("list", { name: /notes on the tier comparison/i });
      expect(within(notes).getAllByRole("listitem").map((item) => item.textContent)).toEqual(
        table.notes.map(withoutCodeSpanMarkers),
      );
    },
  );

  it("renders the three tiers in the backend's order under their own headings", () => {
    render(<TierComparisonSummary table={fixtureTable("multi_arm")} />);
    const tables = screen.getAllByRole("table");
    expect(tables.map((t) => t.getAttribute("data-testid"))).toEqual([
      "tier-comparison-table-supervisor",
      "tier-comparison-table-subagent",
      "tier-comparison-table-tool",
    ]);
    expect(tables.map((t) => t.querySelector("caption")?.textContent)).toEqual([
      "Supervisor",
      "Subagent",
      "ToolCall",
    ]);
  });

  it("carries the fixture's em dash, withheld, not measured and not applicable cells unchanged", () => {
    render(<TierComparisonSummary table={fixtureTable("multi_arm")} />);
    const cells = screen.getAllByRole("cell").map((cell) => cell.textContent);
    for (const literal of ["—", "withheld", "not measured", "not applicable", "n/a"]) {
      expect(cells, literal).toContain(literal);
    }
  });

  it("gives one column per arm and the label column, for one arm as for two", () => {
    for (const name of ["multi_arm", "single_arm"] as const) {
      const table = fixtureTable(name);
      const { unmount } = render(<TierComparisonSummary table={table} />);
      for (const element of screen.getAllByRole("table")) {
        expect(within(element).getAllByRole("columnheader")).toHaveLength(table.headers.length);
      }
      unmount();
    }
  });

  it("offers no sort control and styles every arm column identically", () => {
    render(<TierComparisonSummary table={fixtureTable("multi_arm")} />);
    const summary = screen.getByTestId("tier-comparison-summary");
    expect(within(summary).queryAllByRole("button")).toEqual([]);
    expect(summary.querySelectorAll("[aria-sort]")).toHaveLength(0);
    const cellClasses = new Set(within(summary).getAllByRole("cell").map((cell) => cell.className));
    expect(cellClasses.size).toBe(1);
    const headerClasses = new Set(
      within(summary)
        .getAllByRole("columnheader")
        .map((cell) => cell.className),
    );
    expect(headerClasses.size).toBe(1);
  });

  it("states that the summary is not available when the run carries no table", () => {
    render(<TierComparisonSummary />);
    expect(screen.getByTestId("tier-comparison-unavailable")).toHaveTextContent(
      TIER_COMPARISON_UNAVAILABLE,
    );
    expect(screen.queryAllByRole("table")).toEqual([]);
  });
});

const LONG_CONTEXT_ROW = "Long-context invocations";

function rowLabels(element: HTMLElement): string[] {
  return within(element)
    .getAllByRole("rowheader")
    .map((cell) => cell.textContent ?? "");
}

describe("TierComparisonSummary — long-context invocations", () => {
  it.each<FixtureCase>(["multi_arm", "single_arm"])(
    "shows the row in every tier of the %s fixture, directly after Model invocations",
    (name) => {
      render(<TierComparisonSummary table={fixtureTable(name)} />);

      for (const tier of ["supervisor", "subagent", "tool"]) {
        const labels = rowLabels(screen.getByTestId(`tier-comparison-table-${tier}`));
        const at = labels.indexOf(LONG_CONTEXT_ROW);
        expect(at, tier).toBeGreaterThan(0);
        expect(labels[at - 1], tier).toBe("Model invocations");
      }
    },
  );

  it("shows an em dash for every arm of a run recorded without the count", () => {
    const table = fixtureTable("multi_arm");
    render(<TierComparisonSummary table={table} />);

    for (const section of table.sections) {
      const row = within(screen.getByTestId(`tier-comparison-table-${section.tier}`))
        .getByRole("rowheader", { name: LONG_CONTEXT_ROW })
        .closest("tr");
      expect(row).not.toBeNull();
      const cells = within(row as HTMLElement).getAllByRole("cell").map((cell) => cell.textContent);
      expect(cells).toEqual(table.headers.slice(1).map(() => "—"));
    }
  });

  it("renders a recorded count exactly as sent, zero included", () => {
    const base = fixtureTable("multi_arm");
    const table: EvalTierComparisonTable = {
      ...base,
      sections: base.sections.map((section) => ({
        ...section,
        rows: section.rows.map((row) =>
          row[0] === LONG_CONTEXT_ROW ? [LONG_CONTEXT_ROW, "0", "3"] : row,
        ),
      })),
    };
    render(<TierComparisonSummary table={table} />);

    const row = within(screen.getByTestId("tier-comparison-table-subagent"))
      .getByRole("rowheader", { name: LONG_CONTEXT_ROW })
      .closest("tr");
    expect(within(row as HTMLElement).getAllByRole("cell").map((cell) => cell.textContent)).toEqual([
      "0",
      "3",
    ]);
  });

  it("defines the row in the notes, matrix.md's wording", () => {
    render(<TierComparisonSummary table={fixtureTable("multi_arm")} />);
    const notes = screen.getByRole("list", { name: /notes on the tier comparison/i });
    expect(notes).toHaveTextContent(/Long-context invocations counts those priced at the model's long-context rates/);
  });
});

describe("TierComparisonSummary — markdown code spans in notes", () => {
  const CODE_NOTE =
    'Effort labels are not equivalent across families; see `effort` under "Reading these figures".';

  it("renders a note's inline code span as <code>, with no backtick left in the text", () => {
    const table = { ...fixtureTable("multi_arm"), notes: [CODE_NOTE] };
    render(<TierComparisonSummary table={table} />);

    const notes = screen.getByRole("list", { name: /notes on the tier comparison/i });
    const [item] = within(notes).getAllByRole("listitem");
    expect(item.textContent).toBe(
      'Effort labels are not equivalent across families; see effort under "Reading these figures".',
    );
    expect([...item.querySelectorAll("code")].map((code) => code.textContent)).toEqual(["effort"]);
  });

  it("renders every code span of a note, and keeps a note with an unpaired backtick verbatim", () => {
    const table = {
      ...fixtureTable("multi_arm"),
      notes: ["a `b` c `d`", "no spans", "one ` stray"],
    };
    render(<TierComparisonSummary table={table} />);

    const notes = screen.getByRole("list", { name: /notes on the tier comparison/i });
    const items = within(notes).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual(["a b c d", "no spans", "one ` stray"]);
    expect(items.map((item) => [...item.querySelectorAll("code")].map((c) => c.textContent))).toEqual(
      [["b", "d"], [], []],
    );
  });
});

describe("EvalDashboardPage — tier comparison summary placement", () => {
  function runWith(status: "completed" | "cancelled", table?: EvalTierComparisonTable) {
    return {
      ...jobPayload({ status }),
      matrix: { arms: [arm("anthropic.claude-sonnet-5-5"), arm("openai.gpt-6-1-sol")] },
      ...(table === undefined ? {} : { tier_comparison_table: table }),
    };
  }

  async function renderFocused(run: ReturnType<typeof runWith>): Promise<void> {
    const client = new BaseFakeEvalRunClient([run]);
    render(
      <EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));
    await waitFor(() => expect(screen.getByTestId("tier-comparison-summary")).toBeInTheDocument(), {
      timeout: 4000,
    });
  }

  it("renders the summary above the per-arm results of a completed run", async () => {
    await renderFocused(runWith("completed", fixtureTable("multi_arm")));
    const summary = screen.getByTestId("tier-comparison-summary");
    const firstArm = screen.getByRole("region", { name: /anthropic\.claude-sonnet-5-5/ });
    expect(summary.compareDocumentPosition(firstArm) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getAllByTestId(/^tier-comparison-table-/)).toHaveLength(3);
  });

  it("renders the summary of a cancelled run's partial results", async () => {
    await renderFocused(runWith("cancelled", fixtureTable("single_arm")));
    expect(screen.getAllByTestId(/^tier-comparison-table-/)).toHaveLength(3);
    expect(screen.queryByTestId("tier-comparison-unavailable")).not.toBeInTheDocument();
  });

  it("states the summary is unavailable for a run recorded without one", async () => {
    await renderFocused(runWith("completed"));
    expect(screen.getByTestId("tier-comparison-unavailable")).toHaveTextContent(
      TIER_COMPARISON_UNAVAILABLE,
    );
  });
});

describe("HttpEvalRunClient — tier_comparison_table parsing", () => {
  function clientReturning(body: unknown): HttpEvalRunClient {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(body) }),
    );
    return new HttpEvalRunClient("/api", { getAccessToken: () => Promise.resolve("tok") });
  }

  it("keeps a well-formed table exactly as sent", async () => {
    const table = fixtureTable("multi_arm");
    const run = await clientReturning({ ...jobPayload({ status: "completed" }), tier_comparison_table: table })
      .getRunStatus("run-1");
    expect(run.tier_comparison_table).toEqual(table);
  });

  it.each<[string, (table: EvalTierComparisonTable) => unknown]>([
    ["a missing tier", (t) => ({ ...t, sections: t.sections.slice(0, 2) })],
    ["tiers out of order", (t) => ({ ...t, sections: [...t.sections].reverse() })],
    ["a numeric cell", (t) => ({ ...t, sections: t.sections.map((s) => ({ ...s, rows: s.rows.map((r) => [r[0], 1, ...r.slice(2)]) })) })],
    ["a short row", (t) => ({ ...t, sections: t.sections.map((s) => ({ ...s, rows: s.rows.map((r) => r.slice(1)) })) })],
    ["a non-string note", (t) => ({ ...t, notes: [7] })],
    ["no headers", (t) => ({ ...t, headers: undefined })],
  ])("drops a table with %s and still returns the run", async (_label, corrupt) => {
    const run = await clientReturning({
      ...jobPayload({ status: "completed" }),
      tier_comparison_table: corrupt(fixtureTable("multi_arm")),
    }).getRunStatus("run-1");
    expect(run.run_id).toBe(jobPayload().run_id);
    expect(run).not.toHaveProperty("tier_comparison_table");
  });
});
