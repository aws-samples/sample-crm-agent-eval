// SPDX-License-Identifier: MIT
/**
 * Content-Security-Policy violation trap for the browser journeys. `installCspViolationTrap(page)`
 * records every `securitypolicyviolation` event from any document the page loads; `cspViolationsOf`
 * returns a copy of the record, so a journey cannot clear it. `fixtures/auth.ts` installs it on every
 * journey's `page` and fails the test on any recorded violation.
 *
 * The init script here only listens for violation events and reports their directive, blocked URI
 * and document URI; it reads and writes no storage, cookie or credential.
 */
import type { Page } from "@playwright/test";

/** Name of the page binding the in-page listener reports a violation through. */
const CSP_REPORT_BINDING = "__crmE2eReportCspViolation";

/** The violations recorded on each trapped page, in arrival order. */
const recordedViolations = new WeakMap<Page, string[]>();

/**
 * Install the CSP violation trap on `page`: a page binding plus an init script that registers a
 * capturing `securitypolicyviolation` listener on `window` before any page script runs, in every
 * document the page loads. Call before the first navigation.
 */
export async function installCspViolationTrap(page: Page): Promise<void> {
  const violations: string[] = [];
  recordedViolations.set(page, violations);
  await page.exposeBinding(CSP_REPORT_BINDING, (_source, description: unknown) => {
    violations.push(String(description));
  });
  await page.addInitScript((binding: string) => {
    window.addEventListener(
      "securitypolicyviolation",
      (event: SecurityPolicyViolationEvent) => {
        const report = (window as unknown as Record<string, (text: string) => unknown>)[binding];
        void report(
          `${event.disposition} ${event.effectiveDirective} blocked ` +
            `${event.blockedURI || "(inline)"} on ${event.documentURI}`,
        );
      },
      true,
    );
  }, CSP_REPORT_BINDING);
}

/** A copy of the CSP violations recorded on `page` so far; empty for an untrapped page. */
export function cspViolationsOf(page: Page): readonly string[] {
  return [...(recordedViolations.get(page) ?? [])];
}
