# `bootstrap_account` root

A separate Terraform root, applied once per account **before** the stack, which outlives it. It creates
the network the stack adopts ([`infra/modules/vpc`](../modules/vpc/README.md)) and the account-wide API Gateway CloudWatch Logs
role and setting. It creates no NAT gateway, EIP, VPC endpoint or flow log: the stack creates those
inside the adopted VPC.

See also: the [documentation map](../../README.md#documentation), the
[runbook](../../docs/runbook.md#scriptsbootstrap_accountsh) and the [security
baseline](../../docs/security-baseline.md). Modules named here: [`vpc`](../modules/vpc/README.md).

## Resources

| Resource | Shape |
|---|---|
| `aws_vpc.this` | `vpc_cidr`, DNS support and DNS hostnames on |
| `aws_default_security_group.this` | adopted with zero ingress and zero egress rules |
| `aws_internet_gateway.this` | used only by the stack's NAT gateway |
| `aws_subnet.public` | `/24` in `nat_az_id`, `map_public_ip_on_launch = false`; route table `0.0.0.0/0 → IGW` only |
| `aws_subnet.private[*]` | `/24` per `private_az_ids` entry; one route table each, **no routes** (the stack adds its NAT route) |
| `aws_subnet.runtime[*]` | `/24` per `runtime_az_ids` entry; one shared route table, **no routes** (the stack adds gateway-endpoint associations, never NAT) |
| `aws_iam_role.apigw_cloudwatch` | `crm-agent-eval-apigw-cloudwatch-logs`, trusts `apigateway.amazonaws.com` only, managed `AmazonAPIGatewayPushToCloudWatchLogs` |
| `aws_api_gateway_account.this` | sets the account+region `cloudwatchRoleArn` to that role |

Subnets are placed by `availability_zone_id`, not by a data source, so the root plans offline
(`tests/unit/test_bootstrap_account_plan.py`). The zone ids come from the region's entry in
`infra/regions.json` (read by `locals.tf` as `../regions.json`) unless a zone variable overrides
them. The map is the only copy of the zone ids: no `.tf` file here holds one. Every subnet has an
explicit route-table association; the VPC's main route table is left untouched.

The role, its attachment and the account setting exist only when `manage_apigw_account_logging` is
`true`.

## Inputs

| Variable | Default | Notes |
|---|---|---|
| `aws_profile` | required | named profile for every provider call |
| `aws_region` | `us-east-1` | must match `^us-(east\|west)-[1-9]$` (validation) and be a key in `infra/regions.json` (precondition) |
| `vpc_cidr` | `10.42.0.0/16` | IPv4 network CIDR of exactly `/16` (validated); subnets are the `/24`s `cidrsubnet(vpc_cidr, 8, n)`: public 0, private 10–11, Runtime 20–21 |
| `private_az_ids` | `null` → the region's `bootstrap.private_az_ids` in `infra/regions.json` | when set: ≥ 2 distinct ids, each in the region's `agentcore_az_ids` |
| `runtime_az_ids` | `null` → the region's `bootstrap.runtime_az_ids` | same rule |
| `nat_az_id` | `null` → the region's `bootstrap.nat_az_id` | one of the region's `agentcore_az_ids` |
| `manage_apigw_account_logging` | `true` | `false` where another owner manages the account setting |
| `service`, `environment`, `owner`, `cost_center`, `data_classification` | `crm-agent-eval`, `dev`, `crm-agent-eval-team`, `crm-agent-eval`, `internal` | provider `default_tags`, plus `component = bootstrap-account` and `AppName = crm-agent-eval` |

## Outputs

`scripts/render_vpc_tfvars.py` reads the first six by name and writes them into the gitignored
`infra/terraform.tfvars`; `tests/unit/test_bootstrap_outputs_contract.py` pins the mapping.

| Output | Type | `infra/terraform.tfvars` key |
|---|---|---|
| `vpc_id` | string | `vpc_id` |
| `private_subnet_ids` | list(string), `private_az_ids` order | `vpc_private_subnet_ids` |
| `private_route_table_ids` | list(string), same order | `vpc_private_route_table_ids` |
| `nat_public_subnet_id` | string | `vpc_nat_public_subnet_id` |
| `runtime_subnet_ids` | list(string), `runtime_az_ids` order | `vpc_runtime_subnet_ids` |
| `runtime_route_table_ids` | list(string), one element | `vpc_runtime_route_table_ids` |
| `apigw_cloudwatch_role_arn` | string, or null when the flag is false | none (evidence only) |
| `az_name_by_id` | map(string) | none (evidence only) |

## Apply and destroy

Run it through the wrapper, never bare. `us-east-1` below is an example; any region in
`infra/regions.json` is accepted, and the zones come from its entry:

```bash
CRM_EXPECTED_ACCOUNT_ID=<account-id> scripts/bootstrap_account.sh --profile NAME --region us-east-1 [--no-apigw-logging]
CRM_EXPECTED_ACCOUNT_ID=<account-id> scripts/bootstrap_account.sh --profile NAME --region us-east-1 --destroy
```

The wrapper initialises the S3 backend from the gitignored `infra/backend.hcl` with state key
`crm-agent-eval/bootstrap-account/terraform.tfstate`, applies, renders `infra/terraform.tfvars`, and
runs `scripts/preflight_adopted_vpc.sh` on the result. `--destroy` refuses while the main stack's state
lists any resource. `scripts/remove_state_backend.py` refuses to remove the backend while this root's
state lists resources.

Offline static check:

```bash
cd infra/bootstrap_account
terraform fmt -check
terraform init -backend=false -input=false -plugin-dir=../.terraform/providers
terraform validate
```

`.terraform.lock.hcl` carries the same `hashicorp/aws` 6.71.0 entry as `infra/.terraform.lock.hcl`.

## Common failures

- **First apply fails on `aws_api_gateway_account`** with an error that the role cannot be assumed
  or lacks permissions: IAM propagation. Re-run the apply.
- **The wrapper exits 2 before any AWS call**: `--region` is not in `infra/regions.json`. The message
  lists the supported regions.
- **`Invalid value for variable`**: a region that is not `us-east-N`/`us-west-N`, a `vpc_cidr` that is
  not an IPv4 `/16` network address, or a zone override with fewer than two distinct AZ ids.
- **`Resource precondition failed` on `aws_vpc.this`**, with one of three messages:
  - `aws_region "<r>" is not in infra/regions.json; supported: …`: add the region to the map first.
  - `infra/regions.json lists fewer than 2 agentcore_az_ids for <r>`: the map entry is unusable.
  - `every bootstrap AZ id must be one of the agentcore_az_ids infra/regions.json lists for <r> (…)`:
    a zone override names a zone of another region, or one that AgentCore does not support.
- **Residue while this root exists**: `scan_residue.py` lists the API Gateway role as project
  residue until this root is destroyed, alongside the state backend, which is residue until
  `scripts/remove_state_backend.py` removes it. That is expected. The VPC, subnets, route tables and IGW are
  accepted as adopted network, and the emptied default security group is not reported.

## Destroy and the account setting

The `hashicorp/aws` 6.71.0 schema for `aws_api_gateway_account` has no `reset_on_delete` argument
(checked with `terraform providers schema -json`). The provider's v6 upgrade guide says destroy now
always resets the API Gateway account settings, so `--destroy` clears the account+region
`cloudwatchRoleArn`. Any other workload's API Gateway logging in that account and region stops with
it; use `--no-apigw-logging` where another owner manages the setting. Confirm after a destroy with
`aws apigateway get-account --profile NAME --region <region> --no-cli-pager`.
