// SPDX-License-Identifier: MIT
/**
 * Asserts the relay's toolchain and dependency surface against the installed packages: the AgentCore
 * and Cognito client exports, the package's isolation from `frontend/`, exact pins, the Node engine,
 * and the typed `awslambda` streaming global.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { BedrockAgentCoreClient, InvokeAgentRuntimeCommand } from "@aws-sdk/client-bedrock-agentcore";
import { CognitoJwtVerifier } from "aws-jwt-verify";
import type { StreamifyHandler } from "aws-lambda";

interface PackageManifest {
  readonly name: string;
  readonly type: string;
  readonly engines: Readonly<Record<string, string>>;
  readonly dependencies: Readonly<Record<string, string>>;
  readonly devDependencies: Readonly<Record<string, string>>;
}

const manifest = JSON.parse(
  readFileSync(new URL("../package.json", import.meta.url), "utf8"),
) as PackageManifest;

const frontendManifest = JSON.parse(
  readFileSync(new URL("../../../frontend/package.json", import.meta.url), "utf8"),
) as PackageManifest;

describe("the AgentCore client surface the relay uses", () => {
  it("exports the client and the InvokeAgentRuntime command binding", () => {
    expect(typeof BedrockAgentCoreClient).toBe("function");
    expect(typeof InvokeAgentRuntimeCommand).toBe("function");
  });

  it("accepts the request fields of the first-party example", () => {
    const command = new InvokeAgentRuntimeCommand({
      agentRuntimeArn: "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/example",
      runtimeSessionId: "session-0000000000000000000000000000000000",
      payload: JSON.stringify({ prompt: "probe" }),
      contentType: "application/json",
      qualifier: "DEFAULT",
    });
    expect(command.input.qualifier).toBe("DEFAULT");
    expect(command.input.contentType).toBe("application/json");
  });

  it("leaves runtimeUserId unset, so the role needs only InvokeAgentRuntime", () => {
    const command = new InvokeAgentRuntimeCommand({
      agentRuntimeArn: "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/example",
      runtimeSessionId: "session-0000000000000000000000000000000000",
      payload: JSON.stringify({ prompt: "probe" }),
    });
    expect(command.input.runtimeUserId).toBeUndefined();
  });
});

describe("the Cognito verifier surface", () => {
  it("exposes the JWKS-caching verifier factory the jwt task builds on", () => {
    expect(typeof CognitoJwtVerifier.create).toBe("function");
  });
});

describe("the package is isolated from frontend/", () => {
  it("installs from its own manifest under its own name", () => {
    expect(manifest.name).toBe("crm-eval-relay");
    expect(manifest.name).not.toBe(frontendManifest.name);
    expect(manifest.type).toBe("module");
  });

  it("shares no runtime dependency with the browser bundle", () => {
    const shared = Object.keys(manifest.dependencies).filter(
      (name) => name in frontendManifest.dependencies || name in frontendManifest.devDependencies,
    );
    expect(shared).toEqual([]);
  });

  it("pins every dependency exactly, so the lockfile is the sole install authority", () => {
    const specifiers = [
      ...Object.values(manifest.dependencies),
      ...Object.values(manifest.devDependencies),
    ];
    expect(specifiers.length).toBeGreaterThan(0);
    for (const specifier of specifiers) {
      expect(specifier).toMatch(/^\d+\.\d+\.\d+$/);
    }
  });

  it("targets Node 24 or newer, matching the Lambda managed runtime and above the vitest/vite floor", () => {
    expect(manifest.engines["node"]).toBe(">=24.0.0");
  });
});

describe("the streaming handler shape is typed", () => {
  it("types the streamifyResponse decorator and its response stream", () => {
    const decorate: typeof awslambda.streamifyResponse = (handler) => handler;
    const handler: StreamifyHandler<{ body?: string }, void> = async (
      _event,
      responseStream: awslambda.HttpResponseStream,
    ) => {
      responseStream.end();
    };
    expect(typeof decorate(handler)).toBe("function");
  });
});
