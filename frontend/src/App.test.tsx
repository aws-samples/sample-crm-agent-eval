// SPDX-License-Identifier: MIT
/**
 * Route-table test for the shell (`App.tsx`) and the signed-in layout it renders, `AppShell`.
 *
 * Every page module is stubbed with a marker that echoes its `useParams()`, so this file asserts
 * which path renders which page and that each `:param` name matches the `useParams` key the real
 * page reads. `linkTargetsResolve` derives every link and `navigate(...)` target from the page
 * sources and asserts each one is routed.
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useParams } from "react-router-dom";
import type { JSX } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as AuthProviderModule from "./auth/AuthProvider";
import type { AuthContextValue } from "./auth/AuthProvider";
import { __resetRuntimeConfigForTests, loadRuntimeConfig } from "./config/runtimeConfig";

/** Renders `page:<name>` plus every resolved route param, so a test can assert both at once. */
function marker(name: string): () => JSX.Element {
  return function Marker(): JSX.Element {
    const params = useParams();
    return (
      <main>
        <h1>{`page:${name}`}</h1>
        <p data-testid="params">{JSON.stringify(params)}</p>
      </main>
    );
  };
}

vi.mock("./pages/pipeline/PipelinePage", () => ({ PipelinePage: marker("pipeline") }));
vi.mock("./pages/accounts/AccountListPage", () => ({ AccountListPage: marker("account-list") }));
vi.mock("./pages/accounts/AccountCreatePage", () => ({
  AccountCreatePage: marker("account-create"),
}));
vi.mock("./pages/accounts/AccountDetailPage", () => ({
  AccountDetailPage: marker("account-detail"),
}));
vi.mock("./pages/accounts/AccountEditPage", () => ({ AccountEditPage: marker("account-edit") }));
vi.mock("./pages/contacts/ContactCreatePage", () => ({
  ContactCreatePage: marker("contact-create"),
}));
vi.mock("./pages/contacts/ContactDetailPage", () => ({
  ContactDetailPage: marker("contact-detail"),
}));
vi.mock("./pages/contacts/ContactEditPage", () => ({ ContactEditPage: marker("contact-edit") }));
vi.mock("./pages/opportunities/OpportunityListPage", () => ({
  OpportunityListPage: marker("opportunity-list"),
}));
vi.mock("./pages/opportunities/OpportunityCreatePage", () => ({
  OpportunityCreatePage: marker("opportunity-create"),
}));
vi.mock("./pages/opportunities/OpportunityDetailPage", () => ({
  OpportunityDetailPage: marker("opportunity-detail"),
}));
vi.mock("./pages/activities/ActivitiesPage", () => ({ ActivitiesPage: marker("activities") }));
vi.mock("./pages/approvals/ApprovalsPage", () => ({ ApprovalsPage: marker("approvals") }));
vi.mock("./pages/audit/AuditPage", () => ({ AuditPage: marker("audit") }));
vi.mock("./pages/settings/SettingsPage", () => ({ SettingsPage: marker("settings") }));
vi.mock("./pages/evals/EvalDashboardPage", () => ({
  EvalDashboardPage: ({ apiBaseUrl }: { apiBaseUrl: string }) => (
    <main>
      <h1>page:evals</h1>
      <p data-testid="eval-api-base">{apiBaseUrl}</p>
    </main>
  ),
}));
vi.mock("./auth/SignInForm", () => ({
  SignInForm: () => (
    <main>
      <h1>page:sign-in</h1>
    </main>
  ),
}));

const { App } = await import("./App");

const RUNTIME_CONFIG_BODY = {
  cognitoUserPoolId: "us-east-1_test",
  cognitoClientId: "client123",
  cognitoRegion: "us-east-1",
  cognitoHostedUiDomain: "",
  apiBaseUrl: "/api",
  agentBaseUrl: "/agent",
  agentRuntimeArn: "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/test",
};

const SIGNED_IN: AuthContextValue = {
  status: "signed-in",
  identity: {
    userId: "user-1",
    repId: "rep-ashford",
    book: ["demo-acct-0001"],
    fullVisibility: false,
    expiresAt: 9_999_999_999,
  },
  signIn: vi.fn(),
  signOut: vi.fn(),
  getAccessToken: vi.fn().mockResolvedValue("tok-123"),
};

function stubAuth(overrides: Partial<AuthContextValue> = {}): AuthContextValue {
  const value = { ...SIGNED_IN, ...overrides };
  vi.spyOn(AuthProviderModule, "useAuth").mockReturnValue(value);
  return value;
}

function renderAt(path: string): void {
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

beforeEach(async () => {
  __resetRuntimeConfigForTests();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(RUNTIME_CONFIG_BODY) }),
  );
  await loadRuntimeConfig();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("App auth gating", () => {
  it("renders the sign-in form when signed out", () => {
    stubAuth({ status: "signed-out", identity: undefined });
    renderAt("/pipeline");
    expect(screen.getByRole("heading", { name: "page:sign-in" })).toBeInTheDocument();
  });

  it("renders a busy indicator while auth is still resolving", () => {
    stubAuth({ status: "loading", identity: undefined });
    renderAt("/pipeline");
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("renders the sign-in form on the /logout landing page when signed out", () => {
    stubAuth({ status: "signed-out", identity: undefined });
    renderAt("/logout");
    expect(screen.getByRole("heading", { name: "page:sign-in" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Page not found" })).not.toBeInTheDocument();
  });

  it("does not render any page route while signed out", () => {
    stubAuth({ status: "signed-out", identity: undefined });
    renderAt("/audit");
    expect(screen.queryByRole("heading", { name: "page:audit" })).not.toBeInTheDocument();
  });
});

describe("route table", () => {
  it.each([
    ["/pipeline", "pipeline", {}],
    ["/accounts", "account-list", {}],
    ["/accounts/new", "account-create", {}],
    ["/accounts/demo-acct-0001", "account-detail", { accountId: "demo-acct-0001" }],
    ["/accounts/demo-acct-0001/edit", "account-edit", { accountId: "demo-acct-0001" }],
    ["/contacts/new", "contact-create", {}],
    ["/contacts/demo-cont-0007", "contact-detail", { contactId: "demo-cont-0007" }],
    ["/contacts/demo-cont-0007/edit", "contact-edit", { contactId: "demo-cont-0007" }],
    ["/opportunities", "opportunity-list", {}],
    ["/opportunities/new", "opportunity-create", {}],
    ["/opportunities/demo-opp-0003", "opportunity-detail", { opportunityId: "demo-opp-0003" }],
    [
      "/opportunities/demo-opp-0003/activities",
      "activities",
      { opportunityId: "demo-opp-0003" },
    ],
    ["/approvals", "approvals", {}],
    ["/audit", "audit", {}],
    ["/settings", "settings", {}],
  ])("%s renders %s with the params that page reads", (path, page, params) => {
    stubAuth();
    renderAt(path);
    expect(screen.getByRole("heading", { name: `page:${page}` })).toBeInTheDocument();
    expect(JSON.parse(screen.getByTestId("params").textContent ?? "{}")).toMatchObject(params);
  });

  it("redirects the root path to the pipeline", () => {
    stubAuth();
    renderAt("/");
    expect(screen.getByRole("heading", { name: "page:pipeline" })).toBeInTheDocument();
  });

  it("sends a signed-in user on /logout to the pipeline instead of a not-found page", () => {
    stubAuth();
    renderAt("/logout");
    expect(screen.getByRole("heading", { name: "page:pipeline" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Page not found" })).not.toBeInTheDocument();
  });

  it("passes the runtime config's apiBaseUrl to the eval dashboard", () => {
    stubAuth();
    renderAt("/evals");
    expect(screen.getByTestId("eval-api-base")).toHaveTextContent("/api");
  });

  it("renders a not-found page for an unrouted path instead of a blank screen", () => {
    stubAuth();
    renderAt("/nope/not-a-route");
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });

  it("prefers the static /accounts/new route over the :accountId parameter", () => {
    stubAuth();
    renderAt("/accounts/new");
    expect(screen.getByRole("heading", { name: "page:account-create" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "page:account-detail" })).not.toBeInTheDocument();
  });
});

describe("AppShell as the signed-in layout", () => {
  it("exposes the primary nav on every signed-in route", () => {
    stubAuth();
    renderAt("/audit");
    const nav = screen.getByRole("navigation", { name: "Primary" });
    for (const label of [
      "Pipeline",
      "Accounts",
      "Opportunities",
      "Approvals",
      "Audit",
      "Evals",
      "Settings",
    ]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
    expect(nav).toBeInTheDocument();
  });

  it("reaches the assistant through the panel toggle rather than a nav link", () => {
    stubAuth();
    renderAt("/audit");
    expect(screen.queryByRole("link", { name: "Assistant" })).not.toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: "Assistant" });
    expect(toggle).toHaveAttribute("aria-controls", "assistant-panel");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("marks the current route's nav link with aria-current", () => {
    stubAuth();
    renderAt("/audit");
    expect(screen.getByRole("link", { name: "Audit" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Pipeline" })).not.toHaveAttribute("aria-current");
  });

  it("offers a skip link as the first focusable element", async () => {
    stubAuth();
    renderAt("/audit");
    await userEvent.tab();
    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveFocus();
  });

  it("shows the signed-in rep and calls signOut on the sign-out button", async () => {
    const auth = stubAuth();
    renderAt("/audit");
    // The rep id and the book size are sibling text nodes in one strip, so a regex is needed.
    expect(screen.getByText(/rep-ashford/)).toHaveTextContent(/rep-ashford · \d+ accounts/);
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(auth.signOut).toHaveBeenCalledOnce();
  });

  it("renders exactly one main landmark so the layout does not nest one inside a page's", () => {
    stubAuth();
    renderAt("/audit");
    expect(screen.getAllByRole("main")).toHaveLength(1);
  });
});

describe("every in-app link target resolves to a route", () => {
  const pagesDir = join(dirname(fileURLToPath(import.meta.url)), "pages");

  /** Recursively collect every non-test `.tsx` source under `src/pages`. */
  function sourceFiles(dir: string): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) return sourceFiles(path);
      return entry.isFile() && entry.name.endsWith(".tsx") && !entry.name.includes(".test.")
        ? [path]
        : [];
    });
  }

  /**
   * Extract the literal path prefix of every `to="…"`/`to={`…`}`/`navigate("…")` target, with
   * `${…}` interpolations and query strings collapsed to a single `:param` placeholder.
   */
  function linkTargets(): Set<string> {
    const targets = new Set<string>();
    for (const file of sourceFiles(pagesDir)) {
      const source = readFileSync(file, "utf-8");
      for (const match of source.matchAll(/(?:to=\{?[`"]|navigate\([`"])(\/[^`"?]*)/g)) {
        targets.add(match[1].replace(/\$\{[^}]*\}/g, ":param").replace(/\/$/, "") || "/");
      }
    }
    return targets;
  }

  it.each([...linkTargets()].sort())("%s is a routed path", (target) => {
    stubAuth();
    renderAt(target.replace(/:param/g, "x-1"));
    expect(screen.queryByRole("heading", { name: "Page not found" })).not.toBeInTheDocument();
  });
});
