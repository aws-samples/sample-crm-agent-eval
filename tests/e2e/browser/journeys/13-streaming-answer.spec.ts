// SPDX-License-Identifier: MIT
/**
 * Journey 13 — while a turn streams, the answer accumulates in an `aria-hidden` streaming container;
 * on completion that container is replaced by a non-empty markdown answer and the pill reads completed.
 *
 * The streaming state is transient and can be missed on a fast turn, so the prompt asks for a
 * multi-paragraph answer: a one-sentence answer lands in a single chunk, and the final
 * deltas of a long one can arrive together after a hold of several seconds. The prompt asks
 * only for book-level totals, which one `pipeline_aggregate` call answers, so first text does not
 * wait on a per-account search fan-out. First text may take up to 60 s.
 */
import { expect, test } from "../fixtures/auth";

/** Matches the `data-testid`s in `AssistantConversation.tsx` / `MarkdownAnswer.tsx` / `TurnStatePill.tsx`. */
const STREAMING_ANSWER_TESTID = "assistant-streaming-answer";
const STREAMING_TEXT_TESTID = "assistant-streaming-text";
const MARKDOWN_ANSWER_TESTID = "markdown-answer";
const TURN_STATE_PILL_TESTID = "turn-state-pill";

/** Covers the `signedInPage` sign-in, page load, both 60 s waits and a margin. */
const TEST_TIMEOUT_MS = 180_000;

/** A prompt whose answer takes several seconds to stream, answerable from book-level aggregates. */
const LONG_ANSWER_PROMPT =
  "Using only the aggregate totals for my whole book (the stage funnel counts, the total pipeline " +
  "value, and the forecast by close date), write a structured overview: a heading and a short " +
  "paragraph for each stage of the funnel, a paragraph on the close-date forecast, and a closing " +
  "paragraph with three recommendations. Do not look up or list individual accounts or opportunities.";

test.describe("a streamed answer grows in place and finalises into a markdown bubble", () => {
  test.setTimeout(TEST_TIMEOUT_MS);

  test("shows the streaming container while streaming, then replaces it with the final answer", async ({
    signedInPage: page,
  }) => {
    await page.goto("/pipeline");

    const panel = page.getByTestId("assistant-panel");
    if (await panel.isHidden()) {
      await page.getByTestId("panel-toggle").click();
    }
    await expect(panel).toBeVisible();

    await page.getByTestId("assistant-prompt").fill(LONG_ANSWER_PROMPT);
    await page.getByTestId("assistant-send").click();

    // While the turn streams: the streaming container is present and aria-hidden, and the final
    // markdown answer is not yet rendered.
    const streaming = page.getByTestId(STREAMING_ANSWER_TESTID);
    await expect(streaming).toBeVisible({ timeout: 60_000 });
    await expect(streaming).toHaveAttribute("aria-hidden", "true");
    // Text accumulates into one node per answer, keyed by its event_id.
    const streamingText = page.getByTestId(STREAMING_TEXT_TESTID).first();
    await expect(streamingText).toHaveAttribute("data-event-id", /.+/);
    await expect(page.getByTestId(MARKDOWN_ANSWER_TESTID)).toHaveCount(0);

    // On completion: the streaming container is replaced by a non-empty markdown answer, and the
    // turn's terminal pill reads completed.
    const answer = page.getByTestId(MARKDOWN_ANSWER_TESTID);
    await expect(answer).toBeVisible({ timeout: 60_000 });
    await expect(answer).not.toBeEmpty();
    await expect(page.getByTestId(STREAMING_ANSWER_TESTID)).toHaveCount(0);
    await expect(page.getByTestId(TURN_STATE_PILL_TESTID)).toHaveAttribute("data-status", "completed");
  });
});
