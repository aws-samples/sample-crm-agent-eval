// SPDX-License-Identifier: MIT
/**
 * Tests for the relay's Cognito bearer-token verification.
 *
 * Tokens are signed with a real RSA key pair and resolved through the library's real
 * `SimpleJwksCache`; only the network hop is replaced, by a `Fetcher` that counts calls and returns
 * a real JWKS document.
 */
import { createSign, generateKeyPairSync, type KeyObject } from "node:crypto";

import { afterEach, describe, expect, it } from "vitest";
import { SimpleJwksCache } from "aws-jwt-verify/jwk";
import type { Fetcher } from "aws-jwt-verify/https";

import {
  ENV_AUDIENCE_PARAMETER,
  ENV_ISSUER,
  ENV_JWKS_URL,
  JWT_HEADER,
  JwtVerificationError,
  assertIssuedToAllowedClient,
  createVerifier,
  extractBearerToken,
  loadJwtEnvConfig,
  resetSharedVerifier,
  resolveAudience,
  resolveJwtConfig,
  sharedContext,
  verifyBearerToken,
  type JwtConfig,
  type ParameterReader,
  type TokenVerifier,
} from "../src/jwt.js";

const ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example";
const JWKS_URI = `${ISSUER}/.well-known/jwks.json`;
const CLIENT_ID = "1example23client45id67890";
const OTHER_CLIENT_ID = "9other87client65id43210";
const AUDIENCE_PARAMETER = "/crm-relay/relay/cognito-audience";
const KEY_ID = "test-key-1";
const OTHER_KEY_ID = "test-key-2";

const signingKeys = generateKeyPairSync("rsa", { modulusLength: 2048 });
const foreignKeys = generateKeyPairSync("rsa", { modulusLength: 2048 });

function base64Url(value: Buffer | string): string {
  return Buffer.from(value)
    .toString("base64")
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
}

/** Publish one RSA public key as a JWKS entry, in the shape Cognito publishes. */
function jwkFor(publicKey: KeyObject, kid: string): Record<string, string> {
  const jwk = publicKey.export({ format: "jwk" }) as { n?: string; e?: string };
  return { kty: "RSA", alg: "RS256", use: "sig", kid, n: jwk.n ?? "", e: jwk.e ?? "" };
}

/** Sign a JWT with RS256, or emit an unsigned/foreign-signed token for the negative cases. */
function signToken(
  claims: Record<string, unknown>,
  options: { kid?: string; key?: KeyObject; alg?: string } = {},
): string {
  const header = { alg: options.alg ?? "RS256", kid: options.kid ?? KEY_ID, typ: "JWT" };
  const signingInput = `${base64Url(JSON.stringify(header))}.${base64Url(JSON.stringify(claims))}`;
  if (header.alg === "none") {
    return `${signingInput}.`;
  }
  const signer = createSign("RSA-SHA256");
  signer.update(signingInput);
  return `${signingInput}.${base64Url(signer.sign(options.key ?? signingKeys.privateKey))}`;
}

const inOneHour = () => Math.floor(Date.now() / 1000) + 3600;
const anHourAgo = () => Math.floor(Date.now() / 1000) - 3600;

/** An ID token carrying the CRM's own custom claims, as the pre-token-generation trigger emits. */
function idTokenClaims(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    iss: ISSUER,
    aud: CLIENT_ID,
    token_use: "id",
    exp: inOneHour(),
    iat: Math.floor(Date.now() / 1000),
    sub: "11111111-2222-3333-4444-5555dddd6666",
    "custom:rep_id": "rep-042",
    "custom:tenant": "demo",
    "custom:book": "acc-1,acc-2",
    "custom:full_visibility": "false",
    ...overrides,
  };
}

function accessTokenClaims(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    iss: ISSUER,
    client_id: CLIENT_ID,
    token_use: "access",
    exp: inOneHour(),
    sub: "11111111-2222-3333-4444-5555dddd6666",
    scope: "openid profile",
    ...overrides,
  };
}

/** A `Fetcher` that serves the real JWKS document and counts how often it is asked for it. */
class CountingJwksFetcher implements Fetcher {
  calls = 0;

  fetch(_uri: string): Promise<ArrayBuffer> {
    this.calls += 1;
    const document = JSON.stringify({
      keys: [jwkFor(signingKeys.publicKey, KEY_ID), jwkFor(foreignKeys.publicKey, OTHER_KEY_ID)],
    });
    return Promise.resolve(new TextEncoder().encode(document).buffer as ArrayBuffer);
  }
}

/** A verifier resolving keys through the library's real cache over a counted fetcher. */
function verifierWithCountedFetcher(): {
  deps: { verifier: TokenVerifier; config: JwtConfig };
  fetcher: CountingJwksFetcher;
} {
  const fetcher = new CountingJwksFetcher();
  const jwksCache = new SimpleJwksCache({ fetcher });
  return { deps: { verifier: createVerifier(config, jwksCache), config }, fetcher };
}

const config: JwtConfig = { issuer: ISSUER, jwksUri: JWKS_URI, allowedClientIds: [CLIENT_ID] };

/** A complete relay environment: two static values plus the *name* of the audience parameter. */
function completeEnv(): NodeJS.ProcessEnv {
  return {
    [ENV_ISSUER]: ISSUER,
    [ENV_JWKS_URL]: JWKS_URI,
    [ENV_AUDIENCE_PARAMETER]: AUDIENCE_PARAMETER,
  } as NodeJS.ProcessEnv;
}

/** A parameter reader that counts reads and records the names it was asked for. */
class CountingParameterReader implements ParameterReader {
  calls = 0;
  readonly names: string[] = [];

  constructor(private readonly value: string) {}

  read(name: string): Promise<string> {
    this.calls += 1;
    this.names.push(name);
    return Promise.resolve(this.value);
  }
}

/** A reader whose read stays pending until released, so a race can be observed rather than hoped at. */
class GatedParameterReader implements ParameterReader {
  calls = 0;
  private release_: () => void = () => undefined;
  private readonly gate = new Promise<void>((resolve) => {
    this.release_ = resolve;
  });

  constructor(private readonly value: string) {}

  async read(_name: string): Promise<string> {
    this.calls += 1;
    await this.gate;
    return this.value;
  }

  release(): void {
    this.release_();
  }
}
const deps = verifierWithCountedFetcher().deps;

afterEach(() => {
  resetSharedVerifier();
});

describe("a token the relay accepts", () => {
  it("verifies an ID token and returns its claims verbatim", async () => {
    const claims = idTokenClaims();
    const verified = await verifyBearerToken(`Bearer ${signToken(claims)}`, deps);

    expect(verified["sub"]).toBe(claims["sub"]);
    expect(verified["custom:rep_id"]).toBe("rep-042");
    expect(verified["custom:book"]).toBe("acc-1,acc-2");
    expect(verified["custom:tenant"]).toBe("demo");
  });

  it("verifies an access token, reading the client from client_id rather than aud", async () => {
    const verified = await verifyBearerToken(`Bearer ${signToken(accessTokenClaims())}`, deps);
    expect(verified["client_id"]).toBe(CLIENT_ID);
    expect(verified["aud"]).toBeUndefined();
  });

  it("derives no identity: it adds no rep_id, tenant, or scope field of its own", async () => {
    const verified = await verifyBearerToken(`Bearer ${signToken(idTokenClaims())}`, deps);
    expect(Object.keys(verified)).not.toContain("rep_id");
    expect(Object.keys(verified)).not.toContain("tenant");
    expect(Object.keys(verified)).not.toContain("book");
    expect(Object.keys(verified)).not.toContain("principal_type");
  });

  it("accepts a lowercase scheme and extra whitespace around the header", async () => {
    const token = signToken(idTokenClaims());
    await expect(verifyBearerToken(`  bearer   ${token}  `, deps)).resolves.toBeTruthy();
  });
});

describe("every rejection is a rejection, never a permissive default", () => {
  it("rejects an expired token", async () => {
    const token = signToken(idTokenClaims({ exp: anHourAgo() }));
    await expect(verifyBearerToken(`Bearer ${token}`, deps)).rejects.toBeInstanceOf(
      JwtVerificationError,
    );
  });

  it("rejects a token with no exp claim, which would otherwise never expire", async () => {
    const claims = idTokenClaims();
    delete claims["exp"];
    const token = signToken(claims);
    await expect(verifyBearerToken(`Bearer ${token}`, deps)).rejects.toThrow(/no exp claim/);
  });

  it("rejects a wrong audience", async () => {
    const token = signToken(idTokenClaims({ aud: OTHER_CLIENT_ID }));
    await expect(verifyBearerToken(`Bearer ${token}`, deps)).rejects.toThrow(
      /unrecognised client/,
    );
  });

  it("rejects a multi-valued aud, since no single client issued it", async () => {
    const token = signToken(idTokenClaims({ aud: [CLIENT_ID, OTHER_CLIENT_ID] }));
    await expect(verifyBearerToken(`Bearer ${token}`, deps)).rejects.toThrow(
      /unrecognised client/,
    );
  });

  it("rejects a wrong issuer", async () => {
    const token = signToken(idTokenClaims({ iss: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_other" }));
    await expect(verifyBearerToken(`Bearer ${token}`, deps)).rejects.toBeInstanceOf(
      JwtVerificationError,
    );
  });

  it.each([
    ["not a jwt at all", "not-a-jwt"],
    ["two segments only", "eyJhbGciOiJSUzI1NiJ9" + ".eyJzdWIiOiJhIn0"],
    ["a tampered payload", `${signToken(idTokenClaims()).split(".").slice(0, 2).join(".")}.AAAA`],
    ["an empty string", ""],
  ])("rejects a malformed token (%s)", async (_label, token) => {
    await expect(verifyBearerToken(`Bearer ${token}`, deps)).rejects.toBeInstanceOf(
      JwtVerificationError,
    );
  });

  it.each([
    ["absent", undefined],
    ["blank", ""],
    ["whitespace only", "   "],
    ["another scheme", "Basic YWJjOmRlZg=="],
    ["a bare token with no scheme", "eyJhbGciOiJSUzI1NiJ9" + ".e30.AAAA"], // gitleaks:allow
    ["a scheme with no token", "Bearer"],
    ["a scheme with an empty token", "Bearer    "],
    ["two tokens", "Bearer aaa bbb"],
  ])("rejects a bearer header value that is %s", async (_label, header) => {
    await expect(verifyBearerToken(header, deps)).rejects.toBeInstanceOf(JwtVerificationError);
  });

  it("rejects a token signed by a key that is not the one its kid names", async () => {
    const token = signToken(idTokenClaims(), { kid: KEY_ID, key: foreignKeys.privateKey });
    await expect(verifyBearerToken(`Bearer ${token}`, deps)).rejects.toBeInstanceOf(
      JwtVerificationError,
    );
  });

  it("rejects a token whose kid is in the JWKS but is not its signer", async () => {
    const token = signToken(idTokenClaims(), { kid: OTHER_KEY_ID, key: signingKeys.privateKey });
    await expect(verifyBearerToken(`Bearer ${token}`, deps)).rejects.toBeInstanceOf(
      JwtVerificationError,
    );
  });

  it("rejects a token whose kid resolves to no published key", async () => {
    const token = signToken(idTokenClaims(), { kid: "not-published" });
    await expect(verifyBearerToken(`Bearer ${token}`, deps)).rejects.toBeInstanceOf(
      JwtVerificationError,
    );
  });

  it('rejects alg "none", so an unsigned token cannot pass', async () => {
    const token = signToken(idTokenClaims(), { alg: "none" });
    await expect(verifyBearerToken(`Bearer ${token}`, deps)).rejects.toBeInstanceOf(
      JwtVerificationError,
    );
  });

  it.each([
    ["absent", undefined],
    ["empty", ""],
    ["an unknown value", "refresh"],
    ["not a string", 1],
  ])("rejects a token whose token_use is %s", async (_label, tokenUse) => {
    const claims = idTokenClaims();
    if (tokenUse === undefined) {
      delete claims["token_use"];
    } else {
      claims["token_use"] = tokenUse;
    }
    const token = signToken(claims);
    await expect(verifyBearerToken(`Bearer ${token}`, deps)).rejects.toThrow(
      /unrecognised token_use/,
    );
  });

  it("rejects an ID token presenting its client in client_id instead of aud", async () => {
    const claims = idTokenClaims({ client_id: CLIENT_ID });
    delete claims["aud"];
    const token = signToken(claims);
    await expect(verifyBearerToken(`Bearer ${token}`, deps)).rejects.toThrow(
      /unrecognised client/,
    );
  });

  it("rejects an access token presenting its client in aud instead of client_id", async () => {
    const claims = accessTokenClaims({ aud: CLIENT_ID });
    delete claims["client_id"];
    const token = signToken(claims);
    await expect(verifyBearerToken(`Bearer ${token}`, deps)).rejects.toThrow(
      /unrecognised client/,
    );
  });

  it("reports a rejection as JwtVerificationError while keeping the underlying cause", async () => {
    const token = signToken(idTokenClaims({ exp: anHourAgo() }));
    const error = await verifyBearerToken(`Bearer ${token}`, deps).catch((thrown: unknown) => thrown);
    expect(error).toBeInstanceOf(JwtVerificationError);
    expect((error as JwtVerificationError).cause).toBeDefined();
  });
});

describe("the JWKS is fetched once and cached", () => {
  it("serves a second verification from the cache", async () => {
    const { deps: scoped, fetcher } = verifierWithCountedFetcher();

    await verifyBearerToken(`Bearer ${signToken(idTokenClaims())}`, scoped);
    expect(fetcher.calls).toBe(1);

    await verifyBearerToken(`Bearer ${signToken(accessTokenClaims())}`, scoped);
    expect(fetcher.calls).toBe(1);
  });

  it("fetches once per verifier, so a fresh verifier does fetch", async () => {
    const first = verifierWithCountedFetcher();
    const second = verifierWithCountedFetcher();

    await verifyBearerToken(`Bearer ${signToken(idTokenClaims())}`, first.deps);
    await verifyBearerToken(`Bearer ${signToken(idTokenClaims())}`, second.deps);

    expect(first.fetcher.calls).toBe(1);
    expect(second.fetcher.calls).toBe(1);
  });

  it("reuses one verifier across invocations of the same execution environment", async () => {
    const env = completeEnv();
    const reader = new CountingParameterReader(CLIENT_ID);

    const first = await sharedContext(env, reader);
    const second = await sharedContext(env, reader);

    expect(first.verifier).toBe(second.verifier);
    resetSharedVerifier();
    const third = await sharedContext(env, reader);
    expect(third.verifier).not.toBe(first.verifier);
  });
});

describe("configuration fails closed", () => {
  it("reads all three variables", () => {
    const loaded = loadJwtEnvConfig(completeEnv());
    expect(loaded.issuer).toBe(ISSUER);
    expect(loaded.jwksUri).toBe(JWKS_URI);
    expect(loaded.audienceParameter).toBe(AUDIENCE_PARAMETER);
  });

  it.each([ENV_ISSUER, ENV_JWKS_URL, ENV_AUDIENCE_PARAMETER])(
    "throws when %s is absent",
    (name) => {
      const env = completeEnv();
      delete env[name];
      expect(() => loadJwtEnvConfig(env)).toThrow(JwtVerificationError);
    },
  );

  it.each([ENV_ISSUER, ENV_JWKS_URL, ENV_AUDIENCE_PARAMETER])("throws when %s is blank", (name) => {
    const env = completeEnv();
    env[name] = "   ";
    expect(() => loadJwtEnvConfig(env)).toThrow(JwtVerificationError);
  });

  it("carries no audience value of its own, so there is one source of truth", () => {
    const loaded = loadJwtEnvConfig(completeEnv()) as unknown as Record<string, unknown>;
    expect(loaded["allowedClientIds"]).toBeUndefined();
    expect(Object.keys(loaded).sort()).toEqual(["audienceParameter", "issuer", "jwksUri"]);
  });
});

describe("the audience arrives by SSM parameter reference, and never defaults", () => {
  const envConfig = () => loadJwtEnvConfig(completeEnv());

  it("reads the parameter named by the environment and accepts one client id", async () => {
    const reader = new CountingParameterReader(CLIENT_ID);
    await expect(resolveAudience(envConfig(), reader)).resolves.toEqual([CLIENT_ID]);
    expect(reader.names).toEqual([AUDIENCE_PARAMETER]);
  });

  it("accepts a comma-separated parameter value, so a second client needs no second source", async () => {
    const reader = new CountingParameterReader(` ${CLIENT_ID} , ${OTHER_CLIENT_ID} `);
    await expect(resolveAudience(envConfig(), reader)).resolves.toEqual([
      CLIENT_ID,
      OTHER_CLIENT_ID,
    ]);
  });

  it.each([
    ["empty", ""],
    ["whitespace only", "   "],
    ["separators only", " , ,, "],
  ])("throws when the parameter value is %s, rather than verifying against nothing", async (
    _label,
    value,
  ) => {
    const reader = new CountingParameterReader(value);
    await expect(resolveAudience(envConfig(), reader)).rejects.toThrow(/names no client id/);
  });

  it("throws when the parameter cannot be read at all", async () => {
    const reader: ParameterReader = {
      read: () => Promise.reject(new Error("AccessDeniedException")),
    };
    await expect(resolveAudience(envConfig(), reader)).rejects.toThrow(
      /cannot read the audience parameter/,
    );
  });

  it("keeps the underlying cause of a failed read", async () => {
    const cause = new Error("ParameterNotFound");
    const reader: ParameterReader = { read: () => Promise.reject(cause) };
    const error = await resolveAudience(envConfig(), reader).catch((thrown: unknown) => thrown);
    expect((error as JwtVerificationError).cause).toBe(cause);
  });
});

describe("the cold-start audience read cannot be raced or skipped", () => {
  it("reads the parameter once for concurrent resolutions", async () => {
    const reader = new CountingParameterReader(CLIENT_ID);

    const resolved = await Promise.all([
      resolveJwtConfig(completeEnv(), reader),
      resolveJwtConfig(completeEnv(), reader),
      resolveJwtConfig(completeEnv(), reader),
    ]);

    expect(reader.calls).toBe(1);
    for (const config of resolved) {
      expect(config.allowedClientIds).toEqual([CLIENT_ID]);
    }
  });

  it("no concurrent verification proceeds before the audience is known", async () => {
    const reader = new GatedParameterReader(CLIENT_ID);
    const verifier = createVerifier(config, new SimpleJwksCache({ fetcher: new CountingJwksFetcher() }));
    let resolvedCount = 0;

    const attempts = [0, 1, 2].map(async () => {
      const context = await sharedContext(completeEnv(), reader);
      resolvedCount += 1;
      return verifyBearerToken(`Bearer ${signToken(idTokenClaims())}`, {
        verifier,
        config: context.config,
      });
    });

    // The parameter read is still pending, so nothing has verified even after several microtask ticks.
    for (let tick = 0; tick < 10; tick += 1) {
      await Promise.resolve();
    }
    expect(resolvedCount).toBe(0);

    reader.release();
    const claims = await Promise.all(attempts);

    expect(reader.calls).toBe(1);
    expect(resolvedCount).toBe(3);
    for (const verified of claims) {
      expect(verified["custom:rep_id"]).toBe("rep-042");
    }
  });

  it("a failed read is retried on the next attempt rather than poisoning the container", async () => {
    let attempts = 0;
    const reader: ParameterReader = {
      read: () => {
        attempts += 1;
        return attempts === 1 ? Promise.reject(new Error("throttled")) : Promise.resolve(CLIENT_ID);
      },
    };

    await expect(resolveJwtConfig(completeEnv(), reader)).rejects.toThrow(JwtVerificationError);
    await expect(resolveJwtConfig(completeEnv(), reader)).resolves.toMatchObject({
      allowedClientIds: [CLIENT_ID],
    });
    expect(attempts).toBe(2);
  });

  it("a failed read never yields a usable context, so verification cannot proceed", async () => {
    const reader: ParameterReader = { read: () => Promise.reject(new Error("AccessDenied")) };
    const verifier = createVerifier(
      config,
      new SimpleJwksCache({ fetcher: new CountingJwksFetcher() }),
    );

    await expect(sharedContext(completeEnv(), reader)).rejects.toThrow(JwtVerificationError);

    // No context, and therefore no audience, is ever produced.
    const context = await sharedContext(completeEnv(), reader).catch(() => undefined);
    expect(context).toBeUndefined();
    await expect(
      verifyBearerToken(`Bearer ${signToken(idTokenClaims())}`, {
        verifier,
        config: { ...config, allowedClientIds: [] },
      }),
    ).rejects.toThrow(/unrecognised client/);
  });
});

describe("the claim assertions are directly falsifiable", () => {
  it("accepts claims that satisfy every rule", () => {
    expect(() => assertIssuedToAllowedClient(idTokenClaims(), config)).not.toThrow();
  });

  it("rejects a client that is merely a prefix of an allowed one", () => {
    const claims = idTokenClaims({ aud: CLIENT_ID.slice(0, 5) });
    expect(() => assertIssuedToAllowedClient(claims, config)).toThrow(/unrecognised client/);
  });

  it("rejects an empty issuer", () => {
    expect(() => assertIssuedToAllowedClient(idTokenClaims({ iss: "" }), config)).toThrow(
      /no iss claim/,
    );
  });

  it("rejects a non-numeric exp", () => {
    expect(() => assertIssuedToAllowedClient(idTokenClaims({ exp: "later" }), config)).toThrow(
      /no exp claim/,
    );
  });

  it("throws on an empty allowlist rather than accepting anything", () => {
    const permissive: JwtConfig = { ...config, allowedClientIds: [] };
    expect(() => assertIssuedToAllowedClient(idTokenClaims(), permissive)).toThrow(
      /unrecognised client/,
    );
  });
});

describe("extractBearerToken", () => {
  it("returns the token for a well-formed header", () => {
    expect(extractBearerToken("Bearer abc.def.ghi")).toBe("abc.def.ghi");
  });

  it("accepts a tab between the scheme and the token", () => {
    expect(extractBearerToken("Bearer\tabc.def.ghi")).toBe("abc.def.ghi");
  });
});

describe("the header the token travels in", () => {
  it("is `x-crm-authorization`, and is not `authorization`", () => {
    // The SPA's fetch carries the same literal.
    expect(JWT_HEADER).toBe("x-crm-authorization");
    expect(JWT_HEADER).not.toBe("authorization");
  });

  it("is named in the message for an absent token, so an operator sees which header was missing", () => {
    expect(() => extractBearerToken(undefined)).toThrow(JWT_HEADER);
  });
});
