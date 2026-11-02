#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Checks, while config/pricing.yaml carries provisional: true, that:
#   1. No absolute currency figure appears in a published artifact (results/** plus README.md).
#   1b. No absolute currency figure is rendered by the eval dashboard (--frontend-dir).
#   2. No matrix.json arm sets cost_withheld_provisional to false while every rate in the price
#      table's models block is zero.
#
# When provisional is false, this script performs no check and exits 0.
#
# Usage: check_cost_withheld.sh [--pricing-file PATH] [--results-dir PATH] [--readme-file PATH]
#                               [--frontend-dir PATH]
#
# Detection (implemented in scripts/check_cost_withheld.py):
#   a. Cost field names (cost_input, cost_output, cost_cache_write, cost_cache_read,
#      cost_cache_total, cost_total) at any JSON depth, and textually in matrix.md / matrix.csv.
#   b. Any numeric JSON key or CSV column not in _NON_CURRENCY_NUMERIC_FIELDS. Not applied to prose.
#   c. Currency shapes in every scanned artifact: Unicode currency symbols, ISO codes beside a
#      numeral, unit words, and spelled-out amounts before a unit word, after HTML-unescaping,
#      zero-width stripping, and re-scanning adjacent line pairs.
#
# Not detected (see _CURRENCY_SHAPE_PATTERN): a spelled-out amount with no unit word, an unrecognised
# unit word, a figure split across more than one line break, a renamed cost key in a text artifact,
# and a bare number with no currency indication.
#
# Exit codes: 0 pass; 1 violation found; 2 usage/environment error (pricing file missing, python3
# unavailable, or a tracked consumer path absent); 3 verified nothing (results/ absent and no other
# consumer read). A violation outranks exit 3. provisional: false exits 0, not 3.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd -P)"

PRICING_FILE="${REPO_ROOT}/config/pricing.yaml"
RESULTS_DIR="${REPO_ROOT}/results"
README_FILE="${REPO_ROOT}/README.md"
FRONTEND_DIR="${REPO_ROOT}/frontend/src/pages/evals"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --pricing-file)
      PRICING_FILE="$2"
      shift 2
      ;;
    --results-dir)
      RESULTS_DIR="$2"
      shift 2
      ;;
    --readme-file)
      README_FILE="$2"
      shift 2
      ;;
    --frontend-dir)
      FRONTEND_DIR="$2"
      shift 2
      ;;
    *)
      echo "check_cost_withheld.sh: unrecognised argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ ! -f "${PRICING_FILE}" ]; then
  echo "check_cost_withheld.sh: pricing file not found: ${PRICING_FILE}" >&2
  exit 2
fi

PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
if [ ! -x "${PYTHON_BIN}" ]; then
  PYTHON_BIN="python3"
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "check_cost_withheld.sh: no python interpreter available" >&2
  exit 2
fi

# The README and frontend dir are tracked, so an absent one is exit 2; an absent results/ is exit 3
# (reported by the Python).
if [ ! -f "${README_FILE}" ]; then
  echo "check_cost_withheld.sh: readme file not found: ${README_FILE}" >&2
  echo "  This is a tracked file. Its absence removes a scanned consumer, so it is an error rather" >&2
  echo "  than a skipped half -- pass --readme-file if it moved." >&2
  exit 2
fi
if [ ! -d "${FRONTEND_DIR}" ]; then
  echo "check_cost_withheld.sh: frontend dir not found: ${FRONTEND_DIR}" >&2
  echo "  This is a tracked directory carrying the eval dashboard's render path. Its absence removes" >&2
  echo "  the consumer that caught a rendered 'Cost7.5' behind a green gate, so it is an error" >&2
  echo "  rather than a skipped half -- pass --frontend-dir if it moved." >&2
  exit 2
fi

RUN_ARGS=(
  --pricing-file "${PRICING_FILE}"
  --results-dir "${RESULTS_DIR}"
  --readme-file "${README_FILE}"
  --frontend-dir "${FRONTEND_DIR}"
)

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/check_cost_withheld.py" "${RUN_ARGS[@]}"
