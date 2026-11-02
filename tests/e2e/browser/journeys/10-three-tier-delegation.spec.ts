// SPDX-License-Identifier: MIT
/**
 * Journey 10 — a three-tier turn shows a subagent handoff in the trace: a `trace-step` whose
 * `data-tool` is one of the four subagent roles, followed by a rendered answer.
 */
import { expect, test } from "../fixtures/auth";

/** The four subagent roles the supervisor delegates to; a `tool_call` to one is the handoff. */
const SUBAGENT_ROLES = ["research", "mutation", "analytics", "composer"] as const;

/** Matches `EVENT_TRACE_TESTID` / the `data-testid` in `EventTrace.tsx`. */
const EVENT_TRACE_TESTID = "event-trace";
/** Matches `data-testid="trace-step"` in `EventTrace.tsx` (`StepRow`, stamped with `data-tool`). */
const TRACE_STEP_TESTID = "trace-step";
/** Matches `MARKDOWN_ANSWER_TESTID` in `MarkdownAnswer.tsx`. */
const MARKDOWN_ANSWER_TESTID = "markdown-answer";

test.describe("a three-tier turn surfaces its subagent handoff", () => {
  test("delegates to a subagent whose handoff appears as a trace step, then answers", async ({
    signedInPage: page,
  }) => {
    await page.goto("/pipeline");

    const panel = page.getByTestId("assistant-panel");
    if (await panel.isHidden()) {
      await page.getByTestId("panel-toggle").click();
    }
    await expect(panel).toBeVisible();

    // An analysis-style ask that requires delegation; any subagent role satisfies the assertion.
    // "Alderpoint Co" is a seeded `demo` account (fixtures/accounts.json, acct-demo-0014).
    await page
      .getByTestId("assistant-prompt")
      .fill(
        "Summarise the state of the Alderpoint Co account and draft a next-step for its main contact.",
      );
    await page.getByTestId("assistant-send").click();

    // The turn renders its trace, then a completed answer.
    const trace = page.getByTestId(EVENT_TRACE_TESTID);
    await expect(trace).toBeVisible({ timeout: 60_000 });

    // The three tiers resolve to one answer; the steps are read only once the turn has completed,
    // so a handoff that comes after the first step is not missed.
    await expect(page.getByTestId(MARKDOWN_ANSWER_TESTID)).toBeVisible({ timeout: 90_000 });

    // The handoff: at least one step's `data-tool` is a subagent role.
    const stepTools = await page
      .getByTestId(TRACE_STEP_TESTID)
      .evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-tool")));
    expect(
      stepTools.some((tool) => (SUBAGENT_ROLES as readonly string[]).includes(tool ?? "")),
      `no subagent handoff in the trace; step tools were ${JSON.stringify(stepTools)}`,
    ).toBe(true);
  });
});
