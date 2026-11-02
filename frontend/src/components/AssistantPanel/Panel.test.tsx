// SPDX-License-Identifier: MIT
/**
 * Panel container tests: focus management, Escape, the keyboard shortcut, persistence, and the
 * visibility class contract.
 *
 * Visibility is asserted as a class contract rather than computed style: `vite.config.ts` sets
 * `css: false`, so jsdom loads no stylesheets and reports `hidden` + `flex` as not visible while a
 * real browser shows it. The guard's CSS is compiled through the project's Tailwind config below.
 */

import { StrictMode } from "react";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { compileTailwindUtilities } from "../../test/compileTailwind";

import { EventTrace } from "./EventTrace";
import { MarkdownAnswer } from "./MarkdownAnswer";
import {
  AssistantPanelProvider,
  PANEL_STORAGE_KEY,
  readStoredPanelOpen,
  useAssistantPanel,
  type AssistantTurn,
} from "./PanelProvider";
import {
  DISPLAY_UTILITY_PATTERN,
  PANEL_ELEMENT_ID,
  PANEL_HIDDEN_GUARD_CLASS,
  Panel,
  isPanelShortcut,
  promptChipsForKind,
} from "./Panel";
import { TurnStatePill } from "./TurnStatePill";
import { CONVERSATION_COST_TESTID } from "./ConversationCostHeader";
import { errorHeadline } from "./EventTrace";
import { PAGE_CONTEXT_KINDS } from "../../types/generated/pageContext";
import type { AgentEventFrame } from "../../types/generated/agentEvents";

/** One completed turn, as an `AgentEventFrame` in the shape the SSE stream delivers. */
function completedTurn(text: string): AssistantTurn {
  return {
    kind: "completed",
    text,
    tool_name: null,
    tool_input: null,
    tool_output: null,
    truncated: false,
    proposal_id: null,
    proposal_summary: null,
    input_tokens: null,
    output_tokens: null,
    cache_read_tokens: null,
    cache_write_tokens: null,
    error_kind: null,
    error_message: null,
    partial_work: null,
    event_id: null,
    partial: false,
    cost_lines: null,
    cost_usd: null,
    cost_withheld: null,
    resolved_tiers: null,
  };
}

/**
 * The conversation reduced to its transcript: the panel's state rendered through `EventTrace` over
 * `turns`, without the real component's auth, router, and runtime-config dependencies.
 */
function Conversation(): React.JSX.Element {
  const { turns } = useAssistantPanel();
  // A completed turn's text is the answer, rendered as its own bubble rather than a trace row.
  const answer = [...turns].reverse().find((frame) => frame.kind === "completed")?.text ?? null;
  return (
    <>
      <EventTrace events={turns} />
      {answer === null ? null : <MarkdownAnswer text={answer} />}
    </>
  );
}

/** Chrome standing in for `AppShell`: a toggle outside the panel, plus content that can hold focus. */
function Harness(): React.JSX.Element {
  const { togglePanel, appendTurn } = useAssistantPanel();
  return (
    <div>
      {/* `aria-controls` mirrors `AppShell`'s real toggle; the close-focus fallback reads it. */}
      <button
        type="button"
        data-testid="panel-toggle"
        aria-controls={PANEL_ELEMENT_ID}
        onClick={togglePanel}
      >
        Assistant
      </button>
      <button
        type="button"
        data-testid="outside-control"
        onClick={() => appendTurn(completedTurn("a turn"))}
      >
        Add a turn
      </button>
      <Panel>
        <Conversation />
      </Panel>
    </div>
  );
}

function renderPanel(initialPath = "/") {
  // Wrapped in a router because `Panel` reads the route through `usePageContext`.
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <AssistantPanelProvider>
        <Harness />
      </AssistantPanelProvider>
    </MemoryRouter>,
  );
}

/**
 * Installs an in-memory `localStorage`; jsdom provides none in this project's configuration. Mirrors
 * `AppShell.test.tsx`'s helper.
 */
function installStorage(initial: Record<string, string> = {}): Map<string, string> {
  const store = new Map(Object.entries(initial));
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
  });
  return store;
}

beforeEach(() => {
  installStorage();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Panel visibility class contract", () => {
  it("pairs every display utility with the hidden guard, in the markup", () => {
    renderPanel();
    const classes = screen.getByTestId("assistant-panel").className.split(/\s+/);

    // Non-vacuity: the panel carries a display utility.
    expect(screen.getByTestId("assistant-panel").className).toMatch(DISPLAY_UTILITY_PATTERN);
    expect(classes).toContain(PANEL_HIDDEN_GUARD_CLASS);
  });

  /*
   * A class-list check cannot distinguish `[hidden]:hidden`, which never matches the attribute, from
   * `[&[hidden]]:hidden`. So the guard is compiled through the project's own Tailwind config
   * and the emitted rule is asserted to set `display: none` when the attribute is present.
   */
  it("compiles to a real rule that beats an unconditional display utility", async () => {
    const markup = `<aside class="${PANEL_HIDDEN_GUARD_CLASS} flex lg:static"></aside>`;
    const css = await compileTailwindUtilities(markup);

    // The emitted selector carries both a class and the [hidden] attribute: specificity (0,2,0)
    // outranks `.flex` at (0,1,0); preflight's own `[hidden]` rule is a separate `!important` default.
    const flat = css.replace(/\s+/g, "");
    const guardRule = flat
      .split("}")
      .find((chunk) => chunk.includes("[hidden]") && chunk.includes("display:none"));

    expect(guardRule, `no rule emitted for ${PANEL_HIDDEN_GUARD_CLASS}`).toBeDefined();
    // A class selector escaping the arbitrary variant, not a bare attribute selector.
    expect(guardRule).toMatch(/^\.\\\[/);
    expect(guardRule).toContain("[hidden]");

    // Non-vacuity: the compiler in this test emits utilities at all.
    expect(flat).toContain("display:flex");
  });

  it("would fail for the inert `[hidden]:hidden` form, which never targets the attribute", async () => {
    // The bare form compiles to `:is(hidden)`, a type selector no element matches; if a future
    // Tailwind makes it target the attribute, this goes red.
    const markup = `<aside class="[hidden]:hidden flex"></aside>`;
    const css = await compileTailwindUtilities(markup);

    const flat = css.replace(/\s+/g, "");
    expect(flat).toContain("display:flex"); // the compiler ran
    const bareSelectors = flat
      .split("}")
      .map((chunk) => chunk.split("{")[0])
      .filter((selector) => selector.startsWith(".\\[hidden\\]\\:hidden"));
    for (const selector of bareSelectors) {
      expect(selector.slice(".\\[hidden\\]\\:hidden".length)).not.toContain("[hidden]");
    }
  });


  /*
   * Every class this element carries is compiled and required to emit a rule, catching classes such
   * as `max-w-panel` that emit nothing.
   */
  it("emits a rule for every class the panel carries — no silently inert utilities", async () => {
    const { container } = renderPanel();
    const classes = (container.querySelector('[data-testid="assistant-panel"]') as HTMLElement).className
      .split(/\s+/)
      .filter(Boolean);

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

    // Non-vacuity, both directions: a known-good class adds a rule, a known-inert class does not.
    expect((await compile("flex")).replace(/\s+/g, "")).toContain("display:flex");
    expect(await compile("max-w-panel")).toBe(baseline);
  });

  it("keeps the hidden attribute as the authoritative open/closed signal", async () => {
    const user = userEvent.setup();
    renderPanel();
    expect(screen.getByTestId("assistant-panel")).toHaveAttribute("hidden");

    await user.click(screen.getByTestId("panel-toggle"));
    expect(screen.getByTestId("assistant-panel")).not.toHaveAttribute("hidden");
  });
});

describe("Panel focus management", () => {
  it("moves focus into the panel on open", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByTestId("panel-toggle"));

    expect(screen.getByTestId("panel-close")).toHaveFocus();
    expect(
      within(screen.getByTestId("assistant-panel")).getByTestId("panel-close"),
    ).toBeInTheDocument();
  });

  it("returns focus to the control that opened it on close", async () => {
    const user = userEvent.setup();
    renderPanel();
    const toggle = screen.getByTestId("panel-toggle");

    await user.click(toggle);
    expect(screen.getByTestId("panel-close")).toHaveFocus();

    await user.click(screen.getByTestId("panel-close"));
    expect(toggle).toHaveFocus();
  });

  it("returns focus to whatever held it, not unconditionally to the toggle", async () => {
    const user = userEvent.setup();
    renderPanel();
    const outside = screen.getByTestId("outside-control");

    // Open via the keyboard shortcut while a different control holds focus.
    outside.focus();
    await user.keyboard("{Meta>}{Shift>}a{/Shift}{/Meta}");
    expect(screen.getByTestId("panel-close")).toHaveFocus();

    await user.click(screen.getByTestId("panel-close"));
    expect(outside).toHaveFocus();
  });

  /*
   * Opened by the keyboard shortcut while nothing holds focus, so `activeElement` is `<body>`.
   */
  it("falls back to the toggle when nothing held focus, rather than leaving focus on the body", async () => {
    const user = userEvent.setup();
    renderPanel();
    const toggle = screen.getByTestId("panel-toggle");

    // Nothing has focus in a fresh document; the shortcut is document-level.
    expect(document.activeElement).toBe(document.body);
    await user.keyboard("{Control>}{Shift>}a{/Shift}{/Control}");
    expect(screen.getByTestId("panel-close")).toHaveFocus();

    await user.keyboard("{Escape}");

    expect(screen.getByTestId("assistant-panel")).toHaveAttribute("hidden");
    expect(toggle).toHaveFocus();
    expect(document.activeElement).not.toBe(document.body);
  });

  it("leaves focus alone on first mount, so a page load does not jump to the toggle", () => {
    // The fallback fires only on a close, never on first mount.
    renderPanel();

    expect(screen.getByTestId("assistant-panel")).toHaveAttribute("hidden");
    expect(document.activeElement).toBe(document.body);
  });

  it("finds the toggle through the aria-controls pointer it already declares", async () => {
    // The fallback reads the toggle's `aria-controls` pointer.
    const user = userEvent.setup();
    renderPanel();
    const toggle = screen.getByTestId("panel-toggle");
    expect(document.querySelector(`[aria-controls="${PANEL_ELEMENT_ID}"]`)).toBe(toggle);

    await user.keyboard("{Control>}{Shift>}a{/Shift}{/Control}");
    await user.keyboard("{Escape}");
    expect(toggle).toHaveFocus();
  });

  it("closes without throwing when no toggle declares aria-controls", async () => {
    // Degrades to "focus unchanged" rather than to an exception.
    const user = userEvent.setup();
    renderPanel();
    screen.getByTestId("panel-toggle").removeAttribute("aria-controls");

    await user.keyboard("{Control>}{Shift>}a{/Shift}{/Control}");
    await user.keyboard("{Escape}");

    expect(screen.getByTestId("assistant-panel")).toHaveAttribute("hidden");
  });

  it("does not throw when the element that had focus is gone by the time the panel closes", async () => {
    const user = userEvent.setup();
    function Disappearing(): React.JSX.Element {
      const { open, togglePanel } = useAssistantPanel();
      return (
        <div>
          {!open ? (
            <button type="button" data-testid="ephemeral" onClick={togglePanel}>
              Open
            </button>
          ) : null}
          <Panel />
        </div>
      );
    }
    render(
      <MemoryRouter>
        <AssistantPanelProvider>
          <Disappearing />
        </AssistantPanelProvider>
      </MemoryRouter>,
    );

    await user.click(screen.getByTestId("ephemeral"));
    await user.click(screen.getByTestId("panel-close"));

    expect(screen.getByTestId("assistant-panel")).toHaveAttribute("hidden");
  });

  /*
   * The panel is restored open from storage, and `StrictMode` double-invokes the mount effect, so the
   * second invocation runs with focus already on `panel-close`. The assertion is on the capture:
   * focus never returns to a control inside the panel.
   */
  it("does not capture a control inside the panel when restored open under StrictMode", async () => {
    const user = userEvent.setup();
    installStorage({ [PANEL_STORAGE_KEY]: "true" });
    render(
      <StrictMode>
        <MemoryRouter>
          <AssistantPanelProvider>
            <Harness />
          </AssistantPanelProvider>
        </MemoryRouter>
      </StrictMode>,
    );
    const panel = screen.getByTestId("assistant-panel");
    expect(panel).not.toHaveAttribute("hidden");
    expect(screen.getByTestId("panel-close")).toHaveFocus();

    await user.keyboard("{Escape}");

    expect(panel).toHaveAttribute("hidden");
    expect(screen.getByTestId("panel-toggle")).toHaveFocus();
    expect(panel.contains(document.activeElement)).toBe(false);
    expect(document.activeElement).not.toBe(document.body);
  });

  /*
   * The restore side: a control moved into the panel between capture and close stays connected but
   * is inside the hidden panel, so focus is not restored to it.
   */
  it("falls back to the toggle when the captured control ends up inside the panel", async () => {
    const user = userEvent.setup();
    renderPanel();
    const outside = screen.getByTestId("outside-control");
    const panel = screen.getByTestId("assistant-panel");

    outside.focus();
    await user.keyboard("{Control>}{Shift>}a{/Shift}{/Control}");
    expect(screen.getByTestId("panel-close")).toHaveFocus();

    // Still connected, yet refused.
    panel.append(outside);
    expect(outside.isConnected).toBe(true);

    await user.keyboard("{Escape}");

    expect(panel).toHaveAttribute("hidden");
    expect(screen.getByTestId("panel-toggle")).toHaveFocus();
    expect(outside).not.toHaveFocus();
  });
});

describe("Panel Escape handling", () => {
  it("closes on Escape without losing conversation state", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByTestId("outside-control")); // append a turn
    await user.click(screen.getByTestId("panel-toggle"));
    expect(screen.getByText("a turn")).toBeInTheDocument();

    await user.keyboard("{Escape}");

    expect(screen.getByTestId("assistant-panel")).toHaveAttribute("hidden");
    // Escape closes the panel; it does not discard the turn.
    expect(screen.getByText("a turn")).toBeInTheDocument();
    expect(screen.queryByTestId("panel-empty")).not.toBeInTheDocument();
  });

  it("ignores Escape pressed outside the panel, so it cannot hijack the rest of the app", async () => {
    // `OpportunityCard` uses Escape to cancel a keyboard pick-up; that keypress leaves the panel open.
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByTestId("panel-toggle"));
    expect(screen.getByTestId("assistant-panel")).not.toHaveAttribute("hidden");

    screen.getByTestId("outside-control").focus();
    await user.keyboard("{Escape}");

    expect(screen.getByTestId("assistant-panel")).not.toHaveAttribute("hidden");
  });
});

describe("Panel keyboard shortcut", () => {
  it("toggles open and closed", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.keyboard("{Control>}{Shift>}a{/Shift}{/Control}");
    expect(screen.getByTestId("assistant-panel")).not.toHaveAttribute("hidden");

    await user.keyboard("{Control>}{Shift>}a{/Shift}{/Control}");
    expect(screen.getByTestId("assistant-panel")).toHaveAttribute("hidden");
  });

  it("requires the full chord, so it cannot steal select-all or a bare keypress", () => {
    const chord = { key: "a", shiftKey: true, metaKey: true, ctrlKey: false };
    expect(isPanelShortcut(chord)).toBe(true);
    expect(isPanelShortcut({ ...chord, key: "A" })).toBe(true);

    expect(isPanelShortcut({ ...chord, shiftKey: false })).toBe(false); // Cmd+A is select-all
    expect(isPanelShortcut({ ...chord, metaKey: false })).toBe(false); // Shift+A is a capital A
    expect(isPanelShortcut({ ...chord, key: "k" })).toBe(false);
  });
});

describe("Panel open/closed persistence", () => {
  it("persists the open state and restores it on a fresh mount", async () => {
    const user = userEvent.setup();
    const store = installStorage();
    renderPanel();

    await user.click(screen.getByTestId("panel-toggle"));
    expect(store.get(PANEL_STORAGE_KEY)).toBe("true");

    cleanup();
    renderPanel();
    expect(screen.getByTestId("assistant-panel")).not.toHaveAttribute("hidden");
  });

  it("persists the closed state too, so an explicit close is not forgotten", async () => {
    const user = userEvent.setup();
    const store = installStorage({ [PANEL_STORAGE_KEY]: "true" });
    renderPanel();
    expect(screen.getByTestId("assistant-panel")).not.toHaveAttribute("hidden");

    await user.click(screen.getByTestId("panel-close"));
    expect(store.get(PANEL_STORAGE_KEY)).toBe("false");

    cleanup();
    renderPanel();
    expect(screen.getByTestId("assistant-panel")).toHaveAttribute("hidden");
  });

  it("treats an unparseable stored value as closed rather than trusting it", () => {
    installStorage({ [PANEL_STORAGE_KEY]: "yes-please" });
    expect(readStoredPanelOpen()).toBe(false);
    renderPanel();
    expect(screen.getByTestId("assistant-panel")).toHaveAttribute("hidden");
  });

  it("still renders when storage is denied, rather than failing to mount", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("SecurityError: storage is denied");
      },
      setItem: () => {
        throw new Error("SecurityError: storage is denied");
      },
    });

    renderPanel();
    expect(screen.getByTestId("assistant-panel")).toHaveAttribute("hidden");

    // The panel still opens when persistence is unavailable.
    await user.click(screen.getByTestId("panel-toggle"));
    expect(screen.getByTestId("assistant-panel")).not.toHaveAttribute("hidden");
  });
});

describe("useAssistantPanel", () => {
  it("throws outside a provider rather than silently returning a default", () => {
    // There is no default context value.
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Panel />)).toThrow(/must be used inside an AssistantPanelProvider/);
  });
});

/** An error terminal frame with the given `error_kind`, in the shape the SSE stream delivers. */
function errorTurn(errorKind: AgentEventFrame["error_kind"]): AgentEventFrame {
  return {
    kind: "error",
    text: null,
    tool_name: null,
    tool_input: null,
    tool_output: null,
    truncated: false,
    proposal_id: null,
    proposal_summary: null,
    input_tokens: null,
    output_tokens: null,
    cache_read_tokens: null,
    cache_write_tokens: null,
    error_kind: errorKind,
    error_message: "raw detail that the pill must not invent wording from",
    partial_work: null,
    event_id: null,
    partial: false,
    cost_lines: null,
    cost_usd: null,
    cost_withheld: null,
    resolved_tiers: null,
  };
}

describe("page-aware prompt chips", () => {
  it("offers static example prompts for every PageContext kind, and covers the whole set", () => {
    // Each of the four kinds maps to a non-empty set of static suggestions, and the map is
    // exhaustive over the generated kinds.
    for (const kind of PAGE_CONTEXT_KINDS) {
      const chips = promptChipsForKind(kind);
      expect(chips.length, `no chips for kind ${kind}`).toBeGreaterThan(0);
      for (const chip of chips) {
        expect(typeof chip).toBe("string");
        expect(chip.length).toBeGreaterThan(0);
      }
    }
    // Each kind's suggestions are distinct.
    const entity = promptChipsForKind("entity").join("|");
    const board = promptChipsForKind("board").join("|");
    expect(entity).not.toBe(board);
  });

  it("carries no row data: chips name the view's shape, never an entity or a value", () => {
    // A chip is a static string keyed off the kind: no digits, no id-shaped tokens.
    for (const kind of PAGE_CONTEXT_KINDS) {
      for (const chip of promptChipsForKind(kind)) {
        expect(chip, `chip "${chip}" looks like it carries data`).not.toMatch(/\d|acc-|opp-|#/);
      }
    }
  });

  it("renders the entity kind's chips on an entity route", async () => {
    renderPanel("/accounts/acc-1");
    const chips = await screen.findByTestId("prompt-chips");
    expect(chips).toHaveAttribute("data-page-kind", "entity");
    for (const chip of promptChipsForKind("entity")) {
      expect(within(chips).getByText(chip)).toBeInTheDocument();
    }
  });

  it("renders the opaque kind's chips on a route it cannot describe", async () => {
    renderPanel("/");
    const chips = await screen.findByTestId("prompt-chips");
    expect(chips).toHaveAttribute("data-page-kind", "opaque");
    for (const chip of promptChipsForKind("opaque")) {
      expect(within(chips).getByText(chip)).toBeInTheDocument();
    }
  });

  it("hides the chips once a turn exists (they belong to the empty state only)", async () => {
    const user = userEvent.setup();
    renderPanel("/accounts/acc-1");
    expect(screen.getByTestId("prompt-chips")).toBeInTheDocument();
    await user.click(screen.getByTestId("outside-control")); // appends a turn
    expect(screen.queryByTestId("prompt-chips")).not.toBeInTheDocument();
  });

  it("fills the composer with the chip's static string on click, and does not send", async () => {
    // A probe reads the provider's one-shot `draft`; this stand-in does not clear it, so the fill
    // stays observable. The panel is opened first, so the chip is an accessible button.
    function DraftProbe(): React.JSX.Element {
      const { draft } = useAssistantPanel();
      return <span data-testid="draft-probe">{draft ?? ""}</span>;
    }
    function Opener(): React.JSX.Element {
      const { togglePanel } = useAssistantPanel();
      return (
        <button type="button" data-testid="open-panel" onClick={togglePanel}>
          open
        </button>
      );
    }
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/accounts/acc-1"]}>
        <AssistantPanelProvider>
          <Opener />
          <Panel />
          <DraftProbe />
        </AssistantPanelProvider>
      </MemoryRouter>,
    );
    await user.click(screen.getByTestId("open-panel"));

    const [firstChip] = promptChipsForKind("entity");
    const chipButton = screen.getByRole("button", { name: firstChip });
    expect(chipButton).toHaveAttribute("data-testid", "prompt-chip");

    await user.click(chipButton);

    // Filled: the draft carries the chip's exact static string.
    expect(screen.getByTestId("draft-probe")).toHaveTextContent(firstChip);
    // Not sent: no turn was appended, so the empty state (and its chips) is still shown.
    expect(screen.getByTestId("panel-empty")).toBeInTheDocument();
    expect(screen.getByTestId("prompt-chips")).toBeInTheDocument();
  });
});

describe("TurnStatePill", () => {
  it("labels a completed turn", () => {
    render(<TurnStatePill state={{ status: "completed" }} />);
    const pill = screen.getByTestId("turn-state-pill");
    expect(pill).toHaveAttribute("data-status", "completed");
    expect(pill).toHaveTextContent("Completed");
  });

  it("labels a cancelled turn", () => {
    render(<TurnStatePill state={{ status: "cancelled" }} />);
    const pill = screen.getByTestId("turn-state-pill");
    expect(pill).toHaveAttribute("data-status", "cancelled");
    expect(pill).toHaveTextContent("Cancelled");
  });

  it.each(["budget_exhausted", "tool_failure", "model_failure", "invalid_request"] as const)(
    "keeps the ErrorKind wording for %s, single-sourced from errorHeadline",
    (kind) => {
      const event = errorTurn(kind);
      render(<TurnStatePill state={{ status: "error", event }} />);
      const pill = screen.getByTestId("turn-state-pill");
      expect(pill).toHaveAttribute("data-status", "error");
      expect(pill).toHaveAttribute("data-error-kind", kind);
      // The same wording as the trace row's `errorHeadline`.
      expect(pill).toHaveTextContent(errorHeadline(event));
    },
  );

  it("keeps the invalid_request wording", () => {
    const event = errorTurn("invalid_request");
    render(<TurnStatePill state={{ status: "error", event }} />);
    expect(screen.getByTestId("turn-state-pill")).toHaveTextContent(
      "That request wasn't valid, so the turn didn't start.",
    );
  });

  it("never surfaces the raw error_message as its label", () => {
    const event = errorTurn("model_failure");
    render(<TurnStatePill state={{ status: "error", event }} />);
    expect(screen.getByTestId("turn-state-pill")).not.toHaveTextContent(
      "raw detail that the pill must not invent wording from",
    );
  });
});

describe("the conversation cost sits beside the panel heading", () => {
  function PricedTurns(): React.JSX.Element {
    const { appendTurn } = useAssistantPanel();
    return (
      <button
        type="button"
        data-testid="add-priced-turn"
        onClick={() =>
          appendTurn({ ...completedTurn("priced"), cost_usd: 0.0125, cost_withheld: false })
        }
      >
        Add a priced turn
      </button>
    );
  }

  it("shows nothing before a priced turn and the running total after one", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <AssistantPanelProvider>
          <PricedTurns />
          <Panel />
        </AssistantPanelProvider>
      </MemoryRouter>,
    );

    expect(screen.queryByTestId(CONVERSATION_COST_TESTID)).not.toBeInTheDocument();
    await user.click(screen.getByTestId("add-priced-turn"));

    const heading = screen.getByRole("heading", { name: "Assistant", hidden: true });
    const cost = screen.getByTestId(CONVERSATION_COST_TESTID);
    expect(cost).toHaveTextContent("$0.0125");
    expect(heading.parentElement).toContainElement(cost);
  });
});
