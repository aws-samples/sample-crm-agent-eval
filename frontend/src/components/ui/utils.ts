// SPDX-License-Identifier: MIT
/**
 * Class-name helper shared by every primitive in this directory: merges conditional classes and
 * resolves Tailwind conflicts so a caller's `className` reliably wins over a variant default.
 *
 * `tailwind-merge` knows only Tailwind's default scales, so the token layer's custom keys are
 * declared here; an undeclared key is silently dropped or duplicated.
 */

import { type ClassValue, clsx } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

const BORDER_WIDTHS = ["hairline", "emphasis"] as const;
const RADII = ["none", "control", "surface", "overlay", "pill"] as const;
const HEIGHTS = [
  "row-dense",
  "row-default",
  "row-relaxed",
  "control-sm",
  "control-md",
  "control-lg",
  "header",
] as const;

/**
 * The custom class groups `tailwind-merge` must be taught. Must cover every scale key in `theme.ts`.
 */
export const CUSTOM_CLASS_GROUPS = {
  "font-size": [{ text: ["3xs", "2xs", "md"] }],
  "font-family": [{ font: ["numeric"] }],
  "font-weight": [{ font: ["regular"] }],
  leading: [{ leading: ["snug"] }],

  "border-w": [{ border: BORDER_WIDTHS }],
  "border-w-x": [{ "border-x": BORDER_WIDTHS }],
  "border-w-y": [{ "border-y": BORDER_WIDTHS }],
  "border-w-t": [{ "border-t": BORDER_WIDTHS }],
  "border-w-r": [{ "border-r": BORDER_WIDTHS }],
  "border-w-b": [{ "border-b": BORDER_WIDTHS }],
  "border-w-l": [{ "border-l": BORDER_WIDTHS }],

  "ring-w": [{ ring: ["focus"] }],
  "ring-offset-w": [{ "ring-offset": ["focus"] }],

  rounded: [{ rounded: RADII }],
  "rounded-t": [{ "rounded-t": RADII }],
  "rounded-r": [{ "rounded-r": RADII }],
  "rounded-b": [{ "rounded-b": RADII }],
  "rounded-l": [{ "rounded-l": RADII }],

  shadow: [{ shadow: ["flush", "raised", "overlay", "popover"] }],

  h: [{ h: HEIGHTS }],
  w: [{ w: ["nav", "nav-collapsed", "panel"] }],
  "max-w": [{ "max-w": ["content", "prose", "narrow"] }],

  duration: [{ duration: ["instant", "fast", "normal", "slow"] }],
  ease: [{ ease: ["standard", "entrance", "exit"] }],

  z: [{ z: ["base", "sticky", "panel", "overlay", "popover", "toast"] }],
  "bg-image": [{ bg: ["pending-hatch", "series-1-fill", "series-2-fill"] }],
} as const;

const twMerge = extendTailwindMerge({ extend: { classGroups: CUSTOM_CLASS_GROUPS } });

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/** Visible focus ring applied by every interactive primitive. */
export const focusRing =
  "outline-none focus-visible:ring-focus focus-visible:ring-focus-ring focus-visible:ring-offset-focus focus-visible:ring-offset-surface-base";
