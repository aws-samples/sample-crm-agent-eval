/**
 * Outputs consumed by the root composition, including the CloudWatch Logs CMK for other modules'
 * log groups to reuse.
 */

output "cloudwatch_logs_kms_key_arn" {
  description = "ARN of the customer-managed KMS key for CloudWatch Logs log-group encryption. Sibling modules' `aws_cloudwatch_log_group` resources pass this as `kms_key_id`."
  value       = aws_kms_key.logs.arn
}

output "cloudwatch_logs_kms_key_id" {
  description = "Key ID of the customer-managed KMS key for CloudWatch Logs log-group encryption."
  value       = aws_kms_key.logs.key_id
}

output "cloudtrail_arn" {
  description = "ARN of the CloudTrail trail for API activity."
  value       = aws_cloudtrail.this.arn
}

output "cloudtrail_s3_bucket_name" {
  description = "Name of the S3 bucket CloudTrail delivers log files to: the log source whose access logs land in `cloudtrail_access_logs_bucket_name`."
  value       = aws_s3_bucket.cloudtrail.id
}

output "cloudtrail_access_logs_bucket_name" {
  description = "Name of the S3 bucket receiving the CloudTrail delivery bucket's own S3 server access logs (`aws_s3_bucket_logging.cloudtrail`'s target). Distinct from `cloudtrail_s3_bucket_name`, which is the source of those logs."
  value       = aws_s3_bucket.cloudtrail_access_logs.id
}

output "cloudtrail_kms_key_arn" {
  description = "ARN of the customer-managed KMS key encrypting the CloudTrail S3 destination bucket."
  value       = aws_kms_key.cloudtrail.arn
}

output "eval_metric_namespace" {
  description = "CloudWatch custom metric namespace reserved for eval-run metrics; passthrough of `var.eval_metric_namespace`."
  value       = var.eval_metric_namespace
}
