// SPDX-License-Identifier: MIT
/** Account create page. Creation is low-risk and commits directly. */
import { useNavigate } from "react-router-dom";
import type { JSX } from "react";

import { AccountForm } from "../../components/forms/AccountForm";

export function AccountCreatePage(): JSX.Element {
  const navigate = useNavigate();
  return (
    <main className="mx-auto w-full max-w-narrow space-y-7 px-6 py-8">
      <h1 className="text-2xl font-semibold tracking-tight text-text-primary">New account</h1>
      <AccountForm
        onSaved={(accountId) => navigate(accountId ? `/accounts/${accountId}` : "/accounts")}
        onCancel={() => navigate("/accounts")}
      />
    </main>
  );
}
