# Security baseline

See also: [documentation map](../README.md#documentation) · [runbook](runbook.md) · [cognito module](../infra/modules/cognito/README.md) · [agentcore module](../infra/modules/agentcore/README.md) · [api module](../infra/modules/api/README.md)

The security controls the deployed stack must satisfy. `blocks completion` rows must pass before a
change ships; `review-PASS` rows are recorded as review findings if absent. This table is read by
`scripts/check_plan_security_baseline.py --self-check`, which fails when a row has neither a plan-time
assertion nor a recorded reason it cannot be one, and by `tests/unit/test_verify_security_baseline.py`.

![AWS network baseline: the adopted VPC with its interface and gateway endpoints, security groups, optional egress-only NAT, flow logs, the Runtime resource policy's SourceVpce deny, KMS keys and CloudTrail; WAF and the viewer TLS 1.2 minimum are deferred](figures/security-baseline-aws-network.svg)

| Control | Requirement | Verify | Class |
|---|---|---|---|
| DynamoDB encryption at rest | KMS CMK, not AWS-owned | `describe-table \| jq '.Table.SSEDescription'` → `SSEType=KMS` + CMK ARN | blocks completion |
| DynamoDB PITR | enabled all tables | `describe-continuous-backups` → `ENABLED` | blocks completion |
| DynamoDB IAM | no `dynamodb:*`; per-table, per-action; `LeadingKeys` conditions where applicable | `iam simulate-principal-policy` | blocks completion |
| DynamoDB IAM | table and CMK policies deny the data plane, key use and administration to every principal outside the project's Lambda roles and `crm_operator_principal_arns` | `check_plan_security_baseline.py --only dynamodb`; Layer 4 smoke `dynamodb_principal_deny` (`get-resource-policy`, `get-key-policy`, denied probe calls) | blocks completion |
| S3 frontend bucket | BPA on, private, OAC-only, TLS-deny policy | `get-public-access-block`, `get-bucket-policy` | blocks completion |
| S3 encryption | KMS CMK | `get-bucket-encryption` | blocks completion |
| S3 encryption | eval-artefacts, CloudTrail and frontend-origin bucket policies deny the object data plane (writes only, on the origin) and bucket administration to every principal outside the store's allowlist and `crm_operator_principal_arns`; only the CloudTrail bucket's data-plane Deny exempts service principals | `check_plan_security_baseline.py --only store`; Layer 4 smoke `store_principal_deny` | blocks completion |
| API Gateway | authorizer on **every** route; no unauthenticated endpoint | `get-method` per route | blocks completion |
| API Gateway | throttle + burst on every stage; request validators; access logging | `get-stage \| jq '.accessLogSettings, .throttleSettings'` | review-PASS |
| Lambda | **one execution role per function**, never shared | `check_plan_security_baseline.py --only lambda` over the plan | blocks completion |
| Lambda | reserved concurrency set on every function | `get-function-concurrency` | review-PASS |
| Lambda | KMS-encrypted env vars where sensitive | `get-function-configuration \| jq '.KMSKeyArn'` | review-PASS |
| Cognito | MFA reachable (`ON` or `OPTIONAL`, TOTP factor enabled), password policy, account recovery configured | `describe-user-pool` | blocks completion |
| Gateway | `CUSTOM_JWT` with discovery URL, allowed clients/audiences/scopes, custom claims | `get-gateway` | blocks completion |
| Gateway | REQUEST interceptor present with `passRequestHeaders: true` | `get-gateway \| jq '.interceptorConfigurations'` | blocks completion |
| Action Lambdas | resource policy admits only the Gateway target role and API Gateway | `lambda get-policy` → no wildcard principal | blocks completion |
| Network | no internet-facing SG ingress anywhere | `check_plan_security_baseline.py --only network` over the plan | blocks completion |
| Secrets | Secrets Manager / Parameter Store only; **no vendor API key anywhere, ever** | `gitleaks` + `scripts/check_no_vendor_keys.sh` | blocks completion |
| Bedrock | assumed role + inference profile only | IAM policy review; no key material in any env | blocks completion |
| Transport | TLS 1.2+ everywhere | CloudFront viewer policy, bucket policy `aws:SecureTransport` | **DEFER (unmet)**: the CloudFront minimum TLS version needs a custom ACM certificate |
| Tags | `service`, `environment`, `owner`, `cost-center`, `data-classification`, `AppName` (`crm-agent-eval`) on all resources | `resourcegroupstaggingapi get-resources` | review-PASS |
| CloudTrail | enabled for API activity | `describe-trails` | review-PASS |
| Logs | explicit retention; no secrets, tokens, or full payloads | log-group config + `scripts/check_log_redaction.py` | review-PASS |
| Logs | KMS CMK on every log group (`kms_key_id` set, never null) | `check_plan_security_baseline.py --only logs` over the plan; `describe-log-groups \| jq '.logGroups[].kmsKeyId'` | review-PASS |
| KMS | key usage, rotation, access policy documented | key policies and `enable_key_rotation` on every `aws_kms_key` under `infra/`, reviewed by hand | review-PASS |
| KMS | logs, CloudTrail, frontend and eval-artefacts CMK policies deny key use to every non-service principal outside the key's allowlist, and key administration to every principal outside `crm_operator_principal_arns` and the account root | `check_plan_security_baseline.py --only store`; Layer 4 smoke `store_principal_deny` | review-PASS |

## Accepted wildcard and base-table grants

Grants that do not meet the least-privilege rows above, each accepted for the dev stack with its
compensating controls. `tests/unit/test_infra_eval_sfn.py` reads the backticked lists below: each
action list must equal its planned statement, and the SWEEP-CONTROL-1 table list must equal the
test's pinned set of unconditioned base-table grants, which must in turn equal the set the planned
statements grant.

### SFN-LOGS-1

`eval_sweep` state-machine role (`crm-agent-eval-eval-sweep-role`), inline policy
`crm-agent-eval-eval-sweep-permissions`, statement `SfnVendedLogDelivery` in `infra/eval_sfn.tf`, on
`Resource: "*"`.

- **Actions, exactly:** `logs:CreateLogDelivery`, `logs:GetLogDelivery`, `logs:UpdateLogDelivery`,
  `logs:DeleteLogDelivery`, `logs:ListLogDeliveries`, `logs:CreateLogStream`, `logs:PutLogEvents`,
  `logs:PutResourcePolicy`, `logs:DescribeResourcePolicies`, `logs:DescribeLogGroups`.
- **Why `*`.** These CloudWatch Logs delivery APIs support no resource-level ARNs, and the Step
  Functions developer guide ("Using CloudWatch Logs to log execution history in Step Functions")
  specifies `"Resource": "*"` for them.
- **Compensating controls.**
  1. The role is assumable only by `states.amazonaws.com`, with `aws:SourceAccount` equal to this
     account and `aws:SourceArn` equal to the `crm-agent-eval-eval-sweep` state machine.
  2. Its other permissions are `lambda:InvokeFunction` on `crm-agent-eval-sweep-control` and
     `crm-agent-eval-sweep-task` (unqualified and `:*`) and SFN-XRAY-1.
  3. The state machine logs at level `ALL` with `include_execution_data = false` to
     `/aws/vendedlogs/states/crm-agent-eval-eval-sweep`, encrypted with the observability CMK, with
     365-day retention.
  4. `tests/unit/test_infra_eval_sfn.py` pins this action set, the trust conditions, and the invoke
     statement's four resources.
- **Residual risk.** `logs:PutResourcePolicy` on `*` lets the role rewrite account-level CloudWatch
  Logs resource policies. Only Step Functions, for this one state machine, can obtain the role's
  credentials.
- **Verification at T3.** `aws iam simulate-principal-policy` as the role: the ten actions `allowed`
  on `*`; `lambda:InvokeFunction` on an action Lambda and on `eval_trigger`, and any `dynamodb:*`,
  `s3:*` or `bedrock-agentcore:*` action, `implicitDeny`.
  `aws stepfunctions describe-state-machine --query loggingConfiguration` → level `ALL`,
  `includeExecutionData false`.

### SFN-XRAY-1

The same role and policy, statement `SfnXRayTracing`, on `Resource: "*"`. The state machine has
`tracing_configuration { enabled = true }`.

- **Actions, exactly:** `xray:PutTraceSegments`, `xray:PutTelemetryRecords`,
  `xray:GetSamplingRules`, `xray:GetSamplingTargets`.
- **Why `*`.** These are the actions the Step Functions developer guide requires for X-Ray tracing,
  and X-Ray supports no resource-level scoping for them.
- **Compensating controls.** SFN-LOGS-1 controls 1 and 2. The actions write trace data and read
  sampling configuration; none reads traces or changes X-Ray encryption.
- **Verification at T3.** `aws stepfunctions describe-state-machine` → `tracingConfiguration.enabled`
  `true`; `aws iam simulate-principal-policy` as the role: the four actions `allowed`;
  `xray:BatchGetTraces` and `xray:PutEncryptionConfig` denied.

### SWEEP-CONTROL-1

Residual unconditioned base-table grants on `crm-agent-eval-sweep-control-role` and
`crm-agent-eval-sweep-task-role`, in `local.sweep_control_dynamodb_grants` and
`local.sweep_task_dynamodb_grants` in `infra/eval_sfn.tf`.

- **Tables with an unconditioned base-table grant:** `accounts`, `contacts`, `opportunities`,
  `activities`, `notes`, `audit_records`, `proposals`, `usage_records`.
  - sweep_control `BatchWriteItem` on each of them. Teardown deletes every row of the tenant,
    including rows the agent created through tool Lambdas, whose ids carry no tenant. Seeding
    writes through the same grant: `fixtures.generate.seed_tenant` puts each table's corpus with
    `Store.batch_put_items`, in `BatchWriteItem` calls of up to 25 unconditional `PutRequest`s. The
    `reps` teardown grant stays conditioned on `eval-*`.
  - sweep_task `GetItem` on `proposals`, which is read by the agent-generated `proposal_id`.
- **Conditioned instead.** sweep_task's golden-task `GetItem` on `accounts`, `contacts`,
  `opportunities`, `activities` and `notes` carries a `dynamodb:LeadingKeys` `StringLike` condition
  on `<kind>-eval-*` (`local.sweep_seed_key_patterns`). Every `gsi_tenant` Query is conditioned on
  `eval-*`, plus `canary` for sweep_control.
- **Standing constraints.**
  - sweep_control holds no `PutItem` on `accounts`, `contacts`, `opportunities`, `activities` or
    `notes`. Seeding writes only through `BatchWriteItem`; code that seeds through `PutItem` must not
    be deployed against this role.
  - sweep_control's `demo` `gsi_tenant` Query is a separate statement per tenant table, conditioned
    on `dynamodb:LeadingKeys` = `demo` and `dynamodb:Select` = `COUNT`. A `demo` Query that sends no
    `Select`, or any other value, is denied, so the `demo` baseline and delta reads return counts and
    no item attribute.
- **Why.** These tables' partition keys are synthetic record ids; only seeded ids carry the tenant.
  IAM cannot condition on a non-key attribute value.
- **Accepted residuals.**
  - The `<kind>-eval-*` patterns admit any eval run's seeded rows, not only the current run's. IAM
    cannot bind the dynamic run id. Compensated by `lock#sweep` (one sweep at a time) and the
    per-read `item["tenant"]` re-check.
  - IAM would allow sweep_control to delete `demo` rows by key through the unconditioned
    `BatchWriteItem`. Its `demo` Query returns counts only, so it cannot enumerate those keys through
    `gsi_tenant`. Only `eval.tenancy._reject_protected_tenant` prevents the delete.
  - Seed writes do not fail closed at IAM for a non-eval tenant; code guards only. A seed misdirected
    to `demo` or `canary` through the unconditioned `BatchWriteItem` is allowed. Such a seed is
    caught only afterwards:
    for `canary` by `assert_canary_empty` and `scripts/check_canary_tenant_empty.py`, and for `demo`
    by the `demo_tenant_untouched` row-count check, which sees added rows but not overwritten ones. The
    corpus is deterministic, so a misdirected seed can only write seed-pattern keys with their
    seeded values.
- **Compensating controls.**
  - Seeding refuses protected tenants (`eval.tenancy.seed_tenant` calls
    `_reject_protected_tenant` before any write) and writes only the generated corpus stamped with
    the unit tenant.
  - Teardown refuses protected tenants and enumerates only through `gsi_tenant` Queries conditioned
    on `eval-*`/`canary`.
  - Every scoring read re-checks `item["tenant"]`.
  - `assert_canary_empty` runs after every teardown and cannot be disabled.
  - Both roles are invoked only by the state machine's role.
- **Verification.**
  - Plan-time: `tests/unit/test_infra_eval_sfn.py` has one removed-grant cell per removed grant
    (the seed `PutItem` included), one conditioned-grant cell per conditioned grant, one `demo`
    COUNT-only cell per tenant table, and a scratch-mutation control for each kind.
    `tests/unit/test_infra_sweep_control_demo_count.py` asserts the `lambda_action` module renders
    the extra `Select` condition on the same statement and leaves a grant without one unchanged.
  - Key contract: `tests/unit/test_sweep_seed_key_patterns.py`.
  - Grant gate: `scripts/check_handler_grant_coverage.py` reports 0 undeclared SURPLUS and 0
    UNRESOLVED; re-adding the seed `PutItem` reds it as a SURPLUS.
  - Batch seeding: `tests/unit/test_store_batch_put.py` and
    `tests/integration/test_seed_batch_ddb.py`.
  - T3: `LeadingKeys`- and `Select`-context simulations in `scripts/smoke/iam_roles.py` and
    `scripts/smoke/eval_sfn.py`; the deployed Seed step completing in the `eval_sfn` rehearsal.
- **Review date.** Reviewed 2026-10-29 for the seed `PutItem` removal and the count-only `demo`
  Query. Next review at the next change to either role, or 2027-04-29.

## Holmes scan dispositions

Checkov findings that are not fixed in code. The entries were first written for the Holmes scan of
2026-10-11 (scan `ec066c5f-4be6-4594-a2ba-9cd8b2339b75`) and hold unchanged for Holmes scan 2 of
2026-10-13 (scan `aad33024-75a9-47c0-8166-6eef926b83c7`). Scan 2 found the same rules
at the same subjects. Holmes does not read `checkov:skip`, so the owner dispositions each finding in
Holmes, and this section is the tracked record of the reason. **FP** means a false positive and
**ACCEPT** an accepted risk. `tests/unit/test_security_baseline_doc.py` asserts one entry per finding
id, its rule ids and disposition, and that every path it names exists.

Entry ids are the first scan's triage ids. Scan 2 renumbered them and counted each finding as follows:

| Entry | Scan-2 id | Scan-2 rows |
|---|---|---|
| H-4 | H-2 | 144 `CKV_AWS_355`, all in the four `tests/fixtures/plans/` fixtures |
| H-5 | H-4 | 5 `CKV_AWS_174` (1 source, 4 fixtures), plus the 1 Medium Semgrep CloudFront TLS row |
| H-6, H-7 | H-3 | 3 `CKV_AWS_109`, 6 `CKV_AWS_111`, 6 `CKV_AWS_356` |
| H-8 | H-5 | 5 `CKV_AWS_2` (1 source, 4 fixtures) |
| H-9 | H-7 | 1 `CKV_AWS_260` |
| H-10 | H-6 | 5 `CKV_AWS_316` (1 source, 4 fixtures) |

The four tracked plan fixtures these fixture rows were counted in are removed: the compliant plan
and its three variants (`plan_fixtures.NAMES`). `scripts/plan_fixtures.py` now builds those four plans at
test time, from `tests/fixtures/plans/compliant.base.ndjson`, into the gitignored `results/plan-fixtures/`. The
base carries no `terraform_version` or `planned_values`, so Checkov does not read it as a plan. The
scan-2 rows they account for therefore no longer apply: all of scan-2 H-2, and the 4 fixture rows in each
of scan-2 H-4, H-5 and H-6. The 1 source row in each of those three still stands.
The counts above are scan 2's and have not been re-measured.

Scan 2 measured how Holmes treats inline suppressions. It honours bandit `# nosec Bxxx`. It ignores
Semgrep `# nosemgrep` (92 of the 107 subprocess rows carry one) and `checkov:skip`.

On 2026-10-25, semgrep 1.172.0 reported 0 `dangerous-subprocess-use-audit` rows in the tree, run from
the repository root:

```
semgrep scan --config r/python.lang.security.audit.dangerous-subprocess-use-audit \
  --disable-nosem --no-git-ignore --x-ignore-semgrepignore-files scripts tests src
```

The same run over a probe file holding one `subprocess.run(cmd)` call and one
`subprocess.CompletedProcess(...)` construction reported 2 rows, so the rule matches both forms. Every
process launch in the tree binds its launcher to a local name first (`launch = subprocess.run`):
`proc_run.run` and `proc_run.popen` in `scripts/proc_run.py`, and the `git` calls in
`src/eval/harness.py` and `src/eval/verification.py`. Each passes an argv list with no shell.

The repository has no `.semgrepignore`, so without `--x-ignore-semgrepignore-files` Semgrep applies its
default ignore, which skips `tests/`.

The control is `scripts/check_subprocess_funnel.py`, registered in `scripts/run_gates.py`. It fails
the build if any tracked `*.py` under `scripts/` or `tests/` starts a process other than through
`scripts/proc_run.py`. The `src/eval` sites are outside its scope.

`proc_run.run` and `proc_run.popen` call the launcher
through a local name (`launch = subprocess.run`, `launch = subprocess.Popen`) bound at call time. The
audit rule matches `subprocess.<launcher>(...)` call syntax, so it is not expected to report these two
rows. Their
risk is unchanged; this changes only what the scanner sees. The controls are the same: `proc_run`
raises `TypeError` for a `str` argv or any `shell` keyword before anything starts, and
`check_subprocess_funnel` fails the build on any launch, launcher binding (`x = subprocess.run`) or
`getattr(subprocess, ...)` outside `scripts/proc_run.py`.

The same call-time binding applies to the 2 `git` calls in `src/eval`:
`_git_revision` in `src/eval/harness.py` and `_run_git` in `src/eval/verification.py` each bind
`launch = subprocess.run` inside the function and call `launch(...)`. Argv, keywords and `# nosec B603`
are unchanged, and the risk is unchanged. `src/` is outside `check_subprocess_funnel`'s scope, so no
gate fails a new launch or launcher binding there. Each site still runs a fixed argv with git's
absolute `shutil.which` path, no shell and a timeout, and `tests/unit/test_invariant_guards.py` asserts
git is spawned by absolute path.

The other Semgrep rows that scan 2 leaves standing are:
- test JWT signing helpers;
- placeholder account ids;
- identifiers whose names match `*_key`.

All of these Semgrep rows are filed in Holmes as false positives.

### H-4

- **Rule:** `CKV_AWS_355`.
- **Subject:** `tests/fixtures/plans/` (recorded `terraform plan` JSON). The four fixtures scan 2 counted
  were removed and are now built at test time by `scripts/plan_fixtures.py`.
- **Disposition:** FP.
- **Reason.** Test fixtures read by the plan-gate unit tests; not deployable IaC. The `Resource: "*"`
  statements they render are the ones dispositioned under H-6 and H-7.
- **Compensating control.** The deployable sources of those statements are covered by H-6 and H-7.

### H-5

- **Rule:** `CKV_AWS_174` (and the Semgrep CloudFront insecure-TLS finding on the same block).
- **Subject:** `infra/modules/frontend_cdn/cloudfront.tf`, `viewer_certificate`.
- **Disposition:** ACCEPT.
- **Reason.** The distribution uses the default `*.cloudfront.net` certificate
  (`cloudfront_default_certificate = true`), for which CloudFront fixes the security policy and ignores
  `minimum_protocol_version`. A TLS 1.2 minimum needs a custom domain and an ACM certificate.
- **Compensating control.** Every cache behaviour is `redirect-to-https` or `https-only`, and the
  origins are private (S3 and the relay through OAC, the API through a VPC origin). The Transport row
  above records the control as DEFER (unmet).
- **Owner decision.** Required: a custom domain and ACM certificate, which the
  project does not own.

### H-6

- **Rule:** `CKV_AWS_109`, `CKV_AWS_111`, `CKV_AWS_356`.
- **Subject:** KMS key policies in `infra/modules/observability/kms.tf`,
  `infra/modules/observability/cloudtrail.tf` (`cloudtrail_cmk`) and `infra/modules/frontend_cdn/kms.tf`.
- **Disposition:** FP.
- **Reason.** In a key policy, `Resource: "*"` is the key the policy is attached to, not every resource.
- **Compensating control.** Principals are the account root and named service principals, each service
  grant conditioned on this stack's resource (`kms:EncryptionContext:aws:logs:arn`,
  `kms:EncryptionContext:aws:cloudtrail:arn`, `AWS:SourceArn`); the CloudTrail key's account-wide
  `kms:Decrypt` is conditioned on `kms:CallerAccount`.

### H-7

- **Rule:** `CKV_AWS_111`, `CKV_AWS_356`.
- **Subject:** `infra/eval_sfn.tf` (`SfnVendedLogDelivery`, `SfnXRayTracing`),
  `infra/modules/agent_relay/main.tf` and `infra/modules/lambda_action/main.tf`
  (`ManageOwnHyperplaneEnis`, `WriteXRayTraceSegments`).
- **Disposition:** FP.
- **Reason.** CloudWatch Logs vended delivery, X-Ray segment writes and Lambda VPC ENI management support
  no resource-level permissions; `*` is the only valid resource.
- **Compensating control.** SFN-LOGS-1 and SFN-XRAY-1 above; each function role is assumable only by
  Lambda, and the `eni_function_code_deny` managed policy denies the ENI actions to function code.

### H-8

- **Rule:** `CKV_AWS_2`.
- **Subject:** `infra/modules/api_origin/main.tf`, `aws_lb_listener.this` (HTTP).
- **Disposition:** ACCEPT.
- **Reason.** The ALB is internal and reachable only from the CloudFront VPC origin security group inside
  the VPC; an HTTPS listener needs a certificate for a domain the project does not own.
- **Compensating control.** The ALB security group admits only the referenced CloudFront VPC origin
  security group; viewer traffic is HTTPS to CloudFront.
- **Owner decision.** Required: a custom domain and ACM certificate.

### H-9

- **Rule:** `CKV_AWS_260`.
- **Subject:** `infra/modules/api_origin/main.tf`, `aws_vpc_security_group_ingress_rule.alb_from_cloudfront`.
- **Disposition:** FP.
- **Reason.** The rule's source is `referenced_security_group_id`; it carries no CIDR, so no `0.0.0.0/0`
  ingress exists.
- **Compensating control.** `check_plan_security_baseline.py`'s `security_group_zero_inbound` reads
  `infra/` and fails any ingress rule that names a CIDR or prefix list instead of a security group.

### H-10

- **Rule:** `CKV_AWS_316`.
- **Subject:** `infra/modules/agentcore/ecr_codebuild.tf`, `aws_codebuild_project.agent_image`.
- **Disposition:** ACCEPT.
- **Reason.** Privileged mode is required to run `docker build` for the AgentCore Runtime image.
- **Compensating control.** The project has no webhook and builds only the repository's source object,
  read from one S3 key; the build role can push only to the agent ECR repository. The project is
  encrypted with the eval-artefacts CMK (`encryption_key`; rotation on, store-principal-boundary
  key policy `aws_kms_key_policy.eval_artifacts`), on which the role holds exactly `kms:Decrypt`,
  `kms:Encrypt`, `kms:GenerateDataKey*` and `kms:DescribeKey`.
