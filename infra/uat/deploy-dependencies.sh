#!/usr/bin/env bash

set -Eeuo pipefail

deployment_root="${DEPLOY_ROOT:?DEPLOY_ROOT is required}"
runtime_env="${RUNTIME_ENV:?RUNTIME_ENV is required}"
candidate_images="${CANDIDATE_IMAGES:?CANDIDATE_IMAGES is required}"
candidate_compose="${COMPOSE_FILE:?COMPOSE_FILE is required}"
release_sha="${RELEASE_SHA:?RELEASE_SHA is required}"
state_dir="${deployment_root}/state"

mkdir -p "$state_dir"
exec 9>"${deployment_root}/deploy.lock"
flock -n 9 || { echo "FAIL agentos-deploy-lock: another AgentOS deployment is running" >&2; exit 1; }

compose=(docker compose --env-file "$runtime_env" --env-file "$candidate_images" -f "$candidate_compose")

postgres_identity() {
  "${compose[@]}" exec -T postgres \
    psql --tuples-only --no-align --username agent_os_uat_runtime --dbname agent_os_uat \
      --command 'SELECT system_identifier FROM pg_control_system();'
}

neo4j_identity() {
  "${compose[@]}" exec -T neo4j sh -eu -c \
    'cypher-shell --non-interactive --format plain -u neo4j -p "${NEO4J_AUTH#*/}" \
      "SHOW DATABASES YIELD name, databaseID WHERE name = '\''neo4j'\'' RETURN databaseID"' \
    | tail -n 1 | tr -d '"\r'
}

"${compose[@]}" config --quiet
[ -z "$("${compose[@]}" ps -q agentos)" ] \
  || { echo "FAIL dependencies-only: AgentOS is already running" >&2; exit 1; }

"${compose[@]}" up -d --wait --wait-timeout 240 postgres neo4j

postgres_version="$("${compose[@]}" exec -T postgres \
  psql --tuples-only --no-align --username agent_os_uat_runtime --dbname agent_os_uat \
    --command 'SHOW server_version;')"
[[ "$postgres_version" = 17.* ]] \
  || { echo "FAIL postgres-version: expected 17.x, got $postgres_version" >&2; exit 1; }

postgres_before="$(postgres_identity)"
neo4j_before="$(neo4j_identity)"
[ -n "$postgres_before" ] || { echo "FAIL postgres-persistence-identity: missing" >&2; exit 1; }
[ -n "$neo4j_before" ] || { echo "FAIL neo4j-persistence-identity: missing" >&2; exit 1; }

postgres_container="$("${compose[@]}" ps -q postgres)"
neo4j_container="$("${compose[@]}" ps -q neo4j)"
python3 "$(dirname "$0")/verify-dependency-ports.py" \
  "$postgres_container" "$neo4j_container" "$UAT_LAN_BIND_ADDRESS" \
  "$POSTGRES_LAN_PORT" "$NEO4J_HTTP_LAN_PORT" "$NEO4J_BOLT_LAN_PORT"

docker inspect --format '{{range .Mounts}}{{println .Name .Destination}}{{end}}' "$postgres_container" \
  | grep -Fqx 'tidewise-agentos-uat-postgres-data /var/lib/postgresql/data'
docker inspect --format '{{range .Mounts}}{{println .Name .Destination}}{{end}}' "$neo4j_container" \
  | grep -Fqx 'tidewise-agentos-uat-neo4j-data /data'
docker inspect --format '{{range .Mounts}}{{println .Name .Destination}}{{end}}' "$neo4j_container" \
  | grep -Fqx 'tidewise-agentos-uat-neo4j-logs /logs'

"${compose[@]}" restart postgres neo4j
"${compose[@]}" up -d --wait --wait-timeout 240 postgres neo4j

postgres_container="$("${compose[@]}" ps -q postgres)"
neo4j_container="$("${compose[@]}" ps -q neo4j)"
python3 "$(dirname "$0")/verify-dependency-ports.py" \
  "$postgres_container" "$neo4j_container" "$UAT_LAN_BIND_ADDRESS" \
  "$POSTGRES_LAN_PORT" "$NEO4J_HTTP_LAN_PORT" "$NEO4J_BOLT_LAN_PORT"

[ "$(postgres_identity)" = "$postgres_before" ] \
  || { echo "FAIL postgres-restart-persistence: identity changed" >&2; exit 1; }
[ "$(neo4j_identity)" = "$neo4j_before" ] \
  || { echo "FAIL neo4j-restart-persistence: identity changed" >&2; exit 1; }
[ -z "$("${compose[@]}" ps -q agentos)" ] \
  || { echo "FAIL dependencies-only: AgentOS was started" >&2; exit 1; }

install -m 0640 "$candidate_images" "$state_dir/dependencies.images.env"
install -m 0640 "$candidate_compose" "$state_dir/dependencies.compose.yaml"
printf '%s\n' "$release_sha" > "$state_dir/dependencies.sha"
chmod 0640 "$state_dir/dependencies.sha"
sync "$state_dir/dependencies.images.env" "$state_dir/dependencies.compose.yaml" "$state_dir/dependencies.sha"

echo "PASS fresh-dgx-postgres-neo4j-dependencies ${release_sha}"
