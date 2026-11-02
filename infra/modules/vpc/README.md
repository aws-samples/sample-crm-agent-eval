# `vpc` module

**This module creates no VPC.** It adopts an existing one and creates only what this stack owns inside
it: the VPC endpoints, three security groups, a flow log, and — when `nat_gateway_enabled` is true — an
**egress-only** NAT gateway plus the default route it adds to the adopted private route tables. The
VPC, its subnets and their route tables arrive as ids through the gitignored `terraform.tfvars`; a VPC
or subnet id never appears in tracked content (`check_no_account_id`).

Ids arrive as variables rather than through `data` sources because a data source is read at plan time
and needs live credentials, which would make this module unplannable in the offline harness
(`tests/unit/_terraform_fixture.py`). `test_the_module_declares_no_data_source_so_it_stays_plan_testable`
pins this. For the same reason `region` is a variable rather than an `aws_region` read.

**A VPC is not an exposure control.** VPC attachment governs egress. A Lambda Function URL stays
internet-reachable however its function is networked; what keeps the relay and the API non-public is
`authorization_type = "AWS_IAM"` plus CloudFront OAC on the relay, and the REST API's `PRIVATE`
endpoint type plus its resource policy. `scripts/check_no_public_exposure.py` scores these as distinct
controls (`function-url-auth` vs `lambda-vpc`).

This module creates no subnet, route table, route-table association, internet gateway, or
default-resource override (`aws_default_security_group`, `aws_default_route_table`), adds no subnet
assigning public addresses, and declares no inbound security-group rule from any network source.
`check_plan_security_baseline`'s `adopted_network_wiring` control and `tests/unit/test_infra_vpc.py`
assert these in both the NAT-enabled and NAT-disabled cases.

See also: the [documentation map](../../../README.md#documentation), the
[runbook](../../../docs/runbook.md#run-the-adopted-vpc-preflight-standalone-first) and the [security
baseline](../../../docs/security-baseline.md). Modules named here:
[`agent_relay`](../agent_relay/README.md), [`api`](../api/README.md),
[`lambda_action`](../lambda_action/README.md).

## Inputs

| Variable | Default | Notes |
|---|---|---|
| `name_prefix` | required | prefixes every resource name and `Name` tag |
| `region` | required | builds the `com.amazonaws.<region>.<service>` endpoint names |
| `vpc_id` | required | the existing VPC to adopt |
| `private_subnet_ids` | required | existing private subnets for Lambda ENIs and interface-endpoint ENIs |
| `private_route_table_ids` | required | the adopted private subnets' route tables. Gateway endpoints attach to them, and the NAT default route is added to them when NAT is on. `distinct()`-ed, so repeating a shared table is safe. **Do not pass the VPC's main route table, or a table serving subnets that are not this stack's** — see "The NAT route operates on route tables" |
| `runtime_subnet_ids` | `[]` | the AgentCore Runtime's VPC-mode subnets (the root requires `vpc_runtime_subnet_ids`). Kept out of the interface-endpoint subnet set and the NAT route. Each must be in an AgentCore-supported AZ for the region, as listed in `infra/regions.json` (`preflight_adopted_vpc.sh`). Must not include `nat_public_subnet_id` (plan-time precondition) |
| `runtime_route_table_ids` | `[]` | the Runtime subnets' route tables, joined to the S3/DynamoDB gateway endpoints so ECR image-layer pulls have an S3 path. Not given the NAT route. Must include every Runtime subnet route table that is not in `private_route_table_ids`: such a subnet otherwise has no S3 path and the Runtime's image pull fails |
| `vpc_cidr_block` | required | the adopted VPC's IPv4 CIDR, echoed as an output; nothing is sized from it |
| `nat_public_subnet_id` | `null` | an existing internet-gateway-routed subnet for the NAT gateway. Required when `nat_gateway_enabled` is true, and must not appear in `private_subnet_ids` (both enforced as preconditions in `nat.tf`) |
| `interface_endpoint_service_names` | required | **short** names (`kms`, not `com.amazonaws.<region>.kms`); non-empty, validated |
| `private_dns_disabled_services` | `["execute-api"]` | services whose private DNS stays off. Private DNS on `execute-api` applies VPC-wide, so co-tenants' calls to their own public APIs would resolve to this endpoint and be refused. CloudFront's VPC origin does not need it |
| `gateway_endpoint_service_names` | `["s3", "dynamodb"]` | only those two are accepted |
| `nat_gateway_enabled` | **required, no default** | see "The one dependency that needs egress" |
| `flow_log_enabled` | `true` | plain `bool` |
| `flow_log_retention_days` | `365` | minimum 365 (validated) |
| `flow_log_kms_key_arn` | `null` | the project CMK |
| `tags` | `{}` | the provider's `default_tags` already carries the mandatory tags |

## Outputs

| Output | Consumer |
|---|---|
| `lambda_subnet_ids` | the `subnet_ids` half of every Lambda `vpc_config`. Returns `private_subnet_ids` alone, so the NAT subnet can never appear in it |
| `lambda_security_group_ids` | the `security_group_ids` half of the same |
| `vpc_id`, `vpc_cidr_block` | root outputs and the deployed-network checks |
| `endpoint_security_group_id` | diagnostics |
| `interface_endpoint_service_names`, `gateway_endpoint_service_names` | checks comparing the deployed endpoint set with the declared one |
| `nat_gateway_public_ip` | checks asserting the relay's egress address; `null` when NAT is off |
| `flow_log_group_name` | the access-log delivery check; `null` when flow logs are off |
| `execute_api_vpc_endpoint_id` | `modules/api`'s `vpc_endpoint_ids` (endpoint association and resource-policy allowlist). Read from the endpoint map by key; precondition-guarded |
| `execute_api_security_group_id` | `modules/api_origin`, for the ALB→endpoint rule (the root declares the reverse rule) |
| `execute_api_network_interface_ids` | the root's ENI lookups that place the endpoint's private addresses in the ALB target group; precondition-guarded |
| `runtime_security_group_id` | the AgentCore Runtime's `network_mode_config.security_groups` |
| `runtime_subnet_ids` | the Runtime's `network_mode_config.subnets` |
| `bedrock_agentcore_vpc_endpoint_id` | the `aws:SourceVpce` condition in the Runtime resource policies; read by key, precondition-guarded |
| `states_vpc_endpoint_id` | the `states` endpoint the root's `aws_vpc_endpoint_policy.states` attaches to (`StartExecution`/`DescribeExecution` on the eval sweep only, this account's principals); read by key, precondition-guarded |

## The endpoint set is the reachability policy

Apart from the optional NAT path, `interface_endpoint_service_names` is the complete list of AWS
services any Lambda here can reach.

- **A missing endpoint is a hang, not an error.** No `AccessDenied`, no plan failure — the SDK retries
  against an address it cannot route to until its budget expires. Adding an AWS dependency to `src/`
  without adding its endpoint to `local.vpc_interface_endpoint_services` in `infra/main.tf` produces a
  deployment that passes static checks and times out on first use. The flow log shows the attempt as a
  `REJECT`.
- **`private_dns_enabled = true` is what makes an unmodified SDK call work.** Without it clients
  resolve the public service hostname and have no route to it.

The set is declared in `infra/main.tf`:

| Endpoint | Type | Caller, and what breaks without it |
|---|---|---|
| `dynamodb` | Gateway | every data handler. Gateway, not interface: the interface variant has no private DNS |
| `s3` | Gateway | `sweep-control` artefact writes; ECR image layers for the Runtime |
| `logs` | Interface | every Lambda. Without it a VPC Lambda emits no logs at all |
| `kms` | Interface | table CMK, log CMKs, and environment-variable decryption (an init-phase failure) |
| `lambda` | Interface | action-Lambda invocations from inside the VPC |
| `ssm` | Interface | the relay's audience parameter; an unreadable one is a 5xx per turn |
| `bedrock-agentcore` | Interface | the relay's `InvokeAgentRuntime` data plane. Must be exactly this name — `bedrock-agentcore-control` and `bedrock-agentcore.gateway` do not route to a Runtime. Also the endpoint the Runtime resource policies' `aws:SourceVpce` condition keys on |
| `bedrock-runtime` | Interface | the Runtime's model calls from `src/providers/*_adapter.py`. `check_vpc_endpoint_coverage` requires it while the Runtime is in VPC mode |
| `ecr.api` / `ecr.dkr` | Interface | AgentCore pulling the Runtime container image. Not SDK-derived; `check_vpc_endpoint_coverage` carries a stated non-SDK requirement for them |
| `execute-api` | Interface | the private REST API. Consumed by CloudFront's VPC origin, not by this project's code. Associating it with the API is what makes the `{api-id}-{vpce-id}.execute-api…` hostname exist |
| `states` | Interface | `eval_trigger`'s `StartExecution`/`DescribeExecution` on the eval sweep state machine; without it a sweep cannot start or report status. The root's `aws_vpc_endpoint_policy.states` scopes it (see `states_vpc_endpoint_id`) |

Not present: `cognito-idp` (unusable — see below), `monitoring` and `sts` (nothing here calls them).

## Security groups

| Group | Ingress | Egress |
|---|---|---|
| `aws_security_group.lambda` (every Lambda's `vpc_config`) | none — invocations arrive through the Lambda control plane | 443 to the endpoint group; 443 to each gateway endpoint's managed prefix list; when NAT is on, 443 to `0.0.0.0/0` |
| `aws_security_group.endpoints` (interface-endpoint ENIs) | 443 from the Lambda group and the Runtime group | none |
| `aws_security_group.agent_runtime` (Runtime VPC-mode ENIs) | none | 443 to the endpoint group; 443 to each gateway endpoint's prefix list. Never `0.0.0.0/0`, so the Runtime has no internet path even where its route table reaches the NAT |

Rules are separate `aws_vpc_security_group_{in,e}gress_rule` resources, because the groups reference
each other and inline blocks would be a dependency cycle. With no inline rules, the implicit allow-all
egress AWS attaches to a new group is revoked.

**The gateway-endpoint egress rules are required.** A gateway endpoint is reached over the route table,
not through the endpoint security group. Without a rule to each gateway endpoint's prefix list, apply
succeeds, the endpoint looks correct, and every DynamoDB and S3 call times out with no authorization
error.

The adopted VPC's default security group and default route table are not managed by this module.
Managing them against an adopted VPC is destructive (an emptied `aws_default_security_group` revokes
rules from every co-tenant resource on it), so their state is the VPC owner's responsibility.

### The NAT route operates on route tables, not subnets

The NAT default route is written to route **tables**, and a table serves an arbitrary set of subnets.
Passing the VPC's main route table writes `0.0.0.0/0 → NAT` into the default for every subnet without an
explicit association, including subnets other teams create later: they gain egress through this stack's
NAT, are billed against it, and lose it at `terraform destroy`. Pass only tables whose associated
subnets are exactly `private_subnet_ids`.

## ENI permissions every consuming module needs

Lambda uses the function's **execution role** to create the Hyperplane ENI, so a function in this VPC
without the permissions is created successfully and fails on every invocation. [`modules/lambda_action`](../lambda_action/README.md)
and [`modules/agent_relay`](../agent_relay/README.md) each carry a `vpc_access` inline policy with `ec2:CreateNetworkInterface`,
`ec2:DescribeNetworkInterfaces`, `ec2:DescribeSubnets`, `ec2:DeleteNetworkInterface`,
`ec2:AssignPrivateIpAddresses` and `ec2:UnassignPrivateIpAddresses`;
`test_both_lambda_modules_grant_the_eni_permissions_the_vpc_config_requires` asserts the set.

The grant is on `resources = ["*"]`, as the Lambda Developer Guide's
[VPC configuration](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html) requires.
Those permissions are also available to the function's code; the actions are limited to network
interfaces (no `ec2:RunInstances`, no `ec2:AuthorizeSecurityGroupIngress`), and the reachable surface is
fixed by this module's endpoint set.

## The one dependency that needs egress, and why `nat_gateway_enabled` has no default

`src/relay/src/jwt.ts` fetches `https://cognito-idp.<region>.amazonaws.com/<pool-id>/.well-known/jwks.json`.
The `cognito-idp` interface endpoint exists but is unusable: user pools with a domain assigned are
incompatible with AWS PrivateLink, and this pool's domain is the source of the `/oauth2/token` endpoint
the AgentCore Gateway's MCP `client_credentials` flow uses.

`nat.tf` therefore builds an egress-only path:

- the only route this module adds is `0.0.0.0/0` to the **NAT gateway** (`aws_route.private_nat`'s
  `nat_gateway_id`, never `gateway_id`);
- `lambda_subnet_ids` returns `private_subnet_ids` alone, and a precondition keeps
  `nat_public_subnet_id` out of that set;
- the Lambda group's open-CIDR egress rule is **tcp/443 only** — Cognito publishes no prefix list —
  and `check_plan_security_baseline`'s `security_group_egress_scoped` control asserts it.

A NAT gateway is source NAT for egress and has no inbound path.

Both defaults are wrong, so the variable has none:

- `false` deploys a relay that cannot fetch the JWKS, so **every agent turn fails authentication**
  behind a green apply.
- `true` creates a NAT gateway, billed hourly and per GB processed.

X-Ray trace segments from every Lambda's active tracing take the same NAT 443 path: there is no `xray`
interface endpoint. With `false`, segments are dropped and the functions are unaffected.

Set it with `-var vpc_nat_gateway_enabled=true` or a gitignored `*.auto.tfvars`; the root variable has no
default either. The NAT gateway is single-AZ; while that zone is impaired JWKS fetches fail, partly
covered by `aws-jwt-verify`'s per-execution-environment key cache.

## Verifying

Plan-time, offline, no credentials. Plans this module against mock credentials and asserts each absence
alongside a positive control:

```bash
.venv/bin/python -m pytest tests/unit/test_infra_vpc.py
```

Source-level gate over the whole tree — every Lambda has a `vpc_config`, no Function URL is `NONE`,
every `lambda:InvokeFunctionUrl` grant carries the `AWS_IAM` auth-type condition, no wildcard-principal
Lambda permission, no public S3 surface. It has no waiver file, allowlist or override:

```bash
.venv/bin/python3 scripts/check_no_public_exposure.py     # must exit 0
```

It reads source, so a green run is a statement about the repository, not the account. Its
`api-default-endpoint` control inspects `aws_apigatewayv2_api` blocks only; the REST API's `PRIVATE`
endpoint type is not asserted by this gate.

Standalone validate:

```bash
cd infra/modules/vpc
terraform init -backend=false
terraform validate
```

### Not covered offline

These need the deployed stack:

- route tables carrying no internet-gateway route, read from the account;
- a Lambda invoked from inside the VPC reaching DynamoDB (gateway endpoint) and KMS (interface
  endpoint), with `FunctionError` asserted `None` — the check that catches a missing prefix-list rule;
- the relay reaching the AgentCore data plane, and the Cognito JWKS URL through the NAT gateway. A
  missing route is a timeout, so any check on this path needs a bounded timeout;
- flow-log delivery, confirmed by records arriving in the log group.

Lambda raises `ENINotReadyException` briefly after a `vpc_config` is created or changed, so a smoke run
started immediately after an apply can fail on that condition; retry on that error specifically, never
blanket-retry.

## Cost

Interface endpoints are billed hourly **per availability zone** plus per GB processed, so the endpoint
line scales with `len(interface_endpoint_service_names) × (AZs the adopted private subnets span)`.
Gateway endpoints (`s3`, `dynamodb`) have no hourly charge. The NAT gateway, when enabled, is billed
hourly and per GB.

```bash
# endpoint count — offline
git grep -A20 'vpc_interface_endpoint_services = \[' -- infra/main.tf

# AZ span — reads the account; pin profile and region explicitly (us-east-1 is an example)
aws ec2 describe-subnets --profile default --region us-east-1 --no-cli-pager \
  --subnet-ids <your vpc_private_subnet_ids> \
  --query 'sort(Subnets[].AvailabilityZone)' --output json
```

Pass `--profile` and `--region` explicitly: an ambient profile pointing at another account answers
this about the wrong VPC. Removing an endpoint something calls is not a saving — it produces a hang.
