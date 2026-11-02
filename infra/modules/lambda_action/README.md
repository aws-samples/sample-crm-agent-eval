# `lambda_action` module

Reusable Terraform module for one CRM action Lambda. Each instantiation creates its own IAM execution
role (never shared), its own log group, its own reserved concurrency, and optionally a resource
policy admitting the AgentCore Gateway target role. The root composition instantiates it for every
toolspec tool and for the Gateway identity interceptor, the Cognito pre-token-generation trigger, and
the eval trigger/worker pair (`grep '^module "lambda_action' infra/main.tf` for the current set).

This module grants no API Gateway permission. The API Gateway invoke grant lives only in
[`modules/api`](../api/README.md) (`aws_lambda_permission.apigw_invoke`, one per route).
`tests/unit/test_infra_invoke_grant_scope.py` asserts this module declares no
`apigateway.amazonaws.com` permission.

See also: the [documentation map](../../../README.md#documentation), the
[runbook](../../../docs/runbook.md#updating-deployed-action-lambdas) and the [security
baseline](../../../docs/security-baseline.md#h-7). Modules named here: [`api`](../api/README.md),
[`eni_function_code_deny`](../eni_function_code_deny/README.md), [`vpc`](../vpc/README.md).

## Resources

- `aws_iam_role.this` with inline policies: write to its own log group; manage its own Hyperplane
  ENIs and write X-Ray segments (`vpc_access`, the two action sets that take `Resource = "*"`);
  DynamoDB grants and denies (when any are configured and `dynamodb_policy_managed = false`);
  DynamoDB KMS grant (when enabled).
- `aws_iam_policy.dynamodb` (keyed `allow`/`deny`, plus `allow-2`, ... when
  `dynamodb_allow_statements_per_policy` splits the Allows) and
  `aws_iam_role_policy_attachment.dynamodb_deny` / `.dynamodb_allow[*]`, when
  `dynamodb_policy_managed = true`: the same DynamoDB document split by effect into
  `<function_name>-dynamodb-allow[-N]` and `<function_name>-dynamodb-deny`, in place of the inline
  `<function_name>-dynamodb`. Each exists only when that effect has statements. Every Allow
  attachment `depends_on` the Deny attachment, so the Allows never take effect without the Denies.
- `aws_iam_role_policy_attachment.eni_function_code_deny`: the managed policy at
  `eni_function_code_deny_policy_arn` ([`modules/eni_function_code_deny`](../eni_function_code_deny/README.md)), which denies the ENI
  actions to calls carrying `lambda:SourceFunctionArn`, i.e. to function code. It is managed, not
  inline, so it does not count toward the role's inline-policy quota.
- `aws_cloudwatch_log_group.this` (`/aws/lambda/<function_name>`), retained 365 days (a literal, not
  a variable), encrypted with `environment_kms_key_arn` when set.
- `aws_lambda_function.this`, always with a `vpc_config` block and `tracing_config.mode = "Active"`.
- `aws_lambda_permission.gateway_target_role`, when `attach_gateway_target_grant = true`.

## Inputs

| Variable | Required | Notes |
|---|---|---|
| `function_name`, `handler`, `filename`, `source_code_hash` | yes | |
| `reserved_concurrent_executions` | yes | No default; validated `> 0` (`0` makes the function permanently uninvokable). |
| `vpc_subnet_ids` | yes | `module.vpc.lambda_subnet_ids`; at least two. |
| `vpc_security_group_ids` | yes | `module.vpc.lambda_security_group_ids`; at least one. |
| `eni_function_code_deny_policy_arn` | yes | `module.eni_function_code_deny.policy_arn`; a customer-managed policy ARN. |
| `runtime` | no (`python3.13`) | |
| `timeout_seconds` | no (`30`) | |
| `memory_size_mb` | no (`256`) | |
| `environment_variables` | no (`{}`) | Plaintext only; secrets are resolved at runtime from Secrets Manager or Parameter Store. |
| `environment_kms_key_arn` | no (`null`) | Customer-managed key for environment variables and the log group. |
| `dynamodb_grants` | no (`[]`) | Per-table, per-action Allow statements. `dynamodb:*` and `Resource = "*"` are rejected by validation. Optional `index_arns`, `include_table_arn`, and `leading_key_condition`. |
| `denied_table_arns` | no (`[]`) | One explicit `Deny` on `dynamodb:*` over tables this function has no grant on. Pass `<table_arn>/index/*` alongside each table ARN: `table/X` does not match a request on `table/X/index/Y`. |
| `denied_own_table_actions` | no (`[]`) | Per granted table, the actions this function is explicitly denied on it, so a later additive `Allow` cannot widen access. Wildcard actions are rejected. |
| `dynamodb_policy_managed` | no (`false`) | Attaches the DynamoDB document as customer-managed policies instead of inline, for a role whose inline aggregate nears IAM's 10,240-char cap. Each managed policy has its own 6,144-char cap and uses one of the role's managed-policy attachments (10 by default). `lambda_action_sweep_control` and `lambda_action_sweep_task` set it. `scripts/smoke/iam_policy_budget.py` measures both caps on the deployed roles. |
| `dynamodb_allow_statements_per_policy` | no (`null`) | With `dynamodb_policy_managed`, the most Allow statements one managed policy holds. The Allows are split in lexical order of the grant index (0, 1, 10, 11, 2, …) into `-dynamodb-allow`, `-dynamodb-allow-2`, …, each attached after the Deny. `null` keeps one Allow policy. `lambda_action_sweep_control` sets 20, so its Allows stay under the 6,144-char cap. |
| `dynamodb_kms_key_arn` | no (`null`) | `module.dynamodb.kms_key_arn`. |
| `dynamodb_kms_grant_enabled` | no (`false`) | Attaches `kms:Decrypt`/`kms:GenerateDataKey` on `dynamodb_kms_key_arn`. Set `true` whenever `dynamodb_grants` is non-empty. A separate `bool` because a `count` derived from the key ARN is unknown at plan time once the key is pending destruction, which blocks planning the destroy. A call that sets it must also add its `role_arn` to `local.dynamodb_data_plane_role_arns` in `infra/main.tf`: the tables' resource policies and the CMK key policy deny every role not on that list. |
| `gateway_target_role_arn` | no (`null`) | The Gateway target execution role admitted to invoke this function. |
| `attach_gateway_target_grant` | no (`false`) | Drives the Gateway permission's `count`. Must agree with `gateway_target_role_arn` being non-null (enforced by a precondition). A flag rather than a null check on the ARN so an empty-state plan has a known `count`. |
| `table_action_role_matrix_entries` | no (`[]`) | This instantiation's rows of the isolation matrix (below). Not read at plan time; echoed by `table_action_role_matrix_rows`. |
| `tags` | no (`{}`) | Added to the provider's `default_tags`. |

## Outputs

| Output | Description |
|---|---|
| `function_name` | Function name. |
| `function_arn` | Function ARN. |
| `invoke_arn` | Invoke ARN, for an API Gateway integration or a Gateway target. |
| `role_name` | Dedicated execution role name. |
| `role_arn` | Dedicated execution role ARN. |
| `log_group_name` | Log group name. |
| `table_action_role_matrix_rows` | This instantiation's matrix rows. |

## Reserved concurrency

The module validates each instantiation's value but cannot see its siblings, so it cannot enforce
the deployment-wide sum against the account's concurrency pool (1,000 by default). Current figures
come from these derivations:

| Question | Derived from |
|---|---|
| Per-tool reserved values | `local.tool_reserved_concurrency` in `infra/main.tf` |
| Summed total and headroom at plan time | `check_lambda_reserved_concurrency_headroom` in `scripts/check_plan_security_baseline.py` |
| Apply-time refusal | `terraform_data.reserved_concurrency_headroom_guard`'s `lifecycle.precondition` in `infra/main.tf` |

Every function that does not set reserved concurrency draws from the unreserved remainder, which
Lambda requires to stay non-negative.

## Table × action × role isolation matrix

`table_action_role_matrix.json` records, for every (function, table, action) grant, whether
`dynamodb:LeadingKeys` enforces rep isolation or whether isolation is enforced in application code
(`crm.authz.filter_to_scope` for list/search/aggregate paths, `crm.authz.require_scope` for
single-record paths). Dispositions are `leading_keys`, `key_prefix`, `application_code`, or
`not_applicable`; the matrix's `isolation_mechanism_legend` defines each.

- `leading_keys`: the two identity interceptors' `reps` `GetItem`, each pinned to one rep id (the
  Gateway's is `local.gateway_service_principal_rep_id`).
- `key_prefix`: a `LeadingKeys` condition that bounds a key prefix but pins no identity —
  `eval-trigger` on `eval_runs`, and `user-settings` and `chat-target` on `reps`.
- The pre-token-generation trigger's `GetItem` on `reps` reads whichever user is signing in, so it
  is `not_applicable`.
- Only `reps` is partitioned by the rep id. The entity tables (`accounts`, `contacts`,
  `opportunities`, `activities`, and likewise `notes`, `proposals`, `audit_records`,
  `usage_records`) are partitioned by their own generated id; `owner_rep_id`/`actor_rep_id` appear
  only as GSI key dimensions. Every action Lambda has one execution role shared across all reps, so
  no IAM principal carries a per-rep value a `LeadingKeys` condition could match. A condition on
  these grants would never match and would read as a control that enforces nothing, so these grants
  are `application_code`.
- `eval_runs`/`eval_results` are harness bookkeeping with no per-rep scope.

`scripts/check_plan_security_baseline.py` asserts the matrix shape at plan time (no `dynamodb:*`,
no `Resource = "*"`, no `LeadingKeys` on an `application_code` table).
`scripts/smoke/iam_roles.py` checks the per-role allow/deny boundary, including the interceptor's
`LeadingKeys` condition, with `simulate-principal-policy` against a deployed stack.

## Validation

`cd infra && terraform validate` exercises the module through the root. It also validates
standalone; `versions.tf` pins the same `hashicorp/aws` 6.71.0 as the root:

```bash
cd infra/modules/lambda_action
terraform init -backend=false
terraform validate
```

`terraform validate` does not evaluate `count`, so the destroy-with-KMS-key-absent property is
checked separately, offline and without credentials:

```bash
bash scripts/verify_terraform_plan_with_kms_absent.sh   # --keep to inspect the fixtures
```

Its third scenario is a self-check: it gives the `count` an unknown-until-apply value and requires
the plan to fail.

## Out of scope

- Table and GSI definitions: `modules/dynamodb`. This module takes table names/ARNs as inputs.
- The API Gateway invoke grant: [`modules/api`](../api/README.md).
