// SPDX-License-Identifier: MIT
/**
 * Approval queue: pending proposals rendered as before/after diffs with approve/reject actions.
 *
 * Approve/reject re-reads current state server-side and compares the before-snapshot; a mismatch,
 * including a racing second approval, comes back as `CrmApiError.kind === "conflict"` and is
 * rendered via `ConflictNotice`, with the moved fields when the server names them. Its refresh on
 * an approve conflict calls `rebaseProposal` (the proposal re-captures the record's current state)
 * and then reloads the queue; an already-decided or deleted-target conflict only reloads. A
 * `scope_denied` (403) renders distinctly from an empty queue, and a refused rebase renders as a
 * denial or an error on its row.
 */
import { useCallback, useEffect, useState } from "react";
import type { JSX } from "react";

import { useCrmApi } from "../../api/useCrmApi";
import { CrmApiError, type ConflictingField } from "../../api/CrmApiClient";
import type { Proposal } from "../../types/domain";
import { ConflictNotice, DiffView, ScopeDeniedNotice } from "../../components/diff";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import { cn } from "../../components/ui/utils";
import { newTransactionKey } from "./transactionKey";

/** The page's own container rhythm, shared by every state this page can render. */
const pageShell = "mx-auto w-full max-w-content space-y-7 px-6 py-8";

const pageHeading = "text-2xl font-semibold tracking-tight text-text-primary";

type LoadState =
  | { readonly kind: "loading" }
  | { readonly kind: "denied"; readonly missingScope?: string; readonly message: string }
  | { readonly kind: "error"; readonly message: string }
  | { readonly kind: "ready"; readonly proposals: Proposal[] };

/** Per-row transient state for an in-flight or just-failed approve/reject action. */
type RowState =
  | { readonly kind: "idle" }
  | { readonly kind: "submitting" }
  | {
      readonly kind: "conflict";
      readonly action: "approve" | "reject";
      readonly reason?: string;
      readonly conflictingFields?: readonly ConflictingField[];
      readonly refreshing?: boolean;
    }
  | { readonly kind: "denied"; readonly missingScope?: string; readonly message: string }
  | { readonly kind: "error"; readonly message: string };

/** Server reasons under which the proposal cannot be rebased, so a refresh only reloads. */
const NOT_REBASABLE: ReadonlySet<string> = new Set(["already_decided", "target_missing"]);

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof CrmApiError) return err.body.message;
  if (err instanceof Error) return err.message;
  return fallback;
}

function isProposalArray(value: unknown): value is Proposal[] {
  return Array.isArray(value);
}

/**
 * Extracts proposals from the `list_proposals` envelope `{ proposals, next_cursor }`. Any other
 * shape yields an empty queue.
 */
function extractProposals(response: unknown): Proposal[] {
  if (
    typeof response === "object" &&
    response !== null &&
    "proposals" in response &&
    isProposalArray((response as { proposals: unknown }).proposals)
  ) {
    return (response as { proposals: Proposal[] }).proposals;
  }
  return [];
}

export function ApprovalsPage(): JSX.Element {
  const api = useCrmApi();
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [rowState, setRowState] = useState<Record<string, RowState>>({});

  const fetchState = useCallback(() => {
    void api
      .listProposals({ status: "pending" })
      .then((response) => {
        setState({ kind: "ready", proposals: extractProposals(response) });
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
          message: err instanceof Error ? err.message : "Failed to load the approval queue.",
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

  function setRow(proposalId: string, next: RowState): void {
    setRowState((prev) => ({ ...prev, [proposalId]: next }));
  }

  async function decide(proposal: Proposal, action: "approve" | "reject"): Promise<void> {
    setRow(proposal.proposal_id, { kind: "submitting" });
    try {
      if (action === "approve") {
        await api.approveProposal({
          proposal_id: proposal.proposal_id,
          transaction_key: newTransactionKey(),
        });
      } else {
        await api.rejectProposal({
          proposal_id: proposal.proposal_id,
          transaction_key: newTransactionKey(),
        });
      }
      setRow(proposal.proposal_id, { kind: "idle" });
      load();
    } catch (err) {
      if (err instanceof CrmApiError && err.kind === "conflict") {
        setRow(proposal.proposal_id, {
          kind: "conflict",
          action,
          reason: err.body.reason,
          conflictingFields: err.body.conflictingFields,
        });
        return;
      }
      setRow(proposal.proposal_id, {
        kind: "error",
        message: errorMessage(err, `Failed to ${action} this proposal.`),
      });
    }
  }

  async function refreshAfterConflict(
    proposal: Proposal,
    conflict: Extract<RowState, { kind: "conflict" }>,
  ): Promise<void> {
    if (conflict.action !== "approve" || NOT_REBASABLE.has(conflict.reason ?? "")) {
      setRow(proposal.proposal_id, { kind: "idle" });
      load();
      return;
    }
    setRow(proposal.proposal_id, { ...conflict, refreshing: true });
    try {
      await api.rebaseProposal({
        proposal_id: proposal.proposal_id,
        transaction_key: newTransactionKey(),
      });
      setRow(proposal.proposal_id, { kind: "idle" });
      load();
    } catch (err) {
      if (err instanceof CrmApiError && err.kind === "scope_denied") {
        setRow(proposal.proposal_id, {
          kind: "denied",
          missingScope: err.body.missingScope,
          message: err.body.message,
        });
        return;
      }
      setRow(proposal.proposal_id, {
        kind: "error",
        message: errorMessage(err, "Failed to refresh this proposal."),
      });
    }
  }

  if (state.kind === "loading") {
    return (
      <main aria-busy="true" aria-live="polite" className={pageShell}>
        <h1 className={pageHeading}>Approval queue</h1>
        <div className="space-y-4">
          {[0, 1].map((row) => (
            <div
              key={row}
              className="space-y-3 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6"
            >
              <Skeleton label="Loading pending proposals" className="h-4 w-2/5" />
              <Skeleton label="Loading proposal reason" className="h-3 w-3/5" />
              <Skeleton label="Loading proposal diff" className="h-row-relaxed w-full" />
            </div>
          ))}
        </div>
      </main>
    );
  }

  if (state.kind === "denied") {
    return (
      <main className={pageShell}>
        <h1 className={pageHeading}>Approval queue</h1>
        <ScopeDeniedNotice missingScope={state.missingScope} message={state.message} />
      </main>
    );
  }

  if (state.kind === "error") {
    return (
      <main className={pageShell}>
        <h1 className={pageHeading}>Approval queue</h1>
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

  const { proposals } = state;

  return (
    <main className={pageShell}>
      <h1 className={pageHeading}>Approval queue</h1>
      {proposals.length === 0 ? (
        <p className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-6 py-8 text-sm text-empty-text">
          No pending proposals.
        </p>
      ) : (
        <ul className="space-y-5">
          {proposals.map((proposal) => {
            const row = rowState[proposal.proposal_id] ?? { kind: "idle" };
            const submitting = row.kind === "submitting";
            const busy = submitting || (row.kind === "conflict" && row.refreshing === true);
            return (
              <li
                key={proposal.proposal_id}
                /* `--pending-*` carries a dashed border and `--pending-hatch` as well as its hue,
                   so a pending change survives greyscale. */
                className="space-y-4 rounded-surface border border-emphasis border-dashed border-pending-border bg-pending-surface bg-pending-hatch p-6"
              >
                <div className="flex flex-wrap items-center gap-3">
                  {/* The heading is a single text node; tests match its full text. */}
                  <h2 className="text-md font-semibold tracking-tight text-pending-text">
                    {proposal.operation === "delete" ? "Delete" : "Update"} {proposal.target_table}{" "}
                    record {proposal.target_id}
                  </h2>
                  {/* A delete is marked as destructive *in addition to* being pending, so the two
                      facts are not collapsed into one badge. */}
                  <Badge variant={proposal.operation === "delete" ? "danger" : "pending"}>
                    {proposal.operation === "delete" ? "Delete proposed" : "Update proposed"}
                  </Badge>
                </div>
                <p className="max-w-prose text-sm leading-relaxed text-pending-text">
                  <strong className="font-semibold">Reason:</strong> {proposal.reason}
                </p>
                <DiffView
                  label={`Proposed change to ${proposal.target_table} record ${proposal.target_id}`}
                  before={proposal.before_snapshot}
                  after={proposal.after_payload ?? {}}
                  partialAfter
                />

                {row.kind === "conflict" && (
                  <ConflictNotice
                    action={row.action}
                    reason={row.reason}
                    conflictingFields={row.conflictingFields}
                    refreshing={row.refreshing}
                    onRefresh={() => void refreshAfterConflict(proposal, row)}
                    onDismiss={() => setRow(proposal.proposal_id, { kind: "idle" })}
                  />
                )}
                {row.kind === "denied" && (
                  <ScopeDeniedNotice missingScope={row.missingScope} message={row.message} />
                )}
                {row.kind === "error" && (
                  <p
                    role="alert"
                    className="rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-3 text-sm text-danger-text"
                  >
                    {row.message}
                  </p>
                )}

                <div className={cn("flex flex-wrap items-center gap-3")}>
                  <Button
                    type="button"
                    variant="primary"
                    disabled={busy}
                    onClick={() => void decide(proposal, "approve")}
                  >
                    {submitting ? "Approving…" : "Approve"}
                  </Button>
                  <Button
                    type="button"
                    variant="secondary"
                    disabled={busy}
                    onClick={() => void decide(proposal, "reject")}
                  >
                    {submitting ? "Rejecting…" : "Reject"}
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
