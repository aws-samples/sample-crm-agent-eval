// SPDX-License-Identifier: MIT
/**
 * Records the `data:` frames of every `text/event-stream` response the page fetches, without
 * changing what the page reads. `installSseRecorder` wraps `window.fetch` through an init script:
 * an event-stream body is `tee()`d, one branch goes back to the page in a `Response` with the same
 * status, headers and `url`, and the other is decoded into `window.__crmE2eSseFrames`.
 *
 * Install before the page navigates (the init script applies to each new document), call
 * `clearSseFrames` just before the action that starts a stream, then read with `sseFrameWithArray`.
 */
import { errors, type Page } from "@playwright/test";

/** One decoded `data:` line: its JSON object, or `{ __unparsed: <text> }` when it is not JSON. */
export type SseFrame = Record<string, unknown>;

interface RecorderWindow {
  __crmE2eSseFrames?: SseFrame[];
  __crmE2eSseRecorderInstalled?: boolean;
}

/** Runs in the browser before any page script; must stay self-contained. */
function recordSseFrames(): void {
  const recorder = window as unknown as RecorderWindow;
  if (recorder.__crmE2eSseRecorderInstalled) return;
  recorder.__crmE2eSseRecorderInstalled = true;
  recorder.__crmE2eSseFrames = [];

  const record = (line: string): void => {
    if (!line.startsWith("data:")) return;
    const text = line.slice("data:".length).trim();
    let frame: SseFrame;
    try {
      const parsed: unknown = JSON.parse(text);
      frame = typeof parsed === "object" && parsed !== null ? (parsed as SseFrame) : { __unparsed: text };
    } catch {
      frame = { __unparsed: text };
    }
    (recorder.__crmE2eSseFrames ??= []).push(frame);
  };

  const drain = async (stream: ReadableStream<Uint8Array>): Promise<void> => {
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        lines.forEach(record);
      }
      buffer += decoder.decode();
      buffer.split("\n").forEach(record);
    } catch {
      // The page's own branch reports a broken stream; the recorder keeps what it decoded.
    }
  };

  const originalFetch = window.fetch.bind(window);
  window.fetch = async (...args: Parameters<typeof fetch>): Promise<Response> => {
    const response = await originalFetch(...args);
    const contentType = response.headers.get("content-type") ?? "";
    if (!contentType.includes("text/event-stream") || response.body === null) return response;
    const [forPage, forRecorder] = response.body.tee();
    void drain(forRecorder);
    const forwarded = new Response(forPage, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
    Object.defineProperty(forwarded, "url", { value: response.url });
    Object.defineProperty(forwarded, "redirected", { value: response.redirected });
    return forwarded;
  };
}

/** Add the recorder to every document `page` loads from now on. */
export async function installSseRecorder(page: Page): Promise<void> {
  await page.addInitScript(recordSseFrames);
}

/**
 * Forget the frames recorded so far in the current document.
 *
 * @throws When the current document has no recorder, i.e. it loaded before `installSseRecorder`.
 */
export async function clearSseFrames(page: Page): Promise<void> {
  const installed = await page.evaluate(() => {
    const recorder = window as unknown as RecorderWindow;
    if (!recorder.__crmE2eSseRecorderInstalled) return false;
    recorder.__crmE2eSseFrames = [];
    return true;
  });
  if (!installed) {
    throw new Error(
      "the SSE recorder is not installed in this document: call installSseRecorder(page) before the " +
        "page navigates",
    );
  }
}

/**
 * The first recorded frame whose `key` is an array, waiting up to `timeout` ms for one to arrive;
 * `undefined` when none did.
 */
export async function sseFrameWithArray(
  page: Page,
  key: string,
  timeout: number,
): Promise<SseFrame | undefined> {
  try {
    await page.waitForFunction(
      (name) =>
        ((window as unknown as RecorderWindow).__crmE2eSseFrames ?? []).some((frame) =>
          Array.isArray(frame[name]),
        ),
      key,
      { timeout },
    );
  } catch (error) {
    if (error instanceof errors.TimeoutError) return undefined;
    throw error;
  }
  return page.evaluate(
    (name) =>
      ((window as unknown as RecorderWindow).__crmE2eSseFrames ?? []).find((frame) =>
        Array.isArray(frame[name]),
      ),
    key,
  );
}
