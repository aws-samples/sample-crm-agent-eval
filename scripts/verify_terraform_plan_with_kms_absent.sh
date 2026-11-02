#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Checks that `terraform plan` of infra/modules/lambda_action succeeds when the DynamoDB
# customer-managed KMS key is absent or pending destruction (the KMS grant's `count` comes from
# an input variable, not from the key's ARN attribute).
#
# Offline. Needs terraform on PATH and cached providers in infra/.terraform/providers (run
# `terraform init` in infra/). Plans a fixture root module in a temporary directory with a mock
# provider; no AWS credentials, no state.
#
# Usage: bash scripts/verify_terraform_plan_with_kms_absent.sh [--keep]
#   --keep   leave the fixture workdir and plan logs in place for inspection
#
# Exit 0 = the property holds. 1 = a scenario planned the wrong way; see the emitted log.
# 2 = bad invocation. 3 = this gate verified NOTHING (no terraform, or no cached providers on
# a fresh clone) and must be reported [SKIP], never as a pass.

set -euo pipefail

KEEP=0
if [[ ${1:-} == "--keep" ]]; then
    KEEP=1
elif [[ -n ${1:-} ]]; then
    echo "usage: $0 [--keep]" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE_DIR="${REPO_ROOT}/infra/modules/lambda_action"
PLUGIN_DIR="${REPO_ROOT}/infra/.terraform/providers"

# Exit 3 = verified nothing.
if ! command -v terraform >/dev/null 2>&1; then
    echo "verify_terraform_plan_with_kms_absent: SKIP — terraform is not on PATH, so the plan-time property was not checked" >&2
    exit 3
fi
if [[ ! -d ${PLUGIN_DIR} ]]; then
    echo "verify_terraform_plan_with_kms_absent: SKIP — no cached providers at ${PLUGIN_DIR}, so the plan-time property was not checked (run \`cd infra && terraform init\` first)" >&2
    exit 3
fi

WORK_DIR="$(mktemp -d)"
cleanup() {
    if [[ ${KEEP} -eq 1 ]]; then
        echo "[info] fixture retained at ${WORK_DIR}"
    else
        rm -rf "${WORK_DIR}"
    fi
}
trap cleanup EXIT

FAILURES=0

fail() {
    echo "[FAIL] $*" >&2
    FAILURES=$((FAILURES + 1))
}

pass() {
    echo "[PASS] $*"
}

# Writes a fixture root module into $1/main.tf. $2 is the `dynamodb_kms_key_arn`
# expression, $3 the `dynamodb_kms_grant_enabled` expression — both raw HCL, so a
# scenario can hand either one a value that is unknown until apply.
write_fixture() {
    local dir="$1" kms_arn_expr="$2" kms_enabled_expr="$3"
    mkdir -p "${dir}"
    : >"${dir}/fixture.zip"
    cat >"${dir}/main.tf" <<EOF
terraform {
  required_version = ">= 1.17.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.71.0"
    }
  }
}

provider "aws" {
  region                      = "us-east-1"
  access_key                  = "mock"
  secret_key                  = "mock" # pragma: allowlist secret
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
  skip_requesting_account_id  = true
}

# Stands in for the KMS key resource mid-destroy: its id is unknown until apply.
resource "terraform_data" "pending_destroy" {}

module "action" {
  source = "${MODULE_DIR}"

  function_name    = "fixture-action"
  handler          = "crm.handlers.get_opportunity.handler"
  filename         = "${dir}/fixture.zip"
  source_code_hash = "0000000000000000000000000000000000000000000="

  reserved_concurrent_executions = 5

  dynamodb_grants = [{
    table_name = "opportunities"
    table_arn  = "arn:aws:dynamodb:us-east-1:123456789012:table/fixture-opportunities"
    actions    = ["dynamodb:GetItem"]
  }]

  dynamodb_kms_key_arn       = ${kms_arn_expr}
  dynamodb_kms_grant_enabled = ${kms_enabled_expr}

  gateway_target_role_arn = "arn:aws:iam::123456789012:role/fixture-gateway"
  eni_function_code_deny_policy_arn = "arn:aws:iam::123456789012:policy/fixture-eni-deny"

  # Non-hex placeholders, so check_no_account_id.sh does not match them as real ids.
  # This heredoc is unquoted: a backtick pair in it is command substitution.
  vpc_subnet_ids         = ["subnet-fixturezonea", "subnet-fixturezoneb"]
  vpc_security_group_ids = ["sg-fixturelambda"]
}
EOF
}

# Plans $1 and writes combined output to $1/plan.log. Echoes terraform's exit code.
plan_fixture() {
    local dir="$1"
    # `-plugin-dir` keeps init offline.
    local -a init_args=(-input=false -backend=false -no-color "-plugin-dir=${PLUGIN_DIR}")

    local rc=0
    (cd "${dir}" && terraform init "${init_args[@]}") >"${dir}/init.log" 2>&1 || rc=$?
    if [[ ${rc} -ne 0 ]]; then
        echo "init-failed:${rc}"
        return 0
    fi

    rc=0
    (cd "${dir}" && terraform plan -input=false -no-color) >"${dir}/plan.log" 2>&1 || rc=$?
    echo "${rc}"
}

# --- Scenario A: key pending destruction (ARN unknown until apply) -------------------
# The plan must succeed.
A_DIR="${WORK_DIR}/a-key-pending-destroy"
write_fixture "${A_DIR}" \
    'format("arn:aws:kms:us-east-1:123456789012:key/%s", terraform_data.pending_destroy.id)' \
    'true'
A_RC="$(plan_fixture "${A_DIR}")"
if [[ ${A_RC} == 0 ]]; then
    if grep -q 'module.action.aws_lambda_function.this' "${A_DIR}/plan.log"; then
        pass "A: plan succeeds with the KMS key ARN unknown until apply, and planned the module"
    else
        fail "A: plan exited 0 but never planned module.action — it verified nothing"
    fi
else
    fail "A: plan exited ${A_RC} with the KMS key ARN unknown until apply (regression)"
    tail -n 25 "${A_DIR}/plan.log" >&2 || true
fi

# --- Scenario B: key absent outright (ARN null, grant disabled) ----------------------
# The plan must succeed and the KMS grant must be absent.
B_DIR="${WORK_DIR}/b-key-absent"
write_fixture "${B_DIR}" 'null' 'false'
B_RC="$(plan_fixture "${B_DIR}")"
if [[ ${B_RC} == 0 ]]; then
    if ! grep -q 'fixture-action-dynamodb-kms' "${B_DIR}/plan.log"; then
        if grep -q '"fixture-action-dynamodb"' "${B_DIR}/plan.log"; then
            pass "B: plan succeeds with the key absent; KMS grant absent, table grant intact"
        else
            fail "B: plan exited 0 but the DynamoDB table grant is missing too — wrong shape"
        fi
    else
        fail "B: KMS grant policy still planned with dynamodb_kms_grant_enabled = false"
    fi
else
    fail "B: plan exited ${B_RC} with the KMS key absent"
    tail -n 25 "${B_DIR}/plan.log" >&2 || true
fi

# --- Scenario C: self-check on the harness ------------------------------------------
# Hands the count an unknown-until-apply value, which must fail with "Invalid count
# argument"; otherwise the fixture's value is not actually unknown.
C_DIR="${WORK_DIR}/c-selfcheck-count-from-attribute"
write_fixture "${C_DIR}" \
    'format("arn:aws:kms:us-east-1:123456789012:key/%s", terraform_data.pending_destroy.id)' \
    'terraform_data.pending_destroy.id != ""'
C_RC="$(plan_fixture "${C_DIR}")"
if [[ ${C_RC} == 0 ]]; then
    fail "C: self-check — a count derived from an unknown attribute planned cleanly, so this harness cannot detect the unknown-count failure mode"
elif grep -q 'Invalid count argument' "${C_DIR}/plan.log"; then
    pass "C: self-check — an attribute-derived count still fails 'Invalid count argument'"
else
    fail "C: self-check — plan exited ${C_RC} for a reason other than the count (fixture is broken, not the property)"
    tail -n 25 "${C_DIR}/plan.log" >&2 || true
fi

if [[ ${FAILURES} -ne 0 ]]; then
    echo "verify_terraform_plan_with_kms_absent: ${FAILURES} scenario(s) failed" >&2
    exit 1
fi

echo "verify_terraform_plan_with_kms_absent: all scenarios passed (plan-time property holds)"
