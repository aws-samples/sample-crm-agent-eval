// SPDX-License-Identifier: MIT
/**
 * The assistant conversation: the composer, the streamed transcript, the metering, and the proposal
 * affordances. The only production caller of the turn path: `useAgentStream` opens the SSE stream
 * through `/agent/*`, `useCancelTurn` posts the cancel, `usePageContext` supplies the descriptor, and
 * `useAssistantTurn` derives the disclosure. Rendered inside `Panel` through its `children` seam.
 *
 * `useAgentStream` holds the current turn and empties its transcript on every `sendPrompt`; each
 * arriving frame is forwarded into `AssistantPanelProvider`, which holds the whole conversation.
 *
 * The transcript renders whether or not the runtime config has resolved; only the composer depends
 * on it. One always-mounted `polite` live region covers the transcript.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, JSX } from "react";
import { useLocation } from "react-router-dom";

import { CrmApiError, CrmApiClient } from "../../api/CrmApiClient";
import { useAuth } from "../../auth/AuthProvider";
import { getRuntimeConfig } from "../../config/runtimeConfig";
import { useAgentStream } from "../../hooks/useAgentStream";
import { usePageContext } from "../../hooks/usePageContext";
import { newTransactionKey } from "../../pages/approvals/transactionKey";
import { getSessionTuning } from "../tuning/sessionTuning";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import type { AgentEventFrame } from "../../types/generated/agentEvents";
import { EventTrace } from "./EventTrace";
import { toEventFrame } from "./eventFrame";
import { MarkdownAnswer } from "./MarkdownAnswer";
import { useAssistantPanel, type ConversationTurn } from "./PanelProvider";
import type { ProposalDecision } from "./ProposalCard";
import { TurnStatePill, type TurnTerminalState } from "./TurnStatePill";
import { UsageMeter } from "./UsageMeter";
import { useAssistantTurn } from "./useAssistantTurn";
import { useCancelTurn } from "./useCancelTurn";
import { snapshotForRoute, useViewSnapshot } from "./viewReport";
import { toWireTuning } from "./wireTuning";

export const CONVERSATION_TESTID = "assistant-conversation";
export const TRANSCRIPT_TESTID = "assistant-transcript";
export const PROMPT_TESTID = "assistant-prompt";
export const SEND_TESTID = "assistant-send";
export const STOP_TESTID = "assistant-stop";

/** One turn's bubble, and the user's own prompt within it. */
export const TURN_BUBBLE_TESTID = "assistant-turn";
export const USER_PROMPT_TESTID = "assistant-user-prompt";

/** The control that ends the current conversation and starts a fresh session on the next send. */
export const NEW_CONVERSATION_TESTID = "assistant-new-conversation";

/** The answer while it streams (before completion), and one node per `event_id` within it. */
export const STREAMING_ANSWER_TESTID = "assistant-streaming-answer";
export const STREAMING_TEXT_TESTID = "assistant-streaming-text";

/** The composer's visible label, and the accessible name of its field. */
export const PROMPT_LABEL = "Ask about this page";

/**
 * Shown in place of the composer when the runtime config is unavailable. Asks for a reload, which is
 * the one action that can fix a cold config cache.
 */
export const COMPOSER_UNAVAILABLE_TEXT =
  "The assistant could not be configured, so no new message can be sent. Reload the page. The " +
  "conversation above is unaffected.";

/** Connection states during which a turn is still running and can still be stopped. */
const RUNNING_STATES: readonly string[] = ["connecting", "streaming", "reconnecting"];

/**
 * What the user is told for each terminal stream failure, one sentence per remedy. `useAgentStream`
 * retries none of them. `forbidden` mentions neither signing in nor a cause: the browser cannot tell
 * which of the possible 403 causes applies.
 */
export const STREAM_FAILURE_TEXT = {
  unauthenticated: "Your session was refused. Sign in again, then resend the message.",
  forbidden: "The assistant refused this request. Reload the page and try again; if it keeps happening, report it — signing in again will not help.",
  transport: "The connection dropped and could not be resumed. Resend the message to try again.",
  invalid_request:
    "The assistant couldn't accept these settings. Change the model or tuning and try again.",
} as const;

/**
 * What the user is told for each refused cancel. `forbidden` is not worded as a sign-in problem.
 */
export const CANCEL_FAILURE_TEXT = {
  unauthenticated: "Your session was refused, so the turn was not stopped.",
  forbidden: "This turn could not be stopped from here.",
  rejected: "The turn could not be stopped.",
  transport: "The stop request did not reach the assistant, so the turn may still be running.",
} as const;

/** Shown when the handler reports the proposal was already decided. A fact, not a failure. */
export const DECISION_ALREADY_DECIDED_TEXT =
  "That proposal had already been decided, so nothing changed.";

/**
 * Shown when a decision request failed in a way that leaves its outcome unknown: the change may have
 * committed, so the wording sends the user to the record.
 */
export const DECISION_UNKNOWN_OUTCOME_TEXT = (action: "approve" | "reject"): string =>
  `The connection failed, so whether this ${action} was recorded is unknown. Check the Approvals ` +
  "page before trying again.";

/** What the user is told for each cancel outcome. All three are successes, the no-op included. */
export const CANCEL_DISPOSITION_TEXT = {
  accepted: "Stopping after the current step.",
  already_requested: "Already stopping.",
  already_terminal: "The turn had already finished.",
} as const;

/** The three endpoints this component needs, or `null` while the runtime config is unresolved. */
interface Endpoints {
  readonly agentBaseUrl: string;
  readonly agentRuntimeArn: string;
  readonly apiBaseUrl: string;
}

function readEndpoints(): Endpoints | null {
  try {
    const { agentBaseUrl, agentRuntimeArn, apiBaseUrl } = getRuntimeConfig();
    return { agentBaseUrl, agentRuntimeArn, apiBaseUrl };
  } catch {
    return null;
  }
}

/** The answer's streamed text for one `event_id`, in arrival order. */
interface StreamGroup {
  readonly eventId: string;
  readonly text: string;
}

/**
 * How a turn's answer stands: the final text to render as markdown once complete, the still-streaming
 * text grouped by `event_id`, and whether the turn has ended.
 *
 * On the streaming path the answer arrives as `text_delta` chunks grouped by `event_id`; on the
 * non-streaming path it is the `completed` frame's `text`. The final text is the `completed` text
 * when present, otherwise the concatenated deltas; an errored turn still shows what streamed.
 */
interface AnswerState {
  readonly finalText: string | null;
  readonly streamGroups: readonly StreamGroup[];
  readonly terminal: boolean;
}

function answerState(frames: readonly AgentEventFrame[]): AnswerState {
  let terminal = false;
  let completedText: string | null = null;
  const order: string[] = [];
  const byId = new Map<string, string>();

  for (const frame of frames) {
    if (frame.kind === "completed" || frame.kind === "error") terminal = true;
    if (frame.kind === "completed" && frame.text !== null && frame.text.trim() !== "") {
      completedText = frame.text;
    }
    if (frame.kind === "text_delta" && frame.text !== null) {
      const id = frame.event_id ?? "";
      if (!byId.has(id)) order.push(id);
      byId.set(id, (byId.get(id) ?? "") + frame.text);
    }
  }

  const streamGroups = order.map((eventId) => ({ eventId, text: byId.get(eventId) ?? "" }));
  const concatenated = streamGroups.map((group) => group.text).join("");
  const finalText = completedText ?? (concatenated === "" ? null : concatenated);
  return { finalText, streamGroups, terminal };
}

/**
 * The answer while it is still streaming: one node per `event_id`, updated in place as chunks arrive,
 * with a caret on the active (last) group.
 *
 * Plain text with `whitespace-pre-wrap` rather than markdown, `aria-hidden` so deltas are not read
 * aloud, and the same type scale and spacing as {@link MarkdownAnswer} so the final swap causes no
 * layout shift.
 */
function StreamingAnswer({ groups }: { groups: readonly StreamGroup[] }): JSX.Element {
  return (
    <div
      data-testid={STREAMING_ANSWER_TESTID}
      aria-hidden="true"
      className="space-y-3 text-sm leading-relaxed text-text-secondary"
    >
      {groups.map((group, index) => (
        <p
          key={group.eventId === "" ? index : group.eventId}
          data-testid={STREAMING_TEXT_TESTID}
          data-event-id={group.eventId}
          className="whitespace-pre-wrap"
        >
          {group.text}
          {index === groups.length - 1 ? (
            <span className="ml-0.5 inline-block animate-pulse text-accent-text motion-reduce:animate-none">
              ▋
            </span>
          ) : null}
        </p>
      ))}
    </div>
  );
}

/**
 * How a turn ended, from its own frames plus whether the user cancelled it: an error wins, then a
 * user cancel, then `completed`. `null` means the turn has not ended.
 */
function resolveTerminalState(
  frames: readonly AgentEventFrame[],
  cancelled: boolean,
): TurnTerminalState | null {
  for (let index = frames.length - 1; index >= 0; index -= 1) {
    if (frames[index].kind === "error") return { status: "error", event: frames[index] };
  }
  if (cancelled) return { status: "cancelled" };
  if (frames.some((frame) => frame.kind === "completed")) return { status: "completed" };
  return null;
}

/**
 * One turn, as a bubble: the user's prompt (when present), the steps it took, its answer, a
 * terminal-state pill, and its own usage meter, each once the turn has produced them.
 */
function TurnBubble({
  turn,
  decisions,
  onApprove,
  onReject,
}: {
  readonly turn: ConversationTurn;
  readonly decisions: Readonly<Record<string, ProposalDecision>>;
  readonly onApprove?: (id: string) => void;
  readonly onReject?: (id: string) => void;
}): JSX.Element {
  const answer = answerState(turn.frames);
  return (
    <section data-testid={TURN_BUBBLE_TESTID} className="flex flex-col gap-4">
      {turn.prompt === "" ? null : (
        <p
          data-testid={USER_PROMPT_TESTID}
          className="self-end rounded-surface border border-hairline border-border-default bg-surface-inset px-4 py-2 text-xs text-text-primary"
        >
          {turn.prompt}
        </p>
      )}
      <EventTrace
        events={turn.frames}
        decisions={decisions}
        {...(onApprove === undefined ? {} : { onApprove })}
        {...(onReject === undefined ? {} : { onReject })}
      />
      {/*
        The answer: streamed in place while the turn runs, then the final markdown once it completes.
        The swap keeps the same slot and type scale, so there is no layout shift.
      */}
      {answer.terminal
        ? answer.finalText === null
          ? null
          : <MarkdownAnswer text={answer.finalText} />
        : answer.streamGroups.length === 0
          ? null
          : <StreamingAnswer groups={answer.streamGroups} />}
      {turn.terminalState === null ? null : (
        <div className="self-start">
          <TurnStatePill state={turn.terminalState} />
        </div>
      )}
      <UsageMeter events={turn.frames} />
    </section>
  );
}

export function AssistantConversation(): JSX.Element {
  const auth = useAuth();
  const { turns, conversation, beginTurn, appendTurn, finalizeTurn, resetConversation, draft, clearDraft } =
    useAssistantPanel();
  const endpoints = readEndpoints();

  /*
   * What the routed page reported about the view it is showing, matched to the current pathname so a
   * report from a page the user navigated away from is ignored.
   */
  const { pathname } = useLocation();
  const view = snapshotForRoute(useViewSnapshot(), pathname);
  const pageContext = usePageContext(view?.report);

  const stream = useAgentStream({
    agentBaseUrl: endpoints?.agentBaseUrl ?? "",
    agentRuntimeArn: endpoints?.agentRuntimeArn ?? "",
    getAccessToken: auth.getAccessToken,
  });
  const cancel = useCancelTurn({
    agentBaseUrl: endpoints?.agentBaseUrl ?? "",
    agentRuntimeArn: endpoints?.agentRuntimeArn ?? "",
    getAccessToken: auth.getAccessToken,
  });
  const { disclosure } = useAssistantTurn({
    pageContext,
    transcript: stream.transcript,
    ...(view?.rowCount === undefined ? {} : { viewRowCount: view.rowCount }),
    ...(view?.truncated === undefined ? {} : { viewTruncated: view.truncated }),
  });

  const [prompt, setPrompt] = useState("");
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [decisionStatus, setDecisionStatus] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<Readonly<Record<string, ProposalDecision>>>({});

  /*
   * Forwarding cursor into the hook's transcript.
   */
  const forwardedCountRef = useRef(0);
  // Whether the user asked to stop the current turn; a cancelled turn has no terminal frame. Reset
  // per send.
  const cancelledTurnRef = useRef(false);
  // Whether the next send starts a fresh session (the "New conversation" control) rather than
  // continuing the current one. Read and cleared by the send.
  const startFreshRef = useRef(false);

  useEffect(() => {
    // Every send resets the hook transcript to [], so a length below the cursor restarts forwarding
    // from the top of this turn. A reconnect only grows the transcript.
    if (stream.transcript.length < forwardedCountRef.current) forwardedCountRef.current = 0;
    for (let index = forwardedCountRef.current; index < stream.transcript.length; index += 1) {
      appendTurn(toEventFrame(stream.transcript[index].event));
    }
    forwardedCountRef.current = stream.transcript.length;
  }, [appendTurn, stream.transcript]);

  /*
   * Record how the turn ended, resolved from the stream's own transcript, which this commit can
   * already see. `finalizeTurn` only writes the first outcome.
   */
  useEffect(() => {
    if (!stream.turnEnded) return;
    const frames = stream.transcript.map((entry) => toEventFrame(entry.event));
    const state = resolveTerminalState(frames, cancelledTurnRef.current);
    if (state !== null) finalizeTurn(state);
  }, [finalizeTurn, stream.turnEnded, stream.transcript]);

  /*
   * Adopt a prompt-chip fill: copy it into the composer during render, then focus the field by id
   * (the `ui/input` primitive forwards no ref) and clear the one-shot signal.
   */
  const [adoptedDraft, setAdoptedDraft] = useState<string | null>(null);
  if (draft !== adoptedDraft) {
    setAdoptedDraft(draft);
    if (draft !== null) setPrompt(draft);
  }
  useEffect(() => {
    if (draft === null) return;
    document.getElementById(PROMPT_TESTID)?.focus();
    clearDraft();
  }, [draft, clearDraft]);

  /*
   * The composer clears when a turn actually finishes, and keeps the text otherwise; a 401 sets
   * `failed` and resolves without throwing. The field is disabled while the turn runs.
   */
  const promptClearDue = stream.turnEnded && stream.connectionState !== "failed";
  const [promptClearApplied, setPromptClearApplied] = useState(promptClearDue);
  if (promptClearDue !== promptClearApplied) {
    setPromptClearApplied(promptClearDue);
    if (promptClearDue) setPrompt("");
  }

  const apiBaseUrl = endpoints?.apiBaseUrl ?? null;
  const api = useMemo(
    () => (apiBaseUrl === null ? null : CrmApiClient.fromAuth(apiBaseUrl, auth)),
    [apiBaseUrl, auth],
  );

  const running = RUNNING_STATES.includes(stream.connectionState);
  /** A received frame proves the Runtime has registered the turn, so a cancel can find it. */
  const turnRegistered = stream.transcript.length > 0;
  const canSend = endpoints !== null && !running && prompt.trim() !== "";

  async function handleSend(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!canSend) return;
    const text = prompt.trim();
    setDecisionError(null);
    setDecisionStatus(null);
    cancel.reset();
    // Open the bubble for this send and arm a fresh cancel signal before the turn's frames arrive.
    cancelledTurnRef.current = false;
    beginTurn(text);
    /*
     * Continue the current session unless this is the first turn or the user asked for a fresh one.
     * The session id carries no authority.
     */
    const continueSessionId =
      !startFreshRef.current && stream.sessionId !== undefined ? stream.sessionId : undefined;
    startFreshRef.current = false;
    /*
     * `sendPrompt`'s second argument is a wrapper carrying `pageContext` and `tuning`. The descriptor
     * is replayed on a reconnect; the tuning is read at send time.
     */
    const tuning = toWireTuning(getSessionTuning());
    /*
     * `sendPrompt` rethrows when no token can be minted, having already set
     * `failed`/`unauthenticated`, so the rejection is caught here. The composer is not cleared here.
     */
    try {
      await stream.sendPrompt(text, {
        pageContext,
        ...(tuning === undefined ? {} : { tuning }),
        ...(continueSessionId === undefined ? {} : { continueSessionId }),
      });
    } catch {
      // Reported through `connectionState`/`failureReason`, which the banners above render.
    }
  }

  async function handleStop(): Promise<void> {
    if (stream.sessionId === undefined || !turnRegistered) return;
    /*
     * Marked before the request: the Runtime ending a cancelled turn can tear the stream, which
     * `useAgentStream` would otherwise resume.
     */
    cancelledTurnRef.current = true;
    stream.markTurnCancelled();
    await cancel.cancelTurn(stream.sessionId);
  }

  /*
   * Start a fresh conversation: clear the visible turns and arm the next send to mint a new session.
   * Nothing is torn down mid-turn.
   */
  function handleNewConversation(): void {
    startFreshRef.current = true;
    resetConversation();
    setPrompt("");
    setDecisionError(null);
    setDecisionStatus(null);
  }

  const decide = useCallback(
    async (proposalId: string, action: "approve" | "reject"): Promise<void> => {
      // Unreachable while the controls are withheld below.
      if (api === null) return;
      if (decisions[proposalId] !== undefined) return;

      setDecisionError(null);
      setDecisions((previous) => ({ ...previous, [proposalId]: "deciding" }));
      const input = { proposal_id: proposalId, transaction_key: newTransactionKey() };
      try {
        if (action === "approve") await api.approveProposal(input);
        else await api.rejectProposal(input);
        setDecisions((previous) => ({
          ...previous,
          [proposalId]: action === "approve" ? "approved" : "rejected",
        }));
      } catch (err) {
        /*
         * A `conflict` means the proposal was already decided, so it settles the card instead of
         * raising an alert. A transport failure leaves the outcome unknown.
         */
        if (err instanceof CrmApiError && err.kind === "conflict") {
          setDecisions((previous) => ({
            ...previous,
            [proposalId]: action === "approve" ? "approved" : "rejected",
          }));
          setDecisionStatus(DECISION_ALREADY_DECIDED_TEXT);
          return;
        }
        setDecisions((previous) => {
          const next = { ...previous };
          delete next[proposalId];
          return next;
        });
        setDecisionError(
          err instanceof CrmApiError
            ? err.body.message
            : DECISION_UNKNOWN_OUTCOME_TEXT(action),
        );
      }
    },
    [api, decisions],
  );

  const onApprove = useCallback((id: string) => void decide(id, "approve"), [decide]);
  const onReject = useCallback((id: string) => void decide(id, "reject"), [decide]);

  return (
    <div data-testid={CONVERSATION_TESTID} className="flex min-h-0 flex-col gap-5">
      {/*
        Always mounted, so the first frame of a turn is announced. `aria-relevant="additions"` keeps
        the announcement to the newly appended step rather than re-reading the whole transcript.
      */}
      <div
        data-testid={TRANSCRIPT_TESTID}
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label="Assistant conversation"
        className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto"
      >
        {/*
          One bubble per turn — the user's prompt, its steps, its answer, its terminal pill. The
          approve/reject handlers are passed only when there is an API client; `ProposalCard` renders
          its controls only when they are supplied.
        */}
        {conversation.map((turn, index) => (
          <TurnBubble
            key={index}
            turn={turn}
            decisions={decisions}
            {...(api === null ? {} : { onApprove, onReject })}
          />
        ))}
      </div>

      {/* The disclosure accompanies an answer, so it is hidden until the first turn exists. */}
      {turns.length === 0 || disclosure.kind === "none" ? null : (
        <p
          data-testid="assistant-disclosure"
          role="status"
          className="rounded-surface border border-hairline border-caution-border bg-caution-surface px-4 py-3 text-2xs leading-relaxed text-caution-text"
        >
          {disclosure.message}
        </p>
      )}

      {stream.connectionState === "failed" && stream.failureReason !== undefined ? (
        <p
          data-testid="assistant-stream-error"
          role="alert"
          className="rounded-surface border border-hairline border-danger-border bg-danger-surface px-4 py-3 text-2xs leading-relaxed text-danger-text"
        >
          {stream.failureMessage ?? STREAM_FAILURE_TEXT[stream.failureReason]}
        </p>
      ) : null}

      {cancel.cancelState === "failed" && cancel.failureReason !== undefined ? (
        <p
          data-testid="assistant-cancel-error"
          role="alert"
          className="rounded-surface border border-hairline border-danger-border bg-danger-surface px-4 py-3 text-2xs leading-relaxed text-danger-text"
        >
          {CANCEL_FAILURE_TEXT[cancel.failureReason]}
        </p>
      ) : null}

      {cancel.cancelState === "requested" && cancel.disposition !== undefined ? (
        <p
          data-testid="assistant-cancel-status"
          role="status"
          className="text-2xs text-text-secondary"
        >
          {CANCEL_DISPOSITION_TEXT[cancel.disposition]}
        </p>
      ) : null}

      {decisionStatus === null ? null : (
        <p
          data-testid="assistant-decision-status"
          role="status"
          className="text-2xs leading-relaxed text-text-secondary"
        >
          {decisionStatus}
        </p>
      )}

      {decisionError === null ? null : (
        <p
          data-testid="assistant-decision-error"
          role="alert"
          className="rounded-surface border border-hairline border-danger-border bg-danger-surface px-4 py-3 text-2xs leading-relaxed text-danger-text"
        >
          {decisionError}
        </p>
      )}

      {endpoints === null ? (
        <p
          data-testid="assistant-composer-unavailable"
          role="status"
          className="border-t-hairline border-border-subtle pt-5 text-2xs leading-relaxed text-text-secondary"
        >
          {COMPOSER_UNAVAILABLE_TEXT}
        </p>
      ) : (
        <form
          onSubmit={(event) => void handleSend(event)}
          className="flex flex-col gap-3 border-t-hairline border-border-subtle pt-5"
        >
          <label htmlFor={PROMPT_TESTID} className="text-2xs text-text-secondary">
            {PROMPT_LABEL}
          </label>
          <Input
            id={PROMPT_TESTID}
            data-testid={PROMPT_TESTID}
            value={prompt}
            disabled={running}
            placeholder="Which of these should I chase first?"
            onChange={(event) => setPrompt(event.target.value)}
          />
          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" size="sm" data-testid={SEND_TESTID} disabled={!canSend}>
              Send
            </Button>
            {running ? (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                data-testid={STOP_TESTID}
                disabled={
                  !turnRegistered ||
                  stream.sessionId === undefined ||
                  cancel.cancelState === "requesting"
                }
                onClick={() => void handleStop()}
              >
                Stop
              </Button>
            ) : null}
            {conversation.length > 0 ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                data-testid={NEW_CONVERSATION_TESTID}
                disabled={running}
                onClick={handleNewConversation}
              >
                New conversation
              </Button>
            ) : null}
          </div>
        </form>
      )}
    </div>
  );
}
