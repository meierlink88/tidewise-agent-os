#!/usr/bin/env bash

set -Eeuo pipefail

deployment_root="${DEPLOY_ROOT:?DEPLOY_ROOT is required}"
runtime_env="${RUNTIME_ENV:?RUNTIME_ENV is required}"
candidate_images="${CANDIDATE_IMAGES:?CANDIDATE_IMAGES is required}"
candidate_compose="${COMPOSE_FILE:?COMPOSE_FILE is required}"
release_sha="${RELEASE_SHA:?RELEASE_SHA is required}"
external_url="${AGENTOS_EXTERNAL_URL:?AGENTOS_EXTERNAL_URL is required}"
stage_only="${STAGE_ONLY:-false}"
state_dir="${deployment_root}/state"
current_runtime="${deployment_root}/runtime.env"
current_images="${state_dir}/current.images.env"
current_compose="${state_dir}/current.compose.yaml"
current_sha="${state_dir}/current.sha"
previous_runtime="${state_dir}/previous.runtime.env"
previous_images="${state_dir}/previous.images.env"
previous_compose="${state_dir}/previous.compose.yaml"
previous_sha="${state_dir}/previous.sha"
rollback_in_progress=false
diagnostics_file="${DIAGNOSTICS_FILE:-}"

mkdir -p "$state_dir"
exec 9>"${deployment_root}/deploy.lock"
flock -n 9 || { echo "FAIL agentos-deploy-lock: another AgentOS deployment is running" >&2; exit 1; }

compose_for() {
  local runtime="$1"
  local images="$2"
  local compose_file="$3"
  shift 3
  docker compose --env-file "$runtime" --env-file "$images" -f "$compose_file" "$@"
}

verify_release() {
  local runtime="$1"
  local images="$2"
  local compose_file="$3"
  local expected_sha="$4"
  compose_for "$runtime" "$images" "$compose_file" exec -T agentos \
    curl -fsS http://127.0.0.1:9081/health >/dev/null
  compose_for "$runtime" "$images" "$compose_file" exec -T agentos python /app/scripts/smoke_uat.py
  compose_for "$runtime" "$images" "$compose_file" exec -T agentos python -m sematica.graphiti.readiness
  if [ "$stage_only" = true ]; then
    echo "PASS staged-agentos-health-auth-components-schedules-mcp"
    return 0
  fi
  external_headers="$(curl --silent --show-error --connect-timeout 5 --max-time 20 \
    --dump-header - --output /dev/null "${external_url%/}/health")"
  external_status="$(awk 'toupper($1) ~ /^HTTP\// {code=$2} END {print code}' <<< "$external_headers")"
  [ "$external_status" = 200 ] || { echo "FAIL external-health: HTTP ${external_status}" >&2; return 1; }
  external_release="$(awk -F': *' 'tolower($1) == "x-tidewise-release" {gsub("\r", "", $2); value=$2} END {print value}' <<< "$external_headers")"
  [ "$external_release" = "$expected_sha" ] \
    || { echo "FAIL external-release: expected ${expected_sha}, got ${external_release:-missing}" >&2; return 1; }
  auth_status="$(curl --silent --show-error --connect-timeout 5 --max-time 20 \
    --output /dev/null --write-out '%{http_code}' "${external_url%/}/agents")"
  case "$auth_status" in
    401|403) ;;
    *) echo "FAIL external-auth-gate: expected 401/403, got HTTP ${auth_status}" >&2; return 1 ;;
  esac
  echo "PASS agentos-health-auth-components-schedules-mcp"
}

migrate_candidate_database() {
  local runtime="$1"
  local images="$2"
  local compose_file="$3"
  compose_for "$runtime" "$images" "$compose_file" run --rm --no-deps agentos \
    python -m scripts.migrate_agno_db
  echo "PASS agno-database-migration"
}

rollback() {
  rollback_in_progress=true
  echo "Candidate verification failed; restoring the previous AgentOS release" >&2
  if [ -s "$current_runtime" ] && [ -s "$current_images" ] && [ -s "$current_compose" ]; then
    compose_for "$current_runtime" "$current_images" "$current_compose" up -d --wait --wait-timeout 180 agentos
    verify_release "$current_runtime" "$current_images" "$current_compose" "$(cat "$current_sha")"
    echo "PASS rollback-previous-agentos-release" >&2
  else
    compose_for "$runtime_env" "$candidate_images" "$candidate_compose" stop --timeout 30 agentos || true
    compose_for "$runtime_env" "$candidate_images" "$candidate_compose" rm -f agentos || true
    echo "PASS rollback-first-deploy-candidate-removed" >&2
  fi
}

capture_candidate_diagnostics() {
  [ -n "$diagnostics_file" ] || return 0
  RUNTIME_ENV="$runtime_env" \
    CANDIDATE_IMAGES="$candidate_images" \
    COMPOSE_FILE="$candidate_compose" \
    "$(dirname "$candidate_compose")/collect-diagnostics.sh" > "$diagnostics_file" || true
}

on_error() {
  local exit_code="$1"
  trap - ERR
  if [ "$rollback_in_progress" = false ]; then
    set +e
    capture_candidate_diagnostics
    rollback
    rollback_code="$?"
    set -e
    if [ "$rollback_code" -ne 0 ]; then
      echo "FAIL rollback: manual recovery required" >&2
    fi
  fi
  exit "$exit_code"
}
trap 'on_error $?' ERR

compose_for "$runtime_env" "$candidate_images" "$candidate_compose" config --quiet
compose_for "$runtime_env" "$candidate_images" "$candidate_compose" up -d --wait --wait-timeout 240 postgres neo4j
if [ -s "$current_sha" ]; then
  backup_file="${deployment_root}/backups/agent-os-${release_sha}.sql.gz"
  compose_for "$runtime_env" "$candidate_images" "$candidate_compose" exec -T postgres \
    pg_dump --clean --if-exists --no-owner --username agent_os_uat_runtime agent_os_uat \
    | gzip -9 > "$backup_file"
  chmod 0600 "$backup_file"
  test -s "$backup_file"
  echo "PASS postgres-predeploy-backup"
fi
# Agno v3 moves runs into their own table. Freeze v2 writes, then apply the
# additive, idempotent migration with the candidate image before v3 serves.
compose_for "$runtime_env" "$candidate_images" "$candidate_compose" stop --timeout 30 agentos || true
migrate_candidate_database "$runtime_env" "$candidate_images" "$candidate_compose"
compose_for "$runtime_env" "$candidate_images" "$candidate_compose" up -d --wait --wait-timeout 180 agentos
if [ ! -s "$current_sha" ]; then
  compose_for "$runtime_env" "$candidate_images" "$candidate_compose" exec -T agentos \
    python -m scripts.seed_schedules
fi
verify_release "$runtime_env" "$candidate_images" "$candidate_compose" "$release_sha"

# Prove Docker restart recovery while preserving PostgreSQL-owned Schedule configuration.
compose_for "$runtime_env" "$candidate_images" "$candidate_compose" restart agentos
compose_for "$runtime_env" "$candidate_images" "$candidate_compose" up -d --wait --wait-timeout 180 agentos
verify_release "$runtime_env" "$candidate_images" "$candidate_compose" "$release_sha"

if [ -s "$current_runtime" ] && [ -s "$current_images" ] && [ -s "$current_compose" ] && [ -s "$current_sha" ]; then
  install -m 0600 "$current_runtime" "$previous_runtime"
  install -m 0640 "$current_images" "$previous_images"
  install -m 0640 "$current_compose" "$previous_compose"
  install -m 0640 "$current_sha" "$previous_sha"
fi
install -m 0600 "$runtime_env" "$current_runtime"
install -m 0640 "$candidate_images" "$current_images"
install -m 0640 "$candidate_compose" "$current_compose"
printf '%s\n' "$release_sha" > "$current_sha"
chmod 0640 "$current_sha"
sync "$current_runtime" "$current_images" "$current_compose" "$current_sha"
trap - ERR
echo "PASS deployed-agentos-release ${release_sha}"
