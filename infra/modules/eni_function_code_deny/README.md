# `eni_function_code_deny`

One customer-managed IAM policy, `<name_prefix>-deny-eni-actions-to-function-code`, holding the
`DenyEniActionsToFunctionCode` statement. Every `lambda_action` instantiation and `agent_relay`
attach it to their execution role through `eni_function_code_deny_policy_arn`.

See also: the [documentation map](../../../README.md#documentation), the
[runbook](../../../docs/runbook.md#layer-4-per-resource-smoke) and the [security
baseline](../../../docs/security-baseline.md#h-7). Modules named here:
[`agent_relay`](../agent_relay/README.md), [`lambda_action`](../lambda_action/README.md).

## Statement

`Deny` on `*` for `ec2:CreateNetworkInterface`, `ec2:DescribeNetworkInterfaces`,
`ec2:DescribeSubnets`, `ec2:DeleteNetworkInterface`, `ec2:DetachNetworkInterface`,
`ec2:AssignPrivateIpAddresses` and `ec2:UnassignPrivateIpAddresses`, when `lambda:SourceFunctionArn`
is present (`Null` = `false`).

Calls made by function code carry that key. The calls Lambda makes to create and manage a VPC
function's ENIs do not ([Lambda guide: `lambda:SourceFunctionArn`](https://docs.aws.amazon.com/lambda/latest/dg/permissions-source-function-arn.html)).
So each role's inline `ManageOwnHyperplaneEnis` Allow can be used only by the service. The actions
exclude X-Ray and CloudWatch Logs, which Lambda also calls with that key.

The statement lives in a managed policy rather than in each role's inline `vpc_access` document
because managed policies do not count toward a role's 10,240-character inline-policy quota.

## Inputs

| Variable | Required | Notes |
|---|---|---|
| `name_prefix` | yes | Part of the policy name. |

## Outputs

| Output | Notes |
|---|---|
| `policy_arn` | Passed to every VPC Lambda module as `eni_function_code_deny_policy_arn`. |

Without this statement, function code could use the inline `ManageOwnHyperplaneEnis` Allow on `*`
to delete or detach any ENI in the account.
