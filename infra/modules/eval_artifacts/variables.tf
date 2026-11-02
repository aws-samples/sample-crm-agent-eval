variable "account_id" {
  description = "AWS account id, part of the globally-unique bucket name. Passed in by the caller so the module plans offline."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be the 12-digit AWS account id."
  }
}

variable "name_prefix" {
  description = "Deployment name prefix, from the root composition's `local.name_prefix`. Part of the bucket name alongside the account id."
  type        = string
}

variable "data_classification" {
  description = "Value of the mandatory `data-classification` tag."
  type        = string
}

variable "kms_key_arn" {
  description = <<-EOT
    Customer-managed key ARN encrypting this bucket's objects, and the key the writer grant covers.
    Created by the caller; it must be a dedicated key (see README).
  EOT
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:kms:", var.kms_key_arn))
    error_message = "kms_key_arn must be a KMS key ARN. An alias or bare key id would leave the writer grant unscoped to a specific key."
  }
}

variable "access_log_bucket_id" {
  description = "Bucket id receiving this bucket S3 access logs. Must be a different bucket."
  type        = string
}

variable "artifact_prefix" {
  description = "Single key prefix all artefacts are written under, and the only prefix the writer grant covers."
  type        = string
  default     = "sweeps"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]*$", var.artifact_prefix))
    error_message = "artifact_prefix must be lowercase alphanumeric with hyphens, and must not start or end with a slash — the policy appends `/*` itself, so a slash here would produce a `//*` resource that matches nothing."
  }
}

variable "artifact_retention_days" {
  description = <<-EOT
    Lifecycle expiry in days, applied to current objects, noncurrent versions and incomplete multipart
    uploads alike. Artefacts are reproducible from a sweep with the same arguments and `dataset_seed`.
  EOT
  type        = number
  default     = 30

  validation {
    condition     = var.artifact_retention_days > 0 && var.artifact_retention_days <= 90
    error_message = "artifact_retention_days must be between 1 and 90. Zero would be rejected by S3; a long retention contradicts the reproducibility argument this bucket's `force_destroy = true` disposition rests on."
  }
}

variable "writer_role_name" {
  description = <<-EOT
    IAM role name the writer policy is attached to (the sweep worker's role). `null` (the default)
    attaches nothing. The grant's scope comes from the policy's own resources, not from this value.
  EOT
  type        = string
  default     = null
}

variable "data_plane_principal_arns" {
  description = "IAM role or user ARNs admitted to the bucket's object reads, lists and writes. Every other principal is denied them by the bucket policy."
  type        = list(string)

  validation {
    condition     = length(var.data_plane_principal_arns) > 0
    error_message = "data_plane_principal_arns must not be empty: an empty list denies the bucket's data plane to every principal."
  }

  validation {
    condition     = alltrue([for arn in var.data_plane_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:(role|user)/[^*?]+$", arn))])
    error_message = "Every data_plane_principal_arns entry must be an IAM role or user ARN (arn:aws:iam::<12 digits>:role/... or :user/...) with no `*` or `?`; the account root is not accepted."
  }
}

variable "administrator_principal_arns" {
  description = "IAM role or user ARNs, or the account root, admitted to bucket administration. Every other principal is denied it by the bucket policy."
  type        = list(string)

  validation {
    condition     = length(var.administrator_principal_arns) > 0
    error_message = "administrator_principal_arns must not be empty: an empty list denies bucket administration to every principal."
  }

  validation {
    condition     = alltrue([for arn in var.administrator_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:((role|user)/[^*?]+|root)$", arn))])
    error_message = "Every administrator_principal_arns entry must be an IAM role or user ARN with no `*` or `?`, or arn:aws:iam::<12 digits>:root."
  }
}

variable "caller_principal_arn" {
  description = "Role or user ARN of the principal running this plan. The bucket policy refuses to plan unless it is a listed administrator other than the account root, so no apply can lock its own caller out."
  type        = string
}
