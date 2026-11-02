// SPDX-License-Identifier: MIT
/**
 * `ScopeDeniedNotice` renders a typed 403 (`CrmApiError.kind === "scope_denied"`) distinctly from an
 * empty queue or list. Shared by `pages/approvals` and `pages/audit`. Styled on `--denied-*`, not
 * `--danger-*`.
 */
import type { JSX } from "react";

export interface ScopeDeniedNoticeProps {
  readonly missingScope?: string;
  readonly message?: string;
}

export function ScopeDeniedNotice({ missingScope, message }: ScopeDeniedNoticeProps): JSX.Element {
  return (
    <div
      role="alert"
      className="max-w-prose space-y-2 rounded-surface border border-hairline border-denied-border bg-denied-surface px-5 py-4 text-denied-text"
    >
      <p className="text-sm leading-relaxed">
        <strong className="font-semibold">Access denied.</strong>{" "}
        {message ?? "You don't have permission to view this."}
      </p>
      {missingScope && (
        <p className="text-xs">
          Missing scope: <code className="font-numeric">{missingScope}</code>
        </p>
      )}
    </div>
  );
}
