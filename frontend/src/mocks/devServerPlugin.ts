// SPDX-License-Identifier: MIT
/**
 * Vite plugin mounting the dev API stub.
 *
 * `apply: "serve"` keeps the plugin out of `vite build`, so neither the middleware nor the injected
 * bootstrap script nor anything under `src/mocks/` reaches a production bundle. `mocks.test.ts`
 * asserts that.
 *
 * Serves four things:
 *   - `POST /api/<tool>` from `devApi.ts`, which enforces the bearer token and book scope itself.
 *   - `GET /api/capabilities`, which sits outside the 22-tool POST surface.
 *   - `GET /config.json`, so no gitignored `public/config.json` has to exist to run the dev server.
 *   - an injected `<script>` that seeds a dev session before the app bundle runs.
 */

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import type { Plugin } from "vite";

import { errorEnvelope, handleDevApiRequest } from "./devApi";

const execFileAsync = promisify(execFile);

const REPO_ROOT = resolve(import.meta.dirname ?? process.cwd(), "..", "..", "..");
const VENV_PYTHON = resolve(REPO_ROOT, ".venv", "bin", "python3");

/**
 * Serialises the real capability matrix by asking Python for it.
 *
 * `providers.capabilities.load_capability_matrix` reads `config/model_capabilities.yaml`;
 * `dataclasses.asdict` yields the snake_case wire shape `capabilityMatrix.ts` parses.
 */
async function loadCapabilityMatrixJson(): Promise<string> {
  const { stdout } = await execFileAsync(
    VENV_PYTHON,
    [
      "-c",
      "import dataclasses, json; from providers.capabilities import load_capability_matrix; " +
        "print(json.dumps(dataclasses.asdict(load_capability_matrix())))",
    ],
    { cwd: REPO_ROOT },
  );
  return stdout.trim();
}

/** Matches the deploy-time config shape; values are local and carry no real identifiers. */
const DEV_RUNTIME_CONFIG = {
  cognitoUserPoolId: "us-east-1_devpool",
  cognitoClientId: "dev-client-id",
  cognitoRegion: "us-east-1",
  // Empty: the Hosted UI redirect needs a real pool, so the dev server offers SRP-only.
  cognitoHostedUiDomain: "",
  apiBaseUrl: "/api",
  agentBaseUrl: "/agent",
  // Raw, not pre-encoded: the hooks call `encodeURIComponent` themselves, and
  // `scripts/render_frontend_config.py` writes the raw ARN into the deployed `config.json`.
  agentRuntimeArn: "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/dev",
};

const BOOTSTRAP_SRC = "/src/mocks/devBootstrap.ts";

async function readJsonBody(stream: NodeJS.ReadableStream): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const chunk of stream) chunks.push(Buffer.from(chunk));
  if (chunks.length === 0) return {};
  try {
    const parsed: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    return typeof parsed === "object" && parsed !== null
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

export function devApiStubPlugin(): Plugin {
  return {
    name: "crm-dev-api-stub",
    apply: "serve",

    configureServer(server) {
      server.middlewares.use("/config.json", (_request, response) => {
        response.setHeader("Content-Type", "application/json");
        response.end(JSON.stringify(DEV_RUNTIME_CONFIG));
      });

      /**
       * `GET /api/capabilities`. Requires a bearer token, like the POST surface. A missing `.venv`
       * is a 503 with a reason rather than a fabricated matrix.
       */
      server.middlewares.use("/api/capabilities", (request, response, next) => {
        if (request.method !== "GET") {
          next();
          return;
        }
        response.setHeader("Content-Type", "application/json");
        if (!/^Bearer\s+\S+/.test(request.headers.authorization ?? "")) {
          const denied = errorEnvelope(401, "authentication_failed", "Bearer token required.");
          response.statusCode = denied.status;
          response.end(JSON.stringify(denied.body));
          return;
        }
        if (!existsSync(VENV_PYTHON)) {
          const unavailable = errorEnvelope(
            503,
            "capability_matrix_unavailable",
            `No interpreter at ${VENV_PYTHON}; cannot read config/model_capabilities.yaml.`,
          );
          response.statusCode = unavailable.status;
          response.end(JSON.stringify(unavailable.body));
          return;
        }
        void loadCapabilityMatrixJson().then(
          (json) => {
            response.statusCode = 200;
            response.end(json);
          },
          (error: unknown) => {
            const failed = errorEnvelope(
              503,
              "capability_matrix_unavailable",
              error instanceof Error ? error.message : String(error),
            );
            response.statusCode = failed.status;
            response.end(JSON.stringify(failed.body));
          },
        );
      });

      server.middlewares.use("/api", (request, response, next) => {
        if (request.method !== "POST") {
          next();
          return;
        }
        const toolName = (request.url ?? "/").replace(/^\//, "").split("?")[0];
        void readJsonBody(request).then((input) => {
          const result = handleDevApiRequest(
            toolName,
            input,
            request.headers.authorization ?? undefined,
          );
          response.statusCode = result.status;
          response.setHeader("Content-Type", "application/json");
          response.end(JSON.stringify(result.body));
        });
      });
    },

    transformIndexHtml() {
      return [
        {
          tag: "script",
          attrs: { type: "module", src: BOOTSTRAP_SRC },
          injectTo: "head",
        },
      ];
    },
  };
}
