// SPDX-License-Identifier: MIT
import { defineConfig, mergeConfig } from "vitest/config";

import viteConfig from "./vite.config";

/**
 * Vitest-specific config, layered on top of `vite.config.ts` via `mergeConfig`. Vitest prefers
 * this file over `vite.config.ts` and does not merge the two on its own.
 */
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      css: false,
    },
  }),
);
