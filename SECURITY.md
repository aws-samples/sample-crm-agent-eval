# Security

See also: [documentation map](README.md#documentation) · [security baseline](docs/security-baseline.md) · [agent architecture](docs/agent-architecture.md) · [MCP client guide](docs/mcp-client-guide.md) · [runbook](docs/runbook.md)

This page is the project's threat model: what the system protects, who might attack it and how,
which controls stand in the way, and which risks remain. It maps the system to three OWASP lists:
the OWASP Top 10 (2025), the current web edition; the OWASP Top 10 for LLM Applications (2026); and
the OWASP Top 10 for Agentic Applications (2026). The infrastructure controls themselves, each with
its verification command, live in [`docs/security-baseline.md`](docs/security-baseline.md).

## Reporting a vulnerability

If you discover a potential security issue in this project, notify AWS/Amazon Security via the
[vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do
**not** create a public GitHub issue.

## Scope and status

The stack is a demonstration and measurement instrument, not a production CRM (see
[Limitations](README.md#limitations)). This model assumes:

- one AWS account, deployed with `scripts/deploy.sh` by a small set of operators;
- synthetic CRM data (the fixture data set and seeded eval tenants), with no real customer records;
- users created by an administrator, never by self sign-up;
- the AWS services themselves (Cognito, CloudFront, API Gateway, Lambda, DynamoDB, Bedrock,
  AgentCore, S3, KMS) working as documented.

Anyone deploying it with real data must revisit the accepted risks below first.

## System overview

```text
Browser SPA ──HTTPS──▶ CloudFront ─┬─ /*        ▶ S3 origin (OAC, private)
                                   ├─ /api/*    ▶ VPC origin ▶ internal ALB ▶ private REST API ▶ action Lambdas
                                   └─ /agent/*  ▶ OAC (SigV4) ▶ relay Function URL (AWS_IAM) ▶ AgentCore Runtime
MCP client ──HTTPS──▶ AgentCore Gateway #1 (CRM tools) / #2 (chat_with_supervisor)
                       └─ CUSTOM_JWT authorizer ▶ REQUEST interceptor ▶ action Lambdas / chat target
AgentCore Runtime ── supervisor, subagents, tool tier ──▶ Bedrock (SigV4, no API keys)
                                                      └─▶ action Lambdas (lambda:InvokeFunction)
Admin /evals page ──▶ eval_trigger ──▶ Step Functions sweep ──▶ Runtime (eval_turn) ──▶ eval-artefacts S3
```

All three entry planes, the SPA, external MCP clients and the in-app agents, end at the same CRM
action Lambdas. Authorization is decided there and nowhere else.

### Assets

| Asset | Where it lives | Why it matters |
|---|---|---|
| CRM records (accounts, contacts, opportunities, activities, proposals) | DynamoDB, KMS CMK, PITR | confidentiality and integrity per tenant and per rep book |
| Audit records | DynamoDB, written in the same transaction as each mutation | non-repudiation of writes and approvals |
| User identities and tokens | Cognito user pool, browser session storage, MCP client credential store | every authorization decision rests on them |
| MCP client secret | the file written by `scripts/rotate_mcp_client_secret.py` | admits a machine caller to both Gateways |
| Model spend and quota | Bedrock in the deploying account | direct cost and availability |
| Eval artefacts (matrices, transcripts, raw records) | eval-artefacts S3 bucket, KMS CMK | integrity of published measurements |
| Deployment authority | operator roles, Terraform state bucket and lock table | controls every other asset |

### Trust boundaries

1. **Internet to edge.** CloudFront and the two Gateway endpoints are the only internet-facing
   surfaces. No security group admits internet ingress, and the API, relay and Runtime are reachable
   only through these edges.
2. **Browser to backend.** Everything in the browser is untrusted. Client-side checks (tuning
   validation, admin-only UI controls, decoded identity) are presentation only; the backend repeats
   every check.
3. **Edge to identity.** Each edge verifies a Cognito JWT itself and then re-derives the caller's
   identity. Identity is never forwarded as the caller sent it.
4. **Model to tools.** Model output is untrusted. The tool tier's arguments are validated against
   the generated toolspec, and identity keys written by the model are stripped before dispatch.
5. **Account to model provider.** Prompts, page context and tool results leave the workload for
   Bedrock over SigV4.
6. **Operator to account.** Operators hold deployment and data authority outside every in-app
   control. Resource policies narrow which principals may touch each data store.

### Threat actors

| Actor | Starting position |
|---|---|
| Anonymous internet client | can reach CloudFront and the Gateway endpoints; holds no token |
| Signed-in rep | valid SPA token for one tenant and one book; may be curious or malicious |
| Stolen-token holder | replays a rep or machine token until it expires |
| MCP client operator | holds the machine client secret |
| Content author | plants text in CRM fields that an agent later reads (indirect prompt injection) |
| Compromised dependency | runs inside the build, the browser or a Lambda |
| In-account principal | has IAM rights in the deploying account but is not an operator |

## Security design invariants

These properties are enforced in code and checked by tests or gates. They are what the threat
table below relies on.

- **One enforcement boundary.** The action Lambdas decide authorization through one scope predicate
  (`src/crm/authz.py`) used by get, list, search and aggregates alike. A record hidden from `get`
  cannot leak through a rollup.
- **Identity is channel-decoded, never constructed.** `src/crm/envelope.py:decode_identity` selects
  exactly one of three channels (`apigw`, `gateway`, `agent`) from top-level event keys. There is no
  default; an identity that cannot be established is refused. On `apigw`, the token's app client must
  be in an SSM `SecureString` allowlist, and an unreadable or empty allowlist admits nobody.
- **Every hop strips and rewrites identity.** The relay replaces the caller's claims with its own
  verified `_agent_identity`. The Gateway interceptor removes every identity-channel key from tool
  arguments before writing its own. The tool tier removes identity keys the model wrote.
- **Tenant isolation is structural.** Every GSI partition key is tenant-prefixed and every index
  projects `tenant` (`src/crm/store/keys.py`, `src/crm/store/schema.py`). Eval sweeps run in
  disposable `eval-<run_id>` tenants and may not write `demo` or `canary`.
- **No direct high-risk writes.** High-risk changes go through `submit_proposal` and a human approve
  or reject in the UI. The agent channel and service principals cannot approve, reject or revert. The
  proposal card renders from the stored proposal, not from model text.
- **No vendor or Bedrock API keys.** Model calls are SigV4-signed from the workload role. The SDK
  bearer-token fallbacks are refused, and `scripts/check_no_vendor_keys.sh` and gitleaks gate the
  tree.
- **Fail closed.** No raw `botocore` exception crosses a module boundary (`src/crm/errors.py`).
  Missing configuration, allowlists, cursor keys or scope attributes refuse the request rather than
  widen it.

## Threats and mitigations

Severity reflects this deployment's scope: admin-provisioned users and synthetic data in one
account.

| Id | STRIDE | Threat | Mitigations | Residual and severity |
|---|---|---|---|---|
| TM-01 | Spoofing | Forged or unsigned JWT at an edge | relay, REST authorizer and Gateway interceptor each verify the RS256 signature against the pool JWKS, expiry and app client; Gateway `CUSTOM_JWT` checks allowed clients and audiences | none known; Low |
| TM-02 | Spoofing | Token stolen from the browser or a client store and replayed | token expiry; refresh-token revocation at sign-out; enforced CSP with no `unsafe-inline` script; markdown rendered without raw HTML; each surface accepts only its own app client | a stolen token works until expiry; Medium |
| TM-03 | Spoofing | Caller-supplied `_identity` or `_agent_identity` in a request body | channel chosen from top-level event keys; interceptor and tool tier strip identity keys; Runtime resource policy admits only the relay, sweep and chat-target roles, and by default denies any call not arriving through the stack's `bedrock-agentcore` VPC endpoint (`runtime_source_vpce_deny_enabled`) | in-account principals with `lambda:InvokeFunction` on action Lambdas can hand-build an envelope, which the local live harness does by design; Low |
| TM-04 | Spoofing | Self-registration or username chosen to collide with a rep | `allow_admin_create_user_only`; pre-token trigger keys on the immutable Cognito username; only operators write `reps` rows | operator misuse; Low |
| TM-05 | Tampering | Cross-tenant or out-of-book read or write | structural tenant keys; `require_scope` / `require_record_scope` in every handler touching an existing record; caller-bound KMS-encrypted pagination cursors | none known; Low |
| TM-06 | Tampering | Indirect prompt injection steers a write | proposals for high-risk changes; human approval bound to the stored proposal; writes limited to the rep's own book; per-turn tool-call and time budgets | injected text can still steer low-risk writes inside the rep's book; Medium |
| TM-07 | Tampering | Tampered SPA assets or `/config.json` | origin bucket private, OAC-only, writes denied to all but operators; CSP `connect-src` pins API and Cognito origins | anyone who can write the origin can replace the JavaScript too; Low |
| TM-08 | Tampering | Eval sweep writes outside its run | disposable tenants; `demo`/`canary` writes refused; every sweep step that writes (seed, aggregate, teardown, finalize) first re-checks that its execution still owns the run, and a refused step ends the execution without writing | none known; Low |
| TM-09 | Repudiation | A user denies a write or approval | audit row in the same transaction as each mutation, naming the verified rep and the channel the write arrived on (`apigw`, `gateway` or `agent`); API Gateway access logs; CloudTrail | MCP callers share one service principal; relay log lines carry no rep id; Low |
| TM-10 | Information disclosure | Bulk extraction through the agent | scope predicate on every read; per-call result and read caps; per-turn tool-call cap | a rep can summarise their whole book, which they may already read; Low |
| TM-11 | Information disclosure | CRM data sent to the model provider | Bedrock in the deploying account over SigV4; data retention governed by the account's Bedrock setting; synthetic data | no per-field redaction, which would break the measurement; Low with synthetic data, revisit for real data |
| TM-12 | Information disclosure | Leaked artefact URL | presigned GET of one object for 300 s, issued only to admins or full-visibility users; keys built server-side from the run id, `..` refused; S3 access logs | bearer link until expiry; Low |
| TM-13 | Information disclosure | Session or conversation takeover | session ownership (rep and tenant) checked before resume or cancel; MCP conversation ids minted server-side | all holders of the one MCP secret share a principal; process-local session state fails closed on another container; Low |
| TM-14 | Denial of service | Request or cost flooding by a signed-in caller | API Gateway stage throttle; reserved concurrency on every function; per-turn token, tool-call and time budgets; prompt length cap; no self sign-up | no per-principal quota or spend ceiling, and no WAF; Medium |
| TM-15 | Denial of service | Runaway or repeated eval sweeps | live sweeps admin-only with a confirmed estimate; daily live cap; one sweep at a time; sweep tasks run sequentially; dead lock holders released on the next submit | no hard dollar ceiling on the state-machine live path; Low |
| TM-16 | Denial of service | Dependency outage at sign-in or on the request path | pre-token trigger falls back to an empty book without full visibility (least privilege); JWKS and allowlist cached per warm environment and retried after failure | a failed reps lookup logs at INFO with no alarm; Low |
| TM-17 | Elevation of privilege | Machine token with broad scope | interceptor maps every machine token to one service principal limited to the demo tenant and without full visibility; service principals cannot decide proposals | one principal for every MCP client; Medium if a second client is ever added |
| TM-18 | Elevation of privilege | Non-admin drives privileged eval actions | live submit, cancel and unlock need the admin group; stub sweeps and run reads need admin or full visibility, enforced server-side | none beyond the documented policy; Low |
| TM-19 | Elevation of privilege | Compromised dependency or build input | exact pins with a frozen lock installed `--no-deps`; one lockfile per npm project; SDKs only in the Runtime image; gitleaks and vendor-key gates; the CI `sca` job runs `pip-audit` over the frozen Python lock (any known vulnerability fails it) and `npm audit --audit-level=high` in each npm project; dev mock server applied only to `vite serve` and absent from the build | npm findings below High do not fail the job; container images are not scanned in CI; a vulnerability published after the last pipeline run is not seen until the next one; Low |
| TM-20 | Information disclosure | Bearer headers on internal hops | CloudFront to API traffic stays inside the VPC on a VPC origin; the ALB is internal and admits only security groups; ALB to execute-api is HTTPS | the CloudFront-to-ALB hop is HTTP inside the VPC, with no certificate to terminate TLS there; Low |
| TM-21 | Tampering | A rep is persuaded to approve a misleading or long-forgotten proposal | high-risk changes need an explicit approve in the UI; the proposal card renders from the stored proposal, not from model text; the UI shows the tool trace beside the answer; approving a proposal pending for more than 7 days, or of unknown age, is refused with `proposal_stale` (rejecting it stays allowed) | a rep can still approve a recent stored proposal on the strength of a misleading answer; Low |
| TM-22 | Tampering | Model-authored control characters reach a renderer or a terminal | the Runtime removes U+0000–U+001F (except tab and newline) and U+007F from every event's `text`, `error_message` and `partial_work` before the event is emitted, so streamed answers, stored answers and answers returned to MCP clients carry none | proposal summaries, tool inputs and tool outputs are not stripped; a client that prints them to a terminal must neutralise them; Low |

## OWASP Top 10 (2025)

| Category | How it applies | Controls | Residual |
|---|---|---|---|
| A01 Broken Access Control | tenant, book and role scope on every record; admin-only eval actions | one scope predicate in the action Lambdas; structural tenant keys; channel-decoded identity; authorizer on every API route; resource policies on every data store | TM-03, TM-17 |
| A02 Security Misconfiguration | CloudFront, S3, API Gateway, IAM and KMS configuration | plan-time `check_plan_security_baseline`; Layer 4–6 live smoke; private origins with Block Public Access; no internet-facing ingress; enforced CSP and HSTS | no WAF; CloudFront minimum TLS deferred until a custom certificate is configured |
| A03 Software Supply Chain Failures | Python, npm and container dependencies | exact pins and frozen locks (`check_pin_drift`); `--no-deps` installs; lockfile per npm project; SDKs confined to the Runtime image; the CI `sca` job audits the Python lock and each npm project | npm findings below High pass; no container-image scan in CI (TM-19) |
| A04 Cryptographic Failures | data at rest and in transit | KMS CMKs with rotation on tables, buckets, logs and parameters; TLS-only bucket policies; HTTPS at the viewer; KMS-encrypted caller-bound cursors | TM-20; viewer TLS minimum deferred |
| A05 Injection | DynamoDB expressions, markdown rendering, shell in scripts | DynamoDB calls only through `src/crm/store/store.py` with expression attribute values; generated input validators with `maxLength`; markdown without raw HTML; `shellcheck` over every shell file | CRM strings accept control characters |
| A06 Insecure Design | agent writes and delegation | proposals for high-risk writes; four exclusive delegation outcomes so a denial is never reported as data; measurement-only eval with no verdict | TM-06 |
| A07 Authentication Failures | Cognito sign-in, machine tokens | admin-only sign-up; password policy; MFA available (TOTP, optional); token revocation; per-surface app-client allowlists | MFA is optional by design; TM-02 |
| A08 Software or Data Integrity Failures | SPA assets, eval artefacts, toolspec consumers | operator-only origin writes; generated consumers checked for parity (`check_toolspec_parity`, `check_sse_schema_parity`); fixture data regenerated byte for byte | artefacts are not signed |
| A09 Security Logging and Alerting Failures | detection and attribution | CloudTrail; API Gateway access logs; KMS-encrypted log groups with retention; `check_log_redaction` | no CloudTrail metric filters or alarms; TM-09 |
| A10 Mishandling of Exceptional Conditions | AWS errors, misconfiguration | error taxonomy at every module boundary; handler failures returned as classified payloads; fail-closed allowlist, cursor and identity paths | TM-16 |

## OWASP Top 10 for LLM Applications (2026)

| Category | How it applies | Controls | Residual |
|---|---|---|---|
| LLM01 Prompt Injection | rep prompts, MCP arguments, page context, CRM text returned as tool output, and the session's earlier turns | the model never holds authority: identity, tenant and scope come from verified tokens; tool arguments validated against the toolspec; page context is framed to the model as client-supplied data, not instructions; high-risk writes need human approval | TM-06 |
| LLM02 Sensitive Information Disclosure | CRM data in prompts, answers, tool arguments, logs and stored chat history | answers can only contain what scoped tools returned; images in answers are never fetched, so markdown cannot exfiltrate through a URL; log redaction (`check_log_redaction`); stored history is redacted, readable only by its owner and expires 24 hours after its last write | TM-11 |
| LLM03 Excessive Agency | agent write tools | role-scoped subagents with only the tools they need; no tool commits a high-risk change; the agent cannot approve, reject or revert; per-turn tool-call and time budgets | low-risk writes in the rep's own book (TM-06) |
| LLM04 Supply Chain | models, model access and SDKs | models reached only through Bedrock inference profiles in the account, with no third-party weights, adapters or fine-tunes; pinned vendor SDKs in the Runtime image only; model ids in `config/model_capabilities.yaml` | as A03 (TM-19) |
| LLM05 Data and Model Poisoning | golden tasks, seeded data and stored chat history | no fine-tuning, training or embedding; golden tasks and fixtures are tracked and reviewed; eval tenants are disposable; stored history holds only the session owner's own prompts and answers | planted CRM text is handled as LLM01, stored history as ASI06 |
| LLM06 Unbounded Consumption | token and cost exhaustion, including tool-call fan-out | per-request prompt limit (refused, not trimmed); per-turn token, tool-call and time budgets; tool output bounded before it re-enters the context; eval cost cap and daily live cap; throttles and reserved concurrency | TM-14, TM-15 |
| LLM07 Misinformation | fabricated CRM facts in answers | delegation outcomes are typed (`data`, `denied`, `tool_failure`, `no_match`), so a denial or failure is never turned into data; the UI shows the tool trace beside the answer | a model can still misstate data it was given |
| LLM08 Hidden Context Exposure | system prompts, answer style, page-context framing and the tool schemas the models receive | nothing in the model's context is meant to be secret: the prompts are published in `config/agent_prompts/`, and the agents' tool registry and the Gateway MCP tool definitions are generated from the tracked toolspec, which MCP clients can also list; no credential, token or identity is placed in model context; authorization never depends on prompt text or tool descriptions | none: the rest of a turn's context is the user's own prompt, page context and scoped data |
| LLM09 Vector and Embedding Weaknesses | not applicable | the system has no vector store, embedding index or semantic cache; search is a DynamoDB index query with text matching in process | none |
| LLM10 Improper Output Handling | model output rendered in the SPA, passed to tools and returned to MCP clients | markdown without raw HTML, `https:` and relative links only, no image fetch; tool arguments validated by the handler; SSE frames generated from one schema; control characters removed from answer, error and partial-work text (TM-22) | proposal summaries, tool inputs and tool outputs keep any control characters (TM-22) |

## OWASP Top 10 for Agentic Applications (2026)

| Category | How it applies | Controls | Residual |
|---|---|---|---|
| ASI01 Agent Goal Hijack | CRM text, page context or a prompt redirects the supervisor or a subagent across a multi-step turn | the agent's goal carries no authority: every tool call runs as the signed-in user and is re-authorized in the handler; page context is framed as data; a subagent sees only its delegated intent, not the conversation; high-risk changes need human approval; per-turn tool-call and time budgets | TM-06 |
| ASI02 Tool Misuse and Exploitation | an agent applying a legitimate tool unsafely, such as a wrong write or a costly loop | each agent role holds only the tools the toolspec grants it, and only the `mutation` subagent holds write tools; no tool commits a high-risk change; per-call result and read caps; per-turn tool-call and time budgets | low-risk writes in the rep's own book (TM-06); TM-10 |
| ASI03 Identity and Privilege Abuse | delegation from supervisor to subagent to tool tier, and the Runtime acting for a user | agents hold no identity or credential of their own; every call carries the verified user's identity on the `agent` channel, taken from the relay's or chat target's verified claims, and identity keys the model writes are stripped; delegation never widens scope; the agent channel cannot approve, reject or revert; the Runtime resource policy admits only the relay, sweep and chat-target roles | TM-03, TM-17 |
| ASI04 Agentic Supply Chain Vulnerabilities | tools, prompts, models and agent code the Runtime uses | nothing is loaded at run time from an outside source: tools come from the tracked toolspec and prompts from `config/agent_prompts/`, and both are part of the prompt-bundle digest recorded with each eval run; the agents connect to no external MCP server, agent or registry; models only through Bedrock; pinned SDKs built into the Runtime image | TM-19 |
| ASI05 Unexpected Code Execution (RCE) | whether model output can reach an interpreter | no agent holds a code interpreter, shell, browser or file-system tool; model output is only parsed as JSON tool arguments or rendered as text; YAML configuration is read with safe loading | none known |
| ASI06 Memory & Context Poisoning | durable chat history, the only agent memory that outlives a turn | history keeps each turn's prompt and final answer, never tool payloads; it is kept per session and owner and refused to any other user; at most 10 turns of at most 4,000 characters each, redacted; it expires 24 hours after its last write; no long-term, shared or vector memory; eval turns use no history | an answer that repeats injected CRM text is read again by later turns of the same session until the history expires (TM-06) |
| ASI07 Insecure Inter-Agent Communication | supervisor-to-subagent delegation | subagents run in the supervisor's Runtime process and are called in-process, with no network hop, message bus, agent discovery or agent-to-agent protocol; a delegation passes one `intent` string and returns one typed outcome; the callers into the Runtime (relay, chat target, sweep) sign with SigV4 and are admitted by its resource policy | the intent is natural language, so injected text can still shape it (TM-06) |
| ASI08 Cascading Failures | one bad output spreading through delegations, turns or sweeps | per-turn token, tool-call and time budgets end a runaway turn; a denial or failure is never turned into data; writes stay inside the caller's tenant and book; agents run only within a user's turn or an admin-started sweep, and never start another turn; sweep tasks run one at a time in disposable tenants | TM-14, TM-15 |
| ASI09 Human-Agent Trust Exploitation | a rep approving a change because an answer persuaded them | high-risk changes need an explicit approve in the UI; the proposal card renders from the stored proposal, not from model text; the UI shows the tool trace beside the answer; a proposal pending for more than 7 days can no longer be approved | a rep can still approve a recent stored proposal on the strength of a misleading answer (TM-21); Low |
| ASI10 Rogue Agents | an agent drifting from its intended scope | an agent keeps no state beyond its session's history, holds no credential and cannot change its own prompts, tools or model; it acts only inside a user's turn or a sweep; every write is audited under the verified rep | no alarm on unusual agent behaviour (TM-09) |

## Accepted risks and open items

The residuals named in the tables above are summarised here. The infrastructure deferrals are also
listed under [Limitations](README.md#limitations).

- No AWS WAF, and no per-principal rate or spend quota (TM-14).
- CloudFront minimum TLS 1.2 needs a custom ACM certificate (see the security baseline's Transport
  row).
- MFA stays optional by owner policy. Deployers handling real data should require it.
- npm findings below High do not fail CI (`pip-audit` fails on any known vulnerability), and
  container images are not scanned (TM-19).
- CloudTrail has no alarms (TM-09).
- One shared MCP service principal (TM-17).
- Prompts and tool results reach Bedrock unredacted; acceptable only for synthetic data (TM-11).
- Injected CRM text that an answer repeats stays in that session's history, and is read by its
  later turns, until the history expires (TM-06).
- A rep can approve a recent stored proposal because a misleading answer persuaded them. The card
  shows the stored change, so what is approved is what is stored (TM-21).
- Proposal summaries, tool inputs and tool outputs keep any control characters; a client that
  prints them to a terminal must neutralise them (TM-22).

## External threat model review

An automated STRIDE threat model of this repository, run on 2026-11-10, reported 22 threats (14 High,
8 Medium). Each claim was checked against the code. None reached High once checked: most rest on a
premise the code does not bear out, and the real residuals are recorded above.

| Reported threat | Disposition | Here |
|---|---|---|
| Tampered `/config.json` or SPA origin | false positive: operator-only writes, CSP pins origins, other pools' tokens refused | TM-07 |
| Dev server code execution or flooding | false positive: the dev server binds to localhost and runs only under `vite serve` | TM-19 |
| Eval writes outside the run tenant | mostly false; the one real gap, missing ownership checks in the seed and aggregate steps, is fixed | TM-08 |
| Non-admin drives eval actions | false positive: live, cancel and unlock are admin-only; app client checked | TM-18 |
| Flooding with no per-principal throttle | accepted: aggregate throttles and budgets bound it | TM-14 |
| CRM data sent to hosted models | accepted for synthetic data | TM-11 |
| Bulk extraction and tool enumeration | accepted: scoped, capped, authenticated | TM-10 |
| Session or conversation hijack | accepted: ownership checked; MCP ids minted server-side | TM-13 |
| Chosen username or reps-table writes | false positive: admin-only sign-up, immutable username | TM-04 |
| Book scope not enforced centrally | false positive: every handler that touches a record checks scope | TM-05 |
| Prompt injection into writes and proposals | accepted for low-risk writes; proposal claims false | TM-06 |
| Broad MCP service principal; caller `_identity` | accepted; caller `_identity` is stripped | TM-17 |
| Unsigned identity envelopes | accepted: in-account IAM only; body `_identity` never selects a channel | TM-03 |
| Token replay; audience; MFA; plaintext hops | accepted: per-surface client allowlists; MFA optional by policy | TM-02, TM-20 |
| Repudiation of writes and approvals | accepted: audit names the verified rep | TM-09 |
| Dev stub `alg: none` tokens | false positive: dev-only, absent from the build | TM-19 |
| Tampered run row or artefact keys | accepted: keys built server-side, `..` refused | TM-12 |
| Leaked presigned URL | accepted: 300 s, one object | TM-12 |
| Maximal or repeated sweeps | accepted: no fan-out, daily cap, lock release | TM-15 |
| Crafted settings or CRM payloads | accepted: strict server parsing, length caps | A05 |
| `transaction_key` collision | false positive: keys are random per action; a replay returns success | TM-09 |
| Cold-start floods, outages, fail-open trigger | accepted: the trigger falls back to least privilege | TM-16 |

## Verifying the controls

- `make verify` runs the offline gates (`scripts/run_gates.py`), including the plan-time security
  baseline, secret and vendor-key scans, log redaction, toolspec and SSE schema parity, and the
  identity, authorization and tenancy unit tests.
- `make verify-deploy` runs the live Layer 4–6 smoke against a deployed stack: IAM simulation,
  denied probes against each data store, authorized and unauthenticated calls on every route, and
  CloudFront headers. It is billable.
- [`docs/security-baseline.md`](docs/security-baseline.md) lists each infrastructure control with
  its verification command and class.
