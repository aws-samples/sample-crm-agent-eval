/**
 * Gateway #2 identifiers and its role, consumed by the root composition (the target Lambda's
 * invoke permission names `role_arn`) and the smoke scripts.
 */

output "gateway_url" {
  description = "MCP endpoint URL of Gateway #2."
  value       = aws_bedrockagentcore_gateway.this.gateway_url
}

output "gateway_id" {
  description = "Gateway #2 id."
  value       = aws_bedrockagentcore_gateway.this.gateway_id
}

output "gateway_arn" {
  description = "Gateway #2 ARN; the interceptor permission's `source_arn`."
  value       = aws_bedrockagentcore_gateway.this.gateway_arn
}

output "role_arn" {
  description = "Gateway #2 execution role ARN; the only Gateway principal admitted by the chat target's resource policy."
  value       = aws_iam_role.gateway.arn
}

output "role_name" {
  description = "Gateway #2 execution role name."
  value       = aws_iam_role.gateway.name
}
