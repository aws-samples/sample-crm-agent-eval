// SPDX-License-Identifier: MIT
/**
 * Assistant panel state: open/closed (persisted across reloads), the conversation, and the composer
 * draft. Rendered above the router `<Outlet />`, so the conversation survives navigation.
 *
 * The conversation is stored per turn ({@link ConversationTurn}): `beginTurn` opens one,
 * `appendTurn` adds each arriving frame, and `finalizeTurn` records the outcome. `turns` is the
 * derived flat frame list across all turns.
 *
 * {@link AssistantPanelContextValue.draft} is a one-shot signal from a prompt chip to the composer:
 * `fillComposer` sets it, the composer adopts it and calls `clearDraft`.
 *
 * Persistence is best-effort: an unavailable `localStorage` reads as closed.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { JSX, ReactNode } from "react";

import type { AgentEventFrame } from "../../types/generated/agentEvents";
import type { TurnTerminalState } from "./TurnStatePill";

/** One conversation entry: a frame, as the SSE stream delivered it. Re-exported by `AppShell`. */
export type AssistantTurn = AgentEventFrame;

/**
 * One turn of the conversation: the prompt that started it, the frames that answered it, and how it
 * ended (`null` while still streaming).
 */
export interface ConversationTurn {
  readonly prompt: string;
  readonly frames: readonly AgentEventFrame[];
  readonly terminalState: TurnTerminalState | null;
}

/** `localStorage` key for the panel's open state. */
export const PANEL_STORAGE_KEY = "crm.assistantPanel.open";

export interface AssistantPanelContextValue {
  readonly open: boolean;
  readonly openPanel: () => void;
  readonly closePanel: () => void;
  readonly togglePanel: () => void;
  /** The conversation, per turn. Survives route changes because it lives above the outlet. */
  readonly conversation: readonly ConversationTurn[];
  /** Every turn's frames, concatenated — the view the empty-state gate reads. */
  readonly turns: readonly AssistantTurn[];
  /** Open a new turn for a prompt the user just sent. */
  readonly beginTurn: (prompt: string) => void;
  /** Append a frame to the current turn (opening an anonymous one if none is open). */
  readonly appendTurn: (turn: AssistantTurn) => void;
  /** Record how the current turn ended, so its bubble can show a terminal-state pill. */
  readonly finalizeTurn: (state: TurnTerminalState) => void;
  /** Discard the whole conversation — the client half of starting a fresh session. */
  readonly resetConversation: () => void;
  /** A prompt-chip fill waiting to be applied to the composer, or `null`. */
  readonly draft: string | null;
  /** Ask the composer to adopt `text` without sending it. */
  readonly fillComposer: (text: string) => void;
  /** Clear the pending draft once the composer has adopted it. */
  readonly clearDraft: () => void;
}

const AssistantPanelContext = createContext<AssistantPanelContextValue | null>(null);

/**
/** Reads the persisted open state. Returns `false` for an absent, unparseable, or unavailable store. */
export function readStoredPanelOpen(): boolean {
  try {
    return globalThis.localStorage?.getItem(PANEL_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

/** Persists the open state, best-effort. */
export function storePanelOpen(open: boolean): void {
  try {
    globalThis.localStorage?.setItem(PANEL_STORAGE_KEY, String(open));
  } catch {
    // Storage unavailable or denied; the panel still works for this session.
  }
}

/** Append `frame` to the last turn, opening an anonymous turn first when the conversation is empty. */
function withAppendedFrame(
  conversation: readonly ConversationTurn[],
  frame: AgentEventFrame,
): readonly ConversationTurn[] {
  if (conversation.length === 0) {
    return [{ prompt: "", frames: [frame], terminalState: null }];
  }
  const last = conversation[conversation.length - 1];
  const updated: ConversationTurn = { ...last, frames: [...last.frames, frame] };
  return [...conversation.slice(0, -1), updated];
}

export function AssistantPanelProvider({ children }: { children: ReactNode }): JSX.Element {
  const [open, setOpen] = useState<boolean>(readStoredPanelOpen);
  const [conversation, setConversation] = useState<readonly ConversationTurn[]>([]);
  const [draft, setDraft] = useState<string | null>(null);

  // The single write path for persistence.
  useEffect(() => {
    storePanelOpen(open);
  }, [open]);

  const openPanel = useCallback(() => setOpen(true), []);
  const closePanel = useCallback(() => setOpen(false), []);
  const togglePanel = useCallback(() => setOpen((previous) => !previous), []);

  const beginTurn = useCallback(
    (prompt: string) =>
      setConversation((previous) => [...previous, { prompt, frames: [], terminalState: null }]),
    [],
  );
  const appendTurn = useCallback(
    (turn: AssistantTurn) => setConversation((previous) => withAppendedFrame(previous, turn)),
    [],
  );
  const finalizeTurn = useCallback(
    (state: TurnTerminalState) =>
      setConversation((previous) => {
        if (previous.length === 0) return previous;
        const last = previous[previous.length - 1];
        if (last.terminalState !== null) return previous;
        return [...previous.slice(0, -1), { ...last, terminalState: state }];
      }),
    [],
  );

  const resetConversation = useCallback(() => setConversation([]), []);

  const fillComposer = useCallback((text: string) => setDraft(text), []);
  const clearDraft = useCallback(() => setDraft(null), []);

  // Flattened once per conversation change, so `turns` and `conversation` cannot drift.
  const turns = useMemo(() => conversation.flatMap((turn) => turn.frames), [conversation]);

  const value = useMemo(
    () => ({
      open,
      openPanel,
      closePanel,
      togglePanel,
      conversation,
      turns,
      beginTurn,
      appendTurn,
      finalizeTurn,
      resetConversation,
      draft,
      fillComposer,
      clearDraft,
    }),
    [
      open,
      openPanel,
      closePanel,
      togglePanel,
      conversation,
      turns,
      beginTurn,
      appendTurn,
      finalizeTurn,
      resetConversation,
      draft,
      fillComposer,
      clearDraft,
    ],
  );

  return <AssistantPanelContext value={value}>{children}</AssistantPanelContext>;
}

export function useAssistantPanel(): AssistantPanelContextValue {
  const value = useContext(AssistantPanelContext);
  if (value === null) {
    throw new Error("useAssistantPanel must be used inside an AssistantPanelProvider");
  }
  return value;
}
