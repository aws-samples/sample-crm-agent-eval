#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Fails if the scanned tree contains a vendor model-provider API key pattern (Anthropic, OpenAI, or a
# key assigned to a vendor-named variable). Reads only through `git grep`; makes no network call and
# needs no credential.
#
# The subject is a required argument:
#   --worktree      tracked files as they are on disk.
#   --cached        staged content (the bytes a commit would record).
#   --ref <git-ref> the tree of an existing commit.
#
# Usage: `scripts/check_no_vendor_keys.sh (--worktree | --cached | --ref <git-ref>)`.
set -euo pipefail

# Under a UTF-8 locale (C.UTF-8 included) git grep skips matches on lines with invalid byte sequences,
# so the locale is pinned to C for this process.
export LC_ALL=C

usage() {
  cat >&2 <<'USAGE'
check_no_vendor_keys: a subject is REQUIRED — this gate has no default subject.
  --worktree        tracked files as they are on disk (catches an operator mid-edit)
  --cached          staged content — exactly what a commit would record
  --ref <git-ref>   the tree of an existing commit
Usage: scripts/check_no_vendor_keys.sh (--worktree | --cached | --ref <git-ref>)
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

case "$MODE" in
  worktree) SUBJECT_LABEL="tracked working-tree content" ;;
  cached) SUBJECT_LABEL="staged content" ;;
  ref) SUBJECT_LABEL="content at ref ${REF}" ;;
esac

# `git grep <ref>` prefixes each output line with "<ref>:", so the path-exclusion anchor includes it.
if [[ "$MODE" == "ref" ]]; then
  if [[ ! "$REF" =~ ^[A-Za-z0-9_/.~^-]+$ ]]; then
    echo "check_no_vendor_keys: refusing --ref '${REF}': it contains a character that would be read as a regex metacharacter when anchoring the path exclusions, so an exclusion could silently mis-match. Use a plain ref name." >&2
    exit 2
  fi
  # `.` and `^` are legal in ref names and ARE ERE metacharacters, so escape them for the anchor.
  PATH_ANCHOR="^$(printf '%s' "$REF" | sed 's/[.^]/\\&/g'):"
else
  PATH_ANCHOR="^"
fi

#: Exit status for "the scan could not be performed" (not 3, which run_gates.py renders as a skip).
EXIT_UNREADABLE=4

GATE_NAME="check_no_vendor_keys"

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
    echo "${GATE_NAME}: GIT_INDEX_FILE ('${GIT_INDEX_FILE}') does not resolve inside this repository's git directory ('${_own_git_dir}'), so --cached would scan an index belonging to something else. Refusing rather than reporting on the wrong subject." >&2
    exit "$EXIT_UNREADABLE"
  fi
  export GIT_INDEX_FILE="$_staged_index"
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

#: Captures `git grep`'s stderr. The explicit `XXXXXX` template is portable to GNU mktemp.
_GREP_STDERR="$(mktemp "${TMPDIR:-/tmp}/check_no_vendor_keys_stderr.XXXXXX")"
trap 'rm -f "$_GREP_STDERR"' EXIT

# Runs `git grep` into GIT_GREP_OUT; exits 4 on a status above 1 or on any stderr output.
GIT_GREP_OUT=""
git_grep_scan() {
  local rc
  set +e
  GIT_GREP_OUT="$(git grep "$@" 2>"$_GREP_STDERR")"
  rc=$?
  set -e
  if [[ "$rc" -gt 1 || -s "$_GREP_STDERR" ]]; then
    if [[ "$rc" -gt 1 ]]; then
      echo "${GATE_NAME}: git grep exited ${rc}, which is neither a match (0) nor a clean no-match (1) — the scan verified NOTHING, so this is a FAILURE and not an OK:" >&2
    else
      echo "${GATE_NAME}: git grep exited ${rc} (\"no match\") but reported a diagnostic, so some content was NOT read and the no-match is not evidence of absence:" >&2
    fi
    cat "$_GREP_STDERR" >&2
    exit "$EXIT_UNREADABLE"
  fi
}

# Exits 4 when the subject has no readable text line at all.
assert_subject_is_readable() {
  case "$MODE" in
    ref) git_grep_scan -I -l -E -e '.' "$REF" -- . ;;
    cached) git_grep_scan --cached -I -l -E -e '.' -- . ;;
    worktree) git_grep_scan -I -l -E -e '.' -- . ;;
  esac
  if [[ -z "$GIT_GREP_OUT" ]]; then
    echo "${GATE_NAME}: not one readable line of text was found anywhere in the ${SUBJECT_LABEL}, so every pattern below would find nothing and this gate would report OK having verified NOTHING. Refusing to scan." >&2
    echo "${GATE_NAME}: that is what an empty subject or a git attribute marking tracked text as binary looks like — not what a clean repository looks like." >&2
    exit "$EXIT_UNREADABLE"
  fi
}

assert_subject_is_readable

# Vendor key prefixes plus a vendor-named-variable assignment. POSIX ERE has no `\b`, so the
# patterns are unanchored.
PATTERNS=(
  'sk-ant-[A-Za-z0-9_-]{20,}'
  'sk-(proj-)?[A-Za-z0-9_-]{20,}'
  '(ANTHROPIC|OPENAI)_API_KEY["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9_-]{16,}'
)

FOUND=0

for pattern in "${PATTERNS[@]}"; do
  MATCHES=""
  case "$MODE" in
    ref)
      git_grep_scan -I -n -E -e "$pattern" "$REF" -- .
      MATCHES="$GIT_GREP_OUT"
      ;;
    cached)
      git_grep_scan --cached -I -n -E -e "$pattern" -- .
      MATCHES="$GIT_GREP_OUT"
      ;;
    worktree)
      git_grep_scan -I -n -E -e "$pattern" -- .
      MATCHES="$GIT_GREP_OUT"
      ;;
  esac
  # This script is excluded: it defines the patterns.
  MATCHES="$(printf '%s\n' "$MATCHES" | grep -v -E "${PATH_ANCHOR}scripts/check_no_vendor_keys\.sh" || true)"
  if [[ -n "$MATCHES" ]]; then
    echo "check_no_vendor_keys: VIOLATION — pattern /$pattern/ matched in ${SUBJECT_LABEL}:" >&2
    printf '%s\n' "$MATCHES" >&2
    FOUND=1
  fi
done

if [[ "$FOUND" -ne 0 ]]; then
  echo "check_no_vendor_keys: FAILED" >&2
  exit 1
fi

echo "check_no_vendor_keys: OK"
