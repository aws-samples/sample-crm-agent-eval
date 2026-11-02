# Zone ids come from `infra/regions.json` unless a variable overrides them; preconditions on `aws_vpc.this`.

locals {
  region_map     = jsondecode(file("${path.module}/../regions.json"))
  region_entry   = try(local.region_map.regions[var.aws_region], null)
  region_az_ids  = try(local.region_entry.agentcore_az_ids, [])
  private_az_ids = var.private_az_ids != null ? var.private_az_ids : try(local.region_entry.bootstrap.private_az_ids, [])
  runtime_az_ids = var.runtime_az_ids != null ? var.runtime_az_ids : try(local.region_entry.bootstrap.runtime_az_ids, [])
  nat_az_id      = var.nat_az_id != null ? var.nat_az_id : try(local.region_entry.bootstrap.nat_az_id, null)
  chosen_az_ids  = concat(local.private_az_ids, local.runtime_az_ids, local.nat_az_id == null ? [] : [local.nat_az_id])
}
