/**
 * S3 origin bucket for the frontend build, and the access-log bucket it, the CloudFront
 * distribution and the ALB write to. The origin bucket blocks all public access, has no ACL, uses
 * SSE-KMS with the module's CMK, is versioned and logged, expires only superseded versions, and its
 * policy denies non-TLS requests, restricts `s3:GetObject` to this distribution via
 * `AWS:SourceArn`, and denies object writes and bucket administration outside the operator and
 * administrator lists.
 */

data "aws_caller_identity" "current" {}

# Used in the ALB log-delivery grant's `aws:SourceArn` condition below.
data "aws_partition" "current" {}

data "aws_region" "current" {}

# --- Access-log bucket -------------------------------------------------------
# SSE-S3, not the CMK: S3 server access logging cannot deliver to an SSE-KMS bucket. Holds log
# records only.
resource "aws_s3_bucket" "logs" {
  # checkov:skip=CKV_AWS_145: S3 server access logging cannot deliver to an SSE-KMS bucket
  # checkov:skip=CKV_AWS_144: log records expire under the lifecycle rule; no replica
  # checkov:skip=CKV2_AWS_62: no consumer reacts to log-object events
  bucket = "${var.name_prefix}frontend-cdn-logs-${data.aws_caller_identity.current.account_id}"

  # Lets a destroy proceed while the bucket still holds object versions.
  force_destroy = true

  tags = {
    "data-classification" = var.data_classification
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket = aws_s3_bucket.logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  # checkov:skip=CKV2_AWS_65: CloudFront standard logging delivers through bucket ACLs, so ACLs stay enabled
  bucket = aws_s3_bucket.logs.id

  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.log_retention_days
    }

    # On a versioned bucket `expiration` only writes a delete marker; this expires the old versions.
    noncurrent_version_expiration {
      noncurrent_days = var.log_retention_days
    }

    # Incomplete multipart uploads are not object versions, so `expiration` never reaches them.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_policy" "logs" {
  bucket = aws_s3_bucket.logs.id
  policy = data.aws_iam_policy_document.logs_tls_deny.json
}

# Named for its TLS Deny, but it also carries the log-delivery Allows. The name is the plan address
# consumers and the committed plan fixture read, so it is unchanged.
data "aws_iam_policy_document" "logs_tls_deny" {
  # S3 server access log delivery.
  statement {
    sid    = "AllowS3ServerAccessLogDelivery"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.logs.arn}/*"]

    # Scoped by account rather than source bucket ARN, because `module.eval_artifacts` also logs here.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  # ELB log delivery is a different principal from S3 server access logging; without this grant
  # `aws_lb` creation fails, since ELB writes a test object. Uses the all-Region service principal
  # `logdelivery.elasticloadbalancing.amazonaws.com`, not a per-Region ELB account id.
  statement {
    sid    = "AllowAlbAccessLogDelivery"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
    }

    actions = ["s3:PutObject"]

    # The account id segment stops a load balancer in another account writing here.
    resources = [
      "${aws_s3_bucket.logs.arn}/${var.alb_access_log_prefix}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
    ]

    # Restricts delivery to load balancers in this account and Region.
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values = [
        "arn:${data.aws_partition.current.partition}:elasticloadbalancing:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:loadbalancer/*"
      ]
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
    resources = [aws_s3_bucket.logs.arn, "${aws_s3_bucket.logs.arn}/*"]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

# --- Origin bucket -------------------------------------------------------

resource "aws_s3_bucket" "origin" {
  # checkov:skip=CKV_AWS_144: the build is reproducible from source, so no replica
  # checkov:skip=CKV2_AWS_62: no consumer reacts to object events
  bucket = "${var.name_prefix}frontend-cdn-origin-${data.aws_caller_identity.current.account_id}"

  # Holds the frontend build output, reproducible from source.
  force_destroy = true

  tags = {
    "data-classification" = var.data_classification
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "origin" {
  bucket = aws_s3_bucket.origin.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.frontend.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "origin" {
  bucket = aws_s3_bucket.origin.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "origin" {
  bucket = aws_s3_bucket.origin.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Expires superseded builds and incomplete uploads only. No current-version `expiration`: the current
# SPA build must stay served.
resource "aws_s3_bucket_lifecycle_configuration" "origin" {
  bucket = aws_s3_bucket.origin.id

  rule {
    id     = "expire-superseded-builds"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.origin_noncurrent_version_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_ownership_controls" "origin" {
  bucket = aws_s3_bucket.origin.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_logging" "origin" {
  bucket = aws_s3_bucket.origin.id

  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "s3-access-logs/origin/"
}

# Deny non-TLS requests, restrict s3:GetObject to this CloudFront distribution
# (AWS:SourceArn), and deny object writes and bucket administration outside the operators.
resource "aws_s3_bucket_policy" "origin" {
  bucket = aws_s3_bucket.origin.id
  policy = data.aws_iam_policy_document.origin.json

  lifecycle {
    precondition {
      condition     = contains(var.administrator_principal_arns, var.caller_principal_arn) && var.caller_principal_arn != "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
      error_message = "The caller running this plan is not an operator. Add its role or user ARN to crm_operator_principal_arns in terraform.tfvars, or run as a listed operator; nothing was applied."
    }
  }
}

data "aws_iam_policy_document" "origin" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions   = ["s3:*"]
    resources = [aws_s3_bucket.origin.arn, "${aws_s3_bucket.origin.arn}/*"]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid    = "AllowCloudFrontOACRead"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.origin.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.frontend.arn]
    }
  }

  statement {
    sid    = "DenyObjectWritesToNonOperators"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = [
      "s3:PutObject",
      "s3:PutObjectTagging",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:DeleteObjectTagging",
      "s3:AbortMultipartUpload",
      "s3:PutObjectVersionTagging",
      "s3:DeleteObjectVersionTagging",
    ]
    resources = [aws_s3_bucket.origin.arn, "${aws_s3_bucket.origin.arn}/*"]

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = var.operator_principal_arns
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
    resources = [aws_s3_bucket.origin.arn]

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = var.administrator_principal_arns
    }
  }
}
