// SPDX-License-Identifier: MIT
/**
 * Journey 14: per-tier tuning saved on the Settings page reaches every tier of the next
 * chat turn, survives a reload, and "Reset to defaults" restores the defaults. No tier offers a
 * temperature control. An effort the tier's model does not accept is rejected with a stated reason,
 * by the page and by the server, and not saved; a stale schema-2 block carrying `temperature` is
 * refused by the server naming temperature. The initial page puts every tier on the served
 * `default_family` and, after switching every tier to the other family, on that family; each page
 * selects each tier's default model and effort as the page's own `/api/capabilities` response names
 * them, and the other family's subagent default is offered.
 *
 * User: the demo user (`CRM_DEMO_USER_EMAIL`), any group. Each test resets the account's tuning to
 * the defaults when it finishes, so it leaves no saved settings behind. With no saved tuning, the next
 * chat turn runs every tier on the served `default_family`'s model and effort.
 */
import type { Locator, Page } from "@playwright/test";

import { expect, test } from "../../fixtures/auth";
import { field, postAsSignedInUser } from "../../fixtures/pageApi";
import { clearSseFrames, installSseRecorder, sseFrameWithArray } from "../../fixtures/sseRecorder";

const TIERS = [
  { tier: "supervisor", legend: "Supervisor model tuning" },
  { tier: "subagent", legend: "Subagent model tuning" },
  { tier: "tool", legend: "Tool execution model tuning" },
] as const;

type TierName = (typeof TIERS)[number]["tier"];

function tierGroup(page: Page, legend: string): Locator {
  return page.getByRole("group", { name: legend });
}

async function openSettings(page: Page): Promise<void> {
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Model tuning", level: 1 })).toBeVisible();
  await expect(tierGroup(page, TIERS[0].legend)).toBeVisible({ timeout: 30_000 });
}

async function resetToDefaults(page: Page): Promise<void> {
  await openSettings(page);
  await page.getByRole("button", { name: "Reset to defaults" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Defaults restored." })).toBeVisible();
}

/** The model each tier's Model control currently selects. */
async function selectedModels(page: Page): Promise<Record<TierName, string>> {
  const models = {} as Record<TierName, string>;
  for (const { tier, legend } of TIERS) {
    models[tier] = await tierGroup(page, legend).getByLabel("Model").inputValue();
  }
  return models;
}

/** The effort each tier's Effort control currently selects. */
async function selectedEfforts(page: Page): Promise<Record<TierName, string>> {
  const efforts = {} as Record<TierName, string>;
  for (const { tier, legend } of TIERS) {
    efforts[tier] = await tierGroup(page, legend).getByLabel("Effort").inputValue();
  }
  return efforts;
}

/** Each tier's served default model and effort for `family`, read from a `/api/capabilities` body. */
function servedDefaults(
  served: unknown,
  family: string,
): { models: Record<TierName, string>; efforts: Record<TierName, string> } {
  const models = {} as Record<TierName, string>;
  const efforts = {} as Record<TierName, string>;
  for (const { tier } of TIERS) {
    const modelId = field(served, "tiers", family, tier, "model_id");
    expect(typeof modelId, `/api/capabilities names no ${family} ${tier} model`).toBe("string");
    models[tier] = modelId as string;
    const effort = field(served, "tiers", family, tier, "tuning", "effort");
    expect(typeof effort, `/api/capabilities names no ${family} ${tier} default effort`).toBe("string");
    efforts[tier] = effort as string;
  }
  return { models, efforts };
}

/** The `resolved_tiers` of the next turn's SSE stream, as the page received it. */
async function resolvedTiersOfNextTurn(
  page: Page,
  prompt: string,
): Promise<Record<string, { model_id: string; family: string; effort: string | null }>> {
  const panel = page.getByTestId("assistant-panel");
  if (await panel.isHidden()) await page.getByTestId("panel-toggle").click();
  await expect(panel).toBeVisible();

  await clearSseFrames(page);
  await page.getByTestId("assistant-prompt").fill(prompt);
  await page.getByTestId("assistant-send").click();
  const withTiers = await sseFrameWithArray(page, "resolved_tiers", 60_000);
  expect(withTiers, "no SSE frame of the turn carried resolved_tiers").toBeDefined();
  const resolved: Record<string, { model_id: string; family: string; effort: string | null }> = {};
  for (const entry of withTiers?.resolved_tiers as {
    tier: string;
    model_id: string;
    family: string;
    effort: string | null;
  }[]) {
    resolved[entry.tier] = { model_id: entry.model_id, family: entry.family, effort: entry.effort };
  }
  return resolved;
}

test("saved per-tier tuning reaches every tier of the next turn, survives reload, and resets", async ({
  signedInPage: page,
}) => {
  try {
    await installSseRecorder(page);
    await resetToDefaults(page);

    // Mixed families: move the subagent tier to the other family than the supervisor's.
    const supervisorFamily = await tierGroup(page, TIERS[0].legend).getByLabel("Provider family").inputValue();
    const subagent = tierGroup(page, TIERS[1].legend);
    const families = await subagent.getByLabel("Provider family").locator("option").allTextContents();
    const other = families.map((f) => f.trim()).find((f) => f !== supervisorFamily);
    expect(other, "the capability matrix offers only one provider family, so no mix is possible").toBeDefined();
    await subagent.getByLabel("Provider family").selectOption(other as string);

    const chosen = await selectedModels(page);
    await page.getByRole("button", { name: "Save settings" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Settings saved." })).toBeVisible();
    await expect(page.getByText("Saved to your account", { exact: true })).toBeVisible();

    // Reload keeps them.
    await openSettings(page);
    expect(await selectedModels(page)).toEqual(chosen);
    expect(await tierGroup(page, TIERS[1].legend).getByLabel("Provider family").inputValue()).toBe(other);

    // The next turn ran every tier on the saved model.
    const resolved = await resolvedTiersOfNextTurn(page, "List my three newest opportunities.");
    for (const { tier } of TIERS) {
      expect(resolved[tier]?.model_id, `${tier} ran a model other than the saved one`).toBe(chosen[tier]);
    }
    expect(resolved.subagent?.family).toBe(other);

    // Reset restores the defaults.
    await resetToDefaults(page);
    await expect(page.getByText("Using defaults", { exact: true })).toBeVisible();
    expect(await tierGroup(page, TIERS[1].legend).getByLabel("Provider family").inputValue()).toBe(
      supervisorFamily,
    );
  } finally {
    await resetToDefaults(page);
  }
});

/** The served `default_family`, read from a `/api/capabilities` body. */
function servedDefaultFamily(served: unknown): string {
  const family = field(served, "default_family");
  expect(typeof family, "/api/capabilities names no default_family").toBe("string");
  return family as string;
}

/** The one served family other than the default, read from a `/api/capabilities` body. */
function servedOtherFamily(served: unknown): string {
  const defaultFamily = servedDefaultFamily(served);
  const tiers = field(served, "tiers");
  const others = Object.keys((tiers ?? {}) as Record<string, unknown>).filter((f) => f !== defaultFamily);
  expect(others, `/api/capabilities serves no family other than ${defaultFamily}`).toHaveLength(1);
  return others[0] as string;
}

/** Opens Settings and returns the `/api/capabilities` body the page itself fetched. */
async function openSettingsReadingCapabilities(page: Page): Promise<unknown> {
  const capabilities = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" && new URL(response.url()).pathname === "/api/capabilities",
    { timeout: 30_000 },
  );
  await openSettings(page);
  const response = await capabilities;
  expect(response.status(), "GET /api/capabilities did not succeed").toBe(200);
  return (await response.json()) as unknown;
}

test("the initial page puts every tier on the served default family and its default model and effort", async ({
  signedInPage: page,
}) => {
  try {
    await resetToDefaults(page);
    const served = await openSettingsReadingCapabilities(page);
    const defaultFamily = servedDefaultFamily(served);
    const { models, efforts } = servedDefaults(served, defaultFamily);

    for (const { legend } of TIERS) {
      expect(await tierGroup(page, legend).getByLabel("Provider family").inputValue()).toBe(defaultFamily);
    }
    expect(await selectedModels(page)).toEqual(models);
    expect(await selectedEfforts(page)).toEqual(efforts);
  } finally {
    await resetToDefaults(page);
  }
});

test("switching every tier to the other family selects its served defaults and offers the subagent's", async ({
  signedInPage: page,
}) => {
  try {
    await resetToDefaults(page);
    const served = await openSettingsReadingCapabilities(page);
    const otherFamily = servedOtherFamily(served);
    const { models: expected, efforts } = servedDefaults(served, otherFamily);

    for (const { legend } of TIERS) {
      await tierGroup(page, legend).getByLabel("Provider family").selectOption(otherFamily);
    }
    expect(await selectedModels(page)).toEqual(expected);
    expect(await selectedEfforts(page)).toEqual(efforts);

    const subagentOptions = await tierGroup(page, TIERS[1].legend)
      .getByLabel("Model")
      .locator("option")
      .evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value));
    expect(subagentOptions).toContain(expected.subagent);
  } finally {
    await resetToDefaults(page);
  }
});

test("with no saved tuning the next turn runs every tier on the served default family", async ({
  signedInPage: page,
}) => {
  try {
    await installSseRecorder(page);
    await resetToDefaults(page);
    const served = await openSettingsReadingCapabilities(page);
    await expect(page.getByText("Using defaults", { exact: true })).toBeVisible();
    const defaultFamily = servedDefaultFamily(served);
    const { models, efforts } = servedDefaults(served, defaultFamily);

    const resolved = await resolvedTiersOfNextTurn(page, "List my three newest opportunities.");
    for (const { tier } of TIERS) {
      expect(resolved[tier]?.family, `${tier} ran outside the served default family`).toBe(defaultFamily);
      expect(resolved[tier]?.model_id, `${tier} ran a model other than its served default`).toBe(models[tier]);
      expect(resolved[tier]?.effort, `${tier} ran an effort other than its served default`).toBe(efforts[tier]);
    }
  } finally {
    await resetToDefaults(page);
  }
});

/** An effort value no model in the capability matrix offers. */
const UNSUPPORTED_EFFORT = "e2e-unsupported-effort";

test("no tier offers a temperature control", async ({ signedInPage: page }) => {
  await openSettings(page);
  for (const { legend } of TIERS) {
    await expect(tierGroup(page, legend).getByLabel("Effort")).toBeVisible();
    await expect(tierGroup(page, legend).getByLabel("Temperature")).toHaveCount(0);
  }
});

test("an unsupported effort is rejected with a stated reason and not saved", async ({
  signedInPage: page,
}) => {
  try {
    await resetToDefaults(page);

    // The first tier whose model accepts an effort.
    let target: Locator | undefined;
    let targetTier: TierName | undefined;
    for (const { tier, legend } of TIERS) {
      if (await tierGroup(page, legend).getByLabel("Effort").isEnabled()) {
        target = tierGroup(page, legend);
        targetTier = tier;
        break;
      }
    }
    expect(
      target,
      "no tier's default model accepts an effort (config/model_capabilities.yaml), so there is no rung to violate",
    ).toBeDefined();
    const group = target as Locator;
    const tier = targetTier as TierName;
    const effort = group.getByLabel("Effort");
    const offered = (await effort.locator("option").allTextContents()).map((value) => value.trim());
    expect(offered).not.toContain(UNSUPPORTED_EFFORT);

    // The control offers only the model's rungs, so plant one it does not.
    await effort.evaluate((select, value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.appendChild(option);
    }, UNSUPPORTED_EFFORT);
    await effort.selectOption(UNSUPPORTED_EFFORT);

    const puts: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().includes("/api/user_settings")) {
        const body = request.postDataJSON() as { action?: unknown } | null;
        if (body?.action === "put") puts.push(request.url());
      }
    });
    await page.getByRole("button", { name: "Save settings" }).click();

    await expect(page.getByRole("alert").filter({ hasText: "A setting was rejected." })).toBeVisible();
    await expect(effort).toHaveAttribute("aria-invalid", "true");
    await expect(group.getByRole("alert")).toContainText(UNSUPPORTED_EFFORT);
    await expect(group.getByRole("alert")).toContainText("accepted values are");
    expect(puts, "the page sent a save it had already rejected").toEqual([]);

    // The server refuses the same value on its own, with a reason naming the field.
    const reply = await postAsSignedInUser(page, "user_settings", {
      action: "put",
      tuning: {
        schema: 3,
        tiers: {
          [tier]: {
            family: await group.getByLabel("Provider family").inputValue(),
            model: await group.getByLabel("Model").inputValue(),
            effort: UNSUPPORTED_EFFORT,
          },
        },
      },
    });
    expect(reply.status).toBe(400);
    expect(field(reply.body, "error", "code")).toBe("validation_failed");
    expect(field(reply.body, "error", "source")).toBe(`tuning.tiers.${tier}.effort`);
    expect(String(field(reply.body, "error", "message"))).toContain(UNSUPPORTED_EFFORT);

    // Not saved: a reload shows the defaults.
    await openSettings(page);
    await expect(page.getByText("Using defaults", { exact: true })).toBeVisible();
    await expect(group.getByLabel("Effort")).not.toHaveValue(UNSUPPORTED_EFFORT);
  } finally {
    await resetToDefaults(page);
  }
});

test("a stale schema-2 block carrying a temperature is refused naming temperature and not saved", async ({
  signedInPage: page,
}) => {
  try {
    await resetToDefaults(page);
    const { tier, legend } = TIERS[0];
    const group = tierGroup(page, legend);

    const reply = await postAsSignedInUser(page, "user_settings", {
      action: "put",
      tuning: {
        schema: 2,
        tiers: {
          [tier]: {
            family: await group.getByLabel("Provider family").inputValue(),
            model: await group.getByLabel("Model").inputValue(),
            temperature: 0.5,
          },
        },
      },
    });
    expect(reply.status).toBe(400);
    expect(field(reply.body, "error", "code")).toBe("validation_failed");
    expect(field(reply.body, "error", "source")).toBe(`tuning.tiers.${tier}.temperature`);
    expect(String(field(reply.body, "error", "message"))).not.toBe("");

    await openSettings(page);
    await expect(page.getByText("Using defaults", { exact: true })).toBeVisible();
  } finally {
    await resetToDefaults(page);
  }
});
