// SPDX-License-Identifier: MIT
/**
 * Create/update form for accounts, a low-risk write that commits directly. Pass `initial` with an
 * `account_id` to edit an existing account.
 */
import { useId, useState, type FormEvent, type JSX } from "react";

import { useCrmApi } from "../../api/useCrmApi";
import type { Account } from "../../types/domain";
import { ErrorBanner } from "./ErrorBanner";

export interface AccountFormValues {
  readonly name: string;
  readonly industry: string;
  readonly region: string;
  readonly website: string;
  readonly employee_count: string;
}

export interface AccountFormProps {
  /** When present, the form edits this account; otherwise it creates a new one. */
  readonly initial?: Account;
  readonly onSaved: (accountId: string) => void;
  readonly onCancel?: () => void;
}

function toFormValues(account: Account | undefined): AccountFormValues {
  return {
    name: account?.name ?? "",
    industry: account?.industry ?? "",
    region: account?.region ?? "",
    website: account?.website ?? "",
    employee_count: account?.employee_count !== undefined ? String(account.employee_count) : "",
  };
}

export function AccountForm({ initial, onSaved, onCancel }: AccountFormProps): JSX.Element {
  const api = useCrmApi();
  const [values, setValues] = useState<AccountFormValues>(() => toFormValues(initial));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(undefined);

  const nameId = useId();
  const industryId = useId();
  const regionId = useId();
  const websiteId = useId();
  const employeeCountId = useId();

  function update<K extends keyof AccountFormValues>(key: K, value: string): void {
    setValues((prev) => ({ ...prev, [key]: value }));
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setSubmitting(true);
    setError(undefined);
    const employeeCount = values.employee_count.trim()
      ? Number.parseInt(values.employee_count, 10)
      : undefined;
    try {
      if (initial) {
        const fields: Record<string, unknown> = {
          name: values.name,
          industry: values.industry || undefined,
          region: values.region || undefined,
          website: values.website || undefined,
        };
        if (employeeCount !== undefined) fields.employee_count = employeeCount;
        await api.updateAccount({
          account_id: initial.account_id,
          fields,
          transaction_key: `update-account-${initial.account_id}-${Date.now()}`,
        });
        onSaved(initial.account_id);
      } else {
        const response = await api.createAccount({
          name: values.name,
          industry: values.industry || undefined,
          region: values.region || undefined,
          website: values.website || undefined,
          employee_count: employeeCount,
          transaction_key: `create-account-${Date.now()}-${Math.random().toString(36).slice(2)}`,
        });
        const accountId = extractAccountId(response);
        onSaved(accountId ?? "");
      }
    } catch (err) {
      setError(err);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={(e) => void handleSubmit(e)} aria-label={initial ? "Edit account" : "Create account"}>
      <div>
        <label htmlFor={nameId}>Name</label>
        <input
          id={nameId}
          type="text"
          required
          value={values.name}
          onChange={(e) => update("name", e.target.value)}
        />
      </div>
      <div>
        <label htmlFor={industryId}>Industry</label>
        <input
          id={industryId}
          type="text"
          value={values.industry}
          onChange={(e) => update("industry", e.target.value)}
        />
      </div>
      <div>
        <label htmlFor={regionId}>Region</label>
        <input
          id={regionId}
          type="text"
          value={values.region}
          onChange={(e) => update("region", e.target.value)}
        />
      </div>
      <div>
        <label htmlFor={websiteId}>Website</label>
        <input
          id={websiteId}
          type="text"
          value={values.website}
          onChange={(e) => update("website", e.target.value)}
        />
      </div>
      <div>
        <label htmlFor={employeeCountId}>Employee count</label>
        <input
          id={employeeCountId}
          type="number"
          min={0}
          value={values.employee_count}
          onChange={(e) => update("employee_count", e.target.value)}
        />
      </div>

      {error ? <ErrorBanner error={error} /> : null}

      <button type="submit" disabled={submitting}>
        {submitting ? "Saving…" : initial ? "Save changes" : "Create account"}
      </button>
      {onCancel ? (
        <button type="button" onClick={onCancel} disabled={submitting}>
          Cancel
        </button>
      ) : null}
    </form>
  );
}

/** `create_account` returns `{"account": {...}}` (`crm.handlers.create_account`), never a top-level `account_id`. */
function extractAccountId(response: unknown): string | undefined {
  if (typeof response === "object" && response !== null && "account" in response) {
    const account = (response as { account: unknown }).account;
    if (typeof account === "object" && account !== null && "account_id" in account) {
      const value = (account as { account_id: unknown }).account_id;
      return typeof value === "string" ? value : undefined;
    }
  }
  return undefined;
}
