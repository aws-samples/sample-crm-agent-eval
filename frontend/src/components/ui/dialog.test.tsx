// SPDX-License-Identifier: MIT
/** Tests for the `Dialog` primitive's accessibility contract. */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "./dialog";

// `globals: false` in vite.config.ts means RTL registers no automatic cleanup.
afterEach(() => {
  cleanup();
});

function renderDialog(contentProps: Record<string, unknown> = {}) {
  render(
    <Dialog open>
      <DialogContent {...contentProps}>
        <DialogHeader>
          <DialogTitle>Confirm the thing</DialogTitle>
          <DialogDescription>This explains the thing.</DialogDescription>
        </DialogHeader>
      </DialogContent>
    </Dialog>,
  );
}

describe("Dialog primitive", () => {
  /** The primitive sets `aria-modal`, which Radix does not. */
  it("declares itself modal without the call site having to", () => {
    renderDialog();
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-modal", "true");
  });

  it("is named and described from DialogTitle and DialogDescription", () => {
    renderDialog();
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAccessibleName("Confirm the thing");
    expect(dialog).toHaveAccessibleDescription("This explains the thing.");
  });

  it("lets a call site override aria-modal deliberately, for a non-modal dialog", () => {
    // A consumer prop overrides the default.
    renderDialog({ "aria-modal": "false" });
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-modal", "false");
  });
});
