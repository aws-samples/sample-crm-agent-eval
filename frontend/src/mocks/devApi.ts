// SPDX-License-Identifier: MIT
/**
 * Dev-only handler for `POST /api/<tool_name>`, standing in for the deployed CRM Lambdas.
 *
 * It stubs the backend, not the authorization: a request without a bearer token is 401, and a read
 * of a record outside the caller's book is 403 `scope_denied` from this module's own scope check
 * against `DEV_BOOKS`. The token's signature is not verified; the plugin that mounts this handler is
 * `apply: "serve"`.
 *
 * Never imported by application code.
 */

import {
  ACCOUNTS,
  ACTIVITIES,
  ACTIVITY_PAGE_SIZE,
  AUDIT_RECORDS,
  CONTACTS,
  DEV_BOOKS,
  NOTES,
  OPPORTUNITIES,
  PROPOSALS,
  SEARCH_PAGE_SIZE,
} from "./devFixtures";

export interface DevApiResponse {
  readonly status: number;
  readonly body: unknown;
}

/** The identity the stub authorizes against, decoded from the bearer token's claims. */
interface DevIdentity {
  readonly repId: string;
  readonly book: readonly string[];
  readonly fullVisibility: boolean;
}

/**
 * The wire error envelope, exactly as `crm.errors.CrmError.to_api_response` renders it: `code`,
 * `message` and — for a denial — `missing_scope`, nested under `error` and snake_case.
 *
 * Every error the dev stub returns goes through here, including `devServerPlugin.ts`'s
 * `/api/capabilities` branch. `errorEnvelope.contract.test.ts` checks it against `src/crm/errors.py`.
 */
export function errorEnvelope(
  status: number,
  code: string,
  message: string,
  missingScope?: string,
): DevApiResponse {
  return {
    status,
    body: {
      statusCode: status,
      error: {
        code,
        message,
        ...(missingScope === undefined ? {} : { missing_scope: missingScope }),
      },
    },
  };
}

function unauthorized(message: string): DevApiResponse {
  return errorEnvelope(401, "authentication_failed", message);
}

function scopeDenied(what: string): DevApiResponse {
  return errorEnvelope(403, "scope_denied", `${what} is outside your assigned book.`, what);
}

function notFound(what: string): DevApiResponse {
  return errorEnvelope(404, "not_found", `${what} does not exist.`);
}

/** Decodes the identity claims from a bearer token. Signature is not checked — dev only. */
function decodeIdentity(authorizationHeader: string | undefined): DevIdentity | undefined {
  if (authorizationHeader === undefined) return undefined;
  const prefix = "Bearer ";
  if (!authorizationHeader.startsWith(prefix)) return undefined;

  const segments = authorizationHeader.slice(prefix.length).split(".");
  if (segments.length !== 3) return undefined;

  let claims: Record<string, unknown>;
  try {
    const payload = segments[1].replace(/-/g, "+").replace(/_/g, "/");
    claims = JSON.parse(
      Buffer.from(payload.padEnd(Math.ceil(payload.length / 4) * 4, "="), "base64").toString(
        "utf8",
      ),
    ) as Record<string, unknown>;
  } catch {
    return undefined;
  }

  const repId = claims["custom:rep_id"];
  if (typeof repId !== "string" || repId.length === 0) return undefined;
  if (typeof claims.exp === "number" && claims.exp * 1000 < Date.now()) return undefined;

  return {
    repId,
    book: DEV_BOOKS[repId] ?? [],
    fullVisibility: claims["custom:full_visibility"] === "true",
  };
}

function inScope(identity: DevIdentity, accountId: string | undefined): boolean {
  if (identity.fullVisibility) return true;
  return accountId !== undefined && identity.book.includes(accountId);
}

function accountOf(opportunityId: string): string | undefined {
  return OPPORTUNITIES.find((o) => o.opportunity_id === opportunityId)?.account_id;
}

function asString(input: Record<string, unknown>, key: string): string | undefined {
  const value = input[key];
  return typeof value === "string" ? value : undefined;
}

/**
 * Handles one tool call. Returns a response for every input including malformed ones — it never
 * throws, so a bad request surfaces to the page as a taxonomy-shaped error rather than a dev-server
 * stack trace.
 */
export function handleDevApiRequest(
  toolName: string,
  input: Record<string, unknown>,
  authorizationHeader: string | undefined,
): DevApiResponse {
  const identity = decodeIdentity(authorizationHeader);
  if (identity === undefined) {
    return unauthorized("Request carried no usable bearer token.");
  }

  const visibleOpportunities = OPPORTUNITIES.filter((o) => inScope(identity, o.account_id));

  switch (toolName) {
    case "search_opportunities": {
      const stage = asString(input, "stage");
      const accountId = asString(input, "account_id");
      if (accountId !== undefined && !inScope(identity, accountId)) return scopeDenied(accountId);
      const matched = visibleOpportunities.filter(
        (o) =>
          (stage === undefined || o.stage === stage) &&
          (accountId === undefined || o.account_id === accountId),
      );
      return {
        status: 200,
        body: {
          items: matched.slice(0, SEARCH_PAGE_SIZE),
          // Produced by the data, not asserted: the seeded set exceeds one page.
          truncated: matched.length > SEARCH_PAGE_SIZE,
          examined_count: matched.length,
          next_cursor: matched.length > SEARCH_PAGE_SIZE ? `cursor-${SEARCH_PAGE_SIZE}` : null,
        },
      };
    }

    case "search_accounts": {
      const owner = asString(input, "owner_rep_id");
      const queryTokens = (asString(input, "query") ?? "")
        .toLowerCase()
        .split(/\s+/)
        .filter((t) => t.length > 0);
      // Owner filter routes to `gsi_owner`, which projects `owner_rep_id`; the tenant-wide path does
      // not, so the field is present on a row only when the owner filter was applied.
      const ownerBook = owner === undefined ? undefined : (DEV_BOOKS[owner] ?? []);
      const matched = ACCOUNTS.filter(
        (a) =>
          inScope(identity, a.account_id) &&
          (ownerBook === undefined || ownerBook.includes(a.account_id)) &&
          queryTokens.every((t) => a.name.toLowerCase().includes(t)),
      ).map((a) => ({
        account_id: a.account_id,
        name: a.name,
        tenant: "demo",
        ...(owner === undefined ? {} : { owner_rep_id: owner }),
      }));
      return {
        status: 200,
        body: {
          items: matched.slice(0, SEARCH_PAGE_SIZE),
          truncated: matched.length > SEARCH_PAGE_SIZE,
          examined_count: matched.length,
          next_cursor: matched.length > SEARCH_PAGE_SIZE ? `cursor-${SEARCH_PAGE_SIZE}` : null,
        },
      };
    }

    case "get_opportunity": {
      const id = asString(input, "opportunity_id");
      const opportunity = OPPORTUNITIES.find((o) => o.opportunity_id === id);
      if (opportunity === undefined) return notFound(id ?? "that opportunity");
      if (!inScope(identity, opportunity.account_id)) return scopeDenied(opportunity.account_id);
      return {
        status: 200,
        body: {
          opportunity,
          contacts: CONTACTS.filter((c) => c.account_id === opportunity.account_id),
          notes: NOTES.filter((n) => n.opportunity_id === id),
          activities: ACTIVITIES.filter((a) => a.opportunity_id === id).slice(0, ACTIVITY_PAGE_SIZE),
        },
      };
    }

    case "get_account": {
      const id = asString(input, "account_id");
      const account = ACCOUNTS.find((a) => a.account_id === id);
      if (account === undefined || id === undefined) return notFound(id ?? "that account");
      if (!inScope(identity, id)) return scopeDenied(id);
      return {
        status: 200,
        // The handler returns exactly these two keys — no `opportunities`.
        body: { account, contacts: CONTACTS.filter((c) => c.account_id === id) },
      };
    }

    case "get_contact": {
      const id = asString(input, "contact_id");
      const contact = CONTACTS.find((c) => c.contact_id === id);
      if (contact === undefined) return notFound(id ?? "that contact");
      if (!inScope(identity, contact.account_id)) return scopeDenied(contact.account_id);
      return { status: 200, body: { contact } };
    }

    case "list_activities": {
      const opportunityId = asString(input, "opportunity_id");
      if (opportunityId === undefined) return notFound("that opportunity");
      const account = accountOf(opportunityId);
      if (account === undefined) return notFound(opportunityId);
      if (!inScope(identity, account)) return scopeDenied(account);
      const all = ACTIVITIES.filter((a) => a.opportunity_id === opportunityId);
      return {
        status: 200,
        body: {
          activities: all.slice(0, ACTIVITY_PAGE_SIZE),
          next_cursor: all.length > ACTIVITY_PAGE_SIZE ? `cursor-${ACTIVITY_PAGE_SIZE}` : null,
        },
      };
    }

    case "pipeline_aggregate": {
      const byStage = new Map<string, { count: number; value: number }>();
      for (const opportunity of visibleOpportunities) {
        const bucket = byStage.get(opportunity.stage) ?? { count: 0, value: 0 };
        byStage.set(opportunity.stage, {
          count: bucket.count + 1,
          value: bucket.value + opportunity.value,
        });
      }
      return {
        status: 200,
        body: {
          stage_funnel: Object.fromEntries(
            [...byStage.entries()].map(([stage, bucket]) => [stage, bucket.count]),
          ),
          value_rollup: Object.fromEntries(
            [...byStage.entries()].map(([stage, bucket]) => [stage, bucket.value]),
          ),
          forecast_by_close_date: Object.fromEntries(
            [...visibleOpportunities]
              .filter((o) => o.close_date !== undefined)
              .map((o) => [o.close_date as string, o.value]),
          ),
        },
      };
    }

    /*
     * Key names come from the Python handlers: `list_proposals.py` returns `proposals`,
     * `list_audit_records.py` returns `audit_records`, `list_activities.py` returns `activities`,
     * each with `next_cursor`.
     */
    case "list_proposals":
      return { status: 200, body: { proposals: PROPOSALS, next_cursor: null } };

    case "list_audit_records":
      return { status: 200, body: { audit_records: AUDIT_RECORDS, next_cursor: null } };

    /** Nested under `audit_record` with the derived `action`, per `crm.audit.get_audit_record`. */
    case "get_audit_record": {
      const auditId = asString(input, "audit_id");
      const record = AUDIT_RECORDS.find((r) => r.audit_id === auditId);
      if (record === undefined) return notFound(auditId ?? "that audit record");
      const action =
        Object.keys(record.before_snapshot ?? {}).length === 0
          ? "create"
          : Object.keys(record.after_snapshot ?? {}).length === 0
            ? "delete"
            : "update";
      return { status: 200, body: { audit_record: { ...record, action } } };
    }

    case "draft_message":
      return {
        status: 200,
        body: {
          contact_id: asString(input, "contact_id") ?? "con-dana",
          draft:
            "Hi Dana — following up on the renewal. Happy to walk through the expanded logistics module this week.",
          tone: asString(input, "tone") ?? "neutral",
        },
      };

    /*
     * A high-risk change has no direct tool: the only path is `submit_proposal` then approve/reject.
     */
    case "submit_proposal": {
      const targetId = asString(input, "target_id") ?? "unknown";
      // Nested under `proposal`, per `export_response_shapes.py`'s real handler output.
      return {
        status: 200,
        body: {
          proposal: {
            proposal_id: `prop-dev-${targetId}`,
            target_table: asString(input, "target_table") ?? "opportunities",
            target_id: targetId,
            operation: asString(input, "operation") ?? "update",
            before_snapshot: {},
            after_payload: (input.after_payload as Record<string, unknown>) ?? {},
            reason: asString(input, "reason") ?? "",
            status: "pending",
            actor_rep_id: identity.repId,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          },
        },
      };
    }

    case "approve_proposal":
    case "reject_proposal": {
      const proposalId = asString(input, "proposal_id");
      const proposal = PROPOSALS.find((p) => p.proposal_id === proposalId);
      if (proposal === undefined) return notFound(proposalId ?? "that proposal");
      // Nested under `proposal`, per the exporter.
      return {
        status: 200,
        body: {
          proposal: {
            ...proposal,
            status: toolName === "approve_proposal" ? "approved" : "rejected",
            decided_at: new Date().toISOString(),
          },
        },
      };
    }

    /** Nested under `proposal`; the stub has no live target, so the snapshot is kept as-is. */
    case "rebase_proposal": {
      const proposalId = asString(input, "proposal_id");
      const proposal = PROPOSALS.find((p) => p.proposal_id === proposalId);
      if (proposal === undefined) return notFound(proposalId ?? "that proposal");
      if (proposal.status !== "pending") {
        return errorEnvelope(409, "conflict", "This proposal has already been decided.");
      }
      return {
        status: 200,
        body: { proposal: { ...proposal, updated_at: new Date().toISOString() } },
      };
    }

    case "revert_audit_record": {
      const auditId = asString(input, "audit_id");
      const record = AUDIT_RECORDS.find((r) => r.audit_id === auditId);
      if (record === undefined) return notFound(auditId ?? "that audit record");
      if (record.reversed_at !== undefined) {
        // A second revert is refused.
        return errorEnvelope(409, "conflict", "That change has already been reverted.");
      }
      // Nested under `audit_record`, per the exporter.
      return {
        status: 200,
        body: {
          audit_record: { ...record, status: "reversed", reversed_at: new Date().toISOString() },
        },
      };
    }

    /*
     * Creates nest the new record under its entity name — `{account}`, `{contact}`,
     * `{opportunity}` — never a bare id. Verified against `export_response_shapes.py`.
     */
    case "create_note": {
      const now = new Date().toISOString();
      return {
        status: 200,
        body: {
          note: {
            note_id: "note-dev-1",
            opportunity_id: asString(input, "opportunity_id") ?? "opp-northwind-renewal",
            body: asString(input, "body") ?? "",
            created_at: now,
          },
        },
      };
    }

    /** Returns the activity **and** its opportunity, per the exporter — not the activity alone. */
    case "log_activity": {
      const opportunityId = asString(input, "opportunity_id") ?? "opp-northwind-renewal";
      const opportunity = OPPORTUNITIES.find((o) => o.opportunity_id === opportunityId);
      if (opportunity === undefined) return notFound(opportunityId);
      if (!inScope(identity, opportunity.account_id)) return scopeDenied(opportunity.account_id);
      const now = new Date().toISOString();
      return {
        status: 200,
        body: {
          activity: {
            activity_id: "act-dev-1",
            opportunity_id: opportunityId,
            kind: asString(input, "kind") ?? "note",
            subject: asString(input, "subject") ?? "",
            occurred_at: asString(input, "occurred_at") ?? now,
            created_at: now,
            updated_at: now,
          },
          opportunity: { ...opportunity, last_touch: now },
        },
      };
    }

    /*
     * Creates and updates nest the record under its entity name — `{account}`, `{contact}`,
     * `{opportunity}`, `{activity}` — never a bare id. Verified against
     * `frontend/scripts/export_response_shapes.py`, which runs the real handlers.
     */
    case "create_account":
    case "create_contact":
    case "create_opportunity":
    case "update_account":
    case "update_contact":
    case "update_opportunity":
    case "update_activity": {
      const entity = toolName.replace(/^(create|update)_/, "");
      const now = new Date().toISOString();
      const fields = (input.fields as Record<string, unknown> | undefined) ?? input;
      return {
        status: 200,
        body: {
          [entity]: {
            [`${entity}_id`]: asString(input, `${entity}_id`) ?? `${entity}-dev-1`,
            ...fields,
            created_at: now,
            updated_at: now,
          },
        },
      };
    }

    default:
      return errorEnvelope(
        404,
        "not_found",
        `The dev API stub has no handler for "${toolName}". Add one in src/mocks/devApi.ts.`,
      );
  }
}
