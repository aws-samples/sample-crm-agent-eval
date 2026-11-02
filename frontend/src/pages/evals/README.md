# Eval dashboard (`/evals`)

Starts evaluation sweeps, follows the focused run to its end, renders the result matrix, and lists
every run. Sweeps run server-side on the `eval_sweep` state machine, so a run keeps going when the
page is closed and is picked up again on return. It is a measurement display, not a comparison:
each property below is enforced by a test.

## Behaviour

| Behaviour | How | Enforced by |
|---|---|---|
| **Server state on mount.** | The first `list` page loads, and the newest non-terminal run is focused and polled. | `evals.test.tsx` ("server state on mount") |
| **Polling.** | A non-terminal focused run is polled with `status` every 3 s (`POLL_INTERVAL_MS`). Polling stops when the backend reports `is_terminal`, and the final state renders without a reload. A failed poll also stops it and says so; reopening the run from history, or a cancel that returns a non-terminal run, resumes it. | `evals.test.tsx` ("polling stops at terminal") |
| **History.** | Newest first, one `eval.runs.LIST_PAGE_SIZE` page at a time, in server order; "Load more runs" follows the opaque `next_cursor`. Each row shows the submit time, mode, status, `N/total` tasks, the starter's rep id and the spend. "Show" focuses the row's run. A failed first page offers "Retry loading runs". | `evals.test.tsx` ("run history") |
| **Stub sweep.** | "Run stub sweep" is shown to everyone and sends `{action: "submit"}` only. The server refuses a caller without full visibility. | `evals.test.tsx` |
| **Repeats.** | A "Repeats" select (1 to `MAX_SWEEP_REPEATS`, mirroring `eval.runs.MAX_SWEEP_REPEATS`; default 1) sits above the stub button, and another inside `LiveSweepDialog`. Both submits, and the dialog's `estimate`, add `repeats` only when it is not 1. The dialog re-fetches the estimate for each selection and keeps "Start live sweep" disabled until that selection's estimate has loaded; the sweep size it shows multiplies tasks × arms × the estimate's `repeats`. A run payload without `repeats` reads as 1 (`types.ts` `runRepeats`). | `evals.test.tsx` ("Repeats") |
| **Live sweep, admins only.** | "Run live sweep…" opens `LiveSweepDialog`, which fetches `estimate` first. "Start live sweep" stays disabled until an estimate has loaded, then sends `{action: "submit", mode: "live", confirmed: true}`. The sweep size is derived from the estimate. When the estimate's figures were measured at a supervisor effort other than the current default (`eval.sweep_units.DEFAULT_ARM_EFFORT`), the payload carries `effort_caveat` (`eval.runs.ESTIMATE_EFFORT_CAVEAT`), and the dialog shows it under the basis line; with no caveat, nothing is shown. A failed start keeps the dialog open with the reason. | `evals.test.tsx` ("live sweep, admins only", the effort-caveat cells), `src/auth/adminGroup.test.ts`, `tests/unit/test_runs_estimate_effort_caveat.py` |
| **Cancel, admins only.** | "Cancel sweep" appears on a `queued` or `running` live run with no cancel requested yet. It is cooperative: the task in flight finishes, and the rest are skipped. | `evals.test.tsx` ("cancel") |
| **Cancelled and failed runs.** | A cancelled run reads `Cancelled after N/total tasks` with its spend, plus partial results when any task finished. A failed run shows the backend's failure summary. | `evals.test.tsx` |

Admin status comes from the ID token's `cognito:groups` (`src/auth/adminGroup.ts`) and is
**cosmetic**. It only decides which controls render. The handler decides who may act
(`crm.handlers.eval_trigger`, `eval:admin`).

## Integrity properties

| Property | How it is rendered | Enforced by |
|---|---|---|
| **Effort labels are not comparable across families.** `low` names a different setting on each family's own ladder. | An annotation inside the Effort field of every arm. | `evals.annotations.test.tsx` |
| **Prompt-cache minimums are per model**, and cache-attributable cost is reported apart from cost excluding cache. | The arm's `cache_min_checkpoint_tokens`, the cache read/write token counts, and two separate cost fields — never one summed figure. | `evals.annotations.test.tsx` |
| **Cost is shown only when the backend marks it publishable.** | Keyed on each record's `cost_withheld_provisional` flag: `true` renders a withheld disclosure, `false` renders the backend-emitted cost fields, absent renders "not displayed". This covers the per-tier summary's `cost_total`. The page computes no cost figure from token counts. | `evals.integrity.test.tsx` (adversarial payloads), `scripts/check_cost_withheld.sh --frontend-dir` (active while `config/pricing.yaml` is `provisional: true`) |
| **Run spend is metered spend, live runs only.** | A live run's `cost_spent_usd` is shown as the backend sent it, except a live run with no measured item (`tasks_done` 0, `tasks_failed` above 0), which reads "not measured" rather than a zero figure, beside the errored-items note. A stub run reads "no inference cost (stub)" rather than a zero figure. An unrecognised mode shows no figure. | `evals.test.tsx`, `evals.allFailed.test.tsx` |
| **No verdict, winner, or ranking.** | Arms render in backend order with identical styling and no sort affordance; nothing labels a model. The history table has no sort affordance either. | `evals.test.tsx` (including a source scan) + `evals.integrity.test.tsx` |

Absent values are named rather than blanked or zeroed: an unscored outcome renders `unscored`, an
absent token count renders `not reported`, a model with no documented cache threshold renders
`no documented threshold`, an unmeasured tier figure renders `—`, and a tier with no applicable turn
reads `not applicable` for its success rate.

## Files

| File | Role |
|---|---|
| `EvalDashboardPage.tsx` | The page: start controls, the focused run's progress and record, results, artifact links, history. |
| `EvalRunClient.ts` | `POST {apiBaseUrl}/eval_trigger` for `submit`, `status`, `list`, `estimate`, `cancel` and `artefact_url`, selected by the `action` field (`src/crm/handlers/eval_trigger.py`). It refuses a payload whose branch fields it cannot read, including an estimate whose `effort_caveat` is present but not a string. |
| `useEvalRun.ts` | The focused run: start (stub or confirmed live, with repeats), poll to terminal, cancel, focus from history. |
| `useRunHistory.ts` | The history pages, plus merging in runs this page learns about from a submit or a poll. It also holds the shared status, `N/total` and spend descriptions. |
| `RunHistory.tsx` | The history table. |
| `LiveSweepDialog.tsx` | The Repeats select (`RepeatsSelect`, also used for the stub sweep), the estimate for the selected repeats with its effort caveat when the payload carries one, then the confirmed live submit. |
| `EvalArmResults.tsx` | One arm: the mode-provenance disclosure, the annotated measurement list, the per-tier summary (`tier_summary`), the per-tier/per-tool blocks, and the per-task table. |
| `EvalArtifactList.tsx` | Opens `matrix.json`, `matrix.md`, `matrix.csv` and the raw per-run JSON. The bucket is private: a click asks `artefact_url` for a presigned GET (valid 300 s, admin or full visibility, only for a key the run recorded) and opens it in a new tab; a refusal is shown as an alert naming the artifact. An unavailable artifact keeps its row and says so. |
| `runMode.ts` | The single classifier for a run's reported `mode`: `stub`, `live`, or `unrecognised`. |
| `types.ts` | Hand-written mirror of the harness's wire shapes. |
| `testFixtures.ts` | `jobPayload()` and `BaseFakeEvalRunClient`, a compliant backend for the tests. |

## `types.ts` has no generation step

`types.ts` mirrors the `to_report_fields()` output of `src/eval/matrix.py`, `src/eval/harness.py` and
`src/eval/metrics.py`. The type generator covers the CRM tool surface only, so a renamed Python field
renders as `undefined` without failing any build. Update `types.ts` in the same change as any
`to_report_fields()` key.

The run payload (`RunRecord.to_payload()`) is held to `EvalRunClient.ts` by
`tests/unit/test_eval_run_status_contract.py`, in both directions. The `list`, `estimate` and
`cancel` envelopes are not covered by that test; they are typed by hand here.

## Design notes

Tokens only — no literal colour, `px` font size, or off-scale spacing (`scripts/check_design_tokens.py`).

- The two arms are styled identically and do not use the model-family series marks. Identity is the
  model id.
- Figures use the numeric face and prose the sans face. A field absent by policy also carries a dashed
  border, so "withheld" does not rely on colour alone.
- Only the focused run and the live-sweep dialog raise `role="alert"`. A failed run in the history table is plain text.

## Running the checks

From the repository root:

```bash
(cd frontend && npm run test -- src/pages/evals src/auth)   # this directory and adminGroup
(cd frontend && npm run typecheck && npm run lint)
.venv/bin/python -m pytest tests/unit/test_eval_run_status_contract.py
bash scripts/check_cost_withheld.sh                         # scans this directory
.venv/bin/python3 scripts/check_design_tokens.py
.venv/bin/python3 scripts/check_testid_preservation.py
```

`check_cost_withheld.sh` scans only while `config/pricing.yaml` is `provisional: true`. While it is
`provisional: false`, the script performs no check and exits 0.

The browser journey for this page is `tests/e2e/browser/journeys/08-eval-dashboard.spec.ts`. Its
selectors (each arm as a region named by its model id, the `mode-label-` ordering, the absence of a
sort affordance) are also pinned at the unit tier in `evals.annotations.test.tsx`.

## See also

- [Repository documentation map](../../../../README.md#documentation)
- [Frontend README](../../../README.md)
- [`docs/eval-methodology.md`](../../../../docs/eval-methodology.md), for what the sweep measures and
  how the matrix this page renders is built
- [T4 browser journeys](../../../../tests/e2e/browser/journeys/README.md)
