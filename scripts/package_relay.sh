#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Builds the relay Lambda deployment zip that `infra/main.tf`'s `module "agent_relay"` block
# references as `${var.lambda_build_dir}/relay.zip`, so `terraform plan` can evaluate its
# `filebase64sha256` call.
#
# The relay is packaged here rather than by `scripts/package_lambdas.sh`, which builds the Python
# zips. Both write into the same build directory and neither touches the other's artefacts.
#
# The zip contains:
#   - The compiled output of `src/relay/tsconfig.build.json`, flattened so `index.js` sits at the
#     archive root. The Lambda handler is `index.handler`
#     (`infra/modules/agent_relay/variables.tf`), which resolves relative to the archive root.
#   - `node_modules/` installed with `npm ci --omit=dev` from the committed `package-lock.json`,
#     including `aws-jwt-verify` and `@aws-sdk/client-bedrock-agentcore`.
#   - A minimal `package.json` with `"type": "module"`; the compiled output is ESM.
#   - `.relay-build.json`, the SHA-256 of every compile input, read by `scripts/check_relay_zip.py`.
#
# Verification: `--verify-import` (on by default) extracts the built zip to a temporary directory
# and imports the handler there with the repository and `src/relay/` unreachable, with the
# `awslambda` global the managed runtime injects shimmed in. `--verify-only ZIP` runs that check
# against an existing zip and exits without building.
#
# Usage: scripts/package_relay.sh [--build-dir DIR] [--skip-install] [--no-verify-import]
#        scripts/package_relay.sh --verify-only ZIP
#
#   --build-dir DIR       output directory (default infra/build/lambda)
#   --skip-install        skip the build-time npm ci in src/relay (its node_modules must exist)
#   --no-verify-import    skip the import check after building
#   --verify-only ZIP     run only the import check against ZIP
#
# Env override: LAMBDA_BUILD_DIR (same default as infra/main.tf's lambda_build_dir variable).
# Resolution order: CLI flag -> environment -> default.
#
# Requires node >= 24, npm and zip; the import check also needs unzip.
#
# Non-interactive; safe to re-run. Each run replaces the staging tree and the zip from scratch.
#
# Output: <build-dir>/relay.zip, with normalised mtimes and a sorted entry list, so an unchanged
# source tree produces an unchanged zip.
#
# Exit status: 0 on success, non-zero on any failure.
#

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT
readonly RELAY_DIR="${REPO_ROOT}/src/relay"
#: Relative to the repository root; matches `infra/main.tf`'s `lambda_build_dir`, which Terraform
#: resolves against `infra/`.
readonly DEFAULT_BUILD_DIR="infra/build/lambda"
readonly ZIP_NAME="relay.zip"

#: Handler entry point; matches `infra/modules/agent_relay/variables.tf`'s `handler` default.
readonly HANDLER_MODULE="index"
readonly HANDLER_EXPORT="handler"

#: Build manifest at the archive root: the SHA-256 of every compile input. Written here and compared
#: only by `scripts/check_relay_zip.py`, which scripts/deploy.sh runs before an apply.
readonly BUILD_MANIFEST=".relay-build.json"

#: Compile inputs the manifest covers, relative to `src/relay`.
readonly MANIFEST_INPUT_PATHS=(
  "src"
  "package.json"
  "package-lock.json"
  "tsconfig.json"
  "tsconfig.build.json"
)

build_dir_flag=""
skip_install=0
verify_import=1
verify_only_zip=""

die() {
  printf 'package_relay: %s\n' "$1" >&2
  exit 1
}

log() {
  printf 'package_relay: %s\n' "$1"
}

# Emits the build manifest for the current `src/relay` tree on stdout: one JSON object mapping each
# compile input's path to its SHA-256, keys sorted.
compute_build_manifest() {
  local -a files=()
  local rel digest
  command -v shasum >/dev/null 2>&1 || die "shasum is not on PATH (needed for the build manifest)"
  while IFS= read -r -d '' rel; do
    files+=("${rel#./}")
  done < <(cd "${RELAY_DIR}" && find "${MANIFEST_INPUT_PATHS[@]}" -type f -print0 2>/dev/null)
  if [[ "${#files[@]}" -eq 0 ]]; then
    die "no compile inputs found under ${RELAY_DIR} -- refusing to write an empty build manifest"
  fi
  (
    cd "${RELAY_DIR}"
    printf '%s\n' "${files[@]}" | LC_ALL=C sort | while IFS= read -r rel; do
      digest="$(shasum -a 256 "${rel}" | cut -d' ' -f1)" || die "shasum failed on ${rel}"
      [[ "${digest}" =~ ^[0-9a-f]{64}$ ]] ||
        die "shasum printed no SHA-256 for ${rel} (got '${digest}'); refusing to write the manifest"
      printf '%s  %s\n' "${digest}" "${rel}"
    done
  ) | MANIFEST_INPUT_ROOTS="$(printf '%s\n' "${MANIFEST_INPUT_PATHS[@]}")" node -e '
    let input = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (c) => (input += c));
    process.stdin.on("end", () => {
      const sources = {};
      for (const line of input.split("\n")) {
        if (!line.trim()) continue;
        const [digest, ...rest] = line.split("  ");
        sources[rest.join("  ")] = digest;
      }
      const ordered = {};
      for (const key of Object.keys(sources).sort()) ordered[key] = sources[key];
      const roots = (process.env.MANIFEST_INPUT_ROOTS ?? "").split("\n").filter(Boolean).sort();
      process.stdout.write(
        JSON.stringify({ schema: 1, input_roots: roots, sources: ordered }, null, 2) + "\n"
      );
    });
  '
}

# Extracts $1 to a scratch directory and imports the handler there, with the repository and
# src/relay/ unreachable. Returns non-zero if the handler cannot be imported or is not a function.
verify_zip_import() {
  local zip_path="$1"
  local probe_dir status=0

  [[ -f "${zip_path}" ]] || die "no zip at ${zip_path}"
  command -v node >/dev/null 2>&1 || die "node is not on PATH (needed by the import check)"
  command -v unzip >/dev/null 2>&1 ||
    die "unzip is not on PATH (needed by the import check; pass --no-verify-import to skip)"

  probe_dir="$(mktemp -d "${TMPDIR:-/tmp}/relay-verify.XXXXXX")"
  unzip -q "${zip_path}" -d "${probe_dir}" || {
    rm -rf "${probe_dir}"
    die "${zip_path} is not a readable zip archive"
  }

  # Asserted structurally: Node's module-syntax detection would load the ESM handler without
  # `"type": "module"`, so the import probe alone cannot catch its absence.
  if [[ ! -f "${probe_dir}/package.json" ]]; then
    rm -rf "${probe_dir}"
    die "${zip_path} has no package.json at the archive root; the ESM handler would depend on Node's module-syntax detection to load"
  fi
  if ! (cd "${probe_dir}" && node -e 'process.exit(require("./package.json").type === "module" ? 0 : 1)'); then
    rm -rf "${probe_dir}"
    die "${zip_path}'s root package.json does not declare \"type\": \"module\""
  fi
  if [[ ! -f "${probe_dir}/${HANDLER_MODULE}.js" ]]; then
    rm -rf "${probe_dir}"
    die "${zip_path} has no ${HANDLER_MODULE}.js at the archive root; Lambda resolves '${HANDLER_MODULE}.${HANDLER_EXPORT}' relative to the root"
  fi

  # Runs with cwd inside the extracted tree. The awslambda shim mirrors src/relay/tests/setup.ts.
  # shellcheck disable=SC2016  # `${...}` below is JS template syntax.
  (
    cd "${probe_dir}" &&
      RELAY_HANDLER_MODULE="${HANDLER_MODULE}" RELAY_HANDLER_EXPORT="${HANDLER_EXPORT}" \
        node --input-type=module -e '
      Object.defineProperty(globalThis, "awslambda", {
        value: {
          streamifyResponse: (h) => h,
          HttpResponseStream: { from: (s) => s },
        },
        writable: true,
        configurable: true,
      });
      const moduleName = process.env.RELAY_HANDLER_MODULE;
      const exportName = process.env.RELAY_HANDLER_EXPORT;
      const mod = await import(`./${moduleName}.js`);
      const fn = mod[exportName];
      if (typeof fn !== "function") {
        throw new Error(
          `${moduleName}.${exportName} is ${typeof fn}, expected function; ` +
            `exports: ${Object.keys(mod).join(", ")}`
        );
      }
      console.log("package_relay: handler imported from the zip and is callable");
    '
  ) || status=$?

  rm -rf "${probe_dir}"
  return "${status}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-dir)
      [[ $# -ge 2 ]] || die "--build-dir requires a directory argument"
      build_dir_flag="$2"
      shift 2
      ;;
    --skip-install)
      skip_install=1
      shift
      ;;
    --no-verify-import)
      verify_import=0
      shift
      ;;
    --verify-only)
      [[ $# -ge 2 ]] || die "--verify-only requires a zip path argument"
      verify_only_zip="$2"
      shift 2
      ;;
    -h | --help)
      sed -n '3,45p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      die "unknown argument: $1 (see --help)"
      ;;
  esac
done

# Flag -> environment -> default; a flag overriding LAMBDA_BUILD_DIR is logged.
if [[ -n "${build_dir_flag}" ]]; then
  if [[ -n "${LAMBDA_BUILD_DIR:-}" && "${LAMBDA_BUILD_DIR}" != "${build_dir_flag}" ]]; then
    log "--build-dir ${build_dir_flag} overrides LAMBDA_BUILD_DIR=${LAMBDA_BUILD_DIR}"
  fi
  build_dir="${build_dir_flag}"
else
  build_dir="${LAMBDA_BUILD_DIR:-${DEFAULT_BUILD_DIR}}"
fi
[[ "${build_dir}" == /* ]] || build_dir="${REPO_ROOT}/${build_dir}"

if [[ -n "${verify_only_zip}" ]]; then
  [[ "${verify_only_zip}" == /* ]] || verify_only_zip="${PWD}/${verify_only_zip}"
  log "verifying ${HANDLER_MODULE}.${HANDLER_EXPORT} imports from ${verify_only_zip}"
  verify_zip_import "${verify_only_zip}"
  exit 0
fi

command -v node >/dev/null 2>&1 || die "node is not on PATH"
command -v npm >/dev/null 2>&1 || die "npm is not on PATH"
command -v zip >/dev/null 2>&1 || die "zip is not on PATH"
[[ -d "${RELAY_DIR}" ]] || die "no relay package at ${RELAY_DIR}"
[[ -f "${RELAY_DIR}/package-lock.json" ]] || die "no package-lock.json in ${RELAY_DIR}"

node_major="$(node -p 'process.versions.node.split(".")[0]')"
if [[ "${node_major}" -lt 24 ]]; then
  die "node ${node_major} is older than the nodejs24.x target runtime (src/relay/package.json engines)"
fi

staging_dir="$(mktemp -d "${TMPDIR:-/tmp}/relay-pkg.XXXXXX")"
cleanup() { rm -rf "${staging_dir}"; }
trap cleanup EXIT

# --- Compile -----------------------------------------------------------------
if [[ "${skip_install}" -eq 1 ]]; then
  [[ -d "${RELAY_DIR}/node_modules" ]] || die "--skip-install given but ${RELAY_DIR}/node_modules is absent"
  log "skipping npm ci (--skip-install)"
else
  log "installing dev+prod dependencies for the build (npm ci)"
  (cd "${RELAY_DIR}" && npm ci --no-audit --no-fund)
fi

readonly COMPILED_DIR="${RELAY_DIR}/build"

# Cleaned before the compile, since `tsc` never removes output for a deleted source module.
rm -rf "${COMPILED_DIR}"

# A compile failure is fatal, including tsc's TS18003 for an empty compile scope.
log "compiling src/relay (tsc -p tsconfig.build.json)"
(cd "${RELAY_DIR}" && npm run --silent build)

[[ -d "${COMPILED_DIR}" ]] || die "compile produced no ${COMPILED_DIR}"
if [[ -z "$(find "${COMPILED_DIR}" -name '*.js' -type f -print -quit)" ]]; then
  die "compile produced no .js output in ${COMPILED_DIR} -- refusing to package an empty artefact"
fi
[[ -f "${COMPILED_DIR}/${HANDLER_MODULE}.js" ]] ||
  die "compile produced no ${HANDLER_MODULE}.js; the '${HANDLER_MODULE}.${HANDLER_EXPORT}' handler could not resolve at runtime"

# --- Stage -------------------------------------------------------------------
# Compiled output is copied to the staging ROOT so `index.js` is at the archive root.
log "staging compiled output"
cp -R "${COMPILED_DIR}/." "${staging_dir}/"

# A minimal package.json; only `type: module` is needed at runtime.
cat >"${staging_dir}/package.json" <<'JSON'
{
  "name": "crm-eval-relay",
  "private": true,
  "type": "module"
}
JSON

log "installing production dependencies into the artefact (npm ci --omit=dev)"
cp "${RELAY_DIR}/package.json" "${staging_dir}/.package.json.build"
cp "${RELAY_DIR}/package-lock.json" "${staging_dir}/package-lock.json"
(
  cd "${staging_dir}"
  mv package.json .package.json.deployed
  mv .package.json.build package.json
  npm ci --omit=dev --no-audit --no-fund >/dev/null
  mv package.json .package.json.build
  mv .package.json.deployed package.json
)
rm -f "${staging_dir}/.package.json.build" "${staging_dir}/package-lock.json"
[[ -d "${staging_dir}/node_modules" ]] || die "production install produced no node_modules"

# Computed from `src/relay`, not from the staging tree.
log "recording the build manifest (${BUILD_MANIFEST})"
compute_build_manifest >"${staging_dir}/${BUILD_MANIFEST}"

# --- Archive -----------------------------------------------------------------
# Normalised mtimes and a sorted entry list, so an unchanged source tree produces an unchanged zip.
mkdir -p "${build_dir}"
readonly ZIP_PATH="${build_dir}/${ZIP_NAME}"
rm -f "${ZIP_PATH}"

# `-t` takes `[[CC]YY]MMDDhhmm[.SS]`; YY `00` is 2000. The century is omitted so this line carries no
# 12-digit run, which check_no_account_id reports.
find "${staging_dir}" -exec touch -t 0001010000.00 {} +
(
  cd "${staging_dir}"
  # -X drops platform extras (uid/gid, extended attrs); LC_ALL=C makes the sort locale-independent.
  find . -type f -o -type l | LC_ALL=C sort | zip -q -X -9 "${ZIP_PATH}" -@
)
[[ -f "${ZIP_PATH}" ]] || die "zip produced no ${ZIP_PATH}"

# --- Verify ------------------------------------------------------------------
if [[ "${verify_import}" -eq 1 ]]; then
  log "verifying ${HANDLER_MODULE}.${HANDLER_EXPORT} imports from the zip"
  verify_zip_import "${ZIP_PATH}"
fi

zip_bytes="$(wc -c <"${ZIP_PATH}" | tr -d ' ')"
log "wrote ${ZIP_PATH} (${zip_bytes} bytes)"
