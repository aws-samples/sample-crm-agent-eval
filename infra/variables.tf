/**
 * Root input variables: deploy target (profile/region), network ids, and the mandatory default-tag
 * values applied to every taggable resource. The account id is resolved at runtime, never set here.
 */

variable "aws_profile" {
  description = "Named AWS CLI profile to use for all provider operations."
  type        = string
  default     = "default"
}

variable "aws_region" {
  description = "AWS region to deploy into: a US commercial region listed in `infra/regions.json`."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = can(regex("^us-(east|west)-[1-9]$", var.aws_region))
    error_message = "aws_region must be a US commercial region (`us-east-N` or `us-west-N`) listed in infra/regions.json."
  }
}

variable "service" {
  description = "Value for the mandatory `service` tag applied to every resource."
  type        = string
  default     = "crm-agent-eval"
}

variable "environment" {
  description = "Value for the mandatory `environment` tag applied to every resource."
  type        = string
  default     = "dev"
}

variable "owner" {
  description = "Value for the mandatory `owner` tag applied to every resource."
  type        = string
  default     = "crm-agent-eval-team"
}

variable "cost_center" {
  description = "Value for the mandatory `cost-center` tag applied to every resource."
  type        = string
  default     = "crm-agent-eval"
}

variable "vpc_id" {
  description = <<-EOT
    Id of the existing VPC this deployment adopts. The stack creates no VPC; it creates only the NAT
    gateway path, the route that path adds to the adopted private route tables, two security groups,
    and the VPC endpoints.

    No default. Set it in the gitignored `terraform.tfvars`; `terraform.tfvars.example` shows the shape
    with a placeholder.
  EOT
  type        = string
}

variable "vpc_private_subnet_ids" {
  description = <<-EOT
    Ids of the existing private subnets in the adopted VPC that every Lambda's ENIs are placed in. No
    default; set it in the gitignored `terraform.tfvars`.

    The AZ set is whatever these subnets are; it is not derived from the region.
  EOT
  type        = list(string)
}

variable "vpc_nat_public_subnet_id" {
  description = <<-EOT
    Id of an existing IGW-routed subnet in the adopted VPC for the NAT gateway to land in. Required when
    `vpc_nat_gateway_enabled` is true; the plan fails otherwise. No default.

    Must not also appear in `vpc_private_subnet_ids`; the module asserts that at plan time.
  EOT
  type        = string
  default     = null
}

variable "vpc_private_route_table_ids" {
  description = <<-EOT
    Route table ids the adopted private subnets use. No default.

    Must not be the VPC's main route table; a precondition refuses it at plan time.

    Every table named here receives a `0.0.0.0/0` route to this stack's NAT gateway and the S3/DynamoDB
    gateway-endpoint associations.
  EOT
  type        = list(string)
}

variable "vpc_runtime_subnet_ids" {
  description = <<-EOT
    Ids of the private subnets for the AgentCore Runtime's VPC-mode ENIs, separate from
    `vpc_private_subnet_ids` (see the vpc module's `runtime_subnet_ids`).

    Every subnet must be in an AgentCore-supported AZ for the region (`infra/regions.json`) and must
    not be `vpc_nat_public_subnet_id`; `scripts/preflight_adopted_vpc.sh` and a module precondition
    assert both. The Runtime runs in VPC mode, so an empty list fails the plan.
  EOT
  type        = list(string)
  default     = []
}

variable "vpc_runtime_route_table_ids" {
  description = <<-EOT
    Route table ids of the `vpc_runtime_subnet_ids` subnets, associated with the S3/DynamoDB gateway
    endpoints so ECR image-layer pulls have an S3 path. They receive no NAT default route (that stays
    on `vpc_private_route_table_ids`), so the Runtime has no internet path. Defaults to empty.
  EOT
  type        = list(string)
  default     = []
}

variable "runtime_source_vpce_report_only_enabled" {
  description = <<-EOT
    Stage 1: whether the runtime resource policies carry the report-only `aws:SourceVpce` Allow for any
    principal arriving through the bedrock-agentcore VPC endpoint. It grants nothing the relay-role
    Allow does not already cover for the relay; it shows, before the Deny, that traffic through this
    stack's endpoint is admitted by the SourceVpce condition.
  EOT
  type        = bool
  default     = true
}

variable "runtime_source_vpce_deny_enabled" {
  description = <<-EOT
    Stage 2: whether the runtime resource policies carry the inbound Deny. When true, both policies deny
    `InvokeAgentRuntime` from every principal not arriving through the bedrock-agentcore VPC endpoint
    (with `aws:ViaAWSService = false` exempting internal service calls).

    Defaults to false. Enable it only once the relay's calls are shown to carry `aws:SourceVpce`;
    otherwise every chat turn is denied. Set `-var runtime_source_vpce_deny_enabled=true` (or in a
    gitignored `*.auto.tfvars`) as a separate apply after stage 1 is verified.
  EOT
  type        = bool
  default     = false
}

variable "vpc_cidr_block" {
  description = <<-EOT
    The adopted VPC's IPv4 CIDR, published as an output for Layer 4 checks. Nothing is sized from it.
    It must match the adopted VPC.
  EOT
  type        = string
  default     = "10.20.0.0/20"
}

variable "vpc_nat_gateway_enabled" {
  description = <<-EOT
    Whether to create the egress-only internet path the relay's Cognito JWKS fetch requires.

    No default; `terraform plan` refuses until a value is set. `false` leaves the relay unable to
    authenticate any request (the user pool has a domain, so the `cognito-idp` interface endpoint cannot
    serve it). `true` creates a NAT gateway, billed hourly and per GB.

    Set it on the command line (`-var vpc_nat_gateway_enabled=true`) or in a gitignored `*.auto.tfvars`.
    The NAT gateway is egress-only: it admits no inbound connection (see `infra/modules/vpc/nat.tf`).
  EOT
  type        = bool
}

variable "api_origin_ingress_security_group_ids" {
  description = <<-EOT
    Security group ids permitted to reach the internal ALB's listener port (the `/api/*` private origin
    path). Passed to `modules/api_origin`, whose validation refuses anything that is not an `sg-` id.
    No default; set it in the gitignored `terraform.tfvars`.

    The value is the CloudFront service-managed group `CloudFront-VPCOrigins-Service-SG`, which AWS
    creates in the adopted VPC when `aws_cloudfront_vpc_origin` is first applied. The first deployment
    of this path is therefore two-phase: apply, read the group id, set this variable, apply again. See
    the `api_origin` variables and the runbook.
  EOT
  type        = set(string)
}

variable "vpc_flow_log_enabled" {
  description = "Whether to deliver VPC flow logs to CloudWatch Logs."
  type        = bool
  default     = true
}

variable "data_classification" {
  description = "Default value for the mandatory `data-classification` tag. Individual modules may override this per resource where a more specific classification applies."
  type        = string
  default     = "confidential"
}

variable "crm_operator_principal_arns" {
  description = <<-EOT
    IAM role or user ARNs of the operators who deploy, seed and smoke-test this stack. Admitted to the
    DynamoDB tables' data plane and to table and CMK administration; every principal outside these and
    the project's Lambda roles is denied. No default; set it in the gitignored
    `terraform.tfvars`. A plan whose caller is not listed fails at a precondition.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.crm_operator_principal_arns) > 0
    error_message = "crm_operator_principal_arns must list at least one operator role or user ARN."
  }

  validation {
    condition     = alltrue([for arn in var.crm_operator_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:(role|user)/[^*?]+$", arn))])
    error_message = "Every crm_operator_principal_arns entry must be an IAM role or user ARN (arn:aws:iam::<12 digits>:role/... or :user/...) with no `*` or `?`; the account root is not accepted."
  }
}

provider "aws" {
  profile = var.aws_profile
  region  = var.aws_region

  default_tags {
    tags = {
      service               = var.service
      environment           = var.environment
      owner                 = var.owner
      "cost-center"         = var.cost_center
      "data-classification" = var.data_classification
      AppName               = "crm-agent-eval"
    }
  }
}
