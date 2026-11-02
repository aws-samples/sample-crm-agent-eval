# `agentcore` module

The AgentCore surface: an ECR repository and an in-apply CodeBuild image build, the AgentCore Runtime,
the Gateway (`CUSTOM_JWT` authorizer plus a REQUEST interceptor), and one Gateway target per action
Lambda.

See also: the [documentation map](../../../README.md#documentation), the
[runbook](../../../docs/runbook.md#agentcore-runtime-platform-version) and the [security
baseline](../../../docs/security-baseline.md#h-10). Modules named here:
[`cognito`](../cognito/README.md), [`lambda_action`](../lambda_action/README.md),
[`vpc`](../vpc/README.md).

## Contents

| File | Declares |
|---|---|
| `ecr_codebuild.tf` | ECR repository and lifecycle policy, CodeBuild project and role, `null_resource.image_build` |
| `runtime.tf` | `aws_bedrockagentcore_agent_runtime.this` |
| `runtime_log_group.tf` | the Runtime's default-endpoint log group |
| `gateway.tf` | `aws_bedrockagentcore_gateway.this` with authorizer and interceptor configuration |
| `gateway_targets.tf` | one `aws_bedrockagentcore_gateway_target` per entry in `tool_lambda_arns` |
| `interceptor_permission.tf` | the Gateway's permission to invoke the interceptor Lambda |

## Inputs

| Variable | Default | Notes |
|---|---|---|
| `aws_profile` | `"default"` | profile for the build trigger's `local-exec` |
| `aws_region` | `"us-east-1"` | the root passes `var.aws_region`, which it validates against `infra/regions.json` |
| `name_prefix` | `"crm-agent-eval"` | |
| `ecr_repository_name` | `"crm-agent-eval-agent"` | |
| `kms_key_arn` | required | CMK encrypting the ECR repository and the runtime and CodeBuild log groups |
| `image_retention_count` | `10` | `sha-`-tagged images kept by the lifecycle policy; older ones are expired, and untagged images expire after 14 days |
| `image_tag` | `"latest"` | tag the Runtime consumes |
| `source_bucket_arn`, `source_key` | required | location of the zipped agent source CodeBuild reads |
| `source_kms_key_arn` | required | key encrypting that source object; also the CodeBuild project's `encryption_key` |
| `source_content_hash` | required | content hash of the agent source; the build re-runs only when it changes |
| `buildspec` | required | inline buildspec (Docker build and ECR push) |
| `log_retention_days` | `365` | minimum 365 (validated) |
| `agent_runtime_name` | `"crm_agent_eval_runtime"` | |
| `agent_runtime_role_arn` | required | the Runtime's execution role (created at the root) |
| `agent_runtime_environment_variables` | `{}` | container environment; see below |
| `platform_version` | `"V2"` | Runtime `platformVersion`, `"V1"` or `"V2"` (validated); rollback and V2 notes in `docs/runbook.md` |
| `runtime_network_mode` | `"PUBLIC"` | `"PUBLIC"` or `"VPC"`. The root sets `"VPC"` |
| `runtime_subnet_ids`, `runtime_security_group_ids` | `[]` | required (precondition) when `runtime_network_mode = "VPC"`. Each subnet must be in an AgentCore-supported AZ for the region, as listed in `infra/regions.json` (`preflight_adopted_vpc.sh`, and the `runtime_network_posture` smoke) |
| `cognito_discovery_url` | required | Gateway `CUSTOM_JWT` discovery URL |
| `cognito_allowed_client_ids`, `cognito_allowed_audiences` | required | Gateway authorizer allowlists |
| `cognito_allowed_scopes`, `cognito_required_custom_claims` | `[]` | |
| `gateway_name` | `"crm-agent-eval-gateway"` | |
| `gateway_role_arn` | required | |
| `identity_interceptor_lambda_arn`, `identity_interceptor_lambda_function_name` | required | the REQUEST interceptor |
| `tool_lambda_arns` | required | map of tool name → action Lambda ARN |
| `tool_schemas` | required | per-tool flattened MCP schema; see below |
| `tags` | `{}` | |
| `data_classification` | required | value of the `data-classification` tag |

## Outputs

`ecr_repository_url`, `ecr_repository_arn`, `agent_runtime_arn`, `agent_runtime_id`, `gateway_id`,
`gateway_arn`, `gateway_url`, `gateway_target_ids` (tool name → target id),
`agent_runtime_log_group_name`, `agent_runtime_log_group_arn`, `codebuild_role_arn` (the image
build's service role; the root admits it to the eval-artefacts bucket and key policies, because the
build reads the agent-source object from that bucket).

## Consumption mode, never instance-based

`runtime.tf` sets no `capacity_provider_configuration`. That argument is what opts a runtime into the
**Instances** compute type, which bills continuously; without it the runtime uses the default
microVM/consumption compute type, billed per second while active and nothing at idle. The pinned
`hashicorp/aws` 6.71.0 schema for `aws_bedrockagentcore_agent_runtime` has no
`capacity_provider_configuration` argument, so the omission is structural.

To check any `aws_bedrockagentcore_*` argument against the provider schema:

```bash
terraform init -backend=false
terraform providers schema -json \
  | jq '.provider_schemas["registry.terraform.io/hashicorp/aws"].resource_schemas.aws_bedrockagentcore_agent_runtime'
```

## In-apply CodeBuild image build

`null_resource.image_build` triggers on `source_content_hash`, so the build re-runs only when the source
changes. Its `local-exec` provisioner runs `codebuild start-build`, waits for completion, and fails on any
final `buildStatus` other than `SUCCEEDED`.

If the build fails, `terraform apply` fails at this resource and the Runtime is not created or updated.
Resources already applied in the same run (ECR repository, CodeBuild project, its role) stay in place —
a partial apply, not a rollback. Fix the source and re-run `apply`; the same hash-gated trigger rebuilds.
A source build failure therefore surfaces as a Terraform apply failure, not as a separate CI signal.

### What the image must contain, and what the container must be told

Neither requirement is visible to `terraform validate` or a successful build; both fail only on a real
`/invocations` turn.

- **`config/` must be in the image.** `src/providers/capabilities.py` resolves `config/models.yaml` and
  `config/model_capabilities.yaml` relative to the repository root (`/app` in the container), and every
  normal turn loads them. `scripts/package_lambdas.sh` stages them into `agent-source.zip` and the root
  `Dockerfile` copies them. The `isolation_probe` mode never loads them, so a probe-only smoke passes
  with `config/` missing.
- **`ACTION_LAMBDA_PREFIX` must be set** in `agent_runtime_environment_variables`. The container reaches
  the CRM by invoking each action Lambda, never by importing a handler; the prefix maps a tool name to a
  function name. The root sets it from the same `local.name_prefix` that names every
  `module.lambda_action`.

The Runtime execution role (root, `data.aws_iam_policy_document.agentcore_runtime_permissions`) grants
`lambda:InvokeFunction` on exactly the action Lambdas, no DynamoDB access, and Bedrock
`InvokeModel`/`InvokeModelWithResponseStream` on the inference profiles `local.bedrock_inference_profiles`
derives from `config/model_capabilities.yaml` (both the Anthropic and OpenAI families) and their
foundation models — never `Resource = "*"`. A plan-time precondition requires every profile to be a
`global.<family>.<model>` id of its entry's own family, so no wildcard can enter the grant.

### The request modes `POST /invocations` serves

`src/agents/runtime_server.py` branches on the payload's `mode` field. Every branch resolves identity
through `agents.runtime_entrypoint.resolve_identity` first.

| `mode` | What it does | Model call | Action Lambda call |
|---|---|---|---|
| absent | the full turn: provider stream, tool dispatch, SSE out | yes | yes, per model-chosen tool call |
| `isolation_probe` | reports a role's registry-derived tool surface and classifies a synthetic outcome trigger | no | no |
| `delegation` | routes one **named** tool to its owning subagent and dispatches it | no | yes, one `lambda:InvokeFunction` |
| `cancel` | records a cancellation request for the caller's own `session_id`, which the running turn reads between steps; returns `accepted`, `already_requested` or `already_terminal` | no | no |
| `eval_turn` | one eval task's turn for `sweep_task`: requires an `eval-<run_id>` tenant and the arm's schema-3 `tuning` (strictly parsed: any key outside it is refused), runs under the eval budget with `provider_mode` `stub` or `live`, and returns the observed turn as JSON rather than SSE | yes (the stub provider when `provider_mode` is `stub`) | yes, per model-chosen tool call |

`delegation` lets a test target a specific tool and record, which the model-driven turn path cannot. It
is not read-only: it shares tool dispatch and result classification with the turn path, and so needs
`ACTION_LAMBDA_PREFIX`. It resolves no provider and constructs no `Store`.

## Inbound auth: the Runtime is IAM, the Gateway is CUSTOM_JWT

| Resource | Inbound auth | Caller |
|---|---|---|
| `aws_bedrockagentcore_agent_runtime.this` | IAM / SigV4 | the relay, `sweep_task` and `chat_target`, each signing with its own execution role |
| `aws_bedrockagentcore_gateway.this` | `CUSTOM_JWT` | external MCP clients presenting Cognito tokens |

The provider has no IAM enum — `authorizer_configuration`'s only sub-block is `custom_jwt_authorizer` —
so the Runtime's IAM auth is the absence of that block. Removing the Gateway's block would leave the
MCP surface unauthenticated; adding one to the Runtime would let JWT-bearing callers reach it directly.

`request_header_configuration` is also absent from the Runtime. Under IAM auth AgentCore validates no
bearer token, so a forwarded `Authorization` header would be unvalidated input. Identity reaches the
container in the SigV4-authenticated payload over the `agent` channel.

## Gateway REQUEST interceptor: `pass_request_headers = true`

`gateway.tf` sets `interceptor_configuration.input_configuration.pass_request_headers = true`. Without
it the interceptor cannot receive the inbound `Authorization` header, and the identity path (Gateway →
interceptor → `_identity` → Lambda target) is impossible. It also widens the trust boundary: the
interceptor Lambda and its role receive the raw bearer token. The compensating control is that
`src/gateway/identity_interceptor.py` re-verifies the token against Cognito's JWKS rather than trusting
the header.

## Gateway target tool schemas (`tool_schemas`)

`aws_bedrockagentcore_gateway_target` requires a `tool_schema` block per Lambda target. `tool_schemas`
carries each tool's flattened MCP schema (description plus top-level property list), which the root
derives from `crm.toolspec.definitions.ALL_TOOLS` — never hand-copied. Nested object/array properties
cannot be expressed in this variable's shape; see its description in `variables.tf`.

## Network mode

The root runs the Runtime in **VPC mode** (`runtime_network_mode = "VPC"` with `runtime_subnet_ids` and
`runtime_security_group_ids` from `module.vpc`), rendered as `network_configuration.network_mode` plus a
`network_mode_config` block. A VPC-mode plan without subnets or security groups fails a precondition.
The module's own default is `PUBLIC` so it plans standalone with no network inputs. Changing
`PUBLIC` → `VPC` is an in-place update; the runtime ARN is stable.

VPC mode governs **egress only**; it does not change who can call `InvokeAgentRuntime`. The inbound
boundary is the Runtime's resource policies at the root (`data.aws_iam_policy_document.agent_runtime_*`),
which include an `aws:SourceVpce` Deny keyed on the `bedrock-agentcore` VPC endpoint, enabled by the
root variable `runtime_source_vpce_deny_enabled` (default `false`).

## Teardown

`aws_ecr_repository.agent` sets **`force_delete = true`**. Every content-hash change pushes an image and
untagged images expire only after 14 days, so the repository is rarely empty; without the flag
`terraform destroy` aborts with `RepositoryNotEmptyException`.

A destroy **deletes every image, including tagged ones**, without prompting. The images are reproducible
from `src/agents/` and the `Dockerfile`, so do not store anything else in this repository.

The `teardown_force_destroy` control in `scripts/check_plan_security_baseline.py` requires the flag on
every `aws_ecr_repository` in `infra/`, with no exemption list:

```bash
.venv/bin/python3 scripts/check_plan_security_baseline.py \
  --plan "$(.venv/bin/python3 scripts/plan_fixtures.py compliant)"
.venv/bin/python3 -m pytest tests/unit/test_plan_security_baseline.py -k TeardownForceDestroy
```

## What this module does not own

- The `lambda_action` and `cognito` modules — their outputs arrive as variables.
- The Runtime execution role and resource policies — declared at the root.
- Plan-time assertion of the interceptor configuration and `CUSTOM_JWT` authorizer shape —
  `scripts/check_plan_security_baseline.py`.
- Generating `tool_schemas` from the toolspec — the root's responsibility, so this module has no
  Python dependency.

## Validating

`infra/main.tf` calls this module, so `cd infra && terraform validate` exercises it. Its `versions.tf`
pins the same `hashicorp/aws` release as the root, so it also validates standalone:

```bash
cd infra/modules/agentcore
terraform init -backend=false
terraform validate
```

The Runtime's IAM inbound auth (no `authorizer_configuration`) and the absent
`request_header_configuration` are asserted against a real `terraform show -json` plan — a missing block
is valid HCL, so `terraform validate` cannot see either:

```bash
.venv/bin/python3 -m pytest tests/unit/test_infra_agentcore.py
```

The Runtime's inbound auth on the deployed resource is checked by `scripts/smoke/agents.py`.
