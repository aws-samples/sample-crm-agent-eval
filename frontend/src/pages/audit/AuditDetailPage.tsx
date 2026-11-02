// SPDX-License-Identifier: MIT
/**
 * Audit detail at `/audit/:auditId`: one audited change read in full through `getAuditRecord`
 * (the list's projection carries no snapshots).
 *
 * Shows the record's metadata (action, actor, recorded time, target, status, revert time) and its
 * change from `diffSnapshots`: a create lists the new values, a delete the removed values, an update
 * the field diff, and a record without both snapshots says so. Revert follows the list's rules: a
 * reversed record, and a `proposal_rebased` record (the server refuses it), offer no revert; a
 * conflict renders `ConflictNotice` and its refresh re-reads the record. A 403 renders
 * `ScopeDeniedNotice`, a 404 a not-found message.
 */
import { useCallback, useEffect, useState } from "react";
import type { JSX } from "react";
import { Link, useParams } from "react-router-dom";

import { useCrmApi } from "../../api/useCrmApi";
import { CrmApiError } from "../../api/CrmApiClient";
import type { AuditRecord } from "../../types/domain";
import { ConflictNotice, DiffView, ScopeDeniedNotice } from "../../components/diff";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import { cn, focusRing } from "../../components/ui/utils";
import { formatDateTime } from "../../lib/format";
import { newTransactionKey } from "../approvals/transactionKey";
import { diffSnapshots, type SnapshotField } from "./fieldDiff";

/** The full item `get_audit_record` returns: the list row's fields plus the detail-only ones. */
export interface AuditRecordDetail extends AuditRecord {
  /** `create` | `update` | `delete` | `proposal_rebased`, derived server-side when not stored. */
  readonly action?: string;
  readonly reversed_by?: string | null;
  readonly reversed_reason?: string | null;
}

const pageShell = "mx-auto w-full max-w-content space-y-7 px-6 py-8";
const pageHeading = "text-2xl font-semibold tracking-tight text-text-primary";
const textLink = cn("rounded-control text-text-link underline-offset-4 hover:underline", focusRing);

const ACTION_LABEL: Readonly<Record<string, string>> = {
  create: "Created",
  update: "Updated",
  delete: "Deleted",
  proposal_rebased: "Proposal snapshot re-captured",
};

type LoadState =
  | { readonly kind: "loading" }
  | { readonly kind: "denied"; readonly missingScope?: string; readonly message: string }
  | { readonly kind: "not_found" }
  | { readonly kind: "error"; readonly message: string }
  | { readonly kind: "ready"; readonly record: AuditRecordDetail };

type RevertState =
  | { readonly kind: "idle" }
  | { readonly kind: "submitting" }
  | { readonly kind: "conflict" }
  | { readonly kind: "error"; readonly message: string };

function extractRecord(response: unknown): AuditRecordDetail | undefined {
  if (typeof response !== "object" || response === null || !("audit_record" in response)) {
    return undefined;
  }
  const record = (response as { audit_record: unknown }).audit_record;
  if (typeof record !== "object" || record === null || !("audit_id" in record)) return undefined;
  return record as AuditRecordDetail;
}

function formatValue(value: unknown): string {
  if (value === undefined || value === null) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function SnapshotList({
  fields,
  label,
  tone,
}: {
  readonly fields: readonly SnapshotField[];
  readonly label: string;
  readonly tone: "added" | "removed";
}): JSX.Element {
  return (
    <section aria-label={label} className="space-y-3">
      <p className="text-xs font-medium tracking-wide text-text-secondary">{label}</p>
      <dl
        className={cn(
          "overflow-hidden rounded-surface border border-hairline border-l-emphasis",
          tone === "added"
            ? "border-diff-added-border bg-diff-added-surface text-diff-added-text"
            : "border-diff-removed-border bg-diff-removed-surface text-diff-removed-text",
        )}
      >
        {fields.map(({ field, value }) => (
          <div
            key={field}
            className="grid gap-1 border-b-hairline border-border-subtle px-cell-x py-cell-y last:border-b-0 sm:grid-cols-[minmax(0,10rem)_1fr] sm:gap-4"
          >
            <dt className="text-xs font-medium">{field}</dt>
            <dd className="font-numeric text-sm">{formatValue(value)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function ChangeView({ record }: { readonly record: AuditRecordDetail }): JSX.Element {
  const diff = diffSnapshots(record.before_snapshot, record.after_snapshot);
  const subject = `${record.target_table} record ${record.target_id}`;
  switch (diff.kind) {
    case "create":
      return <SnapshotList fields={diff.fields} label={`Values ${subject} was created with`} tone="added" />;
    case "delete":
      return <SnapshotList fields={diff.fields} label={`Values ${subject} held when deleted`} tone="removed" />;
    case "update":
      return (
        <DiffView
          label={`Change to ${subject}`}
          before={record.before_snapshot}
          after={record.after_snapshot}
        />
      );
    case "legacy":
      return (
        <p className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-5 py-4 text-sm text-empty-text">
          This audit record was written without before and after snapshots, so its field changes
          cannot be shown.
        </p>
      );
  }
}

function MetaRow({ term, children }: { readonly term: string; readonly children: JSX.Element | string }): JSX.Element {
  return (
    <div className="grid gap-1 sm:grid-cols-[minmax(0,10rem)_1fr] sm:gap-4">
      <dt className="text-xs font-medium tracking-wide text-text-secondary">{term}</dt>
      <dd className="text-sm text-text-primary">{children}</dd>
    </div>
  );
}

export function AuditDetailPage(): JSX.Element {
  const { auditId = "" } = useParams<{ auditId: string }>();
  const api = useCrmApi();
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [loadedAuditId, setLoadedAuditId] = useState(auditId);
  if (loadedAuditId !== auditId) {
    setLoadedAuditId(auditId);
    setState({ kind: "loading" });
  }
  const [revertState, setRevertState] = useState<RevertState>({ kind: "idle" });

  const fetchState = useCallback(() => {
    void api
      .getAuditRecord({ audit_id: auditId })
      .then((response) => {
        const record = extractRecord(response);
        setState(record === undefined ? { kind: "not_found" } : { kind: "ready", record });
      })
      .catch((err: unknown) => {
        if (err instanceof CrmApiError && err.kind === "scope_denied") {
          setState({ kind: "denied", missingScope: err.body.missingScope, message: err.body.message });
          return;
        }
        if (err instanceof CrmApiError && err.kind === "not_found") {
          setState({ kind: "not_found" });
          return;
        }
        setState({
          kind: "error",
          message: err instanceof Error ? err.message : "Failed to load this audit record.",
        });
      });
    // `api` is referentially stable (memoised by `useCrmApi`) and is omitted from the deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auditId]);

  useEffect(() => {
    fetchState();
  }, [fetchState]);

  const load = useCallback(() => {
    setState({ kind: "loading" });
    fetchState();
  }, [fetchState]);

  async function revert(record: AuditRecordDetail): Promise<void> {
    setRevertState({ kind: "submitting" });
    try {
      await api.revertAuditRecord({ audit_id: record.audit_id, transaction_key: newTransactionKey() });
      setRevertState({ kind: "idle" });
      load();
    } catch (err) {
      if (err instanceof CrmApiError && err.kind === "conflict") {
        setRevertState({ kind: "conflict" });
        return;
      }
      setRevertState({
        kind: "error",
        message:
          err instanceof CrmApiError
            ? err.body.message
            : err instanceof Error
              ? err.message
              : "Failed to revert this record.",
      });
    }
  }

  const backLink = (
    <Link to="/audit" className={cn(textLink, "text-sm")}>
      Back to the audit log
    </Link>
  );

  if (state.kind === "loading") {
    return (
      <main aria-busy="true" aria-live="polite" className={pageShell}>
        {backLink}
        <h1 className={pageHeading}>Audit record</h1>
        <div className="space-y-3 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6">
          <Skeleton label="Loading audit record" className="h-4 w-2/5" />
          <Skeleton label="Loading audit metadata" className="h-3 w-3/5" />
          <Skeleton label="Loading audit change" className="h-row-relaxed w-full" />
        </div>
      </main>
    );
  }

  if (state.kind === "denied") {
    return (
      <main className={pageShell}>
        {backLink}
        <h1 className={pageHeading}>Audit record</h1>
        <ScopeDeniedNotice missingScope={state.missingScope} message={state.message} />
      </main>
    );
  }

  if (state.kind === "not_found") {
    return (
      <main className={pageShell}>
        {backLink}
        <h1 className={pageHeading}>Audit record</h1>
        <p className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-6 py-8 text-sm text-empty-text">
          No audit record {auditId} exists in your book.
        </p>
      </main>
    );
  }

  if (state.kind === "error") {
    return (
      <main className={pageShell}>
        {backLink}
        <h1 className={pageHeading}>Audit record</h1>
        <div className="max-w-prose space-y-3 rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-4">
          <p role="alert" className="text-sm leading-relaxed text-danger-text">
            {state.message}
          </p>
          <Button type="button" variant="secondary" size="sm" onClick={load}>
            Retry
          </Button>
        </div>
      </main>
    );
  }

  const { record } = state;
  const reversed = record.status === "reversed";
  const rebased = record.action === "proposal_rebased";
  const submitting = revertState.kind === "submitting";

  return (
    <main className={pageShell}>
      {backLink}
      <div className="flex flex-wrap items-center gap-3">
        <h1 className={pageHeading}>
          {record.target_table} record {record.target_id}
        </h1>
        {reversed && <Badge variant="neutral">Reverted</Badge>}
      </div>

      <dl className="max-w-prose space-y-3">
        <MetaRow term="Action">{ACTION_LABEL[record.action ?? ""] ?? record.action ?? "Unknown"}</MetaRow>
        <MetaRow term="Actor">
          <span className="font-numeric">{record.actor_rep_id ?? "Unknown"}</span>
        </MetaRow>
        <MetaRow term="Recorded">
          <time dateTime={record.created_at} className="font-numeric">
            {formatDateTime(record.created_at)}
          </time>
        </MetaRow>
        <MetaRow term="Target">
          <span>
            {record.target_table} <span className="font-numeric">{record.target_id}</span>
          </span>
        </MetaRow>
        <MetaRow term="Status">{reversed ? "Reverted" : "Active"}</MetaRow>
        {record.reversed_at ? (
          <MetaRow term="Reverted">
            <time dateTime={record.reversed_at} className="font-numeric">
              {formatDateTime(record.reversed_at)}
            </time>
          </MetaRow>
        ) : null}
        {record.reversed_by ? (
          <MetaRow term="Reverted by">
            <span className="font-numeric">{record.reversed_by}</span>
          </MetaRow>
        ) : null}
        {record.reversed_reason ? <MetaRow term="Revert reason">{record.reversed_reason}</MetaRow> : null}
      </dl>

      <ChangeView record={record} />

      {revertState.kind === "conflict" && (
        <ConflictNotice
          action="revert"
          onRefresh={() => {
            setRevertState({ kind: "idle" });
            load();
          }}
          onDismiss={() => setRevertState({ kind: "idle" })}
        />
      )}
      {revertState.kind === "error" && (
        <p
          role="alert"
          className="rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-3 text-sm text-danger-text"
        >
          {revertState.message}
        </p>
      )}

      {rebased ? (
        <p className="max-w-prose text-sm text-text-secondary">
          This entry records a proposal&rsquo;s snapshot being re-captured, not a change to the
          record, so there is nothing to revert.
        </p>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <Button
            type="button"
            variant={reversed ? "ghost" : "secondary"}
            disabled={submitting || reversed}
            onClick={() => void revert(record)}
          >
            {reversed ? "Already reverted" : submitting ? "Reverting…" : "Revert"}
          </Button>
        </div>
      )}
    </main>
  );
}
