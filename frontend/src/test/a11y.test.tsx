// SPDX-License-Identifier: MIT
/**
 * Accessibility gate run with `axe-core` directly over every rendered page surface
 * under `frontend/src/pages/`, plus the signed-out sign-in surface (`src/auth/SignInForm.tsx`).
 * Fails on any `serious` or `critical` violation.
 *
 * Imports `axe-core` directly and asserts on `results.violations` via `runAxe` below.
 *
 * jsdom has no paint engine, so axe's `color-contrast` rule is disabled; contrast is asserted at
 * the design-token level in `src/index.css.test.ts` and `src/styles/tokens.test.ts`.
 *
 * Each surface is rendered with realistic mocked data, and each `it()` asserts real content
 * rendered (a heading, a row, a label) before running axe.
 */
import { cleanup, render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import axe from "axe-core";

import { AccountDetailPage } from "../pages/accounts/AccountDetailPage";
import { AccountCreatePage } from "../pages/accounts/AccountCreatePage";
import { AccountEditPage } from "../pages/accounts/AccountEditPage";
import { AccountListPage } from "../pages/accounts/AccountListPage";
import { ContactDetailPage } from "../pages/contacts/ContactDetailPage";
import { ContactCreatePage } from "../pages/contacts/ContactCreatePage";
import { ContactEditPage } from "../pages/contacts/ContactEditPage";
import { OpportunityListPage } from "../pages/opportunities/OpportunityListPage";
import { OpportunityDetailPage } from "../pages/opportunities/OpportunityDetailPage";
import { OpportunityCreatePage } from "../pages/opportunities/OpportunityCreatePage";
import { ActivitiesPage } from "../pages/activities/ActivitiesPage";
import { PipelineBoard } from "../pages/pipeline/PipelineBoard";
import { ApprovalsPage } from "../pages/approvals/ApprovalsPage";
import { AuditPage } from "../pages/audit/AuditPage";
import { EvalDashboardPage } from "../pages/evals/EvalDashboardPage";
import { SettingsPage } from "../pages/settings/SettingsPage";
import { __resetSessionTuningForTests } from "../components/tuning/sessionTuning";
import { SignInForm } from "../auth/SignInForm";
import { AuthProvider } from "../auth/AuthProvider";
import type { AuthContextValue } from "../auth/AuthProvider";
import * as AuthProviderModule from "../auth/AuthProvider";
import { __resetRuntimeConfigForTests, loadRuntimeConfig } from "../config/runtimeConfig";
import * as useCrmApiModule from "../api/useCrmApi";
import type { EvalRunStatusResponse } from "../pages/evals/EvalRunClient";
import { BaseFakeEvalRunClient, jobPayload } from "../pages/evals/testFixtures";
import type { EvalArm } from "../pages/evals/types";

/**
 * Runs axe over `container` with `color-contrast` disabled (see module docstring) and asserts no
 * `serious` or `critical` violation was found. On failure, the error names the rule, impact, and
 * offending selector(s) for every disallowed violation.
 */
async function runAxe(container: Element): Promise<void> {
  const results = await axe.run(container, {
    rules: {
      "color-contrast": { enabled: false },
    },
  });
  const disallowed = results.violations.filter(
    (v) => v.impact === "serious" || v.impact === "critical",
  );
  if (disallowed.length > 0) {
    const detail = disallowed
      .map((v) => {
        const selectors = v.nodes.map((n) => n.target.join(" ")).join(", ");
        return `[${v.impact}] ${v.id}: ${v.help} — offending selector(s): ${selectors}`;
      })
      .join("\n");
    throw new Error(`axe found ${disallowed.length} serious/critical violation(s):\n${detail}`);
  }
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const RUNTIME_CONFIG_BODY = {
  cognitoUserPoolId: "us-east-1_test",
  cognitoClientId: "client123",
  cognitoRegion: "us-east-1",
  cognitoHostedUiDomain: "",
  apiBaseUrl: "/api",
  agentBaseUrl: "/agent",
  agentRuntimeArn: "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/test",
};

/**
 * Preloads the module-cached runtime config via a stubbed `/config.json` fetch, mirroring
 * `main.tsx`'s real bootstrap. Needed by any page whose component tree calls `useCrmApi()`
 * (which reads `getRuntimeConfig()` synchronously) without already stubbing fetch itself.
 */
async function preloadRuntimeConfig(): Promise<void> {
  __resetRuntimeConfigForTests();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) => {
      if (url === "/config.json") {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(RUNTIME_CONFIG_BODY) });
      }
      throw new Error(`unexpected fetch: ${url}`);
    }),
  );
  await loadRuntimeConfig();
}

const ACCOUNT = {
  account_id: "acct-1",
  name: "Acme Corp",
  industry: "Software",
  region: "NA",
  website: "acme.example",
  employee_count: 500,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const CONTACT = {
  contact_id: "contact-1",
  account_id: "acct-1",
  name: "Jane Doe",
  title: "VP Eng",
  status: "active" as const,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const OPPORTUNITY = {
  opportunity_id: "opp-1",
  account_id: "acct-1",
  name: "Acme deal",
  value: 1000,
  stage: "Discovery" as const,
  owner_rep_id: "rep-1",
  close_date: "2026-06-01",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("Sign-in surface", () => {
  it("SignInForm (the first screen every user sees, and the whole signed-out state) has no serious/critical violations", async () => {
    vi.spyOn(AuthProviderModule, "useAuth").mockReturnValue({
      status: "signed-out",
      identity: undefined,
      signIn: vi.fn(),
      signOut: vi.fn(),
      getAccessToken: () => Promise.reject(new Error("not signed in")),
    });

    const { container, findByRole } = render(<SignInForm />);
    expect(await findByRole("heading", { name: "Sign in to FirstCRM" })).toBeInTheDocument();
    await runAxe(container);
  });
});

describe("Accounts pages", () => {
  it("AccountListPage renders its results table", async () => {
    vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue({
      searchAccounts: vi.fn().mockResolvedValue({
        items: [{ account_id: "acct-1", name: "Acme Corp", tenant: "demo", owner_rep_id: "rep-1" }],
        truncated: false,
        examined_count: 1,
        next_cursor: null,
      }),
    } as never);

    const { container, findByRole } = render(
      <MemoryRouter>
        <AccountListPage />
      </MemoryRouter>,
    );
    expect(await findByRole("heading", { name: "Accounts" })).toBeInTheDocument();
    expect(await findByRole("link", { name: "Acme Corp" })).toBeInTheDocument();
    await runAxe(container);
  });

  it("AccountCreatePage renders its form", async () => {
    await preloadRuntimeConfig();
    const { container, findByRole } = render(
      <MemoryRouter>
        <AuthProvider>
          <AccountCreatePage />
        </AuthProvider>
      </MemoryRouter>,
    );
    expect(await findByRole("heading", { name: "New account" })).toBeInTheDocument();
    await runAxe(container);
  });

  it("AccountDetailPage renders account and contact content", async () => {
    vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue({
      getAccount: vi.fn().mockResolvedValue({ account: ACCOUNT, contacts: [CONTACT] }),
      searchOpportunities: vi.fn().mockResolvedValue({ items: [] }),
      updateAccount: vi.fn(),
      submitProposal: vi.fn(),
    } as never);

    const { container, findByRole, findByText } = render(
      <MemoryRouter initialEntries={["/accounts/acct-1"]}>
        <Routes>
          <Route path="/accounts/:accountId" element={<AccountDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await findByRole("heading", { name: "Acme Corp" })).toBeInTheDocument();
    expect(await findByText("Jane Doe")).toBeInTheDocument();
    await runAxe(container);
  });

  it("AccountEditPage renders the pre-filled edit form", async () => {
    vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue({
      getAccount: vi.fn().mockResolvedValue({ account: ACCOUNT, contacts: [] }),
      updateAccount: vi.fn(),
    } as never);

    const { container, findByLabelText } = render(
      <MemoryRouter initialEntries={["/accounts/acct-1/edit"]}>
        <Routes>
          <Route path="/accounts/:accountId/edit" element={<AccountEditPage />} />
        </Routes>
      </MemoryRouter>,
    );
    const nameInput = await findByLabelText("Name");
    expect(nameInput).toHaveValue("Acme Corp");
    await runAxe(container);
  });
});

describe("Contacts pages", () => {
  it("ContactDetailPage renders contact content", async () => {
    vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue({
      getContact: vi.fn().mockResolvedValue({ contact: CONTACT }),
      searchOpportunities: vi.fn().mockResolvedValue({ items: [] }),
      submitProposal: vi.fn(),
    } as never);

    const { container, findByRole } = render(
      <MemoryRouter initialEntries={["/contacts/contact-1"]}>
        <Routes>
          <Route path="/contacts/:contactId" element={<ContactDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await findByRole("heading", { name: "Jane Doe" })).toBeInTheDocument();
    await runAxe(container);
  });

  it("ContactCreatePage renders its form when accountId is present", async () => {
    await preloadRuntimeConfig();
    const { container, findByLabelText } = render(
      <MemoryRouter initialEntries={["/contacts/new?accountId=acct-1"]}>
        <AuthProvider>
          <Routes>
            <Route path="/contacts/new" element={<ContactCreatePage />} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    );
    expect(await findByLabelText("Name")).toBeInTheDocument();
    await runAxe(container);
  });

  it("ContactEditPage renders the pre-filled edit form", async () => {
    vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue({
      getContact: vi.fn().mockResolvedValue({ contact: CONTACT }),
      searchOpportunities: vi.fn().mockResolvedValue({ items: [] }),
      updateContact: vi.fn(),
    } as never);

    const { container, findByLabelText } = render(
      <MemoryRouter initialEntries={["/contacts/contact-1/edit"]}>
        <Routes>
          <Route path="/contacts/:contactId/edit" element={<ContactEditPage />} />
        </Routes>
      </MemoryRouter>,
    );
    const statusSelect = await findByLabelText("Status");
    expect(statusSelect).toBeInTheDocument();
    await runAxe(container);
  });
});

describe("Opportunities pages", () => {
  it("OpportunityListPage renders search results", async () => {
    vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue({
      searchOpportunities: vi.fn().mockResolvedValue({
        items: [OPPORTUNITY],
        truncated: false,
        examined_count: 1,
        next_cursor: null,
      }),
    } as never);

    const { container, findByText } = render(
      <MemoryRouter>
        <OpportunityListPage />
      </MemoryRouter>,
    );
    expect(await findByText("Acme deal")).toBeInTheDocument();
    await runAxe(container);
  });

  it("OpportunityDetailPage renders opportunity, notes, activities, and contacts", async () => {
    vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue({
      getOpportunity: vi.fn().mockResolvedValue({
        opportunity: OPPORTUNITY,
        notes: [
          { note_id: "n1", opportunity_id: "opp-1", body: "Great call", created_at: "2026-01-01T00:00:00Z" },
        ],
        activities: [
          {
            activity_id: "a1",
            opportunity_id: "opp-1",
            kind: "call",
            subject: "Intro call",
            occurred_at: "2026-01-02T00:00:00Z",
            created_at: "2026-01-02T00:00:00Z",
            updated_at: "2026-01-02T00:00:00Z",
          },
        ],
        contacts: [CONTACT],
      }),
      updateOpportunity: vi.fn(),
      submitProposal: vi.fn(),
      logActivity: vi.fn(),
    } as never);

    const { container, findByRole } = render(
      <MemoryRouter initialEntries={["/opportunities/opp-1"]}>
        <Routes>
          <Route path="/opportunities/:opportunityId" element={<OpportunityDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await findByRole("heading", { name: "Acme deal" })).toBeInTheDocument();
    await runAxe(container);
  });

  it("OpportunityCreatePage renders its form", async () => {
    vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue({
      createOpportunity: vi.fn(),
    } as never);

    const { container, findByLabelText } = render(
      <MemoryRouter>
        <OpportunityCreatePage />
      </MemoryRouter>,
    );
    expect(await findByLabelText(/^name$/i)).toBeInTheDocument();
    await runAxe(container);
  });
});

describe("Activities page", () => {
  it("ActivitiesPage renders a real activity via the timeline", async () => {
    vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue({
      listActivities: vi.fn().mockResolvedValue({
        activities: [
          {
            activity_id: "act-1",
            opportunity_id: "opp-1",
            kind: "call",
            subject: "Checked in",
            occurred_at: "2026-05-01T00:00:00Z",
            created_at: "2026-05-01T00:00:00Z",
            updated_at: "2026-05-01T00:00:00Z",
          },
        ],
        next_cursor: null,
      }),
      logActivity: vi.fn(),
      updateActivity: vi.fn(),
    } as never);

    const { container, findByText } = render(
      <MemoryRouter initialEntries={["/opportunities/opp-1/activities"]}>
        <Routes>
          <Route path="/opportunities/:opportunityId/activities" element={<ActivitiesPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await findByText(/Checked in/)).toBeInTheDocument();
    await runAxe(container);
  });
});

describe("Pipeline page", () => {
  it("PipelineBoard renders a real opportunity card", async () => {
    vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue({
      searchOpportunities: vi.fn().mockResolvedValue({
        items: [OPPORTUNITY],
        truncated: false,
        examined_count: 1,
        next_cursor: null,
      }),
      listProposals: vi.fn().mockResolvedValue({ proposals: [], next_cursor: null }),
      submitProposal: vi.fn(),
      updateOpportunity: vi.fn(),
    } as never);

    const { container, findByText } = render(
      // `usePipelineBoard` stamps its row-count report with the current pathname.
      <MemoryRouter initialEntries={["/pipeline"]}>
        <PipelineBoard />
      </MemoryRouter>,
    );
    expect(await findByText(/Acme deal/)).toBeInTheDocument();
    await runAxe(container);
  });
});

describe("Approvals page", () => {
  it("ApprovalsPage renders a pending proposal", async () => {
    vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue({
      // `{proposals, next_cursor}` is the real `crm.handlers.list_proposals` envelope.
      listProposals: vi.fn().mockResolvedValue({
        proposals: [
          {
            proposal_id: "prop-1",
            target_table: "opportunities",
            target_id: "opp-1",
            operation: "update",
            before_snapshot: { stage: "Discovery", value: 1000 },
            after_payload: { stage: "Negotiation" },
            reason: "Rep advanced the deal after a signed LOI.",
            status: "pending",
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        next_cursor: null,
      }),
      approveProposal: vi.fn(),
      rejectProposal: vi.fn(),
    } as never);

    const { container, findByText } = render(<ApprovalsPage />);
    expect(await findByText(/Update opportunities record opp-1/)).toBeInTheDocument();
    await runAxe(container);
  });
});

describe("Audit page", () => {
  it("AuditPage renders an audit record with a before/after diff", async () => {
    vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue({
      listAuditRecords: vi.fn().mockResolvedValue([
        {
          audit_id: "audit-1",
          target_table: "opportunities",
          target_id: "opp-1",
          before_snapshot: { stage: "Discovery" },
          after_snapshot: { stage: "Negotiation" },
          actor_rep_id: "rep-1",
          created_at: "2026-01-01T00:00:00Z",
        },
      ]),
      revertAuditRecord: vi.fn(),
    } as never);

    const { container, findByText } = render(
      <MemoryRouter>
        <AuditPage />
      </MemoryRouter>,
    );
    expect(await findByText(/opportunities record opp-1/)).toBeInTheDocument();
    await runAxe(container);
  });
});

describe("Evals page", () => {
  function buildArm(): EvalArm {
    return {
      provenance: {
        mode: "stub",
        timestamp: "2026-01-01T00:00:00Z",
        model_id: "anthropic.claude-opus-5-5",
        effort: "low",
        dataset_seed: 20260101,
        git_revision: "abc123",
        runtime_identifier: "local-stub",
        price_table_version: "v0",
      },
      model_metrics: {
        model_id: "anthropic.claude-opus-5-5",
        included_invocation_count: 20,
        cost_withheld_provisional: true,
      },
      aggregate_score: {
        mean_correctness: 0.75,
        scored_task_count: 13,
        unscored_task_count: 7,
        bad_write_rate: 0,
      },
      task_scores: [{ task_id: "gt-01", correctness: 1.0, bad_write: false }],
    };
  }

  class FakeEvalRunClient extends BaseFakeEvalRunClient {
    getRunStatus(): Promise<EvalRunStatusResponse> {
      return Promise.resolve({
        ...jobPayload({ status: "completed" }),
        matrix: { arms: [buildArm()] },
        artifacts: { raw: [] },
      });
    }
  }

  const fakeAuth: AuthContextValue = {
    status: "signed-in",
    identity: {
      userId: "user-1",
      repId: "rep-1",
      book: ["acct-1"],
      fullVisibility: false,
      expiresAt: 9_999_999_999,
    },
    signIn: vi.fn(),
    signOut: vi.fn(),
    getAccessToken: vi.fn().mockResolvedValue("tok-123"),
  };

  it("EvalDashboardPage renders a completed run with results", async () => {
    const client = new FakeEvalRunClient();
    const { container, findByText } = render(
      <EvalDashboardPage apiBaseUrl="/api" client={client} authOverride={fakeAuth} pollIntervalMs={5} />,
    );

    await userEvent.click(await (await import("@testing-library/react")).screen.findByRole(
      "button",
      { name: /run stub sweep/i },
    ));
    expect(await findByText(/run complete/i)).toBeInTheDocument();
    await runAxe(container);
  });
});

describe("Settings page", () => {
  const settingsConfigBody = {
    cognitoUserPoolId: "us-east-1_test",
    cognitoClientId: "client123",
    cognitoRegion: "us-east-1",
    cognitoHostedUiDomain: "",
    apiBaseUrl: "/api",
    agentBaseUrl: "/agent",
    agentRuntimeArn: "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/test",
  };

  const capabilitiesBody = {
    default_family: "anthropic",
    allow_cross_family_override: false,
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

  beforeEach(() => {
    __resetRuntimeConfigForTests();
    __resetSessionTuningForTests();
    // The page loads the caller's saved settings through the client's user_settings methods.
    const defaultsView = {
      tuning: { schema: 3, source: "config/models.yaml", tiers: {} },
      is_default: true,
      notice: null,
      updated_at: null,
    };
    vi.spyOn(useCrmApiModule, "useCrmApi").mockReturnValue({
      getUserSettings: vi.fn().mockResolvedValue(defaultsView),
      saveUserSettings: vi.fn().mockResolvedValue(defaultsView),
      resetUserSettings: vi.fn().mockResolvedValue(defaultsView),
    } as never);
    vi.spyOn(AuthProviderModule, "useAuth").mockReturnValue({
      status: "signed-in",
      identity: {
        userId: "user-1",
        repId: "rep-1",
        book: ["acct-1"],
        fullVisibility: false,
        expiresAt: 9_999_999_999,
      },
      signIn: vi.fn(),
      signOut: vi.fn(),
      getAccessToken: vi.fn().mockResolvedValue("tok-123"),
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (url === "/config.json") {
          return Promise.resolve({ ok: true, json: () => Promise.resolve(settingsConfigBody) });
        }
        if (url === "/api/capabilities") {
          return Promise.resolve({ ok: true, json: () => Promise.resolve(capabilitiesBody) });
        }
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );
  });

  it("SettingsPage renders a tuning fieldset per tier", async () => {
    await loadRuntimeConfig();
    const { container, findByText } = render(<SettingsPage />);
    expect(await findByText("Supervisor model tuning")).toBeInTheDocument();
    await runAxe(container);
  });
});
