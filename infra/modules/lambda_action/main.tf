/**
 * Reusable action-Lambda module. One instantiation = one Lambda function, one dedicated execution
 * role, one log group, reserved concurrency, and an optional resource-policy grant for the Gateway
 * target role. API Gateway's invoke grant lives in `modules/api`.
 */

# ---------------------------------------------------------------------------
# One dedicated execution role per instantiation.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = "${var.function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
  tags               = var.tags
}

# CloudWatch Logs writes scoped to this function's own log group, instead of the managed
# `AWSLambdaBasicExecutionRole`, which grants `logs:CreateLogGroup` account-wide.
data "aws_iam_policy_document" "logs" {
  statement {
    sid       = "WriteOwnLogGroup"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }

  # Runtime configuration read at cold start, scoped to the named parameters.
  dynamic "statement" {
    for_each = length(var.config_parameter_arns) > 0 ? [var.config_parameter_arns] : []

    content {
      sid       = "ReadConfigParameters"
      effect    = "Allow"
      actions   = ["ssm:GetParameter"]
      resources = statement.value
    }
  }
}

resource "aws_iam_role_policy" "logs" {
  name   = "${var.function_name}-logs"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.logs.json
}

locals {
  config_parameter_region = length(var.config_parameter_arns) > 0 ? split(":", var.config_parameter_arns[0])[3] : null
}

# Decrypt for the SecureString config parameters, only through SSM under each parameter's encryption
# context, and a refusal to decrypt through SSM under any other key.
data "aws_iam_policy_document" "config_parameter_kms" {
  count = length(var.config_parameter_arns) > 0 ? 1 : 0

  statement {
    sid       = "DecryptConfigParameters"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [var.config_parameter_kms_key_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${local.config_parameter_region}.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:EncryptionContext:PARAMETER_ARN"
      values   = var.config_parameter_arns
    }
  }

  statement {
    sid           = "DenyConfigParameterDecryptUnderOtherKeys"
    effect        = "Deny"
    actions       = ["kms:Decrypt"]
    not_resources = [var.config_parameter_kms_key_arn]

    condition {
      test     = "StringLike"
      variable = "kms:ViaService"
      values   = ["ssm.*.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "config_parameter_kms" {
  count = length(var.config_parameter_arns) > 0 ? 1 : 0

  name   = "${var.function_name}-config-kms"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.config_parameter_kms[0].json
}

# ---------------------------------------------------------------------------
# Grants that take no resource-level scoping: Hyperplane ENI management for `vpc_config`, and
# X-Ray trace writes for `tracing_config`.
# ---------------------------------------------------------------------------

# Lambda creates the function's VPC ENI with the execution role, so without this every invocation
# fails. `resources = ["*"]` is what the Lambda Developer Guide requires for these six actions
# ("Required IAM permissions" in configuration-vpc.html). They cover network interfaces only.
# `aws_iam_role_policy_attachment.eni_function_code_deny` withholds them from function code.
data "aws_iam_policy_document" "vpc_access" {
  # checkov:skip=CKV_AWS_111: ENI and X-Ray write actions support no resource-level scoping; function code is denied the ENI actions
  # checkov:skip=CKV_AWS_356: ENI and X-Ray write actions support no resource-level scoping; function code is denied the ENI actions
  statement {
    sid    = "ManageOwnHyperplaneEnis"
    effect = "Allow"
    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeSubnets",
      "ec2:DeleteNetworkInterface",
      "ec2:AssignPrivateIpAddresses",
      "ec2:UnassignPrivateIpAddresses",
    ]
    resources = ["*"]
  }

  # Active tracing sends segments with the execution role; these two actions have no resource type.
  statement {
    sid       = "WriteXRayTraceSegments"
    effect    = "Allow"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }

}

resource "aws_iam_role_policy" "vpc_access" {
  name   = "${var.function_name}-vpc-access"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.vpc_access.json
}

# A managed policy, so the Deny does not count toward the role's inline-policy quota.
resource "aws_iam_role_policy_attachment" "eni_function_code_deny" {
  role       = aws_iam_role.this.name
  policy_arn = var.eni_function_code_deny_policy_arn
}

# ---------------------------------------------------------------------------
# Per-table, per-action DynamoDB grants from var.dynamodb_grants. No `dynamodb:*`
# and no `Resource = "*"`: each statement names its table and, for GSI access,
# the index ARNs.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "dynamodb" {
  count = length(var.dynamodb_grants) > 0 || length(var.denied_table_arns) > 0 || length(var.denied_own_table_actions) > 0 ? 1 : 0

  dynamic "statement" {
    for_each = { for idx, grant in var.dynamodb_grants : idx => grant }

    content {
      sid       = "DynamoDb${replace(title(statement.value.table_name), "_", "")}${statement.key}"
      effect    = "Allow"
      actions   = statement.value.actions
      resources = concat(statement.value.include_table_arn ? [statement.value.table_arn] : [], statement.value.index_arns)

      dynamic "condition" {
        for_each = statement.value.leading_key_condition != null ? [statement.value.leading_key_condition] : []

        content {
          test     = "ForAllValues:${condition.value.operator}"
          variable = "dynamodb:LeadingKeys"
          values   = condition.value.values
        }
      }

      dynamic "condition" {
        for_each = length(statement.value.select_condition) > 0 ? [statement.value.select_condition] : []

        content {
          test     = "StringEquals"
          variable = "dynamodb:Select"
          values   = condition.value
        }
      }
    }
  }

  # Explicit Deny of the full DynamoDB action surface on every table this function is not granted,
  # so a later additive Allow on this role cannot open them.
  dynamic "statement" {
    for_each = length(var.denied_table_arns) > 0 ? [1] : []

    content {
      sid       = "ExplicitDenyCrossFunctionIsolation"
      effect    = "Deny"
      actions   = ["dynamodb:*"]
      resources = var.denied_table_arns
    }
  }

  # Explicit Deny, on each granted table, of every asserted action this function's grant does not
  # name.
  dynamic "statement" {
    for_each = {
      for idx, entry in var.denied_own_table_actions : idx => entry
      if length(entry.actions) > 0
    }

    content {
      sid       = "ExplicitDenyNonGranted${replace(title(statement.value.table_name), "_", "")}${statement.key}"
      effect    = "Deny"
      actions   = statement.value.actions
      resources = concat([statement.value.table_arn], statement.value.index_arns)
    }
  }
}

resource "aws_iam_role_policy" "dynamodb" {
  count  = !var.dynamodb_policy_managed && (length(var.dynamodb_grants) > 0 || length(var.denied_table_arns) > 0 || length(var.denied_own_table_actions) > 0) ? 1 : 0
  name   = "${var.function_name}-dynamodb"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.dynamodb[0].json
}

# With `dynamodb_policy_managed`, the same document as customer-managed policies, one per effect,
# the Allows in chunks of `dynamodb_allow_statements_per_policy`
# keyed `allow`, `allow-2`, ... in lexical order of the grant index (0, 1, 10, 11, 2, …). Keys
# derive from the inputs, so `for_each` is plan-time known.
locals {
  dynamodb_allow_chunk_size  = coalesce(var.dynamodb_allow_statements_per_policy, max(length(var.dynamodb_grants), 1))
  dynamodb_allow_chunk_count = ceil(length(var.dynamodb_grants) / local.dynamodb_allow_chunk_size)
  dynamodb_allow_chunk_keys  = [for i in range(local.dynamodb_allow_chunk_count) : i == 0 ? "allow" : "allow-${i + 1}"]

  dynamodb_managed_policies = var.dynamodb_policy_managed ? merge(
    { for i, key in local.dynamodb_allow_chunk_keys : key => { effect = "Allow", chunk = i } },
    length(var.denied_table_arns) > 0 || anytrue([for entry in var.denied_own_table_actions : length(entry.actions) > 0]) ? { deny = { effect = "Deny", chunk = 0 } } : {},
  ) : {}

  dynamodb_document_statements = try(jsondecode(data.aws_iam_policy_document.dynamodb[0].json).Statement, [])
  dynamodb_allow_statements    = [for statement in local.dynamodb_document_statements : statement if statement.Effect == "Allow"]
  dynamodb_managed_statements = merge(
    {
      for i, key in local.dynamodb_allow_chunk_keys : key => slice(
        local.dynamodb_allow_statements,
        i * local.dynamodb_allow_chunk_size,
        min((i + 1) * local.dynamodb_allow_chunk_size, length(local.dynamodb_allow_statements)),
      )
    },
    { deny = [for statement in local.dynamodb_document_statements : statement if statement.Effect == "Deny"] },
  )
}

resource "aws_iam_policy" "dynamodb" {
  for_each    = local.dynamodb_managed_policies
  name        = "${var.function_name}-dynamodb-${each.key}"
  description = each.value.chunk == 0 ? "The ${each.value.effect} statements of ${var.function_name}'s DynamoDB grant." : "Part ${each.value.chunk + 1} of the Allow statements of ${var.function_name}'s DynamoDB grant."
  policy = jsonencode({
    Version   = jsondecode(data.aws_iam_policy_document.dynamodb[0].json).Version
    Statement = local.dynamodb_managed_statements[each.key]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "dynamodb_deny" {
  count      = contains(keys(local.dynamodb_managed_policies), "deny") ? 1 : 0
  role       = aws_iam_role.this.name
  policy_arn = aws_iam_policy.dynamodb["deny"].arn
}

# Attached only after the Denies, so no apply leaves the Allows in effect without them.
resource "aws_iam_role_policy_attachment" "dynamodb_allow" {
  count      = var.dynamodb_policy_managed ? local.dynamodb_allow_chunk_count : 0
  role       = aws_iam_role.this.name
  policy_arn = aws_iam_policy.dynamodb[local.dynamodb_allow_chunk_keys[count.index]].arn

  depends_on = [aws_iam_role_policy_attachment.dynamodb_deny]
}

# ---------------------------------------------------------------------------
# KMS grants: the DynamoDB CMK (DynamoDB calls kms:Decrypt/kms:GenerateDataKey on
# the caller's behalf, so the caller's role needs them) and, when enabled, the
# pagination-cursor CMK. Each scoped to its one key.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "dynamodb_kms" {
  # Driven by a boolean input, not by `var.dynamodb_kms_key_arn`: a count derived from a resource
  # attribute is unknown once the key is gone, which blocks `terraform destroy`.
  count = var.dynamodb_kms_grant_enabled ? 1 : 0

  statement {
    sid       = "DecryptDynamoDbTableKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [var.dynamodb_kms_key_arn]
  }

  # Pagination-cursor sealing: Encrypt/Decrypt on the one cursor key, only under the
  # `purpose = crm-cursor` encryption context.
  dynamic "statement" {
    for_each = var.cursor_kms_grant_enabled ? [var.cursor_kms_key_arn] : []

    content {
      sid       = "SealPaginationCursors"
      effect    = "Allow"
      actions   = ["kms:Encrypt", "kms:Decrypt"]
      resources = [statement.value]

      condition {
        test     = "StringEquals"
        variable = "kms:EncryptionContext:purpose"
        values   = ["crm-cursor"]
      }

      condition {
        test     = "ForAllValues:StringEquals"
        variable = "kms:EncryptionContextKeys"
        values   = ["operation", "purpose", "rep_id", "tenant"]
      }
    }
  }
}

resource "aws_iam_role_policy" "dynamodb_kms" {
  count  = var.dynamodb_kms_grant_enabled ? 1 : 0
  name   = "${var.function_name}-dynamodb-kms"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.dynamodb_kms[0].json
}

# ---------------------------------------------------------------------------
# Log group, explicit retention.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = 365
  kms_key_id        = var.environment_kms_key_arn
  tags              = var.tags
}

# ---------------------------------------------------------------------------
# The function itself.
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "this" {
  # checkov:skip=CKV_AWS_116: no DLQ; every caller invokes synchronously
  # checkov:skip=CKV_AWS_272: zips are built by scripts/package_lambdas.sh with no signing profile
  function_name = var.function_name
  role          = aws_iam_role.this.arn
  handler       = var.handler
  runtime       = var.runtime
  timeout       = var.timeout_seconds
  memory_size   = var.memory_size_mb

  filename         = var.filename
  source_code_hash = var.source_code_hash

  reserved_concurrent_executions = var.reserved_concurrent_executions

  dynamic "environment" {
    for_each = length(var.environment_variables) > 0 ? [var.environment_variables] : []

    content {
      variables = environment.value
    }
  }

  kms_key_arn = var.environment_kms_key_arn

  tracing_config {
    mode = "Active"
  }

  # A static block, not `dynamic "vpc_config"`, so no instantiation can run outside a VPC.
  # `scripts/check_no_public_exposure.py` reads it literally.
  vpc_config {
    subnet_ids         = var.vpc_subnet_ids
    security_group_ids = var.vpc_security_group_ids
  }

  tags = var.tags

  depends_on = [
    aws_iam_role_policy.logs,
    aws_iam_role_policy.vpc_access,
    aws_cloudwatch_log_group.this,
  ]
}

# ---------------------------------------------------------------------------
# Resource policy: admits only the Gateway target role, when supplied.
# ---------------------------------------------------------------------------

# No `apigateway.amazonaws.com` permission is granted here; `modules/api`'s
# `aws_lambda_permission.apigw_invoke` is the only API Gateway grant, and
# `tests/unit/test_infra_invoke_grant_scope.py` asserts this module declares none.
resource "aws_lambda_permission" "gateway_target_role" {
  count = var.attach_gateway_target_grant ? 1 : 0

  statement_id  = "AllowGatewayTargetRoleInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this.function_name
  principal     = var.gateway_target_role_arn

  # `count` is driven by the flag because a count derived from the ARN cannot be planned from an
  # empty state; this precondition keeps the flag and the principal in agreement.
  lifecycle {
    precondition {
      condition     = var.gateway_target_role_arn != null
      error_message = "attach_gateway_target_grant is true but gateway_target_role_arn is null: the Gateway-role permission would have no principal."
    }
  }
}

# ---------------------------------------------------------------------------
# This instantiation's rows in the table × action × role matrix, collected by
# the root composition for the plan-time security gate. Documentary only.
# ---------------------------------------------------------------------------

locals {
  matrix_rows = [
    for entry in var.table_action_role_matrix_entries : merge(entry, {
      role = aws_iam_role.this.name
    })
  ]
}
