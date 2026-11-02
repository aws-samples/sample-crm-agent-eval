# Account-wide API Gateway CloudWatch Logs setting. The 6.71.0 `aws_api_gateway_account` schema
# has no `reset_on_delete` argument; see README.md for the destroy behaviour.

resource "aws_iam_role" "apigw_cloudwatch" {
  count = var.manage_apigw_account_logging ? 1 : 0

  name        = "crm-agent-eval-apigw-cloudwatch-logs"
  description = "Lets API Gateway write execution and access logs to CloudWatch Logs for this account and region."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "apigateway.amazonaws.com" }
        Action    = "sts:AssumeRole"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "apigw_cloudwatch" {
  count = var.manage_apigw_account_logging ? 1 : 0

  role       = aws_iam_role.apigw_cloudwatch[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "this" {
  count = var.manage_apigw_account_logging ? 1 : 0

  cloudwatch_role_arn = aws_iam_role.apigw_cloudwatch[0].arn

  depends_on = [aws_iam_role_policy_attachment.apigw_cloudwatch]
}
