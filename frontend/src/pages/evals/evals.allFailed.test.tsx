// SPDX-License-Identifier: MIT
/**
 * A sweep in which every item ended in an error: no matrix, and the run history and the focused
 * dashboard disclose the unmeasured cost without showing a currency figure.
 */
import { render, screen, within, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthContextValue } from "../../auth/AuthProvider";
import { EvalDashboardPage } from "./EvalDashboardPage";
import type { EvalRunStatusResponse } from "./EvalRunClient";
import { RunHistory } from "./RunHistory";
import { BaseFakeEvalRunClient, jobPayload } from "./testFixtures";
import { erroredItemsCostNote } from "./types";

const POLL_MS = 5;
const ITEMS = 4;
const CURRENCY = /\$\s?\d/;
const SPEND_LABEL = { live: "not measured", stub: "no inference cost (stub)" } as const;

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

/** The terminal record of a sweep whose every item failed: no matrix, no artifacts, zero spend. */
function allFailedRun(mode: "live" | "stub"): EvalRunStatusResponse {
  return jobPayload({
    status: "failed",
    mode,
    run_id: `run-all-failed-${mode}`,
    tasks_total: ITEMS,
    tasks_done: 0,
    tasks_failed: ITEMS,
    cost_spent_usd: 0,
    error: `${ITEMS} task(s) failed`,
  });
}

function renderHistory(run: EvalRunStatusResponse): HTMLElement {
  render(
    <RunHistory
      runs={[run]}
      loading={false}
      loadingMore={false}
      error={undefined}
      hasMore={false}
      canRetry={false}
      focusedRunId={undefined}
      onOpen={vi.fn()}
      onLoadMore={vi.fn()}
      onRetry={vi.fn()}
    />,
  );
  const [, row] = screen.getAllByRole("row");
  if (row === undefined) throw new Error("the history rendered no run row");
  return row;
}

describe.each(["live", "stub"] as const)("an all-items-failed %s run", (mode) => {
  const note = erroredItemsCostNote(ITEMS);

  it("is disclosed in its history row with no currency figure", () => {
    expect(note).toBeDefined();
    const row = renderHistory(allFailedRun(mode));

    expect(row.textContent).toContain(note);
    expect(row.textContent).toContain(`${ITEMS}/${ITEMS} tasks, ${ITEMS} failed`);
    expect(row.textContent).toContain(SPEND_LABEL[mode]);
    expect(row.textContent).not.toMatch(CURRENCY);
  });

  it("is disclosed on the focused dashboard with no matrix and no currency figure", async () => {
    render(
      <EvalDashboardPage
        apiBaseUrl="/api"
        client={new BaseFakeEvalRunClient([], { history: [[allFailedRun(mode)]] })}
        authOverride={fakeAuth}
        pollIntervalMs={POLL_MS}
      />,
    );

    await userEvent.click(
      await screen.findByRole("button", { name: `Show run run-all-failed-${mode}` }),
    );

    const spend = screen.getByText("Spend", { selector: "dt" }).nextElementSibling;
    expect(spend?.textContent).toContain(note);
    expect(spend?.textContent).toContain(SPEND_LABEL[mode]);
    expect(spend?.textContent).not.toMatch(CURRENCY);
    expect(screen.getByRole("alert").textContent).toContain(`Run failed: ${ITEMS} task(s) failed`);
    expect(screen.queryByRole("heading", { name: "Results" })).toBeNull();
    const main = screen.getByRole("main");
    expect(main.textContent).not.toMatch(CURRENCY);
    expect(within(main).getAllByText(note ?? "").length).toBeGreaterThanOrEqual(2);
  });
});
