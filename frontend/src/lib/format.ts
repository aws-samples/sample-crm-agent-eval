// SPDX-License-Identifier: MIT
/**
 * The shared currency, date, relative-time, model-spend and latency formatters. Consumed by
 * `pages/opportunities/`, `pages/activities/`, `pages/audit/`, `pages/evals/` and
 * `components/timeline/`.
 *
 * Every formatter is total: it renders an em dash for an absent value and the raw input for an
 * unparseable one.
 */

/** What every formatter renders for an absent value, so absent reads differently from zero. */
export const ABSENT = "—";

/** `Intl` instances, constructed once. */
const currency = new Intl.NumberFormat(undefined, {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

/**
 * Small-figure USD formatter for **model spend** (eval cost), distinct from {@link formatCurrency}.
 *
 * `formatCurrency` renders whole dollars (deal values); this keeps 2–6 fraction digits.
 */
const modelCost = new Intl.NumberFormat(undefined, {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 6,
});

const dateOnly = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
});

const dateTime = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

/**
 * Format a monetary amount in whole dollars (CRM deal values).
 *
 * @param amount - The amount, or `null`/`undefined` for an absent value.
 * @returns The localised currency string, or {@link ABSENT}.
 */
export function formatCurrency(amount: number | null | undefined): string {
  if (amount == null || !Number.isFinite(amount)) return ABSENT;
  return currency.format(amount);
}

/**
 * Format a date, discarding any time component.
 *
 * @param iso - An ISO 8601 date or date-time string, or `null`/`undefined`.
 * @returns The localised date, {@link ABSENT} when absent, or `iso` verbatim when unparseable —
 *   never an empty string and never `"Invalid Date"`.
 */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return ABSENT;
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return dateOnly.format(parsed);
}

/**
 * Format a timestamp including its time of day.
 *
 * @param iso - An ISO 8601 date-time string, or `null`/`undefined`.
 * @returns The localised date and time, {@link ABSENT} when absent, or `iso` verbatim when
 *   unparseable.
 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return ABSENT;
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return dateTime.format(parsed);
}

/**
 * Format a model-spend amount in USD, keeping cents and sub-cent precision.
 *
 * An absent or non-finite value renders {@link ABSENT}; a real `0` renders `$0.00`.
 *
 * @param amount - The amount in USD, or `null`/`undefined` for an absent value.
 * @returns The localised currency string with sub-dollar precision, or {@link ABSENT}.
 */
export function formatUsd(amount: number | null | undefined): string {
  if (amount == null || !Number.isFinite(amount)) return ABSENT;
  return modelCost.format(amount);
}

/**
 * Format a duration in milliseconds as a whole-millisecond figure ("123 ms").
 *
 * An absent or non-finite value renders {@link ABSENT}; a real `0` renders "0 ms". Used by the
 * eval dashboard's P50/P95 latency figures.
 *
 * @param ms - The duration in milliseconds, or `null`/`undefined` for an absent value.
 * @returns The rounded millisecond string, or {@link ABSENT}.
 */
export function formatDurationMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return ABSENT;
  return `${Math.round(ms)} ms`;
}

/** Unit boundaries in seconds, coarsest first, for {@link formatRelativeTime}. */
const RELATIVE_UNITS: readonly (readonly [Intl.RelativeTimeFormatUnit, number])[] = [
  ["year", 60 * 60 * 24 * 365],
  ["month", 60 * 60 * 24 * 30],
  ["week", 60 * 60 * 24 * 7],
  ["day", 60 * 60 * 24],
  ["hour", 60 * 60],
  ["minute", 60],
];

/**
 * Format a timestamp as a relative time ("3 days ago", "in 2 months").
 *
 * @param iso - An ISO 8601 date-time string, or `null`/`undefined`.
 * @param now - The instant to compare against. Injectable so a test is not clock-dependent;
 *   production omits it.
 * @returns The relative phrase, {@link ABSENT} when absent, or `iso` verbatim when unparseable.
 */
export function formatRelativeTime(
  iso: string | null | undefined,
  now: Date = new Date(),
): string {
  if (!iso) return ABSENT;
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;

  const deltaSeconds = (parsed.getTime() - now.getTime()) / 1000;
  for (const [unit, seconds] of RELATIVE_UNITS) {
    if (Math.abs(deltaSeconds) >= seconds) {
      return relative.format(Math.round(deltaSeconds / seconds), unit);
    }
  }
  return relative.format(Math.round(deltaSeconds), "second");
}
