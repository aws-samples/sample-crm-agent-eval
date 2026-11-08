/**
 * Inputs for one `lambda_action` instantiation. Table names and ARNs arrive as variables from the
 * root composition.
 */

variable "function_name" {
  description = "Unique Lambda function name. Also names this instantiation's dedicated IAM role, log group, and CloudWatch alarms."
  type        = string
}

variable "handler" {
  description = "Lambda handler entry point, e.g. `crm.handlers.get_opportunity.handler`."
  type        = string
}

variable "runtime" {
  description = "Lambda runtime identifier."
  type        = string
  default     = "python3.13"
}

variable "source_code_hash" {
  description = "Base64-encoded SHA256 of the deployment package, forcing redeploy on code change."
  type        = string
}

variable "filename" {
  description = "Path to the zipped deployment package for this function."
  type        = string
}

variable "timeout_seconds" {
  description = "Lambda invocation timeout in seconds."
  type        = number
  default     = 30
}

variable "memory_size_mb" {
  description = "Lambda memory allocation in MB."
  type        = number
  default     = 256
}

variable "environment_variables" {
  description = "Plaintext environment variables for the function. Anything sensitive must instead be a Secrets Manager / SSM Parameter Store reference resolved at runtime, never an inline secret value."
  type        = map(string)
  default     = {}
}

variable "environment_kms_key_arn" {
  description = "KMS customer-managed key ARN encrypting the function environment variables. When null, Lambda uses its AWS-managed default key."
  type        = string
  default     = null
}

variable "reserved_concurrent_executions" {
  description = <<-EOT
    Reserved concurrency for this function. Must be greater than zero; 0 makes the function permanently
    uninvokable, and the validation below rejects it.

    Callers keep the sum of every instantiation's reserved concurrency, across the whole deployment,
    below the account's concurrency pool (1,000 by default) minus the headroom the root composition
    reserves for unreserved invocations. The root composition and the Layer 3 gate check that sum.
  EOT
  type        = number

  validation {
    condition     = var.reserved_concurrent_executions > 0
    error_message = "reserved_concurrent_executions must be > 0: a value of 0 makes the function permanently uninvokable rather than merely throttled."
  }
}

variable "dynamodb_grants" {
  description = <<-EOT
    Least-privilege per-table DynamoDB IAM grants for this function's execution role. Each entry names
    one table (or one table's index) and the exact actions permitted on it. The validation blocks below
    reject `dynamodb:*` actions and `Resource = "*"`.

    `leading_key_condition` is optional and applies only where the table's partition key encodes the
    caller's own identity (for example `reps`, keyed by `rep_id`). The four synthetic-id entity tables
    (`accounts`, `contacts`, `opportunities`, `activities`) are keyed by the record's own id, so they
    carry no `LeadingKeys` condition; rep-scoped isolation for them is enforced in `crm.authz`. See
    `table_action_role_matrix.json` for the disposition per table.
  EOT
  type = list(object({
    table_name = string
    table_arn  = string
    index_arns = optional(list(string), [])
    actions    = list(string)
    # `include_table_arn = false` makes an index-only statement: `dynamodb:LeadingKeys` matches the
    # addressed object's partition key, so one table may need separate index and base-table statements.
    include_table_arn = optional(bool, true)
    leading_key_condition = optional(object({
      attribute = string
      # StringEquals for a fixed key set, StringLike for a prefix pattern such as "eval-*".
      operator = optional(string, "StringEquals")
      values   = list(string)
    }), null)
    # Non-empty: the statement also requires `dynamodb:Select` to equal one of these values (for
    # example ["COUNT"]), ANDed with the LeadingKeys condition. A request without `Select` is denied.
    select_condition = optional(list(string), [])
  }))

  validation {
    condition = alltrue([
      for grant in var.dynamodb_grants : !contains(grant.actions, "dynamodb:*")
    ])
    error_message = "dynamodb_grants may not include the wildcard action \"dynamodb:*\"; enumerate exact actions."
  }

  validation {
    condition = alltrue([
      for grant in var.dynamodb_grants : alltrue([
        for action in grant.actions : can(regex("^dynamodb:[A-Za-z]+$", action)) && action != "dynamodb:*"
      ])
    ])
    error_message = "each dynamodb_grants action must be a single, explicit dynamodb: action — no wildcard patterns."
  }

  validation {
    condition = alltrue([
      for grant in var.dynamodb_grants :
      can(regex("^arn:aws:dynamodb:[a-z0-9-]+:[0-9]{12}:table/[A-Za-z0-9_.-]+$", grant.table_arn))
    ])
    error_message = "each dynamodb_grants table_arn must be one fully-qualified DynamoDB table ARN and may not be or contain a wildcard: \"*\", \"arn:aws:dynamodb:*:*:table/*\" and a trailing \"table/*\" are all rejected."
  }

  # A trailing `/index/*` is permitted; a wildcard anywhere else is not.
  validation {
    condition = alltrue([
      for grant in var.dynamodb_grants : alltrue([
        for index_arn in grant.index_arns :
        can(regex("^arn:aws:dynamodb:[a-z0-9-]+:[0-9]{12}:table/[A-Za-z0-9_.-]+/index/([A-Za-z0-9_.-]+|[*])$", index_arn))
      ])
    ])
    error_message = "each dynamodb_grants index_arn must be one table's index ARN; a wildcard is permitted only as the index name (\".../index/*\"), never in the region, account or table segment."
  }

  validation {
    condition = alltrue([
      for grant in var.dynamodb_grants :
      grant.leading_key_condition == null ? true : contains(["StringEquals", "StringLike"], grant.leading_key_condition.operator)
    ])
    error_message = "leading_key_condition.operator must be exactly \"StringEquals\" or \"StringLike\"."
  }
}

variable "denied_table_arns" {
  description = <<-EOT
    ARNs of every table this function is not granted access to via `dynamodb_grants`. The module
    attaches one explicit `Deny` on the full `dynamodb:*` action surface for these resources. An empty
    list is valid for a function with no DynamoDB grants; no dynamodb policy document is attached then.
  EOT
  type        = list(string)
  default     = []
}

variable "denied_own_table_actions" {
  description = <<-EOT
    Per-action explicit denies on granted tables. Each entry names one granted table and the actions
    this function is explicitly denied on it. Entries with an empty `actions` list are ignored.
  EOT
  type = list(object({
    table_name = string
    table_arn  = string
    index_arns = optional(list(string), [])
    actions    = list(string)
  }))
  default = []

  validation {
    condition = alltrue([
      for entry in var.denied_own_table_actions : alltrue([
        for action in entry.actions : can(regex("^dynamodb:[A-Za-z]+$", action)) && action != "dynamodb:*"
      ])
    ])
    error_message = "each denied_own_table_actions action must be a single, explicit dynamodb: action — a wildcard here would deny this function its own granted actions."
  }
}

variable "dynamodb_policy_managed" {
  description = <<-EOT
    Attach the DynamoDB document as customer-managed policies instead of the inline
    `<function_name>-dynamodb` policy: `<function_name>-dynamodb-allow` holds its Allow statements and
    `<function_name>-dynamodb-deny` its Deny statements. For a role whose inline-policy aggregate nears
    IAM's 10,240-char cap; each managed policy has its own 6,144-char cap and spends one of the role's
    managed-policy attachments.
  EOT
  type        = bool
  default     = false
}

variable "dynamodb_allow_statements_per_policy" {
  description = <<-EOT
    With `dynamodb_policy_managed`, the most Allow statements one managed policy holds; the Allows
    are split in lexical order of the grant index (0, 1, 10, 11, 2, …) into
    `<function_name>-dynamodb-allow`, `-allow-2`, ... `null` keeps them in one policy.
  EOT
  type        = number
  default     = null

  validation {
    condition     = var.dynamodb_allow_statements_per_policy == null || try(var.dynamodb_allow_statements_per_policy >= 1 && floor(var.dynamodb_allow_statements_per_policy) == var.dynamodb_allow_statements_per_policy, false)
    error_message = "dynamodb_allow_statements_per_policy must be null or a positive whole number."
  }
}

variable "dynamodb_kms_key_arn" {
  description = <<-EOT
    ARN of the customer-managed KMS key encrypting the DynamoDB tables named in `dynamodb_grants`
    (`module.dynamodb.kms_key_arn`), for the role's `kms:Decrypt`/`kms:GenerateDataKey` grant. Required
    whenever `dynamodb_grants` is non-empty.
  EOT
  type        = string
  default     = null
}

variable "dynamodb_kms_grant_enabled" {
  description = <<-EOT
    Whether to attach the `dynamodb_kms_key_arn` grant policy; it drives the policy's `count` with a
    plan-time-known value. Set true exactly when `dynamodb_grants` is non-empty and the tables use a
    customer-managed key; `dynamodb_kms_key_arn` must then be non-null.
  EOT
  type        = bool
  default     = false
}

variable "config_parameter_arns" {
  description = "SSM parameter ARNs the function reads at runtime. Each is granted `ssm:GetParameter` in the `<function_name>-logs` policy; no wildcard."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for arn in var.config_parameter_arns : can(regex("^arn:aws[a-z-]*:ssm:[a-z0-9-]+:[0-9]{12}:parameter/[A-Za-z0-9_./-]+$", arn))
    ])
    error_message = "Each config_parameter_arns entry must be one fully-qualified SSM parameter ARN with no wildcard."
  }
}

variable "cursor_kms_key_arn" {
  description = <<-EOT
    ARN of the customer-managed KMS key that seals pagination cursors. The role's
    `<function_name>-dynamodb-kms` policy grants `kms:Encrypt` and `kms:Decrypt` on it only with the
    encryption context `purpose = crm-cursor`. Required when `cursor_kms_grant_enabled` is true.
  EOT
  type        = string
  default     = null
}

variable "cursor_kms_grant_enabled" {
  description = "Whether the `<function_name>-dynamodb-kms` policy also grants use of `cursor_kms_key_arn`, as a plan-time-known flag. Requires `dynamodb_kms_grant_enabled`."
  type        = bool
  default     = false

  validation {
    condition     = !var.cursor_kms_grant_enabled || var.cursor_kms_key_arn != null
    error_message = "cursor_kms_grant_enabled is true but cursor_kms_key_arn is null: the cursor grant would name no key."
  }

  validation {
    condition     = !var.cursor_kms_grant_enabled || var.dynamodb_kms_grant_enabled
    error_message = "cursor_kms_grant_enabled requires dynamodb_kms_grant_enabled: the cursor grant is a statement of the `<function_name>-dynamodb-kms` policy."
  }
}

variable "table_action_role_matrix_entries" {
  description = <<-EOT
    This instantiation's rows in the project-wide table × action × role matrix. Each entry records, for
    one (table, action) pair this function is granted, whether `dynamodb:LeadingKeys` enforces rep
    isolation or application code does. Not read at plan time by this module; the Layer 3 gate asserts
    against it.
  EOT
  type = list(object({
    table                 = string
    action                = string
    role                  = string # this function's IAM role name, filled by the module from function_name
    leading_keys_enforced = bool
    isolation_mechanism   = string # "leading_keys" | "application_code" | "not_applicable"
  }))
  default = []
}

variable "gateway_target_role_arn" {
  description = <<-EOT
    The AgentCore Gateway target execution role ARN permitted to invoke this function via its resource
    policy, the only non-API-Gateway principal admitted for a CRM tool. `null` declares no such
    permission, for a function that is not a Gateway target.
  EOT
  type        = string
  default     = null
}

variable "attach_gateway_target_grant" {
  description = <<-EOT
    Whether to declare the Gateway-role invoke permission, as a plan-time-known flag. Must agree with
    `gateway_target_role_arn` being non-`null`; a precondition on the permission asserts that.
  EOT
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# VPC placement. Both required, with no default.
# ---------------------------------------------------------------------------

variable "vpc_subnet_ids" {
  description = <<-EOT
    Private subnet ids this function's Hyperplane ENIs are placed in (`module.vpc.lambda_subnet_ids`).
    Required, with no default.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.vpc_subnet_ids) >= 2
    error_message = "vpc_subnet_ids must span at least two subnets: a single-subnet function is lost entirely when that zone is impaired."
  }
}

variable "vpc_security_group_ids" {
  description = <<-EOT
    Security groups for this function's ENIs (`module.vpc.lambda_security_group_ids`). The group has no
    ingress.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.vpc_security_group_ids) > 0
    error_message = "vpc_security_group_ids must name at least one group: a vpc_config with no group is rejected at create time."
  }
}

variable "tags" {
  description = "Resource tags. The provider's default_tags already covers the mandatory tags; this allows a per-function override or addition."
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
