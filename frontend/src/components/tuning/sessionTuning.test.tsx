// SPDX-License-Identifier: MIT
/**
 * Tests for the server-backed tuning store and its shared editor: the store adopts only what the
 * server accepted, a server rejection lands on the control its `source` names, and nothing touches
 * browser storage.
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CrmApiError, type UserSettingsView } from "../../api/CrmApiClient";
import { parseCapabilityMatrix } from "./capabilityMatrix";
import {
  EMPTY_TIER_SELECTION,
  SETTINGS_NOTICE_TEXT,
  __resetSessionTuningForTests,
  getSessionTuning,
  loadUserSettings,
  resetSessionTuning,
  resetUserSettings,
  saveUserSettings,
  setSessionTuning,
  tuningFromWire,
  useSessionTuning,
  useSettingsStatus,
  useTuningEditor,
  type SessionTuning,
  type SettingsClient,
} from "./sessionTuning";
import { TUNING_CONTROL_EFFORT, TUNING_CONTROL_MODEL, TUNING_CONTROL_TIER } from "./validateTuning";

const EMPTY: SessionTuning = {
  tiers: {
    supervisor: EMPTY_TIER_SELECTION,
    subagent: EMPTY_TIER_SELECTION,
    tool: EMPTY_TIER_SELECTION,
  },
};

function view(tiers: Record<string, unknown>, overrides: Partial<UserSettingsView> = {}): UserSettingsView {
  return {
    tuning: { schema: 3, source: "settings.tuning", tiers },
    is_default: false,
    notice: null,
    updated_at: "2026-10-08T00:00:00Z",
    ...overrides,
  };
}

const DEFAULTS_VIEW = view(
  {
    supervisor: { family: "anthropic", model: "anthropic.claude-opus-5-5", effort: "low" },
    subagent: { family: "anthropic", model: "anthropic.claude-sonnet-5-5", effort: "medium" },
    tool: { family: "anthropic", model: "anthropic.claude-haiku-5-5", effort: "low" },
  },
  { is_default: true, updated_at: null },
);

function client(overrides: Partial<SettingsClient> = {}): SettingsClient & {
  getUserSettings: ReturnType<typeof vi.fn>;
  saveUserSettings: ReturnType<typeof vi.fn>;
  resetUserSettings: ReturnType<typeof vi.fn>;
} {
  return {
    getUserSettings: vi.fn().mockResolvedValue(DEFAULTS_VIEW),
    saveUserSettings: vi.fn().mockImplementation(({ tuning }: { tuning: Record<string, unknown> }) =>
      Promise.resolve({ ...DEFAULTS_VIEW, tuning, is_default: false }),
    ),
    resetUserSettings: vi.fn().mockResolvedValue(DEFAULTS_VIEW),
    ...overrides,
  } as never;
}

function matrix() {
  const effort = (values: string[], dflt: string | null) => ({
    supported: values.length > 0,
    values,
    default: dflt,
    capped_at_when_thinking_disabled: null,
  });
  return parseCapabilityMatrix({
    default_family: "anthropic",
    models: {
      "anthropic.claude-opus-5-5": { model_id: "anthropic.claude-opus-5-5", family: "anthropic", tier: "supervisor", effort: effort(["low", "medium"], "medium") },
      "anthropic.claude-sonnet-5-5": { model_id: "anthropic.claude-sonnet-5-5", family: "anthropic", tier: "subagent", effort: effort(["low", "medium"], "medium") },
      "anthropic.claude-haiku-5-5": { model_id: "anthropic.claude-haiku-5-5", family: "anthropic", tier: "tool", effort: effort(["low", "medium", "high"], "medium") },
      "openai.gpt-6-astra": { model_id: "openai.gpt-6-astra", family: "openai", tier: "supervisor", effort: effort(["none", "low"], "low") },
      "openai.gpt-6.1-sol": { model_id: "openai.gpt-6.1-sol", family: "openai", tier: "subagent", effort: effort(["none", "low"], "low") },
      "openai.gpt-6-luna": { model_id: "openai.gpt-6-luna", family: "openai", tier: "tool", effort: effort(["none", "low"], "low") },
    },
    tiers: {
      anthropic: {
        supervisor: { model_id: "anthropic.claude-opus-5-5", tuning: {} },
        subagent: { model_id: "anthropic.claude-sonnet-5-5", tuning: {} },
        tool: { model_id: "anthropic.claude-haiku-5-5", tuning: {} },
      },
      openai: {
        supervisor: { model_id: "openai.gpt-6-astra", tuning: {} },
        subagent: { model_id: "openai.gpt-6.1-sol", tuning: {} },
        tool: { model_id: "openai.gpt-6-luna", tuning: {} },
      },
    },
  });
}

let storageWrites: string[] = [];

beforeEach(() => {
  __resetSessionTuningForTests();
  storageWrites = [];
  vi.spyOn(Storage.prototype, "setItem").mockImplementation((key: string) => {
    storageWrites.push(key);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  __resetSessionTuningForTests();
  expect(storageWrites, "the tuning store wrote to browser storage").toEqual([]);
});

describe("tuningFromWire", () => {
  it("reads every tier's three fields from a schema-3 block", () => {
    expect(tuningFromWire(DEFAULTS_VIEW.tuning).tiers.tool).toEqual({
      family: "anthropic",
      model: "anthropic.claude-haiku-5-5",
      effort: "low",
    });
  });

  it("ignores a field it does not model, so no removed parameter enters the store", () => {
    const tuning = tuningFromWire({
      tiers: { tool: { family: "openai", model: null, effort: "none", temperature: 0.4 } },
    });
    expect(tuning.tiers.tool).toEqual({ family: "openai", model: null, effort: "none" });
  });

  it("reads a missing tier, a wrong-typed field or an unknown family as the tier default", () => {
    const tuning = tuningFromWire({
      tiers: { supervisor: { family: "mistral", model: 7, effort: "low" } },
    });
    expect(tuning.tiers.supervisor).toEqual({ ...EMPTY_TIER_SELECTION, effort: "low" });
    expect(tuning.tiers.subagent).toEqual(EMPTY_TIER_SELECTION);
    expect(tuningFromWire(null)).toEqual(EMPTY);
  });
});

describe("the store", () => {
  it("starts empty and idle, with no settings loaded", () => {
    expect(getSessionTuning()).toEqual(EMPTY);
    const { result } = renderHook(() => useSettingsStatus());
    expect(result.current).toEqual({ status: "idle" });
  });

  it("loads the saved settings once, however many surfaces ask", async () => {
    const api = client();
    await Promise.all([loadUserSettings(api), loadUserSettings(api)]);
    await loadUserSettings(api);
    expect(api.getUserSettings).toHaveBeenCalledTimes(1);
    expect(getSessionTuning().tiers.supervisor.model).toBe("anthropic.claude-opus-5-5");
  });

  it("records a failed load as an error and allows a retry", async () => {
    const api = client({ getUserSettings: vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(DEFAULTS_VIEW) });
    const { result } = renderHook(() => useSettingsStatus());
    await act(() => loadUserSettings(api));
    expect(result.current).toEqual({ status: "error", message: "offline" });
    expect(getSessionTuning()).toEqual(EMPTY);
    await act(() => loadUserSettings(api));
    expect(result.current).toMatchObject({ status: "ready", isDefault: true });
  });

  it("surfaces the saved_model_unavailable notice from the server", async () => {
    const api = client({ getUserSettings: vi.fn().mockResolvedValue({ ...DEFAULTS_VIEW, notice: "saved_model_unavailable" }) });
    const { result } = renderHook(() => useSettingsStatus());
    await act(() => loadUserSettings(api));
    expect(result.current).toMatchObject({ status: "ready", notice: "saved_model_unavailable" });
  });

  it("keeps both known notices and drops an unknown one", async () => {
    for (const [sent, kept] of [
      ["saved_settings_unreadable", "saved_settings_unreadable"],
      ["something_new", null],
    ] as const) {
      __resetSessionTuningForTests();
      const api = client({ getUserSettings: vi.fn().mockResolvedValue({ ...DEFAULTS_VIEW, notice: sent }) });
      const { result } = renderHook(() => useSettingsStatus());
      await act(() => loadUserSettings(api));
      expect(result.current).toMatchObject({ status: "ready", notice: kept });
    }
  });

  it("gives the two notices distinct copy", () => {
    expect(SETTINGS_NOTICE_TEXT.saved_model_unavailable).not.toEqual(
      SETTINGS_NOTICE_TEXT.saved_settings_unreadable,
    );
  });

  it("saves the schema-3 block, with no field beyond family, model and effort, and adopts the server's view", async () => {
    const api = client();
    const next: SessionTuning = {
      tiers: { ...EMPTY.tiers, subagent: { ...EMPTY_TIER_SELECTION, family: "openai" } },
    };
    await saveUserSettings(api, next);
    const sent = api.saveUserSettings.mock.calls[0][0] as { tuning: Record<string, unknown> };
    expect(sent.tuning).toMatchObject({ schema: 3, tiers: { subagent: { family: "openai" } } });
    for (const tier of Object.values(sent.tuning.tiers as Record<string, object>)) {
      expect(Object.keys(tier)).toEqual(["family", "model", "effort"]);
    }
    expect(getSessionTuning()).toEqual(next);
  });

  it("keeps the stored tuning when the server rejects a save", async () => {
    setSessionTuning(tuningFromWire(DEFAULTS_VIEW.tuning));
    const before = getSessionTuning();
    const api = client({
      saveUserSettings: vi.fn().mockRejectedValue(
        new CrmApiError("validation_failed", 400, { message: "no", source: "tuning.tiers.tool.model" }),
      ),
    });
    await expect(saveUserSettings(api, EMPTY)).rejects.toBeInstanceOf(CrmApiError);
    expect(getSessionTuning()).toBe(before);
  });

  it("reset adopts the server's defaults", async () => {
    setSessionTuning({ tiers: { ...EMPTY.tiers, tool: { ...EMPTY_TIER_SELECTION, effort: "high" } } });
    await resetUserSettings(client());
    expect(getSessionTuning().tiers.tool.effort).toBe("low");
  });

  it("resetSessionTuning clears every in-memory selection", () => {
    setSessionTuning(tuningFromWire(DEFAULTS_VIEW.tuning));
    resetSessionTuning();
    expect(getSessionTuning()).toEqual(EMPTY);
  });

  it("re-renders a subscribed component on change", () => {
    const { result } = renderHook(() => useSessionTuning());
    act(() => setSessionTuning(tuningFromWire(DEFAULTS_VIEW.tuning)));
    expect(result.current[0].tiers.subagent.effort).toBe("medium");
  });
});

describe("useTuningEditor", () => {
  it("edits a draft without touching the store until a save succeeds", async () => {
    const api = client();
    const { result } = renderHook(() => useTuningEditor(matrix(), api));
    act(() => result.current.setTier("tool", { ...EMPTY_TIER_SELECTION, effort: "high" }));
    expect(result.current.pending.tiers.tool.effort).toBe("high");
    expect(getSessionTuning()).toEqual(EMPTY);
    await act(() => result.current.save());
    expect(result.current.outcome).toEqual({ kind: "saved" });
    expect(getSessionTuning().tiers.tool.effort).toBe("high");
  });

  it("rejects client-side before any request, naming the tier and control", async () => {
    const api = client();
    const { result } = renderHook(() => useTuningEditor(matrix(), api));
    act(() => result.current.setTier("tool", { ...EMPTY_TIER_SELECTION, effort: "xhigh" }));
    await act(() => result.current.save());
    expect(api.saveUserSettings).not.toHaveBeenCalled();
    expect(result.current.outcome).toEqual({ kind: "rejected" });
    expect(result.current.errorsByTier.tool?.control).toBe(TUNING_CONTROL_EFFORT);
  });

  it("puts a server rejection's message at the control its source names", async () => {
    const api = client({
      saveUserSettings: vi.fn().mockRejectedValue(
        new CrmApiError("validation_failed", 400, {
          message: "Unknown model 'x' for the subagent tier.",
          source: "tuning.tiers.subagent.model",
        }),
      ),
    });
    const { result } = renderHook(() => useTuningEditor(matrix(), api));
    await act(() => result.current.save());
    expect(result.current.errorsByTier).toEqual({
      subagent: { control: TUNING_CONTROL_MODEL, message: "Unknown model 'x' for the subagent tier." },
    });
    expect(result.current.outcome).toEqual({ kind: "rejected" });
  });

  it("puts a server refusal naming a removed field at that tier, not in a failed-save line", async () => {
    const message = "temperature is no longer a tuning parameter; remove it from the tool tier.";
    const api = client({
      saveUserSettings: vi.fn().mockRejectedValue(
        new CrmApiError("validation_failed", 400, { message, source: "tuning.tiers.tool.temperature" }),
      ),
    });
    const { result } = renderHook(() => useTuningEditor(matrix(), api));
    await act(() => result.current.save());
    expect(result.current.errorsByTier).toEqual({ tool: { control: TUNING_CONTROL_TIER, message } });
    expect(result.current.outcome).toEqual({ kind: "rejected" });
  });

  it("reports a failure with no control as a failed save", async () => {
    const api = client({
      saveUserSettings: vi.fn().mockRejectedValue(new CrmApiError("scope_denied", 403, { message: "Only a signed-in user can change tuning settings." })),
    });
    const { result } = renderHook(() => useTuningEditor(matrix(), api));
    await act(() => result.current.save());
    expect(result.current.outcome).toEqual({
      kind: "failed",
      message: "Only a signed-in user can change tuning settings.",
    });
    expect(result.current.errorsByTier).toEqual({});
  });

  it("discard returns the draft to the stored tuning", () => {
    const { result } = renderHook(() => useTuningEditor(matrix(), client()));
    act(() => result.current.setTier("tool", { ...EMPTY_TIER_SELECTION, effort: "high" }));
    act(() => result.current.discard());
    expect(result.current.pending).toEqual(EMPTY);
  });

  it("reset to defaults adopts the server's defaults and clears the draft", async () => {
    const api = client();
    const { result } = renderHook(() => useTuningEditor(matrix(), api));
    act(() => result.current.setTier("tool", { ...EMPTY_TIER_SELECTION, effort: "high" }));
    await act(() => result.current.resetToDefaults());
    expect(api.resetUserSettings).toHaveBeenCalledTimes(1);
    expect(result.current.outcome).toEqual({ kind: "reset" });
    await waitFor(() => expect(result.current.pending.tiers.tool.effort).toBe("low"));
  });
});
