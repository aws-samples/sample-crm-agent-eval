/**
 * Outputs consumed by the root composition: frontend `apiBaseUrl`, the CloudFront `/api/*` origin
 * hostname, and ids for the gates and smoke scripts. A PRIVATE REST API has no `api_endpoint`
 * attribute; use `private_origin_domain_name`.
 */

output "api_id" {
  description = "Private REST API id."
  value       = aws_api_gateway_rest_api.this.id
}

# The `Host` the CloudFront `/api/*` VPC origin sends; CloudFront routes to the load balancer by ARN
# and does no DNS lookup on it. The `{api-id}-{vpce-id}.execute-api.{region}.amazonaws.com` form
# needs no `x-apigw-api-id` header, which browsers would not send on a CORS preflight.
output "private_origin_domain_name" {
  description = "Hostname for the CloudFront `/api/*` origin's `domain_name` (`<api-id>-<vpce-id>.execute-api.<region>.amazonaws.com`). Sent as the Host header to the private API; requires no custom domain and no `x-apigw-api-id`. Uses the first entry of `vpc_endpoint_ids`."
  value       = "${aws_api_gateway_rest_api.this.id}-${var.vpc_endpoint_ids[0]}.execute-api.${data.aws_region.current.region}.amazonaws.com"
}

data "aws_region" "current" {}

output "stage_invoke_url" {
  description = "Full invoke URL for the deployed stage. For this private API it is reachable only from inside the VPC through a named interface endpoint; it is not a publicly callable URL."
  value       = aws_api_gateway_stage.this.invoke_url
}

output "stage_name" {
  description = "Deployed stage name. Every route is served beneath it (`/<stage>/<tool_name>`), so a path-prefixed CloudFront behaviour in front of this API rewrites onto the stage path."
  value       = aws_api_gateway_stage.this.stage_name
}

output "execution_arn" {
  description = "API execution ARN, for Lambda resource-policy `source_arn` patterns outside this module if needed."
  value       = aws_api_gateway_rest_api.this.execution_arn
}

output "authorizer_id" {
  description = "Cognito authorizer id — every method in this module references this one authorizer; exposed for the Layer 3 gate's authorizer-coverage assertion."
  value       = aws_api_gateway_authorizer.cognito.id
}

output "route_tool_names" {
  description = "Sorted list of the tool names this module generated routes for, derived from `var.tool_lambda_invoke_arns`'s keys — for the Layer 3 gate to diff against `crm.toolspec.definitions.ALL_TOOLS`."
  value       = local.tool_names
}

output "access_log_group_name" {
  description = "CloudWatch Logs group name receiving API Gateway access logs."
  value       = aws_cloudwatch_log_group.access_logs.name
}

output "vpc_endpoint_ids" {
  description = "The `execute-api` interface endpoint ids this API is associated with and whose `aws:sourceVpce` its resource policy allows."
  value       = var.vpc_endpoint_ids
}
