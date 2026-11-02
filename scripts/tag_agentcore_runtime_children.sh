#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Copies the AgentCore runtime's tags onto the two resources the service creates under it: the
# DEFAULT runtime endpoint and the runtime's workload identity. Then reads both back and fails
# unless each carries every runtime tag at the runtime's value. Idempotent.
#
# Usage:
#   scripts/tag_agentcore_runtime_children.sh --profile NAME --region REGION --runtime-arn ARN
#
# Requires aws and jq. Exit codes: 0 tagged and verified; 1 an AWS call failed, the runtime has
# no tags, or a read-back differs; 2 usage error.
set -euo pipefail
export LC_ALL=C

die() { printf 'tag_agentcore_runtime_children: %s\n' "$1" >&2; exit "${2:-1}"; }
info() { printf 'tag_agentcore_runtime_children: %s\n' "$1"; }

PROFILE=""
REGION=""
RUNTIME_ARN=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) PROFILE="${2:-}"; shift 2 ;;
        --region) REGION="${2:-}"; shift 2 ;;
        --runtime-arn) RUNTIME_ARN="${2:-}"; shift 2 ;;
        *) die "unknown argument: $1" 2 ;;
    esac
done
[[ -n "$PROFILE" && -n "$REGION" && -n "$RUNTIME_ARN" ]] \
    || die "--profile, --region and --runtime-arn are required" 2
[[ "$RUNTIME_ARN" =~ ^arn:aws:bedrock-agentcore:${REGION}:[0-9]{12}:runtime/([A-Za-z0-9_-]+)$ ]] \
    || die "--runtime-arn is not an AgentCore runtime ARN in ${REGION}" 2
RUNTIME_ID="${BASH_REMATCH[1]}"

command -v aws >/dev/null 2>&1 || die "aws CLI not found on PATH"
command -v jq >/dev/null 2>&1 || die "jq not found on PATH"

AWS_ARGS=(--profile "$PROFILE" --region "$REGION" --no-cli-pager --output json)
control() { aws bedrock-agentcore-control "$@" "${AWS_ARGS[@]}"; }

RUNTIME_TAGS="$(control list-tags-for-resource --resource-arn "$RUNTIME_ARN" | jq -c '.tags // {}')" \
    || die "list-tags-for-resource on the runtime failed"
[[ "$(jq 'length' <<<"$RUNTIME_TAGS")" -gt 0 ]] \
    || die "the runtime carries no tags; nothing to copy, refusing to report success"

ENDPOINT_ARN="$(control get-agent-runtime-endpoint --agent-runtime-id "$RUNTIME_ID" \
    --endpoint-name DEFAULT | jq -r '.agentRuntimeEndpointArn // empty')" \
    || die "get-agent-runtime-endpoint DEFAULT failed"
[[ -n "$ENDPOINT_ARN" ]] || die "the DEFAULT endpoint returned no agentRuntimeEndpointArn"

IDENTITY_ARN="$(control get-agent-runtime --agent-runtime-id "$RUNTIME_ID" \
    | jq -r '.workloadIdentityDetails.workloadIdentityArn // empty')" \
    || die "get-agent-runtime failed"
[[ -n "$IDENTITY_ARN" ]] || die "the runtime returned no workloadIdentityDetails.workloadIdentityArn"

for target in "$ENDPOINT_ARN" "$IDENTITY_ARN"; do
    control tag-resource --resource-arn "$target" --tags "$RUNTIME_TAGS" >/dev/null \
        || die "tag-resource failed on ${target##*:}"
    actual="$(control list-tags-for-resource --resource-arn "$target" | jq -c '.tags // {}')" \
        || die "list-tags-for-resource failed on ${target##*:}"
    missing="$(jq -nc --argjson want "$RUNTIME_TAGS" --argjson have "$actual" \
        '[$want | to_entries[] | select($have[.key] != .value) | .key]')"
    [[ "$missing" == "[]" ]] || die "${target##*:} lacks runtime tag(s) after tagging: ${missing}"
    info "tagged ${target##*:} with $(jq 'length' <<<"$RUNTIME_TAGS") runtime tag(s)"
done
