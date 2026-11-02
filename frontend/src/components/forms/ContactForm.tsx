// SPDX-License-Identifier: MIT
/**
 * Create/update form for contacts, a low-risk write that commits directly. Marking a contact
 * `departed` is an ordinary `status` change.
 */
import { useId, useState, type FormEvent, type JSX } from "react";

import { useCrmApi } from "../../api/useCrmApi";
import type { Contact, ContactStatus } from "../../types/domain";
import { ErrorBanner } from "./ErrorBanner";

export interface ContactFormValues {
  readonly name: string;
  readonly title: string;
  readonly email: string;
  readonly phone: string;
  readonly status: ContactStatus;
}

export interface ContactFormProps {
  /** Required for create; ignored for update (the contact already has one). */
  readonly accountId?: string;
  /** When present, the form edits this contact; otherwise it creates a new one. */
  readonly initial?: Contact;
  readonly onSaved: (contactId: string) => void;
  readonly onCancel?: () => void;
}

function toFormValues(contact: Contact | undefined): ContactFormValues {
  return {
    name: contact?.name ?? "",
    title: contact?.title ?? "",
    email: contact?.email ?? "",
    phone: contact?.phone ?? "",
    status: contact?.status ?? "active",
  };
}

export function ContactForm({
  accountId,
  initial,
  onSaved,
  onCancel,
}: ContactFormProps): JSX.Element {
  const api = useCrmApi();
  const [values, setValues] = useState<ContactFormValues>(() => toFormValues(initial));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(undefined);

  const nameId = useId();
  const titleId = useId();
  const emailId = useId();
  const phoneId = useId();
  const statusId = useId();

  function update<K extends keyof ContactFormValues>(key: K, value: ContactFormValues[K]): void {
    setValues((prev) => ({ ...prev, [key]: value }));
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setSubmitting(true);
    setError(undefined);
    try {
      if (initial) {
        await api.updateContact({
          contact_id: initial.contact_id,
          fields: {
            name: values.name,
            title: values.title || undefined,
            email: values.email || undefined,
            phone: values.phone || undefined,
            status: values.status,
          },
          transaction_key: `update-contact-${initial.contact_id}-${Date.now()}`,
        });
        onSaved(initial.contact_id);
      } else {
        if (!accountId) {
          throw new Error("ContactForm: accountId is required to create a contact");
        }
        const response = await api.createContact({
          account_id: accountId,
          name: values.name,
          title: values.title || undefined,
          email: values.email || undefined,
          phone: values.phone || undefined,
          status: values.status,
          transaction_key: `create-contact-${Date.now()}-${Math.random().toString(36).slice(2)}`,
        });
        const contactId = extractContactId(response);
        onSaved(contactId ?? "");
      }
    } catch (err) {
      setError(err);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={(e) => void handleSubmit(e)} aria-label={initial ? "Edit contact" : "Create contact"}>
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
        <label htmlFor={titleId}>Title</label>
        <input
          id={titleId}
          type="text"
          value={values.title}
          onChange={(e) => update("title", e.target.value)}
        />
      </div>
      <div>
        <label htmlFor={emailId}>Email</label>
        <input
          id={emailId}
          type="email"
          value={values.email}
          onChange={(e) => update("email", e.target.value)}
        />
      </div>
      <div>
        <label htmlFor={phoneId}>Phone</label>
        <input
          id={phoneId}
          type="tel"
          value={values.phone}
          onChange={(e) => update("phone", e.target.value)}
        />
      </div>
      <div>
        <label htmlFor={statusId}>Status</label>
        <select
          id={statusId}
          value={values.status}
          onChange={(e) => update("status", e.target.value as ContactStatus)}
        >
          <option value="active">Active</option>
          <option value="departed">Departed</option>
        </select>
      </div>

      {error ? <ErrorBanner error={error} /> : null}

      <button type="submit" disabled={submitting}>
        {submitting ? "Saving…" : initial ? "Save changes" : "Create contact"}
      </button>
      {onCancel ? (
        <button type="button" onClick={onCancel} disabled={submitting}>
          Cancel
        </button>
      ) : null}
    </form>
  );
}

/** `create_contact` returns `{"contact": {...}}` (`crm.handlers.create_contact`), never a top-level `contact_id`. */
function extractContactId(response: unknown): string | undefined {
  if (typeof response === "object" && response !== null && "contact" in response) {
    const contact = (response as { contact: unknown }).contact;
    if (typeof contact === "object" && contact !== null && "contact_id" in contact) {
      const value = (contact as { contact_id: unknown }).contact_id;
      return typeof value === "string" ? value : undefined;
    }
  }
  return undefined;
}
