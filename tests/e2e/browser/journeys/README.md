# T4 browser journeys

Playwright specs driven through the browser against a **deployed dev stack**. Each spec's docstring
describes what it asserts and its declared limits.

| Spec | What it asserts |
|---|---|
| `01-signin` | A real sign-in through the SPA's form, against the Cognito pool, lands on a scoped application; before sign-in, a protected route renders no CRM data |
| `02-pipeline` | A keyboard-driven stage change on the board yields exactly one `submit_proposal` call and no `update_opportunity` call; a transition out of Closed Won is refused and offers no way to submit |
| `03-approvals` | An approver sees a before/after diff distinguished by more than colour, approves by keyboard, the change is audited, and a second approval is refused as a conflict |
| `04-audit-revert` | An audited write reverts once; a second revert is refused (disabled control, `Already reverted` label, `Reverted` badge), including from a stale tab; the revert time is read from the record's detail view |
| `05-panel-context` | The panel's page-context descriptor tracks the view's applied filters, and a row-count divergence is disclosed whenever the counts cannot be shown to match |
| `06-panel-proposal` | Asking the assistant for a high-risk change yields a proposal card, with no direct-commit tool in the transcript, the record still in its column under a pending lock, and no applied change in the audit trail |
| `07-panel-cancel` | Cancelling after a write's `tool_result` stops the stream, the write stays committed and audited, and the transcript does not claim it was undone |
| `08-eval-dashboard` | A stub sweep runs and renders with no verdict, winner or ranking. Cost follows `config/pricing.yaml`'s `provisional:` line (`pricingIsProvisional()`): while `true`, neither the page nor the raw records carry a currency figure and every arm discloses the withholding; while `false`, each arm shows exactly the cost figures its record carries and no withheld text |
| `09-panel-focus-and-live-region` | Closing the panel with Escape returns focus to the toggle rather than `<body>`, and the transcript has exactly one `polite` live region |
| `10-three-tier-delegation` | A real turn's trace shows a step whose `data-tool` is a subagent role (`research`, `mutation`, `analytics`, `composer`), followed by that subagent's tool calls |
| `11-steps-and-markdown-answer` | On completion the step disclosure collapses ("Worked through N steps") and the answer renders as markdown, not raw text |
| `12-multi-turn-history` | A follow-up turn adds a second answered bubble to the same conversation, and "New conversation" clears the transcript |
| `13-streaming-answer` | The answer streams into an `aria-hidden` streaming container, which is replaced by the final markdown answer on `completed` |
| `14-tier-tuning` | Saved per-tier tuning (mixed families) survives a reload and reaches every tier of the next turn's `resolved_tiers`; reset restores defaults. No tier shows a temperature control. An effort the model does not accept is rejected in the page (no save sent) and by the API (400 `validation_failed`, source `tuning.tiers.<tier>.effort`), and is not saved; a stale schema-2 block carrying `temperature` is refused by the API (400 `validation_failed`, source `tuning.tiers.<tier>.temperature`) and not saved. The initial page puts every tier on the page's own `/api/capabilities` `default_family` and selects each tier's model and effort as `tiers[default_family]` names them; switching every tier to the other served family selects that family's models and efforts. With no saved tuning, the next turn's `resolved_tiers` puts every tier on `default_family` with `tiers[default_family]`'s model and effort |
| `15-live-sweep-admin` | A non-admin sees no live control and a live submit is refused 403 `eval:admin`; an admin sees the estimate (size, basis) before anything is submitted; a second sweep while one runs is refused 409; the live dialog offers Repeats from 1 to the server's bound (the next value is refused 400 `validation_failed`, source `repeats`), defaulting to 1. Opt-in: a confirmed live sweep (1 repeat) completes with a per-tier report per arm, and each arm's raw artefact scores the `gt-24` multi-step write with no `AccessDenied` in its assertion details |
| `16-sweep-cancel-resume` | Leaving and returning shows the server's state of a sweep, and history lists it newest first with its starter; a repeated or terminal cancel is a no-op (`already_requested` / `already_terminal`); a cancelled stub sweep at 2 repeats is sized tasks × arms × repeats and records every item it did not run as skipped (done + failed + skipped = total). Opt-in: a UI cancel of a live sweep ends it with N/total tasks and its spend |
| `17-audit-detail` | An audit card opens `/audit/:id` with metadata and the diff (update before/after, create after-only, delete before-only), and reverts there. A record written without snapshots is covered by vitest (`frontend/src/pages/audit/AuditDetailPage.test.tsx`), not here |
| `18-approval-rebase` | An unrelated `log_activity` write does not block approval; a write to a touched field shows the three-way conflict view, and "Refresh and review current state" rebases it so approval succeeds |
| `19-account-opportunities` | An account's detail page lists its opportunities in the shared table; an account with none shows "No opportunities yet." and no table |
| `20-chat-cost` | Each reply shows its cost with a per-tier breakdown and the header totals the conversation; the withheld text appears only when `config/pricing.yaml` is `provisional: true` |

Journeys 14–20 sit in one directory each.

## Running them

Two static gates, both needed — they fail on different things:

```bash
./node_modules/.bin/tsc --noEmit -p tsconfig.json   # types; the only gate that sees a bad type import
./node_modules/.bin/playwright test --list          # collection; transpiles with esbuild, no type check
```

Use the pinned local binaries. `npx --yes tsc` resolves to an unrelated public-registry package.

A real run is billable and needs a deployed dev stack. Its entry point is
`scripts/run_browser_journeys_container.sh`, from the repository root, which runs the journeys in the
official Playwright Linux image; see "T4 browser journeys in the Playwright container" in
[`docs/runbook.md`](../../../../docs/runbook.md#t4-browser-journeys-in-the-playwright-container):

```bash
scripts/run_browser_journeys_container.sh                              # every journey
scripts/run_browser_journeys_container.sh journeys/01-signin.spec.ts   # one journey
```

Specs run serially (`workers: 1`).

The container script then runs `scripts/check-executed.mjs` on the JSON report, and the run fails
when nothing executed, when a skip is not one of its `ALLOWED_SKIPS` (the `02` and `09` data guards,
and the `15` and `16` live tests while their opt-in is not `1`), or when the report is missing or
unreadable. `--list` through the container script therefore fails; the local `--list` above is the
collection gate. Details are in the runbook section linked above.

## What refuses a run

`playwright.config.ts` discovers the base URL and `fixtures/globalSetup.ts` refuses rather than
guessing. There is no `localhost` default. Refusals, in the order checked:

1. **Unresolved** — no base URL from `CRM_E2E_BASE_URL`, `CRM_TF_OUTPUTS_JSON`,
   `results/tf-outputs.json`, or `terraform output`.
2. **Production** — the target contains `prod`, `production` or `prd`. These journeys create
   proposals, approve them and revert audit records.
3. **Stale** — the outputs snapshot no longer matches `infra/`. `scripts/outputs_provenance.py --check`
   decides: `scripts/deploy.sh` stamps the gitignored `results/tf-outputs.json` with the commit it was
   captured from, and the snapshot is stale when any `infra/**/*.tf` differs from that commit. A
   snapshot with no stamp falls back to an mtime comparison, and so does a host with no `.venv`.
   That fallback cannot detect staleness on a fresh clone. This is a different check from the
   `plan_freshness` gate, which compares the plan fixture's recorded infra content digest with the
   working tree's.
4. **Incomplete** — the stack is missing an output sign-in needs.

`scripts/deploy.sh` writes and stamps `results/tf-outputs.json` after a successful apply. To record it
by hand, stamp it too:

```bash
terraform -chdir=infra output -json > results/tf-outputs.json
.venv/bin/python3 scripts/outputs_provenance.py --stamp results/tf-outputs.json
```

The stamp records the commit the outputs were captured at, which the freshness comparison uses. An
unstamped document falls back to a weaker mtime comparison, and `scripts/outputs_provenance.py`
reports which form answered.

`globalSetup` does not run for `--list`, so the inventory can be enumerated offline.

## Conventions

- **Each journey is self-sufficient.** A spec creates the state it asserts on rather than inheriting it
  from an earlier file, so `--grep` on one spec behaves like a full run.
- **Data preconditions are asserted, not skipped**, with a failure message naming
  `scripts/seed_demo_tenant.py`. Two tests skip conditionally:
  - `02-pipeline`, "a refused transition is explained and offers no way to submit it", skips when the
    Closed Won column holds no record, since there is then no terminal transition to refuse.
  - `09-panel-focus-and-live-region`, "does not move focus into the panel's toggle on a plain page
    load", skips when the panel was restored open on load, since that load is not a plain closed load.

  The live-sweep tests in `15` and `16` also skip, but only on the billing opt-in (see Billable steps).
  They do not skip on any data condition.
- **Never assert model prose.** Assert tool invocations, structured fields, rendered elements and side
  effects.
- **Journeys import no application code.** The package installs Playwright and the compiler only.
- **Every test that writes CRM data cleans up after itself.** It takes `test` from
  `fixtures/teardown.ts` and the `writes` ledger. Every proposal reason and created name goes
  through `writes.tag(…)`, which appends a marker unique to the run and the test (`CRM_E2E_RUN_ID`
  sets the run part; it defaults to a fresh id per invocation). An assistant-raised proposal is
  registered with `writes.proposal(id)`, a direct create with `writes.created(id)`. After the test,
  the ledger rejects its pending proposals and reverts its audit records, newest first, as the demo
  user. Anything it cannot undo fails the test with the full list. It never touches data without
  the marker or a registration. `tests/unit/test_e2e_browser_scaffold.py` pins which tests write.

## Prerequisites a run needs

- A seeded `demo` tenant — `scripts/seed_demo_tenant.py`.
- A demo user — `scripts/provision_demo_user.py --skip-seed`, plus `CRM_DEMO_USER_EMAIL` and
  `CRM_DEMO_USER_PASSWORD`, which `fixtures/auth.ts` reads.
- An admin user for `15-live-sweep-admin` and `16-sweep-cancel-resume`. This is a Cognito user in the
  `admin` group with full visibility, and its credentials are passed in `CRM_E2E_ADMIN_EMAIL` and
  `CRM_E2E_ADMIN_PASSWORD` (`signInAs(page, "admin")` in `fixtures/auth.ts`). Provision it with
  `scripts/provision_demo_user.py --admin` and an `--email` of its own (`docs/runbook.md`, "Bringing up
  the UI").
- The demo user must **not** be in `admin`. `08-eval-dashboard` and the non-admin half of `15` depend
  on that. All other journeys sign in as the demo user.
- A Docker CLI and daemon for `scripts/run_browser_journeys_container.sh`; the Playwright image
  supplies the browser.

## Billable steps

Every journey that sends a chat turn calls a real model, including `14` and `20`.

Starting a **live** sweep is gated per journey, and each opt-in starts at most one live sweep:
- `15-live-sweep-admin` runs its live completion only when `CRM_E2E_ALLOW_LIVE_SWEEP=1`.
- `16-sweep-cancel-resume` runs its live UI cancel only when `CRM_E2E_ALLOW_LIVE_SWEEP_CANCEL=1`.

`test_each_live_sweep_start_sits_under_its_own_opt_in` pins this. Each live sweep is billable and needs
its own authorization; setting both variables starts two sweeps.

No in-page API probe (`fixtures/pageApi.ts`) ever sends
`{mode: "live", confirmed: true}`. Single-flight is probed with stub sweeps, and the non-admin refusal
is probed with `confirmed: false`.

## See also

- [Repository documentation map](../../../../README.md#documentation)
- [`docs/runbook.md`, T4 browser journeys in the Playwright container](../../../../docs/runbook.md#t4-browser-journeys-in-the-playwright-container)
- [`docs/development.md`](../../../../docs/development.md), for the test tiers and the journeys' static
  gates
- [Frontend README](../../../../frontend/README.md), for the SPA these journeys drive
