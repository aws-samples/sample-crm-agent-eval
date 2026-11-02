// SPDX-License-Identifier: MIT
/**
 * Tests for the eval dashboard: the stub trigger, the admin-only live and cancel affordances,
 * resuming server state on mount, polling to terminal, run history, cancelled and failed runs, the
 * per-tier summary, progress rendering, artifact links, table header semantics, and a source scan
 * for verdict/winner/recommendation language.
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { render, screen, waitFor, within, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthContextValue } from "../../auth/AuthProvider";
import { EvalArmResults } from "./EvalArmResults";
import { EvalDashboardPage } from "./EvalDashboardPage";
import { HttpEvalRunClient, MAX_SWEEP_REPEATS, type EvalRunStatusResponse } from "./EvalRunClient";
import { LiveSweepDialog } from "./LiveSweepDialog";
import { BaseFakeEvalRunClient, estimatePayload, jobPayload } from "./testFixtures";
import { erroredItemsCostNote, runRepeats, usageLowerBoundNote, type EvalArm } from "./types";

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

function buildArm(overrides: Partial<EvalArm["provenance"]> = {}): EvalArm {
  return {
    provenance: {
      mode: "stub",
      timestamp: "2026-01-01T00:00:00Z",
      model_id: "anthropic.claude-opus-5-5",
      effort: "low",
      dataset_seed: 20260101,
      git_revision: "abc123",
      runtime_identifier: "local-stub",
      price_table_version: "v0",
      ...overrides,
    },
    model_metrics: {
      model_id: "anthropic.claude-opus-5-5",
      included_invocation_count: 20,
      cost_withheld_provisional: true,
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

describe("EvalDashboardPage — stub sweeps only", () => {
  it("renders with no run triggered", () => {
    const client = new FakeEvalRunClient([]);
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);

    expect(screen.getByText(/no run is in progress/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /run stub sweep/i })).toBeEnabled();
  });

  it("clicking 'Run stub sweep' triggers exactly one run", async () => {
    const client = new FakeEvalRunClient([
      {
      ...jobPayload({ status: "completed" }), matrix: { arms: [buildArm()] }, artifacts: { raw: [] } },
    ]);
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);

    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));

    await waitFor(() => expect(client.startCallCount).toBe(1));
  });
});

describe("EvalDashboardPage — a non-admin sees no live affordance", () => {
  it("renders no live toggle, no spend acknowledgement, and no live trigger button", () => {
    const client = new FakeEvalRunClient([]);
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);

    // `queryBy*` so absence is the assertion; a disabled live control would also fail.
    expect(screen.queryByLabelText(/enable live mode/i)).toBeNull();
    expect(screen.queryByLabelText(/spend real money/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /live/i })).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("no rendered control can start a run other than the stub button", async () => {
    const client = new FakeEvalRunClient([]);
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);

    const buttons = screen.getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual([
      "Run stub sweep (zero cost, no credentials)",
    ]);
    await userEvent.click(buttons[0]);
    expect(client.startCallCount).toBe(1);
  });

  it("the start-run request body carries no live or mode field", async () => {
    // Asserts the real HTTP client's request body carries no key that could request a live sweep.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(jobPayload({ status: "queued" })),
    });
    vi.stubGlobal("fetch", fetchMock);
    try {
      const httpClient = new HttpEvalRunClient("/api", {
        getAccessToken: () => Promise.resolve("tok-123"),
      });
      await httpClient.startRun();

      expect(fetchMock).toHaveBeenCalledTimes(1);
      const init = fetchMock.mock.calls[0][1] as RequestInit;
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      expect(Object.keys(body).sort()).toEqual(["action"]);
      expect(body.action).toBe("submit");
      for (const forbidden of ["live", "mode", "substrate", "provider"]) {
        expect(Object.keys(body), `the start-run body must carry no ${forbidden} key`).not.toContain(
          forbidden,
        );
      }
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("the dashboard source contains no live-trigger affordance", () => {
    // A source scan for a live-trigger control.
    const source = readFileSync(join(__dirname, "EvalDashboardPage.tsx"), "utf-8");
    const withoutComments = source
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/\/\/.*$/gm, "");
    expect(withoutComments).not.toMatch(/type="checkbox"/);
    expect(withoutComments).not.toMatch(/startRun\s*\(\s*(?:true|false|\{)/);
  });
});

describe("EvalDashboardPage — progress and results rendering", () => {
  it("shows a running status while polling, then renders results on success", async () => {
    const client = new FakeEvalRunClient([
      { ...jobPayload({ status: "running" }) },
      {
        ...jobPayload({ status: "completed" }),
        matrix: { arms: [buildArm()] },
        artifacts: {
          matrix_json: "/results/matrix.json",
          matrix_md: "/results/matrix.md",
          matrix_csv: "/results/matrix.csv",
          raw: ["/results/raw/run-1-anthropic.json"],
        },
      },
    ]);
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);

    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));

    await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument(), {
      timeout: 4000,
    });

    // All four artifact kinds present, each opened through a presigned URL rather than a raw path
    // (`evals.artefacts.test.tsx`).
    for (const name of [/matrix\.json/i, /matrix\.md/i, /matrix\.csv/i, "/results/raw/run-1-anthropic.json"]) {
      expect(screen.getByRole("button", { name })).toBeEnabled();
    }
    expect(screen.queryByRole("link", { name: /matrix\.json/i })).not.toBeInTheDocument();
  });

  it("renders a failure message when the run fails", async () => {
    const client = new FakeEvalRunClient([
      {
      ...jobPayload({ status: "failed" }), error: "tenant teardown failed" },
    ]);
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);

    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/tenant teardown failed/i));
  });

  it("never displays a cost figure when the backend withholds it", async () => {
    const client = new FakeEvalRunClient([
      {
        ...jobPayload({ status: "completed" }),
        matrix: { arms: [buildArm()] }, // cost_withheld_provisional: true, no cost_total field
        artifacts: { raw: [] },
      },
    ]);
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);

    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));
    await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument());

    expect(screen.getByText(/withheld — price table is provisional/i)).toBeInTheDocument();
    // No dollar-sign or bare numeric cost rendering anywhere.
    expect(screen.queryByText(/\$\d/)).not.toBeInTheDocument();
  });

  it("renders an unscored task as the word 'unscored', never as an invented score", async () => {
    // `gt-02` in the fixture is a prose-only outcome (`correctness: null`); asserted on the cell.
    const client = new FakeEvalRunClient([
      {
        ...jobPayload({ status: "completed" }),
        matrix: { arms: [buildArm()] },
        artifacts: { raw: [] },
      },
    ]);
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);
    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));
    await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument());

    const table = screen.getByRole("table", { name: /per-task results/i });
    const unscoredRow = within(table).getByRole("rowheader", { name: "gt-02" }).closest("tr");
    expect(unscoredRow).not.toBeNull();
    const [correctnessCell] = within(unscoredRow as HTMLElement).getAllByRole("cell");

    expect(correctnessCell.textContent).toBe("unscored");
    // The unscored reason is rendered with it.
    expect(within(unscoredRow as HTMLElement).getByText("prose-only")).toBeInTheDocument();

    // Positive control: the scored task alongside it still renders its figure.
    const scoredRow = within(table).getByRole("rowheader", { name: "gt-01" }).closest("tr");
    expect(within(scoredRow as HTMLElement).getAllByRole("cell")[0].textContent).toBe("1.0000");
  });

  it("renders an unscored aggregate as the word 'unscored', never as an invented score", async () => {
    // An arm whose every task is unscored renders no mean, not `0.0000`.
    const arm = buildArm();
    const client = new FakeEvalRunClient([
      {
      ...jobPayload({ status: "completed" }),
        matrix: {
          arms: [
            {
              ...arm,
              aggregate_score: {
                ...arm.aggregate_score,
                mean_correctness: null,
                scored_task_count: 0,
                unscored_task_count: 20,
              },
            },
          ],
        },
        artifacts: { raw: [] },
      },
    ]);
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);
    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));
    await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument());

    const meanValue = screen.getByText("Mean correctness").nextElementSibling;
    expect(meanValue?.textContent).toBe("unscored");

    // Positive control: the arm rendered its other measurements.
    expect(screen.getByText("Included invocations").nextElementSibling?.textContent).toBe("20");
    expect(screen.getByText("Scored / unscored tasks").nextElementSibling?.textContent).toBe(
      "0 / 20",
    );
  });

  it("results table uses proper header semantics", async () => {
    const client = new FakeEvalRunClient([
      {
        ...jobPayload({ status: "completed" }),
        matrix: { arms: [buildArm()] },
        artifacts: { raw: [] },
      },
    ]);
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);
    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));
    await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument());

    const table = screen.getByRole("table", { name: /per-task results/i });
    const columnHeaders = within(table).getAllByRole("columnheader");
    expect(columnHeaders.map((h) => h.textContent)).toEqual([
      "Task",
      "Correctness",
      "Bad write",
      "Notes",
    ]);
    const rowHeaders = within(table).getAllByRole("rowheader");
    expect(rowHeaders.map((h) => h.textContent)).toEqual(["gt-01", "gt-02"]);
  });
});

describe("EvalDashboardPage — three-tier / per-tool / per-task-spread blocks", () => {
  /** An arm carrying the three-tier measurement blocks. Tiers/tools are supplied out of
   * alphabetical order to check backend order is preserved. */
  function armWithBlocks(): EvalArm {
    return {
      ...buildArm(),
      tier_role_model_metrics: [
        {
          tier: "supervisor",
          role: "supervisor",
          model_id: "anthropic.claude-opus-5-5",
          included_invocation_count: 4,
          input_tokens: 1200,
          output_tokens: 340,
          cache_read_tokens: 0,
          cache_write_tokens: 512,
          call_latency_measured: true,
          call_latency_ms_p50: 810,
          call_latency_ms_p95: 950,
          cost_withheld_provisional: false,
          cost_excluding_cache: 0.35,
          cost_cache_total: 0.12,
        },
        {
          tier: "tool",
          role: "mutation",
          model_id: "anthropic.claude-haiku-4-5",
          included_invocation_count: 2,
          input_tokens: 90,
          output_tokens: 12,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          call_latency_measured: false,
          call_latency_ms_p50: null,
          call_latency_ms_p95: null,
          cost_withheld_provisional: true,
        },
      ],
      tool_metrics: [
        {
          tool_name: "zzz_late_tool",
          role: "research",
          count: 3,
          call_latency_ms_p50: 120,
          call_latency_ms_p95: 400,
          truncated_count: 1,
          outcome_distribution: { data: 2, no_match: 1, denied: 0, tool_failure: 0 },
        },
        {
          tool_name: "aaa_early_tool",
          role: "mutation",
          count: 1,
          call_latency_ms_p50: null,
          call_latency_ms_p95: null,
          truncated_count: 0,
          outcome_distribution: { data: 0, no_match: 0, denied: 1, tool_failure: 0 },
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

  async function renderArm(arm: EvalArm): Promise<HTMLElement> {
    const client = new FakeEvalRunClient([
      { ...jobPayload({ status: "completed" }), matrix: { arms: [arm] }, artifacts: { raw: [] } },
    ]);
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);
    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));
    await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument());
    const section = screen.getByRole("heading", { name: "anthropic.claude-opus-5-5" }).closest("section");
    expect(section).not.toBeNull();
    return section as HTMLElement;
  }

  it("renders the per-(tier,role,model) row verbatim: tokens, latency, and the two cost figures apart", async () => {
    const section = await renderArm(armWithBlocks());
    const table = within(section).getByRole("table", { name: /per-tier \/ role \/ model usage/i });
    const supRow = within(table).getByRole("rowheader", { name: "supervisor" }).closest("tr");
    const cells = within(supRow as HTMLElement).getAllByRole("cell");
    // Role, Model, Invocations, Input, Output, Cache read, Cache write, P50, P95, Cost excl., Cost cache.
    expect(cells.map((c) => c.textContent)).toEqual([
      "supervisor",
      "anthropic.claude-opus-5-5",
      "4",
      "1200",
      "340",
      "0",
      "512",
      "810 ms",
      "950 ms",
      "$0.35",
      "$0.12",
    ]);
    // The two cost figures are never summed into one on the page.
    expect(supRow?.textContent).not.toContain("0.47");
  });

  it("renders an unmeasured, cost-withheld tier row as '—' for latency and cost, never 0 ms or $0.00", async () => {
    const section = await renderArm(armWithBlocks());
    const table = within(section).getByRole("table", { name: /per-tier \/ role \/ model usage/i });
    const toolTierRow = within(table).getByRole("rowheader", { name: "tool" }).closest("tr");
    const cells = within(toolTierRow as HTMLElement).getAllByRole("cell");
    expect(cells.map((c) => c.textContent)).toEqual([
      "mutation",
      "anthropic.claude-haiku-4-5",
      "2",
      "90",
      "12",
      "0",
      "0",
      "—", // latency P50
      "—", // latency P95
      "—", // cost excl. cache (withheld)
      "—", // cost, cache (withheld)
    ]);
    expect(toolTierRow?.textContent).not.toMatch(/not reported/);
    expect(toolTierRow?.textContent).not.toMatch(/0\s*ms/);
    expect(toolTierRow?.textContent).not.toContain("$0.00");
  });

  it("renders per-tool rows with the full four-way outcome distribution", async () => {
    const section = await renderArm(armWithBlocks());
    const table = within(section).getByRole("table", { name: /per-tool calls and outcomes/i });
    // Every outcome kind is a column, present even at zero.
    for (const label of ["Data", "No match", "Denied", "Tool failure"]) {
      expect(within(table).getByRole("columnheader", { name: label })).toBeInTheDocument();
    }
    const lateToolRow = within(table).getByRole("rowheader", { name: "zzz_late_tool" }).closest("tr");
    const cells = within(lateToolRow as HTMLElement).getAllByRole("cell");
    // Role, Calls, P50, P95, Truncated, then data/no_match/denied/tool_failure.
    expect(cells.map((c) => c.textContent)).toEqual([
      "research",
      "3",
      "120 ms",
      "400 ms",
      "1",
      "2",
      "1",
      "0",
      "0",
    ]);
  });

  it("renders per-tool rows in backend order, never sorted by name", async () => {
    const section = await renderArm(armWithBlocks());
    const table = within(section).getByRole("table", { name: /per-tool calls and outcomes/i });
    const toolRowHeaders = within(table)
      .getAllByRole("rowheader")
      .map((h) => h.textContent);
    // Supplied worst-alphabetical-first; a dashboard that sorted would swap these.
    expect(toolRowHeaders).toEqual(["zzz_late_tool", "aaa_early_tool"]);
  });

  it("renders the per-task correctness spread", async () => {
    const section = await renderArm(armWithBlocks());
    const table = within(section).getByRole("table", {
      name: /per-task correctness spread across repeats/i,
    });
    const row = within(table).getByRole("rowheader", { name: "gt-01" }).closest("tr");
    const cells = within(row as HTMLElement).getAllByRole("cell");
    // Repeats, Min, Mean, Max, Success fraction.
    expect(cells.map((c) => c.textContent)).toEqual(["5", "0.2000", "0.6000", "1.0000", "0.4000"]);
  });

  it("renders none of the three blocks for an arm that carries no attributions", async () => {
    const section = await renderArm(buildArm());
    expect(within(section).queryByRole("table", { name: /per-tier \/ role \/ model/i })).toBeNull();
    expect(within(section).queryByRole("table", { name: /per-tool calls/i })).toBeNull();
    expect(within(section).queryByRole("table", { name: /per-task correctness spread/i })).toBeNull();
  });
});

describe("EvalDashboardPage — accessible cost disclosure on the one trigger", () => {
  it("the stub button has an accessible description naming the cost implication", () => {
    const client = new FakeEvalRunClient([]);
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);

    const trigger = screen.getByRole("button", { name: /run stub sweep/i });
    const describedBy = trigger.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    const description = document.getElementById(describedBy as string);
    expect(description?.textContent).toMatch(/no inference cost/i);
  });

  it("tells a non-admin nothing about a live control", async () => {
    const client = new FakeEvalRunClient([]);
    render(<EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={POLL_MS} />);

    await waitFor(() => expect(client.listCursors).toEqual([undefined]));
    expect(screen.queryByText(/admins only/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /live/i })).toBeNull();
  });
});

describe("EvalDashboardPage source — no verdict language anywhere", () => {
  it("contains none of 'verdict', 'winner', 'recommendation' outside of comments in any file in this directory", () => {
    // Comments are stripped first, so only rendered strings and identifiers are scanned.
    const forbidden = /verdict|winner|recommendation/i;
    const files = readdirSync(__dirname).filter(
      (name) => (name.endsWith(".ts") || name.endsWith(".tsx")) && !name.includes(".test."),
    );
    const offenders: string[] = [];
    for (const name of files) {
      const contents = readFileSync(join(__dirname, name), "utf-8");
      const withoutBlockComments = contents.replace(/\/\*[\s\S]*?\*\//g, "");
      const withoutLineComments = withoutBlockComments.replace(/\/\/.*$/gm, "");
      if (forbidden.test(withoutLineComments)) {
        offenders.push(name);
      }
    }
    expect(offenders).toEqual([]);
  });
});

/** An unsigned compact JWT naming `groups` in `cognito:groups`; only its payload is decoded. */
function idTokenWithGroups(groups: readonly string[]): string {
  const part = (value: object): string =>
    btoa(JSON.stringify(value)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${part({ alg: "none" })}.${part({ sub: "admin-1", "cognito:groups": groups })}.sig`;
}

const adminAuth: AuthContextValue = {
  ...fakeAuth,
  getAccessToken: vi.fn().mockResolvedValue(idTokenWithGroups(["admin"])),
};

function renderPage(client: BaseFakeEvalRunClient, auth: AuthContextValue = fakeAuth): void {
  render(
    <EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={auth} pollIntervalMs={POLL_MS} />,
  );
}

/** Wait long enough for several further polls to have fired, had polling continued. */
function settle(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, POLL_MS * 8));
}

/** A fake whose status replies wait for `releaseStatus()`, holding the page in its pre-poll state. */
class HeldStatusClient extends FakeEvalRunClient {
  private releaseGate: () => void = () => undefined;
  private readonly gate = new Promise<void>((resolve) => {
    this.releaseGate = resolve;
  });

  releaseStatus(): void {
    this.releaseGate();
  }

  override async getRunStatus(runId: string): Promise<EvalRunStatusResponse> {
    await this.gate;
    return super.getRunStatus(runId);
  }
}

describe("EvalDashboardPage — server state on mount", () => {
  it("resumes the newest non-terminal run and renders its results when it finishes, with no reload", async () => {
    const running = jobPayload({ status: "running", run_id: "run-2", tasks_done: 7 });
    const client = new HeldStatusClient(
      [{ ...jobPayload({ status: "completed", run_id: "run-2" }), matrix: { arms: [buildArm()] }, artifacts: { raw: [] } }],
      { history: [[running, jobPayload({ status: "completed", run_id: "run-1" })]] },
    );
    renderPage(client);

    expect(await screen.findByText(/running stub sweep \(run_id=run-2\)… 7\/40 tasks/i)).toBeInTheDocument();
    client.releaseStatus();
    await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument());
    expect(screen.getByRole("region", { name: "anthropic.claude-opus-5-5" })).toBeInTheDocument();
    expect(client.startCallCount).toBe(0);
    // The history row followed the poll.
    const history = screen.getByRole("table", { name: /evaluation sweeps/i });
    const row = within(history).getByRole("button", { name: "Show run run-2" }).closest("tr");
    expect(row?.textContent).toContain("completed");
    expect(row?.textContent).toContain("40/40 tasks");
  });

  it("focuses no run when every listed run is terminal, and polls nothing", async () => {
    const client = new FakeEvalRunClient([], {
      history: [[jobPayload({ status: "completed", run_id: "run-1" })]],
    });
    const statusSpy = vi.spyOn(client, "getRunStatus");
    renderPage(client);

    await screen.findByRole("button", { name: "Show run run-1" });
    await settle();
    expect(screen.getByText(/no run is in progress/i)).toBeInTheDocument();
    expect(statusSpy).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("EvalDashboardPage — polling stops at terminal", () => {
  it("makes no status call after the backend reports is_terminal", async () => {
    const client = new FakeEvalRunClient([
      jobPayload({ status: "running", tasks_done: 20 }),
      { ...jobPayload({ status: "completed" }), matrix: { arms: [buildArm()] }, artifacts: { raw: [] } },
    ]);
    const statusSpy = vi.spyOn(client, "getRunStatus");
    renderPage(client);

    await userEvent.click(screen.getByRole("button", { name: /run stub sweep/i }));
    await waitFor(() => expect(screen.getByText(/run complete/i)).toBeInTheDocument());
    await settle();
    // An extra poll would exhaust the queue and raise an alert.
    expect(statusSpy).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("the production poll interval is 3 s", async () => {
    const { POLL_INTERVAL_MS } = await import("./useEvalRun");
    expect(POLL_INTERVAL_MS).toBe(3000);
  });
});

describe("EvalDashboardPage — run history", () => {
  const newest = jobPayload({ status: "completed", run_id: "run-3", started_by_rep_id: "rep-7", submitted_at: "2026-01-03T00:00:00+00:00" });
  const middle = jobPayload({ status: "failed", run_id: "run-2", error: "tenant teardown failed", submitted_at: "2026-01-02T00:00:00+00:00" });
  const oldest = jobPayload({ status: "completed", run_id: "run-1", submitted_at: "2026-01-01T00:00:00+00:00" });

  it("lists runs in server order with the starter, status and N/40 tasks", async () => {
    renderPage(new FakeEvalRunClient([], { history: [[newest, middle]] }));

    const history = await screen.findByRole("table", { name: /evaluation sweeps, newest first/i });
    const rowHeaders = within(history).getAllByRole("rowheader").map((cell) => cell.textContent);
    expect(rowHeaders).toEqual(["2026-01-03T00:00:00+00:00", "2026-01-02T00:00:00+00:00"]);
    const first = within(history).getByRole("button", { name: "Show run run-3" }).closest("tr");
    expect(first?.textContent).toContain("rep-7");
    expect(first?.textContent).toContain("40/40 tasks");
    expect(first?.textContent).toContain("no inference cost (stub)");
    // A failed run in the table is not a page alert.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("loads the next page by cursor and appends it", async () => {
    const client = new FakeEvalRunClient([], { history: [[newest, middle], [oldest]] });
    renderPage(client);

    await userEvent.click(await screen.findByRole("button", { name: "Load more runs" }));
    await screen.findByRole("button", { name: "Show run run-1" });
    expect(client.listCursors).toEqual([undefined, "page-1"]);
    const history = screen.getByRole("table", { name: /evaluation sweeps/i });
    expect(within(history).getAllByRole("rowheader")).toHaveLength(3);
    // The last page reported no further cursor.
    expect(screen.queryByRole("button", { name: "Load more runs" })).toBeNull();
  });

  it("opens a history run and renders its persisted results", async () => {
    const persisted = { ...newest, matrix: { arms: [buildArm()] }, artifacts: { raw: [] } };
    renderPage(new FakeEvalRunClient([], { history: [[persisted]] }));

    await userEvent.click(await screen.findByRole("button", { name: "Show run run-3" }));
    expect(screen.getByText(/run complete/i)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "anthropic.claude-opus-5-5" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show run run-3" })).toHaveAttribute("aria-pressed", "true");
  });

  it("opens a temperature-era live run on retired models and renders it as recorded", async () => {
    // A legacy record (temperature key present, models no longer offered) renders field by field.
    const legacyArms = ["anthropic.claude-opus-5", "openai.gpt-5.6-sol"].map((modelId) => {
      const arm = buildArm({ mode: "live", model_id: modelId, temperature: null });
      return { ...arm, model_metrics: { ...arm.model_metrics, model_id: modelId } };
    });
    const pinned = buildArm({ mode: "live", model_id: "anthropic.claude-opus-4-8", temperature: 0.7 });
    const persisted = { ...newest, matrix: { arms: [...legacyArms, pinned] }, artifacts: { raw: [] } };
    renderPage(new FakeEvalRunClient([], { history: [[persisted]] }));

    await userEvent.click(await screen.findByRole("button", { name: "Show run run-3" }));
    for (const modelId of ["anthropic.claude-opus-5", "openai.gpt-5.6-sol"]) {
      const section = screen.getByRole("region", { name: modelId });
      const term = within(section).getByText("Temperature");
      expect(term.nextElementSibling?.firstElementChild?.textContent).toBe("not set");
    }
    const pinnedSection = screen.getByRole("region", { name: "anthropic.claude-opus-4-8" });
    const pinnedTerm = within(pinnedSection).getByText("Temperature");
    expect(pinnedTerm.nextElementSibling?.firstElementChild?.textContent).toBe("0.7");
  });

  it("says so when the history cannot be loaded, without a page alert", async () => {
    const client = new FakeEvalRunClient([]);
    vi.spyOn(client, "listRuns").mockRejectedValue(new Error("list refused"));
    renderPage(client);

    expect(await screen.findByText(/history unavailable: list refused/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("Evals page — cost-completeness disclosure", () => {
  const liveRun = (tasksFailed: number) =>
    jobPayload({
      status: "failed",
      mode: "live",
      run_id: "run-cost",
      tasks_failed: tasksFailed,
      submitted_at: "2026-01-05T00:00:00+00:00",
    });

  it("qualifies a history row's spend with the items that ended in an error", async () => {
    renderPage(new FakeEvalRunClient([], { history: [[liveRun(2)]] }));

    const row = (await screen.findByRole("button", { name: "Show run run-cost" })).closest("tr");
    const note = erroredItemsCostNote(2);
    expect(note).toBeDefined();
    expect(row?.textContent).toContain(note);
    expect(row?.textContent).toContain("2 item(s) that ended in an error");
  });

  it("adds no qualifier to a history row when no item ended in an error", async () => {
    renderPage(new FakeEvalRunClient([], { history: [[liveRun(0)]] }));

    const row = (await screen.findByRole("button", { name: "Show run run-cost" })).closest("tr");
    expect(erroredItemsCostNote(0)).toBeUndefined();
    expect(row?.textContent).not.toContain("Cost excludes");
  });

  it("renders the matrix.md lower-bound note on an arm that names lower-bound task runs", () => {
    const arm: EvalArm = { ...buildArm(), usage_lower_bound_task_ids: ["gt-04", "gt-09"] };
    render(<EvalArmResults arm={arm} />);

    const section = screen.getByRole("region", { name: "anthropic.claude-opus-5-5" });
    const note = usageLowerBoundNote(arm);
    expect(note).toContain("are a lower bound: in 2 task run(s) (gt-04, gt-09)");
    expect(within(section).getByText("Usage completeness")).toBeInTheDocument();
    expect(section.textContent).toContain(note);
  });

  it("renders no lower-bound note for a complete or legacy arm", () => {
    render(<EvalArmResults arm={buildArm()} />);

    const section = screen.getByRole("region", { name: "anthropic.claude-opus-5-5" });
    expect(within(section).queryByText("Usage completeness")).toBeNull();
    expect(section.textContent).not.toContain("lower bound");
  });

  it("states on the arm that its cost excludes items that ended in an error", () => {
    render(<EvalArmResults arm={buildArm()} tasksFailed={3} />);

    const section = screen.getByRole("region", { name: "anthropic.claude-opus-5-5" });
    expect(within(section).getByText("Items not measured")).toBeInTheDocument();
    expect(section.textContent).toContain(erroredItemsCostNote(3));
  });

  it("qualifies the open run's spend, cancelled notice and arms with its errored items", async () => {
    const cancelled = {
      ...jobPayload({
        status: "cancelled",
        mode: "live",
        run_id: "run-cost",
        tasks_failed: 1,
        submitted_at: "2026-01-05T00:00:00+00:00",
      }),
      matrix: { arms: [buildArm()] },
      artifacts: { raw: [] },
    };
    renderPage(new FakeEvalRunClient([], { history: [[cancelled]] }));

    await userEvent.click(await screen.findByRole("button", { name: "Show run run-cost" }));
    const note = erroredItemsCostNote(1) ?? "";
    const spend = screen.getByText("Spend", { selector: "dt" }).nextElementSibling;
    expect(spend?.textContent).toContain(note);
    expect(screen.getByText(/^Cancelled after/).textContent).toContain(note);
    const section = screen.getByRole("region", { name: "anthropic.claude-opus-5-5" });
    expect(within(section).getByText("Items not measured")).toBeInTheDocument();
  });

  it("leaves the open run's spend unqualified when no item ended in an error", async () => {
    const cancelled = {
      ...jobPayload({ status: "cancelled", mode: "live", run_id: "run-cost", tasks_failed: 0 }),
      matrix: { arms: [buildArm()] },
      artifacts: { raw: [] },
    };
    renderPage(new FakeEvalRunClient([], { history: [[cancelled]] }));

    await userEvent.click(await screen.findByRole("button", { name: "Show run run-cost" }));
    const spend = screen.getByText("Spend", { selector: "dt" }).nextElementSibling;
    expect(spend?.textContent).not.toContain("Cost excludes");
    expect(screen.queryByText("Items not measured")).toBeNull();
  });

  it("adds no exclusion note on the arm when no item ended in an error", () => {
    render(<EvalArmResults arm={buildArm()} tasksFailed={0} />);

    const section = screen.getByRole("region", { name: "anthropic.claude-opus-5-5" });
    expect(within(section).queryByText("Items not measured")).toBeNull();
  });
});

describe("EvalDashboardPage — live sweep, admins only", () => {
  it("shows an admin the live control, and the estimate before anything is submitted", async () => {
    let releaseEstimate: (() => void) | undefined;
    const client = new FakeEvalRunClient([]);
    const realEstimate = client.estimateLiveRun.bind(client);
    vi.spyOn(client, "estimateLiveRun").mockImplementation(
      () => new Promise((resolve) => { releaseEstimate = () => void realEstimate().then(resolve); }),
    );
    renderPage(client, adminAuth);

    await userEvent.click(await screen.findByRole("button", { name: /run live sweep/i }));
    const dialog = await screen.findByRole("dialog", { name: /start a live sweep/i });
    const confirm = within(dialog).getByRole("button", { name: "Start live sweep" });
    // Disabled until the estimate is on screen.
    expect(confirm).toBeDisabled();
    expect(client.liveStarts).toEqual([]);

    releaseEstimate?.();
    expect(await within(dialog).findByText("$3.10")).toBeInTheDocument();
    expect(within(dialog).getByText(/static per-task token figures/i)).toBeInTheDocument();
    await waitFor(() => expect(confirm).toBeEnabled());
    await userEvent.click(confirm);

    await waitFor(() => expect(client.liveStarts).toEqual([{ confirmed: true }]));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(client.startCallCount).toBe(0);
    expect(screen.getByText(/queued live sweep \(run_id=run-live\)/i)).toBeInTheDocument();
  });

  it("keeps the live sweep unstartable when the estimate fails", async () => {
    const client = new FakeEvalRunClient([], { estimate: new Error("estimate refused") });
    renderPage(client, adminAuth);

    await userEvent.click(await screen.findByRole("button", { name: /run live sweep/i }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/estimate refused/i);
    expect(within(dialog).getByRole("button", { name: "Start live sweep" })).toBeDisabled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(client.liveStarts).toEqual([]);
  });

  it("hides the live control for a token whose groups do not include admin", async () => {
    const reps: AuthContextValue = { ...fakeAuth, getAccessToken: vi.fn().mockResolvedValue(idTokenWithGroups(["reps"])) };
    const client = new FakeEvalRunClient([]);
    renderPage(client, reps);

    await waitFor(() => expect(reps.getAccessToken).toHaveBeenCalled());
    await settle();
    expect(screen.queryByRole("button", { name: /live/i })).toBeNull();
  });
});

describe("EvalDashboardPage — cancel", () => {
  const liveRunning = jobPayload({ status: "running", mode: "live", run_id: "run-live", tasks_done: 5, cost_spent_usd: 0.21 });

  it("lets an admin cancel a running live sweep and reports what the request did", async () => {
    const cancelling = { ...liveRunning, status: "cancelling" as const, cancel_requested: true, cancel_requested_by: "admin-1" };
    let current: ReturnType<typeof jobPayload> = liveRunning;
    const client = new FakeEvalRunClient([], { history: [[liveRunning]] });
    // The backend's view: status reports reflect the cancel once it is accepted.
    vi.spyOn(client, "getRunStatus").mockImplementation(() => Promise.resolve(current));
    const cancelSpy = vi.spyOn(client, "cancelRun").mockImplementation((runId) => {
      current = cancelling;
      return Promise.resolve({ disposition: "accepted", run: { ...cancelling, run_id: runId } });
    });
    renderPage(client, adminAuth);

    await userEvent.click(await screen.findByRole("button", { name: "Cancel sweep" }));
    await waitFor(() => expect(cancelSpy).toHaveBeenCalledWith("run-live"));
    expect(await screen.findByText(/cancel requested\. the task in flight finishes/i)).toBeInTheDocument();
    await settle();
    expect(screen.queryByRole("button", { name: "Cancel sweep" })).toBeNull();
    expect(screen.getByText(/cancel requested; the remaining tasks will be skipped/i)).toBeInTheDocument();
  });

  it.each<[string, AuthContextValue, ReturnType<typeof jobPayload>]>([
    ["a non-admin on a live run", fakeAuth, liveRunning],
    ["an admin on a stub run", adminAuth, jobPayload({ status: "running", run_id: "run-stub" })],
    ["an admin on a run already cancelling", adminAuth, { ...liveRunning, status: "cancelling", cancel_requested: true }],
    ["an admin on a running run whose cancel is already requested", adminAuth, { ...liveRunning, cancel_requested: true }],
  ])("offers no cancel to %s", async (_label, auth, run) => {
    const client = new FakeEvalRunClient([run, run, run, run, run, run, run, run, run, run], { history: [[run]] });
    renderPage(client, auth);

    await screen.findByText(new RegExp(`run_id=${run.run_id}`));
    await settle();
    expect(screen.queryByRole("button", { name: "Cancel sweep" })).toBeNull();
  });

  it("labels a cancelled live run with N/40 tasks and its spend", async () => {
    const cancelled = jobPayload({
      status: "cancelled",
      mode: "live",
      run_id: "run-live",
      tasks_done: 12,
      tasks_failed: 1,
      tasks_skipped: 27,
      cost_spent_usd: 0.42,
      cancel_requested: true,
    });
    renderPage(new FakeEvalRunClient([], { history: [[cancelled]] }));

    await userEvent.click(await screen.findByRole("button", { name: "Show run run-live" }));
    expect(
      screen.getByText(
        `Cancelled after 13/40 tasks (27 skipped). Spend: $0.42. ${erroredItemsCostNote(1) ?? ""}`,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/no task finished before the cancel/i)).toBeInTheDocument();
  });

  it("shows a failed run's failure summary as the page alert", async () => {
    const failed = jobPayload({ status: "failed", run_id: "run-9", error: "2 tasks failed; teardown ok" });
    renderPage(new FakeEvalRunClient([], { history: [[failed]] }));

    await userEvent.click(await screen.findByRole("button", { name: "Show run run-9" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Run failed: 2 tasks failed; teardown ok");
  });
});

describe("EvalDashboardPage — the HTTP client's request bodies", () => {
  async function bodyOf(call: (client: HttpEvalRunClient) => Promise<unknown>, response: unknown): Promise<Record<string, unknown>> {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(response) });
    vi.stubGlobal("fetch", fetchMock);
    try {
      await call(new HttpEvalRunClient("/api", { getAccessToken: () => Promise.resolve("tok") }));
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("/api/eval_trigger");
      expect(init.method).toBe("POST");
      return JSON.parse(init.body as string) as Record<string, unknown>;
    } finally {
      vi.unstubAllGlobals();
    }
  }

  it("submits a live sweep only with explicit confirmation", async () => {
    const body = await bodyOf((c) => c.startLiveRun({ confirmed: true }), jobPayload({ status: "queued", mode: "live" }));
    expect(body).toEqual({ action: "submit", mode: "live", confirmed: true });
  });

  it("lists by cursor, estimates and cancels with exactly the handler's keys", async () => {
    expect(await bodyOf((c) => c.listRuns(), { runs: [], next_cursor: null })).toEqual({ action: "list" });
    expect(await bodyOf((c) => c.listRuns("abc"), { runs: [], next_cursor: null })).toEqual({ action: "list", cursor: "abc" });
    expect(await bodyOf((c) => c.estimateLiveRun(), { estimate_usd: 3.1, basis: "static", task_count: 20, per_arm_usd: {} })).toEqual({ action: "estimate" });
    expect(
      await bodyOf((c) => c.cancelRun("run-7"), { disposition: "accepted", run: jobPayload({ status: "cancelling", run_id: "run-7" }) }),
    ).toEqual({ action: "cancel", run_id: "run-7" });
  });

  it.each<[string, (client: HttpEvalRunClient) => Promise<unknown>, unknown]>([
    ["a list page with no runs array", (c) => c.listRuns(), { next_cursor: null }],
    ["a list page with an empty cursor", (c) => c.listRuns(), { runs: [], next_cursor: "" }],
    ["a list run missing its counters", (c) => c.listRuns(), { runs: [{ ...jobPayload(), tasks_done: undefined }], next_cursor: null }],
    ["an estimate with no figure", (c) => c.estimateLiveRun(), { basis: "static", task_count: 20, per_arm_usd: {} }],
    ["a cancel with an unknown disposition", (c) => c.cancelRun("run-1"), { disposition: "maybe", run: jobPayload() }],
  ])("refuses %s rather than guessing", async (_label, call, response) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(response) }));
    try {
      await expect(
        call(new HttpEvalRunClient("/api", { getAccessToken: () => Promise.resolve("tok") })),
      ).rejects.toMatchObject({ body: { code: "eval_response_unreadable" } });
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("EvalDashboardPage — per-tier summary", () => {
  function armWithSummary(costWithheld: boolean): EvalArm {
    const arm = buildArm();
    return {
      ...arm,
      model_metrics: { ...arm.model_metrics, cost_withheld_provisional: costWithheld },
      tier_summary: {
        supervisor: {
          input_tokens: 9880,
          output_tokens: 416,
          cost_total: costWithheld ? null : 0.25,
          latency_p50_ms: 810,
          latency_p95_ms: 950,
          success: { passed: 13, applicable: 20, rate: 0.65 },
        },
        subagent: {
          input_tokens: 0,
          output_tokens: 0,
          cost_total: costWithheld ? null : 0,
          latency_p50_ms: null,
          latency_p95_ms: null,
          success: { passed: 0, applicable: 0, rate: null },
        },
        tool: {
          input_tokens: 2351,
          output_tokens: 314,
          cost_total: costWithheld ? null : 0.01,
          latency_p50_ms: 120,
          latency_p95_ms: 400,
          success: { passed: 9, applicable: 12, rate: 0.75 },
        },
      },
    };
  }

  async function summaryRows(arm: EvalArm): Promise<string[][]> {
    renderPage(new FakeEvalRunClient([], { history: [[{ ...jobPayload({ status: "completed", run_id: "run-5" }), matrix: { arms: [arm] }, artifacts: { raw: [] } }]] }));
    await userEvent.click(await screen.findByRole("button", { name: "Show run run-5" }));
    const table = screen.getByRole("table", { name: /per-tier summary for anthropic\.claude-opus-5-5/i });
    return within(table)
      .getAllByRole("row")
      .slice(1)
      .map((row) => [...row.querySelectorAll("th, td")].map((cell) => cell.textContent ?? ""));
  }

  it("renders every tier with tokens, latency and success, and no cost while withheld", async () => {
    expect(await summaryRows(armWithSummary(true))).toEqual([
      ["Supervisor", "9880", "416", "—", "810 ms", "950 ms", "13 of 20", "0.6500"],
      ["Subagent", "0", "0", "—", "—", "—", "0 of 0", "not applicable"],
      ["Tool", "2351", "314", "—", "120 ms", "400 ms", "9 of 12", "0.7500"],
    ]);
  });

  it("renders the backend's tier cost when the arm marks cost not withheld", async () => {
    const rows = await summaryRows(armWithSummary(false));
    expect(rows.map((row) => row[3])).toEqual(["$0.25", "$0.00", "$0.01"]);
  });
});

describe("EvalDashboardPage — polling, errors and estimate rendering", () => {
  const liveRunning = jobPayload({ status: "running", mode: "live", run_id: "run-live", tasks_done: 5 });
  const liveCancelling = { ...liveRunning, status: "cancelling" as const, cancel_requested: true };
  const liveCancelled = jobPayload({
    status: "cancelled",
    mode: "live",
    run_id: "run-live",
    tasks_done: 5,
    tasks_skipped: 35,
    cancel_requested: true,
  });

  it("resumes polling after a failed poll once a cancel returns a non-terminal run", async () => {
    const client = new FakeEvalRunClient([], {
      history: [[liveRunning]],
      cancel: { disposition: "accepted", run: liveCancelling },
    });
    const statusSpy = vi
      .spyOn(client, "getRunStatus")
      .mockRejectedValueOnce(new Error("status unavailable"))
      .mockResolvedValue(liveCancelled);
    renderPage(client, adminAuth);

    expect(await screen.findByText(/status unavailable/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel sweep" }));
    await waitFor(() => expect(statusSpy).toHaveBeenCalledTimes(2), { timeout: 2000 });
    expect(await screen.findByText(/^Cancelled after 5\/40 tasks/)).toBeInTheDocument();
  });

  it("keeps a refused cancel's message after a later poll succeeds", async () => {
    const client = new FakeEvalRunClient([], { history: [[liveRunning]] });
    vi.spyOn(client, "getRunStatus").mockResolvedValue(liveRunning);
    vi.spyOn(client, "cancelRun").mockRejectedValue(new Error("eval:admin required"));
    renderPage(client, adminAuth);

    await userEvent.click(await screen.findByRole("button", { name: "Cancel sweep" }));
    expect(await screen.findByText(/eval:admin required/i)).toBeInTheDocument();
    await settle();
    expect(screen.getByText(/eval:admin required/i)).toBeInTheDocument();
  });

  it("reports a status reply for a different run instead of silently stopping", async () => {
    const client = new FakeEvalRunClient([], { history: [[liveRunning]] });
    vi.spyOn(client, "getRunStatus").mockResolvedValue({ ...liveRunning, run_id: "run-other" });
    renderPage(client);

    expect(await screen.findByRole("alert")).toHaveTextContent(/run-other/);
  });

  it("says that status updates stopped, and how to resume them, when a poll fails", async () => {
    const client = new FakeEvalRunClient([], { history: [[liveRunning]] });
    vi.spyOn(client, "getRunStatus").mockRejectedValue(new Error("status unavailable"));
    renderPage(client);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/status updates stopped/i);
    expect(alert).toHaveTextContent(/open the run from the run history to resume/i);
  });

  it("keeps the live dialog open with the reason when the start fails", async () => {
    const client = new FakeEvalRunClient([]);
    vi.spyOn(client, "startLiveRun").mockRejectedValue(new Error("a sweep is already running"));
    renderPage(client, adminAuth);

    await userEvent.click(await screen.findByRole("button", { name: /run live sweep/i }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Start live sweep" });
    await waitFor(() => expect(confirm).toBeEnabled());
    await userEvent.click(confirm);

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/a sweep is already running/i);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("offers a retry when the first history page fails, and loads it", async () => {
    const client = new FakeEvalRunClient([], { history: [[jobPayload({ run_id: "run-1" })]] });
    const realList = client.listRuns.bind(client);
    vi.spyOn(client, "listRuns")
      .mockRejectedValueOnce(new Error("list refused"))
      .mockImplementation((cursor) => realList(cursor));
    renderPage(client);

    await userEvent.click(await screen.findByRole("button", { name: "Retry loading runs" }));
    expect(await screen.findByRole("button", { name: "Show run run-1" })).toBeInTheDocument();
    expect(screen.queryByText(/history unavailable/i)).toBeNull();
  });

  it("derives the sweep size from the estimate rather than stating a fixed count", async () => {
    const client = new FakeEvalRunClient([], {
      estimate: estimatePayload({ task_count: 3, per_arm_usd: { "anthropic.claude-opus-5-5": 0.5 } }),
    });
    renderPage(client, adminAuth);

    await userEvent.click(await screen.findByRole("button", { name: /run live sweep/i }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/3 tasks on each of 1 arm \(3 agent runs\)/i)).toBeInTheDocument();
    expect(dialog.textContent).not.toMatch(/\b40\b/);
    expect(screen.getByRole("main").textContent).not.toMatch(/40 agent runs/);
  });

  it("shows the estimate's effort caveat under the basis line when the server sends one", async () => {
    const caveat = "These figures were measured at supervisor effort low; sweeps now run at supervisor effort medium, so the cost of this sweep may differ from the estimate.";
    const client = new FakeEvalRunClient([], { estimate: estimatePayload({ effort_caveat: caveat }) });
    renderPage(client, adminAuth);

    await userEvent.click(await screen.findByRole("button", { name: /run live sweep/i }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(caveat)).toBeInTheDocument();
  });

  it("shows no effort caveat when the estimate carries none", async () => {
    const client = new FakeEvalRunClient([], { estimate: estimatePayload() });
    renderPage(client, adminAuth);

    await userEvent.click(await screen.findByRole("button", { name: /run live sweep/i }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("$3.10")).toBeInTheDocument();
    expect(dialog.textContent).not.toMatch(/measured at supervisor effort/i);
  });

  it("names an absent tenant as not reported rather than rendering blank", async () => {
    const run = { ...jobPayload({ run_id: "run-1" }), tenant: undefined } as unknown as ReturnType<typeof jobPayload>;
    renderPage(new FakeEvalRunClient([], { history: [[run]] }));

    await userEvent.click(await screen.findByRole("button", { name: "Show run run-1" }));
    expect(screen.getByText("Eval tenant").nextElementSibling?.textContent).toBe("not reported");
  });
});

describe("HttpEvalRunClient — payload checks", () => {
  async function rejectionOf(call: (client: HttpEvalRunClient) => Promise<unknown>, response: Partial<Response>): Promise<unknown> {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, ...response }));
    try {
      return await call(new HttpEvalRunClient("/api", { getAccessToken: () => Promise.resolve("tok") })).then(
        () => "resolved",
        (err: unknown) => err,
      );
    } finally {
      vi.unstubAllGlobals();
    }
  }

  const estimateBase = { estimate_usd: 3.1, basis: "static", task_count: 20 };

  it.each<[string, unknown]>([
    ["absent", undefined],
    ["null", null],
    ["a string", "1.75"],
    ["an array", [1.75]],
  ])("refuses an estimate whose per_arm_usd is %s", async (_label, perArm) => {
    const err = await rejectionOf((c) => c.estimateLiveRun(), {
      json: () => Promise.resolve({ ...estimateBase, per_arm_usd: perArm }),
    });
    expect(err).toMatchObject({ body: { code: "eval_response_unreadable" } });
  });

  it.each<[string, unknown]>([
    ["null", null],
    ["a number", 3],
    ["an object", { text: "low" }],
  ])("refuses an estimate whose effort_caveat is %s", async (_label, caveat) => {
    const err = await rejectionOf((c) => c.estimateLiveRun(), {
      json: () => Promise.resolve({ ...estimateBase, per_arm_usd: {}, effort_caveat: caveat }),
    });
    expect(err).toMatchObject({ body: { code: "eval_response_unreadable" } });
  });

  it("accepts an estimate whose effort_caveat is a string, and one without it", async () => {
    for (const extra of [{ effort_caveat: "measured at low" }, {}]) {
      const result = await rejectionOf((c) => c.estimateLiveRun(), {
        json: () => Promise.resolve({ ...estimateBase, per_arm_usd: {}, ...extra }),
      });
      expect(result).toBe("resolved");
    }
  });

  it("refuses a 2xx reply that is not JSON as unreadable", async () => {
    const err = await rejectionOf((c) => c.getRunStatus("run-1"), {
      json: () => Promise.reject(new SyntaxError("Unexpected token <")),
    });
    expect(err).toMatchObject({ body: { code: "eval_response_unreadable" } });
  });

  it.each<[string, unknown]>([
    ["no arms", {}],
    ["arms that are not a list", { arms: "x" }],
    ["an arm without provenance", { arms: [{ model_metrics: {}, aggregate_score: {}, task_scores: [] }] }],
    ["an arm without task scores", { arms: [{ provenance: { model_id: "m" }, model_metrics: {}, aggregate_score: {} }] }],
  ])("refuses a run whose matrix has %s", async (_label, matrix) => {
    const err = await rejectionOf((c) => c.getRunStatus("run-1"), {
      json: () => Promise.resolve({ ...jobPayload(), matrix }),
    });
    expect(err).toMatchObject({ body: { code: "eval_response_unreadable" } });
  });

  it("accepts a well-formed matrix, so the refusals above are not refusing everything", async () => {
    const result = await rejectionOf((c) => c.getRunStatus("run-1"), {
      json: () => Promise.resolve({ ...jobPayload(), matrix: { arms: [buildArm()] } }),
    });
    expect(result).toBe("resolved");
  });
});

describe("Repeats — the sweep size selector", () => {
  function renderDialog(client: BaseFakeEvalRunClient, onConfirm = vi.fn().mockResolvedValue(undefined)) {
    render(<LiveSweepDialog client={client} onConfirm={onConfirm} onClose={vi.fn()} />);
    return { dialog: screen.getByRole("dialog", { name: /start a live sweep/i }), onConfirm };
  }

  it("bounds the selector to the server's MAX_SWEEP_REPEATS", () => {
    const runsPy = readFileSync(join(__dirname, "../../../../src/eval/runs.py"), "utf-8");
    const declared = /^MAX_SWEEP_REPEATS: Final\[int\] = (\d+)$/m.exec(runsPy);
    expect(declared, "src/eval/runs.py no longer declares MAX_SWEEP_REPEATS").not.toBeNull();
    expect(MAX_SWEEP_REPEATS).toBe(Number(declared?.[1]));
  });

  it("offers a labelled select from 1 to the bound, defaulting to 1", async () => {
    const { dialog } = renderDialog(new FakeEvalRunClient([]));
    const select = within(dialog).getByRole("combobox", { name: "Repeats" });

    expect(select).toHaveValue("1");
    expect(within(select).getAllByRole("option").map((option) => option.textContent)).toEqual(
      Array.from({ length: MAX_SWEEP_REPEATS }, (_, index) => String(index + 1)),
    );
    expect(select).toHaveAccessibleDescription(/every task on every arm again/i);
    await within(dialog).findByText("$3.10");
  });

  it("is keyboard-operable: focus, arrow to 3, and the live start carries 3", async () => {
    const client = new FakeEvalRunClient([]);
    const { dialog, onConfirm } = renderDialog(client);
    const select = within(dialog).getByRole("combobox", { name: "Repeats" });

    select.focus();
    expect(select).toHaveFocus();
    await userEvent.selectOptions(select, "3");
    expect(select).toHaveValue("3");
    const confirm = within(dialog).getByRole("button", { name: "Start live sweep" });
    await waitFor(() => expect(confirm).toBeEnabled());
    await userEvent.click(confirm);

    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith(3));
  });

  it("starts at 1 repeat when the selector is left alone", async () => {
    const { dialog, onConfirm } = renderDialog(new FakeEvalRunClient([]));
    const confirm = within(dialog).getByRole("button", { name: "Start live sweep" });
    await waitFor(() => expect(confirm).toBeEnabled());
    await userEvent.click(confirm);

    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith(1));
  });

  it("shows the server's estimate for the selected repeats, and no start until it has loaded", async () => {
    const client = new FakeEvalRunClient([]);
    const releases = new Map<number, () => void>();
    vi.spyOn(client, "estimateLiveRun").mockImplementation(
      (repeats = 1) =>
        new Promise((resolve) => {
          releases.set(repeats, () =>
            resolve(estimatePayload({ estimate_usd: 3.1 * repeats, repeats, task_count: 20 })),
          );
        }),
    );
    const { dialog } = renderDialog(client);
    await waitFor(() => expect(releases.has(1)).toBe(true));
    releases.get(1)?.();
    expect(await within(dialog).findByText("$3.10")).toBeInTheDocument();

    await userEvent.selectOptions(within(dialog).getByRole("combobox", { name: "Repeats" }), "2");
    const confirm = within(dialog).getByRole("button", { name: "Start live sweep" });
    expect(confirm).toBeDisabled();
    expect(within(dialog).queryByText("$3.10")).toBeNull();

    await waitFor(() => expect(releases.has(2)).toBe(true));
    releases.get(2)?.();
    expect(await within(dialog).findByText("$6.20")).toBeInTheDocument();
    expect(within(dialog).getByText(/20 tasks on each of 2 arms, 2 times over \(80 agent runs\)/i)).toBeInTheDocument();
    await waitFor(() => expect(confirm).toBeEnabled());
  });

  it("ignores an estimate that arrives after the selection moved on", async () => {
    const client = new FakeEvalRunClient([]);
    const releases = new Map<number, () => void>();
    vi.spyOn(client, "estimateLiveRun").mockImplementation(
      (repeats = 1) =>
        new Promise((resolve) => {
          releases.set(repeats, () => resolve(estimatePayload({ estimate_usd: 1.11 * repeats, repeats })));
        }),
    );
    const { dialog } = renderDialog(client);
    await userEvent.selectOptions(within(dialog).getByRole("combobox", { name: "Repeats" }), "4");
    await waitFor(() => expect(releases.has(4)).toBe(true));
    releases.get(4)?.();
    expect(await within(dialog).findByText("$4.44")).toBeInTheDocument();

    releases.get(1)?.();
    await settle();
    expect(within(dialog).queryByText("$1.11")).toBeNull();
    expect(within(dialog).getByText("$4.44")).toBeInTheDocument();
  });

  it("starts a stub sweep at the page's selected repeats, and at 1 by default", async () => {
    const client = new FakeEvalRunClient([jobPayload(), jobPayload()]);
    renderPage(client);
    const select = await screen.findByRole("combobox", { name: "Repeats" });
    expect(select).toHaveValue("1");
    expect(select).toHaveAccessibleDescription(/stub sweep runs every task on every arm/i);
    const stub = screen.getByRole("button", { name: /run stub sweep/i });

    await userEvent.click(stub);
    await waitFor(() => expect(client.stubStartRepeats).toEqual([1]));
    await waitFor(() => expect(stub).toBeEnabled());
    await userEvent.selectOptions(select, "3");
    await userEvent.click(stub);
    await waitFor(() => expect(client.stubStartRepeats).toEqual([1, 3]));
  });

  it("passes the dialog's selected repeats through the page to the live start", async () => {
    const client = new FakeEvalRunClient([]);
    renderPage(client, adminAuth);

    await userEvent.click(await screen.findByRole("button", { name: /run live sweep/i }));
    const dialog = await screen.findByRole("dialog", { name: /start a live sweep/i });
    await userEvent.selectOptions(within(dialog).getByRole("combobox", { name: "Repeats" }), "2");
    const confirm = within(dialog).getByRole("button", { name: "Start live sweep" });
    await waitFor(() => expect(confirm).toBeEnabled());
    await userEvent.click(confirm);

    await waitFor(() => expect(client.liveStartRepeats).toEqual([2]));
    expect(client.liveStarts).toEqual([{ confirmed: true }]);
    expect(client.stubStartRepeats).toEqual([]);
  });

  it.each<[string, (client: HttpEvalRunClient) => Promise<unknown>, unknown, Record<string, unknown>]>([
    ["a stub submit at 1", (c) => c.startRun(1), jobPayload({ status: "queued" }), { action: "submit" }],
    ["a stub submit at 3", (c) => c.startRun(3), jobPayload({ status: "queued" }), { action: "submit", repeats: 3 }],
    [
      "a live submit at 1",
      (c) => c.startLiveRun({ confirmed: true }, 1),
      jobPayload({ status: "queued", mode: "live" }),
      { action: "submit", mode: "live", confirmed: true },
    ],
    [
      "a live submit at 3",
      (c) => c.startLiveRun({ confirmed: true }, 3),
      jobPayload({ status: "queued", mode: "live" }),
      { action: "submit", mode: "live", confirmed: true, repeats: 3 },
    ],
    ["an estimate at 1", (c) => c.estimateLiveRun(1), estimatePayload(), { action: "estimate" }],
    ["an estimate at 3", (c) => c.estimateLiveRun(3), estimatePayload({ repeats: 3 }), { action: "estimate", repeats: 3 }],
  ])("sends %s with repeats only when it is not 1", async (_label, call, response, expected) => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(response) });
    vi.stubGlobal("fetch", fetchMock);
    try {
      await call(new HttpEvalRunClient("/api", { getAccessToken: () => Promise.resolve("tok") }));
      const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(JSON.parse(init.body as string)).toEqual(expected);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("reads a run's repeats from its payload, and a payload without one as 1", () => {
    expect(runRepeats({ ...jobPayload(), repeats: 2 })).toBe(2);
    const legacy = jobPayload();
    expect("repeats" in legacy).toBe(false);
    expect(runRepeats(legacy)).toBe(1);
  });

  it.each<[string, unknown]>([
    ["zero", 0],
    ["a fraction", 1.5],
    ["a string", "2"],
  ])("refuses a run whose repeats is %s rather than guessing", async (_label, repeats) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ...jobPayload(), repeats }) }),
    );
    try {
      await expect(
        new HttpEvalRunClient("/api", { getAccessToken: () => Promise.resolve("tok") }).getRunStatus("run-1"),
      ).rejects.toMatchObject({ body: { code: "eval_response_unreadable" } });
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
