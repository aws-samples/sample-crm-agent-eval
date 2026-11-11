#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Runs the Layer 4 resource-class smoke modules, then Layer 5 integration modules, then Layer 6
# end-to-end modules against a live deployment, stopping after the first failing layer. Every
# module in a layer runs even if an earlier one failed. `--layer N` runs one layer and `--only`
# runs named modules only; either marks the run PARTIAL. `--dry-run` prints what would run and makes no AWS
# call. Provisions one smoke tenant before running and
# tears it down on exit, failure or interrupt. Writes the per-run, unguarded
# results/verification-inventory.run.{json,md} for a full run, or
# results/verification-inventory.partial.run.{json,md} for a PARTIAL run; the publishable
# results/verification-inventory.{json,md} are written only by `--layer inventory`
# (scripts/consolidate_inventory.py). Requires `--i-understand-this-is-billable`.
#
# BILLABLE AND LIVE: every non-dry-run invocation calls real AWS APIs against a real account and
# writes/deletes rows under a dedicated smoke tenant.
#
# Usage:
#   scripts/verify_deployment.sh --i-understand-this-is-billable [--profile NAME]
#     [--region REGION] [--outputs PATH] [--layer N] [--only MOD[,MOD...]] [--token JWT]
#   scripts/verify_deployment.sh --dry-run [--profile NAME] [--region REGION]
#     [--outputs PATH] [--layer N] [--only MOD[,MOD...]] [--token JWT]
#
# Profile/region resolve as flag, then AWS_PROFILE/AWS_REGION, then default/us-east-1. The bearer
# token resolves as --token, then CRM_SMOKE_TOKEN, then a token minted for the smoke tenant; it is
# passed only to modules that declare --token; the Gateway modules declare --gateway-token instead,
# so they never receive this end-user token. Without a token, token-taking modules report "fail"
# for the checks they cannot make.
#
# Exit codes: 0 every check that ran reported "pass", "skip" or "defer"; 1 a layer failed or
# teardown failed; 2 usage/environment error (missing flag, bad --layer or --only value, required
# tool absent, or a supplied token refused).
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd -P)"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python3"

# `scripts.*` is a namespace package resolved from the current directory.
cd "${REPO_ROOT}"

INVENTORY_JSON="${REPO_ROOT}/results/verification-inventory.json"
INVENTORY_MD="${REPO_ROOT}/results/verification-inventory.md"
# A layer run writes its own unguarded per-run rendering to distinct paths.
RUN_INVENTORY_JSON="${REPO_ROOT}/results/verification-inventory.run.json"
RUN_INVENTORY_MD="${REPO_ROOT}/results/verification-inventory.run.md"
PARTIAL_RUN_INVENTORY_JSON="${REPO_ROOT}/results/verification-inventory.partial.run.json"
PARTIAL_RUN_INVENTORY_MD="${REPO_ROOT}/results/verification-inventory.partial.run.md"

usage() {
  cat <<EOF
Usage: ${SCRIPT_NAME} (--i-understand-this-is-billable | --dry-run) [options]

Runs Layers 4, 5, then 6 in order against a live deployment, stopping at the first failing
layer. Provisions a smoke tenant before running and tears it down after, including on
failure and on interrupt. A "skip" (an authorization boundary or authenticated route the
current invocation did not observe) is reported and counted in the summary and in both
inventory artifacts, but is not itself a failure — a missing token is instead caught as an
explicit "fail" by each token-taking script (see --token below), so an unobserved boundary
is never silently green.

Options:
  --i-understand-this-is-billable   Required opt-in to actually run live AWS checks.
  --dry-run                         Print what would run; make no AWS call, provision no tenant.
  --layer N                         Run only layer N (4, 5, or 6) instead of all three in order.
                                     The run is PARTIAL, exactly as with --only.
                                     Pass --layer inventory to run NO layer and instead
                                     consolidate every inventory artifact already on disk into
                                     results/verification-inventory.{md,json}, together with a
                                     live, read-only residue query (surviving smoke-/eval-
                                     tenant rows, non-terminal eval_runs rows, orphans at the
                                     reaping threshold). Provisions no smoke tenant, because it
                                     re-runs no check and must not add residue of its own.
  --only MOD[,MOD...]               Run only the named modules (short, e.g. iam_roles, or full,
                                     e.g. scripts.smoke.iam_roles), in their normal layer order.
                                     Repeatable. With --layer N every named module must be in
                                     layer N; it cannot be combined with --layer inventory. The
                                     run is PARTIAL: its summary and its per-run inventory say so
                                     and list every module that did not run.
  --profile NAME                    AWS CLI profile (default: default; env AWS_PROFILE).
  --region REGION                   AWS region (default: us-east-1; env AWS_REGION).
  --outputs PATH                    terraform output -json document
                                     (default: \${REPO_ROOT}/results/tf-outputs.json).
  --token JWT                       Bearer token for authenticated checks (env CRM_SMOKE_TOKEN).
                                     Without one, every token-taking script reports "fail" for
                                     the checks it cannot make, rather than "skip". The Gateway
                                     modules never receive it: they mint their own machine
                                     token (override: CRM_SMOKE_GATEWAY_TOKEN).
  -h, --help                        Show this help.
EOF
  exit 0
}

RESOLVED_PROFILE="${AWS_PROFILE:-default}"
RESOLVED_REGION="${AWS_REGION:-us-east-1}"
OUTPUTS_PATH="${REPO_ROOT}/results/tf-outputs.json"
SMOKE_TOKEN="${CRM_SMOKE_TOKEN:-}"
BILLABLE_OK=false
DRY_RUN=false
ONLY_LAYER=""
declare -a ONLY_REQUESTED=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --i-understand-this-is-billable) BILLABLE_OK=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --layer)
      [[ -n "${2:-}" ]] || { echo "${SCRIPT_NAME}: --layer requires a value" >&2; exit 2; }
      ONLY_LAYER="$2"
      shift 2
      ;;
    --only)
      [[ -n "${2:-}" ]] || { echo "${SCRIPT_NAME}: --only requires a value" >&2; exit 2; }
      IFS=',' read -r -a only_names <<< "$2"
      ONLY_REQUESTED+=("${only_names[@]+"${only_names[@]}"}")
      shift 2
      ;;
    --profile) RESOLVED_PROFILE="$2"; shift 2 ;;
    --region) RESOLVED_REGION="$2"; shift 2 ;;
    --outputs) OUTPUTS_PATH="$2"; shift 2 ;;
    --token) SMOKE_TOKEN="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "${SCRIPT_NAME}: unknown argument: $1 (use -h for help)" >&2; exit 2 ;;
  esac
done

if [[ -n "${ONLY_LAYER}" ]]; then
  case "${ONLY_LAYER}" in
    4|5|6|inventory) ;;
    *) echo "${SCRIPT_NAME}: --layer must be 4, 5, 6, or inventory (got '${ONLY_LAYER}')" >&2; exit 2 ;;
  esac
fi

if [[ "${DRY_RUN}" != "true" && "${BILLABLE_OK}" != "true" ]]; then
  echo "${SCRIPT_NAME}: refusing to run — this provisions a live smoke tenant and makes real," >&2
  echo "billable AWS calls against a live deployment. Pass --i-understand-this-is-billable to" >&2
  echo "proceed, or --dry-run to preview what would run with no AWS call and no tenant" >&2
  echo "provisioned." >&2
  exit 2
fi

command -v "${PYTHON_BIN}" >/dev/null 2>&1 \
  || { echo "${SCRIPT_NAME}: ${PYTHON_BIN} not found — run from a repo with .venv provisioned" >&2; exit 2; }

# ---------------------------------------------------------------------------
# Modules that declare --token, derived by scanning each module's AST for the '--token' constant.
# ---------------------------------------------------------------------------
TOKEN_ACCEPTING_MODULES="$("${PYTHON_BIN}" -c "
import ast
from pathlib import Path

repo_root = Path('${REPO_ROOT}')
accepting = set()

def declares_token(py_path):
    # Matches any '--token' string constant in the module.
    tree = ast.parse(py_path.read_text(encoding='utf-8'))
    return any(
        isinstance(node, ast.Constant) and node.value == '--token' for node in ast.walk(tree)
    )

for py_path in sorted((repo_root / 'scripts' / 'smoke').glob('*.py')):
    # smoke_token.py declares '--token' for its own subcommand and is not a layer module.
    #
    # This snippet is inside a double-quoted command substitution: no backtick or dollar-paren
    # anywhere in it, comments included.
    if py_path.name in ('__init__.py', 'harness.py', 'tenant.py', 'smoke_token.py'):
        continue
    if declares_token(py_path):
        accepting.add(f'scripts.smoke.{py_path.stem}')

for py_path in sorted((repo_root / 'scripts').glob('smoke_*.py')):
    if declares_token(py_path):
        accepting.add(f'scripts.{py_path.stem}')

print(' '.join(sorted(accepting)))
")"

# ---------------------------------------------------------------------------
# Layer -> module mapping. tests/unit/test_verify_deployment.py asserts it matches the modules on
# disk and their declared `LAYER` values. Comments must stay outside the arrays: the test parses
# each array up to its first close-paren and reads every double-quoted entry.
# ---------------------------------------------------------------------------
LAYER_4_MODULES=(
  "scripts.smoke.lambda_actions"
  "scripts.smoke.deployed_parity"
  "scripts.smoke.api_routes"
  "scripts.smoke.iam_roles"
  "scripts.smoke.gateway_tools"
  "scripts.smoke.agents"
  "scripts.smoke.dynamodb_tables"
  "scripts.smoke.cognito_claims"
  "scripts.smoke.cloudfront"
  "scripts.smoke.access_log_delivery"
  "scripts.smoke.relay_iam"
  "scripts.smoke.runtime_bedrock_iam"
  "scripts.smoke.runtime_inbound_auth"
  "scripts.smoke.runtime_network_posture"
  "scripts.smoke.chat_timeouts"
  "scripts.smoke.chat_path_iam"
  "scripts.smoke.public_exposure"
  "scripts.smoke.iam_policy_budget"
  "scripts.smoke.eni_function_code_deny"
  "scripts.smoke.resource_protection"
  "scripts.smoke.dynamodb_principal_deny"
  "scripts.smoke.store_principal_deny"
)
LAYER_5_MODULES=(
  "scripts.smoke.integration_ui_api_ddb"
  "scripts.smoke.integration_gateway_scope"
  "scripts.smoke.integration_proposal_chain"
  "scripts.smoke.integration_teardown_privilege"
  "scripts.smoke.three_tier_chat"
  "scripts.smoke.eval_sfn"
  "scripts.smoke.admin_authz"
  "scripts.smoke.user_settings_api"
  "scripts.smoke.mcp_chat_gateway"
  "scripts.smoke.mcp_client_compat"
)
# Layer 6 entries carry the extra flags each module is invoked with.
LAYER_6_MODULES=(
  "scripts.smoke_agent_stream --assert-incremental"
  "scripts.smoke_proposal_card"
  "scripts.smoke_text_delta_stream"
)

# ---------------------------------------------------------------------------
# --only selection. Resolved here, before any AWS call or tenant: RUN_LAYER_N_MODULES hold the
# entries that run (every entry when --only is absent), in their layer's own order.
# ---------------------------------------------------------------------------
PARTIAL_RUN=false
SELECTED_MODULES=""
declare -a RUN_LAYER_4_MODULES=("${LAYER_4_MODULES[@]}")
declare -a RUN_LAYER_5_MODULES=("${LAYER_5_MODULES[@]}")
declare -a RUN_LAYER_6_MODULES=("${LAYER_6_MODULES[@]}")

# Prints "<layer> <module>" for every entry of every layer, the module without its extra flags.
all_layer_modules() {
  local entry
  for entry in "${LAYER_4_MODULES[@]}"; do echo "4 ${entry%% *}"; done
  for entry in "${LAYER_5_MODULES[@]}"; do echo "5 ${entry%% *}"; done
  for entry in "${LAYER_6_MODULES[@]}"; do echo "6 ${entry%% *}"; done
}

if [[ ${#ONLY_REQUESTED[@]} -gt 0 ]]; then
  if [[ "${ONLY_LAYER}" == "inventory" ]]; then
    echo "${SCRIPT_NAME}: --only cannot be combined with --layer inventory, which runs no module" >&2
    exit 2
  fi
  PARTIAL_RUN=true
  for requested in "${ONLY_REQUESTED[@]}"; do
    requested="${requested// /}"
    [[ -n "${requested}" ]] || { echo "${SCRIPT_NAME}: --only has an empty module name" >&2; exit 2; }
    match_layer=""
    match_module=""
    while read -r layer module; do
      if [[ "${requested}" == "${module}" || "${requested}" == "${module##*.}" ]]; then
        match_layer="${layer}"
        match_module="${module}"
      fi
    done < <(all_layer_modules)
    if [[ -z "${match_module}" ]]; then
      echo "${SCRIPT_NAME}: --only names unknown module '${requested}'; it is in no layer's module list" >&2
      echo "known modules:" >&2
      while read -r known_layer known_module; do
        printf '  layer %s %s\n' "${known_layer}" "${known_module}" >&2
      done < <(all_layer_modules)
      exit 2
    fi
    if [[ -n "${ONLY_LAYER}" && "${match_layer}" != "${ONLY_LAYER}" ]]; then
      echo "${SCRIPT_NAME}: --only module '${requested}' is in layer ${match_layer}, outside --layer ${ONLY_LAYER}" >&2
      exit 2
    fi
    SELECTED_MODULES="${SELECTED_MODULES} ${match_module}"
  done
  SELECTED_MODULES="${SELECTED_MODULES# }"
  for layer in 4 5 6; do
    selected_entries=()
    case "${layer}" in
      4) layer_entries=("${LAYER_4_MODULES[@]}") ;;
      5) layer_entries=("${LAYER_5_MODULES[@]}") ;;
      6) layer_entries=("${LAYER_6_MODULES[@]}") ;;
    esac
    for entry in "${layer_entries[@]}"; do
      if [[ " ${SELECTED_MODULES} " == *" ${entry%% *} "* ]]; then
        selected_entries+=("${entry}")
      fi
    done
    case "${layer}" in
      4) RUN_LAYER_4_MODULES=("${selected_entries[@]+"${selected_entries[@]}"}") ;;
      5) RUN_LAYER_5_MODULES=("${selected_entries[@]+"${selected_entries[@]}"}") ;;
      6) RUN_LAYER_6_MODULES=("${selected_entries[@]+"${selected_entries[@]}"}") ;;
    esac
  done
fi

# `--layer N` alone is partial too: the run covers one layer, not the deployment.
PARTIAL_LABEL="--only"
if [[ "${PARTIAL_RUN}" != "true" && ( "${ONLY_LAYER}" == 4 || "${ONLY_LAYER}" == 5 || "${ONLY_LAYER}" == 6 ) ]]; then
  PARTIAL_RUN=true
  PARTIAL_LABEL="--layer ${ONLY_LAYER}"
  while read -r layer module; do
    [[ "${layer}" == "${ONLY_LAYER}" ]] && SELECTED_MODULES="${SELECTED_MODULES} ${module}"
  done < <(all_layer_modules)
  SELECTED_MODULES="${SELECTED_MODULES# }"
fi

# Prints the run entries of one layer, NUL-terminated.
run_layer_entries() {
  local entry
  case "$1" in
    4) for entry in "${RUN_LAYER_4_MODULES[@]+"${RUN_LAYER_4_MODULES[@]}"}"; do printf '%s\0' "${entry}"; done ;;
    5) for entry in "${RUN_LAYER_5_MODULES[@]+"${RUN_LAYER_5_MODULES[@]}"}"; do printf '%s\0' "${entry}"; done ;;
    6) for entry in "${RUN_LAYER_6_MODULES[@]+"${RUN_LAYER_6_MODULES[@]}"}"; do printf '%s\0' "${entry}"; done ;;
  esac
}

# The modules that did not run, given the space-separated modules that did.
not_run_modules() {
  local executed=" $1 "
  local layer module out=""
  while read -r layer module; do
    [[ "${executed}" == *" ${module} "* ]] || out="${out} ${module}"
  done < <(all_layer_modules)
  echo "${out# }"
}

# ---------------------------------------------------------------------------
# Smoke tenant lifecycle, driven through scripts/smoke/tenant.py's inline Python entry points.
# ---------------------------------------------------------------------------
SMOKE_RUN_ID=""
SMOKE_TENANT=""
TENANT_PROVISIONED=false
TEARDOWN_DONE=false
LAYER_EXIT_CODE=0
TEARDOWN_EXIT_CODE=0
# The smoke user minted for CRM_SMOKE_TOKEN; its Cognito user is outside the tenant and is
# removed separately. SMOKE_USER_PROVISIONED=true with an empty username means the mint failed
# after creating the user and cleaned up itself.
SMOKE_USERNAME=""
SMOKE_USER_PROVISIONED=false
SMOKE_USER_CLEANED=false
# A token older than SMOKE_TOKEN_MAX_AGE_SECONDS is re-minted before the next token-taking module;
# Cognito ID tokens expire after 60 minutes. VERIFY_SMOKE_TOKEN_MAX_AGE_SECONDS overrides it.
SMOKE_TOKEN_MAX_AGE_SECONDS="${VERIFY_SMOKE_TOKEN_MAX_AGE_SECONDS:-2700}"
case "${SMOKE_TOKEN_MAX_AGE_SECONDS}" in
  ''|*[!0123456789]*)
    echo "${SCRIPT_NAME}: VERIFY_SMOKE_TOKEN_MAX_AGE_SECONDS must be a whole number of seconds" >&2
    exit 2
    ;;
esac
SMOKE_TOKEN_ISSUED_AT=0
SKIPPED_CHECK_COUNT=0
declare -a SKIPPED_RESOURCE_CLASSES=()
# A "defer" is a disclosed control that is not verified and not enforced.
DEFERRED_CHECK_COUNT=0
declare -a DEFERRED_RESOURCE_CLASSES=()

provision_tenant() {
  local result
  result="$("${PYTHON_BIN}" -c '
import json
import sys

from crm.store.store import Store
from scripts.smoke.harness import assert_account_matches_outputs, smoke_context
from scripts.smoke.tenant import allocate_smoke_tenant, seed_smoke_tenant

profile, region, outputs_path = sys.argv[1], sys.argv[2], sys.argv[3]
ctx = smoke_context(profile=profile, region=region, outputs=outputs_path)
assert_account_matches_outputs(ctx)
table_names = ctx.outputs.require_mapping("dynamodb_table_names")
# Physical table names are "<table_prefix><logical name>".
table_prefix = table_names["eval_runs"].removesuffix("eval_runs")
store = Store(client=ctx.client("dynamodb"), table_prefix=table_prefix)
smoke_tenant = allocate_smoke_tenant(store)
seed_smoke_tenant(store, smoke_tenant)
print(json.dumps({"run_id": smoke_tenant.run_id, "tenant": smoke_tenant.tenant}))
' "${RESOLVED_PROFILE}" "${RESOLVED_REGION}" "${OUTPUTS_PATH}")"
  SMOKE_RUN_ID="$(printf '%s' "${result}" | "${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')"
  SMOKE_TENANT="$(printf '%s' "${result}" | "${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin)["tenant"])')"
  TENANT_PROVISIONED=true
  echo "${SCRIPT_NAME}: provisioned smoke tenant run_id=${SMOKE_RUN_ID} tenant=${SMOKE_TENANT}"
}

# Mints a bearer token for this run's tenant, unless the caller supplied one. Must run after
# provision_tenant: the token's `custom:tenant` claim comes from a `reps` row in that tenant.
# A mint failure warns and continues; token-taking modules then report "fail".
mint_smoke_token() {
  if [[ -n "${SMOKE_TOKEN}" ]]; then
    # A supplied token must name this run's tenant; a mismatch is fatal.
    echo "${SCRIPT_NAME}: asserting the caller-supplied token names tenant ${SMOKE_TENANT}"
    if "${PYTHON_BIN}" -m scripts.smoke.smoke_token \
      --profile "${RESOLVED_PROFILE}" --region "${RESOLVED_REGION}" \
      --outputs "${OUTPUTS_PATH}" \
      assert-token --token "${SMOKE_TOKEN}" --tenant "${SMOKE_TENANT}" >/dev/null; then
      echo "${SCRIPT_NAME}: supplied token accepted; not minting a smoke user."
      # It names a tenant provisioned moments ago, so it cannot have been issued before now.
      SMOKE_TOKEN_ISSUED_AT="$(date +%s)"
      return 0
    fi
    echo "${SCRIPT_NAME}: REFUSING the supplied --token / CRM_SMOKE_TOKEN." >&2
    echo "The smoke tenant is allocated PER RUN (${SMOKE_TENANT}), and a token carries its tenant in" >&2
    echo "its own custom:tenant claim, stamped at sign-in from the user's reps row. So a token" >&2
    echo "minted before this run started names a different tenant by construction, and every module" >&2
    echo "would write rows this run's teardown cannot reap." >&2
    echo "Do not pass --token: omit it and this script mints one AFTER provisioning the tenant," >&2
    echo "which is the only ordering that can produce a correct claim." >&2
    return 1
  fi
  local result
  SMOKE_USER_PROVISIONED=true
  if ! result="$("${PYTHON_BIN}" -m scripts.smoke.smoke_token \
    --profile "${RESOLVED_PROFILE}" --region "${RESOLVED_REGION}" \
    --outputs "${OUTPUTS_PATH}" \
    mint --run-id "${SMOKE_RUN_ID}" --tenant "${SMOKE_TENANT}")"; then
    echo "${SCRIPT_NAME}: WARNING — could not mint a smoke token; every authenticated check will" >&2
    echo "report fail, not skip. The mint's own cleanup has already removed any partial user." >&2
    return 0
  fi
  SMOKE_TOKEN="$(printf '%s' "${result}" | "${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin)["id_token"])')"
  SMOKE_USERNAME="$(printf '%s' "${result}" | "${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin)["username"])')"
  local minted_tenant
  minted_tenant="$(printf '%s' "${result}" | "${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin)["tenant"])')"
  # The minted tenant is re-checked here, where the modules receive it.
  if [[ "${minted_tenant}" != "${SMOKE_TENANT}" ]]; then
    SMOKE_TOKEN=""
    echo "${SCRIPT_NAME}: minted token names tenant '${minted_tenant}', not this run's" >&2
    echo "'${SMOKE_TENANT}'. Discarding it rather than writing rows teardown will not reap." >&2
    return 0
  fi
  SMOKE_TOKEN_ISSUED_AT="$(date +%s)"
  echo "${SCRIPT_NAME}: minted smoke token for tenant=${SMOKE_TENANT} (user ${SMOKE_USERNAME})"
}

# Before a token-taking module: replaces a token issued SMOKE_TOKEN_MAX_AGE_SECONDS or more ago,
# removing the previous minted user first.
ensure_fresh_smoke_token() {
  local module="$1"
  [[ -n "${SMOKE_TOKEN}" ]] || return 0
  [[ " ${TOKEN_ACCEPTING_MODULES} " == *" ${module} "* ]] || return 0
  local age=$(( $(date +%s) - SMOKE_TOKEN_ISSUED_AT ))
  [[ "${age}" -ge "${SMOKE_TOKEN_MAX_AGE_SECONDS}" ]] || return 0
  echo "${SCRIPT_NAME}: smoke token is ${age}s old (limit ${SMOKE_TOKEN_MAX_AGE_SECONDS}s); re-minting before ${module}"
  teardown_smoke_user || true
  SMOKE_USER_PROVISIONED=false
  SMOKE_USER_CLEANED=false
  SMOKE_USERNAME=""
  SMOKE_TOKEN=""
  mint_smoke_token || true
}

# shellcheck disable=SC2329  # invoked indirectly, from on_exit via the EXIT/INT/TERM trap.
teardown_smoke_user() {
  if [[ "${SMOKE_USER_CLEANED}" == "true" || "${SMOKE_USER_PROVISIONED}" != "true" ]]; then
    return 0
  fi
  SMOKE_USER_CLEANED=true
  if [[ -z "${SMOKE_USERNAME}" ]]; then
    echo "${SCRIPT_NAME}: no smoke username to remove (the mint did not complete); its own cleanup ran."
    return 0
  fi
  echo "${SCRIPT_NAME}: removing smoke user ${SMOKE_USERNAME}"
  if "${PYTHON_BIN}" -m scripts.smoke.smoke_token \
    --profile "${RESOLVED_PROFILE}" --region "${RESOLVED_REGION}" \
    --outputs "${OUTPUTS_PATH}" \
    delete-user --username "${SMOKE_USERNAME}"; then
    return 0
  fi
  TEARDOWN_EXIT_CODE=1
  echo "${SCRIPT_NAME}: WARNING — removal of smoke user ${SMOKE_USERNAME} FAILED; a live Cognito" >&2
  echo "user able to sign in may remain. Investigate and delete it manually." >&2
  return 1
}

# shellcheck disable=SC2329  # invoked indirectly, from on_exit via the EXIT/INT/TERM trap.
teardown_tenant() {
  if [[ "${TEARDOWN_DONE}" == "true" || "${TENANT_PROVISIONED}" != "true" ]]; then
    return 0
  fi
  TEARDOWN_DONE=true
  echo "${SCRIPT_NAME}: tearing down smoke tenant run_id=${SMOKE_RUN_ID} tenant=${SMOKE_TENANT}"
  # shellcheck disable=SC2016  # single-quoted on purpose; values arrive as argv.
  if "${PYTHON_BIN}" -c '
import sys

from crm.store.store import Store
from scripts.smoke.harness import smoke_context
from scripts.smoke.tenant import SmokeTenant, teardown_smoke_tenant

profile, region, run_id, tenant, outputs_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
# No account assertion here: it needs a live sts call, and a transient failure would abandon rows.
ctx = smoke_context(profile=profile, region=region, outputs=outputs_path)
table_names = ctx.outputs.require_mapping("dynamodb_table_names")
table_prefix = table_names["eval_runs"].removesuffix("eval_runs")
store = Store(client=ctx.client("dynamodb"), table_prefix=table_prefix)
teardown_smoke_tenant(store, SmokeTenant(run_id=run_id, tenant=tenant))
' "${RESOLVED_PROFILE}" "${RESOLVED_REGION}" "${SMOKE_RUN_ID}" "${SMOKE_TENANT}" "${OUTPUTS_PATH}"; then
    return 0
  fi
  TEARDOWN_EXIT_CODE=1
  echo "${SCRIPT_NAME}: WARNING — teardown of smoke tenant ${SMOKE_TENANT} FAILED; it may still" >&2
  echo "hold live, billable rows. Investigate and reap manually via scripts/reap_eval_tenants.py." >&2
  return 1
}

# Teardown runs on normal exit, on a failing layer, and on interrupt/termination.
# shellcheck disable=SC2329  # invoked indirectly, via `trap on_exit EXIT INT TERM` below.
on_exit() {
  local rc=$?
  local signal="${1:-}"
  if [[ "${signal}" == "INT" ]]; then
    rc=130
  elif [[ "${signal}" == "TERM" ]]; then
    rc=143
  fi
  # The user is removed first; each step sets TEARDOWN_EXIT_CODE on failure.
  teardown_smoke_user || true
  teardown_tenant || true
  # A teardown failure becomes the exit code only when nothing else failed.
  if [[ "${LAYER_EXIT_CODE}" -ne 0 ]]; then
    exit "${LAYER_EXIT_CODE}"
  fi
  if [[ "${rc}" -ne 0 ]]; then
    exit "${rc}"
  fi
  exit "${TEARDOWN_EXIT_CODE}"
}
trap 'on_exit' EXIT
trap 'on_exit INT' INT
trap 'on_exit TERM' TERM

# ---------------------------------------------------------------------------
# Inventory aggregation. Collects every module's --json object into INVENTORY_RESULTS, including
# from modules that failed.
# ---------------------------------------------------------------------------
INVENTORY_RESULTS=()
EXECUTED_MODULES=""

# Builds the argument vector a module is invoked with, for both run_module and print_dry_layer.
# --token is appended only when a token is set and the module is in TOKEN_ACCEPTING_MODULES.
# Prints NUL-terminated arguments; read them back with
# `while IFS= read -r -d '' arg; do ...; done < <(build_module_args ...)`, which works on bash 3.2.
#
# Usage: build_module_args <module_and_args> <tenant_value> <token_value> <redact:true|false>
build_module_args() {
  local module_and_args="$1"
  local tenant_value="$2"
  local token_value="$3"
  local redact="$4"
  local -a module_args
  read -r -a module_args <<< "${module_and_args}"
  local -a out_args=(-m "${module_args[@]}")
  out_args+=(--profile "${RESOLVED_PROFILE}" --region "${RESOLVED_REGION}" --outputs "${OUTPUTS_PATH}")
  if [[ -n "${tenant_value}" ]]; then
    out_args+=(--tenant "${tenant_value}")
  fi
  if [[ -n "${token_value}" ]] && [[ " ${TOKEN_ACCEPTING_MODULES} " == *" ${module_args[0]} "* ]]; then
    if [[ "${redact}" == "true" ]]; then
      out_args+=(--token "<redacted, if set>")
    else
      out_args+=(--token "${token_value}")
    fi
  fi
  out_args+=(--json)
  local arg
  for arg in "${out_args[@]}"; do
    printf '%s\0' "${arg}"
  done
}

run_module() {
  # Runs one module under --json and appends its result to INVENTORY_RESULTS, also on a non-zero
  # exit, which is returned. Counts "skip" and "defer" checks for the summary.
  local module_and_args="$1"
  local layer_number="$2"
  local -a module_args
  read -r -a module_args <<< "${module_and_args}"
  ensure_fresh_smoke_token "${module_args[0]}"
  local -a args
  args=()
  local arg
  while IFS= read -r -d '' arg; do
    args+=("${arg}")
  done < <(build_module_args "${module_and_args}" "${SMOKE_TENANT}" "${SMOKE_TOKEN}" "false")
  local json_output rc
  EXECUTED_MODULES="${EXECUTED_MODULES} ${module_args[0]}"
  set +e
  json_output="$("${PYTHON_BIN}" "${args[@]}" 2>>"${STDERR_LOG}")"
  rc=$?
  set -e
  if [[ -z "${json_output}" ]]; then
    # No parseable stdout is recorded as a failed resource class.
    local resource_class="${module_args[0]##*.}"
    json_output="$(printf '{"layer": %s, "resource_class": "%s", "checks": [{"resource": "%s", "check": "invocation", "status": "fail", "detail": "no parseable output; see %s"}]}' \
      "${layer_number}" "${resource_class}" "${resource_class}" "${STDERR_LOG}")"
    rc=1
  else
    local status_counts skip_count defer_count
    status_counts="$(printf '%s' "${json_output}" | "${PYTHON_BIN}" -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except ValueError:
    print("0 0")
else:
    print(
        sum(1 for c in data.get("checks", []) if c.get("status") == "skip"),
        sum(1 for c in data.get("checks", []) if c.get("status") == "defer"),
    )
')"
    read -r skip_count defer_count <<<"${status_counts}"
    if [[ "${skip_count}" -gt 0 ]]; then
      SKIPPED_CHECK_COUNT=$((SKIPPED_CHECK_COUNT + skip_count))
      SKIPPED_RESOURCE_CLASSES+=("${module_args[0]##*.} (${skip_count})")
    fi
    if [[ "${defer_count}" -gt 0 ]]; then
      DEFERRED_CHECK_COUNT=$((DEFERRED_CHECK_COUNT + defer_count))
      DEFERRED_RESOURCE_CLASSES+=("${module_args[0]##*.} (${defer_count})")
    fi
  fi
  INVENTORY_RESULTS+=("${json_output}")
  return "${rc}"
}

run_layer() {
  local layer_number="$1"
  shift
  local -a modules=("$@")
  echo "${SCRIPT_NAME}: --- Layer ${layer_number} ---"
  local layer_failed=0
  local module
  for module in "${modules[@]}"; do
    echo "${SCRIPT_NAME}: running ${module}"
    if ! run_module "${module}" "${layer_number}"; then
      layer_failed=1
      echo "${SCRIPT_NAME}: FAILED: ${module}" >&2
    fi
  done
  return "${layer_failed}"
}

write_inventory() {
  # Per-run rendering only, without consolidate_inventory.py's publication guards.
  local json_array not_run out_json out_md
  if [[ "${PARTIAL_RUN}" == "true" ]]; then
    out_json="${PARTIAL_RUN_INVENTORY_JSON}"
    out_md="${PARTIAL_RUN_INVENTORY_MD}"
  else
    out_json="${RUN_INVENTORY_JSON}"
    out_md="${RUN_INVENTORY_MD}"
  fi
  not_run="$(not_run_modules "${EXECUTED_MODULES# }")"
  # A full run is a bare array of module results; a --only run is an object marked partial, so it
  # cannot be read as a full run's inventory.
  json_array="$(printf '%s\n' "${INVENTORY_RESULTS[@]+"${INVENTORY_RESULTS[@]}"}" \
    | VD_PARTIAL="${PARTIAL_RUN}" VD_PARTIAL_LABEL="${PARTIAL_LABEL}" \
      VD_SELECTED="${SELECTED_MODULES}" VD_NOT_RUN="${not_run}" \
      "${PYTHON_BIN}" -c '
import json
import os
import sys

partial = os.environ["VD_PARTIAL"] == "true"
selected, not_run = os.environ["VD_SELECTED"].split(), os.environ["VD_NOT_RUN"].split()
results = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    results.append(json.loads(line))
document = (
    {
        "partial": True,
        "partial_selector": os.environ["VD_PARTIAL_LABEL"],
        "selected_modules": selected,
        "not_run_modules": not_run,
        "results": results,
    }
    if partial
    else results
)
print(json.dumps(document, indent=2))
')"
  mkdir -p "$(dirname "${out_json}")"
  printf '%s\n' "${json_array}" > "${out_json}"

  printf '%s\n' "${json_array}" | "${PYTHON_BIN}" -c '
import json
import sys

document = json.load(sys.stdin)
partial = isinstance(document, dict) and document.get("partial") is True
results = document["results"] if partial else document
lines = ["# Verification inventory — single run (UNGUARDED)", ""]
total = 0
failed = 0
skipped = 0
deferred = 0
for entry in results:
    layer = entry.get("layer")
    resource_class = entry.get("resource_class")
    lines.append(f"## Layer {layer} — {resource_class}")
    lines.append("")
    lines.append("| Resource | Check | Status | Defer reason | Detail |")
    lines.append("|---|---|---|---|---|")
    for check in entry.get("checks", []):
        total += 1
        status = check.get("status")
        if status == "fail":
            failed += 1
        elif status == "skip":
            skipped += 1
        elif status == "defer":
            deferred += 1
        detail = str(check.get("detail", "")).replace("|", "\\|").replace("\n", " ")
        defer_reason = str(check.get("defer_reason", "") or "").replace("|", "\\|")
        resource = check.get("resource")
        check_name = check.get("check")
        lines.append(
            f"| {resource} | {check_name} | {status} | {defer_reason} | {detail} |"
        )
    lines.append("")
lines.insert(
    1,
    f"{total} checks total, {failed} failed, {skipped} skipped, {deferred} deferred.",
)
lines.insert(2, "")
lines.insert(
    3,
    "This is the raw rendering of ONE verify_deployment.sh run. It carries none of the "
    "publication guards: no account-id redaction, no anti-laundering assertions, no residue "
    "query. The publishable document is results/verification-inventory.md, written only by "
    "scripts/verify_deployment.sh --layer inventory.",
)
lines.insert(4, "")
if partial:
    lines.insert(
        5,
        "**PARTIAL RUN ("
        + document["partial_selector"]
        + ")** — NOT a full-deployment verification. Selected: "
        + ", ".join(document["selected_modules"])
        + ". Did not run: "
        + (", ".join(document["not_run_modules"]) or "none")
        + ".",
    )
    lines.insert(6, "")
print("\n".join(lines))
' > "${out_md}"

  echo "${SCRIPT_NAME}: wrote ${out_json} and ${out_md}"
  echo "${SCRIPT_NAME}: (per-run, unguarded — publish with --layer inventory)"
}

# ---------------------------------------------------------------------------
# Dry run: print what would run, per layer, with no AWS call and no tenant provisioned.
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" == "true" ]]; then
  # Refuses when the resolved region disagrees with the regions in the outputs snapshot, which a
  # real run would refuse with WrongTargetError. The account is not checked: that needs a live
  # `sts:GetCallerIdentity`.
  DRY_RUN_TARGET_CONFLICT=""
  if [[ -f "${OUTPUTS_PATH}" ]]; then
    DRY_RUN_TARGET_CONFLICT="$("${PYTHON_BIN}" -c '
import sys
from scripts.smoke.harness import load_outputs, outputs_regions

resolved_region, outputs_path = sys.argv[1], sys.argv[2]
try:
    regions = outputs_regions(load_outputs(outputs_path))
except Exception:
    # An unreadable snapshot is reported elsewhere, not as a target conflict.
    sys.exit(0)
if regions and resolved_region not in regions:
    print(", ".join(sorted(regions)))
' "${RESOLVED_REGION}" "${OUTPUTS_PATH}" 2>/dev/null || true)"
  fi

  if [[ -n "${DRY_RUN_TARGET_CONFLICT}" ]]; then
    echo "${SCRIPT_NAME}: dry-run REFUSED — the resolved target is not the deployment the outputs describe." >&2
    echo "  resolved region : ${RESOLVED_REGION}${AWS_REGION:+ (from AWS_REGION=${AWS_REGION})}" >&2
    echo "  outputs describe: ${DRY_RUN_TARGET_CONFLICT}  (${OUTPUTS_PATH})" >&2
    echo "  resolved profile: ${RESOLVED_PROFILE}${AWS_PROFILE:+ (from AWS_PROFILE=${AWS_PROFILE})}" >&2
    echo "" >&2
    echo "No preview is printed, deliberately. A dry-run is documentation: one that names the wrong" >&2
    echo "account is worse than one that refuses, because the commands are read and re-run by hand." >&2
    echo "A real run would stop at the same point with WrongTargetError, so this refuses in the same" >&2
    echo "place rather than previewing something that cannot execute." >&2
    echo "" >&2
    echo "Pass the target explicitly:" >&2
    echo "  ${SCRIPT_NAME} --dry-run --profile <profile> --region ${DRY_RUN_TARGET_CONFLICT%%,*}" >&2
    echo "or unset the ambient AWS_REGION/AWS_PROFILE this shell exports." >&2
    exit 2
  fi

  echo "${SCRIPT_NAME}: dry-run — profile=${RESOLVED_PROFILE} region=${RESOLVED_REGION} outputs=${OUTPUTS_PATH}"
  echo "${SCRIPT_NAME}: (region agrees with ${OUTPUTS_PATH}; the account half needs sts:GetCallerIdentity and is NOT checked in a dry-run)"
  print_dry_layer() {
    local layer_number="$1"
    shift
    [[ $# -gt 0 ]] || return 0
    echo "--- Layer ${layer_number} (dry-run: not executed) ---"
    local module
    local -a preview_args
    local arg
    for module in "$@"; do
      preview_args=()
      while IFS= read -r -d '' arg; do
        preview_args+=("${arg}")
      done < <(build_module_args "${module}" "<smoke-run-id>" "${SMOKE_TOKEN}" "true")
      echo "would run: ${PYTHON_BIN} ${preview_args[*]}"
    done
  }
  if [[ "${PARTIAL_RUN}" == "true" ]]; then
    echo "${SCRIPT_NAME}: PARTIAL (${PARTIAL_LABEL}): would run ${SELECTED_MODULES}"
    echo "${SCRIPT_NAME}: PARTIAL (${PARTIAL_LABEL}): would NOT run $(not_run_modules "${SELECTED_MODULES}")"
  fi
  if [[ -z "${ONLY_LAYER}" || "${ONLY_LAYER}" == "4" ]]; then
    print_dry_layer 4 "${RUN_LAYER_4_MODULES[@]+"${RUN_LAYER_4_MODULES[@]}"}"
  fi
  if [[ -z "${ONLY_LAYER}" || "${ONLY_LAYER}" == "5" ]]; then
    print_dry_layer 5 "${RUN_LAYER_5_MODULES[@]+"${RUN_LAYER_5_MODULES[@]}"}"
  fi
  if [[ -z "${ONLY_LAYER}" || "${ONLY_LAYER}" == "6" ]]; then
    print_dry_layer 6 "${RUN_LAYER_6_MODULES[@]+"${RUN_LAYER_6_MODULES[@]}"}"
  fi
  if [[ "${ONLY_LAYER}" == "inventory" ]]; then
    echo "--- Consolidated inventory (dry-run: not executed) ---"
    echo "would run: ${PYTHON_BIN} scripts/consolidate_inventory.py --profile ${RESOLVED_PROFILE} --region ${RESOLVED_REGION} --table-prefix <from ${OUTPUTS_PATH}> --outputs ${OUTPUTS_PATH} --out ${INVENTORY_MD} --json-out ${INVENTORY_JSON} --older-than 0h"
  fi
  echo "${SCRIPT_NAME}: dry-run complete — no AWS call made, no tenant provisioned"
  exit 0
fi

# ---------------------------------------------------------------------------
# Live run.
# ---------------------------------------------------------------------------

# `--layer inventory` runs no layer: it consolidates the inventory artifacts already on disk and
# runs a live, read-only residue query. It provisions no smoke tenant and exits before
# write_inventory().
if [[ "${ONLY_LAYER}" == "inventory" ]]; then
  echo "${SCRIPT_NAME}: --- Consolidated inventory + live residue query (no layer re-run) ---"
  TABLE_PREFIX="$("${PYTHON_BIN}" -c '
import sys

from scripts.smoke.harness import load_outputs

outputs = load_outputs(sys.argv[1])
table_names = outputs.require_mapping("dynamodb_table_names")
print(table_names["eval_runs"].removesuffix("eval_runs"))
' "${OUTPUTS_PATH}")"
  # --outputs lets the residue query refuse a wrong account/region.
  "${PYTHON_BIN}" scripts/consolidate_inventory.py \
    --profile "${RESOLVED_PROFILE}" \
    --region "${RESOLVED_REGION}" \
    --table-prefix "${TABLE_PREFIX}" \
    --outputs "${OUTPUTS_PATH}" \
    --inventory-dir "${REPO_ROOT}/results/verification-inventory" \
    --out "${INVENTORY_MD}" \
    --json-out "${INVENTORY_JSON}" \
    --older-than 0h || LAYER_EXIT_CODE=1
  if [[ "${LAYER_EXIT_CODE}" -eq 0 ]]; then
    echo "${SCRIPT_NAME}: consolidated inventory published; no residue found"
  else
    echo "${SCRIPT_NAME}: CONSOLIDATED INVENTORY REPORTS A FAILURE OR RESIDUE" >&2
  fi
  exit "${LAYER_EXIT_CODE}"
fi

mkdir -p "${REPO_ROOT}/results"
# `XXXXXX` must be the final path component for BSD and GNU mktemp.
STDERR_LOG="$(mktemp "${REPO_ROOT}/results/verify_deployment_stderr.XXXXXX")"
: > "${STDERR_LOG}"
provision_tenant
# A non-zero return means a supplied token was refused. The EXIT trap still tears down.
if ! mint_smoke_token; then
  echo "${SCRIPT_NAME}: aborting before any layer ran; nothing was written under ${SMOKE_TENANT}." >&2
  exit 2
fi
# Checked after the mint, which may set SMOKE_TOKEN.
if [[ -z "${SMOKE_TOKEN}" ]]; then
  echo "${SCRIPT_NAME}: WARNING — no bearer token (--token / CRM_SMOKE_TOKEN, or a mint); every" >&2
  echo "authenticated check in a token-taking script will report fail, not skip." >&2
fi

for layer in 4 5 6; do
  if [[ -n "${ONLY_LAYER}" && "${layer}" != "${ONLY_LAYER}" ]]; then
    continue
  fi
  layer_modules=()
  while IFS= read -r -d '' entry; do
    layer_modules+=("${entry}")
  done < <(run_layer_entries "${layer}")
  [[ ${#layer_modules[@]} -gt 0 ]] || continue
  if ! run_layer "${layer}" "${layer_modules[@]}"; then
    LAYER_EXIT_CODE=1
    if [[ -z "${ONLY_LAYER}" && "${layer}" != "6" ]]; then
      echo "${SCRIPT_NAME}: Layer ${layer} failed — stopping before Layer $((layer + 1))" >&2
    fi
    break
  fi
done

write_inventory

if [[ "${SKIPPED_CHECK_COUNT}" -gt 0 ]]; then
  # Skips are reported but are not a failure.
  echo "${SCRIPT_NAME}: ${SKIPPED_CHECK_COUNT} check(s) skipped (not a failure by itself):" >&2
  printf '%s\n' "${SKIPPED_RESOURCE_CLASSES[@]+"${SKIPPED_RESOURCE_CLASSES[@]}"}" >&2
fi

if [[ "${DEFERRED_CHECK_COUNT}" -gt 0 ]]; then
  # Deferred controls are reported but do not change the exit code.
  echo "${SCRIPT_NAME}: ${DEFERRED_CHECK_COUNT} check(s) DEFERRED - control not verified and not" >&2
  echo "${SCRIPT_NAME}: enforced; see each check's defer_reason in the inventory artifacts:" >&2
  printf '%s\n' "${DEFERRED_RESOURCE_CLASSES[@]+"${DEFERRED_RESOURCE_CLASSES[@]}"}" >&2
fi

if [[ "${PARTIAL_RUN}" == "true" ]]; then
  echo "${SCRIPT_NAME}: PARTIAL RUN (${PARTIAL_LABEL}) — NOT a full-deployment verification." >&2
  echo "${SCRIPT_NAME}: ran: ${EXECUTED_MODULES# }" >&2
  echo "${SCRIPT_NAME}: did NOT run: $(not_run_modules "${EXECUTED_MODULES# }")" >&2
  if [[ "${LAYER_EXIT_CODE}" -eq 0 ]]; then
    if [[ "${DEFERRED_CHECK_COUNT}" -gt 0 ]]; then
      echo "${SCRIPT_NAME}: PARTIAL RUN: SELECTED MODULES PASSED, WITH ${DEFERRED_CHECK_COUNT} DEFERRED CONTROL(S) NOT VERIFIED"
    else
      echo "${SCRIPT_NAME}: PARTIAL RUN: SELECTED MODULES PASSED"
    fi
  else
    echo "${SCRIPT_NAME}: PARTIAL RUN: ONE OR MORE SELECTED MODULES FAILED" >&2
  fi
elif [[ "${LAYER_EXIT_CODE}" -eq 0 ]]; then
  if [[ "${DEFERRED_CHECK_COUNT}" -gt 0 ]]; then
    echo "${SCRIPT_NAME}: ALL RUN LAYERS PASSED, WITH ${DEFERRED_CHECK_COUNT} DEFERRED CONTROL(S) NOT VERIFIED"
  else
    echo "${SCRIPT_NAME}: ALL RUN LAYERS PASSED"
  fi
else
  echo "${SCRIPT_NAME}: ONE OR MORE LAYERS FAILED" >&2
fi

exit "${LAYER_EXIT_CODE}"
