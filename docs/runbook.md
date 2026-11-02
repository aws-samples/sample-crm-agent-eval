# Runbook

Operations reference for `crm-agent-eval`: building the artefacts, deploying the stack into an
existing VPC, bringing up the UI, running the post-deploy verification layers, cleaning up residue,
and tearing the stack down.

See also: [documentation map](../README.md#documentation) · [development](development.md) · [security baseline](security-baseline.md) · [relay artefact](relay-artefact.md) · [MCP client guide](mcp-client-guide.md) · [browser journeys](../tests/e2e/browser/journeys/README.md).

Every resource this stack creates is billable. Every command below that touches AWS needs credentials
for the target account, and every live verification run needs `--i-understand-this-is-billable`.

## Overview

Three planes reach the CRM's action Lambdas, which are the one authorization boundary:

- the browser SPA, through CloudFront (`/api/*`) to a private REST API Gateway;
- external MCP clients, through the AgentCore Gateway and its identity interceptor;
- the in-app Strands agents in the AgentCore Runtime, by direct `lambda:InvokeFunction`. The browser
  reaches the Runtime through CloudFront (`/agent/*`) and the streaming relay Lambda.

The stack deploys into an **adopted VPC**: you supply the VPC, subnet and route-table ids, and the
stack never creates or destroys them. You choose the region: any region in `infra/regions.json`
(README "Deploying to another region"). Commands here write `--region <region>`; `us-east-1` is one
example.

Verification has six layers. Layers 1-3 run offline and are covered in [`docs/development.md`](development.md). This
runbook covers Layers 4-6, which run against the deployed stack:

| Layer | Proves | Entry point |
|---|---|---|
| 4 — per-resource smoke | each deployed resource works on its own, with its real identity and config | `scripts/verify_deployment.sh --layer 4` |
| 5 — component integration | adjacent resources work together | `scripts/verify_deployment.sh --layer 5` |
| 6 — end to end | the product works through its real front doors | `scripts/verify_deployment.sh --layer 6` |

![The deploy pipeline in runbook order: build the Lambda and relay zips, bootstrap the state backend once, fill in terraform.tfvars, run deploy.sh, which applies Terraform and publishes the SPA (npm run build, s3 sync to the CloudFront origin bucket, CloudFront invalidation), twice on a first deployment, reading the CloudFront VPC-origin security group id between the applies; check /api/*, bring up the UI, run verification layers 4-6, then reap tenants, run teardown.sh (waiting up to 5 minutes for the Runtime's ENIs and re-running it later if they are still held), run the residue scan, remove the state backend with remove_state_backend.py, and run a final residue scan expecting exit 0](figures/runbook-deploy-pipeline.svg)

## Prerequisites

Tools on `PATH`: `aws`, `terraform`, `python3`, `jq`, `node`, `npm`, `zip`, `unzip`, `git`. They are
probed by different scripts, so a missing one can surface late:

| Script | Probes |
|---|---|
| `scripts/deploy.sh` | `git`, `aws`, `terraform`, `python3`; `npm` for the frontend publish unless `--skip-frontend` |
| `scripts/package_relay.sh` | `node`, `npm`, `zip`, `unzip` |
| `scripts/preflight_adopted_vpc.sh` (run inside `deploy.sh`) | `aws`, `jq` |
| `scripts/tag_agentcore_runtime_children.sh` (run inside `deploy.sh`) | `aws`, `jq` |

The project virtualenv, with `boto3` (the deploy-time gates and the post-apply parity gate run under
`.venv/bin/python3`). Create it with the interpreter `.python-version` pins, not whatever `python3`
is on `PATH`: a host `python3` of another minor version (3.14, say) builds a venv that fails at
`pip install -e .` with `requires a different Python`. Check the version before installing:

```bash
python3.13 -m venv .venv          # the interpreter whose --version matches .python-version
.venv/bin/python3 --version; cat .python-version   # must print the same version; if not, stop
.venv/bin/pip install --no-input --no-deps -r requirements.txt
.venv/bin/pip install --no-input --no-deps -e .
```

Prefix `deploy.sh` invocations with `PATH=.venv/bin:$PATH`. The script resolves the venv interpreter
for its gates itself, but it also makes bare `python3` calls (template render, drift classification,
outputs provenance), and the prefix makes those use the pinned interpreter too.

### Check the target account and region first

Profile and region resolve as **CLI flag, then `AWS_PROFILE`/`AWS_REGION`, then `default` /
`us-east-1`**, in `deploy.sh`, `teardown.sh`, `verify_deployment.sh` and every smoke module. A deploy
with no flags therefore targets whatever your shell exports. A wrong profile is a wrong account.

```bash
# Read-only. Confirm the account id is the one you intend to bill.
echo "AWS_PROFILE=${AWS_PROFILE:-<unset>}  AWS_REGION=${AWS_REGION:-<unset>}"
aws sts get-caller-identity --profile default --region <region> --no-cli-pager
```

Pass `--profile` and `--region` explicitly on every command, or `unset AWS_PROFILE AWS_REGION`.

`deploy.sh` and `teardown.sh` export the resolved pair as `AWS_PROFILE`, `AWS_REGION` and
`AWS_DEFAULT_REGION`, so Terraform's provider and its S3 backend authenticate the same way as the
script's own `aws` calls. Outside these scripts nothing does that for you: `infra/backend.tf` is a
bare `backend "s3" {}` and `infra/backend.hcl` carries no `profile`, so a hand-run `terraform init`
or `terraform output` uses ambient credentials.

Do not select the account with static keys. Both scripts refuse when `AWS_ACCESS_KEY_ID` or
`AWS_SESSION_TOKEN` is set, because static keys outrank `AWS_PROFILE` in every AWS SDK credential
chain. Unset them and use a named profile:

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
```

**Expected-account assertion.** `CRM_EXPECTED_ACCOUNT_ID` is **required** by `deploy.sh`: the
12-digit account the deploy may touch. It refuses before anything is rendered, initialised or
applied when the variable is:

- unset or empty;
- not 12 digits;
- different from the account in the state-bucket name in `infra/backend.hcl`
  (`<project>-tfstate-<account>-<region>`), when that file exists (both sources are named);
- different from the STS account of the resolved profile (`WRONG ACCOUNT`).

```bash
CRM_EXPECTED_ACCOUNT_ID=<12-digit account id> scripts/deploy.sh --profile <profile> --region <region>
```

**Before each apply, record the session and the account.** Keep both lines with the deploy's
evidence. With the isolated session profile (`scripts/session_profile.sh`), check that at least 120
minutes remain (a manual threshold: `session_profile.sh` itself refuses only below 15 minutes), then
confirm the account:

```bash
scripts/session_profile.sh status --dir <state-dir> --profile <profile>
scripts/session_profile.sh exec --dir <state-dir> --profile <profile> --region <region> -- \
  aws sts get-caller-identity --query Account --output text
```

### API Gateway CloudWatch Logs role

The REST API stage writes access logs. API Gateway accepts that only when the account's
`cloudwatchRoleArn` setting names an IAM role that `apigateway.amazonaws.com` can assume, with the
`AmazonAPIGatewayPushToCloudWatchLogs` managed policy attached. The setting is per account and per
region. The stack root in `infra/` does not create the role or the setting, so the account needs
them before the first deploy, or the apply fails when it creates the stage. A setting that names a
deleted role fails the same way.

```bash
# Read-only. Expect an ARN, then confirm that role still exists.
aws apigateway get-account --profile default --region <region> --no-cli-pager \
  --query cloudwatchRoleArn --output text
aws iam get-role --role-name <role-name> --profile default --region <region> --no-cli-pager \
  --query Role.Arn --output text
```

When the setting is unset, let the account bootstrap root create both: `scripts/bootstrap_account.sh`
(see "Deploying into a new account") applies `infra/bootstrap_account/`. That root creates
`crm-agent-eval-apigw-cloudwatch-logs`, which trusts only `apigateway.amazonaws.com` and has
`AmazonAPIGatewayPushToCloudWatchLogs` attached, and points the account setting at it. If the setting
already names a working role that another owner manages, pass `--no-apigw-logging` and the root
leaves both alone.

The role and the setting are account-wide and outlive the stack; teardown does not remove them. The
residue scan reports the role (its name starts with `crm-agent-eval`) until
`scripts/bootstrap_account.sh --destroy` removes it. That destroy also resets the account's
`cloudwatchRoleArn`, so any other API Gateway logging in that account and region stops with it.

## Build

Two packaging scripts, both writing to `infra/build/lambda/`, which is the directory Terraform reads.
Keep that default when deploying: `scripts/deploy.sh` checks the Python zips there and passes no other
build directory to Terraform, so a zip built elsewhere is not what the apply ships. Run both; they
can run in either order, because `package_lambdas.sh` keeps `relay.zip`:

```bash
scripts/package_lambdas.sh     # every Python Lambda zip, plus agent-source.zip
scripts/package_relay.sh       # relay.zip (the Node relay Lambda)
```

| Script | Produces | Flags |
|---|---|---|
| `scripts/package_lambdas.sh` | one zip per tool in `crm.toolspec.definitions.ALL_TOOLS`, `identity_interceptor.zip`, `pre_token_generation.zip`, `eval_trigger.zip`, `chat_target.zip`, and `agent-source.zip` (the Runtime image's CodeBuild context) | `--build-dir DIR`, `--print-pins` |
| `scripts/package_relay.sh` | `relay.zip`, with `index.js` at the archive root and production `node_modules` from `src/relay/package-lock.json`; verifies the handler imports from the extracted zip | `--build-dir DIR`, `--skip-install`, `--no-verify-import`, `--verify-only ZIP` |

- `package_lambdas.sh` cleans the build directory at start, preserving only `relay.zip`. It never
  builds the relay, so `package_relay.sh` is a separate, mandatory step.
- `package_relay.sh` runs `npm ci` in `src/relay` itself. `--skip-install` instead requires an
  existing `src/relay/node_modules`.
- `ls infra/build/lambda/` after a run is the artefact inventory.
- Packaging is not byte-deterministic: every rebuild changes every zip hash, so the next plan shows
  Lambda updates on unchanged source. See "Common failure modes".

## State backend

One-time per account, and again whenever the operator list changes. Creates the S3 state bucket, its
KMS key, an access-log bucket and the DynamoDB lock table, and writes `infra/backend.hcl`. Two inputs
are required:

- the expected account, from `CRM_EXPECTED_ACCOUNT_ID` or `--expected-account-id`;
- the operator list, from `CRM_OPERATOR_PRINCIPAL_ARNS` (comma-separated) or a repeatable
  `--operator-principal-arn`. It must equal `crm_operator_principal_arns` in `infra/terraform.tfvars`.
  Nothing in the repository checks that; `smoke/store_principal_deny.py` compares the two in the
  account (`state_operator_list_matches_stack`).

```bash
CRM_EXPECTED_ACCOUNT_ID=<account-id> \
CRM_OPERATOR_PRINCIPAL_ARNS=arn:aws:iam::<account-id>:role/<operator-role> \
  infra/bootstrap_state_backend.sh --profile default --region <region>
```

The backend is per region: the region is the last part of both bucket
names (`crm-agent-eval-tfstate-<account>-<region>`, `crm-agent-eval-tfstate-logs-<account>-<region>`)
and the `region` in the written `infra/backend.hcl`, so a stack in another region needs its own run.

It refuses, before any AWS call, when `AWS_ACCESS_KEY_ID` or `AWS_SESSION_TOKEN` is set, when no
expected account is given, or when the flag and the variable disagree. It refuses the operator list,
also before any AWS call, when it is empty, when an entry is not an IAM role or user ARN (or holds
`*` or `?`), when an entry is the account root or belongs to another account, or when the flag and
the variable disagree. After `sts get-caller-identity`, and before creating anything, it refuses when
the profile resolves to a different account (`WRONG ACCOUNT`), and when the caller is not in the
list. For an assumed role it resolves the caller's role ARN with `iam get-role` (path included) and
refuses if that call fails; the account root is refused as a caller.

It is idempotent. Each run re-applies every bucket setting, and, existing resources included, puts
the key policy, the lock-table resource policy, the log-bucket policy and the state-bucket policy,
in that order. A run that fails part-way leaves the earlier stores on the new policy and the later
ones on their old, weaker one. It never locks the caller out. An existing lock table and KMS key are
otherwise left as they are.

| Resource | Settings |
|---|---|
| state bucket `crm-agent-eval-tfstate-<account>-<region>` | SSE-KMS with the backend key, Block Public Access, versioning, server access logging to the log bucket under `state-access/`; policy = TLS deny, object reads/lists/writes denied outside the operators, bucket administration denied outside the operators and the account root |
| log bucket `crm-agent-eval-tfstate-logs-<account>-<region>` | SSE-S3 (`AES256`), because S3 server access logging cannot deliver to an SSE-KMS default bucket; `BucketOwnerEnforced`, Block Public Access, versioning; policy = TLS deny, an Allow for `logging.s3.amazonaws.com` to `s3:PutObject` under `state-access/` (conditioned on `aws:SourceArn` = the state bucket and `aws:SourceAccount` = the account), and the same two principal Denies. The data-plane Deny exempts requests a service makes as its own service principal, so log delivery is not denied |
| lock table `crm-agent-eval-tflock` | SSE-KMS with the backend key, point-in-time recovery; resource policy = item reads and writes, PartiQL, backup, export, restore and streaming denied outside the operators, table administration denied outside the operators and the account root |
| KMS key `alias/crm-agent-eval-tfstate` | customer-managed, rotation on; key policy = the account-root Allow, key use denied to every non-service principal outside the operators (DynamoDB's own use for the lock table is not denied), key administration denied outside the operators and the account root |

Read the settings back after the first run (read-only). Access logs arrive best-effort, often hours
later, so check for the first object later rather than waiting:

```bash
aws s3api get-bucket-encryption --bucket <bucket> --profile <p> --region <r> --no-cli-pager
aws s3api get-bucket-policy --bucket <log-bucket> --profile <p> --region <r> --no-cli-pager
aws s3api get-bucket-logging --bucket <state-bucket> --profile <p> --region <r> --no-cli-pager
aws s3api list-objects-v2 --bucket <log-bucket> --prefix state-access/ --max-items 1 \
  --profile <p> --region <r> --no-cli-pager
```

`infra/backend.hcl` is gitignored, so a fresh clone has none. Without it `deploy.sh` warns and
`terraform init` falls back to whatever is cached in `infra/.terraform`. Keep the file (or regenerate
it) across teardowns; the backend is outside the stack and outlives it. To remove it once the stack is
gone, see "Removing the state backend".

## terraform.tfvars

```bash
cp infra/terraform.tfvars.example infra/terraform.tfvars
```

Then replace every placeholder. The file is gitignored; tracked content must not carry real ids.
When the network comes from the account bootstrap root, do not copy the six `vpc_*` ids by hand:
`scripts/bootstrap_account.sh` writes them through `scripts/render_vpc_tfvars.py`, which copies the
example when the file is absent, leaves every other line byte-identical, and refuses a leftover
`REPLACE_ME` in those six keys. `api_origin_ingress_security_group_ids` is still yours to set (see
"First deployment").

| Variable | Required | Notes |
|---|---|---|
| `aws_profile`, `aws_region` | defaults | `deploy.sh` passes the resolved pair as `-var` anyway |
| `service`, `environment`, `owner`, `cost_center`, `data_classification` | defaults | applied as tags |
| `vpc_id` | yes | the adopted VPC |
| `vpc_private_subnet_ids` | yes | at least two, in distinct Availability Zones; Lambda ENIs and interface endpoints |
| `vpc_nat_public_subnet_id` | when `vpc_nat_gateway_enabled` is `true` | an existing IGW-routed subnet for the NAT gateway; must not be in the private list. The plan fails when NAT is enabled and this is unset |
| `vpc_private_route_table_ids` | yes | the private subnets' route tables; each receives the NAT default route and the S3/DynamoDB gateway-endpoint associations |
| `vpc_runtime_subnet_ids` | yes, non-empty | the AgentCore Runtime runs in VPC mode; each subnet in an AgentCore-supported AZ for the region (`agentcore_az_ids` in `infra/regions.json`), not the NAT subnet |
| `vpc_runtime_route_table_ids` | yes, for every Runtime subnet route table not already in `vpc_private_route_table_ids` (defaults to `[]`) | the Runtime subnets' route tables; each gets the S3 and DynamoDB gateway-endpoint associations and no NAT route. The Runtime pulls its ECR image layers from S3, and its security group allows egress only to the S3/DynamoDB prefix lists and the interface endpoints, so a Runtime subnet whose route table is in neither list has no S3 path and the image pull fails |
| `vpc_nat_gateway_enabled` | yes | `true` creates a NAT gateway (billed hourly and per GB). `false` leaves the relay unable to fetch Cognito signing keys, so it rejects every request |
| `api_origin_ingress_security_group_ids` | yes | the service-managed `CloudFront-VPCOrigins-Service-SG`; see "First deployment" |

Discover the ids read-only:

```bash
aws ec2 describe-vpcs --profile default --region <region> --no-cli-pager \
  --query 'Vpcs[].[VpcId,CidrBlock,IsDefault]' --output table
aws ec2 describe-subnets --profile default --region <region> --no-cli-pager --filters Name=vpc-id,Values=<vpc-id> \
  --query 'Subnets[].[SubnetId,AvailabilityZone,AvailabilityZoneId,MapPublicIpOnLaunch]' --output table
aws ec2 describe-route-tables --profile default --region <region> --no-cli-pager --filters Name=vpc-id,Values=<vpc-id> \
  --query 'RouteTables[].[RouteTableId,Associations[].SubnetId]' --output table
```

### Run the adopted-VPC preflight standalone first

`deploy.sh` runs `scripts/preflight_adopted_vpc.sh` before the apply, but several of its refusals are
conditions only the VPC owner can fix, so learn its verdict before planning a deploy. Every call is a
read-only `describe-*`:

```bash
bash scripts/preflight_adopted_vpc.sh --profile default --region <region> \
  --vpc-id <vpc-id> --private-subnet-ids <subnet-a>,<subnet-b> \
  --route-table-ids <rtb-a>,<rtb-b> --nat-subnet-id <subnet-c> \
  --runtime-subnet-ids <subnet-r> --runtime-route-table-ids <rtb-r> \
  --min-free-ips <n>
```

It refuses when:

- a named subnet is not in the VPC, or the private subnets span fewer than two Availability Zones
  (the internal ALB needs two);
- a subnet has fewer free addresses than `--min-free-ips`. The default of 8 covers the ALB alone and
  is printed as `UNDERIVED DEFAULT`; derive the floor from `describe-subnets` (interface-endpoint ENIs
  and Lambda ENIs draw from the same pool) and pass it explicitly;
- a route table is the VPC's main table, has no explicit subnet associations, or also serves subnets
  that are not yours;
- `--nat-subnet-id` is also a private subnet, or the NAT subnet has no `0.0.0.0/0` route to an IGW;
- the VPC's **default security group admits any `0.0.0.0/0` ingress**. The stack will not modify that
  group, so this is for the VPC owner to resolve;
- the VPC lacks `enableDnsSupport` or `enableDnsHostnames` (interface-endpoint private DNS needs both);
- an existing S3 or DynamoDB gateway endpoint not managed by this stack already serves one of your
  route tables;
- a Runtime subnet is in an unsupported AZ, uses the main table or a table not in
  `--runtime-route-table-ids`, or its table routes to an IGW;
- any `describe-*` it needs cannot be read (fail-closed).

`deploy.sh` passes the VPC id, private subnets, private route tables, NAT subnet, Runtime subnets and
(when set) Runtime route tables from `terraform.tfvars`, so the deploy runs the same checks as the
standalone form. List the current refusals with `grep -n 'die "' scripts/preflight_adopted_vpc.sh`.

## deploy.sh

```bash
CRM_EXPECTED_ACCOUNT_ID=<account-id> PATH=.venv/bin:$PATH bash scripts/deploy.sh --profile default --region <region>
```

| Flag | Effect |
|---|---|
| `--profile NAME`, `--region REGION` | target; otherwise `AWS_PROFILE`/`AWS_REGION`, then `default`/`us-east-1` |
| `--template PATH` | AgentCore config template (default `.bedrock_agentcore.yaml`). The file is gitignored and optional: when it is absent, the script warns and skips the render |
| `--auto-approve` | default; the script is non-interactive |
| `--no-auto-approve` | Terraform's confirm prompt; refused when stdin is not a TTY |
| `--skip-frontend` | apply without building and publishing the SPA |

In order, the script:

1. refuses static keys in the environment and exports the resolved profile/region;
2. refuses if the git index holds unmerged paths;
3. resolves the caller identity and runs the expected-account assertion;
4. renders the account-specific AgentCore config into the template when one exists (restored on
   exit, failure or interrupt), or warns and skips the render when there is none;
5. runs `scripts/check_no_public_exposure.py` (no waiver; exit 3, "verified nothing", is also fatal);
6. reads the VPC ids, Runtime subnets and route tables, and the API origin security groups from
   `infra/terraform.tfvars`, refuses any empty required list or leftover `REPLACE_ME`, and runs the
   adopted-VPC preflight with the Runtime flags;
7. checks `boto3` imports, then `scripts/check_lambda_zip_imports.py --require-zips`;
8. checks the relay zip with `scripts/check_relay_zip.py --require-zip` and
   `scripts/package_relay.sh --verify-only`;
9. `terraform init` (with `-backend-config=infra/backend.hcl` when present) and `terraform apply`;
10. runs `scripts.smoke.deployed_parity --deploy-gate`: every deployed action Lambda's `CodeSha256`
    must equal its local zip;
11. runs `scripts/tag_agentcore_runtime_children.sh`: copies the AgentCore runtime's tags onto its
    service-created DEFAULT endpoint and workload identity, then reads both back. The deployer needs
    `bedrock-agentcore:TagResource` and `bedrock-agentcore:ListTagsForResource`. A failure fails the
    deploy at the end, after the outputs are recorded;
12. takes a post-apply `terraform plan -detailed-exitcode` and classifies any pending changes with
    `scripts/classify_post_apply_drift.py`;
13. builds the SPA, renders `config.json` from Terraform outputs, uploads hashed assets as
    `immutable` and `index.html`/`config.json` as `no-store`, invalidates `/*`, and prints the site URL;
14. takes the drift bound again after the publish;
15. writes `results/tf-outputs.json` (gitignored), stamped with its commit by
    `scripts/outputs_provenance.py`.

### What it refuses on

Every refusal below happens before `terraform apply` and changes nothing. The message names the
remedy.

| Refusal | Remedy |
|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SESSION_TOKEN` set | unset them; use `--profile` |
| `--no-auto-approve` without a TTY | drop the flag |
| git index holds unmerged paths | resolve the listed paths and `git add` them (or abort the operation in progress) |
| `aws`, `terraform` or `python3` not on `PATH` | install it |
| `sts get-caller-identity` fails | fix the profile's credentials |
| `WRONG ACCOUNT` | pass the right `--profile`, or set `CRM_EXPECTED_ACCOUNT_ID` deliberately |
| a leftover `.bedrock_agentcore.yaml.orig` (only when a template was rendered) | `python3 scripts/render_agentcore_env.py restore --template .bedrock_agentcore.yaml --backup .bedrock_agentcore.yaml.orig` |
| the public-exposure gate fails | fix the Terraform |
| no `infra/terraform.tfvars` | create it from the example |
| `terraform.tfvars` has an empty `vpc_id`, private subnet list, private route-table list or `vpc_runtime_subnet_ids` | fill it in |
| `terraform.tfvars` still carries `REPLACE_ME` in `vpc_id`, a subnet or route-table key, `vpc_nat_public_subnet_id`, `vpc_runtime_subnet_ids`, `vpc_runtime_route_table_ids` or `api_origin_ingress_security_group_ids` | replace the placeholders |
| the adopted-VPC preflight refuses | see the preflight section above |
| the gate interpreter cannot import `boto3` | create the venv and install `requirements.txt` |
| "Lambda zips are stale, incomplete or missing" | `scripts/package_lambdas.sh` |
| "the relay zip is stale, absent or unreadable" (or relay check exit 3) | `scripts/package_relay.sh` |
| "the relay zip's handler cannot be imported" | `scripts/package_relay.sh` |

After the apply, a parity failure dies immediately. A drift-bound failure blocks the publish but the
outputs snapshot is still refreshed before the script exits non-zero. If writing the snapshot fails,
any previous `results/tf-outputs.json` is deleted rather than left stale; every reader refuses an
absent file loudly.

To re-record the snapshot without an apply (for example after a commit made it stale):

```bash
bash scripts/refresh_outputs.sh --profile default --region <region>
```

It runs the same `terraform output -json`, content check, provenance stamp and atomic move, and its
stamp says it was recorded by `scripts/refresh_outputs.sh` with no apply. It needs an initialised
`infra/` and credentials to read state; on failure the previous snapshot is kept.

**A `WARN` about pending changes after the apply is normal.** Packaging is not byte-deterministic,
so the post-apply plan exits 2. The classifier absorbs that as a `WARN` only when every pending change
falls under a declared allowance (Lambda packaging resources, deferred policy-document re-renders) and
fails the deploy with "unexplained post-apply drift" otherwise. A clean run ends with `Deploy
complete.`

## First deployment: two applies

The `/api/*` behaviour reaches the private REST API through a CloudFront **VPC origin** in front of an
internal ALB. The ALB's ingress source is `CloudFront-VPCOrigins-Service-SG`, a service-managed
security group AWS creates only when the VPC origin is first applied. Its id cannot be known before
the first apply, so the first deployment takes two applies:

```bash
# Apply 1: set api_origin_ingress_security_group_ids in infra/terraform.tfvars to a security group
# that already exists in the VPC. The groups this stack creates do not exist yet, so create an empty
# placeholder; a group with no members admits nothing to the ALB.
aws ec2 create-security-group --profile default --region <region> --no-cli-pager --vpc-id <vpc-id> \
  --group-name crm-agent-eval-bootstrap --description "Placeholder for the first apply" \
  --tag-specifications 'ResourceType=security-group,Tags=[{Key=service,Value=crm-agent-eval},{Key=AppName,Value=crm-agent-eval}]' \
  --query GroupId --output text
CRM_EXPECTED_ACCOUNT_ID=<account-id> PATH=.venv/bin:$PATH bash scripts/deploy.sh --profile default --region <region>

# Read the id AWS created (read-only). Expect exactly one group.
aws ec2 describe-security-groups --profile default --region <region> --no-cli-pager \
  --filters Name=vpc-id,Values=<vpc-id> Name=group-name,Values=CloudFront-VPCOrigins-Service-SG \
  --query 'SecurityGroups[].GroupId' --output text

# Apply 2: set that id in infra/terraform.tfvars and apply again.
CRM_EXPECTED_ACCOUNT_ID=<account-id> PATH=.venv/bin:$PATH bash scripts/deploy.sh --profile default --region <region>

# Delete the placeholder once nothing uses it: expect 0 ENIs, then delete.
aws ec2 describe-network-interfaces --profile default --region <region> --no-cli-pager \
  --filters Name=group-id,Values=<placeholder-sg-id> --query 'length(NetworkInterfaces)'
aws ec2 delete-security-group --group-id <placeholder-sg-id> --profile default --region <region> \
  --no-cli-pager
```

On a clean account the first apply creates the whole stack in one run, with no service-linked-role
or IAM first-use error, and the second changes only the ALB ingress source. Both end
`Deploy complete.` with a drift `WARN` at most.

Until the second apply the ALB admits nothing, so `/api/*` fails at the edge. Only an `sg-` id is
accepted; do not widen the ingress to the VPC CIDR. The variable's validation rejects a CIDR or prefix
list because the adopted VPC is shared with other workloads.

## GSI projection changes

DynamoDB cannot change a global secondary index's projection in place, so an apply that changes one
(for example `body` on the notes and activities `gsi_opportunity`) deletes the index and creates it
again. Until the new index is `ACTIVE` and backfilled, queries on it fail or return partial results.
After such an apply, and before any smoke run or sweep, wait until every index reports `ACTIVE`:

```bash
aws dynamodb describe-table --table-name <table> --region <r> --profile <p> --no-cli-pager \
  --query 'Table.GlobalSecondaryIndexes[].[IndexName,IndexStatus]' --output text
```

Take the table names from the `dynamodb_table_names` output. Never apply a projection change while a
smoke run or sweep is in progress.

## Post-deploy checks for /api/*

A green apply does not show that `/api/*` works. Check the origin and one real request:

```bash
AWS_PROFILE=default AWS_REGION=<region> terraform -chdir=infra output -raw api_origin_vpc_origin_id
AWS_PROFILE=default AWS_REGION=<region> terraform -chdir=infra output -raw cloudfront_distribution_id
aws cloudfront get-distribution-config --id <distribution-id> --profile default --region <region> \
  --no-cli-pager --query 'DistributionConfig.Origins.Items[?Id==`api-gateway-origin`]'
# expect VpcOriginConfig present and CustomOriginConfig absent

curl -sS -o /dev/null -w '%{http_code}\n' "https://<cloudfront_domain_name>/api/<route>"
```

An unauthenticated `/api/*` request should return **401 or 403**: the API answered. A 502 or 504
means the origin is not reachable; check that the second apply installed the real security-group id.

The `/api/*` behaviour uses the managed `AllViewerExceptHostHeader` origin request policy, so
CloudFront sends the origin's own domain name as `Host`. Forwarding the viewer `Host` (the
`AllViewer` policy) sends a host API Gateway will not route.

## Bringing up the UI

`terraform apply` alone leaves the CloudFront origin bucket empty, and a published site alone leaves a
signed-in user with an empty CRM. Five steps:

```bash
# 1. Apply and publish (deploy.sh publishes unless --skip-frontend).
CRM_EXPECTED_ACCOUNT_ID=<account-id> PATH=.venv/bin:$PATH bash scripts/deploy.sh --profile default --region <region>

# 2. Seed the demo tenant with the rich corpus (pending proposals, audit history, a truncating
#    activity set, an empty account). Seeds twice to prove idempotency and asserts the canary tenant
#    stays empty. --table-prefix is required in practice: without it the prefix is empty and names
#    no table, and the script exits 3 ("nothing seeded, nothing verified").
PATH=.venv/bin:$PATH python3 scripts/seed_demo_tenant.py \
  --table-prefix crm-agent-eval --outputs results/tf-outputs.json \
  --profile default --region <region>

# 3. Create a sign-in user and its reps item. --skip-seed because step 2 already seeded.
export CRM_DEMO_USER_PASSWORD='<a password meeting the pool policy>'
export CRM_DEMO_USER_EMAIL='demo@example.test'
PATH=.venv/bin:$PATH python3 scripts/provision_demo_user.py \
  --outputs-json results/tf-outputs.json --email "$CRM_DEMO_USER_EMAIL" \
  --profile default --region <region> --skip-seed
unset CRM_DEMO_USER_PASSWORD

# 4. Write the reps item of the AgentCore Gateway service principal svc-mcp-gateway: tenant demo,
#    book = every account id the demo seed writes, full_visibility false. External MCP clients
#    resolve to it; without it every Gateway tools/call fails closed. Convergent.
AWS_PROFILE=default AWS_REGION=<region> terraform -chdir=infra output -json \
  | PATH=.venv/bin:$PATH python3 scripts/provision_gateway_principal.py \
      --outputs-json - --profile default --region <region>

# 5. Open the site and sign in with that email and password.
AWS_PROFILE=default AWS_REGION=<region> terraform -chdir=infra output -raw cloudfront_domain_name
```

Notes:

- `seed_demo_tenant.py` requires `--outputs` for a remote run; the resolved account and region are
  checked against it before anything is written. `provision_demo_user.py` checks `--profile` and
  `--region` against its outputs document (`--outputs-json`, or stdin) the same way.
- `provision_demo_user.py` keys the `reps` item on the Cognito-assigned **username** (a UUID, because
  the pool uses `email` as its username attribute), which is what the pre-token-generation trigger
  reads as `rep_id`. It is convergent: a re-run resets the password and overwrites the `reps` item.
  Other flags: `--book` (comma-separated account ids), `--no-full-visibility`, and `--admin`, which
  also adds the user to the `admin` group (`crm.authz.ADMIN_GROUP`; idempotent). Use a separate
  `--email` for an admin demo user so the primary demo user stays non-admin. Because the
  provisioner refuses an `--email` that differs from `CRM_DEMO_USER_EMAIL`, set
  `CRM_DEMO_USER_EMAIL` (and `CRM_DEMO_USER_PASSWORD`) to the admin user's values for that one
  command. The admin journeys in [`tests/e2e/browser/`](../tests/e2e/browser/) sign in as that user through
  `CRM_E2E_ADMIN_EMAIL` and `CRM_E2E_ADMIN_PASSWORD`.
- Keep both passwords in a `0600` file outside the repository (for example under
  `~/.local/state/crm-eval/`) and `source` it before a run; never in shell history or a tracked file.
- `provision_gateway_principal.py` checks `--profile` and `--region` against the outputs document
  before writing, writes the `demo` tenant only (it has no tenant option), and lives outside
  `scripts/smoke/`. The Layer 5/6
  gateway smokes move that row to their smoke tenant and restore it byte-identical at teardown.
- Without `--skip-seed` the provisioner seeds the base corpus only. That is redundant after step 2
  but not destructive.
- The browser journeys in `tests/e2e/browser/` read `CRM_DEMO_USER_EMAIL`. The provisioner refuses an
  `--email` that differs from it when the variable is set.
- The pool's MFA default is `OPTIONAL`, so the SPA signs in with the password alone. TOTP stays
  available; a user who enrols is still challenged.
- All three scripts write only the `demo` tenant. None can write `canary` or an `eval-<run_id>` tenant.

## Verification layers 4-6

### Running

`scripts/verify_deployment.sh` runs Layers 4, 5 and 6 in order and stops at the first failing layer.
Every module within a layer runs even if an earlier one fails, so the layer's inventory is complete.

```bash
# Preview: no AWS call, no tenant. The printed module list is the authoritative per-layer membership.
bash scripts/verify_deployment.sh --dry-run --profile default --region <region>
bash scripts/verify_deployment.sh --dry-run --layer 5 --profile default --region <region>

# One layer, live.
bash scripts/verify_deployment.sh --layer 4 --i-understand-this-is-billable \
  --profile default --region <region>

# All three layers, live.
bash scripts/verify_deployment.sh --i-understand-this-is-billable --profile default --region <region>

# Named modules only (a PARTIAL run), live; preview the selection first with --dry-run.
bash scripts/verify_deployment.sh --only iam_roles,eval_sfn --i-understand-this-is-billable \
  --profile default --region <region>

# Through make: the target passes no flags of its own.
make verify-deploy VERIFY_DEPLOY_ARGS="--layer 4 --i-understand-this-is-billable --profile default --region <region>"
```

| Flag | Effect |
|---|---|
| `--i-understand-this-is-billable` | required for a live run; without it (or `--dry-run`) the script exits 2 before any AWS call |
| `--dry-run` | print each module's exact command line; no AWS call, no tenant |
| `--layer 4\|5\|6\|inventory` | run one layer only, or consolidate (see below). `--layer 4`, `5` or `6` is **PARTIAL**, exactly as `--only` is, with `"partial_selector": "--layer N"` in the per-run inventory |
| `--only MOD[,MOD...]` | run only the named modules (short `iam_roles` or full `scripts.smoke.iam_roles`; repeatable), in layer order. An unknown name exits 2 before any AWS call. With `--layer N` every name must be in layer N; not allowed with `--layer inventory`. The run is **PARTIAL**: the summary says so, and `verification-inventory.run.json` becomes `{"partial": true, "partial_selector", "selected_modules", "not_run_modules", "results"}` instead of the full run's array. A partial run is never a full-deployment pass |
| `--profile`, `--region` | target; flag, then `AWS_PROFILE`/`AWS_REGION`, then `default`/`us-east-1` |
| `--outputs PATH` | Terraform outputs document (default `results/tf-outputs.json`) |
| `--token JWT` | bearer token for authenticated checks (also `CRM_SMOKE_TOKEN`); never passed to the Gateway modules, which mint their own machine token. A token 45 minutes old (`VERIFY_SMOKE_TOKEN_MAX_AGE_SECONDS`, default 2700) is replaced by a freshly minted one before the next token-taking module, and the previous minted user is removed |

What a live run does:

- refuses a wrong target: the region is checked offline against the ARNs in the outputs document and
  the account against a live `sts:GetCallerIdentity`, both before the first check (`WrongTargetError`,
  "Nothing has been checked"). A dry-run refuses on a region mismatch rather than print a misleading
  preview. If the outputs name no account, the run proceeds and prints `NO TARGET ASSERTION WAS MADE`;
- provisions a disposable `eval-smoke-<hex>` tenant and, unless `--token` is given, mints a smoke
  user and token for that tenant. A supplied token must name this run's tenant or the run refuses;
  in practice, omit `--token`;
- passes `--token` only to modules that declare it (derived by parsing each module). The Gateway
  modules do not: the Gateway admits only a machine token carrying `gateway/invoke`, so each mints
  its own. `gateway_tools` and `integration_gateway_scope` take an operator
  override as `--gateway-token`/`CRM_SMOKE_GATEWAY_TOKEN`; `mcp_chat_gateway` and
  `mcp_client_compat` take none. Minting reads the MCP client secret from the file named by
  `CRM_MCP_CLIENT_SECRET_FILE`, else from the `cognito_mcp_client_secret` output. After a rotation
  that output is empty, so set the variable to the `--secret-out` file before running
  `gateway_tools`, `integration_gateway_scope` or `mcp_chat_gateway`;
- tears down the tenant and the smoke user on exit, failure or interrupt;
- writes a per-run rendering to `results/verification-inventory.run.{json,md}`.

Exit codes: `0` every check reported `pass`, `skip` or `defer`; `1` a layer or the teardown failed;
`2` a usage or environment error. A `defer` is a disclosed control that is not verified, carrying a
machine-readable `defer_reason`; it never passes a check and never masks a `fail`.

Each module also runs on its own, with the shared flags `--profile`, `--region`, `--outputs`,
`--tenant`, `--json`:

```bash
.venv/bin/python3 -m scripts.smoke.dynamodb_tables --profile default --region <region> \
  --outputs results/tf-outputs.json
```

Run directly, a module asserts the same target guard, and `--json` emits its inventory to stdout.

### Layer 4: per-resource smoke

Modules in `LAYER_4_MODULES`, in run order. Every check is read-only or writes only under a disposable
smoke tenant, never `demo`, with one exception: `eni_function_code_deny` creates a run-scoped Lambda
function, `crm-agent-eval-eniprobe-<run>`, invokes it, and deletes it. It also deletes probes an
interrupted earlier run left behind.

| Module | Asserts |
|---|---|
| `smoke/lambda_actions.py` | every action Lambda (from `ALL_TOOLS`) succeeds for an in-scope identity envelope and returns typed `scope_denied` (not a 500 or `not_found`) for an out-of-scope target; no response leaks `detail`; `create_opportunity`'s optional `primary_contact_id` is persisted when valid and refused with `validation_failed` when it belongs to another account. The listing tools (`search_opportunities`, `search_accounts`, `pipeline_aggregate`) are denied `scope_denied` naming `account:*` for an empty-book caller, and `explicit_out_of_book_filter_denied` checks that `search_opportunities` with an out-of-book `account_id` names `account:<id>`. On the seeded departed contact: `departed_contact_update_refused` (409 `contact_departed`, missing scope `status:active`), `departed_contact_reactivation_allowed` (an update that sets `status`) and `departed_contact_restored` (set back to `departed`) |
| `smoke/deployed_parity.py` | every deployed action Lambda plus the identity interceptor and pre-token-generation function has `CodeSha256` equal to its local zip's base64 SHA-256. Fail-closed on a missing build directory, zip or function. `--deploy-gate` is the form `deploy.sh` runs. Does not cover the relay |
| `smoke/api_routes.py` | private REST API: every route answers with a real Cognito token and returns 401 unauthenticated; every method names an authorizer and declares `COGNITO_USER_POOLS`; stage throttling is set; a `body-and-params` request validator exists and every method references it, with an `application/json` model on every POST |
| `smoke/iam_roles.py` | every execution role, via `iam simulate-principal-policy`: allowed its own actions, denied every other function's. Each non-own pair yields a `simulate:` row (denied, pass/fail) and an `explicit_deny:` row (a matching Deny; an implicit deny fails). Each own grant the matrix declares `leading_keys`/`key_prefix`, sweep roles included, also yields a `conditioned_scope_denied:` row that probes a foreign key independent of the HCL parser, and `conditioned_grants_parity` fails if the parsed and declared conditioned grants differ. `matrix_readable` fails, and no role is examined, when `table_action_role_matrix.json` is malformed. A role whose policies cannot be loaded fails `leading_keys:policies_unreadable`. A `leading_keys:<table>` row fails when a `ForAllValues:*` LeadingKeys Allow also grants an action that carries no LeadingKeys context (the condition holds vacuously for it). A role that cannot be resolved skips its matrix. A tool's Query grant scoped by `local.tool_query_indexes` (revert_audit_record's child check) is simulated on its first index, with `index_only_base_table_denied:<fn>:dynamodb:Query@<table>` asserting the base table is refused and `scoped_index_allowed:<fn>:dynamodb:Query@<table>/<index>` covering each further index. `own_table_denied:<fn>:<action>@<table>` asserts an explicit Deny for each action in `OWN_TABLE_DENIAL_PROBES` (revert_audit_record's PutItem and UpdateItem on notes; sweep_control's PutItem on each seed table, which it writes through `BatchWriteItem` only). A sweep grant carrying a `select_condition` (sweep_control's `demo` Query on `gsi_tenant`, `Select = COUNT` only) yields `extra_condition_allowed:` (COUNT allowed), `extra_condition_denied:` (each other `Select` value denied), `extra_condition_absent_denied:` (no `Select` denied, the key reported missing) and `extra_condition_sibling_allowed:` (the `eval-*` and `canary` grants on the same index still read rows) rows; an unreadable grant list fails `extra_conditions_readable`. `coverage_is_non_vacuous` bounds the run by a derived minimum check count |
| `smoke/gateway_tools.py` | Gateway `tools/list` names exactly the toolspec's tools; `schema_matches`, per tool: the schema `type`, property names, each property's `type` and the `required` set match (constraint keywords such as `enum`, `minLength` and `maximum` cannot be carried by the Gateway and are reported as a `skip`); `description_matches_toolspec`, per tool: the tool's own `description` is byte-identical to the toolspec's (fails on a differing or missing description; `defer` when the tool is absent from `tools/list`); `property_descriptions_match_toolspec`, per tool: every property in both the deployed schema and the toolspec has the same `description`, byte for byte (fails on a differing, missing or undeclared description; `defer` when the deployed properties cannot be read or no property is shared); every tool invokes; a caller-supplied `_identity` is discarded. Provisions its own service principal and `client_credentials` token. An expired token and an underscoped token are each refused, after a premise check on the token's own claims; it mints both itself (a second machine token, minted first and aged past its 5-minute lifetime, so the run takes up to about 5 minutes longer; and a real end-user access token whose user is deleted afterwards). Optional operator overrides: `CRM_SMOKE_EXPIRED_TOKEN`, `CRM_SMOKE_UNDERSCOPED_TOKEN`, and `--gateway-token`/`CRM_SMOKE_GATEWAY_TOKEN`, which also turns off both mints, so the two refusal rows then need the other two overrides |
| `smoke/agents.py` | one real chat turn through CloudFront `/agent/*` → relay → Runtime, as a user it provisions on a disposable tenant with an empty book, naming a `demo` account that user cannot see (its listing calls are denied `account:*`): the turn completes, no `tool_result` carries the out-of-book account, every event kind is in the closed vocabulary, and the per-agent grid (`responds`, `no_surplus_tool`, `no_missing_tool`, `no_direct_high_risk_write`, and the mutation subagent's `outcome_discipline_*`). A grid row the stream cannot establish is a `defer` naming its reason (`GRID_DEFER_REASONS`: `stream_carries_no_advertised_tool_surface`, `tool_call_events_carry_no_role`, `relay_turn_cannot_select_an_outcome_trigger`), never a `pass`. One model-backed turn (`MODEL_TURN_ALLOTMENTS`), billable. `demo` is proved untouched |
| `smoke/dynamodb_tables.py` | every table in `crm.store.schema`: customer-managed KMS encryption, PITR, every GSI present, `ACTIVE`, queryable and projecting what `crm.store.schema` declares (`tenant` on every index; `subject` on activities' and `body` on notes' `gsi_tenant`, which scoring reads for agent-created rows), a failure naming the missing attributes, every mandatory tag (`AppName = crm-agent-eval` included); schema and `dynamodb_table_names` output reconciled |
| `smoke/dynamodb_principal_deny.py` | the DynamoDB principal boundary, read from the account. `table_policy_shape:<table>` and `key_policy_shape`: each table's resource policy and the DynamoDB CMK's key policy carry exactly the designed Deny statements, the data-plane allowlist equals the discovered project Lambda roles (each `crm-agent-eval-*` function whose role has a `<function>-dynamodb-kms` inline policy) plus `crm_operator_principal_arns`, and the administration allowlist equals the operators plus the account root. `table_sse_enabled:<table>`: `ACTIVE`, no `InaccessibleEncryptionDateTime`, SSE `ENABLED`. `data_plane_roles_discovered`: at least one project Lambda role was found. `operator_data_plane_admitted`: the operator's `GetItem` on an absent key succeeds. `probe_denied:<call>` (GetItem, Query on a GSI, conditional PutItem, conditional PutResourcePolicy, `kms:GenerateDataKey`): a run-scoped role `crm-agent-eval-probe-<run>`, trusted by the operator only and holding its own Allow for those actions, is refused with "explicit deny in a resource-based policy"; success, an implicit deny, `ConditionalCheckFailedException` or `PolicyNotFoundException` fails. `probe_role_setup` appears only on failure, when creating, granting or assuming the probe role raised. No probe call can write data. The probe role is deleted in a `finally` (`probe_role_deleted`); if that row fails, delete the role by hand. Before the principal-boundary Deny is applied, the `probe_denied` rows fail and the shape rows fail or defer; after it, every row passes |
| `smoke/store_principal_deny.py` | the principal boundary on the state backend, the eval-artefacts key and bucket, the logs key, the CloudTrail key and bucket, and the frontend key and origin bucket; see "Store principal boundary" for its rows. `admin_simulated_denied` rows are a simulation of the live bucket document, not a call. The probe role is deleted in a `finally` (`probe_role_deleted`) |
| `smoke/cognito_claims.py` | MFA and password policy; `AllowAdminCreateUserOnly` true (`admin_create_user_only`); an unsigned `SignUp` refused with `NotAuthorizedException` (`self_signup_refused`); the V2 pre-token-generation trigger stamps `rep_id`, `book`, `full_visibility`, `tenant` into a real token. Provisions and deletes its own user. `CRM_SMOKE_COGNITO_USERNAME`/`CRM_SMOKE_COGNITO_PASSWORD` select an existing enrolled user instead |
| `smoke/cloudfront.py` | OAC-only S3 origin and a direct S3 request refused; `/*`, `/api/*`, `/agent/*` route to the right origins; a deep link returns the SPA document, a missing `*.js` does not, an unauthenticated `/api/*` returns 401/403. TLS 1.2 minimum reports `defer` (`missing_custom_acm_certificate`) |
| `smoke/access_log_delivery.py` | log objects actually arrive in each access-log bucket under each expected prefix; an empty bucket is `defer` (`no_log_objects_delivered_yet`), since delivery takes hours |
| `smoke/relay_iam.py` | the relay execution role's allow and deny simulation matrix with positive controls; every other role denied invoking the Runtime; `X1_chat_target_deny_covers_actions_nobody_enumerated` (a fabricated action on the Runtime, simulated as the chat-target role, is `explicitDeny`); the relay Function URL's `AuthType` is `AWS_IAM`; `R1[<tool>]` for each toolspec tool no agent uses (approve, reject, revert): the Runtime role is denied `lambda:InvokeFunction` on it (synthetic-Allow positive control) and its `InvokeActionLambdas` statement names no pattern matching it |
| `smoke/runtime_bedrock_iam.py` | the Runtime role is allowed `bedrock:InvokeModel` and `InvokeModelWithResponseStream` on every `global.` inference profile in `config/model_capabilities.yaml`, and denied `bedrock:InvokeModel` on each retired profile in the module's `RETIRED_INFERENCE_PROFILES` |
| `smoke/runtime_inbound_auth.py` | the Runtime carries no custom JWT authorizer (IAM inbound auth), with a non-vacuity guard on the response shape; the runner's own principal, which is not an invoker, is refused `InvokeAgentRuntime` (an outside-VPC refusal, which the `SourceVpce` Deny alone could produce); `outside_vpc_invoke_refused_under_declared_deny` judges that refusal against the `aws:SourceVpce` Deny state the deployment declares (declared enabled: an explicit resource-policy refusal passes and an admission fails; declared disabled or unknown, or another error: `defer` with its reason); the resource policies on the runtime ARN and on its DEFAULT endpoint each allow exactly the invoker roles in `local.runtime_invoker_role_arns`, and every Allow names an `AWS` principal only. The policies are read with botocore, or with `aws bedrock-agentcore-control get-resource-policy` under the resolved profile and region when botocore does not model the call; `defer` only when neither does |
| `smoke/runtime_network_posture.py` | the Runtime and its `DEFAULT` endpoint are `READY` on the same version (`runtime_ready`, a bounded poll that fails at once on any status other than `READY`, `CREATING` or `UPDATING`, an absent status included); `platformVersion` is `V2` (`platform_version_expected`, read from the raw `GetAgentRuntime` body because the pinned botocore does not model the member); the ARN equals the stack output (`runtime_arn_unchanged`); the Runtime is `networkMode: VPC` in the stack's Runtime subnets (supported AZs) with the dedicated Runtime security group; the interface endpoints exist; no open egress; the `SourceVpce` deny state (`defer` until applied) |
| `smoke/chat_timeouts.py` | the deployed relay and `chat_target` Lambda timeouts and the CloudFront relay origin's read timeout equal the configured values; the Runtime's container image tag is the one the local `agent-source.zip` mints, and ECR holds it. The 600 s turn itself is a declared `skip`. A repackage after the deploy makes the image row fail |
| `smoke/chat_path_iam.py` | the chat path's least-privilege grants, via `iam simulate-principal-policy`: Gateway #2's role may invoke `chat_target` and the chat interceptor only (denied Gateway #1's interceptor, `eval_trigger`, every CRM action Lambda and `InvokeAgentRuntime`); Gateway #1's role is denied `chat_target`; `chat_target`'s role reaches `reps` only under `usersettings#`/`conversation#` keys and no other table. Every DynamoDB row passes a `LeadingKeys` context. Both functions' resource policies grant invoke to Gateway #2 only |
| `smoke/public_exposure.py` | against the account, not the source: every Function URL reports `AuthType: AWS_IAM`; no resource policy grants a wildcard principal; every Lambda's `VpcConfig` names a subnet and a security group; the REST API is private and reachable only through its VPC endpoint |
| `smoke/iam_policy_budget.py` | two rows per deployment role. `inline_policy_budget`: the aggregate inline-policy size against IAM's 10,240-character per-role quota. `managed_policy_budget`: each attached customer-managed policy's default version against the 6,144-character per-policy quota, and the attachment count against the managed-policies-per-role quota (10 by default; AWS-managed policies count but are not sized). Both rows are marked `ADVISORY:` at 85% and fail at 95% |
| `smoke/eni_function_code_deny.py` | every `crm-agent-eval-*` function is `Active`/`Successful`. Each distinct execution role, via `iam simulate-principal-policy` on `*`: without context the six Hyperplane ENI actions and the two X-Ray write actions are `allowed`; with `lambda:SourceFunctionArn` set, the ENI actions are `explicitDeny` and X-Ray stays `allowed`. Then it creates a run-scoped probe, `crm-agent-eval-eniprobe-<run>`, on the `capabilities` role and network. Reaching `Active` proves Lambda's own ENI path still works. The probe calls `DescribeSubnets` and a dry-run `DeleteNetworkInterface`, and both must return `UnauthorizedOperation`. The probe is deleted in a `finally`, and `get_function` must then return `ResourceNotFoundException`. If `probe_torn_down` fails, delete the function by hand. Lambda's asynchronous ENI work is not exercised |
| `smoke/resource_protection.py` | the trail delivering to `cloudtrail_s3_bucket_name` (exactly one must match) reports `IsMultiRegionTrail` and `IncludeGlobalServiceEvents` true (`trail_multi_region`) and `get-trail-status` `IsLogging` true (`trail_logging`). An API error, a missing output, or zero or several matching trails fails every row it prevents. `alb_deletion_protection`: the ALB named by `api_origin_alb_arn` reports `deletion_protection.enabled` `"true"`. `log_groups_discovered` fails unless each of the stack's `/aws/{lambda,codebuild,vendedlogs/states,apigateway}/crm-agent-eval-` prefixes matches a group and every expected group exists. The expected groups are the three output-named groups, `/aws/lambda/<name>` for every action Lambda in `ALL_TOOLS` and for `eval_trigger_function_name`, and the trail's `CloudWatchLogsLogGroupArn` group when the trail delivers to CloudWatch Logs. `/aws/cloudtrail/<trail name>` is also checked when it exists. `log_group_retention`, one row per group: `retentionInDays >= 365`, unset fails. `codebuild_encryption_key`: `crm-agent-eval-agent-image`'s `encryptionKey` resolves (`kms describe-key`) to a customer-managed key, the eval-artefacts bucket's default SSE-KMS key |

#### The Runtime's isolation-probe mode

The Runtime's `POST /invocations` accepts a read-only request shape for inspecting an agent's tool
surface (`src/agents/runtime_server.py`). No smoke module sends it:

```json
{"mode": "isolation_probe", "role": "mutation", "trigger": "scope_denial"}
```

| Field | Values | Meaning |
|---|---|---|
| `mode` | `isolation_probe` | selects the probe. `delegation`, `cancel` and `eval_turn` select the other three non-turn modes; any other value, or none, takes the normal turn path |
| `role` | `null`, `research`, `mutation`, `analytics`, `composer` | which agent to report on; `null` is the supervisor; unknown is `400 validation_failed` |
| `trigger` | `scope_denial`, `empty_result`, `handler_error` (optional) | which canonical handler payload to classify; unknown is `400 validation_failed` |

The response is JSON `{"mode", "role", "advertised_tools", "outcome"}` (`outcome` only with a
`trigger`). The probe executes no tool, calls no model, constructs no `Store` and writes nothing.
Identity resolves through the same path as a normal turn, so an unauthenticated probe is refused
`401`. `advertised_tools` comes from `agents.registry`, and `outcome` from the production
`agents.supervisor.classify_tool_result`.

### Layer 5: component integration

Modules in `LAYER_5_MODULES`. Each provisions a disposable `eval-smoke-<hex>` tenant where it writes,
refuses to run under `demo`, and reports a teardown failure as its own failing check.

| Module | Asserts | Identity |
|---|---|---|
| `smoke/integration_ui_api_ddb.py` | UI → CloudFront `/api/*` → API Gateway → Lambda → DynamoDB round trip | caller-supplied token (`--token`/`CRM_SMOKE_TOKEN`); tenant from its `custom:tenant` claim; optional `--account-id` and `--out-of-book-account-id`: when absent, the in-book account is the lowest entry of the token's `custom:book` claim (an absent or empty claim fails), and the out-of-book target is the lowest-numbered seeded account of that tenant outside the book |
| `smoke/integration_gateway_scope.py` | Gateway → interceptor → Lambda, opening with the MCP `initialize` handshake; scope enforcement survives the real interceptor hop: in-book reads succeed, out-of-book reads are `scope_denied` naming the missing scope, and an explicit account filter succeeds in book (`in_book_account_filter_succeeds`) and is denied out of book (`out_of_book_account_filter_denied_naming_scope`, `account:<id>`); a forged `_identity` (widening or narrowing) changes no response byte, and forged `_agent_identity` and `requestContext` blocks are refused `scope_denied`; an undeclared argument is refused by the tool's closed schema (`validation_failed`, `source: arguments`, one `{statusCode, error}` item, no data), and a forged identity nested inside one gets the same refusal, byte-identical to a same-shaped decoy; no denial carries internal detail; the service principal is refused `approve_proposal`, `reject_proposal` and `revert_audit_record`, each with HTTP 403 `scope_denied` naming `principal:service` (`service_principal_refused_<tool>`) | its own service principal and `client_credentials` token, or `--gateway-token`/`CRM_SMOKE_GATEWAY_TOKEN` with `--in-book-account`/`--out-of-book-account` |
| `smoke/integration_proposal_chain.py` | proposal → approval → audit → revert, plus both outcomes of the delete-proposal cascade, the `after_payload` allowlist, a note-create revert in scope (`note_revert:*`: the note is deleted and its audit marked reversed), and an approved delete reverted in scope (`delete_revert:*`: the contact is restored from the before-snapshot and its audit marked reversed); and two out-of-book reverts (`out_of_book_revert:*`): a note-create audit and a contact-delete audit, seeded under the disposable tenant for an account outside the book, are each refused with 403 `scope_denied`, and the note, the absent contact and both audit records read back unchanged | its own MFA-enrolled user, ID token; with `--token`, `--account-id` is optional: when absent, the account is the lowest entry of the token's `custom:book` claim (an absent or empty claim fails) |
| `smoke/integration_teardown_privilege.py` | `dynamodb:DeleteItem` simulates as denied for `sweep_control`'s role (the state machine's `Teardown` step) on every tenant-scoped table, each deny resting on positive controls (the action is expressible, the ARN matches teardown's own `Query` grant, and any matrix holder of `DeleteItem` is allowed it); the role holds `BatchWriteItem` and teardown's `Query` on each table (`worker_holds_*`); `teardown_was_not_vacuous`: the fixture corpus the seeder writes is non-empty. No sweep and no tenant write; the teardown itself is `eval_sfn`'s `tenant_emptied_by_the_workers_own_role` | the runner's own credentials (simulation only) |
| `smoke/three_tier_chat.py` | one delegation-forcing turn invokes all three model tiers, attributed to the Runtime role in CloudTrail. The supervisor-tier invocation is the positive control for the role, the window and the supervisor family's CloudTrail event name only; it does not cover a lower tier on the other family. CloudTrail tells tiers apart only by inference profile, so the turn runs under the first `{tier: family}` mapping whose `config/models.yaml` models have three distinct profiles: all-anthropic, then an openai supervisor with anthropic subagent and tool, then the mixed-family mapping. The row detail names the mapping. When none has distinct profiles, the turn is not sent and `attribution_mapping_profiles_distinct` fails, naming each mapping's profiles. The delegation turn gets the mixed-family turn's cost and resolved-tier rows for its mapping, suffixed `:attribution`; a mixed-family turn (schema-3 tuning) completes with non-empty `cost_lines`, a positive `cost_usd` and `resolved_tiers` naming each tier's `config/models.yaml` model and effort, with a priced cost line per tier model; a schema-2 tuning carrying `temperature` is refused as one SSE `error` frame (`invalid_request`, naming `tuning.tiers.<tier>.temperature`) with no `usage`, `completed` or `text_delta` frame; a second principal reusing another user's session id is refused (HTTP 400 through the relay), while the owner's own reuse succeeds; a delegation-forcing turn with every tier on openai gets the mixed-family turn's rows suffixed `:openai_supervisor`, including `supervisor_resolved_effort_is_<effort>:openai_supervisor` (the effort from `config/models.yaml`); for each family, the second of two supervisor turns sharing the stable prefix reports `cache_read_tokens > 0` on its supervisor cost line (`supervisor_prompt_cache_read_on_second_turn:<family>`; the anthropic pair is the mixed-family seed and owner-reuse turns, the openai pair is the openai turn and a short probe turn), `defer` with `platform_reported_no_cache_tokens` when neither turn reports any cache token and `fail` when a write is reported but nothing is read; a `page_context` route planted with a run-unique marker, once longer than the route bound and once outside its character set, is refused with HTTP 400 and the relay's generic validation message with the marker absent from the response, against the probe turn's conforming `page_context` as the control (`page_context_over_bound_route_refused`, `page_context_off_charset_route_refused`; no model call). Five model-backed turns (`MODEL_TURN_ALLOTMENTS`); a turn past that share is not issued | provisioned users |
| `smoke/eval_sfn.py` | the `eval_sweep` state machine through `POST /api/eval_trigger`: a stub rehearsal at `REHEARSAL_REPEATS` repeats (single-flight 409; `unit_tenants_recorded`: the run's `eval_runs` row lists every (arm, repeat) unit tenant; `unit_tenants_seeded`: each holds rows while the run is running; `run_records_repeats`; `completed` with arms x repeats x tasks done, `SUCCEEDED` execution, `eval_results` rows reached the Runtime with one distinct `result_id` per item and an equal share at each `repeat_index`, artifacts pass `assert_artifacts` at tasks x repeats, each arm's `provenance.effort` is `DEFAULT_ARM_EFFORT` (one `arm_effort_is_default:<family>` row per default-sweep family, in sweep order), each arm's models are `config/models.yaml`'s (one `arm_models_are_configured:<family>` row per default-sweep family: `provenance.model_id` is the family's supervisor model and every tier's configured model id is that tier's model; an absent provenance or tier entry fails), `prompt_bundle_matches_repo` (every arm's `provenance.prompt_bundle_sha256` equals `agents.prompt_bundle.prompt_bundle_sha256()` of the checkout running the smoke; an absent digest fails), `artefact_url` presigns a recorded artefact that an https GET fetches (200) while plain http gets 403 `AccessDenied` and a tampered key 403 `SignatureDoesNotMatch` (the S3 error code is asserted, not only the status), the sweep tenant, `canary` and lock left clean, `demo` unchanged; `tenant_emptied_by_the_workers_own_role`: the run's `eval_runs` row is `torn_down` and the run tenant and every unit tenant read back empty, so the `Teardown` step emptied them under `sweep_control`'s own role) and a cancel proof on a second stub sweep at one repeat (`accepted`, then `cancelled` with a partial matrix and clean teardown, then `already_terminal`); least-privilege role simulations for `sweep_control`, `sweep_task`, the state machine role and `eval_trigger`, derived from the `infra/eval_sfn.tf` grant locals: every DynamoDB simulation of `sweep_control`'s role, allows and denies alike, sends `dynamodb:Select = COUNT` (the value its `demo` Query grants admit, so a deny is not the Select condition's doing), and a row answered with a context key missing never passes; an allow per conditioned grant value, denies for out-of-scope keys (a `demo` twin, a foreign key, and a minted-id twin such as `act-<uuid>` for `act-eval-*`) and for `demo`/`canary` where a `gsi_tenant` grant does not admit them, and an explicit-Deny `*_denies_removed_*` row for each asserted action a role no longer holds on a table, such as the seed `PutItem` (incl. `trigger_allows_artifact_get` on the artefact prefix, and `trigger_denies_{get_outside_prefix,artifact_put,list_bucket}`); `machine_tracing_enabled` (the deployed state machine's `tracingConfiguration.enabled` is true); `machine_allows_{GetSamplingRules,GetSamplingTargets,PutTelemetryRecords,PutTraceSegments}` for the four SFN-XRAY-1 X-Ray actions on `*`, and `machine_denies_{BatchGetTraces,PutEncryptionConfig}` for X-Ray actions outside it. `--live-once --i-understand-this-is-billable` is the single live sweep, gated on a matching `--retention-verdict` | its own MFA-enrolled admin user with `full_visibility`, ID token |
| `smoke/admin_authz.py` | the eval run API's admin-only operations: no token refused; the admin group exists and the admin token carries it; a non-admin's `submit {mode: live, confirmed: false}`, `cancel` and `unlock` each return 403 `eval:admin`; an admin `cancel` of a terminal run returns `already_terminal` and changes nothing | two MFA-enrolled users it provisions, an admin and a non-admin with `full_visibility` |
| `smoke/user_settings_api.py` | `POST /api/user_settings` keeps each user's tuning their own: defaults on `config/models.yaml`'s `default_family` before a save; a valid schema-3 `put` reads back; a `put` carrying `temperature` or naming a model no longer configured is 400 with the row unchanged; `GET /api/capabilities` names every `config/models.yaml` model and carries no `temperature`, and its `tiers.<family>.supervisor.tuning.effort` equals `config/models.yaml`'s (one `capabilities_supervisor_effort:<family>` row per configured family); the served defaults match the config: `capabilities_default_family`, `capabilities_tier_model:<family>:<tier>` and `capabilities_tier_effort:<family>:<tier>` per configured cell, and `capabilities_assignable_tiers:<model>` and `capabilities_selectable:<model>` per `config/model_capabilities.yaml` model (Opus and Astra included); another user in the same tenant still sees defaults; a `put` naming another `rep_id` and an unsupported effort are 400 `validation_failed` with nothing changed; `reset` restores the defaults on the default family; least-privilege simulations for the `user-settings` role | two MFA-enrolled users under one tenant it provisions, each with a distinct run-scoped username |
| `smoke/mcp_chat_gateway.py` | Gateway #2 exposes only `chat_with_supervisor` behind its JWT: no token is 401; a token whose signature is replaced is refused (401 or 403, with no `tools/list` result) while the same claims unaltered are answered; the chat-scoped user token is refused on Gateway #1 and a `gateway/invoke`-only token on Gateway #2; a user call and a `client_credentials` call are answered; a call carrying a `temperature` argument returns `isError` `invalid_request` with no answer or conversation id; another user's `conversation_id`, with or without a forged `_agent_identity`, is refused (`isError`, `error_kind` read from the MCP `result`). Two model-backed turns. Each smoke principal's `conversation#` items are found by owner, deleted and re-read | two users and the service principal it provisions |
| `smoke/mcp_client_compat.py` | Both Gateways work from the real `mcp` Python SDK client (`streamable_http_client` + `ClientSession`): Gateway #1 with a machine token completes `initialize` → `notifications/initialized` → `tools/list`, its names (target prefix stripped) equal the toolspec, and one read-only `get_account` answers; Gateway #2 with a user token lists exactly `chat_with_supervisor` (no `tools/call`, which is model-backed and owned by `mcp_chat_gateway`); an unauthenticated SDK connect is 401 on each; Gateway #2's OAuth discovery chain holds (401 `WWW-Authenticate` `resource_metadata` → RFC 9728 document naming the pool issuer and exactly `supervisor/chat` → issuer OpenID configuration → an S256 authorization-code request for `cognito_mcp_oauth_client_id` redirects to the login page, not to the callback with an `error`); the same request for `gateway/invoke` is refused `invalid_scope`; the Gateway #2 user token is refused 401/403 by Gateway #1. No model call | the service principal and the user it provisions |

`integration_ui_api_ddb`, `integration_gateway_scope` and `integration_proposal_chain` also publish `demo_tenant_untouched` (row counts
of `demo` before and after) and `writes_only_under_smoke_tenant` (this run's own rows read back,
failing on zero), from `scripts/smoke/demo_tenant_guard.py`.

When removing a smoke principal raises, `integration_gateway_scope` reports a `fail` row
(`cleanup_service_principal`) naming the exception, and the `demo_tenant_untouched` check still runs. Remove the leftover principal by
hand (see "Residue cleanup").

### Layer 6: end to end

Modules in `LAYER_6_MODULES`, each with the flag `verify_deployment.sh` passes:

| Script | Asserts |
|---|---|
| `scripts/smoke_agent_stream.py --assert-incremental` | a chat turn through CloudFront `/agent/*` is delivered incrementally (the arrival spread clears `_MIN_SPREAD_SECONDS` and the lead-in fraction stays at or below `_MAX_LEAD_IN_FRACTION`); the largest arrival gap follows a `tool_call` or `tool_result` event (`arrival_gap_aligns_with_tool_dispatch`; this does not establish where any coalescing of later frames happens); every `text_delta` frame arrives before the turn's single `completed` frame (`text_delta_frames_arrive_before_completed`); a `START RequestId` for the dispatched Lambda in CloudWatch (`lambda_dispatch_observed`, `defer` while logs are not yet indexed); the Runtime does not answer from the stub provider |
| `scripts/smoke_proposal_card.py` | a model-driven chat turn's high-risk write reaches the user as a structured `proposal` event (a prose rendition fails the same discriminator); nothing mutates and no audit record exists while pending; the card's `before` matches the live row and every `after` field is inside the payload allowlist; approving its proposal commits the change and writes exactly one audit record. A stub-shaped turn defers the card rows (`stub_provider_cannot_request_a_high_risk_write`) and the proposal is then raised through the API's `submit_proposal`, so the pending and approval rows still run |
| `scripts/smoke_text_delta_stream.py` | two turns run concurrently under distinct session ids; each carries exactly one `completed` frame, and their `text_delta` `event_id`s are disjoint and each carries its own session id (`concurrent_sessions_completed`, `concurrent_sessions_distinct_event_ids`) |

These scripts provision their own disposable tenant and Cognito user, prove `demo` untouched, and
delete what they create. Their turns reach the deployed Runtime's live model provider, so they are
billable: `smoke_agent_stream` issues one turn per candidate prompt until one calls a tool (at most
`_MAX_PROMPT_CANDIDATES`), `smoke_proposal_card` one turn, and `smoke_text_delta_stream` two. Run
one alone with `--json` to capture its inventory:

```bash
mkdir -p results/verification-inventory
PATH=.venv/bin:$PATH python3 scripts/smoke_agent_stream.py --assert-incremental \
  --profile default --region <region> --json > results/verification-inventory/agent_stream.json
```

A deployed eval sweep runs in the `eval_sweep` Step Functions state machine, started through
`POST /api/eval_trigger` (the Evals page, or `smoke/eval_sfn.py`). A sweep runs `repeats` from 1 to
`MAX_SWEEP_REPEATS` (`src/eval/runs.py`), default 1, and each (arm, repeat) unit runs in its own
`eval-<run_id>-a<arm>r<repeat>` tenant. `sweep_control` records and seeds every unit tenant,
aggregates, tears every unit tenant down and finalizes the run record, all under its own role.
`sweep_task` drives each (arm, repeat, task) item as one `eval_turn` on the Runtime, in that item's
unit tenant. Layer 5's `eval_sfn`
rehearses it in stub mode. One live sweep is `eval_sfn --live-once --i-understand-this-is-billable
--retention-verdict=<mode>` (see "Data retention").

The two sweep roles hold only the DynamoDB grants their code reaches (`infra/eval_sfn.tf`):

- `sweep_control` seeds (through `Store.batch_put_items`) and deletes with `BatchWriteItem`. Its seed
  `PutItem`, limited to keys matching the table's `<kind>-eval-*` pattern
  (`local.sweep_seed_key_patterns`), is retained but unused, pending narrowing. It reads the tenant tables only through `gsi_tenant` Queries under `eval-*`, `demo` (the baseline
  read) and `canary` (the emptiness check). It has no `GetItem` on any tenant table.
- `sweep_task` reads golden-task records with `GetItem` under the same key patterns, reads proposals
  by id, and queries the five seeded tables' `gsi_tenant` under `eval-*`. It holds nothing on
  `reps`, `audit_records` or `usage_records`.
- On each table a role uses, every DynamoDB action granted anywhere in `infra/` that the role is
  not granted is explicitly denied, and every other table is denied `dynamodb:*`.

On the tenant tables, the grants that cannot carry a key condition are `sweep_control`'s
`BatchWriteItem` on every tenant table except `reps` (teardown deletes rows the agent created, whose
ids carry no tenant, and seeding writes through it) and `sweep_task`'s proposal reads. Both roles' `eval_results` grants are unconditioned too; that
table is keyed by run id, not by tenant. `eval_sfn.tf` labels the tenant-table ones
SWEEP-CONTROL-1. Code guards them: seeding and teardown refuse the `demo` and `canary` tenants, every
scoring read re-checks the row's tenant, and the `canary` tenant is asserted empty after every
teardown. `smoke/iam_roles.py` and `smoke/eval_sfn.py` simulate each conditioned grant with an
in-scope key (allowed) and a `demo` key and a uuid (denied), and each removed action as denied.

`src.eval.harness` still runs a sweep in-process, for local and stub use; see
[`docs/eval-methodology.md`](eval-methodology.md). `--live` without `EVAL_SUBSTRATE=live-aws` (or `--substrate live-aws`)
exits 2, and a live-AWS sweep needs `ACTION_LAMBDA_PREFIX=crm-agent-eval` and
`CRM_TABLE_PREFIX=crm-agent-eval`. Without the Lambda prefix the model call succeeds and every tool
dispatch fails with `ResourceNotFoundException`.

### T4 browser journeys in the Playwright container

`tests/e2e/browser/` cannot run on this host, because the `chromium-headless-shell` install is killed
during extraction. Run the journeys in the official Playwright Linux image instead. This is
billable: they drive the deployed dev stack.

```bash
export CRM_DEMO_USER_EMAIL=... CRM_DEMO_USER_PASSWORD=...   # the provisioned demo user
scripts/run_browser_journeys_container.sh                    # every journey
scripts/run_browser_journeys_container.sh journeys/01-signin.spec.ts
```

| Item | Value |
|---|---|
| Image | `mcr.microsoft.com/playwright:v<version>-noble`, `--platform linux/amd64`. The version is read from `tests/e2e/browser/package-lock.json`, and the script exits 1 if it cannot be read or the three Playwright packages disagree |
| Target | `CRM_E2E_BASE_URL`, or a stack outputs document (`CRM_TF_OUTPUTS_JSON`, default `results/tf-outputs.json`) that passes `scripts/outputs_provenance.py --check`. With neither, or with stale outputs, the script exits 1 before starting a container |
| Secrets | `CRM_DEMO_USER_*`, `CRM_E2E_ADMIN_*` and the `CRM_E2E_ALLOW_LIVE_SWEEP*` opt-ins are passed to the container by name (`-e NAME`), only when set, and never appear on the command line |
| Install | `npm ci` runs inside the container into a container-only `node_modules` volume. The host's `node_modules` is not touched |
| Exit status | Playwright's own when it is non-zero. A run Playwright reports as clean still exits 1 when `tests/e2e/browser/scripts/check-executed.mjs` fails its JSON report (see the executed-count floor below). Reports land in `results/e2e-browser.json`, removed before each run, and failure traces in `tests/e2e/browser/test-results/` |
| Docker CLI | `DOCKER_BIN`, else `docker` on PATH, else Docker Desktop's bundled `/Applications/Docker.app/Contents/Resources/bin/docker` |

The script sets no `channel`, `headless` or `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD` override.

**Executed-count floor.** After Playwright exits, `node scripts/check-executed.mjs` reads
`results/e2e-browser.json` and fails the run (exit 1) when:

- nothing executed (`stats.expected + stats.unexpected + stats.flaky` is 0);
- a skipped test is not in `ALLOWED_SKIPS`, matched by spec file and title. The allowed skips are
  the data guards in `02-pipeline` (no Closed Won record in the book) and `09-panel-focus-and-live-region`
  (panel restored open), plus the live tests in `15-live-sweep-admin` and `16-sweep-cancel-resume`,
  each allowed only while its opt-in (`CRM_E2E_ALLOW_LIVE_SWEEP`, `CRM_E2E_ALLOW_LIVE_SWEEP_CANCEL`)
  is not `1`. With an opt-in set to `1`, a skip of that test fails the run;
- the report is missing, unreadable, or carries no integer `stats` counts.

On success it prints `check-executed: OK — <n> executed, <m> declared skip(s)`. Because the report is
removed before each run, `--list` passed through this script writes no report and fails; run
`./node_modules/.bin/playwright test --list` directly in `tests/e2e/browser/` for collection only.

Set `CRM_E2E_ADMIN_EMAIL`/`CRM_E2E_ADMIN_PASSWORD` (the `--admin` user from "Bringing up the UI") for
the admin journeys. Journeys 15 and 16 each hold one test that starts (and in 16 cancels) a real,
billed sweep. That test skips unless `CRM_E2E_ALLOW_LIVE_SWEEP` (15) or
`CRM_E2E_ALLOW_LIVE_SWEEP_CANCEL` (16) is exactly `1`; leave them unset unless the sweep spend is
intended. A full run takes about 30 minutes.

Journey 14 (`14-tier-tuning`) reads each turn's SSE frames through a recorder installed in the page
(`tests/e2e/browser/fixtures/sseRecorder.ts`): it wraps `fetch`, tees every `text/event-stream`
body, and hands one branch to the app unchanged. To run that journey alone:
`scripts/run_browser_journeys_container.sh journeys/14-tier-tuning/tier-tuning.spec.ts`.

## The consolidated inventory

`--layer inventory` runs no layer and provisions no tenant. It reads every artifact under
`results/verification-inventory/*.json`, adds a live read-only residue query, and writes the
publishable `results/verification-inventory.{md,json}` through `scripts/consolidate_inventory.py`:

```bash
bash scripts/verify_deployment.sh --layer inventory --i-understand-this-is-billable \
  --profile default --region <region>
```

The layer runs write only the per-run `results/verification-inventory.run.*` files. The consolidator
reads only per-class artifacts and has no list of classes it expects, so a class with no file there is
silently absent. Write each class's `--json` output to `results/verification-inventory/<class>.json`,
and confirm the file count before publishing:

```bash
mkdir -p results/verification-inventory
.venv/bin/python3 -m scripts.smoke.deployed_parity --json --profile default --region <region> \
  --outputs results/tf-outputs.json > results/verification-inventory/deployed_parity.json
```

What the consolidator guarantees:

- a `defer` renders as `DEFER` with its `defer_reason`, never as a pass; it refuses to write a page
  showing fewer `DEFER`/`FAIL` rows than the artifacts contain;
- every `skip` is listed with its detail;
- any 12-digit run is rewritten to `123456789012`, and currency symbols are rejected;
- carried caveats (from `CARRIED_CAVEATS`) appear as a table;
- each class's `provenance.generated_at` is compared with a freshness reference. The unqualified
  "Every resource verified against the live deployment" claim appears only when no class is older and
  none is undateable; otherwise an "Evidence age" table names the stale classes.

To measure freshness against the deployment's real change time rather than the newest evidence, call
the consolidator directly (`verify_deployment.sh` does not pass this flag):

```bash
CHANGED_AT=$(aws lambda list-functions --region <region> --no-cli-pager \
  --query 'max(Functions[?starts_with(FunctionName, `crm-agent-eval`)].LastModified)' --output text)
.venv/bin/python3 scripts/consolidate_inventory.py --profile default --region <region> \
  --table-prefix crm-agent-eval --outputs results/tf-outputs.json \
  --inventory-dir results/verification-inventory \
  --out results/verification-inventory.md --json-out results/verification-inventory.json \
  --older-than 0h --deployment-changed-at "$CHANGED_AT"
```

`--no-residue` consolidates offline with no AWS call. The exit code is driven by `fail` alone.

## Residue cleanup

Smoke and eval runs use disposable `eval-<run_id>` tenants (smoke run ids start `smoke-`). Each run
tears its tenant down in a `finally`; an interrupted run can leave one behind.
`scripts/reap_eval_tenants.py` finds runs older than a threshold whose `eval_runs` row is not
`torn_down`, deletes their tenant rows, and then marks the row `torn_down`, so reaping is idempotent:

```bash
# Report only (the default). Run this first.
PATH=.venv/bin:$PATH python3 scripts/reap_eval_tenants.py --older-than 0h --dry-run \
  --table-prefix crm-agent-eval --profile default --region <region>

# Delete. --outputs is required with --yes; the account and region are checked against it.
PATH=.venv/bin:$PATH python3 scripts/reap_eval_tenants.py --older-than 0h --yes \
  --table-prefix crm-agent-eval --outputs results/tf-outputs.json \
  --profile default --region <region>
```

`--older-than` takes `30m`, `24h`, `2d`. `--table-prefix` falls back to `CRM_TABLE_PREFIX`.
`eval.tenancy` refuses the `demo` tenant structurally.

Residue the reaper cannot see:

| Residue | Check |
|---|---|
| A Cognito user from an interrupted smoke run | `aws cognito-idp list-users --user-pool-id <id> --profile <p> --region <r> --no-cli-pager`; remove with `admin-delete-user`. Do not lower MFA to clean up |
| A `crm-agent-eval-eniprobe-<run>` function from an interrupted `eni_function_code_deny` run | `aws lambda list-functions --profile <p> --region <r> --no-cli-pager --query "Functions[?starts_with(FunctionName, 'crm-agent-eval-eniprobe-')].FunctionName"`. The next run of that module deletes any carrying the `smoke-run` tag; otherwise `aws lambda delete-function` |
| A `selfsignup-smoke-<hex>@example.invalid` user (when `self_signup_user_deleted` failed) | `aws cognito-idp list-users --user-pool-id <id> --filter 'email ^= "selfsignup-smoke-"' --profile <p> --region <r> --no-cli-pager` |
| `results/verify_deployment_stderr.*` scratch files | local only: `rm -f results/verify_deployment_stderr.*` |

To check a single run's catalog row:

```bash
aws dynamodb get-item --profile default --region <region> --no-cli-pager \
  --table-name crm-agent-evaleval_runs --key '{"run_id":{"S":"<run_id>"}}'
```

## Updating deployed action Lambdas

`deploy.sh` runs the parity gate after every apply. To check by hand (read-only):

```bash
.venv/bin/python3 -m scripts.smoke.deployed_parity --deploy-gate --profile default --region <region>

# Target counts, derived from the toolspec:
.venv/bin/python3 -c "
from pathlib import Path
import scripts.smoke.deployed_parity as dp
d = Path('infra/build/lambda')
print(len(dp.action_targets(d)), 'action +', len(dp.non_tool_targets(d)), 'non-tool')"
```

To bring the action Lambdas current without rebuilding every zip (a rebuild changes every hash and
cascades through `agent-source.zip` into a Runtime image rebuild), apply only the indexed function
addresses. A resource-level `-target` without an instance key matches zero `for_each` instances and
still exits 0, so enumerate them:

```bash
cd infra
terraform init -input=false -backend-config=backend.hcl
terraform state list | grep -E '^module\.lambda_action\[".*"\]\.aws_lambda_function\.this$' > /tmp/addrs.txt

TARGETS=(); while IFS= read -r a; do TARGETS+=(-target="$a"); done < /tmp/addrs.txt
terraform plan -input=false -out=/tmp/parity.tfplan \
  -var aws_profile=default -var aws_region=<region> "${TARGETS[@]}"
# Inspect the plan. Proceed only if every change is an in-place update of those functions.

terraform apply -input=false -auto-approve /tmp/parity.tfplan
```

Then re-run the parity check and expect exit 0. Export `AWS_PROFILE`/`AWS_REGION` first, since a
hand-run `terraform` authenticates its backend from the environment.

**The relay is not covered by the parity gate.** If `src/relay` changed, rebuild with
`scripts/package_relay.sh`, apply `-target=module.agent_relay` (a module-level target, unaffected by
the `for_each` issue), and compare digests yourself:

```bash
aws lambda get-function-configuration --function-name crm-agent-eval-agent-relay \
  --profile default --region <region> --no-cli-pager --query 'CodeSha256' --output text
openssl dgst -sha256 -binary infra/build/lambda/relay.zip | base64
```

Changes under `src/agents/` reach the Runtime only through a container rebuild: run
`scripts/package_lambdas.sh` and apply `module.agentcore`.

**The Runtime image and `eval_trigger.zip` deploy in the same apply, with no sweep running**.
They share the serialised-turn schema, so a skew between them fails every
eval turn: a stale Runtime against a new `sweep_task` as `turn.schema must be 2, got 1`, and a new
Runtime against a stale `sweep_task` as `turn.schema must be 1, got 2`.

Never run two Terraform commands concurrently (they contend on the state lock), and never destroy to
resolve a parity failure.

## AgentCore Runtime platform version

`module.agentcore` sets `platform_version` on the Runtime from `var.platform_version`. The default is
`"V2"`, and only `"V1"` and `"V2"` are accepted. V2 restores each instance from a prepared snapshot
([AgentCore platform versions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html#runtime-platform-versions)).

- **Creates and updates take minutes.** Every image change updates the Runtime. `terraform apply`
  returns only after the provider has observed `READY` twice in a row, within the module's 30m
  `timeouts`. `runtime_network_posture`'s `runtime_ready` row then polls for up to 900 s until the
  `DEFAULT` endpoint serves the runtime's current version. It fails at once on any status other than
  `READY`, `CREATING` or `UPDATING`, an absent status included. In a full `verify_deployment.sh` run,
  Layers 5 and 6, which run turns on the Runtime, follow it. Within Layer 4, only
  `runtime_inbound_auth`, which expects a refusal, invokes the Runtime before it.
- **The environment variables have a smaller total size limit**: 2.5 KB for a container agent under V2,
  against 4 KB under V1 (same AWS page). An over-limit create or update fails with `ValidationException`.
- **Switching is an in-place update.** Before applying a switch, run a read-only plan and confirm
  that the Runtime shows `~ update in-place` with `agent_runtime_arn` unchanged:

  ```bash
  AWS_PROFILE=default AWS_REGION=<region> terraform -chdir=infra plan -input=false \
    -var aws_profile=default -var aws_region=<region>
  ```

  A `-/+` replacement is a stop. A new ARN breaks every resource policy and grant that names the
  current one.
- **Verify after the deploy:**

  ```bash
  bash scripts/verify_deployment.sh --profile default --region <region> \
    --i-understand-this-is-billable --only runtime_network_posture
  ```

  `runtime_ready`, `platform_version_expected` and `runtime_arn_unchanged` must pass.
  `smoke_text_delta_stream`'s concurrent-session rows run in Layer 6. The read-only equivalent is:

  ```bash
  aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <runtime-id> \
    --profile default --region <region> --no-cli-pager \
    --query '[platformVersion, status, agentRuntimeArn]' --output text
  ```

  An AWS CLI whose bundled botocore does not model `platformVersion` prints `None` in its place.
  The pinned Python botocore 1.43.115 models it, so the smoke row reads it from the parsed
  response; its raw-body fallback applies only under an older botocore.

**Rollback.** Set `platform_version = "V1"` on the `module "agentcore"` call in `infra/main.tf`,
then run `scripts/deploy.sh`. This is the same in-place update, so take the same read-only plan first.
`runtime_network_posture`'s `platform_version_expected` row then fails, because it asserts `V2`.
That failure is expected until the rollback is reverted.

## Teardown

```bash
# Interactive: Terraform's destroy confirmation is kept.
bash scripts/teardown.sh --profile default --region <region>

# Non-interactive.
bash scripts/teardown.sh --profile default --region <region> --auto-approve
```

| Flag | Effect |
|---|---|
| `--profile`, `--region` | target; same resolution order as `deploy.sh` |
| `--template PATH` | AgentCore config template, optional as for `deploy.sh`; when present, destroy renders the same values the apply used |
| `--auto-approve` (or `TEARDOWN_AUTO_APPROVE=true`) | skip the confirmation. Without it and with no TTY, the script exits 1 up front |

A full teardown runs in this order. Each step is detailed below.

1. Reap smoke and eval tenants (see "Residue cleanup").
2. Delete any `crm-agent-eval-eniprobe-*` function.
3. Run `scripts/teardown.sh`. If it exits 1 because the AgentCore Runtime's ENIs still hold its
   security group, wait and re-run it.
4. Run the residue scan. Expect exit 1, with only the state backend as `RESIDUE`, plus the
   `crm-agent-eval-apigw-cloudwatch-logs` role when the account bootstrap root is applied. The KMS
   keys pending deletion and the adopted network are `ACCEPTED`.
5. If the network and the API Gateway role came from the account bootstrap root and are no longer
   needed, run `scripts/bootstrap_account.sh --destroy` (see "Deploying into a new account"). It
   refuses while the stack's state still lists resources.
6. If no redeploy is planned, run `scripts/remove_state_backend.py`. It refuses while the stack's or
   the bootstrap root's state lists resources.
7. Run the residue scan again and expect exit 0.

Before destroying, check for `crm-agent-eval-eniprobe-*` functions left by an interrupted smoke run.
They are not in state and can hold the Lambda security group's ENIs, which blocks destroy:

```bash
aws lambda list-functions --profile <p> --region <r> --no-cli-pager \
  --query "Functions[?starts_with(FunctionName, 'crm-agent-eval-eniprobe-')].FunctionName"
```

Delete any listed with `aws lambda delete-function --function-name <name> --profile <p> --region <r>
--no-cli-pager`.

The AgentCore Runtime's ENIs (`InterfaceType` `agentic_ai`) are created and owned by AgentCore, and
they can outlive the Runtime. AWS documents that they may persist for up to 8 hours after the agent
is deleted ([AgentCore VPC guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html)).
While they exist, `module.vpc.aws_security_group.agent_runtime` cannot be deleted
(`DependencyViolation`). If destroy fails with only that group left in state and such ENIs still
reference it, the script polls them for up to `TEARDOWN_ENI_WAIT_SECONDS` (default 300; poll
interval `TEARDOWN_ENI_POLL_SECONDS`, default 15). If they are released in that time, it re-runs
`terraform destroy` once. Otherwise it exits 1, naming the group and the ENI count. There is no
manual step: the account cannot detach or delete these ENIs (`detach-network-interface --force` on
their `ela-attach-*` attachments returns `OperationNotPermitted`). The only remedy is to wait for
AgentCore to release them, then re-run `scripts/teardown.sh`; destroy is re-runnable, and everything
else is already gone. Other destroy failures exit 1 at once, with no wait.

Check whether the ENIs are gone:

```bash
aws ec2 describe-network-interfaces --profile <p> --region <r> --no-cli-pager \
  --filters Name=group-id,Values=<sg-id> \
  --query "length(NetworkInterfaces[?InterfaceType=='agentic_ai'])"
```

Re-running teardown once they are released is the normal path. If the state backend has to be
removed first, take that one group out of state and delete it by hand later:

```bash
AWS_PROFILE=<p> AWS_REGION=<r> terraform -chdir=infra state rm module.vpc.aws_security_group.agent_runtime
AWS_PROFILE=<p> AWS_REGION=<r> terraform -chdir=infra state list   # now prints nothing

# Later, once the ENIs are released. Look the id up fresh by name.
aws ec2 describe-security-groups --profile <p> --region <r> --no-cli-pager \
  --filters Name=group-name,Values=crm-agent-eval-agent-runtime --query 'SecurityGroups[].GroupId' --output text
aws ec2 delete-security-group --group-id <sg-id> --profile <p> --region <r> --no-cli-pager
```

After the `state rm`, nothing tracks the group: once the backend is removed, `teardown.sh` can no
longer delete it. Until it is deleted, the residue scan exits 1. It lists the group, its ENIs, and
its rules, which arrive through the tagging index as `tagged-unscanned`.

Refusals, all before anything is destroyed:

| Refusal | Remedy |
|---|---|
| `AWS_ACCESS_KEY_ID`/`AWS_SESSION_TOKEN` set | unset them; use a named profile |
| `aws`, `terraform` or `python3` missing, or `sts get-caller-identity` fails | fix the environment |
| a leftover `.bedrock_agentcore.yaml.orig` (only when a template was rendered) | restore the template, as for `deploy.sh` |
| `terraform state list` fails for a reason other than "no state" | investigate; the guard did not run |
| `REFUSING TO DESTROY` naming addresses in state | adopted-VPC resources are in state: any `aws_vpc`, `aws_subnet`, `aws_internet_gateway`, `aws_egress_only_internet_gateway`, `aws_route_table`, `aws_route_table_association`, `aws_default_route_table`, `aws_default_security_group` or `aws_default_network_acl`. Destroying them would delete shared network. Confirm the real resource must survive, `terraform state rm <address>` each one, and re-run |
| `terraform output api_origin_alb_arn` fails, the ARN is outside the resolved region/account, or disabling ALB deletion protection fails | fix the output or the permission (`elasticloadbalancing:ModifyLoadBalancerAttributes`) and re-run |

The stack does own its `aws_vpc_endpoint` and security-group resources; those are not guarded.

The api_origin ALB has deletion protection on. After the guards, and only when
`module.api_origin.aws_lb.this` is in state, the script reads `terraform output -raw
api_origin_alb_arn`, refuses unless it is an application load balancer ARN in the resolved region
and account, and runs `aws elbv2 modify-load-balancer-attributes --attributes
Key=deletion_protection.enabled,Value=false` on it. Any failure there exits 1 before `terraform
destroy` runs. After destroy, whatever its exit code, it reads `aws elbv2 describe-load-balancers` on that ARN.
If the ALB still exists or cannot be confirmed gone (a failed destroy, a declined confirmation, which exits 1),
it re-enables deletion protection on it and exits 1; if re-enabling fails, the error prints the command to run.

Destroy removes every resource in state, including the in-state S3 buckets (CDN origin, CDN logs,
CloudTrail and its logs, eval artefacts; all `force_destroy`) and the ECR repository
(`force_delete`). Tenant rows and Cognito users go with their table and user pool. After every
teardown, work through the residue list the script prints:

| Residue | Why destroy leaves it | Check |
|---|---|---|
| The state backend: the `crm-agent-eval-tfstate-*` and `crm-agent-eval-tfstate-logs-*` buckets, the `crm-agent-eval-tflock` table, and the `alias/crm-agent-eval-tfstate` KMS key and alias | created by `bootstrap_state_backend.sh`, outside the stack, and needed by any redeploy | see "Removing the state backend" |
| KMS customer-managed keys pending deletion | each stack key is scheduled, not deleted: a 30-day window, 7 days for the eval-artefacts key. A key pending deletion is not billed and cannot be used; it can be cancelled until the window ends | `aws kms describe-key --key-id <id>`; expect `KeyState` `PendingDeletion` |
| The `crm-agent-eval-bootstrap` security group | the placeholder from "First deployment: two applies", created outside Terraform | `aws ec2 describe-security-groups --filters Name=group-name,Values=crm-agent-eval-bootstrap`; remove with `aws ec2 delete-security-group --group-id <id>` |
| `CloudFront-VPCOrigins-Service-SG` | AWS creates and manages it for the VPC origin; it is never in state. AWS deletes it itself once no VPC origin uses it: in a measured teardown it was gone about 12 minutes after the VPC origin was destroyed, with no action taken | `aws ec2 describe-security-groups --filters Name=vpc-id,Values=<vpc-id> Name=group-name,Values=CloudFront-VPCOrigins-Service-SG`. Expect no result; there is nothing to delete. If it is still there, do not delete it: the adopted VPC is shared, so another workload's VPC origin may use it. The residue scan accepts it |
| A `crm-agent-eval-eniprobe-<run>` function | not in state. It shares the capabilities function's subnets and security groups, so it can hold their Hyperplane ENI and block deleting the Lambda security group. Check it **before** destroy (see above) | `aws lambda list-functions`, then `aws lambda delete-function --function-name <name>` |
| Untracked CloudWatch log groups | created by a service outside state, before the module-managed group existed, or by console activity | `aws logs describe-log-groups` |

Add `--profile <p> --region <r> --no-cli-pager` to each. Then run the residue scan (below); exit 0
means the account holds no project residue.

The state backend outlives the stack by design; a later `deploy.sh` reuses it, provided
`infra/backend.hcl` survives or is regenerated. Remove it only when no redeploy is planned, with
`scripts/remove_state_backend.py` (see "Removing the state backend").

### Residue scan

`scripts/scan_residue.py` lists every project resource still in the account, read-only. It makes
only `list_*`, `describe_*` and `get_*` calls. A resource is the project's when its name starts with
`crm-agent-eval` or `crm_agent_eval`, or it carries the tag `service=crm-agent-eval`.

```bash
.venv/bin/python3 scripts/scan_residue.py --profile <p> --region <r> \
  --json /tmp/crm-residue/scan.json
```

| Flag | Effect |
|---|---|
| `--profile`, `--region` | required. No env or config fallback, so an ambient profile is never scanned by mistake |
| `--name-prefix P` | repeatable; replaces the default name prefixes |
| `--tag KEY=VALUE` | replaces the default `service=crm-agent-eval` tag |
| `--families f1,f2` | scan only these families; an unknown name exits 2 |
| `--json PATH` | write the full report |

The script refuses (exit 2) when `AWS_ACCESS_KEY_ID` or `AWS_SESSION_TOKEN` is set: environment
credential variables cause a refusal, so unset them and use the named profile.

Stdout first prints `caller: <arn-type> (account resolved)`. It then prints one line per item:
`RESIDUE <family> <kind> <name>`, `ACCEPTED <family> <kind> <name> <reason>` or
`STALE <family> <kind> <name>`. Last come `measured: <families>`, one
`unmeasured: <family> <code> <operation>: <message>` line per unmeasured family, and `exit <n>`. Any
occurrence of the account id is printed as `<account>`.

Besides each family's own listing, the scan reads the tagging index
(`resourcegroupstaggingapi get-resources` with the project tag), plus us-east-1 for CloudFront. A
tagged resource that its family's listing did not return is classified as follows:
- `STALE` if the family was measured and enumerates that resource type. The index lags deletions,
  so the line is informational and does not affect the exit code.
- `RESIDUE <family> tagged-unscanned <name>` otherwise. No scanner covers that type, so the scan
  cannot rule it out.

If the tagging index itself cannot be read, it is reported as the unmeasured family `tag-index`.

| Exit | Meaning |
|---|---|
| 0 | every selected family was measured and none holds residue |
| 1 | at least one residue item, with every family measured |
| 2 | at least one family (or `tag-index`) could not be measured (access denied, throttled, no credential, endpoint unreachable). Any residue found is still listed, but the account was not fully measured, so 2 outranks 1. Also: a usage error or unknown family, the credential-variable refusal, a failed `sts get-caller-identity`, an aborted scan, or an unwritable `--json` path |
| 3 | no family ran (empty selection) |

Exactly three kinds of resource are accepted rather than reported as residue:

| Rule | Accepts | Reason |
|---|---|---|
| `kms-pending-deletion` | a KMS key in `PendingDeletion` | AWS enforces the waiting period, minimum 7 days |
| `vpc-origins-service-sg` | the security group `CloudFront-VPCOrigins-Service-SG` | AWS-managed. A VPC origin still using it is reported as `cloudfront` residue |
| `adopted-network` | a VPC, subnet, route table, internet gateway or network ACL found through the tagging index | the adopted network is shared and out of scope. NAT gateways, Elastic IPs and VPC endpoints are **not** accepted: they are billable and the project creates them |

The JSON report holds `schema_version`, `region`, `caller` (account and ARN type), `families`
(measured, and unmeasured with the error code), `residue`, `accepted`, `stale_tag_index` and
`exit_code`. It contains ARNs and the account id, so write it only under the gitignored `results/`
or under `/tmp`, never to a tracked path.

### Removing the state backend

Run this only after the stack is destroyed and `terraform state list` prints nothing. It deletes
the state bucket (every object version and delete marker), then the access-log bucket, then the
lock table. It then schedules the KMS key for deletion with a 7-day window and deletes its alias.
It is non-interactive and idempotent: a resource already absent counts as success. Bucket calls
carry the expected bucket owner, so a same-named bucket in another account is never touched.

```bash
# Confirm the stack is gone (prints nothing).
AWS_PROFILE=<p> AWS_REGION=<r> terraform -chdir=infra state list

.venv/bin/python3 scripts/remove_state_backend.py --profile <p> --region <r> --yes \
  --json /tmp/crm-residue/remove-backend.json
```

Refusals, before any precondition is read (exit 2, nothing changed):
- `--yes`, `--profile` or `--region` is missing;
- the `--json` path is not writable;
- `AWS_ACCESS_KEY_ID` or `AWS_SESSION_TOKEN` is set;
- `sts get-caller-identity` fails.

Preconditions, all read-only (exit 1, nothing changed):

| Refusal | Remedy |
|---|---|
| a `.tfstate` object in the state bucket (the default `crm-agent-eval/terraform.tfstate`, the bootstrap root's `crm-agent-eval/bootstrap-account/terraform.tfstate`, or any `env:/` workspace state) still lists resources, or is not valid state JSON | the stack or the bootstrap root is not destroyed; run `scripts/teardown.sh` (for each workspace), then `scripts/bootstrap_account.sh --destroy`, first |
| the state bucket holds an object that is not a `.tfstate` file, or is non-empty but has no `crm-agent-eval/terraform.tfstate` | the bucket holds something this script does not expect; inspect it before removing anything |
| the lock table holds an item other than a `-md5` digest, or a malformed item | a live or stale lock; confirm no Terraform run is active, then `terraform force-unlock <LOCK_ID>` |
| the key behind `alias/crm-agent-eval-tfstate` is not customer-managed or lacks `service=crm-agent-eval` | the alias points at a key this project did not create; investigate before deleting anything |

The key is found through `alias/crm-agent-eval-tfstate`. If a re-run finds that key already
pending deletion (an earlier run stopped after scheduling it), it reports the key as pending
deletion and deletes the alias. Only when the alias itself is gone does the script search for the
key by its `service=crm-agent-eval` tag and bootstrap description, skipping keys already pending
deletion. One match is scheduled; no match counts as absent. Several matches are a precondition
refusal: exit 1, with nothing changed.

Both buckets are emptied and deleted with the same procedure, the access-log bucket after the state
bucket. Late S3 access-log delivery can refill a bucket, so `BucketNotEmpty` is retried: up to 5
attempts, 30 s apart (about 2 minutes). If the last attempt still fails, the script exits 1; re-run
it later. Exit 0 means every resource is absent or pending deletion. A later deploy then needs
`infra/bootstrap_state_backend.sh` again.

## Common failure modes

| Symptom | Cause and fix |
|---|---|
| `deploy.sh` refuses "the relay zip is stale, absent or unreadable" | `relay.zip` is missing or built from an older `src/relay`. Run `scripts/package_lambdas.sh` then `scripts/package_relay.sh`, and re-run the deploy |
| `deploy.sh` refuses "Lambda zips are stale, incomplete or missing" | run `scripts/package_lambdas.sh`, then `scripts/package_relay.sh` |
| Right after an apply, reads through `get_opportunity` miss notes or activities, or a query on `gsi_opportunity` fails | the apply changed the index projection, so DynamoDB is re-creating the index. Wait for `ACTIVE` ([GSI projection changes](#gsi-projection-changes)) |
| `deploy.sh` refuses `WRONG ACCOUNT` | the resolved profile is not the state bucket's account. Usually an ambient `AWS_PROFILE`; pass `--profile` |
| `terraform init` fails `AccessDenied: 403` on the state bucket when run by hand | the backend authenticates from the environment. Export the intended `AWS_PROFILE`/`AWS_REGION`, or use `deploy.sh` |
| `deploy.sh` exits naming unmerged paths, before any AWS call | resolve the paths and `git add` them. An exported `GIT_DIR`, `GIT_WORK_TREE` or `GIT_INDEX_FILE` is scrubbed by the script and does not change which index it reads |
| The deploy dies inside the preflight with `jq not on PATH` | install `jq`; `deploy.sh` does not probe for it |
| `/api/*` returns 502 or 504 after a first apply | the second apply has not installed `CloudFront-VPCOrigins-Service-SG`; see "First deployment" |
| `WARN` about pending changes after the apply | normal: packaging is not byte-deterministic and the classifier absorbed every change |
| "unexplained post-apply drift" naming `aws_s3_object.agent_source`, the CodeBuild project or the AgentCore Runtime | the classifier absorbs the `agent-source.zip` cascade only as `update`s, and absorbs a Runtime `update` only when `aws_s3_object.agent_source` or `null_resource.image_build` is pending in the same plan. A `create`, `delete` or `replace` there, or a Runtime change on its own, is a real change; inspect the plan |
| `terraform plan` shows Lambda updates on unchanged source | packaging non-determinism; expected after any `package_lambdas.sh` run |
| A `-target`ed plan reports `No changes` though the zip differs | the target names a resource inside a `for_each` module without an instance key and matched nothing. Use indexed addresses; see "Updating deployed action Lambdas" |
| The deployed Lambdas run older code with every offline gate green | run `scripts.smoke.deployed_parity`; it prints the deployed and local digest of each mismatch |
| The CloudFront URL returns XML `AccessDenied` or an empty response | the origin bucket is empty: the apply ran with `--skip-frontend` or the publish was blocked. Re-run `deploy.sh` |
| Sign-in succeeds but every list shows a `scope_denied` `account:*` banner | no `reps` item for the user's Cognito username, so the trigger applied empty-book defaults, and the listing tools deny an empty book. Usually the item was keyed on the email; re-run `scripts/provision_demo_user.py` |
| A real token carries no `rep_id`/`book`/`full_visibility` claims | check the user's `reps` row and the flow used; `cognito_claims` asserts the trigger stamps all four |
| `cognito_claims` `admin_create_user_only` fails | the pool accepts self-registration. Confirm `admin_create_user_config { allow_admin_create_user_only = true }` in `infra/modules/cognito/main.tf` and re-apply; if source has it, the pool drifted |
| `cognito_claims` `self_signup_refused` fails | `ACCEPTED (UserSub=...)` in the detail is an exposure; fix `admin_create_user_only` first. Any error code other than `NotAuthorizedException` (for example `ResourceNotFoundException`) usually means the client id or region does not match the pool |
| `cognito_claims` `self_signup_user_deleted` fails | a self-registered `selfsignup-smoke-*` user may remain; find and delete it (see "Residue cleanup") before re-running |
| `cloudfront` reports TLS minimum as `defer` | `minimum_protocol_version` has no effect on the default `cloudfront.net` certificate; TLS 1.2 is not enforced without a custom ACM certificate |
| `/agent/*` returns 401 | the relay rejected the Cognito token: it expired, or it was issued for a client the relay does not allow |
| Gateway `tools/call` requests return JSON-RPC error `-32001`, logged as `gateway_auth_denied` with reason `PyJWKClientError`, for up to 30 s after a Cognito signing-key rotation | the Gateway interceptor (`src/gateway/identity_interceptor.py`, PyJWT 2.15 `PyJWKClient`) re-fetches the JWKS on an unknown `kid` at most once per 30 s cooldown, measured from its last fetch. Retry after the cooldown; a miss that persists means the `kid` is not in the pool's JWKS |
| A direct `InvokeAgentRuntime` call fails with `AccessDeniedException` | the Runtime uses IAM (SigV4) inbound auth. Its resource policies allow only the relay's, `sweep_task`'s and `chat_target`'s execution roles and explicitly deny the data-plane actions to every other principal, including same-account admins; call it through the relay |
| `pip install -e .` fails with `requires a different Python` | the venv was built with a host `python3` that is not the `.python-version` pin; rebuild it as in "Prerequisites" |
| `bootstrap_state_backend.sh` refuses `CRM_EXPECTED_ACCOUNT_ID ... is required` or `WRONG ACCOUNT` | set `CRM_EXPECTED_ACCOUNT_ID` (or `--expected-account-id`) to the account the backend belongs in, and pass the profile that resolves to it |
| `bootstrap_state_backend.sh` refuses "The operator list ... is required", "The caller is not in the operator list" or an operator entry | set `CRM_OPERATOR_PRINCIPAL_ARNS` (or `--operator-principal-arn`) to the role ARNs in `crm_operator_principal_arns`, role ARNs rather than session ARNs, and run as one of them |
| `terraform init` or `plan` fails `AccessDenied` with "explicit deny in a resource-based policy" on the state bucket, lock table or state key | the caller is not in the state backend's operator list; run as a listed operator, or re-run the bootstrap as one with the list corrected |
| A Lambda fails at cold start with `KMSAccessDeniedException`, or a log group stops receiving events | its execution role is missing from the logs key's allowlist (`local.logs_key_user_role_arns`); see "Store principal boundary" |
| A `session_profile.sh exec` command exits 4 | the session files are absent, the expiry is unreadable, or fewer than 15 minutes are left; open a fresh vault shell and run `write` again |
| `issued access token names tenant ...` | the pre-token-generation trigger's cold start returned the fail-closed default tenant; the script retries in the next TOTP window |
| `smoke_agent_stream` `tool_calling_turn_elicited` fails | none of the candidate prompts' turns called a tool. Raise `_MAX_PROMPT_CANDIDATES` (each extra candidate is one more billable turn); do not relax the timing thresholds |
| `gateway_tools` and `integration_gateway_scope` fail everywhere with `authentication_failed` | the identity interceptor has no `GATEWAY_SERVICE_PRINCIPAL_REP_ID`, so no service principal exists. These modules seed the principal's `reps` row themselves and restore any prior row on cleanup |
| `expired_token_genuinely_expired` fails, "still valid" | the MCP client's `access_token_validity` is above 5 minutes; tighten it rather than forging a token |
| `out_of_book_target_denied_naming_scope` fails with `not_found` | the out-of-book account was not seeded, so scope was never evaluated |
| `iam_roles` shows an over-broad grant every offline gate missed | expected class: only a live IAM evaluation sees it; fix the policy |
| `verify_deployment.sh` exits 2 immediately | missing `--i-understand-this-is-billable`/`--dry-run`, a bad `--layer`, an unknown or out-of-layer `--only` module, or no `.venv/bin/python3` |
| `WrongTargetError`, "Nothing has been checked" | the resolved region or account differs from `results/tf-outputs.json`; pass `--profile`/`--region` |
| A reader refuses `results/tf-outputs.json` as absent or stale | re-run `deploy.sh`, which rewrites and stamps it; a stale snapshot is detected by its recorded commit |
| `make verify-gates` fails with `.venv/bin/python3: No such file or directory` | the project virtualenv is missing; create it as in "Prerequisites" |

## Data retention

A live eval sweep that includes the OpenAI models needs the account's Bedrock data-retention setting
to permit them. Read it, read-only:

```bash
aws bedrock get-account-data-retention --region <region> --no-cli-pager
```

The setting applies to the whole account, so changing it is the account owner's decision. Nothing in
this repository changes it.

## Deploying into a new account

The sequence below brings the stack up in an account that has none of it: no state backend, no VPC
for the stack to adopt, no API Gateway logging role. Run the steps in this order. Every step passes `--profile` and `--region` explicitly. The names `crm-target` (profile),
`<vault-profile>` and `$HOME/.local/state/crm-eval/target` (session directory) are examples. For
`<region>`, use any region listed in `infra/regions.json`, the same one in every step. `.venv/bin/python3 scripts/region_map.py regions` lists them. To add a region, see README
"Deploying to another region".

### Prerequisites to read first

All read-only. Record each result before the first write.

| Prerequisite | Check | What it must show |
|---|---|---|
| Bedrock model access | `aws bedrock get-inference-profile --inference-profile-identifier <inference_profile>` and `aws bedrock get-foundation-model-availability --model-id <model key>`, for each model key and its `inference_profile` in `config/model_capabilities.yaml` | `ACTIVE`; `AUTHORIZED` and `AVAILABLE` |
| Data retention | `aws bedrock get-account-data-retention` | a mode that permits the OpenAI models (see "Data retention"). `inherit` is expected to permit both families |
| AgentCore | `aws bedrock-agentcore-control list-agent-runtimes` and `list-gateways` | the calls succeed. A runtime owned by another workload does not conflict |
| Lambda concurrency | `aws lambda get-account-settings` | `UnreservedConcurrentExecutions` at least the sum of the stack's `reserved_concurrent_executions` (read it from a `terraform plan`) plus the 100 Lambda keeps unreserved |
| CloudTrail | `aws cloudtrail describe-trails --include-shadow-trails` | room for one more trail in the region (the stack creates one; the limit is 5) |
| VPC, NAT, EIP quotas | `aws service-quotas list-service-quotas --service-code vpc` and `--service-code ec2` | room for one VPC (the bootstrap root), one NAT gateway and one Elastic IP (the stack) |
| AZ ids | `aws ec2 describe-availability-zones --query 'AvailabilityZones[].[ZoneName,ZoneId]'` | the AZ ids the bootstrap root will use exist: the region's `bootstrap` ids in `infra/regions.json` (`region_map.py az-ids --region <r> --field private`, `--field runtime`, `--field nat`); any id in its `agentcore_az_ids` is also allowed as an override. AZ names map to ids differently in every account; the root places subnets by id |
| API Gateway logging | `aws apigateway get-account` | no `cloudwatchRoleArn`, or one that names a working role (then use `--no-apigw-logging`) |
| Service-linked roles | none | the ELB, CloudFront VPC-origin and AgentCore network roles are created on first use; expect the first apply to need no re-run |
| Residue | `.venv/bin/python3 scripts/scan_residue.py --profile crm-target --region <region> --json /tmp/scan.json` | exit 0: nothing of the project is in the account yet |

Add `--profile crm-target --region <region> --no-cli-pager` to each `aws` call.

### Access: an isolated session profile

When the account is reached through an `aws-vault` session, the tooling does not use that session
directly. `scripts/session_profile.sh` copies the session into an isolated profile: its own
`AWS_CONFIG_FILE` and `AWS_SHARED_CREDENTIALS_FILE` in a `0700` directory, files `0600`. `write`
refuses a directory inside any git work tree; keep it out of `~/.aws` as well. Two simpler routes fail:

- a `credential_process` profile stalls when the keychain behind `aws-vault` auto-locks, and the CLI
  and boto3 hang mid-command, inside an apply as easily as anywhere;
- stripping the vault variables from the environment leaves your normal `~/.aws` in force, where
  `--profile default`, or no profile, resolves to a different account.

Under `exec`, the only profile that exists is the one written, and `exec` sets `AWS_PROFILE` to it:
`--profile default` fails with `ProfileNotFound`. `exec` also unsets every ambient credential
variable, including the legacy `AWS_ACCESS_KEY` and `AWS_SECRET_KEY`, and sets
`AWS_EC2_METADATA_DISABLED=true`, so no command falls back to instance-metadata credentials.

```bash
# From the aws-vault shell; write reads the session from the environment.
aws-vault exec <vault-profile>
scripts/session_profile.sh write --dir "$HOME/.local/state/crm-eval/target" \
  --profile crm-target --region <region>

# Every later command runs under exec. zsh does not word-split a plain $S, so use an array.
S=(scripts/session_profile.sh exec --dir "$HOME/.local/state/crm-eval/target" \
  --profile crm-target --region <region> --)
"${S[@]}" aws sts get-caller-identity --profile crm-target --region <region> --no-cli-pager
"${S[@]}" aws sts get-caller-identity --profile default --region <region> --no-cli-pager  # must fail

scripts/session_profile.sh status --dir "$HOME/.local/state/crm-eval/target" --profile crm-target
```

`write` refuses when fewer than 60 minutes remain on the session, when the directory is inside a git
work tree, or when an existing directory is neither empty nor one `write` created. `exec` exits 4
when fewer than 15 minutes remain; run `write` again from a fresh vault shell. Run `status` before
each apply. At the end, delete the files:

```bash
scripts/session_profile.sh delete --dir "$HOME/.local/state/crm-eval/target"
```

### Order

Export the expected account once (`export CRM_EXPECTED_ACCOUNT_ID=<account-id>`; both bootstrap
scripts read it from the environment), and confirm `"${S[@]}" aws sts get-caller-identity` returns it
before each step that writes.

1. **Fresh clone and build.** Clone the commit you deploy. Create the venv with the pinned
   interpreter (see "Prerequisites"), run `npm ci` in `frontend/`, `src/relay/` and
   `tests/e2e/browser/`, then `scripts/package_lambdas.sh` and `scripts/package_relay.sh`.
2. **State backend.**
   `"${S[@]}" infra/bootstrap_state_backend.sh --profile crm-target --region <region>`, with
   `CRM_EXPECTED_ACCOUNT_ID` and `CRM_OPERATOR_PRINCIPAL_ARNS` exported (see "State backend"). Read the
   settings back. Check for the first access-log object later. The region is part of both bucket
   names (`…-<account>-<region>`) and is written into `infra/backend.hcl`, so the backend is per
   region.
3. **Account bootstrap.**
   `"${S[@]}" scripts/bootstrap_account.sh --profile crm-target --region <region>`
   (below). The region must be in `infra/regions.json`; the subnet zones are derived from its
   entry. It applies the network and the API Gateway role, writes the six `vpc_*` keys into
   `infra/terraform.tfvars`, and runs the adopted-VPC preflight on them, including the Runtime
   flags.
   Then add the operator principals to `infra/terraform.tfvars` (see "DynamoDB principal boundary"):
   ```hcl
   crm_operator_principal_arns = ["arn:aws:iam::<account-id>:role/<operator-role-name>"]
   ```
4. **Two applies.** Before each apply, record `scripts/session_profile.sh status` (at least 120
   minutes left; the script itself enforces only 15) and `"${S[@]}" aws sts get-caller-identity --query Account --output text`. Follow "First deployment: two applies", running every command under
   `"${S[@]}"` with `--profile crm-target` in place of `--profile default`. A leading `VAR=value`
   assignment is not a command, so put `env` in front of it:
   ```bash
   "${S[@]}" env PATH=.venv/bin:$PATH bash scripts/deploy.sh --profile crm-target --region <region>
   ```
   Then run "Post-deploy checks for /api/*" the same way: an unauthenticated `/api/accounts` returns
   403, and `/` returns 200.
5. **Seed and users.** The commands of "Bringing up the UI", under `"${S[@]}"`, reading the outputs
   document `deploy.sh` wrote. Source the passwords from their `0600` file first:
   ```bash
   "${S[@]}" env PATH=.venv/bin:$PATH python3 scripts/seed_demo_tenant.py \
     --table-prefix crm-agent-eval --outputs results/tf-outputs.json \
     --profile crm-target --region <region>
   "${S[@]}" env PATH=.venv/bin:$PATH python3 scripts/provision_demo_user.py \
     --outputs-json results/tf-outputs.json --email "$CRM_DEMO_USER_EMAIL" \
     --profile crm-target --region <region> --skip-seed
   # The admin user: override the demo user's variables for this one command.
   "${S[@]}" env PATH=.venv/bin:$PATH CRM_DEMO_USER_EMAIL="$CRM_E2E_ADMIN_EMAIL" \
     CRM_DEMO_USER_PASSWORD="$CRM_E2E_ADMIN_PASSWORD" python3 scripts/provision_demo_user.py \
     --outputs-json results/tf-outputs.json --email "$CRM_E2E_ADMIN_EMAIL" \
     --profile crm-target --region <region> --skip-seed --admin
   "${S[@]}" env PATH=.venv/bin:$PATH python3 scripts/provision_gateway_principal.py \
     --outputs-json results/tf-outputs.json --profile crm-target --region <region>
   ```
   Expect `demo` with 41 accounts and 300 opportunities, and an empty `canary`.
6. **Verify.** Run the whole of Layers 4-6, not a `--only` run:
   ```bash
   "${S[@]}" make verify-deploy \
     VERIFY_DEPLOY_ARGS="--i-understand-this-is-billable --profile crm-target --region <region>"
   ```
   Expect `fail 0`. Every `defer` must carry a `defer_reason` that "Verification layers 4-6"
   describes, or be one of the co-tenant defers below.
7. **T4.** `scripts/run_browser_journeys_container.sh` (see "T4 browser journeys in the Playwright
   container").
8. **Close.** `scripts/session_profile.sh delete --dir "$HOME/.local/state/crm-eval/target"`.

### `scripts/bootstrap_account.sh`

It applies the separate Terraform root `infra/bootstrap_account/` (resources and inputs in
[`infra/bootstrap_account/README.md`](../infra/bootstrap_account/README.md)):

- a VPC with DNS support and hostnames;
- one IGW-routed `/24` for the stack's NAT gateway;
- two private `/24`s, each with its own route table and no routes;
- two Runtime `/24`s on one route table with no routes;
- the VPC's default security group, emptied of every rule;
- the `crm-agent-eval-apigw-cloudwatch-logs` role, and the account's `cloudwatchRoleArn` setting
  pointing at it.

Subnets are placed by AZ id. It creates no NAT gateway, Elastic IP, VPC endpoint or flow log; the
stack does that inside the network.

```bash
"${S[@]}" scripts/bootstrap_account.sh --profile crm-target --region <region> [--no-apigw-logging]
"${S[@]}" scripts/bootstrap_account.sh --profile crm-target --region <region> --destroy
```

- It refuses (exit 2, before any Terraform call) on static credentials in the environment, a
  missing `--profile` or `--region`, a missing or mismatched `CRM_EXPECTED_ACCOUNT_ID`, or no
  `infra/backend.hcl`. It also refuses a region that `scripts/region_map.py az-ids` does not accept,
  before any AWS call. Exit codes are 0 for a supported region, 2 for a region not US or not in
  `infra/regions.json` (the message lists the supported regions), and 1 for an invalid or unreadable
  map. The CLI's own usage error is 64.
- The zones come from the region's `bootstrap` entry in `infra/regions.json`. A zone override
  outside the region's `agentcore_az_ids` fails a precondition on `aws_vpc.this`.
- Its state lives in the same backend, under `crm-agent-eval/bootstrap-account/terraform.tfstate`.
- After it, the residue scan exits 1. The state backend (both buckets, the lock table, the KMS key
  and alias) and the API Gateway role are project residue. The VPC, subnets, route tables and IGW are
  accepted adopted network. The emptied default security group is not reported.
- `--destroy` is the last step before removing the backend: first `scripts/teardown.sh`, then
  `--destroy`, then `scripts/remove_state_backend.py`.
- `--destroy` reads the stack's state object, bucket and key from `infra/backend.hcl`. It refuses
  while that object lists any resource, or while it is absent but still has object versions or
  delete markers. It also refuses while the stack uses a non-default Terraform workspace
  (`TF_WORKSPACE`, or `infra/.terraform/environment`).
- Destroying the root resets the account's API Gateway `cloudwatchRoleArn`.

### Shared (co-tenant) accounts

An account that already runs other workloads needs no change to the stack, but three behaviours
differ from a dedicated account:

- **Runtime invocation.** Other principals in the account may hold broad identity policies, for
  example PowerUser. The Runtime's resource policies carry an explicit Deny on the data-plane actions
  for every principal except the three invoker roles, so such a principal gets
  `AccessDeniedException` (see [`infra/modules/agent_relay/README.md`](../infra/modules/agent_relay/README.md)).
- **Operator-side Runtime invocation.** The operator running `verify-deploy` is not an invoker. The
  only smoke that calls `InvokeAgentRuntime` as the operator is `runtime_inbound_auth`, which
  expects the refusal; `outside_vpc_invoke_refused_under_declared_deny` defers when the declared
  `aws:SourceVpce` Deny state cannot explain it. The chat paths through the relay, the MCP chat
  gateway and the sweep cover the positive case.
- **Data-plane isolation.** The eleven CRM tables and their KMS key deny every principal outside
  the project's Lambda roles and `crm_operator_principal_arns` (see "DynamoDB principal boundary").
  For the stack's buckets and its other keys, isolation from other principals in the account still
  rests on those principals' own IAM policies. Use an account whose other workloads hold no broad
  data-plane grants, or accept that the data in those buckets is readable by them.
- **Who passes the table boundary.** Anyone who can assume an operator role, or who can change the
  code or trust policy of a project Lambda role, acts as an allowed principal. A shared operator role
  widens the boundary to everyone who can assume it.

## DynamoDB principal boundary

Every CRM table carries a resource policy, and the DynamoDB KMS key a key policy, that deny:

- the data plane (item reads and writes, PartiQL, backup, export, restore, Kinesis streaming) and
  key use (`kms:Decrypt`, `Encrypt`, `GenerateDataKey*`, `ReEncrypt*`, `CreateGrant`) to every
  principal except the project Lambda roles and the operators. DynamoDB's own use of the key, as a
  service principal, is not denied;
- table and key administration (`DeleteTable`, `UpdateTable`, backups and TTL settings, resource
  policy changes, tags, contributor insights; `PutKeyPolicy`, `ScheduleKeyDeletion`, `DisableKey`,
  `DisableKeyRotation`, `RevokeGrant`) to every principal except the operators and the account
  root.

`Describe*`, `List*` and tag reads stay open.

### `crm_operator_principal_arns`

The IAM role or user ARNs that deploy, seed, smoke-test and tear down the stack. It has no default;
set it in the gitignored `infra/terraform.tfvars`:

```hcl
crm_operator_principal_arns = ["arn:aws:iam::<account-id>:role/<operator-role-name>"]
```

For an assumed role, list the role ARN (`arn:aws:iam::<account-id>:role/<name>`), not the session
ARN that `aws sts get-caller-identity` prints. The account root is not accepted here.

A plan run by a caller that is not in the list fails at a precondition on the policy resources,
before anything is applied, with a message naming `crm_operator_principal_arns`. The root user cannot run
Terraform or `deploy.sh` for the same reason.

When adding a project Lambda that reads the tables (`dynamodb_kms_grant_enabled = true`), add its
`role_arn` to `local.dynamodb_data_plane_role_arns` in `infra/main.tf`; otherwise the function is
denied.

**Changing operators.** Add the new ARN to the list and apply as the **old** operator, which is still
listed. Remove the old ARN in a later apply run as the new operator.

### Break-glass and rollback

The account root is the only principal besides the operators that may administer the tables and the
key. If every listed operator role is renamed, replaced or deleted, the root user is the only
recovery. Root cannot run Terraform, so recovery is the raw API calls below, run as root (or as a
still-listed operator).

**Key-side breakage** shows as a table with `TableStatus` `INACCESSIBLE_ENCRYPTION_CREDENTIALS` or
an `SSEDescription.InaccessibleEncryptionDateTime`, or as a KMS `AccessDeniedException` in a project
Lambda's logs. DynamoDB archives a table whose key stays inaccessible for 7 days, so act at once.
Put back the default key policy:

```json
{
  "Version": "2012-10-17",
  "Id": "key-default-1",
  "Statement": [
    {
      "Sid": "Enable IAM User Permissions",
      "Effect": "Allow",
      "Principal": {"AWS": "arn:aws:iam::<account-id>:root"},
      "Action": "kms:*",
      "Resource": "*"
    }
  ]
}
```

```bash
aws kms put-key-policy --key-id <dynamodb-kms-key-arn> --policy-name default \
  --policy file://default-key-policy.json --profile <profile> --region <region> --no-cli-pager
```

The key ARN is each table's `SSEDescription.KMSMasterKeyArn` (`aws dynamodb describe-table`).

**Table-side breakage** (a project Lambda or an operator refused with "explicit deny in a
resource-based policy"): remove the table's policy.

```bash
aws dynamodb delete-resource-policy --resource-arn <table-arn> \
  --profile <profile> --region <region> --no-cli-pager
```

The next apply of the same commit puts both policies back. To keep them off, deploy the previous
commit. `smoke/dynamodb_principal_deny.py` then reports the boundary as absent.

## Store principal boundary

The same pattern as "DynamoDB principal boundary" covers the remaining data stores. Each store keeps
its existing statements (the account-root Allow on keys, the TLS Deny and the service Allows on
buckets) and adds Deny statements keyed on `aws:PrincipalArn`:

| Store | Defined in | Use or data plane allowed to | Service use not denied |
|---|---|---|---|
| state key, state bucket, state access-log bucket, lock table | `infra/bootstrap_state_backend.sh` (outside the stack) | the operators | DynamoDB's use of the key for the lock table; S3 log delivery into the log bucket |
| eval-artefacts key and bucket | `infra/main.tf` (`aws_kms_key_policy.eval_artifacts`), `infra/modules/eval_artifacts` | the `eval_trigger`, `sweep_control` and CodeBuild image-build roles, and the operators | the key's service use |
| CloudWatch Logs key | `infra/modules/observability/kms.tf` | every execution role whose Lambda environment the key encrypts (`local.logs_key_user_role_arns`), and the operators | CloudWatch Logs and ECR |
| CloudTrail key and bucket | `infra/modules/observability/cloudtrail.tf` | the operators | CloudTrail's delivery |
| frontend key and origin bucket | `infra/modules/frontend_cdn` | the operators (on the origin bucket, writes only; reads are served through CloudFront) | CloudFront's OAC decrypt |

On every store, administration is allowed only to the operators and the account root. For a key that
means `PutKeyPolicy`, `ScheduleKeyDeletion`, `DisableKey`, `RevokeGrant`, `DisableKeyRotation` and the
alias, tag and description changes. For a bucket it means `DeleteBucket` and the policy, encryption,
versioning, lifecycle, logging, public-access, ownership, ACL, tag, replication, inventory,
notification, Object Lock, request-payment, website, CORS, acceleration and analytics settings.
For the lock table it means the table administration list under "DynamoDB principal boundary".

Metadata reads (`Describe*`, `GetKeyPolicy`, `ListGrants`, bucket and key configuration reads) stay
open. Log reads stay open too: CloudWatch Logs decrypts under its own service identity, so the logs
key Deny refuses direct key use but not a log read by a principal with CloudWatch Logs read IAM. Log
redaction applies to what such a principal reads. A plan by a caller outside `crm_operator_principal_arns` fails at a precondition on each policy
resource; the bootstrap refuses such a caller before it writes anything.

**Adding a Lambda.** A module call that sets
`environment_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn` must have its role in
`local.logs_key_user_role_arns` in `infra/main.tf`, or the function fails at cold start with
`KMSAccessDeniedException`. A new `local.tools` entry needs its own
`module.lambda_action["<tool>"].role_arn` line: the local lists each tool by key, because a reference
to the whole `module.lambda_action` would make the key depend on the functions it encrypts.
`tests/unit/test_infra_root_wiring.py` fails when the list and `local.tools` differ.

### Changing operators

1. Add the new ARN to `crm_operator_principal_arns` and apply as the **old** operator, which is still
   listed.
2. Re-run `infra/bootstrap_state_backend.sh`, still as the old operator, with the new full list in
   `CRM_OPERATOR_PRINCIPAL_ARNS`. The script refuses a caller that is not in the list it is given.
3. To remove the old operator, repeat both steps as the new operator with the old ARN left out.

The two lists must match after every change; `state_operator_list_matches_stack` fails otherwise.

### Rollback

Save every current document before a change, read-only:

```bash
aws kms get-key-policy --key-id <key-arn> --policy-name default --output text --query Policy \
  --profile <p> --region <r> --no-cli-pager > key-policy.json
aws s3api get-bucket-policy --bucket <bucket> --output text --query Policy \
  --profile <p> --region <r> --no-cli-pager > bucket-policy.json
aws dynamodb get-resource-policy --resource-arn <lock-table-arn> --output text --query Policy \
  --profile <p> --region <r> --no-cli-pager > table-policy.json
```

Recovery, as an operator or as the account root:

- **Key**: `aws kms put-key-policy --key-id <key-arn> --policy-name default --policy file://key-policy.json`.
  Every key keeps the account-root `kms:*` Allow, and root is in every key's administration list.
- **Bucket**: `aws s3api delete-bucket-policy --bucket <bucket>`. The root user can always delete a
  bucket policy, even one that denies it. This also removes the bucket's TLS Deny until the policy is
  put back, so restore it with `put-bucket-policy --policy file://bucket-policy.json`, or with the next
  apply (stack buckets) or bootstrap run (state buckets).
- **Lock table**: `aws dynamodb delete-resource-policy --resource-arn <lock-table-arn>`.

Pass `--profile`, `--region` and `--no-cli-pager` on each call. The next apply of the same commit, or
the next bootstrap run, puts the policies back. Root cannot run Terraform (see "Break-glass and
rollback" above), so recovery as root is these raw calls only.

### Smoke: `store_principal_deny`

`smoke/store_principal_deny.py` is in `LAYER_4_MODULES`. Run it alone with:

```bash
bash scripts/verify_deployment.sh --i-understand-this-is-billable --only scripts.smoke.store_principal_deny
```

It reads the stores from the stack outputs and from the gitignored `infra/backend.hcl`. The state rows
defer when that file is absent. Its rows:

- `<store>_policy_shape`: each key, bucket and lock-table policy holds exactly the designed Deny
  statements, with allowlists equal to the roles discovered in the account plus the operators.
- `state_operator_list_matches_stack`: the bootstrap's operator list equals the
  `crm_operator_principal_arns` output.
- `operator_admitted:<store>`: the operator is admitted.
- `probe_denied:<store>:<call>`: a run-scoped role `crm-agent-eval-probe-<run>`, trusted by the
  operator only and with its own Allow, is refused with "explicit deny in a resource-based policy".
- `logs_probe_denied`: the same probe role's `FilterLogEvents` on a log group. A refusal with
  "explicit deny in a resource-based policy" passes. A successful read is a `defer` with
  `logs_read_not_key_gated`, never a fail: CloudWatch Logs decrypts under its own service identity,
  so the logs key Deny refuses direct key use but not log reads, and log redaction applies. Nothing in
  this repository makes the read refusable today, so expect the defer. An implicit deny fails (probe
  setup); any other refusal defers with its message.
- `admin_simulated_denied:<bucket>`: S3 administration has no call that cannot change anything, so
  this row is an `iam simulate-principal-policy` against the live bucket document, not a call. Every
  administration action is `explicitDeny` for the probe role and none is for the operator.
- `ecr_layer_fetch`, `cloudtrail_delivery_current`, `cloudtrail_logs_delivery_current`,
  `flow_log_delivery`, `log_ingestion_current:<writer>`, `logs_key_access_denied_events` and
  `logs_key_decrypt_per_function`: the service paths the exemptions must admit still work, and no
  project role was refused on the logs key.
- `probe_role_deleted`: the probe role was deleted in a `finally`. If this row fails, delete the role
  by hand.

No probe call can write data. The delivery rows poll for up to 20 minutes before deferring. The full
row semantics are in the module docstring.

### Rotating the MCP app-client secret

`scripts/rotate_mcp_client_secret.py` replaces the MCP app client's secret and keeps its client id,
so both Gateways' allowed-client lists stay valid. Run it only after the
state backend's principal Deny is in place and `store_principal_deny` passes: Terraform state holds
the secret until the refresh in step 2 empties it.

1. Rotate. `--secret-out` must be a new file outside any git work tree:

   ```bash
   .venv/bin/python3 scripts/rotate_mcp_client_secret.py --outputs-json results/tf-outputs.json \
     --secret-out ~/.local/state/crm-eval/<stack>/mcp-client-secret.new \
     --profile <p> --region <r>
   ```

   It refuses unless the session's account and region match the outputs and exactly one secret is
   active. It adds a Cognito-generated secret and writes it `0600` to `--secret-out`. Then it mints a
   `client_credentials` token with the new secret and with the old one, deletes the old secret, and
   checks that the old secret is now refused with `invalid_client`. If the new secret never mints, it
   deletes it and its file and leaves the old secret active. It prints ids, dates and HTTP statuses
   only, never a secret. Exit 0 means rotated; 1 means refused or failed, and the message names the
   step and the secrets still active; 2 means a usage error.
2. Refresh state and the outputs snapshot as the operator, in the checkout that was deployed (its
   `infra/build/` zips and `infra/terraform.tfvars` present):
   `AWS_PROFILE=<p> AWS_REGION=<r> terraform -chdir=infra apply -refresh-only -auto-approve`, then
   `bash scripts/refresh_outputs.sh --profile <p> --region <r>`. After a rotation Cognito no longer
   returns the secret, so the refreshed `cognito_mcp_client_secret` output is empty. That is
   expected: the `--secret-out` file is now the only copy.
3. Confirm that the secret in the `--secret-out` file mints a token (the recipe in
   [`docs/mcp-client-guide.md`](mcp-client-guide.md), reading the file).
4. Set `CRM_MCP_CLIENT_SECRET_FILE` to the `--secret-out` path, then run
   `bash scripts/verify_deployment.sh --i-understand-this-is-billable --only scripts.smoke.gateway_tools,scripts.smoke.mcp_chat_gateway`.
   Keep it set for every later `verify_deployment.sh` run: the smokes that mint a machine token read
   the secret from that file when it is set, and otherwise from the output, and they fail, naming
   the variable, when the file is unreadable or the secret is empty.
5. `terraform plan -detailed-exitcode` shows no change to the Cognito user pool client.
6. Overwrite or delete every other copy of the old secret, and give the new one to the owner of every
   external `client_credentials` client out of band.

A later rotation reads the old secret from the output, which is empty after the first rotation.
Pass the previous `--secret-out` file as `--old-secret-file`. The file, when given, supplies the old
secret. The script exits 2 when the file is unreadable or empty, when the output is empty and no
file is given, or when both are present and differ.

## Migrate saved supervisor effort

`scripts/migrate_supervisor_effort.py` moves every saved per-user supervisor effort of `low` to
`medium`. It changes only that value: every other tier and attribute, `updated_at` included, is left
as saved. Each write is conditional on the stored tuning still matching what was read. A save made
in between is kept and counted as `conflict`. An item whose migrated settings would not resolve
against the capability matrix is skipped, counted, and warned about on stderr.

| Flag | Effect |
|---|---|
| `--outputs-json PATH` | `terraform output -json` document, or `-` for stdin (required) |
| `--profile NAME`, `--region REGION` | target for the DynamoDB calls (required); the session's account and region must match the outputs before anything is read |
| `--table-prefix PREFIX` | table prefix; by default derived from the outputs, and must agree with them |
| `--apply` | write; without it the run is a dry run and writes nothing |

Run a dry run, then apply, then a second dry run:

```bash
.venv/bin/python3 scripts/migrate_supervisor_effort.py --outputs-json results/tf-outputs.json \
  --profile <p> --region <r>
.venv/bin/python3 scripts/migrate_supervisor_effort.py --outputs-json results/tf-outputs.json \
  --profile <p> --region <r> --apply
.venv/bin/python3 scripts/migrate_supervisor_effort.py --outputs-json results/tf-outputs.json \
  --profile <p> --region <r>
```

Each run prints one JSON object of counts to stdout:

- `dry_run`, `partial`;
- `scanned`, `eligible`, `migrated`, `conflict`;
- `already_medium`, `unchanged`;
- `skipped_unresolvable`, `skipped_not_settings`.

The second dry run must report `eligible: 0`, with the migrated items now counted in
`already_medium`. A re-run migrates nothing.

Exit codes:

- `0`: done.
- `1`: refused or failed. Refusals include a target mismatch and a configured supervisor that does
  not accept `medium`. A run that fails part-way prints its partial counts with `partial: true`;
  items already written stay migrated, and re-running is safe.
- `2`: usage error.
