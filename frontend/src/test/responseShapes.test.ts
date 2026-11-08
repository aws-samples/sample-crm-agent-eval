// SPDX-License-Identifier: MIT
/**
 * Pins the wire response shapes the frontend reads against the real Python handlers. Runs
 * `export_response_shapes.py` (which executes the `crm.handlers` action Lambdas against a
 * `moto`-mocked DynamoDB store) via `execFileSync` and asserts each tool's real body carries the
 * declared envelope and every key the frontend's readers use.
 *
 * The exporter's tool list is derived from `crm.toolspec.definitions.ALL_TOOLS`; the first two
 * assertions fail if a tool drops out of coverage.
 */
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, "..", "..", "..");

function runPythonExporter(): Record<string, string[]> {
  const venvPython = resolve(repoRoot, ".venv", "bin", "python3");
  const pythonBin = existsSync(venvPython) ? venvPython : "python3";
  const raw = execFileSync(
    pythonBin,
    [resolve(repoRoot, "frontend", "scripts", "export_response_shapes.py")],
    { cwd: repoRoot, encoding: "utf-8" },
  );
  return JSON.parse(raw) as Record<string, string[]>;
}

/**
 * The top-level envelope every frontend-called handler returns, one entry per tool the exporter
 * reports.
 *
 * A set-equality guard below requires this table and the exporter's report to name the same tools.
 *
 * Only the top-level keys are pinned. Where a nested key is part of the contract —
 * `submit_proposal.proposal.proposal_id` — it is asserted explicitly further down.
 */
const EXPECTED_ENVELOPES: Readonly<Record<string, readonly string[]>> = {
  approve_proposal: ["proposal"],
  create_account: ["account"],
  create_contact: ["contact"],
  create_note: ["note"],
  create_opportunity: ["opportunity"],
  draft_message: ["contact_id", "draft", "tone"],
  get_account: ["account", "contacts"],
  get_audit_record: ["audit_record"],
  get_contact: ["contact"],
  get_opportunity: ["activities", "contacts", "notes", "opportunity"],
  list_activities: ["activities", "next_cursor"],
  list_audit_records: ["audit_records", "next_cursor"],
  list_proposals: ["next_cursor", "proposals"],
  log_activity: ["activity", "opportunity"],
  pipeline_aggregate: ["forecast_by_close_date", "stage_funnel", "truncated", "value_rollup"],
  rebase_proposal: ["proposal"],
  reject_proposal: ["proposal"],
  revert_audit_record: ["audit_record"],
  search_accounts: ["examined_count", "items", "next_cursor", "truncated"],
  search_opportunities: ["examined_count", "items", "next_cursor", "truncated"],
  submit_proposal: ["proposal"],
  update_account: ["account"],
  update_activity: ["activity"],
  update_contact: ["contact"],
  update_opportunity: ["opportunity"],
};

describe("wire response shapes (derived from crm.handlers, not restated)", () => {
  const shapes = runPythonExporter();

  /** Top-level tool entries, excluding the `tool.field` nested-record entries. */
  const reportedTools = Object.keys(shapes)
    .filter((key) => !key.includes("."))
    .sort();

  it("ran the exporter and got shapes at all — otherwise every assertion below is vacuous", () => {
    // A missing `.venv` or an empty exporter would otherwise leave every assertion below vacuous.
    expect(reportedTools.length).toBeGreaterThan(0);
    for (const tool of reportedTools) {
      expect(Array.isArray(shapes[tool]), tool).toBe(true);
      expect(shapes[tool].length, `${tool} returned an empty body`).toBeGreaterThan(0);
    }
  });

  it("asserts every tool the exporter reports — neither side may narrow silently", () => {
    // A tool added to `ALL_TOOLS` fails here until its envelope is declared above.
    expect(reportedTools).toEqual(Object.keys(EXPECTED_ENVELOPES).sort());
  });

  it.each(Object.keys(EXPECTED_ENVELOPES).sort())(
    "%s: the real handler body carries exactly the declared envelope",
    (tool) => {
      expect([...shapes[tool]].sort()).toEqual([...EXPECTED_ENVELOPES[tool]].sort());
    },
  );

  it("no envelope uses a paging key other than `next_cursor`", () => {
    for (const tool of reportedTools) {
      expect(shapes[tool], `${tool} declares a bare 'cursor'`).not.toContain("cursor");
    }
  });


  /* Every tool `CrmApiClient` posts to is covered by the exporter. */
  it("covers every tool the frontend's API client calls", () => {
    const clientSource = readFileSync(
      resolve(repoRoot, "frontend", "src", "api", "CrmApiClient.ts"),
      "utf-8",
    );
    const called = [...clientSource.matchAll(/this\.post\("([a-z_]+)"/g)].map((m) => m[1]);
    expect(called.length).toBeGreaterThan(15);

    const covered = new Set(Object.keys(shapes).filter((key) => !key.includes(".")));
    expect([...new Set(called)].filter((tool) => !covered.has(tool)).sort()).toEqual([]);
  });

  it("list_activities: the activities page reads `activities`, never `items`", () => {
    expect(shapes.list_activities).toEqual(["activities", "next_cursor"]);
    expect(shapes.list_activities).not.toContain("items");
    expect(shapes.list_activities).not.toContain("cursor");
  });

  it("list_proposals: the approvals page and pipeline board read `proposals`, never `items`", () => {
    expect(shapes.list_proposals).toEqual(["next_cursor", "proposals"]);
    expect(shapes.list_proposals).not.toContain("items");
    expect(shapes.list_proposals).not.toContain("cursor");
  });

  it("get_opportunity: the detail page reads opportunity, contacts, notes and activities", () => {
    expect(shapes.get_opportunity).toEqual(["activities", "contacts", "notes", "opportunity"]);
  });

  it("get_account: the detail page reads `account` and `contacts`, and no `opportunities`", () => {
    expect(shapes.get_account).toEqual(["account", "contacts"]);
  });

  it("get_contact: the contact pages read a nested `contact`", () => {
    expect(shapes.get_contact).toEqual(["contact"]);
  });

  it("pipeline_aggregate: the three rollups plus the truncation flag the handler returns", () => {
    expect(shapes.pipeline_aggregate).toEqual([
      "forecast_by_close_date",
      "stage_funnel",
      "truncated",
      "value_rollup",
    ]);
  });

  it("every list tool pages on `next_cursor`, never `cursor`", () => {
    for (const tool of ["search_opportunities", "list_activities", "list_proposals", "list_audit_records"]) {
      expect(shapes[tool], tool).toContain("next_cursor");
      expect(shapes[tool], tool).not.toContain("cursor");
    }
  });

  it("search_opportunities: the pipeline board and opportunity list read `items`", () => {
    expect(shapes.search_opportunities).toContain("items");
    expect(shapes.search_opportunities).not.toContain("opportunities");
    expect(shapes.search_opportunities).not.toContain("results");
  });

  it("list_audit_records: the audit page reads `audit_records`, not `records`", () => {
    expect(shapes.list_audit_records).toContain("audit_records");
    expect(shapes.list_audit_records).not.toContain("records");
  });

  it("submit_proposal: the proposal is nested under `proposal`, carrying `proposal_id`", () => {
    expect(shapes.submit_proposal).toEqual(["proposal"]);
    expect(shapes["submit_proposal.proposal"]).toContain("proposal_id");
  });

  it("create_account: the created record is nested under `account`, not a top-level id", () => {
    expect(shapes.create_account).toEqual(["account"]);
  });

  it("create_contact: the created record is nested under `contact`, not a top-level id", () => {
    expect(shapes.create_contact).toEqual(["contact"]);
  });

  it("create_opportunity: the created record is nested under `opportunity`, not a top-level id", () => {
    expect(shapes.create_opportunity).toEqual(["opportunity"]);
  });
});
