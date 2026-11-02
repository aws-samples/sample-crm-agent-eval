#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Fails if any first-party source file in the named subject is missing an SPDX license header.
#
# Scope (SCOPE_PATTERNS below; every match is checked, empty __init__.py files included):
#   - *.py under src/, scripts/, tests/, fixtures/
#   - *.sh under scripts/ and infra/
#   - *.ts, *.tsx, *.js, *.mjs, *.cjs under frontend/
#   - *.ts, *.js, *.mjs, *.cjs under tests/e2e/browser/ and src/relay/
#   - githooks/pre-commit
# Gitignored paths (generated output, node_modules) are never enumerated.
#
# The subject is a required argument:
#   --worktree      tracked files as they are on disk, plus untracked in-scope files.
#   --cached        staged content (the bytes a commit would record); untracked files are not scanned.
#   --ref <git-ref> the tree of an existing commit.
# A pre-commit caller uses --cached.
#
# Usage: scripts/check_license_headers.sh (--worktree | --cached | --ref <git-ref>)
# Exit codes: 0 pass; 1 one or more files missing the header; 2 no subject named, or bad usage;
# 4 the scan could not be performed (foreign GIT_INDEX_FILE, or no in-scope file read).
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
check_license_headers: a subject is REQUIRED — this gate has no default subject.
  --worktree        tracked files on disk, plus untracked in-scope files
  --cached          staged content — exactly what a commit would record
  --ref <git-ref>   the tree of an existing commit
Usage: scripts/check_license_headers.sh (--worktree | --cached | --ref <git-ref>)
USAGE
}

MODE=""
REF=""
case "${1:-}" in
  --worktree)
    MODE="worktree"
    [[ "$#" -eq 1 ]] || { usage; exit 2; }
    ;;
  --cached)
    MODE="cached"
    [[ "$#" -eq 1 ]] || { usage; exit 2; }
    ;;
  --ref)
    MODE="ref"
    REF="${2:-}"
    [[ "$#" -eq 2 && -n "$REF" ]] || { usage; exit 2; }
    ;;
  *)
    usage
    exit 2
    ;;
esac

#: Names the subject in the violation message, so a failure says which tree it read.
case "$MODE" in
  worktree) SUBJECT_LABEL="tracked working-tree content plus untracked in-scope files" ;;
  cached) SUBJECT_LABEL="staged content" ;;
  ref) SUBJECT_LABEL="content at ref ${REF}" ;;
esac

#: Exit status for "the scan could not be performed" (not 3, which run_gates.py renders as a skip).
EXIT_UNREADABLE=4

# Git discovery variables are cleared so the repository scanned is the one containing the cwd.
# GIT_INDEX_FILE is kept for --cached but must resolve inside this repository's git directory.
unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY

if [[ -n "${GIT_INDEX_FILE:-}" ]]; then
  case "$GIT_INDEX_FILE" in
    /*) _staged_index="$GIT_INDEX_FILE" ;;
    *) _staged_index="$PWD/$GIT_INDEX_FILE" ;;
  esac
  _index_dir="$(cd -P -- "$(dirname -- "$_staged_index")" 2> /dev/null && pwd -P)" || _index_dir=""
  _own_git_dir="$(cd -P -- "$(git rev-parse --absolute-git-dir)" && pwd -P)"
  if [[ "$_index_dir" != "$_own_git_dir" ]]; then
    echo "check_license_headers: GIT_INDEX_FILE ('${GIT_INDEX_FILE}') does not resolve inside this repository's git directory ('${_own_git_dir}'), so the enumeration would read an index belonging to something else. Refusing rather than reporting on the wrong subject." >&2
    exit "$EXIT_UNREADABLE"
  fi
  export GIT_INDEX_FILE="$_staged_index"
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

SPDX_PATTERN='SPDX-License-Identifier:'

# Path lists are NUL-delimited end to end, so paths with spaces or non-ASCII bytes survive.
filter_paths() {
  # NUL-in, NUL-out: emits the paths matching the bash regex $1 (BSD grep has no NUL-data mode).
  local pattern="$1" path
  while IFS= read -r -d '' path; do
    if [[ "$path" =~ $pattern ]]; then
      printf '%s\0' "$path"
    fi
  done
  return 0
}

list_tracked() {
  # Enumerates the index (--worktree, --cached) or the ref's tree (--ref).
  local pattern="$1"
  if [[ "$MODE" == "ref" ]]; then
    git ls-tree -r -z --name-only "$REF" -- .
  else
    git ls-files -z -- .
  fi | filter_paths "$pattern"
  return 0
}

list_untracked() {
  # Untracked, non-ignored files, listed individually (-uall); --worktree only. `-z` records are
  # `XY<space>PATH`, so the path begins at offset 3.
  local pattern="$1" record
  [[ "$MODE" != "worktree" ]] && return 0
  while IFS= read -r -d '' record; do
    if [[ "${record:0:2}" == "??" ]]; then
      printf '%s\0' "${record:3}"
    fi
  done < <(git status --porcelain -z -uall -- .) | filter_paths "$pattern"
  return 0
}

read_working_tree_file() {
  # Reads an untracked file from the working tree.
  local path="$1"
  cat -- "$path"
}

read_tracked_file() {
  # Reads a tracked file's content from the named subject.
  local path="$1"
  case "$MODE" in
    ref) git show "${REF}:${path}" ;;
    cached) git show ":${path}" ;;
    worktree) cat -- "$path" ;;
  esac
}

# Matches in-shell rather than piping to `grep -q`, whose early exit SIGPIPEs the producer under pipefail.
has_spdx() {
  local content="$1"
  [[ "$content" == *"$SPDX_PATTERN"* ]]
}

MISSING=()

#: How many files were read; zero means the scan verified nothing.
EXAMINED=0

check_paths() {
  # Runs the missing-header check over a NUL-delimited path list read via $reader, appending any
  # violation to MISSING. $reader is either read_tracked_file or read_working_tree_file.
  local reader="$1" path content
  while IFS= read -r -d '' path; do
    [[ -z "$path" ]] && continue
    EXAMINED=$((EXAMINED + 1))
    content="$("$reader" "$path")"
    if ! has_spdx "$content"; then
      MISSING+=("$path")
    fi
  done
}

#: Bash regexes over repository-relative paths; a path matching any one is in scope.
SCOPE_PATTERNS=(
  '^(src|scripts|tests|fixtures)/.*\.py$'
  '^(scripts|infra)/.*\.sh$'
  '^frontend/.*\.(ts|tsx|js|mjs|cjs)$'
  '^(tests/e2e/browser|src/relay)/.*\.(ts|js|mjs|cjs)$'
  '^githooks/pre-commit$'
)

for scope_pattern in "${SCOPE_PATTERNS[@]}"; do
  check_paths read_tracked_file < <(list_tracked "$scope_pattern")
  check_paths read_working_tree_file < <(list_untracked "$scope_pattern")
done

# A run that read no file is refused before any result is reported.
if [[ "$EXAMINED" -eq 0 ]]; then
  echo "check_license_headers: not one in-scope file was found in the ${SUBJECT_LABEL}, so a pass here would verify NOTHING. Refusing to report a result." >&2
  echo "check_license_headers: that is what a substituted or unusable index looks like — this repository always has in-scope files." >&2
  exit "$EXIT_UNREADABLE"
fi

if [[ "${#MISSING[@]}" -gt 0 ]]; then
  echo "check_license_headers: VIOLATION — missing SPDX-License-Identifier header in ${SUBJECT_LABEL}:" >&2
  printf '  %s\n' "${MISSING[@]}" >&2
  echo "check_license_headers: FAILED" >&2
  exit 1
fi

echo "check_license_headers: OK"
