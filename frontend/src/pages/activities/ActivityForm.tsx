// SPDX-License-Identifier: MIT
/**
 * Activity create form: logs a `call | email | meeting | note | stage_change | finding` activity
 * against one opportunity. A `stage_change` activity is a log entry only and does not change the
 * opportunity's `stage`.
 */
import { useId, useState } from "react";
import type { FormEvent, JSX } from "react";

import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { cn, focusRing } from "../../components/ui/utils";
import { ACTIVITY_KINDS } from "../../types/domain";
import type { ActivityKind } from "../../types/domain";
import { CrmApiError } from "../../api/CrmApiClient";

/** Field label styling, shared by every control on this form. */
const fieldLabel = "text-xs font-medium tracking-wide text-text-secondary";

const selectControl = cn(
  "h-control-md w-full rounded-control border border-hairline border-border-control bg-surface-raised px-4 text-sm text-text-primary",
  focusRing,
);
const textArea = cn(
  "min-h-24 w-full rounded-control border border-hairline border-border-control bg-surface-raised px-4 py-3 text-sm text-text-primary",
  focusRing,
);

export interface ActivityFormProps {
  readonly onSubmit: (input: {
    kind: ActivityKind;
    subject: string;
    body?: string;
    occurred_at?: string;
  }) => Promise<unknown>;
  readonly onCancel?: () => void;
}

export function ActivityForm({ onSubmit, onCancel }: ActivityFormProps): JSX.Element {
  const [kind, setKind] = useState<ActivityKind>("call");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | undefined>(undefined);

  const kindId = useId();
  const subjectId = useId();
  const bodyId = useId();

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!subject.trim()) {
      setError("Subject is required.");
      return;
    }
    setSubmitting(true);
    setError(undefined);
    try {
      await onSubmit({ kind, subject: subject.trim(), body: body.trim() || undefined });
      setSubject("");
      setBody("");
    } catch (err) {
      setError(err instanceof CrmApiError ? err.body.message : "Failed to log activity.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      aria-label="Log activity"
      className="grid gap-4 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6"
    >
      <div className="flex flex-col gap-2">
        <label htmlFor={kindId} className={fieldLabel}>Kind</label>
        <select
          id={kindId}
          value={kind}
          onChange={(event) => setKind(event.target.value as ActivityKind)}
          className={selectControl}
        >
          {ACTIVITY_KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </div>
      <div className="flex flex-col gap-2">
        <label htmlFor={subjectId} className={fieldLabel}>Subject</label>
        <Input
          id={subjectId}
          type="text"
          value={subject}
          onChange={(event) => setSubject(event.target.value)}
          required
        />
      </div>
      <div className="flex flex-col gap-2">
        <label htmlFor={bodyId} className={fieldLabel}>Notes</label>
        <textarea
          id={bodyId}
          value={body}
          onChange={(event) => setBody(event.target.value)}
          className={textArea}
        />
      </div>
      {error && (
        <p
          role="alert"
          className="rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-3 text-sm text-danger-text"
        >
          {error}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" variant="primary" disabled={submitting}>
          {submitting ? "Logging…" : "Log activity"}
        </Button>
        {onCancel && (
          <Button type="button" variant="ghost" onClick={onCancel} disabled={submitting}>
            Cancel
          </Button>
        )}
      </div>
    </form>
  );
}
