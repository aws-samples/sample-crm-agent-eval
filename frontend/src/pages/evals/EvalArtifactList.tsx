// SPDX-License-Identifier: MIT
/**
 * Opens the four artifacts a completed run produces: `matrix.json` (full detail),
 * `matrix.md` (the human-readable file the README references), `matrix.csv` (flat per-task rows),
 * and the per-run raw JSON archived beneath `raw/` (one file per arm).
 *
 * The artefacts bucket is private, so each control asks the run API for a short-lived presigned
 * URL (`EvalRunClient.getArtefactUrl`) when clicked and opens it in a new tab. A refused or failed
 * request is shown as an alert naming the artifact. An unavailable artifact keeps its row and is
 * labelled unavailable.
 */
import { useState } from "react";
import type { JSX } from "react";

import type { EvalRunClient } from "./EvalRunClient";
import type { EvalArtifactLinks } from "./types";

const ROW = "flex flex-col gap-1 border-b-hairline border-border-subtle px-6 py-4 last:border-b-0";
const LINK =
  "self-start text-left font-numeric text-xs text-text-link underline-offset-4 hover:underline disabled:cursor-wait disabled:opacity-60 focus-visible:outline-none focus-visible:ring-focus focus-visible:ring-focus-ring focus-visible:ring-offset-focus focus-visible:ring-offset-surface-raised";
const UNAVAILABLE = "font-sans text-2xs leading-snug text-empty-text";
const FAILURE = "px-6 py-4 font-sans text-2xs leading-snug text-danger-text";

export interface EvalArtifactListProps {
  readonly artifacts: EvalArtifactLinks;
  readonly runId: string;
  readonly client: Pick<EvalRunClient, "getArtefactUrl">;
}

export function EvalArtifactList({ artifacts, runId, client }: EvalArtifactListProps): JSX.Element {
  // `raw` is typed as present but not enforced at runtime.
  const raw = Array.isArray(artifacts.raw) ? artifacts.raw : [];
  const rawCount = raw.length;
  const [opening, setOpening] = useState<string | undefined>(undefined);
  const [failure, setFailure] = useState<string | undefined>(undefined);

  async function open(key: string): Promise<void> {
    // Opened before the request so the browser treats the new tab as the click's own.
    const tab = window.open("", "_blank");
    if (tab) tab.opener = null;
    setOpening(key);
    setFailure(undefined);
    try {
      const { url } = await client.getArtefactUrl(runId, key);
      if (tab) {
        tab.location.href = url;
      } else {
        window.location.assign(url);
      }
    } catch (err) {
      tab?.close();
      const reason = err instanceof Error && err.message ? err.message : "the request failed";
      setFailure(`Could not open ${key}: ${reason}`);
    } finally {
      setOpening(undefined);
    }
  }

  function control(key: string, label: string): JSX.Element {
    return (
      <button
        type="button"
        className={LINK}
        onClick={() => void open(key)}
        disabled={opening !== undefined}
        aria-busy={opening === key}
      >
        {label}
      </button>
    );
  }

  return (
    <section
      aria-labelledby="eval-artifacts-heading"
      className="rounded-surface border border-hairline border-border-subtle bg-surface-raised"
    >
      <h3
        id="eval-artifacts-heading"
        className="border-b-hairline border-border-subtle px-6 py-5 font-sans text-lg font-semibold leading-snug text-text-primary"
      >
        Generated artifacts
      </h3>
      {failure !== undefined ? (
        <p role="alert" className={FAILURE}>
          {failure}
        </p>
      ) : null}
      <ul>
        <li className={ROW}>
          {artifacts.matrixJson ? (
            control(artifacts.matrixJson, "matrix.json (full detail)")
          ) : (
            <span className={UNAVAILABLE}>matrix.json — not available</span>
          )}
        </li>
        <li className={ROW}>
          {artifacts.matrixMd ? (
            control(artifacts.matrixMd, "matrix.md (human-readable, referenced by the README)")
          ) : (
            <span className={UNAVAILABLE}>matrix.md — not available</span>
          )}
        </li>
        <li className={ROW}>
          {artifacts.matrixCsv ? (
            control(artifacts.matrixCsv, "matrix.csv (flat per-task rows)")
          ) : (
            <span className={UNAVAILABLE}>matrix.csv — not available</span>
          )}
        </li>
        <li className={ROW}>
          {rawCount > 0 ? (
            <>
              <span className="font-sans text-2xs leading-snug text-text-secondary">
                Raw per-run JSON (
                {rawCount} file{rawCount === 1 ? "" : "s"} under{" "}
                <code className="font-numeric text-2xs text-text-primary">raw/</code>):
              </span>
              <ul className="flex flex-col gap-1 pl-5">
                {raw.map((path) => (
                  <li key={path}>{control(path, path)}</li>
                ))}
              </ul>
            </>
          ) : (
            <span className={UNAVAILABLE}>raw per-run JSON — not available</span>
          )}
        </li>
      </ul>
    </section>
  );
}
