#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Materialises only git-tracked content into a scratch directory, to show which gates can run
# without untracked local files. It does not run `make verify` there: the scratch tree has no `.venv`.
#
# Usage: scripts/verify_fresh_clone.sh [--ref REF] [--materialise-only]
#   --ref REF            Git tree-ish to materialise via `git archive` (default: HEAD).
#                         Pass `--ref -` to materialise the index (staged content) instead.
#   --materialise-only   Populate the scratch tree and print its path, but do not run gates.
#
# The scratch directory is removed on exit by a `trap`, except under `--materialise-only`, where the
# caller becomes responsible for removing it.
set -euo pipefail

# An inherited GIT_DIR or sibling variable overrides `-C`, so these are unset before any git call.
# GIT_INDEX_FILE is unset too: `--ref -` means this repository's index.
unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_INDEX_FILE

SCRIPT_NAME="$(basename "$0")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

REF="HEAD"
MATERIALISE_ONLY=false

usage() {
    cat <<EOF
Usage: $SCRIPT_NAME [--ref REF] [--materialise-only]
  --ref REF            Tree-ish to materialise (default: HEAD). Use "-" for the index/staged tree.
  --materialise-only   Populate the scratch tree, print its path, and exit without running gates.
  -h, --help           Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref)
            REF="${2:?--ref requires a value}"
            shift 2
            ;;
        --materialise-only)
            MATERIALISE_ONLY=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

SCRATCH_DIR="$(mktemp -d "${TMPDIR:-/tmp}/verify_fresh_clone.XXXXXXXX")"

cleanup() {
    rm -rf "$SCRATCH_DIR"
}
trap cleanup EXIT INT TERM

# materialise_tracked_tree REF DEST
# Streams the tracked content of REF ("-" means the index) into DEST via `git archive | tar -x`.
# DEST must already exist and be empty.
materialise_tracked_tree() {
    local ref="$1"
    local dest="$2"
    if [[ "$ref" == "-" ]]; then
        git -C "$REPO_ROOT" archive --format=tar HEAD -- 2>/dev/null | tar -x -C "$dest" 2>/dev/null || true
        # `git archive` has no built-in "index" tree-ish; write the index out as a real tree
        # object and archive that instead, without ever touching HEAD or the working tree.
        local index_tree
        index_tree="$(git -C "$REPO_ROOT" write-tree)"
        rm -rf "${dest:?}"/*
        git -C "$REPO_ROOT" archive --format=tar "$index_tree" | tar -x -C "$dest"
    else
        git -C "$REPO_ROOT" archive --format=tar "$ref" | tar -x -C "$dest"
    fi
}

materialise_tracked_tree "$REF" "$SCRATCH_DIR"

echo "Materialised ref '$REF' (tracked content only) into: $SCRATCH_DIR" >&2

if [[ "$MATERIALISE_ONLY" == "true" ]]; then
    echo "$SCRATCH_DIR"
    trap - EXIT INT TERM
    exit 0
fi

echo "NOTE: scratch tree has no .venv and no offline dependency source; gates requiring" >&2
echo "installed Python packages (pytest, mypy, ruff) cannot run inside the scratch tree." >&2
echo "Shell-only, dependency-free gates could be run directly against: $SCRATCH_DIR" >&2

echo "$SCRATCH_DIR"
