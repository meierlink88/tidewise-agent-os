#!/usr/bin/env bash

set -euo pipefail

redact() {
  sed -E \
    -e 's/(Authorization: Bearer )[A-Za-z0-9._-]+/\1[REDACTED]/g' \
    -e 's#(postgresql[^:]*://[^:]+:)[^@]+@#\1[REDACTED]@#g' \
    -e 's/(API_KEY|TOKEN|SECRET|PASSWORD|DB_PASS)=([^[:space:]]+)/\1=[REDACTED]/gI' \
    -e 's/(MINIO_ACCESS_KEY|MINIO_SECRET_KEY|MINIO_ROOT_USER|MINIO_ROOT_PASSWORD)=([^[:space:]]+)/\1=[REDACTED]/gI' \
    -e "s#(NEO4J_AUTH[=:][[:space:]]*'?)[^'[:space:]]+#\1[REDACTED]#gI" \
    -e "s#(^|[[:space:]'\"])neo4j/[^'\"[:space:]]+#\1neo4j/[REDACTED]#g"
}

if [ "${1:-}" = --redact-stdin ]; then
  redact
  exit 0
fi

runtime_env="${RUNTIME_ENV:?RUNTIME_ENV is required}"
images_env="${CANDIDATE_IMAGES:?CANDIDATE_IMAGES is required}"
compose_file="${COMPOSE_FILE:?COMPOSE_FILE is required}"

compose=(docker compose --env-file "$runtime_env" --env-file "$images_env" -f "$compose_file")
echo "===== docker compose ps ====="
"${compose[@]}" ps 2>&1 | redact
echo "===== agentos logs ====="
"${compose[@]}" logs --no-color --tail 300 agentos 2>&1 | redact
echo "===== postgres logs ====="
"${compose[@]}" logs --no-color --tail 150 postgres 2>&1 | redact
echo "===== neo4j logs ====="
"${compose[@]}" logs --no-color --tail 150 neo4j 2>&1 | redact
echo "===== minio logs ====="
"${compose[@]}" logs --no-color --tail 150 minio 2>&1 | redact
