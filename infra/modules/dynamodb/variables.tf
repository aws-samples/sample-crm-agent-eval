/**
 * Module input variables. Only `data-classification` is set per resource; the other mandatory tags
 * come from the root provider's `default_tags`.
 */

variable "name_prefix" {
  description = "Prefix applied to table and alias names, e.g. tenant or environment scoping."
  type        = string
  default     = ""
}

variable "data_classification" {
  description = "Value for the `data-classification` tag on resources in this module."
  type        = string
  default     = "confidential"
}

variable "account_id" {
  description = "Account id of the account-root statement in the CMK key policy."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account id."
  }
}

variable "data_plane_principal_arns" {
  description = "IAM role or user ARNs admitted to the tables' data plane and to the CMK's key use. Every other principal is denied by the table and key policies."
  type        = list(string)

  validation {
    condition     = length(var.data_plane_principal_arns) > 0
    error_message = "data_plane_principal_arns must not be empty: an empty list denies the data plane to every principal."
  }

  validation {
    condition     = alltrue([for arn in var.data_plane_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:(role|user)/[^*?]+$", arn))])
    error_message = "Every data_plane_principal_arns entry must be an IAM role or user ARN (arn:aws:iam::<12 digits>:role/... or :user/...) with no `*` or `?`; the account root is not accepted."
  }
}

variable "administrator_principal_arns" {
  description = "IAM role or user ARNs, or the account root, admitted to table and CMK administration. Every other principal is denied by the table and key policies."
  type        = list(string)

  validation {
    condition     = length(var.administrator_principal_arns) > 0
    error_message = "administrator_principal_arns must not be empty: an empty list denies table and key administration to every principal."
  }

  validation {
    condition     = alltrue([for arn in var.administrator_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:((role|user)/[^*?]+|root)$", arn))])
    error_message = "Every administrator_principal_arns entry must be an IAM role or user ARN with no `*` or `?`, or arn:aws:iam::<12 digits>:root."
  }
}

variable "caller_principal_arn" {
  description = "Role or user ARN of the principal running this plan. The table and key policies refuse to plan unless it is a listed administrator other than the account root, so no apply can lock its own caller out."
  type        = string
}
