// SPDX-License-Identifier: MIT
/**
 * Contact create page. Requires an `accountId` query param, since `create_contact` requires
 * `account_id`.
 */
import type { JSX } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { ContactForm } from "../../components/forms/ContactForm";
import { cn, focusRing } from "../../components/ui/utils";

/** The page's container rhythm, shared by both states this page can render. */
const pageShell = "mx-auto w-full max-w-narrow space-y-7 px-6 py-8";

export function ContactCreatePage(): JSX.Element {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const accountId = searchParams.get("accountId") ?? "";

  if (!accountId) {
    return (
      <main className={pageShell}>
        <div className="max-w-prose space-y-3 rounded-surface border border-hairline border-info-border bg-info-surface px-5 py-4 text-info-text">
          <p className="text-sm leading-relaxed">
            A contact must belong to an account. Open an account first, then choose &quot;Add a
            contact&quot; from there.
          </p>
          <p className="text-sm">
            <Link
              to="/accounts"
              className={cn(
                "rounded-control text-text-link underline-offset-4 hover:underline",
                focusRing,
              )}
            >
              Go to accounts
            </Link>
          </p>
        </div>
      </main>
    );
  }

  return (
    <main className={pageShell}>
      <h1 className="text-2xl font-semibold tracking-tight text-text-primary">New contact</h1>
      <ContactForm
        accountId={accountId}
        onSaved={(contactId) => navigate(contactId ? `/contacts/${contactId}` : `/accounts/${accountId}`)}
        onCancel={() => navigate(`/accounts/${accountId}`)}
      />
    </main>
  );
}
