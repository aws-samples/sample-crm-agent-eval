// SPDX-License-Identifier: MIT
/**
 * The assistant panel region: one element at every width, docked beside the content at `lg` and
 * above and a dismissable overlay below it. Owns visibility, focus management, Escape, and the
 * keyboard shortcut; `AppShell` renders it as a sibling of the router `<Outlet />`.
 *
 * Visibility is carried by the `hidden` attribute and the `[&[hidden]]:hidden` class together.
 * Tailwind's preflight `[hidden]` rule is wrapped in `:where()` (zero specificity), so this element's
 * `flex` class would outrank the attribute alone; the guard emits `[hidden]{display:none}` at
 * class+attribute specificity. The bare `[hidden]:hidden` form emits nothing under Tailwind 3.
 */

import { useCallback, useEffect, useRef } from "react";
import type { JSX, KeyboardEvent, ReactNode } from "react";

import { usePageContext } from "../../hooks/usePageContext";
import type { PageContextKind } from "../../types/generated/pageContext";
import { Button } from "../ui/button";
import { cn, focusRing } from "../ui/utils";
import { ConversationCostHeader } from "./ConversationCostHeader";
import { useAssistantPanel } from "./PanelProvider";

/**
 * The example prompts offered in the empty state, keyed off the `PageContext` kind. Static strings
 * that name the shape of the view, never a record's own values.
 */
const PROMPT_CHIPS: Readonly<Record<PageContextKind, readonly string[]>> = {
  entity: ["Summarise this record", "What changed recently?", "Any open risks here?"],
  list: ["Summarise this list", "Which of these need attention?", "Group these by owner"],
  board: ["Summarise this pipeline", "What looks likely to slip?", "Where is the value concentrated?"],
  opaque: ["What can you help with on this page?", "What can you see here?"],
};

/** The static example prompts for a `PageContext` kind. */
export function promptChipsForKind(kind: PageContextKind): readonly string[] {
  return PROMPT_CHIPS[kind];
}

/** The panel's element id, referenced by the chrome's toggle via `aria-controls`. */
export const PANEL_ELEMENT_ID = "assistant-panel";

/** Toggles the panel from anywhere: `Cmd/Ctrl+Shift+A`. */
export function isPanelShortcut(event: Pick<KeyboardEvent, "key" | "shiftKey" | "metaKey" | "ctrlKey">): boolean {
  return (event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === "a";
}

/**
 * Matches a Tailwind `display` utility, optionally breakpoint-prefixed. Wherever one appears, the
 * `hidden` guard below must appear too.
 */
export const DISPLAY_UTILITY_PATTERN =
  /(?:^|\s)(?:\w+:)*(?:block|inline-block|inline|flex|inline-flex|table|inline-table|grid|inline-grid|contents|flow-root|list-item|table-(?:row|cell|caption|column|header-group|row-group|footer-group|column-group)|hidden)(?=\s|$)/;

/** Paired with every `display` utility above; see the module docstring. */
export const PANEL_HIDDEN_GUARD_CLASS = "[&[hidden]]:hidden";

/**
 * The element focus should return to, or `null` when nothing outside the panel held it.
 *
 * Excludes `<body>`/`<html>` (what `document.activeElement` is when nothing has focus) and any
 * control inside the panel, which stays connected but unfocusable once the panel hides.
 */
function elementWithFocus(panel: HTMLElement | null): HTMLElement | null {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement)) return null;
  if (active === document.body || active === document.documentElement) return null;
  if (panel !== null && panel.contains(active)) return null;
  return active;
}

/**
 * Whether focus can actually land on `element` now that the panel is closed: it is connected and not
 * inside the hidden panel. Containment is used rather than `offsetParent`, which jsdom leaves `null`.
 */
function canRestoreFocusTo(element: HTMLElement, panel: HTMLElement | null): boolean {
  if (!element.isConnected) return false;
  return panel === null || !panel.contains(element);
}

/**
 * The control that opens the panel, found through its `aria-controls` pointer. Used as the focus
 * fallback when nothing held focus.
 */
function panelToggleElement(): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[aria-controls="${PANEL_ELEMENT_ID}"]`);
}

export function Panel({ children }: { children?: ReactNode }): JSX.Element {
  const { open, closePanel, togglePanel, turns, conversation, fillComposer } = useAssistantPanel();
  // Called after `useAssistantPanel` so a missing provider throws its own error first.
  const pageContext = usePageContext();
  const panelRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  /*
   * Distinguishes "the panel closed" from "the panel has always been closed", so the fallback does
   * not steal focus on first mount.
   */
  const wasOpenRef = useRef(false);

  /*
   * Focus moves into the panel on open and back to whatever had it on close. The element is captured
   * on the opening transition. When nothing held focus, focus goes to the toggle.
   */
  useEffect(() => {
    const panel = panelRef.current;
    if (open) {
      wasOpenRef.current = true;
      returnFocusRef.current = elementWithFocus(panel);
      closeRef.current?.focus();
      return;
    }
    if (!wasOpenRef.current) return;
    wasOpenRef.current = false;
    const captured = returnFocusRef.current;
    returnFocusRef.current = null;
    const target =
      captured !== null && canRestoreFocusTo(captured, panel) ? captured : panelToggleElement();
    target?.focus();
  }, [open]);

  useEffect(() => {
    function onKeyDown(event: globalThis.KeyboardEvent): void {
      if (isPanelShortcut(event)) {
        event.preventDefault();
        togglePanel();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [togglePanel]);

  /*
   * Escape is handled on the panel subtree, not on the document, so Escape presses the rest of the
   * app owns (e.g. `OpportunityCard`'s keyboard pick-up) do not close the panel.
   */
  const onEscape = useCallback(
    (event: KeyboardEvent<HTMLElement>) => {
      if (event.key !== "Escape" || !open) return;
      event.preventDefault();
      closePanel();
    },
    [closePanel, open],
  );

  return (
    <aside
      ref={panelRef}
      id={PANEL_ELEMENT_ID}
      data-testid="assistant-panel"
      aria-label="Assistant"
      hidden={!open}
      onKeyDown={onEscape}
      className={cn(
        PANEL_HIDDEN_GUARD_CLASS,
        "flex w-full flex-col gap-6 border-l-hairline border-border-default bg-surface-raised p-6",
        // `max-w-[var(…)]`, not `max-w-panel`: the `maxWidth` scale has no `panel` key.
        "fixed inset-y-0 right-0 z-panel max-w-[var(--layout-panel-width)] shadow-overlay",
        "lg:static lg:z-base lg:w-panel lg:shadow-flush",
      )}
    >
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-baseline gap-3">
          <h2 className="text-sm font-semibold text-text-primary">Assistant</h2>
          <ConversationCostHeader conversation={conversation} />
        </div>
        <Button
          ref={closeRef}
          variant="ghost"
          size="sm"
          data-testid="panel-close"
          onClick={closePanel}
        >
          Close
        </Button>
      </div>

      {/*
        The conversation renders through the `children` seam: `AssistantConversation` owns the
        stream, the composer, and the proposal affordances. This container renders the empty state.
      */}
      {turns.length === 0 ? (
        <div
          data-testid="panel-empty"
          className="rounded-surface bg-empty-surface p-5 text-xs text-empty-text"
        >
          <p>Ask about whatever you have open. The assistant can already see this page.</p>
          {/* Example prompts for what the user is looking at. Static per page kind — no row
              data. Clicking one *fills and focuses* the composer through the provider's one-shot
              `draft` channel (`AssistantConversation` adopts it and calls `clearDraft`); it never
              sends. The kind is exposed via `data-page-kind`. */}
          <ul
            data-testid="prompt-chips"
            data-page-kind={pageContext.kind}
            className="mt-4 flex flex-wrap gap-2"
          >
            {promptChipsForKind(pageContext.kind).map((chip) => (
              <li key={chip}>
                <button
                  type="button"
                  data-testid="prompt-chip"
                  onClick={() => fillComposer(chip)}
                  className={cn(
                    focusRing,
                    "rounded-pill border border-hairline border-border-default bg-surface-inset px-3 py-1 text-2xs text-text-secondary hover:border-border-strong hover:text-text-primary",
                  )}
                >
                  {chip}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {children}
    </aside>
  );
}
