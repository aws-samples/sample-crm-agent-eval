// SPDX-License-Identifier: MIT
/**
 * Opportunity detail: notes, activity timeline, and related contacts. Low-risk fields (`name`,
 * `description`, `source`, `competitor`) commit directly via `update_opportunity`; high-risk fields
 * (`stage`, `value`, `close_date`, `owner_rep_id`, `primary_contact_id`) and delete route through
 * `submit_proposal`. The two paths are separate forms.
 */
import { useCallback, useEffect, useId, useState, type FormEvent, type JSX } from "react";
import { Link, useParams } from "react-router-dom";

import { useCrmApi } from "../../api/useCrmApi";
import { ActivityTimeline } from "../../components/timeline/ActivityTimeline";
import { LogActivityForm } from "../../components/timeline/LogActivityForm";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Skeleton } from "../../components/ui/skeleton";
import { cn, focusRing } from "../../components/ui/utils";
import { OPPORTUNITY_STAGES, type OpportunityStage } from "../../types/domain";
import { ApiErrorBanner } from "./ApiErrorBanner";
import { formatCurrency, formatDate } from "../../lib/format";
import { newTransactionKey } from "../../lib/randomToken";
import {
  OPPORTUNITY_HIGH_RISK_FIELDS,
  OPPORTUNITY_LOW_RISK_FIELDS,
} from "./opportunityFieldRisk";
import { asOpportunityDetailResult, type OpportunityDetailResult } from "./types";

/** The page's own container rhythm, shared by every state this page can render. */
const pageShell = "mx-auto w-full max-w-content space-y-7 px-6 py-8";

/** A neutral content card. The default surface for anything that is not risk-marked. */
const card =
  "rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6 shadow-raised";

/**
 * The proposal path's surface. `--pending-*` carries a dashed border and `--pending-hatch` as well
 * as its hue, so it survives greyscale.
 */
const proposalCard =
  "rounded-surface border border-emphasis border-dashed border-pending-border bg-pending-surface bg-pending-hatch p-6";

/** Field label styling, shared by every control on the page. */
const fieldLabel = "text-xs font-medium tracking-wide text-text-secondary";

/** A `<textarea>` styled to match the `Input` primitive, which has no multiline variant. */
const textArea = cn(
  "min-h-24 w-full rounded-control border border-hairline border-border-control bg-surface-raised px-4 py-3 text-sm text-text-primary",
  focusRing,
);

/** A native `<select>` styled to match `Input`. */
const selectControl = cn(
  "h-control-md w-full rounded-control border border-hairline border-border-control bg-surface-raised px-4 text-sm text-text-primary",
  focusRing,
);

export function OpportunityDetailPage(): JSX.Element {
  const { opportunityId } = useParams<{ opportunityId: string }>();
  const api = useCrmApi();

  const [detail, setDetail] = useState<OpportunityDetailResult | undefined>(undefined);
  const [error, setError] = useState<unknown>(undefined);
  const [loading, setLoading] = useState(true);

  const [loadedOpportunityId, setLoadedOpportunityId] = useState(opportunityId);
  if (loadedOpportunityId !== opportunityId) {
    setLoadedOpportunityId(opportunityId);
    setLoading(true);
    setError(undefined);
  }

  const load = useCallback(() => {
    if (!opportunityId) return Promise.resolve();
    return api
      .getOpportunity({ opportunity_id: opportunityId })
      .then((raw) => {
        setDetail(asOpportunityDetailResult(raw));
      })
      .catch((err: unknown) => {
        setError(err);
        setDetail(undefined);
      })
      .finally(() => {
        setLoading(false);
      });
    // `api` is referentially stable (memoised by `useCrmApi`) and is omitted from the deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opportunityId]);

  useEffect(() => {
    void load();
  }, [load]);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(undefined);
    await load();
  }, [load]);

  const headingId = useId();

  if (!opportunityId) {
    return (
      <main className={pageShell}>
        <p
          role="alert"
          className="max-w-prose rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-4 text-sm text-danger-text"
        >
          No opportunity ID was provided.
        </p>
      </main>
    );
  }

  // Loading, error (including the denial branch inside ApiErrorBanner) and not-found each have a
  // distinct rendering.
  if (loading) {
    return (
      <main aria-busy="true" aria-live="polite" className={pageShell}>
        <Skeleton label="Loading opportunity" className="h-9 w-2/5" />
        <div className={cn(card, "grid gap-5 sm:grid-cols-2")}>
          {["stage", "value", "close date", "owner", "primary contact"].map((field) => (
            <div key={field} className="space-y-2">
              <Skeleton label={`Loading ${field}`} className="h-3 w-20" />
              <Skeleton label={`Loading ${field} value`} className="h-4 w-32" />
            </div>
          ))}
        </div>
      </main>
    );
  }

  if (error !== undefined) {
    return (
      <main className={pageShell}>
        <ApiErrorBanner error={error} />
      </main>
    );
  }

  if (!detail) {
    return (
      <main className={pageShell}>
        <p className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-6 py-8 text-sm text-empty-text">
          This opportunity could not be found.
        </p>
      </main>
    );
  }

  const { opportunity, notes, activities, contacts } = detail;

  return (
    <main aria-labelledby={headingId} className={pageShell}>
      <h1 id={headingId} className="text-2xl font-semibold tracking-tight text-text-primary">
        {opportunity.name}
      </h1>

      <dl className={cn(card, "grid gap-5 sm:grid-cols-2 lg:grid-cols-3")}>
        <div className="space-y-1">
          <dt className={fieldLabel}>Stage</dt>
          <dd>
            <Badge variant="accent">{opportunity.stage}</Badge>
          </dd>
        </div>
        <div className="space-y-1">
          <dt className={fieldLabel}>Value</dt>
          <dd className="font-numeric text-md text-text-primary">
            {formatCurrency(opportunity.value)}
          </dd>
        </div>
        <div className="space-y-1">
          <dt className={fieldLabel}>Close date</dt>
          <dd
            className={cn(
              "text-sm",
              opportunity.close_date == null ? "text-text-muted" : "text-text-primary",
            )}
          >
            {formatDate(opportunity.close_date)}
          </dd>
        </div>
        <div className="space-y-1">
          <dt className={fieldLabel}>Owner</dt>
          <dd className="font-numeric text-sm text-text-primary">{opportunity.owner_rep_id}</dd>
        </div>
        <div className="space-y-1">
          <dt className={fieldLabel}>Primary contact</dt>
          <dd
            className={cn(
              "text-sm",
              opportunity.primary_contact_id == null
                ? "text-text-muted"
                : "font-numeric text-text-primary",
            )}
          >
            {opportunity.primary_contact_id ?? "—"}
          </dd>
        </div>
      </dl>

      <LowRiskEditForm opportunity={opportunity} onSaved={reload} />
      <HighRiskProposalForm opportunity={opportunity} contacts={contacts} />
      <DeleteProposalAction opportunity={opportunity} />

      <section aria-label="Related contacts" className="space-y-4">
        <h2 className="text-lg font-semibold tracking-tight text-text-primary">Contacts</h2>
        {contacts.length === 0 ? (
          <p className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-5 py-6 text-sm text-empty-text">
            No contacts are linked to this opportunity&apos;s account.
          </p>
        ) : (
          <ul className="divide-y divide-border-subtle overflow-hidden rounded-surface border border-hairline border-border-subtle bg-surface-raised">
            {contacts.map((contact) => (
              <li
                key={contact.contact_id}
                className="flex flex-wrap items-center gap-3 px-cell-x py-cell-y"
              >
                <span className="font-medium text-text-primary">{contact.name}</span>
                {" — "}
                <Badge variant={contact.status === "departed" ? "neutral" : "positive"}>
                  {contact.status}
                </Badge>
                {contact.email ? (
                  <span className="text-sm text-text-secondary">{` — ${contact.email}`}</span>
                ) : (
                  ""
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label="Notes" className="space-y-4">
        <h2 className="text-lg font-semibold tracking-tight text-text-primary">Notes</h2>
        {notes.length === 0 ? (
          <p className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-5 py-6 text-sm text-empty-text">
            No notes have been added yet.
          </p>
        ) : (
          <ul className="space-y-3">
            {notes.map((note) => (
              <li
                key={note.note_id}
                className="rounded-surface border border-hairline border-border-subtle bg-surface-raised px-5 py-4 text-sm leading-relaxed text-text-primary"
              >
                {note.body}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label="Activity timeline" className="space-y-4">
        <h2 className="text-lg font-semibold tracking-tight text-text-primary">Activity timeline</h2>
        <ActivityTimeline activities={activities} />
        <LogActivityForm opportunityId={opportunityId} onLogged={reload} />
      </section>
    </main>
  );
}

/** Low-risk field edit: commits directly via `update_opportunity`. */
function LowRiskEditForm({
  opportunity,
  onSaved,
}: {
  opportunity: OpportunityDetailResult["opportunity"];
  onSaved: () => void;
}): JSX.Element {
  const api = useCrmApi();
  const [name, setName] = useState(opportunity.name);
  const [description, setDescription] = useState(opportunity.description ?? "");
  const [source, setSource] = useState(opportunity.source ?? "");
  const [competitor, setCompetitor] = useState(opportunity.competitor ?? "");
  const [error, setError] = useState<unknown>(undefined);
  const [saving, setSaving] = useState(false);

  const nameId = useId();
  const descId = useId();
  const sourceId = useId();
  const competitorId = useId();

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError(undefined);
    setSaving(true);
    try {
      // Fields restricted to OPPORTUNITY_LOW_RISK_FIELDS — mirrors the backend allowlist so this
      // form can never attempt a high-risk field through the direct-commit path.
      const fields: Record<string, unknown> = {};
      if (name !== opportunity.name) fields.name = name;
      if (description !== (opportunity.description ?? "")) fields.description = description;
      if (source !== (opportunity.source ?? "")) fields.source = source;
      if (competitor !== (opportunity.competitor ?? "")) fields.competitor = competitor;

      if (Object.keys(fields).length === 0) {
        setSaving(false);
        return;
      }
      await api.updateOpportunity({
        opportunity_id: opportunity.opportunity_id,
        fields,
        transaction_key: newTransactionKey(),
      });
      onSaved();
    } catch (err) {
      setError(err);
    } finally {
      setSaving(false);
    }
  }

  const headingId = useId();

  return (
    <section aria-labelledby={headingId} className={cn(card, "space-y-4")}>
      <h2 id={headingId} className="text-lg font-semibold tracking-tight text-text-primary">
        Edit details
      </h2>
      <p className="max-w-prose text-sm leading-relaxed text-text-secondary">
        These fields ({OPPORTUNITY_LOW_RISK_FIELDS.join(", ")}) save immediately. Stage, value,
        close date, owner, and primary contact require approval — use the form below instead.
      </p>
      {error !== undefined && <ApiErrorBanner error={error} />}
      <form onSubmit={handleSubmit} className="grid gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-2">
          <label htmlFor={nameId} className={fieldLabel}>
            Name
          </label>
          <Input id={nameId} type="text" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="flex flex-col gap-2 sm:col-span-2">
          <label htmlFor={descId} className={fieldLabel}>
            Description
          </label>
          <textarea
            id={descId}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className={textArea}
          />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor={sourceId} className={fieldLabel}>
            Source
          </label>
          <Input
            id={sourceId}
            type="text"
            value={source}
            onChange={(e) => setSource(e.target.value)}
          />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor={competitorId} className={fieldLabel}>
            Competitor
          </label>
          <Input
            id={competitorId}
            type="text"
            value={competitor}
            onChange={(e) => setCompetitor(e.target.value)}
          />
        </div>
        <div className="sm:col-span-2">
          <Button type="submit" variant="primary" disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>
    </section>
  );
}

/**
 * High-risk field change: routes through `submit_proposal`. The after-payload is built only from
 * `OPPORTUNITY_HIGH_RISK_FIELDS`.
 */
function HighRiskProposalForm({
  opportunity,
  contacts,
}: {
  opportunity: OpportunityDetailResult["opportunity"];
  contacts: OpportunityDetailResult["contacts"];
}): JSX.Element {
  const api = useCrmApi();
  const [stage, setStage] = useState<OpportunityStage>(opportunity.stage);
  const [value, setValue] = useState(String(opportunity.value));
  const [closeDate, setCloseDate] = useState(opportunity.close_date ?? "");
  const [ownerRepId, setOwnerRepId] = useState(opportunity.owner_rep_id);
  const [primaryContactId, setPrimaryContactId] = useState(opportunity.primary_contact_id ?? "");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(undefined);
  const [submitted, setSubmitted] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const headingId = useId();
  const stageId = useId();
  const valueId = useId();
  const closeDateId = useId();
  const ownerId = useId();
  const contactId = useId();
  const reasonId = useId();

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError(undefined);
    setSubmitted(false);
    setSubmitting(true);
    try {
      const afterPayload: Record<string, unknown> = {};
      if (stage !== opportunity.stage) afterPayload.stage = stage;
      if (Number(value) !== opportunity.value) afterPayload.value = Number(value);
      if (closeDate !== (opportunity.close_date ?? "")) afterPayload.close_date = closeDate;
      if (ownerRepId !== opportunity.owner_rep_id) afterPayload.owner_rep_id = ownerRepId;
      if (primaryContactId !== (opportunity.primary_contact_id ?? "")) {
        afterPayload.primary_contact_id = primaryContactId;
      }

      if (Object.keys(afterPayload).length === 0) {
        setSubmitting(false);
        return;
      }

      await api.submitProposal({
        target_table: "opportunities",
        target_id: opportunity.opportunity_id,
        operation: "update",
        after_payload: afterPayload,
        reason,
        transaction_key: newTransactionKey(),
      });
      setSubmitted(true);
      setReason("");
    } catch (err) {
      setError(err);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section aria-labelledby={headingId} className={cn(proposalCard, "space-y-4")}>
      <div className="flex flex-wrap items-center gap-3">
        <h2 id={headingId} className="text-lg font-semibold tracking-tight text-pending-text">
          Propose a change
        </h2>
        {/* The badge repeats the section's meaning in text alongside the dashed surface. */}
        <Badge variant="pending">Requires approval</Badge>
      </div>
      <p className="max-w-prose text-sm leading-relaxed text-pending-text">
        Changes to {OPPORTUNITY_HIGH_RISK_FIELDS.join(", ")} require approval. Submitting here
        creates a pending proposal — nothing changes until it is approved. See{" "}
        <Link
          to="/approvals"
          className={cn("rounded-control underline underline-offset-4", focusRing)}
        >
          the approval queue
        </Link>
        .
      </p>
      {error !== undefined && <ApiErrorBanner error={error} />}
      {submitted && (
        <p
          role="status"
          className="rounded-surface border border-hairline border-positive-border bg-positive-surface px-5 py-4 text-sm text-positive-text"
        >
          Proposal submitted. It is now pending approval and has not been applied.
        </p>
      )}
      <form onSubmit={handleSubmit} className="grid gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-2">
          <label htmlFor={stageId} className={fieldLabel}>
            Stage
          </label>
          <select
            id={stageId}
            value={stage}
            onChange={(e) => setStage(e.target.value as OpportunityStage)}
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
          <label htmlFor={valueId} className={fieldLabel}>
            Value
          </label>
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
          <label htmlFor={closeDateId} className={fieldLabel}>
            Close date
          </label>
          <Input
            id={closeDateId}
            type="date"
            value={closeDate}
            onChange={(e) => setCloseDate(e.target.value)}
            className="font-numeric"
          />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor={ownerId} className={fieldLabel}>
            Owner rep ID
          </label>
          <Input
            id={ownerId}
            type="text"
            value={ownerRepId}
            onChange={(e) => setOwnerRepId(e.target.value)}
            className="font-numeric"
          />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor={contactId} className={fieldLabel}>
            Primary contact
          </label>
          <select
            id={contactId}
            value={primaryContactId}
            onChange={(e) => setPrimaryContactId(e.target.value)}
            className={selectControl}
          >
            <option value="">None</option>
            {contacts.map((contact) => (
              <option key={contact.contact_id} value={contact.contact_id}>
                {contact.name}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-2 sm:col-span-2">
          <label htmlFor={reasonId} className={fieldLabel}>
            Reason
          </label>
          <textarea
            id={reasonId}
            required
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className={textArea}
          />
        </div>
        <div className="sm:col-span-2">
          <Button type="submit" variant="primary" disabled={submitting}>
            {submitting ? "Submitting…" : "Submit proposal"}
          </Button>
        </div>
      </form>
    </section>
  );
}

/** Delete is high-risk: routes through `submit_proposal` with `operation: "delete"`. */
function DeleteProposalAction({
  opportunity,
}: {
  opportunity: OpportunityDetailResult["opportunity"];
}): JSX.Element {
  const api = useCrmApi();
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(undefined);
  const [submitted, setSubmitted] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const headingId = useId();
  const reasonId = useId();

  async function handleDelete(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError(undefined);
    setSubmitted(false);
    setSubmitting(true);
    try {
      await api.submitProposal({
        target_table: "opportunities",
        target_id: opportunity.opportunity_id,
        operation: "delete",
        reason,
        transaction_key: newTransactionKey(),
      });
      setSubmitted(true);
      setReason("");
    } catch (err) {
      setError(err);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    /* `--danger-*` for the consequence; the pending badge marks that it only proposes. */
    <section
      aria-labelledby={headingId}
      className="space-y-4 rounded-surface border border-hairline border-danger-border bg-danger-surface p-6"
    >
      <div className="flex flex-wrap items-center gap-3">
        <h2 id={headingId} className="text-lg font-semibold tracking-tight text-danger-text">
          Delete this opportunity
        </h2>
        <Badge variant="pending">Requires approval</Badge>
      </div>
      <p className="max-w-prose text-sm leading-relaxed text-danger-text">
        Deletes require approval. Submitting here proposes a delete — nothing is removed until it is
        approved.
      </p>
      {error !== undefined && <ApiErrorBanner error={error} />}
      {submitted && (
        <p
          role="status"
          className="rounded-surface border border-hairline border-positive-border bg-positive-surface px-5 py-4 text-sm text-positive-text"
        >
          Delete proposal submitted and pending approval.
        </p>
      )}
      <form onSubmit={handleDelete} className="space-y-4">
        <div className="flex flex-col gap-2">
          <label htmlFor={reasonId} className={fieldLabel}>
            Reason for deletion
          </label>
          <textarea
            id={reasonId}
            required
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className={textArea}
          />
        </div>
        <Button type="submit" variant="danger" disabled={submitting}>
          {submitting ? "Submitting…" : "Propose delete"}
        </Button>
      </form>
    </section>
  );
}
