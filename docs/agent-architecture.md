# Agent architecture

See also: [documentation map](../README.md#documentation) · [security baseline](security-baseline.md) · [MCP client guide](mcp-client-guide.md) · [relay artefact](relay-artefact.md) · [evaluation methodology](eval-methodology.md) · [agent prompts](../config/agent_prompts/README.md)

How the CRM assistant is built: where its parts run, how a chat turn moves through them, and how a
supervisor, its subagents and the tool tier divide the work. Each statement names the tracked source
it comes from. Read the source when the two disagree.

## Overview

- **One turn orchestrator.** `src/agents/turn_driver.py` (`drive_turn`) runs every turn. The deployed
  AgentCore Runtime and the eval harness both call it. It holds no identity and makes no
  authorization decision. Tool calls go through an identity-bound dispatch callable that its caller
  injects.
- **Three model tiers.** The supervisor, subagent and tool tiers each resolve their own family,
  model and effort (`src/providers/tier_tuning.py`). The models come from `config/models.yaml`, and
  the tuning each model accepts comes from `config/model_capabilities.yaml`.
- **One enforcement boundary.** Agents reach CRM data only by invoking the same action Lambdas the
  web app and MCP clients use (`src/agents/tool_dispatch.py`, `src/crm/dispatch.py`). Authorization
  happens in those handlers (`src/crm/authz.py`), not in a prompt, the relay or the Runtime.
- **One tool definition.** The agents' tool registry (`src/agents/registry.py`) is generated from
  `src/crm/toolspec/definitions.py`. That file also generates the handler validators and the Gateway
  MCP tool definitions. Each tool definition lists the agent roles allowed to hold it.

## Deployment view

![Deployment view: the browser loads the React SPA through CloudFront from S3, signs in with Cognito, and sends /api/* to the private REST API and /agent/* to the relay Lambda's Function URL; the relay calls the AgentCore Runtime, which calls Amazon Bedrock models and invokes the CRM action Lambdas, which use DynamoDB; MCP clients reach the CRM tools Gateway, which invokes one action Lambda per tool, and the chat Gateway, whose chat_with_supervisor Lambda calls the Runtime; each Gateway runs its own identity interceptor Lambda; CodeBuild builds the Runtime image into ECR](figures/agent-architecture-deployment.svg)

| Component | Role in the agent path | Source |
|---|---|---|
| Amazon CloudFront | `/agent/*` goes to the relay's `AWS_IAM` Function URL through a `lambda`-type OAC, with caching and compression off | `infra/modules/frontend_cdn/cloudfront.tf` |
| Relay Lambda (Node.js) | verifies the Cognito token in `X-Crm-Authorization`, calls `InvokeAgentRuntime` with SigV4, and re-emits the Runtime's SSE frames verbatim | `src/relay/src/index.ts`, `src/relay/src/jwt.ts`, `src/relay/src/runtime.ts` |
| AgentCore Runtime | the container serving `POST /invocations` and `GET /ping`; runs chat turns, eval turns and cancellation | `src/agents/runtime_server.py`, `src/agents/runtime_entrypoint.py`, `infra/modules/agentcore/` |
| Amazon ECR and AWS CodeBuild | CodeBuild builds the Runtime image and pushes it to ECR; the Runtime pulls it from there | `infra/modules/agentcore/ecr_codebuild.tf` |
| Amazon Bedrock | every model call, signed with the Runtime role's IAM credentials | `src/providers/factory.py`, `src/providers/sdk_transport.py` |
| CRM action Lambdas | one per tool; decode the caller's identity, validate input, authorize, and read or write DynamoDB | `src/crm/handlers/`, `src/crm/envelope.py`, `src/crm/store/store.py` |
| AgentCore Gateway (CRM tools) | MCP surface with one Lambda target per tool | `infra/modules/agentcore/gateway.tf`, `infra/modules/agentcore/gateway_targets.tf` |
| AgentCore Gateway (chat) | MCP surface exposing the assistant as the one tool `chat_with_supervisor` | `infra/modules/mcp_chat_gateway/`, `src/gateway/chat_target.py` |
| Identity interceptor Lambdas | the REQUEST interceptor, one function per Gateway running the same code. Each re-verifies the bearer token, removes caller-supplied identity keys and injects `_identity` | `src/gateway/identity_interceptor.py` |
| Amazon Cognito | the user pool whose tokens the relay, the API authorizer and the Gateways verify | `infra/modules/cognito/` |

## Agent workflow

![Agent workflow: the supervisor calls its direct read tools with arguments, and delegates to the research, mutation, analytics and composer subagents, each listed with the tools the registry grants it; each subagent's leaf calls go through the tool tier, formulate_and_invoke, which invokes the CRM action Lambdas; every delegation returns exactly one outcome to the supervisor: data, denied, tool_failure or no_match](figures/agent-architecture-workflow.svg)

- **Supervisor** (`src/agents/supervisor.py`). It runs on the supervisor tier and holds two kinds of
  tool. *Direct tools* (`DIRECT_TOOL_NAMES`) are the high-frequency reads the registry grants the
  `supervisor` role; the supervisor model writes their arguments itself. *Subagent tools*
  (`subagent_tool_schemas`) are one tool per subagent. Each takes a single `intent` string, and the
  subagent sees only that intent, not the conversation.
- **Subagents** (`src/agents/subagents/`). `research`, `mutation`, `analytics` and `composer` each
  have a purpose and the tools `tool_registry_for(role)` returns for that role. The figure lists
  them as generated from the registry. Only `mutation` holds tools that write, and a change a person
  must approve goes through `submit_proposal`. No tool lets an agent approve its own proposal.
  `src/agents/subagent_runner.py` runs one delegation as a subagent-tier model loop over exactly
  those tools.
- **Tool tier** (`src/agents/tool_actor.py`). Each of a subagent's leaf calls carries an intent. One
  tool-tier model call (`formulate_and_invoke`) turns that intent into the tool's arguments and
  dispatches the call. It returns the handler's response verbatim. The actor does not validate the
  arguments; the handler does, and its `validation_failed` response comes back unchanged.
- **Outcomes.** `classify_tool_result` sorts every delegation result into exactly one
  `SubagentOutcome` kind, and `SubagentOutcome.__post_init__` refuses any other combination:

  | Outcome | When | What the supervisor reports |
  |---|---|---|
  | `data` | successful results | the results, in call order, bounded by `bound_results` and marked `truncated` when cut |
  | `denied` | an error with code `scope_denied` or `contact_departed` | the handler's denial message and the missing scope |
  | `tool_failure` | any other error, including a tool-tier model that produced no usable call | the error message; no data is invented |
  | `no_match` | `not_found`, or an empty `items` list | that nothing matched |

  `synthesize_reply` renders the outcome. A denial or a failure is never turned into data. A
  `tool_failure` from a leaf call is cleared when a later call to the same tool succeeds; a denial is
  never cleared (`src/agents/subagent_runner.py`).
- **Prompts.** The supervisor and subagent system prompts are `config/agent_prompts/supervisor.yaml`
  and `config/agent_prompts/subagent.yaml` (`src/agents/agent_prompts.py`). Every model family gets
  the same prompts.

## Chat turn

![One chat turn: the SPA posts to /agent/* with the Cognito token in X-Crm-Authorization; CloudFront's OAC signs the request to the relay; the relay verifies the token and calls InvokeAgentRuntime with the verified claims as _agent_identity; the Runtime decodes the identity and drives the turn; the supervisor calls Bedrock, calls direct read tools on the action Lambdas, and delegates to a subagent, whose subagent-tier and tool-tier model calls lead to leaf tool calls on the action Lambdas; the handler result returns verbatim, the subagent returns one outcome, and the Runtime streams SSE frames that the relay re-emits verbatim back through CloudFront](figures/agent-architecture-chat-turn.svg)

1. The SPA posts the turn to `/agent/*`. It sends the Cognito token in `X-Crm-Authorization`, because
   CloudFront's OAC signs the origin request and overwrites `Authorization`
   (`infra/modules/frontend_cdn/cloudfront.tf`).
2. The relay verifies the token's signature, issuer, expiry and app client. The allowed client is
   read from the SSM parameter named by `COGNITO_AUDIENCE_PARAMETER` (`src/relay/src/jwt.ts`). This is
   the only authentication on the browser path. A rejected token gets a plain HTTP 401 before any
   stream opens (`src/relay/src/index.ts`).
3. The relay calls `InvokeAgentRuntime` with SigV4 and puts the verified claims in the payload as
   `_agent_identity` (`src/relay/src/runtime.ts`). It builds no identity and makes no authorization
   decision.
4. The Runtime keeps only that channel from the authenticated payload (`_authenticated_payload` in
   `src/agents/runtime_server.py`). It decodes the identity with `crm.envelope.decode_identity`
   (`resolve_identity`) and resolves every tier's tuning before the turn starts, so a bad tuning
   field fails fast (`resolve_tuning` in `src/agents/runtime_entrypoint.py`).
5. `drive_turn` runs the supervisor-tier loop. A direct read tool is dispatched with the supervisor's
   own arguments. A subagent tool runs a delegation, and each leaf call in it goes through the tool
   tier.
6. Every dispatch is a `lambda:InvokeFunction` on the tool's action Lambda. The event carries the
   user's identity on the `agent` channel, built by `crm.envelope.identity_event`, which first strips
   every identity key from the model-written arguments (`src/agents/tool_dispatch.py`). The handler
   re-checks scope exactly as it does for the web app.
7. The Runtime streams one SSE frame per `AgentEvent` (`src/agents/events.py`: `tool_call`,
   `tool_result`, `proposal`, `text_delta`, `usage`, `heartbeat`, `completed`, `error`). A
   `heartbeat` goes out after each quiet interval, so an idle stream stays open. The relay forwards
   each frame unchanged, including kinds its generated schema does not know.
8. The turn's caps (`CHAT_BUDGET` in `src/agents/budget.py`) bound tool calls and wall-clock time.
   A turn that runs out of budget ends with a `budget_exhausted` error event and keeps the partial
   work and usage. Events are recorded per session, so a client can resume a stream from a later
   event (`InMemorySessionStore`, `src/agents/runtime_entrypoint.py`). Conversation history is kept
   server-side, per session and owner (`src/agents/conversation_history.py`).

An MCP client takes the same path from the Runtime onward. The chat Gateway's interceptor verifies
the caller's token. Then `chat_with_supervisor` claims or verifies the conversation, builds
`_agent_identity` from the verified identity, and invokes the Runtime (`src/gateway/chat_target.py`).

## Identity channels

`crm.envelope.decode_identity` (`src/crm/envelope.py`) accepts exactly one of three channels and has no
default. An event with no recognised channel raises `AuthenticationFailed`.

| Channel | Key | Who writes it | Path |
|---|---|---|---|
| `apigw` | `requestContext.authorizer` claims | the API Gateway Cognito authorizer; the token must also be issued to an allowed app client | web app `/api/*` |
| `gateway` | `_identity` | the Gateway REQUEST interceptor, after it removes every caller-supplied identity key | MCP clients |
| `agent` | `_agent_identity` | the Runtime's tool dispatch, from the identity the relay or `chat_with_supervisor` forwarded; the eval harness writes it directly (`src/eval/harness.py`) | in-app agents |

A model cannot change whose identity a tool call runs as. `identity_event` removes every key in
`IDENTITY_CHANNEL_KEYS` from the arguments before it writes the trusted channel.

## Model tiers

| Tier | Runs | Configured in |
|---|---|---|
| `supervisor` | routing, direct reads, the reply | `config/models.yaml` (per family), `src/agents/supervisor.py` (`SUPERVISOR_TIER`) |
| `subagent` | one delegation's loop | `config/models.yaml`, `src/agents/subagents/base.py` (`SUBAGENT_TIER`) |
| `tool` | one leaf call's arguments | `config/models.yaml`, `src/agents/tool_actor.py` (`TOOL_TIER`) |

`config/models.yaml` names each family's model and default effort for each tier, and its
`default_family`. `config/model_capabilities.yaml` lists the effort values each model accepts.
`src/providers/factory.py` is the only place a family adapter is chosen. Both adapters reach Bedrock
through the vendor SDKs, signed with IAM credentials, and the project reads no API key. A user's saved
settings, or a turn's tuning block, can choose the family, model and effort per tier. A tier left unset
falls back as `config/models.yaml` describes.
