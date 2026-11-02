#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Bootstraps the Terraform S3 partial-backend prerequisites for crm-agent-eval:
# a KMS customer-managed key, the state bucket, a separate access-log bucket,
# and the DynamoDB lock table. Every run re-applies the key policy, the lock-table resource policy
# and both bucket policies, which deny the stores to every principal outside the operator list, and
# the tags on all four.
# Writes infra/backend.hcl with the resolved values. Idempotent and safe to re-run. Billable.
#
# Usage: infra/bootstrap_state_backend.sh [--profile NAME] [--region REGION]
#            [--expected-account-id ID] --operator-principal-arn ARN [--operator-principal-arn ARN]...
# Env overrides: AWS_PROFILE, AWS_REGION (same defaults: default / us-east-1),
#   CRM_EXPECTED_ACCOUNT_ID, CRM_OPERATOR_PRINCIPAL_ARNS (comma-separated)
# Requires the expected account and the operator list; refuses env static credentials, and refuses
# unless the caller is a listed operator.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${LOG_FILE:-/tmp/bootstrap_state_backend.log}"

log() { printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" | tee -a "$LOG_FILE"; }
info() { log "INFO" "$1"; }
warn() { log "WARN" "$1"; }
error() { log "ERROR" "$1" >&2; }
die() { error "$1"; exit 1; }

usage() {
    cat <<EOF
Usage: $(basename "$0") [--profile NAME] [--region REGION] [--expected-account-id ID]
           --operator-principal-arn ARN [--operator-principal-arn ARN]...

Creates (idempotently) the Terraform state backend for crm-agent-eval:
  - KMS customer-managed key (rotation enabled) + alias
  - S3 access-log bucket (SSE-S3, BPA, BucketOwnerEnforced, TLS-deny policy,
    log-delivery grant scoped to the state bucket, versioning)
  - S3 state bucket (SSE-KMS, BPA, TLS-deny policy, versioning, access logging)
  - DynamoDB lock table (SSE-KMS, PITR)
  - On every run, existing resources included: the key policy, the lock-table resource
    policy and both bucket policies, each denying data access and administration to
    every principal outside the operator list (root is kept for administration), and
    the tags on the key, the lock table and both buckets
  - Writes infra/backend.hcl with the resolved values (gitignored)

Options:
  --profile NAME   AWS CLI profile (default: default; env AWS_PROFILE)
  --region REGION  AWS region (default: us-east-1; env AWS_REGION)
  --expected-account-id ID
                   Account the profile must resolve to (env CRM_EXPECTED_ACCOUNT_ID);
                   required. Refuses before any create on absence or mismatch.
  --operator-principal-arn ARN
                   An IAM role or user ARN in the expected account allowed to use and
                   administer the backend; repeatable (env CRM_OPERATOR_PRINCIPAL_ARNS,
                   comma-separated); required. Must equal the stack's
                   crm_operator_principal_arns. Refuses before any create or put unless the
                   caller is in the list; flag and env disagreeing is refused.
  -h, --help       Show this help
EOF
    exit 0
}

AWS_PROFILE_ARG="${AWS_PROFILE:-default}"
AWS_REGION_ARG="${AWS_REGION:-us-east-1}"
EXPECTED_ACCOUNT_ARG=""
OPERATOR_FLAG_ARNS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) AWS_PROFILE_ARG="$2"; shift 2 ;;
        --region) AWS_REGION_ARG="$2"; shift 2 ;;
        --expected-account-id) EXPECTED_ACCOUNT_ARG="$2"; shift 2 ;;
        --operator-principal-arn) OPERATOR_FLAG_ARNS+=("$2"); shift 2 ;;
        -h|--help) usage ;;
        *) die "Unknown argument: $1 (use -h for help)" ;;
    esac
done

if [[ -n "${AWS_ACCESS_KEY_ID:-}" || -n "${AWS_SESSION_TOKEN:-}" ]]; then
    die "AWS_ACCESS_KEY_ID/AWS_SESSION_TOKEN are set in the environment. Static credentials take precedence over AWS_PROFILE in every AWS SDK, so the aws CLI would authenticate as those keys while this script reports profile='${AWS_PROFILE_ARG}' — a divergence this check refuses. Unset them and re-run: unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN. Nothing was created."
fi

ENV_EXPECTED_ACCOUNT="${CRM_EXPECTED_ACCOUNT_ID:-}"
if [[ -n "$EXPECTED_ACCOUNT_ARG" && -n "$ENV_EXPECTED_ACCOUNT" \
    && "$EXPECTED_ACCOUNT_ARG" != "$ENV_EXPECTED_ACCOUNT" ]]; then
    die "--expected-account-id and CRM_EXPECTED_ACCOUNT_ID disagree; set one. Nothing was created."
fi
EXPECTED_ACCOUNT_ID="${EXPECTED_ACCOUNT_ARG:-$ENV_EXPECTED_ACCOUNT}"
[[ -n "$EXPECTED_ACCOUNT_ID" ]] \
    || die "CRM_EXPECTED_ACCOUNT_ID (or --expected-account-id) is required: the account this backend belongs in. Nothing was created."
[[ "$EXPECTED_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] \
    || die "Expected account id must be 12 digits. Nothing was created."

sorted_unique_lines() { printf '%s\n' "$@" | LC_ALL=C sort -u; }

OPERATOR_FLAG_LIST=""
if [[ ${#OPERATOR_FLAG_ARNS[@]} -gt 0 ]]; then
    OPERATOR_FLAG_LIST="$(sorted_unique_lines "${OPERATOR_FLAG_ARNS[@]}")"
fi
OPERATOR_ENV_LIST=""
if [[ -n "${CRM_OPERATOR_PRINCIPAL_ARNS:-}" ]]; then
    IFS=',' read -r -a OPERATOR_ENV_ARNS <<< "${CRM_OPERATOR_PRINCIPAL_ARNS},"
    OPERATOR_ENV_LIST="$(sorted_unique_lines "${OPERATOR_ENV_ARNS[@]}")"
fi
if [[ -n "$OPERATOR_FLAG_LIST" && -n "$OPERATOR_ENV_LIST" \
    && "$OPERATOR_FLAG_LIST" != "$OPERATOR_ENV_LIST" ]]; then
    die "--operator-principal-arn and CRM_OPERATOR_PRINCIPAL_ARNS disagree; set one. Nothing was created."
fi
OPERATOR_LIST="${OPERATOR_FLAG_LIST:-$OPERATOR_ENV_LIST}"
[[ -n "$OPERATOR_LIST" ]] \
    || die "The operator list (--operator-principal-arn or CRM_OPERATOR_PRINCIPAL_ARNS) is required and must not be empty. Nothing was created."
OPERATOR_ARNS=()
while IFS= read -r operator_arn; do
    [[ "$operator_arn" != "arn:aws:iam::${EXPECTED_ACCOUNT_ID}:root" ]] \
        || die "The account root is not accepted as an operator; list IAM role or user ARNs. Nothing was created."
    [[ "$operator_arn" =~ ^arn:aws:iam::[0-9]{12}:(role|user)/[^*?]+$ ]] \
        || die "An operator entry is not an IAM role or user ARN (arn:aws:iam::<12 digits>:role/... or :user/..., no * or ?). Nothing was created."
    [[ "$operator_arn" == "arn:aws:iam::${EXPECTED_ACCOUNT_ID}:"* ]] \
        || die "An operator entry belongs to an account other than the expected account. Nothing was created."
    OPERATOR_ARNS+=("$operator_arn")
done <<< "$OPERATOR_LIST"

command -v aws >/dev/null 2>&1 || die "aws CLI not found on PATH"
command -v jq >/dev/null 2>&1 || die "jq not found on PATH"

AWS_ARGS=(--profile "$AWS_PROFILE_ARG" --region "$AWS_REGION_ARG" --no-cli-pager --output json)

info "Verifying credentials for profile '$AWS_PROFILE_ARG' in region '$AWS_REGION_ARG'"
CALLER_IDENTITY="$(aws sts get-caller-identity "${AWS_ARGS[@]}" 2>/tmp/sts_err.log)" \
    || die "Unable to resolve AWS identity for profile '$AWS_PROFILE_ARG'. $(cat /tmp/sts_err.log)"
ACCOUNT_ID="$(printf '%s' "$CALLER_IDENTITY" | jq -r '.Account')"
[[ -n "$ACCOUNT_ID" && "$ACCOUNT_ID" != "null" ]] || die "Could not resolve account id from caller identity"
[[ "$ACCOUNT_ID" == "$EXPECTED_ACCOUNT_ID" ]] \
    || die "WRONG ACCOUNT: profile '$AWS_PROFILE_ARG' resolves to a different account than CRM_EXPECTED_ACCOUNT_ID. Nothing was created."
info "Resolved account matches the expected account (not persisted to any file)"

CALLER_ARN="$(printf '%s' "$CALLER_IDENTITY" | jq -r '.Arn')"
CALLER_PRINCIPAL_ARN=""
if [[ "$CALLER_ARN" =~ ^arn:aws:sts::[0-9]{12}:assumed-role/([^/]+)/[^/]+$ ]]; then
    caller_role_name="${BASH_REMATCH[1]}"
    CALLER_PRINCIPAL_ARN="$(aws iam get-role --role-name "$caller_role_name" "${AWS_ARGS[@]}" 2>/dev/null | jq -r '.Role.Arn')" \
        || die "Could not resolve the caller's role ARN with iam get-role; refusing rather than guessing. Nothing was created."
elif [[ "$CALLER_ARN" =~ ^arn:aws:iam::[0-9]{12}:(user)/.+$ ]]; then
    CALLER_PRINCIPAL_ARN="$CALLER_ARN"
else
    die "The caller is neither an assumed IAM role nor an IAM user (the account root is refused). Nothing was created."
fi
[[ -n "$CALLER_PRINCIPAL_ARN" && "$CALLER_PRINCIPAL_ARN" != "null" ]] \
    || die "Could not resolve the caller's role ARN with iam get-role; refusing rather than guessing. Nothing was created."
caller_is_operator=0
for operator_arn in "${OPERATOR_ARNS[@]}"; do
    [[ "$operator_arn" == "$CALLER_PRINCIPAL_ARN" ]] && caller_is_operator=1
done
[[ "$caller_is_operator" -eq 1 ]] \
    || die "The caller is not in the operator list, so these policies would lock it out of the state backend. Run as a listed operator or add it. Nothing was created."
info "Caller is a listed operator (${#OPERATOR_ARNS[@]} operator(s))"

OPERATORS_JSON="$(printf '%s\n' "${OPERATOR_ARNS[@]}" | jq -R . | jq -sc .)"
ADMINISTRATORS_JSON="$(printf '%s' "$OPERATORS_JSON" | jq -c --arg root "arn:aws:iam::${ACCOUNT_ID}:root" '. + [$root]')"

PROJECT="crm-agent-eval"
KMS_ALIAS="alias/${PROJECT}-tfstate"
STATE_BUCKET="${PROJECT}-tfstate-${ACCOUNT_ID}-${AWS_REGION_ARG}"
LOG_BUCKET="${PROJECT}-tfstate-logs-${ACCOUNT_ID}-${AWS_REGION_ARG}"
LOCK_TABLE="${PROJECT}-tflock"
STATE_LOG_PREFIX="state-access/"

# Resource tags; data-classification is "internal" (IaC state, no tenant data).
TAG_SERVICE="$PROJECT"
TAG_ENVIRONMENT="shared"
TAG_OWNER="platform"
TAG_COST_CENTER="crm-agent-eval"
TAG_DATA_CLASSIFICATION="internal"
TAG_APP_NAME="$PROJECT"

# Ordered action tuples; the plan-baseline gate and the T3 smoke hold the same values.
KEY_USE_ACTIONS='["kms:Decrypt","kms:Encrypt","kms:GenerateDataKey*","kms:ReEncrypt*","kms:CreateGrant"]'
STORE_KEY_ADMIN_ACTIONS='["kms:PutKeyPolicy","kms:ScheduleKeyDeletion","kms:DisableKey","kms:RevokeGrant","kms:DisableKeyRotation","kms:CreateAlias","kms:UpdateAlias","kms:DeleteAlias","kms:TagResource","kms:UntagResource","kms:UpdateKeyDescription"]'
TABLE_DATA_PLANE_ACTIONS='["dynamodb:GetItem","dynamodb:BatchGetItem","dynamodb:Query","dynamodb:Scan","dynamodb:ConditionCheckItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:DeleteItem","dynamodb:BatchWriteItem","dynamodb:PartiQLSelect","dynamodb:PartiQLInsert","dynamodb:PartiQLUpdate","dynamodb:PartiQLDelete","dynamodb:CreateBackup","dynamodb:ExportTableToPointInTime","dynamodb:RestoreTableToPointInTime","dynamodb:EnableKinesisStreamingDestination"]'
TABLE_ADMIN_ACTIONS='["dynamodb:DeleteTable","dynamodb:UpdateTable","dynamodb:UpdateContinuousBackups","dynamodb:UpdateTimeToLive","dynamodb:PutResourcePolicy","dynamodb:DeleteResourcePolicy","dynamodb:TagResource","dynamodb:UntagResource","dynamodb:UpdateContributorInsights"]'
S3_OBJECT_READ_ACTIONS='["s3:GetObject","s3:GetObjectVersion","s3:GetObjectAttributes","s3:GetObjectVersionAttributes","s3:GetObjectTagging","s3:GetObjectVersionTagging","s3:RestoreObject","s3:ListBucket","s3:ListBucketVersions","s3:ListBucketMultipartUploads","s3:ListMultipartUploadParts"]'
S3_OBJECT_WRITE_ACTIONS='["s3:PutObject","s3:PutObjectTagging","s3:DeleteObject","s3:DeleteObjectVersion","s3:DeleteObjectTagging","s3:AbortMultipartUpload","s3:PutObjectVersionTagging","s3:DeleteObjectVersionTagging"]'
S3_BUCKET_ADMIN_ACTIONS='["s3:DeleteBucket","s3:PutBucketPolicy","s3:DeleteBucketPolicy","s3:PutBucketVersioning","s3:PutEncryptionConfiguration","s3:PutLifecycleConfiguration","s3:PutBucketLogging","s3:PutBucketPublicAccessBlock","s3:PutBucketOwnershipControls","s3:PutBucketAcl","s3:PutBucketTagging","s3:PutReplicationConfiguration","s3:PutInventoryConfiguration","s3:PutBucketNotification","s3:PutBucketObjectLockConfiguration","s3:PutBucketRequestPayment","s3:PutBucketWebsite","s3:PutBucketCORS","s3:PutAccelerateConfiguration","s3:PutIntelligentTieringConfiguration","s3:PutAnalyticsConfiguration","s3:PutMetricsConfiguration"]'

CREATED=()
EXISTED=()

# principal_deny_statement SID ACTIONS_JSON RESOURCE_JSON PRINCIPALS_JSON [Bool|BoolIfExists]
# The optional operator exempts service principals (aws:PrincipalIsAWSService).
principal_deny_statement() {
    jq -n --arg sid "$1" --argjson actions "$2" --argjson resource "$3" \
        --argjson principals "$4" --arg exempt "${5:-}" '{
        Sid: $sid,
        Effect: "Deny",
        Principal: "*",
        Action: $actions,
        Resource: $resource,
        Condition: ({ArnNotEquals: {"aws:PrincipalArn": $principals}}
            + (if $exempt == "" then {}
               else {($exempt): {"aws:PrincipalIsAWSService": "false"}} end))
    }'
}

aws_tags_json() {
    jq -n \
        --arg service "$TAG_SERVICE" \
        --arg environment "$TAG_ENVIRONMENT" \
        --arg owner "$TAG_OWNER" \
        --arg costcenter "$TAG_COST_CENTER" \
        --arg dataclass "$TAG_DATA_CLASSIFICATION" \
        --arg appname "$TAG_APP_NAME" \
        '[{Key:"service",Value:$service},{Key:"environment",Value:$environment},
          {Key:"owner",Value:$owner},{Key:"cost-center",Value:$costcenter},
          {Key:"data-classification",Value:$dataclass},{Key:"AppName",Value:$appname}]'
}

# --- KMS customer-managed key -------------------------------------------

resolve_kms_key_id() {
    aws kms describe-key --key-id "$KMS_ALIAS" "${AWS_ARGS[@]}" 2>/dev/null \
        | jq -r '.KeyMetadata.KeyId' || true
}

ensure_kms_key() {
    local existing_key_id
    existing_key_id="$(resolve_kms_key_id)"
    if [[ -n "$existing_key_id" && "$existing_key_id" != "null" ]]; then
        info "KMS key already exists via alias $KMS_ALIAS ($existing_key_id)"
        EXISTED+=("kms-key:$KMS_ALIAS")
        KMS_KEY_ID="$existing_key_id"
        return
    fi

    info "Creating KMS customer-managed key for Terraform state encryption"
    local policy_doc key_id
    policy_doc="$(jq -n --arg root "arn:aws:iam::${ACCOUNT_ID}:root" '{
        Version: "2012-10-17",
        Statement: [{
            Sid: "EnableRootAccountPermissions",
            Effect: "Allow",
            Principal: {AWS: $root},
            Action: "kms:*",
            Resource: "*"
        }]
    }')"
    key_id="$(aws kms create-key \
        --description "CMK for ${PROJECT} Terraform state (S3 + DynamoDB)" \
        --key-usage ENCRYPT_DECRYPT \
        --key-spec SYMMETRIC_DEFAULT \
        --policy "$policy_doc" \
        --tags "$(aws_tags_json | jq -c '[.[] | {TagKey: .Key, TagValue: .Value}]')" \
        "${AWS_ARGS[@]}" | jq -r '.KeyMetadata.KeyId')"
    [[ -n "$key_id" && "$key_id" != "null" ]] || die "KMS key creation returned no KeyId"

    aws kms enable-key-rotation --key-id "$key_id" "${AWS_ARGS[@]}" >/dev/null \
        || die "Failed to enable KMS key rotation on $key_id"
    aws kms create-alias --alias-name "$KMS_ALIAS" --target-key-id "$key_id" "${AWS_ARGS[@]}" \
        || die "Failed to create KMS alias $KMS_ALIAS"

    info "Created KMS key $key_id with rotation enabled and alias $KMS_ALIAS"
    CREATED+=("kms-key:$KMS_ALIAS")
    KMS_KEY_ID="$key_id"
}

kms_key_policy() {
    jq -n --arg root "arn:aws:iam::${ACCOUNT_ID}:root" \
        --argjson use "$(principal_deny_statement DenyKeyUseToNonProjectPrincipals \
            "$KEY_USE_ACTIONS" '"*"' "$OPERATORS_JSON" Bool)" \
        --argjson admin "$(principal_deny_statement DenyKeyAdministrationToNonOperators \
            "$STORE_KEY_ADMIN_ACTIONS" '"*"' "$ADMINISTRATORS_JSON")" '{
        Version: "2012-10-17",
        Statement: [{
            Sid: "EnableRootAccountPermissions",
            Effect: "Allow",
            Principal: {AWS: $root},
            Action: "kms:*",
            Resource: "*"
        }, $use, $admin]
    }'
}

converge_kms_key_policy() {
    aws kms put-key-policy --key-id "$KMS_KEY_ID" --policy-name default \
        --policy "$(kms_key_policy)" "${AWS_ARGS[@]}" \
        || die "Failed to put the key policy on $KMS_ALIAS"
    info "Converged the key policy on $KMS_ALIAS"
}

# --- S3 buckets ----------------------------------------------------------

bucket_exists() {
    aws s3api head-bucket --bucket "$1" "${AWS_ARGS[@]}" >/dev/null 2>&1
}

create_bucket_if_absent() {
    local bucket="$1"
    if bucket_exists "$bucket"; then
        info "Bucket $bucket already exists"
        EXISTED+=("s3-bucket:$bucket")
        return
    fi
    info "Creating bucket $bucket"
    if [[ "$AWS_REGION_ARG" == "us-east-1" ]]; then
        aws s3api create-bucket --bucket "$bucket" "${AWS_ARGS[@]}" >/dev/null
    else
        aws s3api create-bucket --bucket "$bucket" \
            --create-bucket-configuration "LocationConstraint=${AWS_REGION_ARG}" \
            "${AWS_ARGS[@]}" >/dev/null
    fi
    CREATED+=("s3-bucket:$bucket")
}

tls_deny_statement() {
    jq -n --arg bucket "$1" '{
        Sid: "DenyInsecureTransport",
        Effect: "Deny",
        Principal: "*",
        Action: ["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket"],
        Resource: [("arn:aws:s3:::" + $bucket), ("arn:aws:s3:::" + $bucket + "/*")],
        Condition: {Bool: {"aws:SecureTransport": "false"}}
    }'
}

log_delivery_statement() {
    jq -n --arg log_bucket "$LOG_BUCKET" --arg prefix "$STATE_LOG_PREFIX" \
        --arg source "arn:aws:s3:::${STATE_BUCKET}" --arg account "$ACCOUNT_ID" '{
        Sid: "AllowStateBucketAccessLogDelivery",
        Effect: "Allow",
        Principal: {Service: "logging.s3.amazonaws.com"},
        Action: "s3:PutObject",
        Resource: ("arn:aws:s3:::" + $log_bucket + "/" + $prefix + "*"),
        Condition: {
            ArnLike: {"aws:SourceArn": $source},
            StringEquals: {"aws:SourceAccount": $account}
        }
    }'
}

kms_encryption_config() {
    jq -n --arg kms "$KMS_KEY_ID" '{
        Rules: [{
            ApplyServerSideEncryptionByDefault: {SSEAlgorithm: "aws:kms", KMSMasterKeyID: $kms},
            BucketKeyEnabled: true
        }]
    }'
}

sse_s3_encryption_config() {
    jq -n '{Rules: [{ApplyServerSideEncryptionByDefault: {SSEAlgorithm: "AES256"}}]}'
}

apply_bucket_baseline() {
    # Applies versioning, default encryption ($2), BPA, bucket policy ($3), tags and, when $4 is
    # non-empty, access logging to bucket $4 under STATE_LOG_PREFIX. Every call is a put, so a
    # re-run converges an existing bucket.
    local bucket="$1" encryption_config="$2" policy_doc="$3" log_target_bucket="${4:-}"

    aws s3api put-bucket-versioning --bucket "$bucket" \
        --versioning-configuration Status=Enabled "${AWS_ARGS[@]}" \
        || die "Failed to enable versioning on $bucket"

    aws s3api put-bucket-encryption --bucket "$bucket" \
        --server-side-encryption-configuration "$encryption_config" "${AWS_ARGS[@]}" \
        || die "Failed to set default encryption on $bucket"

    aws s3api put-public-access-block --bucket "$bucket" \
        --public-access-block-configuration \
        BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true \
        "${AWS_ARGS[@]}" \
        || die "Failed to set Block Public Access on $bucket"

    aws s3api put-bucket-policy --bucket "$bucket" --policy "$policy_doc" "${AWS_ARGS[@]}" \
        || die "Failed to apply bucket policy on $bucket"

    aws s3api put-bucket-tagging --bucket "$bucket" \
        --tagging "$(jq -n --argjson tagset "$(aws_tags_json)" '{TagSet: $tagset}')" \
        "${AWS_ARGS[@]}" \
        || die "Failed to tag bucket $bucket"

    if [[ -n "$log_target_bucket" ]]; then
        aws s3api put-bucket-logging --bucket "$bucket" \
            --bucket-logging-status "$(jq -n --arg target "$log_target_bucket" \
                --arg prefix "$STATE_LOG_PREFIX" '{
                LoggingEnabled: {TargetBucket: $target, TargetPrefix: $prefix}
            }')" "${AWS_ARGS[@]}" \
            || die "Failed to enable access logging on $bucket -> $log_target_bucket"
    fi
}

s3_object_resources() {
    jq -nc --arg bucket "$1" '[("arn:aws:s3:::" + $bucket), ("arn:aws:s3:::" + $bucket + "/*")]'
}

s3_data_plane_deny_statement() {
    principal_deny_statement DenyObjectDataPlaneToNonProjectPrincipals \
        "$(jq -nc --argjson read "$S3_OBJECT_READ_ACTIONS" --argjson write "$S3_OBJECT_WRITE_ACTIONS" \
            '$read + $write')" "$(s3_object_resources "$1")" "$OPERATORS_JSON" "${2:-}"
}

s3_admin_deny_statement() {
    principal_deny_statement DenyBucketAdministrationToNonOperators "$S3_BUCKET_ADMIN_ACTIONS" \
        "$(jq -nc --arg bucket "$1" '"arn:aws:s3:::" + $bucket')" "$ADMINISTRATORS_JSON"
}

apply_log_bucket_baseline() {
    aws s3api put-bucket-ownership-controls --bucket "$LOG_BUCKET" \
        --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]' "${AWS_ARGS[@]}" \
        || die "Failed to set BucketOwnerEnforced on $LOG_BUCKET"
    apply_bucket_baseline "$LOG_BUCKET" "$(sse_s3_encryption_config)" \
        "$(jq -n --argjson tls "$(tls_deny_statement "$LOG_BUCKET")" \
            --argjson grant "$(log_delivery_statement)" \
            --argjson log_data "$(s3_data_plane_deny_statement "$LOG_BUCKET" BoolIfExists)" \
            --argjson log_admin "$(s3_admin_deny_statement "$LOG_BUCKET")" \
            '{Version: "2012-10-17", Statement: [$tls, $grant, $log_data, $log_admin]}')" ""
}

apply_state_bucket_baseline() {
    apply_bucket_baseline "$STATE_BUCKET" "$(kms_encryption_config)" \
        "$(jq -n --argjson tls "$(tls_deny_statement "$STATE_BUCKET")" \
            --argjson state_data "$(s3_data_plane_deny_statement "$STATE_BUCKET")" \
            --argjson state_admin "$(s3_admin_deny_statement "$STATE_BUCKET")" \
            '{Version: "2012-10-17", Statement: [$tls, $state_data, $state_admin]}')" "$LOG_BUCKET"
}

# --- DynamoDB lock table ---------------------------------------------------

table_exists() {
    aws dynamodb describe-table --table-name "$1" "${AWS_ARGS[@]}" >/dev/null 2>&1
}

ensure_lock_table() {
    if table_exists "$LOCK_TABLE"; then
        info "DynamoDB lock table $LOCK_TABLE already exists"
        EXISTED+=("dynamodb-table:$LOCK_TABLE")
        return
    fi

    info "Creating DynamoDB lock table $LOCK_TABLE"
    aws dynamodb create-table \
        --table-name "$LOCK_TABLE" \
        --attribute-definitions AttributeName=LockID,AttributeType=S \
        --key-schema AttributeName=LockID,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST \
        --sse-specification "Enabled=true,SSEType=KMS,KMSMasterKeyId=${KMS_KEY_ID}" \
        --tags "$(aws_tags_json | jq -c '[.[] | {Key: .Key, Value: .Value}]')" \
        "${AWS_ARGS[@]}" >/dev/null \
        || die "Failed to create DynamoDB lock table $LOCK_TABLE"

    aws dynamodb wait table-exists --table-name "$LOCK_TABLE" "${AWS_ARGS[@]}" \
        || die "Timed out waiting for $LOCK_TABLE to become ACTIVE"

    aws dynamodb update-continuous-backups --table-name "$LOCK_TABLE" \
        --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true \
        "${AWS_ARGS[@]}" >/dev/null \
        || die "Failed to enable PITR on $LOCK_TABLE"

    info "Created $LOCK_TABLE with SSE-KMS and PITR enabled"
    CREATED+=("dynamodb-table:$LOCK_TABLE")
}

lock_table_policy() {
    local table_arn="arn:aws:dynamodb:${AWS_REGION_ARG}:${ACCOUNT_ID}:table/${LOCK_TABLE}"
    local resources
    resources="$(jq -nc --arg table "$table_arn" '[$table, ($table + "/index/*")]')"
    jq -n --argjson table_data "$(principal_deny_statement DenyDataPlaneToNonProjectPrincipals \
            "$TABLE_DATA_PLANE_ACTIONS" "$resources" "$OPERATORS_JSON")" \
        --argjson table_admin "$(principal_deny_statement DenyTableAdministrationToNonOperators \
            "$TABLE_ADMIN_ACTIONS" "$resources" "$ADMINISTRATORS_JSON")" \
        '{Version: "2012-10-17", Statement: [$table_data, $table_admin]}'
}

converge_lock_table_policy() {
    aws dynamodb put-resource-policy \
        --resource-arn "arn:aws:dynamodb:${AWS_REGION_ARG}:${ACCOUNT_ID}:table/${LOCK_TABLE}" \
        --policy "$(lock_table_policy)" "${AWS_ARGS[@]}" >/dev/null \
        || die "Failed to put the resource policy on $LOCK_TABLE"
    info "Converged the resource policy on $LOCK_TABLE"
}

converge_key_and_table_tags() {
    aws kms tag-resource --key-id "$KMS_KEY_ID" \
        --tags "$(aws_tags_json | jq -c '[.[] | {TagKey: .Key, TagValue: .Value}]')" \
        "${AWS_ARGS[@]}" >/dev/null \
        || die "Failed to tag KMS key $KMS_ALIAS"
    aws dynamodb tag-resource \
        --resource-arn "arn:aws:dynamodb:${AWS_REGION_ARG}:${ACCOUNT_ID}:table/${LOCK_TABLE}" \
        --tags "$(aws_tags_json | jq -c '[.[] | {Key: .Key, Value: .Value}]')" \
        "${AWS_ARGS[@]}" >/dev/null \
        || die "Failed to tag $LOCK_TABLE"
    info "Converged the tags on $KMS_ALIAS and $LOCK_TABLE"
}

# --- backend.hcl -----------------------------------------------------------

write_backend_hcl() {
    local out_file="${SCRIPT_DIR}/backend.hcl"
    cat > "$out_file" <<EOF
# Generated by infra/bootstrap_state_backend.sh — DO NOT COMMIT (gitignored).
# Re-run the bootstrap script to regenerate if this file is lost.
bucket         = "${STATE_BUCKET}"
key            = "crm-agent-eval/terraform.tfstate"
region         = "${AWS_REGION_ARG}"
dynamodb_table = "${LOCK_TABLE}"
kms_key_id     = "${KMS_KEY_ID}"
encrypt        = true
EOF
    info "Wrote backend config to $out_file"

    local gitignore_file
    gitignore_file="$(cd "$SCRIPT_DIR/.." && pwd)/.gitignore"
    if [[ -f "$gitignore_file" ]] && grep -qxF "infra/backend.hcl" "$gitignore_file"; then
        info "infra/backend.hcl already covered by .gitignore"
    else
        warn "infra/backend.hcl not found in .gitignore — appending entry"
        printf '\ninfra/backend.hcl\n' >> "$gitignore_file"
    fi
}

main() {
    info "Starting Terraform state backend bootstrap (profile=$AWS_PROFILE_ARG region=$AWS_REGION_ARG)"

    ensure_kms_key
    converge_kms_key_policy
    ensure_lock_table
    converge_lock_table_policy
    converge_key_and_table_tags
    create_bucket_if_absent "$LOG_BUCKET"
    apply_log_bucket_baseline
    create_bucket_if_absent "$STATE_BUCKET"
    apply_state_bucket_baseline
    write_backend_hcl

    info "Bootstrap complete."
    if [[ ${#CREATED[@]} -gt 0 ]]; then
        info "Created: ${CREATED[*]}"
    else
        info "Created: (nothing — all resources already existed)"
    fi
    if [[ ${#EXISTED[@]} -gt 0 ]]; then
        info "Already existed: ${EXISTED[*]}"
    fi
}

main "$@"
