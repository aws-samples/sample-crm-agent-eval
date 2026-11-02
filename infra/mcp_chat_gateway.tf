/**
 * Gateway #2: `module.mcp_chat_gateway`, its single target, the `chat_target` Lambda
 * (`gateway.chat_target.lambda_handler`, `chat_target.zip`), which calls the AgentCore Runtime with
 * the chat budget, and its own REQUEST interceptor instance, the identity interceptor's code
 * configured with the chat scope. Root output `mcp_chat_gateway_url`.
 */

module "lambda_action_chat_interceptor" {
  source = "./modules/lambda_action"

  eni_function_code_deny_policy_arn = module.eni_function_code_deny.policy_arn

  function_name = "${local.name_prefix}-chat-identity-interceptor"

  vpc_subnet_ids         = module.vpc.lambda_subnet_ids
  vpc_security_group_ids = module.vpc.lambda_security_group_ids
  handler                = "gateway.identity_interceptor.lambda_handler"
  runtime                = "python3.13"
  filename               = "${var.lambda_build_dir}/identity_interceptor.zip"
  source_code_hash       = filebase64sha256("${var.lambda_build_dir}/identity_interceptor.zip")

  timeout_seconds = 10
  memory_size_mb  = 256

  # As Gateway #1's interceptor, except `COGNITO_ALLOWED_SCOPES` (this gateway's chat scope only) and
  # `COGNITO_ALLOWED_CLIENT_IDS` (adds the MCP OAuth client).
  environment_variables = {
    COGNITO_DISCOVERY_URL            = "https://cognito-idp.${var.aws_region}.amazonaws.com/${module.cognito.user_pool_id}/.well-known/openid-configuration"
    COGNITO_ALLOWED_CLIENT_IDS       = join(",", module.cognito.chat_gateway_allowed_client_ids)
    COGNITO_JWKS_URL                 = "https://cognito-idp.${var.aws_region}.amazonaws.com/${module.cognito.user_pool_id}/.well-known/jwks.json"
    COGNITO_ALLOWED_SCOPES           = module.cognito.chat_scope_identifier
    GATEWAY_SERVICE_PRINCIPAL_REP_ID = local.gateway_service_principal_rep_id
    CRM_TABLE_PREFIX                 = local.name_prefix
  }
  environment_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn

  reserved_concurrent_executions = local.reserved_concurrency["chat_interceptor"]

  dynamodb_grants = [
    {
      table_name = "reps"
      table_arn  = module.dynamodb.table_arns["reps"]
      actions    = ["dynamodb:GetItem"]
      leading_key_condition = {
        attribute = "rep_id"
        values    = [local.gateway_service_principal_rep_id]
      }
    },
  ]
  table_action_role_matrix_entries = [
    {
      table                 = "reps"
      action                = "dynamodb:GetItem"
      role                  = "${local.name_prefix}-chat-identity-interceptor-role"
      leading_keys_enforced = true
      isolation_mechanism   = "leading_keys"
    },
  ]

  denied_table_arns = flatten([
    for table_name, table_arn in module.dynamodb.table_arns : [table_arn, "${table_arn}/index/*"]
    if table_name != "reps"
  ])
  denied_own_table_actions = [
    {
      table_name = "reps"
      table_arn  = module.dynamodb.table_arns["reps"]
      actions    = setsubtract(local.dynamodb_asserted_actions, ["dynamodb:GetItem"])
      index_arns = ["${module.dynamodb.table_arns["reps"]}/index/*"]
    },
  ]
  dynamodb_kms_key_arn       = module.dynamodb.kms_key_arn
  dynamodb_kms_grant_enabled = true

  # Invoked only by Gateway #2: its role here, and the service principal through the module's permission.
  gateway_target_role_arn     = module.mcp_chat_gateway.role_arn
  attach_gateway_target_grant = true
}

module "lambda_action_chat_target" {
  source = "./modules/lambda_action"

  eni_function_code_deny_policy_arn = module.eni_function_code_deny.policy_arn

  function_name = "${local.name_prefix}-chat-target"

  vpc_subnet_ids         = module.vpc.lambda_subnet_ids
  vpc_security_group_ids = module.vpc.lambda_security_group_ids
  handler                = "gateway.chat_target.lambda_handler"
  runtime                = "python3.13"
  filename               = "${var.lambda_build_dir}/chat_target.zip"
  source_code_hash       = filebase64sha256("${var.lambda_build_dir}/chat_target.zip")

  # Above the 640 s internal deadline, itself above the 600 s chat wall clock.
  timeout_seconds = 660
  memory_size_mb  = 512

  environment_variables = {
    CRM_TABLE_PREFIX = local.name_prefix
    # The first-party packages live under `src/` so `config/` resolves at the archive root.
    PYTHONPATH              = "/var/task/src"
    AGENT_RUNTIME_ARN       = module.agentcore.agent_runtime_arn
    AGENT_RUNTIME_QUALIFIER = local.relay_runtime_qualifier
  }
  environment_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn

  reserved_concurrent_executions = local.reserved_concurrency["chat_target"]

  # `reps` only, by key prefix: read the caller's settings and read/create conversation
  # ownership items.
  dynamodb_grants = [
    {
      table_name = "reps"
      table_arn  = module.dynamodb.table_arns["reps"]
      actions    = ["dynamodb:GetItem"]
      leading_key_condition = {
        attribute = "dynamodb:LeadingKeys"
        operator  = "StringLike"
        values    = ["usersettings#*", "conversation#*"]
      }
    },
    {
      table_name = "reps"
      table_arn  = module.dynamodb.table_arns["reps"]
      actions    = ["dynamodb:PutItem"]
      leading_key_condition = {
        attribute = "dynamodb:LeadingKeys"
        operator  = "StringLike"
        values    = ["conversation#*"]
      }
    },
  ]
  table_action_role_matrix_entries = [
    {
      table                 = "reps"
      action                = "dynamodb:GetItem"
      role                  = "${local.name_prefix}-chat-target-role"
      leading_keys_enforced = true
      isolation_mechanism   = "leading_keys"
    },
    {
      table                 = "reps"
      action                = "dynamodb:PutItem"
      role                  = "${local.name_prefix}-chat-target-role"
      leading_keys_enforced = true
      isolation_mechanism   = "leading_keys"
    },
  ]

  denied_table_arns = flatten([
    for table_name, table_arn in module.dynamodb.table_arns : [table_arn, "${table_arn}/index/*"]
    if table_name != "reps"
  ])
  denied_own_table_actions = [
    {
      table_name = "reps"
      table_arn  = module.dynamodb.table_arns["reps"]
      actions    = setsubtract(local.dynamodb_asserted_actions, ["dynamodb:GetItem", "dynamodb:PutItem"])
      index_arns = ["${module.dynamodb.table_arns["reps"]}/index/*"]
    },
  ]
  dynamodb_kms_key_arn       = module.dynamodb.kms_key_arn
  dynamodb_kms_grant_enabled = true

  # Invoked only by Gateway #2's role, never Gateway #1's.
  gateway_target_role_arn     = module.mcp_chat_gateway.role_arn
  attach_gateway_target_grant = true
}

# `InvokeAgentRuntime` on the runtime and its DEFAULT endpoint only; every other action on them denied.
data "aws_iam_policy_document" "chat_target_invoke_runtime" {
  statement {
    sid       = "InvokeTheRuntimeDefaultEndpoint"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [module.agentcore.agent_runtime_arn, local.agent_runtime_endpoint_arn]
  }

  statement {
    sid         = "DenyEverythingButInvokeOnThisRuntime"
    effect      = "Deny"
    not_actions = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources   = [module.agentcore.agent_runtime_arn, local.agent_runtime_endpoint_arn]
  }
}

resource "aws_iam_role_policy" "chat_target_invoke_runtime" {
  name   = "${local.name_prefix}-chat-target-invoke-runtime"
  role   = module.lambda_action_chat_target.role_name
  policy = data.aws_iam_policy_document.chat_target_invoke_runtime.json
}

module "mcp_chat_gateway" {
  source = "./modules/mcp_chat_gateway"

  gateway_name = "${local.name_prefix}-chat-gateway"
  role_name    = "${local.name_prefix}-chat-gateway-role"
  aws_region   = var.aws_region
  account_id   = data.aws_caller_identity.current.account_id

  cognito_discovery_url      = "https://cognito-idp.${var.aws_region}.amazonaws.com/${module.cognito.user_pool_id}/.well-known/openid-configuration"
  cognito_allowed_client_ids = module.cognito.chat_gateway_allowed_client_ids
  # This gateway's own scope; Gateway #1 keeps `gateway_scope_identifier`.
  cognito_allowed_scopes = [module.cognito.chat_scope_identifier]

  identity_interceptor_lambda_arn           = module.lambda_action_chat_interceptor.function_arn
  identity_interceptor_lambda_function_name = module.lambda_action_chat_interceptor.function_name

  chat_target_lambda_arn = module.lambda_action_chat_target.function_arn

  tags = {
    "data-classification" = var.data_classification
  }
}

output "mcp_chat_gateway_url" {
  description = "MCP endpoint URL of Gateway #2, which exposes only `chat_with_supervisor`."
  value       = module.mcp_chat_gateway.gateway_url
}
