/**
 * VPC endpoints: the AWS APIs reachable from this VPC without the optional NAT path. `s3` and
 * `dynamodb` use gateway endpoints (a route-table prefix-list entry, no ENI, no hourly charge);
 * every other service uses an interface endpoint (an ENI per subnet, billed hourly plus per GB).
 */

# ---------------------------------------------------------------------------
# Interface endpoints — one per service, an ENI in every private subnet.
# ---------------------------------------------------------------------------

# `private_dns_enabled = true` lets an unmodified SDK call reach the endpoint through the service's
# public hostname.
resource "aws_vpc_endpoint" "interface" {
  for_each = toset(var.interface_endpoint_service_names)

  vpc_id             = var.vpc_id
  service_name       = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type  = "Interface"
  subnet_ids         = var.private_subnet_ids
  security_group_ids = [aws_security_group.endpoints.id]

  # Private DNS is off for `private_dns_disabled_services`; see that variable for why `execute-api` is
  # in it on an adopted VPC.
  private_dns_enabled = !contains(var.private_dns_disabled_services, each.key)

  tags = merge(var.tags, { Name = "${var.name_prefix}-${each.key}" })
}

# ---------------------------------------------------------------------------
# Gateway endpoints — a route-table entry, associated with every private table.
# ---------------------------------------------------------------------------

resource "aws_vpc_endpoint" "gateway" {
  for_each = toset(var.gateway_endpoint_service_names)

  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type = "Gateway"

  # The Runtime subnets' route tables are associated too, so ECR layer pulls from S3 have a route.
  # They do not get the NAT default route (`nat.tf`).
  route_table_ids = distinct(concat(var.private_route_table_ids, var.runtime_route_table_ids))

  tags = merge(var.tags, { Name = "${var.name_prefix}-${each.key}" })
}

# A gateway endpoint is reached over the route table, not through the endpoint security group, so
# Lambdas need egress to each gateway endpoint's managed prefix list (that service's addresses in this
# region).
resource "aws_vpc_security_group_egress_rule" "lambda_to_gateway_endpoints" {
  # Keyed on the service-name set, which is known at plan time, rather than on
  # `aws_vpc_endpoint.gateway`; the prefix list is looked up below.
  for_each = toset(var.gateway_endpoint_service_names)

  # No apostrophe: EC2 restricts a rule description to `a-zA-Z0-9. _-:/()#,@[]+=&;{}!*$`.
  description       = "HTTPS to the ${each.key} gateway endpoint managed prefix list."
  security_group_id = aws_security_group.lambda.id
  prefix_list_id    = aws_vpc_endpoint.gateway[each.key].prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# ---------------------------------------------------------------------------
# The `execute-api` endpoint's ENIs, for the private-origin ALB's target group.
# ---------------------------------------------------------------------------

# The ENI ids, output for the root to resolve into IPs (a `data "aws_network_interface"` read here
# would need credentials at plan time). The consumer indexes this list by position under `count`, so
# it must hold exactly one entry per `private_subnet_ids` entry; the length is asserted at plan time.
locals {
  execute_api_endpoint_present = contains(var.interface_endpoint_service_names, "execute-api")
}
