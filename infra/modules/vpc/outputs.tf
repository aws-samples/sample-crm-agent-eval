/**
 * Outputs consumed by every module that puts a function in this VPC. `lambda_subnet_ids` and
 * `lambda_security_group_ids` are the two halves of a Lambda `vpc_config`. `lambda_subnet_ids` is
 * `var.private_subnet_ids`, known at plan time; `lambda_security_group_ids` is a resource attribute
 * known only after apply.
 */

output "vpc_id" {
  description = "The VPC's id."
  value       = var.vpc_id
}

output "vpc_cidr_block" {
  description = "The VPC's IPv4 CIDR."
  value       = var.vpc_cidr_block
}

output "lambda_subnet_ids" {
  description = "Private subnet ids, for the `subnet_ids` half of every Lambda `vpc_config`. Returns `var.private_subnet_ids` alone, sorted; a precondition in `nat.tf` asserts `var.nat_public_subnet_id` is absent from it."
  value       = sort(var.private_subnet_ids)
}

output "lambda_security_group_ids" {
  description = "The single Lambda security group, for the `security_group_ids` half of every Lambda `vpc_config`. No ingress."
  value       = [aws_security_group.lambda.id]
}

output "endpoint_security_group_id" {
  description = "The interface-endpoint security group's id."
  value       = aws_security_group.endpoints.id
}

output "interface_endpoint_service_names" {
  description = "Fully-qualified service names of the interface endpoints created, for a Layer 4 check asserting the deployed set."
  value       = sort([for endpoint in aws_vpc_endpoint.interface : endpoint.service_name])
}

output "gateway_endpoint_service_names" {
  description = "Fully-qualified service names of the gateway endpoints created."
  value       = sort([for endpoint in aws_vpc_endpoint.gateway : endpoint.service_name])
}

output "nat_gateway_public_ip" {
  description = "The NAT gateway Elastic IP, or null when NAT is disabled. A Layer 4 check asserts the relay egress leaves by this address."
  value       = var.nat_gateway_enabled ? aws_eip.nat[0].public_ip : null
}

output "flow_log_group_name" {
  description = "The VPC flow-log group name, or null when flow logs are disabled. Read by the access-log delivery smoke check as its source-side target."
  value       = var.flow_log_enabled ? aws_cloudwatch_log_group.flow_logs[0].name : null
}

output "execute_api_vpc_endpoint_id" {
  description = <<-EOT
    The `execute-api` interface endpoint's id, for the private REST API's
    `endpoint_configuration.vpc_endpoint_ids` and its resource-policy allowlist. Read from the endpoint
    map by key. Fails at plan time if `execute-api` is absent from the endpoint list.
  EOT
  value       = aws_vpc_endpoint.interface["execute-api"].id

  # Fails with a named message rather than "Invalid index"; a null here would give the private API
  # no endpoint at all.
  precondition {
    condition     = contains(var.interface_endpoint_service_names, "execute-api")
    error_message = "`execute-api` is absent from interface_endpoint_service_names, so this output has no endpoint to return. The private REST API is reachable only through that endpoint: add \"execute-api\" to the list at the module call site."
  }
}

output "execute_api_network_interface_ids" {
  description = <<-EOT
    The `execute-api` endpoint's ENI ids, for the private-origin ALB's target group.

    `modules/api_origin` indexes this list positionally under `count = length(var.private_subnet_ids)`,
    so it holds exactly one entry per private subnet; the precondition below asserts the length.
  EOT
  value       = tolist(aws_vpc_endpoint.interface["execute-api"].network_interface_ids)

  precondition {
    condition     = local.execute_api_endpoint_present
    error_message = "`execute-api` is absent from interface_endpoint_service_names, so there are no ENIs to return. The private REST API's ALB target group has nothing to target: add \"execute-api\" to the list at the module call site."
  }
}

output "runtime_security_group_id" {
  description = <<-EOT
    The dedicated AgentCore Runtime security group's id, for the Runtime's VPC-mode
    `network_mode_config.security_groups`. No ingress; egress only to the endpoint group and the gateway
    prefix lists. Distinct from `lambda_security_group_ids`.
  EOT
  value       = aws_security_group.agent_runtime.id
}

output "runtime_subnet_ids" {
  description = "The Runtime VPC-mode subnet ids, for the Runtime `network_mode_config.subnets`. Returns `var.runtime_subnet_ids` sorted. Empty when the Runtime is not in VPC mode."
  value       = sort(var.runtime_subnet_ids)
}

output "bedrock_agentcore_vpc_endpoint_id" {
  description = <<-EOT
    The `bedrock-agentcore` interface endpoint's id, for the runtime resource policies' `aws:SourceVpce`
    condition: the Deny keyed on it denies every caller not arriving through this endpoint. Read from the
    endpoint map by key. Fails at plan time if `bedrock-agentcore` is absent from the interface list.
  EOT
  value       = aws_vpc_endpoint.interface["bedrock-agentcore"].id

  precondition {
    condition     = contains(var.interface_endpoint_service_names, "bedrock-agentcore")
    error_message = "`bedrock-agentcore` is absent from interface_endpoint_service_names, so this output has no endpoint to return. The runtime resource-policy Deny keys on it: add \"bedrock-agentcore\" to the list at the module call site."
  }
}

output "states_vpc_endpoint_id" {
  description = <<-EOT
    The `states` interface endpoint's id, for the root's endpoint policy scoping it to the eval sweep.
    Read from the endpoint map by key. Fails at plan time if `states` is absent from the
    interface list.
  EOT
  value       = aws_vpc_endpoint.interface["states"].id

  precondition {
    condition     = contains(var.interface_endpoint_service_names, "states")
    error_message = "`states` is absent from interface_endpoint_service_names, so this output has no endpoint to return. The eval sweep's endpoint policy attaches to it: add \"states\" to the list at the module call site."
  }
}

output "execute_api_security_group_id" {
  description = <<-EOT
    The security group on the `execute-api` endpoint's ENIs, and the ALB's single egress destination.
    Every interface endpoint in this module shares it; its ingress is 443 from the Lambda group, plus
    the ALB's 443 ingress rule, which the root composition attaches.
  EOT
  value       = aws_security_group.endpoints.id
}
