// SPDX-License-Identifier: MIT
/**
 * Audit log: committed writes rendered as before/after diffs with a revert action.
 *
 * Each card is a link to `/audit/:auditId` (the heading link is stretched over the card; the revert
 * action sits above it). A card's "Reverted" state reads the projected `status`; the list
 * projection carries no snapshots, which the detail view reads in full.
 *
 * Revert refuses an already-reversed record and a record modified after the audit entry was
 * written; both come back as `CrmApiError.kind === "conflict"` and are rendered via
 * `ConflictNotice` with a refresh.
 */
import { useCallback, useEffect, useState } from "react";
import type { JSX } from "react";
import { Link } from "react-router-dom";

import { useCrmApi } from "../../api/useCrmApi";
import { CrmApiError } from "../../api/CrmApiClient";
import type { AuditRecord } from "../../types/domain";
import { ConflictNotice, DiffView, ScopeDeniedNotice } from "../../components/diff";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import { cn, focusRing } from "../../components/ui/utils";
import { formatDateTime, formatRelativeTime } from "../../lib/format";
import { newTransactionKey } from "../approvals/transactionKey";

/** The page's own container rhythm, shared by every state this page can render. */
const pageShell = "mx-auto w-full max-w-content space-y-7 px-6 py-8";

const pageHeading = "text-2xl font-semibold tracking-tight text-text-primary";

/** The card's heading link; its `::after` covers the card so the whole card opens the detail. */
const cardLink = cn(
  "rounded-control text-text-primary underline-offset-4 hover:underline",
  "after:absolute after:inset-0 after:rounded-surface",
  focusRing,
);

type LoadState =
  | { readonly kind: "loading" }
  | { readonly kind: "denied"; readonly missingScope?: string; readonly message: string }
  | { readonly kind: "error"; readonly message: string }
  | { readonly kind: "ready"; readonly records: AuditRecord[] };

type RowState =
  | { readonly kind: "idle" }
  | { readonly kind: "submitting" }
  | { readonly kind: "conflict" }
  | { readonly kind: "error"; readonly message: string };

function isRecordArray(value: unknown): value is AuditRecord[] {
  return Array.isArray(value);
}

/**
 * Extracts audit records from either a bare array or the `list_audit_records` envelope
 * (`{audit_records: [...], next_cursor: ...}`). Any other shape yields an empty list.
 */
function extractAuditRecords(response: unknown): AuditRecord[] {
  if (isRecordArray(response)) return response;
  if (
    typeof response === "object" &&
    response !== null &&
    "audit_records" in response &&
    isRecordArray((response as { audit_records: unknown }).audit_records)
  ) {
    return (response as { audit_records: AuditRecord[] }).audit_records;
  }
  return [];
}

export function AuditPage(): JSX.Element {
  const api = useCrmApi();
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [rowState, setRowState] = useState<Record<string, RowState>>({});

  const fetchState = useCallback(() => {
    void api
      .listAuditRecords({})
      .then((response) => {
        setState({ kind: "ready", records: extractAuditRecords(response) });
      })
      .catch((err: unknown) => {
        if (err instanceof CrmApiError && err.kind === "scope_denied") {
          setState({
            kind: "denied",
            missingScope: err.body.missingScope,
            message: err.body.message,
          });
          return;
        }
        setState({
          kind: "error",
          message: err instanceof Error ? err.message : "Failed to load the audit log.",
        });
      });
    // `api` is referentially stable (memoised by `useCrmApi`) and is omitted from the deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    fetchState();
  }, [fetchState]);

  const load = useCallback(() => {
    setState({ kind: "loading" });
    fetchState();
  }, [fetchState]);

  function setRow(auditId: string, next: RowState): void {
    setRowState((prev) => ({ ...prev, [auditId]: next }));
  }

  async function revert(record: AuditRecord): Promise<void> {
    setRow(record.audit_id, { kind: "submitting" });
    try {
      await api.revertAuditRecord({
        audit_id: record.audit_id,
        transaction_key: newTransactionKey(),
      });
      setRow(record.audit_id, { kind: "idle" });
      load();
    } catch (err) {
      if (err instanceof CrmApiError && err.kind === "conflict") {
        setRow(record.audit_id, { kind: "conflict" });
        return;
      }
      setRow(record.audit_id, {
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

  if (state.kind === "loading") {
    return (
      <main aria-busy="true" aria-live="polite" className={pageShell}>
        <h1 className={pageHeading}>Audit log</h1>
        <div className="space-y-4">
          {[0, 1].map((row) => (
            <div
              key={row}
              className="space-y-3 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6"
            >
              <Skeleton label="Loading audit records" className="h-4 w-2/5" />
              <Skeleton label="Loading audit actor" className="h-3 w-1/3" />
              <Skeleton label="Loading audit diff" className="h-row-relaxed w-full" />
            </div>
          ))}
        </div>
      </main>
    );
  }

  if (state.kind === "denied") {
    return (
      <main className={pageShell}>
        <h1 className={pageHeading}>Audit log</h1>
        <ScopeDeniedNotice missingScope={state.missingScope} message={state.message} />
      </main>
    );
  }

  if (state.kind === "error") {
    return (
      <main className={pageShell}>
        <h1 className={pageHeading}>Audit log</h1>
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

  const { records } = state;

  return (
    <main className={pageShell}>
      <h1 className={pageHeading}>Audit log</h1>
      {records.length === 0 ? (
        <p className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-6 py-8 text-sm text-empty-text">
          No audit records.
        </p>
      ) : (
        <ul className="space-y-5">
          {records.map((record) => {
            const row = rowState[record.audit_id] ?? { kind: "idle" };
            const submitting = row.kind === "submitting";
            const alreadyReverted = record.status === "reversed";
            return (
              <li
                key={record.audit_id}
                /* A committed write uses a neutral surface; a reverted row is muted. */
                className={
                  alreadyReverted
                    ? "relative space-y-4 rounded-surface border border-hairline border-border-subtle bg-surface-inset p-6"
                    : "relative space-y-4 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6 shadow-raised"
                }
              >
                <div className="flex flex-wrap items-center gap-3">
                  <h2 className="text-md font-semibold tracking-tight text-text-primary">
                    <Link to={`/audit/${encodeURIComponent(record.audit_id)}`} className={cardLink}>
                      {record.target_table} record {record.target_id}
                    </Link>
                  </h2>
                  {alreadyReverted && <Badge variant="neutral">Reverted</Badge>}
                </div>
                <p className="flex flex-wrap items-baseline gap-2 text-xs text-text-secondary">
                  <strong className="font-medium">Actor:</strong>{" "}
                  {/* `gsi_tenant` does not project `actor_rep_id`, so the default list path has
                      no actor to show. */}
                  <span className="font-numeric">
                    {record.actor_rep_id ?? "not in this view"}
                  </span>{" "}
                  ·{" "}
                  <strong className="font-medium">At:</strong>{" "}
                  {/* Relative time on screen, the machine value in `dateTime` and the absolute
                      time in `title`. */}
                  <time
                    dateTime={record.created_at}
                    title={formatDateTime(record.created_at)}
                    className="font-numeric"
                  >
                    {formatRelativeTime(record.created_at)}
                  </time>
                  {alreadyReverted && record.reversed_at && (
                    <>
                      {" "}
                      ·{" "}
                      <time
                        dateTime={record.reversed_at}
                        className="font-numeric text-text-muted"
                      >
                        Reverted at {formatDateTime(record.reversed_at)}
                      </time>
                    </>
                  )}
                </p>
                <DiffView
                  label={`Change to ${record.target_table} record ${record.target_id}`}
                  before={record.before_snapshot}
                  after={record.after_snapshot}
                />

                {row.kind === "conflict" && (
                  /* Positioned so its actions sit above the card link's overlay. */
                  <div className="relative">
                    <ConflictNotice
                      action="revert"
                      onRefresh={load}
                      onDismiss={() => setRow(record.audit_id, { kind: "idle" })}
                    />
                  </div>
                )}
                {row.kind === "error" && (
                  <p
                    role="alert"
                    className="rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-3 text-sm text-danger-text"
                  >
                    {row.message}
                  </p>
                )}

                <div className="relative flex flex-wrap items-center gap-3">
                  {/* A second revert is refused: the button is disabled, relabelled, and the row
                      carries a `Reverted` badge. */}
                  <Button
                    type="button"
                    variant={alreadyReverted ? "ghost" : "secondary"}
                    disabled={submitting || alreadyReverted}
                    onClick={() => void revert(record)}
                  >
                    {alreadyReverted ? "Already reverted" : submitting ? "Reverting…" : "Revert"}
                  </Button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </main>
  );
}
