// SPDX-License-Identifier: MIT
/**
 * Journey 21 — a stub sweep run from the eval dashboard renders the tier comparison summary as three
 * tables (Supervisor, Subagent, ToolCall), each headed `Metric` plus one column per arm, with fixed
 * row labels and the notes beneath; the `matrix.md` download carries the same cells in its
 * `## Tier comparison summary` section. Nothing in the summary names a verdict or a winner. A run
 * recorded by the current stack reports a count in every `Long-context invocations` cell, never `—`.
 *
 * Three sources are compared cell by cell: the rendered tables, the `tier_comparison_table` field of
 * the completed run's `eval_trigger` status response, and the downloaded `matrix.md`. The parsers
 * these comparisons rely on are themselves tested against planted inputs.
 *
 * The file also carries the controls for the shared CSP violation trap (`fixtures/auth.ts`): a planted
 * violation on a locally routed page is recorded, fails the test it occurs in, and a compliant page
 * records none. Those controls make no network request.
 */
import { readFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { Locator, Page } from "@playwright/test";

import { cspViolationsOf, expect, installCspViolationTrap, test } from "../../fixtures/auth";

/** The tracked sample `matrix.md`, rendered by the same producer the download comes from. */
const SAMPLE_MATRIX = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../../results/matrix.md");

/** The dashboard's route. */
const EVAL_ROUTE = "/evals";

/** `POST {api}/eval_trigger`, matched on the path suffix. */
const EVAL_TRIGGER_PATH = /\/eval_trigger$/;

/** Per-test timeout; a stub sweep over every golden task does not fit the 120 s default. */
const RUN_TIMEOUT_MS = 240_000;

/** The summary's tiers in render order, with each one's section heading. */
const TIERS: readonly { readonly tier: string; readonly heading: string }[] = [
  { tier: "supervisor", heading: "Supervisor" },
  { tier: "subagent", heading: "Subagent" },
  { tier: "tool", heading: "ToolCall" },
];

/** Row labels every tier carries, in order. */
const COMMON_ROW_LABELS: readonly string[] = [
  "Model",
  "Effort",
  "Task completion rate",
  "Task completion over repeats",
  "Task accuracy",
  "Cost per completed task",
  "P95 latency per completed task (ms)",
  "P50 latency per completed task (ms)",
  "Model invocations",
  "Long-context invocations",
  "Input tokens",
  "Output tokens",
  "Cost per task, all tasks",
  "Self-corrected retries",
];

/** The row counting calls priced at long-context rates. */
const LONG_CONTEXT_ROW = "Long-context invocations";

/** Row labels that follow the common ones, per tier. */
const TIER_ROW_LABELS: Readonly<Record<string, readonly string[]>> = {
  supervisor: ["Budget-exhausted tasks"],
  subagent: ["data", "no_match", "denied", "tool_failure", "none"].map(
    (kind) => `Delegation outcome: ${kind}`,
  ),
  tool: ["Truncated tasks"],
};

/** Words that state a judgement; none may appear anywhere in the summary. */
const VERDICT_WORDS = [
  "verdict",
  "winner",
  "wins",
  "loser",
  "recommend",
  "recommended",
  "outperform",
  "outperforms",
  "beats",
  "superior",
  "inferior",
  "runner-up",
];
const VERDICT_PATTERN = new RegExp(`\\b(?:${VERDICT_WORDS.join("|")})\\b`, "i");

/** The first judgement word in `text`, or `null`. */
function firstVerdictWord(text: string): string | null {
  const found = text.match(VERDICT_PATTERN);
  return found === null ? null : found[0];
}

/** One tier's table: its header row and its body rows, each a list of cell strings. */
interface TierTable {
  readonly headers: readonly string[];
  readonly rows: readonly (readonly string[])[];
}

/** The minimum of the run status response this journey reads. Mirrored, never imported. */
interface WireTierTable {
  readonly headers?: readonly string[];
  readonly sections?: readonly {
    readonly tier?: string;
    readonly heading?: string;
    readonly rows?: readonly (readonly string[])[];
  }[];
  readonly notes?: readonly string[];
}
interface WireStatus {
  readonly status?: unknown;
  readonly matrix?: { readonly arms?: readonly unknown[] };
  readonly tier_comparison_table?: WireTierTable;
}

/** The expected row labels of `tier`, in order. */
function expectedRowLabels(tier: string): string[] {
  return [...COMMON_ROW_LABELS, ...(TIER_ROW_LABELS[tier] ?? [])];
}

/** Split one GitHub-flavoured Markdown table line into its trimmed cells. */
function markdownCells(line: string): string[] {
  const trimmed = line.trim();
  return trimmed
    .slice(1, trimmed.endsWith("|") ? -1 : undefined)
    .split("|")
    .map((cell) => cell.trim());
}

/**
 * Parse the `## Tier comparison summary` section of a `matrix.md` into one table per `### ` heading,
 * plus the `- ` notes after its `Notes:` line. Throws when the section is absent.
 */
function parseTierSection(markdown: string): {
  tables: Map<string, TierTable>;
  notes: string[];
} {
  const lines = markdown.split("\n");
  const start = lines.indexOf("## Tier comparison summary");
  if (start < 0) throw new Error("matrix.md has no '## Tier comparison summary' section");
  const tables = new Map<string, TierTable>();
  const notes: string[] = [];
  let heading: string | undefined;
  let inNotes = false;
  let tableLines: string[] = [];
  const flush = (): void => {
    if (heading !== undefined && tableLines.length >= 2) {
      const [header, , ...body] = tableLines;
      tables.set(heading, { headers: markdownCells(header), rows: body.map(markdownCells) });
    }
    tableLines = [];
  };
  for (const line of lines.slice(start + 1)) {
    if (line.startsWith("## ")) break;
    if (line.startsWith("### ")) {
      flush();
      heading = line.slice(4).trim();
      continue;
    }
    if (line.trim() === "Notes:") {
      flush();
      heading = undefined;
      inNotes = true;
      continue;
    }
    if (inNotes && line.startsWith("- ")) notes.push(line.slice(2));
    else if (heading !== undefined && line.trim().startsWith("|")) tableLines.push(line);
  }
  flush();
  return { tables, notes };
}

/** The `## Tier comparison summary` section of `markdown`, up to the next `## ` heading. */
function tierSectionText(markdown: string): string {
  const start = markdown.indexOf("## Tier comparison summary");
  return start < 0 ? "" : markdown.slice(start).split("\n## ")[0];
}

/** The rendered table inside `tableRoot`: header cells, then each body row's cells. */
async function renderedTable(tableRoot: Locator): Promise<TierTable> {
  return tableRoot.evaluate((root) => {
    const table = root instanceof HTMLTableElement ? root : root.querySelector("table");
    if (table === null) throw new Error("no <table> under the tier comparison test id");
    const text = (cell: Element): string => (cell.textContent ?? "").replace(/\s+/g, " ").trim();
    const headerRow = table.tHead?.rows[0] ?? table.rows[0];
    const bodyRows = [...table.querySelectorAll("tbody tr")];
    return {
      headers: [...headerRow.cells].map(text),
      rows: bodyRows.map((row) => [...(row as HTMLTableRowElement).cells].map(text)),
    };
  });
}

/** Record every `eval_trigger` response body the page receives; `settled()` awaits them all. */
function recordStatusBodies(page: Page): { settled: () => Promise<WireStatus[]> } {
  const pending: Promise<WireStatus | undefined>[] = [];
  page.on("response", (response) => {
    if (!EVAL_TRIGGER_PATH.test(new URL(response.url()).pathname)) return;
    if (response.request().method() !== "POST" || response.status() >= 400) return;
    pending.push(
      response.text().then(
        (text) => {
          try {
            return JSON.parse(text) as WireStatus;
          } catch {
            return undefined;
          }
        },
        () => undefined,
      ),
    );
  });
  return {
    settled: async () =>
      (await Promise.all(pending)).filter((body): body is WireStatus => body !== undefined),
  };
}

/** Trigger a stub sweep from the dashboard by keyboard and wait for the run to complete. */
async function runStubSweep(page: Page): Promise<void> {
  await page.goto(EVAL_ROUTE);
  const trigger = page.getByRole("button", { name: /run stub sweep/i });
  await expect(trigger).toBeVisible();
  await trigger.focus();
  await page.keyboard.press("Enter");

  const failureAlert = page.getByRole("alert");
  const completed = page.getByText(/run complete/i);
  await expect
    .poll(
      async () => {
        if ((await failureAlert.count()) > 0) {
          return `failed: ${(await failureAlert.first().innerText()).trim()}`;
        }
        return (await completed.count()) > 0 ? "complete" : "running";
      },
      { timeout: RUN_TIMEOUT_MS, message: "the stub sweep must reach a completed run" },
    )
    .toBe("complete");
}

test.describe.configure({ timeout: RUN_TIMEOUT_MS + 60_000 });

test.describe("the tier comparison summary and its matrix.md download", () => {
  test("the matrix.md parser reads every tier table and note, and refuses a missing section", () => {
    const sample = [
      "# Model Evaluation Matrix",
      "",
      "## Tier comparison summary",
      "",
      "Intro line.",
      "",
      "### Supervisor",
      "",
      "| Metric | anthropic | openai |",
      "| --- | --- | --- |",
      "| Model | a.model | o.model |",
      "| Task accuracy | 1.000000 | — |",
      "",
      "### ToolCall",
      "",
      "| Metric | anthropic | openai |",
      "| --- | --- | --- |",
      "| Self-corrected retries | not applicable | not applicable |",
      "",
      "Notes:",
      "",
      "- First note.",
      "- Second note with `code`.",
      "",
      "## Reading these figures",
      "",
      "| Metric | ignored |",
      "- not a tier note, and the winner of nothing",
    ].join("\n");

    const { tables, notes } = parseTierSection(sample);

    expect([...tables.keys()]).toEqual(["Supervisor", "ToolCall"]);
    expect(tables.get("Supervisor")).toEqual({
      headers: ["Metric", "anthropic", "openai"],
      rows: [
        ["Model", "a.model", "o.model"],
        ["Task accuracy", "1.000000", "—"],
      ],
    });
    expect(tables.get("ToolCall")?.rows).toEqual([
      ["Self-corrected retries", "not applicable", "not applicable"],
    ]);
    expect(notes).toEqual(["First note.", "Second note with `code`."]);
    expect(firstVerdictWord(tierSectionText(sample))).toBeNull();
    expect(() => parseTierSection("# Model Evaluation Matrix\n")).toThrow(/no '## Tier comparison/);
  });

  test("the tracked matrix.md parses into the three tier tables with the fixed row labels", () => {
    const { tables, notes } = parseTierSection(readFileSync(SAMPLE_MATRIX, "utf-8"));

    expect([...tables.keys()]).toEqual(TIERS.map(({ heading }) => heading));
    for (const { tier, heading } of TIERS) {
      const table = tables.get(heading);
      expect(table?.headers[0], `${heading}'s first column`).toBe("Metric");
      expect(table?.rows.map((row) => row[0]), `${heading}'s row labels`).toEqual(
        expectedRowLabels(tier),
      );
      for (const row of table?.rows ?? []) {
        expect(row.length, `${heading} row ${row[0]}`).toBe(table?.headers.length);
      }
    }
    expect(notes.length).toBeGreaterThan(0);
    expect(firstVerdictWord(tierSectionText(readFileSync(SAMPLE_MATRIX, "utf-8")))).toBeNull();
  });

  test("the verdict detector fires on judgement words and passes the summary's own vocabulary", () => {
    for (const sample of ["anthropic wins", "Winner: openai", "we recommend x", "x outperforms y"]) {
      expect(firstVerdictWord(sample), `${sample} states a judgement`).not.toBeNull();
    }
    const vocabulary = [...COMMON_ROW_LABELS, ...Object.values(TIER_ROW_LABELS).flat(), "withheld"];
    for (const label of vocabulary) {
      expect(firstVerdictWord(label), `${label} is a measurement label`).toBeNull();
    }
  });

  test("a completed stub sweep renders the three tier tables and downloads the same cells", async ({
    signedInPage: page,
  }) => {
    const responses = recordStatusBodies(page);
    await runStubSweep(page);

    const completedBodies = (await responses.settled()).filter(
      (body) => body.status === "completed",
    );
    expect(completedBodies.length, "no completed run status response was observed").toBeGreaterThan(0);
    const wire = completedBodies[completedBodies.length - 1];
    const armCount = wire.matrix?.arms?.length ?? 0;
    expect(armCount, "the completed run reported no arms").toBeGreaterThan(0);
    const wireTable = wire.tier_comparison_table;
    expect(wireTable, "the completed run's status carries no tier_comparison_table").toBeDefined();
    const wireSections = wireTable?.sections ?? [];
    const wireNotes = wireTable?.notes ?? [];
    expect(wireSections.map((section) => section.tier)).toEqual(TIERS.map(({ tier }) => tier));
    expect(wireNotes.length, "the tier comparison carries no notes").toBeGreaterThan(0);

    // ---- the rendered summary ----
    const summary = page.getByTestId("tier-comparison-summary");
    await expect(summary).toBeVisible();
    await expect(page.getByTestId("tier-comparison-unavailable")).toHaveCount(0);

    const rendered = new Map<string, TierTable>();
    for (const [index, { tier, heading }] of TIERS.entries()) {
      const tableRoot = summary.getByTestId(`tier-comparison-table-${tier}`);
      await expect(tableRoot, `the ${heading} table is rendered once`).toHaveCount(1);
      const table = await renderedTable(tableRoot);
      rendered.set(heading, table);

      expect(table.headers[0], `${heading}'s first column is the metric label`).toBe("Metric");
      expect(table.headers.length, `${heading} has one column per arm`).toBe(armCount + 1);
      expect(
        table.rows.map((row) => row[0]),
        `${heading} carries the fixed row labels in order`,
      ).toEqual(expectedRowLabels(tier));
      for (const row of table.rows) {
        expect(row.length, `${heading} row ${row[0]} has one cell per arm`).toBe(armCount + 1);
      }
      expect(
        { headers: table.headers, rows: table.rows },
        `${heading}'s rendered cells are the backend's cells verbatim`,
      ).toEqual({ headers: wireTable?.headers, rows: wireSections[index]?.rows });
      const longContext = table.rows.find((row) => row[0] === LONG_CONTEXT_ROW) ?? [];
      for (const cell of longContext.slice(1)) {
        expect(cell, `${heading}'s ${LONG_CONTEXT_ROW} is a recorded count, not —`).toMatch(/^\d+$/);
      }
    }

    const summaryText = (await summary.innerText()).replace(/\s+/g, " ");
    for (const note of wireNotes) {
      expect(summaryText, "every note is rendered beneath the tables").toContain(
        note.replace(/`/g, "").replace(/\s+/g, " ").trim(),
      );
    }
    expect(firstVerdictWord(summaryText), "the summary names no verdict or winner").toBeNull();

    // ---- the matrix.md download ----
    const downloadButton = page.getByTestId("download-matrix-md");
    await expect(downloadButton).toBeVisible();
    const [download] = await Promise.all([page.waitForEvent("download"), downloadButton.click()]);
    expect(download.suggestedFilename()).toMatch(/^matrix-.+\.md$/);
    const markdown = await readFile(await download.path(), "utf-8");
    const { tables, notes } = parseTierSection(markdown);

    expect([...tables.keys()], "matrix.md carries the three tier tables in order").toEqual(
      TIERS.map(({ heading }) => heading),
    );
    for (const { heading } of TIERS) {
      expect(tables.get(heading), `matrix.md's ${heading} cells equal the page's`).toEqual(
        rendered.get(heading),
      );
    }
    expect(notes, "matrix.md's notes equal the backend's").toEqual([...wireNotes]);
    expect(
      firstVerdictWord(tierSectionText(markdown)),
      "matrix.md's tier section names no verdict or winner",
    ).toBeNull();
  });
});

/** An origin no request ever leaves the browser for: every URL under it is fulfilled by `page.route`. */
const PLANTED_ORIGIN = "http://csp-trap.test";

/** Serve `body` at `PLANTED_ORIGIN/` under a policy that forbids every script. */
async function servePlantedPage(page: Page, body: string): Promise<void> {
  await page.route(`${PLANTED_ORIGIN}/**`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/html",
      headers: { "content-security-policy": "default-src 'none'; script-src 'none'" },
      body,
    }),
  );
  await page.goto(`${PLANTED_ORIGIN}/`);
}

const VIOLATING_PAGE = "<!doctype html><title>planted</title><script>void 0;</script>";
const COMPLIANT_PAGE = "<!doctype html><title>compliant</title><p>no script, no subresource</p>";

test.describe("the shared CSP violation trap", () => {
  test("records a planted inline-script violation, naming the directive", async ({ browser }) => {
    const page = await browser.newPage();
    try {
      await installCspViolationTrap(page);
      await servePlantedPage(page, VIOLATING_PAGE);
      await expect.poll(() => cspViolationsOf(page).length).toBeGreaterThan(0);
      expect(cspViolationsOf(page)[0]).toMatch(/^enforce script-src(-elem)? blocked inline on /);
    } finally {
      await page.close();
    }
  });

  test("fails the test a violation occurs in, even when the body itself passes", async ({ page }) => {
    test.fail(true, "the fixture's teardown must fail this test: its page records a violation");
    await servePlantedPage(page, VIOLATING_PAGE);
    await expect.poll(() => cspViolationsOf(page).length).toBeGreaterThan(0);
  });

  test("records nothing for a page that keeps to its policy", async ({ page }) => {
    await servePlantedPage(page, COMPLIANT_PAGE);
    await expect(page).toHaveTitle("compliant");
    expect(cspViolationsOf(page)).toEqual([]);
  });

  test("hands a journey a copy, so a journey cannot clear the record", async ({ browser }) => {
    const page = await browser.newPage();
    try {
      await installCspViolationTrap(page);
      await servePlantedPage(page, VIOLATING_PAGE);
      await expect.poll(() => cspViolationsOf(page).length).toBeGreaterThan(0);
      const copy = cspViolationsOf(page) as string[];
      copy.length = 0;
      expect(cspViolationsOf(page).length).toBeGreaterThan(0);
    } finally {
      await page.close();
    }
  });
});
