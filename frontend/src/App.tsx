// SPDX-License-Identifier: MIT
/**
 * App shell: routes to the signed-in route tree or the sign-in form based on `useAuth().status`.
 *
 * Every path is the target of a `<Link>` or `navigate(...)` in the pages, and each `:param` name
 * matches the `useParams` key the corresponding page reads.
 *
 * `getRuntimeConfig()` is called synchronously: `AuthProvider` only reports `signed-in` after
 * `loadRuntimeConfig()` has resolved, so the cache is warm by the time this tree renders.
 *
 * The signed-in layout is `AppShell`, which mounts the assistant panel as a sibling of the
 * `<Outlet />`. The panel is the only assistant surface; there is no `/chat` route.
 */
import { useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import type { JSX } from "react";

import { AppShell } from "./AppShell";
import { useAuth } from "./auth/AuthProvider";
import { SignInForm } from "./auth/SignInForm";
import { getRuntimeConfig, loadRuntimeConfig } from "./config/runtimeConfig";
import { AccountCreatePage } from "./pages/accounts/AccountCreatePage";
import { AccountDetailPage } from "./pages/accounts/AccountDetailPage";
import { AccountEditPage } from "./pages/accounts/AccountEditPage";
import { AccountListPage } from "./pages/accounts/AccountListPage";
import { ActivitiesPage } from "./pages/activities/ActivitiesPage";
import { ApprovalsPage } from "./pages/approvals/ApprovalsPage";
import { AuditDetailPage } from "./pages/audit/AuditDetailPage";
import { AuditPage } from "./pages/audit/AuditPage";
import { ContactCreatePage } from "./pages/contacts/ContactCreatePage";
import { ContactDetailPage } from "./pages/contacts/ContactDetailPage";
import { ContactEditPage } from "./pages/contacts/ContactEditPage";
import { EvalDashboardPage } from "./pages/evals/EvalDashboardPage";
import { OpportunityCreatePage } from "./pages/opportunities/OpportunityCreatePage";
import { OpportunityDetailPage } from "./pages/opportunities/OpportunityDetailPage";
import { OpportunityListPage } from "./pages/opportunities/OpportunityListPage";
import { PipelinePage } from "./pages/pipeline/PipelinePage";
import { SettingsPage } from "./pages/settings/SettingsPage";

/** Rendered for any path with no matching route, including a mistyped deep link. */
function NotFoundPage(): JSX.Element {
  return (
    <main>
      <h1>Page not found</h1>
      <p>
        That page does not exist. <a href="/pipeline">Go to the pipeline</a>.
      </p>
    </main>
  );
}

export function App(): JSX.Element {
  const { status } = useAuth();

  useEffect(() => {
    // Start the runtime-config fetch early; AuthProvider also awaits it.
    void loadRuntimeConfig();
  }, []);

  if (status === "loading") {
    return (
      <main aria-busy="true" aria-live="polite">
        <p>Loading…</p>
      </main>
    );
  }

  if (status === "signed-out") {
    return <SignInForm />;
  }

  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/pipeline" replace />} />
        <Route path="pipeline" element={<PipelinePage />} />

        <Route path="accounts" element={<AccountListPage />} />
        <Route path="accounts/new" element={<AccountCreatePage />} />
        <Route path="accounts/:accountId" element={<AccountDetailPage />} />
        <Route path="accounts/:accountId/edit" element={<AccountEditPage />} />

        <Route path="contacts/new" element={<ContactCreatePage />} />
        <Route path="contacts/:contactId" element={<ContactDetailPage />} />
        <Route path="contacts/:contactId/edit" element={<ContactEditPage />} />

        <Route path="opportunities" element={<OpportunityListPage />} />
        <Route path="opportunities/new" element={<OpportunityCreatePage />} />
        <Route path="opportunities/:opportunityId" element={<OpportunityDetailPage />} />
        <Route path="opportunities/:opportunityId/activities" element={<ActivitiesPage />} />

        <Route path="approvals" element={<ApprovalsPage />} />
        <Route path="audit" element={<AuditPage />} />
        <Route path="audit/:auditId" element={<AuditDetailPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route
          path="evals"
          element={<EvalDashboardPage apiBaseUrl={getRuntimeConfig().apiBaseUrl} />}
        />

        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
