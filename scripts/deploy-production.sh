#!/usr/bin/env bash

set -Eeuo pipefail

# Run this script from any directory on the production host. It updates only a
# clean checkout, rebuilds the selected Compose service, and waits for its
# Docker health check to pass.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

REMOTE="${DEPLOY_REMOTE:-origin}"
BRANCH="${DEPLOY_BRANCH:-main}"
SERVICE="${DEPLOY_SERVICE:-tvs-ai-gateway}"
HEALTH_TIMEOUT="${DEPLOY_HEALTH_TIMEOUT:-180}"
POLL_INTERVAL="${DEPLOY_POLL_INTERVAL:-3}"

log() {
  printf '[deploy] %s\n' "$*"
}

fail() {
  printf '[deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

show_diagnostics() {
  local exit_code=$?
  if (( exit_code != 0 )); then
    printf '[deploy] Deployment failed. Recent container logs follow.\n' >&2
    docker compose logs --tail 100 "${SERVICE}" >&2 || true
  fi
  exit "${exit_code}"
}

trap show_diagnostics EXIT

require_command git
require_command docker

cd "${PROJECT_DIR}"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || fail "Not a Git working tree: ${PROJECT_DIR}"
docker compose version >/dev/null 2>&1 \
  || fail "Docker Compose v2 is required (docker compose)."

[[ "${HEALTH_TIMEOUT}" =~ ^[1-9][0-9]*$ ]] \
  || fail "DEPLOY_HEALTH_TIMEOUT must be a positive integer."
[[ "${POLL_INTERVAL}" =~ ^[1-9][0-9]*$ ]] \
  || fail "DEPLOY_POLL_INTERVAL must be a positive integer."
[[ -f .env ]] || fail ".env is missing. Create it from .env.example and set production secrets."

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  fail "Tracked files contain local changes. Commit or restore them before deployment."
fi

log "Fetching ${REMOTE}/${BRANCH}"
git fetch --prune "${REMOTE}" "${BRANCH}"

current_branch="$(git branch --show-current)"
[[ "${current_branch}" == "${BRANCH}" ]] \
  || fail "Current branch is '${current_branch:-detached}', expected '${BRANCH}'."

before_revision="$(git rev-parse HEAD)"
git merge --ff-only "${REMOTE}/${BRANCH}"
after_revision="$(git rev-parse HEAD)"
log "Source updated: ${before_revision:0:12} -> ${after_revision:0:12}"

log "Validating Compose configuration"
docker compose config --quiet

log "Building ${SERVICE}"
docker compose build --pull "${SERVICE}"

log "Starting ${SERVICE}"
docker compose up -d --no-deps --remove-orphans "${SERVICE}"

container_id="$(docker compose ps -q "${SERVICE}")"
[[ -n "${container_id}" ]] || fail "Compose did not return a container ID for ${SERVICE}."

log "Waiting up to ${HEALTH_TIMEOUT}s for the container health check"
deadline=$((SECONDS + HEALTH_TIMEOUT))
while (( SECONDS < deadline )); do
  state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")"
  case "${state}" in
    healthy)
      trap - EXIT
      log "Deployment completed: ${SERVICE} is healthy (${after_revision:0:12})."
      exit 0
      ;;
    running)
      # A service without a Docker health check is considered started.
      trap - EXIT
      log "Deployment completed: ${SERVICE} is running (${after_revision:0:12})."
      exit 0
      ;;
    unhealthy|exited|dead)
      fail "Container entered '${state}' state."
      ;;
  esac
  sleep "${POLL_INTERVAL}"
done

fail "Health check timed out after ${HEALTH_TIMEOUT}s."
