/**
 * Outputs consumed by the root composition: runtime ARN, gateway URL, ECR repository URL and each
 * gateway target's id.
 */

output "ecr_repository_url" {
  description = "The ECR repository URL the agent container image is pushed to."
  value       = aws_ecr_repository.agent.repository_url
}

output "ecr_repository_arn" {
  description = <<-EOT
    ARN of the ECR repository the agent container image is pushed to. The root composition scopes the
    AgentCore Runtime execution role's `ecr:BatchGetImage`/`ecr:GetDownloadUrlForLayer` grant to this
    ARN.
  EOT
  value       = aws_ecr_repository.agent.arn
}

output "agent_runtime_arn" {
  description = "The deployed AgentCore Runtime ARN."
  value       = aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn
}

output "agent_runtime_id" {
  description = "The deployed AgentCore Runtime id."
  value       = aws_bedrockagentcore_agent_runtime.this.agent_runtime_id
}

output "gateway_id" {
  description = "The deployed Gateway id."
  value       = aws_bedrockagentcore_gateway.this.gateway_id
}

output "gateway_arn" {
  description = "The deployed Gateway ARN."
  value       = aws_bedrockagentcore_gateway.this.gateway_arn
}

output "gateway_url" {
  description = "The deployed Gateway's invocation URL."
  value       = aws_bedrockagentcore_gateway.this.gateway_url
}

output "gateway_target_ids" {
  description = "Map of tool_name -> that tool's gateway target id, one entry per entry in var.tool_lambda_arns."
  value       = { for name, target in aws_bedrockagentcore_gateway_target.tool : name => target.target_id }
}

output "agent_runtime_log_group_name" {
  description = "CloudWatch Logs group name for the AgentCore Runtime's default endpoint stdout."
  value       = aws_cloudwatch_log_group.runtime.name
}

output "agent_runtime_log_group_arn" {
  description = "CloudWatch Logs group ARN for the AgentCore Runtime's default endpoint stdout, for scoping the execution role's logs:* grant."
  value       = aws_cloudwatch_log_group.runtime.arn
}

output "codebuild_role_arn" {
  description = "ARN of the CodeBuild image-build project's service role, which reads the agent-source object and uses its CMK. The root admits it to the eval-artefacts bucket and key policies."
  value       = aws_iam_role.codebuild.arn
}
