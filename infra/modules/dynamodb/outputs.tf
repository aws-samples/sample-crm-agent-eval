/**
 * Table names/ARNs, GSI ARNs and the CMK ARN, consumed by the root composition and by
 * `infra/modules/lambda_action` for its per-table IAM and KMS grants.
 */

output "table_names" {
  description = "Map of logical table name to physical DynamoDB table name."
  value       = { for name, table in local.tables : name => table.name }
}

output "table_arns" {
  description = "Map of logical table name to table ARN."
  value       = { for name, table in local.tables : name => table.arn }
}

output "gsi_arns" {
  description = <<-EOT
    Map of logical table name to a map of GSI name to that index's full ARN
    (`<table_arn>/index/<gsi_name>`). Consumed by `infra/main.tf`'s per-tool
    `dynamodb_grants[].index_arns`: every `dynamodb:Query` in this schema targets a named GSI (see
    `crm.store.store.query_gsi`), so the grant names the index ARN.
  EOT
  value = {
    for name, table in local.tables : name => {
      for gsi in table.global_secondary_index : gsi.name => "${table.arn}/index/${gsi.name}"
    }
  }
}

output "table_stream_arns" {
  description = "Map of logical table name to stream ARN (null where streams are disabled)."
  value       = { for name, table in local.tables : name => table.stream_arn }
}

output "kms_key_arn" {
  description = "ARN of the customer-managed KMS key encrypting all eleven tables."
  value       = aws_kms_key.dynamodb.arn
}

output "kms_key_id" {
  description = "Key ID of the customer-managed KMS key encrypting all eleven tables."
  value       = aws_kms_key.dynamodb.key_id
}
