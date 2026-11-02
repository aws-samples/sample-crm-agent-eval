// SPDX-License-Identifier: MIT
/**
 * Journey 9 — the assistant panel returns focus to its toggle on Escape (from the shortcut with nothing
 * focused, and after a reload restored it open), exposes exactly one polite live region over the
 * transcript, mounted before any frame arrives, and its composer reaches the agent relay.
 */
import { expect, test } from "../fixtures/auth";

/** Matches `TRANSCRIPT_TESTID` in `frontend/src/components/AssistantPanel/AssistantConversation.tsx`. */
const TRANSCRIPT_TESTID = "assistant-transcript";

test.describe("the assistant panel's keyboard path and announcement channel", () => {
  test("returns focus to the toggle after Escape, even when nothing held focus", async ({ signedInPage: page }) => {
    await page.goto("/pipeline");

    const panel = page.getByTestId("assistant-panel");
    const toggle = page.getByTestId("panel-toggle");
    await expect(toggle).toBeVisible();

    // Start from a document where nothing holds focus.
    await page.evaluate(() => {
      if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
    });
    expect(await page.evaluate(() => document.activeElement?.tagName)).toBe("BODY");

    // Closed panels persist their state, so make sure the shortcut is opening and not closing.
    if (!(await panel.isHidden())) {
      await page.getByTestId("panel-close").click();
      await expect(panel).toBeHidden();
      await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
      });
    }

    await page.keyboard.press("ControlOrMeta+Shift+a");
    await expect(panel).toBeVisible();
    await expect(page.getByTestId("panel-close")).toBeFocused();

    await page.keyboard.press("Escape");

    await expect(panel).toBeHidden();
    // Focus lands on the toggle, never on the document body.
    await expect(toggle).toBeFocused();
    expect(await page.evaluate(() => document.activeElement?.tagName)).not.toBe("BODY");
  });

  test("returns focus to the toggle after Escape on a panel restored open by a reload", async ({
    signedInPage: page,
  }) => {
    /*
     * The panel hides rather than unmounts, so a reload with the panel open captures the hidden
     * `panel-close` as the element to restore focus to; focus must still land on the toggle.
     */
    await page.goto("/pipeline");
    const panel = page.getByTestId("assistant-panel");
    if (await panel.isHidden()) {
      await page.getByTestId("panel-toggle").click();
    }
    await expect(panel).toBeVisible();

    await page.reload();
    // The precondition: open on arrival, with focus already inside the panel.
    await expect(panel).toBeVisible();
    await expect(page.getByTestId("panel-close")).toBeFocused();

    await page.keyboard.press("Escape");

    await expect(panel).toBeHidden();
    await expect(page.getByTestId("panel-toggle")).toBeFocused();
    expect(await page.evaluate(() => document.activeElement?.tagName)).not.toBe("BODY");
    // Focus is not inside the now-hidden panel.
    expect(
      await page.evaluate(() => {
        const root = document.querySelector('[data-testid="assistant-panel"]');
        return root !== null && root.contains(document.activeElement);
      }),
    ).toBe(false);
  });

  test("does not move focus into the panel's toggle on a plain page load", async ({ signedInPage: page }) => {
    // The toggle fallback fires on a close only, not on every page load.
    await page.goto("/pipeline");
    await expect(page.getByTestId("panel-toggle")).toBeVisible();

    const closed = await page.getByTestId("assistant-panel").isHidden();
    test.skip(!closed, "the panel was restored open, so this load had a close to restore focus from");
    expect(await page.evaluate(() => document.activeElement?.tagName)).toBe("BODY");
  });

  test("exposes exactly one polite live region over the transcript", async ({ signedInPage: page }) => {
    await page.goto("/pipeline");

    const panel = page.getByTestId("assistant-panel");
    if (await panel.isHidden()) {
      await page.getByTestId("panel-toggle").click();
    }
    await expect(panel).toBeVisible();

    const transcript = page.getByTestId(TRANSCRIPT_TESTID);
    await expect(transcript).toBeAttached();
    await expect(transcript).toHaveAttribute("role", "log");
    // `polite`, never `assertive`: a streaming transcript on `assertive` interrupts on every frame.
    await expect(transcript).toHaveAttribute("aria-live", "polite");

    // No nested live region inside the transcript. Scoped to the transcript because `PanelTuning`
    // renders its own live region.
    const nested = await page.evaluate((testid: string) => {
      const root = document.querySelector(`[data-testid="${testid}"]`);
      return root === null ? null : root.querySelectorAll("[aria-live]").length;
    }, TRANSCRIPT_TESTID);
    expect(nested, "the transcript container was not found").not.toBeNull();
    expect(nested).toBe(0);
  });

  test("mounts the live region before any frame arrives, so the first step is announced", async ({
    signedInPage: page,
  }) => {
    // The transcript region is attached while `EventTrace`, which renders nothing for an empty turn,
    // is absent.
    await page.goto("/pipeline");

    const panel = page.getByTestId("assistant-panel");
    if (await panel.isHidden()) {
      await page.getByTestId("panel-toggle").click();
    }
    await expect(panel).toBeVisible();

    await expect(page.getByTestId("assistant-transcript")).toBeAttached();
    await expect(page.getByTestId("event-trace")).toHaveCount(0);
  });

  test("the composer reaches the agent relay, not nothing", async ({ signedInPage: page }) => {
    // Asserted at the network boundary: the composer issues a POST to `/agent/*`.
    await page.goto("/pipeline");

    const panel = page.getByTestId("assistant-panel");
    if (await panel.isHidden()) {
      await page.getByTestId("panel-toggle").click();
    }
    await expect(panel).toBeVisible();

    const turnRequest = page.waitForRequest(
      (request) => request.method() === "POST" && new URL(request.url()).pathname.startsWith("/agent/"),
    );

    await page.getByTestId("assistant-prompt").fill("which of these should I chase first");
    await page.getByTestId("assistant-send").click();

    const request = await turnRequest;
    const body = JSON.parse(request.postData() ?? "{}") as Record<string, unknown>;
    expect(body["prompt"]).toBe("which of these should I chase first");
    // The descriptor travels; rows never do.
    expect(body["page_context"]).toBeDefined();
    expect(JSON.stringify(body)).not.toContain("opportunity_id");

    // And the turn renders.
    await expect(page.getByTestId("event-trace")).toBeVisible({ timeout: 30_000 });
  });
});
