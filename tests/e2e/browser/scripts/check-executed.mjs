#!/usr/bin/env node
// SPDX-License-Identifier: MIT
//
// Fails a browser-journey run that executed nothing or skipped a test it did not declare (TB-20).
//
// Usage: node scripts/check-executed.mjs <playwright json report>
//
// Reads the JSON reporter's output. Executed = stats.expected + stats.unexpected + stats.flaky.
// Every skipped test must match an ALLOWED_SKIPS entry by spec file (relative to `testDir`) and
// title; an entry with `optIn` is allowed only while that environment variable is not "1".
//
// Exit codes: 0 executed > 0 and every skip declared; 1 nothing executed, an undeclared skip, or an
// unreadable / malformed report; 2 usage error.

import { readFileSync, realpathSync } from "node:fs";
import { pathToFileURL } from "node:url";

/** Why journey 01's Hosted UI cases skip on a stack whose SPA signs in without the Hosted UI. */
const HOSTED_UI_NOT_CONFIGURED =
  "config-guarded: the deployed /config.json has an empty cognitoHostedUiDomain " +
  "(Hosted UI not configured)";

/** Skips a run may report, each with why it is legitimate. */
export const ALLOWED_SKIPS = Object.freeze([
  {
    file: "01-signin.spec.ts",
    title: "after sign-out, a Hosted UI sign-in asks for credentials again",
    reason: HOSTED_UI_NOT_CONFIGURED,
  },
  {
    file: "01-signin.spec.ts",
    title: "a Hosted UI logout with no session still lands on the signed-out page",
    reason: HOSTED_UI_NOT_CONFIGURED,
  },
  {
    file: "02-pipeline.spec.ts",
    title: "a refused transition is explained and offers no way to submit it",
    reason: "data-guarded: the tenant's book holds no Closed Won record",
  },
  {
    file: "09-panel-focus-and-live-region.spec.ts",
    title: "does not move focus into the panel's toggle on a plain page load",
    reason: "data-guarded: the panel was restored open",
  },
  {
    file: "15-live-sweep-admin/live-sweep-admin.spec.ts",
    title: "a confirmed live sweep runs to completed with a per-tier report for each arm",
    optIn: "CRM_E2E_ALLOW_LIVE_SWEEP",
    reason: "billable live sweep, opt-in",
  },
  {
    file: "16-sweep-cancel-resume/sweep-cancel-resume.spec.ts",
    title: "cancelling a running live sweep from the page ends it cancelled with N/total and its spend",
    optIn: "CRM_E2E_ALLOW_LIVE_SWEEP_CANCEL",
    reason: "billable live sweep cancel, opt-in",
  },
]);

/** Every `{file, title, status}` in a report's suite tree, depth first. */
export function reportTests(report) {
  const found = [];
  const walk = (suite) => {
    for (const spec of suite.specs ?? []) {
      for (const test of spec.tests ?? []) {
        found.push({ file: spec.file, title: spec.title, status: test.status });
      }
    }
    for (const child of suite.suites ?? []) walk(child);
  };
  for (const suite of report.suites ?? []) walk(suite);
  return found;
}

/** Whether `skipped` is declared in ALLOWED_SKIPS under environment `env`. */
export function isAllowedSkip(skipped, env) {
  return ALLOWED_SKIPS.some(
    (entry) =>
      entry.file === skipped.file &&
      entry.title === skipped.title &&
      (entry.optIn === undefined || env[entry.optIn] !== "1"),
  );
}

/** Return the list of problems with `report` under `env`; empty means the run counts. */
export function problems(report, env) {
  const stats = report?.stats;
  const counts = ["expected", "unexpected", "flaky", "skipped"].map((key) => stats?.[key]);
  if (!counts.every((value) => Number.isInteger(value))) {
    return ["the report carries no integer stats.expected/unexpected/flaky/skipped"];
  }
  const [expected, unexpected, flaky] = counts;
  const found = [];
  if (expected + unexpected + flaky === 0) {
    found.push(`nothing executed (${stats.skipped} skipped): the run verified nothing`);
  }
  for (const test of reportTests(report)) {
    if (test.status === "skipped" && !isAllowedSkip(test, env)) {
      found.push(`undeclared skip: ${test.file} › ${test.title}`);
    }
  }
  return found;
}

function main(argv, env) {
  if (argv.length !== 1) {
    process.stderr.write("usage: node scripts/check-executed.mjs <playwright json report>\n");
    return 2;
  }
  let report;
  try {
    report = JSON.parse(readFileSync(argv[0], "utf-8"));
  } catch (error) {
    process.stderr.write(`check-executed: FAIL — cannot read ${argv[0]}: ${error.message}\n`);
    return 1;
  }
  const found = problems(report, env);
  for (const problem of found) process.stderr.write(`check-executed: ${problem}\n`);
  if (found.length > 0) {
    process.stderr.write(`check-executed: FAIL — ${found.length} problem(s)\n`);
    return 1;
  }
  const { expected, unexpected, flaky, skipped } = report.stats;
  process.stdout.write(
    `check-executed: OK — ${expected + unexpected + flaky} executed, ${skipped} declared skip(s)\n`,
  );
  return 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  process.exitCode = main(process.argv.slice(2), process.env);
}
