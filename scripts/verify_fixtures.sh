#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Regenerates the demo-tenant fixture corpus into a scratch directory and diffs it byte-for-byte
# against the committed fixtures/*.json files. Exits non-zero and prints the diff on any
# divergence. Runs offline; no AWS credentials required.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURES_DIR="${REPO_ROOT}/fixtures"
SCRATCH_DIR="$(mktemp -d)"
trap 'rm -rf "${SCRATCH_DIR}"' EXIT

cd "${REPO_ROOT}"
python3 -m fixtures.generate --tenant demo --out "${SCRATCH_DIR}"

STATUS=0
for generated_file in "${SCRATCH_DIR}"/*.json; do
  filename="$(basename "${generated_file}")"
  committed_file="${FIXTURES_DIR}/${filename}"
  if [[ ! -f "${committed_file}" ]]; then
    echo "MISSING committed fixture: ${committed_file}" >&2
    STATUS=1
    continue
  fi
  if ! diff -u "${committed_file}" "${generated_file}"; then
    echo "DIVERGED: ${filename} does not match a fresh regeneration" >&2
    STATUS=1
  fi
done

for committed_file in "${FIXTURES_DIR}"/*.json; do
  filename="$(basename "${committed_file}")"
  if [[ ! -f "${SCRATCH_DIR}/${filename}" ]]; then
    echo "STALE committed fixture with no generator output: ${committed_file}" >&2
    STATUS=1
  fi
done

if [[ "${STATUS}" -eq 0 ]]; then
  echo "OK: regeneration is byte-identical to committed fixtures/*.json"
fi

exit "${STATUS}"
