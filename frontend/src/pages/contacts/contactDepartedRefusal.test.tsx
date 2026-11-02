// SPDX-License-Identifier: MIT
/**
 * The contact edit page renders a 409 `contact_departed` refusal from `update_contact` as an alert
 * carrying the handler's message, and stays on the form. Driven through the real `CrmApiClient`,
 * with `fetch` returning the handler's wire envelope; the message is read from `src/crm/errors.py`.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CrmApiClient } from "../../api/CrmApiClient";
import { ContactEditPage } from "./ContactEditPage";

vi.mock("../../api/useCrmApi", () => {
  const client = new CrmApiClient("/api", { getAccessToken: () => Promise.resolve("token") });
  return { useCrmApi: () => client };
});

function contactDepartedMessage(): string {
  const errorsPy = readFileSync(resolve(process.cwd(), "..", "src", "crm", "errors.py"), "utf8");
  const match = errorsPy.match(/CONTACT_DEPARTED_MESSAGE: Final\[str\] = \(\s*"([^"]+)"\s*\)/);
  if (!match?.[1]) throw new Error("CONTACT_DEPARTED_MESSAGE not found in src/crm/errors.py");
  return match[1];
}

const MESSAGE = contactDepartedMessage();

/** `crm.errors.ContactDeparted.to_api_response`. */
const DEPARTED_REFUSAL = {
  statusCode: 409,
  error: {
    code: "contact_departed",
    message: MESSAGE,
    next_step: "tell_user",
    missing_scope: "status:active",
  },
};

const DEPARTED_CONTACT = {
  contact_id: "contact-1",
  account_id: "acct-1",
  name: "Jane Doe",
  phone: "555-0100",
  status: "departed",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const calledTools: string[] = [];

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  calledTools.length = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      const tool = url.split("/").pop() ?? "";
      calledTools.push(tool);
      switch (tool) {
        case "get_contact":
          return Promise.resolve(jsonResponse(200, { contact: DEPARTED_CONTACT }));
        case "search_opportunities":
          return Promise.resolve(
            jsonResponse(200, { items: [], truncated: false, examined_count: 0, next_cursor: null }),
          );
        case "update_contact":
          return Promise.resolve(jsonResponse(409, DEPARTED_REFUSAL));
        default:
          return Promise.resolve(jsonResponse(500, {}));
      }
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ContactEditPage on a departed contact", () => {
  it("renders the contact_departed message as an alert and keeps the form", async () => {
    render(
      <MemoryRouter initialEntries={["/contacts/contact-1/edit"]}>
        <Routes>
          <Route path="/contacts/:contactId/edit" element={<ContactEditPage />} />
          <Route path="/contacts/:contactId" element={<p>detail page</p>} />
        </Routes>
      </MemoryRouter>,
    );

    const form = await screen.findByRole("form", { name: "Edit contact" });
    const phone = within(form).getByLabelText("Phone");
    await userEvent.clear(phone);
    await userEvent.type(phone, "555-0199");
    await userEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    const alert = await within(form).findByRole("alert");
    expect(alert).toHaveTextContent(MESSAGE);
    expect(calledTools).toContain("update_contact");
    expect(screen.queryByText("detail page")).not.toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Save changes" })).toBeEnabled();
  });
});
