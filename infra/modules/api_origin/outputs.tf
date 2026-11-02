/**
 * Outputs consumed by the root composition, which wires them into
 * `modules/frontend_cdn`'s `/api/*` origin.
 */

output "vpc_origin_id" {
  description = "Id of the CloudFront VPC origin. Goes into the distribution origin's `vpc_origin_config.vpc_origin_id` for the `/api/*` behaviour."
  value       = aws_cloudfront_vpc_origin.this.id
}

output "alb_arn" {
  description = "Internal ALB ARN. The VPC origin references the ALB by this ARN, which is how CloudFront routes without resolving the origin's domain name."
  value       = aws_lb.this.arn
}

output "alb_dns_name" {
  description = "Internal ALB DNS name, for diagnostics only. It is not the CloudFront origin `domain_name`, which is `module.api.private_origin_domain_name`: API Gateway routes the private API on that Host header."
  value       = aws_lb.this.dns_name
}

output "target_group_arn" {
  description = "Target group holding the execute-api endpoint ENI IPs, for the T3 check to assert target health from."
  value       = aws_lb_target_group.execute_api.arn
}

output "alb_security_group_id" {
  description = "The ALB's security group id, for asserting its ingress source and single scoped egress."
  value       = aws_security_group.alb.id
}

output "listener_protocol" {
  description = "The listener protocol configured on the ALB, for a check asserting the deployed `HTTP` listener."
  value       = aws_lb_listener.this.protocol
}
