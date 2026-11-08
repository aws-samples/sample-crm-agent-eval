# `frontend_cdn` module

Content delivery for the CRM frontend: a private S3 origin reached only through CloudFront Origin
Access Control (OAC), and a CloudFront distribution with three behaviours — the SPA (`/*`), the
private REST API (`/api/*`), and the agent relay (`/agent/*`).

See also: the [documentation map](../../../README.md#documentation), the
[runbook](../../../docs/runbook.md#store-principal-boundary) and the [security
baseline](../../../docs/security-baseline.md#h-5). Modules named here:
[`agent_relay`](../agent_relay/README.md), [`api`](../api/README.md).

## Contents

| File | Declares |
|---|---|
| `kms.tf` | customer-managed KMS key (CMK) and alias encrypting the origin bucket |
| `s3.tf` | the origin bucket (private, versioned, SSE-KMS, TLS-deny + OAC-only policy, S3 access logging) and a dedicated access-log bucket receiving S3, CloudFront and ALB access logs |
| `cloudfront.tf` | two OACs (S3 and `lambda`-type for the relay), the two distribution-scoped Lambda permissions for the relay, the managed cache/origin-request policies read by data source, `/agent/*`'s own origin-request policy, the security-headers response-headers policy (with the CSP), two viewer-request functions, and the distribution |
| `functions/api_path_rewrite.js` | `/api/*` viewer-request function, rendered with the stage name by `templatefile` |
| `functions/spa_history_fallback.js` | default-behaviour viewer-request function rewriting extensionless paths to the SPA entry point |

## Inputs

| Variable | Default | Notes |
|---|---|---|
| `name_prefix` | `""` | prefix applied to resource names |
| `data_classification` | `"confidential"` | value of the `data-classification` tag |
| `api_gateway_domain_name` | required | `module.api.private_origin_domain_name` (`<api-id>-<vpce-id>.execute-api.<region>.amazonaws.com`). Used as the `Host` header API Gateway routes on, not as a name CloudFront resolves |
| `api_origin_vpc_origin_id` | required | `module.api_origin.vpc_origin_id`. Validated to look like a VPC origin id (`vo_…`), so an ALB ARN or hostname is refused at plan time |
| `agent_relay_function_url_domain` | required | `module.agent_relay.function_url_domain`. Validated to contain no `/`: a custom origin's `domain_name` rejects a scheme or path, so passing `function_url` fails at plan time |
| `agent_relay_function_name` | required | `module.agent_relay.function_name`, for the distribution-scoped invoke permissions |
| `api_gateway_stage_name` | required | the API's stage, consumed by the `/api/*` path rewrite |
| `alb_access_log_prefix` | required | key prefix `modules/api_origin`'s internal ALB writes access logs under. Must be the same value passed to `module.api_origin` — the root passes one local to both. Must be non-empty and must not contain `AWSLogs` |
| `log_retention_days` | `90` | lifecycle expiration on the access-log bucket |
| `origin_noncurrent_version_retention_days` | `30` | days a superseded origin object version is kept; current objects never expire |
| `operator_principal_arns` | required | IAM role or user ARNs: the only non-service principals allowed frontend-key use and writes into the origin bucket. Non-empty; no `*`, `?` or account root |
| `administrator_principal_arns` | required | IAM role or user ARNs, or the account root, allowed to administer the frontend key and the origin bucket. Non-empty; no `*` or `?` |
| `caller_principal_arn` | required | role or user ARN of the principal running the plan; the key and origin policies refuse to plan unless it is a listed administrator other than the root |
| `csp_script_hashes` | required | CSP hashes (`sha256-<base64>`, unquoted) of the SPA's inline scripts, added to `script-src`. The root derives them at plan time from the attribute-less `<script>` elements in `frontend/index.html`; `tests/unit/test_infra_csp.py` checks the derived value against the hash a browser computes |
| `csp_connect_src_extra` | required | bare `https://<host>` origins added to `connect-src`: the regional `cognito-idp` endpoint and the hosted-UI domain. No path, port or wildcard |

## Outputs

`distribution_id`, `distribution_domain_name`, `distribution_arn`, `origin_bucket_name`,
`origin_bucket_arn`, `logs_bucket_name`, `kms_key_arn`.

## The three behaviours

| Behaviour | Origin | Cache policy | Identity header reaching the origin | Notes |
|---|---|---|---|---|
| default (`/*`) | private S3 via OAC | `Managed-CachingOptimized` | none | static build assets carry no per-caller identity |
| `/api/*` | the private REST API, through `modules/api_origin`'s VPC origin | `Managed-CachingDisabled` | `Authorization`, via `Managed-AllViewerExceptHostHeader` | path rewritten from `/api/…` to `/<stage>/…` |
| `/agent/*` | the relay's `AWS_IAM` Function URL, signed by a `lambda`-type OAC | `Managed-CachingDisabled` | `X-Crm-Authorization` and `x-amz-content-sha256`, via this module's own origin-request policy | `compress = false` |

Both identity-bearing behaviours pair "forward the identity header" with "cache disabled": a cached
response keyed on a per-caller header would be replayed to a different caller.

All three behaviours attach this module's `aws_cloudfront_response_headers_policy.security_headers`
(named `<name_prefix>frontend-security-headers`). It carries the same headers as the AWS-managed
`Managed-SecurityHeadersPolicy`, with `X-Frame-Options: DENY`, plus an enforced
`Content-Security-Policy`. Every header overrides any value from the origin. The policy only adds
headers, so it does not buffer the `/agent/*` SSE stream.

The CSP is:

```
default-src 'self'; script-src 'self' '<csp_script_hashes>…'; style-src 'self' 'unsafe-inline';
img-src 'self' data:; font-src 'self' data:; connect-src 'self' <csp_connect_src_extra>…;
frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'
```

`'unsafe-inline'` appears only in `style-src`, because the UI library injects `<style>` elements at
runtime. No directive allows `'unsafe-eval'`. The script hash follows `frontend/index.html`, so an
edit to the inline script needs a `terraform apply` as well as a frontend upload. Until then browsers
refuse the script, and the page renders in the default theme until the bundle loads. A new direct call from the SPA to another origin also needs that
origin added to `csp_connect_src_extra`. Without it, the browser blocks the request and reports a CSP
violation in the console.

### `/api/*` path rewrite

CloudFront forwards the viewer path unchanged, and the API serves routes beneath its stage
(`/<stage>/<tool_name>`). `aws_cloudfront_function.api_path_rewrite` (runtime `cloudfront-js-2.0`,
`viewer-request`, `/api/*` only) replaces the leading `/api` segment with `/${api_gateway_stage_name}`.
Method, query string, body and every header pass through unmodified. Without it every `/api/*` call
is answered `404 {"message":"Not Found"}`.

### `/api/*` origin is a VPC origin

The private REST API's hostname resolves only inside the VPC. A CloudFront VPC origin routes to the
internal ALB **by ARN** and performs no DNS lookup on the origin domain name:

```hcl
origin {
  domain_name = var.api_gateway_domain_name   # Host header only
  origin_id   = "api-gateway-origin"

  vpc_origin_config {
    vpc_origin_id = var.api_origin_vpc_origin_id
  }
}
```

- **`domain_name` must stay the API's hostname, never the ALB's.** `Host: internal-*.elb.amazonaws.com`
  is not routed by API Gateway. The `<api-id>-<vpce-id>.execute-api.…` form works without a `Host`
  override because the API is associated with the endpoint.
- **`/api/*` must not forward the viewer's `Host`.** CloudFront sends `Host: <origin domain_name>`
  only while the origin-request policy excludes `Host`, which is why this behaviour uses
  `Managed-AllViewerExceptHostHeader`. `Managed-AllViewer` breaks routing.
- Ports, protocol policy and origin SSL protocols are not set here: `vpc_origin_config` accepts none
  of them. They live on `aws_cloudfront_vpc_origin` in `modules/api_origin`.

`tests/unit/test_infra_cdn.py` pins all three, and that the origin declares `vpc_origin_config` and
not `custom_origin_config`.

### First deploy takes two applies

A VPC origin's ingress is controlled by `CloudFront-VPCOrigins-Service-SG`, a service-managed security
group AWS creates only when the VPC origin is first applied. `api_origin_ingress_security_group_ids`
therefore cannot be known in advance: seed it with any valid security-group id in the VPC, apply, read
back the id AWS created, set it, and apply again.

Between the two applies the ALB admits nothing and `/api/*` fails at the edge. That is expected. Do
not widen ingress to the VPC CIDR to work around it — the variable's validation refuses that. The
read-back command is in the commentary above `api_origin_ingress_security_group_ids` in
`infra/terraform.tfvars.example`, which is the authority for this procedure.

### `/agent/*` origin is the relay, locked to this distribution

The relay's Function URL is `authorization_type = AWS_IAM`. `aws_cloudfront_origin_access_control.agent_relay`
(`origin_access_control_origin_type = "lambda"`, `signing_behavior = "always"`,
`signing_protocol = "sigv4"`) signs every origin request, and an unsigned request to the Function
URL's own host is refused by Lambda before the relay runs.

The signature is admitted by two permissions declared in this module, both for principal
`cloudfront.amazonaws.com` with `source_arn` set to this distribution:

- `aws_lambda_permission.agent_relay_function_url` — `lambda:InvokeFunctionUrl`, conditioned on
  `function_url_auth_type = "AWS_IAM"`;
- `aws_lambda_permission.agent_relay_invoke_function` — `lambda:InvokeFunction`.

Without them the URL answers 403 to CloudFront. They live here rather than in `agent_relay` because
they are scoped to the distribution ARN.

Header handling:

- `signing_behavior = "always"` overwrites the viewer's `Authorization` header, so the Cognito JWT
  travels in **`X-Crm-Authorization`**. `no-override` is not an alternative: CloudFront then does not
  sign, and the `AWS_IAM` URL refuses everything.
- The viewer must send `x-amz-content-sha256` (hex SHA-256 of the exact body) on every `POST`; Lambda
  rejects unsigned payloads. The SPA computes it with `crypto.subtle.digest`.
- `/agent/*` uses this module's own origin-request policy (`allExcept ["host"]`: every viewer header
  except `Host`, all cookies, all query strings) rather than the managed equivalent, so
  `tests/unit/test_infra_cdn.py` can assert that `X-Crm-Authorization` and `x-amz-content-sha256` are
  forwarded.
- `compress = false` on this behaviour only. CloudFront compresses by buffering the response, which
  would hold every SSE frame until the turn ended. `tests/unit/test_infra_cdn.py` asserts it.

OAC is an origin lock, not authentication: it establishes that a request came through this
distribution. Identity comes from the JWT alone, verified in the relay.

### Diagnosing `/agent/*` failures

| Symptom | Check first |
|---|---|
| `403` from the edge on every turn, no relay log entry | one of the two Lambda permissions is missing, or its `source_arn` names a different distribution |
| `403` with `x-amz-content-sha256` absent from the request | the viewer did not send the body hash |
| `401` from the relay on every turn | the JWT did not arrive: the SPA sends it in `Authorization` (overwritten by OAC), or the origin-request policy stopped forwarding `X-Crm-Authorization` |
| turn completes, but all output arrives at once | `compress` set to `true` on `/agent/*` |

The first three look the same from the browser. Read the relay's CloudWatch log group: no entry means
the request never reached it (rows 1-2); an entry answering 401 means it did (row 3).

## "Private bucket" means private to *direct S3 access*, not to the distribution

The origin bucket is private, SSE-KMS encrypted, Block-Public-Access-enabled, and readable only by this
distribution's OAC principal. That restricts who can read S3 directly. It says nothing about who can
read through CloudFront.

The default behaviour (`/*`) targets the S3 origin with no path pattern and no authorization, so
**every object in the origin bucket is anonymously fetchable at `https://<distribution>/<key>` and is
CDN-cached.** That is intended for a public SPA's build output, which is all this bucket holds.
Anything else written into it becomes public; non-public objects need their own bucket, not a prefix
in this one.

## S3 bucket security

Origin bucket:

- Block Public Access: all four flags `true`.
- `BucketOwnerEnforced` ownership controls (ACLs disabled).
- The bucket policy's only `Allow` grants `s3:GetObject` to `cloudfront.amazonaws.com`, conditioned on
  `AWS:SourceArn` equal to this distribution's ARN.
- `aws:SecureTransport = false` is denied for every action and principal.
- `DenyObjectWritesToNonOperators` denies object writes, deletes and tagging (`PutObject`,
  `DeleteObject`, `AbortMultipartUpload` and the tagging actions) unless `aws:PrincipalArn` is in
  `operator_principal_arns`. `DenyBucketAdministrationToNonOperators` denies the bucket policy,
  encryption, versioning, lifecycle, logging, public-access, ownership, ACL, tag, website, CORS and
  similar settings, and `DeleteBucket`, unless `aws:PrincipalArn` is in
  `administrator_principal_arns`. Reads are not denied, because the distribution serves the SPA
  without authorization by design.
- SSE-KMS with this module's CMK; versioning enabled; S3 access logging to the `logs` bucket. The key
  policy keeps the account-root statement and `AllowCloudFrontOACDecrypt`, denies key use
  (`kms:Decrypt`, `Encrypt`, `GenerateDataKey*`, `ReEncrypt*`, `CreateGrant`) to every non-service
  principal outside `operator_principal_arns` (CloudFront's OAC decrypt runs as a service principal
  and is not denied), and denies key administration (policy, deletion, disabling, grant revocation,
  rotation, alias, tag and description changes) outside `administrator_principal_arns`.
- The key policy and the origin bucket policy refuse to plan unless `caller_principal_arn` is a listed
  administrator other than the account root.
- Lifecycle: `noncurrent_version_expiration` (`origin_noncurrent_version_retention_days`) and
  `abort_incomplete_multipart_upload` only. No current-version `expiration`, so the served build never
  expires; a superseded build is kept that long for rollback.

Access-log bucket:

- SSE-S3 (`AES256`), not the CMK. Both S3 server access logging and ELB access logging require SSE-S3
  on the destination bucket; do not change it to KMS.
- Block Public Access, versioning, TLS-deny policy.
- Lifecycle: `expiration`, `noncurrent_version_expiration` (both `log_retention_days`) and
  `abort_incomplete_multipart_upload`. On a versioned bucket a bare `expiration` only writes a delete
  marker, so noncurrent versions would otherwise be retained indefinitely.

The `data-classification` tag is applied explicitly on the CMK, both buckets and the distribution; the
other mandatory tags (`service`, `environment`, `owner`, `cost-center`, `AppName`) come from the root provider's
`default_tags`.

## Access-log delivery

`aws_s3_bucket_logging` configures logging; delivery also requires the destination bucket to admit the
log-delivery principal. The logs bucket policy carries:

| Statement | Principal | Producer |
|---|---|---|
| `AllowS3ServerAccessLogDelivery` | `logging.s3.amazonaws.com` | the origin bucket's S3 access logs |
| `AllowAlbAccessLogDelivery` | `logdelivery.elasticloadbalancing.amazonaws.com` | `modules/api_origin`'s internal ALB |

The ALB statement:

- uses the ELB service principal, not the per-Region ELB account id (a unit test refuses the account
  id form);
- spells out this account's id in the resource ARN (`<logs bucket>/<prefix>/AWSLogs/<account>/*`) and
  adds an `aws:SourceArn` condition limiting delivery to this account and Region;
- is required for apply to succeed: ELB writes an `ELBAccessLogTestFile` when access logging is
  enabled, and without the grant `aws_lb` creation fails with `Access Denied for bucket`.

The plan-time control `s3_access_log_delivery_grant` in `scripts/check_plan_security_baseline.py`
asserts these grants, and that every `aws_lb` with `access_logs { enabled = true }` has an ELB grant in
the tree (and that no ELB grant exists without an ALB using it).

Do not fix a delivery problem by changing a bucket's `object_ownership`. The origin bucket's
`BucketOwnerEnforced` is a security property unrelated to delivery, and a unit test fails if it is
relaxed.

CloudFront standard (legacy) logging delivers via an ACL grant to the `awslogsdelivery` canonical user,
which no policy statement can express; it is registered in `PLAN_TIME_INEXPRESSIBLE` in that script.
Delivery itself is checked against the deployed stack after a real request:

```bash
.venv/bin/python3 -m scripts.smoke.access_log_delivery --outputs results/outputs.json
```

An object under a prefix is a `pass`; an empty prefix is `defer` (delivery is asynchronous). The
module's docstring lists the Terraform outputs and IAM permissions it needs.

## Teardown

Both buckets (`origin` and `logs`) set `force_destroy = true`. Both are versioned and fill on their own,
so without it `terraform destroy` aborts with `BucketNotEmpty`.

- A destroy **deletes every object version in both buckets without prompting.** The origin's content
  is the frontend build, reproducible via `scripts/deploy.sh`. The `logs` bucket's access-log history
  is not reproducible — copy it out first if it is needed.
- Do not copy the flag onto a bucket holding data anyone would miss.
- The CMK enters its pending-deletion window on destroy and is billed until the window elapses.

The `teardown_force_destroy` control in `scripts/check_plan_security_baseline.py` fails if a log or
artefact bucket lacks the flag, if a data bucket carries it, or if a bucket is unclassified. A bucket
added to this module must be added to `BUCKET_TEARDOWN_DISPOSITION` in that script as `required` or
`forbidden`.

```bash
.venv/bin/python3 scripts/check_plan_security_baseline.py \
  --plan "$(.venv/bin/python3 scripts/plan_fixtures.py compliant)"
.venv/bin/python3 -m pytest tests/unit/test_plan_security_baseline.py -k TeardownForceDestroy
```

## Viewer TLS — what "1.2 minimum" actually means here

**`minimum_protocol_version` is not set on this distribution's `viewer_certificate` block, and no value
would take effect if it were.** CloudFront honours `minimum_protocol_version` only with a **custom**
certificate (`acm_certificate_arn` or `iam_certificate_id`). With
`cloudfront_default_certificate = true` (this module's configuration — no custom domain), AWS fixes the
effective policy to `TLSv1`, and Terraform silently ignores any `minimum_protocol_version` supplied
alongside it.

Real clients negotiate TLS 1.2 or 1.3 in practice, but this module cannot claim an *enforced* 1.2
floor. Enforcing one requires attaching a custom domain with an ACM certificate and setting
`minimum_protocol_version = "TLSv1.2_2021"` on `viewer_certificate` — a configuration change to this
module.

How the verification layers report it, so no artifact reads as if the control were live:

- `scripts/check_plan_security_baseline.py` does not assert it (recorded in `PLAN_TIME_INEXPRESSIBLE`);
- `scripts/smoke/cloudfront.py` reports its `configured_tls_minimum` check as `defer` with reason
  `missing_custom_acm_certificate` — never `pass`, and never `skip`;
- `scripts/verify_security_baseline.sh` reports the same gap as `DEFER`.

## Limitations

- **No WAF.** No `aws_wafv2_web_acl` is created. A web ACL could attach to this distribution (scope
  `CLOUDFRONT`, set `web_acl_id`) or to the REST API stage (scope `REGIONAL`).
- **Viewer TLS minimum is not enforced at 1.2** — see above.
- **`/agent/*` header forwarding and no-cache posture are asserted from source only**
  (`tests/unit/test_infra_cdn.py`). `scripts/smoke/cloudfront.py` resolves each behaviour's real origin
  from the deployed distribution (`/agent/*` from the `agent_relay_function_url` output) and asserts
  the AgentCore data-plane host is absent from every origin, but no live check asserts that
  `X-Crm-Authorization` reaches the relay or that `/agent/*` responses are not cached.
  `scripts/smoke/integration_ui_api_ddb.py` covers `/api/*`: an authenticated read answers `200`, the
  same read without the header answers `401`, and two identical reads both report an `X-Cache` miss.

## What this module does not own

- [`infra/modules/api/`](../api/README.md), `infra/modules/api_origin/` and [`infra/modules/agent_relay/`](../agent_relay/README.md) — their values
  arrive as input variables. The exception is the relay's two Lambda permissions, declared here because
  they are scoped to this distribution's ARN.
- Plan-time assertion of the security baseline — `scripts/check_plan_security_baseline.py`.
- The frontend build and its upload to the origin bucket — `scripts/deploy.sh`.

## Validating

`infra/main.tf` calls this module, so `cd infra && terraform validate` exercises it. Its `versions.tf`
pins the same `hashicorp/aws` release as the root, so it also validates standalone:

```bash
cd infra/modules/frontend_cdn
terraform init -backend=false
terraform validate
```

The module cannot be planned offline: it reads three AWS-managed CloudFront policies by name and the
caller's account id. Its behaviour properties are asserted from the HCL instead:

```bash
.venv/bin/python3 -m pytest tests/unit/test_infra_cdn.py
```
