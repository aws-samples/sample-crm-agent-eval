#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Materialise the current STS session into an isolated AWS profile and run commands under it.
#
#   scripts/session_profile.sh write  --dir DIR --profile NAME --region REGION
#   scripts/session_profile.sh exec   --dir DIR --profile NAME --region REGION -- CMD [ARG...]
#   scripts/session_profile.sh status --dir DIR --profile NAME
#   scripts/session_profile.sh delete --dir DIR
#
# write   reads AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN and
#         AWS_CREDENTIAL_EXPIRATION (ISO-8601 with an offset) from the environment and writes
#         DIR/credentials, DIR/config and DIR/expiration (DIR 0700, files 0600). An existing DIR must
#         be empty or already owned (the checks `delete` applies). DIR must lie outside
#         any git work tree, and at least 60 minutes must remain. Where `tmutil` exists, DIR is
#         excluded from Time Machine (best effort).
# exec    unsets every ambient credential, profile and endpoint-override variable (the legacy
#         AWS_ACCESS_KEY and AWS_SECRET_KEY included), points AWS_CONFIG_FILE and
#         AWS_SHARED_CREDENTIALS_FILE at DIR, sets AWS_PROFILE, AWS_REGION, AWS_DEFAULT_REGION,
#         AWS_SDK_LOAD_CONFIG and AWS_EC2_METADATA_DISABLED=true, and execs CMD. Needs at least
#         15 minutes left.
#
# A relative DIR is resolved against the working directory once, before any subcommand runs.
# status  prints the minutes left.
# delete  removes the files and DIR, only after checking that DIR is 0700, carries the marker `write`
#         leaves, and holds nothing else; otherwise it refuses with every file left in place.
#
# Exit codes: 0 ok; 2 refusal or usage error; 4 session files absent, or expiring/expired.
# Credential values are never printed. Requires python3 (expiry and mode checks).

set -euo pipefail
umask 077
if [[ -n "${LC_ALL+set}" ]]; then
    CALLER_LC_ALL_SET=1
    CALLER_LC_ALL="$LC_ALL"
else
    CALLER_LC_ALL_SET=0
    CALLER_LC_ALL=""
fi
export LC_ALL=C

readonly WRITE_MIN_MINUTES=60
readonly EXEC_MIN_MINUTES=15
readonly SESSION_FILES="credentials config expiration"
readonly MARKER=".session-profile"
readonly REQUIRED_ENV="AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_CREDENTIAL_EXPIRATION"
readonly UNSET_ON_EXEC="AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
AWS_ACCESS_KEY AWS_SECRET_KEY
AWS_CREDENTIAL_EXPIRATION AWS_VAULT AWS_PROFILE AWS_DEFAULT_PROFILE
AWS_WEB_IDENTITY_TOKEN_FILE AWS_ROLE_ARN AWS_ROLE_SESSION_NAME
AWS_CONTAINER_CREDENTIALS_FULL_URI AWS_CONTAINER_CREDENTIALS_RELATIVE_URI
AWS_CONTAINER_AUTHORIZATION_TOKEN"
readonly PROFILE_PATTERN='^[A-Za-z0-9_-]+$'
readonly REGION_PATTERN='^[a-z]{2}(-[a-z]+)+-[0-9]+$'

# Prints whole minutes until an ISO-8601 timestamp; exits 3 if it is unparseable or has no offset.
readonly MINUTES_LEFT_PY='
import datetime, sys
text = sys.argv[1].strip()
if text[-1:] in ("Z", "z"):
    text = text[:-1] + "+00:00"
try:
    expiry = datetime.datetime.fromisoformat(text)
except ValueError:
    sys.exit(3)
if expiry.tzinfo is None:
    sys.exit(3)
now = datetime.datetime.now(datetime.timezone.utc)
print(int((expiry - now).total_seconds() // 60))
'
readonly MODE_IS_0700_PY='
import os, stat, sys
sys.exit(0 if stat.S_IMODE(os.lstat(sys.argv[1]).st_mode) == 0o700 else 1)
'

TEMP_FILE=""
DIR_ENTRIES=()
trap '[[ -z "$TEMP_FILE" ]] || rm -f "$TEMP_FILE"' EXIT

die() {
    local code="$1"
    shift
    printf 'session_profile.sh: %s\n' "$*" >&2
    exit "$code"
}

usage() {
    die 2 "$1. Usage: session_profile.sh {write|exec|status|delete} --dir DIR [--profile NAME] [--region REGION] [-- CMD ...]"
}

minutes_left() {
    local left
    if ! left="$(python3 -c "$MINUTES_LEFT_PY" "$1")"; then
        return 1
    fi
    printf '%s\n' "$left"
}

absolute_path() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        *) printf '%s/%s\n' "$PWD" "$1" ;;
    esac
}

physical_ancestor() {
    local path="$1"
    while [[ ! -d "$path" ]]; do
        path="$(dirname "$path")"
    done
    (cd -P "$path" && pwd -P)
}

inside_git_tree() {
    local path="$1"
    while :; do
        if [[ -e "$path/.git" ]]; then
            return 0
        fi
        [[ "$path" == "/" ]] && return 1
        path="$(dirname "$path")"
    done
}

require_session_files() {
    local name
    for name in $SESSION_FILES; do
        [[ -f "$DIR/$name" ]] || die 4 "$DIR/$name is absent; run 'session_profile.sh write' from the session shell"
    done
    grep -qxF "[profile $PROFILE]" "$DIR/config" \
        || die 2 "$DIR/config holds no profile '$PROFILE'"
}

session_minutes_left() {
    local left
    left="$(minutes_left "$(cat "$DIR/expiration")")" \
        || die 4 "$DIR/expiration does not hold an ISO-8601 timestamp with an offset"
    printf '%s\n' "$left"
}

write_file() {
    local target="$1"
    shift
    TEMP_FILE="$(mktemp "$DIR/.${target}.XXXXXX")"
    printf '%s\n' "$@" >"$TEMP_FILE"
    chmod 600 "$TEMP_FILE"
    mv -f "$TEMP_FILE" "$DIR/$target"
    TEMP_FILE=""
}

exclude_from_backups() {
    command -v tmutil >/dev/null 2>&1 || return 0
    tmutil addexclusion "$DIR" >/dev/null 2>&1 \
        || printf 'session_profile.sh: warning: tmutil addexclusion %s failed; the directory may be backed up\n' "$DIR" >&2
}

is_session_entry() {
    case "$1" in
        credentials | config | expiration | "$MARKER") return 0 ;;
        .credentials.?????? | .config.?????? | .expiration.??????) return 0 ;;
    esac
    return 1
}

# Sets DIR_ENTRIES to every entry of DIR, dotfiles included.
list_dir_entries() {
    shopt -s dotglob nullglob
    DIR_ENTRIES=("$DIR"/*)
    shopt -u dotglob nullglob
}

# Dies (exit 2, with "$1" appended) unless DIR is 0700, carries the marker, and holds only the
# regular files `write` creates. Sets DIR_ENTRIES.
require_owned_dir() {
    local outcome="$1" entry name
    python3 -c "$MODE_IS_0700_PY" "$DIR" || die 2 "$DIR is not mode 0700; $outcome"
    [[ -f "$DIR/$MARKER" && ! -L "$DIR/$MARKER" ]] \
        || die 2 "$DIR has no $MARKER marker, so this script did not write it; $outcome"
    list_dir_entries
    for entry in "${DIR_ENTRIES[@]}"; do
        name="${entry##*/}"
        if ! is_session_entry "$name" || [[ -L "$entry" || ! -f "$entry" ]]; then
            die 2 "$DIR holds '$name', which this script did not write; $outcome"
        fi
    done
}

cmd_write() {
    local name value left
    for name in $REQUIRED_ENV; do
        value="${!name:-}"
        [[ -n "$value" ]] || die 2 "$name is not set; run 'write' from the session shell"
        case "$value" in
            *[[:space:]]*) die 2 "$name contains whitespace; refusing to write it" ;;
        esac
    done
    left="$(minutes_left "$AWS_CREDENTIAL_EXPIRATION")" \
        || die 2 "AWS_CREDENTIAL_EXPIRATION is not an ISO-8601 timestamp with an offset"
    if ((left < WRITE_MIN_MINUTES)); then
        die 2 "the session has ${left} min left; refresh it (at least ${WRITE_MIN_MINUTES} min required)"
    fi
    local physical
    physical="$(physical_ancestor "$DIR")" || die 2 "cannot resolve $DIR"
    if inside_git_tree "$physical"; then
        die 2 "$DIR is inside a git work tree; choose a directory outside every repository"
    fi
    if [[ -L "$DIR" ]]; then
        die 2 "$DIR is a symlink; refusing"
    fi
    if [[ -e "$DIR" ]]; then
        [[ -d "$DIR" ]] || die 2 "$DIR exists and is not a directory; nothing was written"
        python3 -c "$MODE_IS_0700_PY" "$DIR" || die 2 "$DIR exists and is not mode 0700; nothing was written"
        list_dir_entries
        if ((${#DIR_ENTRIES[@]} > 0)); then
            require_owned_dir "nothing was written"
        fi
    else
        mkdir -p "$DIR"
    fi
    chmod 700 "$DIR"

    printf '%s\n' "written by scripts/session_profile.sh" >"$DIR/$MARKER"
    write_file credentials "[$PROFILE]" \
        "aws_access_key_id = $AWS_ACCESS_KEY_ID" \
        "aws_secret_access_key = $AWS_SECRET_ACCESS_KEY" \
        "aws_session_token = $AWS_SESSION_TOKEN"
    write_file config "[profile $PROFILE]" "region = $REGION"
    write_file expiration "$AWS_CREDENTIAL_EXPIRATION"
    exclude_from_backups

    printf 'dir=%s profile=%s expires=%s minutes_left=%s\n' \
        "$DIR" "$PROFILE" "$AWS_CREDENTIAL_EXPIRATION" "$left"
}

cmd_exec() {
    local left name
    require_session_files
    left="$(session_minutes_left)"
    if ((left < EXEC_MIN_MINUTES)); then
        die 4 "the session has ${left} min left (at least ${EXEC_MIN_MINUTES} required); re-run 'write' from a refreshed session"
    fi
    for name in $UNSET_ON_EXEC ${!AWS_ENDPOINT_URL@}; do
        unset "$name"
    done
    export AWS_CONFIG_FILE="$DIR/config"
    export AWS_SHARED_CREDENTIALS_FILE="$DIR/credentials"
    export AWS_PROFILE="$PROFILE"
    export AWS_REGION="$REGION"
    export AWS_DEFAULT_REGION="$REGION"
    export AWS_SDK_LOAD_CONFIG=1
    export AWS_EC2_METADATA_DISABLED=true
    if ((CALLER_LC_ALL_SET)); then
        export LC_ALL="$CALLER_LC_ALL"
    else
        unset LC_ALL
    fi
    trap - EXIT
    exec "$@"
}

cmd_status() {
    local left
    require_session_files
    left="$(session_minutes_left)"
    printf 'dir=%s profile=%s expires=%s minutes_left=%s\n' \
        "$DIR" "$PROFILE" "$(cat "$DIR/expiration")" "$left"
    if ((left < EXEC_MIN_MINUTES)); then
        die 4 "the session has ${left} min left (at least ${EXEC_MIN_MINUTES} required)"
    fi
}

cmd_delete() {
    [[ -L "$DIR" ]] && die 2 "$DIR is a symlink; nothing was removed"
    if [[ ! -e "$DIR" ]]; then
        printf '%s is already absent\n' "$DIR"
        return 0
    fi
    [[ -d "$DIR" ]] || die 2 "$DIR is not a directory; nothing was removed"
    require_owned_dir "nothing was removed"
    rm -f -- "${DIR_ENTRIES[@]}"
    rmdir "$DIR" || die 2 "could not remove $DIR"
    printf 'deleted %s\n' "$DIR"
}

[[ $# -ge 1 ]] || usage "no subcommand"
SUBCOMMAND="$1"
shift
DIR=""
PROFILE=""
REGION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir | --profile | --region)
            [[ $# -ge 2 && -n "$2" ]] || usage "$1 needs a value"
            case "$1" in
                --dir) DIR="$2" ;;
                --profile) PROFILE="$2" ;;
                --region) REGION="$2" ;;
            esac
            shift 2
            ;;
        --)
            shift
            break
            ;;
        *) usage "unknown argument '$1'" ;;
    esac
done

[[ -n "$DIR" ]] || usage "--dir is required"
DIR="$(absolute_path "$DIR")"
case "$SUBCOMMAND" in
    write | exec | status)
        [[ "$PROFILE" =~ $PROFILE_PATTERN ]] || usage "--profile must match $PROFILE_PATTERN"
        ;;
esac
case "$SUBCOMMAND" in
    write | exec)
        [[ "$REGION" =~ $REGION_PATTERN ]] || usage "--region must be an AWS region name"
        ;;
esac

case "$SUBCOMMAND" in
    write)
        [[ $# -eq 0 ]] || usage "write takes no command"
        cmd_write
        ;;
    exec)
        [[ $# -ge 1 ]] || usage "exec needs '-- CMD [ARG...]'"
        cmd_exec "$@"
        ;;
    status)
        [[ $# -eq 0 ]] || usage "status takes no command"
        cmd_status
        ;;
    delete)
        [[ $# -eq 0 ]] || usage "delete takes no command"
        cmd_delete
        ;;
    *) usage "unknown subcommand '$SUBCOMMAND'" ;;
esac
