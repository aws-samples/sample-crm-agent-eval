/**
 * Inputs for the CloudFront -> internal ALB -> `execute-api` endpoint path. The adopted network
 * arrives as variables, not `data` lookups.
 */

variable "name_prefix" {
  description = "Resource name prefix, from the root's `local.name_prefix`."
  type        = string
}

variable "vpc_id" {
  description = "Id of the adopted VPC the ALB and target group live in (`modules/vpc`'s vpc id output)."
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]{8}([0-9a-f]{9})?$", var.vpc_id))
    error_message = "vpc_id must be a real VPC id (`vpc-` followed by 8 or 17 hex digits)."
  }
}

variable "private_subnet_ids" {
  description = "Private subnet ids for the ALB. Also fixes the plan-time count of endpoint ENIs, since the interface endpoint places one ENI per subnet (see the `count` note in main.tf)."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "private_subnet_ids must name at least two subnets in distinct zones: an Application Load Balancer requires at least two Availability Zone subnets and cannot be created with one."
  }
}

variable "access_logs_prefix" {
  description = "Key prefix the ALB writes access logs under. No default; it must equal the prefix in the destination bucket policy grant (`<bucket>/<this prefix>/AWSLogs/<account>/*`). The root passes one local to both sides."
  type        = string

  validation {
    condition     = length(var.access_logs_prefix) > 0 && !can(regex("AWSLogs", var.access_logs_prefix))
    error_message = "access_logs_prefix must be non-empty and must not contain the string \"AWSLogs\": Elastic Load Balancing reserves that segment and rejects a prefix containing it."
  }
}

variable "execute_api_security_group_id" {
  description = "Security group id of the `execute-api` interface endpoint, the ALB single egress destination on 443."
  type        = string
}

# No default. Security group ids only: `security_group_zero_inbound` requires a security-group
# source, and a CIDR cannot be expressed here.
variable "alb_ingress_security_group_ids" {
  description = "Security group ids permitted to reach the ALB listener port: the security group the deployed CloudFront VPC origin presents. No default; see the first-deployment steps in `docs/runbook.md`."
  type        = set(string)

  validation {
    condition     = length(var.alb_ingress_security_group_ids) >= 1
    error_message = "alb_ingress_security_group_ids must name at least one security group: an ALB with no ingress rule accepts nothing and every /api/* request times out at the edge."
  }

  validation {
    condition     = alltrue([for id in var.alb_ingress_security_group_ids : can(regex("^sg-[0-9a-f]{8}([0-9a-f]{9})?$", id))])
    error_message = "every entry must be a security group id (`sg-` followed by 8 or 17 hex digits). A CIDR or prefix list id is refused on purpose: `security_group_zero_inbound` requires a security-group source, and a CIDR-shaped ingress is how 0.0.0.0/0 gets in."
  }
}

variable "access_logs_bucket" {
  description = "S3 bucket receiving ALB access logs. Required."
  type        = string
}

variable "listener_port" {
  description = "ALB listener port. 80 with the `http-only` origin protocol policy; 443 only if an origin certificate exists, which requires a domain the owner controls."
  type        = number
  default     = 80
}

variable "listener_protocol" {
  description = "ALB listener protocol. `HTTP` on the CloudFront->ALB hop: an accepted deviation from encryption-in-transit on that hop only. The ALB->endpoint hop is HTTPS regardless."
  type        = string
  default     = "HTTP"

  validation {
    condition     = contains(["HTTP", "HTTPS"], var.listener_protocol)
    error_message = "listener_protocol must be HTTP or HTTPS."
  }
}

variable "origin_protocol_policy" {
  description = "How CloudFront connects to the VPC origin. Defaults to `http-only`; `match-viewer` is rejected by validation, since the viewer is always HTTPS and this stack has no origin certificate."
  type        = string
  default     = "http-only"

  validation {
    condition     = contains(["http-only", "https-only"], var.origin_protocol_policy)
    error_message = "origin_protocol_policy must be http-only or https-only. match-viewer is refused: the viewer protocol is always HTTPS here, so it silently becomes https-only and needs an origin certificate that requires a domain the owner has declined to acquire."
  }
}

variable "tags" {
  description = "Resource tags. The provider's default_tags already covers the mandatory tags; this allows a per-resource override or addition."
  type        = map(string)
  default     = {}
}

# Addresses rather than ENI ids, resolved by the root so this module plans without credentials.
variable "execute_api_target_ips" {
  description = "Private IPv4 addresses of the `execute-api` interface endpoint's ENIs, one per entry in `private_subnet_ids`, resolved in the root. Registered as the target group's IP targets."
  type        = list(string)

  validation {
    condition     = length(var.execute_api_target_ips) >= 2
    error_message = "execute_api_target_ips must have one address per private subnet (at least two): a target group with fewer targets than AZs leaves an AZ with no path to the API."
  }

  validation {
    condition     = alltrue([for ip in var.execute_api_target_ips : can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}$", ip))])
    error_message = "every entry in execute_api_target_ips must be an IPv4 address. An ENI id (`eni-...`) here plans and then fails at apply with an unhelpful target-registration error."
  }
}
