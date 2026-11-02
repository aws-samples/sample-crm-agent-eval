/**
 * Inputs for the AgentCore module: ECR + CodeBuild image build, Runtime, Gateway, and one gateway
 * target per action Lambda. Cognito ids and Lambda ARNs arrive as variables from the root.
 */

variable "aws_profile" {
  description = "Named AWS CLI profile used by the in-apply CodeBuild trigger's `local-exec` provisioner."
  type        = string
  default     = "default"
}

variable "aws_region" {
  description = "AWS region for the AgentCore resources and the in-apply build trigger."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix applied to the CodeBuild project, its IAM role, and its log group names."
  type        = string
  default     = "crm-agent-eval"
}

# ---------------------------------------------------------------------------
# ECR + CodeBuild image build
# ---------------------------------------------------------------------------

variable "ecr_repository_name" {
  description = "Name of the ECR repository holding the agent container image."
  type        = string
  default     = "crm-agent-eval-agent"
}

variable "kms_key_arn" {
  description = "KMS customer-managed key ARN encrypting the ECR repository and the module's CloudWatch Logs log groups (runtime and CodeBuild) at rest."
  type        = string
}

variable "image_retention_count" {
  description = "Number of most-recent tagged images retained by the ECR lifecycle policy."
  type        = number
  default     = 10
}

variable "image_tag" {
  description = "Tag applied to the image the agent_runtime resource consumes (e.g. a short content-hash-derived tag the caller mints alongside source_content_hash)."
  type        = string
  default     = "latest"
}

variable "source_bucket_arn" {
  description = "S3 bucket ARN holding the zipped agent source CodeBuild reads from as its `source`."
  type        = string
}

variable "source_kms_key_arn" {
  description = <<-EOT
    ARN of the customer-managed KMS key encrypting `source_bucket_arn`'s objects at rest, also used as
    the CodeBuild project's `encryption_key`. The CodeBuild role is granted `kms:Decrypt`,
    `kms:Encrypt`, `kms:GenerateDataKey*` and `kms:DescribeKey` on exactly this key. The key's own
    policy must also admit `aws_iam_role.codebuild` (or a broader principal it belongs to).
  EOT
  type        = string
}

variable "source_key" {
  description = "S3 object key, within source_bucket_arn, of the zipped agent source archive."
  type        = string
}

variable "source_content_hash" {
  description = <<-EOT
    Content hash of the agent source tree, used as this module's `null_resource.image_build` trigger
    value. The image build re-runs when this value changes between applies.
  EOT
  type        = string
}

variable "buildspec" {
  description = "Inline buildspec for the CodeBuild project (Docker build + ECR push steps)."
  type        = string
}

variable "log_retention_days" {
  description = "Explicit CloudWatch Logs retention for the CodeBuild project's log group."
  type        = number
  default     = 365

  validation {
    condition     = var.log_retention_days >= 365
    error_message = "log_retention_days must be at least 365 days."
  }
}

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

variable "agent_runtime_name" {
  description = "Name for the AgentCore Runtime resource. Must satisfy the CreateAgentRuntime pattern [a-zA-Z][a-zA-Z0-9_]{0,47} -- no hyphens."
  type        = string
  default     = "crm_agent_eval_runtime"
}

variable "agent_runtime_role_arn" {
  description = "Execution role ARN the AgentCore Runtime assumes to run the agent container."
  type        = string
}

variable "agent_runtime_environment_variables" {
  description = "Plaintext environment variables for the runtime. Anything sensitive must instead be a Secrets Manager / SSM Parameter Store reference resolved at runtime, never an inline secret value."
  type        = map(string)
  default     = {}
}

variable "runtime_network_mode" {
  description = <<-EOT
    AgentCore Runtime `networkMode`: "PUBLIC" or "VPC". VPC mode places the microVM's ENIs in the
    adopted VPC's Runtime subnets on a dedicated no-ingress security group; it governs egress only.
    Defaults to "PUBLIC"; the root composition sets "VPC" and wires the subnet and security-group
    variables.
  EOT
  type        = string
  default     = "PUBLIC"

  validation {
    condition     = contains(["PUBLIC", "VPC"], var.runtime_network_mode)
    error_message = "runtime_network_mode must be \"PUBLIC\" or \"VPC\"."
  }
}

variable "platform_version" {
  description = <<-EOT
    AgentCore Runtime `platformVersion`: "V1" or "V2". V2 restores each instance from a
    prepared snapshot; creates and updates take minutes. Switching either way is an in-place update.
    Rollback: set "V1" and apply (docs/runbook.md).
  EOT
  type        = string
  default     = "V2"

  validation {
    condition     = contains(["V1", "V2"], var.platform_version)
    error_message = "platform_version must be \"V1\" or \"V2\"."
  }
}

variable "runtime_subnet_ids" {
  description = <<-EOT
    Subnet ids for the Runtime's VPC-mode ENIs. Required (at least one) when runtime_network_mode is
    "VPC"; ignored under "PUBLIC". Set through the gitignored tfvars.

    Every subnet must be in an AgentCore-supported AZ for the region (`infra/regions.json`);
    `scripts/preflight_adopted_vpc.sh` asserts that at deploy time.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.runtime_subnet_ids : can(regex("^subnet-[0-9a-f]{8}([0-9a-f]{9})?$", id))])
    error_message = "every entry in runtime_subnet_ids must be a real subnet id (`subnet-` + 8 or 17 hex)."
  }

  validation {
    condition     = length(distinct(var.runtime_subnet_ids)) == length(var.runtime_subnet_ids)
    error_message = "runtime_subnet_ids contains a duplicate."
  }
}

variable "runtime_security_group_ids" {
  description = <<-EOT
    Security group ids for the Runtime's VPC-mode ENIs. Required (at least one) when
    runtime_network_mode is "VPC"; ignored under "PUBLIC". Must be the dedicated Runtime security group
    (no ingress, egress only to the endpoint security group and the gateway prefix lists), not the
    Lambda security group.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.runtime_security_group_ids : can(regex("^sg-[0-9a-f]{8}([0-9a-f]{9})?$", id))])
    error_message = "every entry in runtime_security_group_ids must be a real security group id (`sg-` + 8 or 17 hex)."
  }

  validation {
    condition     = length(distinct(var.runtime_security_group_ids)) == length(var.runtime_security_group_ids)
    error_message = "runtime_security_group_ids contains a duplicate."
  }
}

# ---------------------------------------------------------------------------
# Cognito CUSTOM_JWT — shared by Runtime and Gateway authorizer configuration.
# ---------------------------------------------------------------------------

variable "cognito_discovery_url" {
  description = "Cognito user pool OIDC discovery URL, ending in /.well-known/openid-configuration."
  type        = string
}

variable "cognito_allowed_client_ids" {
  description = "App client ids accepted as valid callers by the CUSTOM_JWT authorizer."
  type        = list(string)
}

variable "cognito_allowed_audiences" {
  description = "Accepted `aud` claim values for the CUSTOM_JWT authorizer."
  type        = list(string)
}

variable "cognito_allowed_scopes" {
  description = "Scopes required to be present on an inbound access token, enforced by the Gateway CUSTOM_JWT authorizer."
  type        = list(string)
  default     = []
}

variable "cognito_required_custom_claims" {
  description = <<-EOT
    Custom claims the Gateway CUSTOM_JWT authorizer requires on an inbound token, each matched by
    equality against one fixed `required_value` (for example a fixed `token_use` claim). Presence-only
    checks are not expressible here; the interceptor's `decode_identity` rejects a token without the
    identity claims. Defaults to empty.
  EOT
  type = list(object({
    claim_name     = string
    required_value = string
  }))
  default = []
}

# ---------------------------------------------------------------------------
# Gateway + REQUEST interceptor
# ---------------------------------------------------------------------------

variable "gateway_name" {
  description = "Name for the AgentCore Gateway resource."
  type        = string
  default     = "crm-agent-eval-gateway"
}

variable "gateway_role_arn" {
  description = "Execution role ARN the Gateway assumes, including to invoke each target Lambda via credential_provider_configuration.gateway_iam_role."
  type        = string
}

variable "identity_interceptor_lambda_arn" {
  description = "Invoke ARN of the identity_interceptor Lambda (src/gateway/identity_interceptor.py), attached as the Gateway's REQUEST interceptor."
  type        = string
}

variable "identity_interceptor_lambda_function_name" {
  description = "Function name of the identity_interceptor Lambda, used to grant this Gateway invoke permission in the function resource policy."
  type        = string
}

# ---------------------------------------------------------------------------
# Gateway targets — one per action Lambda
# ---------------------------------------------------------------------------

variable "tool_lambda_arns" {
  description = <<-EOT
    Map of tool_name -> that tool's action Lambda ARN (`module.lambda_action[tool_name].function_arn`),
    one entry per tool in `crm.toolspec.definitions.ALL_TOOLS`. Each entry produces exactly one gateway
    target. Its keys are a superset of the `api` module's tool routes: `get_page_context` is reachable
    by the agents and the Gateway but has no HTTP route (`local.agent_only_tools` in `infra/main.tf`).
    That the key set equals `ALL_TOOLS` is `scripts/check_toolspec_parity.py`'s check on
    `local.tools`, which this map is built from.
  EOT
  type        = map(string)

  validation {
    condition     = length(var.tool_lambda_arns) > 0
    error_message = "tool_lambda_arns must carry one entry per tool in crm.toolspec.definitions.ALL_TOOLS; got none."
  }
}

variable "tool_schemas" {
  description = <<-EOT
    Map of tool_name -> that tool's flattened MCP tool schema, for the required
    `target_configuration.mcp.lambda.tool_schema` block of each `aws_bedrockagentcore_gateway_target`.
    Carries exactly the keys of `tool_lambda_arns`; the gateway target resource's precondition fails
    the plan otherwise.

    The root composition derives each entry's `description` and `properties` from
    `crm.toolspec.definitions.ALL_TOOLS[tool_name]` (`.description` and the top-level keys of
    `.input_schema["properties"]`).

    Only top-level scalar properties, and arrays of scalars, are expressible; a property whose type is
    `object`, or an array of objects, needs this variable's shape extended.
  EOT
  type = map(object({
    description = string
    properties = list(object({
      name        = string
      type        = string
      description = optional(string)
      required    = optional(bool, false)
    }))
  }))

  validation {
    condition     = length(var.tool_schemas) > 0
    error_message = "tool_schemas must carry one entry per tool, the same keys as tool_lambda_arns; got none."
  }
}

variable "tags" {
  description = "Resource tags. The provider's default_tags already covers the mandatory tags; this allows a per-resource override or addition."
  type        = map(string)
  default     = {}
}

variable "data_classification" {
  description = "Value for the mandatory `data-classification` tag, applied per-resource (mirrors every sibling module's convention)."
  type        = string
}
