/**
 * Function, role, log group and matrix-row outputs, consumed by the root composition to wire API
 * routes and Gateway targets.
 */

output "function_name" {
  description = "The deployed Lambda function name."
  value       = aws_lambda_function.this.function_name
}

output "function_arn" {
  description = "The deployed Lambda function ARN."
  value       = aws_lambda_function.this.arn
}

output "invoke_arn" {
  description = "The function's invoke ARN, for wiring an API Gateway integration or a Gateway target."
  value       = aws_lambda_function.this.invoke_arn
}

output "role_name" {
  description = "This instantiation dedicated execution role name."
  value       = aws_iam_role.this.name
}

output "role_arn" {
  description = "This instantiation's dedicated execution role ARN."
  value       = aws_iam_role.this.arn
}

output "log_group_name" {
  description = "This instantiation's CloudWatch Logs group name."
  value       = aws_cloudwatch_log_group.this.name
}

output "table_action_role_matrix_rows" {
  description = <<-EOT
    This instantiation's rows in the project-wide table × action × role matrix,
    with `role` filled in from the actual generated role
    name. The root composition collects every instantiation's rows (via
    `flatten([for m in module.lambda_action : m.table_action_role_matrix_rows])`)
    into one machine-readable file for the consolidated Layer 3 gate to assert
    against.
  EOT
  value       = local.matrix_rows
}
