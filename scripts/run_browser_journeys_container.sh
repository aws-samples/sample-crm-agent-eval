#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Runs the T4 browser journeys (tests/e2e/browser) inside the official Playwright Linux container,
# against the deployed dev stack. Billable: the journeys drive the live stack.
#
# Usage:
#   scripts/run_browser_journeys_container.sh [playwright test args...]
#
# Target (one is required; there is no default):
#   CRM_E2E_BASE_URL       the dev distribution URL, or
#   CRM_TF_OUTPUTS_JSON    a stack outputs document (default: results/tf-outputs.json), which must
#                          pass `scripts/outputs_provenance.py --check`
#
# Passed into the container by name only when set (never on the command line):
#   CRM_DEMO_USER_EMAIL, CRM_DEMO_USER_PASSWORD, CRM_E2E_ADMIN_EMAIL, CRM_E2E_ADMIN_PASSWORD,
#   CRM_E2E_ALLOW_LIVE_SWEEP, CRM_E2E_ALLOW_LIVE_SWEEP_CANCEL, CRM_E2E_BASE_URL
#
# Environment overrides:
#   DOCKER_BIN             docker CLI (default: docker on PATH, else Docker Desktop's bundled CLI)
#   PLAYWRIGHT_IMAGE_DISTRO  image distro suffix (default noble)
#
# The image is mcr.microsoft.com/playwright:v<version>-<distro>, the version read from
# tests/e2e/browser/package-lock.json; the platform is linux/amd64. `npm ci` installs into a
# container-only volume, so the host's node_modules is untouched.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
JOURNEYS_DIR="${REPO_ROOT}/tests/e2e/browser"
LOCK_FILE="${JOURNEYS_DIR}/package-lock.json"
CONTAINER_ROOT="/repo"
PLATFORM="linux/amd64"
DISTRO="${PLAYWRIGHT_IMAGE_DISTRO:-noble}"
DOCKER_DESKTOP_CLI="/Applications/Docker.app/Contents/Resources/bin/docker"

PASSTHROUGH_ENV=(
    CRM_DEMO_USER_EMAIL
    CRM_DEMO_USER_PASSWORD
    CRM_E2E_ADMIN_EMAIL
    CRM_E2E_ADMIN_PASSWORD
    CRM_E2E_ALLOW_LIVE_SWEEP
    CRM_E2E_ALLOW_LIVE_SWEEP_CANCEL
    CRM_E2E_BASE_URL
)

die() { printf 'run_browser_journeys_container: %s\n' "$1" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 not found on PATH"

[[ -f "$LOCK_FILE" ]] || die "no ${LOCK_FILE}; cannot pin the Playwright image"
PLAYWRIGHT_VERSION="$(python3 - "$LOCK_FILE" <<'PY'
import json, re, sys
packages = json.load(open(sys.argv[1], encoding="utf-8")).get("packages", {})
versions = {packages.get(f"node_modules/{n}", {}).get("version") for n in ("@playwright/test", "playwright", "playwright-core")}
if len(versions) != 1 or not re.fullmatch(r"\d+\.\d+\.\d+", str(next(iter(versions)))):
    sys.exit(f"lock file Playwright versions are missing or disagree: {sorted(map(str, versions))}")
print(next(iter(versions)))
PY
)" || die "could not read the Playwright version from ${LOCK_FILE}"
IMAGE="mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-${DISTRO}"

if [[ -n "${DOCKER_BIN:-}" ]]; then
    DOCKER="$DOCKER_BIN"
elif command -v docker >/dev/null 2>&1; then
    DOCKER="$(command -v docker)"
elif [[ -x "$DOCKER_DESKTOP_CLI" ]]; then
    DOCKER="$DOCKER_DESKTOP_CLI"
else
    die "docker CLI not found (set DOCKER_BIN)"
fi

DOCKER_ARGS=(run --rm --init --ipc=host --platform "$PLATFORM"
    -v "${REPO_ROOT}:${CONTAINER_ROOT}"
    -v "${CONTAINER_ROOT}/tests/e2e/browser/node_modules"
    -w "${CONTAINER_ROOT}/tests/e2e/browser")

if [[ -z "${CRM_E2E_BASE_URL:-}" ]]; then
    OUTPUTS="${CRM_TF_OUTPUTS_JSON:-${REPO_ROOT}/results/tf-outputs.json}"
    [[ -f "$OUTPUTS" ]] || die "no target: set CRM_E2E_BASE_URL, or provide a stack outputs document (CRM_TF_OUTPUTS_JSON, default results/tf-outputs.json; ${OUTPUTS} does not exist)"
    OUTPUTS="$(cd "$(dirname "$OUTPUTS")" && pwd)/$(basename "$OUTPUTS")"
    [[ "$OUTPUTS" == "${REPO_ROOT}/"* ]] || die "outputs document ${OUTPUTS} is outside the repository, so the container cannot read it"
    [[ -x "${REPO_ROOT}/.venv/bin/python3" ]] || die "no .venv; cannot check ${OUTPUTS} for freshness"
    "${REPO_ROOT}/.venv/bin/python3" "${REPO_ROOT}/scripts/outputs_provenance.py" --check "$OUTPUTS" >&2 \
        || die "stack outputs ${OUTPUTS} are stale or unreadable; refusing to drive an older stack"
    DOCKER_ARGS+=(-e "CRM_TF_OUTPUTS_JSON=${CONTAINER_ROOT}/${OUTPUTS#"${REPO_ROOT}/"}")
fi

for name in "${PASSTHROUGH_ENV[@]}"; do
    if [[ -n "${!name:-}" ]]; then
        DOCKER_ARGS+=(-e "$name")
    fi
done

# After the run, scripts/check-executed.mjs fails a report that executed nothing or skipped a test
# outside its ALLOWED_SKIPS; Playwright's own non-zero status is kept. The report is removed first
# so a run that writes none cannot pass on a previous one.
# shellcheck disable=SC2016  # expanded by the container's bash, not this one
CONTAINER_SCRIPT='npm ci --no-audit --no-fund && rm -f ../../../results/e2e-browser.json && {
    rc=0
    ./node_modules/.bin/playwright test "$@" || rc=$?
    node scripts/check-executed.mjs ../../../results/e2e-browser.json || { [ "$rc" -ne 0 ] || rc=1; }
    exit "$rc"
}'
DOCKER_ARGS+=("$IMAGE" bash -c "$CONTAINER_SCRIPT" journeys)

printf 'run_browser_journeys_container: image %s, platform %s\n' "$IMAGE" "$PLATFORM" >&2
exec "$DOCKER" "${DOCKER_ARGS[@]}" "$@"
