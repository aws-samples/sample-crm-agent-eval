// SPDX-License-Identifier: MIT
/**
 * One turn's step trace: what the assistant did, grouped so the answer is what remains. Nothing is
 * swallowed.
 *
 * - Steps are the tool calls. An adjacent `tool_call`+`tool_result` are paired into one step with a
 *   label from the toolspec's `display_label` and a short arg/result summary keyed by `ToolName`.
 *   Steps live inside a `<details>` disclosure: open while the turn streams, collapsed once it
 *   completes, and toggleable afterwards.
 * - `usage` is hidden ({@link UsageMeter} reports tokens), and `completed` is not a step: its text is
 *   the answer, rendered by the bubble.
 * - `error`, a `proposal`, a `truncated` result, and a kind this build does not know render outside
 *   the disclosure, always visible.
 *
 * `errorHeadline` is also used by `TurnStatePill`.
 */

import { useEffect, useRef, useState, type JSX } from "react";

import { isErrorKind, isEventKind, type AgentEventFrame } from "../../types/generated/agentEvents";
import { TOOL_METADATA, type ToolName } from "../../types/generated/toolSchemas";
import { Badge, type BadgeProps } from "../ui/badge";
import { ProposalCard, type ProposalDecision } from "./ProposalCard";

export const EVENT_TRACE_TESTID = "event-trace";

/** The `<details>` disclosure wrapping a turn's folded steps. */
export const TRACE_STEPS_TESTID = "trace-steps";

/** Shown for an event whose `kind` this build's schema does not declare. */
export const UNKNOWN_KIND_LABEL = "Unrecognised step";

/** Shown when a tool result was cut to fit the byte ceiling, so a partial answer is never silent. */
export const TRUNCATED_LABEL = "Shortened to fit";

/** The turn-level truncation notice testid — see {@link TRUNCATION_NOTICE_TEXT}. */
export const TRUNCATION_NOTICE_TESTID = "trace-truncation-notice";

/**
 * Turn-level truncation notice, shown outside the steps disclosure so it stays visible once the
 * turn completes and the per-step badge is folded away.
 */
export const TRUNCATION_NOTICE_TEXT = "Some results were shortened to fit, so the answer may be partial.";

/**
 * The sentence an error event shows, which always names its `ErrorKind`. An `error_kind` the schema
 * does not declare is shown verbatim.
 */
export function errorHeadline(event: AgentEventFrame): string {
  const kind = event.error_kind;
  if (kind === null) return "The turn stopped without saying why.";
  if (!isErrorKind(kind)) return `The turn stopped: ${kind}.`;
  if (kind === "budget_exhausted") return "The turn hit its budget and stopped early.";
  if (kind === "tool_failure") return "A CRM action failed, so the turn stopped.";
  if (kind === "invalid_request") return "That request wasn't valid, so the turn didn't start.";
  return "The model failed, so the turn stopped.";
}

/** A first non-empty string value among `keys` of `record`, or `null`. Renders only wire values. */
function firstString(record: Record<string, unknown> | null, keys: readonly string[]): string | null {
  if (record === null) return null;
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim() !== "") return value.trim();
  }
  return null;
}

/** A count from a result: `total`, `count`, or the length of a `results`/`items` array. */
function resultCount(output: Record<string, unknown> | null): number | null {
  if (output === null) return null;
  for (const key of ["total", "count"] as const) {
    const value = output[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  for (const key of ["results", "items", "records", "activities", "proposals"] as const) {
    const value = output[key];
    if (Array.isArray(value)) return value.length;
  }
  return null;
}

/**
 * A one-line arg/result summary for one tool's step, or `null` for none. Reads only fields already
 * on the wire. Declared `Record<ToolName, …>`, so a tool without a summarizer is a type error.
 */
type StepSummarizer = (
  input: Record<string, unknown> | null,
  output: Record<string, unknown> | null,
) => string | null;

const withCount = (label: string | null, output: Record<string, unknown> | null): string | null => {
  const count = resultCount(output);
  if (label === null && count === null) return null;
  if (count === null) return label;
  const plural = `${count} result${count === 1 ? "" : "s"}`;
  return label === null ? plural : `${label} — ${plural}`;
};

const SUMMARIZERS: Readonly<Record<ToolName, StepSummarizer>> = {
  search_opportunities: (input, output) =>
    withCount(firstString(input, ["query", "stage", "owner_rep_id", "account_id"]), output),
  search_accounts: (input, output) =>
    withCount(firstString(input, ["query", "owner_rep_id"]), output),
  get_opportunity: (input) => firstString(input, ["opportunity_id"]),
  get_account: (input) => firstString(input, ["account_id"]),
  get_contact: (input) => firstString(input, ["contact_id"]),
  list_activities: (input, output) => withCount(firstString(input, ["opportunity_id"]), output),
  pipeline_aggregate: (input) => firstString(input, ["owner_rep_id"]),
  list_proposals: (input, output) => withCount(firstString(input, ["status"]), output),
  list_audit_records: (input, output) => withCount(firstString(input, ["target_id"]), output),
  get_audit_record: (input) => firstString(input, ["audit_id"]),
  draft_message: (input) => firstString(input, ["contact_id", "intent"]),
  create_account: (input) => firstString(input, ["name"]),
  update_account: (input) => firstString(input, ["account_id"]),
  create_contact: (input) => firstString(input, ["name", "account_id"]),
  update_contact: (input) => firstString(input, ["contact_id"]),
  create_opportunity: (input) => firstString(input, ["name", "account_id"]),
  update_opportunity: (input) => firstString(input, ["opportunity_id"]),
  log_activity: (input) => firstString(input, ["kind", "opportunity_id"]),
  update_activity: (input) => firstString(input, ["activity_id"]),
  create_note: (input) => firstString(input, ["opportunity_id"]),
  submit_proposal: (input) => firstString(input, ["target_id", "target_table"]),
  approve_proposal: (input) => firstString(input, ["proposal_id"]),
  reject_proposal: (input) => firstString(input, ["proposal_id"]),
  rebase_proposal: (input) => firstString(input, ["proposal_id"]),
  revert_audit_record: (input) => firstString(input, ["audit_id"]),
  get_page_context: () => null,
};

/** Whether `name` is a tool this build has a summarizer for. */
function isToolName(name: string | null): name is ToolName {
  return name !== null && Object.prototype.hasOwnProperty.call(SUMMARIZERS, name);
}

/**
 * One tool step: a `tool_call` and, when present, its `tool_result`. `truncated` is taken from
 * whichever frame carries it.
 */
interface Step {
  readonly toolName: string | null;
  readonly input: Record<string, unknown> | null;
  readonly output: Record<string, unknown> | null;
  readonly truncated: boolean;
}

/**
 * One thing the trace shows outside the folded steps: a proposal, an error, or an unknown kind.
 * Carried as the frame plus its position so the sequence with the steps is preserved on render.
 */
interface Aside {
  readonly frame: AgentEventFrame;
  readonly index: number;
}

interface Classified {
  readonly steps: readonly Step[];
  readonly asides: readonly Aside[];
  readonly streaming: boolean;
}

/**
 * Split one turn's frames into folded steps and always-visible asides.
 *
 * `usage` and `completed` are dropped here: `UsageMeter` reports tokens, and the answer bubble
 * renders `completed.text`. A `tool_result` merges into the immediately preceding `tool_call` step;
 * a result with no preceding call still becomes its own step so it is never lost.
 */
function classify(events: readonly AgentEventFrame[]): Classified {
  const steps: Step[] = [];
  const asides: Aside[] = [];
  let terminal = false;
  let lastWasCall = false;

  events.forEach((frame, index) => {
    switch (frame.kind) {
      case "usage":
        return;
      case "text_delta":
        // The streamed answer; the turn's answer bubble renders it.
        return;
      case "completed":
        terminal = true;
        return;
      case "error":
        terminal = true;
        asides.push({ frame, index });
        lastWasCall = false;
        return;
      case "proposal":
        asides.push({ frame, index });
        lastWasCall = false;
        return;
      case "tool_call":
        steps.push({
          toolName: frame.tool_name,
          input: frame.tool_input,
          output: null,
          truncated: frame.truncated,
        });
        lastWasCall = true;
        return;
      case "tool_result": {
        const previous = steps[steps.length - 1];
        if (lastWasCall && previous !== undefined && previous.output === null) {
          steps[steps.length - 1] = {
            ...previous,
            output: frame.tool_output,
            truncated: previous.truncated || frame.truncated,
          };
        } else {
          steps.push({
            toolName: frame.tool_name,
            input: null,
            output: frame.tool_output,
            truncated: frame.truncated,
          });
        }
        lastWasCall = false;
        return;
      }
      default:
        // A kind this build's schema does not declare: never folded, never dropped.
        asides.push({ frame, index });
        lastWasCall = false;
    }
  });

  return { steps, asides, streaming: !terminal };
}

/** The primary line and detail for one step, from `display_label` + its summarizer. */
function stepLabel(step: Step): { label: string; detail: string | null } {
  if (isToolName(step.toolName)) {
    return {
      label: TOOL_METADATA[step.toolName].display_label,
      detail: SUMMARIZERS[step.toolName](step.input, step.output),
    };
  }
  // A tool this build's toolspec does not know: show its raw name rather than a guessed label.
  return { label: step.toolName ?? "Ran a step", detail: null };
}

function StepRow({ step }: { step: Step }): JSX.Element {
  const { label, detail } = stepLabel(step);
  return (
    <li
      data-testid="trace-step"
      data-tool={step.toolName ?? ""}
      className="flex flex-col gap-1 border-l-hairline border-border-subtle pl-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-text-primary">{label}</span>
        {step.truncated ? <Badge variant="caution">{TRUNCATED_LABEL}</Badge> : null}
      </div>
      {detail === null ? null : (
        <p className="text-2xs leading-relaxed text-text-secondary">{detail}</p>
      )}
    </li>
  );
}

/** Badge variant for an aside: error is a fault, an unknown kind is pending. */
function asideVariant(frame: AgentEventFrame): BadgeProps["variant"] {
  if (frame.kind === "error") return "danger";
  return isEventKind(frame.kind) ? "neutral" : "pending";
}

function AsideRow({ frame }: { frame: AgentEventFrame }): JSX.Element {
  const known = isEventKind(frame.kind);
  const isError = frame.kind === "error";
  const label = isError ? "Stopped" : known ? frame.kind : UNKNOWN_KIND_LABEL;
  // For an unknown kind the badge reads "Unrecognised step", so the line carries the raw wire value.
  const line = isError ? errorHeadline(frame) : known ? (frame.text ?? frame.kind) : String(frame.kind);
  const detail = isError ? frame.error_message : frame.text;

  return (
    <li
      data-testid={`trace-row-${frame.kind}`}
      data-kind={frame.kind}
      className="flex flex-col gap-2 border-l-hairline border-border-subtle pl-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={asideVariant(frame)}>{label}</Badge>
        {frame.truncated ? <Badge variant="caution">{TRUNCATED_LABEL}</Badge> : null}
      </div>
      <p className="text-xs leading-relaxed text-text-primary">{line}</p>
      {detail === null || detail === line ? null : (
        <p className="text-2xs leading-relaxed text-text-secondary">{detail}</p>
      )}
      {frame.partial_work === null ? null : (
        <p data-testid="trace-partial-work" className="text-2xs text-text-secondary">
          Work finished before it stopped: {frame.partial_work}
        </p>
      )}
    </li>
  );
}

/**
 * The `decision` prop for one proposal, or nothing when it is still pending. Spread so the pending
 * case is a genuine absence under `exactOptionalPropertyTypes`.
 */
function proposalDecisionProp(
  decisions: Readonly<Record<string, ProposalDecision>> | undefined,
  proposalId: string | null,
): { decision?: ProposalDecision } {
  if (decisions === undefined || proposalId === null) return {};
  const decision = decisions[proposalId];
  return decision === undefined ? {} : { decision };
}

/** The disclosure summary: what the turn is doing, or how many steps it took. */
function stepsSummary(count: number, streaming: boolean): string {
  if (streaming) return count === 0 ? "Working…" : `Working… ${count} step${count === 1 ? "" : "s"}`;
  return `Worked through ${count} step${count === 1 ? "" : "s"}`;
}

/** The folded steps: open while the turn streams, collapsed once it completes, toggleable after. */
function StepDisclosure({ steps, streaming }: { steps: readonly Step[]; streaming: boolean }): JSX.Element {
  const [open, setOpen] = useState(streaming);
  const wasStreaming = useRef(streaming);
  // Collapse exactly once, on the streaming→done transition, without freezing the user's own toggle.
  useEffect(() => {
    if (wasStreaming.current && !streaming) setOpen(false);
    wasStreaming.current = streaming;
  }, [streaming]);

  return (
    <details
      data-testid={TRACE_STEPS_TESTID}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
      className="flex flex-col gap-2"
    >
      <summary className="cursor-pointer text-2xs text-text-secondary">
        {stepsSummary(steps.length, streaming)}
      </summary>
      <ol aria-label="Steps this turn took" className="mt-2 flex flex-col gap-2">
        {steps.map((step, index) => (
          <StepRow key={`step-${index}`} step={step} />
        ))}
      </ol>
    </details>
  );
}

export interface EventTraceProps {
  /** One turn's frames, in the order they arrived. */
  readonly events: readonly AgentEventFrame[];
  readonly onApprove?: (proposalId: string) => void;
  readonly onReject?: (proposalId: string) => void;
  /**
   * How far a decision has got, per `proposal_id`. Absent entries are still pending.
   */
  readonly decisions?: Readonly<Record<string, ProposalDecision>>;
}

/**
 * Render one turn's trace: folded steps plus always-visible asides, in sequence. Returns `null` for
 * a turn with no steps and no asides.
 */
export function EventTrace({
  events,
  onApprove,
  onReject,
  decisions,
}: EventTraceProps): JSX.Element | null {
  const { steps, asides, streaming } = classify(events);
  if (steps.length === 0 && asides.length === 0) return null;
  const anyTruncated = steps.some((step) => step.truncated);

  return (
    <div data-testid={EVENT_TRACE_TESTID} className="flex flex-col gap-3">
      {steps.length === 0 ? null : <StepDisclosure steps={steps} streaming={streaming} />}
      {anyTruncated ? (
        <p data-testid={TRUNCATION_NOTICE_TESTID} className="flex items-center gap-2">
          <Badge variant="caution">{TRUNCATED_LABEL}</Badge>
          <span className="text-2xs text-text-secondary">{TRUNCATION_NOTICE_TEXT}</span>
        </p>
      ) : null}
      {asides.length === 0 ? null : (
        <ol aria-label="What the assistant did" className="flex flex-col gap-3">
          {asides.map(({ frame, index }) =>
            frame.kind === "proposal" ? (
              <li key={`${index}-proposal`}>
                <ProposalCard
                  event={frame}
                  {...(onApprove === undefined ? {} : { onApprove })}
                  {...(onReject === undefined ? {} : { onReject })}
                  {...proposalDecisionProp(decisions, frame.proposal_id)}
                />
              </li>
            ) : (
              <AsideRow key={`${index}-${String(frame.kind)}`} frame={frame} />
            ),
          )}
        </ol>
      )}
    </div>
  );
}
