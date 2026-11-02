// SPDX-License-Identifier: MIT
/**
 * Reads SSM parameters once per execution environment.
 *
 * {@link cachedResolver} caches the in-flight promise, so concurrent invocations share one read, and
 * clears the cache on failure, so the next call retries.
 */
import { GetParameterCommand, SSMClient } from "@aws-sdk/client-ssm";

/** Environment variable naming the region. Lambda sets it; the relay never declares it. */
export const ENV_REGION = "AWS_REGION";

/** Reads one parameter by name. */
export interface ParameterReader {
  read(name: string): Promise<string>;
}

/**
 * A reader backed by SSM, decrypting a `SecureString` when it finds one.
 */
export function ssmParameterReader(env: NodeJS.ProcessEnv = process.env): ParameterReader {
  const region = (env[ENV_REGION] ?? "").trim();
  const client = new SSMClient(region ? { region } : {});
  return {
    async read(name: string): Promise<string> {
      const response = await client.send(
        new GetParameterCommand({ Name: name, WithDecryption: true }),
      );
      return response.Parameter?.Value ?? "";
    },
  };
}

/** A value resolved at most once per execution environment. */
export interface CachedResolver<T> {
  /** Resolve the value, reusing an in-flight or completed read. */
  get(): Promise<T>;
  /** Discard the cached value, so a test can start from a cold start. */
  reset(): void;
}

/**
 * Wrap `load` so it runs at most once per execution environment, and never leaves a failure cached.
 *
 * @param load Performs the read. Called again only after a previous attempt rejected.
 * @returns A resolver whose `get()` shares one in-flight promise across concurrent callers.
 */
export function cachedResolver<T>(load: () => Promise<T>): CachedResolver<T> {
  let pending: Promise<T> | undefined;
  return {
    get(): Promise<T> {
      if (pending === undefined) {
        pending = load().catch((error: unknown) => {
          pending = undefined;
          throw error;
        });
      }
      return pending;
    },
    reset(): void {
      pending = undefined;
    },
  };
}
