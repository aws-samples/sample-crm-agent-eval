// SPDX-License-Identifier: MIT
/**
 * Integration test for SettingsPage: the controls come from the fetched matrix and the user's
 * saved settings, a save goes to `/api/user_settings` as the schema-3 block, a rejected value is
 * shown at its control (or its tier) with the saved settings unchanged, and reset restores the
 * defaults.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as AuthProviderModule from "../../auth/AuthProvider";
import { __resetRuntimeConfigForTests, loadRuntimeConfig } from "../../config/runtimeConfig";
import { __resetSessionTuningForTests, getSessionTuning } from "../../components/tuning/sessionTuning";
import { SettingsPage } from "./SettingsPage";

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
  expect(storageWrites, "the settings page wrote to browser storage").toEqual([]);
});

async function renderPage() {
  await loadRuntimeConfig();
  const user = userEvent.setup();
  render(<SettingsPage />);
  await screen.findByText("Supervisor model tuning");
  return user;
}

function tierGroup(name: string): HTMLElement {
  return screen.getByRole("group", { name: `${name} model tuning` });
}

describe("SettingsPage", () => {
  it("shows a loading state, then one fieldset per tier", async () => {
    await loadRuntimeConfig();
    render(<SettingsPage />);
    expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
    await screen.findByText("Supervisor model tuning");
    for (const tier of ["Supervisor", "Subagent", "Tool execution"]) {
      expect(tierGroup(tier)).toBeInTheDocument();
    }
  });

  it("labels whether the defaults or saved settings are in use", async () => {
    await renderPage();
    expect(screen.getByText("Using defaults")).toBeInTheDocument();
    cleanup();
    __resetSessionTuningForTests();
    server.saved = { schema: 3, source: "settings.tuning", tiers: DEFAULT_TIERS };
    await renderPage();
    expect(screen.getByText("Saved to your account")).toBeInTheDocument();
  });

  it("no longer says the tuning is session-only or that some tiers are not sent", async () => {
    await renderPage();
    expect(screen.queryByText("Session only")).not.toBeInTheDocument();
    expect(screen.queryByText(/sends only/)).not.toBeInTheDocument();
  });

  it("saves a mixed-family selection and confirms", async () => {
    const user = await renderPage();
    const subagent = within(tierGroup("Subagent"));
    await user.selectOptions(subagent.getByLabelText("Provider family"), "openai");
    await user.selectOptions(subagent.getByLabelText("Effort"), "none");
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Settings saved.");
    const [saved] = puts();
    expect((saved.tiers as Record<string, unknown>).subagent).toEqual({
      family: "openai",
      model: null,
      effort: "none",
    });
    expect((saved.tiers as Record<string, unknown>).supervisor).toEqual(DEFAULT_TIERS.supervisor);
    expect(getSessionTuning().tiers.subagent.family).toBe("openai");
    expect(screen.getByText("Saved to your account")).toBeInTheDocument();
  });

  it("refuses an invalid value client-side, leaving the saved settings unchanged", async () => {
    // A saved effort the selected model no longer offers: Sonnet 5.5 stops at high.
    const stale = { family: "anthropic", model: "anthropic.claude-sonnet-5-5", effort: "xhigh" };
    server.saved = { schema: 3, source: "settings.tuning", tiers: { ...DEFAULT_TIERS, tool: stale } };
    const user = await renderPage();
    const tool = within(tierGroup("Tool execution"));
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(tool.getByLabelText("Effort")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/A setting was rejected/)).toBeInTheDocument();
    expect(puts()).toHaveLength(0);
    expect(getSessionTuning().tiers.tool).toEqual(stale);
  });

  it("shows a server rejection at the control its source names", async () => {
    server.rejectPut = {
      status: 400,
      code: "validation_failed",
      message: "Unknown model 'anthropic.claude-retired' for the supervisor tier.",
      source: "tuning.tiers.supervisor.model",
    };
    const user = await renderPage();
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    const supervisor = within(tierGroup("Supervisor"));
    await waitFor(() => expect(supervisor.getByLabelText("Model")).toHaveAttribute("aria-invalid", "true"));
    expect(supervisor.getByRole("alert")).toHaveTextContent("Unknown model 'anthropic.claude-retired'");
  });

  it("shows a server refusal naming a removed field at its tier, with the saved settings unchanged", async () => {
    server.saved = { schema: 3, source: "settings.tuning", tiers: DEFAULT_TIERS };
    server.rejectPut = {
      status: 400,
      code: "validation_failed",
      message: "temperature is no longer a tuning parameter; remove it from the tool tier.",
      source: "tuning.tiers.tool.temperature",
    };
    const user = await renderPage();
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    const tool = within(tierGroup("Tool execution"));
    expect(await tool.findByRole("alert")).toHaveTextContent("no longer a tuning parameter");
    expect(screen.getByText(/A setting was rejected/)).toBeInTheDocument();
    expect(getSessionTuning().tiers.tool).toEqual(DEFAULT_TIERS.tool);
    expect(server.saved).toEqual({ schema: 3, source: "settings.tuning", tiers: DEFAULT_TIERS });
  });

  it("resets to the defaults", async () => {
    server.saved = {
      schema: 3,
      source: "settings.tuning",
      tiers: { ...DEFAULT_TIERS, supervisor: { family: "openai", model: null, effort: null } },
    };
    const user = await renderPage();
    await user.click(screen.getByRole("button", { name: "Reset to defaults" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Defaults restored.");
    expect(within(tierGroup("Supervisor")).getByLabelText("Provider family")).toHaveValue("anthropic");
    expect(screen.getByText("Using defaults")).toBeInTheDocument();
  });

  it("explains a saved model that is no longer available", async () => {
    server.notice = "saved_model_unavailable";
    await renderPage();
    expect(screen.getByText(/A model you saved is no longer available/)).toHaveAttribute("role", "note");
  });

  it("explains, with different copy, saved settings that could not be read", async () => {
    server.notice = "saved_settings_unreadable";
    await renderPage();
    expect(screen.getByText(/saved settings could not be read/)).toHaveAttribute("role", "note");
    expect(screen.queryByText(/no longer available/)).not.toBeInTheDocument();
  });

  it("names the missing scope when the capability matrix is denied", async () => {
    stubFetch({
      capabilitiesStatus: 403,
      capabilitiesBody: { error: { code: "scope_denied", message: "Denied.", missing_scope: "tuning:read" } },
    });
    await loadRuntimeConfig();
    render(<SettingsPage />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("tuning:read");
  });
});
