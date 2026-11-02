// SPDX-License-Identifier: MIT
/**
 * Journey 12 — a follow-up turn adds a second bubble to the transcript, and "New conversation" clears
 * it and starts a fresh session.
 */
import { expect, test } from "../fixtures/auth";

/** Matches the `data-testid`s in `AssistantConversation.tsx`. */
const TURN_BUBBLE_TESTID = "assistant-turn";
const MARKDOWN_ANSWER_TESTID = "markdown-answer";
const NEW_CONVERSATION_TESTID = "assistant-new-conversation";

/** Fill the prompt and send it. */
async function ask(page: import("@playwright/test").Page, prompt: string): Promise<void> {
  await page.getByTestId("assistant-prompt").fill(prompt);
  await page.getByTestId("assistant-send").click();
}

test.describe("multi-turn history accumulates and 'New conversation' resets it", () => {
  test("a follow-up adds a second turn, then a reset clears the transcript", async ({ signedInPage: page }) => {
    await page.goto("/pipeline");

    const panel = page.getByTestId("assistant-panel");
    if (await panel.isHidden()) {
      await page.getByTestId("panel-toggle").click();
    }
    await expect(panel).toBeVisible();

    // Start clean: reset any restored conversation so the counts below are exact.
    const reset = page.getByTestId(NEW_CONVERSATION_TESTID);
    if (await reset.isVisible()) {
      await reset.click();
    }
    await expect(page.getByTestId(TURN_BUBBLE_TESTID)).toHaveCount(0);

    // Turn 1.
    // Both asks are answerable from the pipeline summary, so neither turn depends on a lookup
    // the deployed tools cannot make.
    await ask(page, "Summarise my pipeline in a sentence.");
    await expect(page.getByTestId(TURN_BUBBLE_TESTID)).toHaveCount(1, { timeout: 60_000 });
    await expect(page.getByTestId(MARKDOWN_ANSWER_TESTID)).toHaveCount(1, { timeout: 60_000 });

    // Turn 2 — a follow-up that refers back to turn 1; the transcript accumulates.
    await ask(page, "Now give me that summary for the Negotiation stage only.");
    await expect(page.getByTestId(TURN_BUBBLE_TESTID)).toHaveCount(2, { timeout: 60_000 });
    await expect(page.getByTestId(MARKDOWN_ANSWER_TESTID)).toHaveCount(2, { timeout: 60_000 });

    // "New conversation" clears the transcript; the button shows only while a conversation exists.
    await page.getByTestId(NEW_CONVERSATION_TESTID).click();
    await expect(page.getByTestId(TURN_BUBBLE_TESTID)).toHaveCount(0);
    await expect(page.getByTestId(NEW_CONVERSATION_TESTID)).toHaveCount(0);
  });
});
