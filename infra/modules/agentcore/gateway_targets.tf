/**
 * One `aws_bedrockagentcore_gateway_target` per action Lambda in `var.tool_lambda_arns`, each with
 * its MCP tool schema from `var.tool_schemas` (the provider requires `tool_schema`). Targets invoke
 * their Lambda with the Gateway's own execution role (`var.gateway_role_arn`).
 *
 * The target `name` must match `^([0-9a-zA-Z][-]?){1,100}$`, so it is the tool name with `_`
 * replaced by `-`. The MCP tool name (`tool_schema.inline_payload.name`) stays the toolspec name.
 */

resource "aws_bedrockagentcore_gateway_target" "tool" {
  for_each = var.tool_lambda_arns

  gateway_identifier = aws_bedrockagentcore_gateway.this.gateway_id
  name               = replace(each.key, "_", "-")
  description        = "Gateway target for CRM tool ${each.key}, backed by its action Lambda."

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = each.value

        tool_schema {
          inline_payload {
            name        = each.key
            description = var.tool_schemas[each.key].description

            input_schema {
              type = "object"

              dynamic "property" {
                for_each = var.tool_schemas[each.key].properties

                content {
                  name        = property.value.name
                  type        = property.value.type
                  description = property.value.description
                  required    = property.value.required
                }
              }
            }
          }
        }
      }
    }
  }

  lifecycle {
    precondition {
      condition     = toset(keys(var.tool_lambda_arns)) == toset(keys(var.tool_schemas))
      error_message = "tool_lambda_arns and tool_schemas must carry identical key sets."
    }
  }
}
