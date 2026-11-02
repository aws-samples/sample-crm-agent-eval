# `cognito` module

Cognito user pool for `crm-agent-eval`: MFA, password policy, account recovery, admin-only user
creation, the authorization-scope custom attributes, the V2 pre-token-generation trigger, a prefix
domain, two resource servers, and three app clients.

See also: the [documentation map](../../../README.md#documentation), the
[runbook](../../../docs/runbook.md#rotating-the-mcp-app-client-secret) and the [security
baseline](../../../docs/security-baseline.md). Modules named here: [`api`](../api/README.md).

## Security properties

- **Users cannot rewrite their own scope.** All three app clients set `write_attributes` to `email`
  and `name` only, excluding `custom:rep_id`, `custom:book`, `custom:full_visibility` and
  `custom:tenant`. A user's own access token therefore cannot change those attributes through
  `UpdateUserAttributes`; they are writable only through `AdminUpdateUserAttributes` under an admin
  role, outside this module.
- **The V2 trigger fires.** The V2 pre-token-generation event requires the Essentials or Plus
  feature plan; on Lite it never fires and every token carries an empty scope.
  `user_pool_tier = "ESSENTIALS"` is pinned alongside `lambda_version = "V2_0"`.
- **No self sign-up.** `allow_admin_create_user_only = true`; users are created with
  `AdminCreateUser` only.
- **MFA.** TOTP is enabled; SMS is not. `mfa_configuration` defaults to `OPTIONAL` and accepts `ON`;
  `OFF` is rejected by validation. The SPA does not implement TOTP enrollment, so `ON` blocks
  interactive sign-in through the deployed frontend.
- **Password policy.** Minimum 14 characters with lowercase, uppercase, number and symbol;
  temporary passwords valid 7 days. Account recovery is by verified email only.
- **Trigger permission.** The `aws_lambda_permission` letting `cognito-idp.amazonaws.com` invoke the
  trigger is scoped by `source_arn` to this pool.

## Claim names matched

Matched against `src/gateway/identity_interceptor.py::_build_verified_identity`, which reads
`custom:rep_id`, `custom:book`, `custom:full_visibility`, and `custom:tenant` off the decoded
token claims. This module's `schema` blocks and `write_attributes` exclusion list use exactly those
four names, and `src/crm/auth/pre_token_generation.py` — the V2 trigger this module wires — stamps
exactly the same four.

Cognito fires that trigger for user sign-in flows only, never for `client_credentials`. A machine
token therefore carries no `custom:*` claim; the Gateway interceptor resolves a machine caller's
identity from an explicitly configured service-principal `rep_id` and that rep's `reps` item. That
path does not use this pool's schema.

## App clients

| Client | Type | Flows | Token lifetime |
|---|---|---|---|
| `aws_cognito_user_pool_client.spa` (`crm-agent-eval-spa`) | Public (`generate_secret = false`) | OAuth authorization code + PKCE (`allowed_oauth_flows = ["code"]`, scopes `openid`/`email`/`profile`); `ALLOW_USER_SRP_AUTH`, `ALLOW_REFRESH_TOKEN_AUTH`, `ALLOW_ADMIN_USER_PASSWORD_AUTH` | access/id 1 hour, refresh 30 days |
| `aws_cognito_user_pool_client.mcp` | Confidential (`generate_secret = var.mcp_client_generate_secret`, default `true`) | `client_credentials` only, scopes `gateway/invoke` and `supervisor/chat` | access 5 minutes (Cognito's minimum) |
| `aws_cognito_user_pool_client.mcp_oauth` (`crm-agent-eval-mcp-oauth`) | Public (`generate_secret = false`) | OAuth authorization code (`allowed_oauth_flows = ["code"]`), scopes `openid` and `supervisor/chat` only; callbacks `http://localhost:6274/oauth/callback`, `http://localhost:6274/oauth/callback/debug` (MCP Inspector), `http://localhost:33418/callback` (Claude Code `--callback-port 33418`); no logout URLs; `ALLOW_REFRESH_TOKEN_AUTH` | access/id 1 hour, refresh 30 days |

`ALLOW_ADMIN_USER_PASSWORD_AUTH` on the SPA client serves server-side smoke tooling that calls
`AdminInitiateAuth`; no password grant is added to the OAuth flows.

`client_credentials` cannot request OIDC scopes, so the MCP client authorizes two custom scopes, one
per AgentCore Gateway:

- `gateway/invoke` (`aws_cognito_resource_server.gateway`, output `gateway_scope_identifier`) is
  Gateway #1's `allowedScopes`.
- `supervisor/chat` (`aws_cognito_resource_server.supervisor`, output `chat_scope_identifier`) is
  Gateway #2's `allowedScopes` and its interceptor's `COGNITO_ALLOWED_SCOPES`.

The SPA client is granted neither. User access tokens get `supervisor/chat` only from the V2 pre-token
trigger's `scopesToAdd` (env `CHAT_SCOPE`). That needs this pool's `ESSENTIALS` tier and `V2_0`
trigger, so Gateway #1 refuses a user token.

`aws_cognito_user_group.admin` (output `admin_group_name`) is the admin group. `crm.authz` reads
membership from `cognito:groups` on the API channel only. Adding users to the group is an operator
action, not something this module's apply does.
`client_credentials` issues no refresh token, so an MCP client re-requests a token on expiry.

## Inputs

| Variable | Required | Notes |
|---|---|---|
| `pre_token_generation_lambda_arn` | yes | The V2 trigger Lambda (created elsewhere). |
| `pre_token_generation_lambda_function_name` | yes | Same function; scopes the invoke permission. |
| `spa_callback_urls` | yes | OAuth callback URLs, from the frontend CDN domain. |
| `spa_logout_urls` | yes | OAuth sign-out URLs. |
| `user_pool_name` | no (default `crm-agent-eval`) | Also the domain prefix base. |
| `mcp_client_generate_secret` | no (default `true`) | |
| `mfa_configuration` | no (default `OPTIONAL`) | `ON` or `OPTIONAL`. |

## Outputs

| Output | Description |
|---|---|
| `user_pool_id` | User pool id. |
| `user_pool_arn` | User pool ARN; the `api` module's authorizer input. |
| `user_pool_endpoint` | Pool endpoint, for OIDC discovery and JWKS URLs. |
| `spa_client_id` | SPA client id. |
| `mcp_client_id` | MCP client id. |
| `mcp_client_secret` | The MCP app client's credential. `sensitive`; held only in Terraform state and outputs, never in a tracked file. The stack creates no Secrets Manager entry for it; an operator may keep an out-of-band copy. |
| `allowed_client_ids` | SPA and MCP client ids: Gateway #1's `allowed_clients` and its interceptor's `COGNITO_ALLOWED_CLIENT_IDS`. Excludes `mcp_oauth`. |
| `mcp_oauth_client_id` | MCP OAuth client id (Gateway #2 only). |
| `chat_gateway_allowed_client_ids` | `allowed_client_ids` plus `mcp_oauth`: Gateway #2's `allowed_clients` and its interceptor's `COGNITO_ALLOWED_CLIENT_IDS`. |
| `domain` | Prefix domain (`<user_pool_name>-<last 8 chars of pool id>`), base of the `/oauth2/token` endpoint. |
| `gateway_scope_identifier` | `gateway/invoke`: Gateway #1's `allowedScopes` and its interceptor's `COGNITO_ALLOWED_SCOPES`. |
| `chat_scope_identifier` | `supervisor/chat`: Gateway #2's `allowedScopes`, its interceptor's scope, and (as the root literal `local.chat_scope_identifier`, held equal by a precondition) the pre-token trigger's `CHAT_SCOPE`. |
| `admin_group_name` | `admin`, the group whose members are admin. |

## Verification

```bash
cd infra && terraform fmt -check -recursive && terraform validate
cd infra/modules/cognito && terraform init -backend=false && terraform validate
```

These are syntax and type checks. `scripts/check_plan_security_baseline.py` asserts the
`write_attributes` exclusion, the tier/trigger-version pairing and the MFA setting at plan time;
`scripts/smoke/cognito_claims.py` and `scripts/smoke/mfa_user.py` check the deployed pool.

## Out of scope

- The pre-token-generation Lambda itself: created by the root composition.
- Storing `mcp_client_secret` anywhere besides Terraform state and outputs: no part of the stack
  does, and an out-of-band copy is the operator's (`docs/mcp-client-guide.md`).
