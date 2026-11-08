// SPDX-License-Identifier: MIT
/**
 * Journey 8 — the eval dashboard triggers a stub sweep, renders its results in the order the backend
 * sent them, and publishes no verdict, winner, or ranking — on the page, in the raw eval-run records
 * the backend sent, or in the linked artefacts.
 *
 * Cost depends on `config/pricing.yaml`'s `provisional:` line, the file the deployed stack prices
 * from: while provisional, no absolute currency figure appears anywhere and every arm discloses the
 * withholding; otherwise each arm shows exactly the cost figures its record carries.
 *
 * Every absence assertion follows positive assertions that the run completed and its results rendered,
 * and the two detectors are themselves tested against planted figures.
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { Locator, Page } from "@playwright/test";

import { expect, test } from "../fixtures/auth";

const PRICING_FILE = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../config/pricing.yaml");

/** Whether the deployed price table is provisional; fails when the file declares neither. */
function pricingIsProvisional(): boolean {
  const match = readFileSync(PRICING_FILE, "utf-8").match(/^provisional:\s*(true|false)\b/m);
  expect(match, `${PRICING_FILE} declares no top-level provisional: true|false`).not.toBeNull();
  return match?.[1] === "true";
}

/** The arm's cost metrics as `EvalArmResults` labels them, keyed by the record field it renders. */
const COST_METRICS: readonly { readonly label: string; readonly field: string }[] = [
  { label: "Cost, cache-attributable", field: "cost_cache_total" },
  { label: "Cost, excluding cache", field: "cost_excluding_cache" },
];

/** The figure rendered beside the `<dt>` labelled exactly `label` inside `scope`, without its note. */
function metricValue(scope: Locator, label: string): Locator {
  return scope
    .locator("dt", { hasText: new RegExp(`^${label}$`) })
    .locator("xpath=following-sibling::dd[1]/span[1]");
}

/** The dashboard's route (`frontend/src/App.tsx`). */
const EVAL_ROUTE = "/evals";

/**
 * `POST {api}/eval_trigger`, the one eval endpoint, matched on the path suffix.
 */
const EVAL_TRIGGER_PATH = /\/eval_trigger$/;

/** The `eval_trigger` actions whose response body is one run (`EvalRunStatusResponse`). */
const RUN_ACTIONS: ReadonlySet<string> = new Set(["submit", "status"]);

/**
 * Per-test timeout for this file; a 40-run sweep does not fit the config's 120 s default.
 */
const RUN_TIMEOUT_MS = 240_000;

/**
 * Currency-shaped text: a symbol or ISO code adjacent to a numeral, in either order, or a numeral
 * before a unit word. A bare decimal does not match.
 */
const CURRENCY_SHAPED =
  /(?:[$£€¥₹¢]\s*\d|\d\s*[$£€¥₹¢]|\b(?:USD|EUR|GBP|JPY|CAD|AUD|CHF|INR)\b|\d\s*(?:dollars?|cents?|euros?|pounds?)\b)/i;

/**
 * Wire-record keys shaped like a money figure; a match is a violation only when the value is numeric,
 * so `price_table_version` and `cost_withheld_provisional` pass. A figure delivered as a string is
 * caught only by `CURRENCY_SHAPED` over the serialised record.
 */
const MONEY_FIGURE_KEY = /cost|price|spend|charge|usd/i;

/**
 * Wire-record keys shaped like a judgement; a violation whatever their value.
 */
const JUDGEMENT_KEY = /verdict|winner|loser|rank|recommend|outperform/i;

/** The withholding disclosure a compliant record must carry, as a boolean. */
const WITHHELD_FLAG_KEY = "cost_withheld_provisional";

/** Comparative language that constitutes a verdict when it appears inside a measurement. */
const VERDICT_LANGUAGE = [
  "verdict",
  "winner",
  "wins",
  "loser",
  "recommend",
  "best",
  "worst",
  "better",
  "worse",
  "outperform",
  "beats",
  "superior",
  "inferior",
  "leader",
  "runner-up",
];

/** The minimum of `EvalRunStatusResponse` this journey reads. Mirrored, never imported. */
interface WireArm {
  readonly provenance?: { readonly model_id?: unknown };
  readonly model_metrics?: Record<string, unknown>;
}
interface WireStatus {
  readonly status?: unknown;
  readonly matrix?: { readonly arms?: readonly WireArm[] };
  readonly error?: unknown;
}

/** One observed eval-run response, with its body read as text. */
interface EvalResponseRecord {
  readonly pathname: string;
  readonly action: string;
  readonly status: number;
  readonly text?: string;
  readonly readError?: string;
}

/**
 * Records every run-shaped `eval_trigger` response the page receives. Body reads are collected as promises and
 * awaited by `settled()`; a read that fails is recorded as `readError` rather than dropped, so a
 * short scan cannot pass as a clean one.
 */
function recordEvalResponses(page: Page): {
  settled: () => Promise<EvalResponseRecord[]>;
} {
  const pending: Promise<EvalResponseRecord>[] = [];
  page.on("response", (response) => {
    const { pathname } = new URL(response.url());
    if (!EVAL_TRIGGER_PATH.test(pathname)) return;
    const request = response.request();
    if (request.method() !== "POST") return;
    let action = "";
    try {
      action = String((request.postDataJSON() as { action?: unknown } | null)?.action ?? "");
    } catch {
      action = "<unparseable request>";
    }
    if (!RUN_ACTIONS.has(action) && action !== "<unparseable request>") return;
    const status = response.status();
    pending.push(
      response.text().then(
        (text) => ({ pathname, action, status, text }),
        (error: unknown) => ({
          pathname,
          action,
          status,
          readError: error instanceof Error ? error.message : String(error),
        }),
      ),
    );
  });
  return { settled: () => Promise.all(pending) };
}

/**
 * Every money figure and every judgement anywhere in a JSON value, at any depth, reported as
 * `path: value` so a violation names the field that carries it.
 */
function findForbiddenFields(value: unknown, path = "$", { money = true } = {}): string[] {
  if (Array.isArray(value)) {
    return value.flatMap((item, index) => findForbiddenFields(item, `${path}[${index}]`, { money }));
  }
  if (value === null || typeof value !== "object") return [];

  const found: string[] = [];
  for (const [key, nested] of Object.entries(value)) {
    const where = `${path}.${key}`;
    if (JUDGEMENT_KEY.test(key)) {
      found.push(`${where}: judgement field (${JSON.stringify(nested)})`);
    } else if (money && MONEY_FIGURE_KEY.test(key) && typeof nested === "number") {
      found.push(`${where}: money figure (${nested})`);
    }
    found.push(...findForbiddenFields(nested, where, { money }));
  }
  return found;
}

/**
 * Drives the dashboard's own trigger by keyboard and polls to a terminal state; a failed run fails
 * here with the backend's message.
 */
async function runStubSweep(page: Page): Promise<void> {
  await page.goto(EVAL_ROUTE);

  const trigger = page.getByRole("button", { name: /run stub sweep/i });
  await expect(trigger).toBeVisible();
  // There is no live-sweep affordance.
  await expect(page.getByRole("button", { name: /live/i })).toHaveCount(0);

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
      {
        timeout: RUN_TIMEOUT_MS,
        message:
          "the stub sweep must reach a completed run; every absence assertion in this journey is " +
          "vacuous over a page that rendered no results",
      },
    )
    .toBe("complete");
}

/** The `model_id` of every arm the backend reported, in the order it reported them. */
function wireArmOrder(body: WireStatus): string[] {
  return (body.matrix?.arms ?? []).map((arm) => String(arm.provenance?.model_id ?? ""));
}

/**
 * The last completed status body among the recorded responses. An unread body, an error status, or
 * a non-JSON body fails here naming the response.
 */
function succeededStatusBody(records: EvalResponseRecord[]): WireStatus {
  expect(
    records
      .filter((record) => record.readError !== undefined)
      .map((record) => `${record.pathname}: ${record.readError}`),
    "every eval-run response body must be readable",
  ).toEqual([]);

  expect(
    records.filter((record) => record.status >= 400).map((record) => `${record.pathname}: ${record.status}`),
    "every eval-run response must be a success status",
  ).toEqual([]);

  const parsed: WireStatus[] = [];
  const unparseable: string[] = [];
  for (const record of records) {
    try {
      parsed.push(JSON.parse(record.text ?? "") as WireStatus);
    } catch {
      unparseable.push(`${record.pathname}: ${(record.text ?? "").slice(0, 120)}`);
    }
  }
  expect(unparseable, "every eval-run response body must be JSON").toEqual([]);

  const succeeded = parsed.filter((body) => body.status === "completed");

  expect(
    succeeded.length,
    "no completed eval-run status response was observed, so there are no writer records to scan",
  ).toBeGreaterThan(0);
  return succeeded[succeeded.length - 1];
}

test.describe.configure({ timeout: RUN_TIMEOUT_MS + 60_000 });

/** Rank wording: a rank, ranking or ranked entry, which would order the arms by a result. */
const RANK_WORDING = /\brank(?:ing|ed|s)?\b/;

/**
 * Phrases that contain "rank" but name a statistics method rather than an ordering of arms. Each is
 * removed before the rank check; any other "rank" wording still fails it.
 */
const RANK_WORDING_EXEMPT_PHRASES: readonly string[] = ["nearest-rank percentile"];

/** The first rank wording in `text` after the exempt phrases are removed, or `null`. */
function rankWording(text: string): string | null {
  let scanned = text.toLowerCase();
  for (const phrase of RANK_WORDING_EXEMPT_PHRASES) scanned = scanned.split(phrase).join(" ");
  const found = scanned.match(RANK_WORDING);
  return found === null ? null : found[0];
}

/** The artefact control that downloads `matrix.md` in place instead of opening it in a new tab. */
const MATRIX_DOWNLOAD_TEST_ID = "download-matrix-md";

/** The content type the sweep stores an artefact under, by its key's extension. */
function artefactContentType(path: string): string {
  if (path.endsWith(".csv")) return "text/csv";
  if (path.endsWith(".md")) return "text/markdown";
  return "application/json";
}

test.describe("the eval dashboard publishes measurements and withholds every judgement", () => {
  test("the currency matcher fires on currency and stays quiet on a bare score", () => {
    // Control for the currency matcher.
    for (const sample of ["$12.34", "12.34 USD", "€5", "0.75 dollars", "45¢", "USD 0.47"]) {
      expect(sample, `${sample} must be recognised as a currency figure`).toMatch(CURRENCY_SHAPED);
    }
    for (const sample of ["0.7500", "20", "2026-01-01T00:00:00Z", "abc123", "13 / 7"]) {
      expect(sample, `${sample} is a measurement, not a currency figure`).not.toMatch(
        CURRENCY_SHAPED,
      );
    }
  });

  test("the rank check fires on rank wording and exempts only the named percentile method", () => {
    // Control for `rankWording`.
    for (const sample of ["Rank 1", "ranked first", "the ranking below", "ranks", "nearest-rank and rank"]) {
      expect(rankWording(sample), `${sample} is rank wording`).not.toBeNull();
    }
    for (const sample of ["takes the nearest-rank percentile over those tasks", "frank", "rankle"]) {
      expect(rankWording(sample), `${sample} is not rank wording`).toBeNull();
    }
  });

  test("the record scan fires on a planted figure and passes a compliant record", () => {
    // Control for the record scan; `price_table_version` matches `MONEY_FIGURE_KEY` but is not a figure.
    const compliant = {
      status: "completed",
      matrix: {
        arms: [
          {
            provenance: { model_id: "anthropic.claude-opus-5-5", price_table_version: "v0" },
            model_metrics: { included_invocation_count: 20, [WITHHELD_FLAG_KEY]: true },
            aggregate_score: { mean_correctness: 0.75, bad_write_rate: 0 },
            task_scores: [{ task_id: "gt-01", correctness: 1, bad_write: false }],
          },
        ],
      },
    };
    expect(findForbiddenFields(compliant)).toEqual([]);

    for (const [key, value] of [
      ["cost_total", 0.47],
      ["total_spend_usd", 42],
      ["price_per_1k_tokens", 0.003],
      ["verdict", "anthropic wins"],
      ["winner", true],
      ["rank", 1],
    ] as const) {
      const planted = structuredClone(compliant) as typeof compliant & {
        matrix: { arms: { model_metrics: Record<string, unknown> }[] };
      };
      planted.matrix.arms[0].model_metrics[key] = value;
      expect(
        findForbiddenFields(planted),
        `a planted ${key} must be reported`,
      ).toHaveLength(1);
      // With pricing final, a money figure is permitted and a judgement still is not.
      expect(
        findForbiddenFields(planted, "$", { money: false }),
        `a planted ${key} is reported with pricing final only if it is a judgement`,
      ).toHaveLength(MONEY_FIGURE_KEY.test(key) && !JUDGEMENT_KEY.test(key) ? 0 : 1);
    }
  });

  test("a completed stub sweep renders its results with no currency figure on the page", async ({
    signedInPage: page,
  }) => {
    const provisional = pricingIsProvisional();
    const responses = recordEvalResponses(page);
    await runStubSweep(page);
    const wire = succeededStatusBody(await responses.settled());

    // ---- positive structure first: the absences below are facts about a populated page ----
    await expect(page.getByRole("heading", { name: /^results$/i })).toBeVisible();

    const armModelIds = wireArmOrder(wire);
    expect(
      armModelIds.length,
      "the backend reported no arms, so the rendered results section is empty",
    ).toBeGreaterThan(0);

    for (const [index, modelId] of armModelIds.entries()) {
      const armSection = page.getByRole("region", { name: modelId, exact: true });
      await expect(armSection).toHaveCount(1);
      await expect(armSection).toContainText(/mean correctness/i);
      await expect(armSection).toContainText(/included invocations/i);
      if (provisional) {
        // The withholding disclosure is present, not merely the figure absent.
        await expect(
          armSection,
          `${modelId} must disclose that cost is withheld while the price table is provisional`,
        ).toContainText(/withheld — price table is provisional/i);
        continue;
      }
      // Pricing is final: no withholding wording, and each displayed cost is the record's own figure.
      await expect(armSection).not.toContainText(/withheld/i);
      const metrics = wire.matrix?.arms?.[index]?.model_metrics ?? {};
      for (const { label, field } of COST_METRICS) {
        const recorded = metrics[field];
        const shown = ((await metricValue(armSection, label).innerText()) ?? "").trim();
        if (typeof recorded !== "number") {
          expect(shown, `${modelId}'s record carries no numeric ${field}`).toBe("not reported");
          continue;
        }
        expect(shown, `${modelId}'s ${label} must render as currency`).toMatch(CURRENCY_SHAPED);
        expect(
          Number(shown.replace(/[^0-9.-]/g, "")),
          `${modelId}'s ${label} must equal the record's ${field} (${recorded})`,
        ).toBeCloseTo(recorded, 6);
      }
    }

    const table = page.getByRole("table");
    await expect(table.first()).toBeVisible();
    expect(await page.getByRole("columnheader").count()).toBeGreaterThan(0);

    if (!provisional) return;

    // ---- no currency figure on the page ----
    const mainText = await page.getByRole("main").innerText();
    const currency = CURRENCY_SHAPED.exec(mainText);
    expect(
      currency === null ? null : currency[0],
      "no absolute currency figure may appear on the dashboard while the price table is provisional",
    ).toBeNull();
  });

  test("no verdict, winner, or comparative language appears inside any arm's measurements", async ({
    signedInPage: page,
  }) => {
    const responses = recordEvalResponses(page);
    await runStubSweep(page);
    const wire = succeededStatusBody(await responses.settled());

    const armModelIds = wireArmOrder(wire);
    expect(armModelIds.length, "the backend reported no arms to scan").toBeGreaterThan(0);

    for (const modelId of armModelIds) {
      const armSection = page.getByRole("region", { name: modelId, exact: true });
      const sectionText = (await armSection.innerText()).toLowerCase();

      // Positive control: this really is a populated measurement section.
      expect(sectionText, `${modelId}'s section rendered no measurements`).toContain(
        "mean correctness",
      );

      for (const forbidden of VERDICT_LANGUAGE) {
        expect(
          sectionText,
          `"${forbidden}" appeared inside ${modelId}'s measurements`,
        ).not.toContain(forbidden);
      }
    }

    // Verdict language is scanned per arm because the page carries this integrity disclosure.
    await expect(page.getByRole("main")).toContainText(/does not label either model as better/i);
  });

  test("arms render in the order the backend sent them, with no rank and no sort affordance", async ({
    signedInPage: page,
  }) => {
    const responses = recordEvalResponses(page);
    await runStubSweep(page);
    const wire = succeededStatusBody(await responses.settled());

    const wireOrder = wireArmOrder(wire);
    expect(wireOrder.length, "one arm cannot be ranked against anything").toBeGreaterThan(0);

    // Arms render in wire order. The testid is `mode-label-<model_id>#<effort>`; only the model id
    // is compared, because that is what the wire order is expressed in.
    const renderedOrder = await page
      .locator('[data-testid^="mode-label-"]')
      .evaluateAll((nodes) =>
        nodes.map((node) =>
          (node.getAttribute("data-testid") ?? "").replace("mode-label-", "").split("#")[0],
        ),
      );
    expect(
      renderedOrder,
      "the dashboard must render arms in the order the backend supplied, never resorted by a score",
    ).toEqual(wireOrder);

    // No rank, position, or ordinal anywhere in the results.
    const mainText = await page.getByRole("main").innerText();
    expect(rankWording(mainText), "no rank wording appears in the results").toBeNull();
    expect(mainText).not.toMatch(/\b(?:1st|2nd|3rd)\b/);

    // No sort affordance: no `aria-sort` and no control in a column header.
    const headers = await page.getByRole("columnheader").all();
    expect(headers.length, "the per-task table rendered no column headers").toBeGreaterThan(0);
    for (const header of headers) {
      await expect(header).not.toHaveAttribute("aria-sort", /.*/);
      await expect(header.getByRole("button")).toHaveCount(0);
    }
  });

  test("the writer's own records carry no currency figure, verdict, winner, or rank", async ({
    signedInPage: page,
  }) => {
    // The records the backend produced (the parsed `matrix.json` contents) are scanned directly.
    const provisional = pricingIsProvisional();
    const responses = recordEvalResponses(page);
    await runStubSweep(page);
    const records = await responses.settled();
    const wire = succeededStatusBody(records);

    const arms = wire.matrix?.arms ?? [];
    expect(arms.length, "a succeeded run must report its arms; an empty record scans clean").toBeGreaterThan(
      0,
    );

    // Positive control: the record carries its measurements.
    for (const arm of arms) {
      expect(
        arm.model_metrics?.["included_invocation_count"],
        "each arm's record must carry its invocation count",
      ).not.toBeUndefined();
      expect(
        arm.model_metrics?.[WITHHELD_FLAG_KEY],
        provisional
          ? `each arm's record must disclose withholding via ${WITHHELD_FLAG_KEY}`
          : `with pricing final, each arm's record must carry ${WITHHELD_FLAG_KEY}: false`,
      ).toBe(provisional);
    }

    // Any judgement-shaped field is a violation; while provisional, so is any numeric money field.
    expect(
      findForbiddenFields(wire, "$", { money: provisional }),
      provisional
        ? "the run record must carry no cost figure, verdict, winner, or rank while the price table is provisional"
        : "the run record must carry no verdict, winner, or rank",
    ).toEqual([]);

    if (!provisional) return;

    // And no currency figure in the serialised record, whatever key it arrived under.
    for (const record of records) {
      const currency = CURRENCY_SHAPED.exec(record.text ?? "");
      expect(
        currency === null ? null : currency[0],
        `${record.pathname} carried a currency-shaped figure`,
      ).toBeNull();
    }
  });

  test("every published artefact the completed run offers opens and carries no currency figure", async ({
    signedInPage: page,
  }) => {
    // Each artefact control fetches a presigned URL and opens it in a new tab. The tab's own
    // navigation must answer 200 with the artefact's content type; anything else fails the test.
    const provisional = pricingIsProvisional();
    await runStubSweep(page);

    const artifactSection = page.getByRole("region", { name: /generated artifacts/i });
    await expect(artifactSection).toHaveCount(1);
    await expect(artifactSection.getByRole("alert")).toHaveCount(0);

    // The matrix.md download control saves in place rather than opening a tab; journey 21 covers it.
    const downloadControl = artifactSection.getByTestId(MATRIX_DOWNLOAD_TEST_ID);
    await expect(downloadControl, "the matrix.md download control appears once").toHaveCount(1);
    const controls = artifactSection
      .getByRole("button")
      .and(artifactSection.locator(`:not([data-testid="${MATRIX_DOWNLOAD_TEST_ID}"])`));
    const count = await controls.count();
    expect(
      count,
      "a completed run must publish its result artefacts; a run offering none leaves this scan empty",
    ).toBeGreaterThan(0);

    for (let index = 0; index < count; index += 1) {
      const control = controls.nth(index);
      const label = (await control.innerText()).trim();
      await expect(control).toBeEnabled();
      // Listening on the context before the click: the tab navigates as soon as the URL arrives.
      const navigation = page
        .context()
        .waitForEvent(
          "response",
          (candidate) =>
            candidate.request().isNavigationRequest() &&
            candidate.frame().page() !== page &&
            candidate.url().startsWith("https://"),
        );
      const [popup] = await Promise.all([page.waitForEvent("popup"), control.click()]);
      const response = await navigation;
      expect(response.frame().page(), `${label} must open in the new tab`).toBe(popup);
      expect(response.status(), `${label} must open`).toBe(200);
      expect(response.headers()["content-type"] ?? "", `${label}'s content type`).toBe(
        artefactContentType(new URL(response.url()).pathname),
      );
      // A text/csv navigation is a download, so Chromium keeps no body for it; read the same
      // presigned URL again outside the tab.
      const refetch = await page.context().request.get(response.url());
      expect(refetch.status(), `${label}'s body must be readable`).toBe(200);
      const body = await refetch.text();
      await popup.close();
      await expect(artifactSection.getByRole("alert"), `${label} raised an error alert`).toHaveCount(0);
      if (!provisional) continue;
      const currency = CURRENCY_SHAPED.exec(body);
      expect(
        currency === null ? null : currency[0],
        `${label} carried a currency-shaped figure`,
      ).toBeNull();
    }
  });
});
