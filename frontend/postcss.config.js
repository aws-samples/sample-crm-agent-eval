// SPDX-License-Identifier: MIT
/**
 * PostCSS pipeline. Vite picks this up automatically for every stylesheet it processes, which is
 * what makes `src/styles/tailwind.css` (and through its `@config`, `tailwind.config.ts`) consumed by
 * the build rather than merely present. Tailwind 4 adds vendor prefixes itself.
 */
export default {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};
