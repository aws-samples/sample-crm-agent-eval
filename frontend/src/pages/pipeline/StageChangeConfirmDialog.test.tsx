// SPDX-License-Identifier: MIT
/**
 * Tests for the stage-change confirmation dialog: the modal contract, dismissal behaviour, and the
 * reason field's validation.
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { StageChangeConfirmDialog } from "./StageChangeConfirmDialog";

// `globals: false` in vite.config.ts means RTL registers no automatic cleanup.
afterEach(() => {
  cleanup();
});

function renderDialog(overrides: Partial<Parameters<typeof StageChangeConfirmDialog>[0]> = {}) {
  const onConfirm = vi.fn().mockResolvedValue(undefined);
  const onCancel = vi.fn();
  render(
    <StageChangeConfirmDialog
      opportunityName="Big Deal"
      fromStage="Discovery"
      toStage="Qualification"
      transitionKind="advance"
      onConfirm={onConfirm}
      onCancel={onCancel}
      {...overrides}
    />,
  );
  return { onConfirm, onCancel };
}

describe("StageChangeConfirmDialog", () => {
  it("declares itself modal, so the board behind it is not offered as browsable content", () => {
    // Radix sets no `aria-modal`; the `Dialog` primitive sets it explicitly.
    renderDialog();
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-modal", "true");
  });

  it("is named and described, so the dialog announces what it is and what it will do", () => {
    renderDialog();
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAccessibleName("Propose stage change");
    expect(dialog).toHaveAccessibleDescription(/Big Deal.*Discovery.*Qualification/s);
  });

  it("moves focus into the reason field rather than leaving it on the board behind", () => {
    renderDialog();
    expect(screen.getByLabelText("Reason")).toHaveFocus();
  });

  /**
   * The rest of the page is pointer-inert while this dialog is open, so a stray click cannot reach
   * the board behind it and cannot discard a typed reason. Focus-outside dismissal is not asserted:
   * jsdom does not reproduce it.
   */
  it("is pointer-inert outside, so a stray click cannot discard a typed reason", async () => {
    const user = userEvent.setup();
    const { onCancel } = renderDialog();
    await user.type(screen.getByLabelText("Reason"), "Signed LOI in hand");

    expect(getComputedStyle(document.body).pointerEvents).toBe("none");
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText("Reason")).toHaveValue("Signed LOI in hand");
  });

  it("cancels on Escape, which is an explicit gesture unlike an incidental click", async () => {
    const user = userEvent.setup();
    const { onCancel } = renderDialog();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(onCancel).toHaveBeenCalledTimes(1));
  });

  it("refuses an empty reason with an announced error and submits nothing", async () => {
    const user = userEvent.setup();
    const { onConfirm } = renderDialog();

    await user.click(screen.getByRole("button", { name: "Submit proposal" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/reason is required/i);
    expect(screen.getByLabelText("Reason")).toHaveAttribute("aria-invalid", "true");
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("submits the trimmed reason", async () => {
    const user = userEvent.setup();
    const { onConfirm } = renderDialog();
    await user.type(screen.getByLabelText("Reason"), "   Advancing after review   ");
    await user.click(screen.getByRole("button", { name: "Submit proposal" }));

    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith("Advancing after review"));
  });

  it("surfaces a submission failure as a recoverable message, leaving the reason intact", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn().mockRejectedValue(new Error("network down"));
    renderDialog({ onConfirm });

    await user.type(screen.getByLabelText("Reason"), "Advancing");
    await user.click(screen.getByRole("button", { name: "Submit proposal" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/could not submit the proposal/i);
    expect(screen.getByLabelText("Reason")).toHaveValue("Advancing");
    expect(screen.getByRole("button", { name: "Submit proposal" })).toBeEnabled();
  });

  it("renders a refused transition as a denial with no way to submit it", () => {
    const { onConfirm } = renderDialog({
      transitionKind: "reopen",
      fromStage: "Closed Won",
    });

    expect(screen.getByRole("alert")).toHaveTextContent(/not permitted/i);
    expect(screen.queryByRole("button", { name: "Submit proposal" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Reason")).not.toBeInTheDocument();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("offers Close on a refusal, and it routes to the same single cancel path", async () => {
    const user = userEvent.setup();
    const { onCancel } = renderDialog({
      transitionKind: "closed_lateral",
      fromStage: "Closed Won",
    });

    await user.click(screen.getByRole("button", { name: "Close" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
