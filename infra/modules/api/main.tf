/**
 * PRIVATE REST API (API Gateway v1) fronting the routed CRM tools plus the allowlisted
 * `extra_routes`, one route per name at `{apiBaseUrl}/{route_name}`. Resources, methods and
 * integrations are generated with `for_each` over `local.route_invoke_arns`, so every route carries
 * the Cognito authorizer.
 *
 * `tool_lambda_invoke_arns` and `tool_lambda_function_names` must share one key set; `extra_routes`
 * is a closed allowlist of non-toolspec operations. The PRIVATE endpoint type makes the API addressable only through the
 * named `execute-api` interface endpoint.
 */

locals {
  tool_names = sort(keys(var.tool_lambda_invoke_arns))

  # The single map every resource, method, integration and permission below is generated from.
  route_invoke_arns = merge(
    var.tool_lambda_invoke_arns,
    { for name, route in var.extra_routes : name => route.invoke_arn },
  )

  route_function_names = merge(
    var.tool_lambda_function_names,
    { for name, route in var.extra_routes : name => route.function_name },
  )

  # The client-facing HTTP method per route. Every tool is POST; an `extra_routes` entry may differ
  # within its validation.
  route_methods = merge(
    { for name in keys(var.tool_lambda_invoke_arns) : name => "POST" },
    { for name, route in var.extra_routes : name => route.method },
  )

  # Redeployment trigger: a REST API stage keeps serving its last deployment, so any change to
  # routes, methods, integrations, authorizer, policy or validation must change this hash.
  deployment_trigger = sha1(jsonencode([
    local.route_invoke_arns,
    local.route_function_names,
    local.route_methods,
    var.cognito_user_pool_arn,
    var.vpc_endpoint_ids,
    local.request_validation_trigger,
  ]))

  # The validation surface folded into the deployment hash, derived from the resources themselves.
  request_validation_trigger = {
    validate_request_body       = aws_api_gateway_request_validator.body_and_params.validate_request_body
    validate_request_parameters = aws_api_gateway_request_validator.body_and_params.validate_request_parameters
    model_schema                = aws_api_gateway_model.json_object.schema
  }
}

# ---------------------------------------------------------------------------
# Private REST API + resource policy
# ---------------------------------------------------------------------------

# A PRIVATE endpoint type has no public hostname: the API is addressable only as
# `https://<api-id>-<vpce-id>.execute-api.<region>.amazonaws.com` through the interface endpoints
# below, reached from CloudFront via a VPC origin and an internal ALB.
#
# `ip_address_type` is unset: a private API defaults to `dualstack` and does not support `IPV4`.
resource "aws_api_gateway_rest_api" "this" {
  name = var.api_name

  endpoint_configuration {
    types            = ["PRIVATE"]
    vpc_endpoint_ids = var.vpc_endpoint_ids
  }

  # Denies every request not arriving through `var.vpc_endpoint_ids`, since an interface endpoint in
  # another VPC could otherwise resolve a private API.
  policy = data.aws_iam_policy_document.resource_policy.json

  tags = var.tags

  lifecycle {
    create_before_destroy = true

    precondition {
      condition     = toset(keys(var.tool_lambda_invoke_arns)) == toset(keys(var.tool_lambda_function_names))
      error_message = "tool_lambda_invoke_arns and tool_lambda_function_names must carry identical key sets."
    }

    precondition {
      condition     = length(setintersection(toset(keys(var.tool_lambda_invoke_arns)), toset(keys(var.extra_routes)))) == 0
      error_message = "extra_routes must not reuse a tool route name; merge would silently drop one."
    }
  }
}

data "aws_iam_policy_document" "resource_policy" {
  # checkov:skip=CKV_AWS_283: `Principal: *` on a PRIVATE API, bounded by the aws:sourceVpce Deny below; callers authenticate via the Cognito authorizer
  statement {
    sid    = "AllowInvokeFromNamedVpcEndpointsOnly"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions   = ["execute-api:Invoke"]
    resources = ["execute-api:/*"]
  }

  statement {
    sid    = "DenyInvokeFromAnyOtherVpcEndpoint"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions   = ["execute-api:Invoke"]
    resources = ["execute-api:/*"]

    condition {
      test     = "StringNotEquals"
      variable = "aws:sourceVpce"
      values   = var.vpc_endpoint_ids
    }
  }
}

# ---------------------------------------------------------------------------
# Cognito authorizer, attached to every method below
# ---------------------------------------------------------------------------

# The one authorizer every method references. A v1 `COGNITO_USER_POOLS` authorizer takes the pool
# ARN and checks that the token was issued by this pool; it does not check the token's audience
# (`aud`/`client_id`), so a token minted for another app client in the same pool is accepted.
resource "aws_api_gateway_authorizer" "cognito" {
  name          = "cognito-user-pools"
  rest_api_id   = aws_api_gateway_rest_api.this.id
  type          = "COGNITO_USER_POOLS"
  provider_arns = [var.cognito_user_pool_arn]

  # `/api/*` has no OAC, so the SPA's JWT arrives in `Authorization` unchanged.
  identity_source = "method.request.header.Authorization"
}

# ---------------------------------------------------------------------------
# One resource + method + integration per route, generated from the single map.
#
# Request validation is an edge envelope check: every POST method carries a `{"type":"object"}`
# model with no `properties`, so a non-object JSON body is refused with 400 before any Lambda runs.
# Per-field validation is each tool's in-Lambda `input_schema`. No request parameters are declared
# as required.
# ---------------------------------------------------------------------------

# One validator, referenced by every method.
resource "aws_api_gateway_request_validator" "body_and_params" {
  rest_api_id                 = aws_api_gateway_rest_api.this.id
  name                        = "body-and-params"
  validate_request_body       = true
  validate_request_parameters = true
}

# The `{"type":"object"}` envelope model, attached to POST methods only.
resource "aws_api_gateway_model" "json_object" {
  rest_api_id  = aws_api_gateway_rest_api.this.id
  name         = "JsonObject"
  content_type = "application/json"
  schema = jsonencode({
    "$schema" = "http://json-schema.org/draft-04/schema#"
    type      = "object"
  })
}

resource "aws_api_gateway_resource" "tool" {
  for_each = local.route_invoke_arns

  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_rest_api.this.root_resource_id
  path_part   = each.key
}

resource "aws_api_gateway_method" "tool" {
  for_each = local.route_invoke_arns

  rest_api_id = aws_api_gateway_rest_api.this.id
  resource_id = aws_api_gateway_resource.tool[each.key].id
  http_method = local.route_methods[each.key]

  # Every route is authorized.
  authorization = "COGNITO_USER_POOLS"
  authorizer_id = aws_api_gateway_authorizer.cognito.id

  # The object model is attached to POST only; a GET has no body.
  request_validator_id = aws_api_gateway_request_validator.body_and_params.id
  request_models = (
    local.route_methods[each.key] == "POST"
    ? { "application/json" = aws_api_gateway_model.json_object.name }
    : {}
  )
}

resource "aws_api_gateway_integration" "tool" {
  for_each = local.route_invoke_arns

  rest_api_id = aws_api_gateway_rest_api.this.id
  resource_id = aws_api_gateway_resource.tool[each.key].id
  http_method = aws_api_gateway_method.tool[each.key].http_method

  # `AWS_PROXY` passes the whole request to the handler, which returns `statusCode`/`headers`/`body`.
  # A REST API integration sends payload format 1.0, which `crm.envelope.decode_identity` and
  # `crm.handlers._common.is_proxy_event` both handle; `tests/unit/test_handlers_apigw_proxy.py`
  # reads this file to check that.
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = each.value
}

resource "aws_lambda_permission" "apigw_invoke" {
  for_each = local.route_function_names

  statement_id  = "AllowAPIGatewayInvoke-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = each.value
  principal     = "apigateway.amazonaws.com"

  # Scoped to this route's own path and method, taken from `local.route_methods`, on any stage.
  source_arn = "${aws_api_gateway_rest_api.this.execution_arn}/*/${local.route_methods[each.key]}/${each.key}"
}

# ---------------------------------------------------------------------------
# Deployment + stage: throttle + burst limits, access logging.
# ---------------------------------------------------------------------------

resource "aws_api_gateway_deployment" "this" {
  rest_api_id = aws_api_gateway_rest_api.this.id

  triggers = {
    redeployment = local.deployment_trigger
  }

  # The deployment must be created after the methods it captures; the `triggers` hash controls only
  # when it is replaced.
  depends_on = [
    aws_api_gateway_method.tool,
    aws_api_gateway_integration.tool,
    aws_api_gateway_request_validator.body_and_params,
    aws_api_gateway_model.json_object,
  ]

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_cloudwatch_log_group" "access_logs" {
  name              = "/aws/apigateway/${var.api_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.access_log_kms_key_arn

  tags = var.tags
}

resource "aws_api_gateway_stage" "this" {
  # checkov:skip=CKV_AWS_120: responses are per-user and authorized per request, so none is cacheable
  # checkov:skip=CKV2_AWS_51: mTLS needs a custom domain; the only caller is the internal ALB, which presents no client certificate
  # checkov:skip=CKV2_AWS_4: execution logging and method metrics are off, an open follow-up; only access logging is on
  rest_api_id   = aws_api_gateway_rest_api.this.id
  deployment_id = aws_api_gateway_deployment.this.id
  stage_name    = var.stage_name

  xray_tracing_enabled = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access_logs.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      resourcePath   = "$context.resourcePath"
      status         = "$context.status"
      integrationErr = "$context.integrationErrorMessage"
      responseLength = "$context.responseLength"
      sourceIp       = "$context.identity.sourceIp"
      userSub        = "$context.authorizer.claims.sub"
      vpcEndpointId  = "$context.identity.vpceId"
    })
  }

  tags = var.tags
}

# `method_path = "*/*"` applies throttling to every method in the stage.
resource "aws_api_gateway_method_settings" "this" {
  # checkov:skip=CKV_AWS_225: responses are per-user and authorized per request, so none is cacheable
  rest_api_id = aws_api_gateway_rest_api.this.id
  stage_name  = aws_api_gateway_stage.this.stage_name
  method_path = "*/*"

  settings {
    throttling_burst_limit = var.throttle_burst_limit
    throttling_rate_limit  = var.throttle_rate_limit
  }
}
