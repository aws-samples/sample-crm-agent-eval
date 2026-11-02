variable "aws_profile" {
  description = "Named AWS CLI profile for every provider operation. No default: the caller always names it."
  type        = string
}

variable "aws_region" {
  description = "Region to bootstrap: a US region listed in infra/regions.json, whose entry supplies the zone defaults."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = can(regex("^us-(east|west)-[1-9]$", var.aws_region))
    error_message = "aws_region must be a US commercial region (us-east-N or us-west-N) listed in infra/regions.json."
  }
}

variable "vpc_cidr" {
  description = "IPv4 /16 network CIDR of the bootstrap VPC; each subnet is a /24 carved from it."
  type        = string
  default     = "10.42.0.0/16"

  validation {
    condition = try(
      can(cidrnetmask(var.vpc_cidr))
      && endswith(var.vpc_cidr, "/16")
      && cidrsubnet(var.vpc_cidr, 0, 0) == var.vpc_cidr,
      false,
    )
    error_message = "vpc_cidr must be an IPv4 network CIDR of exactly /16, such as 10.42.0.0/16."
  }
}

variable "private_az_ids" {
  description = "AZ ids of the two private (Lambda) subnets, in output order. null: the region's bootstrap.private_az_ids in infra/regions.json."
  type        = list(string)
  default     = null

  validation {
    condition = var.private_az_ids == null ? true : (
      length(var.private_az_ids) >= 2
      && length(distinct(var.private_az_ids)) == length(var.private_az_ids)
    )
    error_message = "private_az_ids must hold at least two distinct AZ ids."
  }
}

variable "runtime_az_ids" {
  description = "AZ ids of the two AgentCore Runtime subnets, in output order. null: the region's bootstrap.runtime_az_ids in infra/regions.json."
  type        = list(string)
  default     = null

  validation {
    condition = var.runtime_az_ids == null ? true : (
      length(var.runtime_az_ids) >= 2
      && length(distinct(var.runtime_az_ids)) == length(var.runtime_az_ids)
    )
    error_message = "runtime_az_ids must hold at least two distinct AZ ids."
  }
}

variable "nat_az_id" {
  description = "AZ id of the public subnet the stack places its NAT gateway in. null: the region's bootstrap.nat_az_id in infra/regions.json."
  type        = string
  default     = null
}

variable "manage_apigw_account_logging" {
  description = "Whether this root creates the API Gateway CloudWatch Logs role and sets the account-wide cloudwatchRoleArn. Set false where another owner manages that setting."
  type        = bool
  default     = true
}

variable "service" {
  description = "Value for the mandatory `service` tag."
  type        = string
  default     = "crm-agent-eval"
}

variable "environment" {
  description = "Value for the mandatory `environment` tag."
  type        = string
  default     = "dev"
}

variable "owner" {
  description = "Value for the mandatory `owner` tag."
  type        = string
  default     = "crm-agent-eval-team"
}

variable "cost_center" {
  description = "Value for the mandatory `cost-center` tag."
  type        = string
  default     = "crm-agent-eval"
}

variable "data_classification" {
  description = "Value for the mandatory `data-classification` tag."
  type        = string
  default     = "internal"
}
