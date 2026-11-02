output "function_name" {
  description = "The relay function's name. Consumed by `modules/frontend_cdn` to attach the CloudFront-scoped `lambda:InvokeFunctionUrl` permission."
  value       = aws_lambda_function.this.function_name
}

output "function_arn" {
  description = "The relay function's ARN."
  value       = aws_lambda_function.this.arn
}

output "role_arn" {
  description = "The relay execution role's ARN. Consumed by the Layer 4 `simulate-principal-policy` check."
  value       = aws_iam_role.this.arn
}

output "role_name" {
  description = "The relay execution role's name."
  value       = aws_iam_role.this.name
}

output "log_group_name" {
  description = "The relay's CloudWatch log group name."
  value       = aws_cloudwatch_log_group.this.name
}

output "function_url" {
  description = "The Function URL, scheme and trailing slash included, as Lambda returns it."
  value       = aws_lambda_function_url.this.function_url
}

output "function_url_domain" {
  description = "Host portion of the Function URL, for use as a CloudFront custom-origin `domain_name` (which rejects a scheme or a path)."
  value       = replace(replace(aws_lambda_function_url.this.function_url, "https://", ""), "/", "")
}

output "environment_variable_names" {
  description = "Names of every environment variable this module sets on the relay. Lets a contract test assert the config surface in both directions — nothing the relay reads is unset, nothing set is unread."
  value       = sort(keys(local.environment_variables))
}
