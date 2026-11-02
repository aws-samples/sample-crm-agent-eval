/**
 * Inputs for the streaming relay. The CloudFront-only `lambda:InvokeFunctionUrl` permission needs
 * the distribution ARN, so it lives in `modules/frontend_cdn`, which reads this module's
 * `function_name`.
 */

variable "function_name" {
  description = "Unique Lambda function name. Also names this module's IAM role and log group."
  type        = string
}

variable "handler" {
  description = "Lambda handler entry point inside the relay zip."
  type        = string
  default     = "index.handler"
}

variable "runtime" {
  description = "Lambda runtime identifier. Must be a Node.js runtime, which supports response streaming."
  type        = string
  default     = "nodejs24.x"

  validation {
    condition     = startswith(var.runtime, "nodejs")
    error_message = "runtime must be a nodejs* runtime: only Node.js managed runtimes support response streaming."
  }
}

variable "filename" {
  description = "Path to the relay deployment package built by `scripts/package_relay.sh`."
  type        = string
}

variable "source_code_hash" {
  description = "Base64-encoded SHA256 of the relay package, forcing redeploy on code change."
  type        = string
}

variable "timeout_seconds" {
  description = "Lambda invocation timeout in seconds. Must exceed the 600s chat turn wall clock."
  type        = number
  default     = 660

  validation {
    condition     = var.timeout_seconds > 600
    error_message = "timeout_seconds must exceed the 600s chat turn wall clock: a relay that dies at or before it makes a budget exhaustion and a relay timeout indistinguishable."
  }
}

variable "memory_size_mb" {
  description = "Lambda memory allocation in MB, sizing the Node runtime and the JWKS cache."
  type        = number
  default     = 512
}

variable "reserved_concurrent_executions" {
  description = <<-EOT
    Reserved concurrency for the relay. Must be greater than zero; 0 makes the function permanently
    uninvokable.

    One chat turn holds one invocation for up to `timeout_seconds`. The caller keeps the sum of every
    reserved concurrency in the deployment below the account pool minus unreserved headroom.

    No default; the caller states a value.
  EOT
  type        = number

  validation {
    condition     = var.reserved_concurrent_executions > 0
    error_message = "reserved_concurrent_executions must be > 0: a value of 0 makes the relay permanently uninvokable rather than merely throttled."
  }
}

variable "agent_runtime_arn" {
  description = <<-EOT
    ARN of the one AgentCore Runtime this relay may invoke
    (`module.agentcore.agent_runtime_arn`). The execution role is granted
    `bedrock-agentcore:InvokeAgentRuntime` on this ARN and its endpoint sub-resources and on nothing
    else — no wildcard, no second runtime.
  EOT
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:bedrock-agentcore:", var.agent_runtime_arn))
    error_message = "agent_runtime_arn must be a bedrock-agentcore ARN."
  }

  validation {
    condition     = !strcontains(var.agent_runtime_arn, "*")
    error_message = "agent_runtime_arn must name one runtime: a wildcard here would widen the relay's only meaningful permission."
  }
}

variable "cognito_issuer" {
  description = "Cognito user-pool issuer URL (`https://cognito-idp.<region>.amazonaws.com/<pool-id>`). The relay derives its JWKS URL from this and rejects any token whose `iss` differs."
  type        = string
}

variable "agent_runtime_qualifier" {
  description = <<-EOT
    Runtime endpoint qualifier the relay invokes, passed to the relay as `AGENT_RUNTIME_QUALIFIER`
    (`src/relay/src/runtime.ts`). The execution role's `InvokeAgentRuntime` grant covers the endpoint
    this value names.
  EOT
  type        = string
  default     = "DEFAULT"

  validation {
    condition     = length(var.agent_runtime_qualifier) > 0
    error_message = "agent_runtime_qualifier must be non-empty: a blank qualifier would make the endpoint ARN in the IAM grant unresolvable."
  }
}

variable "cognito_audience_parameter_name" {
  description = <<-EOT
    Name of the SSM parameter holding the SPA app-client id the relay requires. The relay reads it at
    cold start and rejects any token issued to a different client.
  EOT
  type        = string
}

variable "cognito_audience_parameter_arn" {
  description = <<-EOT
    ARN of that same parameter, for the execution role's `ssm:GetParameter` grant. The caller constructs
    it from region, account and name, not from `aws_ssm_parameter.…arn`, to avoid a module cycle.
  EOT
  type        = string

  validation {
    condition     = !strcontains(var.cognito_audience_parameter_arn, "*")
    error_message = "cognito_audience_parameter_arn must name one parameter: a wildcard would grant the relay every parameter in the account."
  }
}

variable "environment_kms_key_arn" {
  description = "KMS customer-managed key ARN encrypting the function environment variables and its log group."
  type        = string
  default     = null
}

variable "log_retention_days" {
  description = "Explicit CloudWatch Logs retention for the relay's log group."
  type        = number
  default     = 365

  validation {
    condition     = var.log_retention_days >= 365
    error_message = "log_retention_days must be at least 365 days."
  }
}

# ---------------------------------------------------------------------------
# VPC placement. Both required, with no default.
# ---------------------------------------------------------------------------

variable "vpc_subnet_ids" {
  description = <<-EOT
    Private subnet ids the relay's Hyperplane ENIs are placed in (`module.vpc.lambda_subnet_ids`).
    Required, with no default; at least two subnets.

    The relay's outbound calls (`bedrock-agentcore:InvokeAgentRuntime`, `ssm:GetParameter` and the
    Cognito JWKS fetch) leave through these subnets, via the VPC endpoints and the NAT gateway. A missing
    path presents as a hung turn, not as an authorization error.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.vpc_subnet_ids) >= 2
    error_message = "vpc_subnet_ids must span at least two subnets: a single-subnet relay is lost entirely when that zone is impaired."
  }
}

variable "vpc_security_group_ids" {
  description = <<-EOT
    Security groups for the relay's ENIs (`module.vpc.lambda_security_group_ids`). The group has no
    ingress; the relay is invoked through the Lambda control plane (a signed Function URL invoke).
  EOT
  type        = list(string)

  validation {
    condition     = length(var.vpc_security_group_ids) > 0
    error_message = "vpc_security_group_ids must name at least one group: a vpc_config with no group is rejected at create time."
  }
}

variable "tags" {
  description = "Resource tags. The provider's default_tags already carries the mandatory tags; this adds or overrides per-resource."
  type        = map(string)
  default     = {}
}

variable "eni_function_code_deny_policy_arn" {
  description = "ARN of the managed policy denying the Hyperplane ENI actions to function code (`modules/eni_function_code_deny`). Attached to this function's role."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:policy/", var.eni_function_code_deny_policy_arn))
    error_message = "eni_function_code_deny_policy_arn must be a customer-managed IAM policy ARN."
  }
}
