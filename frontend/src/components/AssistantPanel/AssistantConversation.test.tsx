// SPDX-License-Identifier: MIT
/**
 * `AssistantConversation`, the panel's production wiring: a real click on a real control produces a
 * real request to the real path, and an arriving frame reaches the transcript the user reads.
 * Requests are asserted by their bodies where a body carries a contract: `page_context`, `tuning`,
 * and `{mode: "cancel"}`.
 */

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { compileTailwindUtilities } from "../../test/compileTailwind";
import { formatUsd } from "../../lib/format";

import * as AuthProviderModule from "../../auth/AuthProvider";
import { __resetRuntimeConfigForTests, loadRuntimeConfig } from "../../config/runtimeConfig";
import {
  __resetSessionTuningForTests,
  setSessionTuning,
} from "../tuning/sessionTuning";
import {
  AssistantConversation,
  CANCEL_DISPOSITION_TEXT,
  COMPOSER_UNAVAILABLE_TEXT,
  NEW_CONVERSATION_TESTID,
  PROMPT_LABEL,
  PROMPT_TESTID,
  SEND_TESTID,
  STOP_TESTID,
  STREAM_FAILURE_TEXT,
  STREAMING_ANSWER_TESTID,
  STREAMING_TEXT_TESTID,
  TRANSCRIPT_TESTID,
  TURN_BUBBLE_TESTID,
} from "./AssistantConversation";
import { MARKDOWN_ANSWER_TESTID } from "./MarkdownAnswer";
import type { StreamFailureReason } from "../../hooks/useAgentStream";
import { toEventFrame } from "./eventFrame";
import { useReportView } from "./useReportView";
import {
  __resetViewSnapshotForTests,
  setViewSnapshot,
} from "./viewReport";
import { OpportunityListPage } from "../../pages/opportunities/OpportunityListPage";
import { AssistantPanelProvider, useAssistantPanel } from "./PanelProvider";

const RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/test";
const AGENT_PATH = `/agent/${encodeURIComponent(RUNTIME_ARN)}`;

const configBody = {
  cognitoUserPoolId: "us-east-1_test",
  cognitoClientId: "client123",
  cognitoRegion: "us-east-1",
  cognitoHostedUiDomain: "",
  apiBaseUrl: "/api",
  agentBaseUrl: "/agent",
  agentRuntimeArn: RUNTIME_ARN,
};

function frame(obj: Record<string, unknown>): string {
  return `data: ${JSON.stringify(obj)}\n\n`;
}

/** An SSE response that emits `frames` and then closes cleanly. */
function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  let index = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index >= frames.length) {
        controller.close();
        return;
      }
      controller.enqueue(encoder.encode(frames[index]));
      index += 1;
    },
  });
  return { ok: true, status: 200, body: stream } as unknown as Response;
}

/** An SSE response that never closes, so the turn stays running and `Stop` stays offered. */
function openSseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  let index = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index < frames.length) {
        controller.enqueue(encoder.encode(frames[index]));
        index += 1;
      }
      // Never closes and never errors: a turn still in flight.
    },
  });
  return { ok: true, status: 200, body: stream } as unknown as Response;
}

/**
 * An SSE response that stays open and emits nothing until `push` is called, so a test can hold a
 * turn before its first frame and then release one.
 */
function heldSseResponse(): { response: Response; push: (chunk: string) => void } {
  const encoder = new TextEncoder();
  let controllerRef: ReadableStreamDefaultController<Uint8Array> | undefined;
  const pending: string[] = [];
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controllerRef = controller;
      for (const chunk of pending.splice(0)) controller.enqueue(encoder.encode(chunk));
    },
  });
  return {
    response: { ok: true, status: 200, body: stream } as unknown as Response,
    push: (chunk) => {
      if (controllerRef === undefined) pending.push(chunk);
      else controllerRef.enqueue(encoder.encode(chunk));
    },
  };
}

interface StubOptions {
  readonly turn?: () => Response;
  readonly cancel?: () => Response;
  readonly api?: (tool: string) => Response;
}

/** Every request the component made, in order, with its parsed body. */
let requests: { url: string; body: Record<string, unknown> }[] = [];

function stubFetch(options: StubOptions = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (url === "/config.json") {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(configBody) });
      }
      const body =
        init?.body === undefined
          ? {}
          : (JSON.parse(init.body as string) as Record<string, unknown>);
      requests.push({ url, body });

      if (url === AGENT_PATH) {
        return Promise.resolve(
          body["mode"] === "cancel"
            ? (options.cancel ?? defaultCancel)()
            : (options.turn ?? (() => sseResponse([frame({ kind: "completed", text: "done" })])))(),
        );
      }
      if (url.startsWith("/api/")) {
        const tool = url.slice("/api/".length);
        return Promise.resolve(
          (options.api ?? (() => ({ ok: true, status: 200, json: () => Promise.resolve({}) }) as unknown as Response))(
            tool,
          ),
        );
      }
      throw new Error(`unexpected fetch: ${url}`);
    }),
  );
}

function defaultCancel(): Response {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve({ disposition: "accepted" }),
  } as unknown as Response;
}

function installStorage(): void {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
  });
}

beforeEach(() => {
  requests = [];
  __resetRuntimeConfigForTests();
  __resetSessionTuningForTests();
  __resetViewSnapshotForTests();
  installStorage();
  vi.spyOn(AuthProviderModule, "useAuth").mockReturnValue({
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
  } as unknown as ReturnType<typeof AuthProviderModule.useAuth>);
  stubFetch();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  __resetSessionTuningForTests();
  __resetViewSnapshotForTests();
});

async function renderConversation(path = "/opportunities") {
  await loadRuntimeConfig();
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AssistantPanelProvider>
        <AssistantConversation />
      </AssistantPanelProvider>
    </MemoryRouter>,
  );
}

/**
 * The conversation with one proposal already in the transcript and no runtime config resolved,
 * seeded through the provider.
 */
function ConversationWithSeededProposal(): React.JSX.Element {
  const { appendTurn } = useAssistantPanel();
  useEffect(() => {
    appendTurn(
      toEventFrame({
        kind: "proposal",
        proposal_id: "prop-7",
        proposal_summary: { reason: "renewal uplift", before: { amount: 1 }, after: { amount: 2 } },
      }),
    );
  }, [appendTurn]);
  return <AssistantConversation />;
}

/** Bodies sent to the turn path, cancels excluded. */
function turnBodies(): Record<string, unknown>[] {
  return requests.filter((r) => r.url === AGENT_PATH && r.body["mode"] !== "cancel").map((r) => r.body);
}

describe("the turn path has a UI caller", () => {
  it("posts to /agent/<arn> when the composer is submitted", async () => {
    const user = userEvent.setup();
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "which should I chase first");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() => expect(turnBodies()).toHaveLength(1));
    const [body] = turnBodies();
    expect(body["prompt"]).toBe("which should I chase first");
    expect(typeof body["session_id"]).toBe("string");
    expect(body["resume_from"]).toBe(0);
  });

  it("carries the page descriptor for the view the panel is attached to", async () => {
    const user = userEvent.setup();
    await renderConversation("/opportunities/opp-9");

    await user.type(screen.getByTestId(PROMPT_TESTID), "summarise this");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() => expect(turnBodies()).toHaveLength(1));
    const context = turnBodies()[0]["page_context"] as Record<string, unknown>;
    expect(context["route"]).toBe("/opportunities/opp-9");
    // No camelCase wire key.
    expect(Object.keys(turnBodies()[0])).not.toContain("pageContext");
  });

  it("carries the tuning as the schema-3 block under the wrapper's tuning key", async () => {
    // `sendPrompt`'s second argument is a wrapper containing `tuning`; every tier travels.
    const none = { family: null, model: null, effort: null };
    setSessionTuning({
      tiers: {
        supervisor: { ...none, family: "openai", effort: "high" },
        subagent: { ...none, family: "anthropic", model: "anthropic.claude-sonnet-5-5" },
        tool: none,
      },
    });
    const user = userEvent.setup();
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "summarise this");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() => expect(turnBodies()).toHaveLength(1));
    expect(turnBodies()[0]["tuning"]).toEqual({
      schema: 3,
      source: "settings.tuning",
      tiers: {
        supervisor: { ...none, family: "openai", effort: "high" },
        subagent: { ...none, family: "anthropic", model: "anthropic.claude-sonnet-5-5" },
        tool: none,
      },
    });
  });

  it("omits tuning when the session overrides nothing", async () => {
    const user = userEvent.setup();
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "summarise this");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() => expect(turnBodies()).toHaveLength(1));
    expect("tuning" in turnBodies()[0]).toBe(false);
  });

  it("refuses to send an empty prompt rather than starting a turn with nothing in it", async () => {
    const user = userEvent.setup();
    await renderConversation();

    expect(screen.getByTestId(SEND_TESTID)).toBeDisabled();
    await user.type(screen.getByTestId(PROMPT_TESTID), "   ");
    expect(screen.getByTestId(SEND_TESTID)).toBeDisabled();
    expect(turnBodies()).toHaveLength(0);
  });
});

describe("arriving frames reach the transcript the user reads", () => {
  it("renders each frame's step, and keeps them after the next turn starts", async () => {
    const user = userEvent.setup();
    let turnIndex = 0;
    stubFetch({
      turn: () => {
        turnIndex += 1;
        return turnIndex === 1
          ? sseResponse([
              frame({ kind: "tool_call", tool_name: "search_opportunities" }),
              frame({ kind: "completed", text: "three are worth chasing" }),
            ])
          : sseResponse([frame({ kind: "completed", text: "and two are stale" })]);
      },
    });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "first question");
    await user.click(screen.getByTestId(SEND_TESTID));

    // The completed answer renders as the answer bubble; the tool call renders as a step labelled by
    // its `display_label` ("Search opportunities").
    await waitFor(() => expect(screen.getByText("three are worth chasing")).toBeInTheDocument());
    expect(screen.getByText("Search opportunities")).toBeInTheDocument();

    // The conversation spans turns, although `useAgentStream` empties its transcript on each send.
    await user.type(screen.getByTestId(PROMPT_TESTID), "second question");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() => expect(screen.getByText("and two are stale")).toBeInTheDocument());
    expect(screen.getByText("three are worth chasing")).toBeInTheDocument();
    expect(screen.getByText("Search opportunities")).toBeInTheDocument();
  });

  it("continues one session across turns, then starts a fresh one on New conversation", async () => {
    const user = userEvent.setup();
    stubFetch({ turn: () => sseResponse([frame({ kind: "completed", text: "answer" })]) });
    await renderConversation();

    // Turn 1 mints a session.
    await user.type(screen.getByTestId(PROMPT_TESTID), "first question");
    await user.click(screen.getByTestId(SEND_TESTID));
    await waitFor(() => expect(turnBodies()).toHaveLength(1));
    const firstSession = turnBodies()[0]["session_id"] as string;
    expect(firstSession).toBeTruthy();

    // Turn 2 reuses the same session id.
    await user.type(screen.getByTestId(PROMPT_TESTID), "second question");
    await user.click(screen.getByTestId(SEND_TESTID));
    await waitFor(() => expect(turnBodies()).toHaveLength(2));
    expect(turnBodies()[1]["session_id"]).toBe(firstSession);
    // Both turns' bubbles are on screen.
    expect(screen.getAllByTestId(TURN_BUBBLE_TESTID)).toHaveLength(2);

    // New conversation clears the turns and arms a fresh session for the next send.
    await user.click(screen.getByTestId(NEW_CONVERSATION_TESTID));
    expect(screen.queryAllByTestId(TURN_BUBBLE_TESTID)).toHaveLength(0);

    await user.type(screen.getByTestId(PROMPT_TESTID), "third question");
    await user.click(screen.getByTestId(SEND_TESTID));
    await waitFor(() => expect(turnBodies()).toHaveLength(3));
    expect(turnBodies()[2]["session_id"]).not.toBe(firstSession);
    expect(screen.getAllByTestId(TURN_BUBBLE_TESTID)).toHaveLength(1);
  });

  it("streams the answer in place, accumulating deltas into one node per event_id", async () => {
    const user = userEvent.setup();
    // No terminal frame: the answer renders as the in-place streaming node.
    stubFetch({
      turn: () =>
        sseResponse([
          frame({ kind: "text_delta", text: "The pipeline ", event_id: "d1", partial: true }),
          frame({ kind: "text_delta", text: "totals $2M.", event_id: "d1", partial: true }),
        ]),
    });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "summarise");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() => expect(screen.getByTestId(STREAMING_ANSWER_TESTID)).toBeInTheDocument());
    // The two deltas of one event_id accumulate into a single node.
    expect(screen.getAllByTestId(STREAMING_TEXT_TESTID)).toHaveLength(1);
    expect(screen.getByTestId(STREAMING_TEXT_TESTID)).toHaveTextContent("The pipeline totals $2M.");
    // aria-hidden while streaming.
    expect(screen.getByTestId(STREAMING_ANSWER_TESTID)).toHaveAttribute("aria-hidden", "true");
    // No final markdown render before completion.
    expect(screen.queryByTestId(MARKDOWN_ANSWER_TESTID)).not.toBeInTheDocument();
  });

  it("renders the streamed answer as markdown once the turn completes", async () => {
    const user = userEvent.setup();
    // Streaming path: the answer arrives as deltas and `completed` carries no text.
    stubFetch({
      turn: () =>
        sseResponse([
          frame({ kind: "text_delta", text: "Pipeline totals **$2M**.", event_id: "d1", partial: true }),
          frame({ kind: "completed" }),
        ]),
    });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "summarise");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() => expect(screen.getByTestId(MARKDOWN_ANSWER_TESTID)).toBeInTheDocument());
    // The streamed text is now the final markdown answer, and the in-place node is gone.
    expect(screen.getByTestId(MARKDOWN_ANSWER_TESTID)).toHaveTextContent("Pipeline totals $2M.");
    expect(screen.queryByTestId(STREAMING_ANSWER_TESTID)).not.toBeInTheDocument();
  });

  it("renders one streaming node per event_id", async () => {
    const user = userEvent.setup();
    stubFetch({
      turn: () =>
        sseResponse([
          frame({ kind: "text_delta", text: "First point. ", event_id: "d1", partial: true }),
          frame({ kind: "text_delta", text: "Second point.", event_id: "d2", partial: true }),
        ]),
    });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "list points");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() => expect(screen.getAllByTestId(STREAMING_TEXT_TESTID)).toHaveLength(2));
  });

  it("renders a kind this build does not declare rather than dropping it", async () => {
    const user = userEvent.setup();
    stubFetch({
      turn: () =>
        sseResponse([
          frame({ kind: "subagent_boundary" }),
          frame({ kind: "completed", text: "done" }),
        ]),
    });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "go");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() =>
      expect(screen.getByTestId("trace-row-subagent_boundary")).toBeInTheDocument(),
    );
    expect(screen.getByText("subagent_boundary")).toBeInTheDocument();
  });

  it("meters the turn's tokens without printing a currency figure", async () => {
    const user = userEvent.setup();
    stubFetch({
      turn: () =>
        sseResponse([
          frame({ kind: "usage", input_tokens: 120, output_tokens: 30, cost_withheld: true }),
          frame({ kind: "completed", text: "done" }),
        ]),
    });
    const { container } = await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "go");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() => expect(screen.getByTestId("usage-meter")).toBeInTheDocument());
    expect(screen.getByTestId("usage-input")).toHaveTextContent("120");
    expect(screen.getByTestId("usage-cost-withheld")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/[$£€]\s?\d/);
  });
});

describe("every reply keeps its own usage meter", () => {
  /** A turn whose `completed` frame carries its own token counts and cost. */
  function pricedTurn(inputTokens: number, costUsd: number): Response {
    return sseResponse([
      frame({ kind: "usage", input_tokens: inputTokens, output_tokens: 10 }),
      frame({ kind: "completed", text: "done", cost_usd: costUsd, cost_withheld: false }),
    ]);
  }

  async function sendTurn(user: ReturnType<typeof userEvent.setup>, prompt: string, count: number) {
    await user.type(screen.getByTestId(PROMPT_TESTID), prompt);
    await user.click(screen.getByTestId(SEND_TESTID));
    await waitFor(() => expect(screen.getAllByTestId("usage-meter")).toHaveLength(count));
  }

  it("renders one meter per reply, inside its bubble, each with its own figures", async () => {
    const user = userEvent.setup();
    const turns = [pricedTurn(120, 0.0125), pricedTurn(450, 0.0375)];
    stubFetch({ turn: () => turns.shift() ?? pricedTurn(0, 0) });
    await renderConversation();

    await sendTurn(user, "first question", 1);
    await sendTurn(user, "second question", 2);

    const bubbles = screen.getAllByTestId(TURN_BUBBLE_TESTID);
    expect(bubbles).toHaveLength(2);
    const [first, second] = bubbles.map((bubble) => within(bubble).getByTestId("usage-meter"));
    expect(within(first).getByTestId("usage-input")).toHaveTextContent("120");
    expect(within(first).getByTestId("usage-cost")).toHaveTextContent(formatUsd(0.0125));
    expect(within(second).getByTestId("usage-input")).toHaveTextContent("450");
    expect(within(second).getByTestId("usage-cost")).toHaveTextContent(formatUsd(0.0375));
  });

  it("states the withholding on the reply whose frame withheld it, and only there", async () => {
    const user = userEvent.setup();
    const turns = [
      sseResponse([
        frame({ kind: "usage", input_tokens: 80, output_tokens: 5 }),
        frame({ kind: "completed", text: "done", cost_withheld: true }),
      ]),
      pricedTurn(200, 0.02),
    ];
    stubFetch({ turn: () => turns.shift() ?? pricedTurn(0, 0) });
    await renderConversation();

    await sendTurn(user, "first question", 1);
    await sendTurn(user, "second question", 2);

    const [first, second] = screen
      .getAllByTestId(TURN_BUBBLE_TESTID)
      .map((bubble) => within(bubble).getByTestId("usage-meter"));
    expect(within(first).getByTestId("usage-cost-withheld")).toBeInTheDocument();
    expect(within(first).queryByTestId("usage-cost")).toBeNull();
    expect(within(second).queryByTestId("usage-cost-withheld")).toBeNull();
    expect(within(second).getByTestId("usage-cost")).toHaveTextContent(formatUsd(0.02));
  });
});

describe("the transcript is announced politely, and the region exists before the first frame", () => {
  it("mounts exactly one polite live region over the transcript", async () => {
    const { container } = await renderConversation();

    const live = container.querySelectorAll("[aria-live]");
    expect(live).toHaveLength(1);
    expect(live[0]).toBe(screen.getByTestId(TRANSCRIPT_TESTID));
    expect(live[0].getAttribute("aria-live")).toBe("polite");
    expect(live[0].getAttribute("role")).toBe("log");
  });

  it("has the region in the DOM with an empty conversation, so the first frame is announced", async () => {
    // The live region exists before the first frame lands.
    await renderConversation();

    expect(screen.getByTestId(TRANSCRIPT_TESTID)).toBeInTheDocument();
    expect(screen.queryByTestId("event-trace")).not.toBeInTheDocument();
  });

  it("gives the composer's field a real label, not just a placeholder", async () => {
    await renderConversation();
    expect(screen.getByLabelText(PROMPT_LABEL)).toBe(screen.getByTestId(PROMPT_TESTID));
  });
});

describe("a running turn can be stopped", () => {
  it("posts the cancel body to the same endpoint a turn goes to", async () => {
    const user = userEvent.setup();
    stubFetch({ turn: () => openSseResponse([frame({ kind: "tool_call", tool_name: "x" })]) });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "a long question");
    await user.click(screen.getByTestId(SEND_TESTID));

    const stop = await screen.findByTestId(STOP_TESTID);
    await user.click(stop);

    await waitFor(() => {
      const cancels = requests.filter((r) => r.body["mode"] === "cancel");
      expect(cancels).toHaveLength(1);
      // The cancel is a body on the turn endpoint.
      expect(cancels[0].url).toBe(AGENT_PATH);
      expect(cancels[0].body["session_id"]).toBe(turnBodies()[0]["session_id"]);
    });
    expect(await screen.findByText(CANCEL_DISPOSITION_TEXT.accepted)).toBeInTheDocument();
  });

  it("holds Stop disabled until the turn's first frame arrives, and sends no cancel meanwhile", async () => {
    const user = userEvent.setup();
    const held = heldSseResponse();
    stubFetch({ turn: () => held.response });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "a long question");
    await user.click(screen.getByTestId(SEND_TESTID));

    const stop = await screen.findByTestId(STOP_TESTID);
    expect(stop).toBeDisabled();
    await user.click(stop);
    expect(requests.filter((r) => r.body["mode"] === "cancel")).toHaveLength(0);

    held.push(frame({ kind: "tool_call", tool_name: "x" }));
    await waitFor(() => expect(screen.getByTestId(STOP_TESTID)).toBeEnabled());
    await user.click(screen.getByTestId(STOP_TESTID));

    await waitFor(() => {
      const cancels = requests.filter((r) => r.body["mode"] === "cancel");
      expect(cancels).toHaveLength(1);
      expect(cancels[0].body["session_id"]).toBe(turnBodies()[0]["session_id"]);
    });
  });

  it("never enables Stop for a turn that ends before any frame", async () => {
    const user = userEvent.setup();
    stubFetch({ turn: () => sseResponse([]) });
    const { container } = await renderConversation();

    let enabledStopSeen = false;
    const observer = new MutationObserver(() => {
      const stop = container.querySelector<HTMLButtonElement>(`[data-testid="${STOP_TESTID}"]`);
      if (stop !== null && !stop.disabled) enabledStopSeen = true;
    });
    observer.observe(container, { subtree: true, childList: true, attributes: true });

    await user.type(screen.getByTestId(PROMPT_TESTID), "a question");
    await user.click(screen.getByTestId(SEND_TESTID));
    await waitFor(() => expect(turnBodies()).toHaveLength(1));
    await waitFor(() => expect(screen.queryByTestId(STOP_TESTID)).not.toBeInTheDocument());
    observer.disconnect();

    expect(enabledStopSeen).toBe(false);
    expect(requests.filter((r) => r.body["mode"] === "cancel")).toHaveLength(0);
  });

  it("offers no Stop control when no turn is running", async () => {
    await renderConversation();
    expect(screen.queryByTestId(STOP_TESTID)).not.toBeInTheDocument();
  });

  it("reports a cancel the Runtime had already finished as the no-op it is", async () => {
    const user = userEvent.setup();
    stubFetch({
      turn: () => openSseResponse([frame({ kind: "tool_call", tool_name: "x" })]),
      cancel: () =>
        ({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ disposition: "already_terminal" }),
        }) as unknown as Response,
    });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "a long question");
    await user.click(screen.getByTestId(SEND_TESTID));
    await user.click(await screen.findByTestId(STOP_TESTID));

    // A success, not rendered as an error.
    expect(
      await screen.findByText(CANCEL_DISPOSITION_TEXT.already_terminal),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("assistant-cancel-error")).not.toBeInTheDocument();
  });

  it("says the stop may not have landed when the request never arrived", async () => {
    const user = userEvent.setup();
    stubFetch({
      turn: () => openSseResponse([frame({ kind: "tool_call", tool_name: "x" })]),
      cancel: () => {
        throw new Error("network down");
      },
    });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "a long question");
    await user.click(screen.getByTestId(SEND_TESTID));
    await user.click(await screen.findByTestId(STOP_TESTID));

    const alert = await screen.findByTestId("assistant-cancel-error");
    expect(alert).toHaveAttribute("role", "alert");
    // Never "stopped" for a cancel that did not land.
    expect(alert.textContent).toContain("may still be running");
  });

  it("does not resume the turn after the user stops it, even if the stream then tears", async () => {
    /*
     * Drives the real Stop button and counts turn POSTs: `handleStop` calls
     * `stream.markTurnCancelled()`, so the torn stream of a cancelled turn is not resumed.
     */
    const user = userEvent.setup();
    let tear: () => void = () => {};
    let announceHolding: () => void = () => {};
    const holding = new Promise<void>((resolve) => {
      announceHolding = resolve;
    });
    stubFetch({
      turn: () => {
        const encoder = new TextEncoder();
        let delivered = false;
        const stream = new ReadableStream<Uint8Array>({
          pull(controller) {
            if (!delivered) {
              delivered = true;
              controller.enqueue(encoder.encode(frame({ kind: "tool_call", tool_name: "x" })));
              return;
            }
            return new Promise<void>((resolve) => {
              tear = () => {
                controller.error(new Error("connection torn by the cancelled turn"));
                resolve();
              };
              announceHolding();
            });
          },
        });
        return { ok: true, status: 200, body: stream } as unknown as Response;
      },
      cancel: () =>
        ({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ disposition: "accepted" }),
        }) as unknown as Response,
    });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "a long question");
    await user.click(screen.getByTestId(SEND_TESTID));
    await holding;
    await user.click(await screen.findByTestId(STOP_TESTID));

    tear();
    // Past the first reconnect backoff (1000ms).
    await new Promise((resolve) => setTimeout(resolve, 1400));

    expect(turnBodies()).toHaveLength(1);
  });
});

describe("a refused turn is reported, and the refusal is not retried", () => {
  it("tells the user to sign in again on a 401, without reconnecting", async () => {
    const user = userEvent.setup();
    stubFetch({
      turn: () => ({ ok: false, status: 401, body: null }) as unknown as Response,
    });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "go");
    await user.click(screen.getByTestId(SEND_TESTID));

    const alert = await screen.findByTestId("assistant-stream-error");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent(STREAM_FAILURE_TEXT.unauthenticated);
    // No retry of a rejected token.
    expect(turnBodies()).toHaveLength(1);
  });

  it("does not tell the user to sign in again on a 403, because that cannot fix it", async () => {
    const user = userEvent.setup();
    stubFetch({
      turn: () => ({ ok: false, status: 403, body: null }) as unknown as Response,
    });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "go");
    await user.click(screen.getByTestId(SEND_TESTID));

    const alert = await screen.findByTestId("assistant-stream-error");
    expect(alert).toHaveTextContent(STREAM_FAILURE_TEXT.forbidden);
    // A 403 does not read as a sign-in instruction.
    expect(alert).not.toHaveTextContent(STREAM_FAILURE_TEXT.unauthenticated);
    expect(STREAM_FAILURE_TEXT.forbidden).not.toMatch(/sign in again, then/i);
    expect(turnBodies()).toHaveLength(1);
  });

  it("gives every stream failure reason its own sentence", () => {
    // The map is asserted total against the union.
    const reasons: readonly StreamFailureReason[] = [
      "unauthenticated",
      "forbidden",
      "transport",
      "invalid_request",
    ];
    for (const reason of reasons) {
      expect(STREAM_FAILURE_TEXT[reason]).toBeTruthy();
    }
    expect(new Set(Object.values(STREAM_FAILURE_TEXT)).size).toBe(reasons.length);
  });
});

describe("a proposal is actionable without leaving the panel", () => {
  const proposalFrame = frame({
    kind: "proposal",
    proposal_id: "prop-7",
    proposal_summary: {
      target_table: "opportunities",
      target_id: "opp-1",
      reason: "renewal uplift",
      before: { amount: 1000 },
      after: { amount: 5000 },
    },
  });

  it("approves through the CRM API with an idempotency key", async () => {
    const user = userEvent.setup();
    stubFetch({ turn: () => sseResponse([proposalFrame, frame({ kind: "completed" })]) });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "raise it");
    await user.click(screen.getByTestId(SEND_TESTID));

    await user.click(await screen.findByTestId("proposal-approve"));

    await waitFor(() => {
      const approve = requests.find((r) => r.url === "/api/approve_proposal");
      expect(approve).toBeDefined();
      expect(approve?.body["proposal_id"]).toBe("prop-7");
      expect(String(approve?.body["transaction_key"] ?? "")).not.toBe("");
    });
  });

  it("rejects through the CRM API", async () => {
    const user = userEvent.setup();
    stubFetch({ turn: () => sseResponse([proposalFrame, frame({ kind: "completed" })]) });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "raise it");
    await user.click(screen.getByTestId(SEND_TESTID));

    await user.click(await screen.findByTestId("proposal-reject"));

    await waitFor(() =>
      expect(requests.find((r) => r.url === "/api/reject_proposal")?.body["proposal_id"]).toBe(
        "prop-7",
      ),
    );
  });

  it("settles the card on success, so a successful approval never looks like a pending one", async () => {
    // A settled approval shows its decision state rather than "Waiting for you".
    const user = userEvent.setup();
    stubFetch({ turn: () => sseResponse([proposalFrame, frame({ kind: "completed" })]) });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "raise it");
    await user.click(screen.getByTestId(SEND_TESTID));
    await user.click(await screen.findByTestId("proposal-approve"));

    expect(await screen.findByTestId("proposal-decision")).toHaveTextContent("Approved");
    // The controls are withdrawn, not merely disabled.
    expect(screen.queryByTestId("proposal-approve")).not.toBeInTheDocument();
    expect(screen.queryByTestId("proposal-reject")).not.toBeInTheDocument();
    expect(screen.queryByTestId("assistant-decision-error")).not.toBeInTheDocument();
    // Exactly one request.
    expect(requests.filter((r) => r.url === "/api/approve_proposal")).toHaveLength(1);
  });

  it("reports an already-decided proposal as a fact, not as an error", async () => {
    // A 409 for a non-pending proposal is not rendered in a danger surface.
    const user = userEvent.setup();
    stubFetch({
      turn: () => sseResponse([proposalFrame, frame({ kind: "completed" })]),
      api: () =>
        ({
          ok: false,
          status: 409,
          json: () =>
            Promise.resolve({ error: "conflict", message: "already decided" }),
        }) as unknown as Response,
    });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "raise it");
    await user.click(screen.getByTestId(SEND_TESTID));
    await user.click(await screen.findByTestId("proposal-approve"));

    const status = await screen.findByTestId("assistant-decision-status");
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveTextContent("had already been decided");
    expect(screen.queryByTestId("assistant-decision-error")).not.toBeInTheDocument();
  });

  it("says the outcome is unknown when the request never completed", async () => {
    // A rejection after the request may have committed the change, so the copy says the outcome is
    // unknown.
    const user = userEvent.setup();
    stubFetch({
      turn: () => sseResponse([proposalFrame, frame({ kind: "completed" })]),
      api: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "raise it");
    await user.click(screen.getByTestId(SEND_TESTID));
    await user.click(await screen.findByTestId("proposal-approve"));

    const alert = await screen.findByTestId("assistant-decision-error");
    expect(alert).toHaveTextContent("whether this approve was recorded is unknown");
    expect(alert).not.toHaveTextContent("Failed to fetch");
    // The card returns to pending so the user can retry after checking.
    expect(screen.getByTestId("proposal-approve")).toBeInTheDocument();
  });

  it("surfaces a refused decision instead of swallowing it", async () => {
    // A decision that did not land is surfaced, not swallowed.
    const user = userEvent.setup();
    stubFetch({
      turn: () => sseResponse([proposalFrame, frame({ kind: "completed" })]),
      api: () =>
        ({
          ok: false,
          status: 403,
          // The real wire envelope `crm.errors.ScopeDenied.to_api_response` emits: nested under
          // `error`, snake_case.
          json: () =>
            Promise.resolve({
              statusCode: 403,
              error: {
                code: "scope_denied",
                message: "That opportunity is out of your book.",
                missing_scope: "account:acct-9",
              },
            }),
        }) as unknown as Response,
    });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "raise it");
    await user.click(screen.getByTestId(SEND_TESTID));
    await user.click(await screen.findByTestId("proposal-approve"));

    const alert = await screen.findByTestId("assistant-decision-error");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("out of your book");
  });
});

describe("the disclosure accompanies an answer", () => {
  it("says an inexpressible view cannot be summarised, once a turn has answered", async () => {
    const user = userEvent.setup();
    await renderConversation("/accounts");

    // Nothing to disclose before anything was asked.
    expect(screen.queryByTestId("assistant-disclosure")).not.toBeInTheDocument();

    await user.type(screen.getByTestId(PROMPT_TESTID), "summarise these");
    await user.click(screen.getByTestId(SEND_TESTID));

    const disclosure = await screen.findByTestId("assistant-disclosure");
    // `/accounts` reports no query, so its descriptor is `opaque` and the answer says so.
    expect(disclosure).toHaveTextContent("can't be summarised");
    expect(disclosure).toHaveAttribute("role", "status");
  });

  it("discloses nothing for a view whose query the descriptor states", async () => {
    /*
     * `/pipeline` runs an unfiltered query, so `{}` describes it. This asserts the disclosure is
     * conditional on the descriptor's kind; it does not exercise the row-count comparison.
     */
    const user = userEvent.setup();
    await renderConversation("/pipeline");

    await user.type(screen.getByTestId(PROMPT_TESTID), "summarise these");
    await user.click(screen.getByTestId(SEND_TESTID));

    // Wait for the answer bubble, then assert no disclosure banner on `/pipeline`.
    await waitFor(() => expect(screen.getByText("done")).toBeInTheDocument());
    expect(screen.queryByTestId("assistant-disclosure")).not.toBeInTheDocument();
  });
});

describe("the row-count divergence is reachable, not merely present", () => {
/** A page reporting its view, as `OpportunityListPage` and `usePipelineBoard` do. */
  function ReportingPage(props: {
    readonly rowCount?: number;
    readonly truncated?: boolean;
    readonly filters?: Readonly<Record<string, string>>;
  }): React.JSX.Element {
    useReportView({
      query: { filters: props.filters ?? {} },
      ...(props.rowCount === undefined ? {} : { rowCount: props.rowCount }),
      ...(props.truncated === undefined ? {} : { truncated: props.truncated }),
    });
    return <p>a list view</p>;
  }

  async function renderWithPage(
    page: React.JSX.Element,
    path = "/opportunities",
  ): Promise<ReturnType<typeof render>> {
    await loadRuntimeConfig();
    return render(
      <MemoryRouter initialEntries={[path]}>
        <AssistantPanelProvider>
          {page}
          <AssistantConversation />
        </AssistantPanelProvider>
      </MemoryRouter>,
    );
  }

  /** A `tool_result` frame carrying `count` opportunities, as `search_opportunities` returns them. */
  function resultFrame(count: number, extra: Record<string, unknown> = {}): string {
    return frame({
      kind: "tool_result",
      tool_name: "search_opportunities",
      tool_output: { opportunities: Array.from({ length: count }, (_, i) => ({ id: `opp-${i}` })) },
      ...extra,
    });
  }

  it("discloses a divergence when the agent's own query returned a different count", async () => {
    // The user and the agent resolve to different scopes, and the panel says so.
    const user = userEvent.setup();
    stubFetch({ turn: () => sseResponse([resultFrame(7), frame({ kind: "completed" })]) });
    await renderWithPage(<ReportingPage rowCount={3} filters={{ stage: "Negotiation" }} />);

    await user.type(screen.getByTestId(PROMPT_TESTID), "summarise these");
    await user.click(screen.getByTestId(SEND_TESTID));

    const disclosure = await screen.findByTestId("assistant-disclosure");
    expect(disclosure).toHaveTextContent("This view shows 3 records");
    expect(disclosure).toHaveTextContent("own query returned 7");
    expect(disclosure).toHaveAttribute("role", "status");
  });

  it("carries the page's reported filters on the wire, so the agent re-runs this view's query", async () => {
    const user = userEvent.setup();
    await renderWithPage(<ReportingPage rowCount={3} filters={{ stage: "Negotiation" }} />);

    await user.type(screen.getByTestId(PROMPT_TESTID), "summarise these");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() => expect(turnBodies()).toHaveLength(1));
    const context = turnBodies()[0]["page_context"] as Record<string, unknown>;
    // The report turns an `opaque` list view into a described one.
    expect(context["kind"]).toBe("list");
    expect(context["query"]).toEqual({ filters: { stage: "Negotiation" } });
    // Still no rows anywhere in the body.
    expect(JSON.stringify(turnBodies()[0])).not.toContain("opp-");
  });

  it("discloses nothing when the counts agree", async () => {
    // Non-vacuity: matching counts produce `none`.
    const user = userEvent.setup();
    stubFetch({ turn: () => sseResponse([resultFrame(3), frame({ kind: "completed" })]) });
    await renderWithPage(<ReportingPage rowCount={3} />);

    await user.type(screen.getByTestId(PROMPT_TESTID), "summarise these");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() => expect(screen.getByTestId("event-trace")).toBeInTheDocument());
    expect(screen.queryByTestId("assistant-disclosure")).not.toBeInTheDocument();
  });

  it("refuses to compare when this view's own result was truncated", async () => {
    // A view truncated at its own ceiling is not reported as a scope difference, nor silently.
    const user = userEvent.setup();
    stubFetch({ turn: () => sseResponse([resultFrame(7), frame({ kind: "completed" })]) });
    await renderWithPage(<ReportingPage rowCount={3} truncated />);

    await user.type(screen.getByTestId(PROMPT_TESTID), "summarise these");
    await user.click(screen.getByTestId(SEND_TESTID));

    const disclosure = await screen.findByTestId("assistant-disclosure");
    expect(disclosure).toHaveTextContent("this view's own result stopped early");
    expect(disclosure).not.toHaveTextContent("own query returned");
  });

  it("refuses to compare when the agent's own result was truncated", async () => {
    const user = userEvent.setup();
    stubFetch({
      turn: () => sseResponse([resultFrame(7, { truncated: true }), frame({ kind: "completed" })]),
    });
    await renderWithPage(<ReportingPage rowCount={3} />);

    await user.type(screen.getByTestId(PROMPT_TESTID), "summarise these");
    await user.click(screen.getByTestId(SEND_TESTID));

    const disclosure = await screen.findByTestId("assistant-disclosure");
    expect(disclosure).toHaveTextContent("assistant's own query stopped early");
  });

  it("carries the real opportunity list page's applied filters, not its form state", async () => {
    /*
     * The real page: the descriptor describes the search that produced the rows on screen, not what
     * is typed into the controls.
     */
    const user = userEvent.setup();
    stubFetch({
      api: () =>
        ({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({
              items: [{ opportunity_id: "opp-1", name: "A deal", stage: "Negotiation" }],
              truncated: false,
              examined_count: 1,
              next_cursor: null,
            }),
        }) as unknown as Response,
    });
    await loadRuntimeConfig();
    render(
      <MemoryRouter initialEntries={["/opportunities"]}>
        <AssistantPanelProvider>
          <OpportunityListPage />
          <AssistantConversation />
        </AssistantPanelProvider>
      </MemoryRouter>,
    );

    // Typing without submitting does not reach the descriptor.
    await user.selectOptions(await screen.findByLabelText("Stage"), "Negotiation");
    await user.type(screen.getByTestId(PROMPT_TESTID), "summarise these");
    await user.click(screen.getByTestId(SEND_TESTID));
    await waitFor(() => expect(turnBodies()).toHaveLength(1));
    expect(
      (turnBodies()[0]["page_context"] as { query?: { filters?: unknown } }).query?.filters,
      "an unsubmitted filter must not be reported as this view's query",
    ).toEqual({});

    // Submitting does.
    await user.click(screen.getByRole("button", { name: "Search" }));
    await user.type(screen.getByTestId(PROMPT_TESTID), "and again");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() => expect(turnBodies()).toHaveLength(2));
    const context = turnBodies()[1]["page_context"] as { query?: { filters?: unknown } };
    expect(context.query?.filters).toEqual({ stage: "Negotiation" });
  });

  it("ignores a report left behind by the page the user navigated away from", async () => {
    /*
     * A snapshot stamped with another route is ignored.
     */
    setViewSnapshot({ route: "/opportunities", rowCount: 99 });
    const user = userEvent.setup();
    stubFetch({ turn: () => sseResponse([resultFrame(7), frame({ kind: "completed" })]) });

    // The panel is on a different route than the snapshot describes.
    await renderConversation("/pipeline");

    await user.type(screen.getByTestId(PROMPT_TESTID), "summarise these");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() => expect(screen.getByTestId("event-trace")).toBeInTheDocument());
    // No divergence is claimed against a count from another page.
    expect(screen.queryByTestId("assistant-disclosure")).not.toBeInTheDocument();
  });
});

describe("the composer keeps the user's message until the turn actually finished", () => {
  it("clears the field on a completed turn", async () => {
    const user = userEvent.setup();
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "which should I chase first");
    await user.click(screen.getByTestId(SEND_TESTID));

    await waitFor(() => expect(screen.getByTestId(PROMPT_TESTID)).toHaveValue(""));
  });

  it("keeps the field's text when the turn was refused, because the banner says to resend it", async () => {
    // A 401 sets `failed` and resolves without throwing; the message is kept.
    const user = userEvent.setup();
    stubFetch({ turn: () => ({ ok: false, status: 401, body: null }) as unknown as Response });
    await renderConversation();

    await user.type(screen.getByTestId(PROMPT_TESTID), "a long question worth keeping");
    await user.click(screen.getByTestId(SEND_TESTID));

    await screen.findByTestId("assistant-stream-error");
    expect(screen.getByTestId(PROMPT_TESTID)).toHaveValue("a long question worth keeping");
  });
});

describe("received content survives an unavailable runtime config", () => {
  it("shows no proposal controls it could not act on", async () => {
    // With no API client the approve/reject handlers are withheld, so `ProposalCard` renders no
    // controls.
    render(
      <MemoryRouter initialEntries={["/opportunities"]}>
        <AssistantPanelProvider>
          <ConversationWithSeededProposal />
        </AssistantPanelProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId("proposal-card")).toBeInTheDocument();
    expect(screen.queryByTestId("proposal-approve")).not.toBeInTheDocument();
    expect(screen.queryByTestId("proposal-reject")).not.toBeInTheDocument();
  });

  it("renders the transcript region and says why the composer is missing", () => {
    // Frames already on screen stay while the runtime config is unresolved.
    render(
      <MemoryRouter initialEntries={["/opportunities"]}>
        <AssistantPanelProvider>
          <AssistantConversation />
        </AssistantPanelProvider>
      </MemoryRouter>,
    );

    expect(screen.getByTestId(TRANSCRIPT_TESTID)).toBeInTheDocument();
    expect(screen.getByText(COMPOSER_UNAVAILABLE_TEXT)).toBeInTheDocument();
    expect(screen.queryByTestId(PROMPT_TESTID)).not.toBeInTheDocument();
    expect(screen.queryByTestId(SEND_TESTID)).not.toBeInTheDocument();
  });
});

/*
 * Every class this component renders is compiled through the project's Tailwind config and required
 * to emit a rule, as in `Panel.test.tsx`.
 */
describe("no silently inert utility class", () => {
  it("emits a rule for every class the conversation carries", async () => {
    const { container } = await renderConversation();

    const classes = new Set<string>();
    for (const element of container.querySelectorAll<HTMLElement>("[class]")) {
      for (const cls of element.className.split(/\s+/).filter(Boolean)) classes.add(cls);
    }
    expect(classes.size).toBeGreaterThan(0);

    const compile = (cls: string): Promise<string> =>
      compileTailwindUtilities(`<i class="${cls}"></i>`);

    // `tailwindcss-animate` emits `@keyframes` unconditionally, so a class is inert when it adds
    // nothing beyond what an unmatched class already produces.
    const baseline = await compile("crm-no-such-utility-baseline");

    const inert: string[] = [];
    for (const cls of classes) {
      if ((await compile(cls)) === baseline) inert.push(cls);
    }
    expect(inert, `these classes emit no CSS: ${inert.join(", ")}`).toEqual([]);

    // Non-vacuity, both directions.
    expect((await compile("flex")).replace(/\s+/g, "")).toContain("display:flex");
    expect(await compile("max-w-panel")).toBe(baseline);
  }, 30_000);
});
