// SPDX-License-Identifier: MIT
/**
 * Journey 11 — a completed turn collapses its steps disclosure (summary "Worked through N steps") and
 * renders the answer as markdown, with a table rather than literal pipe-delimited text.
 */
import { expect, test } from "../fixtures/auth";

/** Matches the `data-testid`s in `EventTrace.tsx` / `MarkdownAnswer.tsx`. */
const TRACE_STEPS_TESTID = "trace-steps";
const MARKDOWN_ANSWER_TESTID = "markdown-answer";

const ANSWER_TIMEOUT_MS = 120_000;

test.describe.configure({ timeout: ANSWER_TIMEOUT_MS + 60_000 });

test.describe("a completed turn folds its steps and renders a markdown answer", () => {
  test("collapses the steps disclosure on completion and renders the answer as markdown", async ({
    signedInPage: page,
  }) => {
    await page.goto("/pipeline");

    const panel = page.getByTestId("assistant-panel");
    if (await panel.isHidden()) {
      await page.getByTestId("panel-toggle").click();
    }
    await expect(panel).toBeVisible();

    // A list ask returns three or more records, which the answer-style prompt formats as a table.
    await page
      .getByTestId("assistant-prompt")
      .fill("List the open opportunities in the pipeline with their stage and amount.");
    await page.getByTestId("assistant-send").click();

    // The answer arrives (the turn completed).
    const answer = page.getByTestId(MARKDOWN_ANSWER_TESTID);
    await expect(answer).toBeVisible({ timeout: ANSWER_TIMEOUT_MS });

    // Once the turn is done the steps disclosure is collapsed (no `open` attribute) with the
    // "Worked through" summary.
    const steps = page.getByTestId(TRACE_STEPS_TESTID);
    await expect(steps).toBeAttached();
    await expect(steps).not.toHaveAttribute("open", /.*/);
    await expect(steps.locator("summary")).toContainText(/Worked through \d+ step/);

    // The answer is rendered markdown: at least one <table>, and no raw pipe-delimited row.
    await expect(answer.locator("table").first()).toBeVisible();
    await expect(answer).not.toContainText("|---");
  });
});
