// SPDX-License-Identifier: MIT
/**
 * Tailwind configuration, loaded by `src/styles/tailwind.css` through `@config`. Every theme value
 * is a `var(--token)` reference resolved from `src/styles/tokens.css` via `src/styles/theme.ts`; no
 * literal colour, size, or spacing value appears here.
 *
 * Content sources are declared by `@source` in `src/styles/tailwind.css`, not here.
 */

import animate from "tailwindcss-animate";

import { themeExtension } from "./src/styles/theme";

const config = {
  darkMode: ["class", '[data-theme="dark"]'],
  theme: { extend: themeExtension },
  plugins: [animate],
};

export default config;
