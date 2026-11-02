# Subnets are placed by `availability_zone_id`, with no availability-zone data source.
# The main route table is left untouched and unassociated; every subnet has an explicit association.

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "crm-agent-eval-bootstrap-vpc"
  }

  lifecycle {
    precondition {
      condition     = local.region_entry != null
      error_message = "aws_region \"${var.aws_region}\" is not in infra/regions.json; supported: ${join(", ", sort(keys(local.region_map.regions)))}."
    }
    precondition {
      condition     = length(local.region_az_ids) >= 2
      error_message = "infra/regions.json lists fewer than 2 agentcore_az_ids for ${var.aws_region}."
    }
    precondition {
      condition     = local.nat_az_id != null && alltrue([for id in local.chosen_az_ids : contains(local.region_az_ids, id)])
      error_message = "every bootstrap AZ id must be one of the agentcore_az_ids infra/regions.json lists for ${var.aws_region} (${join(", ", local.region_az_ids)}); got ${join(", ", local.chosen_az_ids)}."
    }
  }
}

resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "crm-agent-eval-bootstrap-default-sg"
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "crm-agent-eval-bootstrap-igw"
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 0)
  availability_zone_id    = local.nat_az_id
  map_public_ip_on_launch = false

  tags = {
    Name = "crm-agent-eval-bootstrap-public-${local.nat_az_id}"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "crm-agent-eval-bootstrap-public"
  }
}

resource "aws_route" "public_default" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_subnet" "private" {
  count = length(local.private_az_ids)

  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 10 + count.index)
  availability_zone_id    = local.private_az_ids[count.index]
  map_public_ip_on_launch = false

  tags = {
    Name = "crm-agent-eval-bootstrap-private-${local.private_az_ids[count.index]}"
  }
}

resource "aws_route_table" "private" {
  count = length(local.private_az_ids)

  vpc_id = aws_vpc.this.id

  tags = {
    Name = "crm-agent-eval-bootstrap-private-${local.private_az_ids[count.index]}"
  }
}

resource "aws_route_table_association" "private" {
  count = length(local.private_az_ids)

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_subnet" "runtime" {
  count = length(local.runtime_az_ids)

  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 20 + count.index)
  availability_zone_id    = local.runtime_az_ids[count.index]
  map_public_ip_on_launch = false

  tags = {
    Name = "crm-agent-eval-bootstrap-runtime-${local.runtime_az_ids[count.index]}"
  }
}

resource "aws_route_table" "runtime" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "crm-agent-eval-bootstrap-runtime"
  }
}

resource "aws_route_table_association" "runtime" {
  count = length(local.runtime_az_ids)

  subnet_id      = aws_subnet.runtime[count.index].id
  route_table_id = aws_route_table.runtime.id
}
