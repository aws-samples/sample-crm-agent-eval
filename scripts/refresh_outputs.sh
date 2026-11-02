#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Re-records results/tf-outputs.json from the deployed Terraform state, with no apply: the same
# `terraform output -json`, non-empty-object assert, provenance stamp and atomic mv that
# scripts/deploy.sh runs after an apply. The stamp's `how` names this script. Reads remote state, so
# it needs credentials; it writes only the outputs file. On any failure the previous snapshot is
# left in place, since no apply changed the stack it describes.
#
# Usage: scripts/refresh_outputs.sh [--profile NAME] [--region REGION] [--out PATH]
#
# Profile/region resolve as flag, then AWS_PROFILE/AWS_REGION, then default/us-east-1, as in
# scripts/deploy.sh. Requires an initialised infra/ (scripts/deploy.sh or `terraform init`).
#
# Exit codes: 0 recorded; 1 terraform output, the content assert, the stamp or the mv failed;
# 2 usage error or static credentials in the environment.
set -euo pipefail
export LC_ALL=C

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd -P)"
INFRA_DIR="${REPO_ROOT}/infra"

RESOLVED_PROFILE="${AWS_PROFILE:-default}"
RESOLVED_REGION="${AWS_REGION:-us-east-1}"
OUTPUTS_SNAPSHOT="${REPO_ROOT}/results/tf-outputs.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) [[ -n "${2:-}" ]] || { echo "${SCRIPT_NAME}: --profile requires a value" >&2; exit 2; }
      RESOLVED_PROFILE="$2"; shift 2 ;;
    --region) [[ -n "${2:-}" ]] || { echo "${SCRIPT_NAME}: --region requires a value" >&2; exit 2; }
      RESOLVED_REGION="$2"; shift 2 ;;
    --out) [[ -n "${2:-}" ]] || { echo "${SCRIPT_NAME}: --out requires a value" >&2; exit 2; }
      OUTPUTS_SNAPSHOT="$2"; shift 2 ;;
    -h|--help) sed -n '3,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "${SCRIPT_NAME}: unknown argument: $1 (use -h for help)" >&2; exit 2 ;;
  esac
done

# Static keys take precedence over AWS_PROFILE in every AWS SDK, as in scripts/deploy.sh.
if [[ -n "${AWS_ACCESS_KEY_ID:-}" || -n "${AWS_SESSION_TOKEN:-}" ]]; then
  echo "${SCRIPT_NAME}: AWS_ACCESS_KEY_ID/AWS_SESSION_TOKEN are set; terraform would read state as" >&2
  echo "those keys, not profile '${RESOLVED_PROFILE}'. Unset them and re-run. Nothing was recorded." >&2
  exit 2
fi
export AWS_PROFILE="${RESOLVED_PROFILE}"
export AWS_REGION="${RESOLVED_REGION}"
echo "${SCRIPT_NAME}: profile=${RESOLVED_PROFILE} region=${RESOLVED_REGION} out=${OUTPUTS_SNAPSHOT}"

PARTIAL="${OUTPUTS_SNAPSHOT}.partial"
refresh_failed() {
  rm -f "${PARTIAL}"
  echo "${SCRIPT_NAME}: $1; the previous ${OUTPUTS_SNAPSHOT} (if any) was left unchanged" >&2
  exit 1
}

mkdir -p "$(dirname "${OUTPUTS_SNAPSHOT}")"
(cd "${INFRA_DIR}" && terraform output -no-color -json) > "${PARTIAL}" \
  || refresh_failed "terraform output -json exited non-zero"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if isinstance(d, dict) and d else 1)' \
  "${PARTIAL}" || refresh_failed "the recorded document is not a non-empty JSON object"
python3 "${SCRIPT_DIR}/outputs_provenance.py" --stamp "${PARTIAL}" --recorded-by refresh \
  || refresh_failed "recording the snapshot's provenance failed"
mv "${PARTIAL}" "${OUTPUTS_SNAPSHOT}" || refresh_failed "the atomic install (mv) failed"
echo "${SCRIPT_NAME}: recorded $(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(len([k for k in d if not k.startswith("_")]))' "${OUTPUTS_SNAPSHOT}") outputs to ${OUTPUTS_SNAPSHOT}"
