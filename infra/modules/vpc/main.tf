/**
 * Adopts an existing VPC read-only and creates only what this stack owns inside it: the optional NAT
 * path (`nat.tf`) and the route it adds to the adopted private tables, the security groups below, and
 * the endpoints in `endpoints.tf`. It creates no VPC, subnet, route table, internet gateway, default
 * security group or default route table.
 *
 * `tests/unit/test_infra_vpc.py` asserts at plan time that the only route added targets a NAT
 * gateway, never an internet gateway, and that no Lambda receives a public-subnet id.
 */

# ---------------------------------------------------------------------------
# The adopted VPC, referenced by id from variables.
# ---------------------------------------------------------------------------

# Ids arrive as variables rather than `data "aws_vpc"` / `data "aws_subnet"` lookups so the module
# plans offline in `tests/unit/_terraform_fixture.py`, which uses mock credentials. The ids are
# checked by shape in `variables.tf` and for existence by `scripts/preflight_adopted_vpc.sh`,
# which `scripts/deploy.sh` runs before apply.
# `test_the_module_declares_no_data_source_so_it_stays_plan_testable` asserts there is no `data`
# block in this module.

# ---------------------------------------------------------------------------
# Security groups, created by this stack.
# ---------------------------------------------------------------------------

# Attached to every Lambda's `vpc_config`. No ingress rule (invocations arrive through the Lambda
# control plane). Egress only to the endpoint group below, plus 443 via the NAT when enabled.
resource "aws_security_group" "lambda" {
  # checkov:skip=CKV2_AWS_5: attached to every Lambda's vpc_config in other modules via the `lambda_security_group_ids` output
  name        = "${var.name_prefix}-lambda"
  description = "Lambda ENIs: no ingress, egress only to the VPC endpoint group on 443."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-lambda" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "endpoints" {
  name        = "${var.name_prefix}-vpc-endpoints"
  description = "Interface VPC endpoint ENIs: ingress 443 from the Lambda group only, no egress."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpc-endpoints" })

  lifecycle {
    create_before_destroy = true
  }
}

# Rules are separate resources because the two groups reference each other; inline blocks would be a
# dependency cycle. With no inline rules, the default allow-all egress is removed.

resource "aws_vpc_security_group_egress_rule" "lambda_to_endpoints" {
  description                  = "HTTPS to the interface VPC endpoints. The only in-VPC egress any Lambda has."
  security_group_id            = aws_security_group.lambda.id
  referenced_security_group_id = aws_security_group.endpoints.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_lambda" {
  description                  = "HTTPS from the Lambda ENIs. One of two ingresses on the endpoint group: the root adds the private-origin ALB for health checks."
  security_group_id            = aws_security_group.endpoints.id
  referenced_security_group_id = aws_security_group.lambda.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

# ---------------------------------------------------------------------------
# The AgentCore Runtime's security group for VPC mode.
# ---------------------------------------------------------------------------

# Attached to the Runtime's microVM ENIs. Separate from the Lambda group, which has the NAT 443
# egress rule. No ingress rule; egress only to the endpoint group on 443 and to each gateway
# endpoint's managed prefix list, so the Runtime has no internet path.
resource "aws_security_group" "agent_runtime" {
  # checkov:skip=CKV2_AWS_5: attached to the AgentCore Runtime's network configuration in modules/agentcore
  name        = "${var.name_prefix}-agent-runtime"
  description = "AgentCore Runtime ENIs: no ingress, egress only to the VPC endpoint group and gateway prefix lists on 443."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-agent-runtime" })

  lifecycle {
    create_before_destroy = true

    # A Runtime subnet must not be the NAT's public subnet, which routes to an internet gateway.
    precondition {
      # `contains(list, null)` is an error and `||` does not short-circuit, so null is handled by
      # `coalesce`.
      condition     = !contains(var.runtime_subnet_ids, coalesce(var.nat_public_subnet_id, "__no_nat_subnet__"))
      error_message = "nat_public_subnet_id appears in runtime_subnet_ids: a Runtime ENI would land on the IGW-routed NAT subnet."
    }
  }
}

resource "aws_vpc_security_group_egress_rule" "agent_runtime_to_endpoints" {
  description                  = "HTTPS to the interface VPC endpoints. Reaches bedrock-runtime, ecr.api, ecr.dkr, lambda and logs cross-AZ via private DNS."
  security_group_id            = aws_security_group.agent_runtime.id
  referenced_security_group_id = aws_security_group.endpoints.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_agent_runtime" {
  description                  = "HTTPS from the Runtime ENIs. The second SG source on the endpoint group, alongside the Lambda group and the root module ALB rule."
  security_group_id            = aws_security_group.endpoints.id
  referenced_security_group_id = aws_security_group.agent_runtime.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

# ECR pulls image layers from S3 through the S3 gateway endpoint, reached via its managed prefix list.
resource "aws_vpc_security_group_egress_rule" "agent_runtime_to_gateway_endpoints" {
  for_each = toset(var.gateway_endpoint_service_names)

  description       = "HTTPS to the ${each.key} gateway endpoint managed prefix list."
  security_group_id = aws_security_group.agent_runtime.id
  prefix_list_id    = aws_vpc_endpoint.gateway[each.key].prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
