/**
 * Inputs for the Cognito module. Tags come from the root provider's `default_tags`.
 */

variable "user_pool_name" {
  description = "Name for the Cognito user pool."
  type        = string
  default     = "crm-agent-eval"
}

variable "pre_token_generation_lambda_arn" {
  description = <<-EOT
    ARN of the pre-token-generation Lambda (V2 event) that stamps `custom:rep_id`,
    `custom:book`, `custom:full_visibility`, and `custom:tenant` into the access
    token. Created by the root composition and passed in.
  EOT
  type        = string
}

variable "pre_token_generation_lambda_function_name" {
  description = <<-EOT
    Name of the pre-token-generation Lambda, used to scope the
    `aws_lambda_permission` that lets Cognito invoke it. Must be the function
    behind `pre_token_generation_lambda_arn`.
  EOT
  type        = string
}

variable "mcp_client_generate_secret" {
  description = <<-EOT
    Whether the MCP app client is confidential (generates a client secret) and uses
    `client_credentials`. Defaults to true: the MCP client is a machine-to-machine caller. The stack
    creates no Secrets Manager entry for the secret; see docs/mcp-client-guide.md.
  EOT
  type        = bool
  default     = true
}

variable "spa_callback_urls" {
  description = "Allowed OAuth callback URLs for the SPA app client."
  type        = list(string)
}

variable "spa_logout_urls" {
  description = "Allowed OAuth sign-out redirect URLs for the SPA app client."
  type        = list(string)
}

variable "mfa_configuration" {
  description = <<-EOT
    Pool-level MFA enforcement. Defaults to `OPTIONAL`; the SPA does not implement TOTP enrollment, so
    `ON` makes interactive sign-in through the deployed frontend impossible. TOTP stays enabled either
    way, so a user who enrolls is still challenged.

    `OFF` is rejected by validation. Set `ON` once the frontend can complete
    `MFA_SETUP`/`SOFTWARE_TOKEN_MFA`.
  EOT
  type        = string
  default     = "OPTIONAL"

  validation {
    condition     = contains(["ON", "OPTIONAL"], var.mfa_configuration)
    error_message = "mfa_configuration must be \"ON\" or \"OPTIONAL\"; \"OFF\" would disable the second factor entirely."
  }
}
