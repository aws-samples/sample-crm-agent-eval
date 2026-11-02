// SPDX-License-Identifier: MIT
/**
 * The signed-in user's per-tier tuning: one `{family, model, effort}` selection per tier, persisted
 * server-side through `POST /api/user_settings` and mirrored in memory here.
 *
 * {@link loadUserSettings} fetches the saved settings once per page load, {@link saveUserSettings}
 * and {@link resetUserSettings} write them, and the store adopts the server's view only after the
 * server accepts it. The chat surface reads `getSessionTuning()` once per new turn, so a change
 * applies to the next turn and never to one already streaming. Nothing here touches browser
 * storage.
 *
 * {@link useTuningEditor} is the editing state shared by the settings page and the assistant
 * panel: a pending copy, client-side validation, and server rejections mapped onto the offending
 * control.
 */
import { useCallback, useState, useSyncExternalStore } from "react";

import type { CrmApiClient, UserSettingsView } from "../../api/CrmApiClient";
import { CrmApiError } from "../../api/CrmApiClient";
import { wireTuningBlock } from "../AssistantPanel/wireTuning";
import type { CapabilityMatrix, Family, Tier } from "./capabilityMatrix";
import { FAMILIES, TIERS } from "./capabilityMatrix";
import {
  TuningValidationError,
  controlForServerSource,
  validateTierSelection,
} from "./validateTuning";

/** One tier's selection. `null` in any field means "the default for this tier". */
export interface TierSelection {
  readonly family: Family | null;
  readonly model: string | null;
  readonly effort: string | null;
}

export interface SessionTuning {
  readonly tiers: Readonly<Record<Tier, TierSelection>>;
}

/**
 * Why the server returned defaults instead of what was saved: a saved model left the matrix, or
 * the saved item could not be read at all. Mirrors `crm.user_settings`' notice constants.
 */
export type SettingsNotice = NonNullable<UserSettingsView["notice"]>;

const SETTINGS_NOTICES: readonly SettingsNotice[] = [
  "saved_model_unavailable",
  "saved_settings_unreadable",
];

/** What each notice tells the user; the controls show the defaults in both cases. */
export const SETTINGS_NOTICE_TEXT: Readonly<Record<SettingsNotice, string>> = {
  saved_model_unavailable:
    "A model you saved is no longer available, so the defaults are in use. Choose a model and save to replace them.",
  saved_settings_unreadable:
    "Your saved settings could not be read, so the defaults are in use. Save to store new settings.",
};

/** Whether the saved settings have been read from the server, and what the server said. */
export type SettingsStatus =
  | { readonly status: "idle" }
  | { readonly status: "loading" }
  | {
      readonly status: "ready";
      readonly isDefault: boolean;
      readonly notice: SettingsNotice | null;
      readonly updatedAt: string | null;
    }
  | { readonly status: "error"; readonly message: string };

/** The three `CrmApiClient` methods this module calls. */
export type SettingsClient = Pick<
  CrmApiClient,
  "getUserSettings" | "saveUserSettings" | "resetUserSettings"
>;

export const EMPTY_TIER_SELECTION: TierSelection = {
  family: null,
  model: null,
  effort: null,
};

const EMPTY_TUNING: SessionTuning = {
  tiers: {
    supervisor: EMPTY_TIER_SELECTION,
    subagent: EMPTY_TIER_SELECTION,
    tool: EMPTY_TIER_SELECTION,
  },
};

interface StoreState {
  readonly tuning: SessionTuning;
  readonly settings: SettingsStatus;
}

const INITIAL_STATE: StoreState = { tuning: EMPTY_TUNING, settings: { status: "idle" } };

let state: StoreState = INITIAL_STATE;
let inFlightLoad: Promise<void> | null = null;
const listeners = new Set<() => void>();

function update(next: Partial<StoreState>): void {
  state = { ...state, ...next };
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function readField<T>(value: unknown, guard: (candidate: unknown) => candidate is T): T | null {
  return guard(value) ? value : null;
}

const isString = (value: unknown): value is string => typeof value === "string";
const isFamily = (value: unknown): value is Family =>
  typeof value === "string" && (FAMILIES as readonly string[]).includes(value);

/**
 * Read a wire block (as `/api/user_settings` returns it) into a {@link SessionTuning}.
 * A missing tier or a field of the wrong type reads as `null`, the tier default; any other field is
 * ignored.
 */
export function tuningFromWire(block: unknown): SessionTuning {
  const rawTiers =
    typeof block === "object" && block !== null && "tiers" in block
      ? (block as { tiers?: unknown }).tiers
      : undefined;
  const tiers = {} as Record<Tier, TierSelection>;
  for (const tier of TIERS) {
    const raw =
      typeof rawTiers === "object" && rawTiers !== null
        ? (rawTiers as Record<string, unknown>)[tier]
        : undefined;
    const fields = typeof raw === "object" && raw !== null ? (raw as Record<string, unknown>) : {};
    tiers[tier] = {
      family: readField(fields.family, isFamily),
      model: readField(fields.model, isString),
      effort: readField(fields.effort, isString),
    };
  }
  return { tiers };
}

function adoptView(view: UserSettingsView): void {
  update({
    tuning: tuningFromWire(view.tuning),
    settings: {
      status: "ready",
      isDefault: view.is_default,
      notice: SETTINGS_NOTICES.find((notice) => notice === (view.notice as unknown)) ?? null,
      updatedAt: view.updated_at,
    },
  });
}

/** Read the current tuning. */
export function getSessionTuning(): SessionTuning {
  return state.tuning;
}

/** Replace the in-memory tuning without writing it to the server (tests and previews only). */
export function setSessionTuning(next: SessionTuning): void {
  update({ tuning: next });
}

/** Forget every in-memory selection; the next turn then carries no tuning block. */
export function resetSessionTuning(): void {
  update({ tuning: EMPTY_TUNING });
}

/**
 * Fetch the saved settings once per page load. Later calls resolve with the first call's result;
 * a failed load may be retried.
 */
export function loadUserSettings(client: SettingsClient): Promise<void> {
  if (state.settings.status === "ready") return Promise.resolve();
  if (inFlightLoad !== null) return inFlightLoad;
  update({ settings: { status: "loading" } });
  inFlightLoad = client
    .getUserSettings()
    .then(adoptView)
    .catch((err: unknown) => {
      update({
        settings: {
          status: "error",
          message: err instanceof Error ? err.message : "Could not load your saved settings.",
        },
      });
    })
    .finally(() => {
      inFlightLoad = null;
    });
  return inFlightLoad;
}

/** Save `tuning` to the server and adopt the server's view. Rejects with the server's error. */
export async function saveUserSettings(client: SettingsClient, tuning: SessionTuning): Promise<void> {
  adoptView(await client.saveUserSettings({ tuning: wireTuningBlock(tuning) }));
}

/** Delete the saved settings and adopt the server's defaults. Rejects with the server's error. */
export async function resetUserSettings(client: SettingsClient): Promise<void> {
  adoptView(await client.resetUserSettings());
}

/** React hook binding a component to the current tuning, re-rendering on change. */
export function useSessionTuning(): [SessionTuning, (next: SessionTuning) => void] {
  const value = useSyncExternalStore(subscribe, getSessionTuning);
  const set = useCallback((next: SessionTuning) => setSessionTuning(next), []);
  return [value, set];
}

/** React hook binding a component to the saved-settings load status. */
export function useSettingsStatus(): SettingsStatus {
  return useSyncExternalStore(subscribe, () => state.settings);
}

/** A rejected control, as shown inside that tier's fieldset. */
export interface FieldError {
  readonly control: string;
  readonly message: string;
}

/** The outcome of the last editor action, for the status line under the buttons. */
export type EditorOutcome =
  | { readonly kind: "none" }
  | { readonly kind: "saved" }
  | { readonly kind: "reset" }
  | { readonly kind: "rejected" }
  | { readonly kind: "failed"; readonly message: string };

export interface TuningEditor {
  readonly pending: SessionTuning;
  readonly errorsByTier: Partial<Record<Tier, FieldError>>;
  readonly outcome: EditorOutcome;
  readonly busy: boolean;
  readonly setTier: (tier: Tier, selection: TierSelection) => void;
  readonly save: () => Promise<void>;
  readonly discard: () => void;
  readonly resetToDefaults: () => Promise<void>;
}

/** Validate every tier of `pending`, returning one error per offending tier. */
export function validatePendingTuning(
  matrix: CapabilityMatrix,
  pending: SessionTuning,
): Partial<Record<Tier, FieldError>> {
  const errors: Partial<Record<Tier, FieldError>> = {};
  for (const tier of TIERS) {
    try {
      validateTierSelection(matrix, tier, pending.tiers[tier]);
    } catch (err) {
      if (!(err instanceof TuningValidationError)) throw err;
      errors[tier] = { control: err.control, message: err.message };
    }
  }
  return errors;
}

/**
 * Editing state over the stored tuning. `save` validates client-side, then writes; a server
 * `validation_failed` naming `tuning.tiers.<tier>.<field>` lands on that control, any other failure
 * becomes `outcome.failed`. The stored tuning changes only when the server accepts.
 */
export function useTuningEditor(matrix: CapabilityMatrix, client: SettingsClient): TuningEditor {
  const [stored] = useSessionTuning();
  const [draft, setDraft] = useState<SessionTuning | null>(null);
  const [errorsByTier, setErrorsByTier] = useState<Partial<Record<Tier, FieldError>>>({});
  const [outcome, setOutcome] = useState<EditorOutcome>({ kind: "none" });
  const [busy, setBusy] = useState(false);
  const pending = draft ?? stored;

  const setTier = useCallback(
    (tier: Tier, selection: TierSelection) => {
      setDraft({ tiers: { ...pending.tiers, [tier]: selection } });
      setOutcome({ kind: "none" });
    },
    [pending],
  );

  const save = useCallback(async () => {
    const clientErrors = validatePendingTuning(matrix, pending);
    setErrorsByTier(clientErrors);
    if (Object.keys(clientErrors).length > 0) {
      setOutcome({ kind: "rejected" });
      return;
    }
    setBusy(true);
    try {
      await saveUserSettings(client, pending);
      setDraft(null);
      setOutcome({ kind: "saved" });
    } catch (err) {
      const target =
        err instanceof CrmApiError && err.kind === "validation_failed"
          ? controlForServerSource(err.body.source)
          : undefined;
      if (target !== undefined && err instanceof CrmApiError) {
        setErrorsByTier({ [target.tier]: { control: target.control, message: err.body.message } });
        setOutcome({ kind: "rejected" });
      } else {
        setOutcome({
          kind: "failed",
          message: err instanceof Error ? err.message : "The settings could not be saved.",
        });
      }
    } finally {
      setBusy(false);
    }
  }, [client, matrix, pending]);

  const discard = useCallback(() => {
    setDraft(null);
    setErrorsByTier({});
    setOutcome({ kind: "none" });
  }, []);

  const resetToDefaults = useCallback(async () => {
    setBusy(true);
    setErrorsByTier({});
    try {
      await resetUserSettings(client);
      setDraft(null);
      setOutcome({ kind: "reset" });
    } catch (err) {
      setOutcome({
        kind: "failed",
        message: err instanceof Error ? err.message : "The settings could not be reset.",
      });
    } finally {
      setBusy(false);
    }
  }, [client]);

  return { pending, errorsByTier, outcome, busy, setTier, save, discard, resetToDefaults };
}

/** Test-only: restore the module to its initial state between test cases. */
export function __resetSessionTuningForTests(): void {
  state = INITIAL_STATE;
  inFlightLoad = null;
  listeners.clear();
}
