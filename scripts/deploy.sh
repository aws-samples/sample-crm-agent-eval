#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Deploy entry point for crm-agent-eval: renders account-specific AgentCore
# config into the optional, gitignored template (warns and skips when absent),
# verifies the built Lambda zips and the relay zip match the current source tree
# (and that the relay handler imports from its zip), runs `terraform apply`,
# restores the template afterward (even on failure or interrupt), copies the AgentCore runtime's tags onto its
# DEFAULT endpoint and workload identity, then builds the SPA, renders its deploy-time
# `config.json`, publishes both to the CloudFront origin bucket and invalidates
# the edge cache.
#
# Non-interactive by default: `-input=false` and `-auto-approve` are both passed.
# `--no-auto-approve` opts into Terraform's confirmation prompt; it requires a TTY
# and the script exits 1 before any AWS call when stdin is not one.
#
# Usage: scripts/deploy.sh [--profile NAME] [--region REGION]
#          [--template PATH] [--auto-approve|--no-auto-approve] [--skip-frontend]
# Env overrides: AWS_PROFILE, AWS_REGION (same defaults: default / us-east-1),
#                CRM_EXPECTED_ACCOUNT_ID (required: the 12-digit account the deploy must
#                resolve to; refused before any render, init or apply when unset, malformed,
#                or disagreeing with the account in infra/backend.hcl)
#
# Resolution order for profile/region: flag, then AWS_PROFILE/AWS_REGION env,
# then the default/us-east-1 defaults. The resolved pair is passed explicitly to
# every `aws` and `terraform` invocation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INFRA_DIR="${REPO_ROOT}/infra"
BACKEND_CONFIG="${INFRA_DIR}/backend.hcl"
LOG_FILE="${LOG_FILE:-/tmp/deploy.log}"

log() { printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" | tee -a "$LOG_FILE"; }
info() { log "INFO" "$1"; }
warn() { log "WARN" "$1"; }
error() { log "ERROR" "$1" >&2; }
die() { error "$1"; exit 1; }

#: `-lock-timeout` for every state-locking terraform call.
readonly STATE_LOCK_TIMEOUT="60s"

#: First deferred fatal reason, or empty. A failing post-apply check records its reason here so the
#: stack-outputs snapshot is still refreshed; the last block in this file re-raises it as a `die`.
DEPLOY_DEFERRED_FAILURE=""

#: Record a fatal reason without exiting. The first reason recorded is kept.
defer_failure() {
    error "$1"
    [[ -n "$DEPLOY_DEFERRED_FAILURE" ]] || DEPLOY_DEFERRED_FAILURE="$1"
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [--profile NAME] [--region REGION] [--template PATH]
                 [--auto-approve|--no-auto-approve] [--skip-frontend]

Renders AgentCore config, runs terraform apply, restores the config template,
then builds the frontend and publishes it to the CloudFront origin bucket.

Options:
  --profile NAME       AWS CLI profile (default: default; env AWS_PROFILE)
  --region REGION      AWS region (default: us-east-1; env AWS_REGION)
  --template PATH      Account-agnostic AgentCore config template
                        (default: \${REPO_ROOT}/.bedrock_agentcore.yaml)
  --auto-approve       Pass -auto-approve to terraform apply. This is the DEFAULT:
                        the script is non-interactive, so no prompt is reached.
  --no-auto-approve    Opt into terraform's review-then-confirm prompt. Requires a
                        TTY on stdin; without one the script exits 1 up front
                        rather than letting terraform fail on EOF mid-apply.
  --skip-frontend      Skip the frontend build/publish/invalidate step. The apply
                        still runs; the deployed site is then left as it was.
  -h, --help           Show this help

Environment:
  CRM_EXPECTED_ACCOUNT_ID  Required. The 12-digit account the resolved credential
                           must belong to; it must also agree with the account in
                           infra/backend.hcl when that file names one.
EOF
    exit 0
}

RESOLVED_PROFILE="${AWS_PROFILE:-default}"
RESOLVED_REGION="${AWS_REGION:-us-east-1}"
# Where each value came from, named in the wrong-account refusal.
PROFILE_SOURCE="the built-in default"
REGION_SOURCE="the built-in default"
[[ -z "${AWS_PROFILE:-}" ]] || PROFILE_SOURCE="AWS_PROFILE=${AWS_PROFILE}"
[[ -z "${AWS_REGION:-}" ]] || REGION_SOURCE="AWS_REGION=${AWS_REGION}"
TEMPLATE_PATH="${REPO_ROOT}/.bedrock_agentcore.yaml"
AUTO_APPROVE=true
PUBLISH_FRONTEND=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) RESOLVED_PROFILE="$2"; PROFILE_SOURCE="--profile"; shift 2 ;;
        --region) RESOLVED_REGION="$2"; REGION_SOURCE="--region"; shift 2 ;;
        --template) TEMPLATE_PATH="$2"; shift 2 ;;
        --auto-approve) AUTO_APPROVE=true; shift ;;
        --no-auto-approve) AUTO_APPROVE=false; shift ;;
        --skip-frontend) PUBLISH_FRONTEND=false; shift ;;
        -h|--help) usage ;;
        *) die "Unknown argument: $1 (use -h for help)" ;;
    esac
done

if [[ "$AUTO_APPROVE" != "true" ]] && [[ ! -t 0 ]]; then
    die "--no-auto-approve needs a TTY on stdin for terraform's confirmation prompt; stdin is not a terminal. Re-run without --no-auto-approve (the non-interactive default)."
fi

# ---------------------------------------------------------------------------
# Propagate the resolved profile/region to terraform.
# ---------------------------------------------------------------------------
#
# `-var` binds terraform's provider, but `infra/backend.tf`'s partial `backend "s3" {}` authenticates
# state from the environment. Exporting the resolved pair makes state and resources use the same
# credentials. Static keys in the environment would take precedence over AWS_PROFILE, so they are
# refused.
if [[ -n "${AWS_ACCESS_KEY_ID:-}" || -n "${AWS_SESSION_TOKEN:-}" ]]; then
    die "AWS_ACCESS_KEY_ID/AWS_SESSION_TOKEN are set in the environment. Static credentials take precedence over AWS_PROFILE in every AWS SDK, so terraform would authenticate as those keys while this script reports profile='${RESOLVED_PROFILE}' — a divergence this check refuses. Unset them and re-run: unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN. Nothing was applied."
fi
export AWS_PROFILE="$RESOLVED_PROFILE"
export AWS_REGION="$RESOLVED_REGION"
export AWS_DEFAULT_REGION="$RESOLVED_REGION"

# ---------------------------------------------------------------------------
# The index must carry no unmerged path.
# ---------------------------------------------------------------------------
#
# `scripts/check_lambda_zip_imports.py` reports `tracked_provenance: unavailable` and exits 0 when any
# path is unmerged, so the refusal happens here. It needs only git and runs before every
# credentialed check.
#
# The whole index is read from the work tree's toplevel, found with `rev-parse --show-toplevel
# --show-prefix`, so a conflict outside $REPO_ROOT is also seen. With no work tree at all (tarball,
# non-git checkout) it warns and proceeds; a `.git` with no resolvable work tree refuses.
#
# GIT_DIR, GIT_WORK_TREE and GIT_INDEX_FILE override `-C`, so they are unset first.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
info "Verifying the git index carries no unmerged path"
if ! command -v git >/dev/null 2>&1; then
    if [[ -e "${REPO_ROOT}/.git" ]]; then
        die "git not found on PATH, so whether ${REPO_ROOT}'s index holds an unresolved conflict cannot be established. On the deploy path an unknown answer to that is not an acceptable outcome — the artifacts would be uncertifiable against the repository and nothing downstream would say so; nothing was applied."
    fi
    warn "git was not found on PATH and ${REPO_ROOT} has no .git of its own, so whether any repository tracks this tree could not be established here. The separate artifact/repository question is NOT answered here and is not answered anywhere else either: scripts/check_lambda_zip_imports.py below asks the same git, so it will report tracked_provenance UNAVAILABLE for every zip and still exit 0."
else
    set +e
    WORKTREE_INFO="$(git -C "$REPO_ROOT" rev-parse --show-toplevel --show-prefix 2>/tmp/deploy_worktree_err.log)"
    worktree_status=$?
    set -e
    if [[ "$worktree_status" -ne 0 ]]; then
        if [[ -e "${REPO_ROOT}/.git" ]]; then
            die "could not read the git index at ${REPO_ROOT}: it has a .git, so a repository is claimed here, but git resolved no work tree from it (git rev-parse --show-toplevel --show-prefix exited ${worktree_status}): $(cat /tmp/deploy_worktree_err.log). Whether this tree holds an unresolved conflict is therefore unknown, which on the deploy path is not an acceptable outcome; nothing was applied."
        fi
        warn "git resolved no work tree from ${REPO_ROOT} (git rev-parse --show-toplevel --show-prefix exited ${worktree_status}: $(cat /tmp/deploy_worktree_err.log)) and ${REPO_ROOT} has no .git of its own, so there is no index this check can read and no unresolved conflict for it to find. The separate artifact/repository question is NOT answered here and is not answered anywhere else either: scripts/check_lambda_zip_imports.py below asks git from this same path, so it will report tracked_provenance UNAVAILABLE for every zip and still exit 0."
    else
        # `--show-toplevel --show-prefix` prints the root then the prefix, one per line; one line means
        # REPO_ROOT is the toplevel. The prefix is compared rather than paths, since `--show-toplevel` is
        # physical and REPO_ROOT is `cd`-logical.
        if [[ "$WORKTREE_INFO" == *$'\n'* ]]; then
            INDEX_ROOT="${WORKTREE_INFO%%$'\n'*}"
            REPO_PREFIX="${WORKTREE_INFO#*$'\n'}"
            warn "${REPO_ROOT} is not a checkout root of its own: git resolves it inside the work tree rooted at ${INDEX_ROOT}, at prefix '${REPO_PREFIX}'. The index that governs this tree is that one, so this check reads the whole index from ${INDEX_ROOT} — \`git ls-files\` answers only for its invocation prefix, and a conflict outside ${REPO_ROOT} would otherwise go unseen."
        else
            INDEX_ROOT="$WORKTREE_INFO"
        fi
        # `git -C ''` is a no-op, so an empty root would query the caller's cwd.
        [[ -n "$INDEX_ROOT" ]] || die "git resolved a work tree from ${REPO_ROOT} but named no root for it, so which index governs this tree is unknown; nothing was applied."
        set +e
        UNMERGED_ENTRIES="$(git -C "$INDEX_ROOT" ls-files --unmerged 2>/tmp/deploy_unmerged_err.log)"
        unmerged_status=$?
        set -e
        if [[ "$unmerged_status" -ne 0 ]]; then
            die "could not read the git index at ${INDEX_ROOT}, which git resolved as the root of the work tree holding ${REPO_ROOT} and so has one (git ls-files --unmerged exited ${unmerged_status}): $(cat /tmp/deploy_unmerged_err.log). Whether this tree holds an unresolved conflict is therefore unknown, which on the deploy path is not an acceptable outcome; nothing was applied."
        fi
        if [[ -n "$UNMERGED_ENTRIES" ]]; then
            # One entry per merge stage, so paths repeat.
            UNMERGED_PATHS="$(printf '%s\n' "$UNMERGED_ENTRIES" | cut -f2- | sort -u | tr '\n' ' ')"
            UNMERGED_PATHS="${UNMERGED_PATHS% }"
            die "the git index at ${INDEX_ROOT} holds unmerged path(s): ${UNMERGED_PATHS} — so this tree has no single resolved content, and nothing packaged from it is reproducible from the repository. The zip provenance gate below cannot refuse this: it records \`tracked_provenance: unavailable\` and still exits 0, by design. An in-progress merge, rebase, cherry-pick, revert, \`am -3\` or \`stash apply\` can each leave the index in this state, among others, and some of them leave no operation to abort — so the remedy that always applies is to resolve the listed path(s) and \`git add\` them; aborting is an alternative only where an operation is in progress, using that operation's own --abort. Then re-run this deploy; nothing was applied."
        fi
    fi
fi

command -v aws >/dev/null 2>&1 || die "aws CLI not found on PATH"
command -v terraform >/dev/null 2>&1 || die "terraform not found on PATH"
command -v python3 >/dev/null 2>&1 || die "python3 not found on PATH"

info "Resolved profile=${RESOLVED_PROFILE} region=${RESOLVED_REGION}"

AWS_ARGS=(--profile "$RESOLVED_PROFILE" --region "$RESOLVED_REGION" --no-cli-pager --output json)

info "Resolving account id via sts get-caller-identity (never written to a tracked file)"
CALLER_IDENTITY="$(aws sts get-caller-identity "${AWS_ARGS[@]}" 2>/tmp/deploy_sts_err.log)" \
    || die "Unable to resolve AWS identity for profile '${RESOLVED_PROFILE}'. $(cat /tmp/deploy_sts_err.log)"
ACCOUNT_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])' <<<"$CALLER_IDENTITY")"
[[ -n "$ACCOUNT_ID" ]] || die "Could not resolve account id from caller identity"

# ---------------------------------------------------------------------------
# The account this deploy is allowed to touch. Refused before the render, the init and the apply.
# ---------------------------------------------------------------------------
#
# The expected account is CRM_EXPECTED_ACCOUNT_ID, which is required and must be 12 digits. When the
# gitignored `infra/backend.hcl` names a state bucket (`<project>-tfstate-<account>-<region>`, built
# by `infra/bootstrap_state_backend.sh`), that account must agree with it.
EXPECTED_ACCOUNT_ID="${CRM_EXPECTED_ACCOUNT_ID:-}"
EXPECTED_ACCOUNT_SOURCE="CRM_EXPECTED_ACCOUNT_ID"
[[ -n "$EXPECTED_ACCOUNT_ID" ]] \
    || die "CRM_EXPECTED_ACCOUNT_ID is required: it names the only account this deploy may touch (resolved account ${ACCOUNT_ID}, profile=${RESOLVED_PROFILE} from ${PROFILE_SOURCE}). Set it to the account you intend to bill and re-run. Nothing was rendered, initialised or applied."
[[ "$EXPECTED_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] \
    || die "CRM_EXPECTED_ACCOUNT_ID='${EXPECTED_ACCOUNT_ID}' is not a 12-digit AWS account id. Nothing was rendered, initialised or applied."

if [[ -f "$BACKEND_CONFIG" ]]; then
    BACKEND_ACCOUNT_ID="$(sed -n 's/^[[:space:]]*bucket[[:space:]]*=[[:space:]]*"[^"]*-tfstate-\([0-9][0-9]*\)-[^"]*".*/\1/p' \
        "$BACKEND_CONFIG")"
    BACKEND_ACCOUNT_ID="${BACKEND_ACCOUNT_ID%%$'\n'*}"
    if [[ -n "$BACKEND_ACCOUNT_ID" && "$BACKEND_ACCOUNT_ID" != "$EXPECTED_ACCOUNT_ID" ]]; then
        error "EXPECTED-ACCOUNT DISAGREEMENT — refusing before anything was applied."
        error "  CRM_EXPECTED_ACCOUNT_ID                       : ${EXPECTED_ACCOUNT_ID}"
        error "  the state bucket named in ${BACKEND_CONFIG} : ${BACKEND_ACCOUNT_ID}"
        die "the two sources of the expected account disagree, so this deploy would write state for one account while applying into another. Correct CRM_EXPECTED_ACCOUNT_ID or ${BACKEND_CONFIG}. Nothing was rendered, initialised or applied."
    fi
fi

if [[ "$ACCOUNT_ID" != "$EXPECTED_ACCOUNT_ID" ]]; then
    # Both ids are printed; this output goes to stderr and /tmp, not to tracked content.
    error "WRONG ACCOUNT — refusing before anything was applied."
    error "  resolved account : ${ACCOUNT_ID}"
    error "  expected account : ${EXPECTED_ACCOUNT_ID} (from ${EXPECTED_ACCOUNT_SOURCE})"
    error "  resolved profile : ${RESOLVED_PROFILE} (from ${PROFILE_SOURCE})"
    error "  resolved region  : ${RESOLVED_REGION} (from ${REGION_SOURCE})"
    die "nothing was rendered, initialised or applied. Profile resolves flag -> AWS_PROFILE -> default, so an unset \`--profile\` takes AWS_PROFILE from your environment: pass \`--profile\` explicitly, or unset AWS_PROFILE. A wrong profile is a wrong ACCOUNT, which no amount of pinning the region protects you from. If you really do intend this account, set CRM_EXPECTED_ACCOUNT_ID deliberately."
else
    info "Expected-account assertion passed: resolved account matches ${EXPECTED_ACCOUNT_SOURCE} (profile=${RESOLVED_PROFILE} from ${PROFILE_SOURCE}, region=${RESOLVED_REGION} from ${REGION_SOURCE})"
fi

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
    info "Rendering account-specific AgentCore config"
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
if [[ -f "$BACKEND_CONFIG" ]]; then
    TF_INIT_ARGS+=(-backend-config="$BACKEND_CONFIG")
else
    warn "No ${BACKEND_CONFIG}; terraform init will rely on the cached backend config in infra/.terraform (run infra/bootstrap_state_backend.sh on a fresh clone)"
fi

# ---------------------------------------------------------------------------
# No resource may be internet-facing; refused before the apply.
# ---------------------------------------------------------------------------
#
# Runs first, before the zip checks. Exit 3 (the gate verified nothing) is fatal here.
info "Verifying no resource is internet-facing (scripts/check_no_public_exposure.py)"
EXPOSURE_CHECK_PYTHON="${REPO_ROOT}/.venv/bin/python3"
[[ -x "$EXPOSURE_CHECK_PYTHON" ]] || EXPOSURE_CHECK_PYTHON="python3"
set +e
(cd "$REPO_ROOT" && "$EXPOSURE_CHECK_PYTHON" scripts/check_no_public_exposure.py)
exposure_status=$?
set -e
case "$exposure_status" in
    0) ;;
    3) die "scripts/check_no_public_exposure.py verified nothing (exit 3) — whether this deployment exposes a public surface is unknown, which on the deploy path is not an acceptable outcome; nothing was applied." ;;
    *) die "the no-public-exposure constraint is violated by infra/ (see the findings above): no resource may be globally accessible, Lambdas execute within VPCs, and external exposure is through CloudFront only. This gate has no waiver by design — fix the Terraform, do not bypass it; nothing was applied." ;;
esac

# ---------------------------------------------------------------------------
# Adopted-VPC pre-apply checks.
# ---------------------------------------------------------------------------
#
# Read-only `describe-*` checks by `scripts/preflight_adopted_vpc.sh`, run before the zip checks.
# The ids come from the gitignored `infra/terraform.tfvars`.
TFVARS="${INFRA_DIR}/terraform.tfvars"
if [[ ! -f "$TFVARS" ]]; then
    die "no ${TFVARS}: the adopted VPC's ids (vpc_id, vpc_private_subnet_ids, vpc_private_route_table_ids) have no defaults, and tracked content must not carry them. Copy terraform.tfvars.example and fill in real ids; nothing was applied."
fi

# Read one scalar or list variable out of the tfvars. Handles only the keys below; an empty result
# is refused by the caller.
tfvars_value() {
    local key="$1"
    "$TFVARS_PYTHON" - "$TFVARS" "$key" <<'PYEOF'
import re, sys, pathlib
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
key = sys.argv[2]
listed = re.search(rf'^{key}\s*=\s*\[(.*?)\]', text, re.MULTILINE | re.DOTALL)
if listed:
    print(",".join(re.findall(r'"([^"]+)"', listed.group(1))))
    raise SystemExit(0)
scalar = re.search(rf'^{key}\s*=\s*"([^"]+)"', text, re.MULTILINE)
print(scalar.group(1) if scalar else "")
PYEOF
}

TFVARS_PYTHON="${REPO_ROOT}/.venv/bin/python3"
[[ -x "$TFVARS_PYTHON" ]] || TFVARS_PYTHON="python3"

PF_VPC_ID="$(tfvars_value vpc_id)"
PF_SUBNETS="$(tfvars_value vpc_private_subnet_ids)"
PF_TABLES="$(tfvars_value vpc_private_route_table_ids)"
PF_NAT_SUBNET="$(tfvars_value vpc_nat_public_subnet_id)"
PF_RUNTIME_SUBNETS="$(tfvars_value vpc_runtime_subnet_ids)"
PF_RUNTIME_TABLES="$(tfvars_value vpc_runtime_route_table_ids)"
PF_ORIGIN_SGS="$(tfvars_value api_origin_ingress_security_group_ids)"

for pair in "vpc_id:${PF_VPC_ID}" "vpc_private_subnet_ids:${PF_SUBNETS}" \
            "vpc_private_route_table_ids:${PF_TABLES}" \
            "vpc_runtime_subnet_ids:${PF_RUNTIME_SUBNETS}"; do
    if [[ -z "${pair#*:}" ]]; then
        die "${TFVARS} sets no usable ${pair%%:*}. The adopted-VPC preflight would then examine nothing and report OK, which is worse than not running it; nothing was applied."
    fi
done
for pair in "vpc_id:${PF_VPC_ID}" "vpc_private_subnet_ids:${PF_SUBNETS}" \
            "vpc_private_route_table_ids:${PF_TABLES}" \
            "vpc_nat_public_subnet_id:${PF_NAT_SUBNET}" \
            "vpc_runtime_subnet_ids:${PF_RUNTIME_SUBNETS}" \
            "vpc_runtime_route_table_ids:${PF_RUNTIME_TABLES}" \
            "api_origin_ingress_security_group_ids:${PF_ORIGIN_SGS}"; do
    if [[ "${pair#*:}" == *REPLACE_ME* ]]; then
        die "${TFVARS} still carries the placeholder ids from terraform.tfvars.example in ${pair%%:*}. Replace them with real ids; nothing was applied."
    fi
done

PREFLIGHT_ARGS=(--region "$RESOLVED_REGION" --vpc-id "$PF_VPC_ID"
                --private-subnet-ids "$PF_SUBNETS" --route-table-ids "$PF_TABLES"
                --runtime-subnet-ids "$PF_RUNTIME_SUBNETS")
[[ -n "$RESOLVED_PROFILE" ]] && PREFLIGHT_ARGS+=(--profile "$RESOLVED_PROFILE")
[[ -n "$PF_NAT_SUBNET" ]] && PREFLIGHT_ARGS+=(--nat-subnet-id "$PF_NAT_SUBNET")
[[ -n "$PF_RUNTIME_TABLES" ]] && PREFLIGHT_ARGS+=(--runtime-route-table-ids "$PF_RUNTIME_TABLES")

info "Verifying the adopted VPC is safe to apply into (scripts/preflight_adopted_vpc.sh)"
(cd "$REPO_ROOT" && bash scripts/preflight_adopted_vpc.sh "${PREFLIGHT_ARGS[@]}") \
    || die "the adopted-VPC pre-apply checks refused (see above). These guard shared network this stack does not own: a NAT default route on the VPC main table or on a table serving other teams' subnets changes their egress and removes it again at our teardown. Nothing was applied."

# The zips Terraform consumes must be the ones the current source tree builds: `source_code_hash`
# pins the zip, not its source. This fails the deploy rather than rebuilding. `--require-zips` makes
# an absent zip a failure too.
info "Verifying every Lambda zip matches the current source tree (scripts/check_lambda_zip_imports.py)"
ZIP_CHECK_PYTHON="${REPO_ROOT}/.venv/bin/python3"
if [[ ! -x "$ZIP_CHECK_PYTHON" ]]; then
    warn "No ${REPO_ROOT}/.venv/bin/python3; falling back to the ambient python3 for the zip and parity gates"
    ZIP_CHECK_PYTHON="python3"
fi

# The post-apply parity gate imports boto3, so its absence is reported here, before the apply.
if ! "$ZIP_CHECK_PYTHON" -c 'import boto3' >/dev/null 2>&1; then
    die "the interpreter running the deploy-time gates ('${ZIP_CHECK_PYTHON}') cannot import boto3, which the post-apply deployed<->zip parity gate needs. Create the project virtualenv and install the pinned dependencies, then re-run this deploy: python3 -m venv ${REPO_ROOT}/.venv && ${REPO_ROOT}/.venv/bin/pip install -r ${REPO_ROOT}/requirements.txt — nothing was applied."
fi
(cd "$REPO_ROOT" && "$ZIP_CHECK_PYTHON" scripts/check_lambda_zip_imports.py --require-zips) \
    || die "Lambda zips are stale, incomplete or missing relative to src/. Run scripts/package_lambdas.sh and re-run this deploy; nothing was applied."

# The same guarantee for the relay zip, in two checks:
#   check_relay_zip.py       the zip's embedded manifest still matches src/relay's compile inputs.
#   package_relay.sh
#     --verify-only          the handler imports from the zip, with its production dependency
#                            closure, as an ESM artefact with index.js at the archive root.
# Currency is checked first. Both fail the deploy rather than rebuilding.
# RELAY_ZIP is the path Terraform reads: `var.lambda_build_dir` is relative to infra/.
RELAY_ZIP="${REPO_ROOT}/infra/build/lambda/relay.zip"
if [[ -n "${LAMBDA_BUILD_DIR:-}" ]]; then
    RELAY_ZIP="${LAMBDA_BUILD_DIR}/relay.zip"
    [[ "$RELAY_ZIP" == /* ]] || RELAY_ZIP="${REPO_ROOT}/${RELAY_ZIP}"
fi
info "Verifying the relay zip matches the current src/relay tree (scripts/check_relay_zip.py)"
RELAY_ZIP_CHECK_STATUS=0
(cd "$REPO_ROOT" && "$ZIP_CHECK_PYTHON" scripts/check_relay_zip.py --require-zip --zip "$RELAY_ZIP") \
    || RELAY_ZIP_CHECK_STATUS=$?
case "$RELAY_ZIP_CHECK_STATUS" in
    0) ;;
    # Exit 3 means the gate verified nothing; fatal here, with its own message.
    3)
        die "scripts/check_relay_zip.py verified nothing (exit 3) — the relay zip's currency is unknown, which on the deploy path is not an acceptable outcome. Run scripts/package_relay.sh and re-run this deploy; nothing was applied."
        ;;
    *)
        die "the relay zip is stale, absent or unreadable relative to src/relay (differing inputs listed above). Run scripts/package_relay.sh and re-run this deploy; nothing was applied."
        ;;
esac

# A current zip can still be unloadable (a dropped production dependency, a misplaced entry point).
info "Verifying the relay handler imports from its zip (scripts/package_relay.sh --verify-only)"
(cd "$REPO_ROOT" && bash scripts/package_relay.sh --verify-only "$RELAY_ZIP") \
    || die "the relay zip's handler cannot be imported from the zip. Run scripts/package_relay.sh and re-run this deploy; nothing was applied."

info "Running terraform init"
(cd "$INFRA_DIR" && terraform init "${TF_INIT_ARGS[@]}") \
    || die "terraform init failed"

TF_APPLY_ARGS=(-input=false -var "aws_profile=${RESOLVED_PROFILE}" -var "aws_region=${RESOLVED_REGION}")
if [[ "$AUTO_APPROVE" == "true" ]]; then
    TF_APPLY_ARGS+=(-auto-approve)
fi

info "Running terraform apply (profile=${RESOLVED_PROFILE} region=${RESOLVED_REGION})"
(cd "$INFRA_DIR" && terraform apply "${TF_APPLY_ARGS[@]}") \
    || die "terraform apply failed"

# Deployed == zip: compares each action Lambda's CodeSha256 with the SHA-256 of the zip Terraform
# consumed. Runs before the drift classification below.
info "Verifying every deployed action Lambda's CodeSha256 equals its local zip digest"
(cd "$REPO_ROOT" && "$ZIP_CHECK_PYTHON" -m scripts.smoke.deployed_parity --deploy-gate \
    --profile "$RESOLVED_PROFILE" --region "$RESOLVED_REGION") \
    || die "post-apply deployed<->zip parity FAILED: at least one deployed action Lambda does not carry the code this source tree built. The apply did not land what was verified; do not treat this deployment as current."

info "Tagging the AgentCore runtime's DEFAULT endpoint and workload identity with the runtime's tags"
if AGENTCORE_RUNTIME_ARN="$(cd "$INFRA_DIR" && terraform output -raw agentcore_runtime_arn)"; then
    bash "${SCRIPT_DIR}/tag_agentcore_runtime_children.sh" --profile "$RESOLVED_PROFILE" \
        --region "$RESOLVED_REGION" --runtime-arn "$AGENTCORE_RUNTIME_ARN" \
        || defer_failure "tagging the AgentCore runtime's DEFAULT endpoint and workload identity failed; see the lines above"
else
    defer_failure "terraform output agentcore_runtime_arn is absent; the AgentCore runtime's DEFAULT endpoint and workload identity were not tagged"
fi

# Post-apply drift bound. `scripts/package_lambdas.sh` is not byte-deterministic, so an apply leaves
# Lambda packaging resources with pending changes; any pending change outside the packaging
# allow-list fails the deploy.
PLAN_DIR="${REPO_ROOT}/results/plan"
POST_APPLY_PLAN="${PLAN_DIR}/post_apply_plan.json"
# The plan is also saved as a binary file so `terraform show -json` can render attribute-level
# detail for the classifier without taking the state lock again.
# A function, so the bound is taken before the frontend publish and again after it.
check_drift_bound() {
    local label="$1" suffix="$2"
    POST_APPLY_PLAN_FILE="${PLAN_DIR}/drift_plan${suffix}.tfplan"
    POST_APPLY_PLAN_STRUCTURED="${PLAN_DIR}/drift_plan${suffix}.show.json"
    mkdir -p "$PLAN_DIR"
    info "Checking ${label} drift bound (terraform plan -detailed-exitcode)"
    set +e
    (cd "$INFRA_DIR" && terraform plan -input=false -detailed-exitcode -json \
    -lock-timeout="$STATE_LOCK_TIMEOUT" \
    -out="$POST_APPLY_PLAN_FILE" \
    -var "aws_profile=${RESOLVED_PROFILE}" -var "aws_region=${RESOLVED_REGION}") \
    > "$POST_APPLY_PLAN" 2>>"$LOG_FILE"
    PLAN_RC=$?
    set -e

    case "$PLAN_RC" in
    0)
            info "${label} plan: no changes — zero pending drift."
        ;;
    2)
        # Exit 2 is "changes present". Only the packaging-attributable resource types are accepted.
        CLASSIFY_ARGS=("$POST_APPLY_PLAN")
        if (cd "$INFRA_DIR" && terraform show -json "$POST_APPLY_PLAN_FILE") \
                > "$POST_APPLY_PLAN_STRUCTURED" 2>>"$LOG_FILE"; then
            CLASSIFY_ARGS+=(--structured-plan "$POST_APPLY_PLAN_STRUCTURED")
        else
            warn "terraform show -json over the saved post-apply plan failed; the classifier will run without attribute-level detail and will refuse to absorb a deferred policy-document re-render. See ${LOG_FILE}"
        fi
        set +e
        DRIFT_SUMMARY="$(python3 "${SCRIPT_DIR}/classify_post_apply_drift.py" "${CLASSIFY_ARGS[@]}")"
        CLASSIFY_RC=$?
        set -e
        # `${DRIFT_SUMMARY}` carries the classifier's attribution of which allowance applied.
        case "$CLASSIFY_RC" in
            0)
                warn "Post-apply plan reports pending changes, every one absorbed by a declared allowance (${DRIFT_SUMMARY}) — the per-address attribution is on stderr above. Full plan: ${POST_APPLY_PLAN}"
                ;;
            1)
                error "Post-apply drift absorbed by NO declared allowance (${DRIFT_SUMMARY}); the offending addresses and the allowance that refused each are listed above"
                defer_failure "unexplained post-apply drift — no declared allowance absorbs it, so the Lambda-packaging churn allowance is not the only thing that does not hold for this apply"
                return 1
                ;;
            *)
                defer_failure "post-apply plan classification failed; ${POST_APPLY_PLAN} could not be parsed"
                return 1
                ;;
        esac
        ;;
    *)
        defer_failure "terraform plan -detailed-exitcode failed (exit ${PLAN_RC}) after a successful apply; the ${label} drift bound could not be established. See ${LOG_FILE}"
        return 1
        ;;
    esac
    return 0
}

mkdir -p "$PLAN_DIR"
# A failed pre-publish bound blocks the publish but not the outputs snapshot below.
PUBLISH_BLOCKED=false
check_drift_bound "post-apply" "" || PUBLISH_BLOCKED=true

# ---------------------------------------------------------------------------
# Frontend publish. Terraform creates the origin bucket and the distribution but puts no site
# content in either. Runs after the parity and drift gates.
#
# `config.json` is rendered here rather than committed: it carries the Cognito user-pool and
# app-client ids. The SPA fetches it at app start and hard-fails on any missing key, so the renderer
# refuses to write a partial file.
if [[ "$PUBLISH_BLOCKED" == "true" ]]; then
    warn "Skipping frontend publish: the post-apply drift bound did not hold, so this apply is not a state worth publishing. The deploy will exit non-zero; the stack outputs below are still refreshed so no reader is handed an earlier stack's snapshot."
elif [[ "$PUBLISH_FRONTEND" != "true" ]]; then
    warn "Skipping frontend publish (--skip-frontend): the origin bucket and CloudFront cache are left as they are, so the deployed site may not match this source tree."
else
    command -v npm >/dev/null 2>&1 \
        || die "npm not found on PATH, needed to build the frontend. Install Node (see frontend/.nvmrc if present) or re-run with --skip-frontend; the infrastructure apply above has already landed."

    FRONTEND_DIR="${REPO_ROOT}/frontend"
    DIST_DIR="${FRONTEND_DIR}/dist"

    if [[ ! -d "${FRONTEND_DIR}/node_modules" ]]; then
        info "Installing frontend dependencies (npm ci)"
        (cd "$FRONTEND_DIR" && npm ci --no-audit --no-fund) || die "npm ci failed"
    fi

    info "Building the frontend (npm run build)"
    (cd "$FRONTEND_DIR" && npm run build) || die "frontend build failed; nothing was published"

    [[ -f "${DIST_DIR}/index.html" ]] \
        || die "frontend build produced no ${DIST_DIR}/index.html; refusing to publish an empty site"

    info "Reading terraform outputs for the publish targets"
    TF_OUTPUTS="$(cd "$INFRA_DIR" && terraform output -json)" \
        || die "terraform output -json failed; cannot resolve the origin bucket or distribution id"
    read_output() {
        python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]]["value"])' "$1" \
            <<<"$TF_OUTPUTS"
    }
    ORIGIN_BUCKET="$(read_output frontend_origin_bucket_name)" \
        || die "terraform output frontend_origin_bucket_name is absent; re-run the apply"
    DISTRIBUTION_ID="$(read_output cloudfront_distribution_id)" \
        || die "terraform output cloudfront_distribution_id is absent; re-run the apply"
    [[ -n "$ORIGIN_BUCKET" && -n "$DISTRIBUTION_ID" ]] \
        || die "resolved an empty origin bucket or distribution id from terraform outputs"

    info "Rendering ${DIST_DIR}/config.json from terraform outputs"
    "$ZIP_CHECK_PYTHON" "${SCRIPT_DIR}/render_frontend_config.py" \
        --region "$RESOLVED_REGION" --out "${DIST_DIR}/config.json" <<<"$TF_OUTPUTS" \
        || die "rendering config.json failed; nothing was published (the SPA would have failed to load without it)"

    # Vite copies `public/` into `dist/`, including the committed placeholder template; remove it.
    rm -f "${DIST_DIR}/config.example.json"

    # Two passes, because the cache headers differ. Hashed assets under `assets/` are immutable and
    # take a long max-age. `index.html` and `config.json` keep their names across deploys, so they are
    # uploaded with `no-store`; the managed CachingOptimized policy takes its TTLs from the origin's
    # headers.
    info "Publishing static assets to s3://${ORIGIN_BUCKET}"
    aws s3 sync "$DIST_DIR" "s3://${ORIGIN_BUCKET}" \
        --profile "$RESOLVED_PROFILE" --region "$RESOLVED_REGION" --no-cli-pager \
        --delete \
        --exclude "index.html" --exclude "config.json" \
        --cache-control "public, max-age=31536000, immutable" \
        || die "aws s3 sync of the hashed assets failed"

    aws s3 cp "${DIST_DIR}/index.html" "s3://${ORIGIN_BUCKET}/index.html" \
        --profile "$RESOLVED_PROFILE" --region "$RESOLVED_REGION" --no-cli-pager \
        --cache-control "no-store, max-age=0" --content-type "text/html; charset=utf-8" \
        || die "uploading index.html failed"

    aws s3 cp "${DIST_DIR}/config.json" "s3://${ORIGIN_BUCKET}/config.json" \
        --profile "$RESOLVED_PROFILE" --region "$RESOLVED_REGION" --no-cli-pager \
        --cache-control "no-store, max-age=0" --content-type "application/json" \
        || die "uploading config.json failed"

    # `/*`, because the SPA history-fallback function caches rewritten routes under their own keys.
    info "Invalidating CloudFront distribution ${DISTRIBUTION_ID}"
    INVALIDATION_ID="$(aws cloudfront create-invalidation \
        --distribution-id "$DISTRIBUTION_ID" --paths "/*" \
        --profile "$RESOLVED_PROFILE" --region "$RESOLVED_REGION" --no-cli-pager \
        --query 'Invalidation.Id' --output text)" \
        || die "creating the CloudFront invalidation failed; the assets are published but the edge may serve the previous build"
    info "Invalidation ${INVALIDATION_ID} created (propagation is asynchronous)"

    SITE_URL="https://$(read_output cloudfront_domain_name)"
    info "Frontend published: ${SITE_URL}"
fi

# The bound again, after the publish. Skipped when the pre-publish bound already failed.
if [[ "$PUBLISH_BLOCKED" != "true" ]]; then
    check_drift_bound "final post-publish" "_final" || true
fi

# ---------------------------------------------------------------------------
# Record the stack outputs to `results/tf-outputs.json`. Reached whether or not a post-apply check
# failed; every path either installs a current snapshot or removes the old one.
#
# Read by `scripts/smoke/harness.py`, `verify_deployment.sh --outputs` and the T4 browser config.
# Written even under `--skip-frontend`.
#
# Freshness is decided by the commit recorded in the document by
# `scripts/outputs_provenance.py --stamp` below and checked with
# `scripts/outputs_provenance.py --check`, not by mtime.
OUTPUTS_SNAPSHOT="${REPO_ROOT}/results/tf-outputs.json"
mkdir -p "$(dirname "$OUTPUTS_SNAPSHOT")"
info "Recording stack outputs to ${OUTPUTS_SNAPSHOT}"
# Fatal, with a message distinct from an apply failure. On failure the stale snapshot is removed,
# so readers see it missing rather than outdated.
outputs_write_failed() {
    rm -f "${OUTPUTS_SNAPSHOT}.partial"
    if [[ -e "$OUTPUTS_SNAPSHOT" ]]; then
        rm -f "$OUTPUTS_SNAPSHOT"
        error "removed the previous ${OUTPUTS_SNAPSHOT}: it describes an EARLIER stack, and a stale snapshot passes the freshness check whenever infra/ has not changed since. Absence is refused loudly by every reader; staleness is not."
    fi
    # Reports success only when no post-apply check failed.
    if [[ -n "$DEPLOY_DEFERRED_FAILURE" ]]; then
        error "The apply landed, but this deploy has ALREADY FAILED for another reason (${DEPLOY_DEFERRED_FAILURE}); recording the stack outputs then also failed: $1"
    else
        error "THE DEPLOY ITSELF SUCCEEDED — apply landed, both drift bounds zero, publish done. What failed is only recording the stack outputs: $1"
    fi
    die "stack outputs not recorded; re-run \`terraform output -json\` into ${OUTPUTS_SNAPSHOT} before any live verification. See ${LOG_FILE}"
}

(cd "$INFRA_DIR" && terraform output -json) > "${OUTPUTS_SNAPSHOT}.partial" 2>>"$LOG_FILE" \
    || outputs_write_failed "terraform output -json exited non-zero"
# The content is asserted before it replaces the previous snapshot, and the move is atomic.
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if isinstance(d, dict) and d else 1)' \
    "${OUTPUTS_SNAPSHOT}.partial" 2>>"$LOG_FILE" \
    || outputs_write_failed "the recorded document is not a non-empty JSON object"
# Stamped before the atomic install, so no reader sees the snapshot without its provenance.
python3 "${SCRIPT_DIR}/outputs_provenance.py" --stamp "${OUTPUTS_SNAPSHOT}.partial" 2>>"$LOG_FILE" \
    || outputs_write_failed "recording the snapshot's provenance commit failed"
mv "${OUTPUTS_SNAPSHOT}.partial" "$OUTPUTS_SNAPSHOT" \
    || outputs_write_failed "the atomic install (mv) failed"
# Counts Terraform outputs only; keys beginning `_` (the provenance block) are excluded.
info "Stack outputs recorded ($(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(len([k for k in d if not k.startswith("_")]))' "$OUTPUTS_SNAPSHOT") outputs)"

# Re-raise the first deferred fatal reason. `Deploy complete.` is printed only on a clean run.
if [[ -n "$DEPLOY_DEFERRED_FAILURE" ]]; then
    error "The stack outputs above ARE current for the apply that ran — they were refreshed before this exit so no reader is handed an earlier stack's snapshot. The deploy itself failed."
    die "$DEPLOY_DEFERRED_FAILURE"
fi

info "Deploy complete."
