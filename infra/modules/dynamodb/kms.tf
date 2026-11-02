/**
 * Customer-managed KMS key for the eleven entity tables, and its key policy. Roles that use the
 * tables are granted access in the `lambda_action` module; the key policy keeps the account-root
 * statement and denies key use and key administration outside the module's principal lists.
 */

resource "aws_kms_key" "dynamodb" {
  # checkov:skip=CKV2_AWS_64: the policy is managed by aws_kms_key_policy.dynamodb
  description             = "CMK for CRM DynamoDB table encryption at rest"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = {
    "data-classification" = var.data_classification
  }
}

resource "aws_kms_alias" "dynamodb" {
  name          = "alias/${var.name_prefix}dynamodb"
  target_key_id = aws_kms_key.dynamodb.key_id
}

resource "aws_kms_key_policy" "dynamodb" {
  key_id                             = aws_kms_key.dynamodb.id
  bypass_policy_lockout_safety_check = false

  lifecycle {
    precondition {
      condition     = contains(var.administrator_principal_arns, var.caller_principal_arn) && var.caller_principal_arn != "arn:aws:iam::${var.account_id}:root"
      error_message = "The caller running this plan is not an operator. Add its role or user ARN to crm_operator_principal_arns in terraform.tfvars, or run as a listed operator; nothing was applied."
    }
  }

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "Enable IAM User Permissions"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "DenyKeyUseToNonProjectPrincipals"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey*",
          "kms:ReEncrypt*",
          "kms:CreateGrant",
        ]
        Resource = "*"
        Condition = {
          ArnNotEquals = { "aws:PrincipalArn" = var.data_plane_principal_arns }
          Bool         = { "aws:PrincipalIsAWSService" = "false" }
        }
      },
      {
        Sid       = "DenyKeyAdministrationToNonOperators"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "kms:PutKeyPolicy",
          "kms:ScheduleKeyDeletion",
          "kms:DisableKey",
          "kms:RevokeGrant",
          "kms:DisableKeyRotation",
        ]
        Resource = "*"
        Condition = {
          ArnNotEquals = { "aws:PrincipalArn" = var.administrator_principal_arns }
        }
      },
    ]
  })
}
