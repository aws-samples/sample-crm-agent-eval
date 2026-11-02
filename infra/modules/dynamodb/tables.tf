/**
 * The eleven entity tables, matching `src/crm/store/schema.py::TABLES` key for key and index for
 * index, so a Terraform-provisioned table and a test-provisioned one are interchangeable. Every
 * table: on-demand billing, SSE with the module's CMK, PITR, a `data-classification` tag (the other
 * mandatory tags come from the root provider's `default_tags`). Every key attribute is type "S".
 */

locals {
  # Logical table name -> resource, for the maps in outputs.tf.
  tables = {
    accounts      = aws_dynamodb_table.accounts
    contacts      = aws_dynamodb_table.contacts
    opportunities = aws_dynamodb_table.opportunities
    activities    = aws_dynamodb_table.activities
    notes         = aws_dynamodb_table.notes
    proposals     = aws_dynamodb_table.proposals
    audit_records = aws_dynamodb_table.audit_records
    usage_records = aws_dynamodb_table.usage_records
    reps          = aws_dynamodb_table.reps
    eval_runs     = aws_dynamodb_table.eval_runs
    eval_results  = aws_dynamodb_table.eval_results
  }
}

# accounts — PK account_id | gsi_owner (tenant_owner_rep_id / created_at) |
# gsi_tenant (tenant / name_lower)
resource "aws_dynamodb_table" "accounts" {
  name         = "${var.name_prefix}accounts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "account_id"

  attribute {
    name = "account_id"
    type = "S"
  }
  attribute {
    name = "tenant_owner_rep_id"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }
  attribute {
    name = "tenant"
    type = "S"
  }
  attribute {
    name = "name_lower"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi_owner"
    hash_key        = "tenant_owner_rep_id"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "account_id", "owner_rep_id", "name", "tenant",
    ]
  }

  global_secondary_index {
    name            = "gsi_tenant"
    hash_key        = "tenant"
    range_key       = "name_lower"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "account_id", "name", "tenant",
    ]
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    "data-classification" = var.data_classification
  }
}

# contacts — PK contact_id | gsi_account (tenant_account_id / created_at) |
# gsi_tenant (tenant / name_lower)
resource "aws_dynamodb_table" "contacts" {
  name         = "${var.name_prefix}contacts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "contact_id"

  attribute {
    name = "contact_id"
    type = "S"
  }
  attribute {
    name = "tenant_account_id"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }
  attribute {
    name = "tenant"
    type = "S"
  }
  attribute {
    name = "name_lower"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi_account"
    hash_key        = "tenant_account_id"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "contact_id", "account_id", "name", "status", "tenant",
    ]
  }

  global_secondary_index {
    name            = "gsi_tenant"
    hash_key        = "tenant"
    range_key       = "name_lower"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "contact_id", "account_id", "name", "tenant",
    ]
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    "data-classification" = var.data_classification
  }
}

# opportunities — PK opportunity_id | gsi_account (tenant_account_id / created_at) |
# gsi_owner_stage (tenant_owner_rep_id / stage_close_date) |
# gsi_stage (tenant_stage / close_date) | gsi_tenant (tenant / name_lower)
resource "aws_dynamodb_table" "opportunities" {
  name         = "${var.name_prefix}opportunities"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "opportunity_id"

  attribute {
    name = "opportunity_id"
    type = "S"
  }
  attribute {
    name = "tenant_account_id"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }
  attribute {
    name = "tenant_owner_rep_id"
    type = "S"
  }
  attribute {
    name = "stage_close_date"
    type = "S"
  }
  attribute {
    name = "tenant_stage"
    type = "S"
  }
  attribute {
    name = "close_date"
    type = "S"
  }
  attribute {
    name = "tenant"
    type = "S"
  }
  attribute {
    name = "name_lower"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi_account"
    hash_key        = "tenant_account_id"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "opportunity_id", "account_id", "name", "name_lower", "owner_rep_id", "stage", "close_date",
      "value", "tenant",
    ]
  }

  # The indexes crm.search._index_for selects among; each mirrors crm.store.schema's
  # OPPORTUNITY_SEARCH_PROJECTION.
  global_secondary_index {
    name            = "gsi_owner_stage"
    hash_key        = "tenant_owner_rep_id"
    range_key       = "stage_close_date"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "opportunity_id", "account_id", "name", "name_lower", "owner_rep_id", "stage", "close_date",
      "value", "tenant",
    ]
  }

  global_secondary_index {
    name            = "gsi_stage"
    hash_key        = "tenant_stage"
    range_key       = "close_date"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "opportunity_id", "account_id", "name", "name_lower", "owner_rep_id", "stage", "close_date",
      "value", "tenant",
    ]
  }

  global_secondary_index {
    name            = "gsi_tenant"
    hash_key        = "tenant"
    range_key       = "name_lower"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "opportunity_id", "account_id", "name", "owner_rep_id", "stage", "close_date", "value", "tenant",
    ]
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    "data-classification" = var.data_classification
  }
}

# activities — PK activity_id | gsi_opportunity (tenant_opportunity_id / occurred_at) |
# gsi_account (tenant_account_id / occurred_at) | gsi_tenant (tenant / created_at)
resource "aws_dynamodb_table" "activities" {
  name         = "${var.name_prefix}activities"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "activity_id"

  attribute {
    name = "activity_id"
    type = "S"
  }
  attribute {
    name = "tenant_opportunity_id"
    type = "S"
  }
  attribute {
    name = "occurred_at"
    type = "S"
  }
  attribute {
    name = "tenant_account_id"
    type = "S"
  }
  attribute {
    name = "tenant"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi_opportunity"
    hash_key        = "tenant_opportunity_id"
    range_key       = "occurred_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "activity_id", "opportunity_id", "kind", "subject", "body", "tenant",
    ]
  }

  global_secondary_index {
    name            = "gsi_account"
    hash_key        = "tenant_account_id"
    range_key       = "occurred_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "activity_id", "account_id", "opportunity_id", "kind", "subject", "tenant",
    ]
  }

  global_secondary_index {
    name            = "gsi_tenant"
    hash_key        = "tenant"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "activity_id", "opportunity_id", "kind", "subject", "tenant",
    ]
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    "data-classification" = var.data_classification
  }
}

# notes — PK note_id | gsi_opportunity (tenant_opportunity_id / created_at) |
# gsi_tenant (tenant / created_at)
resource "aws_dynamodb_table" "notes" {
  name         = "${var.name_prefix}notes"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "note_id"

  attribute {
    name = "note_id"
    type = "S"
  }
  attribute {
    name = "tenant_opportunity_id"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }
  attribute {
    name = "tenant"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi_opportunity"
    hash_key        = "tenant_opportunity_id"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "note_id", "opportunity_id", "body", "tenant",
    ]
  }

  global_secondary_index {
    name            = "gsi_tenant"
    hash_key        = "tenant"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "note_id", "opportunity_id", "body", "tenant",
    ]
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    "data-classification" = var.data_classification
  }
}

# proposals — PK proposal_id | gsi_status (tenant_status / created_at) |
# gsi_actor (tenant_actor_rep_id / created_at) | gsi_target (tenant_target_id / created_at) |
# gsi_tenant (tenant / created_at)
resource "aws_dynamodb_table" "proposals" {
  name         = "${var.name_prefix}proposals"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "proposal_id"

  attribute {
    name = "proposal_id"
    type = "S"
  }
  attribute {
    name = "tenant_status"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }
  attribute {
    name = "tenant_actor_rep_id"
    type = "S"
  }
  attribute {
    name = "tenant_target_id"
    type = "S"
  }
  attribute {
    name = "tenant"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi_status"
    hash_key        = "tenant_status"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "proposal_id", "status", "target_table", "target_id", "actor_rep_id", "tenant",
    ]
  }

  global_secondary_index {
    name            = "gsi_actor"
    hash_key        = "tenant_actor_rep_id"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "proposal_id", "actor_rep_id", "status", "target_table", "target_id", "tenant",
    ]
  }

  global_secondary_index {
    name            = "gsi_target"
    hash_key        = "tenant_target_id"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "proposal_id", "target_table", "target_id", "status", "tenant",
    ]
  }

  global_secondary_index {
    name            = "gsi_tenant"
    hash_key        = "tenant"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "proposal_id", "tenant",
    ]
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    "data-classification" = var.data_classification
  }
}

# audit_records — PK audit_id | gsi_target (tenant_target_id / created_at) |
# gsi_actor (tenant_actor_rep_id / created_at) |
# gsi_tenant (tenant / created_at)
resource "aws_dynamodb_table" "audit_records" {
  name         = "${var.name_prefix}audit_records"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "audit_id"

  attribute {
    name = "audit_id"
    type = "S"
  }
  attribute {
    name = "tenant_target_id"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }
  attribute {
    name = "tenant_actor_rep_id"
    type = "S"
  }
  attribute {
    name = "tenant"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi_target"
    hash_key        = "tenant_target_id"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "audit_id", "target_table", "target_id", "actor_rep_id", "reversed_at", "account_id", "status", "tenant",
    ]
  }

  global_secondary_index {
    name            = "gsi_actor"
    hash_key        = "tenant_actor_rep_id"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "audit_id", "actor_rep_id", "target_table", "target_id", "tenant",
    ]
  }

  global_secondary_index {
    name            = "gsi_tenant"
    hash_key        = "tenant"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "audit_id", "account_id", "target_id", "target_table", "status", "tenant",
    ]
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    "data-classification" = var.data_classification
  }
}

# usage_records — PK usage_id | gsi_correlation (tenant_correlation_id / created_at) |
# gsi_run (tenant_run_id / created_at) |
# gsi_tenant (tenant / created_at)
resource "aws_dynamodb_table" "usage_records" {
  name         = "${var.name_prefix}usage_records"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "usage_id"

  attribute {
    name = "usage_id"
    type = "S"
  }
  attribute {
    name = "tenant_correlation_id"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }
  attribute {
    name = "tenant_run_id"
    type = "S"
  }
  attribute {
    name = "tenant"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi_correlation"
    hash_key        = "tenant_correlation_id"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "usage_id", "correlation_id", "run_id", "tenant",
    ]
  }

  global_secondary_index {
    name            = "gsi_run"
    hash_key        = "tenant_run_id"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "usage_id", "run_id", "correlation_id", "tenant",
    ]
  }

  global_secondary_index {
    name            = "gsi_tenant"
    hash_key        = "tenant"
    range_key       = "created_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "usage_id", "tenant",
    ]
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    "data-classification" = var.data_classification
  }
}

# reps — PK rep_id | gsi_tenant (tenant / rep_id)
resource "aws_dynamodb_table" "reps" {
  name         = "${var.name_prefix}reps"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "rep_id"

  attribute {
    name = "rep_id"
    type = "S"
  }
  attribute {
    name = "tenant"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi_tenant"
    hash_key        = "tenant"
    range_key       = "rep_id"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "rep_id", "tenant",
    ]
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    "data-classification" = var.data_classification
  }
}

# eval_runs — PK run_id | gsi_status (status / started_at)
# Not tenant-prefixed: an index over the eval-run tenants themselves.
resource "aws_dynamodb_table" "eval_runs" {
  name         = "${var.name_prefix}eval_runs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"

  attribute {
    name = "run_id"
    type = "S"
  }
  attribute {
    name = "status"
    type = "S"
  }
  attribute {
    name = "started_at"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi_status"
    hash_key        = "status"
    range_key       = "started_at"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "tenant", "run_id", "status",
    ]
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    "data-classification" = var.data_classification
  }
}

# eval_results — PK run_id, SK result_id | gsi_task (run_id_task_id / arm)
# Not tenant-prefixed, like eval_runs.
resource "aws_dynamodb_table" "eval_results" {
  name         = "${var.name_prefix}eval_results"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"
  range_key    = "result_id"

  attribute {
    name = "run_id"
    type = "S"
  }
  attribute {
    name = "result_id"
    type = "S"
  }
  attribute {
    name = "run_id_task_id"
    type = "S"
  }
  attribute {
    name = "arm"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi_task"
    hash_key        = "run_id_task_id"
    range_key       = "arm"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "tenant", "run_id", "result_id", "arm",
    ]
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    "data-classification" = var.data_classification
  }
}
