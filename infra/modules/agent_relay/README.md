# `agent_relay` module

The browser's only agent endpoint: a Node.js Lambda in the deployment's private VPC, with an
`AWS_IAM`-authorized Function URL in `RESPONSE_STREAM` mode, its own execution role, and its own log
group. It verifies the caller's Cognito JWT, calls `InvokeAgentRuntime` with SigV4, and re-emits the
Runtime's frames as SSE. It holds no business logic, no tool dispatch and no prompt, and it constructs
no `Identity`.

See also: the [documentation map](../../../README.md#documentation), the
[runbook](../../../docs/runbook.md#updating-deployed-action-lambdas) and the [security
baseline](../../../docs/security-baseline.md#h-7). Modules named here:
[`agentcore`](../agentcore/README.md), [`cognito`](../cognito/README.md),
[`eni_function_code_deny`](../eni_function_code_deny/README.md),
[`frontend_cdn`](../frontend_cdn/README.md), [`vpc`](../vpc/README.md).

## Inputs

| Variable | Default | Notes |
|---|---|---|
| `function_name` | required | also names the role and log group |
| `handler` | `"index.handler"` | |
| `runtime` | `"nodejs24.x"` | validated to start with `nodejs`. Python managed runtimes cannot stream a response; a Python value would deploy and buffer |
| `filename` | required | the zip built by `scripts/package_relay.sh` |
| `source_code_hash` | required | base64 SHA-256 of the zip |
| `timeout_seconds` | `660` | validated `> 600`, above the 600s chat turn wall clock, so a budget-exhausted turn and a relay timeout are distinguishable |
| `memory_size_mb` | `512` | |
| `reserved_concurrent_executions` | required | validated `> 0` (0 makes the function uninvokable). No default, so every claim on the account's concurrency pool is explicit; the root wires `local.reserved_concurrency["agent_relay"]`, the same map its headroom validation sums |
| `eni_function_code_deny_policy_arn` | required | `module.eni_function_code_deny.policy_arn`; a customer-managed policy ARN, attached to the role |
| `agent_runtime_arn` | required | the one Runtime this relay may invoke. Validated to be a `bedrock-agentcore` ARN with no wildcard |
| `cognito_issuer` | required | user-pool issuer URL; the JWKS URL is derived from it |
| `agent_runtime_qualifier` | `"DEFAULT"` | the endpoint the relay invokes and the role is granted, from one value; non-empty |
| `cognito_audience_parameter_name` | required | name of the SSM parameter holding the expected JWT audience — see "The audience indirection" |
| `cognito_audience_parameter_arn` | required | that parameter's ARN, wildcard-free. **Construct it** from region/account/name; never read it from `aws_ssm_parameter.…arn` |
| `environment_kms_key_arn` | `null` | CMK for environment variables and the log group |
| `log_retention_days` | `365` | minimum 365 (validated) |
| `vpc_subnet_ids` | required | `module.vpc.lambda_subnet_ids`; at least two |
| `vpc_security_group_ids` | required | `module.vpc.lambda_security_group_ids`; non-empty |
| `tags` | `{}` | |

The VPC inputs have no default so a call site cannot omit them. Never pass literals: a subnet or
security-group id in tracked content fails `check_no_account_id`.

## Outputs

`function_name`, `function_arn`, `function_url`, `function_url_domain`, `role_arn`, `role_name`,
`log_group_name`, `environment_variable_names`.

`function_url_domain` is the host alone (no scheme, no trailing slash), as a CloudFront custom origin's
`domain_name` requires. `function_url` is the full URL — use it for `curl`, not for an origin.

## Configuration surface

| Environment variable | Value |
|---|---|
| `AGENT_RUNTIME_ARN` | the one Runtime to invoke |
| `AGENT_RUNTIME_QUALIFIER` | endpoint qualifier; the same value the IAM grant's endpoint ARN covers |
| `COGNITO_ISSUER` | user-pool issuer URL |
| `COGNITO_JWKS_URL` | derived from the issuer, so the two cannot disagree |
| `COGNITO_AUDIENCE_PARAMETER` | **name** of the SSM parameter holding the expected app-client id |

`environment_variable_names` exports this set so a contract test can assert it in both directions —
nothing the relay reads is unset, nothing set is unread.

## The audience indirection

The relay needs the SPA app-client id to check `aud`. Taking it from `module.cognito` directly creates a
Terraform cycle: the SPA client's callback URLs need the CloudFront domain, the distribution needs this
relay's Function URL, and this relay would need the client id.

So the value travels as a parameter name, the relay reads it at cold start with `ssm:GetParameter`
scoped to that one parameter, and the edge disappears. Reading
`aws_ssm_parameter.relay_cognito_audience.arn` instead of constructing the ARN restores the cycle.

## Execution role

| Grant | Resource |
|---|---|
| `bedrock-agentcore:InvokeAgentRuntime` | the runtime ARN **and** its `runtime-endpoint/*` children. AgentCore authorizes over both, so a runtime-only grant is `AccessDenied` on the first real turn |
| `logs:CreateLogStream`, `logs:PutLogEvents` | this function's log group only |
| `ssm:GetParameter` | exactly the audience parameter, never `parameter/*` |
| `ec2:CreateNetworkInterface`, `ec2:DescribeNetworkInterfaces`, `ec2:DescribeSubnets`, `ec2:DeleteNetworkInterface`, `ec2:AssignPrivateIpAddresses`, `ec2:UnassignPrivateIpAddresses` | `*`, as the [Lambda VPC documentation](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html) requires. Without it the function is created and every invocation fails |
| `xray:PutTraceSegments`, `xray:PutTelemetryRecords` | `*`; neither action takes a resource type. Used by `tracing_config.mode = "Active"`. Same `vpc_access` policy as the ENI grant. No Deny below reaches the X-Ray actions, which `test_infra_relay.py` asserts |

Three explicit `Deny` statements:

- `DenyEverythingButTheOnePermittedInvokeOnThisRuntime` — `not_actions =
  ["bedrock-agentcore:InvokeAgentRuntime"]` on the runtime and its endpoints. This denies every other
  action on them, including the command-execution APIs (`InvokeAgentRuntimeCommand`, which is *not*
  the JS SDK class of the same name), `InvokeAgentRuntimeForUser`, the WebSocket-stream variants, and
  actions AWS has not shipped yet. **A future legitimate action on the runtime must be added to
  `not_actions`**, or it will be denied. Its `resources` must stay scoped to the runtime: a `not_actions`
  Deny on `"*"` would also deny `logs:*` and `ssm:GetParameter`.
- `DenyEveryHarness` — `bedrock-agentcore:*` on every harness resource in the account and Region, since
  `InvokeHarness` authorizes against `harness*` and the first Deny does not reach it.
- `DenyEniActionsToFunctionCode` — not inline: the managed policy from [`modules/eni_function_code_deny`](../eni_function_code_deny/README.md),
  attached through `eni_function_code_deny_policy_arn`. It denies the six ENI actions above plus
  `ec2:DetachNetworkInterface` on `*` when `lambda:SourceFunctionArn` is present. Calls made by
  function code carry that key and Lambda's own ENI management does not, so only the service can use
  the ENI grant. Its actions exclude X-Ray.

The role has no DynamoDB, Bedrock model, or `lambda:InvokeFunction` access. The action Lambdas remain
the single enforcement boundary.

No *other* principal can invoke the Runtime because of its resource-based policies, declared at the
root because they need both the Runtime ARN and this module's `role_arn`:
`aws_bedrockagentcore_resource_policy.agent_runtime_relay_only` on the runtime, and
`agent_runtime_endpoint_relay_only` on its `DEFAULT` endpoint. Each policy pairs an Allow for the
three invoker roles (this role, `sweep_task`'s and `chat_target`'s) with the explicit Deny
`DenyRuntimeDataPlaneToAnyOtherPrincipal`. That Deny covers the eight Runtime data-plane actions,
from `InvokeAgentRuntime` to `GetAgentCard`, for `Principal: "*"`, and applies when
`aws:PrincipalArn` is not one of the three invoker ARNs (`local.runtime_invoker_role_arns`). An
Allow alone does not refuse a same-account principal whose identity policy grants
`bedrock-agentcore:*`. The Deny does, so a co-tenant PowerUser role gets `AccessDeniedException`
rather than reaching the Runtime. Control-plane actions are not denied.

## Reachability: `AWS_IAM` behind CloudFront OAC

The Function URL is `authorization_type = "AWS_IAM"`, and this module declares no
`aws_lambda_permission`. The only permitted caller is the CloudFront distribution, through a
`lambda`-type origin access control that SigV4-signs every origin request; the matching
`lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` permissions live in [`modules/frontend_cdn`](../frontend_cdn/README.md)
because they are scoped to the distribution ARN.

OAC's `signing_behavior = "always"` overwrites the viewer's `Authorization` header, so headers are
divided as follows:

| Concern | Header | Owner |
|---|---|---|
| reachability (did this come through the distribution?) | `Authorization` | CloudFront's SigV4 |
| authentication (who is calling?) | `X-Crm-Authorization` | the Cognito JWT, verified in `src/relay/src/jwt.ts` |
| body integrity (Lambda rejects unsigned `POST` payloads) | `x-amz-content-sha256` | the SPA, via `crypto.subtle.digest` |

A validly signed origin request establishes nothing about who is calling: an absent or unverified token
is a 401 before any Runtime call.

## VPC placement

The relay runs inside `module.vpc`'s private subnets.

- **The VPC is not what makes this relay non-public.** A Function URL is internet-reachable however its
  function is networked; the `AWS_IAM` URL and OAC close the exposure. `check_no_public_exposure` scores
  them as separate controls (`function-url-auth` vs `lambda-vpc`).
- **Outbound calls:** `InvokeAgentRuntime` and `ssm:GetParameter` go through interface endpoints. The
  Cognito JWKS fetch has no usable endpoint (a user pool with a domain is incompatible with
  PrivateLink) and needs the NAT path. With `module.vpc`'s `nat_gateway_enabled = false`, `jwt.ts`
  cannot fetch the key set and every turn fails authentication.
- **A missing route is a timeout**, at the SDK's retry budget — not an `AccessDenied` and not a plan
  failure. `module.vpc`'s flow log shows the blocked attempt as a `REJECT`.

## Verifying

Plan-time, offline — streaming mode, IAM auth type, Node runtime, timeout, exact environment surface,
and the role's exact grant set:

```bash
.venv/bin/python3 -m pytest tests/unit/test_infra_relay.py
```

The `AWS_IAM` URL, the absence of any wildcard-principal permission, the `vpc_config` and the ENI grant
are asserted in `tests/unit/test_infra_vpc.py`, and across the whole tree by a gate with no waiver:

```bash
.venv/bin/python3 -m pytest tests/unit/test_infra_vpc.py
.venv/bin/python3 scripts/check_no_public_exposure.py    # must exit 0
```

Standalone validate:

```bash
cd infra/modules/agent_relay
terraform init -backend=false
terraform validate
```

Needs the deployed stack:

- `simulate-principal-policy` against the deployed role, including actions that must evaluate `Deny`
  (`scripts/smoke/iam_roles.py`, `scripts/smoke/relay_iam.py`).
- The logs policy's action set. Its document embeds the log group's ARN, so the plan renders it
  `(known after apply)`; `test_infra_relay.py` asserts that exactly one policy is deferred this way and
  names it.
- An unauthenticated request to the Function URL's **own hostname** refused
  (`scripts/smoke/public_exposure.py`), and the `/agent/*` behaviour's real origin
  (`scripts/smoke/cloudfront.py`).
- The relay reaching the AgentCore data plane from inside the VPC, and the Cognito JWKS URL through the
  NAT gateway — see `infra/modules/vpc/README.md`, "Not covered offline".

## Packaging

`scripts/package_relay.sh` builds the zip. The root points `filename` at
`${var.lambda_build_dir}/relay.zip` (`lambda_build_dir` is declared in `infra/main.tf`). Its churn is
absorbed by `scripts/classify_post_apply_drift.py` as ordinary `aws_lambda_function` packaging drift.

**`LAMBDA_BUILD_DIR` is not a free override.** The packaging scripts and `scripts/deploy.sh`'s pre-apply
guard honour it, but `deploy.sh` does not pass `-var lambda_build_dir` to `terraform apply`, so Terraform
keeps its default. Leave it unset unless you also pass `-var lambda_build_dir=` to Terraform yourself.
