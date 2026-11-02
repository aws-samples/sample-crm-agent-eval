// SPDX-License-Identifier: MIT
/**
 * A single opportunity card on the pipeline board.
 *
 * The whole card is a navigational control: a click anywhere, or Enter on the focused
 * card link, opens `/opportunities/:opportunityId` — the same detail page the opportunities list
 * links to. The card is rendered as a "stretched link" (a transparent overlay `Link`) so the entire
 * surface navigates while a dedicated move handle can still sit above it as its own control.
 *
 * Two parallel move paths change stage, both routed through `PipelineBoard` (never a direct commit):
 *  - Mouse: native HTML5 drag-and-drop (`draggable` on the card), handled by the column. A drag sets
 *    a "drag occurred" flag that suppresses the click a browser synthesises after a drag, so moving a
 *    card never also navigates.
 *  - Keyboard: focus the move handle (`aria-label` "Move opportunity"), press Enter/Space to "pick it
 *    up" (announced via `aria-pressed` and the live region in `PipelineBoard`), arrow keys move it
 *    between columns, Enter/Space again "drops" it, Escape cancels. No pointer is required.
 *
 * A card carrying a pending proposal renders a distinct badge and its move handle is disabled until
 * the proposal is resolved; the card still navigates on click.
 */
import { useId, useRef } from "react";
import type { JSX, KeyboardEvent, MouseEvent } from "react";
import { Link } from "react-router-dom";

import type { Opportunity } from "../../types/domain";
import type { PendingStageChange } from "./usePipelineBoard";
import { Badge } from "../../components/ui/badge";
import { cn, focusRing } from "../../components/ui/utils";

const cardShell = cn(
  "relative block w-full cursor-grab space-y-3 rounded-surface border border-hairline p-4 pr-12 text-left",
  "transition-colors duration-fast ease-standard",
);

/** A card the user has picked up, distinguished by border width and elevation as well as hue. */
const cardPicked = "border-emphasis border-accent-border bg-accent-surface shadow-overlay";

const cardIdle = "border-border-subtle bg-surface-raised shadow-flush hover:bg-surface-hover";

/**
 * A card whose change is awaiting approval. `--pending-*` carries a dashed border and
 * `--pending-hatch` as well as its hue; `cursor-default` because the card is not draggable.
 */
const cardLocked =
  "cursor-default border-dashed border-pending-border bg-pending-surface bg-pending-hatch";

export interface OpportunityCardProps {
  readonly opportunity: Opportunity;
  readonly pending: PendingStageChange | undefined;
  readonly isPicked: boolean;
  readonly onDragStart: (event: React.DragEvent<HTMLDivElement>) => void;
  readonly onDragEnd: () => void;
  readonly onPickUp: () => void;
  readonly onCancelPickUp: () => void;
  readonly onMove: (direction: "left" | "right") => void;
  readonly onConfirmDrop: () => void;
}

export function OpportunityCard({
  opportunity,
  pending,
  isPicked,
  onDragStart,
  onDragEnd,
  onPickUp,
  onCancelPickUp,
  onMove,
  onConfirmDrop,
}: OpportunityCardProps): JSX.Element {
  const labelId = useId();
  const locked = pending !== undefined;

  // Set on dragStart, cleared by the click it suppresses. A browser fires a click after a mouse
  // drag; without this guard that click would also navigate, so a move would open the card too.
  const didDragRef = useRef(false);

  function handleDragStart(event: React.DragEvent<HTMLDivElement>): void {
    didDragRef.current = true;
    onDragStart(event);
  }

  function handleDragEnd(): void {
    // Clear on the next tick so a drag ending without a trailing click can't suppress a later click.
    window.setTimeout(() => {
      didDragRef.current = false;
    }, 0);
    onDragEnd();
  }

  function handleClickCapture(event: MouseEvent<HTMLDivElement>): void {
    if (didDragRef.current) {
      event.preventDefault(); // react-router's Link skips navigation when the event is defaultPrevented.
      didDragRef.current = false;
    }
  }

  function handleHandleKeyDown(event: KeyboardEvent<HTMLButtonElement>): void {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (isPicked) {
        onConfirmDrop();
      } else {
        onPickUp();
      }
      return;
    }
    if (event.key === "Escape" && isPicked) {
      event.preventDefault();
      onCancelPickUp();
      return;
    }
    if (isPicked && (event.key === "ArrowRight" || event.key === "ArrowLeft")) {
      event.preventDefault();
      onMove(event.key === "ArrowRight" ? "right" : "left");
    }
  }

  return (
    <div
      draggable={!locked}
      onDragStart={locked ? undefined : handleDragStart}
      onDragEnd={handleDragEnd}
      onClickCapture={handleClickCapture}
      data-opportunity-id={opportunity.opportunity_id}
      data-picked={isPicked ? "true" : "false"}
      className={cn(cardShell, locked ? cardLocked : isPicked ? cardPicked : cardIdle)}
    >
      {/* The stretched link: a transparent overlay that makes the whole card navigate. Ordered below
          the move handle (see z-index) so the handle stays clickable. `draggable={false}` keeps the
          anchor's default URL-drag from pre-empting the card's own HTML5 drag. */}
      <Link
        to={`/opportunities/${opportunity.opportunity_id}`}
        draggable={false}
        aria-labelledby={labelId}
        aria-describedby={locked ? `${labelId}-pending` : undefined}
        className={cn("absolute inset-0 z-sticky rounded-surface", focusRing)}
      >
        <span className="sr-only">Open opportunity</span>
      </Link>
      {/* Name and value stay in one text node; tests match the name against a single element. */}
      <span
        id={labelId}
        draggable={false}
        className={cn(
          "block text-sm font-medium leading-snug",
          locked ? "text-pending-text" : "text-text-primary",
        )}
      >
        {opportunity.name} — ${opportunity.value.toLocaleString()}
      </span>
      {/* The dedicated move control. Layered above the stretched link so it receives its own clicks
          and keystrokes; disabled (not removed) while a proposal is pending so its affordance stays
          legible. Keyboard pick-up/move/drop lives here, not on the card body. */}
      <button
        type="button"
        aria-label="Move opportunity"
        aria-pressed={isPicked}
        disabled={locked}
        onKeyDown={handleHandleKeyDown}
        data-testid="opportunity-move-handle"
        data-picked={isPicked ? "true" : "false"}
        className={cn(
          "absolute right-2 top-2 z-panel inline-flex h-8 w-8 cursor-grab items-center justify-center",
          "rounded-control text-text-secondary transition-colors duration-fast ease-standard",
          "hover:bg-surface-hover disabled:cursor-default disabled:opacity-50",
          focusRing,
        )}
      >
        <span aria-hidden="true" className="text-md leading-none">
          ⠿
        </span>
      </button>
      {/* Wording avoids "Awaiting approval": `PipelineBoard`'s live region announces
          that phrase after a successful proposal, and a second copy in the DOM would make the
          `findByText(/Awaiting approval/)` queries used elsewhere ambiguous. */}
      {locked && pending && (
        <Badge id={`${labelId}-pending`} data-testid="pending-badge" variant="pending">
          Pending: move to {pending.toStage}
        </Badge>
      )}
    </div>
  );
}
