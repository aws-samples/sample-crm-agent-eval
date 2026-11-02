#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Fails if the scanned tree contains an AWS account id, a VPC id, a subnet id, a security-group id,
# a non-placeholder IAM user name in an ARN, a private-key marker, or an absolute local filesystem
# path outside the repository. Reads only
# through `git grep`; never calls an AWS API or the network.
#
# The subject is a required argument:
#   --worktree      tracked files as they are on disk.
#   --cached        staged content (the bytes a commit would record).
#   --ref <git-ref> the tree of an existing commit.
# A pre-commit caller uses --cached; `--ref HEAD` there would scan the parent commit.
#
# Usage: `scripts/check_no_account_id.sh (--worktree | --cached | --ref <git-ref>)`.
set -euo pipefail

# Under a UTF-8 locale (C.UTF-8 included) a line with an invalid byte sequence is skipped by git grep
# or kills sed, so the locale is pinned to C for this process. Every pattern is ASCII.
export LC_ALL=C

usage() {
  cat >&2 <<'USAGE'
check_no_account_id: a subject is REQUIRED — this gate has no default subject.
  --worktree        tracked files as they are on disk (catches an operator mid-edit)
  --cached          staged content — exactly what a commit would record
  --ref <git-ref>   the tree of an existing commit
Usage: scripts/check_no_account_id.sh (--worktree | --cached | --ref <git-ref>)
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

#: Names the subject in every violation message, so a failure says which tree it read.
case "$MODE" in
  worktree) SUBJECT_LABEL="tracked working-tree content" ;;
  cached) SUBJECT_LABEL="staged content" ;;
  ref) SUBJECT_LABEL="content at ref ${REF}" ;;
esac

# `git grep <ref>` prefixes each output line with "<ref>:", so the path-exclusion anchor includes it.
# A ref name with any character outside `[A-Za-z0-9_/.~^-]` is refused.
if [[ "$MODE" == "ref" ]]; then
  if [[ ! "$REF" =~ ^[A-Za-z0-9_/.~^-]+$ ]]; then
    echo "check_no_account_id: refusing --ref '${REF}': it contains a character that would be read as a regex metacharacter when anchoring the path exclusions, so an exclusion could silently mis-match. Use a plain ref name." >&2
    exit 2
  fi
  # `.` and `^` are legal in ref names and ARE ERE metacharacters, so escape them for the anchor.
  PATH_ANCHOR="^$(printf '%s' "$REF" | sed 's/[.^]/\\&/g'):"
else
  PATH_ANCHOR="^"
fi

#: Exit status for "the scan could not be performed" (not 3, which run_gates.py renders as a skip).
EXIT_UNREADABLE=4

#: This gate's own name, for the diagnostics below.
GATE_NAME="check_no_account_id"

# Git discovery variables are cleared so the repository scanned is the one containing the cwd.
unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY

# GIT_INDEX_FILE is kept (a partial commit's temporary index) but must resolve, as a physical path,
# inside this repository's git directory. Checked before the `cd` below, since git may set it relative.
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
  # Re-exported absolute so the `cd` below cannot move a relative value out from under it.
  export GIT_INDEX_FILE="$_staged_index"
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

#: Captures `git grep`'s stderr. The explicit `XXXXXX` template is portable to GNU mktemp.
_GREP_STDERR="$(mktemp "${TMPDIR:-/tmp}/check_no_account_id_stderr.XXXXXX")"

#: Scratch files for `select_survivors`, created once and reused for every pattern.
_CAND_LINES="$(mktemp "${TMPDIR:-/tmp}/check_no_account_id_cand.XXXXXX")"
_REDACTED_LINES="$(mktemp "${TMPDIR:-/tmp}/check_no_account_id_redacted.XXXXXX")"
_SURVIVOR_LINE_NOS="$(mktemp "${TMPDIR:-/tmp}/check_no_account_id_survivors.XXXXXX")"
_PASS_STDERR="$(mktemp "${TMPDIR:-/tmp}/check_no_account_id_pass_stderr.XXXXXX")"
trap 'rm -f "$_GREP_STDERR" "$_CAND_LINES" "$_REDACTED_LINES" "$_SURVIVOR_LINE_NOS" "$_PASS_STDERR"' EXIT

# Runs `git grep` into GIT_GREP_OUT; exits 4 on a status above 1 or on any stderr output. Call it as a
# plain command, not inside `$( )`, so its `exit` ends the script.
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

# Exits 4 when the subject has no readable text line at all (for example, every file marked binary).
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

# Placeholder account ids redacted before re-testing a candidate line. Also parsed by
# scripts/check_untracked_artifact_secrets.py and tests/unit/test_gates.py.
_PLACEHOLDER_ACCOUNT_IDS='(000000000000|123456789012|999999999999|111122223333|555566667777|111111111111)'

#: Placeholder VPC, subnet, endpoint, route-table and internet-gateway ids (eight leading zeros or the
#: AWS documentation form).
#: Only the placeholder token is redacted; the rest of the line is still tested.
_PLACEHOLDER_RESOURCE_IDS='(vpc|subnet|vpce|rtb|igw)-(0{8}([0-9a-f]{9})?|0123456789abcdef0)'

#: Placeholder IAM user names, matched as a whole name: the redaction consumes the character after the
#: name, so a longer name sharing the prefix is not excused.
_PLACEHOLDER_IAM_USERS='user\/(example-deployer|smoke-test)([^A-Za-z0-9+=,.@_/-]|$)'

_BENIGN_SHAPES=(
  "$_PLACEHOLDER_ACCOUNT_IDS"
  "$_PLACEHOLDER_RESOURCE_IDS"
  "$_PLACEHOLDER_IAM_USERS"
)

# One `sed` expression per benign shape, applied in array order.
_REDACT_EXPRS=()
for _shape in "${_BENIGN_SHAPES[@]}"; do
  _REDACT_EXPRS+=(-e "s/${_shape}/<redacted>/g")
done
unset _shape

# Re-tests the `$MATCHES` block against $1 with the benign shapes redacted, writing the line numbers
# that still match to `$_SURVIVOR_LINE_NOS`; the caller selects the original lines by number. Exits 4
# if sed fails or changes the line count, or grep exits above 1.
select_survivors() {
  local pattern="$1" rc n_cand n_red
  #: An empty `$MATCHES` has no survivors.
  : > "$_SURVIVOR_LINE_NOS"
  [[ -n "$MATCHES" ]] || return 0
  printf '%s\n' "$MATCHES" > "$_CAND_LINES"
  set +e
  sed -E "${_REDACT_EXPRS[@]}" "$_CAND_LINES" > "$_REDACTED_LINES" 2> "$_PASS_STDERR"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    echo "${GATE_NAME}: the benign-shape redaction pass exited ${rc}, so it stopped part-way and every candidate line from that point on would leave the re-test unexamined. For pattern /${pattern}/ that is a re-test which verified NOTHING, not a clean result:" >&2
    cat "$_PASS_STDERR" >&2
    exit "$EXIT_UNREADABLE"
  fi
  n_cand="$(wc -l < "$_CAND_LINES")"
  n_red="$(wc -l < "$_REDACTED_LINES")"
  if [[ "$n_cand" -ne "$n_red" ]]; then
    #: `$(( ))` strips the padding BSD `wc -l` adds.
    echo "${GATE_NAME}: the benign-shape redaction pass returned $((n_red)) lines for $((n_cand)) candidates, so a surviving line number no longer addresses the line it was measured on and the report could name the wrong line. Refusing rather than reporting on the wrong subject." >&2
    exit "$EXIT_UNREADABLE"
  fi
  set +e
  grep -n -E -e "$pattern" "$_REDACTED_LINES" > "$_SURVIVOR_LINE_NOS" 2> "$_PASS_STDERR"
  rc=$?
  set -e
  if [[ "$rc" -gt 1 ]]; then
    echo "${GATE_NAME}: the post-redaction re-test exited ${rc}, which is neither a match (0) nor a clean no-match (1), so no candidate line for pattern /${pattern}/ was actually re-tested:" >&2
    cat "$_PASS_STDERR" >&2
    exit "$EXIT_UNREADABLE"
  fi
}
PATTERNS=(
  "arn:aws[a-zA-Z-]*:[a-zA-Z0-9-]+:[a-zA-Z0-9-]*:(0{12}|[0-9]{12}):"
  '(account[_-]?id|AccountId)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"']?[0-9]{12}'
  # An IAM user ARN under any account id; only the `_PLACEHOLDER_IAM_USERS` names are excused.
  'arn:aws[a-zA-Z-]*:iam::[^:]*:user/[A-Za-z0-9+=,.@_/-]+'
  'vpc-[0-9a-f]{8}([0-9a-f]{9})?'
  'subnet-[0-9a-f]{8}([0-9a-f]{9})?'
  'sg-[0-9a-f]{8}([0-9a-f]{9})?'
  # VPC endpoint, route table and internet gateway ids.
  '(vpce|rtb|igw)-[0-9a-f]{8}([0-9a-f]{9})?'
  # CloudFront VPC origin id: `vo_` plus a suffix with at least two `[A-Z0-9]` characters, so
  # lowercase snake_case identifiers do not match. The suffix length is not fixed.
  'vo_[A-Za-z0-9]*[A-Z0-9][A-Za-z0-9]*[A-Z0-9][A-Za-z0-9]*'
  '-----BEGIN( RSA| EC| OPENSSH| DSA)? PRIVATE KEY-----'
  '/(Users|home)/[A-Za-z0-9._-]+/'
  # A bare 12-digit run in any context, not flanked by a digit or hex letter (so hex digests and
  # longer decimal figures do not match).
  '(^|[^0-9a-fA-F])[0-9]{12}([^0-9a-fA-F]|$)'
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
  MATCHES="$(printf '%s\n' "$MATCHES" | grep -v -E "${PATH_ANCHOR}scripts/check_no_account_id\.sh" || true)"
  # A line that no longer matches once placeholders are redacted is not a finding.
  select_survivors "$pattern"
  #: Selects the original lines by survivor line number. The `-s` guard keeps awk's `NR == FNR` from
  #: treating the candidate file as the index when there are no survivors; `$0 != ""` drops the blank
  #: line produced from an empty `$MATCHES`.
  if [[ -s "$_SURVIVOR_LINE_NOS" ]]; then
    MATCHES="$(awk -F: 'NR == FNR { keep[$1]; next } FNR in keep && $0 != ""' \
      "$_SURVIVOR_LINE_NOS" "$_CAND_LINES")"
  else
    MATCHES=""
  fi
  if [[ -n "$MATCHES" ]]; then
    echo "check_no_account_id: VIOLATION — pattern /$pattern/ matched in ${SUBJECT_LABEL}:" >&2
    printf '%s\n' "$MATCHES" >&2
    FOUND=1
  fi
done

if [[ "$FOUND" -ne 0 ]]; then
  echo "check_no_account_id: FAILED" >&2
  exit 1
fi

echo "check_no_account_id: OK"
