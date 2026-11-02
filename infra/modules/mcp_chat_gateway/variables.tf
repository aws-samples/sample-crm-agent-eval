/**
 * Inputs for the MCP chat Gateway (Gateway #2). The Cognito values are the same mechanism as
 * Gateway #1's (`modules/agentcore`) with this gateway's own scope.
 */

variable "gateway_name" {
  description = "Name of the AgentCore Gateway."
  type        = string
}

variable "role_name" {
  description = "Name of this gateway's own execution role."
  type        = string
}

variable "aws_region" {
  description = "Region of the gateway; also the region segment of the trust policy's `aws:SourceArn` pattern."
  type        = string
}

variable "account_id" {
  description = "Account id for the trust policy's `aws:SourceAccount` and `aws:SourceArn` conditions and the interceptor permission's `source_account`."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account id."
  }
}

variable "cognito_discovery_url" {
  description = "OIDC discovery URL of the Cognito user pool, as on Gateway #1."
  type        = string
}

variable "cognito_allowed_client_ids" {
  description = "Cognito app client ids the `CUSTOM_JWT` authorizer accepts, as on Gateway #1."
  type        = list(string)

  validation {
    condition     = length(var.cognito_allowed_client_ids) > 0
    error_message = "cognito_allowed_client_ids must not be empty."
  }
}

variable "cognito_allowed_scopes" {
  description = "Scopes the `CUSTOM_JWT` authorizer accepts: this gateway's own chat scope, never Gateway #1's."
  type        = list(string)

  validation {
    condition     = length(var.cognito_allowed_scopes) > 0
    error_message = "cognito_allowed_scopes must not be empty: CreateGateway rejects an empty allowedScopes, and an empty list would not constrain callers."
  }
}

variable "identity_interceptor_lambda_arn" {
  description = "ARN of the shared REQUEST interceptor Lambda."
  type        = string
}

variable "identity_interceptor_lambda_function_name" {
  description = "Function name of the shared REQUEST interceptor Lambda, for this gateway's invoke permission."
  type        = string
}

variable "chat_target_lambda_arn" {
  description = "ARN of the `chat_with_supervisor` target Lambda; the only target this gateway's role may invoke."
  type        = string
}

variable "tags" {
  description = "Tags for the gateway and its role. The provider's default_tags carries the mandatory set."
  type        = map(string)
  default     = {}
}
