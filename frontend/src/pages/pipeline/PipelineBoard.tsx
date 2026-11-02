// SPDX-License-Identifier: MIT
/**
 * Pipeline board: stage columns, drag-to-change-stage. A drag or keyboard move never commits a
 * stage change directly; both paths converge on `usePipelineBoard().proposeStageChange`, which calls
 * only `submit_proposal`.
 *
 * Keyboard: a card's move handle is focusable and, on Enter/Space, enters a "picked up" state
 * (`aria-pressed="true"`, announced via the `aria-live` region below); Left/Right arrow keys move
 * the picked-up card one column at a time; Enter/Space again opens the same confirm dialog a mouse
 * drop would open; Escape cancels.
 *
 * A typed 403 (`CrmApiError.kind === "scope_denied"`) renders a distinct denial panel, never an
 * empty board.
 */
import { useCallback, useState } from "react";
import type { DragEvent, JSX } from "react";

import { OPPORTUNITY_STAGES } from "../../types/domain";
import type { Opportunity, OpportunityStage } from "../../types/domain";
import { usePipelineBoard } from "./usePipelineBoard";
import { OpportunityCard } from "./OpportunityCard";
import { StageChangeConfirmDialog } from "./StageChangeConfirmDialog";
import { classifyTransition } from "./stageTransitions";
import { Badge } from "../../components/ui/badge";
import { Skeleton } from "../../components/ui/skeleton";
import { cn } from "../../components/ui/utils";

interface PendingMove {
  readonly opportunity: Opportunity;
  readonly toStage: OpportunityStage;
}

/**
 * The board's internal rhythm, shared by every state it can render. The `main` landmark, the `h1`
 * and the page padding belong to `PipelinePage`.
 */
const boardShell = "space-y-5";

/** Stage columns wrap rather than scrolling horizontally; widths come from the grid. */
const columnGrid = "grid gap-5 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6";

/** A column is a track holding cards, so it sits *below* the page surface and its cards sit above it. */
const columnShell =
  "flex flex-col gap-4 rounded-surface border border-hairline border-border-subtle bg-surface-sunken p-5";

/**
 * The keyboard drop target during a pick-up, distinguished by border width, border hue and surface.
 */
const columnTargeted = "border-emphasis border-accent-border bg-accent-surface";

export function PipelineBoard(): JSX.Element {
  const board = usePipelineBoard();
  const [pickedUpId, setPickedUpId] = useState<string | undefined>(undefined);
  const [pickedUpStage, setPickedUpStage] = useState<OpportunityStage | undefined>(undefined);
  const [pendingMove, setPendingMove] = useState<PendingMove | undefined>(undefined);
  const [announcement, setAnnouncement] = useState("");

  const findOpportunity = useCallback(
    (opportunityId: string): Opportunity | undefined => {
      for (const stage of OPPORTUNITY_STAGES) {
        const match = board.opportunitiesByStage
          .get(stage)
          ?.find((o) => o.opportunity_id === opportunityId);
        if (match) return match;
      }
      return undefined;
    },
    [board.opportunitiesByStage],
  );

  function beginMove(opportunity: Opportunity, toStage: OpportunityStage): void {
    if (toStage === opportunity.stage) return;
    setPendingMove({ opportunity, toStage });
    setPickedUpId(undefined);
    setPickedUpStage(undefined);
  }

  // ---- Mouse drag-and-drop path ----
  function handleDragStart(event: DragEvent<HTMLDivElement>, opportunityId: string): void {
    event.dataTransfer.setData("text/plain", opportunityId);
    event.dataTransfer.effectAllowed = "move";
  }

  function handleColumnDragOver(event: DragEvent<HTMLDivElement>): void {
    event.preventDefault();
  }

  function handleColumnDrop(event: DragEvent<HTMLDivElement>, toStage: OpportunityStage): void {
    event.preventDefault();
    const opportunityId = event.dataTransfer.getData("text/plain");
    const opportunity = findOpportunity(opportunityId);
    if (opportunity) beginMove(opportunity, toStage);
  }

  // ---- Keyboard path ----
  function pickUp(opportunity: Opportunity): void {
    setPickedUpId(opportunity.opportunity_id);
    setPickedUpStage(opportunity.stage);
    setAnnouncement(
      `Picked up ${opportunity.name}, currently in ${opportunity.stage}. Use left and right arrow keys to choose a column, Enter to confirm, Escape to cancel.`,
    );
  }

  function cancelPickUp(): void {
    setAnnouncement("Cancelled moving the card.");
    setPickedUpId(undefined);
    setPickedUpStage(undefined);
  }

  function moveWithinKeyboard(direction: "left" | "right"): void {
    if (!pickedUpStage) return;
    const index = OPPORTUNITY_STAGES.indexOf(pickedUpStage);
    const nextIndex = direction === "right" ? index + 1 : index - 1;
    if (nextIndex < 0 || nextIndex >= OPPORTUNITY_STAGES.length) return;
    const nextStage = OPPORTUNITY_STAGES[nextIndex];
    setPickedUpStage(nextStage);
    setAnnouncement(`Now over ${nextStage}. Press Enter to confirm or Escape to cancel.`);
  }

  function confirmKeyboardDrop(): void {
    if (!pickedUpId || !pickedUpStage) return;
    const opportunity = findOpportunity(pickedUpId);
    if (!opportunity) return;
    beginMove(opportunity, pickedUpStage);
  }

  // Before the first fetch settles the board shows a skeleton. Gated on `loaded` rather than
  // `loading` so a later refetch leaves the already-rendered cards in place.
  if (!board.loaded) {
    return (
      <section aria-busy="true" className={boardShell}>
        {/* The skeleton columns carry no `pipeline-column-empty-*` testid. */}
        <div role="status" aria-live="polite" data-testid="pipeline-loading" className={columnGrid}>
          <span className="sr-only">Loading the pipeline…</span>
          {OPPORTUNITY_STAGES.map((stage) => (
            <div key={stage} className={columnShell} aria-hidden="true">
              <Skeleton label={`Loading the ${stage} column`} className="h-4 w-3/5" />
              <Skeleton label={`Loading ${stage} opportunities`} className="h-row-relaxed w-full" />
              <Skeleton label={`Loading ${stage} opportunities`} className="h-row-relaxed w-full" />
            </div>
          ))}
        </div>
      </section>
    );
  }

  if (board.error) {
    if (board.error.kind === "scope_denied") {
      return (
        <section className={boardShell}>
          <div
            role="alert"
            data-testid="pipeline-scope-denied"
            className="max-w-prose space-y-3 rounded-surface border border-hairline border-denied-border bg-denied-surface px-6 py-5"
          >
            <h2 className="text-md font-semibold tracking-tight text-denied-text">
              Pipeline unavailable
            </h2>
            <p className="text-sm leading-relaxed text-denied-text">
              You do not have access to this pipeline.
            </p>
            {board.error.body.missingScope && (
              <p className="text-sm leading-relaxed text-denied-text">
                Missing scope:{" "}
                <code className="font-mono text-xs">{board.error.body.missingScope}</code>
              </p>
            )}
          </div>
        </section>
      );
    }
    return (
      <section className={boardShell}>
        <div
          role="alert"
          className="max-w-prose space-y-3 rounded-surface border border-hairline border-danger-border bg-danger-surface px-6 py-5"
        >
          <h2 className="text-md font-semibold tracking-tight text-danger-text">
            Could not load the pipeline
          </h2>
          <p className="text-sm leading-relaxed text-danger-text">{board.error.body.message}</p>
        </div>
      </section>
    );
  }

  return (
    <section aria-label="Pipeline board" className={boardShell}>
      <div aria-live="polite" data-testid="pipeline-live-region" className="sr-only">
        {announcement}
      </div>
      {board.truncated && (
        <p
          role="status"
          data-testid="pipeline-truncated-notice"
          className="max-w-prose rounded-surface border border-hairline border-caution-border bg-caution-surface px-5 py-3 text-sm leading-relaxed text-caution-text"
        >
          Showing a partial result set; some opportunities may not be displayed.
        </p>
      )}
      <div className={columnGrid}>
        {OPPORTUNITY_STAGES.map((stage) => {
          const opportunities = board.opportunitiesByStage.get(stage) ?? [];
          const isKeyboardTarget = pickedUpStage === stage;
          return (
            <div
              key={stage}
              aria-label={`${stage} column`}
              data-testid={`pipeline-column-${stage}`}
              data-keyboard-target={isKeyboardTarget ? "true" : "false"}
              onDragOver={handleColumnDragOver}
              onDrop={(event) => handleColumnDrop(event, stage)}
              className={cn(columnShell, isKeyboardTarget && columnTargeted)}
            >
              <h2 className="flex items-center justify-between gap-3 text-sm font-semibold tracking-tight text-text-secondary">
                {stage}
                <Badge variant="neutral">{opportunities.length}</Badge>
              </h2>
              {opportunities.length === 0 ? (
                <p
                  data-testid={`pipeline-column-empty-${stage}`}
                  className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-4 py-5 text-xs leading-relaxed text-empty-text"
                >
                  No opportunities in this stage.
                </p>
              ) : (
                opportunities.map((opportunity) => (
                  <OpportunityCard
                    key={opportunity.opportunity_id}
                    opportunity={opportunity}
                    pending={board.pendingChanges.get(opportunity.opportunity_id)}
                    isPicked={pickedUpId === opportunity.opportunity_id}
                    onDragStart={(event) => handleDragStart(event, opportunity.opportunity_id)}
                    onDragEnd={() => undefined}
                    onPickUp={() => pickUp(opportunity)}
                    onCancelPickUp={cancelPickUp}
                    onMove={moveWithinKeyboard}
                    onConfirmDrop={confirmKeyboardDrop}
                  />
                ))
              )}
            </div>
          );
        })}
      </div>
      {pendingMove && (
        <StageChangeConfirmDialog
          opportunityName={pendingMove.opportunity.name}
          fromStage={pendingMove.opportunity.stage}
          toStage={pendingMove.toStage}
          transitionKind={classifyTransition(pendingMove.opportunity.stage, pendingMove.toStage)}
          onConfirm={async (reason) => {
            await board.proposeStageChange(pendingMove.opportunity, pendingMove.toStage, reason);
            setAnnouncement(
              `Proposed moving ${pendingMove.opportunity.name} to ${pendingMove.toStage}. Awaiting approval.`,
            );
            setPendingMove(undefined);
          }}
          onCancel={() => setPendingMove(undefined)}
        />
      )}
    </section>
  );
}
