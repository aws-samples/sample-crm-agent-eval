// SPDX-License-Identifier: MIT
/**
 * Installs the `awslambda` global the Lambda Node runtime injects, so the deployed entry point can be
 * exercised offline.
 *
 * `streamifyResponse` returns the handler it was given, and `HttpResponseStream.from` returns the
 * stream with the given metadata attached, so a test reads the committed status and headers off it.
 */
interface MetadataCarrier {
  metadata?: { statusCode?: number; headers?: Record<string, string> } | undefined;
}

const streamifyResponse = <H>(handler: H): H => handler;

const HttpResponseStream = {
  from<S extends object>(stream: S, metadata: Record<string, unknown>): S {
    (stream as S & MetadataCarrier).metadata = metadata as MetadataCarrier["metadata"];
    return stream;
  },
};

Object.defineProperty(globalThis, "awslambda", {
  value: { streamifyResponse, HttpResponseStream },
  writable: true,
  configurable: true,
});
