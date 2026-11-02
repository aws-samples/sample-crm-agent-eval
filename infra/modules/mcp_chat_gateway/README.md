# `mcp_chat_gateway` module

Gateway #2: a separate AgentCore MCP Gateway exposing exactly one tool, `chat_with_supervisor`,
backed by the `chat_target` Lambda. It uses the same mechanism as Gateway #1 ([`modules/agentcore`](../agentcore/README.md)):
a `CUSTOM_JWT` authorizer over the same Cognito pool and app clients, plus the same REQUEST identity
interceptor. Its scope is its own, so a token admitted here is not admitted by Gateway #1.

The `chat_target` Lambda, its `InvokeAgentRuntime` policy and the root output `mcp_chat_gateway_url`
are declared at the root in `infra/mcp_chat_gateway.tf`, not in this module.

See also: the [documentation map](../../../README.md#documentation), the
[runbook](../../../docs/runbook.md#rotating-the-mcp-app-client-secret) and the [security
baseline](../../../docs/security-baseline.md). Modules named here:
[`agentcore`](../agentcore/README.md), [`cognito`](../cognito/README.md).

## Contents

| File | Declares |
|---|---|
| `main.tf` | the gateway's execution role, trust policy and permissions; `aws_bedrockagentcore_gateway.this`; `aws_bedrockagentcore_gateway_target.chat`; the interceptor's invoke permission for this gateway |
| `variables.tf`, `outputs.tf`, `versions.tf` | interface and provider pin (`hashicorp/aws` 6.71.0) |

## Inputs

| Variable | Default | Notes |
|---|---|---|
| `gateway_name` | required | |
| `role_name` | required | the gateway's own execution role |
| `aws_region` | required | also the region in the trust policy's `aws:SourceArn` pattern |
| `account_id` | required | 12 digits; used by `aws:SourceAccount`, `aws:SourceArn` and the interceptor permission's `source_account` |
| `cognito_discovery_url` | required | the same discovery URL as Gateway #1 |
| `cognito_allowed_client_ids` | required, non-empty | `module.cognito.chat_gateway_allowed_client_ids`: Gateway #1's clients plus the MCP OAuth client |
| `cognito_allowed_scopes` | required, non-empty | the chat scope (`module.cognito.chat_scope_identifier`), never `gateway/invoke` |
| `identity_interceptor_lambda_arn`, `identity_interceptor_lambda_function_name` | required | the shared REQUEST interceptor |
| `chat_target_lambda_arn` | required | the only Lambda target |
| `tags` | `{}` | the root passes `data-classification` |

## Outputs

`gateway_url`, `gateway_id`, `gateway_arn`, `role_arn`, `role_name`. The chat target's resource policy
admits `role_arn`, and no other gateway role.

## IAM

| Principal | Allowed | Not allowed |
|---|---|---|
| Gateway #2 role | `lambda:InvokeFunction` on the chat target and the interceptor | every action Lambda; anything else |
| Gateway #2 role trust | `bedrock-agentcore.amazonaws.com`, with `aws:SourceAccount` = this account and `ArnLike aws:SourceArn` = `arn:aws:bedrock-agentcore:<region>:<account>:gateway/*` | other services and accounts |
| Interceptor permission | `bedrock-agentcore.amazonaws.com` with `source_arn` = this gateway and `source_account` set, under its own `statement_id` | a wildcard source |

The trust condition is a `gateway/*` pattern because the gateway needs `role_arn` when it is created.
Pinning the gateway's own ARN would create a dependency cycle.

## Target tool

`chat_with_supervisor` takes `message` (string, required) and `conversation_id` (string, optional).
The target handler (`src/gateway/chat_target.py`) checks the length and pattern limits. The MCP schema
only declares the property types.

## Verification

- Plan time: `CRM_REQUIRE_TERRAFORM=1 .venv/bin/python -m pytest tests/unit/test_mcp_chat_gateway_plan.py`.
  This plans the module and the root file `infra/mcp_chat_gateway.tf` offline, with mock credentials.
  It asserts that Gateway #1's scope is unchanged and Gateway #2's is different.
- Deployed (Layer 5): `scripts/smoke/mcp_chat_gateway.py` checks that `tools/list` returns only
  `chat_with_supervisor`, that a call with no JWT gets 401, and that a token whose signature is
  replaced is refused (401 or 403, with no `tools/list` result) while the same claims unaltered are
  answered. It checks that the chat-scoped user token is refused by Gateway #1 and a
  `gateway/invoke`-only token by Gateway #2, and that another user's conversation is refused. The
  role simulations for both Gateway roles and `chat_target` are Layer 4's
  `scripts/smoke/chat_path_iam.py`.

## Common failures

- **`CreateGateway` rejects empty `allowedScopes`.** The variable validation catches this at plan.
- **Every call fails closed at the interceptor.** Check both of the gateway's routes to the
  interceptor: the role's `InvokeInterceptor` grant, and the service-principal permission whose
  `source_arn` is this gateway.
