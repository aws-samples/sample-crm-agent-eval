/**
 * CloudTrail for API activity plus its S3 log destination bucket: SSE-KMS with this module's own
 * CMK, Block Public Access, an `aws:SecureTransport` deny policy, versioning, S3 access logging to
 * a dedicated log-of-logs bucket, and the mandatory tags. The destination bucket and its CMK deny
 * data-plane access and key use to every non-service principal outside `operator_principal_arns`,
 * and administration to every principal outside `administrator_principal_arns`.
 *
 * Multi-region trail with global service events, management events only, no data events and no
 * CloudTrail Insights. Every region delivers to the one bucket and CMK below.
 */

# --- Access-log bucket for the CloudTrail destination bucket's own S3 access logs ---
# SSE-S3, not the CMK: S3 server access logging cannot deliver to an SSE-KMS bucket.
resource "aws_s3_bucket" "cloudtrail_access_logs" {
  # checkov:skip=CKV_AWS_145: S3 server access logging cannot deliver to an SSE-KMS bucket
  # checkov:skip=CKV_AWS_144: no cross-region replica of the audit buckets
  # checkov:skip=CKV2_AWS_62: no consumer reacts to log-object events
  bucket = "${var.name_prefix}cloudtrail-access-logs-${data.aws_caller_identity.current.account_id}"

  # Lets a destroy proceed while the versioned bucket still holds object versions.
  force_destroy = true

  tags = merge(var.tags, {
    "data-classification" = var.data_classification
  })
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cloudtrail_access_logs" {
  bucket = aws_s3_bucket.cloudtrail_access_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "cloudtrail_access_logs" {
  bucket = aws_s3_bucket.cloudtrail_access_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "cloudtrail_access_logs" {
  bucket = aws_s3_bucket.cloudtrail_access_logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

# ACLs disabled: S3 server access log delivery is granted by the bucket policy below.
resource "aws_s3_bucket_ownership_controls" "cloudtrail_access_logs" {
  bucket = aws_s3_bucket.cloudtrail_access_logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "cloudtrail_access_logs" {
  bucket = aws_s3_bucket.cloudtrail_access_logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.cloudtrail_s3_log_retention_days
    }

    # On a versioned bucket `expiration` only writes a delete marker; this expires the old versions.
    noncurrent_version_expiration {
      noncurrent_days = var.cloudtrail_s3_log_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_policy" "cloudtrail_access_logs" {
  bucket = aws_s3_bucket.cloudtrail_access_logs.id
  policy = data.aws_iam_policy_document.cloudtrail_access_logs_tls_deny.json
}

# Named for its TLS Deny, but it also carries the log-delivery Allow. The name is the plan address
# the committed plan fixture and consumers read, so it is unchanged.
data "aws_iam_policy_document" "cloudtrail_access_logs_tls_deny" {
  # S3 server access log delivery, scoped to the one source bucket in this module.
  statement {
    sid    = "AllowS3ServerAccessLogDelivery"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.cloudtrail_access_logs.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.cloudtrail.arn]
    }
  }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions   = ["s3:*"]
    resources = [aws_s3_bucket.cloudtrail_access_logs.arn, "${aws_s3_bucket.cloudtrail_access_logs.arn}/*"]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

# --- CloudTrail's own log destination bucket ---------------------------------

resource "aws_s3_bucket" "cloudtrail" {
  # checkov:skip=CKV_AWS_144: no cross-region replica of the audit buckets
  # checkov:skip=CKV2_AWS_62: no consumer reacts to log-object events
  bucket = "${var.name_prefix}cloudtrail-logs-${data.aws_caller_identity.current.account_id}"

  # Lets a destroy proceed while the versioned bucket still holds object versions.
  force_destroy = true

  tags = merge(var.tags, {
    "data-classification" = var.data_classification
  })
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.cloudtrail.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_ownership_controls" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_logging" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  target_bucket = aws_s3_bucket.cloudtrail_access_logs.id
  target_prefix = "s3-access-logs/cloudtrail/"
}

resource "aws_s3_bucket_lifecycle_configuration" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  rule {
    id     = "expire-trail-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.cloudtrail_s3_log_retention_days
    }

    # On a versioned bucket `expiration` only writes a delete marker; this expires the old versions.
    noncurrent_version_expiration {
      noncurrent_days = var.cloudtrail_s3_log_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Deny non-TLS requests; allow only the CloudTrail service principal to check the bucket ACL and
# write objects, scoped to this trail via `aws:SourceArn`, per AWS's documented CloudTrail bucket
# policy (https://docs.aws.amazon.com/awscloudtrail/latest/userguide/create-s3-bucket-policy-for-cloudtrail.html).
#
# The trail's ARN is computed from its name, region and account rather than read from
# `aws_cloudtrail.this.arn`, because the trail depends on this bucket policy.
locals {
  cloudtrail_name = "${var.name_prefix}crm-agent-eval-trail"
  cloudtrail_arn  = "arn:aws:cloudtrail:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:trail/${local.cloudtrail_name}"
}

resource "aws_s3_bucket_policy" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id
  policy = data.aws_iam_policy_document.cloudtrail_bucket.json

  lifecycle {
    precondition {
      condition     = contains(var.administrator_principal_arns, var.caller_principal_arn) && var.caller_principal_arn != "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
      error_message = "The caller running this plan is not an operator. Add its role or user ARN to crm_operator_principal_arns in terraform.tfvars, or run as a listed operator; nothing was applied."
    }
  }
}

data "aws_iam_policy_document" "cloudtrail_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions   = ["s3:*"]
    resources = [aws_s3_bucket.cloudtrail.arn, "${aws_s3_bucket.cloudtrail.arn}/*"]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid    = "AWSCloudTrailAclCheck"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.cloudtrail.arn]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.cloudtrail_arn]
    }
  }

  statement {
    sid    = "AWSCloudTrailWrite"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.cloudtrail.arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.cloudtrail_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }

  statement {
    sid    = "DenyObjectDataPlaneToNonProjectPrincipals"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:GetObjectAttributes",
      "s3:GetObjectVersionAttributes",
      "s3:GetObjectTagging",
      "s3:GetObjectVersionTagging",
      "s3:RestoreObject",
      "s3:ListBucket",
      "s3:ListBucketVersions",
      "s3:ListBucketMultipartUploads",
      "s3:ListMultipartUploadParts",
      "s3:PutObject",
      "s3:PutObjectTagging",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:DeleteObjectTagging",
      "s3:AbortMultipartUpload",
      "s3:PutObjectVersionTagging",
      "s3:DeleteObjectVersionTagging",
    ]
    resources = [aws_s3_bucket.cloudtrail.arn, "${aws_s3_bucket.cloudtrail.arn}/*"]

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = var.operator_principal_arns
    }

    condition {
      test     = "BoolIfExists"
      variable = "aws:PrincipalIsAWSService"
      values   = ["false"]
    }
  }

  statement {
    sid    = "DenyBucketAdministrationToNonOperators"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = [
      "s3:DeleteBucket",
      "s3:PutBucketPolicy",
      "s3:DeleteBucketPolicy",
      "s3:PutBucketVersioning",
      "s3:PutEncryptionConfiguration",
      "s3:PutLifecycleConfiguration",
      "s3:PutBucketLogging",
      "s3:PutBucketPublicAccessBlock",
      "s3:PutBucketOwnershipControls",
      "s3:PutBucketAcl",
      "s3:PutBucketTagging",
      "s3:PutReplicationConfiguration",
      "s3:PutInventoryConfiguration",
      "s3:PutBucketNotification",
      "s3:PutBucketObjectLockConfiguration",
      "s3:PutBucketRequestPayment",
      "s3:PutBucketWebsite",
      "s3:PutBucketCORS",
      "s3:PutAccelerateConfiguration",
      "s3:PutIntelligentTieringConfiguration",
      "s3:PutAnalyticsConfiguration",
      "s3:PutMetricsConfiguration",
    ]
    resources = [aws_s3_bucket.cloudtrail.arn]

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = var.administrator_principal_arns
    }
  }
}

# --- KMS key for the CloudTrail S3 destination --------------------------------
# Separate from `aws_kms_key.logs`. Follows CloudTrail's documented key-policy shape
# (https://docs.aws.amazon.com/awscloudtrail/latest/userguide/create-kms-key-policy-for-cloudtrail.html):
# `kms:GenerateDataKey*` for `cloudtrail.amazonaws.com` scoped to this trail via
# `kms:EncryptionContext:aws:cloudtrail:arn`, and `kms:Decrypt` for the account's IAM principals.
data "aws_iam_policy_document" "cloudtrail_cmk" {
  # checkov:skip=CKV_AWS_109: key policy; `Resource: *` is this key alone
  # checkov:skip=CKV_AWS_111: key policy; `Resource: *` is this key alone
  # checkov:skip=CKV_AWS_356: key policy; `Resource: *` is this key alone
  statement {
    sid    = "EnableIAMUserPermissions"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    actions   = ["kms:*"]
    resources = ["*"]
  }

  statement {
    sid    = "AllowCloudTrailToEncryptLogs"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    actions   = ["kms:GenerateDataKey*"]
    resources = ["*"]

    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:aws:cloudtrail:arn"
      values   = ["arn:aws:cloudtrail:*:${data.aws_caller_identity.current.account_id}:trail/*"]
    }
  }

  statement {
    sid    = "AllowCloudTrailToDescribeKey"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    actions   = ["kms:DescribeKey"]
    resources = ["*"]
  }

  statement {
    sid    = "AllowPrincipalsInAccountToDecryptLogFiles"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions = [
      "kms:Decrypt",
      "kms:ReEncryptFrom",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:aws:cloudtrail:arn"
      values   = ["arn:aws:cloudtrail:*:${data.aws_caller_identity.current.account_id}:trail/*"]
    }
  }

  statement {
    sid    = "DenyKeyUseToNonProjectPrincipals"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey*",
      "kms:ReEncrypt*",
      "kms:CreateGrant",
    ]
    resources = ["*"]

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = var.operator_principal_arns
    }

    condition {
      test     = "Bool"
      variable = "aws:PrincipalIsAWSService"
      values   = ["false"]
    }
  }

  statement {
    sid    = "DenyKeyAdministrationToNonOperators"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = [
      "kms:PutKeyPolicy",
      "kms:ScheduleKeyDeletion",
      "kms:DisableKey",
      "kms:RevokeGrant",
      "kms:DisableKeyRotation",
      "kms:CreateAlias",
      "kms:UpdateAlias",
      "kms:DeleteAlias",
      "kms:TagResource",
      "kms:UntagResource",
      "kms:UpdateKeyDescription",
    ]
    resources = ["*"]

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = var.administrator_principal_arns
    }
  }
}

resource "aws_kms_key" "cloudtrail" {
  description                        = "CMK for CloudTrail S3 log destination encryption at rest"
  deletion_window_in_days            = 30
  enable_key_rotation                = true
  policy                             = data.aws_iam_policy_document.cloudtrail_cmk.json
  bypass_policy_lockout_safety_check = false

  lifecycle {
    precondition {
      condition     = contains(var.administrator_principal_arns, var.caller_principal_arn) && var.caller_principal_arn != "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
      error_message = "The caller running this plan is not an operator. Add its role or user ARN to crm_operator_principal_arns in terraform.tfvars, or run as a listed operator; nothing was applied."
    }
  }

  tags = merge(var.tags, {
    "data-classification" = var.data_classification
  })
}

resource "aws_kms_alias" "cloudtrail" {
  name          = "alias/${var.name_prefix}cloudtrail"
  target_key_id = aws_kms_key.cloudtrail.key_id
}

# --- The trail itself ---------------------------------------------------------

resource "aws_cloudtrail" "this" {
  # checkov:skip=CKV2_AWS_10: S3 is the audit destination; no metric filter or alarm consumes CloudWatch delivery
  # checkov:skip=CKV_AWS_252: no SNS delivery notification; nothing subscribes to log delivery
  name                          = local.cloudtrail_name
  s3_bucket_name                = aws_s3_bucket.cloudtrail.id
  kms_key_id                    = aws_kms_key.cloudtrail.arn
  is_multi_region_trail         = true
  include_global_service_events = true
  enable_log_file_validation    = true

  event_selector {
    read_write_type           = "All"
    include_management_events = true
  }

  cloud_watch_logs_group_arn = var.enable_cloudtrail_cloudwatch_logs ? "${aws_cloudwatch_log_group.cloudtrail[0].arn}:*" : null
  cloud_watch_logs_role_arn  = var.enable_cloudtrail_cloudwatch_logs ? aws_iam_role.cloudtrail_cloudwatch_logs[0].arn : null

  tags = merge(var.tags, {
    "data-classification" = var.data_classification
  })

  depends_on = [aws_s3_bucket_policy.cloudtrail]
}
