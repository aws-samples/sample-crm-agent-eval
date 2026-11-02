/**
 * Pool identifiers consumed by the root composition, the interceptor's env vars, the `api`
 * module's authorizer and the smoke scripts.
 */

output "user_pool_id" {
  description = "Cognito user pool id."
  value       = aws_cognito_user_pool.this.id
}

output "user_pool_arn" {
  description = "Cognito user pool ARN."
  value       = aws_cognito_user_pool.this.arn
}

output "user_pool_endpoint" {
  description = "Cognito user pool endpoint, used to derive the OIDC discovery and JWKS URLs."
  value       = aws_cognito_user_pool.this.endpoint
}

output "spa_client_id" {
  description = "App client id for the SPA (public client, no secret)."
  value       = aws_cognito_user_pool_client.spa.id
}

output "mcp_client_id" {
  description = "App client id for MCP clients (confidential client)."
  value       = aws_cognito_user_pool_client.mcp.id
}

output "mcp_client_secret" {
  description = "App client secret for the MCP client. Sensitive; held in Terraform state and the gitignored results/tf-outputs.json, never in a tracked file. This stack creates no Secrets Manager entry for it."
  value       = aws_cognito_user_pool_client.mcp.client_secret
  sensitive   = true
}

output "allowed_client_ids" {
  description = "The SPA and MCP app client ids, for Gateway #1's authorizer and interceptor `COGNITO_ALLOWED_CLIENT_IDS`. Excludes the MCP OAuth client."
  value       = [aws_cognito_user_pool_client.spa.id, aws_cognito_user_pool_client.mcp.id]
}

output "mcp_oauth_client_id" {
  description = "App client id for interactive MCP OAuth clients (public client, no secret; Gateway #2 only)."
  value       = aws_cognito_user_pool_client.mcp_oauth.id
}

output "chat_gateway_allowed_client_ids" {
  description = "`allowed_client_ids` plus the MCP OAuth client: Gateway #2's authorizer `allowed_clients` and its interceptor's `COGNITO_ALLOWED_CLIENT_IDS`."
  value = [
    aws_cognito_user_pool_client.spa.id,
    aws_cognito_user_pool_client.mcp.id,
    aws_cognito_user_pool_client.mcp_oauth.id,
  ]
}

output "domain" {
  description = "Cognito user pool prefix domain (no custom ACM certificate). The base for the `/oauth2/token` token endpoint the MCP `client_credentials` flow mints against."
  value       = aws_cognito_user_pool_domain.this.domain
}

output "gateway_scope_identifier" {
  description = <<-EOT
    Fully-qualified `<identifier>/<scope_name>` custom scope
    (`aws_cognito_resource_server.gateway.scope_identifiers[0]`) that the MCP app client's
    `client_credentials` token carries, and the value the AgentCore Gateway's `CUSTOM_JWT` authorizer
    allows (`allowedScopes`). The root composition consumes this output for both.
  EOT
  value       = aws_cognito_resource_server.gateway.scope_identifiers[0]
}

output "chat_scope_identifier" {
  description = "Gateway #2's own `supervisor/chat` scope: its `CUSTOM_JWT` `allowedScopes`, the pre-token trigger's and interceptor's `CHAT_SCOPE`. Distinct from `gateway_scope_identifier`."
  value       = aws_cognito_resource_server.supervisor.scope_identifiers[0]
}

output "admin_group_name" {
  description = "Name of the Cognito group whose members are admin."
  value       = aws_cognito_user_group.admin.name
}
