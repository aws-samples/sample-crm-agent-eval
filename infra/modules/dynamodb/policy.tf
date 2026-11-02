/**
 * Principal boundary on the eleven tables: one resource policy per table denying the data
 * plane to every principal outside `data_plane_principal_arns` and table administration to every
 * principal outside `administrator_principal_arns`. Each policy covers the table and its indexes.
 */

data "aws_partition" "current" {}

data "aws_region" "current" {}

locals {
  # Built from plan-time-known parts, so the rendered policies are visible in the plan.
  table_policy_arns = {
    for name, table in local.tables :
    name => "arn:${data.aws_partition.current.partition}:dynamodb:${data.aws_region.current.region}:${var.account_id}:table/${table.name}"
  }
}

resource "aws_dynamodb_resource_policy" "tables" {
  for_each = local.tables

  resource_arn = each.value.arn

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
        Sid       = "DenyDataPlaneToNonProjectPrincipals"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:BatchGetItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:ConditionCheckItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:BatchWriteItem",
          "dynamodb:PartiQLSelect",
          "dynamodb:PartiQLInsert",
          "dynamodb:PartiQLUpdate",
          "dynamodb:PartiQLDelete",
          "dynamodb:CreateBackup",
          "dynamodb:ExportTableToPointInTime",
          "dynamodb:RestoreTableToPointInTime",
          "dynamodb:EnableKinesisStreamingDestination",
        ]
        Resource = [local.table_policy_arns[each.key], "${local.table_policy_arns[each.key]}/index/*"]
        Condition = {
          ArnNotEquals = { "aws:PrincipalArn" = var.data_plane_principal_arns }
        }
      },
      {
        Sid       = "DenyTableAdministrationToNonOperators"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "dynamodb:DeleteTable",
          "dynamodb:UpdateTable",
          "dynamodb:UpdateContinuousBackups",
          "dynamodb:UpdateTimeToLive",
          "dynamodb:PutResourcePolicy",
          "dynamodb:DeleteResourcePolicy",
          "dynamodb:TagResource",
          "dynamodb:UntagResource",
          "dynamodb:UpdateContributorInsights",
        ]
        Resource = [local.table_policy_arns[each.key], "${local.table_policy_arns[each.key]}/index/*"]
        Condition = {
          ArnNotEquals = { "aws:PrincipalArn" = var.administrator_principal_arns }
        }
      },
    ]
  })
}
