/**
 * Customer-managed KMS key for the frontend origin bucket. Its policy denies key use to every
 * non-service principal outside `operator_principal_arns`, and key administration to every principal
 * outside `administrator_principal_arns`.
 */

resource "aws_kms_key" "frontend" {
  description             = "CMK for CRM frontend CDN origin bucket encryption at rest"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = {
    "data-classification" = var.data_classification
  }
}

resource "aws_kms_alias" "frontend" {
  name          = "alias/${var.name_prefix}frontend-cdn"
  target_key_id = aws_kms_key.frontend.key_id
}

# Grants the OAC service principal `kms:Decrypt`, scoped by `AWS:SourceArn` to this distribution;
# without it every object fetch from the SSE-KMS origin bucket fails with `AccessDenied`.
resource "aws_kms_key_policy" "frontend" {
  key_id                             = aws_kms_key.frontend.id
  policy                             = data.aws_iam_policy_document.frontend_kms.json
  bypass_policy_lockout_safety_check = false

  lifecycle {
    precondition {
      condition     = contains(var.administrator_principal_arns, var.caller_principal_arn) && var.caller_principal_arn != "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
      error_message = "The caller running this plan is not an operator. Add its role or user ARN to crm_operator_principal_arns in terraform.tfvars, or run as a listed operator; nothing was applied."
    }
  }
}

data "aws_iam_policy_document" "frontend_kms" {
  # checkov:skip=CKV_AWS_109: key policy; `Resource: *` is this key alone
  # checkov:skip=CKV_AWS_111: key policy; `Resource: *` is this key alone
  # checkov:skip=CKV_AWS_356: key policy; `Resource: *` is this key alone
  # Keeps the default key policy's root grant, so the key stays administrable outside this state.
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
    sid    = "AllowCloudFrontOACDecrypt"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    actions   = ["kms:Decrypt"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.frontend.arn]
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
      values   = var.operator_principal_arns
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
