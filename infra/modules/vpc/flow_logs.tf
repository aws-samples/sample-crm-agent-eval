/**
 * VPC flow logs. A connection blocked for lack of an endpoint in `endpoints.tf` appears only here,
 * as a `REJECT` record.
 */

resource "aws_cloudwatch_log_group" "flow_logs" {
  count = var.flow_log_enabled ? 1 : 0

  name              = "/aws/vpc/${var.name_prefix}/flow-logs"
  retention_in_days = var.flow_log_retention_days
  kms_key_id        = var.flow_log_kms_key_arn

  tags = var.tags
}

data "aws_iam_policy_document" "flow_logs_assume_role" {
  count = var.flow_log_enabled ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "flow_logs" {
  count = var.flow_log_enabled ? 1 : 0

  name               = "${var.name_prefix}-vpc-flow-logs-role"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume_role[0].json
  tags               = var.tags
}

# Scoped to this one log group and the stream/put actions, instead of the managed
# `VPCFlowLogsDeliveryRolePolicy`, which grants `logs:CreateLogGroup` and `logs:DescribeLogGroups`
# account-wide.
data "aws_iam_policy_document" "flow_logs_delivery" {
  count = var.flow_log_enabled ? 1 : 0

  statement {
    sid    = "DeliverFlowLogsToOwnGroup"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.flow_logs[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "flow_logs" {
  count = var.flow_log_enabled ? 1 : 0

  name   = "${var.name_prefix}-vpc-flow-logs"
  role   = aws_iam_role.flow_logs[0].id
  policy = data.aws_iam_policy_document.flow_logs_delivery[0].json
}

# `traffic_type = ALL`, so accepted connections to endpoints are recorded as well as rejected ones.
resource "aws_flow_log" "this" {
  count = var.flow_log_enabled ? 1 : 0

  vpc_id                   = var.vpc_id
  traffic_type             = "ALL"
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.flow_logs[0].arn
  iam_role_arn             = aws_iam_role.flow_logs[0].arn
  max_aggregation_interval = 60

  tags = merge(var.tags, { Name = "${var.name_prefix}-flow-logs" })

  depends_on = [aws_iam_role_policy.flow_logs]
}
