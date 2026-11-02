// SPDX-License-Identifier: MIT
/**
 * The user's saved model tuning, editable from inside the assistant panel, reusing
 * `components/tuning/TuningControls` and the same `useTuningEditor` as the settings page.
 *
 * Collapsed by default behind a native `<details>`/`<summary>`. Collapsed `<details>` content is
 * excluded from the accessibility tree, so an axe check must expand it and open the panel.
 *
 * Mounting it loads the saved settings, so the chat composer beside it sends them from the first
 * turn. A save writes them to the user's account and applies from the next message.
 */

import { useEffect } from "react";
import type { JSX } from "react";

import { useCrmApi } from "../../api/useCrmApi";
import { useAuth } from "../../auth/AuthProvider";
import { getRuntimeConfig } from "../../config/runtimeConfig";
import type { CapabilityMatrix } from "../tuning/capabilityMatrix";
import { TIERS } from "../tuning/capabilityMatrix";
import { TuningControls } from "../tuning/TuningControls";
import { useCapabilityMatrix } from "../tuning/useCapabilityMatrix";
import {
  SETTINGS_NOTICE_TEXT,
  loadUserSettings,
  useSettingsStatus,
  useTuningEditor,
  type SettingsClient,
} from "../tuning/sessionTuning";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";

/**
 * Renders nothing until the runtime config is available, since `getRuntimeConfig()` throws before
 * `loadRuntimeConfig()` resolves.
 */
export function PanelTuning(): JSX.Element | null {
  let apiBaseUrl: string;
  try {
    apiBaseUrl = getRuntimeConfig().apiBaseUrl;
  } catch {
    return null;
  }
  return <PanelTuningReady apiBaseUrl={apiBaseUrl} />;
}

function PanelTuningReady({ apiBaseUrl }: { apiBaseUrl: string }): JSX.Element {
  const auth = useAuth();
  const client = useCrmApi();
  const matrixState = useCapabilityMatrix(apiBaseUrl, auth);
  const settings = useSettingsStatus();

  useEffect(() => {
    void loadUserSettings(client);
  }, [client]);

  if (matrixState.status === "loading" || settings.status === "loading" || settings.status === "idle") {
    return (
      <section aria-busy="true" aria-live="polite" className="space-y-4">
        <h3 className="text-xs font-semibold text-text-secondary">Model tuning</h3>
        <Skeleton label="Loading model capabilities" className="h-4 w-2/5" />
        <Skeleton label="Loading tuning controls" className="h-control-md w-full" />
      </section>
    );
  }

  if (matrixState.status === "error") {
    return (
      <section className="space-y-3">
        <h3 className="text-xs font-semibold text-text-secondary">Model tuning</h3>
        {/*
          `--danger-*` for every load failure. A `scope_denied` lands here too; the text names its
          `missingScope`, inside this one `role="alert"`.
        */}
        <p
          role="alert"
          className="rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-3 text-xs leading-relaxed text-danger-text"
        >
          Couldn&rsquo;t load model capabilities: {matrixState.error}
          {matrixState.missingScope !== undefined
            ? ` (missing scope: ${matrixState.missingScope})`
            : ""}
          . Your saved tuning still applies to your messages.
        </p>
      </section>
    );
  }

  return <PanelTuningEditor matrix={matrixState.matrix} client={client} />;
}

function PanelTuningEditor({
  matrix,
  client,
}: {
  matrix: CapabilityMatrix;
  client: SettingsClient;
}): JSX.Element {
  const editor = useTuningEditor(matrix, client);
  const settings = useSettingsStatus();
  const { outcome } = editor;

  return (
    <details data-testid="panel-tuning" className="border-t-hairline border-border-subtle pt-5">
      <summary className="cursor-pointer list-none rounded-control text-xs font-semibold text-text-secondary outline-none hover:text-text-primary focus-visible:ring-focus focus-visible:ring-focus-ring focus-visible:ring-offset-focus focus-visible:ring-offset-surface-raised">
        Model tuning
      </summary>
      <p className="mt-4 text-2xs leading-relaxed text-text-muted">
        Saved to your account and used from your next message. A reply already in progress keeps
        the settings it started with.
      </p>

      {settings.status === "error" ? (
        <p
          role="alert"
          className="mt-4 rounded-surface border border-hairline border-danger-border bg-danger-surface px-4 py-2 text-2xs text-danger-text"
        >
          Couldn&rsquo;t load your saved settings: {settings.message}. The controls show the
          defaults.
        </p>
      ) : settings.status === "ready" && settings.notice !== null ? (
        <p role="note" className="mt-4 rounded-surface border border-hairline border-info-border bg-info-surface px-4 py-2 text-2xs text-info-text">
          {SETTINGS_NOTICE_TEXT[settings.notice]}
        </p>
      ) : null}

      <div className="mt-4 space-y-4">
        {TIERS.map((tier) => (
          <TuningControls
            key={tier}
            matrix={matrix}
            tier={tier}
            selection={editor.pending.tiers[tier]}
            onChange={(next) => editor.setTier(tier, next)}
            error={editor.errorsByTier[tier]}
            disabled={editor.busy}
          />
        ))}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button type="button" size="sm" disabled={editor.busy} onClick={() => void editor.save()}>
          Save settings
        </Button>
        <Button type="button" variant="secondary" size="sm" disabled={editor.busy} onClick={editor.discard}>
          Discard changes
        </Button>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          disabled={editor.busy}
          onClick={() => void editor.resetToDefaults()}
        >
          Reset to defaults
        </Button>
      </div>

      {outcome.kind === "saved" || outcome.kind === "reset" ? (
        <p
          role="status"
          className="mt-4 rounded-surface border border-hairline border-positive-border bg-positive-surface px-4 py-2 text-2xs text-positive-text"
        >
          {outcome.kind === "saved"
            ? "Settings saved. They apply from your next message."
            : "Defaults restored. They apply from your next message."}
        </p>
      ) : null}
      {outcome.kind === "rejected" || outcome.kind === "failed" ? (
        <p
          role="alert"
          className="mt-4 rounded-surface border border-hairline border-danger-border bg-danger-surface px-4 py-2 text-2xs text-danger-text"
        >
          {outcome.kind === "rejected"
            ? "A setting was rejected. Change the highlighted control, then save again."
            : `Settings not saved: ${outcome.message}`}
        </p>
      ) : null}
    </details>
  );
}
