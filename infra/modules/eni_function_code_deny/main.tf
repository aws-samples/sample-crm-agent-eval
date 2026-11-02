/**
 * One customer-managed policy denying the Hyperplane ENI actions to function code, attached to
 * every VPC Lambda role by `lambda_action` and `agent_relay`.
 */

# Function-code calls carry `lambda:SourceFunctionArn` and Lambda's own ENI management does not, so
# this leaves the ENI actions each role is granted to the service only.
data "aws_iam_policy_document" "this" {
  statement {
    sid    = "DenyEniActionsToFunctionCode"
    effect = "Deny"
    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeSubnets",
      "ec2:DeleteNetworkInterface",
      "ec2:DetachNetworkInterface",
      "ec2:AssignPrivateIpAddresses",
      "ec2:UnassignPrivateIpAddresses",
    ]
    resources = ["*"]

    condition {
      test     = "Null"
      variable = "lambda:SourceFunctionArn"
      values   = ["false"]
    }
  }
}

resource "aws_iam_policy" "this" {
  name        = "${var.name_prefix}-deny-eni-actions-to-function-code"
  description = "Denies the Hyperplane ENI actions to Lambda function code; Lambda's own ENI management is unaffected."
  policy      = data.aws_iam_policy_document.this.json
}
