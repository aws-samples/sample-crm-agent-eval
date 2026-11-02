// SPDX-License-Identifier: MIT
/**
 * The error envelope is one contract, declared on the sides **enumerated** below — never counted.
 *
 * | Side | Covered by |
 * |---|---|
 * | `src/crm/errors.py` — the authority | this file |
 * | `api/CrmApiClient.ts` — normaliser + its wiring | this file |
 * | `mocks/devApi.ts` — the dev stub | `mocks/mocks.test.ts` |
 * | `components/tuning/capabilityMatrix.ts` — second client | `components/tuning/capabilityMatrix.test.ts`, and its **field-set wiring** in this file |
 * | `pages/evals/EvalRunClient.ts` — third client | this file (`EvalRunClient.request` block below) |
 *
 * The final block drives every wire field derived from `errors.py` through all three clients' real
 * error paths.
 *
 * `src/crm/errors.py` is authoritative: field names are parsed from its source, and the client
 * reads exactly those. The dev stub's side (`mocks/devApi.ts` emits the same names) is asserted in
 * `mocks/mocks.test.ts`, which also reads `errors.py` directly.
 *
 * `the enumeration above is complete` walks non-test modules under `src/` and greps each for the
 * markers of envelope consumption — constructing a `CrmApiError`, calling `normaliseErrorBody`, or
 * casting a `res.json()` body to `ApiErrorBody` — and requires the result to equal the table.
 */
import { readFileSync, readdirSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CrmApiClient,
  CrmApiError,
  errorKindForStatus,
  normaliseErrorBody,
  type ApiErrorBody,
} from "./CrmApiClient";
import { HttpEvalRunClient } from "../pages/evals/EvalRunClient";
import { fetchCapabilityMatrix } from "../components/tuning/capabilityMatrix";

const errorsPy = readFileSync(
  resolve(process.cwd(), "..", "src", "crm", "errors.py"),
  "utf8",
);

describe("the Python source is the authority for the field names", () => {
  it("found the source, so the assertions below are not comparing two absences", () => {
    expect(errorsPy).toContain("class ScopeDenied");
    expect(errorsPy).toContain("def to_api_response");
  });

  it("nests message under `error`, which is why a flat `body.message` read undefined", () => {
    // `{"statusCode": ..., "error": {"code": ..., "message": ...}}`
    expect(errorsPy).toMatch(/"error":\s*\{"code":\s*self\.code,\s*"message":\s*self\.message\}/);
  });

  it("spells the denial field `missing_scope`, under `error`", () => {
    expect(errorsPy).toMatch(/payload\["error"\]\["missing_scope"\]\s*=\s*self\.missing_scope/);
    // The camelCase spelling `missingScope` appears nowhere in the authority.
    expect(errorsPy).not.toContain("missingScope");
  });
});

describe("the normaliser reads the shape Python actually emits", () => {
  /** The exact bytes `_proxy_response` produces for a `ScopeDenied`, parsed as the client would. */
  const wire = JSON.parse(
    '{"statusCode": 403, "error": {"code": "scope_denied", "message": "That account is outside your book.", "missing_scope": "account:acct-9"}}',
  ) as unknown;

  it("recovers the message, the field with the widest blast radius", () => {
    expect(normaliseErrorBody(wire, "fallback").message).toBe("That account is outside your book.");
  });

  it("recovers the missing scope as camelCase for the six consumers that render it", () => {
    expect(normaliseErrorBody(wire, "fallback").missingScope).toBe("account:acct-9");
  });

  it("recovers the taxonomy code, so anything branching on it is not reading undefined", () => {
    expect(normaliseErrorBody(wire, "fallback").code).toBe("scope_denied");
  });

  it("surfaces the message through CrmApiError.message, not just the body", () => {
    const error = new CrmApiError("scope_denied", 403, normaliseErrorBody(wire, "fallback"));
    expect(error.message).toBe("That account is outside your book.");
  });
});

/**
 * The wire field set, derived from `errors.py`.
 */
/** Base fields, from `CrmError.to_api_response`'s literal. */
function baseFields(source: string): string[] {
  const match = /"error":\s*\{([^}]*)\}/.exec(source);
  return match === null ? [] : [...match[1].matchAll(/"(\w+)":/g)].map((m) => m[1]);
}

/** Subclass additions, from every `payload["error"]["<name>"] = ...` assignment. */
function subclassFields(source: string): string[] {
  return [...source.matchAll(/payload\["error"\]\["(\w+)"\]\s*=/g)].map((m) => m[1]);
}

/** The `payload["error"]["<name>"] = ...` assignments of each class that makes any, in source order. */
function subclassFieldsByClass(source: string): Record<string, string[]> {
  const byClass: Record<string, string[]> = {};
  for (const block of source.split(/^(?=class \w+)/m)) {
    const name = block.match(/^class (\w+)/)?.[1];
    const fields = subclassFields(block);
    if (name !== undefined && fields.length > 0) byClass[name] = fields;
  }
  return byClass;
}

const wireFields = [...new Set([...baseFields(errorsPy), ...subclassFields(errorsPy)])].sort();

/**
 * How each wire name is expected to surface on the normalised body. snake_case -> camelCase.
 */
const EXPECTED_SURFACE: Readonly<Record<string, string>> = {
  code: "code",
  conflicting_fields: "conflictingFields",
  message: "message",
  missing_scope: "missingScope",
  next_step: "nextStep",
  reason: "reason",
  source: "source",
};

/** A `fields_changed` row as `crm.errors.Conflict.to_api_response` renders it. */
const CONFLICTING_FIELD = {
  field: "stage",
  snapshot: "Discovery",
  current: "Closed Lost",
  proposed: "Negotiation",
};

describe("the wire field set is derived from the authority, not hand-listed", () => {
  it("parsed a plausible field set, so the assertions below are not built on an empty match", () => {
    // Vacuity guard: names two base fields it must have found, not just a count.
    expect(wireFields).toContain("message");
    expect(wireFields).toContain("code");
    expect(wireFields.length).toBeGreaterThanOrEqual(3);
  });

  it("found the subclass additions, not just the base literal", () => {
    // `next_step` is added by `CrmError` for every member; the rest by the named subclass.
    expect(subclassFieldsByClass(errorsPy)).toEqual({
      CrmError: ["next_step"],
      ScopeDenied: ["missing_scope"],
      ValidationFailed: ["source"],
      Conflict: ["reason", "conflicting_fields"],
      ContactDeparted: ["missing_scope"],
    });
  });

  it("normalises a contact_departed 409's missing_scope like any other", () => {
    const body = normaliseErrorBody(
      {
        statusCode: 409,
        error: {
          code: "contact_departed",
          message: "That contact has left the company.",
          missing_scope: "status:active",
          next_step: "tell_user",
        },
      },
      "fallback",
    );
    expect(body.code).toBe("contact_departed");
    expect(body.missingScope).toBe("status:active");
    expect(body.nextStep).toBe("tell_user");
  });

  /* Iterates the derived set. */
  it.each(wireFields)(
    "surfaces the derived field %s through the normaliser",
    (wireName) => {
      const surfaced = EXPECTED_SURFACE[wireName];
      const body = normaliseErrorBody(
        {
          error: {
            code: "validation_failed",
            message: "m",
            missing_scope: "s",
            source: "amount",
            reason: "fields_changed",
            conflicting_fields: [CONFLICTING_FIELD],
            next_step: "fix_arguments",
          },
        },
        "fallback",
      );
      expect(body[surfaced]).toBeDefined();
    },
  );

  it("has a surface mapping for every field the authority emits", () => {
    // A new `payload["error"]["x"]` in `errors.py` fails this until the normaliser carries it.
    const unmapped = wireFields.filter((name) => EXPECTED_SURFACE[name] === undefined);
    expect(unmapped, `errors.py emits fields the client has no mapping for: ${unmapped.join(", ")}`).toEqual([]);
  });

  it("surfaces every derived field on a real envelope, none dropped", () => {
    const body = normaliseErrorBody(
      {
        statusCode: 400,
        error: {
          code: "validation_failed",
          message: "Amount must be positive.",
          source: "amount",
          next_step: "fix_arguments",
        },
      },
      "fallback",
    );
    expect(body.code).toBe("validation_failed");
    expect(body.message).toBe("Amount must be positive.");
    expect(body.source).toBe("amount");
    expect(body.nextStep).toBe("fix_arguments");
  });

  it("surfaces a conflict's reason and conflicting fields on a real 409 envelope", () => {
    const body = normaliseErrorBody(
      {
        statusCode: 409,
        error: {
          code: "conflict",
          message: "The record changed since this proposal was raised.",
          reason: "fields_changed",
          conflicting_fields: [CONFLICTING_FIELD],
        },
      },
      "fallback",
    );
    expect(body.reason).toBe("fields_changed");
    expect(body.conflictingFields).toEqual([CONFLICTING_FIELD]);
  });

  it("omits reason and conflictingFields rather than inventing them when absent", () => {
    const body = normaliseErrorBody({ error: { code: "conflict", message: "decided" } }, "fallback");
    expect("reason" in body).toBe(false);
    expect("conflictingFields" in body).toBe(false);
  });

  it("drops malformed conflicting-field rows and ignores a non-array or non-string value", () => {
    const body = normaliseErrorBody(
      {
        error: {
          code: "conflict",
          message: "m",
          reason: 7,
          conflicting_fields: [CONFLICTING_FIELD, null, "stage", { snapshot: 1 }, { field: 3 }],
        },
      },
      "fallback",
    );
    expect("reason" in body).toBe(false);
    expect(body.conflictingFields).toEqual([CONFLICTING_FIELD]);
    expect(
      "conflictingFields" in
        normaliseErrorBody({ error: { message: "m", conflicting_fields: { field: "x" } } }, "f"),
    ).toBe(false);
  });

  it("omits source rather than inventing it when the handler sent none", () => {
    // `ValidationFailed` only writes `source` when one is known, so absent must stay absent.
    const body = normaliseErrorBody(
      { error: { code: "validation_failed", message: "bad" } },
      "fallback",
    );
    expect("source" in body).toBe(false);
  });
});

describe("the normaliser is total, because throwing inside the error path is worse than a vague message", () => {
  it.each([
    ["an HTML error page from the edge", "<html><body>403 Forbidden</body></html>"],
    ["a bare string", "nope"],
    ["null", null],
    ["a number", 502],
    ["an empty object", {}],
    ["an envelope with no error member", { statusCode: 500 }],
    ["an error member that is not an object", { error: "denied" }],
    ["an error member that is null", { error: null }],
    ["a non-string message", { error: { message: 42 } }],
    ["an empty-string message", { error: { message: "" } }],
  ])("falls back rather than throwing for %s", (_label, raw) => {
    const body = normaliseErrorBody(raw, "fallback message");
    expect(body.message).toBe("fallback message");
    expect(body.missingScope).toBeUndefined();
  });

  it("omits missingScope and code rather than inventing them when absent", () => {
    // A 404 carries no `missing_scope`; the key must be absent, not `undefined`-valued or `""`.
    const body = normaliseErrorBody(
      { statusCode: 404, error: { code: "not_found", message: "gone" } },
      "fallback",
    );
    expect(body.missingScope).toBeUndefined();
    expect("missingScope" in body).toBe(false);
    expect(body.code).toBe("not_found");
  });

  it("ignores a non-string missing_scope rather than rendering an object", () => {
    const body = normaliseErrorBody(
      { error: { message: "denied", missing_scope: { account: "a" } } },
      "fallback",
    );
    expect(body.missingScope).toBeUndefined();
  });
});

/**
 * The wiring: drives `CrmApiClient`'s real error path through a stubbed `fetch`.
 */
describe("CrmApiClient.post applies the normaliser to a real error response", () => {
  const envelope = {
    statusCode: 403,
    error: {
      code: "scope_denied",
      message: "That account is outside your book.",
      missing_scope: "account:acct-9",
    },
  };

  function clientAgainst(status: number, json: () => Promise<unknown>): CrmApiClient {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status, json } as unknown as Response),
    );
    return new CrmApiClient("/api", { getAccessToken: () => Promise.resolve("tok") });
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("throws a CrmApiError carrying the normalised message, scope and code", async () => {
    const client = clientAgainst(403, () => Promise.resolve(envelope));
    const caught = await client.getAccount({ account_id: "acct-9" }).catch((err: unknown) => err);

    expect(caught).toBeInstanceOf(CrmApiError);
    const error = caught as CrmApiError;
    expect(error.kind).toBe("scope_denied");
    expect(error.status).toBe(403);
    expect(error.body.message).toBe("That account is outside your book.");
    expect(error.body.missingScope).toBe("account:acct-9");
    expect(error.body.code).toBe("scope_denied");
    expect(error.message).toBe("That account is outside your book.");
  });

  it("does not throw out of the error path when the body is not JSON at all", async () => {
    // An HTML error page from the edge.
    const client = clientAgainst(502, () => Promise.reject(new SyntaxError("Unexpected token <")));
    const caught = await client.getAccount({ account_id: "acct-9" }).catch((err: unknown) => err);

    expect(caught).toBeInstanceOf(CrmApiError);
    expect((caught as CrmApiError).body.message).toContain("failed with status 502");
    expect((caught as CrmApiError).body.missingScope).toBeUndefined();
  });

  it("falls back rather than throwing when the body is JSON but not the envelope", async () => {
    const client = clientAgainst(500, () => Promise.resolve({ oops: true }));
    const caught = await client.getAccount({ account_id: "acct-9" }).catch((err: unknown) => err);

    expect(caught).toBeInstanceOf(CrmApiError);
    expect((caught as CrmApiError).body.message).toContain("failed with status 500");
  });
});

/**
 * The status→kind map shared by `CrmApiClient` and `pages/evals/EvalRunClient.ts`.
 */
describe("errorKindForStatus is the one status-to-kind map", () => {
  it.each([
    [400, "validation_failed"],
    [401, "authentication_failed"],
    [403, "scope_denied"],
    [404, "not_found"],
    [409, "conflict"],
  ] as const)("maps %i to %s", (status, kind) => {
    expect(errorKindForStatus(status)).toBe(kind);
  });

  it.each([418, 429, 500, 502, 503, 0])("maps the unmapped status %i to unknown", (status) => {
    // `unknown` is the catch-all for an unmapped status.
    expect(errorKindForStatus(status)).toBe("unknown");
  });

  it("agrees with the kind the client actually throws", async () => {
    // The client uses the map.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: () => Promise.resolve({ error: { code: "conflict", message: "already decided" } }),
      } as unknown as Response),
    );
    const client = new CrmApiClient("/api", { getAccessToken: () => Promise.resolve("tok") });
    const caught = (await client
      .getAccount({ account_id: "a" })
      .catch((err: unknown) => err)) as CrmApiError;

    expect(caught.kind).toBe(errorKindForStatus(409));
    expect(caught.kind).toBe("conflict");
    vi.unstubAllGlobals();
  });
});

/**
 * The enumeration in this file's header, enforced: a module that consumes the envelope without
 * being listed fails here.
 */
describe("the enumeration above is complete", () => {
  /** Non-test modules under `src/` permitted to consume the envelope, per the header table. */
  const DECLARED_CONSUMERS = [
    "src/api/CrmApiClient.ts",
    "src/components/tuning/capabilityMatrix.ts",
    "src/pages/evals/EvalRunClient.ts",
  ] as const;

  /**
   * One known file per directory the header table names, so a walk that loses a subtree fails.
   */
  const MUST_BE_REACHED = [
    "src/api/CrmApiClient.ts",
    "src/components/tuning/capabilityMatrix.ts",
    "src/pages/evals/EvalRunClient.ts",
    "src/mocks/devApi.ts",
    "src/hooks/useAgentStream.ts",
  ] as const;

  function sourceFiles(dir: string): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) return sourceFiles(path);
      if (!/\.tsx?$/.test(entry.name)) return [];
      if (/\.test\.tsx?$/.test(entry.name)) return [];
      return [path];
    });
  }

  const srcRoot = resolve(process.cwd(), "src");
  const walked = sourceFiles(srcRoot).map((path) =>
    relative(process.cwd(), path).split(sep).join("/"),
  );

  /**
   * Markers of envelope consumption in a module's source.
   */
  const CONSUMPTION_PATTERNS: readonly RegExp[] = [
    // Constructing a `CrmApiError` turns a wire error into a taxonomy member.
    /new CrmApiError\(/,
    // The helper, or an unchecked cast. Either alone is enough.
    /normaliseErrorBody/,
    /res\.json\(\)\s*\)?\s*as\s+ApiErrorBody/,
  ];

  const consumers = walked
    .filter((path) => {
      const source = readFileSync(resolve(process.cwd(), path), "utf8");
      return CONSUMPTION_PATTERNS.some((pattern) => pattern.test(source));
    })
    .sort();

  it("reached every directory the header table names, so the walk lost no subtree", () => {
    for (const required of MUST_BE_REACHED) {
      expect(walked, `walk never reached ${required}`).toContain(required);
    }
  });

  it("detects consumption by what a module does, not by whether it names the helper", () => {
    // The detector's own positive and negative controls.
    const detects = (source: string) => CONSUMPTION_PATTERNS.some((p) => p.test(source));

    expect(detects("body = (await res.json()) as ApiErrorBody;")).toBe(true);
    expect(detects("throw new CrmApiError(kindFor(res.status), res.status, body);")).toBe(true);
    expect(detects("body = normaliseErrorBody(await res.json(), fallback);")).toBe(true);

    // Reading `.message` off a caught JS Error is not envelope consumption.
    expect(detects("const text = error instanceof Error ? error.message : String(error);")).toBe(false);
    expect(detects("const x = y as SomeOtherType;")).toBe(false);
  });

  it("lists exactly the modules that consume the envelope", () => {
    expect(consumers).toEqual([...DECLARED_CONSUMERS].sort());
  });

  it("names every declared consumer in the header table", () => {
    const header = readFileSync(resolve(process.cwd(), "src/api/errorEnvelope.contract.test.ts"), "utf8");
    for (const consumer of DECLARED_CONSUMERS) {
      expect(header).toContain(consumer.replace("src/", ""));
    }
  });

  it("names EvalRunClient, which converged onto the shared helpers", () => {
    const evalClient = readFileSync(
      resolve(process.cwd(), "src/pages/evals/EvalRunClient.ts"),
      "utf8",
    );
    expect(evalClient).toContain("normaliseErrorBody(await res.json()");
    /* The taxonomy map is declared once. */
    expect(evalClient).toContain("errorKindForStatus");
    expect(evalClient).not.toContain("function errorKindForStatus");
  });
});

/**
 * The third client's wiring: drives `EvalRunClient.request`'s real error path.
 */
describe("EvalRunClient.request applies the normaliser too", () => {
  const envelope = {
    statusCode: 403,
    error: {
      code: "scope_denied",
      message: "That sweep is not yours.",
      missing_scope: "eval:run-1",
    },
  };

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("surfaces the nested message and scope through CrmApiError, not undefined", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 403,
        json: () => Promise.resolve(envelope),
      } as unknown as Response),
    );
    const client = HttpEvalRunClient.fromAuth("/api", {
      getAccessToken: () => Promise.resolve("tok"),
    } as unknown as Parameters<typeof HttpEvalRunClient.fromAuth>[1]);

    const caught = (await client.startRun().catch((err: unknown) => err)) as CrmApiError;

    expect(caught).toBeInstanceOf(CrmApiError);
    expect(caught.body.message).toBe("That sweep is not yours.");
    expect(caught.body.missingScope).toBe("eval:run-1");
    expect(caught.body.code).toBe("scope_denied");
    expect(caught.message).toBe("That sweep is not yours.");
  });

  it("does not throw out of its error path on a non-envelope body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 502,
        json: () => Promise.reject(new SyntaxError("Unexpected token <")),
      } as unknown as Response),
    );
    const client = HttpEvalRunClient.fromAuth("/api", {
      getAccessToken: () => Promise.resolve("tok"),
    } as unknown as Parameters<typeof HttpEvalRunClient.fromAuth>[1]);

    const caught = (await client.startRun().catch((err: unknown) => err)) as CrmApiError;

    expect(caught).toBeInstanceOf(CrmApiError);
    // Its own fallback wording is kept.
    expect(caught.body.message).toContain("eval request");
    expect(caught.body.message).toContain("502");
  });
});

/**
 * Every field derived from `errors.py` survives every client's real error path, not just the
 * helper: one row per field per declared consumer.
 */
describe("every client's wiring carries every derived wire field", () => {
  /**
   * One envelope carrying every field the authority can emit, exactly as `_proxy_response` renders it.
   *
   * `validation_failed` is the one taxonomy member that emits `source`; the envelope also carries
   * `missing_scope`, `reason` and `conflicting_fields` so a single payload exercises every derived
   * field.
   */
  const envelope = {
    statusCode: 400,
    error: {
      code: "validation_failed",
      message: "Amount must be a positive number.",
      missing_scope: "opportunity:write",
      source: "amount",
      reason: "fields_changed",
      conflicting_fields: [CONFLICTING_FIELD],
      next_step: "fix_arguments",
    },
  };

  /** What each **wire** field must equal once surfaced. Keyed by wire name, so the derived set indexes it. */
  const EXPECTED_VALUE: Readonly<Record<string, unknown>> = {
    code: "validation_failed",
    message: "Amount must be a positive number.",
    missing_scope: "opportunity:write",
    source: "amount",
    reason: "fields_changed",
    conflicting_fields: [CONFLICTING_FIELD],
    next_step: "fix_arguments",
  };

  const tokenSource = { getAccessToken: () => Promise.resolve("tok") };

  /**
   * The three declared consumers, each driven through its own real error path.
   *
   * Every entry goes through the module's public surface — `getAccount`, `startRun`,
   * `fetchCapabilityMatrix`.
   */
  const CLIENTS: readonly { readonly module: string; readonly reject: () => Promise<unknown> }[] = [
    {
      module: "api/CrmApiClient.ts",
      reject: () =>
        new CrmApiClient("/api", tokenSource).getAccount({ account_id: "acct-9" }),
    },
    {
      module: "pages/evals/EvalRunClient.ts",
      reject: () =>
        HttpEvalRunClient.fromAuth("/api", tokenSource as unknown as Parameters<
          typeof HttpEvalRunClient.fromAuth
        >[1]).startRun(),
    },
    {
      module: "components/tuning/capabilityMatrix.ts",
      reject: () => fetchCapabilityMatrix("/api", tokenSource),
    },
  ];

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function bodyFrom(reject: () => Promise<unknown>): Promise<ApiErrorBody> {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 400,
        json: () => Promise.resolve(envelope),
      } as unknown as Response),
    );
    const caught = await reject().then(
      () => {
        throw new Error("the client resolved on a 400 instead of rejecting");
      },
      (err: unknown) => err,
    );
    expect(caught).toBeInstanceOf(CrmApiError);
    return (caught as CrmApiError).body;
  }

  it("has an expected value for every derived field, so no row below is vacuous", () => {
    // Vacuity guard: every derived field needs an entry in `EXPECTED_VALUE`.
    const unvalued = wireFields.filter((name) => EXPECTED_VALUE[name] === undefined);
    expect(
      unvalued,
      `errors.py emits fields this wiring check has no expected value for: ${unvalued.join(", ")}`,
    ).toEqual([]);
    expect(wireFields).toContain("source");
  });

  describe.each(CLIENTS)("$module", ({ reject }) => {
    it.each(wireFields)("surfaces the derived field %s through its own error path", async (wireName) => {
      const body = await bodyFrom(reject);
      expect(body[EXPECTED_SURFACE[wireName]]).toEqual(EXPECTED_VALUE[wireName]);
    });
  });
});
