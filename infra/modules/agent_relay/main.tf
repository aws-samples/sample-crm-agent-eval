/**
 * The streaming relay: one Node.js Lambda with a RESPONSE_STREAM Function URL, its own execution
 * role and log group, placed in the private VPC, with X-Ray active tracing.
 *
 * The role may invoke one runtime, write its own log group, read the one audience parameter,
 * manage its VPC ENIs and write X-Ray segments; every other action on the runtime is explicitly
 * denied. It has no DynamoDB,
 * Bedrock model or `lambda:InvokeFunction` access.
 */

locals {
  # The ARN path segment for a runtime endpoint is `runtime-endpoint`, not `endpoint`:
  #   arn:aws:bedrock-agentcore:<region>:<account>:runtime/<runtime-id>/runtime-endpoint/DEFAULT
  agent_runtime_endpoints_arn = "${var.agent_runtime_arn}/runtime-endpoint/*"

  # Harness resources (`harness/<id>`, `harness/<id>/harness-endpoint/<name>`) are a different
  # resource type from `runtime/*`. Built from the runtime ARN's partition, service, region and account.
  agentcore_harness_arn = "${join(":", slice(split(":", var.agent_runtime_arn), 0, 5))}:harness/*"
}

# ---------------------------------------------------------------------------
# Execution role, dedicated to this function.
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

data "aws_iam_policy_document" "logs" {
  statement {
    sid       = "WriteOwnLogGroup"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }
}

resource "aws_iam_role_policy" "logs" {
  name   = "${var.function_name}-logs"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.logs.json
}

# Hyperplane ENI management for `vpc_config`, and X-Ray trace writes for `tracing_config`. Lambda
# creates the function's ENI with the execution role. `resources = ["*"]` is what these actions
# require; the runtime-scoped Deny in `invoke_runtime` does not reach `*`.
# `aws_iam_role_policy_attachment.eni_function_code_deny` withholds the ENI actions from function
# code.
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

# `InvokeAgentRuntime` on the runtime and on its endpoints: AgentCore authorizes both, and the relay
# always addresses an endpoint (`runtime.ts` sends a qualifier, default `DEFAULT`). The endpoint
# pattern is `/runtime-endpoint/*`, not a bare trailing wildcard, which would also match any runtime
# whose id shares this one's prefix.
data "aws_iam_policy_document" "invoke_runtime" {
  statement {
    sid       = "InvokeOneRuntimeAndItsEndpoints"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [var.agent_runtime_arn, local.agent_runtime_endpoints_arn]
  }

  # Explicit Deny of every action on the runtime and its endpoints except `InvokeAgentRuntime`,
  # including command-execution, WebSocket, `…ForUser` and future actions. Any new legitimate action
  # on these resources must be added to `not_actions` here.
  #
  # `resources` stays scoped to the runtime and its endpoints: a `not_actions` Deny on `"*"` would
  # also deny the relay's logs and SSM reads.
  statement {
    sid         = "DenyEverythingButTheOnePermittedInvokeOnThisRuntime"
    effect      = "Deny"
    not_actions = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources   = [var.agent_runtime_arn, local.agent_runtime_endpoints_arn]
  }

  # Denies every bedrock-agentcore action on every harness resource, which the runtime-scoped Deny
  # above does not reach. A separate statement because `test_infra_relay.py` pins that Deny's
  # resources to equal the Allow's.
  statement {
    sid       = "DenyEveryHarness"
    effect    = "Deny"
    actions   = ["bedrock-agentcore:*"]
    resources = [local.agentcore_harness_arn]
  }
}

resource "aws_iam_role_policy" "invoke_runtime" {
  name   = "${var.function_name}-invoke-runtime"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.invoke_runtime.json
}

# Read-only access to the one expected-JWT-audience parameter, scoped to its constructed ARN.
data "aws_iam_policy_document" "read_audience_parameter" {
  statement {
    sid       = "ReadRelayParameters"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = [var.cognito_audience_parameter_arn]
  }
}

resource "aws_iam_role_policy" "read_audience_parameter" {
  name   = "${var.function_name}-read-parameters"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.read_audience_parameter.json
}

# ---------------------------------------------------------------------------
# Log group, explicit retention, project CMK.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.environment_kms_key_arn
  tags              = var.tags
}

# ---------------------------------------------------------------------------
# The function.
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "this" {
  # checkov:skip=CKV_AWS_116: invoked synchronously through its Function URL; a DLQ applies to async invokes only
  # checkov:skip=CKV_AWS_272: relay.zip is built by scripts/package_relay.sh with no signing profile
  function_name = var.function_name
  role          = aws_iam_role.this.arn
  handler       = var.handler
  runtime       = var.runtime
  timeout       = var.timeout_seconds
  memory_size   = var.memory_size_mb

  filename         = var.filename
  source_code_hash = var.source_code_hash

  reserved_concurrent_executions = var.reserved_concurrent_executions

  environment {
    variables = local.environment_variables
  }

  kms_key_arn = var.environment_kms_key_arn

  tracing_config {
    mode = "Active"
  }

  # A static block, not `dynamic "vpc_config"`, so the function cannot be planned without a VPC.
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

# The relay's configuration surface, exported as `environment_variable_names`.
locals {
  # Cognito publishes its key set at a fixed path beneath the issuer.
  cognito_jwks_url = "${var.cognito_issuer}/.well-known/jwks.json"

  # Exactly the names `src/relay/src/{jwt,runtime}.ts` read; each throws at startup when absent or
  # blank. `AWS_REGION` is reserved and injected by Lambda, so it is not set here.
  environment_variables = {
    AGENT_RUNTIME_ARN          = var.agent_runtime_arn
    AGENT_RUNTIME_QUALIFIER    = var.agent_runtime_qualifier
    COGNITO_ISSUER             = var.cognito_issuer
    COGNITO_JWKS_URL           = local.cognito_jwks_url
    COGNITO_AUDIENCE_PARAMETER = var.cognito_audience_parameter_name
  }
}

# ---------------------------------------------------------------------------
# Function URL: RESPONSE_STREAM, IAM-authorized.
# ---------------------------------------------------------------------------

# `invoke_mode = RESPONSE_STREAM`: the default BUFFER would hold an SSE stream until the handler
# returns.
#
# `authorization_type = "AWS_IAM"`. The only caller is the CloudFront distribution, through an OAC
# that SigV4-signs each origin request; the distribution-scoped `lambda:InvokeFunctionUrl`
# permission is in `modules/frontend_cdn`. That signature occupies the `Authorization` header, so the
# user's Cognito JWT travels in `X-Crm-Authorization`, and the relay verifies it before any Runtime
# call.
resource "aws_lambda_function_url" "this" {
  function_name      = aws_lambda_function.this.function_name
  authorization_type = "AWS_IAM"
  invoke_mode        = "RESPONSE_STREAM"
}
