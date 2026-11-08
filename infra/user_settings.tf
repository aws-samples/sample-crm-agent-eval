/**
 * `POST /api/user_settings`: the per-user tuning settings handler
 * (`crm.handlers.user_settings.handler`) on `eval_trigger.zip`, routed through `module.api`'s
 * `extra_routes`. It reads and writes only the caller's `usersettings#*` item in `reps`.
 */

module "lambda_action_user_settings" {
  source = "./modules/lambda_action"

  eni_function_code_deny_policy_arn = module.eni_function_code_deny.policy_arn

  function_name = "${local.name_prefix}-user-settings"

  vpc_subnet_ids         = module.vpc.lambda_subnet_ids
  vpc_security_group_ids = module.vpc.lambda_security_group_ids
  handler                = "crm.handlers.user_settings.handler"
  runtime                = "python3.13"
  filename               = local.eval_lambda_zip
  source_code_hash       = filebase64sha256(local.eval_lambda_zip)

  timeout_seconds = 10
  memory_size_mb  = 256

  environment_variables = {
    CRM_TABLE_PREFIX                     = local.name_prefix
    CRM_API_ALLOWED_CLIENT_IDS_PARAMETER = local.api_allowed_client_ids_parameter_name
    # Required for this zip's layout: the first-party packages live under `src/`.
    PYTHONPATH = "/var/task/src"
  }
  environment_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn
  config_parameter_arns   = [local.api_allowed_client_ids_parameter_arn]

  reserved_concurrent_executions = local.reserved_concurrency["user_settings"]

  dynamodb_grants = [
    {
      table_name = "reps"
      table_arn  = module.dynamodb.table_arns["reps"]
      actions    = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
      leading_key_condition = {
        attribute = "dynamodb:LeadingKeys"
        operator  = "StringLike"
        values    = ["usersettings#*"]
      }
    },
  ]
  table_action_role_matrix_entries = [
    for action in ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"] : {
      table                 = "reps"
      action                = action
      role                  = "${local.name_prefix}-user-settings-role"
      leading_keys_enforced = true
      isolation_mechanism   = "leading_keys"
    }
  ]

  denied_table_arns = flatten([
    for table_name, table_arn in module.dynamodb.table_arns : [table_arn, "${table_arn}/index/*"]
    if table_name != "reps"
  ])
  denied_own_table_actions = [
    {
      table_name = "reps"
      table_arn  = module.dynamodb.table_arns["reps"]
      actions    = setsubtract(local.dynamodb_asserted_actions, ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"])
      index_arns = ["${module.dynamodb.table_arns["reps"]}/index/*"]
    },
  ]
  dynamodb_kms_key_arn       = module.dynamodb.kms_key_arn
  dynamodb_kms_grant_enabled = true

  # Routed at `POST /user_settings` through `module.api`'s `extra_routes`; not a Gateway target.
  gateway_target_role_arn = null
}
