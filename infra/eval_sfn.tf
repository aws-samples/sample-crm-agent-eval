/**
 * eval_sweep — the Standard state machine that runs one eval sweep, and its two
 * Lambdas on `eval_trigger.zip`: `sweep_control` (start, seed, item_failed, aggregate, teardown,
 * finalize) and `sweep_task` (one golden task through the AgentCore Runtime's `eval_turn` mode).
 *
 * Started only by `eval_trigger` (`states:StartExecution` on `local.eval_sweep_state_machine_arn`,
 * granted in `main.tf`). The `Resource: "*"` Logs and X-Ray statements are SFN-LOGS-1 and
 * SFN-XRAY-1 in `docs/security-baseline.md`.
 */

locals {
  eval_sweep_state_machine_name = "${local.name_prefix}-eval-sweep"

  # Constructed, so `eval_trigger`'s policy and the role trust below render at plan time.
  eval_sweep_state_machine_arn     = "arn:aws:states:${var.aws_region}:${data.aws_caller_identity.current.account_id}:stateMachine:${local.eval_sweep_state_machine_name}"
  eval_sweep_execution_arn_pattern = "arn:aws:states:${var.aws_region}:${data.aws_caller_identity.current.account_id}:execution:${local.eval_sweep_state_machine_name}:*"

  # `eval_runs` key families: `job#<run_id>` job rows, `lock#sweep`, and the bare run row
  # `record_run_started` writes, keyed by `eval.tenancy.allocate_run_id` (32 hex characters).
  eval_run_row_key_pattern = join("", [for i in range(32) : "?"])

  # The Step Functions logs-delivery actions, which accept only `Resource: "*"` (SFN-LOGS-1).
  sfn_vended_log_delivery_actions = [
    "logs:CreateLogDelivery",
    "logs:CreateLogStream",
    "logs:DeleteLogDelivery",
    "logs:DescribeLogGroups",
    "logs:DescribeResourcePolicies",
    "logs:GetLogDelivery",
    "logs:ListLogDeliveries",
    "logs:PutLogEvents",
    "logs:PutResourcePolicy",
    "logs:UpdateLogDelivery",
  ]

  # The Step Functions X-Ray tracing actions, which support no resource-level scoping (SFN-XRAY-1).
  sfn_xray_tracing_actions = [
    "xray:GetSamplingRules",
    "xray:GetSamplingTargets",
    "xray:PutTelemetryRecords",
    "xray:PutTraceSegments",
  ]

  # Same {table, actions} shape as `local.tool_dynamodb_grants`. Golden-task `GetItem` is
  # conditioned on `local.sweep_seed_key_patterns`; seeding writes only through `BatchWriteItem`, and
  # the unconditioned tenant-table base-table grants are SWEEP-CONTROL-1 in
  # `docs/security-baseline.md`. Every Query is `gsi_tenant`: rows under `eval-*` and `canary`
  # (teardown's emptiness check), and only `Select = COUNT` under `demo` (the baseline and delta
  # reads).
  sweep_seed_key_patterns = {
    accounts      = "acct-eval-*"
    contacts      = "cont-eval-*"
    opportunities = "opp-eval-*"
    activities    = "act-eval-*"
    notes         = "note-eval-*"
  }
  sweep_control_tenant_query_keys = ["eval-*", "canary"]
  sweep_control_demo_select       = ["COUNT"]
  sweep_control_dynamodb_grants = [
    { table = "eval_runs", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem"], index_names = [], leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = ["job#*"] } },
    { table = "eval_runs", actions = ["dynamodb:GetItem", "dynamodb:PutItem"], index_names = [], leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = [local.eval_run_row_key_pattern] } },
    { table = "eval_runs", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"], index_names = [], leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringEquals", values = ["lock#sweep"] } },
    { table = "eval_results", actions = ["dynamodb:GetItem", "dynamodb:PutItem"], index_names = [] },

    { table = "accounts", actions = ["dynamodb:BatchWriteItem"], index_names = [] },
    { table = "contacts", actions = ["dynamodb:BatchWriteItem"], index_names = [] },
    { table = "opportunities", actions = ["dynamodb:BatchWriteItem"], index_names = [] },
    { table = "activities", actions = ["dynamodb:BatchWriteItem"], index_names = [] },
    { table = "notes", actions = ["dynamodb:BatchWriteItem"], index_names = [] },
    { table = "audit_records", actions = ["dynamodb:BatchWriteItem"], index_names = [] },
    { table = "proposals", actions = ["dynamodb:BatchWriteItem"], index_names = [] },
    { table = "reps", actions = ["dynamodb:BatchWriteItem"], index_names = [], leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = ["eval-*"] } },
    { table = "usage_records", actions = ["dynamodb:BatchWriteItem"], index_names = [] },

    { table = "accounts", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = local.sweep_control_tenant_query_keys } },
    { table = "contacts", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = local.sweep_control_tenant_query_keys } },
    { table = "opportunities", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = local.sweep_control_tenant_query_keys } },
    { table = "activities", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = local.sweep_control_tenant_query_keys } },
    { table = "notes", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = local.sweep_control_tenant_query_keys } },
    { table = "audit_records", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = local.sweep_control_tenant_query_keys } },
    { table = "proposals", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = local.sweep_control_tenant_query_keys } },
    { table = "reps", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = local.sweep_control_tenant_query_keys } },
    { table = "usage_records", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = local.sweep_control_tenant_query_keys } },

    { table = "accounts", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringEquals", values = ["demo"] }, select_condition = local.sweep_control_demo_select },
    { table = "contacts", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringEquals", values = ["demo"] }, select_condition = local.sweep_control_demo_select },
    { table = "opportunities", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringEquals", values = ["demo"] }, select_condition = local.sweep_control_demo_select },
    { table = "activities", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringEquals", values = ["demo"] }, select_condition = local.sweep_control_demo_select },
    { table = "notes", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringEquals", values = ["demo"] }, select_condition = local.sweep_control_demo_select },
    { table = "audit_records", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringEquals", values = ["demo"] }, select_condition = local.sweep_control_demo_select },
    { table = "proposals", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringEquals", values = ["demo"] }, select_condition = local.sweep_control_demo_select },
    { table = "reps", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringEquals", values = ["demo"] }, select_condition = local.sweep_control_demo_select },
    { table = "usage_records", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringEquals", values = ["demo"] }, select_condition = local.sweep_control_demo_select },
  ]

  # Reads for scoring and `capture_pre_turn_values`; `record_item`'s transaction is per-item
  # PutItem on `eval_results` and UpdateItem on the job row, and its Conflict fallback a GetItem on
  # `eval_results`.
  sweep_task_tenant_tables             = ["accounts", "contacts", "opportunities", "activities", "notes"]
  sweep_task_unconditioned_read_tables = ["proposals"]
  sweep_task_dynamodb_grants = concat(
    [
      { table = "eval_runs", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem"], index_names = [], leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = ["job#*"] } },
      { table = "eval_results", actions = ["dynamodb:GetItem", "dynamodb:PutItem"], index_names = [] },
      { table = "accounts", actions = ["dynamodb:GetItem"], index_names = [], leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = [local.sweep_seed_key_patterns["accounts"]] } },
      { table = "contacts", actions = ["dynamodb:GetItem"], index_names = [], leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = [local.sweep_seed_key_patterns["contacts"]] } },
      { table = "opportunities", actions = ["dynamodb:GetItem"], index_names = [], leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = [local.sweep_seed_key_patterns["opportunities"]] } },
      { table = "activities", actions = ["dynamodb:GetItem"], index_names = [], leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = [local.sweep_seed_key_patterns["activities"]] } },
      { table = "notes", actions = ["dynamodb:GetItem"], index_names = [], leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = [local.sweep_seed_key_patterns["notes"]] } },
    ],
    [for table in local.sweep_task_unconditioned_read_tables : { table = table, actions = ["dynamodb:GetItem"], index_names = [] }],
    [for table in local.sweep_task_tenant_tables : { table = table, actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = ["eval-*"] } }],
  )

  sweep_control_granted_tables = distinct([for grant in local.sweep_control_dynamodb_grants : grant.table])
  sweep_task_granted_tables    = distinct([for grant in local.sweep_task_dynamodb_grants : grant.table])
}

# ---------------------------------------------------------------------------
# sweep_control
# ---------------------------------------------------------------------------

module "lambda_action_sweep_control" {
  source = "./modules/lambda_action"

  eni_function_code_deny_policy_arn = module.eni_function_code_deny.policy_arn

  function_name = "crm-agent-eval-sweep-control"

  vpc_subnet_ids         = module.vpc.lambda_subnet_ids
  vpc_security_group_ids = module.vpc.lambda_security_group_ids
  handler                = "eval.sfn_handlers.sweep_control_handler"
  runtime                = "python3.13"
  filename               = local.eval_lambda_zip
  source_code_hash       = filebase64sha256(local.eval_lambda_zip)

  timeout_seconds = 120
  memory_size_mb  = 1024

  environment_variables = {
    CRM_TABLE_PREFIX      = local.name_prefix
    PYTHONPATH            = "/var/task/src"
    EVAL_ARTIFACTS_BUCKET = module.eval_artifacts.bucket_name
    EVAL_ARTIFACTS_PREFIX = module.eval_artifacts.artifact_prefix
  }
  environment_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn

  reserved_concurrent_executions = local.reserved_concurrency["sweep_control"]

  dynamodb_grants = [
    for grant in local.sweep_control_dynamodb_grants : {
      table_name            = grant.table
      table_arn             = module.dynamodb.table_arns[grant.table]
      actions               = grant.actions
      include_table_arn     = try(grant.include_table_arn, true)
      leading_key_condition = try(grant.leading_key_condition, null)
      select_condition      = try(grant.select_condition, [])
      index_arns = try(
        [for n in grant.index_names : module.dynamodb.gsi_arns[grant.table][n]],
        contains(grant.actions, "dynamodb:Query") ? values(module.dynamodb.gsi_arns[grant.table]) : []
      )
    }
  ]

  denied_table_arns = flatten([
    for table_name, table_arn in module.dynamodb.table_arns : [table_arn, "${table_arn}/index/*"]
    if !contains(local.sweep_control_granted_tables, table_name)
  ])

  denied_own_table_actions = [
    for table_name in local.sweep_control_granted_tables : {
      table_name = table_name
      table_arn  = module.dynamodb.table_arns[table_name]
      actions = setsubtract(
        local.dynamodb_asserted_actions,
        flatten([for g in local.sweep_control_dynamodb_grants : g.actions if g.table == table_name])
      )
      index_arns = ["${module.dynamodb.table_arns[table_name]}/index/*"]
    }
  ]

  dynamodb_policy_managed              = true
  dynamodb_allow_statements_per_policy = 20

  dynamodb_kms_key_arn       = module.dynamodb.kms_key_arn
  dynamodb_kms_grant_enabled = true

  # Invoked only by the state machine's role.
  gateway_target_role_arn = null

  tags = {
    "data-classification" = var.data_classification
  }
}

resource "aws_iam_role_policy" "sweep_control_artifacts" {
  name = "${local.name_prefix}-sweep-control-artifacts"
  role = module.lambda_action_sweep_control.role_name

  policy = data.aws_iam_policy_document.sweep_control_artifacts.json
}

data "aws_iam_policy_document" "sweep_control_artifacts" {
  statement {
    sid       = "WriteAndReadArtifactPrefix"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${module.eval_artifacts.bucket_arn}/${module.eval_artifacts.artifact_prefix}/*"]
  }

  statement {
    sid       = "UseArtifactsBucketCmk"
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.eval_artifacts.arn]
  }
}

# ---------------------------------------------------------------------------
# sweep_task
# ---------------------------------------------------------------------------

module "lambda_action_sweep_task" {
  source = "./modules/lambda_action"

  eni_function_code_deny_policy_arn = module.eni_function_code_deny.policy_arn

  function_name = "crm-agent-eval-sweep-task"

  vpc_subnet_ids         = module.vpc.lambda_subnet_ids
  vpc_security_group_ids = module.vpc.lambda_security_group_ids
  handler                = "eval.sfn_handlers.sweep_task_handler"
  runtime                = "python3.13"
  filename               = local.eval_lambda_zip
  source_code_hash       = filebase64sha256(local.eval_lambda_zip)

  timeout_seconds = 300
  memory_size_mb  = 1024

  environment_variables = {
    CRM_TABLE_PREFIX        = local.name_prefix
    PYTHONPATH              = "/var/task/src"
    AGENT_RUNTIME_ARN       = module.agentcore.agent_runtime_arn
    AGENT_RUNTIME_QUALIFIER = local.relay_runtime_qualifier
  }
  environment_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn

  reserved_concurrent_executions = local.reserved_concurrency["sweep_task"]

  dynamodb_grants = [
    for grant in local.sweep_task_dynamodb_grants : {
      table_name            = grant.table
      table_arn             = module.dynamodb.table_arns[grant.table]
      actions               = grant.actions
      include_table_arn     = try(grant.include_table_arn, true)
      leading_key_condition = try(grant.leading_key_condition, null)
      index_arns            = [for n in grant.index_names : module.dynamodb.gsi_arns[grant.table][n]]
    }
  ]

  denied_table_arns = flatten([
    for table_name, table_arn in module.dynamodb.table_arns : [table_arn, "${table_arn}/index/*"]
    if !contains(local.sweep_task_granted_tables, table_name)
  ])

  denied_own_table_actions = [
    for table_name in local.sweep_task_granted_tables : {
      table_name = table_name
      table_arn  = module.dynamodb.table_arns[table_name]
      actions = setsubtract(
        local.dynamodb_asserted_actions,
        flatten([for g in local.sweep_task_dynamodb_grants : g.actions if g.table == table_name])
      )
      index_arns = ["${module.dynamodb.table_arns[table_name]}/index/*"]
    }
  ]

  dynamodb_policy_managed = true

  dynamodb_kms_key_arn       = module.dynamodb.kms_key_arn
  dynamodb_kms_grant_enabled = true

  gateway_target_role_arn = null

  tags = {
    "data-classification" = var.data_classification
  }
}

resource "aws_iam_role_policy" "sweep_task_invoke_runtime" {
  name = "${local.name_prefix}-sweep-task-invoke-runtime"
  role = module.lambda_action_sweep_task.role_name

  policy = data.aws_iam_policy_document.sweep_task_invoke_runtime.json
}

# Both ARNs: the Runtime's resource policies are on the runtime and its DEFAULT endpoint.
data "aws_iam_policy_document" "sweep_task_invoke_runtime" {
  statement {
    sid       = "InvokeAgentRuntimeEvalTurn"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [module.agentcore.agent_runtime_arn, local.agent_runtime_endpoint_arn]
  }
}

# ---------------------------------------------------------------------------
# The state machine, its role and its log group.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "eval_sweep_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [local.eval_sweep_state_machine_arn]
    }
  }
}

resource "aws_iam_role" "eval_sweep" {
  name               = "${local.eval_sweep_state_machine_name}-role"
  assume_role_policy = data.aws_iam_policy_document.eval_sweep_assume_role.json

  tags = {
    "data-classification" = var.data_classification
  }
}

data "aws_iam_policy_document" "eval_sweep_permissions" {
  # checkov:skip=CKV_AWS_111: CloudWatch Logs vended-delivery and X-Ray tracing actions accept only Resource "*" (SFN-LOGS-1, SFN-XRAY-1)
  # checkov:skip=CKV_AWS_356: CloudWatch Logs vended-delivery and X-Ray tracing actions accept only Resource "*" (SFN-LOGS-1, SFN-XRAY-1)
  statement {
    sid     = "InvokeSweepLambdas"
    effect  = "Allow"
    actions = ["lambda:InvokeFunction"]
    resources = [
      module.lambda_action_sweep_control.function_arn,
      "${module.lambda_action_sweep_control.function_arn}:*",
      module.lambda_action_sweep_task.function_arn,
      "${module.lambda_action_sweep_task.function_arn}:*",
    ]
  }

  statement {
    sid       = "SfnVendedLogDelivery"
    effect    = "Allow"
    actions   = local.sfn_vended_log_delivery_actions
    resources = ["*"]
  }

  statement {
    sid       = "SfnXRayTracing"
    effect    = "Allow"
    actions   = local.sfn_xray_tracing_actions
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "eval_sweep" {
  name   = "${local.eval_sweep_state_machine_name}-permissions"
  role   = aws_iam_role.eval_sweep.name
  policy = data.aws_iam_policy_document.eval_sweep_permissions.json
}

resource "aws_cloudwatch_log_group" "eval_sweep" {
  name              = "/aws/vendedlogs/states/${local.eval_sweep_state_machine_name}"
  retention_in_days = 365
  kms_key_id        = module.observability.cloudwatch_logs_kms_key_arn

  tags = {
    "data-classification" = var.data_classification
  }
}

resource "aws_sfn_state_machine" "eval_sweep" {
  # checkov:skip=CKV_AWS_285: include_execution_data = false keeps identity claims out of the logs
  name     = local.eval_sweep_state_machine_name
  type     = "STANDARD"
  role_arn = aws_iam_role.eval_sweep.arn

  definition = templatefile("${path.module}/templates/eval_sweep.asl.json", {
    sweep_control_arn = module.lambda_action_sweep_control.function_arn
    sweep_task_arn    = module.lambda_action_sweep_task.function_arn
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.eval_sweep.arn}:*"
    include_execution_data = false
    level                  = "ALL"
  }

  tracing_configuration {
    enabled = true
  }

  tags = {
    "data-classification" = var.data_classification
  }

  depends_on = [aws_iam_role_policy.eval_sweep]
}

output "eval_state_machine_arn" {
  description = "ARN of the eval_sweep Step Functions state machine, started by eval_trigger."
  value       = aws_sfn_state_machine.eval_sweep.arn
}
