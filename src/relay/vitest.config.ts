// SPDX-License-Identifier: MIT
// Vitest configuration for the streaming relay: Node environment, with tests/setup.ts installing the
// `awslambda` global.
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
    setupFiles: ["tests/setup.ts"],
    clearMocks: true,
    restoreMocks: true,
    unstubEnvs: true,
  },
});
