#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Resolves the interpreter for repository Python: <repo>/.venv/bin/python3 when executable, else an
# ambient `python3`. The chosen interpreter must be at least MIN_PYTHON, else exit 2 naming the required
# and found versions; a stale virtualenv is refused rather than skipped.
#
# Usage: scripts/repo_python.sh --print          print the resolved interpreter path
#        scripts/repo_python.sh ARGS...          exec the resolved interpreter with ARGS
#
# MIN_PYTHON must equal the major.minor of .python-version (tests/unit/test_repo_python.py).
#
# Exit codes: 2 no acceptable interpreter, or no arguments; otherwise the interpreter's own status.
set -euo pipefail

# The script's own matchers run under LC_ALL=C; the exec'd interpreter gets the caller's LC_ALL (set or
# unset as it was).
if [[ -n "${LC_ALL+set}" ]]; then
  CALLER_LC_ALL_SET=1
  CALLER_LC_ALL="$LC_ALL"
else
  CALLER_LC_ALL_SET=0
  CALLER_LC_ALL=""
fi
export LC_ALL=C

readonly MIN_PYTHON="3.14"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() {
  echo "repo_python.sh: $*" >&2
  exit 2
}

[[ $# -gt 0 ]] || die "usage: repo_python.sh --print | repo_python.sh ARGS..."

# Prints the major.minor of interpreter $1, or exits 2 naming $2 (how the interpreter was found).
version_of() {
  local found
  if ! found="$("$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"; then
    die "requires Python >= ${MIN_PYTHON}: could not run $2 $1"
  fi
  [[ "$found" =~ ^[0-9]+\.[0-9]+$ ]] ||
    die "requires Python >= ${MIN_PYTHON}: $2 $1 reported an unreadable version '${found}'"
  printf '%s\n' "$found"
}

# Exit 0 when major.minor $1 is at least MIN_PYTHON.
meets_floor() {
  local want_major="${MIN_PYTHON%%.*}" want_minor="${MIN_PYTHON#*.}"
  local have_major="${1%%.*}" have_minor="${1#*.}"
  ((have_major > want_major || (have_major == want_major && have_minor >= want_minor)))
}

resolve() {
  local venv_python="${REPO_ROOT}/.venv/bin/python3"
  local found
  if [[ -x "$venv_python" ]]; then
    found="$(version_of "$venv_python" "the project virtualenv")"
    meets_floor "$found" ||
      die "requires Python >= ${MIN_PYTHON}: the project virtualenv ${venv_python} is ${found}; rebuild .venv"
    printf '%s\n' "$venv_python"
    return 0
  fi
  local ambient
  ambient="$(command -v python3 || true)"
  [[ -n "$ambient" ]] ||
    die "requires Python >= ${MIN_PYTHON}: no ${venv_python} and no python3 on PATH"
  found="$(version_of "$ambient" "the ambient")"
  if meets_floor "$found"; then
    printf '%s\n' "$ambient"
    return 0
  fi
  die "requires Python >= ${MIN_PYTHON}: no ${venv_python}, and the ambient ${ambient} is ${found}"
}

PYTHON="$(resolve)"
if [[ "$1" == "--print" ]]; then
  [[ $# -eq 1 ]] || die "--print takes no further arguments"
  printf '%s\n' "$PYTHON"
  exit 0
fi
if ((CALLER_LC_ALL_SET)); then
  export LC_ALL="$CALLER_LC_ALL"
else
  unset LC_ALL
fi
exec "$PYTHON" "$@"
