// SPDX-License-Identifier: MIT
/**
 * Contact detail page: the contact (from `get_contact`) plus its account's opportunities (via
 * `search_opportunities({ account_id })`), so a contact still referenced as an opportunity's
 * primary contact is surfaced before a delete or departure. A referenced delete is refused; a
 * departure is allowed and leaves the reference in place (`DepartureNotice`).
 */
import { useCallback, useEffect, useState, type JSX } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useCrmApi } from "../../api/useCrmApi";
import { DeleteProposalButton } from "../../components/forms/DeleteProposalButton";
import { ErrorBanner } from "../../components/forms/ErrorBanner";
import { isScopeDenied, ScopeDeniedNotice } from "../../components/forms/ScopeDeniedNotice";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import { cn } from "../../components/ui/utils";
import type { Contact, Opportunity } from "../../types/domain";

/** The page's own container rhythm, shared by every state this page can render. */
const pageShell = "mx-auto w-full max-w-content space-y-7 px-6 py-8";

function parseContact(response: unknown): Contact | undefined {
  if (typeof response === "object" && response !== null && "contact" in response) {
    return (response as { contact: Contact }).contact;
  }
  return undefined;
}

function parseOpportunities(response: unknown): readonly Opportunity[] {
  if (typeof response === "object" && response !== null && "items" in response) {
    const items = (response as { items: unknown }).items;
    return Array.isArray(items) ? (items as Opportunity[]) : [];
  }
  return [];
}

export function ContactDetailPage(): JSX.Element {
  const { contactId = "" } = useParams<{ contactId: string }>();
  const navigate = useNavigate();
  const api = useCrmApi();

  const [contact, setContact] = useState<Contact | undefined>(undefined);
  const [referencingOpportunities, setReferencingOpportunities] = useState<
    readonly Opportunity[]
  >([]);
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
        const parsedContact = parseContact(contactResponse);
        if (!parsedContact) {
          throw new Error("get_contact returned an unexpected shape");
        }
        setContact(parsedContact);

        const opportunitiesResponse = await api.searchOpportunities({
          account_id: parsedContact.account_id,
        });
        const opportunities = parseOpportunities(opportunitiesResponse);
        setReferencingOpportunities(
          opportunities.filter((opp) => opp.primary_contact_id === contactId),
        );
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
        <Skeleton label="Loading contact" className="h-9 w-2/5" />
        <div className="grid gap-5 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6 sm:grid-cols-2">
          {["title", "email", "phone", "status"].map((field) => (
            <div key={field} className="space-y-2">
              <Skeleton label={`Loading ${field}`} className="h-3 w-20" />
              <Skeleton label={`Loading ${field} value`} className="h-4 w-32" />
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
      <header className="flex flex-wrap items-start justify-between gap-4">
        <h1 className="text-2xl font-semibold tracking-tight text-text-primary">{contact.name}</h1>
        <nav aria-label={`Actions for ${contact.name}`} className="flex items-center gap-3">
          <Button asChild variant="secondary" size="sm">
            <Link to={`/contacts/${contactId}/edit`}>Edit contact</Link>
          </Button>
          <Button asChild variant="ghost" size="sm">
            <Link to={`/accounts/${contact.account_id}`}>View account</Link>
          </Button>
        </nav>
      </header>

      <dl className="grid gap-5 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6 shadow-raised sm:grid-cols-2">
        {[
          { label: "Title", value: contact.title },
          { label: "Email", value: contact.email },
          { label: "Phone", value: contact.phone },
        ].map((field) => (
          <div key={field.label} className="space-y-1">
            <dt className="text-xs font-medium tracking-wide text-text-secondary">{field.label}</dt>
            {/* Absent reads differently from present: an em dash in muted text, never blank. */}
            <dd
              className={cn("text-sm", field.value == null ? "text-text-muted" : "text-text-primary")}
            >
              {field.value ?? "—"}
            </dd>
          </div>
        ))}
        <div className="space-y-1">
          <dt className="text-xs font-medium tracking-wide text-text-secondary">Status</dt>
          <dd className="text-sm">
            {/* Status carries a badge as well as its word, so it is not colour- or prose-only. */}
            <Badge variant={contact.status === "departed" ? "neutral" : "positive"}>
              {contact.status === "departed" ? "Departed" : "Active"}
            </Badge>
          </dd>
        </div>
      </dl>

      {referencingOpportunities.length > 0 ? (
        <div
          role="status"
          className="max-w-prose rounded-surface border border-hairline border-caution-border bg-caution-surface px-5 py-4 text-caution-text"
        >
          <p className="text-sm leading-relaxed">
            {contact.name} is the primary contact on {referencingOpportunities.length}{" "}
            {referencingOpportunities.length === 1 ? "opportunity" : "opportunities"} (
            {referencingOpportunities.map((opp) => opp.name).join(", ")}). Deleting this contact
            will be refused by the backend until each is reassigned to a different primary
            contact. Marking them departed instead is allowed, but will not clear this reference —
            you will still need to propose a primary-contact change on each opportunity.
          </p>
        </div>
      ) : null}

      <DeleteProposalButton targetTable="contacts" targetId={contactId} label={contact.name} />

      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => navigate(`/accounts/${contact.account_id}`)}
      >
        Back to account
      </Button>
    </main>
  );
}
