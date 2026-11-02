// SPDX-License-Identifier: MIT
/**
 * Account detail page: the account and its contacts (from `get_account`), plus the account's
 * opportunities (via `search_opportunities({ account_id })`), listed in the shared opportunities
 * table and counted so a delete that would be refused for open opportunities is surfaced
 * before it is attempted.
 */
import { useCallback, useEffect, useState, type JSX } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useCrmApi } from "../../api/useCrmApi";
import { DeleteProposalButton } from "../../components/forms/DeleteProposalButton";
import { OpportunityTable } from "../../components/opportunities/OpportunityTable";
import { ErrorBanner } from "../../components/forms/ErrorBanner";
import { isScopeDenied, ScopeDeniedNotice } from "../../components/forms/ScopeDeniedNotice";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import { cn, focusRing } from "../../components/ui/utils";
import type { Account, Contact, Opportunity } from "../../types/domain";
import { OPEN_STAGES } from "../../types/domain";
import { TruncationNotice } from "../opportunities/TruncationNotice";
import { asSearchOpportunitiesResult, type SearchOpportunitiesResult } from "../opportunities/types";

/** Inline text link styling, with the shared `focusRing`. */
const textLink = cn(
  "rounded-control text-text-link underline-offset-4 hover:underline",
  focusRing,
);

/** The page's own container rhythm, shared by every state this page can render. */
const pageShell = "mx-auto w-full max-w-content space-y-7 px-6 py-8";

interface AccountDetail {
  readonly account: Account;
  readonly contacts: readonly Contact[];
}

function parseAccountDetail(response: unknown): AccountDetail | undefined {
  if (
    typeof response === "object" &&
    response !== null &&
    "account" in response &&
    "contacts" in response
  ) {
    return response as unknown as AccountDetail;
  }
  return undefined;
}

const NO_OPPORTUNITIES: SearchOpportunitiesResult = {
  items: [],
  truncated: false,
  examined_count: 0,
  next_cursor: null,
};

function parseOpportunities(response: unknown): SearchOpportunitiesResult {
  return typeof response === "object" && response !== null
    ? asSearchOpportunitiesResult(response)
    : NO_OPPORTUNITIES;
}

function isOpenStage(stage: Opportunity["stage"]): boolean {
  return (OPEN_STAGES as readonly string[]).includes(stage);
}

export function AccountDetailPage(): JSX.Element {
  const { accountId = "" } = useParams<{ accountId: string }>();
  const navigate = useNavigate();
  const api = useCrmApi();

  const [detail, setDetail] = useState<AccountDetail | undefined>(undefined);
  const [opportunities, setOpportunities] = useState<SearchOpportunitiesResult>(NO_OPPORTUNITIES);
  const [error, setError] = useState<unknown>(undefined);
  const [loading, setLoading] = useState(true);

  const [loadedAccountId, setLoadedAccountId] = useState(accountId);
  if (loadedAccountId !== accountId) {
    setLoadedAccountId(accountId);
    setLoading(true);
    setError(undefined);
  }

  const load = useCallback(() => {
    // Both settle before either outcome is used, so a `get_account` failure wins over a
    // `search_opportunities` failure regardless of which settles first.
    return Promise.allSettled([
      api.getAccount({ account_id: accountId }),
      api.searchOpportunities({ account_id: accountId }),
    ])
      .then(([accountResult, opportunitiesResult]) => {
        if (accountResult.status === "rejected") {
          throw accountResult.reason;
        }
        if (opportunitiesResult.status === "rejected") {
          throw opportunitiesResult.reason;
        }
        const accountResponse = accountResult.value;
        const opportunitiesResponse = opportunitiesResult.value;
        const parsedDetail = parseAccountDetail(accountResponse);
        if (!parsedDetail) {
          throw new Error("get_account returned an unexpected shape");
        }
        setDetail(parsedDetail);
        setOpportunities(parseOpportunities(opportunitiesResponse));
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
        <Skeleton label="Loading account" className="h-9 w-2/5" />
        <div className="grid gap-5 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6 sm:grid-cols-2">
          {["industry", "region", "website", "employees"].map((field) => (
            <div key={field} className="space-y-2">
              <Skeleton label={`Loading ${field}`} className="h-3 w-20" />
              <Skeleton label={`Loading ${field} value`} className="h-4 w-32" />
            </div>
          ))}
        </div>
      </main>
    );
  }

  if (error) {
    if (isScopeDenied(error)) {
      return (
        <main className={pageShell}>
          <ScopeDeniedNotice error={error} />
        </main>
      );
    }
    return (
      <main className={pageShell}>
        <ErrorBanner error={error} />
      </main>
    );
  }

  if (!detail) {
    return (
      <main className={pageShell}>
        <div className="max-w-prose space-y-3 rounded-surface border border-hairline border-empty-border bg-empty-surface px-6 py-8 text-empty-text">
          <p className="text-md font-medium">Account not found.</p>
          <p className="text-sm leading-relaxed">
            No account exists with this id. This is not a permissions problem — if you expected to
            see a record here, check the id.
          </p>
          <p className="text-sm">
            <Link to="/accounts" className={textLink}>
              Back to accounts
            </Link>
          </p>
        </div>
      </main>
    );
  }

  const openCount = opportunities.items.filter((opp) => isOpenStage(opp.stage)).length;

  return (
    <main className={pageShell}>
      <header className="flex flex-wrap items-start justify-between gap-4">
        <h1 className="text-2xl font-semibold tracking-tight text-text-primary">
          {detail.account.name}
        </h1>
        <nav aria-label={`Actions for ${detail.account.name}`}>
          <Button asChild variant="secondary" size="sm">
            <Link to={`/accounts/${accountId}/edit`}>Edit account</Link>
          </Button>
        </nav>
      </header>

      <dl className="grid gap-5 rounded-surface border border-hairline border-border-subtle bg-surface-raised p-6 shadow-raised sm:grid-cols-2">
        {[
          { label: "Industry", value: detail.account.industry, numeric: false },
          { label: "Region", value: detail.account.region, numeric: false },
          { label: "Website", value: detail.account.website, numeric: false },
          { label: "Employees", value: detail.account.employee_count, numeric: true },
        ].map((field) => (
          <div key={field.label} className="space-y-1">
            <dt className="text-xs font-medium tracking-wide text-text-secondary">{field.label}</dt>
            {/* A missing value is an em dash in muted text, so absent reads differently from zero. */}
            <dd
              className={cn(
                "text-sm",
                field.value == null ? "text-text-muted" : "text-text-primary",
                field.numeric && field.value != null && "font-numeric",
              )}
            >
              {field.value ?? "—"}
            </dd>
          </div>
        ))}
      </dl>

      {openCount > 0 ? (
        <div
          role="status"
          className="max-w-prose rounded-surface border border-hairline border-caution-border bg-caution-surface px-5 py-4 text-caution-text"
        >
          <p className="text-sm leading-relaxed">
            This account has {openCount} open {openCount === 1 ? "opportunity" : "opportunities"}.
            Per the backend&apos;s cascade rule, deleting this account will be refused until those
            opportunities are closed or moved.
          </p>
        </div>
      ) : (
        <DeleteProposalButton
          targetTable="accounts"
          targetId={accountId}
          label={detail.account.name}
        />
      )}

      <section aria-labelledby="opportunities-heading" className="space-y-4">
        <h2
          id="opportunities-heading"
          className="text-lg font-semibold tracking-tight text-text-primary"
        >
          Opportunities
        </h2>
        {opportunities.truncated && (
          <TruncationNotice examinedCount={opportunities.examined_count} hasMore={false} />
        )}
        {opportunities.items.length === 0 ? (
          <p className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-5 py-6 text-sm text-empty-text">
            No opportunities yet.
          </p>
        ) : (
          <OpportunityTable
            items={opportunities.items}
            caption={`Opportunities for ${detail.account.name}`}
            truncated={opportunities.truncated}
          />
        )}
      </section>

      <section aria-labelledby="contacts-heading" className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2
            id="contacts-heading"
            className="text-lg font-semibold tracking-tight text-text-primary"
          >
            Contacts
          </h2>
          <Link to={`/contacts/new?accountId=${encodeURIComponent(accountId)}`} className={textLink}>
            Add a contact
          </Link>
        </div>
        {detail.contacts.length === 0 ? (
          <p className="rounded-surface border border-hairline border-empty-border bg-empty-surface px-5 py-6 text-sm text-empty-text">
            No contacts yet.
          </p>
        ) : (
          <ul className="divide-y divide-border-subtle overflow-hidden rounded-surface border border-hairline border-border-subtle bg-surface-raised">
            {detail.contacts.map((contact) => (
              <li
                key={contact.contact_id}
                className="flex items-center justify-between gap-3 px-cell-x py-cell-y hover:bg-surface-hover"
              >
                <Link to={`/contacts/${contact.contact_id}`} className={textLink}>
                  {contact.name}
                </Link>
                {contact.status === "departed" ? (
                  <Badge variant="neutral">(departed)</Badge>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <Button type="button" variant="ghost" size="sm" onClick={() => navigate("/accounts")}>
        Back to accounts
      </Button>
    </main>
  );
}
