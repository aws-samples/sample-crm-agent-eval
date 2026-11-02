// SPDX-License-Identifier: MIT
/**
 * Signed-in application shell: chrome, the routed `<Outlet />`, and the assistant panel mounted
 * as the outlet's sibling, so the panel keeps its state across navigation.
 *
 * Also applies the theme by writing `data-theme` on the document element; `tokens.css` has no
 * `prefers-color-scheme` block.
 *
 * The panel lives in `components/AssistantPanel/`: `AssistantPanelProvider` owns the open state
 * and its persistence, `Panel` owns the region, focus management, Escape, and the keyboard
 * shortcut.
 */

import { useCallback, useLayoutEffect, useState } from "react";
import type { JSX } from "react";
import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "./auth/AuthProvider";
import { AssistantConversation } from "./components/AssistantPanel/AssistantConversation";
import { Panel } from "./components/AssistantPanel/Panel";
import { PanelTuning } from "./components/AssistantPanel/PanelTuning";
import { AssistantPanelProvider, useAssistantPanel } from "./components/AssistantPanel/PanelProvider";
import { Button } from "./components/ui/button";
import { cn } from "./components/ui/utils";

/* ------------------------------------------------------------------ theme */

export type ThemePreference = "light" | "dark";

/** Persisted per user. */
export const THEME_STORAGE_KEY = "crm.theme";

function isThemePreference(value: unknown): value is ThemePreference {
  return value === "light" || value === "dark";
}

/**
 * Reads the persisted preference, or `null` when storage is absent or throws.
 */
export function readStoredTheme(): ThemePreference | null {
  let raw: string | null;
  try {
    raw = globalThis.localStorage?.getItem(THEME_STORAGE_KEY) ?? null;
  } catch {
    return null;
  }
  return isThemePreference(raw) ? raw : null;
}

/** Persists the preference, best-effort. */
export function storeTheme(theme: ThemePreference): void {
  try {
    globalThis.localStorage?.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Storage unavailable or denied; the theme still applies for this session.
  }
}

/** The OS preference, used only to seed the first visit before anything is persisted. */
export function systemTheme(): ThemePreference {
  try {
    return globalThis.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

export function resolveInitialTheme(): ThemePreference {
  return readStoredTheme() ?? systemTheme();
}

/* ----------------------------------------------------------- assistant panel */

/* Re-exported for `AppShell.test.tsx` and `themeBoot.test.ts`. */
export {
  AssistantPanelProvider,
  useAssistantPanel,
  type AssistantPanelContextValue,
  type AssistantTurn,
} from "./components/AssistantPanel/PanelProvider";
export {
  DISPLAY_UTILITY_PATTERN,
  PANEL_HIDDEN_GUARD_CLASS,
} from "./components/AssistantPanel/Panel";

/* ------------------------------------------------------------------------- chrome */

/** Primary nav destinations, in the order a rep works through them. */
const NAV_ITEMS: readonly { readonly to: string; readonly label: string }[] = [
  { to: "/pipeline", label: "Pipeline" },
  { to: "/accounts", label: "Accounts" },
  { to: "/opportunities", label: "Opportunities" },
  { to: "/approvals", label: "Approvals" },
  { to: "/audit", label: "Audit" },
  { to: "/evals", label: "Evals" },
  { to: "/settings", label: "Settings" },
];

function ThemeToggle({
  theme,
  onToggle,
}: {
  theme: ThemePreference;
  onToggle: () => void;
}): JSX.Element {
  return (
    <Button
      variant="ghost"
      size="sm"
      data-testid="theme-toggle"
      aria-pressed={theme === "dark"}
      onClick={onToggle}
    >
      Dark theme
    </Button>
  );
}

export function AppShell(): JSX.Element {
  const { identity, signOut } = useAuth();
  const [theme, setTheme] = useState<ThemePreference>(resolveInitialTheme);

  /* `useLayoutEffect` commits before the browser paints, so no frame renders in the wrong theme. */
  useLayoutEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  const toggleTheme = useCallback(() => {
    setTheme((previous) => {
      const next: ThemePreference = previous === "dark" ? "light" : "dark";
      storeTheme(next);
      return next;
    });
  }, []);

  return (
    <AssistantPanelProvider>
      <div data-testid="app-shell" className="flex min-h-screen flex-col bg-surface-base">
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-toast focus:rounded-control focus:bg-surface-raised focus:px-5 focus:py-3 focus:text-sm focus:shadow-overlay"
        >
          Skip to main content
        </a>

        <header className="flex h-header items-center gap-6 border-b-hairline border-border-default bg-surface-raised px-6">
          <span className="text-sm font-semibold tracking-tight text-text-primary">FirstCRM</span>
          <nav aria-label="Primary" className="min-w-0 flex-1">
            <ul className="flex flex-wrap items-center gap-5">
              {NAV_ITEMS.map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    className={({ isActive }) =>
                      cn(
                        "rounded-control px-3 py-2 text-xs font-medium",
                        "text-text-secondary hover:bg-surface-hover hover:text-text-primary",
                        isActive && "bg-surface-selected text-text-primary",
                      )
                    }
                  >
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>
          <div className="flex shrink-0 items-center gap-4">
            <PanelToggle />
            <ThemeToggle theme={theme} onToggle={toggleTheme} />
            {identity ? (
              <span className="hidden text-2xs text-text-muted md:inline">
                {identity.repId}
                {identity.fullVisibility
                  ? " · full visibility"
                  : ` · ${identity.book.length} accounts`}
              </span>
            ) : null}
            <Button variant="secondary" size="sm" onClick={signOut}>
              Sign out
            </Button>
          </div>
        </header>

        <div className="flex flex-1 items-stretch">
          {/* The outlet and the panel are siblings. */}
          <div id="main-content" className="min-w-0 flex-1 p-6">
            <Outlet />
          </div>
          {/* The only place that renders `Panel`; its content is passed as `children`. */}
          <Panel>
            <AssistantConversation />
            <PanelTuning />
          </Panel>
        </div>
      </div>
    </AssistantPanelProvider>
  );
}

function PanelToggle(): JSX.Element {
  const { open, togglePanel } = useAssistantPanel();
  return (
    <Button
      variant="ghost"
      size="sm"
      data-testid="panel-toggle"
      aria-expanded={open}
      aria-controls="assistant-panel"
      onClick={togglePanel}
    >
      Assistant
    </Button>
  );
}
