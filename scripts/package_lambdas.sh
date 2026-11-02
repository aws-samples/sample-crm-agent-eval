#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Builds every Lambda deployment zip infra/main.tf's filebase64sha256 call sites reference under
# var.lambda_build_dir, so `terraform plan` can evaluate those functions. Non-interactive; safe to
# re-run -- each run replaces the zips it owns from scratch.
#
# What gets built:
#   - One zip per action-Lambda tool key in crm.toolspec.definitions.ALL_TOOLS, each containing crm/.
#   - identity_interceptor.zip -- src/gateway/identity_interceptor.py plus crm/.
#   - pre_token_generation.zip -- src/crm/auth/pre_token_generation.py plus crm/.
#   - eval_trigger.zip -- src/crm/handlers/eval_trigger.py plus crm/, eval/, agents/, providers/,
#     the repo-root fixtures/ package, and config/.
#   - chat_target.zip -- src/gateway/chat_target.py plus crm/, gateway/, providers/ and config/,
#     laid out like eval_trigger.zip (first-party packages under src/). No model SDK.
#   - agent-source.zip -- the full src/ tree plus config/, pyproject.toml, requirements.txt and the
#     root Dockerfile: CodeBuild's Docker build context (infra/modules/agentcore/ecr_codebuild.tf),
#     not a Lambda zip.
#
# Third-party dependencies: boto3/botocore are provided by the Lambda runtime and never vendored.
# Each zip declares a seed (jsonschema for the tool zips; PyJWT and cryptography for the interceptor;
# none for pre_token_generation; jsonschema, PyYAML, starlette and python-multipart for
# eval_trigger; jsonschema and PyYAML for chat_target), and the rest of the closure is resolved: pip installs for the Lambda platform
# (python3.13, x86_64), then scripts/check_lambda_zip_imports.py reports which distributions the
# Lambda marker environment still requires, and those are installed until a fixed point. Every
# vendored version must be exact-pinned in requirements.txt; no version literal appears here.
#
# Every zip is checked by scripts/check_lambda_zip_imports.py before this script exits.
#
# Usage: scripts/package_lambdas.sh [--build-dir DIR] [--print-pins]
#   --build-dir DIR   output directory (default infra/build/lambda)
#   --print-pins      print the seed pins resolved from requirements.txt and build nothing
# Env override: LAMBDA_BUILD_DIR (same default as infra/main.tf's lambda_build_dir variable),
# REQUIREMENTS_FILE (the manifest every vendored version is resolved from).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SRC_DIR="${REPO_ROOT}/src"

# Terraform resolves lambda_build_dir's relative default against infra/.
BUILD_DIR="${LAMBDA_BUILD_DIR:-${REPO_ROOT}/infra/build/lambda}"

# The manifest every vendored version is read from.
REQUIREMENTS_FILE="${REQUIREMENTS_FILE:-${REPO_ROOT}/requirements.txt}"

PRINT_PINS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-dir) BUILD_DIR="$2"; shift 2 ;;
    --print-pins) PRINT_PINS=1; shift ;;
    -h|--help)
      echo "Usage: $(basename "$0") [--build-dir DIR] [--print-pins]"
      exit 0
      ;;
    *) echo "Unknown argument: $1 (use -h for help)" >&2; exit 1 ;;
  esac
done

log() { printf '%s [package_lambdas] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"; }

# PEP 503 name normalisation: `-`, `_` and `.` are equivalent and case is insignificant.
_normalise_distribution_name() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr '_.' '--'
}

# Prints one distribution's exact `name==version` pin from REQUIREMENTS_FILE; fails if it is absent
# or not exact-pinned.
_requirement_pin() {
  local wanted="$1" target line name
  target="$(_normalise_distribution_name "${wanted}")"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%%#*}"
    line="${line//[[:space:]]/}"
    [[ -z "${line}" || "${line}" != *"=="* ]] && continue
    name="$(_normalise_distribution_name "${line%%==*}")"
    if [[ "${name}" == "${target}" ]]; then
      printf '%s==%s\n' "${wanted}" "${line#*==}"
      return 0
    fi
  done <"${REQUIREMENTS_FILE}"
  echo "package_lambdas: '${wanted}' is not exact-pinned in ${REQUIREMENTS_FILE}, so the vendored Lambda closure cannot be derived from it" >&2
  return 1
}

# Resolve a name array into a pin array: _resolve_pins name... -> one `name==version` per line.
_resolve_pins() {
  local dep
  for dep in "$@"; do
    _requirement_pin "${dep}"
  done
}

# Seed for the action-tool zips (crm.handlers._common -> crm.toolspec.validation -> jsonschema).
TOOL_ZIP_SEED_NAMES=(
  'jsonschema'
)
# Seed for identity_interceptor.zip. cryptography is listed because PyJWT declares it only under
# its `crypto` extra.
INTERCEPTOR_SEED_NAMES=(
  'PyJWT'
  'cryptography'
)
# Seed for eval_trigger.zip. python-multipart is listed because starlette declares it only under an
# extra. No model SDK or HTTP client is seeded; scripts/check_lambda_adapter_isolation.py gates that.
EVAL_TRIGGER_SEED_NAMES=(
  'jsonschema'
  'PyYAML'
  'starlette'
  'python-multipart'
)
# Seed for chat_target.zip: crm/ (jsonschema) and providers.capabilities (PyYAML).
CHAT_TARGET_SEED_NAMES=(
  'jsonschema'
  'PyYAML'
)
# Read line-by-line: `mapfile` is bash 4+ and macOS ships bash 3.2.
TOOL_ZIP_DEPENDENCIES=()
while IFS= read -r pin; do
  TOOL_ZIP_DEPENDENCIES+=("${pin}")
done < <(_resolve_pins "${TOOL_ZIP_SEED_NAMES[@]}")
INTERCEPTOR_DEPENDENCIES=()
while IFS= read -r pin; do
  INTERCEPTOR_DEPENDENCIES+=("${pin}")
done < <(_resolve_pins "${INTERCEPTOR_SEED_NAMES[@]}")
EVAL_TRIGGER_DEPENDENCIES=()
while IFS= read -r pin; do
  EVAL_TRIGGER_DEPENDENCIES+=("${pin}")
done < <(_resolve_pins "${EVAL_TRIGGER_SEED_NAMES[@]}")
CHAT_TARGET_DEPENDENCIES=()
while IFS= read -r pin; do
  CHAT_TARGET_DEPENDENCIES+=("${pin}")
done < <(_resolve_pins "${CHAT_TARGET_SEED_NAMES[@]}")
if [[ "${#TOOL_ZIP_DEPENDENCIES[@]}" -ne "${#TOOL_ZIP_SEED_NAMES[@]}" ]] ||
  [[ "${#INTERCEPTOR_DEPENDENCIES[@]}" -ne "${#INTERCEPTOR_SEED_NAMES[@]}" ]] ||
  [[ "${#EVAL_TRIGGER_DEPENDENCIES[@]}" -ne "${#EVAL_TRIGGER_SEED_NAMES[@]}" ]] ||
  [[ "${#CHAT_TARGET_DEPENDENCIES[@]}" -ne "${#CHAT_TARGET_SEED_NAMES[@]}" ]]; then
  echo "package_lambdas: not every vendored distribution resolved to an exact pin in ${REQUIREMENTS_FILE}" >&2
  exit 1
fi

if [[ "${PRINT_PINS}" -eq 1 ]]; then
  # Prints the seed pins only; the resolved remainder needs a real install.
  printf 'tool_zip %s\n' "${TOOL_ZIP_DEPENDENCIES[@]}"
  printf 'interceptor %s\n' "${INTERCEPTOR_DEPENDENCIES[@]}"
  printf 'eval_trigger %s\n' "${EVAL_TRIGGER_DEPENDENCIES[@]}"
  printf 'chat_target %s\n' "${CHAT_TARGET_DEPENDENCIES[@]}"
  exit 0
fi

# Prefers the project's virtualenv, falling back to `python3`.
PYTHON="${REPO_ROOT}/.venv/bin/python3"
if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="python3"
fi

# Installs `_PIP_PINS` into `$1` for the Lambda platform (x86_64, python3.13); `--constraint` binds
# every version pip resolves to requirements.txt.
_PIP_PINS=()
_pip_vendor_install() {
  local dir="$1"
  "${PYTHON}" -m pip install --no-input --target "${dir}" --quiet --upgrade \
    --platform manylinux2014_x86_64 --python-version 3.13 --only-binary=:all: \
    --constraint "${REQUIREMENTS_FILE}" "${_PIP_PINS[@]}"
  rm -rf "${dir:?}/bin"
  find "${dir}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
}

# Fails unless every vendored distribution is exact-pinned in requirements.txt at the installed
# version.
_verify_vendor_pins() {
  local dir="$1" dist_info base name version expected
  for dist_info in "${dir}"/*.dist-info; do
    [[ -d "${dist_info}" ]] || continue
    base="$(basename "${dist_info}")"
    base="${base%.dist-info}"
    name="${base%-*}"
    version="${base##*-}"
    expected="$(_requirement_pin "${name}")"
    if [[ "${expected#*==}" != "${version}" ]]; then
      echo "package_lambdas: vendored ${name} ${version} does not match ${REQUIREMENTS_FILE}'s pin ${expected#*==}" >&2
      return 1
    fi
  done
}

# Installs `_PIP_PINS` into `$1`, then repeatedly installs the distributions the Lambda marker
# environment (python_version 3.13 / linux / x86_64) still requires, until none are missing. The round
# cap guards against a metadata cycle.
_install_target_closure() {
  local dir="$1" round=0 missing_text pins_text name pin
  local missing_names=()
  _pip_vendor_install "${dir}"
  while :; do
    round=$((round + 1))
    if [[ "${round}" -gt 10 ]]; then
      echo "package_lambdas: marker-aware closure did not converge for ${dir} in 10 rounds" >&2
      return 1
    fi
    missing_text="$("${PYTHON}" "${SCRIPT_DIR}/check_lambda_zip_imports.py" \
      --print-missing-required-distributions "${dir}")"
    missing_names=()
    while IFS= read -r name; do
      if [[ -n "${name}" ]]; then missing_names+=("${name}"); fi
    done <<<"${missing_text}"
    if [[ "${#missing_names[@]}" -eq 0 ]]; then
      break
    fi
    pins_text="$(_resolve_pins "${missing_names[@]}")"
    _PIP_PINS=()
    while IFS= read -r pin; do
      if [[ -n "${pin}" ]]; then _PIP_PINS+=("${pin}"); fi
    done <<<"${pins_text}"
    log "Lambda-target closure round ${round}: adding ${_PIP_PINS[*]}"
    _pip_vendor_install "${dir}"
  done
  _verify_vendor_pins "${dir}"
  log "Vendored closure under ${dir##*/} verified against ${REQUIREMENTS_FILE} after $((round - 1)) resolution round(s)"
}

#: `scripts/package_relay.sh`'s artefact in the shared build directory, preserved by the clean step.
#: Must equal that script's `ZIP_NAME` (asserted by tests/unit/test_package_lambdas_preserves_relay_zip.py).
RELAY_ZIP_NAME="relay.zip"

# Wipes everything in the build directory except relay.zip, whose staleness check_relay_zip judges.
if [[ -d "${BUILD_DIR}" ]]; then
  find "${BUILD_DIR}" -mindepth 1 -maxdepth 1 ! -name "${RELAY_ZIP_NAME}" -exec rm -rf {} +
fi
mkdir -p "${BUILD_DIR}"
log "Build directory: ${BUILD_DIR} (cleaned, preserving ${RELAY_ZIP_NAME} if present)"

# Tool keys come from crm.toolspec.definitions.ALL_TOOLS.
TOOL_NAMES=()
while IFS= read -r tool_name; do
  TOOL_NAMES+=("${tool_name}")
done < <(
  PYTHONPATH="${SRC_DIR}" "${PYTHON}" -c "
from crm.toolspec.definitions import ALL_TOOLS
for tool in ALL_TOOLS:
    print(tool.name)
"
)
if [[ "${#TOOL_NAMES[@]}" -eq 0 ]]; then
  echo "derived no tool names from crm.toolspec.definitions.ALL_TOOLS" >&2
  exit 1
fi
if [[ "$(printf '%s\n' "${TOOL_NAMES[@]}" | sort -u | wc -l | tr -d ' ')" -ne "${#TOOL_NAMES[@]}" ]]; then
  echo "crm.toolspec.definitions.ALL_TOOLS declares a tool name more than once" >&2
  exit 1
fi
log "Derived ${#TOOL_NAMES[@]} tool names from crm.toolspec.definitions.ALL_TOOLS"

# Stages the shared crm/ package tree, without __pycache__.
STAGE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/package_lambdas.XXXXXX")"
trap 'rm -rf "${STAGE_ROOT}"' EXIT

_stage_crm() {
  local dest="$1"
  mkdir -p "${dest}"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' "${SRC_DIR}/crm" "${dest}/"
}

# The tool-zip closure is installed once into a shared staging directory and rsynced into each zip.
VENDOR_DIR="${STAGE_ROOT}/_vendor_tools"
mkdir -p "${VENDOR_DIR}"
_PIP_PINS=("${TOOL_ZIP_DEPENDENCIES[@]}")
_install_target_closure "${VENDOR_DIR}"
log "Vendored tool-zip dependency closure into staging (seed: ${TOOL_ZIP_DEPENDENCIES[*]})"

# One zip per action-Lambda tool: crm/ plus the vendored closure.
for tool_name in "${TOOL_NAMES[@]}"; do
  work_dir="${STAGE_ROOT}/${tool_name}"
  _stage_crm "${work_dir}"
  rsync -a "${VENDOR_DIR}/" "${work_dir}/"
  (cd "${work_dir}" && zip -qr "${BUILD_DIR}/${tool_name}.zip" .)
done
log "Packaged ${#TOOL_NAMES[@]} tool zips"

# identity_interceptor.zip -- handler gateway.identity_interceptor.lambda_handler, plus crm/ and the
# PyJWT/cryptography closure built for the Lambda platform.
interceptor_dir="${STAGE_ROOT}/identity_interceptor"
_stage_crm "${interceptor_dir}"
mkdir -p "${interceptor_dir}/gateway"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "${SRC_DIR}/gateway/" "${interceptor_dir}/gateway/"
INTERCEPTOR_VENDOR_DIR="${STAGE_ROOT}/_vendor_interceptor"
mkdir -p "${INTERCEPTOR_VENDOR_DIR}"
_PIP_PINS=("${INTERCEPTOR_DEPENDENCIES[@]}")
_install_target_closure "${INTERCEPTOR_VENDOR_DIR}"
rsync -a "${INTERCEPTOR_VENDOR_DIR}/" "${interceptor_dir}/"
(cd "${interceptor_dir}" && zip -qr "${BUILD_DIR}/identity_interceptor.zip" .)
log "Packaged identity_interceptor.zip"

# pre_token_generation.zip -- handler crm.auth.pre_token_generation.lambda_handler, plus crm/.
pretoken_dir="${STAGE_ROOT}/pre_token_generation"
_stage_crm "${pretoken_dir}"
(cd "${pretoken_dir}" && zip -qr "${BUILD_DIR}/pre_token_generation.zip" crm)
log "Packaged pre_token_generation.zip"

# eval_trigger.zip -- handler crm.handlers.eval_trigger.handler. First-party packages go under src/
# (PYTHONPATH=/var/task/src, set in infra/main.tf) so `Path(__file__).resolve().parents[2]` finds
# config/ and fixtures/ at the archive root. src/relay is excluded.
eval_trigger_dir="${STAGE_ROOT}/eval_trigger"
mkdir -p "${eval_trigger_dir}/src"
for pkg in crm eval agents providers; do
  rsync -a --exclude '__pycache__' --exclude '*.pyc' "${SRC_DIR}/${pkg}" "${eval_trigger_dir}/src/"
done
rsync -a --exclude '__pycache__' --exclude '*.pyc' "${REPO_ROOT}/fixtures" "${eval_trigger_dir}/"
rsync -a "${REPO_ROOT}/config/" "${eval_trigger_dir}/config/"
EVAL_TRIGGER_VENDOR_DIR="${STAGE_ROOT}/_vendor_eval_trigger"
mkdir -p "${EVAL_TRIGGER_VENDOR_DIR}"
_PIP_PINS=("${EVAL_TRIGGER_DEPENDENCIES[@]}")
_install_target_closure "${EVAL_TRIGGER_VENDOR_DIR}"
rsync -a "${EVAL_TRIGGER_VENDOR_DIR}/" "${eval_trigger_dir}/"
(cd "${eval_trigger_dir}" && zip -qr "${BUILD_DIR}/eval_trigger.zip" .)
log "Packaged eval_trigger.zip"

# chat_target.zip -- handler gateway.chat_target.lambda_handler, on PYTHONPATH=/var/task/src like
# eval_trigger.zip, so providers.capabilities finds config/ at the archive root.
chat_target_dir="${STAGE_ROOT}/chat_target"
mkdir -p "${chat_target_dir}/src"
for pkg in crm gateway providers; do
  rsync -a --exclude '__pycache__' --exclude '*.pyc' "${SRC_DIR}/${pkg}" "${chat_target_dir}/src/"
done
rsync -a "${REPO_ROOT}/config/" "${chat_target_dir}/config/"
CHAT_TARGET_VENDOR_DIR="${STAGE_ROOT}/_vendor_chat_target"
mkdir -p "${CHAT_TARGET_VENDOR_DIR}"
_PIP_PINS=("${CHAT_TARGET_DEPENDENCIES[@]}")
_install_target_closure "${CHAT_TARGET_VENDOR_DIR}"
rsync -a "${CHAT_TARGET_VENDOR_DIR}/" "${chat_target_dir}/"
(cd "${chat_target_dir}" && zip -qr "${BUILD_DIR}/chat_target.zip" .)
log "Packaged chat_target.zip"

# agent-source.zip -- src/, config/, pyproject.toml, requirements.txt, and the Dockerfile at the
# archive root, where CodeBuild's `docker build` expects it.
agent_source_dir="${STAGE_ROOT}/agent-source"
mkdir -p "${agent_source_dir}"
rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude '*.egg-info' "${SRC_DIR}/" "${agent_source_dir}/src/"
rsync -a "${REPO_ROOT}/config/" "${agent_source_dir}/config/"
for f in pyproject.toml requirements.txt Dockerfile; do
  if [[ -f "${REPO_ROOT}/${f}" ]]; then
    cp "${REPO_ROOT}/${f}" "${agent_source_dir}/"
  fi
done
(cd "${agent_source_dir}" && zip -qr "${BUILD_DIR}/agent-source.zip" .)
log "Packaged agent-source.zip"

log "Done. $(find "${BUILD_DIR}" -maxdepth 1 -name '*.zip' | wc -l | tr -d ' ') zip(s) in ${BUILD_DIR}"

# Verification-only, never packaged: a host-loadable CPython 3.13 build of the tool and interceptor
# zips' vendored distributions, at the same pins, for check_lambda_zip_imports's isolated_import phase. Best effort;
# when it is absent or incomplete the gate falls back to the build host interpreter and says so.
TARGET_SHADOW_DIR="${LAMBDA_TARGET_SHADOW_DIR:-${REPO_ROOT}/infra/build/target-python-shadow}"
_build_target_shadow() {
  local target_python
  _SHADOW_PINS=()
  target_python="$("${PYTHON}" "${SCRIPT_DIR}/check_lambda_zip_imports.py" --print-target-python || true)"
  if [[ -z "${target_python}" ]]; then
    log "No CPython 3.13 discoverable; skipping the target-version substitute directory (isolated_import will run on this host and say so)"
    return 0
  fi
  local dist_info stem
  for dist_info in "${VENDOR_DIR}"/*.dist-info "${INTERCEPTOR_VENDOR_DIR}"/*.dist-info; do
    [[ -d "${dist_info}" ]] || continue
    stem="$(basename "${dist_info}")"
    stem="${stem%.dist-info}"
    _SHADOW_PINS+=("${stem%-*}==${stem##*-}")
  done
  [[ "${#_SHADOW_PINS[@]}" -gt 0 ]] || return 0
  rm -rf "${TARGET_SHADOW_DIR}"
  mkdir -p "${TARGET_SHADOW_DIR}"
  if "${PYTHON}" -m pip install --no-input --target "${TARGET_SHADOW_DIR}" --quiet --upgrade \
      --python-version 3.13 --only-binary=:all: \
      --constraint "${REQUIREMENTS_FILE}" "${_SHADOW_PINS[@]}"; then
    log "Target-version substitute directory built for ${target_python} at ${TARGET_SHADOW_DIR}"
  else
    log "Could not build the target-version substitute directory (no host wheel for a pin at python-version 3.13); isolated_import will fall back to this host and say so"
  fi
}
_build_target_shadow

# `--require-zips` makes a missing zip a failure rather than a skip.
log "Verifying every zip's handler import closure (scripts/check_lambda_zip_imports.py)"
"${PYTHON}" "${SCRIPT_DIR}/check_lambda_zip_imports.py" --build-dir "${BUILD_DIR}" --require-zips
