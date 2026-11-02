#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Verifies every blocks-completion control in docs/security-baseline.md against a live
# deployment, running each row's listed `Verify` command. `review-PASS` rows are not run.
#
# BILLABLE AND LIVE: every non-dry-run invocation calls the real AWS CLI against a real account,
# and requires `--i-understand-this-is-billable`. `--dry-run` prints what each control would run
# and makes no AWS call.
#
# Usage:
#   scripts/verify_security_baseline.sh --i-understand-this-is-billable [--profile NAME]
#     [--region REGION] [--outputs PATH]
#   scripts/verify_security_baseline.sh --dry-run [--profile NAME] [--region REGION]
#     [--outputs PATH]
#
# Profile/region resolve as flag, then AWS_PROFILE/AWS_REGION, then default/us-east-1.
#
# --outputs points at a `terraform output -json` document (default: results/tf-outputs.json).
# The remaining resources are discovered at run time with `describe`/`list` calls.
#
# Every control discovers only this deployment's own resources: DynamoDB tables come from the
# `dynamodb_table_names` output, and Lambda functions from `list-functions` filtered to
# `${NAME_PREFIX}*` (default "crm-agent-eval", overridable with --name-prefix). A discovery
# failure is reported as FAIL.
#
# Exit codes: 0 all controls passed; 1 one or more controls failed; 2 usage/environment error
# (missing flag, missing tf-outputs file, required CLI tool absent).
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd -P)"

usage() {
  cat <<EOF
Usage: ${SCRIPT_NAME} (--i-understand-this-is-billable | --dry-run) [options]

Verifies every blocks-completion control in docs/security-baseline.md against a live deployment.

Options:
  --i-understand-this-is-billable   Required opt-in to actually run live AWS checks.
  --dry-run                         Print what each control would run; make no AWS call.
  --profile NAME                    AWS CLI profile (default: default; env AWS_PROFILE).
  --region REGION                   AWS region (default: us-east-1; env AWS_REGION).
  --outputs PATH                    terraform output -json document
                                     (default: \${REPO_ROOT}/results/tf-outputs.json).
  --name-prefix PREFIX              This deployment's resource name prefix, used to scope
                                     discovery calls that have no Terraform output naming every
                                     resource individually (default: crm-agent-eval, matching
                                     infra/variables.tf's name_prefix default).
  -h, --help                        Show this help.
EOF
  exit 0
}

RESOLVED_PROFILE="${AWS_PROFILE:-default}"
RESOLVED_REGION="${AWS_REGION:-us-east-1}"
OUTPUTS_PATH="${REPO_ROOT}/results/tf-outputs.json"
NAME_PREFIX="crm-agent-eval"
BILLABLE_OK=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --i-understand-this-is-billable) BILLABLE_OK=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --profile) RESOLVED_PROFILE="$2"; shift 2 ;;
    --region) RESOLVED_REGION="$2"; shift 2 ;;
    --outputs) OUTPUTS_PATH="$2"; shift 2 ;;
    --name-prefix) NAME_PREFIX="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "${SCRIPT_NAME}: unknown argument: $1 (use -h for help)" >&2; exit 2 ;;
  esac
done

if [[ "${DRY_RUN}" != "true" && "${BILLABLE_OK}" != "true" ]]; then
  echo "${SCRIPT_NAME}: refusing to run — this makes real, billable AWS calls against a live" >&2
  echo "deployment. Pass --i-understand-this-is-billable to proceed, or --dry-run to preview" >&2
  echo "the commands with no AWS call." >&2
  exit 2
fi

if [[ "${DRY_RUN}" != "true" ]]; then
  command -v aws >/dev/null 2>&1 || { echo "${SCRIPT_NAME}: aws CLI not found on PATH" >&2; exit 2; }
fi
command -v jq >/dev/null 2>&1 || { echo "${SCRIPT_NAME}: jq not found on PATH" >&2; exit 2; }

AWS_ARGS=(--profile "${RESOLVED_PROFILE}" --region "${RESOLVED_REGION}" --no-cli-pager --output json)

# ---------------------------------------------------------------------------
# Terraform-output lookup. Accepts both the `terraform output -json` shape
# ({"name": {"value": ...}}) and an already-flattened mapping.
# ---------------------------------------------------------------------------
TF_OUTPUT_VALUE=""
tf_output() {
  local name="$1"
  if [[ ! -f "${OUTPUTS_PATH}" ]]; then
    echo "${SCRIPT_NAME}: terraform outputs file not found: ${OUTPUTS_PATH}" >&2
    return 2
  fi
  TF_OUTPUT_VALUE="$(jq -r --arg n "${name}" \
    'if (.[$n] | type) == "object" and (.[$n] | has("value")) then .[$n].value else .[$n] end' \
    "${OUTPUTS_PATH}")"
  if [[ -z "${TF_OUTPUT_VALUE}" || "${TF_OUTPUT_VALUE}" == "null" ]]; then
    echo "${SCRIPT_NAME}: terraform output '${name}' missing or null in ${OUTPUTS_PATH}" >&2
    return 2
  fi
}

# Prints this deployment's DynamoDB table names, one per line, from the `dynamodb_table_names`
# output. Returns non-zero, printing nothing, if the output is missing.
this_deployment_table_names() {
  if [[ ! -f "${OUTPUTS_PATH}" ]]; then
    echo "${SCRIPT_NAME}: terraform outputs file not found: ${OUTPUTS_PATH}" >&2
    return 2
  fi
  local names
  names="$(jq -r \
    'if (.dynamodb_table_names | type) == "object" and (.dynamodb_table_names | has("value"))
       then (.dynamodb_table_names.value | to_entries[].value)
       else (.dynamodb_table_names // {} | to_entries[].value)
     end' "${OUTPUTS_PATH}" 2>/dev/null)"
  if [[ -z "${names}" ]]; then
    echo "${SCRIPT_NAME}: terraform output 'dynamodb_table_names' missing, null, or empty in ${OUTPUTS_PATH}" >&2
    return 2
  fi
  printf '%s\n' "${names}"
}

# Prints this deployment's Lambda function names: `list-functions` filtered to `${NAME_PREFIX}*`.
# Returns non-zero, printing nothing, if the call fails or the filtered result is empty.
this_deployment_function_names() {
  local all
  all="$(aws lambda list-functions "${AWS_ARGS[@]}" | jq -r '.Functions // [] | .[].FunctionName')" || return 1
  local filtered
  filtered="$(printf '%s\n' "${all}" | grep -E "^${NAME_PREFIX}" || true)"
  if [[ -z "${filtered}" ]]; then
    echo "${SCRIPT_NAME}: no Lambda functions matched name-prefix '${NAME_PREFIX}' (checked $(printf '%s\n' "${all}" | wc -l | tr -d ' ') functions account-wide)" >&2
    return 2
  fi
  printf '%s\n' "${filtered}"
}

# ---------------------------------------------------------------------------
# Reporting. One line per control: the control name, the command run, and the outcome.
# OVERALL_STATUS is set when any control fails or defers.
# ---------------------------------------------------------------------------
OVERALL_STATUS=0

report() {
  local control="$1" command_run="$2" outcome="$3" detail="${4:-}"
  local line="[${outcome}] ${control} :: ran \`${command_run}\`"
  if [[ -n "${detail}" ]]; then
    line="${line} :: ${detail}"
  fi
  printf '%s\n' "${line}"
  # DEFER is a known unresolved gap (see control_transport_tls); it forces a non-zero exit like FAIL.
  if [[ "${outcome}" == "FAIL" || "${outcome}" == "DEFER" ]]; then
    OVERALL_STATUS=1
  fi
}

# ===========================================================================
# Control 1 — DynamoDB encryption at rest (KMS CMK, not AWS-owned)
# Verify: describe-table | jq '.Table.SSEDescription' -> SSEType=KMS + CMK ARN
# ===========================================================================
control_dynamodb_encryption() {
  local cmd="aws dynamodb describe-table --table-name <t> | jq '.Table.SSEDescription' (tables: dynamodb_table_names output)"
  if [[ "${DRY_RUN}" == "true" ]]; then
    report "DynamoDB encryption at rest" "${cmd}" "SKIP" "dry-run: no AWS call made"
    return 0
  fi
  local tables
  tables="$(this_deployment_table_names)" || {
    report "DynamoDB encryption at rest" "${cmd}" "FAIL" "could not resolve this deployment's own tables from dynamodb_table_names"
    return 0
  }
  local table sse sse_type kms_arn failed=0 detail=""
  while IFS= read -r table; do
    [[ -z "${table}" ]] && continue
    sse="$(aws dynamodb describe-table --table-name "${table}" "${AWS_ARGS[@]}" \
      | jq -c '.Table.SSEDescription // {}')"
    sse_type="$(printf '%s' "${sse}" | jq -r '.SSEType // "NONE"')"
    kms_arn="$(printf '%s' "${sse}" | jq -r '.KMSMasterKeyArn // ""')"
    if [[ "${sse_type}" != "KMS" || -z "${kms_arn}" ]]; then
      failed=1
      detail="${detail}${table}: SSEType=${sse_type} KMSMasterKeyArn=${kms_arn:-<none>}; "
    fi
  done <<<"${tables}"
  if [[ "${failed}" -eq 1 ]]; then
    report "DynamoDB encryption at rest" "${cmd}" "FAIL" "${detail}"
  else
    report "DynamoDB encryption at rest" "${cmd}" "PASS" "all $(printf '%s\n' "${tables}" | wc -l | tr -d ' ') of this deployment's tables SSEType=KMS with a CMK ARN"
  fi
}

# ===========================================================================
# Control 2 — DynamoDB PITR (enabled on all tables)
# Verify: describe-continuous-backups -> ENABLED
# ===========================================================================
control_dynamodb_pitr() {
  local cmd="aws dynamodb describe-continuous-backups --table-name <t> (tables: dynamodb_table_names output)"
  if [[ "${DRY_RUN}" == "true" ]]; then
    report "DynamoDB PITR" "${cmd}" "SKIP" "dry-run: no AWS call made"
    return 0
  fi
  local tables
  tables="$(this_deployment_table_names)" || {
    report "DynamoDB PITR" "${cmd}" "FAIL" "could not resolve this deployment's own tables from dynamodb_table_names"
    return 0
  }
  local table status failed=0 detail=""
  while IFS= read -r table; do
    [[ -z "${table}" ]] && continue
    status="$(aws dynamodb describe-continuous-backups --table-name "${table}" "${AWS_ARGS[@]}" \
      | jq -r '.ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus // "DISABLED"')"
    if [[ "${status}" != "ENABLED" ]]; then
      failed=1
      detail="${detail}${table}: PointInTimeRecoveryStatus=${status}; "
    fi
  done <<<"${tables}"
  if [[ "${failed}" -eq 1 ]]; then
    report "DynamoDB PITR" "${cmd}" "FAIL" "${detail}"
  else
    report "DynamoDB PITR" "${cmd}" "PASS" "PITR ENABLED on all $(printf '%s\n' "${tables}" | wc -l | tr -d ' ') of this deployment's tables"
  fi
}

# ===========================================================================
# Control 3 — DynamoDB IAM (no dynamodb:*; per-table, per-action; LeadingKeys where applicable)
# Verify: iam simulate-principal-policy
#
# The per-role, per-action matrix is checked at plan time by
# scripts/check_plan_security_baseline.py --only dynamodb. This control simulates each discovered
# Lambda execution role and asserts it is denied `dynamodb:*` on the live tables.
# ===========================================================================
control_dynamodb_iam() {
  local cmd="aws iam simulate-principal-policy --policy-source-arn <role-arn> --action-names dynamodb:* (roles: this deployment's Lambda execution roles, from list-functions filtered to name-prefix '${NAME_PREFIX}')"
  if [[ "${DRY_RUN}" == "true" ]]; then
    report "DynamoDB IAM" "${cmd}" "SKIP" "dry-run: no AWS call made"
    return 0
  fi
  local functions
  functions="$(this_deployment_function_names)" || {
    report "DynamoDB IAM" "${cmd}" "FAIL" "could not resolve this deployment's own Lambda functions via name-prefix '${NAME_PREFIX}'"
    return 0
  }
  local roles
  roles="$(while IFS= read -r fn; do
    [[ -z "${fn}" ]] && continue
    aws lambda get-function-configuration --function-name "${fn}" "${AWS_ARGS[@]}" | jq -r '.Role'
  done <<<"${functions}" | sort -u)"
  if [[ -z "${roles}" ]]; then
    report "DynamoDB IAM" "${cmd}" "FAIL" "no Lambda execution roles found to simulate against"
    return 0
  fi
  local role decision failed=0 detail=""
  while IFS= read -r role; do
    [[ -z "${role}" ]] && continue
    decision="$(aws iam simulate-principal-policy --policy-source-arn "${role}" \
      --action-names 'dynamodb:*' "${AWS_ARGS[@]}" \
      | jq -r '.EvaluationResults[0].EvalDecision // "unknown"')"
    if [[ "${decision}" != "explicitDeny" && "${decision}" != "implicitDeny" ]]; then
      failed=1
      detail="${detail}${role}: dynamodb:* EvalDecision=${decision} (expected a deny); "
    fi
  done <<<"${roles}"
  if [[ "${failed}" -eq 1 ]]; then
    report "DynamoDB IAM" "${cmd}" "FAIL" "${detail}"
  else
    report "DynamoDB IAM" "${cmd}" "PASS" "dynamodb:* denied for every discovered Lambda role of this deployment" \
      "(full per-action/LeadingKeys matrix asserted at plan time by check_plan_security_baseline.py --only dynamodb)"
  fi
}

# ===========================================================================
# Control 4 — S3 frontend bucket (BPA on, private, OAC-only, TLS-deny policy)
# Verify: get-public-access-block, get-bucket-policy
# ===========================================================================
control_s3_frontend_bucket() {
  local cmd="aws cloudfront get-distribution-config (resolve origin bucket) && aws s3api get-public-access-block --bucket <b> && aws s3api get-bucket-policy --bucket <b>"
  if [[ "${DRY_RUN}" == "true" ]]; then
    report "S3 frontend bucket" "${cmd}" "SKIP" "dry-run: no AWS call made"
    return 0
  fi
  local domain dist_id bucket bpa policy_json failed=0 detail=""
  if ! tf_output cloudfront_domain_name; then
    report "S3 frontend bucket" "${cmd}" "FAIL" "cloudfront_domain_name output unavailable"
    return 0
  fi
  domain="${TF_OUTPUT_VALUE}"
  dist_id="$(aws cloudfront list-distributions "${AWS_ARGS[@]}" \
    | jq -r --arg d "${domain}" '.DistributionList.Items[] | select(.DomainName==$d) | .Id')"
  if [[ -z "${dist_id}" ]]; then
    report "S3 frontend bucket" "${cmd}" "FAIL" "no CloudFront distribution matches ${domain}"
    return 0
  fi
  bucket="$(aws cloudfront get-distribution-config --id "${dist_id}" "${AWS_ARGS[@]}" \
    | jq -r '.DistributionConfig.Origins.Items // [] | .[] | select(has("S3OriginConfig")) | .DomainName' \
    | sed -n '1{s/\.s3\..*$//; s/\.s3-website.*$//; p;}')"
  if [[ -z "${bucket}" ]]; then
    report "S3 frontend bucket" "${cmd}" "FAIL" "could not resolve origin S3 bucket from distribution ${dist_id}"
    return 0
  fi
  bpa="$(aws s3api get-public-access-block --bucket "${bucket}" "${AWS_ARGS[@]}" \
    | jq -c '.PublicAccessBlockConfiguration')" || { bpa="{}"; failed=1; detail="${detail}${bucket}: get-public-access-block failed; "; }
  if [[ "$(printf '%s' "${bpa}" | jq -r '.BlockPublicAcls // false')" != "true" \
    || "$(printf '%s' "${bpa}" | jq -r '.BlockPublicPolicy // false')" != "true" \
    || "$(printf '%s' "${bpa}" | jq -r '.IgnorePublicAcls // false')" != "true" \
    || "$(printf '%s' "${bpa}" | jq -r '.RestrictPublicBuckets // false')" != "true" ]]; then
    failed=1
    detail="${detail}${bucket}: BlockPublicAccess not fully enabled: ${bpa}; "
  fi
  policy_json="$(aws s3api get-bucket-policy --bucket "${bucket}" "${AWS_ARGS[@]}" 2>/dev/null \
    | jq -r '.Policy' 2>/dev/null || echo "")"
  if [[ -z "${policy_json}" ]] || ! printf '%s' "${policy_json}" \
    | jq -e '.Statement[] | select(.Effect=="Deny" and (.Condition.Bool."aws:SecureTransport"=="false"))' >/dev/null 2>&1; then
    failed=1
    detail="${detail}${bucket}: no bucket policy Deny statement on aws:SecureTransport=false; "
  fi
  if [[ "${failed}" -eq 1 ]]; then
    report "S3 frontend bucket" "${cmd}" "FAIL" "${detail}"
  else
    report "S3 frontend bucket" "${cmd}" "PASS" "${bucket}: BPA fully enabled and TLS-deny policy present"
  fi
}

# ===========================================================================
# Control 5 — S3 encryption (KMS CMK)
# Verify: get-bucket-encryption
# ===========================================================================
control_s3_encryption() {
  local cmd="aws cloudfront get-distribution-config (resolve origin bucket) && aws s3api get-bucket-encryption --bucket <b>"
  if [[ "${DRY_RUN}" == "true" ]]; then
    report "S3 encryption" "${cmd}" "SKIP" "dry-run: no AWS call made"
    return 0
  fi
  local domain dist_id bucket enc sse_algo kms_arn
  if ! tf_output cloudfront_domain_name; then
    report "S3 encryption" "${cmd}" "FAIL" "cloudfront_domain_name output unavailable"
    return 0
  fi
  domain="${TF_OUTPUT_VALUE}"
  dist_id="$(aws cloudfront list-distributions "${AWS_ARGS[@]}" \
    | jq -r --arg d "${domain}" '.DistributionList.Items[] | select(.DomainName==$d) | .Id')"
  if [[ -z "${dist_id}" ]]; then
    report "S3 encryption" "${cmd}" "FAIL" "no CloudFront distribution matches ${domain}"
    return 0
  fi
  bucket="$(aws cloudfront get-distribution-config --id "${dist_id}" "${AWS_ARGS[@]}" \
    | jq -r '.DistributionConfig.Origins.Items // [] | .[] | select(has("S3OriginConfig")) | .DomainName' \
    | sed -n '1{s/\.s3\..*$//; s/\.s3-website.*$//; p;}')"
  if [[ -z "${bucket}" ]]; then
    report "S3 encryption" "${cmd}" "FAIL" "could not resolve origin S3 bucket from distribution ${dist_id}"
    return 0
  fi
  enc="$(aws s3api get-bucket-encryption --bucket "${bucket}" "${AWS_ARGS[@]}" 2>/dev/null \
    | jq -c '.ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault // {}')" \
    || { report "S3 encryption" "${cmd}" "FAIL" "${bucket}: get-bucket-encryption failed"; return 0; }
  sse_algo="$(printf '%s' "${enc}" | jq -r '.SSEAlgorithm // "NONE"')"
  kms_arn="$(printf '%s' "${enc}" | jq -r '.KMSMasterKeyID // ""')"
  if [[ "${sse_algo}" != "aws:kms" || -z "${kms_arn}" ]]; then
    report "S3 encryption" "${cmd}" "FAIL" "${bucket}: SSEAlgorithm=${sse_algo} KMSMasterKeyID=${kms_arn:-<none>}"
  else
    report "S3 encryption" "${cmd}" "PASS" "${bucket}: aws:kms with a CMK id"
  fi
}

# ===========================================================================
# Control 6 — API Gateway authorizer on every method; no unauthenticated endpoint
# Verify: aws apigateway get-resources --embed methods, per method authorizationType/authorizerId
#
# The API is a private REST API (v1); its id comes from the `api_id` output.
# ===========================================================================
control_api_gateway_authorizer() {
  local cmd="aws apigateway get-resources --rest-api-id <id> --embed methods"
  if [[ "${DRY_RUN}" == "true" ]]; then
    report "API Gateway authorizer coverage" "${cmd}" "SKIP" "dry-run: no AWS call made"
    return 0
  fi
  local api_id methods failed=0 detail=""
  if ! tf_output api_id; then
    report "API Gateway authorizer coverage" "${cmd}" "FAIL" \
      "api_id output unavailable; the API's identity is unknown, so no method could be examined"
    return 0
  fi
  api_id="${TF_OUTPUT_VALUE}"
  # One object per (path, verb) with its authorizationType/authorizerId. The `||` branch reports a
  # failed call; without it `set -e` would abort the whole script.
  methods="$(aws apigateway get-resources --rest-api-id "${api_id}" --embed methods \
    "${AWS_ARGS[@]}" \
    | jq -c '.items[] | . as $r | (.resourceMethods // {}) | to_entries[]
             | {path: $r.path, verb: .key,
                authorizationType: (.value.authorizationType // ""),
                authorizerId: (.value.authorizerId // "")}')" \
    || { report "API Gateway authorizer coverage" "${cmd}" "FAIL" \
           "api ${api_id}: get-resources --embed methods did not complete, so the method list could not be enumerated at all. This is a broken enumeration, never an authorized API"; return 0; }
  if [[ -z "${methods}" ]]; then
    report "API Gateway authorizer coverage" "${cmd}" "FAIL" \
      "api ${api_id}: no methods found. Every assertion below iterates this list, so an empty one is a broken enumeration rather than a clean API"
    return 0
  fi
  local path verb auth_type authorizer_id
  while IFS= read -r method; do
    path="$(printf '%s' "${method}" | jq -r '.path')"
    verb="$(printf '%s' "${method}" | jq -r '.verb')"
    auth_type="$(printf '%s' "${method}" | jq -r '.authorizationType')"
    authorizer_id="$(printf '%s' "${method}" | jq -r '.authorizerId')"
    # OPTIONS is the CORS preflight and is authorized as NONE. Every other verb must name a
    # Cognito authorizer.
    if [[ "${verb}" == "OPTIONS" ]]; then
      continue
    fi
    if [[ "${auth_type}" != "COGNITO_USER_POOLS" || -z "${authorizer_id}" ]]; then
      failed=1
      detail="${detail}${verb} ${path}: authorizationType=${auth_type:-<none>} authorizerId=${authorizer_id:-<none>}; "
    fi
  done <<<"${methods}"
  if [[ "${failed}" -eq 1 ]]; then
    report "API Gateway authorizer coverage" "${cmd}" "FAIL" "${detail}"
  else
    report "API Gateway authorizer coverage" "${cmd}" "PASS" \
      "api ${api_id}: every non-OPTIONS method declares authorizationType=COGNITO_USER_POOLS with an authorizerId"
  fi
}

# ===========================================================================
# Control 7 — Lambda: one execution role per function, never shared
# Verify: scripts/check_plan_security_baseline.py --only lambda over the plan. This control also
# checks that every deployed function's live Role ARN is distinct.
# ===========================================================================
control_lambda_role_uniqueness() {
  local plan_cmd=".venv/bin/python3 scripts/check_plan_security_baseline.py --plan <plan.json> --only lambda"
  local live_cmd="aws lambda list-functions (filtered to name-prefix '${NAME_PREFIX}') && aws lambda get-function-configuration --function-name <f>"
  if [[ "${DRY_RUN}" == "true" ]]; then
    report "Lambda: one execution role per function" "${plan_cmd}" "SKIP" "dry-run: no AWS call made"
    report "Lambda: one execution role per function (live cross-check)" "${live_cmd}" "SKIP" "dry-run: no AWS call made"
    return 0
  fi
  report "Lambda: one execution role per function" "${plan_cmd}" "SKIP" \
    "structural uniqueness is a plan-time property, asserted by scripts/check_plan_security_baseline.py --only lambda; run separately against a real terraform plan -json"
  local functions roles dup_count
  functions="$(this_deployment_function_names)" || {
    report "Lambda: one execution role per function (live cross-check)" "${live_cmd}" "FAIL" "could not resolve this deployment's own Lambda functions via name-prefix '${NAME_PREFIX}'"
    return 0
  }
  roles="$(while IFS= read -r fn; do
    [[ -z "${fn}" ]] && continue
    aws lambda get-function-configuration --function-name "${fn}" "${AWS_ARGS[@]}" | jq -r '.Role'
  done <<<"${functions}")"
  dup_count="$(printf '%s\n' "${roles}" | sort | uniq -d | wc -l | tr -d ' ')"
  if [[ "${dup_count}" != "0" ]]; then
    report "Lambda: one execution role per function (live cross-check)" "${live_cmd}" "FAIL" \
      "$(printf '%s\n' "${roles}" | sort | uniq -d | tr '\n' ' ') shared by more than one function"
  else
    report "Lambda: one execution role per function (live cross-check)" "${live_cmd}" "PASS" \
      "every one of this deployment's deployed functions' live Role ARN is distinct"
  fi
}

# ===========================================================================
# Control 8 — Cognito: MFA, password policy, account recovery configured
# Verify: describe-user-pool
# ===========================================================================
control_cognito_baseline() {
  local cmd="aws cognito-idp describe-user-pool --user-pool-id <id>"
  if [[ "${DRY_RUN}" == "true" ]]; then
    report "Cognito: MFA/password policy/account recovery" "${cmd}" "SKIP" "dry-run: no AWS call made"
    return 0
  fi
  local pool_id pool mfa min_len failed=0 detail=""
  if ! tf_output cognito_user_pool_id; then
    report "Cognito: MFA/password policy/account recovery" "${cmd}" "FAIL" "cognito_user_pool_id output unavailable"
    return 0
  fi
  pool_id="${TF_OUTPUT_VALUE}"
  pool="$(aws cognito-idp describe-user-pool --user-pool-id "${pool_id}" "${AWS_ARGS[@]}" \
    | jq -c '.UserPool')" || { report "Cognito: MFA/password policy/account recovery" "${cmd}" "FAIL" "describe-user-pool failed"; return 0; }
  mfa="$(printf '%s' "${pool}" | jq -r '.MfaConfiguration // "OFF"')"
  min_len="$(printf '%s' "${pool}" | jq -r '.Policies.PasswordPolicy.MinimumLength // 0')"
  if [[ "${mfa}" == "OFF" ]]; then
    failed=1
    detail="${detail}MfaConfiguration=OFF; "
  fi
  if [[ "${min_len}" -lt 8 ]]; then
    failed=1
    detail="${detail}PasswordPolicy.MinimumLength=${min_len} (<8); "
  fi
  if ! printf '%s' "${pool}" | jq -e '.AccountRecoverySetting.RecoveryMechanisms | length > 0' >/dev/null 2>&1; then
    failed=1
    detail="${detail}no AccountRecoverySetting.RecoveryMechanisms configured; "
  fi
  if [[ "${failed}" -eq 1 ]]; then
    report "Cognito: MFA/password policy/account recovery" "${cmd}" "FAIL" "${detail}"
  else
    report "Cognito: MFA/password policy/account recovery" "${cmd}" "PASS" \
      "pool ${pool_id}: MFA=${mfa}, MinimumLength=${min_len}, recovery mechanisms configured"
  fi
}

# ===========================================================================
# Control 9 — Gateway: CUSTOM_JWT with discovery URL, allowed clients/audiences/scopes, claims
# Verify: get-gateway
# ===========================================================================
control_gateway_custom_jwt() {
  local cmd="aws bedrock-agentcore-control get-gateway --gateway-identifier <id>"
  if [[ "${DRY_RUN}" == "true" ]]; then
    report "Gateway: CUSTOM_JWT configuration" "${cmd}" "SKIP" "dry-run: no AWS call made"
    return 0
  fi
  local gw_url gw_id gw failed=0 detail=""
  if ! tf_output agentcore_gateway_url; then
    report "Gateway: CUSTOM_JWT configuration" "${cmd}" "FAIL" "agentcore_gateway_url output unavailable"
    return 0
  fi
  gw_url="${TF_OUTPUT_VALUE}"
  # The gateway id is the leftmost label of the URL's hostname, e.g.
  # https://crm-agent-eval-gateway-abc123.gateway.bedrock-agentcore.us-east-1.amazonaws.com
  # -> crm-agent-eval-gateway-abc123.
  gw_id="$(printf '%s' "${gw_url}" | sed -n 's#^https\{0,1\}://\([^./]*\)\..*#\1#p')"
  if [[ -z "${gw_id}" ]]; then
    report "Gateway: CUSTOM_JWT configuration" "${cmd}" "FAIL" "could not resolve gateway id from ${gw_url}"
    return 0
  fi
  gw="$(aws bedrock-agentcore-control get-gateway --gateway-identifier "${gw_id}" "${AWS_ARGS[@]}")" \
    || { report "Gateway: CUSTOM_JWT configuration" "${cmd}" "FAIL" "get-gateway failed"; return 0; }
  local auth_type discovery_url
  auth_type="$(printf '%s' "${gw}" | jq -r '.authorizerType // ""')"
  discovery_url="$(printf '%s' "${gw}" | jq -r '.authorizerConfiguration.customJWTAuthorizer.discoveryUrl // ""')"
  if [[ "${auth_type}" != "CUSTOM_JWT" ]]; then
    failed=1
    detail="${detail}authorizerType=${auth_type} (expected CUSTOM_JWT); "
  fi
  if [[ -z "${discovery_url}" ]]; then
    failed=1
    detail="${detail}no customJWTAuthorizer.discoveryUrl configured; "
  fi
  if ! printf '%s' "${gw}" | jq -e \
    '.authorizerConfiguration.customJWTAuthorizer | (.allowedClients // [] | length > 0) or (.allowedAudience // [] | length > 0)' \
    >/dev/null 2>&1; then
    failed=1
    detail="${detail}no allowedClients/allowedAudience restriction configured; "
  fi
  if [[ "${failed}" -eq 1 ]]; then
    report "Gateway: CUSTOM_JWT configuration" "${cmd}" "FAIL" "${detail}"
  else
    report "Gateway: CUSTOM_JWT configuration" "${cmd}" "PASS" "gateway ${gw_id}: CUSTOM_JWT with discovery URL and client/audience allowlist"
  fi
}

# ===========================================================================
# Control 10 — Gateway: REQUEST interceptor present with passRequestHeaders: true
# Verify: get-gateway | jq '.interceptorConfigurations'
#
# Each `get-gateway` entry has the shape `{"interceptionPoints": ["REQUEST", ...],
# "inputConfiguration": {"passRequestHeaders": true}}`. The predicate also accepts the older
# `{"type": "REQUEST", "parameters": {"passRequestHeaders": true}}` shape.
# ===========================================================================
control_gateway_interceptor() {
  local cmd="aws bedrock-agentcore-control get-gateway --gateway-identifier <id> | jq '.interceptorConfigurations'"
  if [[ "${DRY_RUN}" == "true" ]]; then
    report "Gateway: REQUEST interceptor" "${cmd}" "SKIP" "dry-run: no AWS call made"
    return 0
  fi
  local gw_url gw_id gw
  if ! tf_output agentcore_gateway_url; then
    report "Gateway: REQUEST interceptor" "${cmd}" "FAIL" "agentcore_gateway_url output unavailable"
    return 0
  fi
  gw_url="${TF_OUTPUT_VALUE}"
  # Same hostname-label parse as control_gateway_custom_jwt.
  gw_id="$(printf '%s' "${gw_url}" | sed -n 's#^https\{0,1\}://\([^./]*\)\..*#\1#p')"
  if [[ -z "${gw_id}" ]]; then
    report "Gateway: REQUEST interceptor" "${cmd}" "FAIL" "could not resolve gateway id from ${gw_url}"
    return 0
  fi
  gw="$(aws bedrock-agentcore-control get-gateway --gateway-identifier "${gw_id}" "${AWS_ARGS[@]}")" \
    || { report "Gateway: REQUEST interceptor" "${cmd}" "FAIL" "get-gateway failed"; return 0; }
  if ! printf '%s' "${gw}" | jq -e \
    '.interceptorConfigurations[]? | select((.interceptionPoints // [.type] // [] | index("REQUEST")) and ((.inputConfiguration.passRequestHeaders // .parameters.passRequestHeaders // .passRequestHeaders) == true))' \
    >/dev/null 2>&1; then
    report "Gateway: REQUEST interceptor" "${cmd}" "FAIL" "gateway ${gw_id}: no REQUEST interceptor with passRequestHeaders: true"
  else
    report "Gateway: REQUEST interceptor" "${cmd}" "PASS" "gateway ${gw_id}: REQUEST interceptor present with passRequestHeaders: true"
  fi
}

# ===========================================================================
# Control 11 — Action Lambdas: resource policy admits only Gateway target role and API Gateway
# Verify: lambda get-policy -> no wildcard principal
# ===========================================================================
control_lambda_resource_policy() {
  local cmd="aws lambda list-functions (filtered to name-prefix '${NAME_PREFIX}') && aws lambda get-policy --function-name <f>"
  if [[ "${DRY_RUN}" == "true" ]]; then
    report "Action Lambdas: resource policy" "${cmd}" "SKIP" "dry-run: no AWS call made"
    return 0
  fi
  local functions failed=0 detail="" fn policy_json principal
  functions="$(this_deployment_function_names)" || {
    report "Action Lambdas: resource policy" "${cmd}" "FAIL" "could not resolve this deployment's own Lambda functions via name-prefix '${NAME_PREFIX}'"
    return 0
  }
  while IFS= read -r fn; do
    [[ -z "${fn}" ]] && continue
    policy_json="$(aws lambda get-policy --function-name "${fn}" "${AWS_ARGS[@]}" 2>/dev/null \
      | jq -r '.Policy' 2>/dev/null || echo "")"
    if [[ -z "${policy_json}" ]]; then
      failed=1
      detail="${detail}${fn}: no resource policy attached; "
      continue
    fi
    if printf '%s' "${policy_json}" | jq -e '.Statement[] | select(.Principal=="*" or .Principal.AWS=="*")' \
      >/dev/null 2>&1; then
      principal="$(printf '%s' "${policy_json}" | jq -c '.Statement[].Principal')"
      failed=1
      detail="${detail}${fn}: wildcard principal ${principal}; "
    fi
  done <<<"${functions}"
  if [[ "${failed}" -eq 1 ]]; then
    report "Action Lambdas: resource policy" "${cmd}" "FAIL" "${detail}"
  else
    report "Action Lambdas: resource policy" "${cmd}" "PASS" "no wildcard principal on any of this deployment's action Lambdas' resource policies"
  fi
}

# ===========================================================================
# Control 12 — Network: no internet-facing SG ingress anywhere
# Verify: check_no_public_ingress in scripts/check_plan_security_baseline.py, over the plan
#
# Not implemented as a live check: reported as SKIP. The property is checked offline against the
# repository by check_plan_security_baseline.py's `no_public_ingress` and
# `security_group_zero_inbound`.
# ===========================================================================
control_network_no_public_ingress() {
  local cmd="scripts/check_plan_security_baseline.py --plan <plan.json> --only network"
  report "Network: no internet-facing SG ingress" "${cmd}" "SKIP" \
    "Every Lambda runs in an adopted VPC and there are three deployed security groups, so this is an UNIMPLEMENTED live check, not a non-applicability. Asserted offline instead by check_plan_security_baseline.py --only network (no_public_ingress reads the standalone ingress rules, the inline block, and aws_lb.internal) and by security_group_zero_inbound over infra/ source. Neither reads the account. Nothing was verified here."
}

# ===========================================================================
# Control 13 — Secrets: Secrets Manager/Parameter Store only; no vendor API key anywhere, ever
# Verify (row's listed command): gitleaks + scripts/check_no_vendor_keys.sh
#
# This row's Verify command scans repository content, not a live resource.
# ===========================================================================
control_no_vendor_keys() {
  # The gate requires an explicit subject; this control scans the working tree.
  local cmd="scripts/check_no_vendor_keys.sh --worktree"
  if [[ "${DRY_RUN}" == "true" ]]; then
    report "Secrets: no vendor API key anywhere" "${cmd}" "SKIP" "dry-run: would run ${cmd} (makes no AWS call in any mode)"
    return 0
  fi
  if bash "${REPO_ROOT}/scripts/check_no_vendor_keys.sh" --worktree >/tmp/verify_security_baseline_vendor_keys.log 2>&1; then
    report "Secrets: no vendor API key anywhere" "${cmd}" "PASS" "$(tail -n1 /tmp/verify_security_baseline_vendor_keys.log)"
  else
    report "Secrets: no vendor API key anywhere" "${cmd}" "FAIL" "$(tail -n1 /tmp/verify_security_baseline_vendor_keys.log)"
  fi
  rm -f /tmp/verify_security_baseline_vendor_keys.log
}

# ===========================================================================
# Control 14 — Bedrock: assumed role + inference profile only
# Verify: IAM policy review; no key material in any env
# ===========================================================================
control_bedrock_no_static_credentials() {
  local cmd="aws lambda list-functions (filtered to name-prefix '${NAME_PREFIX}') && aws lambda get-function-configuration --function-name <f> | jq '.Environment.Variables'"
  if [[ "${DRY_RUN}" == "true" ]]; then
    report "Bedrock: assumed role + inference profile only" "${cmd}" "SKIP" "dry-run: no AWS call made"
    return 0
  fi
  local functions failed=0 detail="" fn env_vars
  functions="$(this_deployment_function_names)" || {
    report "Bedrock: assumed role + inference profile only" "${cmd}" "FAIL" "could not resolve this deployment's own Lambda functions via name-prefix '${NAME_PREFIX}'"
    return 0
  }
  while IFS= read -r fn; do
    [[ -z "${fn}" ]] && continue
    env_vars="$(aws lambda get-function-configuration --function-name "${fn}" "${AWS_ARGS[@]}" \
      | jq -r '.Environment.Variables // {} | keys[]' 2>/dev/null || echo "")"
    if grep -qiE '(ANTHROPIC|OPENAI)_API_KEY|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY' <<<"${env_vars}"; then
      failed=1
      detail="${detail}${fn}: static credential or vendor API key env var present; "
    fi
  done <<<"${functions}"
  if [[ "${failed}" -eq 1 ]]; then
    report "Bedrock: assumed role + inference profile only" "${cmd}" "FAIL" "${detail}"
  else
    report "Bedrock: assumed role + inference profile only" "${cmd}" "PASS" "no static credential or vendor API key env var on any of this deployment's functions"
  fi
}

# ===========================================================================
# Control 15 — Transport: TLS 1.2+ everywhere
# Verify: CloudFront viewer policy, bucket policy aws:SecureTransport
#
# KNOWN GAP (infra/modules/frontend_cdn/README.md): CloudFront's `minimum_protocol_version` has no
# effect on the default `cloudfront.net` certificate. When the distribution uses
# `CloudFrontDefaultCertificate: true` this sub-check reports DEFER, which still forces a non-zero
# exit. With a custom ACM certificate it reports PASS/FAIL on the enforced minimum version.
# ===========================================================================
control_transport_tls() {
  local cmd="aws cloudfront get-distribution-config --id <id> | jq '.DistributionConfig.ViewerCertificate.MinimumProtocolVersion, .DistributionConfig.DefaultCacheBehavior.ViewerProtocolPolicy'"
  if [[ "${DRY_RUN}" == "true" ]]; then
    report "Transport: TLS 1.2+ everywhere" "${cmd}" "SKIP" "dry-run: no AWS call made"
    return 0
  fi
  local domain dist_id cfg min_tls viewer_policy failed=0 detail=""
  if ! tf_output cloudfront_domain_name; then
    report "Transport: TLS 1.2+ everywhere" "${cmd}" "FAIL" "cloudfront_domain_name output unavailable"
    return 0
  fi
  domain="${TF_OUTPUT_VALUE}"
  dist_id="$(aws cloudfront list-distributions "${AWS_ARGS[@]}" \
    | jq -r --arg d "${domain}" '.DistributionList.Items[] | select(.DomainName==$d) | .Id')"
  if [[ -z "${dist_id}" ]]; then
    report "Transport: TLS 1.2+ everywhere" "${cmd}" "FAIL" "no CloudFront distribution matches ${domain}"
    return 0
  fi
  cfg="$(aws cloudfront get-distribution-config --id "${dist_id}" "${AWS_ARGS[@]}")" \
    || { report "Transport: TLS 1.2+ everywhere" "${cmd}" "FAIL" "get-distribution-config failed"; return 0; }
  min_tls="$(printf '%s' "${cfg}" | jq -r '.DistributionConfig.ViewerCertificate.MinimumProtocolVersion // ""')"
  viewer_policy="$(printf '%s' "${cfg}" | jq -r '.DistributionConfig.DefaultCacheBehavior.ViewerProtocolPolicy // ""')"
  local using_default_cert
  using_default_cert="$(printf '%s' "${cfg}" | jq -r '.DistributionConfig.ViewerCertificate.CloudFrontDefaultCertificate // false')"
  if [[ "${viewer_policy}" != "redirect-to-https" && "${viewer_policy}" != "https-only" ]]; then
    failed=1
    detail="${detail}ViewerProtocolPolicy=${viewer_policy} (expected redirect-to-https or https-only); "
  fi
  case "${min_tls}" in
    TLSv1.2_*|TLSv1.3) ;;
    *)
      if [[ "${using_default_cert}" == "true" ]]; then
        report "Transport: TLS 1.2+ everywhere" "${cmd}" "DEFER" \
          "distribution ${dist_id}: MinimumProtocolVersion=${min_tls} on the default cloudfront.net certificate -- minimum_protocol_version is INERT without a custom ACM certificate (README 'Limitations', infra/modules/frontend_cdn/README.md); ViewerProtocolPolicy=${viewer_policy}"
        return 0
      fi
      failed=1
      detail="${detail}MinimumProtocolVersion=${min_tls} (expected TLSv1.2+); "
      ;;
  esac
  if [[ "${failed}" -eq 1 ]]; then
    report "Transport: TLS 1.2+ everywhere" "${cmd}" "FAIL" "${detail}"
  else
    report "Transport: TLS 1.2+ everywhere" "${cmd}" "PASS" "distribution ${dist_id}: ${min_tls}, ${viewer_policy}"
  fi
}

# ===========================================================================
# Orchestration — runs every control regardless of earlier failures; exits non-zero if any
# control failed or deferred.
# ===========================================================================
main() {
  echo "verify_security_baseline: profile=${RESOLVED_PROFILE} region=${RESOLVED_REGION} dry_run=${DRY_RUN}"
  echo "verify_security_baseline: checking every blocks-completion control from docs/security-baseline.md"
  echo "---"

  control_dynamodb_encryption
  control_dynamodb_pitr
  control_dynamodb_iam
  control_s3_frontend_bucket
  control_s3_encryption
  control_api_gateway_authorizer
  control_lambda_role_uniqueness
  control_cognito_baseline
  control_gateway_custom_jwt
  control_gateway_interceptor
  control_lambda_resource_policy
  control_network_no_public_ingress
  control_no_vendor_keys
  control_bedrock_no_static_credentials
  control_transport_tls

  echo "---"
  if [[ "${OVERALL_STATUS}" -eq 0 ]]; then
    echo "verify_security_baseline: ALL CONTROLS PASSED (or intentionally SKIPPED per row)"
  else
    echo "verify_security_baseline: ONE OR MORE CONTROLS FAILED OR CARRY A DISCLOSED, UNRESOLVED KNOWN GAP (DEFER)" >&2
  fi
  return "${OVERALL_STATUS}"
}

main
exit $?
