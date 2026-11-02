// SPDX-License-Identifier: MIT
/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

import { devApiStubPlugin } from "./src/mocks/devServerPlugin";

// Vite config, including the base vitest `test` block; `frontend/vitest.config.ts` merges its
// additions on top via `mergeConfig`.
export default defineConfig({
  plugins: [react(), devApiStubPlugin()],
  // `amazon-cognito-identity-js` references the Node `global` at module-import time, which does not
  // exist in a browser. Rollup's build shims it already; only the dev server needs this.
  define: { global: "globalThis" },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    globals: false,
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      exclude: ["src/types/generated/**", "scripts/**"],
    },
  },
});
