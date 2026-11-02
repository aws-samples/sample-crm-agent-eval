#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Applies (or destroys) the account bootstrap root `infra/bootstrap_account/`, then renders its
# outputs into the gitignored `infra/terraform.tfvars` and runs the adopted-VPC preflight on them.
#
# Usage:
#   CRM_EXPECTED_ACCOUNT_ID=<id> scripts/bootstrap_account.sh --profile NAME --region REGION \
#       [--destroy] [--no-apigw-logging]
#
#   --profile NAME        AWS profile; required, passed to every aws and terraform call
#   --region REGION       a region in infra/regions.json (default example: us-east-1)
#   --destroy             destroy the bootstrap root instead; refused while the main stack's state
#                         object (bucket and key from infra/backend.hcl) lists any resource, or is
#                         absent but has object versions or delete markers, or while the main stack
#                         uses a non-default Terraform workspace
#   --no-apigw-logging    -var manage_apigw_account_logging=false
#
# Requires `infra/backend.hcl` (written by `infra/bootstrap_state_backend.sh`); state goes under the
# key `crm-agent-eval/bootstrap-account/terraform.tfstate` in that bucket. Non-interactive.
#
# Exit codes: 0 ok; 1 a step failed (including the renderer or the preflight after a successful
# apply), or --destroy refused because the main state lists resources or has versions; 2 usage or
# refusal before any terraform call (static credentials or any AWS_ENDPOINT_URL* variable in the
# environment, missing --profile/--region, region not in infra/regions.json or the map invalid,
# CRM_EXPECTED_ACCOUNT_ID unset or not the STS account,
# no infra/backend.hcl, no bucket or key in it, a non-default workspace on --destroy).
#
# The script's own matchers run under LC_ALL=C; aws, terraform, the renderer and the preflight run
# under the caller's LC_ALL (set or unset as it was).
set -euo pipefail
if [[ -n "${LC_ALL+set}" ]]; then
    CALLER_LC_ALL_SET=1
    CALLER_LC_ALL="$LC_ALL"
else
    CALLER_LC_ALL_SET=0
    CALLER_LC_ALL=""
fi
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

readonly ROOT_DIR="infra/bootstrap_account"
readonly BACKEND_CONFIG="infra/backend.hcl"
readonly BOOTSTRAP_STATE_KEY="crm-agent-eval/bootstrap-account/terraform.tfstate"
readonly MAIN_TF_ENVIRONMENT="infra/.terraform/environment"
readonly REGION_MAP="infra/regions.json"
readonly TFVARS="infra/terraform.tfvars"
readonly TFVARS_EXAMPLE="infra/terraform.tfvars.example"

info() { printf 'bootstrap_account: %s\n' "$1"; }
refuse() { printf 'bootstrap_account: REFUSED - %s\n' "$1" >&2; exit 2; }
die() { printf 'bootstrap_account: ERROR - %s\n' "$1" >&2; exit 1; }

# Runs a child command under the caller's LC_ALL.
with_caller_locale() {
    if ((CALLER_LC_ALL_SET)); then
        LC_ALL="$CALLER_LC_ALL" "$@"
    else
        env -u LC_ALL "$@"
    fi
}

usage() {
    cat <<'USAGE'
Usage: CRM_EXPECTED_ACCOUNT_ID=<id> scripts/bootstrap_account.sh --profile NAME --region REGION
           [--destroy] [--no-apigw-logging]

  REGION is a region in infra/regions.json (default example: us-east-1).
USAGE
}

PROFILE=""
REGION=""
DESTROY=false
APIGW_LOGGING=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)
            [[ $# -ge 2 && -n "$2" ]] || refuse "--profile needs a value"
            PROFILE="$2"; shift 2 ;;
        --region)
            [[ $# -ge 2 && -n "$2" ]] || refuse "--region needs a value"
            REGION="$2"; shift 2 ;;
        --destroy) DESTROY=true; shift ;;
        --no-apigw-logging) APIGW_LOGGING=false; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; refuse "unknown argument: $1" ;;
    esac
done

if [[ -n "${AWS_ACCESS_KEY_ID:-}" || -n "${AWS_SECRET_ACCESS_KEY:-}" || -n "${AWS_SESSION_TOKEN:-}" ]]; then
    refuse "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN are set in the environment. Static credentials take precedence over AWS_PROFILE for the S3 backend, so terraform state would be read as those keys while this script reports profile='${PROFILE}'. Unset them and re-run: unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN. Nothing was applied."
fi
ENDPOINT_OVERRIDES="${!AWS_ENDPOINT_URL*}"
if [[ -n "$ENDPOINT_OVERRIDES" ]]; then
    refuse "endpoint override variable(s) set in the environment: ${ENDPOINT_OVERRIDES}. They redirect aws and terraform calls away from the real service endpoints. Unset them and re-run: unset ${ENDPOINT_OVERRIDES}. Nothing was applied."
fi
[[ -n "$PROFILE" ]] || refuse "--profile is required; nothing was applied"
[[ -n "$REGION" ]] || refuse "--region is required; nothing was applied"

PYTHON="${REPO_ROOT}/.venv/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="python3"

REGION_CHECK_RC=0
REGION_CHECK_ERR="$("$PYTHON" scripts/region_map.py az-ids --region "$REGION" 2>&1 >/dev/null)" \
    || REGION_CHECK_RC=$?
if ((REGION_CHECK_RC == 2)); then
    refuse "--region '${REGION}' is not supported: ${REGION_CHECK_ERR}. Add it to ${REGION_MAP} first; nothing was applied"
elif ((REGION_CHECK_RC != 0)); then
    refuse "cannot read the region map ${REGION_MAP} (exit ${REGION_CHECK_RC}): ${REGION_CHECK_ERR}. Nothing was applied"
fi
EXPECTED_ACCOUNT_ID="${CRM_EXPECTED_ACCOUNT_ID:-}"
[[ -n "$EXPECTED_ACCOUNT_ID" ]] \
    || refuse "CRM_EXPECTED_ACCOUNT_ID is not set; it names the only account this script may touch. Nothing was applied."
[[ -f "$BACKEND_CONFIG" ]] \
    || refuse "no ${BACKEND_CONFIG}; run infra/bootstrap_state_backend.sh first. Nothing was applied."

command -v aws >/dev/null 2>&1 || refuse "aws CLI not found on PATH"
command -v terraform >/dev/null 2>&1 || refuse "terraform not found on PATH"

export AWS_PROFILE="$PROFILE"
export AWS_REGION="$REGION"
export AWS_DEFAULT_REGION="$REGION"
AWS_ARGS=(--profile "$PROFILE" --region "$REGION" --no-cli-pager)

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

ACCOUNT_ID="$(with_caller_locale aws sts get-caller-identity "${AWS_ARGS[@]}" --query Account --output text 2>"${WORK_DIR}/sts.err")" \
    || refuse "cannot resolve the caller identity for profile '${PROFILE}': $(cat "${WORK_DIR}/sts.err"). Nothing was applied."
if [[ "$ACCOUNT_ID" != "$EXPECTED_ACCOUNT_ID" ]]; then
    refuse "WRONG ACCOUNT: profile '${PROFILE}' resolves account ${ACCOUNT_ID}, CRM_EXPECTED_ACCOUNT_ID is ${EXPECTED_ACCOUNT_ID}. Nothing was applied."
fi
info "account matches CRM_EXPECTED_ACCOUNT_ID (profile=${PROFILE} region=${REGION})"

TF_VARS=(-var "aws_profile=${PROFILE}" -var "aws_region=${REGION}")
[[ "$APIGW_LOGGING" == "true" ]] || TF_VARS+=(-var "manage_apigw_account_logging=false")

tf_init() {
    with_caller_locale terraform -chdir="$ROOT_DIR" init -input=false -reconfigure \
        -backend-config=../backend.hcl -backend-config="key=${BOOTSTRAP_STATE_KEY}"
}

# Prints the first quoted value assigned to NAME in infra/backend.hcl, or nothing.
backend_value() {
    local value
    value="$(sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$BACKEND_CONFIG")"
    printf '%s' "${value%%$'\n'*}"
}

# Refuses unless the main stack uses the default Terraform workspace, whose state is the plain key.
require_default_workspace() {
    if [[ -n "${TF_WORKSPACE:-}" && "$TF_WORKSPACE" != "default" ]]; then
        refuse "TF_WORKSPACE=${TF_WORKSPACE} is set; the destroy guard reads only the default workspace's state. Nothing was destroyed."
    fi
    if [[ -f "$MAIN_TF_ENVIRONMENT" ]]; then
        local workspace
        workspace="$(cat "$MAIN_TF_ENVIRONMENT")"
        [[ "$workspace" == "default" ]] \
            || refuse "${MAIN_TF_ENVIRONMENT} selects workspace '${workspace}'; the destroy guard reads only the default workspace's state. Nothing was destroyed."
    fi
}

# Prints how many versions and delete markers exist for exactly KEY in BUCKET.
state_version_count() {
    local bucket="$1" key="$2"
    with_caller_locale aws s3api list-object-versions --bucket "$bucket" --prefix "$key" \
        "${AWS_ARGS[@]}" --output json >"${WORK_DIR}/versions.json" 2>"${WORK_DIR}/versions.err" \
        || die "cannot list versions of s3://${bucket}/${key}: $(cat "${WORK_DIR}/versions.err"). Nothing was destroyed."
    "$PYTHON" - "${WORK_DIR}/versions.json" "$key" <<'PYEOF' \
        || die "the version listing of s3://${bucket}/${key} is unreadable. Nothing was destroyed."
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    text = handle.read().strip()
listing = json.loads(text) if text else {}
entries = (listing.get("Versions") or []) + (listing.get("DeleteMarkers") or [])
print(sum(1 for entry in entries if entry.get("Key") == sys.argv[2]))
PYEOF
}

# Prints the number of resources the main stack's state object lists; prints 0 only when the object
# has never existed (no current object, no version, no delete marker).
main_state_resource_count() {
    local bucket key versions
    bucket="$(backend_value bucket)"
    [[ -n "$bucket" ]] || refuse "${BACKEND_CONFIG} names no bucket; nothing was destroyed"
    key="$(backend_value key)"
    [[ -n "$key" ]] || refuse "${BACKEND_CONFIG} names no key; nothing was destroyed"
    require_default_workspace
    if ! with_caller_locale aws s3api get-object --bucket "$bucket" --key "$key" "${WORK_DIR}/main.tfstate" \
            "${AWS_ARGS[@]}" >/dev/null 2>"${WORK_DIR}/state.err"; then
        if grep -E 'NoSuchKey|Not Found|\(404\)' "${WORK_DIR}/state.err" >/dev/null; then
            versions="$(state_version_count "$bucket" "$key")"
            if [[ "$versions" != "0" ]]; then
                die "s3://${bucket}/${key} has no current object but ${versions} version(s) or delete marker(s): the main state was deleted, not torn down. Nothing was destroyed."
            fi
            info "s3://${bucket}/${key} has never existed: no main stack state" >&2
            printf '0\n'
            return 0
        fi
        die "cannot read s3://${bucket}/${key}: $(cat "${WORK_DIR}/state.err"). Nothing was destroyed."
    fi
    "$PYTHON" - "${WORK_DIR}/main.tfstate" <<'PYEOF' \
        || die "s3://${bucket}/${key} is not a readable state document. Nothing was destroyed."
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    resources = json.load(handle)["resources"]
if not isinstance(resources, list):
    raise SystemExit("state 'resources' is not a list")
print(len(resources))
PYEOF
}

if [[ "$DESTROY" == "true" ]]; then
    COUNT="$(main_state_resource_count)"
    if [[ "$COUNT" != "0" ]]; then
        die "the main stack state still lists ${COUNT} resource(s); run scripts/teardown.sh first. Nothing was destroyed."
    fi
    tf_init
    with_caller_locale terraform -chdir="$ROOT_DIR" destroy -input=false -auto-approve "${TF_VARS[@]}"
    info "bootstrap root destroyed"
    exit 0
fi

tf_init
with_caller_locale terraform -chdir="$ROOT_DIR" apply -input=false -auto-approve "${TF_VARS[@]}"

OUTPUTS_JSON="$(with_caller_locale terraform -chdir="$ROOT_DIR" output -json)"
printf '%s' "$OUTPUTS_JSON" | with_caller_locale "$PYTHON" scripts/render_vpc_tfvars.py \
    --outputs-json - --tfvars "$TFVARS" --example "$TFVARS_EXAMPLE" \
    || die "rendering ${TFVARS} failed; the bootstrap root IS applied. Fix the cause and re-run this script (the apply converges)."

# Prints one output's value; a list is joined with commas.
output_value() {
    "$PYTHON" -c '
import json, sys
value = json.load(sys.stdin)[sys.argv[1]]["value"]
print(",".join(value) if isinstance(value, list) else value)
' "$1" <<<"$OUTPUTS_JSON"
}

PREFLIGHT_ARGS=(--profile "$PROFILE" --region "$REGION")
for pair in vpc_id:--vpc-id private_subnet_ids:--private-subnet-ids \
            private_route_table_ids:--route-table-ids nat_public_subnet_id:--nat-subnet-id \
            runtime_subnet_ids:--runtime-subnet-ids runtime_route_table_ids:--runtime-route-table-ids; do
    name="${pair%%:*}"
    value="$(output_value "$name")" || die "terraform output carries no usable '${name}'"
    [[ -n "$value" ]] || die "terraform output '${name}' is empty; the preflight would examine nothing"
    PREFLIGHT_ARGS+=("${pair#*:}" "$value")
done

with_caller_locale bash scripts/preflight_adopted_vpc.sh "${PREFLIGHT_ARGS[@]}" \
    || die "the adopted-VPC preflight refused the bootstrap network (see above)"
info "bootstrap root applied, ${TFVARS} rendered, preflight passed"
