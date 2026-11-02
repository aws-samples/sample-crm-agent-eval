// SPDX-License-Identifier: MIT
/**
 * Settings page: the signed-in user's saved model tuning. Renders one {@link TuningControls}
 * fieldset per tier (supervisor, subagent, tool) against the fetched capability matrix. "Save
 * settings" validates client-side (`../components/tuning/validateTuning.ts`), then writes through
 * `POST /api/user_settings`; a value the server rejects is shown at the offending control and the
 * saved settings stay as they were. "Reset to defaults" deletes the saved settings.
 *
 * Saved settings apply from the user's next message. A turn already streaming continues under the
 * tuning in effect when it started.
 */
import { useEffect } from "react";
import type { JSX } from "react";

import { useCrmApi } from "../../api/useCrmApi";
import { useAuth } from "../../auth/AuthProvider";
import { getRuntimeConfig } from "../../config/runtimeConfig";
import { TIERS, type CapabilityMatrix } from "../../components/tuning/capabilityMatrix";
import { TuningControls } from "../../components/tuning/TuningControls";
import { useCapabilityMatrix } from "../../components/tuning/useCapabilityMatrix";
import {
  SETTINGS_NOTICE_TEXT,
  loadUserSettings,
  useSettingsStatus,
  useTuningEditor,
  type SettingsClient,
} from "../../components/tuning/sessionTuning";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";

/** The page's own container rhythm, shared by every state this page can render. */
const pageShell = "mx-auto w-full max-w-content space-y-7 px-6 py-8";

const pageHeading = "text-2xl font-semibold tracking-tight text-text-primary";

export function SettingsPage(): JSX.Element {
  // /api/capabilities needs the same `Authorization: Bearer` token as CrmApiClient calls, taken
  // from `useAuth()` since this route is outside CrmApiClient's tool surface.
  const auth = useAuth();
  const client = useCrmApi();
  const { apiBaseUrl } = getRuntimeConfig();
  const matrixState = useCapabilityMatrix(apiBaseUrl, auth);
  const settings = useSettingsStatus();

  useEffect(() => {
    void loadUserSettings(client);
  }, [client]);

  if (matrixState.status === "loading" || settings.status === "loading" || settings.status === "idle") {
    return (
      <main aria-busy="true" aria-live="polite" className={pageShell}>
        <h1 className={pageHeading}>Model tuning</h1>
        <div className="space-y-5">
          {TIERS.map((tier) => (
            <div
              key={tier}
              className="space-y-4 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6"
            >
              <Skeleton label="Loading model capabilities" className="h-4 w-1/4" />
              <div className="flex flex-wrap gap-5">
                <Skeleton label="Loading provider family control" className="h-control-md w-52" />
                <Skeleton label="Loading effort control" className="h-control-md w-52" />
              </div>
            </div>
          ))}
        </div>
      </main>
    );
  }

  if (matrixState.status === "error") {
    return (
      <main className={pageShell}>
        <h1 className={pageHeading}>Model tuning</h1>
        {/* A `scope_denied` is distinguished by naming the missing scope in the text, inside this
            one `role="alert"`. */}
        <div className="max-w-prose space-y-3 rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-4">
          <p role="alert" className="text-sm leading-relaxed text-danger-text">
            Could not load model capabilities: {matrixState.error}
            {matrixState.missingScope !== undefined
              ? ` (missing scope: ${matrixState.missingScope})`
              : ""}
          </p>
          <p className="text-xs leading-relaxed text-text-secondary">
            Tuning is unavailable until the capability matrix loads. Your saved tuning still applies
            to your messages.
          </p>
        </div>
      </main>
    );
  }

  return <SettingsEditor matrix={matrixState.matrix} client={client} />;
}

function SettingsEditor({
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
    <main className={pageShell}>
      <header className="flex flex-wrap items-center gap-4">
        <h1 className={pageHeading}>Model tuning</h1>
        {settings.status === "ready" ? (
          <Badge variant="info">{settings.isDefault ? "Using defaults" : "Saved to your account"}</Badge>
        ) : null}
      </header>

      <p className="max-w-prose rounded-surface border border-hairline border-info-border bg-info-surface px-5 py-4 text-sm leading-relaxed text-info-text">
        Each tier can run any model of its provider family. Saved settings follow your account and
        apply from your next message; a reply already in progress keeps the settings it started
        with.
      </p>

      {settings.status === "error" ? (
        <p
          role="alert"
          className="max-w-prose rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-3 text-sm text-danger-text"
        >
          Could not load your saved settings: {settings.message}. The controls show the defaults.
        </p>
      ) : settings.status === "ready" && settings.notice !== null ? (
        <p role="note" className="max-w-prose rounded-surface border border-hairline border-info-border bg-info-surface px-5 py-3 text-sm text-info-text">
          {SETTINGS_NOTICE_TEXT[settings.notice]}
        </p>
      ) : null}

      <div className="space-y-5">
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

      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" disabled={editor.busy} onClick={() => void editor.save()}>
          Save settings
        </Button>
        <Button type="button" variant="secondary" disabled={editor.busy} onClick={editor.discard}>
          Discard changes
        </Button>
        <Button
          type="button"
          variant="secondary"
          disabled={editor.busy}
          onClick={() => void editor.resetToDefaults()}
        >
          Reset to defaults
        </Button>
      </div>

      {/* The rejected control also carries `aria-invalid` and an inline message in its fieldset. */}
      {outcome.kind === "saved" || outcome.kind === "reset" ? (
        <p
          role="status"
          className="max-w-prose rounded-surface border border-hairline border-positive-border bg-positive-surface px-5 py-3 text-sm text-positive-text"
        >
          {outcome.kind === "saved"
            ? "Settings saved. They apply from your next message."
            : "Defaults restored. They apply from your next message."}
        </p>
      ) : null}
      {outcome.kind === "rejected" || outcome.kind === "failed" ? (
        <p
          role="alert"
          className="max-w-prose rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-3 text-sm text-danger-text"
        >
          {outcome.kind === "rejected"
            ? "A setting was rejected. Change the highlighted control, then save again."
            : `Settings not saved: ${outcome.message}`}
        </p>
      ) : null}
    </main>
  );
}
