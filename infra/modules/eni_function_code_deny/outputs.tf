output "policy_arn" {
  description = "ARN of the ENI function-code Deny policy, passed to every `lambda_action` and `agent_relay` instantiation."
  value       = aws_iam_policy.this.arn
}
