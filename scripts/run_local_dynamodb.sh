#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Starts DynamoDB Local as a Docker container on loopback for the local integration tests.
# Requires no AWS credential; the container accepts its `dummy` credential pair.
#
# Usage:
#   scripts/run_local_dynamodb.sh start   # start (or reuse) the container, block until ready
#   scripts/run_local_dynamodb.sh stop    # stop and remove the container
#   scripts/run_local_dynamodb.sh status  # exit 0 if reachable, 1 otherwise; prints the reason
#
# Environment overrides:
#   DYNAMODB_LOCAL_PORT      host port to bind (default 8000)
#   DYNAMODB_LOCAL_CONTAINER container name (default crm-eval-dynamodb-local)
#   DYNAMODB_LOCAL_IMAGE     image reference (default amazon/dynamodb-local 3.3.2, pinned by digest)

set -euo pipefail

readonly PORT="${DYNAMODB_LOCAL_PORT:-8000}"
readonly CONTAINER="${DYNAMODB_LOCAL_CONTAINER:-crm-eval-dynamodb-local}"
readonly IMAGE="${DYNAMODB_LOCAL_IMAGE:-amazon/dynamodb-local:3.3.2@sha256:6ada631ec7aa5fc83446855eb17233e3dad184fec47477e8140c6c569cfe0c86}"
readonly READY_TIMEOUT_SECONDS=30

log() {
  printf '[run_local_dynamodb] %s\n' "$1" >&2
}

docker_available() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

reachable() {
  # TCP-connect readiness probe; `status` also checks the container name.
  (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") >/dev/null 2>&1
}

wait_until_ready() {
  local waited=0
  while ! reachable; do
    if [ "${waited}" -ge "${READY_TIMEOUT_SECONDS}" ]; then
      log "DynamoDB Local did not become reachable on port ${PORT} within ${READY_TIMEOUT_SECONDS}s"
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  return 0
}

cmd_start() {
  if ! docker_available; then
    log "docker is not installed or the docker daemon is not running; skipping start"
    return 1
  fi

  local running_names
  running_names="$(docker ps --filter "name=^${CONTAINER}$" --filter "status=running" --format '{{.Names}}')" \
    || running_names=""
  if grep -qx "${CONTAINER}" <<<"${running_names}"; then
    log "container ${CONTAINER} is already running"
  else
    docker rm --force "${CONTAINER}" >/dev/null 2>&1 || true
    log "starting ${IMAGE} as ${CONTAINER} on 127.0.0.1:${PORT}"
    docker run \
      --detach \
      --rm \
      --name "${CONTAINER}" \
      --publish "127.0.0.1:${PORT}:8000" \
      "${IMAGE}" \
      -jar DynamoDBLocal.jar -inMemory -sharedDb >/dev/null
  fi

  if wait_until_ready; then
    log "DynamoDB Local is ready on 127.0.0.1:${PORT}"
    return 0
  fi
  return 1
}

cmd_stop() {
  if ! command -v docker >/dev/null 2>&1; then
    log "docker is not installed; nothing to stop"
    return 0
  fi
  docker rm --force "${CONTAINER}" >/dev/null 2>&1 || true
  log "container ${CONTAINER} stopped and removed"
}

cmd_status() {
  if reachable; then
    log "DynamoDB Local is reachable on 127.0.0.1:${PORT}"
    return 0
  fi
  log "DynamoDB Local is not reachable on 127.0.0.1:${PORT}"
  return 1
}

main() {
  local action="${1:-}"
  case "${action}" in
    start)
      cmd_start
      ;;
    stop)
      cmd_stop
      ;;
    status)
      cmd_status
      ;;
    *)
      log "usage: $0 {start|stop|status}"
      return 2
      ;;
  esac
}

main "$@"
