/**
 * Inputs for the private REST API. The tool route set is the keys of `tool_lambda_invoke_arns`,
 * which must be exactly the routed tool names: `crm.toolspec.definitions.ALL_TOOLS` less the root's
 * `local.agent_only_tools`.
 */

variable "tool_lambda_invoke_arns" {
  description = <<-EOT
    Map of tool_name -> that tool's action Lambda invoke ARN (`module.lambda_action[tool_name].invoke_arn`),
    one entry per routed tool in `crm.toolspec.definitions.ALL_TOOLS`. Every route, integration and
    authorizer attachment is generated from this map (see `main.tf`). Must not be empty.
  EOT
  type        = map(string)

  validation {
    condition     = length(var.tool_lambda_invoke_arns) > 0
    error_message = "tool_lambda_invoke_arns must carry one entry per routed tool in crm.toolspec.definitions.ALL_TOOLS; got none."
  }
}

variable "tool_lambda_function_names" {
  description = <<-EOT
    Map of tool_name -> that tool's action Lambda function name
    (`module.lambda_action[tool_name].function_name`), used to grant API Gateway
    `lambda:InvokeFunction` per function. Carries exactly the key set of `tool_lambda_invoke_arns`;
    the REST API resource's precondition fails the plan otherwise.
  EOT
  type        = map(string)

  validation {
    condition     = length(var.tool_lambda_function_names) > 0
    error_message = "tool_lambda_function_names must carry the same keys as tool_lambda_invoke_arns; got none."
  }
}

variable "extra_routes" {
  description = <<-EOT
    Routes that are not toolspec tools, and so are absent from `tool_lambda_invoke_arns`. Merged with
    the tool map into the single local that generates every integration, route and permission, so
    these carry the Cognito authorizer on the same terms as the tool routes.

    The permitted keys are the allowlist validated below (`eval_trigger`, `capabilities`, `user_settings`).

    `method` is the HTTP method the client calls and defaults to `POST`; `capabilities` is a browser
    `GET` (`frontend/src/components/tuning/capabilityMatrix.ts`). The Lambda integration method is
    always `POST` for `AWS_PROXY`, whatever the client method.
  EOT
  type = map(object({
    invoke_arn    = string
    function_name = string
    method        = optional(string, "POST")
  }))
  default = {}

  validation {
    condition     = length(setsubtract(keys(var.extra_routes), ["eval_trigger", "capabilities", "user_settings"])) == 0
    error_message = "extra_routes accepts only the allowlisted non-tool route names: eval_trigger, capabilities, user_settings. Add a new name to this validation deliberately, not by passing one in."
  }

  # Only the two methods this API serves.
  validation {
    condition = length([
      for name, route in var.extra_routes : name if !contains(["GET", "POST"], route.method)
    ]) == 0
    error_message = "extra_routes[*].method must be GET or POST. Anything else is an unintended value, not a capability this API offers."
  }
}

variable "api_name" {
  description = "Name for the REST API."
  type        = string
  default     = "crm-agent-eval-api"
}

variable "stage_name" {
  description = "API Gateway stage name."
  type        = string
  default     = "prod"
}

# A `COGNITO_USER_POOLS` authorizer takes the pool ARN only, so there is no client-id or issuer
# input.
variable "cognito_user_pool_arn" {
  description = "ARN of the Cognito user pool whose tokens the authorizer accepts (the `cognito` module user pool ARN output). A COGNITO_USER_POOLS authorizer validates that the token was issued by this pool; it does not validate the app-client audience."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-zA-Z-]*:cognito-idp:", var.cognito_user_pool_arn))
    error_message = "cognito_user_pool_arn must be a cognito-idp user pool ARN. A user pool *id* here creates the authorizer successfully and refuses every request at runtime."
  }
}

variable "vpc_endpoint_ids" {
  description = "The `execute-api` interface VPC endpoint ids this private API is reachable through. Sets `endpoint_configuration.vpc_endpoint_ids` and is the allowlist in the API resource policy."
  type        = list(string)

  validation {
    condition     = length(var.vpc_endpoint_ids) >= 1
    error_message = "vpc_endpoint_ids must name at least one execute-api interface endpoint: a PRIVATE REST API with no endpoint is unreachable by every caller, including CloudFront."
  }

  validation {
    condition     = alltrue([for id in var.vpc_endpoint_ids : can(regex("^vpce-[0-9a-f]{8}([0-9a-f]{9})?$", id))])
    error_message = "every entry in vpc_endpoint_ids must be a real interface endpoint id (`vpce-` followed by 8 or 17 hex digits)."
  }
}

variable "throttle_burst_limit" {
  description = "Per-route burst limit (concurrent requests) enforced at the default-route throttle setting."
  type        = number
  default     = 50
}

variable "throttle_rate_limit" {
  description = "Per-route steady-state rate limit (requests/second) enforced at the default-route throttle setting."
  type        = number
  default     = 25
}

variable "log_retention_days" {
  description = "Explicit CloudWatch Logs retention for the API Gateway access log group."
  type        = number
  default     = 365

  validation {
    condition     = var.log_retention_days >= 365
    error_message = "log_retention_days must be at least 365 days."
  }
}

variable "access_log_kms_key_arn" {
  description = "KMS customer-managed key ARN encrypting the API Gateway access log group at rest."
  type        = string
}

variable "tags" {
  description = "Resource tags. The provider's default_tags already covers the mandatory tags; this allows a per-resource override or addition."
  type        = map(string)
  default     = {}
}
