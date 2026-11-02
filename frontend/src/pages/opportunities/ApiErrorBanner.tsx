// SPDX-License-Identifier: MIT
/**
 * Renders a `CrmApiError` as an accessible alert, with `scope_denied` rendered distinctly from every
 * other error kind. It is for errors only: a page receiving `scope_denied` renders this, never an
 * empty-state message.
 */
import type { JSX } from "react";

import { CrmApiError } from "../../api/CrmApiClient";

export function describeApiError(error: unknown): { title: string; detail: string; isScopeDenied: boolean } {
  if (error instanceof CrmApiError) {
    if (error.kind === "scope_denied") {
      const scope = error.body.missingScope;
      return {
        title: "Not permitted",
        detail: scope
          ? `You do not have the "${scope}" scope required for this request.`
          : "You do not have the scope required for this request.",
        isScopeDenied: true,
      };
    }
    return { title: errorKindTitle(error.kind), detail: error.body.message, isScopeDenied: false };
  }
  return {
    title: "Something went wrong",
    detail: error instanceof Error ? error.message : "An unexpected error occurred.",
    isScopeDenied: false,
  };
}

function errorKindTitle(kind: CrmApiError["kind"]): string {
  switch (kind) {
    case "authentication_failed":
      return "Sign-in required";
    case "not_found":
      return "Not found";
    case "validation_failed":
      return "Invalid request";
    case "conflict":
      return "Conflict";
    default:
      return "Something went wrong";
  }
}

export function ApiErrorBanner({ error }: { error: unknown }): JSX.Element {
  const { title, detail, isScopeDenied } = describeApiError(error);
  return (
    <div
      role="alert"
      data-testid={isScopeDenied ? "scope-denied-banner" : "error-banner"}
      // `--denied-*` is a separate hue from `--danger-*`; the two branches differ in hue and wording.
      className={
        isScopeDenied
          ? "max-w-prose rounded-surface border border-hairline border-denied-border bg-denied-surface px-5 py-4 text-denied-text"
          : "max-w-prose rounded-surface border border-hairline border-danger-border bg-danger-surface px-5 py-4 text-danger-text"
      }
    >
      <p className="text-sm leading-relaxed">
        <strong className="font-semibold">{title}.</strong> {detail}
      </p>
    </div>
  );
}
