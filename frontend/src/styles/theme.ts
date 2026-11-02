// SPDX-License-Identifier: MIT
/**
 * Tailwind theme map for the design token layer.
 *
 * Every value here is a `var(--token)` reference into `tokens.css` and never a literal, asserted by
 * `tokens.test.ts`. Add a token to `tokens.css` and a key here; never inline a value.
 */

/** A Tailwind theme scale: utility suffix to CSS value. */
export type TokenScale = Record<string, string>;

/** Builds a scale whose every value is a `var()` reference, so a literal cannot slip in. */
function scale(entries: Record<string, string>): TokenScale {
  return Object.fromEntries(
    Object.entries(entries).map(([key, token]) => [key, `var(--${token})`]),
  );
}

export const colors: TokenScale = scale({
  "surface-sunken": "surface-sunken",
  "surface-base": "surface-base",
  "surface-raised": "surface-raised",
  "surface-overlay": "surface-overlay",
  "surface-inset": "surface-inset",
  "surface-hover": "surface-hover",
  "surface-selected": "surface-selected",
  "surface-inverse": "surface-inverse",

  "text-primary": "text-primary",
  "text-secondary": "text-secondary",
  "text-muted": "text-muted",
  "text-inverse": "text-inverse",
  "text-on-accent": "text-on-accent",
  "text-link": "text-link",

  "border-subtle": "border-subtle",
  "border-default": "border-default",
  "border-control": "border-control",
  "border-strong": "border-strong",
  "border-inverse": "border-inverse",

  "accent-emphasis": "accent-emphasis",
  "accent-emphasis-hover": "accent-emphasis-hover",
  "accent-emphasis-active": "accent-emphasis-active",
  "accent-surface": "accent-surface",
  "accent-surface-hover": "accent-surface-hover",
  "accent-border": "accent-border",
  "accent-text": "accent-text",

  "denied-emphasis": "denied-emphasis",
  "denied-surface": "denied-surface",
  "denied-border": "denied-border",
  "denied-text": "denied-text",

  "danger-emphasis": "danger-emphasis",
  "danger-emphasis-hover": "danger-emphasis-hover",
  "danger-surface": "danger-surface",
  "danger-border": "danger-border",
  "danger-text": "danger-text",

  "caution-emphasis": "caution-emphasis",
  "caution-surface": "caution-surface",
  "caution-border": "caution-border",
  "caution-text": "caution-text",

  "positive-emphasis": "positive-emphasis",
  "positive-surface": "positive-surface",
  "positive-border": "positive-border",
  "positive-text": "positive-text",

  "info-emphasis": "info-emphasis",
  "info-surface": "info-surface",
  "info-border": "info-border",
  "info-text": "info-text",

  "empty-surface": "empty-surface",
  "empty-border": "empty-border",
  "empty-text": "empty-text",

  "pending-surface": "pending-surface",
  "pending-border": "pending-border",
  "pending-text": "pending-text",

  "diff-removed-surface": "diff-removed-surface",
  "diff-removed-border": "diff-removed-border",
  "diff-removed-text": "diff-removed-text",
  "diff-added-surface": "diff-added-surface",
  "diff-added-border": "diff-added-border",
  "diff-added-text": "diff-added-text",

  "series-1": "series-1",
  "series-2": "series-2",
  "series-1-surface": "series-1-surface",
  "series-2-surface": "series-2-surface",

  "skeleton-base": "skeleton-base",
  "skeleton-shimmer": "skeleton-shimmer",
  "overlay-scrim": "overlay-scrim",
  "focus-ring": "focus-ring-color",
});

export const spacing: TokenScale = scale({
  0: "space-0",
  px: "space-px",
  1: "space-1",
  2: "space-2",
  3: "space-3",
  4: "space-4",
  5: "space-5",
  6: "space-6",
  7: "space-7",
  8: "space-8",
  9: "space-9",
  10: "space-10",
  11: "space-11",
  12: "space-12",
  13: "space-13",
  "cell-x": "cell-padding-x",
  "cell-y": "cell-padding-y",
});

export const fontSize: TokenScale = scale({
  "3xs": "text-3xs",
  "2xs": "text-2xs",
  xs: "text-xs",
  sm: "text-sm",
  base: "text-base",
  md: "text-md",
  lg: "text-lg",
  xl: "text-xl",
  "2xl": "text-2xl",
  "3xl": "text-3xl",
});

export const fontFamily: Record<string, string[]> = {
  sans: ["var(--font-sans)"],
  numeric: ["var(--font-numeric)"],
};

export const lineHeight: TokenScale = scale({
  tight: "leading-tight",
  snug: "leading-snug",
  normal: "leading-normal",
  relaxed: "leading-relaxed",
});

export const fontWeight: TokenScale = scale({
  regular: "weight-regular",
  medium: "weight-medium",
  semibold: "weight-semibold",
  bold: "weight-bold",
});

export const letterSpacing: TokenScale = scale({
  tight: "tracking-tight",
  normal: "tracking-normal",
  wide: "tracking-wide",
});

export const borderRadius: TokenScale = scale({
  none: "radius-none",
  control: "radius-control",
  surface: "radius-surface",
  overlay: "radius-overlay",
  pill: "radius-pill",
});

export const borderWidth: TokenScale = scale({
  hairline: "border-width-hairline",
  emphasis: "border-width-emphasis",
});

/** The focus ring is a token applied to every control. */
export const ringWidth: TokenScale = scale({ focus: "focus-ring-width" });
export const ringOffsetWidth: TokenScale = scale({ focus: "focus-ring-offset" });

export const boxShadow: TokenScale = scale({
  flush: "elevation-flush",
  raised: "elevation-raised",
  overlay: "elevation-overlay",
  popover: "elevation-popover",
});

export const transitionDuration: TokenScale = scale({
  instant: "duration-instant",
  fast: "duration-fast",
  normal: "duration-normal",
  slow: "duration-slow",
});

export const transitionTimingFunction: TokenScale = scale({
  standard: "ease-standard",
  entrance: "ease-entrance",
  exit: "ease-exit",
});

export const height: TokenScale = scale({
  "row-dense": "row-height-dense",
  "row-default": "row-height-default",
  "row-relaxed": "row-height-relaxed",
  "control-sm": "control-height-sm",
  "control-md": "control-height-md",
  "control-lg": "control-height-lg",
  header: "layout-header-height",
});

export const width: TokenScale = scale({
  nav: "layout-nav-width",
  "nav-collapsed": "layout-nav-width-collapsed",
  panel: "layout-panel-width",
});

export const maxWidth: TokenScale = scale({
  content: "layout-content-max",
  prose: "measure-prose",
  narrow: "measure-narrow",
});

export const zIndex: TokenScale = scale({
  base: "z-base",
  sticky: "z-sticky",
  panel: "z-panel",
  overlay: "z-overlay",
  popover: "z-popover",
  toast: "z-toast",
});

export const backgroundImage: TokenScale = scale({
  "pending-hatch": "pending-hatch",
  "series-1-fill": "series-1-fill",
  "series-2-fill": "series-2-fill",
});

/**
 * Breakpoints. Tailwind resolves `screens` at config time and cannot read a CSS variable
 * here, so these repeat the `--breakpoint-*` rem values; `tokens.test.ts` asserts the two agree.
 */
export const screens: TokenScale = {
  md: "48rem",
  lg: "64rem",
  xl: "80rem",
};

/** The `theme.extend` object consumed by `tailwind.config.ts`. */
export const themeExtension = {
  colors,
  spacing,
  fontSize,
  fontFamily,
  lineHeight,
  fontWeight,
  letterSpacing,
  borderRadius,
  borderWidth,
  ringWidth,
  ringOffsetWidth,
  boxShadow,
  transitionDuration,
  transitionTimingFunction,
  height,
  width,
  maxWidth,
  zIndex,
  backgroundImage,
  screens,
} as const;
