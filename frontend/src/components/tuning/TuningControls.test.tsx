// SPDX-License-Identifier: MIT
/**
 * Component tests for TuningControls: options come from the matrix, the model list is the family's
 * models, an unavailable effort is disabled with an accessible reason, a change clears what the new
 * model may not accept, and a server rejection is shown at the control or tier it names.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseCapabilityMatrix } from "./capabilityMatrix";
import { EMPTY_TIER_SELECTION, type TierSelection } from "./sessionTuning";
import { TuningControls } from "./TuningControls";

function rawMatrix() {
  return {
    default_family: "anthropic",
    models: {
      "anthropic.claude-opus-5-5": {
        model_id: "anthropic.claude-opus-5-5",
        family: "anthropic",
        tier: "supervisor",
        effort: {
          supported: true,
          values: ["low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: null,
        },
      },
      "anthropic.claude-sonnet-5-5": {
        model_id: "anthropic.claude-sonnet-5-5",
        family: "anthropic",
        tier: "subagent",
        effort: {
          supported: true,
          values: ["low", "medium", "high"],
          default: "high",
          capped_at_when_thinking_disabled: null,
        },
      },
      "anthropic.claude-haiku-5-5": {
        model_id: "anthropic.claude-haiku-5-5",
        family: "anthropic",
        tier: "tool",
        effort: {
          supported: true,
          values: ["low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: "high",
        },
      },
      "anthropic.claude-effortless-test": {
        model_id: "anthropic.claude-effortless-test",
        family: "anthropic",
        tier: "tool",
        effort: { supported: false, values: [], default: null, capped_at_when_thinking_disabled: null },
      },
      "openai.gpt-6-astra": {
        model_id: "openai.gpt-6-astra",
        family: "openai",
        tier: "supervisor",
        effort: {
          supported: true,
          values: ["none", "low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: null,
        },
      },
      "openai.gpt-6.1-sol": {
        model_id: "openai.gpt-6.1-sol",
        family: "openai",
        tier: "subagent",
        effort: {
          supported: true,
          values: ["none", "low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: null,
        },
      },
      "openai.gpt-6-luna": {
        model_id: "openai.gpt-6-luna",
        family: "openai",
        tier: "tool",
        effort: {
          supported: true,
          values: ["none", "low", "medium", "high", "xhigh", "max"],
          default: "medium",
          capped_at_when_thinking_disabled: null,
        },
      },
    },
    tiers: {
      anthropic: {
        supervisor: { model_id: "anthropic.claude-opus-5-5", tuning: { effort: "low" } },
        subagent: { model_id: "anthropic.claude-sonnet-5-5", tuning: { effort: "medium" } },
        tool: { model_id: "anthropic.claude-haiku-5-5", tuning: { effort: "low" } },
      },
      openai: {
        supervisor: { model_id: "openai.gpt-6-astra", tuning: { effort: "low" } },
        subagent: { model_id: "openai.gpt-6.1-sol", tuning: { effort: "medium" } },
        tool: { model_id: "openai.gpt-6-luna", tuning: { effort: "low" } },
      },
    },
  };
}

const matrix = () => parseCapabilityMatrix(rawMatrix());

function select(overrides: Partial<TierSelection> = {}): TierSelection {
  return { ...EMPTY_TIER_SELECTION, ...overrides };
}

function renderControls(tier: "supervisor" | "subagent" | "tool", selection: TierSelection, extra = {}) {
  const onChange = vi.fn();
  render(<TuningControls matrix={matrix()} tier={tier} selection={selection} onChange={onChange} {...extra} />);
  return onChange;
}

afterEach(() => {
  cleanup();
});

describe("TuningControls", () => {
  it("names the tier in the fieldset legend", () => {
    renderControls("tool", select());
    expect(screen.getByRole("group", { name: "Tool execution model tuning" })).toBeInTheDocument();
  });

  it("renders effort options exactly as the matrix declares them for the resolved model", () => {
    renderControls("supervisor", select());
    const options = Array.from((screen.getByLabelText("Effort") as HTMLSelectElement).options);
    expect(options.map((o) => o.value)).toEqual(["low", "medium", "high", "xhigh", "max"]);
    // An unset effort shows the tier's configured low, not Opus 5.5's default medium.
    expect(screen.getByLabelText("Effort")).toHaveValue("low");
  });

  it("shows the model's own default effort once the tier runs a model it does not configure", () => {
    // The selection after a model change: effort cleared, Sonnet 5.5 at the tool tier.
    renderControls("tool", select({ family: "anthropic", model: "anthropic.claude-sonnet-5-5" }));
    expect(screen.getByLabelText("Effort")).toHaveValue("high");
  });

  it("shows the other family's configured tier effort after a family change", () => {
    // The selection after a family change: model and effort cleared.
    renderControls("supervisor", select({ family: "openai" }));
    expect(screen.getByLabelText("Model")).toHaveValue("openai.gpt-6-astra");
    expect(screen.getByLabelText("Effort")).toHaveValue("low");
  });

  it("offers every model of the selected family, defaulting to the tier's model", () => {
    renderControls("tool", select({ family: "openai" }));
    const model = screen.getByLabelText("Model") as HTMLSelectElement;
    expect(Array.from(model.options).map((o) => o.value)).toEqual([
      "openai.gpt-6-astra",
      "openai.gpt-6.1-sol",
      "openai.gpt-6-luna",
    ]);
    expect(model).toHaveValue("openai.gpt-6-luna");
  });

  it("renders exactly the family, model and effort controls, and no numeric input", () => {
    renderControls("tool", select());
    const group = screen.getByRole("group", { name: "Tool execution model tuning" });
    const labels = Array.from(group.querySelectorAll("label")).map((label) => label.textContent);
    expect(labels).toEqual(["Provider family", "Model", "Effort"]);
    expect(group.querySelectorAll("input")).toHaveLength(0);
    expect(screen.queryByRole("spinbutton")).toBeNull();
  });

  it("offers Haiku 5.5's effort rungs at the tool tier", () => {
    renderControls("tool", select());
    const effort = screen.getByLabelText("Effort") as HTMLSelectElement;
    expect(effort.disabled).toBe(false);
    expect(Array.from(effort.options).map((o) => o.value)).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
  });

  it("disables effort, with a stated reason, for a model that accepts none", () => {
    renderControls("tool", select({ model: "anthropic.claude-effortless-test" }));
    const effort = screen.getByLabelText("Effort") as HTMLSelectElement;
    expect(effort.disabled).toBe(true);
    const reason = screen.getByText(/does not accept an effort parameter/);
    expect(reason).toHaveAttribute("role", "note");
    expect(effort.getAttribute("aria-describedby")).toBe(reason.id);
  });

  it("changing the family clears model and effort", async () => {
    const onChange = renderControls("tool", select({ effort: "high" }));
    await userEvent.selectOptions(screen.getByLabelText("Provider family"), "openai");
    expect(onChange).toHaveBeenLastCalledWith({ family: "openai", model: null, effort: null });
  });

  it("changing the model keeps the family and clears effort", async () => {
    const onChange = renderControls("tool", select({ family: "anthropic", effort: "xhigh" }));
    await userEvent.selectOptions(screen.getByLabelText("Model"), "anthropic.claude-sonnet-5-5");
    expect(onChange).toHaveBeenLastCalledWith({
      family: "anthropic",
      model: "anthropic.claude-sonnet-5-5",
      effort: null,
    });
  });

  it("changing effort reports the new value and keeps the family and model", async () => {
    const onChange = renderControls("supervisor", select({ family: "openai", effort: "none" }));
    await userEvent.selectOptions(screen.getByLabelText("Effort"), "low");
    expect(onChange).toHaveBeenLastCalledWith({ family: "openai", model: null, effort: "low" });
  });

  it.each([
    ["settings.tuning.family", "Provider family"],
    ["settings.tuning.model", "Model"],
    ["settings.tuning.effort", "Effort"],
  ])("shows the %s error at that control", (control, label) => {
    renderControls("supervisor", select({ family: "openai", effort: "none" }), {
      error: { control, message: "rejected by the server" },
    });
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("rejected by the server");
    const field = screen.getByLabelText(label);
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field.getAttribute("aria-describedby")).toBe(alert.id);
  });

  it("shows a rejection naming a field with no control at the top of the tier, invalidating no control", () => {
    const message = "temperature is no longer a tuning parameter; remove it from the tool tier.";
    renderControls("tool", select(), { error: { control: "settings.tuning.tier", message } });
    const group = screen.getByRole("group", { name: "Tool execution model tuning" });
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(message);
    expect(group).toContainElement(alert);
    expect(alert.compareDocumentPosition(screen.getByLabelText("Provider family"))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    for (const label of ["Provider family", "Model", "Effort"]) {
      expect(screen.getByLabelText(label)).not.toHaveAttribute("aria-invalid");
    }
  });

  it("disables every control while a save is in flight", () => {
    renderControls("tool", select(), { disabled: true });
    expect(screen.getByLabelText("Provider family")).toBeDisabled();
    expect(screen.getByLabelText("Model")).toBeDisabled();
  });
});

/**
 * The pickers over the capability payload the handler serves, as
 * `tests/integration/test_effort_resolver_contract.py` records it in `resolved_efforts.json`.
 */
describe("TuningControls over the served capability payload", () => {
  const served = parseCapabilityMatrix(
    (
      JSON.parse(
        readFileSync(
          resolve(process.cwd(), "..", "tests", "integration", "fixtures", "resolved_efforts.json"),
          "utf8",
        ),
      ) as { capabilities: unknown }
    ).capabilities,
  );

  function renderServed(
    tier: "supervisor" | "subagent" | "tool",
    family: "anthropic" | "openai" = "openai",
  ): HTMLSelectElement {
    render(
      <TuningControls matrix={served} tier={tier} selection={select({ family })} onChange={vi.fn()} />,
    );
    return screen.getByLabelText("Model") as HTMLSelectElement;
  }

  it.each([
    ["supervisor", "openai.gpt-6.1-sol", "medium"],
    ["subagent", "openai.gpt-6.1-terra", "low"],
    ["tool", "openai.gpt-6-luna", "low"],
  ] as const)(
    "with no saved selection selects the served default family at the %s tier: %s at effort %s",
    (tier, model, effort) => {
      expect(served.defaultFamily).toBe("openai");
      render(
        <TuningControls matrix={served} tier={tier} selection={EMPTY_TIER_SELECTION} onChange={vi.fn()} />,
      );
      expect(screen.getByLabelText("Provider family")).toHaveValue("openai");
      expect(screen.getByLabelText("Model")).toHaveValue(model);
      expect(screen.getByLabelText("Effort")).toHaveValue(effort);
    },
  );

  it("lists Astra, Sol, Terra and Luna as the OpenAI models", () => {
    const options = Array.from(renderServed("supervisor").options).map((o) => o.value);
    expect([...options].sort()).toEqual(
      ["openai.gpt-6-astra", "openai.gpt-6.1-sol", "openai.gpt-6.1-terra", "openai.gpt-6-luna"].sort(),
    );
  });

  it("defaults the supervisor tier to Sol", () => {
    expect(renderServed("supervisor")).toHaveValue("openai.gpt-6.1-sol");
  });

  it("defaults the subagent tier to Terra, shown by its model id", () => {
    const model = renderServed("subagent");
    expect(model).toHaveValue("openai.gpt-6.1-terra");
    const terra = Array.from(model.options).find((o) => o.value === "openai.gpt-6.1-terra");
    expect(terra?.textContent).toBe("openai.gpt-6.1-terra");
  });

  it.each([
    ["supervisor", "anthropic.claude-sonnet-5-5", "medium"],
    ["subagent", "anthropic.claude-sonnet-5-5", "low"],
    ["tool", "anthropic.claude-haiku-5-5", "low"],
  ] as const)("defaults the anthropic %s tier to %s at effort %s", (tier, model, effort) => {
    expect(renderServed(tier, "anthropic")).toHaveValue(model);
    expect(screen.getByLabelText("Effort")).toHaveValue(effort);
  });

  it("defaults the openai subagent tier to effort low", () => {
    renderServed("subagent");
    expect(screen.getByLabelText("Effort")).toHaveValue("low");
  });

  it.each([
    ["anthropic", "supervisor", "anthropic.claude-opus-5-5"],
    ["anthropic", "subagent", "anthropic.claude-opus-5-5"],
    ["anthropic", "tool", "anthropic.claude-opus-5-5"],
    ["openai", "supervisor", "openai.gpt-6-astra"],
    ["openai", "subagent", "openai.gpt-6-astra"],
    ["openai", "tool", "openai.gpt-6-astra"],
  ] as const)("offers the %s non-default model at the %s tier: %s", (family, tier, model) => {
    const options = Array.from(renderServed(tier, family).options).map((o) => o.value);
    expect(options).toContain(model);
  });
});
