/**
 * Optional egress-only internet path for the relay's Cognito JWKS fetch (`src/relay/src/jwt.ts`),
 * which no VPC endpoint can serve: a user pool with a domain cannot be reached over PrivateLink. A
 * NAT gateway has no inbound path, and no Lambda is placed in the public subnet.
 *
 * `nat_gateway_enabled` has no default, so a plan fails until a value is set: `false` leaves the
 * relay unable to verify any token, `true` incurs recurring cost.
 *
 * Creates the EIP, the NAT gateway, the default route on the adopted private route tables and the
 * Lambda group's 443 egress rule; no public subnet, internet gateway or route table.
 */

# ---------------------------------------------------------------------------
# The adopted public subnet the NAT gateway lands in.
# ---------------------------------------------------------------------------

# `count` on a plain `bool` input, never on a resource attribute, which would be unknown once the
# resource is gone and block `terraform destroy`. The NAT gateway is zonal: one gateway in this
# subnet's AZ.
# ---------------------------------------------------------------------------
# The NAT gateway itself, the only billable resource here.
# ---------------------------------------------------------------------------

resource "aws_eip" "nat" {
  # checkov:skip=CKV2_AWS_19: the EIP is attached to the NAT gateway below, not to an EC2 instance
  count = var.nat_gateway_enabled ? 1 : 0

  domain = "vpc"

  tags = merge(var.tags, { Name = "${var.name_prefix}-nat" })
}

resource "aws_nat_gateway" "this" {
  count = var.nat_gateway_enabled ? 1 : 0

  allocation_id = aws_eip.nat[0].id
  subnet_id     = var.nat_public_subnet_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-nat" })

  lifecycle {
    precondition {
      condition     = var.nat_public_subnet_id != null
      error_message = "nat_gateway_enabled is true but nat_public_subnet_id is null: the NAT gateway has no subnet to land in. Supply an existing IGW-routed subnet id from the adopted VPC."
    }

    precondition {
      condition     = !contains(var.private_subnet_ids, coalesce(var.nat_public_subnet_id, "-"))
      error_message = "nat_public_subnet_id is also listed in private_subnet_ids. That subnet routes to an internet gateway, so every Lambda ENI placed in it would be directly internet-facing."
    }
  }
}

# ---------------------------------------------------------------------------
# The route added to the adopted private route tables.
# ---------------------------------------------------------------------------

# Adds `0.0.0.0/0 -> NAT` to adopted route tables shared with other workloads. `nat_gateway_id`,
# never `gateway_id`, which would route to an internet gateway. Keyed by `distinct` route table id, since several subnets can share one table.
#
# A route table serves every subnet associated with it, and the main table serves every subnet with
# no explicit association. The preconditions on the NAT gateway refuse the main route table and any
# table whose associations are not exactly this stack's subnets.
resource "aws_route" "private_nat" {
  for_each = var.nat_gateway_enabled ? { for id in distinct(var.private_route_table_ids) : id => id } : {}

  route_table_id         = each.value
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[0].id
}

# ---------------------------------------------------------------------------
# The Lambda group's egress to the internet, scoped to 443.
# ---------------------------------------------------------------------------

# `0.0.0.0/0` on 443 only, and only when NAT is on: AWS publishes no prefix list for the Cognito
# hostname.
resource "aws_vpc_security_group_egress_rule" "lambda_https_via_nat" {
  count = var.nat_gateway_enabled ? 1 : 0

  # No apostrophe: EC2 restricts a rule description to `a-zA-Z0-9. _-:/()#,@[]+=&;{}!*$`.
  description       = "HTTPS egress via the NAT gateway for the relay Cognito JWKS fetch."
  security_group_id = aws_security_group.lambda.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
