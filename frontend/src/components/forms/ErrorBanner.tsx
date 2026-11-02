// SPDX-License-Identifier: MIT
/**
 * Generic error banner for the closed error taxonomy, used wherever a `CrmApiError` is not
 * `scope_denied` (which renders via `ScopeDeniedNotice`).
 */
import type { JSX } from "react";

import { CrmApiError } from "../../api/CrmApiClient";

export function ErrorBanner({ error }: { error: unknown }): JSX.Element {
  const message = error instanceof CrmApiError ? error.body.message : "Something went wrong.";
  return (
    // `--danger-*`, not `--denied-*`: something failed.
    <p
      role="alert"
      className="max-w-prose rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-4 text-sm leading-relaxed text-danger-text"
    >
      {message}
    </p>
  );
}
