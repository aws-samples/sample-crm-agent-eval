// SPDX-License-Identifier: MIT
/**
 * Tests for the contacts CRUD surfaces, including the delete-routes-to-proposal path and the
 * departure/reference-visibility behaviour.
 */
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import { ContactCreatePage } from "./ContactCreatePage";
import { ContactDetailPage } from "./ContactDetailPage";
import { ContactEditPage } from "./ContactEditPage";

const mockApi = {
  getContact: vi.fn(),
  searchOpportunities: vi.fn(),
  createContact: vi.fn(),
  updateContact: vi.fn(),
  submitProposal: vi.fn(),
};

vi.mock("../../api/useCrmApi", () => ({
  useCrmApi: () => mockApi,
}));

const CONTACT = {
  contact_id: "contact-1",
  account_id: "acct-1",
  name: "Jane Doe",
  title: "VP Eng",
  status: "active" as const,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const REFERENCING_OPPORTUNITY = {
  opportunity_id: "opp-1",
  account_id: "acct-1",
  name: "Renewal",
  value: 5000,
  stage: "Negotiation" as const,
  owner_rep_id: "rep-1",
  primary_contact_id: "contact-1",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
});

// `globals: false` in vite.config.ts disables RTL's automatic DOM cleanup, so it is registered here.
afterEach(() => {
  cleanup();
});

describe("ContactDetailPage", () => {
  it("renders contact details", async () => {
    mockApi.getContact.mockResolvedValue({ contact: CONTACT });
    mockApi.searchOpportunities.mockResolvedValue({ items: [] });

    render(
      <MemoryRouter initialEntries={["/contacts/contact-1"]}>
        <Routes>
          <Route path="/contacts/:contactId" element={<ContactDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Jane Doe" })).toBeInTheDocument();
  });

  it("surfaces primary-contact reference and still allows delete to be proposed", async () => {
    // `search_opportunities` returns `{items: [...]}`; the reference notice reads `items`.
    mockApi.getContact.mockResolvedValue({ contact: CONTACT });
    mockApi.searchOpportunities.mockResolvedValue({ items: [REFERENCING_OPPORTUNITY] });

    render(
      <MemoryRouter initialEntries={["/contacts/contact-1"]}>
        <Routes>
          <Route path="/contacts/:contactId" element={<ContactDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText(/primary contact on 1 opportunity/)).toBeInTheDocument();
    expect(screen.getByText(/Renewal/)).toBeInTheDocument();
    // Delete is still offered (as a proposal) — the backend, not the UI, refuses it if referenced.
    expect(screen.getByRole("button", { name: /Delete Jane Doe/ })).toBeInTheDocument();
  });

  it("submits a delete proposal rather than deleting directly, and surfaces a backend refusal", async () => {
    mockApi.getContact.mockResolvedValue({ contact: CONTACT });
    mockApi.searchOpportunities.mockResolvedValue({ items: [] });
    // `submit_proposal` returns `{proposal: {...}}`; the rendered id is read from the nested proposal.
    mockApi.submitProposal.mockResolvedValue({ proposal: { proposal_id: "prop-1" } });

    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/contacts/contact-1"]}>
        <Routes>
          <Route path="/contacts/:contactId" element={<ContactDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("button", { name: /Delete Jane Doe/ }));
    await user.click(await screen.findByRole("button", { name: /Confirm delete proposal/ }));

    await waitFor(() => {
      expect(mockApi.submitProposal).toHaveBeenCalledWith(
        expect.objectContaining({
          target_table: "contacts",
          target_id: "contact-1",
          operation: "delete",
        }),
      );
    });
    expect(await screen.findByText(/proposal prop-1/)).toBeInTheDocument();
    expect(await screen.findByText(/Awaiting approval/)).toBeInTheDocument();
  });
});

describe("ContactCreatePage", () => {
  it("requires an accountId, creates a contact under it, and navigates to the new record", async () => {
    // `create_contact` returns `{contact: {...}}`; navigation uses the nested contact id.
    mockApi.createContact.mockResolvedValue({ contact: { contact_id: "contact-new" } });
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/contacts/new?accountId=acct-1"]}>
        <Routes>
          <Route path="/contacts/new" element={<ContactCreatePage />} />
          <Route path="/contacts/:contactId" element={<p>Contact detail: contact-new</p>} />
        </Routes>
      </MemoryRouter>,
    );

    await user.type(screen.getByLabelText("Name"), "New Person");
    await user.click(screen.getByRole("button", { name: /Create contact/ }));

    await waitFor(() => {
      expect(mockApi.createContact).toHaveBeenCalledWith(
        expect.objectContaining({ account_id: "acct-1", name: "New Person" }),
      );
    });
    expect(await screen.findByText("Contact detail: contact-new")).toBeInTheDocument();
  });

  it("shows a clear message rather than a broken form when accountId is missing", () => {
    render(
      <MemoryRouter initialEntries={["/contacts/new"]}>
        <Routes>
          <Route path="/contacts/new" element={<ContactCreatePage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText(/must belong to an account/)).toBeInTheDocument();
  });
});

describe("ContactEditPage", () => {
  it("loads the contact, surfaces the departure notice, and saves a status change", async () => {
    mockApi.getContact.mockResolvedValue({ contact: CONTACT });
    mockApi.searchOpportunities.mockResolvedValue({ items: [REFERENCING_OPPORTUNITY] });
    mockApi.updateContact.mockResolvedValue({});

    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/contacts/contact-1/edit"]}>
        <Routes>
          <Route path="/contacts/:contactId/edit" element={<ContactEditPage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText(/will not clear or reassign that reference/)).toBeInTheDocument();

    const statusSelect = screen.getByLabelText("Status");
    await user.selectOptions(statusSelect, "departed");
    await user.click(screen.getByRole("button", { name: /Save changes/ }));

    await waitFor(() => {
      expect(mockApi.updateContact).toHaveBeenCalledWith(
        expect.objectContaining({
          contact_id: "contact-1",
          fields: expect.objectContaining({ status: "departed" }),
        }),
      );
    });
  });
});
