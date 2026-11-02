// SPDX-License-Identifier: MIT
/**
 * Proves the dev API stub is dev-only, and that it stubs the backend rather than the authorization.
 *
 * The dev-only claim is asserted three ways: the plugin's `apply`, the absence of any `src/mocks/`
 * import from application source, and the absence of `mocks` from a real production build.
 */

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { normaliseErrorBody } from "../api/CrmApiClient";
import { errorEnvelope, handleDevApiRequest } from "./devApi";
import { DEV_BOOKS, DEV_OTHER_REP_ID, DEV_REP_ID, SEARCH_PAGE_SIZE } from "./devFixtures";
import { DEV_REPS, mintDevToken } from "./devToken";
import { devApiStubPlugin } from "./devServerPlugin";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const srcRoot = join(frontendRoot, "src");

function sourceFiles(dir: string): string[] {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.(ts|tsx)$/.test(entry) ? [path] : [];
  });
}

const bearer = (rep: keyof typeof DEV_REPS) => `Bearer ${mintDevToken(DEV_REPS[rep])}`;

describe("the stub is dev-only, three independent ways", () => {
  it("is a serve-only Vite plugin", () => {
    expect(devApiStubPlugin().apply).toBe("serve");
  });

  it("is imported by nothing under src/pages or src/components", () => {
    const offenders: string[] = [];
    for (const dir of [join(srcRoot, "pages"), join(srcRoot, "components")]) {
      for (const file of sourceFiles(dir)) {
        if (/from\s+["'][^"']*mocks\//.test(readFileSync(file, "utf8"))) offenders.push(file);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("is imported by no application source at all, only by vite.config and its own tests", () => {
    const allowed = new Set(["vite.config.ts"]);
    const offenders: string[] = [];
    for (const file of sourceFiles(srcRoot)) {
      if (file.includes(`${join("src", "mocks")}`)) continue; // the stub may import itself
      if (/from\s+["'][^"']*mocks\//.test(readFileSync(file, "utf8"))) offenders.push(file);
    }
    for (const file of ["vite.config.ts", "vitest.config.ts", "tailwind.config.ts"]) {
      const path = join(frontendRoot, file);
      if (!existsSync(path)) continue;
      if (/from\s+["'][^"']*mocks\//.test(readFileSync(path, "utf8")) && !allowed.has(file)) {
        offenders.push(path);
      }
    }
    expect(offenders).toEqual([]);
  });

  /**
   * Builds for real and greps the output for the stub.
   */
  it("is absent from a production build", () => {
    /*
     * Invokes `vite build` directly into a private `--outDir` nested inside the ignored `dist/`,
     * rather than `npm run build`, so it neither type-checks the whole tree, regenerates
     * `src/types/generated/`, nor shares output with a concurrent run.
     */
    const outDir = join("dist", "stub-isolation-check");
    execFileSync("npx", ["vite", "build", "--outDir", outDir, "--emptyOutDir"], {
      cwd: frontendRoot,
      stdio: "pipe",
    });

    // The shipping build script ends in `vite build`, the bundler this test exercises.
    const packageJson = JSON.parse(
      readFileSync(join(frontendRoot, "package.json"), "utf8"),
    ) as { scripts: Record<string, string> };
    expect(packageJson.scripts.build).toContain("vite build");

    const distRoot = join(frontendRoot, outDir);
    const distIndex = readFileSync(join(distRoot, "index.html"), "utf8");
    expect(distIndex).not.toContain("devBootstrap");
    expect(distIndex).not.toContain("mocks");

    const assets = join(distRoot, "assets");
    const bundles = readdirSync(assets).filter((f) => f.endsWith(".js"));
    expect(bundles.length).toBeGreaterThan(0);
    for (const bundle of bundles) {
      const contents = readFileSync(join(assets, bundle), "utf8");
      // Fixture identifiers unique to the stub; none may appear in shipped JavaScript.
      expect(contents).not.toContain("acc-tailspin");
      expect(contents).not.toContain("dev-not-a-signature");
      expect(contents).not.toContain("Quiet Harbour Freight");
    }
  }, 120_000);
});

describe("it stubs the backend, not the authorization", () => {
  it("rejects a request with no bearer token", () => {
    expect(handleDevApiRequest("search_opportunities", {}, undefined).status).toBe(401);
  });

  it.each([
    ["a malformed header", "Token abc"],
    ["a non-JWT bearer", "Bearer not-a-jwt"],
    ["an empty bearer", "Bearer "],
  ])("rejects %s", (_name, header) => {
    expect(handleDevApiRequest("search_opportunities", {}, header).status).toBe(401);
  });

  it("rejects an expired token", () => {
    const expired = `Bearer ${mintDevToken(DEV_REPS.scoped, -60)}`;
    expect(handleDevApiRequest("search_opportunities", {}, expired).status).toBe(401);
  });

  it("rejects a token whose claims omit the rep id", () => {
    const header = btoa(JSON.stringify({ alg: "none" }));
    const payload = btoa(JSON.stringify({ sub: "x", exp: 4_000_000_000 }));
    expect(
      handleDevApiRequest("get_account", {}, `Bearer ${header}.${payload}.sig`).status,
    ).toBe(401);
  });

  /** The 403 is produced by the stub's own scope check, not by a flag the caller passes. */
  it("returns 403 scope_denied for an account outside the caller's book", () => {
    const result = handleDevApiRequest(
      "get_account",
      { account_id: "acc-tailspin" },
      bearer("scoped"),
    );
    expect(result.status).toBe(403);
    // The wire envelope, not the client's internal shape.
    expect(result.body).toMatchObject({
      error: { code: "scope_denied", missing_scope: "acc-tailspin" },
    });
    expect(DEV_BOOKS[DEV_REP_ID]).not.toContain("acc-tailspin");
  });

  /*
   * The stub half of the error-envelope contract. Its client half lives in
   * `api/errorEnvelope.contract.test.ts`; both read `src/crm/errors.py` directly.
   */
  it("emits the field names crm/errors.py emits, not the client's internal camelCase", () => {
    const errorsPy = readFileSync(
      resolve(frontendRoot, "..", "src", "crm", "errors.py"),
      "utf8",
    );
    // Positive control: a regex that matched nothing would make the assertions below vacuous.
    expect(errorsPy).toContain("class ScopeDenied");
    expect(errorsPy).toMatch(/payload\["error"\]\["missing_scope"\]/);
    expect(errorsPy).not.toContain("missingScope");

    const denial = handleDevApiRequest(
      "get_account",
      { account_id: "acc-tailspin" },
      bearer("scoped"),
    );
    expect(denial.status).toBe(403);
    expect(denial.body).toMatchObject({
      statusCode: 403,
      error: { code: "scope_denied", missing_scope: "acc-tailspin" },
    });
    expect(JSON.stringify(denial.body)).not.toContain("missingScope");
  });

  it("round-trips through the client normaliser into the fields a component renders", () => {
    // The end-to-end property both halves exist for: what the stub emits is what a component reads.
    const denial = handleDevApiRequest(
      "get_account",
      { account_id: "acc-tailspin" },
      bearer("scoped"),
    );
    const body = normaliseErrorBody(denial.body, "fallback");
    expect(body.missingScope).toBe("acc-tailspin");
    expect(body.message).toContain("outside your assigned book");
    expect(body.code).toBe("scope_denied");
  });

  it("routes every error status through the same envelope, not only the denial", () => {
    const notFound = handleDevApiRequest("get_account", { account_id: "nope" }, bearer("scoped"));
    expect(notFound.body).toMatchObject({ error: { code: "not_found" } });
    const unauthenticated = handleDevApiRequest("get_account", {}, undefined);
    expect(unauthenticated.body).toMatchObject({ error: { code: "authentication_failed" } });
  });

  /*
   * Every error body `devServerPlugin.ts` writes is built by `errorEnvelope`. Asserted on the source,
   * because the branches are inside a Vite middleware.
   */
  it("emits no hand-rolled error payload anywhere in the dev stub", () => {
    const plugin = readFileSync(join(srcRoot, "mocks", "devServerPlugin.ts"), "utf8");
    const api = readFileSync(join(srcRoot, "mocks", "devApi.ts"), "utf8");

    // Positive control: the marker must exist, or the assertions below are vacuous.
    expect(plugin).toContain("errorEnvelope(");

    /*
     * Enumerates the response-writing sites and requires each body to trace to `errorEnvelope`.
     */
    const bodySites = [...plugin.matchAll(/response\.end\(\s*(?:JSON\.stringify\(\s*)?([A-Za-z_$][\w$]*)/g)]
      .map((m) => m[1]);

    // Positive control on the enumeration itself: a parser that matches nothing proves nothing.
    expect(bodySites.length).toBeGreaterThanOrEqual(5);

    // The sites that legitimately write a non-error body, each named with why it is not an envelope.
    const NON_ERROR_BODIES: Readonly<Record<string, string>> = {
      DEV_RUNTIME_CONFIG: "the runtime config document, not an error",
      json: "the 200 capability-matrix payload",
      result: "handleDevApiRequest's own result — envelope-routed inside devApi.ts",
    };

    for (const ident of bodySites) {
      if (ident in NON_ERROR_BODIES) continue;
      expect(
        plugin,
        `devServerPlugin.ts writes '${ident}' as a response body without binding it to errorEnvelope(). ` +
          `Either route it through errorEnvelope, or add it to NON_ERROR_BODIES with a reason.`,
      ).toMatch(new RegExp(`const ${ident} = errorEnvelope\\(`));
    }

    // devApi.ts has no server plumbing; the flat shape must simply not reappear in its factories.
    expect(api, "devApi.ts hand-rolls a flat error payload").not.toMatch(/error:\s*["'`]/);
  });

  it("survives the round trip: an envelope's 503 reason reaches the client unchanged", () => {
    const unavailable = errorEnvelope(503, "capability_matrix_unavailable", "No interpreter at /x.");
    const body = normaliseErrorBody(unavailable.body, "fallback");

    expect(unavailable.status).toBe(503);
    expect(body.code).toBe("capability_matrix_unavailable");
    expect(body.message).toBe("No interpreter at /x.");
    expect(body.message).not.toBe("fallback");
  });

  it("keeps the interpreter-missing reason in the plugin's own branch, not just in the helper", () => {
    /*
     * The capabilities branch passes its specific 503 reason into the envelope. Asserted on the
     * source, because the branch is inside a Vite middleware.
     */
    const plugin = readFileSync(join(srcRoot, "mocks", "devServerPlugin.ts"), "utf8");

    const branch = plugin.match(
      /errorEnvelope\(\s*503,\s*"capability_matrix_unavailable",\s*([^)]*)\)/,
    );
    expect(branch, "no 503 capability_matrix_unavailable envelope in devServerPlugin.ts").not.toBeNull();

    // Interpolated, so the path the dev is actually missing appears in the message they see.
    expect(branch?.[1], "the 503 reason is a constant; it must name the interpreter path").toMatch(
      /\$\{VENV_PYTHON\}/,
    );
  });

  it("returns 403 for an opportunity whose account is outside the book", () => {
    expect(
      handleDevApiRequest(
        "get_opportunity",
        { opportunity_id: "opp-tailspin-frame" },
        bearer("scoped"),
      ).status,
    ).toBe(403);
  });

  it("distinguishes 403 from 404 — a hidden record is not reported as missing", () => {
    expect(
      handleDevApiRequest("get_account", { account_id: "acc-tailspin" }, bearer("scoped")).status,
    ).toBe(403);
    expect(
      handleDevApiRequest("get_account", { account_id: "acc-nonexistent" }, bearer("scoped"))
        .status,
    ).toBe(404);
  });

  it("lets full visibility read what a scoped rep cannot", () => {
    const denied = handleDevApiRequest(
      "get_account",
      { account_id: "acc-tailspin" },
      bearer("scoped"),
    );
    const allowed = handleDevApiRequest(
      "get_account",
      { account_id: "acc-tailspin" },
      bearer("manager"),
    );
    expect(denied.status).toBe(403);
    expect(allowed.status).toBe(200);
  });

  it("scopes a search to the caller's book rather than filtering client-side", () => {
    const result = handleDevApiRequest("search_opportunities", {}, bearer("scoped")) as {
      body: { items: { account_id: string }[] };
    };
    const accounts = new Set(result.body.items.map((o) => o.account_id));
    expect(accounts).not.toContain("acc-tailspin");
    for (const account of accounts) {
      expect(DEV_BOOKS[DEV_REP_ID]).toContain(account);
    }
  });

  it("scopes the aggregate by the same predicate, so a rollup cannot leak a hidden record", () => {
    const total = (result: { body: { stage_funnel: Record<string, number> } }) =>
      Object.values(result.body.stage_funnel).reduce((sum, n) => sum + n, 0);
    const scoped = handleDevApiRequest("pipeline_aggregate", {}, bearer("scoped")) as {
      body: { stage_funnel: Record<string, number> };
    };
    const manager = handleDevApiRequest("pipeline_aggregate", {}, bearer("manager")) as {
      body: { stage_funnel: Record<string, number> };
    };
    expect(total(scoped)).toBeLessThan(total(manager));
  });
});

describe("every state a page must render distinctly has a real record behind it", () => {
  it("truncates a search because the data exceeds a page, not because a flag says so", () => {
    const result = handleDevApiRequest("search_opportunities", {}, bearer("scoped")) as {
      body: { items: unknown[]; truncated: boolean; examined_count: number; next_cursor: unknown };
    };
    expect(result.body.truncated).toBe(true);
    expect(result.body.items).toHaveLength(SEARCH_PAGE_SIZE);
    expect(result.body.examined_count).toBeGreaterThan(SEARCH_PAGE_SIZE);
    expect(result.body.next_cursor).not.toBeNull();
  });

  it("truncates an activity list for the high-touch opportunity", () => {
    const result = handleDevApiRequest(
      "list_activities",
      { opportunity_id: "opp-northwind-renewal" },
      bearer("scoped"),
    ) as { body: { activities: unknown[]; next_cursor: unknown } };
    expect(result.body.next_cursor).not.toBeNull();
    expect(result.body.activities.length).toBeGreaterThan(0);
  });

  it("has an account with no opportunities, so the empty state has a subject", () => {
    const result = handleDevApiRequest(
      "get_account",
      { account_id: "acc-quiet" },
      bearer("scoped"),
    ) as { status: number; body: { contacts: unknown[] } };
    expect(result.status).toBe(200);
    expect(result.body.contacts).toEqual([]);
  });

  it("has a departed contact", () => {
    const result = handleDevApiRequest(
      "get_contact",
      { contact_id: "con-marcus" },
      bearer("scoped"),
    ) as { body: { contact: { status: string } } };
    expect(result.body.contact.status).toBe("departed");
  });

  it("has four pending proposals, each with a real before-snapshot to diff against", () => {
    const result = handleDevApiRequest("list_proposals", {}, bearer("scoped")) as {
      body: { proposals: { status: string; before_snapshot: object; after_payload?: object }[] };
    };
    const pending = result.body.proposals.filter((p) => p.status === "pending");
    expect(pending).toHaveLength(4);
    for (const proposal of pending) {
      expect(Object.keys(proposal.before_snapshot).length).toBeGreaterThan(0);
      expect(Object.keys(proposal.after_payload ?? {}).length).toBeGreaterThan(0);
      expect(proposal.after_payload).not.toEqual(proposal.before_snapshot);
    }
  });

  it("refuses a second revert of an already-reversed record", () => {
    expect(handleDevApiRequest("revert_audit_record", { audit_id: "aud-3" }, bearer("scoped")))
      .toMatchObject({ status: 409 });
    expect(handleDevApiRequest("revert_audit_record", { audit_id: "aud-2" }, bearer("scoped")))
      .toMatchObject({ status: 200 });
  });

  it("offers no direct high-risk write path", () => {
    const result = handleDevApiRequest(
      "submit_proposal",
      { target_id: "opp-northwind-renewal" },
      bearer("scoped"),
    ) as { body: { proposal: { status: string } } };
    expect(result.body.proposal.status).toBe("pending");
    // There is no tool that commits a stage change directly.
    expect(
      handleDevApiRequest("high_risk_write", {}, bearer("scoped")).status,
    ).toBe(404);
  });

  it("names the fix when asked for a tool it does not implement", () => {
    const result = handleDevApiRequest("some_new_tool", {}, bearer("scoped"));
    expect(result.status).toBe(404);
    expect(JSON.stringify(result.body)).toContain("src/mocks/devApi.ts");
  });
});

describe("dev identities", () => {
  it("gives the scoped rep a strictly smaller book than the manager sees", () => {
    expect(DEV_REPS.scoped.fullVisibility).toBe(false);
    expect(DEV_REPS.manager.fullVisibility).toBe(true);
    expect(DEV_BOOKS[DEV_REP_ID]).not.toEqual(DEV_BOOKS[DEV_OTHER_REP_ID]);
  });

  /**
   * The empty-book rep exists so the designed empty states are reachable in a browser.
   */
  it("gives the empty rep a genuinely empty book, so the empty states are reachable", () => {
    expect(DEV_REPS.empty.book).toEqual([]);
    expect(DEV_REPS.empty.fullVisibility).toBe(false);
  });

  /**
   * An empty book is not a denial: the empty-book rep gets a 200 with no rows, compared against a rep
   * that does see rows.
   */
  it("returns no rows for the empty rep and rows for the scoped one, neither as a denial", () => {
    const empty = handleDevApiRequest("search_opportunities", {}, bearer("empty"));
    const scoped = handleDevApiRequest("search_opportunities", {}, bearer("scoped"));

    expect(empty.status).toBe(200);
    expect((empty.body as { items: unknown[] }).items).toEqual([]);

    expect(scoped.status).toBe(200);
    expect((scoped.body as { items: unknown[] }).items.length).toBeGreaterThan(0);
  });

  it("mints a token whose claims carry what parseIdentityClaims requires", () => {
    const [, payload] = mintDevToken(DEV_REPS.scoped).split(".");
    const claims = JSON.parse(
      Buffer.from(payload.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8"),
    ) as Record<string, unknown>;
    expect(claims.sub).toBeTruthy();
    expect(claims["custom:rep_id"]).toBe(DEV_REP_ID);
    expect(typeof claims.exp).toBe("number");
  });
});

/**
 * Contract test against the Python handlers, which are authoritative for response shapes.
 */
describe("response envelopes match the Python handlers", () => {
  const repoRoot = resolve(frontendRoot, "..");

  function handlerSource(name: string): string {
    return readFileSync(join(repoRoot, "src", "crm", "handlers", `${name}.py`), "utf8");
  }

  it.each([
    ["list_proposals", "proposals"],
    ["list_audit_records", "audit_records"],
    ["list_activities", "activities"],
  ])("%s returns a %s key, and the stub uses the same one", (handler, key) => {
    expect(handlerSource(handler)).toContain(`"${key}":`);
    const stubBody = handleDevApiRequest(handler, { opportunity_id: "opp-northwind-renewal" }, bearer("scoped"))
      .body as Record<string, unknown>;
    expect(Object.keys(stubBody)).toContain(key);
    expect(Object.keys(stubBody)).toContain("next_cursor");
    expect(Object.keys(stubBody)).not.toContain("items");
    expect(Object.keys(stubBody)).not.toContain("cursor");
  });

  it("get_account returns account and contacts only", () => {
    expect(handlerSource("get_account")).toContain('{"account": account, "contacts": contacts}');
    const body = handleDevApiRequest("get_account", { account_id: "acc-northwind" }, bearer("scoped"))
      .body as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(["account", "contacts"]);
  });

  it("get_opportunity returns opportunity, contacts, notes, activities", () => {
    const body = handleDevApiRequest(
      "get_opportunity",
      { opportunity_id: "opp-northwind-renewal" },
      bearer("scoped"),
    ).body as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(["activities", "contacts", "notes", "opportunity"]);
  });

  it("pipeline_aggregate returns the three rollup keys the handler returns", () => {
    expect(handlerSource("pipeline_aggregate")).toContain('"stage_funnel"');
    const body = handleDevApiRequest("pipeline_aggregate", {}, bearer("scoped")).body as Record<
      string,
      unknown
    >;
    expect(Object.keys(body).sort()).toEqual([
      "forecast_by_close_date",
      "stage_funnel",
      "value_rollup",
    ]);
  });
});

/**
 * Asserts the stub's envelopes against `export_response_shapes.py`, which executes the real
 * handlers against moto and emits their true top-level keys. Tools the exporter does not cover are
 * asserted against the handler source above.
 */
describe("stub envelopes match the executed handlers (export_response_shapes.py)", () => {
  const repoRoot = resolve(frontendRoot, "..");

  /**
   * Inputs sufficient for the stub to reach a 200 for each tool the exporter reports. The "covers
   * every exporter-reported tool" assertion below fails if this map falls behind.
   */
  const INPUTS: Readonly<Record<string, Record<string, unknown>>> = {
    search_accounts: {},
    search_opportunities: {},
    get_opportunity: { opportunity_id: "opp-northwind-renewal" },
    get_account: { account_id: "acc-northwind" },
    get_contact: { contact_id: "con-dana" },
    list_activities: { opportunity_id: "opp-northwind-renewal" },
    pipeline_aggregate: {},
    list_proposals: {},
    list_audit_records: {},
    get_audit_record: { audit_id: "aud-2" },
    draft_message: { contact_id: "con-dana", intent: "follow_up" },
    create_account: { name: "Dev Account" },
    create_contact: { account_id: "acc-northwind", name: "Dev Contact" },
    create_opportunity: { account_id: "acc-northwind", name: "Dev Opportunity" },
    create_note: { opportunity_id: "opp-northwind-renewal", body: "note" },
    log_activity: { opportunity_id: "opp-northwind-renewal", kind: "call", subject: "Call" },
    update_account: { account_id: "acc-northwind", fields: { name: "Renamed" } },
    update_contact: { contact_id: "con-dana", fields: { title: "VP" } },
    update_opportunity: { opportunity_id: "opp-northwind-renewal", fields: { description: "x" } },
    update_activity: { activity_id: "act-northwind-01", fields: { subject: "Renamed" } },
    submit_proposal: { target_id: "opp-northwind-renewal", target_table: "opportunities" },
    approve_proposal: { proposal_id: "prop-1" },
    reject_proposal: { proposal_id: "prop-2" },
    rebase_proposal: { proposal_id: "prop-1" },
    revert_audit_record: { audit_id: "aud-2" },
  };

  let shapes: Record<string, string[]>;

  try {
    const raw = execFileSync(
      join(repoRoot, ".venv", "bin", "python3"),
      [join(frontendRoot, "scripts", "export_response_shapes.py")],
      { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
    );
    shapes = JSON.parse(raw) as Record<string, string[]>;
  } catch {
    shapes = {};
  }

  it("could run the exporter at all — otherwise this suite verifies nothing", () => {
    expect(
      Object.keys(shapes).length,
      "export_response_shapes.py produced no shapes; the .venv may be missing",
    ).toBeGreaterThan(0);
  });

  it("covers every exporter-reported tool the stub implements", () => {
    const reported = Object.keys(shapes).filter((k) => !k.includes("."));
    expect(reported.sort()).toEqual(Object.keys(INPUTS).sort());
  });

  it.each(Object.keys(INPUTS))("%s: stub keys equal the real handler's keys", (toolName) => {
    const expected = shapes[toolName];
    expect(expected, `${toolName} absent from the exporter`).toBeDefined();

    const result = handleDevApiRequest(toolName, INPUTS[toolName], bearer("scoped"));
    expect(result.status, JSON.stringify(result.body)).toBe(200);

    const actual = Object.keys(result.body as Record<string, unknown>).sort();
    /*
     * The stub may omit a key the handler returns, but must never invent one the handler does not
     * return.
     */
    expect(actual.filter((key) => !expected.includes(key))).toEqual([]);
  });

  it("nests a created record under its entity name rather than returning a bare id", () => {
    for (const [toolName, entity] of [
      ["create_account", "account"],
      ["create_contact", "contact"],
      ["create_opportunity", "opportunity"],
    ] as const) {
      const body = handleDevApiRequest(toolName, INPUTS[toolName], bearer("scoped")).body as Record<
        string,
        unknown
      >;
      expect(Object.keys(body)).toEqual([entity]);
      expect(body[entity]).toHaveProperty(`${entity}_id`);
    }
  });

  it("nests a submitted proposal under `proposal`", () => {
    const body = handleDevApiRequest("submit_proposal", INPUTS.submit_proposal, bearer("scoped"))
      .body as Record<string, unknown>;
    expect(Object.keys(body)).toEqual(["proposal"]);
    expect(body.proposal).toHaveProperty("proposal_id");
    expect(body.proposal).toHaveProperty("status", "pending");
  });
});
