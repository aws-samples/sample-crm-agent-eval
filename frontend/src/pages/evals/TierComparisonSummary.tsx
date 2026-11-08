// SPDX-License-Identifier: MIT
/**
 * The run's tier comparison summary: one table per tier (Supervisor, Subagent, ToolCall) with a
 * `Metric` column and one column per arm in run order, then the notes that define each row.
 *
 * Every header, cell and note is the backend's string (`tier_comparison_table`), rendered verbatim,
 * so the page shows exactly the section `matrix.md` carries. Notes are markdown there; their inline
 * code spans (`` `x` ``) render as `<code>`, and nothing else in a note is interpreted. Columns share
 * one style and offer no sort control. Without a table the section says the summary is not available.
 */
import type { JSX } from "react";

import type { EvalTierComparisonTable } from "./types";

export const TIER_COMPARISON_UNAVAILABLE = "Tier comparison summary is not available for this run.";

const INTRO =
  "Per-tier figures for each arm, one column per arm in run order; the notes below the tables " +
  "define each row.";

const SECTION = "rounded-surface border border-hairline border-border-subtle bg-surface-raised";
const HEADING =
  "border-b-hairline border-border-subtle px-6 py-5 font-sans text-lg font-semibold leading-snug text-text-primary";
const PROSE = "max-w-prose px-6 pt-5 font-sans text-sm leading-relaxed text-text-secondary";
const TABLE_WRAP = "w-full overflow-x-auto";
const TABLE = "w-full min-w-max border-collapse font-sans text-sm text-text-primary";
const CAPTION =
  "px-6 pb-2 pt-6 text-left font-sans text-sm font-semibold leading-snug text-text-primary";
const COLUMN_HEADER =
  "h-row-dense px-cell-x text-left align-middle font-sans text-2xs font-semibold text-text-secondary";
const ROW_HEADER =
  "px-cell-x py-cell-y text-left align-middle font-sans text-xs font-medium text-text-primary";
const CELL = "px-cell-x py-cell-y align-middle font-numeric text-xs tabular-nums text-text-primary";
const NOTES = "flex max-w-prose list-disc flex-col gap-2 px-6 pb-6 pl-10 pt-5";
const NOTE = "font-sans text-2xs leading-normal text-text-muted";
const NOTE_CODE = "font-numeric text-2xs text-text-primary";
const UNAVAILABLE =
  "m-6 rounded-control border border-hairline border-dashed border-empty-border bg-empty-surface px-5 py-4 font-sans text-sm leading-snug text-empty-text";

/** One run of a note's text: inside an inline code span or not. */
interface NotePart {
  readonly code: boolean;
  readonly text: string;
}

/**
 * Split a markdown note into its inline code spans and the text around them. A note with an unpaired
 * backtick is returned whole, as plain text.
 */
function noteParts(note: string): readonly NotePart[] {
  const pieces = note.split("`");
  if (pieces.length % 2 === 0) return [{ code: false, text: note }];
  return pieces
    .map((text, index) => ({ code: index % 2 === 1, text }))
    .filter((part) => part.code || part.text !== "");
}

export interface TierComparisonSummaryProps {
  /** The run payload's `tier_comparison_table`; `undefined` renders the unavailable state. */
  readonly table?: EvalTierComparisonTable;
}

export function TierComparisonSummary({ table }: TierComparisonSummaryProps): JSX.Element {
  return (
    <section
      aria-labelledby="tier-comparison-heading"
      data-testid="tier-comparison-summary"
      className={SECTION}
    >
      <h3 id="tier-comparison-heading" className={HEADING}>
        Tier comparison summary
      </h3>
      {table === undefined ? (
        <p role="status" data-testid="tier-comparison-unavailable" className={UNAVAILABLE}>
          {TIER_COMPARISON_UNAVAILABLE}
        </p>
      ) : (
        <>
          <p className={PROSE}>{INTRO}</p>
          {table.sections.map((section) => (
            <div key={section.tier} className={TABLE_WRAP}>
              <table data-testid={`tier-comparison-table-${section.tier}`} className={TABLE}>
                <caption className={CAPTION}>{section.heading}</caption>
                <thead className="border-b-hairline border-border-default">
                  <tr>
                    {table.headers.map((header, index) => (
                      <th key={`${index}#${header}`} scope="col" className={COLUMN_HEADER}>
                        {header}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {section.rows.map(([label, ...cells], rowIndex) => (
                    <tr
                      key={`${rowIndex}#${label ?? ""}`}
                      className="border-b-hairline border-border-subtle last:border-b-0"
                    >
                      <th scope="row" className={ROW_HEADER}>
                        {label}
                      </th>
                      {cells.map((cell, cellIndex) => (
                        <td key={cellIndex} className={CELL}>
                          {cell}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
          <ul aria-label="Notes on the tier comparison summary" className={NOTES}>
            {table.notes.map((note, index) => (
              <li key={index} className={NOTE}>
                {noteParts(note).map((part, partIndex) =>
                  part.code ? (
                    <code key={partIndex} className={NOTE_CODE}>
                      {part.text}
                    </code>
                  ) : (
                    part.text
                  ),
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
