output "bucket_name" {
  description = "Name of the eval-artefacts bucket, which embeds the account id. Read by application code and the smoke suite."
  value       = aws_s3_bucket.this.id
}

output "bucket_arn" {
  description = "ARN of the eval-artefacts bucket, for a consumer composing its own policy against it."
  value       = aws_s3_bucket.this.arn
}

output "artifact_prefix" {
  description = "The single key prefix artefacts are written under, and the only prefix the writer grant covers."
  value       = var.artifact_prefix
}
