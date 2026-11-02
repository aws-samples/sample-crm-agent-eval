# `eval_artifacts`

Dedicated S3 bucket for eval sweep artefacts, plus the writer grant for the sweep worker's role.
Objects are encrypted with a dedicated KMS key that the root composition declares and passes in.
Artefacts are read through presigned GET URLs.

See also: the [documentation map](../../../README.md#documentation), the
[runbook](../../../docs/runbook.md#store-principal-boundary) and the [security
baseline](../../../docs/security-baseline.md).

## Resources

- `aws_s3_bucket.this` (`force_destroy = true`).
- SSE-KMS default encryption with `var.kms_key_arn`.
- Block Public Access (all four settings), versioning, `BucketOwnerEnforced` ownership.
- Server access logging to `var.access_log_bucket_id` under `s3-access-logs/eval-artifacts/`.
- Lifecycle rule scoped to `<artifact_prefix>/`: current and noncurrent versions expire after
  `artifact_retention_days`. Keys outside the prefix (the bucket also holds the CodeBuild
  agent-source archive) are not expired. A second, whole-bucket rule expires noncurrent versions
  after `artifact_retention_days` and aborts incomplete multipart uploads after 7 days, with no
  current-version expiry.
- Bucket policy with four DENY statements and no ALLOW: `DenyInsecureTransport`
  (`aws:SecureTransport = false`), `DenyUnencryptedObjectUploads` (`PutObject` without `aws:kms`),
  `DenyObjectDataPlaneToNonProjectPrincipals` (object reads, lists and writes, unless
  `aws:PrincipalArn` is in `data_plane_principal_arns`) and `DenyBucketAdministrationToNonOperators`
  (bucket policy, encryption, versioning, lifecycle, logging, public-access, ownership, ACL, tag,
  replication, inventory, notification, Object Lock and similar settings, and `DeleteBucket`, unless
  `aws:PrincipalArn` is in `administrator_principal_arns`). The bucket policy refuses to plan unless
  `caller_principal_arn` is a listed administrator other than the account root.
- `aws_iam_role_policy.writer` on `var.writer_role_name`, when set.

The bucket name and ARN used inside the policies are derived from inputs in a `locals` block so both
policy documents render at plan time. The outputs read the real resource attributes.

## Inputs

| Variable | Required | Default | Notes |
|---|---|---|---|
| `name_prefix` | yes | — | Part of the bucket name. |
| `account_id` | yes | — | 12-digit account id, part of the bucket name. Passed in so the module plans offline. |
| `data_classification` | yes | — | The `data-classification` tag value. |
| `kms_key_arn` | yes | — | KMS key ARN (an alias or bare key id is rejected). Must be a key dedicated to this bucket. |
| `access_log_bucket_id` | yes | — | Destination for this bucket's access logs. |
| `artifact_prefix` | no | `sweeps` | The only prefix the writer grant covers. Lowercase alphanumeric and hyphens, no slashes. |
| `artifact_retention_days` | no | `30` | Lifecycle expiry, 1–90. |
| `writer_role_name` | no | `null` | A role to attach the writer policy to. `null` attaches no policy. |
| `data_plane_principal_arns` | yes | — | IAM role or user ARNs allowed the bucket's object reads, lists and writes. Non-empty; no `*`, `?` or account root. |
| `administrator_principal_arns` | yes | — | IAM role or user ARNs, or the account root, allowed bucket administration. Non-empty; no `*` or `?`. |
| `caller_principal_arn` | yes | — | Role or user ARN of the principal running the plan; must be in `administrator_principal_arns` and not the root. |

The root composition passes `null`: the writer is `sweep_control`, whose own `sweep_control_artifacts`
policy in `infra/eval_sfn.tf` grants the same prefix and key. It passes
`concat(local.eval_artifacts_principal_arns, var.crm_operator_principal_arns)` as the data-plane list
(the `eval_trigger`, `sweep_control` and CodeBuild roles, plus the operators), the operators plus the
account root as administrators, and the session's issuer ARN as the caller. The root also attaches
`aws_kms_key_policy.eval_artifacts` to the key, which denies key use outside the same data-plane list
(the key's own service use is exempt) and key administration outside the administrators.

## Outputs

| Output | Notes |
|---|---|
| `bucket_name` | Consumers read this rather than reconstructing the name. Wired to the worker as `EVAL_ARTIFACTS_BUCKET`. |
| `bucket_arn` | For a consumer composing its own policy. |
| `artifact_prefix` | The artefact key space. Wired to the worker as `EVAL_ARTIFACTS_PREFIX`. |

## Access model

The bucket policy grants nothing; access comes from identity policy only, and its principal Denies
refuse every other principal even when an identity policy allows it. A presigned URL carries its
signer's permissions, so the signer must be in `data_plane_principal_arns`. The writer grant is the
effective boundary on reads:

- `s3:PutObject` and `s3:GetObject` on `<bucket>/<artifact_prefix>/*`. No bucket-level resource.
- `kms:GenerateDataKey` and `kms:Decrypt` on `var.kms_key_arn`. An SSE-KMS `PutObject` fails without
  `GenerateDataKey`, and a `GetObject` without `Decrypt`.
- No `s3:ListBucket`: the role can address only keys it already knows.

Do not make this bucket a CloudFront origin or share it with another purpose. The frontend origin
bucket is readable through the distribution without authorization, so a presigned GET there would
not restrict anything.

## Teardown

`force_destroy = true`, classified `required` in `scripts/check_plan_security_baseline.py`'s
`BUCKET_TEARDOWN_DISPOSITION`. Artefacts are reproducible from a sweep with the same arguments and
`dataset_seed`, and they expire on the lifecycle rule. If the bucket comes to hold results cited
outside this repository, revisit that classification.

## Tests

`tests/unit/test_infra_eval_artifacts.py` asserts the bucket policy and the writer grant at plan
time. It also asserts that the root wires the module with no writer role and that `sweep_control`'s
own policy grants the writer the artefact prefix and key.

## Known limitations

- **Access-log delivery is not verified offline.** The bucket logs to the CDN module's access-log
  bucket, whose policy grants `logging.s3.amazonaws.com` `s3:PutObject`
  (`AllowS3ServerAccessLogDelivery`, scoped by `aws:SourceAccount`). The destination is known only at
  the root, so the plan-time gate cannot assert this module's link to it and reports it as deferred.
  `scripts/smoke/access_log_delivery.py` checks delivery after deploying: `pass` once log objects
  arrive under the expected prefix, `defer` while none have been delivered yet.
