/**
 * Customer-managed KMS key for this module's CloudWatch Logs log groups, exported as
 * `cloudwatch_logs_kms_key_arn` for other modules' log groups to reuse. The `logs.<region>.amazonaws.com`
 * principal gets `kms:Encrypt`, `kms:Decrypt`, `kms:ReEncrypt*`, `kms:GenerateDataKey*` and
 * `kms:Describe*`, restricted to this account and region's log groups via
 * `kms:EncryptionContext:aws:logs:arn`, per
 * https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/encrypt-log-data-kms.html. Key use by any
 * non-service principal outside `logs_key_user_principal_arns`, and key administration by any
 * principal outside `administrator_principal_arns`, is denied.
 */

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_iam_policy_document" "cloudwatch_logs_cmk" {
  # checkov:skip=CKV_AWS_109: key policy; `Resource: *` is this key alone
  # checkov:skip=CKV_AWS_111: key policy; `Resource: *` is this key alone
  # checkov:skip=CKV_AWS_356: key policy; `Resource: *` is this key alone
  statement {
    sid    = "EnableIAMUserPermissions"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    actions   = ["kms:*"]
    resources = ["*"]
  }

  statement {
    sid    = "AllowCloudWatchLogsUseOfTheKey"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logs.${data.aws_region.current.region}.amazonaws.com"]
    }

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:Describe*",
    ]
    resources = ["*"]

    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:*"]
    }
  }

  statement {
    sid    = "DenyKeyUseToNonProjectPrincipals"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey*",
      "kms:ReEncrypt*",
      "kms:CreateGrant",
    ]
    resources = ["*"]

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = var.logs_key_user_principal_arns
    }

    condition {
      test     = "Bool"
      variable = "aws:PrincipalIsAWSService"
      values   = ["false"]
    }
  }

  statement {
    sid    = "DenyKeyAdministrationToNonOperators"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = [
      "kms:PutKeyPolicy",
      "kms:ScheduleKeyDeletion",
      "kms:DisableKey",
      "kms:RevokeGrant",
      "kms:DisableKeyRotation",
      "kms:CreateAlias",
      "kms:UpdateAlias",
      "kms:DeleteAlias",
      "kms:TagResource",
      "kms:UntagResource",
      "kms:UpdateKeyDescription",
    ]
    resources = ["*"]

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = var.administrator_principal_arns
    }
  }
}

resource "aws_kms_key" "logs" {
  description                        = "CMK for CloudWatch Logs log group encryption at rest"
  deletion_window_in_days            = 30
  enable_key_rotation                = true
  policy                             = data.aws_iam_policy_document.cloudwatch_logs_cmk.json
  bypass_policy_lockout_safety_check = false

  lifecycle {
    precondition {
      condition     = contains(var.administrator_principal_arns, var.caller_principal_arn) && var.caller_principal_arn != "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
      error_message = "The caller running this plan is not an operator. Add its role or user ARN to crm_operator_principal_arns in terraform.tfvars, or run as a listed operator; nothing was applied."
    }
  }

  tags = merge(var.tags, {
    "data-classification" = var.data_classification
  })
}

resource "aws_kms_alias" "logs" {
  name          = "alias/${var.name_prefix}cloudwatch-logs"
  target_key_id = aws_kms_key.logs.key_id
}
