// SPDX-License-Identifier: MIT
/**
 * One tier's tuning controls, driven entirely by the fetched capability matrix: provider family,
 * model (any model of that family), and effort.
 *
 * Effort is always rendered. When the selected model cannot take it, the control is disabled and a
 * `role="note"` says why. Changing the family or model clears the fields the new model may not
 * accept, so a stale value never travels. A server rejection naming a field with no control here
 * ({@link TUNING_CONTROL_TIER}) is shown at the top of the tier's fieldset.
 */
import { useId, useMemo } from "react";
import type { JSX } from "react";

import { cn, focusRing } from "../ui/utils";

import type { CapabilityMatrix, Family, Tier } from "./capabilityMatrix";
import {
  FAMILIES,
  effortOptionsFor,
  modelFor,
  modelsOfFamily,
  resolvedEffort,
} from "./capabilityMatrix";
import { EMPTY_TIER_SELECTION, type FieldError, type TierSelection } from "./sessionTuning";
import {
  TUNING_CONTROL_EFFORT,
  TUNING_CONTROL_FAMILY,
  TUNING_CONTROL_MODEL,
  TUNING_CONTROL_TIER,
} from "./validateTuning";

export interface TuningControlsProps {
  readonly matrix: CapabilityMatrix;
  readonly tier: Tier;
  readonly selection: TierSelection;
  readonly onChange: (next: TierSelection) => void;
  /** Present when the last save was rejected for this tier; names the offending control. */
  readonly error?: FieldError;
  /** Disables every control, e.g. while a save is in flight. */
  readonly disabled?: boolean;
}

/** Field label styling, shared by every control here. */
const fieldLabel = "text-xs font-medium tracking-wide text-text-secondary";

const selectControl = cn(
  "h-control-md w-full rounded-control border border-hairline border-border-control bg-surface-raised px-4 text-sm text-text-primary disabled:cursor-not-allowed disabled:opacity-60",
  focusRing,
);

/**
 * A field-level message: `--danger-*` for an invalid value the user must change, `--info-*` for a
 * capability note ("this model does not accept effort").
 */
const fieldError =
  "rounded-control border border-hairline border-danger-border bg-danger-surface px-4 py-2 text-xs text-danger-text";
const fieldNote =
  "rounded-control border border-hairline border-info-border bg-info-surface px-4 py-2 text-xs text-info-text";

export function TuningControls({
  matrix,
  tier,
  selection,
  onChange,
  error,
  disabled = false,
}: TuningControlsProps): JSX.Element {
  const familyId = useId();
  const modelId = useId();
  const effortId = useId();
  const familyErrorId = useId();
  const modelErrorId = useId();
  const effortMessageId = useId();

  const family = selection.family ?? matrix.defaultFamily;
  const familyModels = useMemo(() => modelsOfFamily(matrix, family), [matrix, family]);
  const model = useMemo(
    () =>
      (selection.model !== null ? matrix.models[selection.model] : undefined) ??
      modelFor(matrix, family, tier),
    [matrix, family, tier, selection.model],
  );
  const effortOptions = effortOptionsFor(model);
  const effort = resolvedEffort(matrix, family, tier, model, selection.effort);

  const familyErrorVisible = error?.control === TUNING_CONTROL_FAMILY;
  const modelErrorVisible = error?.control === TUNING_CONTROL_MODEL;
  const effortErrorVisible = error?.control === TUNING_CONTROL_EFFORT;
  const tierErrorVisible = error?.control === TUNING_CONTROL_TIER;

  return (
    <fieldset
      disabled={disabled}
      className="space-y-4 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6"
    >
      <legend className="px-2 text-sm font-semibold tracking-tight text-text-primary">
        {tierLabel(tier)} model tuning
      </legend>

      {tierErrorVisible ? (
        <p role="alert" className={fieldError}>
          {error.message}
        </p>
      ) : null}

      <div className="flex flex-wrap gap-4">
        <div className="flex grow basis-52 flex-col gap-2">
          <label htmlFor={familyId} className={fieldLabel}>
            Provider family
          </label>
          <select
            id={familyId}
            value={family}
            aria-invalid={familyErrorVisible || undefined}
            aria-describedby={familyErrorVisible ? familyErrorId : undefined}
            onChange={(e) => onChange({ ...EMPTY_TIER_SELECTION, family: e.target.value as Family })}
            className={selectControl}
          >
            {FAMILIES.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
          {familyErrorVisible ? (
            <p id={familyErrorId} role="alert" className={fieldError}>
              {error.message}
            </p>
          ) : null}
        </div>

        <div className="flex grow basis-72 flex-col gap-2">
          <label htmlFor={modelId} className={fieldLabel}>
            Model
          </label>
          <select
            id={modelId}
            value={model.modelId}
            aria-invalid={modelErrorVisible || undefined}
            aria-describedby={modelErrorVisible ? modelErrorId : undefined}
            onChange={(e) =>
              onChange({ ...EMPTY_TIER_SELECTION, family: selection.family, model: e.target.value })
            }
            className={cn(selectControl, "font-numeric")}
          >
            {familyModels.map((candidate) => (
              <option key={candidate.modelId} value={candidate.modelId}>
                {candidate.modelId}
              </option>
            ))}
          </select>
          {modelErrorVisible ? (
            <p id={modelErrorId} role="alert" className={fieldError}>
              {error.message}
            </p>
          ) : null}
        </div>

        <div className="flex grow basis-52 flex-col gap-2">
          <label htmlFor={effortId} className={fieldLabel}>
            Effort
          </label>
          <select
            id={effortId}
            value={effort ?? ""}
            disabled={effortOptions === undefined}
            aria-invalid={effortErrorVisible || undefined}
            aria-describedby={effortOptions === undefined || effortErrorVisible ? effortMessageId : undefined}
            onChange={(e) => onChange({ ...selection, effort: e.target.value })}
            className={selectControl}
          >
            {effortOptions === undefined ? (
              <option value="">Not available</option>
            ) : (
              effortOptions.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))
            )}
          </select>
          {effortOptions === undefined ? (
            <p id={effortMessageId} role="note" className={fieldNote}>
              {model.modelId} does not accept an effort parameter.
            </p>
          ) : effortErrorVisible ? (
            <p id={effortMessageId} role="alert" className={fieldError}>
              {error.message}
            </p>
          ) : null}
        </div>

      </div>
    </fieldset>
  );
}

function tierLabel(tier: Tier): string {
  switch (tier) {
    case "supervisor":
      return "Supervisor";
    case "subagent":
      return "Subagent";
    case "tool":
      return "Tool execution";
  }
}
