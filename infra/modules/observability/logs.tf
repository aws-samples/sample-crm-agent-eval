/**
 * Optional CloudTrail-to-CloudWatch-Logs delivery, gated by `var.enable_cloudtrail_cloudwatch_logs`
 * (off by default), encrypted with `aws_kms_key.logs` and with explicit retention.
 */

resource "aws_cloudwatch_log_group" "cloudtrail" {
  count = var.enable_cloudtrail_cloudwatch_logs ? 1 : 0

  name              = "/aws/cloudtrail/${var.name_prefix}crm-agent-eval-trail"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs.arn

  tags = merge(var.tags, {
    "data-classification" = var.data_classification
  })
}

data "aws_iam_policy_document" "cloudtrail_cloudwatch_logs_assume" {
  count = var.enable_cloudtrail_cloudwatch_logs ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cloudtrail_cloudwatch_logs" {
  count = var.enable_cloudtrail_cloudwatch_logs ? 1 : 0

  name               = "${var.name_prefix}cloudtrail-cwl-delivery"
  assume_role_policy = data.aws_iam_policy_document.cloudtrail_cloudwatch_logs_assume[0].json

  tags = merge(var.tags, {
    "data-classification" = var.data_classification
  })
}

data "aws_iam_policy_document" "cloudtrail_cloudwatch_logs_delivery" {
  count = var.enable_cloudtrail_cloudwatch_logs ? 1 : 0

  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.cloudtrail[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "cloudtrail_cloudwatch_logs" {
  count = var.enable_cloudtrail_cloudwatch_logs ? 1 : 0

  name   = "${var.name_prefix}cloudtrail-cwl-delivery"
  role   = aws_iam_role.cloudtrail_cloudwatch_logs[0].id
  policy = data.aws_iam_policy_document.cloudtrail_cloudwatch_logs_delivery[0].json
}
