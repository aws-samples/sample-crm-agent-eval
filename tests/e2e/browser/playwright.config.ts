// SPDX-License-Identifier: MIT
/**
 * Playwright configuration for the browser journeys. The base URL is discovered from stack outputs,
 * first hit wins:
 *
 *   1. `CRM_E2E_BASE_URL` — an explicit override.
 *   2. `CRM_TF_OUTPUTS_JSON` — path to a saved `terraform -chdir=infra output -json` document.
 *   3. `results/tf-outputs.json`.
 *   4. `terraform -chdir=infra output -json` — invoked directly, if terraform and state are present.
 *
 * A saved outputs file is checked for freshness by `scripts/outputs_provenance.py --check`.
 * `globalSetup` does not run for `--list`, so `playwright test --list` works offline.
 */
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig, devices } from "@playwright/test";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "../../..");
const INFRA_DIR = resolve(REPO_ROOT, "infra");

/** Terraform output whose value is the CloudFront domain the SPA is served from. */
const BASE_URL_OUTPUT = "cloudfront_domain_name";

/**
 * Outputs whose presence a journey run requires. A precondition only: no journey reads them; the
 * SPA takes its Cognito configuration from the deployed `/config.json`.
 */
const REQUIRED_OUTPUTS = [
  BASE_URL_OUTPUT,
  "cognito_user_pool_id",
  "cognito_spa_client_id",
  "cognito_domain",
] as const;

type TerraformOutputs = Record<string, { value?: unknown } | unknown>;

/** Unwrap `terraform output -json`'s `{key: {value: ...}}` shape, tolerating an already-flat map. */
function readOutputValue(outputs: TerraformOutputs, key: string): string | undefined {
  const entry = outputs[key];
  const value =
    entry !== null && typeof entry === "object" && "value" in entry
      ? (entry as { value?: unknown }).value
      : entry;
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function loadOutputsFromFile(path: string): TerraformOutputs | undefined {
  if (!existsSync(path)) return undefined;
  try {
    return JSON.parse(readFileSync(path, "utf-8")) as TerraformOutputs;
  } catch {
    // A malformed outputs file counts as a discovery miss, which globalSetup reports.
    return undefined;
  }
}

function loadOutputsFromTerraform(): TerraformOutputs | undefined {
  if (!existsSync(resolve(REPO_ROOT, "infra", ".terraform"))) return undefined;
  try {
    const raw = execFileSync(
      "terraform",
      [`-chdir=${resolve(REPO_ROOT, "infra")}`, "output", "-json"],
      { encoding: "utf-8", stdio: ["ignore", "pipe", "ignore"] },
    );
    return JSON.parse(raw) as TerraformOutputs;
  } catch {
    return undefined;
  }
}

/**
 * Resolved outputs plus the file they came from; `path` is absent for a live `terraform output`.
 */
type OutputsSource = { outputs: TerraformOutputs; path?: string };

/** Resolve the stack outputs a journey needs, recording which source won. */
function discoverOutputsWithSource(): OutputsSource | undefined {
  const explicit = process.env.CRM_TF_OUTPUTS_JSON;
  if (explicit) {
    const path = resolve(explicit);
    const outputs = loadOutputsFromFile(path);
    if (outputs) return { outputs, path };
  }
  const defaultPath = resolve(REPO_ROOT, "results", "tf-outputs.json");
  const fromDefault = loadOutputsFromFile(defaultPath);
  if (fromDefault) return { outputs: fromDefault, path: defaultPath };
  const fromTerraform = loadOutputsFromTerraform();
  return fromTerraform ? { outputs: fromTerraform } : undefined;
}

/** Resolve the stack outputs a journey needs, or `undefined` when no source is available. */
function discoverOutputs(): TerraformOutputs | undefined {
  return discoverOutputsWithSource()?.outputs;
}

/** Repo-relative POSIX form, so a refusal message names a path a reader can act on. */
function repoRelative(path: string): string {
  return relative(REPO_ROOT, path).split("\\").join("/");
}

/** Every `infra/**` Terraform source, skipping the `.terraform` cache the way `plan_freshness` does. */
function infraSourceFiles(): string[] {
  if (!existsSync(INFRA_DIR)) return [];
  const found: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === ".terraform") continue;
      const full = resolve(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith(".tf")) found.push(full);
    }
  };
  walk(INFRA_DIR);
  return found;
}

/** The most recently modified Terraform source under `infra/`, or `undefined` when there are none. */
function newestInfraSource(): { mtime: number; path: string } | undefined {
  let newest: { mtime: number; path: string } | undefined;
  for (const path of infraSourceFiles()) {
    const { mtimeMs } = statSync(path);
    if (!newest || mtimeMs > newest.mtime) newest = { mtime: mtimeMs, path };
  }
  return newest;
}

/** Second-resolution UTC stamp, matching the form `plan_freshness` prints. */
function utcStamp(epochMs: number): string {
  return `${new Date(epochMs).toISOString().slice(0, 19)}+00:00`;
}

/** The freshness verdict shape `scripts/outputs_provenance.py --check --json` prints. */
interface FreshnessVerdict {
  fresh: boolean;
  form: "commit" | "mtime";
  basis: string;
  refusal: string | null;
  used_weaker_form: boolean;
}

/**
 * Ask `scripts/outputs_provenance.py` whether `path` is current. `undefined` when it cannot be asked.
 */
function askProvenance(path: string): FreshnessVerdict | undefined {
  const interpreter = resolve(REPO_ROOT, ".venv/bin/python3");
  if (!existsSync(interpreter)) return undefined;
  try {
    const stdout = execFileSync(
      interpreter,
      [resolve(REPO_ROOT, "scripts/outputs_provenance.py"), "--check", path, "--json"],
      { cwd: REPO_ROOT, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] },
    );
    return JSON.parse(stdout) as FreshnessVerdict;
  } catch (error) {
    // `--check` exits 3 on a stale snapshot; the verdict is on stdout either way.
    const stdout = (error as { stdout?: string }).stdout;
    if (!stdout) return undefined;
    try {
      return JSON.parse(stdout) as FreshnessVerdict;
    } catch {
      return undefined;
    }
  }
}

/**
 * A refusal message when the outputs snapshot no longer matches `infra/`, else `undefined`.
 *
 * Uses `scripts/outputs_provenance.py --check`, falling back to a local mtime comparison when that
 * cannot run (no `.venv`). Returns `undefined` for an explicit `CRM_E2E_BASE_URL` or a live
 * `terraform output`.
 */
export function stalenessRefusal(): string | undefined {
  if (process.env.CRM_E2E_BASE_URL) return undefined;
  const source = discoverOutputsWithSource();
  if (!source?.path) return undefined;

  const remedy =
    "Resolve by re-recording the outputs after a SUCCESSFUL apply, which stamps the commit they " +
    "were captured at:\n" +
    "  scripts/deploy.sh --profile <p> --region <r>\n" +
    "or stamp an existing document with\n" +
    "  .venv/bin/python3 scripts/outputs_provenance.py --stamp results/tf-outputs.json\n" +
    "or name a known-good document with CRM_TF_OUTPUTS_JSON=/path/to/outputs.json,\n" +
    "or name the target directly with CRM_E2E_BASE_URL=<dev distribution>.";

  const verdict = askProvenance(source.path);
  if (verdict) {
    if (verdict.fresh) return undefined;
    return (
      "T4 browser journeys cannot run: the stack outputs are STALE — not absent, which is why " +
      `nothing else complained.\n${verdict.refusal}\n` +
      "Running now would drive whatever the PREVIOUS apply deployed while asserting against the " +
      "current Terraform, and a mismatch would read as 'the app is broken' rather than as outputs " +
      `describing an older stack.\nVerdict from scripts/outputs_provenance.py (${verdict.basis}).\n` +
      remedy
    );
  }

  // Fallback mtime comparison; it cannot detect staleness on a fresh clone.
  const outputsMtime = statSync(source.path).mtimeMs;
  const newest = newestInfraSource();
  if (!newest || newest.mtime <= outputsMtime) return undefined;

  const newer = infraSourceFiles()
    .filter((path) => statSync(path).mtimeMs > outputsMtime)
    .map(repoRelative)
    .sort();
  return (
    "T4 browser journeys cannot run: the stack outputs are STALE — not absent, which is why nothing " +
    `else complained. ${repoRelative(source.path)} was written ${utcStamp(outputsMtime)}, but ` +
    `${newer.length} infra/**/*.tf file(s) are newer (newest: ` +
    `${repoRelative(newest.path)} at ${utcStamp(newest.mtime)}).\n` +
    "Running now would drive whatever the PREVIOUS apply deployed while asserting against the " +
    "current Terraform, and a mismatch would read as 'the app is broken' rather than as outputs " +
    "describing an older stack.\n" +
    "NOTE: this verdict came from the MTIME comparison, because scripts/outputs_provenance.py could " +
    "not be run (no .venv). That comparison cannot detect staleness on a fresh clone, so a PASS from " +
    "it is weaker than a commit-form pass.\n" +
    `Newer than the outputs: ${newer.join(", ")}\n` +
    remedy
  );
}

/**
 * Resolve the base URL, or `undefined` if it cannot be discovered. There is no default.
 */
export function resolveBaseUrl(): string | undefined {
  const override = process.env.CRM_E2E_BASE_URL;
  if (override) return override.replace(/\/+$/, "");

  const outputs = discoverOutputs();
  if (!outputs) return undefined;
  const domain = readOutputValue(outputs, BASE_URL_OUTPUT);
  if (!domain) return undefined;
  return domain.startsWith("http") ? domain.replace(/\/+$/, "") : `https://${domain}`;
}

/** Names of the outputs that are missing, for a `globalSetup` failure message that says what to fix. */
export function missingOutputs(): string[] {
  if (process.env.CRM_E2E_BASE_URL) return [];
  const outputs = discoverOutputs();
  if (!outputs) return [...REQUIRED_OUTPUTS];
  return REQUIRED_OUTPUTS.filter((key) => readOutputValue(outputs, key) === undefined);
}

export default defineConfig({
  testDir: "./journeys",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  // Serial: the journeys mutate shared `demo`-tenant CRM state.
  workers: 1,
  fullyParallel: false,
  // No retries: a retry would re-run a mutation whose first attempt may have partly applied.
  retries: 0,
  forbidOnly: true,
  reporter: [["list"], ["json", { outputFile: "../../../results/e2e-browser.json" }]],
  globalSetup: "./fixtures/globalSetup.ts",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: resolveBaseUrl(),
    trace: "retain-on-failure",
    video: "retain-on-failure",
    screenshot: "only-on-failure",
    // The journeys assert incremental SSE delivery; a proxy that buffers would defeat that.
    extraHTTPHeaders: { "Accept-Encoding": "identity" },
  },
});
