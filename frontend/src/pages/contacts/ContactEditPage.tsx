// SPDX-License-Identifier: MIT
/**
 * Contact edit page. Update, including marking `departed`, is low-risk and commits directly.
 * Renders `DepartureNotice` from the opportunities fetched for the contact's account.
 */
import { useCallback, useEffect, useState, type JSX } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { useCrmApi } from "../../api/useCrmApi";
import { ContactForm } from "../../components/forms/ContactForm";
import { DepartureNotice } from "../../components/forms/DepartureNotice";
import { ErrorBanner } from "../../components/forms/ErrorBanner";
import { isScopeDenied, ScopeDeniedNotice } from "../../components/forms/ScopeDeniedNotice";
import { Skeleton } from "../../components/ui/skeleton";
import type { Contact, Opportunity } from "../../types/domain";

/** The page's own container rhythm, shared by every state this page can render. */
const pageShell = "mx-auto w-full max-w-narrow space-y-7 px-6 py-8";

function parseOpportunities(response: unknown): readonly Opportunity[] {
  if (typeof response === "object" && response !== null && "items" in response) {
    const items = (response as { items: unknown }).items;
    return Array.isArray(items) ? (items as Opportunity[]) : [];
  }
  return [];
}

export function ContactEditPage(): JSX.Element {
  const { contactId = "" } = useParams<{ contactId: string }>();
  const navigate = useNavigate();
  const api = useCrmApi();

  const [contact, setContact] = useState<Contact | undefined>(undefined);
  const [accountOpportunities, setAccountOpportunities] = useState<readonly Opportunity[]>([]);
  const [error, setError] = useState<unknown>(undefined);
  const [loading, setLoading] = useState(true);

  const [loadedContactId, setLoadedContactId] = useState(contactId);
  if (loadedContactId !== contactId) {
    setLoadedContactId(contactId);
    setLoading(true);
    setError(undefined);
  }

  const load = useCallback(() => {
    return api
      .getContact({ contact_id: contactId })
      .then(async (contactResponse) => {
        if (
          typeof contactResponse !== "object" ||
          contactResponse === null ||
          !("contact" in contactResponse)
        ) {
          throw new Error("get_contact returned an unexpected shape");
        }
        const loadedContact = (contactResponse as { contact: Contact }).contact;
        setContact(loadedContact);

        const opportunitiesResponse = await api.searchOpportunities({
          account_id: loadedContact.account_id,
        });
        setAccountOpportunities(parseOpportunities(opportunitiesResponse));
      })
      .catch((err: unknown) => {
        setError(err);
      })
      .finally(() => {
        setLoading(false);
      });
    // `api` is referentially stable (memoised by `useCrmApi`) and is omitted from the deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contactId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Loading, denied, error and not-found each have a distinct rendering.
  if (loading) {
    return (
      <main aria-busy="true" aria-live="polite" className={pageShell}>
        <Skeleton label="Loading contact" className="h-9 w-1/2" />
        <div className="space-y-5 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6">
          {["name", "title", "email", "status"].map((field) => (
            <div key={field} className="space-y-2">
              <Skeleton label={`Loading ${field} label`} className="h-3 w-24" />
              <Skeleton label={`Loading ${field} field`} className="h-control-md w-full" />
            </div>
          ))}
        </div>
      </main>
    );
  }

  if (error) {
    return (
      <main className={pageShell}>
        {isScopeDenied(error) ? <ScopeDeniedNotice error={error} /> : <ErrorBanner error={error} />}
      </main>
    );
  }

  if (!contact) {
    return (
      <main className={pageShell}>
        <p className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-6 py-8 text-sm text-empty-text">
          Contact not found.
        </p>
      </main>
    );
  }

  return (
    <main className={pageShell}>
      <h1 className="text-2xl font-semibold tracking-tight text-text-primary">
        Edit {contact.name}
      </h1>
      <DepartureNotice contactId={contactId} accountOpportunities={accountOpportunities} />
      <ContactForm
        initial={contact}
        onSaved={() => navigate(`/contacts/${contactId}`)}
        onCancel={() => navigate(`/contacts/${contactId}`)}
      />
    </main>
  );
}
