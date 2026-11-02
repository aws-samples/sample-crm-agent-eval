// SPDX-License-Identifier: MIT
/**
 * Create opportunity. Creation is low-risk and commits directly, with no proposal. Stage defaults
 * to the input schema's default (Discovery) if omitted.
 */
import { useId, useState, type FormEvent, type JSX } from "react";
import { useNavigate } from "react-router-dom";

import { useCrmApi } from "../../api/useCrmApi";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { cn, focusRing } from "../../components/ui/utils";
import { OPPORTUNITY_STAGES } from "../../types/domain";
import { ApiErrorBanner } from "./ApiErrorBanner";
import { asRecord } from "./types";

/** Field label styling, shared by every control on this form. */
const fieldLabel = "text-xs font-medium tracking-wide text-text-secondary";

/** A native `<select>`/`<textarea>` styled to match the `Input` primitive, which has no variant. */
const selectControl = cn(
  "h-control-md w-full rounded-control border border-hairline border-border-control bg-surface-raised px-4 text-sm text-text-primary",
  focusRing,
);
const textArea = cn(
  "min-h-24 w-full rounded-control border border-hairline border-border-control bg-surface-raised px-4 py-3 text-sm text-text-primary",
  focusRing,
);

function newTransactionKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `txn-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function OpportunityCreatePage(): JSX.Element {
  const api = useCrmApi();
  const navigate = useNavigate();

  const [name, setName] = useState("");
  const [accountId, setAccountId] = useState("");
  const [value, setValue] = useState("");
  const [closeDate, setCloseDate] = useState("");
  const [stage, setStage] = useState<string>("Discovery");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<unknown>(undefined);
  const [submitting, setSubmitting] = useState(false);

  const nameId = useId();
  const accountIdId = useId();
  const valueId = useId();
  const closeDateId = useId();
  const stageId = useId();
  const descriptionId = useId();

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError(undefined);
    setSubmitting(true);
    try {
      const response = await api.createOpportunity({
        name,
        account_id: accountId,
        value: value.trim() === "" ? undefined : Number(value),
        close_date: closeDate === "" ? undefined : closeDate,
        stage: stage as (typeof OPPORTUNITY_STAGES)[number],
        description: description.trim() === "" ? undefined : description,
        transaction_key: newTransactionKey(),
      });
      const created = asRecord(response);
      const opportunity = asRecord(created.opportunity);
      const newId = typeof opportunity.opportunity_id === "string" ? opportunity.opportunity_id : undefined;
      navigate(newId ? `/opportunities/${newId}` : "/opportunities");
    } catch (err) {
      setError(err);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main
      aria-labelledby={`${nameId}-heading`}
      className="mx-auto w-full max-w-narrow space-y-7 px-6 py-8"
    >
      <h1
        id={`${nameId}-heading`}
        className="text-2xl font-semibold tracking-tight text-text-primary"
      >
        Create opportunity
      </h1>

      {error !== undefined && <ApiErrorBanner error={error} />}

      <form
        onSubmit={handleSubmit}
        className="grid gap-4 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6 shadow-raised"
      >
        <div className="flex flex-col gap-2">
          <label htmlFor={nameId} className={fieldLabel}>Name</label>
          <Input id={nameId} type="text" required value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor={accountIdId} className={fieldLabel}>Account ID</label>
          <Input
            id={accountIdId}
            type="text"
            required
            value={accountId}
            onChange={(e) => setAccountId(e.target.value)}
            className="font-numeric"
          />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor={valueId} className={fieldLabel}>Value</label>
          <Input
            id={valueId}
            type="number"
            min="0"
            step="0.01"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="font-numeric"
          />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor={closeDateId} className={fieldLabel}>Close date</label>
          <Input
            id={closeDateId}
            type="date"
            value={closeDate}
            onChange={(e) => setCloseDate(e.target.value)}
            className="font-numeric"
          />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor={stageId} className={fieldLabel}>Stage</label>
          <select
            id={stageId}
            value={stage}
            onChange={(e) => setStage(e.target.value)}
            className={selectControl}
          >
            {OPPORTUNITY_STAGES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor={descriptionId} className={fieldLabel}>Description</label>
          <textarea
            id={descriptionId}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className={textArea}
          />
        </div>
        <Button type="submit" variant="primary" disabled={submitting}>
          {submitting ? "Creating…" : "Create opportunity"}
        </Button>
      </form>
    </main>
  );
}
