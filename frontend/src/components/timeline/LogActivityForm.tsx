// SPDX-License-Identifier: MIT
/**
 * Log-activity form shown alongside the timeline on an opportunity detail page. `log_activity`
 * commits directly and updates the parent opportunity's `last_touch`.
 */
import { useId, useState, type FormEvent, type JSX } from "react";

import { useCrmApi } from "../../api/useCrmApi";
import { newTransactionKey } from "../../lib/randomToken";
import { ACTIVITY_KINDS, type ActivityKind } from "../../types/domain";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { cn, focusRing } from "../ui/utils";
import { ApiErrorBanner } from "../../pages/opportunities/ApiErrorBanner";

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

export function LogActivityForm({
  opportunityId,
  onLogged,
}: {
  opportunityId: string;
  onLogged: () => void;
}): JSX.Element {
  const api = useCrmApi();
  const [kind, setKind] = useState<ActivityKind>("note");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [error, setError] = useState<unknown>(undefined);
  const [submitting, setSubmitting] = useState(false);

  const kindId = useId();
  const subjectId = useId();
  const bodyId = useId();

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError(undefined);
    setSubmitting(true);
    try {
      await api.logActivity({
        opportunity_id: opportunityId,
        kind,
        subject,
        body: body.trim() === "" ? undefined : body,
        transaction_key: newTransactionKey(),
      });
      setSubject("");
      setBody("");
      onLogged();
    } catch (err) {
      setError(err);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section aria-labelledby={`${kindId}-heading`} className="space-y-4">
      <h3 id={`${kindId}-heading`} className="text-md font-semibold tracking-tight text-text-primary">
        Log activity
      </h3>
      {error !== undefined && <ApiErrorBanner error={error} />}
      <form
        onSubmit={handleSubmit}
        className="grid gap-4 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6"
      >
        <div className="flex flex-col gap-2">
          <label htmlFor={kindId} className={fieldLabel}>Kind</label>
          <select
            id={kindId}
            value={kind}
            onChange={(e) => setKind(e.target.value as ActivityKind)}
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
            required
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
          />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor={bodyId} className={fieldLabel}>Details</label>
          <textarea
            id={bodyId}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            className={textArea}
          />
        </div>
        <Button type="submit" variant="primary" disabled={submitting}>
          {submitting ? "Logging…" : "Log activity"}
        </Button>
      </form>
    </section>
  );
}
