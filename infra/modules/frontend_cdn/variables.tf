/**
 * Inputs for the frontend CDN (private S3 origin + CloudFront). The API and relay origins arrive as
 * variables wired by the root composition.
 */

variable "name_prefix" {
  description = "Prefix applied to resource names, e.g. project or environment scoping."
  type        = string
  default     = ""
}

variable "data_classification" {
  description = "Value for the `data-classification` tag on resources in this module."
  type        = string
  default     = "confidential"
}

variable "api_gateway_domain_name" {
  description = "Origin hostname for the `/api/*` behaviour, from `module.api.private_origin_domain_name` (`<api-id>-<vpce-id>.execute-api.<region>.amazonaws.com`). It is the `Host` header the private REST API routes on, not a name CloudFront resolves; the VPC origin routes by ALB ARN."
  type        = string
}

variable "api_origin_vpc_origin_id" {
  description = "Id of the CloudFront VPC origin fronting the private REST API (`module.api_origin.vpc_origin_id`), consumed by the `/api/*` origin `vpc_origin_config`."
  type        = string

  # A VPC origin id is `vo_` plus 22 base62 characters; a hyphenated form is also accepted. This
  # rejects an ALB ARN or a bare hostname.
  validation {
    condition     = can(regex("^vo[_-][A-Za-z0-9]+$", var.api_origin_vpc_origin_id))
    error_message = "api_origin_vpc_origin_id must be a CloudFront VPC origin id (`vo_…`, e.g. `vo_` plus 22 base62 characters; a hyphen is also accepted): passing an ALB ARN or the endpoint name instead produces an origin the distribution cannot route to, and nothing in a plan would catch it."
  }
}

variable "agent_relay_function_url_domain" {
  description = "Host portion of the relay's Lambda Function URL that the `/agent/*` behaviour routes to (`module.agent_relay.function_url_domain`). The browser reaches the Runtime only through the relay."
  type        = string

  validation {
    condition     = !strcontains(var.agent_relay_function_url_domain, "/")
    error_message = "agent_relay_function_url_domain must be a bare host: a CloudFront custom origin `domain_name` rejects a scheme or a path (pass `module.agent_relay.function_url_domain`, not `function_url`)."
  }
}

variable "agent_relay_function_name" {
  description = "The relay function name (`module.agent_relay.function_name`), for the distribution-scoped `lambda:InvokeFunctionUrl` permission this module declares."
  type        = string
}

variable "api_gateway_stage_name" {
  description = "Stage name the REST API serves its routes beneath (`infra/modules/api/` `stage_name`). The `/api/*` viewer-request function rewrites `/api/<tool>` to `/<stage>/<tool>`, since CloudFront forwards the viewer path to a custom origin unchanged."
  type        = string
}

variable "alb_access_log_prefix" {
  description = "Key prefix the internal ALB writes access logs under, used to scope this module ELB log-delivery grant to `<logs bucket>/<prefix>/AWSLogs/<account>/*`. No default; it must equal the prefix the load balancer is configured with, and the root passes one local to both modules."
  type        = string

  validation {
    condition     = length(var.alb_access_log_prefix) > 0 && !can(regex("AWSLogs", var.alb_access_log_prefix))
    error_message = "alb_access_log_prefix must be non-empty and must not contain the string \"AWSLogs\": Elastic Load Balancing reserves that segment, so a prefix containing it can never match a delivered object's key."
  }
}

variable "origin_noncurrent_version_retention_days" {
  description = "Days a superseded (noncurrent) object version in the origin bucket is kept before it expires. Current objects never expire."
  type        = number
  default     = 30

  validation {
    condition     = var.origin_noncurrent_version_retention_days > 0
    error_message = "origin_noncurrent_version_retention_days must be > 0."
  }
}

variable "log_retention_days" {
  description = "Retention, in days, for the S3 bucket holding S3 access logs and CloudFront access logs."
  type        = number
  default     = 90
}

variable "operator_principal_arns" {
  description = "IAM role or user ARNs of the operators: the only non-service principals admitted to frontend-CMK use and to writes into the origin bucket."
  type        = list(string)

  validation {
    condition     = length(var.operator_principal_arns) > 0
    error_message = "operator_principal_arns must not be empty: an empty list denies frontend-key use and origin writes to every principal."
  }

  validation {
    condition     = alltrue([for arn in var.operator_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:(role|user)/[^*?]+$", arn))])
    error_message = "Every operator_principal_arns entry must be an IAM role or user ARN (arn:aws:iam::<12 digits>:role/... or :user/...) with no `*` or `?`; the account root is not accepted."
  }
}

variable "administrator_principal_arns" {
  description = "IAM role or user ARNs, or the account root, admitted to administration of the frontend CMK and the origin bucket. Every other principal is denied it."
  type        = list(string)

  validation {
    condition     = length(var.administrator_principal_arns) > 0
    error_message = "administrator_principal_arns must not be empty: an empty list denies key and bucket administration to every principal."
  }

  validation {
    condition     = alltrue([for arn in var.administrator_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:((role|user)/[^*?]+|root)$", arn))])
    error_message = "Every administrator_principal_arns entry must be an IAM role or user ARN with no `*` or `?`, or arn:aws:iam::<12 digits>:root."
  }
}

variable "caller_principal_arn" {
  description = "Role or user ARN of the principal running this plan. The key and origin-bucket policies refuse to plan unless it is a listed administrator other than the account root, so no apply can lock its own caller out."
  type        = string
}
