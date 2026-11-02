/**
 * Gateway #2: an MCP `aws_bedrockagentcore_gateway` with a `CUSTOM_JWT` authorizer and
 * the shared REQUEST identity interceptor, exactly one Lambda target `chat_with_supervisor`, and its
 * own execution role allowed to invoke only that target and the interceptor.
 *
 * The role trust uses `ArnLike aws:SourceArn .../gateway/*` because the gateway takes `role_arn` at
 * creation; pinning its own ARN would be a dependency cycle.
 */

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.aws_region}:${var.account_id}:gateway/*"]
    }
  }
}

resource "aws_iam_role" "gateway" {
  name               = var.role_name
  assume_role_policy = data.aws_iam_policy_document.assume_role.json

  tags = var.tags
}

data "aws_iam_policy_document" "gateway_permissions" {
  statement {
    sid       = "InvokeChatTarget"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [var.chat_target_lambda_arn]
  }

  statement {
    sid       = "InvokeInterceptor"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [var.identity_interceptor_lambda_arn]
  }
}

resource "aws_iam_role_policy" "gateway" {
  name   = "${var.role_name}-permissions"
  role   = aws_iam_role.gateway.id
  policy = data.aws_iam_policy_document.gateway_permissions.json
}

resource "aws_bedrockagentcore_gateway" "this" {
  name            = var.gateway_name
  role_arn        = aws_iam_role.gateway.arn
  protocol_type   = "MCP"
  authorizer_type = "CUSTOM_JWT"
  region          = var.aws_region

  protocol_configuration {
    mcp {
      supported_versions = ["2025-03-26", "2025-06-18", "2025-11-25"]
    }
  }

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url   = var.cognito_discovery_url
      allowed_clients = var.cognito_allowed_client_ids
      # A Cognito `client_credentials` access token carries no `aud` claim (as on Gateway #1).
      allowed_audience = null
      allowed_scopes   = var.cognito_allowed_scopes
    }
  }

  interceptor_configuration {
    interception_points = ["REQUEST"]

    input_configuration {
      pass_request_headers = true
    }

    interceptor {
      lambda {
        arn = var.identity_interceptor_lambda_arn
      }
    }
  }

  tags = var.tags

  # The role policy must exist before the gateway first assumes the role.
  depends_on = [aws_iam_role_policy.gateway]
}

resource "aws_bedrockagentcore_gateway_target" "chat" {
  gateway_identifier = aws_bedrockagentcore_gateway.this.gateway_id
  name               = "chat-with-supervisor"
  description        = "Gateway target for chat_with_supervisor, backed by the chat_target Lambda."

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = var.chat_target_lambda_arn

        tool_schema {
          inline_payload {
            name        = "chat_with_supervisor"
            description = "Send one message to the CRM supervisor agent and receive its answer. Pass the returned conversation_id on later calls to continue the same conversation."

            input_schema {
              type = "object"

              property {
                name        = "message"
                type        = "string"
                description = "The message to send, 1 to 8000 characters."
                required    = true
              }

              property {
                name        = "conversation_id"
                type        = "string"
                description = "A conversation_id returned by an earlier call; omit to start a new conversation."
                required    = false
              }
            }
          }
        }
      }
    }
  }
}

resource "aws_lambda_permission" "gateway_invoke_interceptor" {
  statement_id   = "AllowChatGatewayInvokeIdentityInterceptor"
  action         = "lambda:InvokeFunction"
  function_name  = var.identity_interceptor_lambda_function_name
  principal      = "bedrock-agentcore.amazonaws.com"
  source_arn     = aws_bedrockagentcore_gateway.this.gateway_arn
  source_account = var.account_id
}
