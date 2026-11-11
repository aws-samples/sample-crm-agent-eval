/**
 * Root outputs consumed by the frontend deploy config and the smoke scripts. The MCP client secret
 * is `sensitive = true`; this composition stores it nowhere else.
 */

# The private VPC every Lambda runs in, and its endpoint set.
output "vpc_id" {
  description = "Id of the adopted VPC the stack deploys into. Read by the Layer 4 VPC smoke check."
  value       = module.vpc.vpc_id
}

output "vpc_lambda_subnet_ids" {
  description = "The private subnet ids every Lambda's `vpc_config` places ENIs in. Read by the Layer 4 VPC smoke check to assert each subnet's route table carries no route to a gateway."
  value       = module.vpc.lambda_subnet_ids
}

output "vpc_lambda_security_group_ids" {
  description = "The Lambda ENI security group. Read by Layer 4 to assert it has no ingress rule and no egress destination outside this VPC's endpoints."
  value       = module.vpc.lambda_security_group_ids
}

output "vpc_interface_endpoint_service_names" {
  description = "Fully-qualified service names of the interface endpoints created: the AWS services the workload can reach privately. Read by checks that assert a dependency is reachable."
  value       = module.vpc.interface_endpoint_service_names
}

output "vpc_flow_log_group_name" {
  description = "The VPC flow-log group name, or null when flow logs are disabled. Blocked egress appears here as REJECT records."
  value       = module.vpc.flow_log_group_name
}

output "cloudfront_domain_name" {
  description = "CloudFront distribution domain name serving the frontend build."
  value       = module.frontend_cdn.distribution_domain_name
}

# The frontend publish targets for `scripts/deploy.sh`: the private origin bucket and the
# distribution to invalidate.
output "frontend_origin_bucket_name" {
  description = "Private S3 bucket holding the frontend static build — the `aws s3 sync` target of the deploy script's publish step."
  value       = module.frontend_cdn.origin_bucket_name
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution id, used to invalidate cached assets after a frontend publish."
  value       = module.frontend_cdn.distribution_id
}

# Not a public endpoint: the private REST API's invoke hostname, reachable only through the
# `execute-api` VPC endpoint and sent by CloudFront as the origin `Host` header. Use `api_id` for the
# API's id.
output "api_endpoint" {
  description = "The private REST API invoke hostname. Not a public endpoint: it is reachable only through the `execute-api` VPC endpoint, and CloudFront uses it only as a `Host` header. Use `api_id` to identify the API."
  value       = module.api.private_origin_domain_name
}

output "api_id" {
  description = "The REST API's id, read by `scripts/smoke/api_routes.py` to identify the API."
  value       = module.api.api_id
}

output "api_stage_invoke_url" {
  description = "Full invoke URL for the deployed API stage."
  value       = module.api.stage_invoke_url
}

# The private `/api/*` path's ALB, target group, security group and VPC origin, for the smoke checks.
output "api_origin_alb_arn" {
  description = "Internal ALB fronting the private REST API. The CloudFront VPC origin references it by this ARN and performs no DNS lookup."
  value       = module.api_origin.alb_arn
}

output "api_origin_target_group_arn" {
  description = "Target group holding the `execute-api` endpoint ENI IPs. A T3 check reads `describe-target-health` here."
  value       = module.api_origin.target_group_arn
}

output "api_origin_alb_security_group_id" {
  description = "The ALB security group, the source of `aws_vpc_security_group_ingress_rule.endpoints_from_api_origin_alb`. A T3 check asserts the endpoint group admits this group."
  value       = module.api_origin.alb_security_group_id
}

output "api_origin_vpc_origin_id" {
  description = "Id of the CloudFront VPC origin, which `module.frontend_cdn` consumes as `api_origin_vpc_origin_id` for the `/api/*` origin `vpc_origin_config`. A T3 check asserts the deployed distribution routes `/api/*` through this origin."
  value       = module.api_origin.vpc_origin_id
}

output "api_origin_listener_protocol" {
  description = "The listener protocol deployed on the ALB. A check asserts the `http-only` listener against the resource."
  value       = module.api_origin.listener_protocol
}

output "agentcore_gateway_url" {
  description = "AgentCore Gateway invocation URL."
  value       = module.agentcore.gateway_url
}

output "agentcore_gateway_mcp_url" {
  description = "MCP endpoint of Gateway #1: `agentcore_gateway_url` with the `/mcp` path, which the deployed `gatewayUrl` omits."
  value       = "${trimsuffix(module.agentcore.gateway_url, "/mcp")}/mcp"
}

output "agentcore_runtime_arn" {
  description = "Deployed AgentCore Runtime ARN."
  value       = module.agentcore.agent_runtime_arn
}

output "agentcore_runtime_log_group_name" {
  description = "CloudWatch Logs group name for the AgentCore Runtime's default endpoint stdout."
  value       = module.agentcore.agent_runtime_log_group_name
}

output "cognito_user_pool_id" {
  description = "Cognito user pool id."
  value       = module.cognito.user_pool_id
}

output "cognito_spa_client_id" {
  description = "App client id for the SPA (public client)."
  value       = module.cognito.spa_client_id
}

output "cognito_mcp_client_id" {
  description = "App client id for MCP clients (confidential client)."
  value       = module.cognito.mcp_client_id
}

output "cognito_mcp_oauth_client_id" {
  description = "App client id for interactive MCP OAuth clients (public client, authorization code, loopback callbacks); accepted by Gateway #2 only."
  value       = module.cognito.mcp_oauth_client_id
}

output "cognito_mcp_client_secret" {
  description = "App client secret for the MCP client, empty after the first secret rotation (`scripts/rotate_mcp_client_secret.py`). Sensitive; never write it to a tracked file. Any copy kept elsewhere, for example in Secrets Manager, is made out of band; nothing in this stack stores one."
  value       = module.cognito.mcp_client_secret
  sensitive   = true
}

output "cognito_domain" {
  description = "Cognito prefix domain (no custom ACM certificate). Combine with `var.aws_region` as `https://<this>.auth.<region>.amazoncognito.com` to reach the `/oauth2/token` endpoint the MCP `client_credentials` flow uses; see `docs/mcp-client-guide.md`."
  value       = module.cognito.domain
}

output "cognito_token_endpoint" {
  description = "Fully-formed Cognito `/oauth2/token` endpoint URL for the MCP app client's `client_credentials` grant."
  value       = "https://${module.cognito.domain}.auth.${var.aws_region}.amazoncognito.com/oauth2/token"
}

output "dynamodb_table_names" {
  description = "Map of logical table name to physical DynamoDB table name, for the 11 tables."
  value       = module.dynamodb.table_names
}

output "crm_operator_principal_arns" {
  description = "The operator principals admitted by the DynamoDB table and CMK policies. Read by the Layer 4 `dynamodb_principal_deny` smoke check to assert the deployed policies' allowlists."
  value       = var.crm_operator_principal_arns
}

output "agent_relay_role_arn" {
  description = "The relay Lambda execution role ARN. Read by `scripts/smoke/iam_roles.py` to simulate the relay rows against the deployed role."
  value       = module.agent_relay.role_arn
}

output "agent_relay_function_name" {
  description = "The relay Lambda's function name, for the relay smoke check's `get-function-configuration` env-var assertions."
  value       = module.agent_relay.function_name
}

output "agent_relay_function_url" {
  description = "The relay Function URL, scheme included. The relay smoke check asserts an unauthenticated call to this hostname, not through the distribution, is denied."
  value       = module.agent_relay.function_url
}

output "eval_artifacts_bucket_name" {
  description = "The eval-artefacts bucket name, which embeds the account id. Read by `sweep_control` (as EVAL_ARTIFACTS_BUCKET) and by the presigned-URL smoke check."
  value       = module.eval_artifacts.bucket_name
}

output "eval_artifacts_prefix" {
  description = "The single key prefix artefacts are written under, and the only prefix the `sweep_control` artifacts grant covers. Read by `sweep_control` as EVAL_ARTIFACTS_PREFIX."
  value       = module.eval_artifacts.artifact_prefix
}

output "eval_artifacts_bucket_arn" {
  description = "The eval-artefacts bucket ARN, for a smoke check composing its own policy or simulation against it."
  value       = module.eval_artifacts.bucket_arn
}

# Read by `scripts/smoke/access_log_delivery.py`. Each log destination is published with the source
# bucket that logs to it.
output "frontend_cdn_logs_bucket_name" {
  description = "Destination bucket for the CloudFront origin bucket and eval-artefacts bucket S3 server access logs, and for CloudFront standard access logs. Log sources: `frontend_origin_bucket_name`, `eval_artifacts_bucket_name`, and the distribution."
  value       = module.frontend_cdn.logs_bucket_name
}

output "cloudtrail_access_logs_bucket_name" {
  description = "Destination bucket for the CloudTrail delivery bucket S3 server access logs. Its log source is `cloudtrail_s3_bucket_name`, a different bucket."
  value       = module.observability.cloudtrail_access_logs_bucket_name
}

output "cloudtrail_s3_bucket_name" {
  description = "The CloudTrail log-file delivery bucket, whose access logs land in `cloudtrail_access_logs_bucket_name`. Read by the delivery smoke check as the source side of its target assertion."
  value       = module.observability.cloudtrail_s3_bucket_name
}

# Read by `scripts/smoke/relay_iam.py`. Both are real resource attributes, not names built from a
# convention.
output "agent_relay_log_group_name" {
  description = "The relay Lambda CloudWatch log group name, taken from `aws_cloudwatch_log_group.this.name`. Read by the relay-IAM smoke check to simulate `logs:CreateLogStream`/`logs:PutLogEvents` against the deployed group."
  value       = module.agent_relay.log_group_name
}

# The role is created at the root and passed into `module.agentcore`, so it is output from here.
output "agentcore_runtime_role_arn" {
  description = "Execution role ARN of the AgentCore Runtime, the positive-control principal for the relay-IAM smoke check deny rows."
  value       = aws_iam_role.agentcore_runtime.arn
}

# The output name is the smoke checks' name for `module.cognito.gateway_scope_identifier`;
# `tests/unit/test_smoke_output_contract.py` asserts the root declares every name they read.
output "cognito_gateway_scope" {
  description = "The `gateway/invoke` resource-server scope identifier, re-exported from `module.cognito.gateway_scope_identifier`. Read by `scripts/smoke/gateway_tools.py`."
  value       = module.cognito.gateway_scope_identifier
}

output "eval_trigger_function_name" {
  description = "The `eval_trigger` Lambda's function name. Read by the smoke checks: `admin_authz` derives the Lambda name prefix from it, `eval_sfn` and `chat_path_iam` read its configuration, and `resource_protection` checks its log group."
  value       = module.lambda_action_eval_trigger.function_name
}


output "api_allowlist_kms_key_arn" {
  description = "The CMK encrypting the API client-allowlist SecureString parameter. Read by the smoke checks to assert the parameter's key, the key policy and rotation, and to simulate the roles' KMS access."
  value       = aws_kms_key.api_allowlist.arn
}
