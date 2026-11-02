# Names are a contract: `scripts/render_vpc_tfvars.py` maps them onto `infra/terraform.tfvars` keys.

output "vpc_id" {
  description = "Id of the bootstrap VPC. -> tfvars `vpc_id`."
  value       = aws_vpc.this.id
}

output "private_subnet_ids" {
  description = "Private (Lambda) subnet ids, in `private_az_ids` order. -> tfvars `vpc_private_subnet_ids`."
  value       = aws_subnet.private[*].id
}

output "private_route_table_ids" {
  description = "Route-less private route table ids, one per private subnet, same order. -> tfvars `vpc_private_route_table_ids`."
  value       = aws_route_table.private[*].id
}

output "nat_public_subnet_id" {
  description = "Id of the IGW-routed public subnet for the stack's NAT gateway. -> tfvars `vpc_nat_public_subnet_id`."
  value       = aws_subnet.public.id
}

output "runtime_subnet_ids" {
  description = "AgentCore Runtime subnet ids, in `runtime_az_ids` order. -> tfvars `vpc_runtime_subnet_ids`."
  value       = aws_subnet.runtime[*].id
}

output "runtime_route_table_ids" {
  description = "The route-less Runtime route table id, as a one-element list. -> tfvars `vpc_runtime_route_table_ids`."
  value       = [aws_route_table.runtime.id]
}

output "apigw_cloudwatch_role_arn" {
  description = "ARN of the API Gateway CloudWatch Logs role, or null when `manage_apigw_account_logging` is false. Evidence only."
  value       = one(aws_iam_role.apigw_cloudwatch[*].arn)
}

output "az_name_by_id" {
  description = "AZ id -> AZ name for every subnet this root created. Evidence only."
  value = merge(
    { (aws_subnet.public.availability_zone_id) = aws_subnet.public.availability_zone },
    { for s in aws_subnet.private : s.availability_zone_id => s.availability_zone },
    { for s in aws_subnet.runtime : s.availability_zone_id => s.availability_zone },
  )
}
