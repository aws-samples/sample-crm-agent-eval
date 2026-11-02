// SPDX-License-Identifier: MIT
/** Account edit page. Update is low-risk and commits directly. */
import { useCallback, useEffect, useState, type JSX } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { useCrmApi } from "../../api/useCrmApi";
import { AccountForm } from "../../components/forms/AccountForm";
import { ErrorBanner } from "../../components/forms/ErrorBanner";
import { isScopeDenied, ScopeDeniedNotice } from "../../components/forms/ScopeDeniedNotice";
import { Skeleton } from "../../components/ui/skeleton";
import type { Account } from "../../types/domain";

/** The page's own container rhythm, shared by every state this page can render. */
const pageShell = "mx-auto w-full max-w-narrow space-y-7 px-6 py-8";

export function AccountEditPage(): JSX.Element {
  const { accountId = "" } = useParams<{ accountId: string }>();
  const navigate = useNavigate();
  const api = useCrmApi();

  const [account, setAccount] = useState<Account | undefined>(undefined);
  const [error, setError] = useState<unknown>(undefined);
  const [loading, setLoading] = useState(true);

  const [loadedAccountId, setLoadedAccountId] = useState(accountId);
  if (loadedAccountId !== accountId) {
    setLoadedAccountId(accountId);
    setLoading(true);
    setError(undefined);
  }

  const load = useCallback(() => {
    return api
      .getAccount({ account_id: accountId })
      .then((response) => {
        if (typeof response === "object" && response !== null && "account" in response) {
          setAccount((response as { account: Account }).account);
        }
      })
      .catch((err: unknown) => {
        setError(err);
      })
      .finally(() => {
        setLoading(false);
      });
    // `api` is referentially stable (memoised by `useCrmApi`) and is omitted from the deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Loading, denied, error and not-found each have a distinct rendering.
  if (loading) {
    return (
      <main aria-busy="true" aria-live="polite" className={pageShell}>
        <Skeleton label="Loading account" className="h-9 w-1/2" />
        <div className="space-y-5 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6">
          {["name", "industry", "region", "website"].map((field) => (
            <div key={field} className="space-y-2">
              <Skeleton label={`Loading ${field} label`} className="h-3 w-24" />
              <Skeleton label={`Loading ${field} field`} className="h-control-md w-full" />
            </div>
          ))}
        </div>
      </main>
    );
  }

  if (error) {
    return (
      <main className={pageShell}>
        {isScopeDenied(error) ? <ScopeDeniedNotice error={error} /> : <ErrorBanner error={error} />}
      </main>
    );
  }

  if (!account) {
    return (
      <main className={pageShell}>
        <p className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-6 py-8 text-sm text-empty-text">
          Account not found.
        </p>
      </main>
    );
  }

  return (
    <main className={pageShell}>
      <h1 className="text-2xl font-semibold tracking-tight text-text-primary">
        Edit {account.name}
      </h1>
      <AccountForm
        initial={account}
        onSaved={() => navigate(`/accounts/${accountId}`)}
        onCancel={() => navigate(`/accounts/${accountId}`)}
      />
    </main>
  );
}
