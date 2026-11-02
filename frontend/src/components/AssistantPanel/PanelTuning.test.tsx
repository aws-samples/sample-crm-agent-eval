// SPDX-License-Identifier: MIT
/**
 * Panel tuning tests: the controls come from the fetched matrix and the user's saved settings, a
 * save writes the schema-3 block to `/api/user_settings` and the next turn reads what the server
 * accepted, a rejection lands at the offending control, and nothing is written to browser storage.
 *
 * The axe assertion renders through `Panel`'s `children` seam with the panel open, asserted before
 * the scan, since axe excludes hidden subtrees.
 */

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { JSX } from "react";

import * as AuthProviderModule from "../../auth/AuthProvider";
import { __resetRuntimeConfigForTests, loadRuntimeConfig } from "../../config/runtimeConfig";
import { __resetSessionTuningForTests, getSessionTuning } from "../tuning/sessionTuning";
import { Panel } from "./Panel";
import { AssistantPanelProvider, useAssistantPanel } from "./PanelProvider";
import { PanelTuning } from "./PanelTuning";

const configBody = {
  cognitoUserPoolId: "us-east-1_test",
  cognitoClientId: "client123",
  cognitoRegion: "us-east-1",
  cognitoHostedUiDomain: "",
  apiBaseUrl: "/api",
  agentBaseUrl: "/agent",
  agentRuntimeArn: "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
};

function rawMatrix() {
  return {
    default_family: "anthropic",
    models: {
      "anthropic.claude-opus-5-5": {
        model_id: "anthropic.claude-opus-5-5",
        family: "anthropic",
        tier: "supervisor",
        effort: {
          supported: true,
          values: ["low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: null,
        },
      },
      "anthropic.claude-sonnet-5-5": {
        model_id: "anthropic.claude-sonnet-5-5",
        family: "anthropic",
        tier: "subagent",
        effort: {
          supported: true,
          values: ["low", "medium", "high"],
          default: "high",
          capped_at_when_thinking_disabled: null,
        },
      },
      "anthropic.claude-haiku-5-5": {
        model_id: "anthropic.claude-haiku-5-5",
        family: "anthropic",
        tier: "tool",
        effort: {
          supported: true,
          values: ["low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: "high",
        },
      },
      "openai.gpt-6-astra": {
        model_id: "openai.gpt-6-astra",
        family: "openai",
        tier: "supervisor",
        effort: {
          supported: true,
          values: ["none", "low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: null,
        },
      },
      "openai.gpt-6.1-sol": {
        model_id: "openai.gpt-6.1-sol",
        family: "openai",
        tier: "subagent",
        effort: {
          supported: true,
          values: ["none", "low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: null,
        },
      },
      "openai.gpt-6-luna": {
        model_id: "openai.gpt-6-luna",
        family: "openai",
        tier: "tool",
        effort: {
          supported: true,
          values: ["none", "low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: null,
        },
      },
    },
    tiers: {
      anthropic: {
        supervisor: { model_id: "anthropic.claude-opus-5-5", tuning: { effort: "low" } },
        subagent: { model_id: "anthropic.claude-sonnet-5-5", tuning: { effort: "medium" } },
        tool: { model_id: "anthropic.claude-haiku-5-5", tuning: { effort: "low" } },
      },
      openai: {
        supervisor: { model_id: "openai.gpt-6-astra", tuning: { effort: "low" } },
        subagent: { model_id: "openai.gpt-6.1-sol", tuning: { effort: "medium" } },
        tool: { model_id: "openai.gpt-6-luna", tuning: { effort: "low" } },
      },
    },
  };
}

const capabilitiesBody = rawMatrix();

/** The defaults view `/api/user_settings` returns for a user with nothing saved. */
const DEFAULT_TIERS = {
  supervisor: { family: "anthropic", model: "anthropic.claude-opus-5-5", effort: "low" },
  subagent: { family: "anthropic", model: "anthropic.claude-sonnet-5-5", effort: "medium" },
  tool: { family: "anthropic", model: "anthropic.claude-haiku-5-5", effort: "low" },
};

/** A fake `/api/user_settings` backend: what is saved, what it was asked, and an optional rejection. */
interface SettingsServer {
  saved: Record<string, unknown> | null;
  notice: "saved_model_unavailable" | "saved_settings_unreadable" | null;
  calls: { action: string; tuning?: Record<string, unknown> }[];
  rejectPut: { status: number; code: string; message: string; source?: string } | null;
}

let server: SettingsServer;
let storageWrites: string[] = [];

function settingsView(): Record<string, unknown> {
  return {
    tuning: server.saved ?? { schema: 3, source: "config/models.yaml", tiers: DEFAULT_TIERS },
    is_default: server.saved === null,
    notice: server.notice,
    updated_at: server.saved === null ? null : "2026-10-08T00:00:00Z",
  };
}

function jsonResponse(status: number, body: unknown) {
  return Promise.resolve({ ok: status < 300, status, json: () => Promise.resolve(body) });
}

function stubFetch(options: { capabilitiesStatus?: number; capabilitiesBody?: unknown; settingsStatus?: number } = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (url === "/config.json") return jsonResponse(200, configBody);
      const headers = init?.headers as Record<string, string> | undefined;
      if (headers?.Authorization !== "Bearer tok-123") {
        throw new Error(`missing Authorization on ${url}: ${JSON.stringify(headers)}`);
      }
      if (url === "/api/capabilities") {
        return jsonResponse(options.capabilitiesStatus ?? 200, options.capabilitiesBody ?? capabilitiesBody);
      }
      if (url === "/api/user_settings") {
        const body = JSON.parse(String(init?.body)) as { action: string; tuning?: Record<string, unknown> };
        server.calls.push(body);
        if (options.settingsStatus !== undefined) {
          return jsonResponse(options.settingsStatus, { error: { code: "internal_error", message: "settings store unavailable" } });
        }
        if (body.action === "put") {
          if (server.rejectPut !== null) {
            const { status, code, message, source } = server.rejectPut;
            return jsonResponse(status, { error: { code, message, ...(source ? { source } : {}) } });
          }
          server.saved = body.tuning ?? null;
          server.notice = null;
        }
        if (body.action === "reset") {
          server.saved = null;
          server.notice = null;
        }
        return jsonResponse(200, settingsView());
      }
      throw new Error(`unexpected fetch: ${url}`);
    }),
  );
}

function installStorageSpy(): void {
  const store = new Map<string, string>();
  const spy = (label: string) => ({
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => {
      storageWrites.push(`${label}.setItem(${key}=${value})`);
      store.set(key, value);
    },
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
  });
  vi.stubGlobal("localStorage", spy("localStorage"));
  vi.stubGlobal("sessionStorage", spy("sessionStorage"));
}

function puts(): Record<string, unknown>[] {
  return server.calls.filter((call) => call.action === "put").map((call) => call.tuning ?? {});
}

beforeEach(() => {
  server = { saved: null, notice: null, calls: [], rejectPut: null };
  storageWrites = [];
  __resetRuntimeConfigForTests();
  __resetSessionTuningForTests();
  installStorageSpy();
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
  });
  stubFetch();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  __resetSessionTuningForTests();
});

async function renderTuning() {
  await loadRuntimeConfig();
  const user = userEvent.setup();
  const result = render(<PanelTuning />);
  await screen.findByText("Supervisor model tuning");
  return { ...result, user };
}

function tierGroup(name: string): HTMLElement {
  return screen.getByRole("group", { name: `${name} model tuning` });
}

describe("PanelTuning loads the matrix and the saved settings", () => {
  it("renders one fieldset per tier", async () => {
    await renderTuning();
    for (const tier of ["Supervisor", "Subagent", "Tool execution"]) {
      expect(tierGroup(tier)).toBeInTheDocument();
    }
  });

  it("reads the saved settings once and shows them in each tier's controls", async () => {
    server.saved = {
      schema: 3,
      source: "settings.tuning",
      tiers: { ...DEFAULT_TIERS, subagent: { family: "openai", model: "openai.gpt-6.1-sol", effort: "low" } },
    };
    await renderTuning();
    const subagent = within(tierGroup("Subagent"));
    expect(subagent.getByLabelText("Provider family")).toHaveValue("openai");
    expect(subagent.getByLabelText("Model")).toHaveValue("openai.gpt-6.1-sol");
    expect(subagent.getByLabelText("Effort")).toHaveValue("low");
    expect(server.calls.filter((call) => call.action === "get")).toHaveLength(1);
    // The chat composer reads the same store, so the next turn carries what was saved.
    expect(getSessionTuning().tiers.subagent.family).toBe("openai");
  });

  it("says when a saved model is no longer available", async () => {
    server.notice = "saved_model_unavailable";
    await renderTuning();
    expect(screen.getByText(/no longer available, so the defaults are in use/)).toBeInTheDocument();
  });

  it("says, differently, when the saved settings could not be read", async () => {
    server.notice = "saved_settings_unreadable";
    await renderTuning();
    expect(screen.getByText(/saved settings could not be read/)).toHaveAttribute("role", "note");
    expect(screen.queryByText(/no longer available/)).not.toBeInTheDocument();
  });

  it("shows a failed settings load as an alert and falls back to the defaults", async () => {
    stubFetch({ settingsStatus: 500 });
    await renderTuning();
    expect(screen.getByRole("alert")).toHaveTextContent(/Couldn.t load your saved settings/);
    expect(within(tierGroup("Supervisor")).getByLabelText("Model")).toHaveValue("anthropic.claude-opus-5-5");
  });

  it("surfaces a failed capabilities fetch as an alert, not an empty panel", async () => {
    stubFetch({ capabilitiesStatus: 503, capabilitiesBody: {} });
    await loadRuntimeConfig();
    render(<PanelTuning />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Couldn.t load model capabilities/);
  });

  it("names the missing scope in the one alert when the matrix fetch is denied", async () => {
    stubFetch({
      capabilitiesStatus: 403,
      capabilitiesBody: {
        statusCode: 403,
        error: { code: "scope_denied", message: "Model tuning is outside your book.", missing_scope: "tuning:read" },
      },
    });
    await loadRuntimeConfig();
    render(<PanelTuning />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Model tuning is outside your book.");
    expect(alert).toHaveTextContent("tuning:read");
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  it("renders nothing when the runtime config has not loaded, instead of taking the shell down", () => {
    __resetRuntimeConfigForTests();
    const { container } = render(<PanelTuning />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId("panel-tuning")).not.toBeInTheDocument();
  });
});

describe("PanelTuning saves to the user's account", () => {
  it("does not change what the next turn sends until a save succeeds", async () => {
    const { user } = await renderTuning();
    const before = getSessionTuning();
    await user.selectOptions(within(tierGroup("Tool execution")).getByLabelText("Provider family"), "openai");
    expect(getSessionTuning()).toBe(before);
    expect(puts()).toHaveLength(0);
  });

  it("saves the schema-3 block for every tier, then applies it and confirms", async () => {
    const { user } = await renderTuning();
    await user.selectOptions(within(tierGroup("Tool execution")).getByLabelText("Provider family"), "openai");
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    await screen.findByRole("status");
    expect(screen.getByRole("status")).toHaveTextContent("Settings saved. They apply from your next message.");
    expect(puts()).toEqual([
      {
        schema: 3,
        source: "settings.tuning",
        tiers: {
          supervisor: DEFAULT_TIERS.supervisor,
          subagent: DEFAULT_TIERS.subagent,
          tool: { family: "openai", model: null, effort: null },
        },
      },
    ]);
    expect(getSessionTuning().tiers.tool.family).toBe("openai");
    // No sentence claims that some tiers are not sent: every tier travels now.
    expect(screen.queryByText(/sends only/)).not.toBeInTheDocument();
    expect(storageWrites, "a save wrote to browser storage").toEqual([]);
  });

  it("offers no temperature control in any tier", async () => {
    await renderTuning();
    for (const tier of ["Supervisor", "Subagent", "Tool execution"]) {
      expect(within(tierGroup(tier)).queryByLabelText(/temperature/i)).toBeNull();
      expect(within(tierGroup(tier)).queryByRole("spinbutton")).toBeNull();
    }
  });

  it("refuses an effort the selected model does not offer before any request, at the effort control", async () => {
    server.saved = {
      schema: 3,
      source: "settings.tuning",
      tiers: { ...DEFAULT_TIERS, tool: { family: "anthropic", model: "anthropic.claude-sonnet-5-5", effort: "max" } },
    };
    const { user } = await renderTuning();
    const tool = within(tierGroup("Tool execution"));
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(tool.getByLabelText("Effort")).toHaveAttribute("aria-invalid", "true");
    expect(tool.getByRole("alert")).toHaveTextContent(/accepted values are/);
    expect(puts()).toHaveLength(0);
  });

  it("shows a server refusal naming a removed field at its tier, not as a failed save", async () => {
    server.rejectPut = {
      status: 400,
      code: "validation_failed",
      message: "temperature is no longer a tuning parameter; remove it from the supervisor tier.",
      source: "tuning.tiers.supervisor.temperature",
    };
    const { user } = await renderTuning();
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    const supervisor = within(tierGroup("Supervisor"));
    expect(await supervisor.findByRole("alert")).toHaveTextContent("no longer a tuning parameter");
    expect(screen.queryByText(/Settings not saved/)).toBeNull();
    expect(getSessionTuning().tiers.supervisor).toEqual(DEFAULT_TIERS.supervisor);
  });

  it("shows the server's message at the control its source names", async () => {
    server.rejectPut = {
      status: 400,
      code: "validation_failed",
      message: "openai.gpt-6.1-sol does not accept effort='max'.",
      source: "tuning.tiers.subagent.effort",
    };
    const { user } = await renderTuning();
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    const subagent = within(tierGroup("Subagent"));
    await waitFor(() => expect(subagent.getByLabelText("Effort")).toHaveAttribute("aria-invalid", "true"));
    expect(subagent.getByRole("alert")).toHaveTextContent("openai.gpt-6.1-sol does not accept effort='max'.");
    expect(getSessionTuning().tiers.subagent).toEqual(DEFAULT_TIERS.subagent);
  });

  it("reports a save failure with no control as a page-level alert", async () => {
    server.rejectPut = { status: 403, code: "scope_denied", message: "Only a signed-in user can change tuning settings." };
    const { user } = await renderTuning();
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() =>
      expect(screen.getByText(/Settings not saved: Only a signed-in user can change tuning settings/)).toBeInTheDocument(),
    );
  });

  it("discards an unsaved change", async () => {
    const { user } = await renderTuning();
    const family = within(tierGroup("Tool execution")).getByLabelText("Provider family");
    await user.selectOptions(family, "openai");
    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(family).toHaveValue("anthropic");
  });

  it("resets to the defaults through the server", async () => {
    server.saved = { schema: 3, source: "settings.tuning", tiers: { ...DEFAULT_TIERS, tool: { family: "openai", model: null, effort: null } } };
    const { user } = await renderTuning();
    expect(within(tierGroup("Tool execution")).getByLabelText("Provider family")).toHaveValue("openai");
    await user.click(screen.getByRole("button", { name: "Reset to defaults" }));

    await screen.findByText("Defaults restored. They apply from your next message.");
    expect(server.calls.map((call) => call.action)).toContain("reset");
    expect(within(tierGroup("Tool execution")).getByLabelText("Provider family")).toHaveValue("anthropic");
    expect(getSessionTuning().tiers.tool).toEqual(DEFAULT_TIERS.tool);
    expect(storageWrites, "a reset wrote to browser storage").toEqual([]);
  });
});

describe("PanelTuning inside the panel is accessible", () => {
  /** `children` is the seam `Panel` documents, so the tuning is reached the way the app reaches it. */
  function PanelHarness(): JSX.Element {
    const { open, openPanel } = useAssistantPanel();
    return (
      <div>
        <button type="button" data-testid="open-panel" onClick={openPanel}>
          Assistant
        </button>
        {open ? (
          <Panel>
            <PanelTuning />
          </Panel>
        ) : null}
      </div>
    );
  }

  it("has no serious or critical axe violations, with the panel open", async () => {
    const user = userEvent.setup();
    await loadRuntimeConfig();
    const { container } = render(
      // `Panel` reads the route (`usePageContext`), so its render sites need a router.
      <MemoryRouter>
        <AssistantPanelProvider>
          <PanelHarness />
        </AssistantPanelProvider>
      </MemoryRouter>,
    );

    await user.click(screen.getByTestId("open-panel"));
    const panel = screen.getByTestId("assistant-panel");
    const disclosure = (await within(panel).findByTestId("panel-tuning")) as HTMLDetailsElement;
    await user.click(within(disclosure).getByText("Model tuning", { selector: "summary" }));
    expect(disclosure.open, "the tuning disclosure must be expanded before axe runs").toBe(true);

    await screen.findByText("Supervisor model tuning");
    expect(within(disclosure).getAllByLabelText("Provider family")).toHaveLength(3);
    expect(within(disclosure).getAllByLabelText("Model")).toHaveLength(3);

    // `color-contrast` is indeterminate in jsdom (no paint engine), so it is disabled.
    const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    const disallowed = results.violations.filter(
      (v) => v.impact === "serious" || v.impact === "critical",
    );
    expect(
      disallowed.map((v) => `[${v.impact}] ${v.id}: ${v.help}`),
      "axe violations inside the open panel",
    ).toEqual([]);
  });
});
