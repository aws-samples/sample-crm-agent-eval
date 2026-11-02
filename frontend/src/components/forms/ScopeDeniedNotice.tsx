// SPDX-License-Identifier: MIT
/**
 * Renders a `scope_denied` error as a clear, non-alarming message naming the missing scope, never as
 * an empty state.
 */
import type { JSX } from "react";

import type { CrmApiError } from "../../api/CrmApiClient";

export function ScopeDeniedNotice({ error }: { error: CrmApiError }): JSX.Element {
  return (
    <div
      role="status"
      className="max-w-prose space-y-2 rounded-surface border border-hairline border-denied-border bg-denied-surface px-5 py-4 text-denied-text"
    >
      <p className="text-sm leading-relaxed">
        You don&apos;t have access to this record
        {error.body.missingScope ? (
          <>
            {" "}
            (missing scope: <code className="font-numeric text-xs">{error.body.missingScope}</code>)
          </>
        ) : null}
        .
      </p>
      {/* States in words that this is not an absence; hue alone does not convey it. */}
      <p className="text-sm leading-relaxed">
        This is not the same as the record not existing — ask an admin if you believe this is wrong.
      </p>
    </div>
  );
}

/** True when `error` is the typed 403 this component is built to render. */
export function isScopeDenied(error: unknown): error is CrmApiError {
  return (
    typeof error === "object" &&
    error !== null &&
    "kind" in error &&
    (error as CrmApiError).kind === "scope_denied"
  );
}
