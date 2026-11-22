// SPDX-License-Identifier: MIT
/**
 * Journey 22: the assistant recalls a fact from an earlier turn of the same conversation, and a new
 * conversation does not.
 *
 * Turn 1 asks the assistant to remember a run-scoped code word and to reply without repeating it.
 * Turn 2, in the same conversation, asks for the code word, and its answer must contain it. Both
 * requests are read at the network boundary. Turn 2 must reuse turn 1's `session_id` with a
 * positive `resume_from`, and its answer must differ from turn 1's, so a replay of turn 1 cannot
 * pass. No turn here is a reconnect, so no request carries `resume`. Turn 3 states a second code
 * word, and turn 4 must recall both. After "New conversation", the first question gets a fresh
 * `session_id` and an answer that does not contain the code word.
 * Every request is the SPA's own; nothing is overridden.
 *
 * User: the demo user (`CRM_DEMO_USER_EMAIL`). Each turn calls a real model and is billed as any
 * chat turn is.
 */
import type { Page, Request } from "@playwright/test";

import { expect, test } from "../../fixtures/auth";
import { randomToken } from "../../fixtures/randomToken";

/** Matches the `data-testid`s in `AssistantConversation.tsx` and `MarkdownAnswer.tsx`. */
const PANEL_TESTID = "assistant-panel";
const MARKDOWN_ANSWER_TESTID = "markdown-answer";
const NEW_CONVERSATION_TESTID = "assistant-new-conversation";
const TURN_BUBBLE_TESTID = "assistant-turn";

const TURN_TIMEOUT_MS = 90_000;

/** The fields of one chat-turn request body this journey reads. */
interface TurnRequestBody {
  readonly prompt: string;
  readonly session_id: string;
  readonly resume_from: number | undefined;
  readonly resume: unknown;
}

function isTurnRequest(request: Request): boolean {
  return request.method() === "POST" && new URL(request.url()).pathname.startsWith("/agent/");
}

function turnBody(request: Request): TurnRequestBody {
  const body = JSON.parse(request.postData() ?? "{}") as Record<string, unknown>;
  return {
    prompt: String(body.prompt ?? ""),
    session_id: String(body.session_id ?? ""),
    resume_from: typeof body.resume_from === "number" ? body.resume_from : undefined,
    resume: body.resume,
  };
}

/** Send `prompt`, wait for its answer to render, and return the request body and the answer text. */
async function sendTurn(page: Page, prompt: string): Promise<{ body: TurnRequestBody; answer: string }> {
  const answers = page.getByTestId(MARKDOWN_ANSWER_TESTID);
  const before = await answers.count();
  const requested = page.waitForRequest(
    (request) => isTurnRequest(request) && turnBody(request).prompt === prompt,
    { timeout: TURN_TIMEOUT_MS },
  );
  await page.getByTestId("assistant-prompt").fill(prompt);
  await page.getByTestId("assistant-send").click();
  const body = turnBody(await requested);
  expect(body.resume, "a plain follow-up is not a reconnect and carries no resume").toBeUndefined();
  await expect(answers).toHaveCount(before + 1, { timeout: TURN_TIMEOUT_MS });
  await expect(page.getByTestId("turn-state-pill").last()).toHaveAttribute("data-status", "completed", {
    timeout: TURN_TIMEOUT_MS,
  });
  const answer = (await answers.last().textContent()) ?? "";
  return { body, answer };
}

async function openCleanPanel(page: Page): Promise<void> {
  await page.goto("/pipeline");
  const panel = page.getByTestId(PANEL_TESTID);
  if (await panel.isHidden()) await page.getByTestId("panel-toggle").click();
  await expect(panel).toBeVisible();
  const reset = page.getByTestId(NEW_CONVERSATION_TESTID);
  if (await reset.isVisible()) await reset.click();
  await expect(page.getByTestId(TURN_BUBBLE_TESTID)).toHaveCount(0);
}

test.describe("chat recall across turns", () => {
  test("follow-ups in the same conversation recall earlier code words; a new conversation does not", async ({
    signedInPage: page,
  }) => {
    const codeWord = `kestrel-${randomToken(8)}`;
    const question = "What is the code word I asked you to remember? Reply with the code word only.";
    await openCleanPanel(page);

    const seed = await sendTurn(
      page,
      `Remember the code word ${codeWord} for later in this conversation. Do not repeat it now; reply with only the word OK.`,
    );
    expect(seed.body.session_id, "turn 1 carries a session id").not.toBe("");
    expect(
      seed.answer.toLowerCase(),
      "turn 1's answer does not contain the code word, so recall below is not an echo of it",
    ).not.toContain(codeWord);

    const recall = await sendTurn(page, question);
    expect(recall.body.session_id, "turn 2 continues turn 1's session").toBe(seed.body.session_id);
    expect(recall.body.resume_from ?? 0, "turn 2 sends the events already received").toBeGreaterThan(0);
    expect(recall.answer, "turn 2 is a new answer, not a replay of turn 1").not.toBe(seed.answer);
    expect(recall.answer.toLowerCase(), "turn 2 recalls the code word").toContain(codeWord);

    const secondWord = `heron-${randomToken(8)}`;
    const secondSeed = await sendTurn(
      page,
      `Also remember a second code word, ${secondWord}. Do not repeat either code word now; reply with only the word OK.`,
    );
    expect(secondSeed.body.session_id, "turn 3 continues the same session").toBe(seed.body.session_id);
    expect(
      secondSeed.body.resume_from ?? 0,
      "turn 3's cursor is past every event of turns 1 and 2",
    ).toBeGreaterThan(recall.body.resume_from ?? 0);
    expect(secondSeed.answer.toLowerCase(), "turn 3's answer does not echo the second code word").not.toContain(
      secondWord,
    );

    const both = await sendTurn(page, "List both code words I asked you to remember, and nothing else.");
    expect(both.body.session_id, "turn 4 continues the same session").toBe(seed.body.session_id);
    const bothAnswer = both.answer.toLowerCase();
    expect(bothAnswer, "turn 4 recalls the code word from turn 1").toContain(codeWord);
    expect(bothAnswer, "turn 4 recalls the code word from turn 3").toContain(secondWord);

    await page.getByTestId(NEW_CONVERSATION_TESTID).click();
    await expect(page.getByTestId(TURN_BUBBLE_TESTID)).toHaveCount(0);

    const fresh = await sendTurn(page, question);
    expect(fresh.body.session_id, "a new conversation mints a new session id").not.toBe(seed.body.session_id);
    expect(fresh.answer.toLowerCase(), "a new conversation does not know the code word").not.toContain(codeWord);
  });
});
