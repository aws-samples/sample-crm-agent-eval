/**
 * Inputs for the adopted VPC every Lambda runs in. VPC and subnet ids have no defaults:
 * `check_no_account_id` forbids them in tracked content, so they arrive through the gitignored
 * tfvars. The endpoint service sets are inputs declared at the call site.
 */

variable "name_prefix" {
  description = "Prefix for every resource name and `Name` tag this module creates."
  type        = string
}

variable "vpc_id" {
  description = <<-EOT
    Id of the existing VPC to adopt. This module creates no VPC.

    No default; set it through the gitignored tfvars. `terraform.tfvars.example` carries a placeholder.
  EOT
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]{8}([0-9a-f]{9})?$", var.vpc_id))
    error_message = "vpc_id must be a real VPC id (`vpc-` followed by 8 or 17 hex digits). The tfvars placeholder is not a deployable value — supply the adopted VPC's id."
  }
}

variable "private_subnet_ids" {
  description = <<-EOT
    Ids of the existing private subnets in the adopted VPC to place Lambda ENIs in. No default.

    These are the subnets whose route tables receive the NAT default route. They must be private; the
    module cannot detect a public subnet from its id.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "private_subnet_ids must name at least two subnets in distinct zones: a single-AZ Lambda VPC loses the function entirely when that zone is impaired."
  }

  validation {
    condition     = alltrue([for id in var.private_subnet_ids : can(regex("^subnet-[0-9a-f]{8}([0-9a-f]{9})?$", id))])
    error_message = "every entry in private_subnet_ids must be a real subnet id (`subnet-` followed by 8 or 17 hex digits)."
  }

  validation {
    condition     = length(distinct(var.private_subnet_ids)) == length(var.private_subnet_ids)
    error_message = "private_subnet_ids contains a duplicate: a repeated id would place every Lambda ENI in one zone while appearing multi-AZ."
  }
}

variable "nat_public_subnet_id" {
  description = <<-EOT
    Id of an existing IGW-routed subnet in the adopted VPC for the NAT gateway to land in. Required when
    `nat_gateway_enabled` is true; ignored otherwise. No default.

    Must not appear in `private_subnet_ids`; a precondition on `aws_nat_gateway.this` in `nat.tf`
    asserts that at plan time.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.nat_public_subnet_id == null || can(regex("^subnet-[0-9a-f]{8}([0-9a-f]{9})?$", coalesce(var.nat_public_subnet_id, "")))
    error_message = "nat_public_subnet_id must be a real subnet id (`subnet-` followed by 8 or 17 hex digits), or null when NAT is disabled."
  }

}

variable "private_route_table_ids" {
  description = <<-EOT
    Route table ids the adopted private subnets use. No default.

    Must not be the VPC's main route table, nor a table serving subnets outside `private_subnet_ids`;
    preconditions refuse both at plan time.

    Two consumers: the NAT default route in `nat.tf`, and the gateway endpoints' `route_table_ids` in
    `endpoints.tf`. Every table named here receives a `0.0.0.0/0` route to this stack's NAT gateway;
    `scripts/teardown.sh` removes exactly that route.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.private_route_table_ids) > 0
    error_message = "private_route_table_ids must name at least one route table: with none, the NAT gateway is created and billed while no subnet has a route to it — a silently useless egress path."
  }

  validation {
    condition     = alltrue([for id in var.private_route_table_ids : can(regex("^rtb-[0-9a-f]{8}([0-9a-f]{9})?$", id))])
    error_message = "every entry in private_route_table_ids must be a real route table id (`rtb-` followed by 8 or 17 hex digits)."
  }
}

variable "runtime_subnet_ids" {
  description = <<-EOT
    Subnet ids for the AgentCore Runtime's VPC-mode ENIs, separate from `private_subnet_ids` (which
    feeds every interface endpoint's `subnet_ids` and the `execute-api` positional-length contract). The
    Runtime reaches the interface endpoints through private DNS, so it needs no local endpoint ENI.

    Empty when the Runtime is not in VPC mode. When non-empty, the subnets must be in AgentCore-supported
    AZs for the region (`infra/regions.json`) and must not include `nat_public_subnet_id`;
    `preflight_adopted_vpc.sh` asserts the AZs and the precondition below asserts the disjointness.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.runtime_subnet_ids : can(regex("^subnet-[0-9a-f]{8}([0-9a-f]{9})?$", id))])
    error_message = "every entry in runtime_subnet_ids must be a real subnet id (`subnet-` followed by 8 or 17 hex digits)."
  }

  validation {
    condition     = length(distinct(var.runtime_subnet_ids)) == length(var.runtime_subnet_ids)
    error_message = "runtime_subnet_ids contains a duplicate."
  }
}

variable "runtime_route_table_ids" {
  description = <<-EOT
    Route table ids of the Runtime subnets, associated with the S3/DynamoDB gateway endpoints so ECR
    image-layer pulls have an S3 path. Separate from `private_route_table_ids`: these tables get no NAT
    default route, so the Runtime has no internet path. Empty when the Runtime is not in VPC mode.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.runtime_route_table_ids : can(regex("^rtb-[0-9a-f]{8}([0-9a-f]{9})?$", id))])
    error_message = "every entry in runtime_route_table_ids must be a real route table id (`rtb-` followed by 8 or 17 hex digits)."
  }
}

variable "vpc_cidr_block" {
  description = "The adopted VPC's IPv4 CIDR, used only for the `vpc_cidr_block` output that Layer 4 checks read."
  type        = string

  validation {
    condition     = can(cidrhost(var.vpc_cidr_block, 0))
    error_message = "vpc_cidr_block must be a valid IPv4 CIDR."
  }
}

variable "interface_endpoint_service_names" {
  description = <<-EOT
    Short service names for the interface (AWS PrivateLink) VPC endpoints this workload needs, e.g.
    `["kms", "logs"]`. Each is expanded to `com.amazonaws.<region>.<name>`. A Lambda call to an AWS
    service with no endpoint here presents as a connection timeout, not as an authorization error.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.interface_endpoint_service_names) > 0
    error_message = "interface_endpoint_service_names must not be empty: with no internet gateway and no NAT, a VPC with no interface endpoint can reach no AWS API at all."
  }

  validation {
    condition     = alltrue([for name in var.interface_endpoint_service_names : !startswith(name, "com.amazonaws.")])
    error_message = "interface_endpoint_service_names takes SHORT names (`kms`, not `com.amazonaws.<region>.kms`); the module adds the prefix and the region."
  }
}

variable "private_dns_disabled_services" {
  description = <<-EOT
    Interface-endpoint services whose private DNS is off. Defaults to `["execute-api"]`: private DNS on
    an `execute-api` endpoint would resolve `*.execute-api.<region>.amazonaws.com` to this endpoint for
    every workload in the adopted VPC. CloudFront routes to the VPC origin by load balancer ARN and uses
    the hostname only as the `Host` header.
  EOT
  type        = list(string)
  default     = ["execute-api"]
}

variable "gateway_endpoint_service_names" {
  description = "Short service names for the gateway VPC endpoints (route-table entries, not ENIs); only `s3` and `dynamodb` support this form."
  type        = list(string)
  default     = ["s3", "dynamodb"]

  validation {
    condition     = alltrue([for name in var.gateway_endpoint_service_names : contains(["s3", "dynamodb"], name)])
    error_message = "only `s3` and `dynamodb` offer gateway endpoints; every other service must go in interface_endpoint_service_names."
  }
}

variable "region" {
  description = "Region the endpoint service names are built for."
  type        = string
}

variable "nat_gateway_enabled" {
  description = <<-EOT
    Whether to create the egress-only internet path: an EIP and NAT gateway in the adopted IGW-routed
    subnet, the default route it adds to the adopted private route tables, and a 443-only egress rule on
    the Lambda group. The NAT gateway admits no inbound connection, and the private subnets are never
    associated with the public route table (see `nat.tf`).

    No default; plan refuses until a value is set. With `false` the relay cannot fetch the Cognito JWKS
    (the user pool has a domain, so the `cognito-idp` interface endpoint cannot serve it) and every agent
    turn fails authentication. `true` creates a NAT gateway, billed hourly and per GB.
  EOT
  type        = bool
}

variable "flow_log_enabled" {
  description = <<-EOT
    Whether to deliver VPC flow logs to CloudWatch Logs. A plain `bool` input, so the flow-log `count`
    is known at plan time.
  EOT
  type        = bool
  default     = true
}

variable "flow_log_retention_days" {
  description = "CloudWatch Logs retention, in days, for the flow-log group."
  type        = number
  default     = 365

  validation {
    condition     = var.flow_log_retention_days >= 365
    error_message = "flow_log_retention_days must be at least 365 days."
  }
}

variable "flow_log_kms_key_arn" {
  description = "KMS customer-managed key ARN encrypting the flow-log group."
  type        = string
  default     = null
}

variable "tags" {
  description = "Resource tags. The provider's default_tags already carries the mandatory tags; this adds or overrides per-resource."
  type        = map(string)
  default     = {}
}
