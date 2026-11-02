/**
 * Inputs for the observability module. The account id is not an input: the CloudTrail bucket
 * name's unique suffix comes from `data.aws_caller_identity`.
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

variable "log_retention_days" {
  description = "CloudWatch Logs retention, in days, for the CloudTrail CloudWatch Logs integration log group this module creates."
  type        = number
  default     = 365

  validation {
    condition     = var.log_retention_days >= 365
    error_message = "log_retention_days must be at least 365 days."
  }
}

variable "cloudtrail_s3_log_retention_days" {
  description = "Lifecycle expiration, in days, for CloudTrail delivered log objects in the trail S3 destination bucket."
  type        = number
  default     = 365
}

variable "enable_cloudtrail_cloudwatch_logs" {
  description = <<-EOT
    Whether the trail also delivers to a CloudWatch Logs log group (via
    `cloud_watch_logs_group_arn`/`cloud_watch_logs_role_arn`), in addition to its S3 destination.
    Defaults to false. The CMK this module creates already grants `logs.<region>.amazonaws.com` the
    actions CloudWatch Logs needs.
  EOT
  type        = bool
  default     = false
}

variable "eval_metric_namespace" {
  description = <<-EOT
    CloudWatch custom metric namespace reserved for eval-run metrics. No code
    under `src/` publishes a CloudWatch metric; eval metrics are written to the
    `matrix.json`/`.md`/`.csv` report files instead.
  EOT
  type        = string
  default     = "CrmAgentEval/Eval"
}

variable "tags" {
  description = "Resource tags. The provider's default_tags already covers the mandatory tags; this allows a per-resource override or addition."
  type        = map(string)
  default     = {}
}

variable "logs_key_user_principal_arns" {
  description = "IAM role or user ARNs admitted to key use on the CloudWatch Logs CMK: every execution role whose Lambda environment the key encrypts, plus the operators. Every other non-service principal is denied key use."
  type        = list(string)

  validation {
    condition     = length(var.logs_key_user_principal_arns) > 0
    error_message = "logs_key_user_principal_arns must not be empty: an empty list denies logs-key use to every principal."
  }

  validation {
    condition     = alltrue([for arn in var.logs_key_user_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:(role|user)/[^*?]+$", arn))])
    error_message = "Every logs_key_user_principal_arns entry must be an IAM role or user ARN (arn:aws:iam::<12 digits>:role/... or :user/...) with no `*` or `?`; the account root is not accepted."
  }
}

variable "operator_principal_arns" {
  description = "IAM role or user ARNs of the operators: the only non-service principals admitted to CloudTrail-key use and to the CloudTrail bucket's data plane."
  type        = list(string)

  validation {
    condition     = length(var.operator_principal_arns) > 0
    error_message = "operator_principal_arns must not be empty: an empty list denies CloudTrail-key use and log reads to every principal."
  }

  validation {
    condition     = alltrue([for arn in var.operator_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:(role|user)/[^*?]+$", arn))])
    error_message = "Every operator_principal_arns entry must be an IAM role or user ARN (arn:aws:iam::<12 digits>:role/... or :user/...) with no `*` or `?`; the account root is not accepted."
  }
}

variable "administrator_principal_arns" {
  description = "IAM role or user ARNs, or the account root, admitted to administration of both CMKs and of the CloudTrail bucket. Every other principal is denied it."
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
  description = "Role or user ARN of the principal running this plan. The key and bucket policies refuse to plan unless it is a listed administrator other than the account root, so no apply can lock its own caller out."
  type        = string
}
