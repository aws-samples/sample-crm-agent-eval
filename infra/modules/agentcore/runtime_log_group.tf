/**
 * Pre-created CloudWatch Logs group for the AgentCore Runtime's default endpoint, at AWS's naming
 * convention `/aws/bedrock-agentcore/runtimes/{agentRuntimeId}-DEFAULT`, so retention, KMS key and
 * tags are set before the service would otherwise create it on first invocation.
 */

resource "aws_cloudwatch_log_group" "runtime" {
  name              = "/aws/bedrock-agentcore/runtimes/${aws_bedrockagentcore_agent_runtime.this.agent_runtime_id}-DEFAULT"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn

  tags = merge(var.tags, {
    "data-classification" = var.data_classification
  })
}
