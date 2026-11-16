// SPDX-License-Identifier: MIT
/**
 * A run recorded in an earlier format: its payload carries `record_format: "earlier"` and no
 * matrix, tier table or artifacts. The page still lists and opens it, shows its run metadata, and
 * states that its results are not shown instead of reporting a missing matrix.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthContextValue } from "../../auth/AuthProvider";
import { EvalDashboardPage } from "./EvalDashboardPage";
import { HttpEvalRunClient, type EvalRunStatusResponse } from "./EvalRunClient";
import { BaseFakeEvalRunClient, jobPayload } from "./testFixtures";
import { isEarlierRecordFormat } from "./types";

const POLL_MS = 5;
const NOTICE_TEXT = "Recorded in an earlier format; its results are not shown.";

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

function earlierRun(status: "completed" | "cancelled", runId: string): EvalRunStatusResponse {
  return {
    ...jobPayload({ status, run_id: runId, mode: "live", cost_spent_usd: 4.52 }),
    record_format: "earlier",
  };
}

async function openRun(run: EvalRunStatusResponse, others: readonly EvalRunStatusResponse[] = []) {
  render(
    <EvalDashboardPage
      apiBaseUrl="/api"
      client={new BaseFakeEvalRunClient([], { history: [[...others, run]] })}
      authOverride={fakeAuth}
      pollIntervalMs={POLL_MS}
    />,
  );
  await userEvent.click(await screen.findByRole("button", { name: `Show run ${run.run_id}` }));
}

describe("the HTTP client's run parsing", () => {
  async function parsed(
    call: (client: HttpEvalRunClient) => Promise<unknown>,
    response: unknown,
  ): Promise<unknown> {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(response) }),
    );
    try {
      return await call(new HttpEvalRunClient("/api", { getAccessToken: () => Promise.resolve("tok") }));
    } finally {
      vi.unstubAllGlobals();
    }
  }

  it("keeps record_format on a status response", async () => {
    const run = (await parsed((c) => c.getRunStatus("run-e"), earlierRun("completed", "run-e"))) as
      EvalRunStatusResponse;
    expect(run.record_format).toBe("earlier");
  });

  it("keeps record_format when an unreadable tier table is dropped", async () => {
    const raw = { ...earlierRun("completed", "run-e"), tier_comparison_table: { headers: 1 } };
    const run = (await parsed((c) => c.getRunStatus("run-e"), raw)) as EvalRunStatusResponse;
    expect(run.tier_comparison_table).toBeUndefined();
    expect(run.record_format).toBe("earlier");
  });

  it("keeps record_format on every listed run", async () => {
    const page = (await parsed((c) => c.listRuns(), {
      runs: [earlierRun("completed", "run-e")],
      next_cursor: null,
    })) as { runs: readonly EvalRunStatusResponse[] };
    expect(page.runs.map((run) => run.record_format)).toEqual(["earlier"]);
  });
});

describe("isEarlierRecordFormat", () => {
  it("is true only for the earlier marker", () => {
    expect(isEarlierRecordFormat({ record_format: "earlier" })).toBe(true);
    expect(isEarlierRecordFormat({})).toBe(false);
  });
});

describe.each(["completed", "cancelled"] as const)("an earlier-format %s run", (status) => {
  const runId = `run-earlier-${status}`;

  it("shows the notice and run metadata, and no results, tier table or artifacts", async () => {
    await openRun(earlierRun(status, runId));

    const notice = screen.getByTestId("eval-record-format-earlier");
    expect(notice.textContent).toBe(NOTICE_TEXT);
    expect(notice.getAttribute("role")).toBe("status");

    expect(screen.queryByRole("heading", { name: "Results" })).toBeNull();
    expect(screen.queryByText(/returned no result matrix/)).toBeNull();
    expect(screen.queryByText(/has no result matrix/)).toBeNull();
    expect(screen.queryByText(/No artifact manifest was reported/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();

    const runIdTerm = screen.getByText("Run id", { selector: "dt" });
    expect(runIdTerm.nextElementSibling?.textContent).toBe(runId);
    expect(screen.getByText("Spend", { selector: "dt" }).nextElementSibling?.textContent).not.toBe(
      "",
    );
    expect(screen.getByText("Finished at", { selector: "dt" }).nextElementSibling?.textContent).toBe(
      "2026-01-01T00:05:00+00:00",
    );
  });

  it("stays listed in the run history", async () => {
    await openRun(earlierRun(status, runId));

    const rows = screen.getAllByRole("row");
    const listed = rows.filter(
      (row) => within(row).queryByRole("button", { name: `Show run ${runId}` }) !== null,
    );
    expect(listed).toHaveLength(1);
  });
});

describe("a current-format run", () => {
  it("does not show the earlier-format notice", async () => {
    await openRun(jobPayload({ status: "completed", run_id: "run-current" }));

    expect(screen.queryByTestId("eval-record-format-earlier")).toBeNull();
    expect(screen.getByRole("alert").textContent).toContain("returned no result matrix");
  });

  it("can be opened after an earlier-format run, which then drops the notice", async () => {
    const current = jobPayload({ status: "completed", run_id: "run-current" });
    await openRun(earlierRun("completed", "run-earlier"), [current]);
    expect(screen.getByTestId("eval-record-format-earlier")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Show run run-current" }));
    expect(screen.queryByTestId("eval-record-format-earlier")).toBeNull();
  });
});
