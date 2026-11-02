// SPDX-License-Identifier: MIT
/**
 * The terminal-state pill a turn ends with: completed, cancelled, or stopped by an error named by
 * its `ErrorKind`. The error wording comes from {@link errorHeadline} (EventTrace); the pill only
 * chooses the badge variant.
 */
import type { JSX } from "react";

import type { AgentEventFrame } from "../../types/generated/agentEvents";
import { Badge, type BadgeProps } from "../ui/badge";
import { errorHeadline } from "./EventTrace";

/**
 * How a turn finished. `error` carries the terminal frame, whose wording the pill reuses.
 */
export type TurnTerminalState =
  | { readonly status: "completed" }
  | { readonly status: "cancelled" }
  | { readonly status: "error"; readonly event: AgentEventFrame };

/**
 * The badge variant for an errored turn, keyed off its `ErrorKind`: a budget limit and an invalid
 * request are cautions the user can act on, while a tool or model failure is a system fault. An
 * unknown or absent kind stays neutral rather than claiming a fault it cannot name.
 */
function errorVariant(errorKind: AgentEventFrame["error_kind"]): BadgeProps["variant"] {
  switch (errorKind) {
    case "budget_exhausted":
    case "invalid_request":
      return "caution";
    case "tool_failure":
    case "model_failure":
      return "danger";
    default:
      return "neutral";
  }
}

/** Render one turn's terminal-state pill. */
export function TurnStatePill({ state }: { state: TurnTerminalState }): JSX.Element {
  if (state.status === "completed") {
    return (
      <Badge variant="positive" data-testid="turn-state-pill" data-status="completed">
        Completed
      </Badge>
    );
  }
  if (state.status === "cancelled") {
    return (
      <Badge variant="neutral" data-testid="turn-state-pill" data-status="cancelled">
        Cancelled
      </Badge>
    );
  }
  return (
    <Badge
      variant={errorVariant(state.event.error_kind)}
      data-testid="turn-state-pill"
      data-status="error"
      data-error-kind={state.event.error_kind ?? "unknown"}
    >
      {errorHeadline(state.event)}
    </Badge>
  );
}
