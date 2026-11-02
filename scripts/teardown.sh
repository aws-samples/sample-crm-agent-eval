#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Teardown entry point for crm-agent-eval: renders account-specific AgentCore
# config (so `terraform destroy` sees the same values `deploy.sh` used),
# disables deletion protection on this stack's ALB (from state), runs
# `terraform destroy`, and restores the committed account-agnostic
# template afterward, even on failure or interrupt. Prints the manual residue
# Terraform cannot remove.
#
# `terraform destroy` prompts for confirmation unless `--auto-approve` (or
# TEARDOWN_AUTO_APPROVE=true) is given. Without it and with no TTY on stdin,
# the script exits 1 up front.
#
# Usage: scripts/teardown.sh [--profile NAME] [--region REGION]
#          [--template PATH] [--auto-approve]
# Env overrides: AWS_PROFILE, AWS_REGION (same defaults: default / us-east-1),
#                TEARDOWN_AUTO_APPROVE (same effect as --auto-approve)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INFRA_DIR="${REPO_ROOT}/infra"
LOG_FILE="${LOG_FILE:-/tmp/teardown.log}"

log() { printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" | tee -a "$LOG_FILE"; }
info() { log "INFO" "$1"; }
warn() { log "WARN" "$1"; }
error() { log "ERROR" "$1" >&2; }
die() { error "$1"; exit 1; }

usage() {
    cat <<EOF
Usage: $(basename "$0") [--profile NAME] [--region REGION] [--template PATH] [--auto-approve]

Renders AgentCore config, runs terraform destroy, restores the config template.

Options:
  --profile NAME     AWS CLI profile (default: default; env AWS_PROFILE)
  --region REGION    AWS region (default: us-east-1; env AWS_REGION)
  --template PATH    Account-agnostic AgentCore config template
                      (default: \${REPO_ROOT}/.bedrock_agentcore.yaml)
  --auto-approve     Pass -auto-approve to terraform destroy. NOT the default:
                      destroy is irreversible, so its confirmation prompt is kept
                      as a safety control. Without this flag terraform WILL prompt,
                      and if stdin is not a TTY this script exits 1 before making
                      any change. Env: TEARDOWN_AUTO_APPROVE=true.
  -h, --help         Show this help
EOF
    exit 0
}

RESOLVED_PROFILE="${AWS_PROFILE:-default}"
RESOLVED_REGION="${AWS_REGION:-us-east-1}"
TEMPLATE_PATH="${REPO_ROOT}/.bedrock_agentcore.yaml"
AUTO_APPROVE="${TEARDOWN_AUTO_APPROVE:-false}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) RESOLVED_PROFILE="$2"; shift 2 ;;
        --region) RESOLVED_REGION="$2"; shift 2 ;;
        --template) TEMPLATE_PATH="$2"; shift 2 ;;
        --auto-approve) AUTO_APPROVE=true; shift ;;
        -h|--help) usage ;;
        *) die "Unknown argument: $1 (use -h for help)" ;;
    esac
done

if [[ "$AUTO_APPROVE" != "true" ]] && [[ ! -t 0 ]]; then
    die "terraform destroy needs an interactive confirmation and stdin is not a TTY. Re-run with --auto-approve (or TEARDOWN_AUTO_APPROVE=true) to confirm the irreversible destroy up front. Nothing has been changed."
fi

# ---------------------------------------------------------------------------
# Credential guard: the resolved profile/region bind both the `aws` calls and terraform
# (provider via -var, backend via the exported AWS_PROFILE/AWS_REGION).
# ---------------------------------------------------------------------------
if [[ -n "${AWS_ACCESS_KEY_ID:-}" || -n "${AWS_SESSION_TOKEN:-}" ]]; then
    die "AWS_ACCESS_KEY_ID/AWS_SESSION_TOKEN are set in the environment. Static credentials take precedence over AWS_PROFILE in every AWS SDK, so terraform would authenticate as those keys while this script reports profile='${RESOLVED_PROFILE}' — and this script DESTROYS. Unset them and re-run: unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN. Nothing has been destroyed."
fi
export AWS_PROFILE="$RESOLVED_PROFILE"
export AWS_REGION="$RESOLVED_REGION"
export AWS_DEFAULT_REGION="$RESOLVED_REGION"

command -v aws >/dev/null 2>&1 || die "aws CLI not found on PATH"
command -v terraform >/dev/null 2>&1 || die "terraform not found on PATH"
command -v python3 >/dev/null 2>&1 || die "python3 not found on PATH"

info "Resolved profile=${RESOLVED_PROFILE} region=${RESOLVED_REGION}"

AWS_ARGS=(--profile "$RESOLVED_PROFILE" --region "$RESOLVED_REGION" --no-cli-pager --output json)

info "Resolving account id via sts get-caller-identity (never written to a tracked file)"
CALLER_IDENTITY="$(aws sts get-caller-identity "${AWS_ARGS[@]}" 2>/tmp/teardown_sts_err.log)" \
    || die "Unable to resolve AWS identity for profile '${RESOLVED_PROFILE}'. $(cat /tmp/teardown_sts_err.log)"
ACCOUNT_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])' <<<"$CALLER_IDENTITY")"
[[ -n "$ACCOUNT_ID" ]] || die "Could not resolve account id from caller identity"

BACKUP_PATH="${TEMPLATE_PATH}.orig"
RENDER_SCRIPT="${SCRIPT_DIR}/render_agentcore_env.py"
RESTORED=false

restore_template() {
    if [[ "$RESTORED" == "true" ]]; then
        return
    fi
    RESTORED=true
    if [[ -f "$BACKUP_PATH" ]]; then
        info "Restoring account-agnostic AgentCore config template"
        python3 "$RENDER_SCRIPT" restore --template "$TEMPLATE_PATH" --backup "$BACKUP_PATH" \
            || warn "Restore of ${TEMPLATE_PATH} from ${BACKUP_PATH} failed — manual restore required"
    fi
}

# Restore on normal exit, on failure, and on interrupt/termination.
trap restore_template EXIT INT TERM

if [[ ! -f "$TEMPLATE_PATH" ]]; then
    warn "AgentCore config template not found at ${TEMPLATE_PATH} — skipping render step"
else
    if [[ -f "$BACKUP_PATH" ]]; then
        die "A pre-render backup already exists at ${BACKUP_PATH}: a previous run did not restore ${TEMPLATE_PATH}, so it may still hold rendered account-specific values. Restore it first (python3 ${RENDER_SCRIPT} restore --template ${TEMPLATE_PATH} --backup ${BACKUP_PATH}), then re-run."
    fi
    info "Rendering account-specific AgentCore config (destroy needs the same values apply used)"
    VALUES_JSON="$(python3 -c "import json,sys; print(json.dumps({'ACCOUNT_ID': sys.argv[1], 'AWS_REGION': sys.argv[2], 'AWS_PROFILE': sys.argv[3]}))" \
        "$ACCOUNT_ID" "$RESOLVED_REGION" "$RESOLVED_PROFILE")"
    python3 "$RENDER_SCRIPT" render \
        --template "$TEMPLATE_PATH" \
        --backup "$BACKUP_PATH" \
        --values-json "$VALUES_JSON" \
        || die "Rendering AgentCore config failed"
fi

# infra/backend.tf declares a partial `backend "s3" {}`; backend.hcl supplies it when present.
TF_INIT_ARGS=(-input=false)
BACKEND_CONFIG="${INFRA_DIR}/backend.hcl"
if [[ -f "$BACKEND_CONFIG" ]]; then
    TF_INIT_ARGS+=(-backend-config="$BACKEND_CONFIG")
else
    warn "No ${BACKEND_CONFIG}; terraform init will rely on the cached backend config in infra/.terraform (run infra/bootstrap_state_backend.sh on a fresh clone)"
fi

info "Running terraform init"
(cd "$INFRA_DIR" && terraform init "${TF_INIT_ARGS[@]}") \
    || die "terraform init failed"

# ---------------------------------------------------------------------------
# Adopted-VPC guard. Refuses to destroy shared network this stack does not own.
# ---------------------------------------------------------------------------
#
# The VPC, its subnets and route tables are adopted by id and must never appear in state. This reads
# `terraform state list`, the actual destroy subject, and refuses if any guarded type is present.
GUARDED_TYPES=(
    aws_vpc
    aws_subnet
    aws_internet_gateway
    aws_egress_only_internet_gateway
    aws_route_table
    aws_route_table_association
    aws_default_route_table
    aws_default_security_group
    aws_default_network_acl
)

info "Checking state for adopted-VPC resources that must never be destroyed"
STATE_LIST_OUTPUT=""
if ! STATE_LIST_OUTPUT=$(cd "$INFRA_DIR" && terraform state list 2>&1); then
    # An empty or absent state is a no-op teardown; any other failure aborts.
    if grep -qiE 'no state|state file|does not exist|empty' <<<"$STATE_LIST_OUTPUT"; then
        info "No state to inspect — nothing was deployed, so there is nothing to guard"
        STATE_LIST_OUTPUT=""
    else
        die "terraform state list failed, so the adopted-VPC guard could not run. Refusing to destroy blind. Output: ${STATE_LIST_OUTPUT}"
    fi
fi

FORBIDDEN_IN_STATE=""
for guarded_type in "${GUARDED_TYPES[@]}"; do
    # Whole address segment: matches `module.vpc.aws_vpc.this`, not `aws_vpc_endpoint.gateway`.
    matches=$(printf '%s\n' "$STATE_LIST_OUTPUT" | grep -E "(^|\.)${guarded_type}\.") || true
    if [[ -n "$matches" ]]; then
        FORBIDDEN_IN_STATE+="${matches}"$'\n'
    fi
done

if [[ -n "${FORBIDDEN_IN_STATE//[$'\n\t ']/}" ]]; then
    printf '%s\n' "$FORBIDDEN_IN_STATE" >&2
    die "REFUSING TO DESTROY. The addresses above are in Terraform state but belong to the ADOPTED VPC, which this stack does not own. Destroying them would delete network other workloads share — subnets, route tables or the VPC itself. This means state was written by the pre-adoption version of modules/vpc, or a later edit reintroduced a created VPC resource. Resolve deliberately: \`terraform state rm <address>\` each one after confirming the real resource must SURVIVE, then re-run. Nothing has been destroyed."
fi
info "Adopted-VPC guard passed: no VPC, subnet or route-table resource is in state"

# ---------------------------------------------------------------------------
# ALB deletion protection: off for this stack's ALB only, immediately before destroy.
# ---------------------------------------------------------------------------
ALB_STATE_ADDRESS="module.api_origin.aws_lb.this"
ALB_ARN=""
if grep -qxF "$ALB_STATE_ADDRESS" <<<"$STATE_LIST_OUTPUT"; then
    ALB_ARN="$(cd "$INFRA_DIR" && terraform output -raw api_origin_alb_arn)" \
        || die "${ALB_STATE_ADDRESS} is in state but terraform output api_origin_alb_arn failed. Nothing has been destroyed."
    ALB_ARN_PREFIX="arn:aws:elasticloadbalancing:${RESOLVED_REGION}:${ACCOUNT_ID}:loadbalancer/app/"
    [[ "$ALB_ARN" == "${ALB_ARN_PREFIX}"* ]] \
        || die "api_origin_alb_arn '${ALB_ARN}' is not an ALB in region ${RESOLVED_REGION} of account ${ACCOUNT_ID}. Nothing has been destroyed."
    info "Disabling deletion protection on ${ALB_ARN}"
    aws elbv2 modify-load-balancer-attributes \
        --load-balancer-arn "$ALB_ARN" \
        --attributes Key=deletion_protection.enabled,Value=false \
        "${AWS_ARGS[@]}" >/dev/null \
        || die "Disabling deletion protection on ${ALB_ARN} failed, so terraform destroy would fail on it. Nothing has been destroyed."
else
    info "${ALB_STATE_ADDRESS} is not in state; no ALB deletion protection to disable"
fi

TF_DESTROY_ARGS=(-input=false -var "aws_profile=${RESOLVED_PROFILE}" -var "aws_region=${RESOLVED_REGION}")
if [[ "$AUTO_APPROVE" == "true" ]]; then
    TF_DESTROY_ARGS+=(-auto-approve)
fi

# ---------------------------------------------------------------------------
# AgentCore ENI release: the Runtime's agentic_ai ENIs can outlive the Runtime and hold its SG.
# ---------------------------------------------------------------------------
RUNTIME_SG_ADDRESS="module.vpc.aws_security_group.agent_runtime"
ENI_WAIT_SECONDS="${TEARDOWN_ENI_WAIT_SECONDS:-300}"
ENI_POLL_SECONDS="${TEARDOWN_ENI_POLL_SECONDS:-15}"
[[ "$ENI_WAIT_SECONDS" =~ ^[0-9]+$ ]] \
    || die "TEARDOWN_ENI_WAIT_SECONDS must be a whole number of seconds, got '${ENI_WAIT_SECONDS}'. Nothing has been destroyed."
[[ "$ENI_POLL_SECONDS" =~ ^[0-9]+$ ]] \
    || die "TEARDOWN_ENI_POLL_SECONDS must be a whole number of seconds, got '${ENI_POLL_SECONDS}'. Nothing has been destroyed."
ENI_HELD_MESSAGE=""

# Succeeds when the only managed resource left in state is the Runtime security group.
only_runtime_sg_remains() {
    local remaining line found=false
    remaining=$(cd "$INFRA_DIR" && terraform state list 2>/dev/null) || return 1
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == data.* || "$line" == *.data.* ]] && continue
        [[ "$line" == "$RUNTIME_SG_ADDRESS" ]] || return 1
        found=true
    done <<<"$remaining"
    [[ "$found" == "true" ]]
}

# Prints the Runtime security group id from state.
runtime_sg_id() {
    local shown line
    shown=$(cd "$INFRA_DIR" && terraform state show -no-color "$RUNTIME_SG_ADDRESS") || return 1
    while IFS= read -r line; do
        if [[ "$line" =~ ^[[:space:]]*id[[:space:]]*=[[:space:]]*\"(sg-[0-9a-f]+)\" ]]; then
            printf '%s\n' "${BASH_REMATCH[1]}"
            return 0
        fi
    done <<<"$shown"
    return 1
}

# Prints the number of agentic_ai ENIs that reference security group $1.
count_agentic_enis() {
    aws ec2 describe-network-interfaces \
        --filters "Name=group-id,Values=$1" \
        --query "length(NetworkInterfaces[?InterfaceType=='agentic_ai'])" \
        "${AWS_ARGS[@]}"
}

info "Running terraform destroy (profile=${RESOLVED_PROFILE} region=${RESOLVED_REGION})"
DESTROY_RC=0
(cd "$INFRA_DIR" && terraform destroy "${TF_DESTROY_ARGS[@]}") || DESTROY_RC=$?

if [[ "$DESTROY_RC" -ne 0 ]] && only_runtime_sg_remains; then
    RUNTIME_SG_ID="$(runtime_sg_id)" || RUNTIME_SG_ID=""
    ENI_COUNT=""
    if [[ -n "$RUNTIME_SG_ID" ]]; then
        ENI_COUNT="$(count_agentic_enis "$RUNTIME_SG_ID")" || ENI_COUNT=""
    fi
    if [[ "$ENI_COUNT" =~ ^[1-9][0-9]*$ ]]; then
        info "Destroy left only ${RUNTIME_SG_ADDRESS} (${RUNTIME_SG_ID}), held by ${ENI_COUNT} AgentCore-managed agentic_ai ENI(s); waiting up to ${ENI_WAIT_SECONDS}s for AWS to release them"
        ENI_DEADLINE=$((SECONDS + ENI_WAIT_SECONDS))
        while [[ "$ENI_COUNT" != "0" ]] && (( SECONDS < ENI_DEADLINE )); do
            sleep "$ENI_POLL_SECONDS"
            ENI_COUNT="$(count_agentic_enis "$RUNTIME_SG_ID")" || { ENI_COUNT="unknown"; break; }
        done
        if [[ "$ENI_COUNT" == "0" ]]; then
            info "agentic_ai ENIs on ${RUNTIME_SG_ID} released; re-running terraform destroy once"
            DESTROY_RC=0
            (cd "$INFRA_DIR" && terraform destroy "${TF_DESTROY_ARGS[@]}") || DESTROY_RC=$?
        else
            ENI_HELD_MESSAGE="terraform destroy could not delete ${RUNTIME_SG_ADDRESS} (${RUNTIME_SG_ID}): ${ENI_COUNT} AgentCore-managed ENI(s) (InterfaceType agentic_ai) still reference it after waiting ${ENI_WAIT_SECONDS}s. Every other resource is destroyed. AgentCore releases these ENIs itself after the Runtime is gone, and AWS documents that they can persist for up to 8 hours. They cannot be detached or deleted by the account (detach is refused with OperationNotPermitted on their ela-attach attachments), so the only remedy is to wait for AgentCore to release them, then re-run scripts/teardown.sh (destroy is re-runnable). Check with: aws ec2 describe-network-interfaces --filters Name=group-id,Values=${RUNTIME_SG_ID} --profile ${RESOLVED_PROFILE} --region ${RESOLVED_REGION} --no-cli-pager"
        fi
    fi
fi

# Re-protect the ALB when destroy failed, was declined, or left it in the account.
if [[ -n "$ALB_ARN" ]]; then
    ALB_REMAINS=true
    if DESCRIBE_ERR=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" "${AWS_ARGS[@]}" 2>&1 >/dev/null); then
        ALB_REMAINS=true
    elif [[ "$DESCRIBE_ERR" == *LoadBalancerNotFound* ]]; then
        ALB_REMAINS=false
    else
        error "Could not confirm whether ${ALB_ARN} was destroyed: ${DESCRIBE_ERR}"
    fi
    if [[ "$ALB_REMAINS" == "true" ]]; then
        if aws elbv2 modify-load-balancer-attributes \
            --load-balancer-arn "$ALB_ARN" \
            --attributes Key=deletion_protection.enabled,Value=true \
            "${AWS_ARGS[@]}" >/dev/null; then
            warn "Destroy did not remove ${ALB_ARN}; deletion protection re-enabled"
        else
            error "Destroy did not remove ${ALB_ARN} and re-enabling deletion protection FAILED; it is unprotected. Re-enable it: aws elbv2 modify-load-balancer-attributes --load-balancer-arn ${ALB_ARN} --attributes Key=deletion_protection.enabled,Value=true --profile ${RESOLVED_PROFILE} --region ${RESOLVED_REGION}"
        fi
        [[ "$DESTROY_RC" -ne 0 ]] || die "terraform destroy exited 0 but ${ALB_ARN} still exists or could not be confirmed gone"
    fi
fi
[[ -z "$ENI_HELD_MESSAGE" ]] || die "$ENI_HELD_MESSAGE"
[[ "$DESTROY_RC" -eq 0 ]] || die "terraform destroy failed or was declined (exit ${DESTROY_RC})"

cat <<'RESIDUE'
Teardown complete via terraform destroy. Destroy removed every resource in state,
including the in-state S3 buckets (force_destroy) and the ECR repository
(force_delete). The following are left behind and are not removed here:
  - State backend (left by design; any redeploy needs it): the tfstate and
    tfstate-logs buckets, the lock table, and the KMS key and alias from
    infra/bootstrap_state_backend.sh. Remove it with:
      .venv/bin/python3 scripts/remove_state_backend.py --profile <p> --region <r> --yes
  - KMS keys pending deletion: each stack CMK is scheduled, not deleted. The
    window is 30 days (7 for eval_artifacts). A key is not billed while pending
    deletion; cancelling the deletion restores billing.
  - Never in state:
      the crm-agent-eval-bootstrap placeholder security group;
      CloudFront-VPCOrigins-Service-SG (AWS-managed, for the VPC origin);
      crm-agent-eval-eniprobe-* functions from an interrupted smoke run, which
      can hold Lambda security-group ENIs. Check for them BEFORE teardown;
      log groups that services create outside state.
Verify the account is clean (exit 0 = no residue):
  .venv/bin/python3 scripts/scan_residue.py --profile <p> --region <r>
RESIDUE

info "Teardown complete. See the manual-residue notice above."
