// SPDX-License-Identifier: MIT
/**
 * Tests for the signed-in shell.
 *
 * "Panel state survives a route change" navigates for real and asserts the panel's open state and
 * conversation are still present.
 */

import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import type { JSX } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  AppShell,
  DISPLAY_UTILITY_PATTERN,
  PANEL_HIDDEN_GUARD_CLASS,
  THEME_STORAGE_KEY,
  readStoredTheme,
  resolveInitialTheme,
  storeTheme,
  useAssistantPanel,
  type AssistantTurn,
} from "./AppShell";
import { useAuth } from "./auth/AuthProvider";

vi.mock("./auth/AuthProvider", () => ({ useAuth: vi.fn() }));

const signOut = vi.fn();

function stubAuth(fullVisibility = false): void {
  vi.mocked(useAuth).mockReturnValue({
    status: "signed-in",
    identity: { repId: "rep-7", book: ["acc-1", "acc-2"], fullVisibility },
    signOut,
  } as unknown as ReturnType<typeof useAuth>);
}

/** Installs an in-memory `localStorage`; jsdom provides none in this project's configuration. */
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

function installMatchMedia(prefersDark: boolean): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: prefersDark && query.includes("dark"),
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
}

/**
 * One completed turn, in the shape the SSE stream delivers. A `completed` frame's `text` renders as
 * the turn's answer bubble, which the survival assertions look for.
 */
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

/** A page that can write to the panel's conversation and navigate away. */
function PageOne(): JSX.Element {
  const { appendTurn, openPanel } = useAssistantPanel();
  const navigate = useNavigate();
  return (
    <div>
      <h1>Pipeline</h1>
      <button type="button" onClick={openPanel}>
        open from page
      </button>
      <button type="button" onClick={() => appendTurn(completedTurn("summarise this view"))}>
        add turn
      </button>
      <button type="button" onClick={() => navigate("/accounts")}>
        go to accounts
      </button>
    </div>
  );
}

function PageTwo(): JSX.Element {
  return <h1>Accounts</h1>;
}

function renderShell(initialPath = "/pipeline") {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="pipeline" element={<PageOne />} />
          <Route path="accounts" element={<PageTwo />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  stubAuth();
  installStorage();
  installMatchMedia(false);
  document.documentElement.removeAttribute("data-theme");
});

afterEach(() => {
  // `globals: false` in the vitest config means RTL registers no automatic cleanup.
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

/**
 * Tailwind's preflight emits `[hidden]:where(…){display:none}` at zero specificity, so a display
 * utility outranks the `hidden` attribute unless paired with a guard class. jsdom loads no
 * stylesheet, so these assert the class contract; the computed check is a browser journey.
 */
describe("the panel's hidden attribute is not outranked by its own display utility", () => {
  it("pairs every display utility with the [hidden] guard", () => {
    renderShell();
    const className = screen.getByTestId("assistant-panel").className;

    expect(className).toMatch(DISPLAY_UTILITY_PATTERN);
    expect(className).toContain(PANEL_HIDDEN_GUARD_CLASS);
  });

  it("uses the guard form that Tailwind actually emits, not the inert one", () => {
    // `[hidden]:hidden` emits no rule under Tailwind 3.
    expect(PANEL_HIDDEN_GUARD_CLASS).toBe("[&[hidden]]:hidden");

    renderShell();
    const className = screen.getByTestId("assistant-panel").className;
    const classes = className.split(/\s+/);
    expect(classes).toContain("[&[hidden]]:hidden");
    expect(classes).not.toContain("[hidden]:hidden");
  });

  it("keeps the attribute authoritative, so assistive technology and CSS agree", async () => {
    const user = userEvent.setup();
    renderShell();
    const panel = () => screen.getByTestId("assistant-panel");

    expect(panel()).toHaveAttribute("hidden");
    await user.click(screen.getByRole("button", { name: "open from page" }));
    expect(panel()).not.toHaveAttribute("hidden");
    await user.click(screen.getByTestId("panel-close"));
    expect(panel()).toHaveAttribute("hidden");

    // The guard is unconditional, present while open as well as closed.
    expect(panel().className).toContain(PANEL_HIDDEN_GUARD_CLASS);
  });
});

describe("the panel is a presence, not a route", () => {
  it("keeps panel state across a route change", async () => {
    const user = userEvent.setup();
    renderShell();

    await user.click(screen.getByRole("button", { name: "open from page" }));
    await user.click(screen.getByRole("button", { name: "add turn" }));

    const panel = screen.getByTestId("assistant-panel");
    expect(panel).not.toHaveAttribute("hidden");
    expect(screen.getByText("summarise this view")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "go to accounts" }));
    expect(screen.getByRole("heading", { name: "Accounts" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Pipeline" })).not.toBeInTheDocument();

    // The route changed underneath it; the panel kept both its open state and its conversation.
    expect(screen.getByTestId("assistant-panel")).not.toHaveAttribute("hidden");
    expect(screen.getByText("summarise this view")).toBeVisible();
  });

  it("renders the panel as a sibling of the routed outlet, not inside it", () => {
    renderShell();
    const panel = screen.getByTestId("assistant-panel");
    const outlet = document.getElementById("main-content");

    expect(outlet).not.toBeNull();
    expect(outlet?.contains(panel)).toBe(false);
    expect(panel.parentElement).toBe(outlet?.parentElement);
  });

  it("does not remount the panel element on navigation", async () => {
    const user = userEvent.setup();
    renderShell();
    const before = screen.getByTestId("assistant-panel");

    await user.click(screen.getByRole("button", { name: "go to accounts" }));

    expect(screen.getByTestId("assistant-panel")).toBe(before);
  });

  it("toggles from the header and closes from inside the panel", async () => {
    const user = userEvent.setup();
    renderShell();
    const toggle = screen.getByTestId("panel-toggle");

    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByTestId("assistant-panel")).toHaveAttribute("hidden");

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("assistant-panel")).not.toHaveAttribute("hidden");

    await user.click(screen.getByTestId("panel-close"));
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByTestId("assistant-panel")).toHaveAttribute("hidden");
  });

  it("never renders the panel as a blank region", async () => {
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByTestId("panel-toggle"));

    const panel = screen.getByTestId("assistant-panel");
    expect(screen.getByTestId("panel-empty")).toBeVisible();
    expect(panel.textContent?.trim()).not.toBe("");

    await user.click(screen.getByRole("button", { name: "add turn" }));
    expect(screen.queryByTestId("panel-empty")).not.toBeInTheDocument();
  });

  it("points the toggle at the panel it controls", () => {
    renderShell();
    const controls = screen.getByTestId("panel-toggle").getAttribute("aria-controls");
    expect(controls).toBe("assistant-panel");
    expect(document.getElementById(controls ?? "")).toBe(screen.getByTestId("assistant-panel"));
  });
});

describe("theme application", () => {
  it("writes data-theme on the document element", () => {
    renderShell();
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
  });

  it("applies and persists the toggled theme", async () => {
    const user = userEvent.setup();
    const store = installStorage();
    renderShell();

    await user.click(screen.getByTestId("theme-toggle"));

    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(store.get(THEME_STORAGE_KEY)).toBe("dark");
    expect(screen.getByTestId("theme-toggle")).toHaveAttribute("aria-pressed", "true");
  });

  it("restores the persisted preference over the OS preference", () => {
    installStorage({ [THEME_STORAGE_KEY]: "dark" });
    installMatchMedia(false);
    renderShell();
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });

  it("falls back to the OS preference when nothing is persisted", () => {
    installStorage();
    installMatchMedia(true);
    renderShell();
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });

  it("keeps the theme across a route change", async () => {
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByTestId("theme-toggle"));
    await user.click(screen.getByRole("button", { name: "go to accounts" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });
});

describe("theme storage is guarded, not assumed", () => {
  it("falls back when localStorage is absent", () => {
    vi.stubGlobal("localStorage", undefined);
    installMatchMedia(true);
    expect(readStoredTheme()).toBeNull();
    expect(resolveInitialTheme()).toBe("dark");
  });

  it("falls back when localStorage throws, as in a privacy-restricted browser", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("SecurityError");
      },
    });
    installMatchMedia(false);
    expect(readStoredTheme()).toBeNull();
    expect(() => storeTheme("dark")).not.toThrow();
    expect(resolveInitialTheme()).toBe("light");
  });

  it("still renders and still applies a theme with no storage at all", () => {
    vi.stubGlobal("localStorage", undefined);
    vi.stubGlobal("matchMedia", undefined);
    renderShell();
    expect(screen.getByTestId("app-shell")).toBeInTheDocument();
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
  });

  it("ignores a corrupt persisted value rather than applying it", () => {
    installStorage({ [THEME_STORAGE_KEY]: "solarized" });
    installMatchMedia(false);
    expect(readStoredTheme()).toBeNull();
    renderShell();
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
  });
});

describe("shell chrome preserves the retained accessibility contract", () => {
  it("exposes the primary nav", () => {
    renderShell();
    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(within(nav).getByRole("link", { name: "Pipeline" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Approvals" })).toBeInTheDocument();
  });

  it("marks the current route's nav link with aria-current", () => {
    renderShell("/accounts");
    expect(screen.getByRole("link", { name: "Accounts" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("puts the skip link first in the tab order and targets the outlet wrapper", async () => {
    const user = userEvent.setup();
    renderShell();
    await user.tab();
    const skip = screen.getByRole("link", { name: "Skip to main content" });
    expect(skip).toHaveFocus();
    expect(skip).toHaveAttribute("href", "#main-content");
    expect(document.getElementById("main-content")).not.toBeNull();
  });

  it("renders no second main landmark, because pages own their own", () => {
    renderShell();
    expect(screen.queryAllByRole("main")).toHaveLength(0);
  });

  it("signs out through the auth provider", async () => {
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(signOut).toHaveBeenCalledTimes(1);
  });

  it("reports book size for a scoped rep and full visibility for an unscoped one", () => {
    renderShell();
    expect(screen.getByText(/2 accounts/)).toBeInTheDocument();

    cleanup();
    stubAuth(true);
    renderShell();
    expect(screen.getByText(/full visibility/)).toBeInTheDocument();
  });

  it("does not link to the retired /chat route", () => {
    renderShell();
    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(within(nav).queryByRole("link", { name: "Assistant" })).not.toBeInTheDocument();
    for (const link of within(nav).getAllByRole("link")) {
      expect(link.getAttribute("href")).not.toBe("/chat");
    }
  });
});
