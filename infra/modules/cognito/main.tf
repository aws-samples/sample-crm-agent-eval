/**
 * Cognito user pool: MFA, password policy, account recovery, the custom scope attributes, the V2
 * pre-token-generation trigger, and three app clients (SPA public client, MCP confidential client,
 * MCP OAuth public client).
 *
 *   - `write_attributes` on every client excludes every scope-bearing custom attribute, so a
 *     caller's own access token cannot change its scope via `UpdateUserAttributes`.
 *   - `user_pool_tier = "ESSENTIALS"` is paired with `lambda_version = "V2_0"` on the trigger; the
 *     Lite tier does not fire the V2 event.
 */

locals {
  # The claims the authorization model trusts (`crm.authz.Identity`, and the interceptor's verified
  # identity in `src/gateway/identity_interceptor.py`). A user's own access token must never write them.
  scope_bearing_attributes = [
    "custom:rep_id",
    "custom:book",
    "custom:full_visibility",
    "custom:tenant",
  ]

  # Non-scope-bearing profile attributes both app clients may read and write.
  writable_profile_attributes = [
    "email",
    "name",
  ]
}

resource "aws_cognito_user_pool" "this" {
  name = var.user_pool_name

  # The V2 pre-token-generation event requires Essentials or Plus; the provider default is Lite.
  user_pool_tier = "ESSENTIALS"

  # Defaults to "OPTIONAL": the SPA (`frontend/src/auth/CognitoAuthSession.ts`) implements no TOTP
  # enrollment, so an unenrolled user signs in with a password, while an enrolled user is still
  # challenged. `check_cognito_mfa` accepts "OPTIONAL" and fails "OFF". Set `var.mfa_configuration`
  # to "ON" to require MFA.
  mfa_configuration = var.mfa_configuration

  # TOTP is the only second factor offered (no SMS).
  software_token_mfa_configuration {
    enabled = true
  }

  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # No public SignUp; users are created only with AdminCreateUser.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # Custom attributes carrying the authorization scope. Mutable so the trigger's admin path can
  # update them; `write_attributes` below excludes them from user writes.
  schema {
    name                = "rep_id"
    attribute_data_type = "String"
    mutable             = true
    string_attribute_constraints {
      min_length = "1"
      max_length = "128"
    }
  }

  schema {
    name                = "book"
    attribute_data_type = "String"
    mutable             = true
    string_attribute_constraints {
      min_length = "0"
      max_length = "2048"
    }
  }

  schema {
    name                = "full_visibility"
    attribute_data_type = "String"
    mutable             = true
    string_attribute_constraints {
      min_length = "1"
      max_length = "8"
    }
  }

  schema {
    name                = "tenant"
    attribute_data_type = "String"
    mutable             = true
    string_attribute_constraints {
      min_length = "1"
      max_length = "128"
    }
  }

  lambda_config {
    # lambda_version must be V2_0, paired with user_pool_tier above; the plan-time gate asserts both.
    pre_token_generation_config {
      lambda_arn     = var.pre_token_generation_lambda_arn
      lambda_version = "V2_0"
    }
  }
}

# Lets Cognito invoke the pre-token-generation Lambda, scoped to this user pool ARN and account.
resource "aws_lambda_permission" "pre_token_generation" {
  statement_id  = "AllowCognitoInvokePreTokenGeneration"
  action        = "lambda:InvokeFunction"
  function_name = var.pre_token_generation_lambda_function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.this.arn
}

# Cognito prefix domain, which provides the `/oauth2/token` endpoint the MCP `client_credentials`
# client uses without a custom certificate. The prefix must be globally unique, so it includes
# `user_pool_id`.
resource "aws_cognito_user_pool_domain" "this" {
  domain       = "${var.user_pool_name}-${lower(substr(aws_cognito_user_pool.this.id, -8, 8))}"
  user_pool_id = aws_cognito_user_pool.this.id
}

# SPA app client: public client (no secret), authorization-code + PKCE only.
resource "aws_cognito_user_pool_client" "spa" {
  name         = "crm-agent-eval-spa"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret = false

  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    # For the smoke tooling's `admin_initiate_auth` test-user sign-in (server-side, not a browser).
    # `allowed_oauth_flows` stays `["code"]` only.
    "ALLOW_ADMIN_USER_PASSWORD_AUTH",
  ]

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  callback_urls                        = var.spa_callback_urls
  logout_urls                          = var.spa_logout_urls
  supported_identity_providers         = ["COGNITO"]

  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  # Explicit allow-list excluding every entry in `local.scope_bearing_attributes`.
  read_attributes  = concat(local.writable_profile_attributes, local.scope_bearing_attributes)
  write_attributes = local.writable_profile_attributes
}

# Resource server defining the custom `invoke` scope the MCP `client_credentials` client requests
# as `<identifier>/invoke`. A `client_credentials` client cannot request OIDC scopes.
resource "aws_cognito_resource_server" "gateway" {
  user_pool_id = aws_cognito_user_pool.this.id
  identifier   = "gateway"
  name         = "AgentCore Gateway"

  scope {
    scope_name        = "invoke"
    scope_description = "Invoke the AgentCore Gateway MCP surface machine-to-machine."
  }
}

# Resource server for Gateway #2's own scope, `supervisor/chat`. The V2 pre-token trigger adds
# it to user access tokens (`scopesToAdd`); the MCP client is granted it for `client_credentials`.
resource "aws_cognito_resource_server" "supervisor" {
  user_pool_id = aws_cognito_user_pool.this.id
  identifier   = "supervisor"
  name         = "AgentCore chat Gateway"

  scope {
    scope_name        = "chat"
    scope_description = "Call chat_with_supervisor on the MCP chat Gateway."
  }
}

# Members are the only principals `crm.authz` treats as admin, read from `cognito:groups`.
resource "aws_cognito_user_group" "admin" {
  name         = "admin"
  user_pool_id = aws_cognito_user_pool.this.id
  description  = "CRM eval administrators: live sweeps, cancel and unlock."
}

# MCP app client: confidential client (`client_credentials` grant) for MCP clients calling the
# Gateway machine-to-machine. Its secret is generated by Cognito and output as sensitive.
resource "aws_cognito_user_pool_client" "mcp" {
  name         = "crm-agent-eval-mcp"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret = var.mcp_client_generate_secret

  explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH"]

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_scopes = [
    aws_cognito_resource_server.gateway.scope_identifiers[0],
    aws_cognito_resource_server.supervisor.scope_identifiers[0],
  ]
  supported_identity_providers = ["COGNITO"]

  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  # 5 minutes, Cognito's minimum access-token lifetime.
  access_token_validity = 5
  token_validity_units {
    access_token = "minutes"
  }

  # Same allow-list as the SPA client.
  read_attributes  = concat(local.writable_profile_attributes, local.scope_bearing_attributes)
  write_attributes = local.writable_profile_attributes
}

# MCP OAuth app client: public client (no secret), authorization code only, for interactive
# MCP clients (MCP Inspector, Claude Code `--callback-port 33418`) on Gateway #2. Loopback callbacks
# only, and never the `gateway/invoke` scope.
resource "aws_cognito_user_pool_client" "mcp_oauth" {
  name         = "crm-agent-eval-mcp-oauth"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret = false

  explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH"]

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes = [
    "openid",
    aws_cognito_resource_server.supervisor.scope_identifiers[0],
  ]
  callback_urls = [
    "http://localhost:6274/oauth/callback",
    "http://localhost:6274/oauth/callback/debug",
    "http://localhost:33418/callback",
  ]
  supported_identity_providers = ["COGNITO"]

  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  # Same allow-list as the SPA client.
  read_attributes  = concat(local.writable_profile_attributes, local.scope_bearing_attributes)
  write_attributes = local.writable_profile_attributes
}
