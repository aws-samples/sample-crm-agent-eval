# `observability` module

CloudWatch Logs KMS key, a CloudTrail trail with a secured S3 destination, and the eval metric
namespace passthrough. The module depends on no sibling module. `infra/main.tf` declares
`module "observability"` early because `cloudwatch_logs_kms_key_arn` feeds other modules' log
groups.

See also: the [documentation map](../../../README.md#documentation), the
[runbook](../../../docs/runbook.md#store-principal-boundary) and the [security
baseline](../../../docs/security-baseline.md#h-6). Modules named here:
[`agent_relay`](../agent_relay/README.md), [`agentcore`](../agentcore/README.md),
[`api`](../api/README.md), [`lambda_action`](../lambda_action/README.md), [`vpc`](../vpc/README.md).

## Resources

- **`aws_kms_key.logs`** (`kms.tf`): customer-managed key for CloudWatch Logs encryption, rotation
  on, 30-day deletion window. The key policy grants the `logs.<region>.amazonaws.com` principal
  `kms:Encrypt`, `kms:Decrypt`, `kms:ReEncrypt*`, `kms:GenerateDataKey*` and `kms:Describe*`, scoped by
  `kms:EncryptionContext:aws:logs:arn`, per
  [Encrypt log data in CloudWatch Logs using AWS KMS](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/encrypt-log-data-kms.html).
  The same key encrypts every project Lambda's environment variables. Key use (`kms:Decrypt`,
  `Encrypt`, `GenerateDataKey*`, `ReEncrypt*`, `CreateGrant`) is denied to every non-service principal
  outside `logs_key_user_principal_arns`, so a Lambda whose execution role is not listed fails at cold
  start. Use by CloudWatch Logs and ECR as service principals is not denied.
- **`aws_cloudtrail.this`** (`cloudtrail.tf`): multi-region trail with global service events,
  management events only, no data events, no Insights. Every region delivers to the one
  destination bucket and CMK; the bucket policy and key policy condition on the trail ARN, which
  carries the home region only.
- **`aws_s3_bucket.cloudtrail`**: the trail's destination. SSE-KMS with a dedicated key
  (`aws_kms_key.cloudtrail`, rotation on, scoped by `kms:EncryptionContext:aws:cloudtrail:arn`, per
  [Configure AWS KMS key policies for CloudTrail](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/create-kms-key-policy-for-cloudtrail.html)),
  Block Public Access, a TLS-only deny, versioning, and lifecycle expiry at
  `cloudtrail_s3_log_retention_days`. The bucket policy follows
  [S3 bucket policy for CloudTrail](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/create-s3-bucket-policy-for-cloudtrail.html):
  `s3:GetBucketAcl` and `s3:PutObject` (with `bucket-owner-full-control`) for
  `cloudtrail.amazonaws.com`, scoped to this trail by `aws:SourceArn`. Two Deny statements follow:
  object reads, lists and writes are denied to every principal outside `operator_principal_arns`,
  except requests that a service makes as its own service principal (CloudTrail's delivery), and
  bucket administration is denied outside `administrator_principal_arns`. The key's use is denied
  to every non-service principal outside `operator_principal_arns`, so decrypting delivered log
  files is operator-only.
- **Key administration** on both keys (policy, deletion, disabling, grant revocation, rotation,
  alias, tag and description changes) is denied outside `administrator_principal_arns`. Both keys and
  the CloudTrail bucket policy refuse to plan unless `caller_principal_arn` is a listed administrator
  other than the account root.
- **`aws_s3_bucket.cloudtrail_access_logs`**: receives the CloudTrail bucket's S3 server access logs.
  SSE-S3, because S3 server access logging cannot deliver to an SSE-KMS bucket. `BucketOwnerEnforced`
  (ACLs disabled). Its policy allows `logging.s3.amazonaws.com` to `s3:PutObject`, scoped to this
  account and the CloudTrail bucket, and denies non-TLS requests.
- Both buckets' lifecycle rules expire noncurrent versions after `cloudtrail_s3_log_retention_days`
  too (on a versioned bucket `expiration` alone only writes a delete marker) and abort incomplete
  multipart uploads after 7 days.
- **`aws_cloudwatch_log_group.cloudtrail`** (`logs.tf`) and its delivery role: created only when
  `enable_cloudtrail_cloudwatch_logs = true`. Retention `log_retention_days`, encrypted with
  `aws_kms_key.logs`. The root leaves it off, and no metric filter or alarm exists in `infra/`;
  `CKV2_AWS_10` is skipped inline on the trail.

## Inputs

| Variable | Default | Notes |
|---|---|---|
| `name_prefix` | `""` | Resource name prefix. |
| `data_classification` | `confidential` | `data-classification` tag value. |
| `log_retention_days` | `365` | CloudWatch Logs retention for the optional CloudTrail log group; minimum 365 (validated). |
| `cloudtrail_s3_log_retention_days` | `365` | Lifecycle expiry for both CloudTrail buckets. |
| `enable_cloudtrail_cloudwatch_logs` | `false` | Also deliver the trail to CloudWatch Logs. |
| `eval_metric_namespace` | `CrmAgentEval/Eval` | Reserved CloudWatch namespace. No code under `src/` publishes custom metrics; eval measurements are written as files (`matrix.json`/`.md`/`.csv`). |
| `tags` | `{}` | Added to the provider's `default_tags`. |
| `logs_key_user_principal_arns` | required | IAM role or user ARNs allowed logs-key use: every execution role whose Lambda environment the key encrypts, plus the operators. Non-empty; no `*`, `?` or account root. |
| `operator_principal_arns` | required | IAM role or user ARNs allowed CloudTrail-key use and the CloudTrail bucket's data plane. Same validation. |
| `administrator_principal_arns` | required | IAM role or user ARNs, or the account root, allowed to administer both keys and the CloudTrail bucket. Non-empty; no `*` or `?`. |
| `caller_principal_arn` | required | Role or user ARN of the principal running the plan; must be in `administrator_principal_arns` and not the root. |

## Outputs

| Output | Description |
|---|---|
| `cloudwatch_logs_kms_key_arn` | Logs key ARN. The root passes it to every module that declares a log group: `vpc` (`flow_log_kms_key_arn`), each `lambda_action` instantiation and `agent_relay` (`environment_kms_key_arn`), `api` (`access_log_kms_key_arn`) and `agentcore` (`kms_key_arn`). |
| `cloudwatch_logs_kms_key_id` | Logs key id. |
| `cloudtrail_arn` | Trail ARN. |
| `cloudtrail_s3_bucket_name` | Trail destination bucket. |
| `cloudtrail_access_logs_bucket_name` | Access-log bucket; read by the access-log delivery smoke check. |
| `cloudtrail_kms_key_arn` | CloudTrail bucket key ARN. |
| `eval_metric_namespace` | Echo of the namespace input. |

## Cost profile

Management-event logging on an account's first trail carries no per-event CloudTrail charge; an
additional trail's copy of management events is billed per event. This trail is multi-region, so
it delivers objects for every region with activity, and it is a paid copy in any account that
already has a multi-region trail. The remaining cost is S3 storage and requests. Each of these
settings, all off here, adds cost:

- Data events (S3 object-level, Lambda invocations) are billed per event, and routine DynamoDB and
  Lambda traffic can far exceed management-event volume.
- CloudTrail Insights is billed per analyzed event.
- `enable_cloudtrail_cloudwatch_logs = true` adds CloudWatch Logs ingestion and storage, billed
  per GB.

## Access-log delivery

`aws_s3_bucket_logging` being configured does not mean log objects arrive. The destination bucket
must also admit the log-delivery principal, either by a bucket-policy statement for
`logging.s3.amazonaws.com` (used here) or by an ACL grant to the log-delivery group. The plan-time
control `s3_access_log_delivery_grant` in `scripts/check_plan_security_baseline.py` asserts the
grant and accepts either mechanism.

Do not change a bucket's `object_ownership` to address a delivery problem. Delivery is governed by
the destination bucket; the origin bucket's `BucketOwnerEnforced` is a separate security property,
and a unit test fails if it is relaxed.

CloudFront standard logging delivers through an ACL grant to the `awslogsdelivery` canonical user,
which no policy statement can express; that control is registered in `PLAN_TIME_INEXPRESSIBLE` in the
same script. Actual delivery is checked against a deployed stack, after real requests:

```bash
.venv/bin/python3 -m scripts.smoke.access_log_delivery --outputs results/outputs.json
```

An object under a prefix is `pass`; an empty prefix is `defer`, because delivery is asynchronous.
The script's docstring lists the Terraform outputs and IAM permissions it needs.

## Teardown

Both S3 buckets set `force_destroy = true`. They are versioned and fill as soon as the trail
delivers, so without the flag `terraform destroy` stops with `BucketNotEmpty`.

- Destroying this module deletes every CloudTrail log object and access-log record without
  prompting. Copy out any trail history you need first.
- Both KMS keys enter a 30-day pending-deletion window on destroy.

The `teardown_force_destroy` control in `scripts/check_plan_security_baseline.py` reads
`infra/**/*.tf` and fails if a log/artefact bucket lacks the flag, if a data bucket carries it, or
if a bucket is unclassified. A bucket added here must be added to `BUCKET_TEARDOWN_DISPOSITION` as
`required` (log/artefact) or `forbidden` (holds data).

## Verification

```bash
.venv/bin/python3 scripts/check_plan_security_baseline.py \
  --plan "$(.venv/bin/python3 scripts/plan_fixtures.py compliant)"
.venv/bin/python3 -m pytest tests/unit/test_plan_security_baseline.py -k TeardownForceDestroy

cd infra/modules/observability && terraform init -backend=false && terraform validate
cd infra && terraform fmt -check -recursive && terraform validate
```

The `terraform` commands are syntax and type checks over the repository.
