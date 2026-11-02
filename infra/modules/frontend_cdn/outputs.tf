/**
 * Distribution, bucket and log-bucket identifiers consumed by the root composition.
 */

output "distribution_id" {
  description = "ID of the CloudFront distribution."
  value       = aws_cloudfront_distribution.frontend.id
}

output "distribution_domain_name" {
  description = "CloudFront distribution domain name (e.g. `d111111abcdef8.cloudfront.net`)."
  value       = aws_cloudfront_distribution.frontend.domain_name
}

output "distribution_arn" {
  description = "ARN of the CloudFront distribution, used by the origin bucket policy's `AWS:SourceArn` condition."
  value       = aws_cloudfront_distribution.frontend.arn
}

output "origin_bucket_name" {
  description = "Name of the private S3 bucket that holds the frontend static build."
  value       = aws_s3_bucket.origin.id
}

output "origin_bucket_arn" {
  description = "ARN of the private S3 origin bucket."
  value       = aws_s3_bucket.origin.arn
}

output "logs_bucket_name" {
  description = "Name of the S3 bucket receiving S3 access logs and CloudFront access logs."
  value       = aws_s3_bucket.logs.id
}

output "kms_key_arn" {
  description = "ARN of the customer-managed KMS key encrypting the origin bucket."
  value       = aws_kms_key.frontend.arn
}
